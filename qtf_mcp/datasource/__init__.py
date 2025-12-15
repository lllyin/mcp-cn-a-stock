"""
数据源抽象层

支持多种数据源的统一接口，方便切换不同的行情数据提供商。
"""

from .base import DataSource, StockData
from .akshare_source import AkShareDataSource

# 默认使用 AkShare 数据源
default_datasource: DataSource = AkShareDataSource()


def get_datasource() -> DataSource:
    """获取当前配置的数据源"""
    return default_datasource


def set_datasource(source: DataSource) -> None:
    """设置数据源"""
    global default_datasource
    default_datasource = source


__all__ = [
    "DataSource",
    "StockData",
    "AkShareDataSource",
    "get_datasource",
    "set_datasource",
]

