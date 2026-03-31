"""
CN Stock 数据源实现

综合使用 AkShare 和 efinance 获取 A 股行情数据。
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

import akshare_proxy_patch
import efinance as ef
from ..config import AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD, AKSHARE_PROXY_PORT
from .base import DataSource, StockData


def check_is_index(symbol: str, name: str) -> bool:
    """判定是否为指数的辅助函数"""
    if not symbol:
        return False
    # 1. 名字中包含“指数”
    if name and "指数" in name:
        return True
    # 2. 识别标准指数前缀：沪市SH000, 深市SZ39, 中证SH93
    if symbol.startswith(("SH000", "SZ39", "SH93")):
        return True
    return False

# Initialize the proxy patch to improve reliability of AkShare API calls,
# especially for Eastmoney interfaces (push2his.eastmoney.com etc.)
akshare_proxy_patch.install_patch(AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD, AKSHARE_PROXY_PORT)

print(f"AKSHARE_PROXY_IP: {AKSHARE_PROXY_IP}")
print(f"AKSHARE_PROXY_PASSWORD: {AKSHARE_PROXY_PASSWORD}")
print(f"AKSHARE_PROXY_PORT: {AKSHARE_PROXY_PORT}")

logger = logging.getLogger("qtf_mcp")

# 线程池用于执行同步的调用
_executor = ThreadPoolExecutor(max_workers=4)


def _run_in_executor(func, *args):
    """在线程池中运行同步函数"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, func, *args)


class CNStockDataSource(DataSource):
    """
    CN Stock 数据源
    
    综合使用 AkShare 和 efinance 获取 A 股数据，包括：
    - 日K线数据（支持复权）
    - 财务数据
    - 资金流向
    - 分红数据
    - 实时估值指标 (PE/PB)
    """
    
    @property
    def name(self) -> str:
        return "CNStock"
    
    def _safe_float(self, value, default: float = 0.0) -> float:
        """
        安全将任意值转换为 float。
        对于 ETF、停牌股等特殊品种，efinance 可能返回 '-' 、None 等非数字内容。
        """
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if s in ("", "-", "--", "N/A", "nan"):
            return default
        try:
            return float(s)
        except (ValueError, TypeError):
            return default
    
    def _symbol_to_akshare(self, symbol: str) -> tuple[str, str]:
        """
        将内部格式转换为 akshare 格式
        
        SH600000 -> ("600000", "sh")
        SZ000001 -> ("000001", "sz")
        """
        if symbol.startswith("SH"):
            return symbol[2:], "sh"
        elif symbol.startswith("SZ"):
            return symbol[2:], "sz"
        else:
            # 尝试根据代码推断交易所
            code = symbol
            if code.startswith("6"):
                return code, "sh"
            else:
                return code, "sz"
    
    def _akshare_to_symbol(self, code: str, market: str = "") -> str:
        """
        将 akshare 格式转换为内部格式
        
        ("600000", "sh") -> "SH600000"
        """
        if market:
            prefix = "SH" if market.lower() in ["sh", "1"] else "SZ"
        else:
            prefix = "SH" if code.startswith("6") else "SZ"
        return f"{prefix}{code}"
    
    def _date_to_ns(self, date_val) -> int:
        """将日期转换为纳秒时间戳"""
        if isinstance(date_val, str):
            dt = datetime.strptime(date_val[:10], "%Y-%m-%d")
        elif hasattr(date_val, "timestamp"):
            dt = date_val
        else:
            dt = datetime.strptime(str(date_val)[:10], "%Y-%m-%d")
        return int(dt.timestamp() * 1e9)
    
    def _parse_numeric_column(self, series, is_percent: bool = False) -> np.ndarray:
        """
        解析数值列，处理各种格式
        
        Args:
            series: pandas Series
            is_percent: 是否为百分比格式（如 "24.00%"）
        """
        def parse_value(val):
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return 0.0
            if isinstance(val, (int, float)):
                return float(val)
            
            # 字符串处理
            s = str(val).strip()
            if s == "" or s == "-" or s == "--":
                return 0.0
            
            # 移除百分号
            if s.endswith("%"):
                s = s[:-1]
                try:
                    return float(s) / 100.0
                except ValueError:
                    return 0.0
            
            # 处理亿/万单位
            multiplier = 1.0
            if s.endswith("亿"):
                s = s[:-1]
                multiplier = 1e8
            elif s.endswith("万"):
                s = s[:-1]
                multiplier = 1e4
            
            try:
                return float(s) * multiplier
            except ValueError:
                return 0.0
        
        result = np.array([parse_value(v) for v in series], dtype=np.float64)
        return result
    
    def _fetch_kline_sync(
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq", symbol: str = None
    ) -> Optional[Dict]:
        """同步获取K线数据"""
        try:
            from ..symbols import get_symbol_name
            symbol_name = get_symbol_name(symbol) if symbol else ""
            is_index = check_is_index(symbol, symbol_name)
            # 对指数优先使用名称查询 (解决科创50等代码冲突问题)
            query_code = symbol_name if (is_index and symbol_name) else (symbol if is_index else code)

            # 映射复权类型
            adj_map = {"qfq": 1, "hfq": 2, "none": 0}
            fqt = adj_map.get(adjust, 1)
            
            df = None
            # 特殊处理：如果是新上市的 ETF (以 1 或 5 开头)，efinance 往往识别不了
            # 我们直接用 akshare 获取，避免 efinance 的 8s 超时等待
            if code.startswith(("1", "5")):
                import akshare as ak
                ak_adj_map = {1: "qfq", 2: "hfq", 0: ""}
                ak_adj = ak_adj_map.get(fqt, "qfq")
                df = ak.fund_etf_hist_em(
                    symbol=code,
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust=ak_adj
                )
            
            if df is None or df.empty:
                # 使用 efinance 获取日K线
                df = ef.stock.get_quote_history(
                    query_code,
                    beg=start_date.replace("-", ""),
                    end=end_date.replace("-", ""),
                    fqt=fqt
                )
            
            if df is None or df.empty:
                # 如果 efinance 还是不行 (可能由于代码不属于1/5开头但也是新股)，尝试 fallback
                logger.warning(f"efinance 获取K线数据为空 {code}，尝试使用 akshare fallback...")
                import akshare as ak
                ak_adj_map = {1: "qfq", 2: "hfq", 0: ""}
                ak_adj = ak_adj_map.get(fqt, "qfq")
                
                # 判断是股票还是基金
                if code.startswith(("1", "5")):
                    df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust=ak_adj)
                elif is_index:
                    df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
                else:
                    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust=ak_adj)
                
                if df is None or df.empty:
                    logger.warning(f"获取K线数据依然为空 {code}")
                    return None
            
            # 同时获取不复权数据用于计算
            if fqt != 0:
                df_unadj = None
                if code.startswith(("1", "5")):
                    import akshare as ak
                    df_unadj = ak.fund_etf_hist_em(
                        symbol=code,
                        period="daily",
                        start_date=start_date.replace("-", ""),
                        end_date=end_date.replace("-", ""),
                        adjust=""
                    )
                
                if df_unadj is None or df_unadj.empty:
                    df_unadj = ef.stock.get_quote_history(
                        query_code,
                        beg=start_date.replace("-", ""),
                        end=end_date.replace("-", ""),
                        fqt=0  # 不复权
                    )
                
                if df_unadj is None or df_unadj.empty:
                    import akshare as ak
                    if code.startswith(("1", "5")):
                        df_unadj = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="")
                    elif is_index:
                        df_unadj = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
                    else:
                        df_unadj = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="")
            else:
                df_unadj = df
            
            return {
                "adjusted": df,
                "unadj": df_unadj if df_unadj is not None else df,
                "adjust_type": adjust,
            }
        except Exception as e:
            logger.warning(f"获取K线数据失败 {code}: {e}")
            return None
    
    def fetch_kline_simple_sync(
        self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> Optional[Dict]:
        """
        简单获取 K 线数据（同步方法，返回简化的字典格式）
        """
        code, market = self._symbol_to_akshare(symbol)
        kline_data = self._fetch_kline_sync(code, start_date, end_date, adjust, symbol)
        
        if kline_data is None:
            return None
        
        df = kline_data["adjusted"]
        adjust_type = kline_data["adjust_type"]
        
        # 转换为简单的字典列表格式
        result = []
        for _, row in df.iterrows():
            result.append({
                "日期": str(row["日期"]),
                "开盘": float(row["开盘"]),
                "收盘": float(row["收盘"]),
                "最高": float(row["最高"]),
                "最低": float(row["最低"]),
                "成交量": int(row["成交量"]),
                "成交额": float(row["成交额"]),
                "振幅": float(row["振幅"]) if "振幅" in row else 0,
                "涨跌幅": float(row["涨跌幅"]) if "涨跌幅" in row else 0,
                "涨跌额": float(row["涨跌额"]) if "涨跌额" in row else 0,
                "换手率": float(row["换手率"]) if "换手率" in row else 0,
            })
        
        return {
            "symbol": symbol,
            "adjust": {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}.get(adjust_type, adjust_type),
            "data": result,
        }
    
    async def fetch_kline_simple(
        self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> Optional[Dict]:
        """异步获取 K 线数据"""
        return await _run_in_executor(
            self.fetch_kline_simple_sync, symbol, start_date, end_date, adjust
        )
    
    def _fetch_finance_sync(self, code: str, symbol: str = None) -> Optional[Dict]:
        """同步获取财务数据"""
        from ..symbols import get_symbol_name
        symbol_name = get_symbol_name(symbol) if symbol else ""
        if code.startswith(("1", "5")) or check_is_index(symbol, symbol_name):
            return None
        try:
            import akshare as ak
            df = ak.stock_financial_abstract_ths(symbol=code)
            if df is None or df.empty:
                return None
            return {"finance": df}
        except Exception as e:
            logger.warning(f"获取财务数据失败 {code}: {e}")
            return None
    
    def _fetch_fund_flow_sync(self, code: str, symbol: str = None) -> Optional[Dict]:
        """同步获取资金流向数据"""
        from ..symbols import get_symbol_name
        symbol_name = get_symbol_name(symbol) if symbol else ""
        is_index = check_is_index(symbol, symbol_name)
        
        if code.startswith(("1", "5")):
            return None
        try:
            import akshare as ak
            df = None
            is_market = False
            if is_index:
                # 仅针对三大核心指数返回“沪深两市”大盘资金流向
                if symbol in ["SH000001", "SZ399001", "SZ399006"]:
                    df = ak.stock_market_fund_flow()
                    is_market = True
                # 其他指数（如科创50）保持 df = None
            else:
                # 个股
                exchange = "sh" if code.startswith("6") else "sz"
                df = ak.stock_individual_fund_flow(stock=code, market=exchange)
            
            if df is None or df.empty:
                return None
            return {"fund_flow": df, "is_market": is_market}
        except Exception as e:
            logger.warning(f"获取资金流向数据失败 {code}: {e}")
            return None
    
    def _fetch_dividend_sync(self, code: str) -> Optional[Dict]:
        """同步获取分红数据"""
        # Note: dividend sync isn't passed symbol, but wait, does fetch_stock_data pass symbol?
        # Let me see. We need to optionally pass symbol to dividend_sync.
        if code.startswith(("1", "5")):
            return None
        try:
            import akshare as ak
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
            df = ak.stock_fhps_detail_em(symbol=symbol)
            if df is None or df.empty:
                return None
            return {"dividend": df}
        except Exception as e:
            logger.warning(f"获取分红数据失败 {code}: {e}")
            return None
    
    def _fetch_realtime_sync(self, code: str, symbol: str = None) -> Optional[Dict]:
        """同步获取实时数据"""
        try:
            # 对于 ETF，避免调用 ak.fund_etf_category_sina，因为它会拉取全量 1000+ 条数据导致超时
            if code.startswith(("1", "5")):
                snapshot = ef.stock.get_quote_snapshot(code)
                if snapshot is not None and not snapshot.empty:
                    info = {
                        "股票简称": str(snapshot.get("名称", "")),
                        "最新价": float(snapshot.get("最新价", 0)),
                        "总股本": 0.0,
                        "总市值": 0.0,
                        "流通市值": 0.0,
                        "动态市盈率": 0.0,
                    }
                    return {"info": info}
                return None
            from ..symbols import get_symbol_name
            symbol_name = get_symbol_name(symbol) if symbol else ""
            is_index = check_is_index(symbol, symbol_name)
            # 对指数优先使用名称查询
            query_code = symbol_name if (is_index and symbol_name) else (symbol if is_index else code)
                
            info_series = ef.stock.get_base_info(query_code)
            if info_series is None or info_series.empty:
                # 即使 base_info 失败，尝试用 snapshot 保底
                snapshot = ef.stock.get_quote_snapshot(query_code)
                if snapshot is not None and not snapshot.empty:
                    info = {
                        "股票简称": str(snapshot.get("名称", "")),
                        "最新价": float(snapshot.get("最新价", 0)),
                        "总股本": 0.0,
                        "总市值": 0.0,
                        "流通市值": 0.0,
                        "动态市盈率": 0.0,
                    }
                    return {"info": info}
                return None
            
            info = {
                "股票简称": info_series.get("股票名称", ""),
                "动态市盈率": self._safe_float(info_series.get("市盈率(动)", 0)),
            }
            
            snapshot = ef.stock.get_quote_snapshot(query_code)
            if snapshot is not None and not snapshot.empty:
                latest_price = self._safe_float(snapshot.get("最新价", 0))
                total_market_val = self._safe_float(info_series.get("总市值", 0))
                if latest_price > 0:
                    info["总股本"] = total_market_val / latest_price
                info["最新价"] = latest_price
                info["总市值"] = total_market_val
                info["流通市值"] = self._safe_float(info_series.get("流通市值", 0))
            
            return {"info": info}
        except Exception as e:
            logger.warning(f"获取实时数据失败 {code}: {e}")
            return None
    
    def _fetch_sector_sync(self, code: str) -> List[str]:
        """同步获取所属板块"""
        try:
            df = ef.stock.get_belong_board(code)
            if df is not None and not df.empty:
                return df["板块名称"].tolist()
            return []
        except Exception as e:
            logger.warning(f"获取板块数据失败 {code}: {e}")
            return []
    
    async def fetch_stock_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> StockData:
        """获取股票完整数据"""
        code, market = self._symbol_to_akshare(symbol)
        
        kline_future = _run_in_executor(self._fetch_kline_sync, code, start_date, end_date, "qfq", symbol)
        finance_future = _run_in_executor(self._fetch_finance_sync, code, symbol)
        fund_flow_future = _run_in_executor(self._fetch_fund_flow_sync, code, symbol)
        realtime_future = _run_in_executor(self._fetch_realtime_sync, code, symbol)
        
        kline_data, finance_data, fund_flow_data, realtime_data = await asyncio.gather(
            kline_future, finance_future, fund_flow_future, realtime_future
        )
        
        stock_data = StockData(symbol=symbol)
        
        if realtime_data and "info" in realtime_data:
            info = realtime_data["info"]
            stock_data.name = info.get("股票简称", "")
            
            total_shares = info.get("总股本", 0)
            stock_data.total_shares = np.array([self._safe_float(total_shares)])
            
            total_market_cap = info.get("总市值", 0)
            stock_data.total_market_cap = np.array([self._safe_float(total_market_cap)])
            
            float_market_cap = info.get("流通市值", 0)
            stock_data.float_market_cap = np.array([self._safe_float(float_market_cap)])
            
            latest_price = self._safe_float(info.get("最新价", 0))
            if latest_price > 0:
                float_shares = self._safe_float(float_market_cap) / latest_price
                stock_data.float_shares = np.array([float(float_shares)])
            else:
                stock_data.float_shares = np.array([0.0])
            
            pe_ttm = info.get("动态市盈率", 0)
            stock_data.pe_ttm = np.array([self._safe_float(pe_ttm)])
        
        if kline_data:
            df_qfq = kline_data.get("adjusted")
            df_unadj = kline_data.get("unadj")
            
            if df_qfq is not None and not df_qfq.empty:
                stock_data.date = np.array([self._date_to_ns(d) for d in df_qfq["日期"]], dtype=np.int64)
                stock_data.open = df_qfq["开盘"].values.astype(np.float64)
                stock_data.high = df_qfq["最高"].values.astype(np.float64)
                stock_data.low = df_qfq["最低"].values.astype(np.float64)
                stock_data.close = df_qfq["收盘"].values.astype(np.float64)
                stock_data.volume = df_qfq["成交量"].values.astype(np.float64)
                stock_data.amount = df_qfq["成交额"].values.astype(np.float64)
                
                if df_unadj is not None and not df_unadj.empty:
                    stock_data.close_unadj = df_unadj["收盘"].values.astype(np.float64)
                else:
                    stock_data.close_unadj = stock_data.close.copy()
                
                n = len(stock_data.date)
                stock_data.given_cash = np.zeros(n, dtype=np.float64)
                stock_data.given_share = np.zeros(n, dtype=np.float64)
        
        if finance_data and "finance" in finance_data:
            df = finance_data["finance"]
            if not df.empty:
                try:
                    if "报告期" in df.columns:
                        stock_data.finance_date = np.array(
                            [self._date_to_ns(d) for d in df["报告期"]], 
                            dtype=np.int64
                        )
                    if "基本每股收益" in df.columns:
                        stock_data.eps = self._parse_numeric_column(df["基本每股收益"])
                    if "每股净资产" in df.columns:
                        stock_data.nav_per_share = self._parse_numeric_column(df["每股净资产"])
                    if "净资产收益率" in df.columns:
                        stock_data.roe = self._parse_numeric_column(df["净资产收益率"], is_percent=True)
                    if "营业总收入" in df.columns:
                        stock_data.main_revenue = self._parse_numeric_column(df["营业总收入"])
                    if "净利润" in df.columns:
                        stock_data.net_profit = self._parse_numeric_column(df["净利润"])
                except Exception as e:
                    logger.warning(f"处理财务数据失败: {e}")
        
        if fund_flow_data and "fund_flow" in fund_flow_data:
            df = fund_flow_data["fund_flow"]
            stock_data.is_market = fund_flow_data.get("is_market", False)
            if not df.empty:
                try:
                    latest = df.iloc[-1] if len(df) > 0 else None
                    if latest is not None:
                        # 字段映射表：(DataFrame字段名, StockData属性名, 是否为占比)
                        fields = [
                            ("主力净流入-净额", "fund_main_amount", False),
                            ("主力净流入-净占比", "fund_main_ratio", True),
                            ("超大单净流入-净额", "fund_xl_amount", False),
                            ("超大单净流入-净占比", "fund_xl_ratio", True),
                            ("大单净流入-净额", "fund_l_amount", False),
                            ("大单净流入-净占比", "fund_l_ratio", True),
                            ("中单净流入-净额", "fund_m_amount", False),
                            ("中单净流入-净占比", "fund_m_ratio", True),
                            ("小单净流入-净额", "fund_s_amount", False),
                            ("小单净流入-净占比", "fund_s_ratio", True),
                        ]
                        for df_col, attr, is_ratio in fields:
                            if df_col in df.columns:
                                val = latest.get(df_col, 0)
                                if is_ratio:
                                    val = val / 100.0  # 转换为 0.0-1.0
                                setattr(stock_data, attr, np.array([val], dtype=np.float64))
                except Exception as e:
                    logger.warning(f"处理资金流向数据失败: {e}")
        
        return stock_data
    
    async def fetch_stock_list(self) -> List[Dict[str, str]]:
        """获取股票列表"""
        def _fetch():
            df = ef.stock.get_realtime_quotes()
            result = []
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row["股票代码"])
                    name = str(row["股票名称"])
                    prefix = "SH" if code.startswith(("6", "5")) else "SZ"
                    result.append({"code": f"{prefix}{code}", "name": name})
            return result
        
        return await _run_in_executor(_fetch)
