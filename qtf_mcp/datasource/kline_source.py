"""K 线这一维：请求类型、归一后的契约，以及它在平台层上的入口。

源本身在 ``platforms/`` 下（腾讯、新浪，以后的同花顺、雪球），这里只负责三件事：
定义请求长什么样、声明归一后的契约、把配置顺序喂给通用的 ``platform.resolve()``。
架构见 docs/data-provider-architecture.md。

**接一个新的 K 线源不用改这个文件**——写一个 ``platforms/<名字>.py``，在
``platforms/__init__.py`` import 一行，再把名字加进 ``KLINE_PROVIDERS``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from . import platform as pf
from .kline_frame import FALLBACK_FRAME_COLUMNS, _market_prefixed_symbol

logger = logging.getLogger("qtf_mcp")

CAPABILITY = "kline"
PROVIDER_ORDER_ENV = "KLINE_PROVIDERS"
#   tencent  个股/ETF/指数都覆盖，北交所大半不认
#   sina     覆盖腾讯不认的北交所代码，但 ETF 和创业板指是 JSONDecodeError
# 两家各补各的洞，所以顺序是"覆盖面大的在前，补漏的在后"。
DEFAULT_PROVIDER_ORDER = ("tencent", "sina")

#: 兼容旧名字：这些常量原先在本模块里，外部按 kline_source.X 取过。
UNSUPPORTED_ERRORS = pf.UNSUPPORTED_ERRORS


@dataclass(frozen=True)
class KlineRequest:
    """一次取数的全部输入。加字段不影响已有 provider。"""

    code: str            # 纯六位码
    start_date: str      # YYYY-MM-DD
    end_date: str        # YYYY-MM-DD
    adjust: str          # qfq | hfq | none
    symbol: Optional[str] = None     # SH600519 这种带市场前缀的

    @property
    def prefixed(self) -> str:
        """兜底源要的小写带前缀码，如 sh600519。"""
        return _market_prefixed_symbol(self.code, self.symbol)

    @property
    def fetch_start(self) -> str:
        """实际请求的起点，YYYYMMDD。

        比请求区间往前多取 20 个自然日：派生列（涨跌幅这些）需要区间之前那个交易
        日的收盘价，否则首行只能填 0，而 kline_daily 只请求一天、首行就是唯一一行。
        A 股最长假期约 8 个交易日，20 个自然日足够跨过去。
        """
        start = datetime.strptime(self.start_date, "%Y-%m-%d").date()
        return (start - timedelta(days=20)).strftime("%Y%m%d")

    @property
    def requested_start(self):
        return datetime.strptime(self.start_date, "%Y-%m-%d").date()


@dataclass(frozen=True)
class KlineResult:
    """取回来的行情，外加"谁给的"。

    provider 名字不是给日志看的装饰——编排要靠它决定补不补当天那根实时 bar。
    """

    frame: object          # pandas.DataFrame
    provider: str


def _honours_kline_contract(frame) -> bool:
    """归一后必须是带标准列的日线表。

    只判类型说明不了列对不对，而**列不对正是接一个新源最容易出的错**：各家的
    原始列序不一样，抄错一个位置报告里的最高价就成了收盘价，而两个数都在合理
    区间，肉眼看不出来。
    """
    columns = getattr(frame, "columns", None)
    if columns is None:
        return False
    return set(FALLBACK_FRAME_COLUMNS) <= set(columns)


pf.define_capability(CAPABILITY, _honours_kline_contract, describe="含标准列的日线表")


def configured_order() -> tuple:
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


def resolve(
    request: KlineRequest,
    *,
    order: Optional[tuple] = None,
    status: Optional[dict] = None,
) -> Optional[KlineResult]:
    """按配置顺序问每个平台，第一个给出非空结果的赢。"""
    resolved = pf.resolve(
        CAPABILITY,
        request,
        order=configured_order() if order is None else order,
        status=status,
    )
    if resolved is None:
        return None
    return KlineResult(frame=resolved.value, provider=resolved.platform)


def registered() -> tuple:
    return pf.registered(CAPABILITY)


def provider(name: str):
    """按名字取一个平台。给测试和排查用。"""
    return pf.get(name)


__all__ = [
    "CAPABILITY",
    "DEFAULT_PROVIDER_ORDER",
    "KlineRequest",
    "KlineResult",
    "PROVIDER_ORDER_ENV",
    "UNSUPPORTED_ERRORS",
    "configured_order",
    "provider",
    "registered",
    "resolve",
]
