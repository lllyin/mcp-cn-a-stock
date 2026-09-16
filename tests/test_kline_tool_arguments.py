import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError


app_module = importlib.import_module("finmcp.mcp_app")


@pytest.fixture(params=["kline_daily", "kline_range"])
def kline_tool(request):
    arguments = {"symbol": "SH512480"}
    if request.param == "kline_daily":
        arguments["date"] = "2026-09-11"
    else:
        arguments.update(start_date="2026-09-11", end_date="2026-09-16")
    return app_module.mcp_app._tool_manager.get_tool(request.param), arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied,expected", [
    ({}, "qfq"),
    ({"adjust": None}, "none"),
    ({"adjust": "none"}, "none"),
    ({"adjust": "qfq"}, "qfq"),
    ({"adjust": "hfq"}, "hfq"),
])
async def test_adjust_is_normalized_before_cache_and_fetch(monkeypatch, kline_tool, supplied, expected):
    tool, arguments = kline_tool
    fetch = AsyncMock(return_value=None)
    cache = Mock()
    cache.get.return_value = None
    build_key = Mock(wraps=app_module.build_key)
    monkeypatch.setattr(app_module, "get_datasource", lambda: SimpleNamespace(fetch_kline_simple=fetch))
    monkeypatch.setattr(app_module, "get_report_cache", lambda: cache)
    monkeypatch.setattr(app_module, "build_key", build_key)

    await tool.run({**arguments, **supplied})

    assert fetch.await_args.args[-1] == expected
    assert build_key.call_args.args[2]["adjust"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("adjust", ["invalid", "", 0, False, [], {}])
async def test_invalid_adjust_is_rejected_before_fetch(monkeypatch, kline_tool, adjust):
    tool, arguments = kline_tool
    datasource = Mock()
    monkeypatch.setattr(app_module, "get_datasource", datasource)

    with pytest.raises(ToolError, match="adjust"):
        await tool.run({**arguments, "adjust": adjust})

    datasource.assert_not_called()


def test_adjust_schema_preserves_default_and_declares_null(kline_tool):
    tool, _ = kline_tool
    schema = tool.parameters["properties"]["adjust"]
    assert schema["default"] == "qfq"
    assert {"type": "null"} in schema["anyOf"]
    assert next(branch["enum"] for branch in schema["anyOf"] if "enum" in branch) == ["qfq", "hfq", "none"]
