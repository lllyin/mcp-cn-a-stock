"""
研究分析模块

根据股票数据生成各类分析报告。
"""

import asyncio
import datetime
import logging
from dataclasses import dataclass
from io import StringIO
from typing import Dict, Optional, TextIO

import numpy as np
import talib
from numpy import ndarray

from .datafeed import load_data_msd
from .config import ALL_INDICES
from .datasource.base import FetchRequirements
from .datasource import trading_calendar
from . import market_session
from .datasource.realtime_ff import get_fund_flow
from .datasource import realtime_fund_flow_source
from .datasource.fund_flow_source import FundFlowRequest
from .symbols import symbol_with_name

logger = logging.getLogger("finmcp")


def compute_kdj(close: ndarray, high: ndarray, low: ndarray, n: int = 9, m1: int = 3, m2: int = 3) -> tuple:
    """
    计算 KDJ 指标
    
    使用中国证券软件通用的递归计算公式（递归权重 (n-1)/n），
    以确保与东方财富、富途等软件显示数值一致。
    K = (2/3)*prev_K + (1/3)*current_RSV
    D = (2/3)*prev_D + (1/3)*current_K
    """
    import pandas as pd
    
    s_close = pd.Series(close)
    s_high = pd.Series(high)
    s_low = pd.Series(low)
    
    # 计算 RSV (Raw Stochastic Value)
    low_min = s_low.rolling(window=n).min()
    high_max = s_high.rolling(window=n).max()
    
    # 避免分母为0
    diff = high_max - low_min
    rsv = (s_close - low_min) / diff * 100
    rsv = rsv.fillna(50)
    
    # 递归计算 K, D (采用 com=m-1 对应的 alpha=1/m)
    # K[t] = (2/3) * K[t-1] + (1/3) * RSV[t]
    k = rsv.ewm(com=m1-1, adjust=False).mean()
    # D[t] = (2/3) * D[t-1] + (1/3) * K[t]
    d = k.ewm(com=m2-1, adjust=False).mean()
    
    # J = 3*K - 2*D
    j = 3 * k - 2 * d
    
    return k.values, d.values, j.values


def compute_macd(close: ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    计算 MACD 指标
    
    使用 TA-Lib 的 MACD 函数计算
    """
    macd, signal_line, hist = talib.MACD(close, fastperiod=fast, slowperiod=slow, signalperiod=signal)
    # 返回 DIF 和 DEA
    return macd, signal_line


TECHNICAL_FIELDS = ("kdj", "macd", "rsi", "bbands")


def parse_technical_fields(fields: str = "all") -> list[str]:
    """Parse requested technical indicator groups."""
    if not fields or fields.strip().lower() == "all":
        return list(TECHNICAL_FIELDS)

    requested = []
    for field in fields.split(","):
        item = field.strip().lower()
        if item in TECHNICAL_FIELDS and item not in requested:
            requested.append(item)
    return requested or list(TECHNICAL_FIELDS)


def _json_number(value) -> float | None:
    """Convert numpy/talib values to JSON-safe numbers."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(val) or np.isinf(val):
        return None
    return val


def get_technical_indicators(
    data: Dict[str, ndarray],
    days: int = 30,
    fields: str = "all",
    include_derived: bool = True,
) -> list[dict]:
    """Return machine-readable technical indicators for recent trading days."""
    if "CLOSE" not in data or "DATE" not in data:
        return []

    close = data["CLOSE"]
    high = data.get("HIGH", close)
    low = data.get("LOW", close)
    open_ = data.get("OPEN", close)
    volume = data.get("VOLUME", np.full_like(close, np.nan))
    dates = data["DATE"]

    if len(close) == 0 or len(dates) == 0:
        return []

    requested_fields = parse_technical_fields(fields)
    days = max(1, int(days or 30))

    kdj_k, kdj_d, kdj_j = compute_kdj(close, high, low, 9, 3, 3)
    macd_diff, macd_dea = compute_macd(close, 12, 26, 9)
    rsi_6 = talib.RSI(close, timeperiod=6)
    rsi_12 = talib.RSI(close, timeperiod=12)
    rsi_24 = talib.RSI(close, timeperiod=24)
    bb_upper, bb_middle, bb_lower = talib.BBANDS(close, matype=talib.MA_Type.T3)

    formatted_dates = [
        datetime.datetime.fromtimestamp(d / 1e9).strftime("%Y-%m-%d") for d in dates
    ]

    indicators = []
    start = max(0, len(formatted_dates) - days)
    for i in range(len(formatted_dates) - 1, start - 1, -1):
        item: dict = {
            "date": formatted_dates[i],
            "ohlc": {
                "open": _json_number(open_[i]),
                "close": _json_number(close[i]),
                "high": _json_number(high[i]),
                "low": _json_number(low[i]),
                "volume": _json_number(volume[i]),
            },
        }
        if "kdj" in requested_fields:
            item["kdj"] = {
                "k": _json_number(kdj_k[i]),
                "d": _json_number(kdj_d[i]),
                "j": _json_number(kdj_j[i]),
            }
        if "macd" in requested_fields:
            dif = _json_number(macd_diff[i])
            dea = _json_number(macd_dea[i])
            macd = {
                "dif": dif,
                "dea": dea,
            }
            if include_derived:
                macd["histogram"] = None if dif is None or dea is None else dif - dea
            item["macd"] = macd
        if "rsi" in requested_fields:
            item["rsi"] = {
                "rsi6": _json_number(rsi_6[i]),
                "rsi12": _json_number(rsi_12[i]),
                "rsi24": _json_number(rsi_24[i]),
            }
        if "bbands" in requested_fields:
            item["bbands"] = {
                "upper": _json_number(bb_upper[i]),
                "middle": _json_number(bb_middle[i]),
                "lower": _json_number(bb_lower[i]),
            }
        indicators.append(item)

    return indicators


async def load_raw_data(
    symbol: str,
    end_date=None,
    who: str = "",
    requirements: Optional[FetchRequirements] = None,
) -> Dict[str, ndarray]:
    """加载股票原始数据"""
    is_historical_query = end_date is not None
    if end_date is None:
        end_date = datetime.datetime.now() + datetime.timedelta(days=1)
    if type(end_date) == str:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d")

    start_date = end_date - datetime.timedelta(days=365 * 2)

    data = await load_data_msd(
        symbol,
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        0,
        who,
        requirements=requirements,
    )
    if data and is_historical_query:
        data["QUERY_DATE"] = end_date.strftime("%Y-%m-%d")  # type: ignore
        data["IS_HISTORICAL_QUERY"] = True  # type: ignore
    return data


def is_stock(symbol: str) -> bool:
    """判断是否为个股（而非指数）"""
    if symbol.startswith("SH6") or symbol.startswith("SZ00") or symbol.startswith("SZ30"):
        return True
    return False


async def build_stock_data(symbol: str, raw_data: Dict[str, ndarray]) -> str:
    """构建完整的股票数据报告"""
    md = StringIO()
    build_basic_data(md, symbol, raw_data)
    await build_trading_data(md, symbol, raw_data)
    build_technical_data(md, symbol, raw_data)
    build_financial_data(md, symbol, raw_data)

    return md.getvalue()


def filter_sector(sectors: list[str]) -> list[str]:
    """过滤掉不重要的板块"""
    keywords = ["MSCI", "标普", "同花顺", "融资融券", "沪股通"]
    return [s for s in sectors if not any(k in s for k in keywords)]


def est_fin_ratio(last_fin_date: datetime.datetime) -> float:
    """估算财务数据的年化比例"""
    if last_fin_date.month == 12:
        return 1
    elif last_fin_date.month == 9:
        return 0.75
    elif last_fin_date.month == 6:
        return 0.5
    elif last_fin_date.month == 3:
        return 0.25
    else:
        return 0


def yearly_fin_index(dates: ndarray) -> int:
    """
    返回日期数组中最后一个12月的索引
    """
    for i in range(len(dates) - 1, -1, -1):
        date = datetime.datetime.fromtimestamp(dates[i] / 1e9)
        if date.month == 12:
            return i
    return -1


def build_basic_data(fp: TextIO, symbol: str, data: Dict[str, ndarray]) -> None:
    """构建基本数据部分"""
    print("# 基本数据", file=fp)
    print("", file=fp)
    
    # 优先使用纠偏后的规范代码
    symbol = data.get("SYMBOL", symbol)
    
    # 优先使用数据源返回的名称，否则从本地配置获取
    name = data.get("NAME", "")
    if not name:
        symbol_name = list(symbol_with_name([symbol]))[0]
        name = symbol_name[1] if symbol_name[1] else symbol
    
    sector_list = data.get("SECTOR", [])
    sector = " ".join(filter_sector(sector_list)) if sector_list else ""
    
    if "DATE" not in data or len(data["DATE"]) == 0:
        print(f"- 股票代码: {symbol}", file=fp)
        print(f"- 股票名称: {name}", file=fp)
        print("- 数据: 无", file=fp)
        return
    
    data_date = datetime.datetime.fromtimestamp(data["DATE"][-1] / 1e9)
    
    # 获取财务数据索引
    last_year_index = -1
    if is_stock(symbol) and "_DS_FINANCE" in data:
        fin, _ = data["_DS_FINANCE"]
        if "DATE" in fin and len(fin["DATE"]) > 0:
            last_year_index = yearly_fin_index(fin["DATE"])

    print(f"- 股票代码: {symbol}", file=fp)
    print(f"- 股票名称: {name}", file=fp)
    print(f"- 数据日期: {data_date.strftime('%Y-%m-%d')}", file=fp)
    if sector:
        print(f"- 行业概念: {sector}", file=fp)
    
    if is_stock(symbol):
        # 总市值、流通市值
        mcap = data.get("MCAP", np.array([]))
        fmcap = data.get("FMCAP", np.array([]))
        if len(mcap) > 0 and mcap[-1] > 0:
            print(f"- 总市值: {mcap[-1]/1e8:.2f}亿", file=fp)
        if len(fmcap) > 0 and fmcap[-1] > 0:
            print(f"- 流通市值: {fmcap[-1]/1e8:.2f}亿", file=fp)
    
    if is_stock(symbol):
        # 总股本
        tcap = data.get("TCAP", np.array([]))
        if len(tcap) > 0:
            total_shares = tcap[-1] if isinstance(tcap[-1], (int, float)) else tcap[-1]
        else:
            total_shares = 0
        
        # 当前价格
        close2 = data.get("CLOSE2", data.get("CLOSE", np.array([])))
        current_price = close2[-1] if len(close2) > 0 else 0
        
        # 净利润
        np_arr = data.get("NP", np.array([]))
        if len(np_arr) > 0 and last_year_index >= 0 and last_year_index < len(np_arr):
            net_profit = np_arr[last_year_index]
        else:
            net_profit = 0
        
        # 计算市盈率
        if total_shares > 0 and current_price > 0:
            total_amount = total_shares * current_price
            pe_static = total_amount / net_profit if net_profit != 0 else float("inf")
            print(f"- 市盈率(静): {pe_static:.2f}", file=fp)
            
            # 动态市盈率 (优先使用数据源直接提供的)
            pe_ttm_arr = data.get("PE_TTM", np.array([]))
            if len(pe_ttm_arr) > 0 and pe_ttm_arr[-1] > 0:
                print(f"- 市盈率(动): {pe_ttm_arr[-1]:.2f}", file=fp)
        
        # 市净率。优先用数据源直接给的口径：总市值 / 最新报告期归母净资产，
        # 分子用最新总股本，与券商终端一致。回退式只能用每股净资产反推，隐含的
        # 是报告期末股本，股本在报告期之后变动过就会偏低——2026-09-03 的
        # SZ300408 两者是 9.78 与 9.38，比值正好是总股本 19.97 亿股与期末
        # 19.16 亿股之比。回退式在实时行情不可用时仍能出数，所以保留。
        pb = None
        pb_arr = data.get("PB", np.array([]))
        if len(pb_arr) > 0 and pb_arr[-1] > 0:
            pb = float(pb_arr[-1])
        else:
            navps = data.get("NAVPS", np.array([]))
            if len(navps) > 0 and navps[-1] != 0 and current_price > 0:
                pb = current_price / navps[-1]
        if pb is not None:
            print(f"- 市净率: {pb:.2f}", file=fp)
        
        # 净资产收益率
        roe = data.get("ROE", np.array([]))
        if len(roe) > 0:
            print(f"- 净资产收益率: {roe[-1]*100:.2f}%", file=fp)
    
    print("", file=fp)


def today_volume_est_ratio(data: Dict[str, ndarray], now: int = 0) -> float:
    """估算今日成交量的比例（用于盘中数据）"""
    if "DATE" not in data or len(data["DATE"]) == 0:
        return 1
    
    data_dt = datetime.datetime.fromtimestamp(data["DATE"][-1] / 1e9)
    now_dt = (
        datetime.datetime.now() if now == 0 else datetime.datetime.fromtimestamp(now / 1e9)
    )

    data_date = data_dt.strftime("%Y-%m-%d")
    now_date = now_dt.strftime("%Y-%m-%d")
    if data_date != now_date:
        return 1
    
    now_time = now_dt.strftime("%H:%M:%S")
    if now_time >= "09:30:00" and now_time < "11:30:00":
        start_dt = now_dt.replace(hour=9, minute=30, second=0)
        minutes = (now_dt - start_dt).seconds / 60
        return 240 / (minutes + 1)
    elif now_time >= "11:30:00" and now_time < "13:00:00":
        return 2
    elif now_time >= "13:00:00" and now_time < "15:00:00":
        start_dt = now_dt.replace(hour=13, minute=0, second=0)
        minutes = (now_dt - start_dt).seconds / 60
        return 240 / (120 + minutes + 1)
    else:
        return 1


#: 该不该走浏览器抓资金流页面。定义在 market_session——它和报告缓存的纪元划分用的
#: 是同两个边界（warmup / final），各写一遍的话改一处就会让纪元横跨翻转点，同一个
#: 纪元里出现两种形状的报告。这里只是个别名，外部按 research.<名字> 调过。
is_realtime_fund_flow_window = market_session.is_realtime_fund_flow_window


FUND_FLOW_FIELDS = [
    ("主力", "A"),
    ("超大单", "XL"),
    ("大单", "L"),
    ("中单", "M"),
    ("小单", "S"),
]


def build_fund_flow(field: tuple[str, str], data: Dict[str, ndarray]) -> str:
    """构建资金流向信息"""
    field_amount = field[1] + "_A"
    field_ratio = field[1] + "_R"
    value_amount = data.get(field_amount, None)
    value_ratio = data.get(field_ratio, None)
    if value_amount is None or value_ratio is None:
        return ""
    if len(value_amount) == 0 or len(value_ratio) == 0:
        return ""

    kind = field[0]
    raw_amount = value_amount[-1]
    ratio = value_ratio[-1]
    
    # 自动转换单位：超过1亿显示亿，否则显示万
    if abs(raw_amount) >= 1e8:
        amount_str = f"{raw_amount / 1e8:.2f}亿"
    else:
        amount_str = f"{raw_amount / 1e4:.2f}万"
    
    # 前缀沿用原来的结构：要么"沪深两市"（大盘口径），要么标时间。时间那个从
    # "今日"改成"当日"——"今日"在非交易日是假话，周末查出来是"今日主力净流入"
    # 配着 09-04 的数；"当日"指的是报告开头那个"数据日期"，和价格、涨跌幅、
    # 成交量那几段用的是同一个词。
    #
    # 只换词不删前缀是有意的：删了行的形状就变了（`- 今日X净流入` → `- X净流入`），
    # 下游按标签正则取数的得改结构；换词的话一次 今日→当日 替换就全覆盖。
    prefix = "沪深两市" if data.get("IS_MARKET", False) else "当日"
    return f"{prefix}{kind}净流入: {amount_str}  {kind}净占比: {ratio:.2%}"


def data_date(data: Dict[str, ndarray]) -> Optional[datetime.date]:
    """报告开头那个"数据日期"，来自 K 线最后一根。"""
    dates = data.get("DATE")
    if dates is None or len(dates) == 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(dates[-1] / 1e9).date()
    except Exception:
        return None


def fund_flow_lag(data: Dict[str, ndarray]) -> Optional[tuple]:
    """资金流比 K 线晚了几天。一致就返回 None。

    返回 ``(资金流日期, 数据日期)``。两个用途：

    - **提示**：报告只写一个"数据日期"，资金流那段没有自己的日期。真不一致时
      读者无从察觉，所以要在 warnings 里说出来。
    - **缓存守卫**：CLOSED 纪元长达 16-64 小时，而 AkShare 的当日资金流行要到
      收盘后一段时间才落地。没落地就把报告冻进去，等于缺一段冻一整晚。

    盘中不算滞后：那时资金流走的是实时抓取，本来就是当天的。
    """
    if is_realtime_fund_flow_window() and not data.get("IS_HISTORICAL_QUERY", False):
        return None
    flow, kline = fund_flow_date(data), data_date(data)
    if flow is None or kline is None or flow >= kline:
        return None
    return flow, kline


def fund_flow_date(data: Dict[str, ndarray]) -> Optional[datetime.date]:
    """这批资金流数据是哪一天的。取自资金流历史的最后一行。

    可能和报告顶部的"数据日期"差一天——东财的资金流历史有时比 K 线晚一个交易日。
    正因为会差，才要单独标出来，不能借用 K 线那个日期。
    """
    fund_flow = data.get("_DS_FUND_FLOW")
    if not fund_flow:
        return None
    dates = fund_flow.get("DATE", np.array([], dtype=np.int64))
    if len(dates) == 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(dates[-1] / 1e9).date()
    except Exception:
        return None


def has_today_fund_flow_from_api(data: Dict[str, ndarray], today: Optional[datetime.date] = None) -> bool:
    """Return whether AkShare fund-flow history has a latest row for today."""
    fund_flow = data.get("_DS_FUND_FLOW")
    if not fund_flow:
        return False

    dates = fund_flow.get("DATE", np.array([], dtype=np.int64))
    if len(dates) == 0:
        return False

    if today is None:
        today = datetime.datetime.now().date()

    try:
        latest_date = datetime.datetime.fromtimestamp(dates[-1] / 1e9).date()
    except Exception:
        return False
    return latest_date == today


def print_api_fund_flow_if_today(
    fp: TextIO,
    data: Dict[str, ndarray],
    today: Optional[datetime.date] = None,
) -> bool:
    """Print latest AkShare fund-flow fields if they are dated today."""
    if not has_today_fund_flow_from_api(data, today):
        return False

    has_fund_flow = False
    for field in FUND_FLOW_FIELDS:
        val = build_fund_flow(field, data)
        if val:
            print(f"- {val}", file=fp)
            has_fund_flow = True
    return has_fund_flow


async def print_provider_realtime_fund_flow(
    fp: TextIO, symbol: str, data: Dict[str, ndarray]
) -> bool:
    """没有资金流向页面的标的（科创50 等）盘中走 realtime_fund_flow 那层 provider。

    输出和页面路径同一种形状：一行标的名称、五行"当日X净流入 … X净占比"，下游解析同一套
    正则。接口不给净占比，用净流入除以当日成交额（``AMOUNT`` 最后一根，单位元）；成交额
    没有就写 ``--``，不编数。取不到返回 False，调用方照旧写"暂无实时资金流向"。
    """
    pure_code = "".join(c for c in symbol if c.isdigit())
    if not pure_code:
        return False
    request = FundFlowRequest(code=pure_code, symbol=symbol, is_index=pure_code in ALL_INDICES)
    try:
        flow = await asyncio.to_thread(realtime_fund_flow_source.resolve, request)
    except Exception:
        logger.warning("实时资金流 provider 取数异常 %s", symbol, exc_info=True)
        return False
    if flow is None:
        return False

    amounts = data.get("AMOUNT")
    amount = float(amounts[-1]) if amounts is not None and len(amounts) and amounts[-1] > 0 else None
    prefix = get_realtime_fund_flow_prefix(pure_code, data)
    print(f"- 标的名称: {data.get('NAME') or flow.name or symbol}", file=fp)
    for kind, net in flow.rows():
        ratio = f"{net / amount:.2%}" if amount else "--"
        print(f"- {prefix}{kind}净流入: {format_fund_flow_amount(net)}  {kind}净占比: {ratio}", file=fp)
    return True


def has_realtime_fund_flow_values(res: dict) -> bool:
    """Return whether Playwright realtime scraping yielded any non-placeholder amount."""
    amount_keys = ["主力净流入", "超大单净流入", "大单净流入", "中单净流入", "小单净流入"]
    placeholders = {"", "-", "--", "0", "0.0", "0.00", "0万", "0.00万", "0亿", "0.00亿"}
    for key in amount_keys:
        value = str(res.get(key, "")).strip()
        if value not in placeholders:
            return True
    return False


def format_fund_flow_amount(value) -> str:
    """Format fund-flow amount with Chinese market units."""
    val = _json_number(value)
    if val is None:
        return "--"
    if abs(val) >= 1e8:
        return f"{val / 1e8:.2f}亿"
    return f"{val / 1e4:.2f}万"


def format_fund_flow_percent(value) -> str:
    """Format an internal ratio value as a percentage string."""
    val = _json_number(value)
    if val is None:
        return "--"
    return f"{val:.2%}"


def format_fund_flow_price(value) -> str:
    val = _json_number(value)
    if val is None:
        return "--"
    return f"{val:.2f}"


def build_historical_fund_flow_data(fp: TextIO, data: Dict[str, ndarray], limit: int = 15) -> None:
    """构建历史资金流向表格"""
    limit = int(limit or 0)
    if limit <= 0:
        return

    fund_flow = data.get("_DS_FUND_FLOW")
    if not fund_flow:
        return

    dates = fund_flow.get("DATE", np.array([], dtype=np.int64))
    if len(dates) == 0:
        return

    query_date = data.get("QUERY_DATE")
    indices = list(range(len(dates)))
    if query_date:
        try:
            query_dt = datetime.datetime.strptime(str(query_date)[:10], "%Y-%m-%d")
            query_ns = int(query_dt.timestamp() * 1e9)
            indices = [idx for idx in indices if dates[idx] <= query_ns]
        except ValueError:
            pass

    indices = indices[-limit:][::-1]
    if not indices:
        return

    print("## 历史资金流向", file=fp)
    print("", file=fp)
    print(
        "| 日期 | 收盘价 | 涨跌幅 | 主力净流入 | 主力占比 | 超大单净流入 | 超大单占比 | 大单净流入 | 大单占比 | 中单净流入 | 中单占比 | 小单净流入 | 小单占比 |",
        file=fp,
    )
    print(
        "| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |",
        file=fp,
    )

    for idx in indices:
        def value_for(key: str):
            values = fund_flow.get(key)
            if values is None or len(values) <= idx:
                return np.nan
            return values[idx]

        date_str = datetime.datetime.fromtimestamp(dates[idx] / 1e9).strftime("%Y-%m-%d")
        row = [
            date_str,
            format_fund_flow_price(value_for("CLOSE")),
            format_fund_flow_percent(value_for("PCT_CHG")),
            format_fund_flow_amount(value_for("A_A")),
            format_fund_flow_percent(value_for("A_R")),
            format_fund_flow_amount(value_for("XL_A")),
            format_fund_flow_percent(value_for("XL_R")),
            format_fund_flow_amount(value_for("L_A")),
            format_fund_flow_percent(value_for("L_R")),
            format_fund_flow_amount(value_for("M_A")),
            format_fund_flow_percent(value_for("M_R")),
            format_fund_flow_amount(value_for("S_A")),
            format_fund_flow_percent(value_for("S_R")),
        ]
        print(f"| {' | '.join(row)} |", file=fp)
    print("", file=fp)


CORE_REALTIME_FUND_FLOW_INDICES = {"000001", "399001", "399006"}


def get_realtime_fund_flow_target(symbol: str, data: Dict[str, ndarray]) -> Optional[str]:
    """Return the realtime fund-flow target code for Eastmoney page scraping."""
    pure_code = "".join([c for c in symbol if c.isdigit()])
    if pure_code in ALL_INDICES:
        if pure_code in CORE_REALTIME_FUND_FLOW_INDICES:
            return pure_code
        return None
    if data.get("IS_MARKET", False):
        return "dpzjlx"
    return pure_code


def get_realtime_fund_flow_prefix(target_code: str, data: Dict[str, ndarray]) -> str:
    """Return the display prefix for realtime fund-flow rows.

    和 ``build_fund_flow`` 同一套前缀：大盘口径写"沪深两市"，其余写"当日"。
    两条路径必须一致，否则同一份报告里盘中和盘后的措辞会不一样。
    """
    if data.get("IS_MARKET", False) and target_code == "dpzjlx":
        return "沪深两市"
    return "当日"


def resolve_realtime_fund_flow_target(symbol: str) -> Optional[str]:
    """Resolve the scraping target from the symbol alone, before base data exists."""
    return get_realtime_fund_flow_target(symbol, {})


@dataclass
class RealtimeFundFlowPrefetch:
    """A live fund-flow scrape started in parallel with the base-data fetch."""

    target_code: str
    task: "asyncio.Task[str]"

    def discard(self) -> None:
        """Drop an unused prefetch without leaving an unretrieved task exception."""
        if not self.task.done():
            self.task.cancel()
            return
        if not self.task.cancelled():
            self.task.exception()


def start_realtime_fund_flow_prefetch(
    symbol: str,
    date: Optional[str] = None,
) -> Optional[RealtimeFundFlowPrefetch]:
    """Start the Eastmoney scrape early so it overlaps the base-data fetch.

    Returns None whenever the report would not scrape at all; the decision is
    re-made in build_trading_data, which stays authoritative.
    """
    if date is not None:
        return None
    if not is_realtime_fund_flow_window():
        return None
    target_code = resolve_realtime_fund_flow_target(symbol)
    if target_code is None:
        return None
    return RealtimeFundFlowPrefetch(
        target_code,
        asyncio.create_task(
            get_fund_flow([target_code], keep_alive_on_cancel=False)
        ),
    )


async def build_trading_data(
    fp: TextIO,
    symbol: str,
    data: Dict[str, ndarray],
    include_historical_fund_flow: bool = False,
    historical_fund_flow_limit: int = 15,
    realtime_fund_flow: Optional[RealtimeFundFlowPrefetch] = None,
) -> None:
    """构建交易数据部分"""
    if "CLOSE" not in data or len(data["CLOSE"]) == 0:
        return
    
    today_ratio = today_volume_est_ratio(data)
    is_intra_day = today_ratio > 1.05  # 显著超过1说明是盘中
    
    close = data["CLOSE"]
    # 原始成交量/成交额
    volume_actual = data.get("VOLUME", np.zeros_like(close)).copy()
    amount_actual = (data.get("AMOUNT", np.zeros_like(close)) / 1e8).copy()
    
    # 预估全天成交量/成交额
    volume_est = volume_actual.copy()
    amount_est = amount_actual.copy()
    if len(volume_est) > 0:
        volume_est[-1] = volume_actual[-1] * today_ratio
        amount_est[-1] = amount_actual[-1] * today_ratio
    
    high = data.get("HIGH", close)
    low = data.get("LOW", close)
    open_ = data.get("OPEN", close)

    periods = list(filter(lambda n: n <= len(close), [5, 20, 60, 120, 240]))

    print("# 交易数据", file=fp)
    print("", file=fp)

    print("## 价格", file=fp)
    print(
        f"- 当日: {close[-1]:.3f} 开盘: {open_[-1]:.3f} "
        f"最高: {high[-1]:.3f} 最低: {low[-1]:.3f}",
        file=fp,
    )
    for p in periods:
        print(
            f"- {p}日均价: {close[-p:].mean():.3f} 最高: {high[-p:].max():.3f} 最低: {low[-p:].min():.3f}",
            file=fp,
        )
    print("", file=fp)

    print("## 涨跌幅", file=fp)
    if len(close) >= 2 and close[-2] != 0:
        print(f"- 当日: {(close[-1] / close[-2] - 1):.2%}", file=fp)
    for p in periods:
        if close[-p] != 0:
            print(f"- {p}日累计: {(close[-1] / close[-p] - 1) * 100:.2f}%", file=fp)
    print("", file=fp)

    print("## 振幅", file=fp)
    prev_close = close[-2] if len(close) >= 2 else close[-1]
    if prev_close != 0:
        print(f"- 当日: {(high[-1] - low[-1]) / prev_close:.2%}", file=fp)
        
    for p in periods:
        mean_p = close[-p:].mean()
        if mean_p != 0:
            print(f"- {p}日振幅: {(high[-p:].max() - low[-p:].min()) / mean_p:.2%}", file=fp)
    print("", file=fp)

    print("## 成交量(万手)", file=fp)
    if is_intra_day:
        print(f"- 当日(实时): {volume_actual[-1] / 1e4:.2f}", file=fp)
    else:
        print(f"- 当日: {volume_actual[-1] / 1e4:.2f}", file=fp)
        
    for p in periods:
        # 均量使用预估值来填补当日，否则均值会偏低
        vol_for_mean = volume_actual.copy()
        vol_for_mean[-1] = volume_est[-1]
        print(f"- {p}日均量(万手): {vol_for_mean[-p:].mean() / 1e4:.2f}", file=fp)
    print("", file=fp)

    print("## 成交额(亿)", file=fp)
    if is_intra_day:
        print(f"- 当日(实时): {amount_actual[-1]:.2f}", file=fp)
    else:
        print(f"- 当日: {amount_actual[-1]:.2f}", file=fp)
        
    for p in periods:
        amt_for_mean = amount_actual.copy()
        amt_for_mean[-1] = amount_est[-1]
        print(f"- {p}日均额(亿): {amt_for_mean[-p:].mean():.2f}", file=fp)
    print("", file=fp)

    # 资金流向部分。标题不带日期——这一段的日期就是报告开头那个"数据日期"，
    # 两处写同一个日期是冗余。真出现不一致（东财的资金流历史偶尔比 K 线晚一个
    # 交易日），由 fund_flow_lag() 判出来、走 warnings 提示，而不是靠读者自己
    # 比对两个标题。
    print("## 资金流向", file=fp)

    if data.get("IS_HISTORICAL_QUERY", False):
        print("- 指定日期查询暂不展示实时资金流向", file=fp)
        print("", file=fp)
    else:
    
        # The configurable warmup-to-final window uses Playwright because the API feed lags.
        is_trading = is_realtime_fund_flow_window()
        
        if is_trading:
            # 调用无头浏览器抓取实时数据
            import json
            target_code = get_realtime_fund_flow_target(symbol, data)
            if target_code is None:
                # 没有页面的标的：先看 API 日线有没有当天行（盘中没有），再问分钟线那层
                # provider，都没有才写暂无。顺序不动是为了让有当天行时的输出逐字不变。
                if not print_api_fund_flow_if_today(fp, data):
                    if not await print_provider_realtime_fund_flow(fp, symbol, data):
                        print("- 暂无实时资金流向", file=fp)
            else:
                try:
                    if (
                        realtime_fund_flow is not None
                        and realtime_fund_flow.target_code == target_code
                    ):
                        json_str = await realtime_fund_flow.task
                    else:
                        json_str = await get_fund_flow([target_code])
                    results = json.loads(json_str)
                    res = results.get(target_code, {})
                    
                    if "error" in res:
                         if not print_api_fund_flow_if_today(fp, data):
                             print(f"- [实时抓取失败] {res['error']}", file=fp)
                    elif res and not has_realtime_fund_flow_values(res):
                         # 抓到了页面但十档全是占位符。之前这里会继续把占位符渲染
                         # 成一栏 0，等于声称"今日主力净流入为零"，而实际是没拿到：
                         # 2026-09-04 10:19 就是这样，历史表 120 行、今日全 0。
                         # 接口有今日数据就用接口的，否则如实说取不到。
                         if not print_api_fund_flow_if_today(fp, data):
                             print("- 盘中实时数据暂时不可用", file=fp)
                    elif res:
                         # [增加] 显式输出抓取到的标的名称，方便交叉验证
                         print(f"- 标的名称: {res.get('标的名称', '')}", file=fp)
                         prefix = get_realtime_fund_flow_prefix(target_code, data)
                         # 按顺序对齐：主力, 超大单, 大单, 中单, 小单
                         field_configs = [
                             ("主力", "主力净流入", "主力净比(%)"),
                             ("超大单", "超大单净流入", "超大单净比(%)"),
                             ("大单", "大单净流入", "大单净比(%)"),
                             ("中单", "中单净流入", "中单净比(%)"),
                             ("小单", "小单净流入", "小单净比(%)"),
                         ]
                         for name, amt_key, ratio_key in field_configs:
                             if amt_key in res:
                                 amount_str = res[amt_key]
                                 ratio = res.get(ratio_key, 0.0) / 100.0  # 修正百分比倍数
                                 print(f"- {prefix}{name}净流入: {amount_str}  {name}净占比: {ratio:.2%}", file=fp)
                    else:
                         if not print_api_fund_flow_if_today(fp, data):
                             print("- 盘中实时数据暂时不可用", file=fp)
                except Exception as e:
                    if not print_api_fund_flow_if_today(fp, data):
                        print(f"- [实时调用异常] {str(e)}", file=fp)
        else:
            # 非交易时段展示详情数据
            has_fund_flow = False
            fields = [
                ("主力", "A"), ("超大单", "XL"), ("大单", "L"), ("中单", "M"), ("小单", "S"),
            ]
            for field_name, field_id in fields:
                val = build_fund_flow((field_name, field_id), data)
                if val:
                    print(f"- {val}", file=fp)
                    has_fund_flow = True
            if not has_fund_flow:
                print("- 暂无资金流向数据", file=fp)
        print("", file=fp)

    if include_historical_fund_flow:
        build_historical_fund_flow_data(fp, data, limit=historical_fund_flow_limit)

    # 换手率计算
    fcap = data.get("FCAP", np.array([]))
    if len(fcap) == 0 or fcap[-1] == 0:
        fcap = data.get("TCAP", np.array([]))
        
    if len(fcap) > 0 and fcap[-1] > 0:
        print("## 换手率", file=fp)
        if is_intra_day:
            print(f"- 当日(实时): {volume_actual[-1] * 100 / fcap[-1]:.2%}", file=fp)
        else:
            print(f"- 当日: {volume_actual[-1] * 100 / fcap[-1]:.2%}", file=fp)
            
        for p in periods:
            vol_for_mean = volume_actual.copy()
            vol_for_mean[-1] = volume_est[-1]
            print(f"- {p}日均换手: {vol_for_mean[-p:].mean() * 100 / fcap[-1]:.2%}", file=fp)
            print(f"- {p}日总换手 (含当日): {vol_for_mean[-p:].sum() * 100 / fcap[-1]:.2%}", file=fp)
        print("", file=fp)


def build_technical_data(fp: TextIO, symbol: str, data: Dict[str, ndarray]) -> None:
    """构建技术指标部分"""
    if "CLOSE" not in data:
        return
    
    close = data["CLOSE"]
    high = data.get("HIGH", close)
    low = data.get("LOW", close)

    if len(close) < 30:
        return

    print("# 技术指标(最近30日)", file=fp)
    print("", file=fp)

    indicators = get_technical_indicators(data, days=30, include_derived=False)
    columns = [
        "日期",
        "KDJ.K",
        "KDJ.D",
        "KDJ.J",
        "MACD DIF",
        "MACD DEA",
        "RSI(6)",
        "RSI(12)",
        "RSI(24)",
        "BBands Upper",
        "BBands Middle",
        "BBands Lower",
    ]
    print("| " + " | ".join(columns) + " |", file=fp)
    print("| --- " * len(columns) + "|", file=fp)

    def format_value(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.2f}"

    for item in indicators:
        kdj = item["kdj"]
        macd = item["macd"]
        rsi = item["rsi"]
        bbands = item["bbands"]
        values = [
            format_value(kdj["k"]),
            format_value(kdj["d"]),
            format_value(kdj["j"]),
            format_value(macd["dif"]),
            format_value(macd["dea"]),
            format_value(rsi["rsi6"]),
            format_value(rsi["rsi12"]),
            format_value(rsi["rsi24"]),
            format_value(bbands["upper"]),
            format_value(bbands["middle"]),
            format_value(bbands["lower"]),
        ]
        print(
            "| " + item["date"] + "|" + " | ".join(values) + " |",
            file=fp,
        )
    print("", file=fp)


def build_financial_data(fp: TextIO, symbol: str, data: Dict[str, ndarray]) -> None:
    """构建财务数据部分"""
    # 与 build_basic_data 一致地先纠偏：is_stock 只认 SH6/SZ00/SZ30，调用方传进来
    # 的可能是交易所前缀写错的输入。查 SH300408（实为深市）时数据源已把代码纠正
    # 成 SZ300408，基本数据段用的是纠正后的值，这里不纠偏就会静默丢掉整段财务数据。
    symbol = data.get("SYMBOL", symbol)
    if not is_stock(symbol):
        return
    
    if "_DS_FINANCE" not in data:
        print("# 财务数据", file=fp)
        print("", file=fp)
        print("- 暂无财务数据", file=fp)
        print("", file=fp)
        return
    
    fin, _ = data["_DS_FINANCE"]
    if "DATE" not in fin or len(fin["DATE"]) == 0:
        return
    
    dates = fin["DATE"]
    max_years = 5
    print("# 财务数据", file=fp)
    print("", file=fp)
    years = 0
    fields = [
        # (名称, 字段ID, 除数, 是否显示)
        # akshare 返回的财务数据带"万"单位，解析后为元，需除以1e8转为亿元
        ("主营收入(亿元)", "MR", 1e8, True),
        ("净利润(亿元)", "NP", 1e8, True),
        ("每股收益", "EPS", 1, True),
        ("每股净资产", "NAVPS", 1, True),
        ("净资产收益率(%)", "ROE", 0.01, True),
    ]

    rows = []
    # 从最后一个索引遍历到 0（不包含），与原始代码保持一致
    # 跳过索引 0 是因为最早的财务数据可能不完整
    for i in range(len(dates) - 1, 0, -1):
        date = datetime.datetime.fromtimestamp(dates[i] / 1e9)
        if date.month != 12 or years >= max_years:
            continue
        row = [date.strftime("%Y年度")]
        for _, field, div, show in fields:
            if show and field in fin:
                field_data = fin[field]
                # 检查数组长度，避免索引越界
                if len(field_data) > i:
                    row.append(field_data[i] / div)
                else:
                    row.append(0)
            else:
                row.append(0)
        rows.append(row)
        years += 1

    if not rows:
        print("- 暂无年度财务数据", file=fp)
        print("", file=fp)
        return

    print("| 指标 | " + " ".join([f"{r[0]} |" for r in rows]), file=fp)
    print("| --- " * (len(rows) + 1) + "|", file=fp)
    for i in range(1, len(rows[0])):
        print(
            f"| {fields[i - 1][0]} | " + " ".join([f"{r[i]:.2f} |" for r in rows]),
            file=fp,
        )

    print("", file=fp)
