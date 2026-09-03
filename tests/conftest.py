"""
pytest 配置和共享 fixture
"""

import datetime
import os

# 通道安装会全局改写 requests，且发生在 qtf_mcp.datasource 导入期。
# 测试固定为 direct（原生 requests），保证断言的是业务逻辑而不是某个通道的转发行为；
# 需要验证通道本身的测试自行调用 install_http_channel。
os.environ.setdefault("CN_STOCK_HTTP_MODE", "direct")

import numpy as np
import pytest

from qtf_mcp import cache as cache_module
from qtf_mcp import research as research_module
from qtf_mcp.datasource import realtime_ff as realtime_ff_module


@pytest.fixture(autouse=True)
def closed_realtime_window(monkeypatch):
    """默认让实时资金流窗口为关闭状态。

    报告链路在 load_raw_data 之前就启动实时抓取，所以只 mock load_raw_data
    不足以隔离浏览器。若沿用真实时钟，同一个测试在 09:15-17:00 之内会真的
    拉起 Chromium，之后事件循环无法关闭，表现为测试全部通过但进程挂住。
    需要实时分支的测试自行把它 patch 成 True，并同时 patch get_fund_flow。
    """
    monkeypatch.setattr(
        research_module, "is_realtime_fund_flow_window", lambda now=None: False
    )


@pytest.fixture(autouse=True)
def forbid_real_browser(monkeypatch):
    """触达真实 Playwright 的测试应当立刻失败，而不是挂住。

    与 closed_realtime_window 配套：前者消除时钟依赖，这一条保证"想跑实时
    分支但忘了 patch"会得到明确报错。

    两层都换掉：报告链路走 research.get_fund_flow，在这一层报错才能带出可读
    信息（get_fund_flow 自己会把每个标的的 Exception 收敛成结果里的 error
    字段）；realtime_ff 那一层是最后一道栅栏，保证任何调用方都无法真的拉起
    Chromium。用 pytest.fail 而非 AssertionError，因为它继承 BaseException，
    能穿透链路上的 except Exception。
    """

    async def refuse_report_path(symbols, **kwargs):
        pytest.fail(
            f"测试触达了真实 Playwright（symbols={symbols}）。"
            "请 patch research.get_fund_flow；需要实时分支时同时 patch "
            "is_realtime_fund_flow_window。",
            pytrace=False,
        )

    async def refuse_browser(symbol):
        pytest.fail(
            f"测试触达了真实浏览器（symbol={symbol}）。", pytrace=False
        )

    monkeypatch.setattr(research_module, "get_fund_flow", refuse_report_path)
    monkeypatch.setattr(realtime_ff_module, "_fetch_single_with_context", refuse_browser)
    # 资金流向页面兜底是第二个会拉起 Chromium 的入口，同样要拦住：主源在测试
    # 环境里必然失败，兜底会被触发。
    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", refuse_browser)


@pytest.fixture(scope="session", autouse=True)
def no_browser_left_behind():
    """会话结束时确认没有测试起过浏览器。

    这里刻意不去关它：Playwright 对象绑定创建它的事件循环，用新循环调
    close_browser() 可能抛错甚至挂住，而挂住正是这两道 guard 要消除的问题。
    浏览器还在说明 guard 被绕过了，那是需要修测试而不是兜底掩盖。
    """
    yield
    if realtime_ff_module._browser is not None:
        pytest.fail(
            "会话结束时浏览器仍然存活，说明某个测试绕过了 forbid_real_browser；"
            "事件循环可能无法关闭。请修正该测试的 mock。",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def isolate_report_cache():
    """默认关闭报告缓存，并隔离生产缓存目录。

    缓存是一个可选层，断言"是否回源"的测试必须在关闭状态下运行；
    需要缓存的测试自行调用 set_report_cache 覆盖。
    """
    cache_module.set_report_cache(
        cache_module.ReportCache(enabled=False, disk_enabled=False)
    )
    yield
    cache_module.set_report_cache(None)


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

