"""板块资金流：行业 / 概念 / 地域。

这一维回答的是个股资金流答不了的问题——报告说"茅台主力净流入 3.68亿"，但没有语境：
是白酒整个板块在被买，还是只有它。板块排行给的就是这个语境。

架构见 docs/data-provider-architecture.md。平台在 platforms/ 下，这里只定义请求、
契约和配置接线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from . import platform as pf

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


def _honours_contract(value) -> bool:
    return isinstance(value, SectorFundFlowBoard) and bool(value.sectors)


pf.define_capability(CAPABILITY, _honours_contract, describe="SectorFundFlowBoard")


def configured_order() -> tuple:
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


def resolve(request: SectorFundFlowRequest, *, order: Optional[tuple] = None,
            status: Optional[dict] = None) -> Optional[SectorFundFlowBoard]:
    resolved = pf.resolve(
        CAPABILITY, request,
        order=configured_order() if order is None else order,
        status=status,
    )
    return None if resolved is None else resolved.value


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
