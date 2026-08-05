"""
Realtime fund-flow page routing tests.
"""

import asyncio
import datetime
from io import StringIO

import numpy as np
import pytest

from qtf_mcp import research
from qtf_mcp.datasource import realtime_ff
from qtf_mcp.datasource.realtime_ff import get_fund_flow_display_name, get_fund_flow_url


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
@pytest.mark.parametrize("mode", ["brief", "medium", "full"])
async def test_all_markdown_report_modes_use_playwright_during_live_window(monkeypatch, mode):
    calls = []

    async def fake_get_fund_flow(symbols):
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
