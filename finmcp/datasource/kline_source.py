"""K 线这一维：请求类型、归一后的契约，以及它在平台层上的入口。

源本身在 ``platforms/`` 下（腾讯、新浪、同花顺，以后的雪球），这里只负责三件事：
定义请求长什么样、声明归一后的契约、把配置顺序喂给通用的 ``platform.resolve()``。
架构见 docs/architecture.md。

**接一个新的 K 线源不用改这个文件**——写一个 ``platforms/<名字>.py``，在
``platforms/__init__.py`` import 一行，再把名字加进 ``KLINE_PROVIDERS``。
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..config import KLINE_MAX_GAP_TRADING_DAYS
from . import platform as pf, trading_calendar
from .kline_frame import FALLBACK_FRAME_COLUMNS, _market_prefixed_symbol

logger = logging.getLogger("finmcp")

CAPABILITY = "kline"
PROVIDER_ORDER_ENV = "KLINE_PROVIDERS"
# 兜底源按标的类别分两条顺序。判据只有一个：**哪一家更贴近主源东财**——兜底的
# 本分是让人看不出走了兜底，不是自己另开一套口径。两边都是实测出来的：
#
#   指数（INDEX_PROVIDER_ORDER）
#     创业板指 2026-09-04 成交量：东财/同花顺 200,462,510 手，腾讯/新浪 193,413,042 手
#     （低 3.52%）。上证/深证/科创50 三家一致。同花顺不覆盖北交所，新浪接住。
#
#   其余（DEFAULT_PROVIDER_ORDER）
#     个股的分歧比指数小两个数量级，而且**准的那一家是同花顺**。SZ000333 美的
#     2026-09-05 用同花顺 + 平安证券两家人工核对（截至 09-04）：
#
#         周期     券商核对值    同花顺              腾讯/东财
#         MA5      87.40      87.404            87.404        一致
#         MA10     86.85      86.850            86.850        一致
#         MA20     85.58      85.578            85.578        一致
#         MA60     82.23      82.229  ✅        82.242 (0.015%)
#         MA120    78.73      78.734  ✅        78.780 (0.064%)
#         MA240    76.03      76.026  ✅        76.091 (0.080%)
#
#     所以个股这一条**不是按准确度排的**——0.08% 已经小到可以忽略，排序按稳定性：
#       - 请求数：腾讯一次拿完一个窗口，同花顺按年取，跨年的窗口要两次
#       - 成熟度：腾讯的解析由 AkShare 维护，同花顺那个客户端是本项目自己写的
#         （JSONP 剥壳、沪市指数内部码映射），出问题只能自己修
#       - 限流：同花顺的限流策略未知，而个股是流量大头（指数就那么四个）
#     指数那条则相反：3.52% 不是可以忽略的量级，所以那里认准确度不认稳定性。
#
#     注意别把这段读成"腾讯更准"——之前确实这么写过，依据是"腾讯和东财逐位一致"，
#     而券商核对证明东财自己在长周期上就是偏的。判据是稳定性，不是准确度。
#
# 这是按**类别**分，不是按标的——同一类里不再有例外，加一只新股票不用改任何东西。
DEFAULT_PROVIDER_ORDER = ("tencent", "sina")
INDEX_PROVIDER_ORDER = ("tonghuashun", "tencent", "sina")
INDEX_PROVIDER_ORDER_ENV = "KLINE_PROVIDERS_INDEX"

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
    def is_index(self) -> bool:
        """是不是指数。只用于选兜底源的顺序。

        用结构规则而不是名单：沪市 000/沪市 1A1B、深市 399、北交所 899。名单会漏，
        而漏判一个指数在这里的后果是它拿到偏低 3.5% 的成交量。
        """
        from .kline_frame import _is_index_code

        return _is_index_code(self.prefixed)

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


#: 缺口闸门的两个开关，见 ``_has_no_gap`` 与 ``resolve``。用 ContextVar 而不是模块
#: 全局：这个服务是多线程并发跑多批标的的，全局变量会让 A 标的的宽松模式漏给 B。
_ALLOW_GAPS = contextvars.ContextVar("kline_allow_gaps", default=False)
_GAP_REJECTED = contextvars.ContextVar("kline_gap_rejected", default=False)


def _has_no_gap(frame) -> bool:
    """相邻两根之间不允许缺超过 ``KLINE_MAX_GAP_TRADING_DAYS`` 个**交易日**。

    这一条防的是**序列断裂**，而断裂比缺列危险得多：缺列会让契约当场判失败、
    链路回退；断裂的序列列是齐的、每个数值也在合理区间，源"成功"返回，
    没有任何东西会拦它。

    实际发生过：``SH000688 涨跌幅`` 报成 +20.17%（真实 +2.41%）——同花顺某个年份
    文件取失败被静默跳过，当天前面那一根从 1577.36 变成了
    1344.07，涨跌幅是跨缺口算的。同一份报告里 240 日均价 1540 → 1148、
    240 日最高 2255 → 1626（等于当天最高，历史高点整段没了）。

    **为什么查缺口而不是查涨跌幅越界**：越界只拦得住缺口正好落在最后一根之前的
    情形；缺口在序列中间时当日涨跌幅是对的，而均线仍然全错——今天那份报告两种
    症状都有。查缺口一次覆盖两者，而且对所有 K 线源生效，不只同花顺。

    **单位是交易日，不是自然日。** 数自然日曾经是这里的写法，而它只是「别把长假
    当缺口」的代理指标：得坐在最长连休之上、又远低于「丢掉一整年」。实测那个余量
    很薄——2015 年以来春节最长 11 个自然日，而当时的阈值是 15。问交易日历就没有这个
    问题：长假之间**缺 0 个交易日**（33 条真实序列实测全为 0），阈值于是只需要回答
    一个真问题——容忍几天**停牌**。

    默认 10 个交易日：2018 年之后重大资产重组停牌基本以 10 个交易日为上限，所以
    合规范围内的停牌不会触发额外那一轮回退；而漏掉一个年份文件是 242 个交易日、
    漏一个季度是 60，都远在阈值之上。日历取不到时退回按星期数，那时候最长的长假
    会被数成 6，仍在 10 之内（见 ``trading_calendar.missing_trading_days``）。

    **这是偏好不是硬条件**，判死之前先看 ``resolve``：真实的长期停牌（重大资产重组、
    ST）会让每个源都给出同样带缺口的序列，那时候缺口是真的，硬拦等于把 K 线这一维
    整个抹掉——比均线偏一点严重得多。所以全都拦下来之后会宽松再问一轮。
    """
    if KLINE_MAX_GAP_TRADING_DAYS <= 0 or _ALLOW_GAPS.get():
        return True
    if "日期" not in getattr(frame, "columns", ()) or len(frame) < 2:
        return True
    import pandas as pd

    parsed = pd.to_datetime(frame["日期"], errors="coerce").dropna()
    if len(parsed) < 2:
        return True
    days = sorted(parsed.dt.date)
    # 日历只取一次：load() 每次都要过缓存层，240 根逐对去问就是 240 次。
    calendar = trading_calendar.load()
    worst, at = 0, None
    for earlier, later in zip(days, days[1:]):
        missing = trading_calendar.missing_trading_days(earlier, later, calendar)
        if missing > worst:
            worst, at = missing, later
    if worst <= KLINE_MAX_GAP_TRADING_DAYS:
        return True
    _GAP_REJECTED.set(True)
    logger.warning(
        "K 线序列缺了 %d 个交易日（上限 %d），判该源失败让链路回退；"
        "断裂的序列会让涨跌幅跨缺口计算、均线全错 断点≈%s 行数=%d 日历=%s",
        worst, KLINE_MAX_GAP_TRADING_DAYS, at, len(frame),
        getattr(calendar, "source", None) or "无（按星期）",
    )
    return False


def _honours_kline_contract(frame) -> bool:
    """归一后必须是带标准列的日线表，且序列不能有缺口。

    只判类型说明不了列对不对，而**列不对正是接一个新源最容易出的错**：同花顺的
    原始列序是"开高低收"，别家是"开收高低"，抄错一个位置报告里的最高价就成了收盘价，
    而两个数都在合理区间，肉眼看不出来。
    """
    columns = getattr(frame, "columns", None)
    if columns is None:
        return False
    if not set(FALLBACK_FRAME_COLUMNS) <= set(columns):
        return False
    return _has_no_gap(frame)


pf.define_capability(CAPABILITY, _honours_kline_contract, describe="含标准列的日线表")


def configured_order(request: Optional[KlineRequest] = None) -> tuple:
    """这个请求该按什么顺序问。指数一条，其余一条，理由见上面的常量注释。"""
    if request is not None and request.is_index:
        return pf.configured_order(CAPABILITY, INDEX_PROVIDER_ORDER_ENV, INDEX_PROVIDER_ORDER)
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


def resolve(
    request: KlineRequest,
    *,
    order: Optional[tuple] = None,
    status: Optional[dict] = None,
) -> Optional[KlineResult]:
    """按配置顺序问每个平台，第一个给出非空结果的赢。

    比通用的 ``pf.resolve`` 多一件事：缺口闸门（``_has_no_gap``）是**偏好**，所以
    一轮下来一个源都不剩、且原因是缺口时，宽松再问一轮。

    为什么要这一轮：带缺口的序列有两种成因，而闸门分不开——
      - 源坏了（同花顺某年文件 404 / 正文被截断）。别的源是好的，第一轮就换到它，
        这一轮不会发生。
      - 这只票真的长期停牌（重大资产重组、ST）。缺口在真实数据里，每个源都一样，
        第一轮会把所有源都拦掉。这时候硬拦的结果是 K 线、均线整段消失，比均线偏
        一点严重得多（AGENTS.md §一：数据完整 > 功能正确）。

    额外那一轮只在第一轮**确实被缺口拦过**时发生（``_GAP_REJECTED``），所以纯网络
    失败不会付这个代价——那种情况重问一遍也是全失败。
    """
    resolved_order = configured_order(request) if order is None else order
    local = {} if status is None else status

    token = _GAP_REJECTED.set(False)
    try:
        resolved = pf.resolve(CAPABILITY, request, order=resolved_order, status=local)
        rejected_for_gaps = resolved is None and _GAP_REJECTED.get()
    finally:
        _GAP_REJECTED.reset(token)

    if rejected_for_gaps:
        logger.warning(
            "%s 每个 K 线源都带缺口，按真实停牌处理、放行带缺口的序列："
            "长周期均线会偏，但总比这一维整段缺失强 source=%s",
            request.code, ",".join(resolved_order) or "（空）",
        )
        local["kline_gap_tolerated"] = True
        relaxed = _ALLOW_GAPS.set(True)
        try:
            resolved = pf.resolve(CAPABILITY, request, order=resolved_order, status=local)
        finally:
            _ALLOW_GAPS.reset(relaxed)

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
