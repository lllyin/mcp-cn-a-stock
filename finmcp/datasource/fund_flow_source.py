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
#: 页面兜底的金额经两位小数（万）渲染再解析回来，误差上限在千元级。1 万元远高于
#: 舍入、远低于「扰动副本」档 30%-50% 的偏差（2026-09-11 实测主力 +3.3%、
#: 超大 +30.5%、大单 -52.6%，收盘价和涨跌幅是真值）。占比是两位小数的舍入值，
#: 主力+中+小三项之和恒为 0，每列 ±0.005 的舍入给 0.05 的和容差。
_AMOUNT_TOLERANCE = 1e4      # 元
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


def consistency_violations(frame) -> list:
    """一行一格地报出违反资金流内部恒等式的行。

    恒等式来自分桶的定义本身：主力 = 超大单 + 大单，主力+大+中+小 = 0，
    占比口径同理（主力占比+中单占比+小单占比 = 0）。它们对任何真实交易日都
    成立，所以违反即「这行不是真实成交的账」。

    起因是 2026-09-11 的发现：push2his 会按请求身份给一部分客户端发一份金额
    被扰动过的副本——HTTP 200、行数齐全、收盘价和涨跌幅是真值，只有各单净额
    偏 30%-50%，任何可用性指标都看不见它。这项检测是唯一一道能当场咬住的
    闸门；调用方按返回的 violations 决定记 warnings 还是换源重取。

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
        if None not in (major, xl, big) and all(
                isinstance(v, (int, float)) for v in (major, xl, big)):
            drift = major - (xl + big)
            if abs(drift) > _AMOUNT_TOLERANCE:
                row_issues.append(f"主力({major:.0f}) != 超大+大({xl + big:.0f})，差 {drift:+.0f} 元")
        if None not in (xl, big, mid, small) and all(
                isinstance(v, (int, float)) for v in (xl, big, mid, small)):
            total = xl + big + mid + small
            if abs(total) > _AMOUNT_TOLERANCE:
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


def _enough(value: FundFlowHistory) -> bool:
    """拿到完整历史就停，不再问下一个源。"""
    return value.complete


def resolve(
    request: FundFlowRequest,
    *,
    order: Optional[tuple] = None,
    status: Optional[dict] = None,
) -> Optional[FundFlowResult]:
    """按配置顺序问每个平台：第一个给出完整历史的赢；都不完整就留行数最多的。"""
    resolved = pf.resolve(
        CAPABILITY,
        request,
        order=configured_order() if order is None else order,
        merge=_longer,
        enough=_enough,
        status=status,
    )
    if resolved is None:
        return None
    history: FundFlowHistory = resolved.value
    return FundFlowResult(frame=history.frame, provider=resolved.source, complete=history.complete)


def registered() -> tuple:
    return pf.registered(CAPABILITY)


__all__ = [
    "CAPABILITY",
    "DEFAULT_PROVIDER_ORDER",
    "FUND_FLOW_COLUMNS",
    "FundFlowHistory",
    "FundFlowRequest",
    "FundFlowResult",
    "PROVIDER_ORDER_ENV",
    "configured_order",
    "consistency_violations",
    "provider_endpoint",
    "registered",
    "resolve",
]
