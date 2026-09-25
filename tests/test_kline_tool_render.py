"""K 线工具的价格精度：ETF 按 0.001 元渲染，个股与指数保持两位小数。"""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


app_module = importlib.import_module("finmcp.mcp_app")

# SH512480 2026-09-11 的真实日线：上游给的就是三位小数。
ETF_BAR = {
    "日期": "2026-09-11", "开盘": 0.961, "收盘": 0.960, "最高": 0.965, "最低": 0.935,
    "成交量": 11281033, "成交额": 1073651800.0, "振幅": 3.07, "涨跌幅": -1.64,
    "涨跌额": -0.016, "换手率": 5.49,
}
STOCK_BAR = {
    "日期": "2026-09-11", "开盘": 56.12, "收盘": 55.8, "最高": 56.5, "最低": 55.31,
    "成交量": 312045, "成交额": 1745512300.0, "振幅": 2.11, "涨跌幅": -0.64,
    "涨跌额": -0.36, "换手率": 1.52,
}


def _install(monkeypatch, bar):
    fetch = AsyncMock(return_value={"data": [bar]})
    cache = Mock()
    cache.get.return_value = None
    monkeypatch.setattr(app_module, "get_datasource", lambda: SimpleNamespace(fetch_kline_simple=fetch))
    monkeypatch.setattr(app_module, "get_report_cache", lambda: cache)


async def _run(tool_name, arguments):
    return await app_module.mcp_app._tool_manager.get_tool(tool_name).run(arguments)


@pytest.mark.parametrize("symbol,decimals", [
    ("SH512480", 3), ("SZ159755", 3),
    ("SH600362", 2), ("SZ000001", 2),
    ("SH000001", 2), ("SZ399006", 2),
])
def test_price_decimals_follow_instrument_tick(symbol, decimals):
    assert app_module._kline_price_decimals(symbol) == decimals


@pytest.mark.asyncio
async def test_kline_daily_keeps_etf_third_decimal(monkeypatch):
    _install(monkeypatch, ETF_BAR)

    report = await _run("kline_daily", {"symbol": "SH512480", "date": "2026-09-11", "adjust": "none"})

    for line in ("- 开盘价: 0.961", "- 收盘价: 0.960", "- 最高价: 0.965",
                 "- 最低价: 0.935", "- 涨跌额: -0.016", "- 涨跌幅: -1.64%"):
        assert line in report


@pytest.mark.asyncio
async def test_kline_range_keeps_etf_third_decimal(monkeypatch):
    _install(monkeypatch, ETF_BAR)

    report = await _run("kline_range", {
        "symbol": "SH512480", "start_date": "2026-09-11", "end_date": "2026-09-11", "adjust": "none",
    })

    assert "| 2026-09-11 | 0.961 | 0.960 | 0.965 | 0.935 | 11,281,033 | -1.64% |" in report


@pytest.mark.asyncio
async def test_stock_output_unchanged(monkeypatch):
    _install(monkeypatch, STOCK_BAR)

    daily = await _run("kline_daily", {"symbol": "SH600362", "date": "2026-09-11"})
    ranged = await _run("kline_range", {
        "symbol": "SH600362", "start_date": "2026-09-11", "end_date": "2026-09-11",
    })

    for line in ("- 开盘价: 56.12", "- 收盘价: 55.80", "- 最高价: 56.50",
                 "- 最低价: 55.31", "- 涨跌额: -0.36"):
        assert line in daily
    assert "| 2026-09-11 | 56.12 | 55.80 | 56.50 | 55.31 | 312,045 | -0.64% |" in ranged
