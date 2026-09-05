"""
数据源抽象层

支持多种数据源的统一接口，方便切换不同的行情数据提供商。
"""

from .base import DataSource, FetchRequirements, StockData
from .cn_stock_source import CNStockDataSource
# 各个上游平台的注册发生在 import 期。放在这里而不是让调用方各自 import，是为了
# 保证"配置里写了名字但平台没注册"这种情况不会出现——注册表在服务起来时就是全的。
from . import platforms  # noqa: F401,E402

# 默认使用 CNStock 数据源
default_datasource: DataSource = CNStockDataSource()


def get_datasource() -> DataSource:
    
    """获取当前配置的数据源"""
    return default_datasource


def set_datasource(source: DataSource) -> None:
    """设置数据源"""
    global default_datasource
    default_datasource = source


__all__ = [
    "DataSource",
    "FetchRequirements",
    "StockData",
    "CNStockDataSource",
    "get_datasource",
    "set_datasource",
]
