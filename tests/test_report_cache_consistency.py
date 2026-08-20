"""缓存开关前后返回一致性验证

只桩掉取数层（load_raw_data），渲染路径全部走真实代码。
断言：开启缓存后返回的报告与关闭缓存时逐字节相同，且第二次调用不再回源。
"""

import asyncio
import datetime
import inspect
import logging
import sys
import types

import numpy as np
import pytest

import qtf_mcp.mcp_app  # noqa: F401  确保子模块已加载
from qtf_mcp import cache as cache_module
from qtf_mcp import research
from qtf_mcp.cache import ReportCache
from qtf_mcp.datasource.base import StockData

# qtf_mcp/__init__.py 把 `mcp_app` 这个名字重绑定成了 QtfMCP 实例，
# 因此必须从 sys.modules 取真正的模块对象。
mcp_app = sys.modules["qtf_mcp.mcp_app"]


def build_raw_data(symbol: str = "SH600000", bars: int = 300) -> dict:
    """用真实的 StockData.to_dict() 造数据，保证字典形状与生产一致。"""
    rng = np.random.default_rng(20260820)
    base = datetime.datetime(2025, 1, 2)
    dates = np.array(
        [int((base + datetime.timedelta(days=i)).timestamp() * 1e9) for i in range(bars)],
        dtype=np.int64,
    )
    close = 10 + np.cumsum(rng.standard_normal(bars) * 0.1)
    high = close + np.abs(rng.standard_normal(bars) * 0.1)
    low = close - np.abs(rng.standard_normal(bars) * 0.1)

    data = StockData(symbol=symbol, name="测试标的")
    data.date = dates
    data.open = low + (high - low) * 0.5
    data.high = high
    data.low = low
    data.close = close
    data.close_unadj = close.copy()
    data.volume = np.abs(rng.standard_normal(bars)) * 1e6
    data.amount = np.abs(rng.standard_normal(bars)) * 1e8
    data.given_cash = np.zeros(bars)
    data.given_share = np.zeros(bars)

    fin_bars = 8
    fin_base = datetime.datetime(2024, 3, 31)
    data.finance_date = np.array(
        [int((fin_base + datetime.timedelta(days=90 * i)).timestamp() * 1e9) for i in range(fin_bars)],
        dtype=np.int64,
    )
    data.eps = np.linspace(0.5, 1.2, fin_bars)
    data.nav_per_share = np.linspace(5.0, 7.0, fin_bars)
    data.roe = np.linspace(0.08, 0.14, fin_bars)
    data.main_revenue = np.linspace(1e9, 2e9, fin_bars)
    data.net_profit = np.linspace(1e8, 3e8, fin_bars)
    data.total_shares = np.array([1.8e9])
    data.float_shares = np.array([1.5e9])
    data.total_market_cap = np.array([1.8e10])
    data.float_market_cap = np.array([1.5e10])
    data.pe_ttm = np.array([12.5])

    for attr, value in (
        ("fund_main_amount", 1.2e8), ("fund_main_ratio", 0.031),
        ("fund_xl_amount", 6.0e7), ("fund_xl_ratio", 0.015),
        ("fund_l_amount", 6.0e7), ("fund_l_ratio", 0.016),
        ("fund_m_amount", -3.0e7), ("fund_m_ratio", -0.008),
        ("fund_s_amount", -9.0e7), ("fund_s_ratio", -0.023),
    ):
        setattr(data, attr, np.array([value], dtype=np.float64))

    ff_bars = 20
    data.fund_flow_history = {
        "DATE": dates[-ff_bars:],
        "CLOSE": close[-ff_bars:],
        "PCT_CHG": rng.standard_normal(ff_bars) * 0.01,
        "A_A": rng.standard_normal(ff_bars) * 1e8,
        "A_R": rng.standard_normal(ff_bars) * 0.02,
        "XL_A": rng.standard_normal(ff_bars) * 1e7,
        "XL_R": rng.standard_normal(ff_bars) * 0.01,
        "L_A": rng.standard_normal(ff_bars) * 1e7,
        "L_R": rng.standard_normal(ff_bars) * 0.01,
        "M_A": rng.standard_normal(ff_bars) * 1e7,
        "M_R": rng.standard_normal(ff_bars) * 0.01,
        "S_A": rng.standard_normal(ff_bars) * 1e7,
        "S_R": rng.standard_normal(ff_bars) * 0.01,
    }
    return data.to_dict()


@pytest.fixture
def deterministic_render(monkeypatch):
    """固定渲染分支与取数结果，并统计回源次数。"""
    calls = {"count": 0}
    raw = build_raw_data()

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        calls["count"] += 1
        await asyncio.sleep(0)
        return dict(raw)

    # 走非交易时段分支，避免测试依赖真实时钟拉起 Chromium
    monkeypatch.setattr(research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: False)
    monkeypatch.setattr(
        research, "start_realtime_fund_flow_prefetch", lambda symbol, date=None: None
    )
    yield calls
    cache_module.set_report_cache(None)


def install_cache(tmp_path, **kwargs) -> ReportCache:
    params = {
        "enabled": True,
        "live_ttl_seconds": 600.0,  # 覆盖盘中运行的情况，使测试与时钟无关
        "max_entries": 64,
        "disk_enabled": False,
        "directory": str(tmp_path / "cache"),
    }
    params.update(kwargs)
    cache = ReportCache(**params)
    cache_module.set_report_cache(cache)
    return cache


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["brief", "medium", "full"])
async def test_cached_reports_are_byte_identical(mode, tmp_path, deterministic_render):
    """关闭缓存的输出 == 开启缓存首次输出 == 开启缓存命中输出。"""
    install_cache(tmp_path, enabled=False)
    baseline = await mcp_app.fetch_batch_reports("SH600000", mode, "test")
    assert deterministic_render["count"] == 1

    cache = install_cache(tmp_path)
    first = await mcp_app.fetch_batch_reports("SH600000", mode, "test")
    assert deterministic_render["count"] == 2

    second = await mcp_app.fetch_batch_reports("SH600000", mode, "test")
    assert deterministic_render["count"] == 2, "命中缓存后不应再回源"
    assert cache.hits == 1

    assert first.reports["SH600000"] == baseline.reports["SH600000"]
    assert second.reports["SH600000"] == baseline.reports["SH600000"]
    assert second.errors == baseline.errors
    assert second.warnings == baseline.warnings
    assert second.symbols_count == baseline.symbols_count


@pytest.mark.asyncio
async def test_partial_batch_hit_still_fetches_missing_symbol(tmp_path, deterministic_render):
    """一批 4 个标的中命中 3 个时，只回源缺失的那个。"""
    install_cache(tmp_path)
    await mcp_app.fetch_batch_reports("SH600000,SH600001,SH600002", "brief", "test")
    assert deterministic_render["count"] == 3

    await mcp_app.fetch_batch_reports("SH600000,SH600001,SH600002,SH600003", "brief", "test")
    assert deterministic_render["count"] == 4, "只应为新增标的回源一次"


@pytest.mark.asyncio
async def test_response_timestamp_is_regenerated(tmp_path, monkeypatch, deterministic_render):
    """报告正文可以复用，但响应外壳的生成时间必须是当次的。"""
    install_cache(tmp_path)
    clock = [datetime.datetime(2026, 8, 17, 18, 0, 0)]

    fake = types.SimpleNamespace(
        datetime=types.SimpleNamespace(now=lambda: clock[0]),
        timedelta=datetime.timedelta,
    )
    monkeypatch.setattr(mcp_app, "datetime", fake)

    first = await mcp_app.fetch_batch_reports("SH600000", "brief", "test")
    clock[0] = datetime.datetime(2026, 8, 17, 18, 30, 0)
    second = await mcp_app.fetch_batch_reports("SH600000", "brief", "test")

    assert second.reports["SH600000"] == first.reports["SH600000"]
    assert first.timestamp == "2026-08-17 18:00:00"
    assert second.timestamp == "2026-08-17 18:30:00"


@pytest.mark.asyncio
async def test_empty_data_is_not_cached(tmp_path, monkeypatch):
    """取不到数据是瞬时状态，不能被固化一个纪元。"""
    calls = {"count": 0}

    async def empty_load(symbol, end_date=None, who="", requirements=None):
        calls["count"] += 1
        return {}

    monkeypatch.setattr(research, "load_raw_data", empty_load)
    monkeypatch.setattr(
        research, "start_realtime_fund_flow_prefetch", lambda symbol, date=None: None
    )
    install_cache(tmp_path)

    first = await mcp_app.fetch_batch_reports("SH600000", "brief", "test")
    second = await mcp_app.fetch_batch_reports("SH600000", "brief", "test")

    assert calls["count"] == 2, "空结果不应进入缓存"
    assert first.errors == second.errors
    cache_module.set_report_cache(None)


@pytest.mark.asyncio
async def test_disabled_cache_never_serves(tmp_path, deterministic_render):
    """总开关关闭时每次都回源。"""
    install_cache(tmp_path, enabled=False)
    for expected in (1, 2, 3):
        await mcp_app.fetch_batch_reports("SH600000", "brief", "test")
        assert deterministic_render["count"] == expected


@pytest.mark.asyncio
async def test_tech_reports_round_trip_through_cache(tmp_path, deterministic_render):
    """tech 返回结构化模型，经 JSON 往返后必须与原对象相等。"""
    install_cache(tmp_path, enabled=False)
    baseline = await mcp_app.fetch_technical_reports("SH600000", days=30)

    install_cache(tmp_path)
    first = await mcp_app.fetch_technical_reports("SH600000", days=30)
    second = await mcp_app.fetch_technical_reports("SH600000", days=30)

    key = "SH600000"
    assert first.reports[key] == baseline.reports[key]
    assert second.reports[key] == baseline.reports[key]
    assert second.reports[key].model_dump() == baseline.reports[key].model_dump()


@pytest.mark.asyncio
async def test_tech_params_are_not_conflated(tmp_path, deterministic_render):
    """days 不同必须分别回源，不能互相命中。"""
    install_cache(tmp_path)
    await mcp_app.fetch_technical_reports("SH600000", days=30)
    await mcp_app.fetch_technical_reports("SH600000", days=60)
    assert deterministic_render["count"] == 2

    a = await mcp_app.fetch_technical_reports("SH600000", days=30)
    b = await mcp_app.fetch_technical_reports("SH600000", days=60)
    assert deterministic_render["count"] == 2
    assert len(a.reports["SH600000"].indicators) != len(b.reports["SH600000"].indicators)


# --- 命中日志 -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["brief", "medium", "full"])
async def test_report_cache_hit_is_logged(mode, tmp_path, caplog, deterministic_render):
    install_cache(tmp_path)
    await mcp_app.fetch_batch_reports("SH600000", mode, "test")

    caplog.set_level(logging.INFO, logger="qtf_mcp")
    await mcp_app.fetch_batch_reports("SH600000", mode, "test")

    hits = [r for r in caplog.records if "Report cache hit" in r.getMessage()]
    assert len(hits) == 1
    message = hits[0].getMessage()
    assert f"tool={mode}" in message
    assert "symbol=SH600000" in message
    assert "epoch=" in message


@pytest.mark.asyncio
async def test_tech_cache_hit_is_logged(tmp_path, caplog, deterministic_render):
    install_cache(tmp_path)
    await mcp_app.fetch_technical_reports("SH600000", days=30)

    caplog.set_level(logging.INFO, logger="qtf_mcp")
    await mcp_app.fetch_technical_reports("SH600000", days=30)

    hits = [r for r in caplog.records if "Report cache hit" in r.getMessage()]
    assert len(hits) == 1
    assert "tool=tech" in hits[0].getMessage()


# --- 缓存故障不得损坏响应 -----------------------------------------------


class ExplodingCache(ReportCache):
    """读写都抛异常的缓存，用于验证故障隔离。"""

    def _get(self, key):
        raise RuntimeError("cache read exploded")

    def _put(self, key, value):
        raise RuntimeError("cache write exploded")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["brief", "full"])
async def test_cache_faults_do_not_damage_reports(mode, tmp_path, caplog, deterministic_render):
    """写入发生在报告生成之后；写失败绝不能把好报告变成错误。"""
    install_cache(tmp_path, enabled=False)
    baseline = await mcp_app.fetch_batch_reports("SH600000", mode, "test")

    cache_module.set_report_cache(
        ExplodingCache(enabled=True, disk_enabled=False, directory=str(tmp_path / "boom"))
    )
    caplog.set_level(logging.WARNING, logger="qtf_mcp")
    broken = await mcp_app.fetch_batch_reports("SH600000", mode, "test")

    assert broken.errors == {}
    assert broken.reports["SH600000"] == baseline.reports["SH600000"]
    # 证明故障缓存确实被调用过，测试不是空过
    faults = [r for r in caplog.records if "Report cache" in r.getMessage()]
    assert any("read failed" in r.getMessage() or "lookup failed" in r.getMessage() for r in faults)
    assert any("write failed" in r.getMessage() for r in faults)


@pytest.mark.asyncio
async def test_cache_faults_do_not_break_tech(tmp_path, deterministic_render):
    install_cache(tmp_path, enabled=False)
    baseline = await mcp_app.fetch_technical_reports("SH600000", days=30)

    cache_module.set_report_cache(
        ExplodingCache(enabled=True, disk_enabled=False, directory=str(tmp_path / "boom"))
    )
    broken = await mcp_app.fetch_technical_reports("SH600000", days=30)

    assert broken.errors == {}
    assert broken.reports["SH600000"].model_dump() == baseline.reports["SH600000"].model_dump()


@pytest.mark.asyncio
async def test_every_cached_tool_logs_its_hit():
    """接入缓存的工具必须同时打命中日志和未命中日志。

    只打命中日志会造成不对称：未命中时日志里什么都没有，运维只能靠 DEBUG 级的
    取数记录反推——分析 19:07 那段日志时就因此把 53 次 kline 调用误判成"零调用"。
    """
    LOG_MARKERS = {
        "fetch_batch_reports": ("Report cache hit", "Finished symbol"),
        "fetch_technical_reports": ("Report cache hit", "Finished tech query"),
        "kline_daily": ("Report cache hit", "Finished kline_daily"),
        "kline_range": ("Report cache hit", "Finished kline_range"),
    }
    missing = []
    for name, (hit_marker, miss_marker) in LOG_MARKERS.items():
        fn = getattr(mcp_app, name)
        fn = getattr(fn, "fn", fn)  # FastMCP 会包装工具函数
        source = inspect.getsource(fn)
        if "report_cache.get" not in source:
            continue
        if hit_marker not in source:
            missing.append(f"{name}: 缺命中日志")
        if miss_marker not in source:
            missing.append(f"{name}: 缺未命中日志")
    assert missing == [], f"日志缺失: {missing}"
