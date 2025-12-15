"""
研究分析模块

根据股票数据生成各类分析报告。
"""

import datetime
from io import StringIO
from typing import Dict, TextIO

import numpy as np
import talib
from numpy import ndarray

from .datafeed import load_data_msd
from .symbols import symbol_with_name


def compute_kdj(close: ndarray, high: ndarray, low: ndarray, n: int = 9, m1: int = 3, m2: int = 3) -> tuple:
    """
    计算 KDJ 指标
    
    使用 TA-Lib 的 STOCH 函数计算
    """
    slowk, slowd = talib.STOCH(
        high, low, close,
        fastk_period=n,
        slowk_period=m1,
        slowk_matype=0,
        slowd_period=m2,
        slowd_matype=0
    )
    # J = 3*K - 2*D
    j = 3 * slowk - 2 * slowd
    return slowk, slowd, j


def compute_macd(close: ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    计算 MACD 指标
    
    使用 TA-Lib 的 MACD 函数计算
    """
    macd, signal_line, hist = talib.MACD(close, fastperiod=fast, slowperiod=slow, signalperiod=signal)
    # 返回 DIF 和 DEA
    return macd, signal_line


async def load_raw_data(
    symbol: str, end_date=None, who: str = ""
) -> Dict[str, ndarray]:
    """加载股票原始数据"""
    if end_date is None:
        end_date = datetime.datetime.now() + datetime.timedelta(days=1)
    if type(end_date) == str:
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d")

    start_date = end_date - datetime.timedelta(days=365 * 2)

    return await load_data_msd(
        symbol, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), 0, who
    )


def is_stock(symbol: str) -> bool:
    """判断是否为个股（而非指数）"""
    if symbol.startswith("SH6") or symbol.startswith("SZ00") or symbol.startswith("SZ30"):
        return True
    return False


def build_stock_data(symbol: str, raw_data: Dict[str, ndarray]) -> str:
    """构建完整的股票数据报告"""
    md = StringIO()
    build_basic_data(md, symbol, raw_data)
    build_trading_data(md, symbol, raw_data)
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
            net_profit = np_arr[last_year_index] * 10000
        else:
            net_profit = 0
        
        # 计算市盈率
        if total_shares > 0 and current_price > 0:
            total_amount = total_shares * current_price
            pe_static = total_amount / net_profit if net_profit != 0 else float("inf")
            print(f"- 市盈率(静): {pe_static:.2f}", file=fp)
        
        # 市净率
        navps = data.get("NAVPS", np.array([]))
        if len(navps) > 0 and navps[-1] != 0 and current_price > 0:
            pb = current_price / navps[-1]
            print(f"- 市净率: {pb:.2f}", file=fp)
        
        # 净资产收益率
        roe = data.get("ROE", np.array([]))
        if len(roe) > 0:
            print(f"- 净资产收益率: {roe[-1]:.2f}", file=fp)
    
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
    amount = value_amount[-1] / 1e8  # 转换为亿
    ratio = abs(value_ratio[-1])
    in_out = "流入" if amount > 0 else "流出"
    amount = abs(amount)
    return f"- {kind} {in_out}: {amount:.2f}亿, 占比: {ratio:.2%}"


def build_trading_data(fp: TextIO, symbol: str, data: Dict[str, ndarray]) -> None:
    """构建交易数据部分"""
    if "CLOSE" not in data or len(data["CLOSE"]) == 0:
        return
    
    today_vol_est_ratio = today_volume_est_ratio(data)
    close = data["CLOSE"]
    volume = data.get("VOLUME", np.zeros_like(close))
    volume = volume.copy()
    if len(volume) > 0:
        volume[-1] = volume[-1] * today_vol_est_ratio
    
    amount = data.get("AMOUNT", np.zeros_like(close)) / 1e8
    amount = amount.copy()
    if len(amount) > 0:
        amount[-1] = amount[-1] * today_vol_est_ratio
    
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

    print("## 振幅", file=fp)
    if low[-1] != 0:
        print(f"- 当日: {(high[-1] / low[-1] - 1):.2%}", file=fp)
    for p in periods:
        min_low = low[-p:].min()
        if min_low != 0:
            print(f"- {p}日振幅: {(high[-p:].max() / min_low - 1):.2%}", file=fp)
    print("", file=fp)

    print("## 涨跌幅", file=fp)
    if len(close) >= 2 and close[-2] != 0:
        print(f"- 当日: {(close[-1] / close[-2] - 1):.2%}", file=fp)
    for p in periods:
        if close[-p] != 0:
            print(f"- {p}日累计: {(close[-1] / close[-p] - 1) * 100:.2f}%", file=fp)
    print("", file=fp)

    print("## 成交量(万手)", file=fp)
    print(f"- 当日: {volume[-1] / 1e6:.2f}", file=fp)
    for p in periods:
        print(f"- {p}日均量(万手): {volume[-p:].mean() / 1e6:.2f}", file=fp)
    print("", file=fp)

    print("## 成交额(亿)", file=fp)
    print(f"- 当日: {amount[-1]:.2f}", file=fp)
    for p in periods:
        print(f"- {p}日均额(亿): {amount[-p:].mean():.2f}", file=fp)
    print("", file=fp)

    print("## 资金流向", file=fp)
    has_fund_flow = False
    for field in FUND_FLOW_FIELDS:
        value = build_fund_flow(field, data)
        if value:
            print(value, file=fp)
            has_fund_flow = True
    if not has_fund_flow:
        print("- 暂无资金流向数据", file=fp)
    print("", file=fp)

    if is_stock(symbol):
        tcap = data.get("TCAP", np.array([]))
        if len(tcap) > 0 and tcap[-1] > 0:
            print("## 换手率", file=fp)
            print(f"- 当日: {volume[-1] / tcap[-1]:.2%}", file=fp)
            for p in periods:
                print(f"- {p}日均换手: {volume[-p:].mean() / tcap[-1]:.2%}", file=fp)
                print(f"- {p}日总换手: {volume[-p:].sum() / tcap[-1]:.2%}", file=fp)
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

    # 使用自定义的 KDJ 和 MACD 函数
    kdj_k, kdj_d, kdj_j = compute_kdj(close, high, low, 9, 3, 3)
    macd_diff, macd_dea = compute_macd(close, 12, 26, 9)

    rsi_6 = talib.RSI(close, timeperiod=6)
    rsi_12 = talib.RSI(close, timeperiod=12)
    rsi_24 = talib.RSI(close, timeperiod=24)

    bb_upper, bb_middle, bb_lower = talib.BBANDS(close, matype=talib.MA_Type.T3)

    date = [
        datetime.datetime.fromtimestamp(d / 1e9).strftime("%Y-%m-%d") for d in data["DATE"]
    ]
    columns = [
        ("日期", date),
        ("KDJ.K", kdj_k),
        ("KDJ.D", kdj_d),
        ("KDJ.J", kdj_j),
        ("MACD DIF", macd_diff),
        ("MACD DEA", macd_dea),
        ("RSI(6)", rsi_6),
        ("RSI(12)", rsi_12),
        ("RSI(24)", rsi_24),
        ("BBands Upper", bb_upper),
        ("BBands Middle", bb_middle),
        ("BBands Lower", bb_lower),
    ]
    print("| " + " | ".join([c[0] for c in columns]) + " |", file=fp)
    print("| --- " * len(columns) + "|", file=fp)
    for i in range(-1, max(-len(date), -31), -1):
        values = []
        for c in columns[1:]:
            val = c[1][i]
            if np.isnan(val):
                values.append("N/A")
            else:
                values.append(f"{val:.2f}")
        print(
            "| " + date[i] + "|" + " | ".join(values) + " |",
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
        ("净资产收益率(%)", "ROE", 1, True),
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
