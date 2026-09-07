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

取不到日历就逐级往下退，最后退到"周一到周五算交易日"——也就是接入之前的行为,
**永远不比现在差**。这条降级路径不是藏在代码里的 try/except，而是配置里看得见的
一行 ``TRADING_CALENDAR_PROVIDERS=sina,holiday_cn,weekday``，于是每一级都能被单独
关掉、单独测试。三级的差别是量出来的:

    sina        交易所公布的交易日名单本身，最权威
    holiday_cn  国务院放假安排换算而来，不含交易所临时休市，但节假日全对
    weekday     只知道周一到周五，把春节、国庆整段算成交易日

中间那一级补的是实打实的落差:2015 年以来 32 个长假，直接退到 weekday 会把最长的
误当成 6 个交易日，而 holiday_cn 把这 32 个全部算对。
"""

from __future__ import annotations

import bisect
import datetime
import functools
import logging
from dataclasses import dataclass
from typing import Optional

from .. import cache
from ..config import CACHE_CALENDAR_TTL_SECONDS
from . import platform as pf

logger = logging.getLogger("finmcp")

CAPABILITY = "trading_calendar"
PROVIDER_ORDER_ENV = "TRADING_CALENDAR_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("sina", "holiday_cn", "weekday")


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
# holiday_cn（国务院放假安排换算）、weekday（纯计算的兜底）。
# 这里只留契约和对外的判断函数。


# ── 取数与缓存 ──────────────────────────────────────────────────

# 缓存这一维：TTL 型（和交易时段无关，日历一年才发布一次），落盘。
# 落盘的理由和分级表一样：有效期以天计，而进程重启是分钟级的事——不落盘等于
# 每次重启都重付一次上游。
#
# ## 为什么不加后台刷新线程（调研过，结论是不加）
#
# 现在的行为已经是"每天第一次查询时刷一次，失败就继续用旧的"：TTL 86400 秒管
# "每天一次"，``max_age_seconds`` 管"上游挂了还能用多久"，单飞管"并发只取一份"，
# 落盘管"重启不重付"。缺的只有一点——刷新是**阻塞**首个调用方的，不是后台静默。
#
# 那一下值多少钱，量过：sina 363 ms、holiday_cn 兜底 1082 ms、weekday 0 ms，
# **一天一次**。对照 brief 的 P90 是 50 秒级。也就是说后台化能省的是"一天一次、
# 几百毫秒"，而代价是一个常驻线程或任务（AGENTS.md §三 明确要求避免无界线程），
# 外加一个新的失败模式：刷新任务静默死掉之后，日历会一路旧到 max_age 才被发现。
#
# 收益说不清到值得的量级，副作用是确定的，所以不做（AGENTS.md §六）。
# 同理也没有另起一层"按年缓存 holiday-cn 原始 JSON"：那些年份文件的解析结果已经
# 落在这个命名空间的磁盘缓存里了，再加一层是重复。
CACHE_NAMESPACE = "calendar"

cache.register_namespace(cache.Namespace(
    name=CACHE_NAMESPACE,
    max_entries=1,
    epoch_bound=False,
    ttl_seconds=CACHE_CALENDAR_TTL_SECONDS,
    # 日历提前一年公布，取不到时旧的照样准。30 天是"这份名单还没过期到不能用"
    # 的宽松上限；真超了就退回按星期判断，也就是接入日历之前的行为。
    max_age_seconds=30 * 86400,
    disk=True,
    encode=lambda cal: {
        "days": sorted(d.isoformat() for d in cal.days),
        "covers_through": cal.covers_through.isoformat(),
        "source": cal.source,
    },
    decode=lambda payload: Calendar(
        days=frozenset(datetime.date.fromisoformat(d) for d in payload["days"]),
        covers_through=datetime.date.fromisoformat(payload["covers_through"]),
        source=payload.get("source", ""),
    ),
))


def _fetch() -> Optional[Calendar]:
    order = pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)
    resolved = pf.resolve(CAPABILITY, CalendarRequest(), order=order)
    calendar = resolved.value if resolved is not None else None
    # 空名单只有 weekday 兜底平台才是合法的（它的空集就是"我不知道，按星期算"）；
    # 别的源给空名单是没取到，不能当成"这一年没有交易日"。
    if calendar is not None and not calendar.days and calendar.source != "weekday":
        return None
    return calendar


def load(*, force: bool = False) -> Optional[Calendar]:
    """取一份日历。取不到就返回 None，调用方退回按星期判断。

    这一层不抛异常——日历是个优化，它挂了不该让报告挂掉。单飞和旧值兜底由
    缓存层内建：并发只取一份，上游挂了用 30 天内的旧名单（日历提前一年公布，
    旧的照样准）。
    """
    if force:
        cache.cache_for(CACHE_NAMESPACE).clear()
    entry = cache.get_or_load(CACHE_NAMESPACE, "cn", _fetch)
    calendar = None if entry is None else entry.value
    if calendar is not None:
        logger.debug(
            "交易日历来源=%s 覆盖至=%s 天数=%s 新鲜=%s",
            calendar.source, calendar.covers_through, len(calendar.days), entry.fresh,
        )
    return calendar


def reset_cache() -> None:
    """清掉缓存。给测试和排查用。"""
    cache.cache_for(CACHE_NAMESPACE).clear()


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


@functools.lru_cache(maxsize=2)
def _sorted_days(days: frozenset) -> tuple:
    """名单排好序的版本，给 bisect 用。frozenset 自己缓存 hash，所以查这个表很便宜。"""
    return tuple(sorted(days))


def missing_trading_days(start: datetime.date, end: datetime.date,
                         calendar: Optional[Calendar] = None) -> int:
    """开区间 ``(start, end)`` 里还剩几个交易日。

    用途是问"相邻两根 K 线之间，这个源少给了几根"。**长假在这里天然是 0**，所以
    调用方不需要"多少个自然日算长假"这种代理阈值——那种阈值 2026-09-07 之前是
    15 个自然日，而实测春节能到 11 个自然日，只剩 4 天余量。

    日历取不到、或者名单没盖住这一段时退回按星期数。实测 2015 年以来最长的长假
    （春节 11 个自然日）按星期会被数成 6，所以调用方的阈值只要大于 6，降级状态下
    长假仍然不会被当成缺口。
    """
    if end <= start:
        return 0
    cal = calendar if calendar is not None else load()
    if cal is not None and cal.days:
        days = _sorted_days(cal.days)
        # 上下边界都要查。``knows()`` 只管上边界，而一份只有今年的名单会把去年的
        # 交易日全判成"不开市"——那样缺口检查会静默失效，比没有更糟。
        if days[0] <= start and cal.knows(end):
            return max(0, bisect.bisect_left(days, end) - bisect.bisect_right(days, start))
    count, cursor = 0, start + datetime.timedelta(days=1)
    while cursor < end:
        if _fallback(cursor):
            count += 1
        cursor += datetime.timedelta(days=1)
    return count


__all__ = [
    "CAPABILITY",
    "Calendar",
    "CalendarRequest",
    "DEFAULT_PROVIDER_ORDER",
    "PROVIDER_ORDER_ENV",
    "is_trading_day",
    "load",
    "missing_trading_days",
    "next_trading_day",
    "previous_trading_day",
    "reset_cache",
    "trading_days",
]
