"""个股 / 指数资金流这一维：请求类型、归一后的契约，以及它在平台层上的入口。

源在 ``platforms/eastmoney.py``：``eastmoney`` 是 push2his 的 ``fflow/daykline``（经
AkShare 封装，给全部历史），``eastmoney_delay`` 是同一个接口在 push2delay 主机上的
副本——**只回最近一天**，但它不在伪装通道的接管名单里，push2his 拒绝出口 IP 的同一
时刻它仍应答（2026-09-06 实测，三个标的当日行逐字节相同）。

为什么要第二条 HTTP 路：浏览器页面兜底覆盖不到没有资金流向页面的标的（科创 50 这类
指数），主源一次 ``RemoteDisconnected`` 就整维缺失。2026-09-06 14:35 那轮的
3 项缺失全在 ``SH000688`` 身上。

契约是 ``FundFlowHistory``：一张 AkShare 列名的日表，外加 ``complete``——这份是不是
该源能给的全部历史。编排层靠它决定还要不要付一次页面加载：``eastmoney`` 给的是全部
（哪怕新股只有 5 行），不用再补；``eastmoney_delay`` 只有一行，有页面的标的还该去
页面把 120 行历史取回来。**页面兜底不在这条链里**：它走浏览器、有自己的名额和熔断，
挂在 ``cn_stock_source`` 的 gather 之后，见那里的注释。

接一个新的资金流源不用改这个文件——写 ``platforms/<名字>.py`` 里的
``fetch_fund_flow``，再把名字加进 ``FUND_FLOW_PROVIDERS``。
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from . import platform as pf

logger = logging.getLogger("finmcp")

CAPABILITY = "fund_flow"
PROVIDER_ORDER_ENV = "FUND_FLOW_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("eastmoney", "eastmoney_delay")

#: 归一后的列，就是 ``ak.stock_individual_fund_flow`` 的列。金额是元，占比是百分数
#: （7.79 表示 7.79%），涨跌幅同样是百分数——下游 ``_build_fund_flow_history`` 按这个
#: 口径乘 0.01，所以谁提供都必须归一到它，不能有一家给小数。
FUND_FLOW_COLUMNS = (
    "日期", "收盘价", "涨跌幅",
    "主力净流入-净额", "主力净流入-净占比",
    "超大单净流入-净额", "超大单净流入-净占比",
    "大单净流入-净额", "大单净流入-净占比",
    "中单净流入-净额", "中单净流入-净占比",
    "小单净流入-净额", "小单净流入-净占比",
)

#: 一致性检测的容差。原始接口的金额是精确值，四档之和恒为 0、主力恒等于超大+大；
#: 页面兜底的金额经两位小数渲染再解析回来，误差随显示单位缩放：万粒度每格
#: ±50 元（四桶之和 ≤ 几百元），亿粒度每格 ±0.005 亿 = ±50 万元（四桶之和
#: 可达 ±200 万——2026-09-13 实测 3 月老日期整批误报的根源，当时容差按
#: 万粒度定为 1 万元）。所以页面路径的容差按行内最大金额的显示单位放宽，
#: API 路径维持严格；「扰动副本」档的偏差在 30%-50%，远高于两者。
#: 占比是两位小数的舍入值，主力+中+小三项之和恒为 0，每列 ±0.005 的舍入
#: 给 0.05 的和容差（与量级无关）。
_AMOUNT_TOLERANCE = 1e4      # 元（API 路径；页面万粒度同值）
_PAGE_YI_TOLERANCE = 2.1e6   # 元（页面亿粒度：0.01 亿步进的四桶舍入上界）
_PAGE_YI_SCALE = 1e8         # 行内最大金额到这个量级，页面按亿显示
_RATIO_TOLERANCE = 0.05      # 百分点


@dataclass(frozen=True)
class FundFlowRequest:
    """一次取数的全部输入。"""

    code: str                       # 纯六位码
    symbol: Optional[str] = None    # SH600519 这种带市场前缀的
    is_index: bool = False

    #: 纯代码猜市场时算沪市的前缀。``6`` 是主板加科创板 688，``5`` 是沪市基金/ETF。
    #:
    #: **不要加 ``9``。** 沪市 B 股是 900xxx，但北交所是 92xxxx，两者都以 9 开头，
    #: 加了会把北交所判成沪市。B 股这一档就先空着——本项目的标的池里没有 B 股，而
    #: 猜错市场的后果是静默取空（见 ``exchange``），不值得为一个没人查的档位冒险。
    _SHANGHAI_CODE_PREFIXES = ("6", "5")

    @property
    def exchange(self) -> str:
        """AkShare 的 ``market`` 参数：``sh`` / ``sz``。

        **带前缀的 ``symbol`` 优先。** 它是调用方给的权威答案，而按纯代码猜市场是
        有洞的：这里原先写 ``code.startswith("6")``（连同上面那句"原样搬自
        ``cn_stock_source``，一个字都没改"），把**沪市 5 开头的基金全判成深市**。

        踩过，而且是在线上：``SH512480`` 算出 secid ``0.512480``，东财返回
        ``rc=100``、0 行；正确的 ``1.512480`` 有 121 行。2026-09-08 的表现是伪装通道
        一进冷却，``eastmoney`` 被跳过、``eastmoney_delay`` 也因为共用这个错 secid 而
        拿不到，于是沪市 ETF 的资金流向整维变成"暂无资金流向数据"——当天落地 4 次
        （512480、588200 各两次，都在盘前和收盘后）。个股没露出来，是因为它们的
        secid 本来就是对的。同一秒的日志里 AkShare 那条 base_info 用的是
        ``secid=1.512480``，两处算法不一致正是这个 bug 的现场证据。

        没有 ``symbol`` 时才退回按代码猜，前缀表见 ``_SHANGHAI_CODE_PREFIXES``。
        其余一律 ``sz``——北交所也落这里，AkShare 的 market_map 里 sz 和 bj 是同一个
        市场号 0，secid 一样，不影响结果。

        ``is_index`` 不再参与判断：指数原先就是看 ``symbol`` 前缀的，而现在所有标的
        都先看它，那一支单独的分支已经被这一条包含了。
        """
        prefixed = (self.symbol or "").upper()
        if prefixed.startswith("SH"):
            return "sh"
        if prefixed.startswith(("SZ", "BJ")):
            return "sz"
        return "sh" if self.code.startswith(self._SHANGHAI_CODE_PREFIXES) else "sz"

    @property
    def secid(self) -> str:
        """东财接口的 ``secid``：沪市 1、其余 0，和 AkShare 的 ``market_map`` 一致。"""
        return f"{1 if self.exchange == 'sh' else 0}.{self.code}"


@dataclass(frozen=True)
class FundFlowHistory:
    """归一后的资金流日表，外加这份是不是该源能给的全部历史。"""

    frame: object          # pandas.DataFrame，列见 FUND_FLOW_COLUMNS
    complete: bool = True

    @property
    def rows(self) -> int:
        return 0 if self.frame is None else len(self.frame)


@dataclass(frozen=True)
class FundFlowResult:
    frame: object
    provider: str
    complete: bool


def _honours_contract(value) -> bool:
    """必须是带全部标准列、至少一行的表。列不对正是接新源最容易出的错。"""
    if not isinstance(value, FundFlowHistory):
        return False
    columns = getattr(value.frame, "columns", None)
    if columns is None or value.rows == 0:
        return False
    return set(FUND_FLOW_COLUMNS) <= set(columns)


pf.define_capability(CAPABILITY, _honours_contract, describe="含标准列的资金流日表")


def configured_order() -> tuple:
    return pf.configured_order(CAPABILITY, PROVIDER_ORDER_ENV, DEFAULT_PROVIDER_ORDER)


#: 各提供方对应的上游端点。一致性告警带上它，日志读者不用再翻源码查
#: 「eastmoney 给的」到底是哪个接口。新提供方接入时在这里登记。
PROVIDER_ENDPOINTS = {
    "eastmoney": "push2his fflow/daykline(akshare)",
    "eastmoney_delay": "push2delay fflow/daykline",
    "eastmoney_gateway": "push2his fflow/daykline(付费网关)",
    "page_fallback": "zjlx 页面 table_ls",
}


def provider_endpoint(name: Optional[str]) -> str:
    """提供方 → 上游端点描述。未登记的名字原样返回，不让告警哑掉。"""
    return PROVIDER_ENDPOINTS.get(name or "", f"未知来源 {name or '-'}")


def _longer(accumulated, value):
    """合成规则：留行数多的那份。

    只在前一个源"给了但不完整"时才轮到后面的源，而后面的源（delay）只有一行；
    它不该覆盖掉前面几行的历史。等长时保留先到的——先配置的优先级更高。
    """
    if accumulated is None:
        return value
    return value if value.rows > accumulated.rows else accumulated


def consistency_violations(frame, *, rendered: bool = False) -> list:
    """一行一格地报出违反资金流内部恒等式的行。

    恒等式来自分桶的定义本身：主力 = 超大单 + 大单，主力+大+中+小 = 0，
    占比口径同理（主力占比+中单占比+小单占比 = 0）。它们对任何真实交易日都
    成立，所以违反即「这行不是真实成交的账」。

    起因是 2026-09-11 的发现：push2his 会按请求身份给一部分客户端发一份金额
    被扰动过的副本——HTTP 200、行数齐全、收盘价和涨跌幅是真值，只有各单净额
    偏 30%-50%，任何可用性指标都看不见它。这项检测是唯一一道能当场咬住的
    闸门；调用方按返回的 violations 决定记 warnings 还是换源重取。

    ``rendered=True`` 表示这批行来自页面兜底，金额经两位小数的"万/亿"渲染，
    金额容差按行内显示单位放宽（见 _PAGE_YI_TOLERANCE）；占比容差不变。

    只做算术，不发请求、不比 K 线。金额列有 NaN 的行跳过——那是页面占位符
    （停牌之类），让 None 与 0 的区分保持原样。
    """
    issues: list = []
    if frame is None or getattr(frame, "empty", True):
        return issues
    required = {"日期",
                "主力净流入-净额", "超大单净流入-净额", "大单净流入-净额",
                "中单净流入-净额", "小单净流入-净额",
                "主力净流入-净占比", "中单净流入-净占比", "小单净流入-净占比"}
    if not required <= set(frame.columns):
        return issues
    # itertuples 的命名元组会把中文列名改写成 _5 这类位置名，按列位取值。
    order = list(frame.columns)
    for values in frame.itertuples(index=False, name=None):
        row = dict(zip(order, values))
        date = row.get("日期", "?")
        major, xl, big, mid, small = (row.get(name) for name in
            ("主力净流入-净额", "超大单净流入-净额", "大单净流入-净额",
             "中单净流入-净额", "小单净流入-净额"))
        row_issues = []
        amounts_known = [v for v in (major, xl, big, mid, small)
                         if isinstance(v, (int, float))]
        amount_tol = _AMOUNT_TOLERANCE
        if rendered and amounts_known and max(map(abs, amounts_known)) >= _PAGE_YI_SCALE:
            amount_tol = _PAGE_YI_TOLERANCE
        if None not in (major, xl, big) and all(
                isinstance(v, (int, float)) for v in (major, xl, big)):
            drift = major - (xl + big)
            if abs(drift) > amount_tol:
                row_issues.append(f"主力({major:.0f}) != 超大+大({xl + big:.0f})，差 {drift:+.0f} 元")
        if None not in (xl, big, mid, small) and all(
                isinstance(v, (int, float)) for v in (xl, big, mid, small)):
            total = xl + big + mid + small
            if abs(total) > amount_tol:
                row_issues.append(f"四档之和={total:+.0f} 元，应为 0")
        mratio, mratio_mid, mratio_small = (row.get(name) for name in
            ("主力净流入-净占比", "中单净流入-净占比", "小单净流入-净占比"))
        if None not in (mratio, mratio_mid, mratio_small) and all(
                isinstance(v, (int, float)) for v in (mratio, mratio_mid, mratio_small)):
            total = mratio + mratio_mid + mratio_small
            if abs(total) > _RATIO_TOLERANCE:
                row_issues.append(f"占比之和(主力+中+小)={total:+.2f}%，应为 0")
        if row_issues:
            issues.append(f"{date}: " + "；".join(row_issues))
    return issues


@dataclass(frozen=True)
class FundFlowNeed:
    """一次查询对资金流历史的要求。链按它决定"还要不要问下一个源"：
    拿到满足需求的就停，不满足才继续——付费的网关级尤其不能在需求已满足时被调用。
    """

    history_rows: int = 0             # 需要的历史行数（full 的历史表）
    pinned_date: Optional[str] = None  # 钉日期：历史里必须有这一天


def satisfies(history: Optional[FundFlowHistory], need: FundFlowNeed) -> bool:
    """这份历史帧满足本次查询的需求吗。

    全量历史（``complete``）有就有、没有就是谁都没有，定局，不再问下一个源——
    钉了一个非交易日或早于上市日期的，任何源都给不出，为它付费是纯亏。
    部分帧看覆盖：钉日期要精确命中那天（不借最新一行），历史表要行数够。
    """
    if history is None or history.rows == 0:
        return False
    if history.complete:
        return True
    if need is None:
        return False
    # 钉日期是必要条件：部分帧没命中那天就不满足。行数同样要够——full 渲染历史表，
    # 钉今天时 delay 的当日单行虽命中日期但只有 1 行，历史表会缩成一行，不能算满足。
    if need.pinned_date and not _has_date(history.frame, need.pinned_date):
        return False
    return history.rows >= need.history_rows


@dataclass(frozen=True)
class FundFlowSupply:
    """本次请求**该有几行**、**给到几行**，以及要渲染的那几个下标。

    渲染层那句"只取到 N/M"和报告缓存拦不拦，都从这一个判定里取数。两边各自数一遍
    就会数出两个答案：基准取 ``limit`` 的那一侧会把"新股只有 20 个交易日"当成缺 40 天
    而拦住缓存，取 K 线交易日数的那一侧又放过"源自称全份却只给 3 行"。
    """

    indices: tuple[int, ...]     # 要渲染的下标，按日期从新到旧
    want: int                    # 该有几行；0 = 无从判断，不主张缺失

    @property
    def rows(self) -> int:
        return len(self.indices)

    @property
    def short(self) -> bool:
        return 0 < self.want and self.rows < self.want


def fund_flow_supply(flow_dates, kline_dates, limit: int,
                     query_ns: Optional[int] = None) -> FundFlowSupply:
    """数一遍这次能渲染几行、该有几行。

    基准取 ``min(limit, 同一份报告里的 K 线交易日数)`` 而不是 ``limit``：新股上市不足
    limit、K 线窗口本身短于 limit 时那个差额是正当的，报成缺失是把没坏的东西喊出来。
    K 线那一维没给日期时 ``want`` 记 0——宁可不主张缺失，也不误伤。
    """
    limit = int(limit or 0)
    dates = list(flow_dates) if flow_dates is not None else []
    kept = [i for i, day in enumerate(dates) if query_ns is None or day <= query_ns]
    if limit <= 0:
        kept = []
    else:
        kept = kept[-limit:][::-1]
    if kline_dates is None or limit <= 0:
        want = 0
    else:
        kline = [d for d in kline_dates if query_ns is None or d <= query_ns]
        want = min(limit, len(kline))
    return FundFlowSupply(indices=tuple(kept), want=want)


def date_to_ns(value) -> Optional[int]:
    """``YYYY-MM-DD`` → 日期数组用的同一套 ns 时间戳。解析不了返回 None（＝不裁剪）。

    取数层和渲染层要裁的是同一条线，换算也得是同一处：两边各写一遍 ``strptime``，
    一边漏了本地时区口径就会差一天，而差一天的表现是"该报的没报"，看不见。
    """
    if not value:
        return None
    try:
        return int(datetime.datetime.strptime(str(value)[:10], "%Y-%m-%d").timestamp() * 1e9)
    except ValueError:
        return None


def _has_date(frame, pinned: str) -> bool:
    """帧里有没有这一天。日期列在不同来源里是 date 对象或字符串，统一按文本比。"""
    columns = getattr(frame, "columns", None)
    if columns is None or "日期" not in columns:
        return False
    try:
        return bool((frame["日期"].astype(str) == pinned).any())
    except Exception:  # noqa: BLE001 - 列内容异常就当没命中，让链继续走
        return False


def _enough(value: FundFlowHistory) -> bool:
    """拿到完整历史就停，不再问下一个源。"""
    return value.complete


def resolve(
    request: FundFlowRequest,
    *,
    order: Optional[tuple] = None,
    status: Optional[dict] = None,
    need: Optional[FundFlowNeed] = None,
) -> Optional[FundFlowResult]:
    """按配置顺序问每个平台：第一个满足需求的赢；都不满足就留行数最多的。

    ``need`` 为空时维持旧语义：拿到完整历史才停。传了 need 就按需求判定——
    链尾的付费级（eastmoney_gateway）因此不会在需求已满足时被调用。
    """
    enough = _enough if need is None else lambda value: satisfies(value, need)
    resolved = pf.resolve(
        CAPABILITY,
        request,
        order=configured_order() if order is None else order,
        merge=_longer,
        enough=enough,
        status=status,
    )
    if resolved is None:
        return None
    history: FundFlowHistory = resolved.value
    return FundFlowResult(frame=history.frame, provider=resolved.source, complete=history.complete)


def registered() -> tuple:
    return pf.registered(CAPABILITY)


def split_order(order: tuple) -> tuple:
    """把配置顺序切成（页面之前的同步段，页面之后的同步段）。

    页面兜底（``fund_flow_page``）走浏览器、绑事件循环，进不了线程池里的同步
    provider 链，由编排层在 gather 之后执行。它在配置里的位置决定网关级
    （``eastmoney_gateway``）排在它前面还是后面——目标顺序是
    ``eastmoney,eastmoney_delay,fund_flow_page,eastmoney_gateway``：页面是免费的，
    排在付费级前面。配置里没写页面时，页面按既有行为挂在链尾。
    """
    if "fund_flow_page" in order:
        index = order.index("fund_flow_page")
        return order[:index], order[index + 1:]
    return order, ()


__all__ = [
    "CAPABILITY",
    "DEFAULT_PROVIDER_ORDER",
    "FUND_FLOW_COLUMNS",
    "FundFlowHistory",
    "FundFlowNeed",
    "FundFlowRequest",
    "FundFlowResult",
    "PROVIDER_ORDER_ENV",
    "configured_order",
    "consistency_violations",
    "provider_endpoint",
    "registered",
    "resolve",
    "satisfies",
    "split_order",
]
