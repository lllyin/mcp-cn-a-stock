"""盘中行情的多级回退。

盘中报告必须反映当天，而各个数据源的能力并不一致：东财 K 线接口盘中带当天那根
未完成的 bar，腾讯的日 K 不带（实测 2026-09-04 盘中最后一行仍是 09-03），于是
东财一失败，报告的"当日"就退回昨天，而同一份报告里的市值又是今天的。

这里把"当天这根 bar 从哪来"独立成一层：每个来源是一个 provider，按名字注册，按
配置排序，逐个尝试直到拿到满足要求的数据。加一个源就是写一个 provider 并注册，
去掉一个源就是从配置里删掉它的名字，不需要改调用方。

现成的 provider：

``fund_flow_page``
    复用资金流向页面里已经解析出来的页头行情（``ul.hqlist``）。不发请求，所以
    页面已经加载过时它是零成本的；但那块没有开盘/最高/最低，拼不出完整 bar。

``tencent``
    qt.gtimg.cn 的单标的行情，六项俱全，实测 0.15 秒。

两者可以同时启用做交叉验证：``collect`` 返回所有能拿到的报价，``compare`` 给出
它们之间的差异，用于发现某一侧的数据异常。
"""

from __future__ import annotations

import abc
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .fund_flow_page import FundFlowPage, parse_amount, parse_percent, parse_price

logger = logging.getLogger("qtf_mcp")

# 启用哪些 provider、按什么顺序。留空或设为 off 则整层关闭，调用方拿到 None。
PROVIDER_ORDER_ENV = "CN_STOCK_INTRADAY_QUOTE_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("fund_flow_page", "tencent")
_DISABLED = {"", "off", "none", "0", "false"}


@dataclass(frozen=True)
class IntradayQuote:
    """一次盘中报价。字段缺失一律为 None，不用 0 代替。"""

    symbol: str
    source: str
    last: Optional[float] = None
    prev_close: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    volume_lots: Optional[float] = None   # 手
    amount_yuan: Optional[float] = None   # 元
    turnover_pct: Optional[float] = None  # 百分数，2.26 表示 2.26%
    # 数据源自报的时间戳，形如 20260904095909；没有则为 None。
    as_of: Optional[str] = None

    @property
    def has_ohlc(self) -> bool:
        """能否拼出一根完整的日 K bar。"""
        return None not in (self.open, self.high, self.low, self.last)

    @property
    def change(self) -> Optional[float]:
        if self.last is None or self.prev_close is None:
            return None
        return self.last - self.prev_close

    @property
    def change_pct(self) -> Optional[float]:
        if self.change is None or not self.prev_close:
            return None
        return self.change / self.prev_close * 100


@dataclass
class QuoteContext:
    """调用方已经拿到的中间产物。

    provider 能复用就不该再发一次请求：资金流向页面在实时链路里本来就会加载，
    页头行情跟着一起解析出来了，再去请求一次行情纯属浪费。
    """

    fund_flow_page: Optional[FundFlowPage] = None


class QuoteProvider(abc.ABC):
    """一个行情来源。"""

    name: str = ""
    # 是否需要发起网络请求。调用方可以据此优先使用零成本的来源。
    needs_network: bool = True

    @abc.abstractmethod
    def fetch(self, symbol: str, context: QuoteContext) -> Optional[IntradayQuote]:
        """取一次报价；取不到返回 None，不要抛异常给调用方。"""


_PROVIDERS: dict[str, QuoteProvider] = {}


def register(provider: QuoteProvider, *, replace: bool = False) -> None:
    """注册一个 provider。同名默认拒绝覆盖，避免两处注册互相顶掉。"""
    if not provider.name:
        raise ValueError("provider 必须有 name")
    if provider.name in _PROVIDERS and not replace:
        raise ValueError(f"provider 已存在: {provider.name}；如需替换请传 replace=True")
    _PROVIDERS[provider.name] = provider


def unregister(name: str) -> None:
    _PROVIDERS.pop(name, None)


def registered() -> tuple:
    return tuple(_PROVIDERS)


def configured_order() -> tuple:
    """按配置解析启用顺序。未知名字会被忽略并告警，不让服务起不来。"""
    raw = os.getenv(PROVIDER_ORDER_ENV)
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
                "未知的盘中行情来源 %s；可用: %s", name, ",".join(registered()) or "-"
            )
    return tuple(order)


def resolve(
    symbol: str,
    context: Optional[QuoteContext] = None,
    *,
    order: Optional[tuple] = None,
    require_ohlc: bool = False,
) -> Optional[IntradayQuote]:
    """按顺序取第一个可用报价。

    ``require_ohlc`` 用于"要拼一根完整 bar"的场景：只给了最新价的来源会被跳过，
    而不是让调用方拿到一个填不满的结果。
    """
    context = context or QuoteContext()
    for name in order if order is not None else configured_order():
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        try:
            quote = provider.fetch(symbol, context)
        except Exception as e:
            logger.warning("盘中行情来源 %s 取数失败 %s: %s", name, symbol, e)
            continue
        if quote is None or quote.last is None:
            continue
        if require_ohlc and not quote.has_ohlc:
            logger.debug("盘中行情来源 %s 缺开高低，跳过 %s", name, symbol)
            continue
        return quote
    return None


def collect(
    symbol: str,
    context: Optional[QuoteContext] = None,
    *,
    order: Optional[tuple] = None,
) -> list:
    """把所有能拿到的报价都取回来，用于交叉验证。"""
    context = context or QuoteContext()
    quotes = []
    for name in order if order is not None else configured_order():
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        try:
            quote = provider.fetch(symbol, context)
        except Exception as e:
            logger.warning("盘中行情来源 %s 取数失败 %s: %s", name, symbol, e)
            continue
        if quote is not None and quote.last is not None:
            quotes.append(quote)
    return quotes


def compare(
    left: IntradayQuote, right: IntradayQuote, *, tolerance_pct: float = 1.0
) -> list:
    """列出两个报价之间超出容差的字段。

    容差按相对值算，默认 1%：两个来源的抓取时刻本来就差几秒，盘中价格有正常漂移，
    完全相等反而不该期待。
    """
    issues = []
    for field_name in ("last", "open", "high", "low", "volume_lots", "amount_yuan"):
        a = getattr(left, field_name)
        b = getattr(right, field_name)
        if a is None or b is None:
            continue
        scale = max(abs(a), abs(b))
        if scale == 0:
            continue
        drift = abs(a - b) / scale * 100
        if drift > tolerance_pct:
            issues.append(
                f"{field_name}: {left.source}={a} {right.source}={b} 相差 {drift:.2f}%"
            )
    return issues


# --- 内置来源 ---------------------------------------------------------------


class FundFlowPageQuoteProvider(QuoteProvider):
    """复用资金流向页面页头的行情块，不发请求。

    这块没有开盘/最高/最低，所以 has_ohlc 为假；它的价值在于零成本，以及在腾讯
    也不可用时至少还能给出最新价、成交量和成交额。
    """

    name = "fund_flow_page"
    needs_network = False

    def fetch(self, symbol: str, context: QuoteContext) -> Optional[IntradayQuote]:
        page = context.fund_flow_page
        if page is None or not page.quote_text:
            return None
        text = page.quote_text
        last = parse_price(text.get("最新价", ""))
        if last is None:
            return None
        change = parse_price(text.get("涨跌", ""))
        return IntradayQuote(
            symbol=symbol,
            source=self.name,
            last=last,
            prev_close=None if change is None else last - change,
            # 总手写作 "16.26万手"：先去掉量词，剩下的 "16.26万" 才走单位表。
            volume_lots=parse_amount(text.get("总手", "").rstrip("手")),
            amount_yuan=parse_amount(text.get("成交额", "")),
            turnover_pct=parse_percent(text.get("换手率", "")),
        )


# 腾讯行情按 ~ 分隔，字段是位置固定的。下标为 0 基。
TENCENT_FIELDS = {
    "code": 2,
    "last": 3,
    "prev_close": 4,
    "open": 5,
    "volume_lots": 6,
    "as_of": 30,
    "amount_wan": 37,
    "turnover_pct": 38,
    "high": 41,
    "low": 42,
}
TENCENT_URL = "https://qt.gtimg.cn/q={code}"


class TencentQuoteProvider(QuoteProvider):
    """qt.gtimg.cn 的单标的行情，六项俱全。"""

    name = "tencent"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def fetch(self, symbol: str, context: QuoteContext) -> Optional[IntradayQuote]:
        code = tencent_code(symbol)
        if code is None:
            return None

        import requests

        response = requests.get(TENCENT_URL.format(code=code), timeout=self.timeout)
        response.encoding = "gbk"
        parts = response.text.split("~")
        if len(parts) <= max(TENCENT_FIELDS.values()):
            return None
        # 代码对不上说明返回的不是这只票（腾讯对未知代码会返回 pv_none_match）。
        if parts[TENCENT_FIELDS["code"]] != code[2:]:
            return None

        def number(key: str, parser=parse_price):
            return parser(parts[TENCENT_FIELDS[key]])

        last = number("last")
        if last is None:
            return None
        amount_wan = number("amount_wan")
        return IntradayQuote(
            symbol=symbol,
            source=self.name,
            last=last,
            prev_close=number("prev_close"),
            open=number("open"),
            high=number("high"),
            low=number("low"),
            volume_lots=number("volume_lots"),
            amount_yuan=None if amount_wan is None else amount_wan * 1e4,
            turnover_pct=number("turnover_pct"),
            as_of=parts[TENCENT_FIELDS["as_of"]].strip() or None,
        )


def tencent_code(symbol: str) -> Optional[str]:
    """把 SZ300408 / 300408 归一成腾讯的 sz300408。

    basic_info 那层也用它——同一个 qt.gtimg.cn 端点，代码归一的规则只该有一份。
    """
    digits = "".join(filter(str.isdigit, symbol))
    if len(digits) != 6:
        return None
    upper = symbol.upper()
    for prefix in ("SH", "SZ", "BJ"):
        if upper.startswith(prefix):
            return prefix.lower() + digits
    if digits.startswith(("60", "68", "51", "58", "11")):
        return "sh" + digits
    if digits.startswith(("43", "83", "87", "92")):
        return "bj" + digits
    return "sz" + digits


register(FundFlowPageQuoteProvider())
register(TencentQuoteProvider())
