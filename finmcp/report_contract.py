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
而矩阵全标成了 ✅。渲染层加一句新的提示语就要往这里加一行,有测试盯着:它去 research.py
数所有打印出来的提示语,**f-string 也算**(占位符压成骨架后按子串匹配),否则像
``历史资金流向只取到 {}/{} 个交易日`` 这种带插值的句子会从守卫眼里漏出去。
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


def price_decimals(symbol: str) -> int:
    """价格按标的最小变动价位渲染的小数位：ETF/基金 0.001 元，个股与指数两位小数。

    上游对 ETF 给的就是三位小数。截成两位对一元上下的品种是 0.5% 以上的价差，两端都截的
    单日涨跌能差出一个百分点——和下游按 ±0.5% 划档的统计同量级。A 股个股最小变动 0.01，
    两位小数不丢信息，输出保持原样。

    资金流历史表的收盘价、价格量纲的技术指标（MACD、布林带）跟着同一个位数走：一元上下的
    ETF 的 MACD 在 ±0.05 以内，两位小数下 DIF 和 DEA 常印成同一个数，看不出谁在上。
    """
    return 3 if classify(symbol) == ETF else 2


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
    # 括号是"这个数还额外要谁的输入"，不是"两份数据拼起来"（那是 basic_info 里
    # ``eastmoney+tencent`` 的写法）。静态市盈率 = 实时总股本 × 现价 ÷ 财务年度净利润。
    Dimension("市盈率(静)", "- 市盈率(静):", "realtime(财务净利润)", applies_to=STOCK_ONLY),
    Dimension("市盈率(动)", "- 市盈率(动):", "realtime", applies_to=STOCK_ONLY),
    Dimension("市净率", "- 市净率:", "realtime", applies_to=STOCK_ONLY),
    # 名字带"收益率"不代表是实时口径：这一行的值只有财务表提供（`cn_stock_source`
    # 从同花顺财务指标的 ``净资产收益率`` 列填 ``stock_data.roe``）。标成 realtime 时
    # 发版报告会把"同花顺挂了"写成"realtime 源没取到 净资产收益率"，归因指向错的源。
    Dimension("净资产收益率", "- 净资产收益率:", "finance", applies_to=STOCK_ONLY),
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
    "历史资金流向只取到": "资金流历史行数不足本次请求，其余天数上游没给",
    "当日资金流只取到部分档": "当日资金流的五档里上游没给全，缺档已在行内点出",
    "暂无数据": "该维度取到空值",
    "获取失败": "该维度取数失败",
}


#: 降级提示语归属哪一维。算单维可用率时"段落在但写着暂无"要算**没拿到**——问的是
#: 返回了数据没有，一个空段落对使用者和整段消失是一回事。
#:
#: 只归属**渲染层确实会打印、且位置明确**的那几句(见 research.py 里各段的边界)。
#: ``暂无数据`` / ``获取失败`` 是防御性的通用词，渲染层并不产出，归不到具体维度，
#: 不进这张表——宁可少算一次降级，也不要把它记到无辜的维度头上。
#:
#: ``历史资金流向`` 一行都没有时整段不打印(``if not indices: return``)，由 ``scan``
#: 直接判为缺失；**只给到一部分行**时走下面这条降级标记——可用率只有拿到/没拿到两
#: 个格子，请求 60 行只回来 1 行属于后者。拦下来不是为了记分，是为了让"这一维缺了"
#: 不再以 SUCCESS 的形态出现：短供的报告本来就不该进跨请求缓存(见 cn_stock_source
#: 的 ``fund_flow:partial``)，而那道判定需要一个能看见短供的前提。
#:
#: 别拿历史比例当这件事的频率：下游 866 段 ``full`` 里只给到 1 行的是 5 段(0.6%)、
#: 整段没有的是 52 段(6.0%)，而当前服务器窗口 512 次 ``fund_flow_outcome`` 里
#: "要 60 行只给到更少" 是 **0 次**。这是一道防线，不是在止一处正在流的血。
DEGRADED_DIMENSION = {
    "暂无实时资金流向": "资金流向",
    "暂无资金流向数据": "资金流向",
    "盘中实时数据暂时不可用": "资金流向",
    "历史资金流向只取到": "历史资金流向",
    "暂无财务数据": "财务数据",
    "暂无年度财务数据": "财务数据",
}

#: 这一句出现时，那一维是**正当缺席**——不进分母，也不算没拿到。
#:
#: 和上面那张表的区别是"能不能怪源"。钉了日期的查询问的是过去某天，那天没有"实时"
#: 资金流可言，渲染层因此只打一行提示(``IS_HISTORICAL_QUERY`` 分支)，换任何源都
#: 一样——和 ETF 没有财务报表是同一类。算成降级的话，一批钉日期的重放就能把资金流
#: 的可用率从 100% 打到 64%，而什么都没坏。
#:
#: 对**探活**是另一回事：探活一律不钉日期，这一句在那里出现就说明有人给探活加了
#: ``date=``，是 bug 不是缺席。所以只在这里放行，``DEGRADED_MARKERS`` 里仍然留着它。
NOT_APPLICABLE_MARKERS = {
    "指定日期查询暂不展示实时资金流向": "资金流向",
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
    "ALL_CLASSES", "CONTRACT", "DEGRADED_DIMENSION", "DEGRADED_MARKERS",
    "Dimension", "ETF", "INDEX", "NOT_APPLICABLE_MARKERS",
    "STOCK", "STOCK_ONLY", "classify", "degraded_in", "expected", "is_index", "scan",
]
