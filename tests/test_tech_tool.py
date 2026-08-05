"""
Technical indicator JSON tool tests.
"""

import asyncio
import datetime
import importlib
import json

import numpy as np
import pytest

from qtf_mcp.datasource.base import FetchRequirements

app_module = importlib.import_module("qtf_mcp.mcp_app")


def _make_raw_data(symbol: str, name: str = "测试股票", n: int = 40):
    base = datetime.datetime(2026, 1, 1)
    dates = np.array(
        [int((base + datetime.timedelta(days=i)).timestamp() * 1e9) for i in range(n)],
        dtype=np.int64,
    )
    close = np.linspace(10.0, 20.0, n, dtype=np.float64)
    return {
        "SYMBOL": symbol,
        "NAME": name,
        "DATE": dates,
        "OPEN": close - 0.2,
        "HIGH": close + 0.5,
        "LOW": close - 0.5,
        "CLOSE": close,
        "VOLUME": np.linspace(100000.0, 200000.0, n, dtype=np.float64),
    }


def _dump_json(response):
    if hasattr(response, "model_dump"):
        return json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    return response.json(ensure_ascii=False)


@pytest.mark.asyncio
async def test_tech_single_symbol_returns_json(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return _make_raw_data("SZ002463", "沪电股份")

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ002463", days=2)

    assert response.symbols_count == 1
    assert response.errors == {}
    report = response.reports["SZ002463"]
    assert report.symbol == "SZ002463"
    assert report.name == "沪电股份"
    assert len(report.indicators) == 2
    assert report.indicators[0].date == report.quote_date
    assert isinstance(report.indicators[0].ohlc.close, float)
    assert isinstance(report.indicators[0].ohlc.volume, float)
    assert isinstance(report.indicators[0].kdj.k, float)


@pytest.mark.asyncio
async def test_tech_batch_symbols_return_json(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return _make_raw_data(symbol)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ002463,SZ300502", days=1)

    assert response.symbols_count == 2
    assert set(response.reports) == {"SZ002463", "SZ300502"}
    assert response.errors == {}


@pytest.mark.asyncio
async def test_tech_batch_preserves_input_order_when_fetches_finish_out_of_order(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        if symbol == "SZ000001":
            await asyncio.sleep(0.01)
        return _make_raw_data(symbol)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ000001,SZ000002", days=1)

    assert list(response.reports) == ["SZ000001", "SZ000002"]


@pytest.mark.asyncio
async def test_tech_missing_indicator_values_are_null(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return _make_raw_data("SZ002463", n=35)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ002463", days=35)
    oldest = response.reports["SZ002463"].indicators[-1]

    assert oldest.macd is not None
    assert oldest.macd.dif is None
    assert oldest.macd.dea is None
    assert oldest.macd.histogram is None


@pytest.mark.asyncio
async def test_tech_query_failure_enters_errors(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return {}

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZXXXXXX", days=1)

    assert response.reports == {}
    assert "SZXXXXXX" in response.errors


@pytest.mark.asyncio
async def test_tech_output_does_not_contain_markdown_table(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return _make_raw_data("SZ002463")

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ002463", days=2)
    payload = _dump_json(response)

    assert "| --- |" not in payload
    assert "技术指标" not in payload


@pytest.mark.asyncio
async def test_tech_passes_query_date_to_loader(monkeypatch):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        seen["end_date"] = end_date
        seen["requirements"] = requirements
        return _make_raw_data("SZ002463")

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports(
        "SZ002463",
        days=1,
        date="2026-06-05",
    )

    assert response.errors == {}
    assert seen["end_date"] == "2026-06-05"
    assert seen["requirements"].finance is False
    assert seen["requirements"].fund_flow is False
    assert seen["requirements"].realtime is True
    assert seen["requirements"].unadjusted_kline is False


@pytest.mark.asyncio
async def test_markdown_batch_passes_query_date_to_loader(monkeypatch):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        seen["end_date"] = end_date
        return _make_raw_data("SZ002463")

    async def fake_build_trading_data(fp, symbol, data):
        print("# trading", file=fp)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(app_module.research, "build_trading_data", fake_build_trading_data)

    response = await app_module.fetch_batch_reports(
        "SZ002463",
        "brief",
        "",
        date="2026-06-05",
    )

    assert response.errors == {}
    assert seen["end_date"] == "2026-06-05"


@pytest.mark.asyncio
async def test_tech_batch_limit_adds_warning(monkeypatch):
    seen_symbols = []

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        seen_symbols.append(symbol)
        return _make_raw_data(symbol)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports(
        "SZ000001,SZ000002,SZ000003,SZ000004,SZ000005",
        days=1,
    )

    assert response.symbols_count == 4
    assert seen_symbols == ["SZ000001", "SZ000002", "SZ000003", "SZ000004"]
    assert len(response.warnings) == 1
    assert "批量查询最多支持 4 个标的" in response.warnings[0]
    assert "SZ000005" in response.warnings[0]


@pytest.mark.asyncio
async def test_markdown_batch_limit_adds_warning(monkeypatch):
    seen_symbols = []

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        seen_symbols.append(symbol)
        return _make_raw_data(symbol)

    async def fake_build_trading_data(fp, symbol, data):
        print("# trading", file=fp)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(app_module.research, "build_trading_data", fake_build_trading_data)

    response = await app_module.fetch_batch_reports(
        "SZ000001,SZ000002,SZ000003,SZ000004,SZ000005",
        "brief",
        "",
    )

    assert response.symbols_count == 4
    assert seen_symbols == ["SZ000001", "SZ000002", "SZ000003", "SZ000004"]
    assert len(response.warnings) == 1
    assert "批量查询最多支持 4 个标的" in response.warnings[0]
    assert "SZ000005" in response.warnings[0]


@pytest.mark.asyncio
async def test_full_enables_historical_fund_flow(monkeypatch):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return _make_raw_data(symbol)

    async def fake_build_trading_data(
        fp,
        symbol,
        data,
        include_historical_fund_flow=False,
        historical_fund_flow_limit=15,
    ):
        seen["include_historical_fund_flow"] = include_historical_fund_flow
        seen["historical_fund_flow_limit"] = historical_fund_flow_limit
        print("# trading", file=fp)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(app_module.research, "build_trading_data", fake_build_trading_data)

    response = await app_module.fetch_batch_reports(
        "SZ002463",
        "full",
        "",
        fund_flow_limit=3,
    )

    assert response.errors == {}
    assert seen["include_historical_fund_flow"] is True
    assert seen["historical_fund_flow_limit"] == 3


@pytest.mark.asyncio
async def test_medium_keeps_historical_fund_flow_disabled(monkeypatch):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        return _make_raw_data(symbol)

    async def fake_build_trading_data(
        fp,
        symbol,
        data,
        include_historical_fund_flow=False,
        historical_fund_flow_limit=15,
    ):
        seen["include_historical_fund_flow"] = include_historical_fund_flow
        seen["historical_fund_flow_limit"] = historical_fund_flow_limit
        print("# trading", file=fp)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(app_module.research, "build_trading_data", fake_build_trading_data)

    response = await app_module.fetch_batch_reports("SZ002463", "medium", "")

    assert response.errors == {}
    assert seen["include_historical_fund_flow"] is False
    assert seen["historical_fund_flow_limit"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["brief", "medium", "full"])
async def test_markdown_reports_preserve_all_datasource_requirements(monkeypatch, mode):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        seen["requirements"] = requirements
        return _make_raw_data(symbol)

    async def fake_build_trading_data(fp, symbol, data, **kwargs):
        print("# trading", file=fp)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(app_module.research, "build_trading_data", fake_build_trading_data)

    response = await app_module.fetch_batch_reports("SZ002463", mode, "")

    assert response.errors == {}
    assert seen["requirements"] == FetchRequirements()


@pytest.mark.asyncio
async def test_report_modes_share_batch_concurrency_limit(monkeypatch):
    active = 0
    max_active = 0

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return _make_raw_data(symbol)

    async def fake_build_trading_data(fp, symbol, data, **kwargs):
        print("# trading", file=fp)

    monkeypatch.setattr(app_module, "BATCH_QUERY_CONCURRENCY", 2)
    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)
    monkeypatch.setattr(app_module.research, "build_trading_data", fake_build_trading_data)

    responses = await asyncio.gather(
        app_module.fetch_batch_reports("SZ000001", "brief", ""),
        app_module.fetch_batch_reports("SZ000002", "medium", ""),
        app_module.fetch_batch_reports("SZ000003", "full", ""),
    )

    assert max_active == 2
    assert all(response.errors == {} for response in responses)


@pytest.mark.asyncio
async def test_batch_admission_queued_cancellation_does_not_leak_permit():
    admission = app_module.BatchQueryAdmission(1)
    await admission.acquire()
    queued = asyncio.create_task(admission.acquire())
    await asyncio.sleep(0)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    admission.release()

    await asyncio.wait_for(admission.acquire(), timeout=0.1)
    assert admission.active == 1
    assert admission.waiting == 0
    admission.release()


@pytest.mark.asyncio
async def test_cancelled_report_releases_batch_permit(monkeypatch):
    admission = app_module.BatchQueryAdmission(1)
    started = asyncio.Event()

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "_get_batch_query_admission", lambda: admission)
    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    request = asyncio.create_task(
        app_module.fetch_batch_reports("SZ000001", "brief", "")
    )
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert admission.active == 0
    await asyncio.wait_for(admission.acquire(), timeout=0.1)
    admission.release()
