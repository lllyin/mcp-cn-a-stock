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


def resolve(request: SectorFundFlowRequest, *, order: Optional[tuple] = None,
            status: Optional[dict] = None) -> Optional[SectorFundFlowBoard]:
    resolved = pf.resolve(
        CAPABILITY, request,
        order=configured_order() if order is None else order,
        status=status,
    )
    if resolved is None:
        return None
    board = dataclasses.replace(resolved.value, as_of=_as_of())
    return _with_levels(board)


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
