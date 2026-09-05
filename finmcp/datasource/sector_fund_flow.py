"""板块资金流：行业 / 概念 / 地域。

这一维回答的是个股资金流答不了的问题——报告说"茅台主力净流入 3.68亿"，但没有语境：
是白酒整个板块在被买，还是只有它。板块排行给的就是这个语境。

架构见 docs/architecture.md。平台在 platforms/ 下，这里只定义请求、
契约和配置接线。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from . import platform as pf
from . import sector_taxonomy, trading_calendar

logger = logging.getLogger("finmcp")

CAPABILITY = "sector_fund_flow"
PROVIDER_ORDER_ENV = "SECTOR_FUND_FLOW_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("eastmoney", "eastmoney_dataapi")

#: 板块类型。三个都是东财 m:90 下的子集，t 值分别是 2/3/1。
SECTOR_TYPES = ("industry", "concept", "region")
#: 统计口径。
PERIODS = ("today", "5d", "10d")


@dataclass(frozen=True)
class SectorFundFlowRequest:
    sector_type: str = "industry"
    period: str = "today"


@dataclass(frozen=True)
class SectorFlow:
    """一个板块的资金流。

    金额一律是**元**，占比是百分数（2.32 表示 2.32%）。归一到元而不是亿，是因为
    "亿"是渲染层的事——这一层保留原始精度，渲染时再折算。
    """

    name: str
    code: str = ""
    change_pct: Optional[float] = None
    main_net: Optional[float] = None
    main_pct: Optional[float] = None
    xl_net: Optional[float] = None      # 超大单
    l_net: Optional[float] = None       # 大单
    m_net: Optional[float] = None       # 中单
    s_net: Optional[float] = None       # 小单
    leader: str = ""                    # 主力净流入最大股
    #: 在行业分类里是第几级。None = 不知道（源没说，也没查到分级表）。
    #: 平台自己知道的话就在归一时填；东财不给这个字段，由 resolve() 用
    #: sector_taxonomy 补。缺了它，Top N 会把父子板块一起排进来，同一笔钱数两遍。
    level: Optional[int] = None


@dataclass(frozen=True)
class SectorFundFlowBoard:
    """一次板块资金流查询的全部结果。

    ``partial`` 标出这份数据是不是缺字段的降级版——东财的 dataapi 端点只给板块名和
    主力净额，没有涨跌幅和四档明细。渲染层据此决定要不要少画几列，而不是画一堆
    空格子让人以为数据丢了。
    """

    sectors: tuple
    sector_type: str
    period: str
    source: str = ""
    partial: bool = False
    #: 数据日期。东财这两个端点都不返回它，由 resolve() 按交易日历定：口径是
    #: "最近一个已开盘的交易日"。周末查出来必须是 09-04 而不是"今日"。
    as_of: Optional[datetime.date] = None
    #: 层级按谁的分类标准分的（``shenwan``）。空 = 这一批分不出层级。
    level_scheme: str = ""
    #: 这个板块类型**本来**有没有分级。概念和地域天然没有层级，那不是缺失；
    #: 行业有层级却没分出来才是。两者的措辞不一样，所以要分开记。
    levels_applicable: bool = False

    @property
    def levels_known(self) -> bool:
        return any(s.level is not None for s in self.sectors)

    def at_level(self, level: int) -> tuple:
        return tuple(s for s in self.sectors if s.level == level)


def _honours_contract(value) -> bool:
    return isinstance(value, SectorFundFlowBoard) and bool(value.sectors)


pf.define_capability(CAPABILITY, _honours_contract, describe="SectorFundFlowBoard")


def configured_order() -> tuple:
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


def _as_of(now: Optional[datetime.datetime] = None) -> datetime.date:
    """这批资金流是哪一天的。

    东财两个端点都不返回日期，只能按交易日历推：开盘之后是今天，否则是上一个交易日。
    这不是"猜"——资金流是按交易日结算的，非交易日拿到的必然是上一个交易日的存量。
    09:15 这个界和 ``is_realtime_fund_flow_window`` 用的是同一个。
    """
    moment = now or datetime.datetime.now()
    today = moment.date()
    if trading_calendar.is_trading_day(today) and moment.time() >= datetime.time(9, 15):
        return today
    return trading_calendar.previous_trading_day(today)


def _with_levels(board: SectorFundFlowBoard) -> SectorFundFlowBoard:
    """把层级补进每个板块。

    分级来自另一个能力（申万的行业分类），不是东财给的，所以补在维度这一层而不是
    平台里——平台只负责把**自己**的数据归一，跨能力的补全归维度管。
    """
    applicable = sector_taxonomy.applies_to(board.sector_type)
    if not applicable:
        return board
    taxonomy = sector_taxonomy.load(board.sector_type)
    if taxonomy is None:
        return dataclasses.replace(board, levels_applicable=True)
    return dataclasses.replace(
        board,
        sectors=tuple(dataclasses.replace(s, level=taxonomy.level_of(s.name))
                      for s in board.sectors),
        level_scheme=taxonomy.scheme,
        levels_applicable=True,
    )


# ── 缓存 ────────────────────────────────────────────────────────
#
# 缓存的是**上游那份 board**，不是渲染好的报告。理由是 key 空间：``top``(1-50) 和
# ``level``(1-2) 只影响渲染，按报告缓存就是 3 类 × 3 口径 × 50 × 2 = 900 个 key，
# 会把 512 条的上限撑爆、连带挤掉个股报告的条目。按 (类型, 口径) 缓存只有 9 个。
# 渲染是纯计算，每次重做的代价可以忽略。
#
# 复用 ReportCache 而不是自己开一个字典，图的是三样现成的东西：市场纪元（非交易日
# 一个纪元长达 64 小时）、盘中 TTL、以及 ``REPORT_CACHE_ENABLED=0`` 能一起关掉——
# 少了最后这条，prove_equivalence.py 的"关掉缓存再比对"就又变成假的了。


def _payload(board: SectorFundFlowBoard) -> dict:
    """board → 可 JSON 化的字典（磁盘层要）。"""
    return {
        "sectors": [dataclasses.asdict(s) for s in board.sectors],
        "sector_type": board.sector_type,
        "period": board.period,
        "source": board.source,
        "partial": board.partial,
        "as_of": board.as_of.isoformat() if board.as_of else None,
        "level_scheme": board.level_scheme,
        "levels_applicable": board.levels_applicable,
    }


def _from_payload(payload) -> Optional[SectorFundFlowBoard]:
    """字典 → board。形状不对就当作没缓存过——旧版本写下的条目不该让这次查询挂掉。"""
    if not isinstance(payload, dict) or not payload.get("sectors"):
        return None
    try:
        as_of = payload.get("as_of")
        return SectorFundFlowBoard(
            sectors=tuple(SectorFlow(**s) for s in payload["sectors"]),
            sector_type=payload["sector_type"],
            period=payload["period"],
            source=payload.get("source", ""),
            partial=bool(payload.get("partial")),
            as_of=datetime.date.fromisoformat(as_of) if as_of else None,
            level_scheme=payload.get("level_scheme", ""),
            levels_applicable=bool(payload.get("levels_applicable")),
        )
    except (TypeError, ValueError, KeyError):
        logger.debug("板块资金流缓存条目形状不对，忽略", exc_info=True)
        return None


def _cache_key(request: SectorFundFlowRequest):
    from ..cache import build_key

    return build_key("sector_fund_flow", request.sector_type,
                     {"period": request.period})


# ── 熔断 ────────────────────────────────────────────────────────
#
# 主源（push2）挂掉时必须有人拦一下，否则每次调用都要先付一遍它的超时。这一层
# 尤其需要：降级源的结果按设计**不进缓存**（缓住就是整个纪元只有两列），所以没有
# 熔断就是每次调用都重试一遍死掉的主源。
#
# 只给主源装，降级源不装：装了也只是把"少两列"变成"整层没有"，换不到东西——
# 和 cn_stock_source 里"K 线装、资金流接口不装"是同一条判据。

_breakers: dict = {}


def _breaker_for(name: str):
    from .cn_stock_source import SourceBreaker
    from ..config import SOURCE_BREAKER_COOLDOWN_SECONDS, SOURCE_BREAKER_OPEN_AFTER_FAILURES

    if name != "eastmoney":
        return None
    breaker = _breakers.get(name)
    if breaker is None:
        breaker = _breakers[name] = SourceBreaker(
            f"{name}_sector_fund_flow",
            SOURCE_BREAKER_OPEN_AFTER_FAILURES,
            SOURCE_BREAKER_COOLDOWN_SECONDS,
        )
    return breaker


def resolve(request: SectorFundFlowRequest, *, order: Optional[tuple] = None,
            status: Optional[dict] = None) -> Optional[SectorFundFlowBoard]:
    from ..cache import get_report_cache

    cache = get_report_cache()
    key = _cache_key(request)
    cached = _from_payload(cache.get(key)) if cache is not None else None
    if cached is not None:
        logger.debug("板块资金流命中缓存 sector_type=%s period=%s 纪元=%s",
                     request.sector_type, request.period, key.epoch)
        return cached

    resolved = pf.resolve(
        CAPABILITY, request,
        order=configured_order() if order is None else order,
        status=status,
        breaker_for=_breaker_for,
    )
    if resolved is None:
        return None
    board = _with_levels(dataclasses.replace(resolved.value, as_of=_as_of()))

    # 降级源的结果不进缓存。它字段少一半，而非交易日一个纪元长达 64 小时——缓住
    # 就是整个周末都只有两列，哪怕主源十秒后就恢复了。不缓的代价是下次重试一遍
    # 主源，正是想要的行为。同 is_cacheable_report 的道理：别把一次瞬时降级腌起来。
    if cache is not None and not board.partial:
        cache.put(key, _payload(board))
    return board


__all__ = [
    "CAPABILITY",
    "DEFAULT_PROVIDER_ORDER",
    "PERIODS",
    "PROVIDER_ORDER_ENV",
    "SECTOR_TYPES",
    "SectorFlow",
    "SectorFundFlowBoard",
    "SectorFundFlowRequest",
    "configured_order",
    "resolve",
]
