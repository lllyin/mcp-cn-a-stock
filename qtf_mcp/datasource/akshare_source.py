"""
AkShare 数据源实现

使用 AkShare 获取 A 股行情数据。
文档: https://akshare.akfamily.xyz/data/index.html
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from .base import DataSource, StockData

logger = logging.getLogger("qtf_mcp")

# 线程池用于执行同步的 akshare 调用
_executor = ThreadPoolExecutor(max_workers=4)


def _run_in_executor(func, *args):
    """在线程池中运行同步函数"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, func, *args)


class AkShareDataSource(DataSource):
    """
    AkShare 数据源
    
    使用 AkShare 库获取 A 股数据，包括：
    - 日K线数据（前复权）
    - 财务数据
    - 资金流向
    - 分红数据
    """
    
    @property
    def name(self) -> str:
        return "AkShare"
    
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
                # 如果是百分比格式，已经移除了%，不需要额外除以100
                # 因为 akshare 返回的 "24.00%" 意思就是 24%
            
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
    
    def _fetch_kline_sync(self, code: str, start_date: str, end_date: str) -> Optional[Dict]:
        """同步获取K线数据"""
        try:
            import akshare as ak
            
            # 使用东财接口获取前复权日K线
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="qfq"  # 前复权
            )
            
            if df is None or df.empty:
                return None
            
            # 同时获取不复权数据用于计算
            df_unadj = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust=""  # 不复权
            )
            
            return {
                "qfq": df,
                "unadj": df_unadj if df_unadj is not None else df,
            }
        except Exception as e:
            logger.warning(f"获取K线数据失败 {code}: {e}")
            return None
    
    def _fetch_finance_sync(self, code: str) -> Optional[Dict]:
        """同步获取财务数据"""
        try:
            import akshare as ak
            
            # 获取主要财务指标
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
            
            # 获取个股资金流向
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
            
            # 获取分红配送数据
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
            df = ak.stock_fhps_detail_em(symbol=symbol)
            
            if df is None or df.empty:
                return None
            
            return {"dividend": df}
        except Exception as e:
            logger.warning(f"获取分红数据失败 {code}: {e}")
            return None
    
    def _fetch_realtime_sync(self, code: str) -> Optional[Dict]:
        """同步获取实时数据（用于获取股票名称和总股本）"""
        try:
            import akshare as ak
            
            # 获取实时行情
            df = ak.stock_individual_info_em(symbol=code)
            
            if df is None or df.empty:
                return None
            
            # 转换为字典
            info = {}
            for _, row in df.iterrows():
                info[row["item"]] = row["value"]
            
            return {"info": info}
        except Exception as e:
            logger.warning(f"获取实时数据失败 {code}: {e}")
            return None
    
    def _fetch_sector_sync(self, code: str) -> List[str]:
        """同步获取所属板块"""
        try:
            import akshare as ak
            
            # 获取股票所属行业
            df = ak.stock_board_industry_name_em()
            
            # 这里简化处理，返回空列表
            # 实际使用时可以通过其他接口获取个股所属板块
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
        
        # 并行获取各类数据
        kline_future = _run_in_executor(self._fetch_kline_sync, code, start_date, end_date)
        finance_future = _run_in_executor(self._fetch_finance_sync, code)
        fund_flow_future = _run_in_executor(self._fetch_fund_flow_sync, code)
        realtime_future = _run_in_executor(self._fetch_realtime_sync, code)
        
        kline_data, finance_data, fund_flow_data, realtime_data = await asyncio.gather(
            kline_future, finance_future, fund_flow_future, realtime_future
        )
        
        # 构建 StockData 对象
        stock_data = StockData(symbol=symbol)
        
        # 处理实时数据（获取股票名称）
        if realtime_data and "info" in realtime_data:
            info = realtime_data["info"]
            stock_data.name = info.get("股票简称", "")
            
            # 获取总股本
            total_shares_str = info.get("总股本", "0")
            try:
                if isinstance(total_shares_str, str):
                    # 处理类似 "1.23亿" 的格式
                    if "亿" in total_shares_str:
                        stock_data.total_shares = np.array([float(total_shares_str.replace("亿", "")) * 1e8])
                    elif "万" in total_shares_str:
                        stock_data.total_shares = np.array([float(total_shares_str.replace("万", "")) * 1e4])
                    else:
                        stock_data.total_shares = np.array([float(total_shares_str)])
                else:
                    stock_data.total_shares = np.array([float(total_shares_str)])
            except (ValueError, TypeError):
                stock_data.total_shares = np.array([0.0])
        
        # 处理K线数据
        if kline_data:
            df_qfq = kline_data.get("qfq")
            df_unadj = kline_data.get("unadj")
            
            if df_qfq is not None and not df_qfq.empty:
                # 日期转换为纳秒时间戳
                stock_data.date = np.array([self._date_to_ns(d) for d in df_qfq["日期"]], dtype=np.int64)
                stock_data.open = df_qfq["开盘"].values.astype(np.float64)
                stock_data.high = df_qfq["最高"].values.astype(np.float64)
                stock_data.low = df_qfq["最低"].values.astype(np.float64)
                stock_data.close = df_qfq["收盘"].values.astype(np.float64)
                stock_data.volume = df_qfq["成交量"].values.astype(np.float64)
                stock_data.amount = df_qfq["成交额"].values.astype(np.float64)
                
                # 不复权收盘价
                if df_unadj is not None and not df_unadj.empty:
                    stock_data.close_unadj = df_unadj["收盘"].values.astype(np.float64)
                else:
                    stock_data.close_unadj = stock_data.close.copy()
                
                # 初始化分红相关数组
                n = len(stock_data.date)
                stock_data.given_cash = np.zeros(n, dtype=np.float64)
                stock_data.given_share = np.zeros(n, dtype=np.float64)
        
        # 处理财务数据
        if finance_data and "finance" in finance_data:
            df = finance_data["finance"]
            if not df.empty:
                try:
                    # 同花顺财务摘要格式
                    # 列名: 报告期, 净利润, 营业总收入, 基本每股收益, 每股净资产, 净资产收益率 等
                    if "报告期" in df.columns:
                        stock_data.finance_date = np.array(
                            [self._date_to_ns(d) for d in df["报告期"]], 
                            dtype=np.int64
                        )
                    
                    # 基本每股收益
                    if "基本每股收益" in df.columns:
                        stock_data.eps = self._parse_numeric_column(df["基本每股收益"])
                    
                    if "每股净资产" in df.columns:
                        stock_data.nav_per_share = self._parse_numeric_column(df["每股净资产"])
                    
                    if "净资产收益率" in df.columns:
                        # 净资产收益率可能带 % 符号
                        stock_data.roe = self._parse_numeric_column(df["净资产收益率"], is_percent=True)
                    
                    if "营业总收入" in df.columns:
                        # 营业总收入可能带"万"或"亿"单位，_parse_numeric_column 会处理
                        stock_data.main_revenue = self._parse_numeric_column(df["营业总收入"])
                    
                    if "净利润" in df.columns:
                        # 净利润可能带"万"或"亿"单位
                        stock_data.net_profit = self._parse_numeric_column(df["净利润"])
                except Exception as e:
                    logger.warning(f"处理财务数据失败: {e}")
        
        # 处理资金流向数据
        if fund_flow_data and "fund_flow" in fund_flow_data:
            df = fund_flow_data["fund_flow"]
            if not df.empty:
                try:
                    # 只取最新一条数据
                    latest = df.iloc[-1] if len(df) > 0 else None
                    if latest is not None:
                        # 主力净额和占比
                        if "主力净流入-净额" in df.columns:
                            stock_data.fund_main_amount = np.array([latest.get("主力净流入-净额", 0)], dtype=np.float64)
                        if "主力净流入-净占比" in df.columns:
                            stock_data.fund_main_ratio = np.array([latest.get("主力净流入-净占比", 0) / 100], dtype=np.float64)
                        
                        # 超大单
                        if "超大单净流入-净额" in df.columns:
                            stock_data.fund_xl_amount = np.array([latest.get("超大单净流入-净额", 0)], dtype=np.float64)
                        if "超大单净流入-净占比" in df.columns:
                            stock_data.fund_xl_ratio = np.array([latest.get("超大单净流入-净占比", 0) / 100], dtype=np.float64)
                        
                        # 大单
                        if "大单净流入-净额" in df.columns:
                            stock_data.fund_l_amount = np.array([latest.get("大单净流入-净额", 0)], dtype=np.float64)
                        if "大单净流入-净占比" in df.columns:
                            stock_data.fund_l_ratio = np.array([latest.get("大单净流入-净占比", 0) / 100], dtype=np.float64)
                        
                        # 中单
                        if "中单净流入-净额" in df.columns:
                            stock_data.fund_m_amount = np.array([latest.get("中单净流入-净额", 0)], dtype=np.float64)
                        if "中单净流入-净占比" in df.columns:
                            stock_data.fund_m_ratio = np.array([latest.get("中单净流入-净占比", 0) / 100], dtype=np.float64)
                        
                        # 小单
                        if "小单净流入-净额" in df.columns:
                            stock_data.fund_s_amount = np.array([latest.get("小单净流入-净额", 0)], dtype=np.float64)
                        if "小单净流入-净占比" in df.columns:
                            stock_data.fund_s_ratio = np.array([latest.get("小单净流入-净占比", 0) / 100], dtype=np.float64)
                except Exception as e:
                    logger.warning(f"处理资金流向数据失败: {e}")
        
        return stock_data
    
    async def fetch_stock_list(self) -> List[Dict[str, str]]:
        """获取股票列表"""
        def _fetch():
            import akshare as ak
            
            result = []
            
            # 获取沪深A股列表
            try:
                df_sh = ak.stock_info_sh_name_code()
                for _, row in df_sh.iterrows():
                    code = str(row.get("证券代码", row.get("SECURITY_CODE_A", "")))
                    name = str(row.get("证券简称", row.get("SECURITY_NAME_A", "")))
                    if code and name:
                        result.append({"code": f"SH{code}", "name": name})
            except Exception as e:
                logger.warning(f"获取上海股票列表失败: {e}")
            
            try:
                df_sz = ak.stock_info_sz_name_code()
                for _, row in df_sz.iterrows():
                    code = str(row.get("A股代码", ""))
                    name = str(row.get("A股简称", ""))
                    if code and name:
                        result.append({"code": f"SZ{code}", "name": name})
            except Exception as e:
                logger.warning(f"获取深圳股票列表失败: {e}")
            
            return result
        
        return await _run_in_executor(_fetch)

