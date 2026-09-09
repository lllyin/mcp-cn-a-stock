"""报告里应该有哪些维度,以及一份报告实际渲染出了哪些。

这张表原先只长在 ``scripts/verify_release.py`` 里,发版闸门自己扫报告文本。搬到
包内是因为**服务自己也要用**:渲染完当场比对一次,把结果写进日志,``health`` 工具
才能算出实测的可用率而不是按上游失败推算的估计值。

两处必须读同一张表。各存一份的话,同一件事发版闸门报一个可用率、``health`` 报另一个,
两个数都没法用。

## 维度、来源、适用范围

``source`` 是这一维由哪个上游源提供——一个源挂掉会带走它名下的全部维度,归因时按它
聚合。``applies_to`` 排除结构上就没有的组合(ETF 没有财务报表、指数没有市值),那些
不是缺失,不进分母。

**只按"哪一类标的本来就没有这一维"排除,不按具体是哪个标的排除。** 区别在于是不是
会变:ETF 没有市盈率是因为它没有盈利这个东西,换任何源都不会有——那是标的本身的属性。
而"科创50 拿不到资金流向"是**当前这条源**的属性,换条源就有了。曾经硬编码过一份
"有页面的指数"名单,把 SH000688 判成"本来就没有",于是真实的一处缺失被记成了满分。
取到就是取到,没取到就是没取到;分数会因此变低,但那个低才是真的。

## 有段落标题 ≠ 有值

``DEGRADED_MARKERS`` 里的句子表示"这一维渲染出来了但没有值"。只查标题会把降级当成
正常——2026-09-04 漏过一次,``暂无资金流向数据`` 不在表里,四个标的的资金流其实是空的
而矩阵全标成了 ✅。渲染层加一句新的提示语就要往这里加一行,有测试盯着。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("finmcp")

STOCK, ETF, INDEX = "stock", "etf", "index"
ALL_CLASSES = frozenset((STOCK, ETF, INDEX))
STOCK_ONLY = frozenset((STOCK,))

#: 指数判定与取数层保持一致,见 kline_frame._INDEX_CODE_PREFIXES。
_INDEX_PREFIXES = {"sh": ("000",), "sz": ("399",), "bj": ("899",)}


def is_index(symbol: str) -> bool:
    normalized = (symbol or "").lower()
    return normalized[2:].startswith(_INDEX_PREFIXES.get(normalized[:2], ()))


def classify(symbol: str) -> str:
    """个股 / ETF / 指数。ETF 的判据与取数层一致:六位码以 1 或 5 开头。"""
    if is_index(symbol):
        return INDEX
    return ETF if (symbol or "")[2:].startswith(("1", "5")) else STOCK


@dataclass(frozen=True)
class Dimension:
    name: str
    marker: str          # 在报告文本里的行首特征
    source: str          # 由哪个上游源提供
    applies_to: frozenset = ALL_CLASSES

    def applies(self, symbol: str) -> bool:
        return classify(symbol) in self.applies_to


_BASIC = (
    Dimension("股票代码", "- 股票代码:", "realtime"),
    Dimension("股票名称", "- 股票名称:", "realtime"),
    Dimension("数据日期", "- 数据日期:", "kline"),
    Dimension("行业概念", "- 行业概念:", "realtime", applies_to=STOCK_ONLY),
    Dimension("总市值", "- 总市值:", "realtime", applies_to=STOCK_ONLY),
    Dimension("流通市值", "- 流通市值:", "realtime", applies_to=STOCK_ONLY),
    Dimension("市盈率(静)", "- 市盈率(静):", "realtime", applies_to=STOCK_ONLY),
    Dimension("市盈率(动)", "- 市盈率(动):", "realtime", applies_to=STOCK_ONLY),
    Dimension("市净率", "- 市净率:", "realtime", applies_to=STOCK_ONLY),
    Dimension("净资产收益率", "- 净资产收益率:", "realtime", applies_to=STOCK_ONLY),
)

_TRADING = (
    Dimension("价格", "## 价格", "kline"),
    Dimension("涨跌幅", "## 涨跌幅", "kline"),
    Dimension("振幅", "## 振幅", "kline"),
    Dimension("成交量", "## 成交量(万手)", "kline"),
    Dimension("成交额", "## 成交额(亿)", "kline"),
    Dimension("资金流向", "## 资金流向", "fund_flow"),
    # 换手率 = 成交量 / 流通股本,分母来自 realtime 的市值,所以 realtime 挂了
    # 表现是"换手率整段不见了",而不是数字不对。
    Dimension("换手率", "## 换手率", "realtime(流通市值)", applies_to=STOCK_ONLY),
)

# 财务报表只有个股有。历史资金流向不排除指数——实测 full 对 SH000001 和 SZ399006
# 都渲染出了完整的历史表,排除等于把真实拿到的数据不计分。技术指标算的是 K 线,三类都有。
_FINANCE = (Dimension("财务数据", "# 财务数据", "finance", STOCK_ONLY),)
_HISTORY_FLOW = (Dimension("历史资金流向", "## 历史资金流向", "fund_flow"),)
_TECHNICAL = (Dimension("技术指标", "# 技术指标", "kline"),)

CONTRACT: dict[str, tuple[Dimension, ...]] = {
    "brief": _BASIC + _TRADING,
    "medium": _BASIC + _TRADING + _FINANCE,
    "full": _BASIC + _TRADING + _HISTORY_FLOW + _FINANCE + _TECHNICAL,
}

#: 这些句子说明某一维渲染出来了但没有值,见模块开头。
DEGRADED_MARKERS = {
    # 探活一律不钉日期,所以这一句不该出现。出现了说明有人给探活加了 date=,
    # 那会让"实时资金流取不取得到"这件事永远查不出来——是 bug 不是正当缺席。
    "指定日期查询暂不展示实时资金流向": "探活钉了日期？实时资金流因此查不到，检查调用参数",
    "暂无实时资金流向": "实时资金流没取到（主源被拒且页面兜底也没成）",
    "暂无资金流向数据": "资金流整段为空",
    "暂无财务数据": "财务报表没取到",
    "暂无年度财务数据": "财务报表里没有年度期",
    "盘中实时数据暂时不可用": "盘中回退整层被跳过或熔断",
    "暂无数据": "该维度取到空值",
    "获取失败": "该维度取数失败",
}


def expected(tool: str, symbol: str) -> tuple[Dimension, ...]:
    """这个标的在这个工具下**应该**有哪些维度。分母就是它。"""
    return tuple(d for d in CONTRACT.get(tool, ()) if d.applies(symbol))


def scan(text: str, tool: str, symbol: str) -> tuple[list[str], list[str]]:
    """扫一份报告,返回(拿到的维度, 缺的维度)。

    **这个函数在渲染热路径上被调用**,所以只做子串查找,不做正则、不切行、不建中间
    字符串。一个 ``brief`` 报告约 1 KiB、17 个维度,``in`` 是 C 层实现,实测在微秒
    量级——比它上面那次取数低六个数量级。调用方仍然要用 ``logger.isEnabledFor`` 把
    它挡在关闭的日志级别之外,免得白算。

    有标题但内容是降级提示的算缺失:段落在、值不在,对使用者是一回事。
    """
    if not text:
        return [], [d.name for d in expected(tool, symbol)]
    present, missing = [], []
    for dimension in expected(tool, symbol):
        if dimension.marker in text:
            present.append(dimension.name)
        else:
            missing.append(dimension.name)
    return present, missing


def degraded_in(text: str) -> list[str]:
    """报告里出现了哪些"渲染了但没值"的提示语。同样只做子串查找。"""
    return [marker for marker in DEGRADED_MARKERS if marker in text] if text else []


__all__ = [
    "ALL_CLASSES", "CONTRACT", "DEGRADED_MARKERS", "Dimension", "ETF", "INDEX",
    "STOCK", "STOCK_ONLY", "classify", "degraded_in", "expected", "is_index", "scan",
]
