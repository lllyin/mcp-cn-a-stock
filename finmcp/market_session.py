"""市场时段：交易所的时刻表，以及上游数据围绕它的行为。

这个模块是**边界的单一事实源**。报告缓存的纪元划分和盘中资金流走哪条分支，都从
这里读——在它之前，同两个边界在 ``cache.py`` 和 ``research.py`` 各写了一遍
（``PRE_OPEN=09:15`` 对 ``hour==9 and minute>=15``，``BRANCH_FLIP=17:00`` 对
``10 <= hour <= 16``）。改一处不改另一处，纪元就会横跨"报告从抓页面版切成读接口版"
的那一刻，同一个纪元里出现两种形状的报告——而"命中与否不改变返回内容"正是报告缓存
赖以成立的前提。

## 交易所事实 vs 上游行为

交易所的时刻表是死的：09:30 开盘、11:30 午休、13:00 续盘、15:00 收盘。
**但上游不在这些时刻定稿**：

- 盘前：09:30 还没开盘，上游已经在更新当日数据了。
- 盘后：15:00 收了盘，东财的资金流页面还在整理，AkShare 的当日资金流行更晚才落地。

提前多少、延后多少取决于上游当时的行为，**会变**，所以那几个边界是配置项，
交易所的时刻表是常量。

```
        [warmup]      开盘                午休          续盘          收盘   [settle]      [final]
          09:15      09:30              11:30         13:00         15:00    15:30         16:00
 ─ CLOSED ──┼───── LIVE ─────────────────┼─buf─ LUNCH ──┼──── LIVE ────┼─ POSTCLOSE ─┼─LIVE─┼─buf─ CLOSED ─
```

设计说明见 docs/cache-design.md §2。
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional
from zoneinfo import ZoneInfo

from .config import (
    MARKET_EPOCH_BUFFER_MINUTES,
    MARKET_EPOCH_FINAL_TIME,
    MARKET_EPOCH_SETTLE_TIME,
    MARKET_EPOCH_WARMUP_TIME,
)

logger = logging.getLogger("finmcp")

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# ── 交易所事实：不是配置项 ────────────────────────────────────────

OPEN = datetime.time(9, 30)
LUNCH_START = datetime.time(11, 30)
LUNCH_END = datetime.time(13, 0)
CLOSE = datetime.time(15, 0)

#: warmup 不能早于这个点——再早就不是"上游提前更新"，是配错了。
_EARLIEST_WARMUP = datetime.time(7, 0)
#: final 不能晚于这个点，否则纪元会滑进第二天。
_LATEST_FINAL = datetime.time(23, 0)

PHASE_LIVE = "live"
PHASE_LUNCH = "lunch"
PHASE_POSTCLOSE = "postclose"
PHASE_CLOSED = "closed"


# ── 边界校验 ────────────────────────────────────────────────────


def _clamp(value: datetime.time, low: datetime.time, high: datetime.time,
           name: str) -> datetime.time:
    """夹回合法区间并告警。

    配错一个时刻不该让服务起不来——但也不能默默接受一个会算错的值，所以夹回去之后
    一定要留一条 WARNING。
    """
    if value < low:
        logger.warning("%s=%s 早于下界 %s，已按 %s 处理",
                       name, value.strftime("%H:%M"), low.strftime("%H:%M"),
                       low.strftime("%H:%M"))
        return low
    if value > high:
        logger.warning("%s=%s 晚于上界 %s，已按 %s 处理",
                       name, value.strftime("%H:%M"), high.strftime("%H:%M"),
                       high.strftime("%H:%M"))
        return high
    return value


def _resolve_boundaries() -> tuple:
    """把配置夹成一组自洽的边界。

    夹取有先后：final 先夹进 [收盘, 23:00]，settle 再夹进 [收盘, final]。反过来做
    两者会互相依赖，夹不出确定的结果。

    各自的上下界为什么是这些：

    - ``warmup`` **晚于开盘**会让 09:30→warmup 这段真在交易却被判成 CLOSED，
      于是把昨天纪元的数据当成今天的发出去。
    - ``settle`` **早于收盘**会把连续竞价的一段算进"完全复用"，而那时
      ``research.today_volume_est_ratio`` 还在动。
    - ``final`` **早于 settle** 会让两个纪元次序颠倒。
    """
    warmup = _clamp(MARKET_EPOCH_WARMUP_TIME, _EARLIEST_WARMUP, OPEN,
                    "MARKET_EPOCH_WARMUP_TIME")
    final = _clamp(MARKET_EPOCH_FINAL_TIME, CLOSE, _LATEST_FINAL,
                   "MARKET_EPOCH_FINAL_TIME")
    settle = _clamp(MARKET_EPOCH_SETTLE_TIME, CLOSE, final,
                    "MARKET_EPOCH_SETTLE_TIME")
    buffer = datetime.timedelta(minutes=max(0, min(MARKET_EPOCH_BUFFER_MINUTES, 60)))
    return warmup, settle, final, buffer


WARMUP_TIME, SETTLE_TIME, FINAL_TIME, BUFFER = _resolve_boundaries()


def _add(clock: datetime.time, delta: datetime.timedelta) -> datetime.time:
    return (datetime.datetime.combine(datetime.date(2000, 1, 1), clock) + delta).time()


#: 边界过后上游还要整理一会儿，这段单独给一个纪元 token，免得"还在整理"的那一版
#: 被当成定稿复用。
LUNCH_SETTLE = _add(LUNCH_START, BUFFER)
EVENING_SETTLE = _add(FINAL_TIME, BUFFER)


# ── 时钟 ────────────────────────────────────────────────────────


def now_shanghai(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    """把输入归一到上海时区。

    naive 的按上海墙钟解释（既有测试和调用方的约定），aware 的显式换算——
    否则一台按 UTC 配置的 Ubuntu 会把所有市场边界平移八小时。
    """
    current = datetime.datetime.now(SHANGHAI_TZ) if now is None else now
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ)


def _is_trading_day(day: datetime.date) -> bool:
    """延迟 import：本模块被 cache/research 早于 datasource 导入，模块级 import 会把
    datasource 的导入副作用（安装出站 HTTP 通道）提前，那是另一件事。
    """
    from .datasource import trading_calendar

    return trading_calendar.is_trading_day(day)


def _previous_trading_day(day: datetime.date) -> datetime.date:
    from .datasource import trading_calendar

    return trading_calendar.previous_trading_day(day)


# ── 对外 ────────────────────────────────────────────────────────


def phase_and_epoch(now: Optional[datetime.datetime] = None) -> tuple[str, str]:
    """返回 ``(阶段, 纪元 token)``。

    纪元是"重新生成会读到同样的上游数字、走同样的渲染分支"的一段时间。闭市纪元锚在
    刚结束的那个交易日上，所以周五傍晚到周一开盘是**一个**连续的纪元（64 小时）。

    非交易日整天都是 CLOSED——修之前用的是 ``weekday()>=5``，于是国庆 10-01~10-08
    被判成盘中，报告缓存退化成 30 秒 TTL，八天等于没有缓存。
    """
    local_now = now_shanghai(now)
    day = local_now.date()
    clock = local_now.replace(tzinfo=None).time()

    if not _is_trading_day(day) or clock < WARMUP_TIME:
        return PHASE_CLOSED, f"closed-{_previous_trading_day(day)}"
    if clock < LUNCH_START:
        return PHASE_LIVE, f"live-{day}"
    if clock < LUNCH_SETTLE:
        return PHASE_LIVE, f"lunch-open-{day}"
    if clock < LUNCH_END:
        return PHASE_LUNCH, f"lunch-{day}"
    if clock < SETTLE_TIME:
        return PHASE_LIVE, f"live-{day}"
    if clock < FINAL_TIME:
        return PHASE_POSTCLOSE, f"postclose-{day}"
    if clock < EVENING_SETTLE:
        return PHASE_LIVE, f"evening-open-{day}"
    return PHASE_CLOSED, f"closed-{day}"


def is_realtime_fund_flow_window(now: Optional[datetime.datetime] = None) -> bool:
    """现在该不该走浏览器抓资金流页面。

    ``warmup`` 之前上游还没开始更新当日数据，``final`` 之后 AkShare 的当日资金流行
    已经落地、读接口就够。这两个边界同时是纪元边界（见模块开头），一个纪元不会横跨
    这里的翻转点。

    非交易日一律 False。修之前这里只看时钟不看日期，代价实测过：2026-09-05 是周六，
    15:11 那一轮服务照样为一个不存在的交易日拉起 Chromium——66 次页面加载、
    65 次撞验证码，还把出口 IP 打热，连累了后面几轮的取数。
    """
    moment = now_shanghai(now)
    if not _is_trading_day(moment.date()):
        return False
    return WARMUP_TIME <= moment.replace(tzinfo=None).time() < FINAL_TIME


__all__ = [
    "BUFFER",
    "CLOSE",
    "FINAL_TIME",
    "LUNCH_END",
    "LUNCH_START",
    "OPEN",
    "PHASE_CLOSED",
    "PHASE_LIVE",
    "PHASE_LUNCH",
    "PHASE_POSTCLOSE",
    "SETTLE_TIME",
    "SHANGHAI_TZ",
    "WARMUP_TIME",
    "is_realtime_fund_flow_window",
    "now_shanghai",
    "phase_and_epoch",
]
