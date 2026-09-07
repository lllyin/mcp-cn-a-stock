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


@dataclass(frozen=True)
class FundFlowRequest:
    """一次取数的全部输入。"""

    code: str                       # 纯六位码
    symbol: Optional[str] = None    # SH600519 这种带市场前缀的
    is_index: bool = False

    @property
    def exchange(self) -> str:
        """AkShare 的 ``market`` 参数：``sh`` / ``sz``。

        规则原样搬自 ``cn_stock_source._fetch_fund_flow_sync``，一个字都没改：指数看
        前缀，个股看代码首位。北交所落进 ``sz``——AkShare 里 sz 和 bj 映射到同一个
        市场号 0，所以 secid 一样，不影响结果。
        """
        if self.is_index:
            return "sh" if (self.symbol or "").startswith("SH") else "sz"
        return "sh" if self.code.startswith("6") else "sz"

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


def _longer(accumulated, value):
    """合成规则：留行数多的那份。

    只在前一个源"给了但不完整"时才轮到后面的源，而后面的源（delay）只有一行；
    它不该覆盖掉前面几行的历史。等长时保留先到的——先配置的优先级更高。
    """
    if accumulated is None:
        return value
    return value if value.rows > accumulated.rows else accumulated


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
    "registered",
    "resolve",
]
