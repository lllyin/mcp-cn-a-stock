"""
Technical indicator JSON tool tests.
"""

import datetime
import importlib
import json

import numpy as np
import pytest

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
    async def fake_load_raw_data(symbol, end_date=None, who=""):
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
    async def fake_load_raw_data(symbol, end_date=None, who=""):
        return _make_raw_data(symbol)

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ002463,SZ300502", days=1)

    assert response.symbols_count == 2
    assert set(response.reports) == {"SZ002463", "SZ300502"}
    assert response.errors == {}


@pytest.mark.asyncio
async def test_tech_missing_indicator_values_are_null(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who=""):
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
    async def fake_load_raw_data(symbol, end_date=None, who=""):
        return {}

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZXXXXXX", days=1)

    assert response.reports == {}
    assert "SZXXXXXX" in response.errors


@pytest.mark.asyncio
async def test_tech_output_does_not_contain_markdown_table(monkeypatch):
    async def fake_load_raw_data(symbol, end_date=None, who=""):
        return _make_raw_data("SZ002463")

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports("SZ002463", days=2)
    payload = _dump_json(response)

    assert "| --- |" not in payload
    assert "技术指标" not in payload


@pytest.mark.asyncio
async def test_tech_passes_query_date_to_loader(monkeypatch):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who=""):
        seen["end_date"] = end_date
        return _make_raw_data("SZ002463")

    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_technical_reports(
        "SZ002463",
        days=1,
        date="2026-06-05",
    )

    assert response.errors == {}
    assert seen["end_date"] == "2026-06-05"


@pytest.mark.asyncio
async def test_markdown_batch_passes_query_date_to_loader(monkeypatch):
    seen = {}

    async def fake_load_raw_data(symbol, end_date=None, who=""):
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
