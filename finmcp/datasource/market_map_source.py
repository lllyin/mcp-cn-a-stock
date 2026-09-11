"""市场云图基础数据：代码、名称、价格、涨跌幅、市值、成交额、主力资金流、行业。

``size`` 保存上游原始金额（元），由 ``size_field`` 声明是流通市值还是成交额，
不代表格子面积。缺值保留为 None；布局、面积和配色由调用方计算。

## 分页是这一维的主要风险

上游单页硬上限 100 行（``pz`` 给到 6000 也只回 100，2026-09-09 实测），所以全 A
5911 只要 60 次请求，正是最容易触发东财风控的形状。三条对策：

1. **筛板块**。``fs`` 直接编码交易所和板块，筛选是少取而不是取回来再滤：
   科创板 7 页、创业板 15 页、上证主板 19 页，比全 A 的 60 页低一个量级。
2. **缓存整份快照**，一次取数服务所有调用方。裁剪和渲染都是纯计算，不进缓存键。
3. **取到几页就返回几页**，并在 ``missing_pages`` 里说清缺了多少。静默返回一个更小的
   池子，调用方会拿它当全集去算占比（AGENTS §一）。

架构见 docs/architecture.md。平台在 platforms/ 下，这里只定义请求、契约和配置接线。
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import cache, market_session
from ..config import CACHE_INTRADAY_TTL_SECONDS
from . import platform as pf
from . import trading_calendar

logger = logging.getLogger("finmcp")

CAPABILITY = "market_map"
PROVIDER_ORDER_ENV = "MARKET_MAP_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("eastmoney", "eastmoney_delay")

#: 板块代码 → (中文名, 东财 fs 表达式)。
#:
#: **用代码不用中文**：「上证」含不含科创板是有歧义的，而这个歧义要在参数名上解掉，
#: 不能留给文档。这里 ``sse`` 只指上证主板，科创板是独立的 ``star``。
#:
#: 2026-09-09 实测各板块 total：1846 + 621 + 1640 + 1450 + 354 = 5911，与 ``all``
#: 返回的 total 逐位相等——说明这五个 fs 不重不漏。
BOARDS: dict[str, tuple[str, str]] = {
    "all": ("全部A股", "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"),
    "sse": ("上证主板", "m:1+t:2"),
    "star": ("科创板", "m:1+t:23"),
    "szse": ("深证主板", "m:0+t:6"),
    "chinext": ("创业板", "m:0+t:80"),
    "bse": ("北交所", "m:0+t:81+s:2048"),
}

#: 返回的金额字段：float_cap 流通市值，turnover 当日成交额；单位均为元。
SIZE_FIELDS = ("float_cap", "turnover")

CACHE_NAMESPACE = "market_map"


@dataclass(frozen=True)
class MarketMapRequest:
    board: str = "all"
    size: str = "float_cap"

    @property
    def board_name(self) -> str:
        return BOARDS[self.board][0]

    @property
    def selector(self) -> str:
        """上游的板块选择表达式。筛选推到上游，不在本地过滤。"""
        return BOARDS[self.board][1]


@dataclass(frozen=True)
class MarketMapStock:
    symbol: str            # 带市场前缀，SH600519 这种
    name: str
    change_pct: Optional[float]
    size: Optional[float]  # 口径见 MarketMap.size_field
    sector: str            # 所属行业，上游给的原名
    last: Optional[float] = None
    float_cap: Optional[float] = None
    amount_yuan: Optional[float] = None
    main_net: Optional[float] = None
    main_pct: Optional[float] = None


@dataclass(frozen=True)
class MarketMap:
    """归一后的云图数据。**只有基础事实**，聚合值一律不放。"""

    stocks: tuple = ()
    board: str = "all"
    size_field: str = "float_cap"
    #: 上游声称一共有多少只。和 len(stocks) 不等就是有页没取到。
    upstream_total: int = 0
    #: 没取到的页号。空表示这一份是完整的。
    missing_pages: tuple = ()
    as_of: Optional[datetime.date] = None
    source: str = ""
    warnings: tuple = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return not self.missing_pages and len(self.stocks) >= self.upstream_total


def _honours_contract(value) -> bool:
    """校验成员结构，不以是否能绘图作为取数成功条件。缺失金额按 None 返回。"""
    if not isinstance(value, MarketMap) or not value.stocks:
        return False
    return all(isinstance(s, MarketMapStock) and s.symbol and s.sector
               for s in value.stocks)


pf.define_capability(CAPABILITY, _honours_contract, describe="MarketMap")

cache.register_namespace(cache.Namespace(
    name=CACHE_NAMESPACE,
    # 六个板块 × 两个加权字段 = 12 个键，留一倍余量。
    max_entries=24,
    epoch_bound=True,
    ttl_seconds=CACHE_INTRADAY_TTL_SECONDS,
    disk=True,
    encode=lambda value: _payload(value),
    decode=lambda payload: _from_payload(payload),
    # 不完整的那份不缓：缓了它，之后每次调用都拿到同一个缺页的池子，而缺页
    # 多半是上游一时的事（AGENTS §一）。
    cacheable=lambda value, key: isinstance(value, MarketMap) and value.complete,
))


def configured_order() -> tuple:
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


def _payload(value: MarketMap) -> dict:
    return {
        "board": value.board, "size_field": value.size_field,
        "upstream_total": value.upstream_total,
        "missing_pages": list(value.missing_pages),
        "as_of": value.as_of.isoformat() if value.as_of else None,
        "source": value.source, "warnings": list(value.warnings),
        "stocks": [dataclasses.asdict(s) for s in value.stocks],
    }


def _from_payload(payload) -> Optional[MarketMap]:
    if not isinstance(payload, dict) or not isinstance(payload.get("stocks"), list):
        return None
    try:
        stocks = tuple(MarketMapStock(**row) for row in payload["stocks"])
    except (IndexError, TypeError):
        return None
    as_of = payload.get("as_of")
    return MarketMap(
        stocks=stocks, board=payload.get("board", "all"),
        size_field=payload.get("size_field", "float_cap"),
        upstream_total=payload.get("upstream_total", 0),
        missing_pages=tuple(payload.get("missing_pages") or ()),
        as_of=datetime.date.fromisoformat(as_of) if as_of else None,
        source=payload.get("source", ""),
        warnings=tuple(payload.get("warnings") or ()),
    )


def _as_of(now: Optional[datetime.datetime] = None) -> datetime.date:
    """这批行情是哪一天的。

    上游不返回日期，只能按交易日历推：开盘之后是今天，否则是上一个交易日。
    界和 sector_fund_flow._as_of 保持一致。
    """
    moment = market_session.now_shanghai(now)
    today = moment.date()
    clock = moment.replace(tzinfo=None).time()
    if trading_calendar.is_trading_day(today) and clock >= market_session.WARMUP_TIME:
        return today
    return trading_calendar.previous_trading_day(today)


def _fetch(request: MarketMapRequest, order, status) -> Optional[MarketMap]:
    resolved = pf.resolve(
        CAPABILITY, request,
        order=configured_order() if order is None else order,
        status=status,
    )
    if resolved is None:
        return None
    value: MarketMap = resolved.value
    return dataclasses.replace(value, as_of=_as_of(), source=resolved.source)


def resolve(request: MarketMapRequest, *, order: Optional[tuple] = None,
            status: Optional[dict] = None) -> Optional[MarketMap]:
    """缓存的是**整份快照**，不是裁剪或渲染的结果。

    裁剪（只要涨跌幅头尾几个行业）和渲染都是纯计算，重做不要钱；而按裁剪参数缓存
    会让键数乘上一个维度，还会让同一份上游数据在缓存里存好几遍。
    """
    if request.board not in BOARDS:
        raise ValueError(f"board 必须是 {'/'.join(BOARDS)} 之一")
    if request.size not in SIZE_FIELDS:
        raise ValueError(f"size 必须是 {'/'.join(SIZE_FIELDS)} 之一")
    entry = cache.get_or_load(
        CACHE_NAMESPACE, f"{request.board}:{request.size}",
        lambda: _fetch(request, order, status),
    )
    return None if entry is None else entry.value


def registered() -> tuple:
    return pf.registered(CAPABILITY)


__all__ = [
    "BOARDS",
    "CACHE_NAMESPACE",
    "CAPABILITY",
    "DEFAULT_PROVIDER_ORDER",
    "MarketMap",
    "MarketMapRequest",
    "MarketMapStock",
    "PROVIDER_ORDER_ENV",
    "SIZE_FIELDS",
    "configured_order",
    "registered",
    "resolve",
]
