"""
Realtime fund-flow page routing tests.
"""

import asyncio
import datetime
import importlib
from io import StringIO

import numpy as np
import pytest

from qtf_mcp import research
from qtf_mcp.datasource import realtime_ff
from qtf_mcp.datasource.realtime_ff import get_fund_flow_display_name, get_fund_flow_url

app_module = importlib.import_module("qtf_mcp.mcp_app")


def test_core_indices_use_specific_index_pages():
    assert get_fund_flow_url("000001") == "https://data.eastmoney.com/zjlx/zs000001.html"
    assert get_fund_flow_url("SH000001") == "https://data.eastmoney.com/zjlx/zs000001.html"
    assert get_fund_flow_url("399001") == "https://data.eastmoney.com/zjlx/zs399001.html"
    assert get_fund_flow_url("SZ399001") == "https://data.eastmoney.com/zjlx/zs399001.html"
    assert get_fund_flow_url("399006") == "https://data.eastmoney.com/zjlx/zs399006.html"
    assert get_fund_flow_url("SZ399006") == "https://data.eastmoney.com/zjlx/zs399006.html"


def test_indices_without_realtime_pages_return_none():
    assert get_fund_flow_url("000688") is None
    assert get_fund_flow_url("SH000688") is None
    assert get_fund_flow_url("dpzjlx") == "https://data.eastmoney.com/zjlx/dpzjlx.html"


def test_stock_uses_stock_page():
    assert get_fund_flow_url("300308") == "https://data.eastmoney.com/zjlx/300308.html"


def test_core_indices_use_stable_display_names():
    assert get_fund_flow_display_name("000001", "沪深资金流向") == "上证指数"
    assert get_fund_flow_display_name("399001", "沪深资金流向") == "深证成指"
    assert get_fund_flow_display_name("399006", "沪深资金流向") == "创业板指"
    assert get_fund_flow_display_name("300308", "中际旭创") == "中际旭创"


@pytest.mark.asyncio
async def test_cancelled_browser_launch_stops_partial_playwright(monkeypatch):
    launch_started = asyncio.Event()

    class FakeChromium:
        async def launch(self, **kwargs):
            launch_started.set()
            await asyncio.Event().wait()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
            self.stopped = False

        async def start(self):
            return self

        async def stop(self):
            self.stopped = True

    fake_playwright = FakePlaywright()
    monkeypatch.setattr(realtime_ff, "async_playwright", lambda: fake_playwright)
    monkeypatch.setattr(realtime_ff, "_playwright", None)
    monkeypatch.setattr(realtime_ff, "_browser", None)
    monkeypatch.setattr(realtime_ff, "_context", None)

    startup = asyncio.create_task(realtime_ff.get_context())
    await launch_started.wait()
    startup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await startup

    assert fake_playwright.stopped
    assert realtime_ff._playwright is None
    assert realtime_ff._browser is None
    assert realtime_ff._context is None


@pytest.mark.asyncio
async def test_cancelled_context_creation_closes_partial_browser(monkeypatch):
    context_started = asyncio.Event()

    class FakeBrowser:
        def __init__(self):
            self.closed = False

        async def new_context(self, **kwargs):
            context_started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    fake_browser = FakeBrowser()

    class FakeChromium:
        async def launch(self, **kwargs):
            return fake_browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
            self.stopped = False

        async def start(self):
            return self

        async def stop(self):
            self.stopped = True

    fake_playwright = FakePlaywright()
    monkeypatch.setattr(realtime_ff, "async_playwright", lambda: fake_playwright)
    monkeypatch.setattr(realtime_ff, "_playwright", None)
    monkeypatch.setattr(realtime_ff, "_browser", None)
    monkeypatch.setattr(realtime_ff, "_context", None)

    startup = asyncio.create_task(realtime_ff.get_context())
    await context_started.wait()
    startup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await startup

    assert fake_browser.closed
    assert fake_playwright.stopped
    assert realtime_ff._playwright is None
    assert realtime_ff._browser is None
    assert realtime_ff._context is None


@pytest.mark.asyncio
async def test_realtime_fund_flow_singleflight_only_while_inflight(monkeypatch):
    calls = 0
    release = asyncio.Event()
    realtime_ff._inflight.clear()

    async def fake_fetch(symbol):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"标的名称": symbol, "主力净流入": "1亿"}

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    first = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    second = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    fresh_result = await realtime_ff.fetch_single_shared("300408")

    assert calls == 2
    assert first_result == second_result == fresh_result


@pytest.mark.asyncio
async def test_realtime_fetch_survives_disconnected_leader_for_retry(monkeypatch):
    calls = 0
    release = asyncio.Event()
    realtime_ff._inflight.clear()

    async def fake_fetch(symbol):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"标的名称": symbol, "主力净流入": "1亿"}

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    disconnected = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    await asyncio.sleep(0)
    disconnected.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnected

    retry = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    await asyncio.sleep(0)
    release.set()

    assert await retry == {"标的名称": "300408", "主力净流入": "1亿"}
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_prefetch_stops_unshared_underlying_fetch(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    realtime_ff._inflight.clear()
    realtime_ff._inflight_waiters.clear()
    realtime_ff._inflight_keep_alive.clear()

    async def fake_fetch(symbol):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    prefetch = asyncio.create_task(
        realtime_ff.get_fund_flow(
            ["300408"],
            keep_alive_on_cancel=False,
        )
    )
    await started.wait()
    shared = realtime_ff._inflight["300408"]

    prefetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prefetch
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert shared.cancelled()
    assert "300408" not in realtime_ff._inflight
    assert "300408" not in realtime_ff._inflight_waiters
    assert "300408" not in realtime_ff._inflight_keep_alive


@pytest.mark.asyncio
async def test_cancelled_prefetch_keeps_fetch_used_by_another_waiter(monkeypatch):
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    realtime_ff._inflight.clear()
    realtime_ff._inflight_waiters.clear()
    realtime_ff._inflight_keep_alive.clear()

    async def fake_fetch(symbol):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"标的名称": symbol, "主力净流入": "1亿"}

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    prefetch = asyncio.create_task(
        realtime_ff.get_fund_flow(
            ["300408"],
            keep_alive_on_cancel=False,
        )
    )
    await started.wait()
    shared = realtime_ff._inflight["300408"]
    follower = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    while realtime_ff._inflight_waiters.get("300408") != 2:
        await asyncio.sleep(0)

    prefetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prefetch

    assert realtime_ff._inflight["300408"] is shared
    assert not shared.cancelled()

    release.set()
    assert await follower == {"标的名称": "300408", "主力净流入": "1亿"}
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["brief", "medium", "full"])
async def test_all_markdown_report_modes_use_playwright_during_live_window(monkeypatch, mode):
    calls = []

    async def fake_get_fund_flow(symbols, **kwargs):
        calls.append(symbols)
        return '{"300408": {"标的名称": "三环集团", "主力净流入": "1亿", "主力净比(%)": 1}}'

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    data = {
        "SYMBOL": "SZ300408",
        "DATE": np.array([int(datetime.datetime(2026, 8, 5).timestamp() * 1e9)]),
        "OPEN": np.array([10.0]),
        "HIGH": np.array([10.5]),
        "LOW": np.array([9.8]),
        "CLOSE": np.array([10.2]),
        "VOL": np.array([10000.0]),
        "AMOUNT": np.array([1000000.0]),
    }
    output = StringIO()

    await research.build_trading_data(
        output,
        "SZ300408",
        data,
        include_historical_fund_flow=mode == "full",
    )

    assert calls == [["300408"]]
    assert "主力净流入: 1亿" in output.getvalue()


def _live_window_raw_data(symbol: str = "SZ300408"):
    return {
        "SYMBOL": symbol,
        "NAME": "三环集团",
        "DATE": np.array([int(datetime.datetime(2026, 8, 5).timestamp() * 1e9)]),
        "OPEN": np.array([10.0]),
        "HIGH": np.array([10.5]),
        "LOW": np.array([9.8]),
        "CLOSE": np.array([10.2]),
        "VOLUME": np.array([10000.0]),
        "AMOUNT": np.array([1000000.0]),
    }


_LIVE_PAYLOAD = (
    '{"300408": {"标的名称": "三环集团", "主力净流入": "1亿", "主力净比(%)": 1}}'
)


def test_prefetch_skipped_when_scraping_would_not_run(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    assert research.start_realtime_fund_flow_prefetch("SZ300408", "2026-06-05") is None
    assert research.start_realtime_fund_flow_prefetch("SH000688") is None

    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: False)
    assert research.start_realtime_fund_flow_prefetch("SZ300408") is None


def test_prefetch_target_matches_report_target():
    for symbol in ("SZ300408", "SH600519", "SH000001", "SZ399006", "SH000688", "SH512480"):
        assert research.resolve_realtime_fund_flow_target(
            symbol
        ) == research.get_realtime_fund_flow_target(symbol, {})


@pytest.mark.asyncio
async def test_live_scrape_starts_before_base_data_completes(monkeypatch):
    order = []

    async def fake_get_fund_flow(symbols, **kwargs):
        order.append(f"scrape_started:{symbols}")
        await asyncio.sleep(0)
        return _LIVE_PAYLOAD

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        order.append("raw_started")
        await asyncio.sleep(0.02)
        order.append("raw_finished")
        return _live_window_raw_data(symbol)

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_batch_reports("SZ300408", "brief", "")

    assert response.errors == {}
    assert "主力净流入: 1亿" in response.reports["SZ300408"]
    assert order.count("scrape_started:['300408']") == 1
    assert order.index("scrape_started:['300408']") < order.index("raw_finished")


@pytest.mark.asyncio
async def test_prefetched_report_is_identical_to_inline_report(monkeypatch):
    calls = []

    async def fake_get_fund_flow(symbols, **kwargs):
        calls.append(symbols)
        return _LIVE_PAYLOAD

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    inline = StringIO()
    await research.build_trading_data(inline, "SZ300408", _live_window_raw_data())

    prefetch = research.start_realtime_fund_flow_prefetch("SZ300408")
    assert prefetch is not None
    prefetched = StringIO()
    await research.build_trading_data(
        prefetched,
        "SZ300408",
        _live_window_raw_data(),
        realtime_fund_flow=prefetch,
    )

    assert prefetched.getvalue() == inline.getvalue()
    assert calls == [["300408"], ["300408"]]


@pytest.mark.asyncio
async def test_report_refetches_when_prefetch_target_differs(monkeypatch):
    calls = []

    async def fake_get_fund_flow(symbols, **kwargs):
        calls.append(symbols)
        return '{"dpzjlx": {"标的名称": "沪深两市", "主力净流入": "2亿", "主力净比(%)": 2}}'

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    prefetch = research.RealtimeFundFlowPrefetch(
        "300408",
        asyncio.create_task(asyncio.sleep(0, result=_LIVE_PAYLOAD)),
    )
    data = _live_window_raw_data()
    data["IS_MARKET"] = True
    output = StringIO()

    await research.build_trading_data(
        output,
        "SZ300408",
        data,
        realtime_fund_flow=prefetch,
    )

    assert calls == [["dpzjlx"]]
    assert "沪深两市主力净流入: 2亿" in output.getvalue()
    prefetch.discard()


@pytest.mark.asyncio
async def test_prefetch_failure_matches_inline_failure_output(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    async def failing_get_fund_flow(symbols, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(research, "get_fund_flow", failing_get_fund_flow)
    inline = StringIO()
    await research.build_trading_data(inline, "SZ300408", _live_window_raw_data())

    prefetch = research.start_realtime_fund_flow_prefetch("SZ300408")
    assert prefetch is not None
    prefetched = StringIO()
    await research.build_trading_data(
        prefetched,
        "SZ300408",
        _live_window_raw_data(),
        realtime_fund_flow=prefetch,
    )

    assert prefetched.getvalue() == inline.getvalue()
    assert "[实时调用异常] boom" in prefetched.getvalue()


@pytest.mark.asyncio
async def test_unused_prefetch_is_discarded_without_task_warnings(monkeypatch):
    started = []

    async def fake_get_fund_flow(symbols, **kwargs):
        started.append(symbols)
        await asyncio.sleep(5)
        return _LIVE_PAYLOAD

    async def empty_load_raw_data(symbol, end_date=None, who="", requirements=None):
        await asyncio.sleep(0.02)
        return {}

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(app_module.research, "load_raw_data", empty_load_raw_data)

    response = await app_module.fetch_batch_reports("SZ300408", "brief", "")

    assert "未找到证券代码" in response.errors["SZ300408"]
    assert started == [["300408"]]
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


@pytest.mark.asyncio
async def test_discard_consumes_failed_prefetch_exception(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    async def failing_get_fund_flow(symbols, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(research, "get_fund_flow", failing_get_fund_flow)
    prefetch = research.start_realtime_fund_flow_prefetch("SZ300408")
    assert prefetch is not None
    await asyncio.sleep(0)

    prefetch.discard()

    assert isinstance(prefetch.task.exception(), RuntimeError)
