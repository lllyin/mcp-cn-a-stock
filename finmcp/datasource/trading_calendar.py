"""A 股交易日历。

这是"平台 → 能力 → 归一"这套架构（docs/architecture.md）落地的第一个
能力，选它打头是因为它是全新维度、没有等价性包袱，正好用来验证抽象。

## 为什么需要它

在它之前，代码里判"今天开不开市"靠 ``weekday() < 5``，四个地方各写各的，其中一处
连星期都不看。代价是实打实的：

- ``research.is_realtime_fund_flow_window()`` 只看时钟，于是 2026-09-05（周六）
  15:11 那一轮，服务为一个根本不存在的交易日拉起 Chromium 抓资金流——66 次页面
  加载、65 次撞上验证码，还把出口 IP 打热，连累了后面几轮的取数。
- ``cache.market_phase()`` 用 ``weekday()>=5``，于是国庆 10-01~10-08 这种整周的
  节假日会被判成盘中，报告缓存退化成 30 秒 TTL，八天里几乎等于没有缓存。
- ``cache._previous_weekday()`` 回退到"上一个工作日"，长假里这个锚点天天在变，
  磁盘缓存的纪元每天作废一次。
- ``market_breadth`` 的 TTL 同理，节假日 15s 而不是 300s，上游请求多 20 倍。

节假日历（比如 timor.tech 那种）解决不了这件事：它不知道调休上班日不开市，也不
知道交易所临时休市。要的是**交易所的交易日名单**本身。

## 降级不是 if/else，是一个平台

取不到日历就退回"周一到周五算交易日"——也就是接入之前的行为，**永远不比现在差**。
这条降级路径在这里不是藏在代码里的 try/except，而是一个叫 ``weekday`` 的内建平台，
配成 ``TRADING_CALENDAR_PROVIDERS=sina,weekday``。降级从代码里的暗礁变成配置里
看得见的一行，也就能被单独关掉、单独测试。
"""

from __future__ import annotations

import datetime
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from ..config import TRADING_CALENDAR_TTL_SECONDS
from . import platform as pf

logger = logging.getLogger("finmcp")

CAPABILITY = "trading_calendar"
PROVIDER_ORDER_ENV = "TRADING_CALENDAR_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("sina", "weekday")


# ── 契约 ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CalendarRequest:
    """要哪一段的交易日。

    ``through`` 给的是需要覆盖到哪天——平台据此判断自己够不够用。问 2027 年的事
    而日历只到 2026 年底时，宁可让下一个平台接手，也不要拿"名单里没有"当成
    "那天不开市"。
    """

    through: Optional[datetime.date] = None


@dataclass(frozen=True)
class Calendar:
    """一份交易日名单，外加它覆盖到哪天。

    ``covers_through`` 是这个契约里最关键的字段：没有它，"2027-01-05 不在名单里"
    会被误读成"那天不开市"，而真相是"日历还没发布到那天"。所有查询超出这个边界时
    都必须显式说不知道，不能猜。
    """

    days: frozenset
    covers_through: datetime.date
    source: str = ""

    def contains(self, day: datetime.date) -> bool:
        return day in self.days

    def knows(self, day: datetime.date) -> bool:
        return day <= self.covers_through


#: 这个能力归一后的结构。谁来提供 trading_calendar 都得返回 Calendar——
#: resolve() 会实际校验，返回别的东西会被当成"没给出结果"降级到下一个平台。
pf.define_capability(CAPABILITY, Calendar)


# 平台实现在 platforms/ 下：新浪（同时提供 kline，是同一个平台的两种能力）、
# weekday（纯计算的兜底）。这里只留契约和对外的判断函数。


# ── 取数与缓存 ──────────────────────────────────────────────────

_lock = threading.Lock()
_cached: Optional[Calendar] = None
_cached_at: float = 0.0


def _now() -> float:
    import time

    return time.monotonic()


def load(*, force: bool = False) -> Optional[Calendar]:
    """取一份日历，进程内缓存 ``TRADING_CALENDAR_TTL_SECONDS``。

    取不到就返回 None，调用方退回按星期判断。这一层不抛异常——日历是个优化，
    它挂了不该让报告挂掉。
    """
    global _cached, _cached_at
    with _lock:
        if not force and _cached is not None and _now() - _cached_at < TRADING_CALENDAR_TTL_SECONDS:
            return _cached
    order = pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)
    resolved = pf.resolve(CAPABILITY, CalendarRequest(), order=order)
    calendar = resolved.value if resolved is not None else None
    if calendar is not None and not calendar.days and calendar.source != "weekday":
        calendar = None
    with _lock:
        _cached, _cached_at = calendar, _now()
    if calendar is not None:
        logger.debug(
            "交易日历来源=%s 覆盖至=%s 天数=%s",
            calendar.source, calendar.covers_through, len(calendar.days),
        )
    return calendar


def reset_cache() -> None:
    """清掉进程内缓存。给测试和排查用。"""
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0


# ── 对外的判断函数 ──────────────────────────────────────────────
#
# 这四个是全项目唯一该被调用的入口。任何地方再写 weekday() < 5 都是 bug。


def _fallback(day: datetime.date) -> bool:
    """没有日历时的判断：周一到周五。就是接入日历之前的行为。"""
    return day.weekday() < 5


def is_trading_day(day: datetime.date, calendar: Optional[Calendar] = None) -> bool:
    """这天开不开市。

    三种情况都退回按星期判断，而且都不算错：日历取不到、日历还没覆盖到这天、
    用的是 ``weekday`` 兜底平台。三者的共同点是"我不知道"，而按星期判断是这个项目
    在有日历之前一直在用的答案。
    """
    cal = calendar if calendar is not None else load()
    if cal is None or not cal.days or not cal.knows(day):
        return _fallback(day)
    return cal.contains(day)


def previous_trading_day(day: datetime.date, calendar: Optional[Calendar] = None) -> datetime.date:
    """``day`` 之前最近的一个交易日（不含当天）。"""
    cal = calendar if calendar is not None else load()
    cursor = day - datetime.timedelta(days=1)
    # 上限 30 天：A 股最长的连续休市是春节，不到两周；30 天既够用，
    # 又保证日历异常时这个循环一定会停。
    for _ in range(30):
        if is_trading_day(cursor, cal):
            return cursor
        cursor -= datetime.timedelta(days=1)
    return cursor


def next_trading_day(day: datetime.date, calendar: Optional[Calendar] = None) -> datetime.date:
    """``day`` 之后最近的一个交易日（不含当天）。"""
    cal = calendar if calendar is not None else load()
    cursor = day + datetime.timedelta(days=1)
    for _ in range(30):
        if is_trading_day(cursor, cal):
            return cursor
        cursor += datetime.timedelta(days=1)
    return cursor


def trading_days(start: datetime.date, end: datetime.date,
                 calendar: Optional[Calendar] = None) -> list:
    """闭区间 ``[start, end]`` 内的交易日。"""
    cal = calendar if calendar is not None else load()
    out, cursor = [], start
    while cursor <= end:
        if is_trading_day(cursor, cal):
            out.append(cursor)
        cursor += datetime.timedelta(days=1)
    return out


__all__ = [
    "CAPABILITY",
    "Calendar",
    "CalendarRequest",
    "DEFAULT_PROVIDER_ORDER",
    "PROVIDER_ORDER_ENV",
    "is_trading_day",
    "load",
    "next_trading_day",
    "previous_trading_day",
    "reset_cache",
    "trading_days",
]
