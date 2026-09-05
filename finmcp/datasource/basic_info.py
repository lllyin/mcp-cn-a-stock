"""基本数据的多级回退。

报告的"基本数据"那一段——总市值、流通市值、市盈率、市净率——原先全部只来自
``ef.stock.get_base_info``（东财 push2 的 ``f=`` 端点）。它一挂，连带市盈率(静) 和
换手率也没了：前者要总股本，后者要流通股本，而渲染层把这两项都放在
``total_shares > 0`` 的判断里。**七个维度挂在一个调用上。**

2026-09-04 实测：不带 akshare-proxy-patch 的出口上 ``get_base_info`` 直接抛
JSONDecodeError，而 ``get_quote_snapshot`` 是通的。原先的代码里本来写了一条
snapshot 兜底分支，但 base_info 是抛异常而不是返回空，被外层 ``except`` 一把接住，
把已经取到的 snapshot 数据一起扔了——兜底分支永远走不到。

这里把"基本数据从哪来"独立成一层，和 ``intraday_quote`` 同一个套路：每个来源是一个
provider，按名字注册、按配置排序、逐个尝试。加源就写一个 provider，去掉源就从配置
里删名字，调用方不动。

现成的 provider：

``eastmoney``
    ``ef.stock.get_base_info`` + ``get_quote_snapshot``。字段最全，但需要网关或
    未被封的出口。两个调用分开容错：base_info 挂了仍用 snapshot 补名称和最新价。

``tencent``
    ``qt.gtimg.cn`` 的长表。一次请求可批量取多个标的（实测 16 个 8.4KB / 67ms），
    无需鉴权。2026-09-04 收盘后用开着网关的服务器逐项比过 7 个个股：

    | 字段 | 腾讯索引 | 一致 | 最大偏差 |
    |------|---------|------|---------|
    | 总市值 | [45] | 7/7 | 0.000% |
    | 流通市值 | [44] | 7/7 | 0.000% |
    | 市盈率(动) | [52] | 7/7 | 0.000% |
    | 市盈率(静) | [53] | 7/7 | 0.005% |
    | 市净率 | [46] | 6/7 | 1.87%（宁德时代 4.28 vs 4.36） |

    **市净率故意不从腾讯取**，见 ``TencentBasicInfoProvider`` 的注释：项目里已有的
    本地回退式（现价/每股净资产）在同一批标的上最大偏差 0.42%，比腾讯的 1.87% 稳，
    而且不需要多一个源。

已知行为：市净率会在上游"半通"时抖动
---------------------------------

市净率有两个口径——东财的 f167（总市值/最新报告期归母净资产）和渲染层的本地回退
（现价/每股净资产）。``base_info`` 通的时候用前者，不通的时候用后者。

问题在于 impersonate 通道下 ``base_info`` 是**偶发成功**的：2026-09-04 在本机实测
SH600118 连查 8 次，7 次 10.63（本地回退）、1 次 10.64（东财 f167），整份报告只有
这一行不同。改动前不会抖——``base_info`` 的异常会让整个 realtime 判失败，市净率
永远走本地那条。

抖动只发生在"上游有时通有时不通"这个中间态：网关开着时永远走东财，完全封锁时
永远走本地，两头都是确定的。而没有 akshare-proxy-patch 的部署恰好就在中间态。

要消掉它就得让市净率不依赖主源——也就是永远用本地回退式。代价是网关开着的部署
会从 f167 换成本地口径：实测 7 个标的上最大差 0.42%，但 research.py 的注释记了一个
更坏的例子（SZ300408 的 9.78 vs 9.38，差 4.3%，成因是股本在报告期之后变动过）。
收益和代价都不占压倒优势，所以保持现状，把结论写在这里，别下次重新推一遍。

    注意 ``[39]`` 不是东财的市盈率(动)：600519 上它是 20.42 而东财是 18.67。
    ``[39]`` 是另一个 PE(TTM)（总市值/滚动四季净利润）。按字段位置猜含义会栽在
    这里，上面那张表是让数据反查出来的——对每个东财字段扫描腾讯全部字段，看哪个
    索引在最多标的上落进 ±0.6%。
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional

from ..config import env
from .intraday_quote import tencent_code

logger = logging.getLogger("qtf_mcp")

PROVIDER_ORDER_ENV = "BASIC_INFO_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("eastmoney", "tencent")
_DISABLED = {"", "off", "none", "0", "false"}


@dataclass(frozen=True)
class BasicInfo:
    """一份基本数据。字段缺失一律为 None，不用 0 代替——0 和"没有"是两件事。"""

    symbol: str
    source: str
    name: Optional[str] = None
    last: Optional[float] = None                # 最新价，元
    total_market_cap: Optional[float] = None    # 总市值，元
    float_market_cap: Optional[float] = None    # 流通市值，元
    pe_ttm: Optional[float] = None              # 市盈率(动)
    pb: Optional[float] = None                  # 市净率

    @property
    def total_shares(self) -> Optional[float]:
        """总股本 = 总市值 / 最新价。

        东财原本也是这么派生的（它不直接给股本），所以这里保持同一个口径，
        换源之后 市盈率(静) = 总股本 × 现价 / 上年净利润 的算法完全不变。
        """
        if not self.total_market_cap or not self.last:
            return None
        return self.total_market_cap / self.last

    @property
    def has_valuation(self) -> bool:
        """市值这一组齐不齐。名称和最新价齐了不算齐——那两项 snapshot 就能给。"""
        return bool(self.total_market_cap and self.float_market_cap)


class BasicInfoProvider(abc.ABC):
    """一个基本数据来源。"""

    name: str = ""

    @abc.abstractmethod
    def fetch(self, query: str, symbol: str) -> Optional[BasicInfo]:
        """取一次；取不到返回 None，不要抛异常给调用方。

        两个标识是有意分开的，因为两类源认的东西不一样：

        ``query``
            给"按名字查"的源用。个股是六位码，指数是中文名——东财按 ``000001``
            查会返回深市的平安银行，按"上证指数"才对。
        ``symbol``
            **带市场前缀**的规范代码（SH600519 / SH000001），给"按代码查"的源用。
            前缀不能省：``000001`` 本身是歧义的，腾讯按代码段猜会猜成深市的
            平安银行，上证指数得是 ``sh000001``。

        这两个坑都踩过：先是把六位码喂给了东财（拿到平安银行），改完又把不带前缀
        的六位码喂给了腾讯（还是平安银行）。所以两个标识都得由调用方明确给出。
        """


_PROVIDERS: dict[str, BasicInfoProvider] = {}


def register(provider: BasicInfoProvider, *, replace: bool = False) -> None:
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
                "未知的基本数据来源 %s；可用: %s", name, ",".join(registered()) or "-"
            )
    return tuple(order)


def resolve(
    query: str,
    symbol: str,
    *,
    order: Optional[tuple] = None,
    require_valuation: bool = True,
) -> Optional[BasicInfo]:
    """按顺序取第一个够用的结果，并把后面的源用来补前面缺的字段。

    ``require_valuation`` 是这一层的关键：只拿到名称和最新价不算成功，否则东财的
    snapshot 一通就直接返回，市值那一组永远轮不到腾讯去补。但"半个结果"也不能扔——
    第一个源给了名称、第二个源给了市值，合起来才是完整的一份。
    """
    names = order if order is not None else configured_order()
    merged: Optional[BasicInfo] = None
    for name in names:
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        try:
            info = provider.fetch(query, symbol)
        except Exception as e:
            logger.warning("基本数据来源 %s 取数失败 %s: %s", name, symbol, e)
            continue
        if info is None:
            continue
        merged = _merge(merged, info)
        if not require_valuation or merged.has_valuation:
            return merged
        logger.debug("基本数据来源 %s 缺市值，继续下一个 symbol=%s", name, symbol)
    return merged


def _merge(base: Optional[BasicInfo], extra: BasicInfo) -> BasicInfo:
    """用后来的源补先前缺的字段。已经有值的不覆盖——先配置的源优先级更高。

    source 记成 "eastmoney+tencent" 这样，日志里能看出这份数据是拼出来的。
    """
    if base is None:
        return extra
    picked = {}
    for field in ("name", "last", "total_market_cap", "float_market_cap", "pe_ttm", "pb"):
        current = getattr(base, field)
        picked[field] = current if current else getattr(extra, field)
    # 按 "+" 切开再判重，不能用子串：将来要是有个源叫 east，``in`` 会把它误判成
    # 已经在 eastmoney 里了。
    sources = base.source.split("+")
    if extra.source not in sources:
        sources.append(extra.source)
    return BasicInfo(symbol=base.symbol, source="+".join(sources), **picked)


# ── 东财 ────────────────────────────────────────────────────────


class EastmoneyBasicInfoProvider(BasicInfoProvider):
    """efinance 的 base_info + quote_snapshot。

    两个调用分开容错是有原因的：base_info 提供市值和市盈率，snapshot 提供最新价，
    而 base_info 被封时抛的是异常。放在一个 try 里，snapshot 那份能用的数据会被
    一起丢掉——这正是改动前的行为。
    """

    name = "eastmoney"

    def fetch(self, query: str, symbol: str) -> Optional[BasicInfo]:
        import efinance as ef

        series = None
        try:
            series = ef.stock.get_base_info(query)
            if series is not None and series.empty:
                series = None
        except Exception as e:
            # 出口被封时这里是 JSONDecodeError。记 debug 不记 warning：没有网关的
            # 部署上它每次都会发生，warning 会把日志刷满而不带来新信息。
            logger.debug("东财 base_info 不可用 %s: %s", query, e)

        snapshot = None
        try:
            snapshot = ef.stock.get_quote_snapshot(query)
            if snapshot is not None and snapshot.empty:
                snapshot = None
        except Exception as e:
            logger.debug("东财 quote_snapshot 不可用 %s: %s", query, e)

        if series is None and snapshot is None:
            return None

        def pick(source, key):
            if source is None:
                return None
            return _number(source.get(key))

        name = _text(series.get("股票名称") if series is not None else None) or _text(
            snapshot.get("名称") if snapshot is not None else None
        )
        if name is None and pick(snapshot, "最新价") is None:
            # 按名字查指数时，东财会返回一个字段全是 NaN 的 series。它既不是空
            # DataFrame 也不是异常，不挡住的话 NaN 会一路渲染成"股票名称: nan"。
            return None

        return BasicInfo(
            symbol=symbol,
            source=self.name,
            name=name,
            last=pick(snapshot, "最新价"),
            total_market_cap=pick(series, "总市值"),
            float_market_cap=pick(series, "流通市值"),
            pe_ttm=pick(series, "市盈率(动)"),
            pb=pick(series, "市净率"),
        )


# ── 腾讯 ────────────────────────────────────────────────────────

# 索引见模块 docstring 里那张表，是用服务器真数据反查出来的，不是按位置猜的。
#
# 市净率在 [46]，但这里刻意不取。以服务器的东财值为真，在 7 个标的上比过两个候选：
#
#   候选              最大偏差   说明
#   腾讯 [46]          1.87%    宁德时代 4.36 vs 东财 4.28
#   现价/每股净资产      0.42%    项目里本来就有的回退，见 research.py
#
# 宁德时代那 1.87% 没有可解释的机制：东财和"现价/每股净资产"是两条独立口径，
# 都指向 4.28，只有腾讯给 4.36。既然已有的回退更稳又不需要多一个源，就不取它。
# 真要改回来，先把这个偏差的成因查清楚。
TENCENT_FIELDS = {
    "code": 2,
    "name": 1,
    "last": 3,
    "float_market_cap_yi": 44,
    "total_market_cap_yi": 45,
    "pe_ttm": 52,
}
TENCENT_URL = "https://qt.gtimg.cn/q={code}"
_YI = 1e8


class TencentBasicInfoProvider(BasicInfoProvider):
    """qt.gtimg.cn 长表。市值单位是亿元，换成元再交出去。"""

    name = "tencent"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def fetch(self, query: str, symbol: str) -> Optional[BasicInfo]:
        # 只认带前缀的代码：query 对指数是中文名，喂给腾讯只会拿到 pv_none_match。
        target = tencent_code(symbol)
        if target is None:
            return None

        import requests

        response = requests.get(TENCENT_URL.format(code=target), timeout=self.timeout)
        response.encoding = "gbk"
        if '="' not in response.text:
            return None
        parts = response.text.split('="', 1)[1].rstrip('";\n').split("~")
        if len(parts) <= max(TENCENT_FIELDS.values()):
            return None
        # 代码对不上说明返回的不是这只票（腾讯对未知代码返回 pv_none_match）。
        if parts[TENCENT_FIELDS["code"]] != target[2:]:
            return None

        def field(key, scale=1.0):
            value = _number(parts[TENCENT_FIELDS[key]])
            return None if value is None else value * scale

        return BasicInfo(
            symbol=symbol,
            source=self.name,
            name=parts[TENCENT_FIELDS["name"]].strip() or None,
            last=field("last"),
            total_market_cap=field("total_market_cap_yi", _YI),
            float_market_cap=field("float_market_cap_yi", _YI),
            pe_ttm=field("pe_ttm"),
            # pb 不取：留空让渲染层退回本地的 现价/每股净资产，实测更准。
        )


def _text(raw) -> Optional[str]:
    """文本字段的清洗。pandas 的 NaN 是 float，``or`` 判定为真，会一路漏到报告里。"""
    if raw is None or raw != raw:
        return None
    text = str(raw).strip()
    return text or None


def _number(raw) -> Optional[float]:
    """空串、``-``、``--`` 和 0 都当成"没有"。

    腾讯对 ETF 的市盈率给空串、对指数的市净率给 0.00，两者都是"这类标的没有这一项"，
    不是"取数失败"。当成 None 交出去，让上层按缺失处理，而不是渲染出一个 0。
    """
    if raw is None or raw != raw:      # NaN != NaN
        return None
    text = str(raw).strip()
    if text in ("", "-", "--"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value or None


register(EastmoneyBasicInfoProvider())
register(TencentBasicInfoProvider())
