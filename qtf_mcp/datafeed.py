"""
数据获取模块

提供统一的数据获取接口，底层使用可插拔的数据源。
"""

import json
import logging
import time
from typing import Dict, List

import numpy as np

from .datasource import get_datasource

logger = logging.getLogger("qtf_mcp")

# 股票板块数据配置文件路径
stock_sector_data = "confs/stock_sector.json"

STOCK_SECTOR: Dict[str, List[str]] | None = None


def get_stock_sector() -> Dict[str, List[str]]:
    """获取股票板块映射"""
    global STOCK_SECTOR
    if STOCK_SECTOR is None:
        try:
            with open(stock_sector_data, "r", encoding="utf-8") as f:
                STOCK_SECTOR = json.load(f)
        except FileNotFoundError:
            logger.warning(f"板块配置文件不存在: {stock_sector_data}")
            STOCK_SECTOR = {}
        except json.JSONDecodeError as e:
            logger.error(f"板块配置文件解析失败: {e}")
            STOCK_SECTOR = {}
    return STOCK_SECTOR if STOCK_SECTOR is not None else {}


async def load_data_msd(
    symbol: str, start_date: str, end_date: str, n: int = 0, who: str = ""
) -> Dict[str, np.ndarray]:
    """
    获取单只股票的数据

    为了兼容现有代码，返回与原 qtf 格式兼容的字典。

    Args:
        symbol: 股票代码，如 "SH600000"
        start_date: 开始日期
        end_date: 结束日期
        n: 保留参数，暂未使用
        who: 调用者标识，用于日志

    Returns:
        包含各类数据的字典
    """
    t1 = time.time()

    datasource = get_datasource()
    stock_data = await datasource.fetch_stock_data(symbol, start_date, end_date)

    t2 = time.time()
    logger.info(f"{who} [{datasource.name}] fetch data cost {t2 - t1:.2f}s, symbol: {symbol}")

    if stock_data.is_empty():
        return {}

    # 添加板块信息
    sector = get_stock_sector().get(symbol, [])
    stock_data.sectors = sector

    return stock_data.to_dict()


def load_data_msd_batch(
    symbols: List[str], start_date: str, end_date: str, n: int = 0, who: str = ""
) -> Dict[str, Dict[str, np.ndarray]]:
    """
    批量获取股票数据

    注意：此函数为同步版本，内部会创建事件循环。
    如果在异步上下文中使用，请使用 load_data_msd_batch_async。
    """
    import asyncio

    async def _batch():
        results = {}
        for symbol in symbols:
            data = await load_data_msd(symbol, start_date, end_date, n, who)
            if data:
                results[symbol] = data
        return results

    return asyncio.run(_batch())


async def load_data_msd_batch_async(
    symbols: List[str], start_date: str, end_date: str, n: int = 0, who: str = ""
) -> Dict[str, Dict[str, np.ndarray]]:
    """
    异步批量获取股票数据
    """
    import asyncio

    async def fetch_one(symbol: str) -> tuple:
        data = await load_data_msd(symbol, start_date, end_date, n, who)
        return symbol, data

    tasks = [fetch_one(s) for s in symbols]
    results_list = await asyncio.gather(*tasks)

    return {symbol: data for symbol, data in results_list if data}
