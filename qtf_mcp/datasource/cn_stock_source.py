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

# Initialize the proxy patch to improve reliability of AkShare API calls,
# especially for Eastmoney interfaces (push2his.eastmoney.com etc.)
akshare_proxy_patch.install_patch(AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD, AKSHARE_PROXY_PORT)

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
        self, code: str, start_date: str, end_date: str, adjust: str = "qfq"
    ) -> Optional[Dict]:
        """同步获取K线数据"""
        try:
            # 映射复权类型
            adj_map = {"qfq": 1, "hfq": 2, "none": 0}
            fqt = adj_map.get(adjust, 1)
            
            # 使用 efinance 获取日K线
            df = ef.stock.get_quote_history(
                code,
                beg=start_date.replace("-", ""),
                end=end_date.replace("-", ""),
                fqt=fqt
            )
            
            if df is None or df.empty:
                logger.warning(f"获取K线数据为空 {code}")
                return None
            
            # 同时获取不复权数据用于计算
            if fqt != 0:
                df_unadj = ef.stock.get_quote_history(
                    code,
                    beg=start_date.replace("-", ""),
                    end=end_date.replace("-", ""),
                    fqt=0  # 不复权
                )
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
        kline_data = self._fetch_kline_sync(code, start_date, end_date, adjust)
        
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
    
    def _fetch_finance_sync(self, code: str) -> Optional[Dict]:
        """同步获取财务数据"""
        try:
            import akshare as ak
            df = ak.stock_financial_abstract_ths(symbol=code)
            if df is None or df.empty:
                return None
            return {"finance": df}
        except Exception as e:
            logger.warning(f"获取财务数据失败 {code}: {e}")
            return None
    
    def _fetch_fund_flow_sync(self, code: str) -> Optional[Dict]:
        """同步获取资金流向数据"""
        try:
            import akshare as ak
            df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
            if df is None or df.empty:
                return None
            return {"fund_flow": df}
        except Exception as e:
            logger.warning(f"获取资金流向数据失败 {code}: {e}")
            return None
    
    def _fetch_dividend_sync(self, code: str) -> Optional[Dict]:
        """同步获取分红数据"""
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
    
    def _fetch_realtime_sync(self, code: str) -> Optional[Dict]:
        """同步获取实时数据"""
        try:
            info_series = ef.stock.get_base_info(code)
            if info_series is None or info_series.empty:
                return None
            
            info = {
                "股票简称": info_series.get("股票名称", ""),
            }
            
            snapshot = ef.stock.get_quote_snapshot(code)
            if snapshot is not None and not snapshot.empty:
                latest_price = snapshot.get("最新价", 0)
                total_market_val = info_series.get("总市值", 0)
                if latest_price > 0:
                    info["总股本"] = total_market_val / latest_price
                info["最新价"] = latest_price
                info["总市值"] = total_market_val
                info["流通市值"] = info_series.get("流通市值", 0)
            
            rt_quotes = ef.stock.get_realtime_quotes()
            if rt_quotes is not None and not rt_quotes.empty:
                match = rt_quotes[rt_quotes['股票代码'] == code]
                if not match.empty:
                    info["动态市盈率"] = match.iloc[0].get("动态市盈率", 0)
            
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
        
        kline_future = _run_in_executor(self._fetch_kline_sync, code, start_date, end_date)
        finance_future = _run_in_executor(self._fetch_finance_sync, code)
        fund_flow_future = _run_in_executor(self._fetch_fund_flow_sync, code)
        realtime_future = _run_in_executor(self._fetch_realtime_sync, code)
        
        kline_data, finance_data, fund_flow_data, realtime_data = await asyncio.gather(
            kline_future, finance_future, fund_flow_future, realtime_future
        )
        
        stock_data = StockData(symbol=symbol)
        
        if realtime_data and "info" in realtime_data:
            info = realtime_data["info"]
            stock_data.name = info.get("股票简称", "")
            
            total_shares = info.get("总股本", 0)
            stock_data.total_shares = np.array([float(total_shares)])
            
            total_market_cap = info.get("总市值", 0)
            stock_data.total_market_cap = np.array([float(total_market_cap)])
            
            float_market_cap = info.get("流通市值", 0)
            stock_data.float_market_cap = np.array([float(float_market_cap)])
            
            latest_price = info.get("最新价", 0)
            if latest_price > 0:
                float_shares = float_market_cap / latest_price
                stock_data.float_shares = np.array([float(float_shares)])
            else:
                stock_data.float_shares = np.array([0.0])
            
            pe_ttm = info.get("动态市盈率", 0)
            stock_data.pe_ttm = np.array([float(pe_ttm)])
        
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
            if not df.empty:
                try:
                    latest = df.iloc[-1] if len(df) > 0 else None
                    if latest is not None:
                        if "主力净流入-净额" in df.columns:
                            stock_data.fund_main_amount = np.array([latest.get("主力净流入-净额", 0)], dtype=np.float64)
                        if "主力净流入-净占比" in df.columns:
                            stock_data.fund_main_ratio = np.array([latest.get("主力净流入-净占比", 0) / 100], dtype=np.float64)
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
