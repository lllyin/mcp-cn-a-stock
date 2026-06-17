"""
研究分析模块

根据股票数据生成各类分析报告。
"""

import datetime
from io import StringIO
from typing import Dict, Optional, TextIO

import numpy as np
import talib
from numpy import ndarray

from .datafeed import load_data_msd
from .config import ALL_INDICES
from .datasource.realtime_ff import get_fund_flow
from .symbols import symbol_with_name


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
    symbol: str, end_date=None, who: str = ""
) -> Dict[str, ndarray]:
    """加载股票原始数据"""
    is_historical_query = end_date is not None
    if end_date is None:
        end_date = datetime.datetime.now() + datetime.timedelta(days=1)
    if type(end_date) == str:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d")

    start_date = end_date - datetime.timedelta(days=365 * 2)

    data = await load_data_msd(
        symbol, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), 0, who
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
        
        # 市净率
        navps = data.get("NAVPS", np.array([]))
        if len(navps) > 0 and navps[-1] != 0 and current_price > 0:
            pb = current_price / navps[-1]
            # 优先检查数据源是否直接提供了 PB (如果有的话)
            pb_arr = data.get("PB", np.array([]))
            if len(pb_arr) > 0 and pb_arr[-1] > 0:
                 pb = pb_arr[-1]
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
    
    # 针对大盘数据增加前缀标识（沪深两市），解决歧义
    prefix = "沪深两市" if data.get("IS_MARKET", False) else "今日"
    return f"{prefix}{kind}净流入: {amount_str}  {kind}净占比: {ratio:.2%}"


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
    """Return the display prefix for realtime fund-flow rows."""
    if data.get("IS_MARKET", False) and target_code == "dpzjlx":
        return "沪深两市"
    return "今日"


async def build_trading_data(
    fp: TextIO,
    symbol: str,
    data: Dict[str, ndarray],
    include_historical_fund_flow: bool = False,
    historical_fund_flow_limit: int = 15,
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

    periods = list(filter(lambda n: n <= len(close), [5, 20, 60, 120, 240]))

    print("# 交易数据", file=fp)
    print("", file=fp)

    print("## 价格", file=fp)
    print(f"- 当日: {close[-1]:.3f} 最高: {high[-1]:.3f} 最低: {low[-1]:.3f}", file=fp)
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

    # 资金流向部分
    print("## 资金流向", file=fp)

    if data.get("IS_HISTORICAL_QUERY", False):
        print("- 指定日期查询暂不展示实时资金流向", file=fp)
        print("", file=fp)
    else:
    
        from datetime import datetime
        now_time = datetime.now().time()
        # 强制判定交易时间: 09:15 - 17:00 (延长至17:00以支持收盘后的即时汇总抓取)
        is_trading = (now_time.hour == 9 and now_time.minute >= 15) or (10 <= now_time.hour <= 16)
        
        if is_trading:
            # 调用无头浏览器抓取实时数据
            import json
            target_code = get_realtime_fund_flow_target(symbol, data)
            if target_code is None:
                print("- 暂无实时资金流向", file=fp)
            else:
                try:
                    json_str = await get_fund_flow([target_code])
                    results = json.loads(json_str)
                    res = results.get(target_code, {})
                    
                    if "error" in res:
                         print(f"- [实时抓取失败] {res['error']}", file=fp)
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
                         print("- 盘中实时数据暂时不可用", file=fp)
                except Exception as e:
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
            print(f"- {p}日总换手 (含今日): {vol_for_mean[-p:].sum() * 100 / fcap[-1]:.2%}", file=fp)
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
