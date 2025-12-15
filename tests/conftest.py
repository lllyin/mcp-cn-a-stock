"""
pytest 配置和共享 fixture
"""

import datetime

import numpy as np
import pytest


@pytest.fixture(scope="session")
def sample_dates():
    """示例日期数据（纳秒时间戳）"""
    dates = []
    for i in range(30):
        dt = datetime.datetime(2024, 1, 1) + datetime.timedelta(days=i)
        dates.append(int(dt.timestamp() * 1e9))
    return np.array(dates, dtype=np.int64)


@pytest.fixture(scope="session")
def sample_prices(sample_dates):
    """示例价格数据"""
    n = len(sample_dates)
    np.random.seed(42)

    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.abs(np.random.randn(n) * 0.5)
    low = close - np.abs(np.random.randn(n) * 0.5)
    open_ = low + np.random.rand(n) * (high - low)

    return {
        "open": open_.astype(np.float64),
        "high": high.astype(np.float64),
        "low": low.astype(np.float64),
        "close": close.astype(np.float64),
    }


@pytest.fixture
def sample_stock_data_dict(sample_dates, sample_prices):
    """示例股票数据字典（兼容旧 API 格式）"""
    n = len(sample_dates)
    return {
        "NAME": "测试股票",
        "DATE": sample_dates,
        "OPEN": sample_prices["open"],
        "HIGH": sample_prices["high"],
        "LOW": sample_prices["low"],
        "CLOSE": sample_prices["close"],
        "VOLUME": np.random.rand(n) * 1e6,
        "AMOUNT": np.random.rand(n) * 1e8,
        "CLOSE2": sample_prices["close"],
        "PRICE": sample_prices["close"],
        "SECTOR": ["银行", "金融"],
        "TCAP": np.array([1e10] * n),
        "GCASH": np.zeros(n),
        "GSHARE": np.zeros(n),
    }

