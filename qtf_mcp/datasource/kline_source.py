"""K 线的可插拔取数层。

和 ``basic_info`` / ``intraday_quote`` 同一套架构（AGENTS.md §二）：provider 注册进
表里，启用哪些、按什么顺序由 ``KLINE_PROVIDERS`` 决定，前一个没给出结果就试下一个。

**接一个新源（同花顺、雪球……）要做的全部事情**：写一个 ``KlineProvider`` 子类，
在模块末尾 ``register()`` 一行。不改调用链、不改别的 provider、不加 if/else。
新源想只覆盖一部分标的，就实现 ``supports()``——返回 False 的标的直接跳过，
连请求都不发。

为什么要有 ``supports()`` 而不是靠 try/except 兜：腾讯对北交所的代码抛 KeyError，
靠异常发现等于每个北交所标的每次都白付一个往返。声明清楚就省掉了。

**这一层只负责"把一段历史行情取回来"**，不负责复权与不复权取两次、不负责补当天
那根实时 bar、也不负责熔断——那些是编排，留在 ``cn_stock_source`` 里。这么切是因为
编排里有几处依赖"是谁给的数"（东财给的不补实时 bar，兜底源给的要补），所以
``resolve()`` 把 provider 名字一起返回。
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..config import env
from .kline_frame import (
    _finalize_fallback_frame,
    _is_index_code,
    _market_prefixed_symbol,
)

logger = logging.getLogger("qtf_mcp")

PROVIDER_ORDER_ENV = "KLINE_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("tencent", "sina")
_DISABLED = {"", "off", "none", "0", "false"}

# 这些异常的含义是"这个源不认识这个标的"，不是"这次没取到"。区分开才能让调用方
# 判断"所有源都不覆盖它"（真空白）还是"都失败了"（可以重试）。
UNSUPPORTED_ERRORS = (KeyError, IndexError, ValueError)


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


class KlineProvider(abc.ABC):
    """一个 K 线来源。

    子类至少要给 ``name`` 和 ``fetch``。``fetch`` 返回 None 或空表都表示"这次没有"，
    调用方会继续问下一个源；抛 ``UNSUPPORTED_ERRORS`` 里的异常表示"不认识这个标的"。
    """

    name: str = ""
    #: 打日志用的中文名。和 name 分开是因为 name 是配置里写的标识符。
    label: str = ""

    def supports(self, request: KlineRequest) -> bool:
        """这个源认不认这个标的。默认全认。

        返回 False 的标的连请求都不会发出去。
        """
        return True

    @abc.abstractmethod
    def fetch(self, request: KlineRequest):
        """取回一段历史行情，列名已经归一到主路径的中文列。"""


_PROVIDERS: dict[str, KlineProvider] = {}


def register(provider: KlineProvider, *, replace: bool = False) -> None:
    if not replace and provider.name in _PROVIDERS:
        raise ValueError(f"K 线来源重名：{provider.name}")
    _PROVIDERS[provider.name] = provider


def unregister(name: str) -> None:
    _PROVIDERS.pop(name, None)


def registered() -> tuple:
    return tuple(_PROVIDERS)


def provider(name: str) -> Optional[KlineProvider]:
    """按名字取一个已注册的源。没有就返回 None。

    给测试和排查用：想单独打一个源、或者把某个源换成假的，不必去动注册表内部。
    """
    return _PROVIDERS.get(name)


def configured_order() -> tuple:
    """按配置解析启用顺序。未知名字忽略并告警，不让服务起不来。"""
    raw = env(PROVIDER_ORDER_ENV)
    if raw is None:
        names = DEFAULT_PROVIDER_ORDER
    elif raw.strip().lower() in _DISABLED:
        return ()
    else:
        names = tuple(part.strip() for part in raw.split(",") if part.strip())

    order = []
    for name in names:
        if name in _PROVIDERS:
            order.append(name)
        else:
            logger.warning(
                "%s 里的 %s 不是已注册的 K 线来源，已忽略；可用：%s",
                PROVIDER_ORDER_ENV, name, ",".join(registered()) or "（空）",
            )
    return tuple(order)


def resolve(
    request: KlineRequest,
    *,
    order: Optional[tuple] = None,
    status: Optional[dict] = None,
) -> Optional[KlineResult]:
    """按顺序问每个源，第一个给出非空结果的赢。

    ``status`` 用来把"为什么空"带回给调用方：每个源不支持这个标的时记
    ``<name>_unsupported``，全都不支持时记 ``unsupported``——那是覆盖缺口，
    和"上游安静"是两回事，报告里的措辞不一样。
    """
    local = {} if status is None else status
    names = order if order is not None else configured_order()
    for name in names:
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        if not provider.supports(request):
            local[f"{name}_unsupported"] = True
            logger.debug("K 线来源 %s 不覆盖 %s，跳过", name, request.code)
            continue
        try:
            frame = provider.fetch(request)
        except Exception as error:
            if isinstance(error, UNSUPPORTED_ERRORS):
                local[f"{name}_unsupported"] = True
            logger.warning("%s历史行情 fallback 失败 %s: %s", provider.label, request.code, error)
            continue
        if frame is not None and not frame.empty:
            return KlineResult(frame=frame, provider=name)

    considered = [name for name in names if name in _PROVIDERS]
    if considered and all(local.get(f"{name}_unsupported") for name in considered):
        # 每个源都不认这个标的，是覆盖缺口，不是一次安静的失败。
        local["unsupported"] = True
    return None


# ── 内置来源 ──────────────────────────────────────────────────────
#
# 新增一个源就照着这两个写：继承 KlineProvider、给 name/label、实现 fetch，
# 需要的话再给 supports，最后在文件末尾 register 一行。


#: 两家的英文列名到主路径中文列名的映射。新源大概率也要用。
_COLUMN_MAP = {
    "date": "日期",
    "open": "开盘",
    "close": "收盘",
    "high": "最高",
    "low": "最低",
    "volume": "成交量",
    "amount": "成交额",
    "turnover": "换手率",
}


class TencentKlineProvider(KlineProvider):
    """腾讯的历史行情。个股、ETF、指数都覆盖，北交所大半抛 KeyError。"""

    name = "tencent"
    label = "腾讯"

    # 这里**没有**声明 supports(request) 排除 bj：实测只是"大半"北交所代码抛
    # KeyError，不是全部，排掉会把本来腾讯能给的那部分改判给新浪——那是换数据源，
    # 不是等价重构。要开这个优化得先逐个 bj 代码测一遍腾讯到底认哪些，单独一次
    # 改动、单独一次等价比对。

    def fetch(self, request: KlineRequest):
        import akshare as ak

        frame = ak.stock_zh_a_hist_tx(
            symbol=request.prefixed,
            start_date=request.fetch_start,
            end_date=request.end_date.replace("-", ""),
            adjust="" if request.adjust == "none" else request.adjust,
        )
        if frame is None or frame.empty:
            return None
        frame = frame.rename(columns=_COLUMN_MAP).copy()
        required = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
        if any(column not in frame.columns for column in required):
            logger.warning("腾讯历史行情字段不完整 %s: %s", request.code, list(frame.columns))
            return None
        return _finalize_fallback_frame(
            frame,
            request.code,
            request.requested_start,
            self.label,
            is_index=_is_index_code(request.prefixed),
        )


class SinaKlineProvider(KlineProvider):
    """新浪的历史行情，覆盖腾讯不认的那部分代码。

    实测 2026-09-03：腾讯对约一半的北交所代码抛 KeyError，新浪能给，没有这一级
    那些标的会返回"未找到...数据"——数据其实是有的。

    它也有自己的洞：ETF（512480/159995）、创业板指走这个端点是 JSONDecodeError，
    2026-09-05 复测仍然如此。也就是说那几类目前只有腾讯一条路。要补的话是换
    ``stock_zh_index_daily``（覆盖全但没有成交额），单独起一个 provider，别改这个。
    """

    name = "sina"
    label = "新浪"

    def fetch(self, request: KlineRequest):
        import akshare as ak

        frame = ak.stock_zh_a_daily(
            symbol=request.prefixed,
            start_date=request.fetch_start,
            end_date=request.end_date.replace("-", ""),
            adjust="" if request.adjust == "none" else request.adjust,
        )
        if frame is None or frame.empty:
            return None
        return _finalize_fallback_frame(
            frame.rename(columns=_COLUMN_MAP),
            request.code,
            request.requested_start,
            self.label,
            is_index=_is_index_code(request.prefixed),
        )


register(TencentKlineProvider())
register(SinaKlineProvider())


__all__ = [
    "KlineProvider",
    "KlineRequest",
    "KlineResult",
    "PROVIDER_ORDER_ENV",
    "UNSUPPORTED_ERRORS",
    "configured_order",
    "register",
    "provider",
    "registered",
    "resolve",
    "unregister",
]
