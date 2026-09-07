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
import json
import logging
from dataclasses import dataclass
from typing import Optional

from ..config import INTRADAY_QUOTE_CROSS_CHECK_PCT, env
from .fund_flow_page import FundFlowPage, parse_amount, parse_percent, parse_price

logger = logging.getLogger("finmcp")

# 启用哪些 provider、按什么顺序。留空或设为 off 则整层关闭，调用方拿到 None。
PROVIDER_ORDER_ENV = "INTRADAY_QUOTE_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("fund_flow_page", "tencent", "tonghuashun")
#: **指数单独一条顺序**，判据和历史 K 线那一层一样（``kline_source`` 的常量注释）：
#: 指数的成交量各源口径差得多，而同花顺与东财一致。2026-09-07 收盘后四方核对
#: 创业板指当日成交量：
#:
#:     东财 push2delay f47   172,310,434 手   ← 本项目的基准源
#:     同花顺 realhead       172,310,430 手   ✅ 与东财一致
#:     腾讯 qt.gtimg.cn      165,865,859 手   低 3.885%
#:     新浪 hq.sinajs.cn     165,865,859 手   与腾讯一字不差（同源，互相校验不了）
#:
#: 上证/深证/科创50 四家全部逐位一致，个股与 ETF 也一致（12 个标的实测），所以
#: **只有指数需要换这个顺序**。个股不换的理由和 K 线那层相同：同花顺的限流策略
#: 未知，而个股是流量大头；指数就那么几个，暴露面小，值得为 3.885% 换过去。
INDEX_PROVIDER_ORDER = ("fund_flow_page", "tonghuashun", "tencent")
INDEX_PROVIDER_ORDER_ENV = "INTRADAY_QUOTE_PROVIDERS_INDEX"
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


def to_lots(raw, *, amount_yuan, last, symbol: str, source: str):
    """把上游给的成交量归一成**手**。归一不了就返回 None，不要蒙一个。

    ``volume_lots`` 这个契约字段的单位是手，而**上游的单位不统一，同一个端点内部
    都不统一**：腾讯 qt.gtimg.cn 的 ``[6]`` 对沪深主板、创业板是手，对**科创板
    （688xxx）是股**；同花顺 realhead 的 ``[13]`` 一律是股。实测（2026-09-07 收盘后，
    用 成交额/收盘 反算股数判定）：

        标的                腾讯[6]        判定    同花顺[13]        判定
        贵州茅台 600519       25,250        手     2,524,962        股
        宁德时代 300750      246,788        手    24,678,820        股
        中芯国际 688981   31,314,788      **股**  31,314,788        股
        金山办公 688111    4,856,986      **股**   4,856,986        股
        澜起科技 688008   38,159,931      **股**  38,159,931        股

    不归一的后果是**整整 100 倍**，而且两个数都"看着像成交量"：修之前报告里
    SH688981 的当日成交量是 3131.48 万手，真实 31.2 万手，同一份报告的成交额
    38.74亿 对不上（3131万手 × 124元 = 3887亿）。

    判定用数据而不是用代码前缀名单——名单会漏，而在这件事上漏一个就是 100 倍。
    判据和历史行情那条一样（``kline_frame._normalize_volume_to_lots``，那条在 1497
    个东财口径的交易日上验过）：``成交额/收盘`` 是股数，它和手数相差两个数量级，
    0.1 是对数中点。

    **指数走不了这条推断**：指数的"收盘"是点位不是股价，``成交额/点位`` 算不出任何
    股数。实测指数上腾讯是手、同花顺是股，所以指数由 provider 自己按端点的固定
    口径给，不进这个函数。
    """
    if raw is None or raw <= 0:
        return raw
    if not amount_yuan or not last or last <= 0:
        # 缺了反算的材料。宁可不给这一维，也不要把 100 倍的数写进报告。
        logger.warning(
            "%s %s 成交量无法定单位（成交额=%s 收盘=%s），丢弃该字段",
            source, symbol, amount_yuan, last,
        )
        return None
    implied_shares = amount_yuan / last
    if implied_shares <= 0:
        return None
    ratio = raw / implied_shares
    in_shares = ratio > 0.1
    scaled = ratio if in_shares else ratio * 100
    if not 0.5 <= scaled <= 2.0:
        logger.warning(
            "%s %s 盘中成交量量级异常 ratio=%.4g，按%s处理",
            source, symbol, ratio, "股" if in_shares else "手",
        )
    else:
        logger.debug(
            "%s %s 盘中成交量单位=%s ratio=%.4g",
            source, symbol, "股" if in_shares else "手", ratio,
        )
    return raw / 100 if in_shares else raw


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


def configured_order(symbol: Optional[str] = None) -> tuple:
    """按配置解析启用顺序。未知名字会被忽略并告警，不让服务起不来。

    指数走 ``INDEX_PROVIDER_ORDER``，理由见那个常量的注释（成交量口径）。
    判断用结构规则而不是名单——漏判一个指数的后果是它的成交量偏 3.9%。
    """
    from .kline_frame import _is_index_code

    is_index = bool(symbol) and _is_index_code(tencent_code(symbol) or "")
    env_name = INDEX_PROVIDER_ORDER_ENV if is_index else PROVIDER_ORDER_ENV
    default = INDEX_PROVIDER_ORDER if is_index else DEFAULT_PROVIDER_ORDER
    raw = env(env_name)
    if raw is None:
        names = default
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
    names = tuple(order if order is not None else configured_order(symbol))
    for index, name in enumerate(names):
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
        _cross_check(symbol, quote, context, names, after=index)
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
    for name in order if order is not None else configured_order(symbol):
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


def _cross_check(symbol, chosen, context, names, *, after: int) -> None:
    """拿到结果之后，再问后面的源一遍，值对不上就告警。默认关。

    collect() 和 compare() 这两个零件早就有了却没人调，等于白造；这里就是把它们
    接进主链。源之间的口径差会在日志里当场暴露，而不是等到有人拿两台机器的报告
    逐字比——创业板指成交量差 3.5% 那件事就是那么查出来的，花了几个钟头。

    只问 ``after`` 之后的源：前面的已经试过且没给出结果，再问一遍纯属浪费。
    整段包在 try 里——校验只是个观察点，它自己炸了不能把取数带下水。
    """
    if INTRADAY_QUOTE_CROSS_CHECK_PCT <= 0:
        return
    try:
        for other in collect(symbol, context, order=tuple(names[after + 1:])):
            issues = compare(chosen, other, tolerance_pct=INTRADAY_QUOTE_CROSS_CHECK_PCT)
            if issues:
                logger.warning(
                    "盘中行情跨源不一致 symbol=%s 采用=%s 对照=%s %s",
                    symbol, chosen.source, other.source, "；".join(issues),
                )
            else:
                logger.debug(
                    "盘中行情跨源一致 symbol=%s %s vs %s",
                    symbol, chosen.source, other.source,
                )
    except Exception as error:  # 观察点不许把主链带下水
        logger.debug("盘中行情跨源校验出错 symbol=%s: %s", symbol, error)


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
        amount_yuan = None if amount_wan is None else amount_wan * 1e4
        raw_volume = number("volume_lots")
        # ``[6]`` 的单位在这个端点内部就不统一：主板/创业板给手，科创板给股。
        # 指数不进推断（点位算不出股数），实测那里是手，原样用。
        from .kline_frame import _is_index_code

        volume_lots = raw_volume if _is_index_code(code) else to_lots(
            raw_volume, amount_yuan=amount_yuan, last=last,
            symbol=symbol, source=self.name,
        )
        return IntradayQuote(
            symbol=symbol,
            source=self.name,
            last=last,
            prev_close=number("prev_close"),
            open=number("open"),
            high=number("high"),
            low=number("low"),
            volume_lots=volume_lots,
            amount_yuan=amount_yuan,
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


#: 同花顺 realhead 的字段号 → 语义。全部用已知值反查确认过（2026-09-07 收盘后，
#: 上证指数 最新 3932.70 / 昨收 3930.12 / 开 3942.51 / 高 3948.42 / 低 3916.49）。
#:
#: 两个坑写在这里，别再踩：
#:   - ``13`` 是**股**，不是手，而且指数也是股（上证 47,737,526,000 股 = 4.77亿手，
#:     与交易所定稿值一致）。腾讯那个端点相反——它的 ``[6]`` 主板给手、科创板给股。
#:   - 换手率是 ``1968584``，不是 ``1771976``。两个都是小数、量级也像，第一次核对时
#:     取错了那个，于是误判"这个源没有换手率"。判据是拿腾讯的换手率逐个比：
#:     茅台 0.202/0.20、宁德 0.579/0.58、中芯 1.566/1.57、50ETF 8.137/8.14、
#:     美的 0.333/0.33 —— ``1968584`` 五个全中，``1771976`` 五个全不中。
#: ``time`` 是服务器时刻，``updateTime`` 才是数据时刻，取后者。
TONGHUASHUN_QUOTE_FIELDS = {
    "last": "10", "prev_close": "6", "open": "7", "high": "8", "low": "9",
    "volume_shares": "13", "amount_yuan": "19", "change_pct": "199112",
    "turnover_pct": "1968584", "code": "5", "as_of": "updateTime",
}
TONGHUASHUN_QUOTE_URL = "https://d.10jqka.com.cn/v6/realhead/hs_{code}/last.js"


class TonghuashunQuoteProvider(QuoteProvider):
    """同花顺 realhead。当日那一根的第二个来源。

    为什么需要第二个：``upsert_intraday_bar`` 之后，**当天那一根完全依赖这一层**
    （日线端点给的当天行一律不作准），于是腾讯成了单点——它一失败，当天那一根就
    拼不出来。这个源和腾讯不同域、不同厂，实测收盘价 12/12 逐位一致。

    它还是那 3.89% 的唯一对照：创业板指的成交量腾讯比它低 3.89%（而它与东财一致），
    开 ``INTRADAY_QUOTE_CROSS_CHECK_PCT`` 就能在日志里看到这个差，不必再人工三方比对。

    裸 GET，必须带 Referer 和 Accept，缺了返回 0 字节——和历史行情那个端点同一规矩。
    """

    name = "tonghuashun"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def fetch(self, symbol: str, context: QuoteContext) -> Optional[IntradayQuote]:
        from .platforms.tonghuashun import _HEADERS, tonghuashun_code

        code = tonghuashun_code(tencent_code(symbol) or "")
        if code is None:
            return None

        import requests

        response = requests.get(TONGHUASHUN_QUOTE_URL.format(code=code),
                                headers=_HEADERS, timeout=self.timeout)
        body = response.text
        if response.status_code != 200 or "(" not in body:
            return None
        try:
            payload = json.loads(body[body.index("(") + 1: body.rindex(")")])
        except (ValueError, json.JSONDecodeError):
            return None
        items = payload.get("items") or payload
        if not isinstance(items, dict):
            return None
        # 代码对不上说明拿回的不是这只票。踩过：hs_000001 返回的是平安银行。
        if str(items.get(TONGHUASHUN_QUOTE_FIELDS["code"], "")).upper() != code.upper():
            logger.warning("同花顺行情返回的标的不符 请求=%s 返回=%s",
                           code, items.get(TONGHUASHUN_QUOTE_FIELDS["code"]))
            return None

        def number(key: str):
            return parse_price(str(items.get(TONGHUASHUN_QUOTE_FIELDS[key], "")))

        last = number("last")
        if last is None:
            return None
        amount_yuan = number("amount_yuan")
        raw_shares = number("volume_shares")
        prefixed = tencent_code(symbol) or ""
        from .kline_frame import _is_index_code

        if raw_shares is None:
            volume_lots = None
        elif _is_index_code(prefixed):
            # 指数也是股，但点位算不出股数，推断不了——按这个端点的固定口径换算。
            volume_lots = raw_shares / 100
        else:
            volume_lots = to_lots(raw_shares, amount_yuan=amount_yuan, last=last,
                                  symbol=symbol, source=self.name)
        return IntradayQuote(
            symbol=symbol,
            source=self.name,
            last=last,
            prev_close=number("prev_close"),
            open=number("open"),
            high=number("high"),
            low=number("low"),
            volume_lots=volume_lots,
            amount_yuan=amount_yuan,
            turnover_pct=number("turnover_pct"),
            as_of=_compact_timestamp(items.get(TONGHUASHUN_QUOTE_FIELDS["as_of"])),
        )


def _compact_timestamp(raw) -> Optional[str]:
    """``2026-09-07 15:00`` → ``20260907150000``。

    契约要求 ``as_of`` 的前 8 位能按 ``%Y%m%d`` 解析（``upsert_intraday_bar`` 靠它
    判断这根 bar 是哪天的），所以分隔符必须去掉。
    """
    text = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(text) < 8:
        return None
    return (text + "000000")[:14]


register(FundFlowPageQuoteProvider())
register(TencentQuoteProvider())
register(TonghuashunQuoteProvider())
