"""财务数据的多级回退。

报告的财务那一整段——`净资产收益率`、`每股收益`、`每股净资产`、`主营收入`、`净利润`，
以及要用净利润当分母的 `市盈率(静)`——原先只来自
``ak.stock_financial_abstract_ths``（同花顺 ``basic.10jqka.com.cn/new/{code}/finance.html``）。
**一个维度挂在一次 HTTP 上**，那个主机一次 TLS 抖动就整段消失。

实测频率（服务器归档日志 11 天，1575 次财务取数）：失败 **6 次 = 0.38%**，全部是传输层
（3 次 ``SSLEOFError``、3 次 ``RemoteDisconnected``），没有一次是同花顺返回了坏数据。也
就是说是链路抖动，不是口径问题——补一个独立主机的源就能吃掉绝大部分。

现成的 provider：

``ths``
    同花顺财务摘要。现状，原样搬进来，仍然是主源：列最全（25 列），而且健康路径的输出
    要逐字不变。

``sina``
    新浪 ``stock_financial_abstract``（``money.finance.sina.com.cn``，独立主机）。一次调用
    带全部历史期（实测 43~118 期），数值列本身就是 float，不用解析"亿/%"字符串。
    2026-09-21 与同花顺逐字段核对 12 个个股（含科创板、北交所、深市主板）：**常用指标里
    这 5 项 12/12 全有**，最新报告期取值与同花顺同值。

两个必须记住的映射陷阱
--------------------

1. **同花顺的 ``净利润`` 对应新浪的 ``归母净利润``，不是新浪的 ``净利润``。** 8/8 标的
   核过（600519 都是 445.17亿）。新浪那两个是不同的数，取错会静默差 44%（688981 是
   44.67 vs 64.39亿），而这个数直接进 ``市盈率(静)`` 的分母。
2. **比率不能按 float 交付。** ``_parse_numeric_column(is_percent=True)`` 只对以 ``%``
   结尾的**字符串**除以 100，float 走的是原样返回那条分支。同花顺给 ``"8.58%"``、新浪给
   ``8.58``——直接把新浪的 float 塞进 ``净资产收益率`` 列，ROE 会差 **100 倍**（报告打成
   ``858.00%``）。所以适配层交付的必须是**和同花顺同一形态**的表，见
   :func:`normalize_sina`。

不合并两个源的列
--------------

``basic_info.resolve`` 会按字段把后面的源补到前面缺的位上。这里**故意不这么做**：财务表
的每一列都是按报告期对齐的位置数组，两个源的报告期轴不同（实测 15 个标的里新浪独有
19 期、同花顺独有 5 期），按列拼接必须先进一个日期索引，错一位就是把 2025 年的净利润
配到 2026 年的 ROE 上——那种错看不出来说不定还会进缓存。先出数者全份胜出，顺序由配置
定，符合"按固定顺序判，不能让谁先返回决定数据"。

两套口径唯一的分歧：**年度净资产收益率**
-------------------------------------

按报告期 join 后逐项比过 15 个标的、1029 个同期：最新报告期 15/15 一致，净利润、每股
收益、每股净资产在**报告真正渲染的那几个期**（最新行 + 最近 5 个年度期，90 项）上
0 项不同（最大 0.44%，是同花顺只给两位小数造成的）。唯一分歧是年度净资产收益率，
90 项里 2 项不同：SZ002371 的 2024 年度 24.83% 对 20.63%、SH601318 的 2022 年度
10.10% 对 13.20%。既不是同花顺的 ``净资产收益率`` 也不是它的 ``净资产收益率-摊薄``
（2024 年那期摊薄是 18.09%），是第三套口径。

处理方式：主源永远是同花顺（换源不会让健康路径变数）；真走了回退，那份报告的财务段
会写明这一节来自新浪，``verify_release`` 的 ``KNOWN_DIFFERENCES`` 里也记着这条口径差，
免得第二天主源恢复时那一次跳动作废重来查一遍。
"""

from __future__ import annotations

import abc
import logging
import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..config import env
from ..observability import log_context

logger = logging.getLogger("finmcp")

PROVIDER_ORDER_ENV = "FINANCE_PROVIDERS"
DEFAULT_PROVIDER_ORDER = ("ths", "sina")
_DISABLED = {"", "off", "none", "0", "false"}

#: 下游（``cn_stock_source`` 的财务装配段）实际读的列。适配层产出的表必须有这六列，
#: 且**行序从旧到新**（``index 0`` 是最早的报告期，``-1`` 是最新）——渲染层用
#: ``yearly_fin_index`` 从尾往前找最新年度期，顺序反了就会取到二十年前的数。
FINANCE_COLUMNS = (
    "报告期", "净利润", "营业总收入", "基本每股收益", "每股净资产", "净资产收益率",
)

_SINA_COMMON_SECTION = "常用指标"
_SINA_INDICATORS = {
    "净利润": "归母净利润",
    "营业总收入": "营业总收入",
    "基本每股收益": "基本每股收益",
    "每股净资产": "每股净资产",
    "净资产收益率": "净资产收益率(ROE)",
}
_PERIOD_COLUMN = re.compile(r"^\d{8}$")


#: 主源。健康路径永远先问它，报告正文因此不会因回退而变数。
PRIMARY_PROVIDER = "ths"
#: 报告里给读者看的名字，不是配置里的 provider 名。
PROVIDER_LABELS = {"ths": "同花顺", "sina": "新浪"}


def provider_label(name: str) -> str:
    return PROVIDER_LABELS.get(name, name)


class FinanceProvider(abc.ABC):
    """一个财务数据来源。"""

    name: str = ""

    @abc.abstractmethod
    def fetch(self, code: str, symbol: str) -> Optional[pd.DataFrame]:
        """取一次；取不到返回 None，不要抛给调用方。

        返回的表必须是 :data:`FINANCE_COLUMNS` 那六列的形状（多余的列无所谓）。
        """


@dataclass(frozen=True)
class FinanceFrame:
    """一张财务表，和给出它的那个源。

    源必须跟着数据走到渲染层：两个源的**年度**净资产收益率不是一套口径（实测
    2026-09-21，15 个标的 90 个渲染期里 2 个不同，SZ002371 的 2024 年度
    24.83% 对 20.63%）。不回传来路，换源的那一份报告就会凭空跳一个数且无从解释。
    """

    provider: str
    frame: pd.DataFrame


_PROVIDERS: dict[str, FinanceProvider] = {}


def register(provider: FinanceProvider, *, replace: bool = False) -> None:
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
            logger.warning("未知的财务数据来源 %s；可用: %s", name, ",".join(registered()) or "-")
    return tuple(order)


def resolve(code: str, symbol: str, *,
            order: Optional[tuple] = None) -> Optional[FinanceFrame]:
    """按配置顺序问，第一个给出完整表的源全份胜出。

    回退结果和主源结果走**同一个**进程内缓存（``FINANCE_CACHE_TTL_SECONDS``，默认 6 小时），
    所以主源抖一下，这段时间内都用回退那一份。这是刻意的：不缓回退就等于每次请求都先去
    敲一台正在拒连的主机，而两家的差只有已登记的那一处年度口径，数据本身都是季度披露的。
    """
    names = order if order is not None else configured_order()
    attempted = []
    outcome = None
    for name in names:
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        attempted.append(name)
        try:
            candidate = provider.fetch(code, symbol)
        except Exception as error:  # noqa: BLE001 - 一个源挂了要继续问下一个
            logger.warning("财务数据来源 %s 取数失败 %s: %s", name, code, error)
            continue
        missing = [column for column in FINANCE_COLUMNS if candidate is None
                   or candidate.empty or column not in candidate.columns]
        if missing:
            logger.warning(
                "财务数据来源 %s 没给全 %s 的财务表，缺 %s；继续下一个源",
                name, code, ",".join(missing),
            )
            continue
        outcome = FinanceFrame(provider=name, frame=candidate)
        break
    request_id, tool, _ = log_context()
    logger.info(
        "finance_outcome request_id=%s tool=%s symbol=%s final_source=%s rows=%s attempted=%s",
        request_id, tool, symbol,
        outcome.provider if outcome is not None else "-",
        0 if outcome is None else len(outcome.frame),
        ",".join(attempted) or "-",
    )
    return outcome


# ── 同花顺 ────────────────────────────────────────────────────────


class ThsFinanceProvider(FinanceProvider):
    """同花顺财务摘要。列名就是下游读的那六个。"""

    name = "ths"

    def fetch(self, code: str, symbol: str) -> Optional[pd.DataFrame]:
        import akshare as ak

        frame = ak.stock_financial_abstract_ths(symbol=code)
        if frame is None or frame.empty:
            return None
        return frame


# ── 新浪 ──────────────────────────────────────────────────────────


class SinaFinanceProvider(FinanceProvider):
    """新浪财务指标摘要，同花顺的独立回退。"""

    name = "sina"

    def fetch(self, code: str, symbol: str) -> Optional[pd.DataFrame]:
        import akshare as ak

        return normalize_sina(ak.stock_financial_abstract(symbol=code))


def _as_float(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def normalize_sina(raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """把新浪那张"指标 × 报告期"的宽表转成同花顺那张长表的形状。

    交付形态和同花顺逐项对齐，是为了让下游只用一套解析：比率带 ``%``（``is_percent``
    靠它才除以 100）、金额带 ``亿``（同理 ×1e8）、每股类是纯数、``报告期`` 是
    ``YYYY-MM-DD``。缺精度就白丢，所以亿保留 6 位小数、比率保留 4 位，都远高于报告
    ``:.2f`` 的显示精度。
    """
    if raw is None or raw.empty or "选项" not in getattr(raw, "columns", []):
        return None
    section = raw[raw["选项"] == _SINA_COMMON_SECTION]
    if section.empty:
        return None
    indicators = section.drop_duplicates(subset="指标", keep="first").set_index("指标")
    periods = sorted((str(column) for column in raw.columns if _PERIOD_COLUMN.match(str(column))))
    if not periods:
        return None

    rows = []
    for period in periods:  # 新浪的列从新到旧，这里排序成从旧到新
        record = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:]}"}
        for column, indicator in _SINA_INDICATORS.items():
            value = None
            if indicator in indicators.index:
                value = _as_float(indicators.at[indicator, period])
            if value is None:
                # 缺值一律留成 None：`_parse_numeric_column` 对 None 和 "--" 一样给 0，
                # 和同花顺那张表里的空缺同一个表现。填 0 会把"没披露"说成"是 0"。
                record[column] = None
            elif column in ("净利润", "营业总收入"):
                record[column] = f"{value / 1e8:.6f}亿"
            elif column == "净资产收益率":
                record[column] = f"{value:.4f}%"
            else:
                record[column] = value
        rows.append(record)
    return pd.DataFrame(rows, columns=list(FINANCE_COLUMNS))


for _provider in (ThsFinanceProvider(), SinaFinanceProvider()):
    register(_provider)

__all__ = [
    "DEFAULT_PROVIDER_ORDER", "FINANCE_COLUMNS", "FinanceFrame", "FinanceProvider",
    "PROVIDER_ORDER_ENV", "configured_order", "normalize_sina", "register", "registered",
    "resolve", "unregister",
]
