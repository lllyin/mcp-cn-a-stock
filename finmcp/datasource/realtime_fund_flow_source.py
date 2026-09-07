"""无页面标的的盘中实时资金流。

报告里"资金流向"一段的当日五行，有页面的标的由浏览器加载 zjlx 页面拿到；科创50 这类
没有页面的标的，原先只能等 API 日线里出现当天那一行——而日线盘中没有当天，push2his 与
push2delay 都一样（2026-09-07 实测），于是盘中永远是"暂无实时资金流向"。

这一层补的就是那条路：push2delay 主机上的**分钟线** ``fflow/kline/get?klt=1`` 盘中可用，
每分钟一行，最后一行就是当日累计的主力、小单、中单、大单、超大单净流入，正是 zjlx 页面
"今日"栏背后的接口。2026-09-07 10:16 实测科创50 46 行，上证指数、茅台同样有；出口通畅时能到
push2delay（它不在伪装通道的接管名单里）。接口不给净占比，渲染层用净流入除以当日成交额算，
上证指数核对：43.28 亿 / 3181.89 亿 = 1.36%，页面写 1.35%。

先只给没有页面的标的用（渲染层在 ``get_realtime_fund_flow_target`` 返回 None 时才来问）；
将来页面被拒时也可以做备源，那是编排层的决定，这里不管。

架构见 docs/architecture.md：请求复用 ``fund_flow_source.FundFlowRequest``（同一个 secid
规则），平台在 platforms/eastmoney.py，这里只定义契约、缓存和配置接线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .. import cache
from ..config import CACHE_INTRADAY_TTL_SECONDS
from . import platform as pf
from .fund_flow_source import FundFlowRequest

logger = logging.getLogger("finmcp")

CAPABILITY = "realtime_fund_flow"
PROVIDER_ORDER_ENV = "REALTIME_FUND_FLOW_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("eastmoney_delay",)


@dataclass(frozen=True)
class RealtimeFundFlow:
    """当日累计到 ``time`` 为止的五档净流入，单位元。"""

    time: str                 # 最后一根分钟线的时刻，如 2026-09-07 10:16
    main_net: float
    xl_net: float             # 超大单
    l_net: float              # 大单
    m_net: float              # 中单
    s_net: float              # 小单
    name: str = ""
    source: str = ""

    def rows(self) -> tuple:
        """按报告里的顺序：主力、超大单、大单、中单、小单。"""
        return (
            ("主力", self.main_net),
            ("超大单", self.xl_net),
            ("大单", self.l_net),
            ("中单", self.m_net),
            ("小单", self.s_net),
        )


pf.define_capability(CAPABILITY, RealtimeFundFlow)

# 盘中 TTL 型、跟市场纪元走：同一批指数 brief / medium / full 三个工具先后各渲染一次，
# 没有它就是三次请求。值很小，不落盘。
CACHE_NAMESPACE = "realtime_fund_flow"

cache.register_namespace(cache.Namespace(
    name=CACHE_NAMESPACE,
    max_entries=64,
    epoch_bound=True,
    ttl_seconds=CACHE_INTRADAY_TTL_SECONDS,
    disk=False,
))


def configured_order() -> tuple:
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


def _fetch(request: FundFlowRequest, status: Optional[dict]) -> Optional[RealtimeFundFlow]:
    resolved = pf.resolve(CAPABILITY, request, order=configured_order(), status=status)
    if resolved is None:
        return None
    flow = resolved.value
    return flow if flow.source else RealtimeFundFlow(**{**flow.__dict__, "source": resolved.source})


def resolve(request: FundFlowRequest, *, status: Optional[dict] = None) -> Optional[RealtimeFundFlow]:
    """取一份当日累计资金流。取不到返回 None，调用方按"暂无"处理，不抛。"""
    entry = cache.get_or_load(CACHE_NAMESPACE, request.secid, lambda: _fetch(request, status))
    return None if entry is None else entry.value


__all__ = [
    "CAPABILITY",
    "DEFAULT_PROVIDER_ORDER",
    "PROVIDER_ORDER_ENV",
    "RealtimeFundFlow",
    "configured_order",
    "resolve",
]
