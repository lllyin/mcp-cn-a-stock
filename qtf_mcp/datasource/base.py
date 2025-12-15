"""
数据源抽象基类

定义统一的数据接口，所有具体数据源都需要实现这些接口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np


@dataclass
class StockData:
    """
    股票数据统一格式
    
    所有数据源返回的数据都会转换成这个格式，确保上层代码不依赖具体数据源。
    """
    
    # 基础信息
    symbol: str
    name: str = ""
    
    # K线数据 (日期为纳秒级时间戳)
    date: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.int64))
    open: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))
    high: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))
    low: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))
    close: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))
    volume: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))
    amount: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))
    
    # 复权相关
    close_unadj: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))  # 不复权收盘价
    given_cash: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))   # 每股派息
    given_share: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))  # 每股送转
    
    # 财务数据
    finance_date: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.int64))
    total_shares: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))   # 总股本
    main_revenue: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))   # 主营收入
    net_profit: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))     # 净利润
    eps: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))            # 每股收益
    nav_per_share: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))  # 每股净资产
    roe: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))            # 净资产收益率
    
    # 资金流向
    fund_main_amount: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))   # 主力净额
    fund_main_ratio: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))    # 主力净占比
    fund_xl_amount: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))     # 超大单净额
    fund_xl_ratio: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))      # 超大单净占比
    fund_l_amount: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))      # 大单净额
    fund_l_ratio: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))       # 大单净占比
    fund_m_amount: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))      # 中单净额
    fund_m_ratio: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))       # 中单净占比
    fund_s_amount: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))      # 小单净额
    fund_s_ratio: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.float64))       # 小单净占比
    
    # 行业板块
    sectors: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, np.ndarray]:
        """
        转换为兼容旧 API 的字典格式
        
        为了兼容 research.py 中的现有代码，提供与原 qtf 返回格式兼容的字典。
        """
        result: Dict[str, np.ndarray] = {
            # 基本信息
            "NAME": self.name,  # type: ignore
            
            # K线数据
            "DATE": self.date,
            "OPEN": self.open,
            "HIGH": self.high,
            "LOW": self.low,
            "CLOSE": self.close,
            "VOLUME": self.volume,
            "AMOUNT": self.amount,
            "CLOSE2": self.close_unadj,
            "PRICE": self.close_unadj,
            
            # 复权相关
            "GCASH": self.given_cash,
            "GSHARE": self.given_share,
            
            # 财务数据
            "TCAP": self.total_shares,
            "MR": self.main_revenue,
            "NP": self.net_profit,
            "EPS": self.eps,
            "NAVPS": self.nav_per_share,
            "ROE": self.roe,
            
            # 资金流向
            "A_A": self.fund_main_amount,
            "A_R": self.fund_main_ratio,
            "XL_A": self.fund_xl_amount,
            "XL_R": self.fund_xl_ratio,
            "L_A": self.fund_l_amount,
            "L_R": self.fund_l_ratio,
            "M_A": self.fund_m_amount,
            "M_R": self.fund_m_ratio,
            "S_A": self.fund_s_amount,
            "S_R": self.fund_s_ratio,
            
            # 行业板块
            "SECTOR": self.sectors,  # type: ignore
        }
        
        # 添加财务数据集 (兼容原格式)
        if len(self.finance_date) > 0:
            result["_DS_FINANCE"] = (
                {
                    "DATE": self.finance_date,
                    "MR": self.main_revenue,
                    "NP": self.net_profit,
                    "EPS": self.eps,
                    "NAVPS": self.nav_per_share,
                    "ROE": self.roe,
                    "TCAP": self.total_shares,
                },
                "1q",
            )  # type: ignore
        
        return result
    
    def is_empty(self) -> bool:
        """检查数据是否为空"""
        return len(self.date) == 0


class DataSource(ABC):
    """
    数据源抽象基类
    
    所有具体数据源（如 AkShare, Tushare, 自建数据库等）都需要继承此类。
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称"""
        pass
    
    @abstractmethod
    async def fetch_stock_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> StockData:
        """
        获取股票数据
        
        Args:
            symbol: 股票代码，格式如 "SH600000" 或 "SZ000001"
            start_date: 开始日期，格式 "YYYY-MM-DD"
            end_date: 结束日期，格式 "YYYY-MM-DD"
            
        Returns:
            StockData 对象
        """
        pass
    
    @abstractmethod
    async def fetch_stock_list(self) -> List[Dict[str, str]]:
        """
        获取股票列表
        
        Returns:
            股票列表，每个元素包含 code 和 name
        """
        pass
    
    def convert_symbol_to_internal(self, symbol: str) -> str:
        """
        将内部格式 (SH600000) 转换为数据源需要的格式
        
        子类可以重写此方法以适配不同数据源的代码格式
        """
        return symbol
    
    def convert_symbol_from_internal(self, symbol: str, exchange: str = "") -> str:
        """
        将数据源格式转换为内部格式 (SH600000)
        
        子类可以重写此方法以适配不同数据源的代码格式
        """
        return symbol

