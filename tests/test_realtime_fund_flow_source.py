"""无页面标的（科创50 等）的盘中实时资金流：push2delay 分钟线 provider 与报告接线。

样例分钟线是 2026-09-07 10:16 从接口抄的：科创50 46 行，最后一行五个净流入，无净占比。
"""

from __future__ import annotations

import datetime
from io import StringIO

import numpy as np
import pytest

from finmcp import research
from finmcp.datasource import platform as pf
from finmcp.datasource import realtime_fund_flow_source as rt
from finmcp.datasource.fund_flow_source import FundFlowRequest
from finmcp.datasource.platforms.eastmoney import EastmoneyDelayPlatform

LAST = "2026-09-07 10:16,278061749.0,-444315993.0,166193970.0,-525206307.0,803268056.0"
PAYLOAD = {"rc": 0, "data": {"code": "000688", "name": "科创50", "klines": [
    "2026-09-07 09:31,95322128.0,-64627066.0,-31944784.0,-13920528.0,109242656.0", LAST]}}
REQUEST = FundFlowRequest(code="000688", symbol="SH000688", is_index=True)


def _serve(payload):
    return staticmethod(lambda secid: payload)


# --- provider -----------------------------------------------------------------


def test_the_last_minute_row_is_the_running_total(monkeypatch):
    monkeypatch.setattr(EastmoneyDelayPlatform, "_get_minutes", _serve(PAYLOAD))
    flow = EastmoneyDelayPlatform().fetch_realtime_fund_flow(REQUEST)
    assert flow.time == "2026-09-07 10:16"
    # 列序与日线前六位相同：主力、小单、中单、大单、超大单
    assert (flow.main_net, flow.s_net, flow.m_net, flow.l_net, flow.xl_net) == (
        278061749.0, -444315993.0, 166193970.0, -525206307.0, 803268056.0)
    assert flow.name == "科创50" and flow.source == "eastmoney_delay"
    assert [k for k, _ in flow.rows()] == ["主力", "超大单", "大单", "中单", "小单"]


@pytest.mark.parametrize("payload", [None, {}, {"data": None}, {"data": {"klines": []}},
                                     {"data": {"klines": ["2026-09-07 09:31,1,2"]}}])
def test_no_minute_rows_is_none(monkeypatch, payload):
    """盘外分钟线是空的；缺字段也当没有，不编数。"""
    monkeypatch.setattr(EastmoneyDelayPlatform, "_get_minutes", _serve(payload))
    assert EastmoneyDelayPlatform().fetch_realtime_fund_flow(REQUEST) is None


def test_the_delay_platform_declares_the_capability():
    assert {"fund_flow", "realtime_fund_flow"} <= pf.get("eastmoney_delay").capabilities
    assert rt.DEFAULT_PROVIDER_ORDER == ("eastmoney_delay",)


# --- resolve ------------------------------------------------------------------


def test_resolve_goes_through_the_registry(monkeypatch):
    monkeypatch.delenv("REALTIME_FUND_FLOW_PROVIDERS", raising=False)
    monkeypatch.setattr(EastmoneyDelayPlatform, "_get_minutes", _serve(PAYLOAD))
    flow = rt.resolve(REQUEST)
    assert flow is not None and flow.main_net == 278061749.0 and flow.source == "eastmoney_delay"


def test_the_tier_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("REALTIME_FUND_FLOW_PROVIDERS", "off")
    monkeypatch.setattr(EastmoneyDelayPlatform, "_get_minutes", _serve(PAYLOAD))
    assert rt.configured_order() == ()
    assert rt.resolve(REQUEST) is None


def test_a_repeat_within_the_window_does_not_hit_the_host_again(monkeypatch):
    """同一批指数 brief / medium / full 先后各渲染一次，只该问一次 push2delay。"""
    from finmcp import cache as cache_module

    calls = []

    def get_minutes(secid):
        calls.append(secid)
        return PAYLOAD

    monkeypatch.delenv("REALTIME_FUND_FLOW_PROVIDERS", raising=False)
    monkeypatch.setattr(EastmoneyDelayPlatform, "_get_minutes", staticmethod(get_minutes))
    cache = cache_module.cache_for(rt.CACHE_NAMESPACE)
    cache.clear()
    cache.enabled, cache.disk_enabled = True, False
    try:
        rt.resolve(REQUEST)
        rt.resolve(REQUEST)
        assert calls == ["1.000688"]
    finally:
        cache.clear()
        cache.enabled = False


# --- 报告接线 --------------------------------------------------------------------


def _raw_data(amount: float = 3.0535e10):
    return {
        "SYMBOL": "SH000688",
        "NAME": "科创50",
        "DATE": np.array([int(datetime.datetime(2026, 9, 7).timestamp() * 1e9)]),
        "OPEN": np.array([1596.58]), "HIGH": np.array([1600.24]), "LOW": np.array([1579.02]),
        "CLOSE": np.array([1594.63]), "VOLUME": np.array([2447800.0]), "AMOUNT": np.array([amount]),
    }


def _flow():
    return rt.RealtimeFundFlow(
        time="2026-09-07 10:16", main_net=278061749.0, xl_net=803268056.0, l_net=-525206307.0,
        m_net=166193970.0, s_net=-444315993.0, name="科创50", source="eastmoney_delay",
    )


@pytest.mark.asyncio
async def test_a_symbol_without_a_page_gets_its_five_lines_from_the_provider(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(research.realtime_fund_flow_source, "resolve", lambda request, **_: _flow())
    fp = StringIO()
    await research.build_trading_data(fp, "SH000688", _raw_data())
    text = fp.getvalue()
    assert "- 标的名称: 科创50" in text
    # 净占比 = 净流入 / 当日成交额（305.35 亿），与页面口径同一种写法
    assert "- 当日主力净流入: 2.78亿  主力净占比: 0.91%" in text
    assert "- 当日超大单净流入: 8.03亿  超大单净占比: 2.63%" in text
    assert "- 当日大单净流入: -5.25亿  大单净占比: -1.72%" in text
    assert "- 当日中单净流入: 1.66亿  中单净占比: 0.54%" in text
    assert "- 当日小单净流入: -4.44亿  小单净占比: -1.46%" in text
    assert "暂无实时资金流向" not in text


@pytest.mark.asyncio
async def test_without_turnover_the_ratio_is_left_blank(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(research.realtime_fund_flow_source, "resolve", lambda request, **_: _flow())
    fp = StringIO()
    await research.build_trading_data(fp, "SH000688", _raw_data(amount=0.0))
    assert "- 当日主力净流入: 2.78亿  主力净占比: --" in fp.getvalue()


@pytest.mark.asyncio
async def test_nothing_from_the_provider_keeps_the_old_wording(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(research.realtime_fund_flow_source, "resolve", lambda request, **_: None)
    fp = StringIO()
    await research.build_trading_data(fp, "SH000688", _raw_data())
    assert "- 暂无实时资金流向" in fp.getvalue()


@pytest.mark.asyncio
async def test_a_provider_error_is_not_fatal(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    def boom(request, **_):
        raise RuntimeError("push2delay down")

    monkeypatch.setattr(research.realtime_fund_flow_source, "resolve", boom)
    fp = StringIO()
    await research.build_trading_data(fp, "SH000688", _raw_data())
    assert "- 暂无实时资金流向" in fp.getvalue()


@pytest.mark.asyncio
async def test_an_api_today_row_still_wins(monkeypatch):
    """API 日线有当天行时输出逐字不变，provider 一次都不问。"""
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(research, "print_api_fund_flow_if_today",
                        lambda fp, data, today=None: print("- 当日主力净流入: 1.00亿  主力净占比: 1.00%", file=fp) or True)
    asked = []
    monkeypatch.setattr(research.realtime_fund_flow_source, "resolve",
                        lambda request, **_: asked.append(request) or _flow())
    fp = StringIO()
    await research.build_trading_data(fp, "SH000688", _raw_data())
    assert asked == []
    assert "- 当日主力净流入: 1.00亿" in fp.getvalue()


@pytest.mark.asyncio
async def test_symbols_with_a_page_are_untouched(monkeypatch):
    """有页面的标的仍走浏览器，不问 provider。"""
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    asked = []
    monkeypatch.setattr(research.realtime_fund_flow_source, "resolve",
                        lambda request, **_: asked.append(request) or _flow())

    async def fake_get_fund_flow(targets, **_):
        return '{"000001": {"标的名称": "上证指数", "主力净流入": "1亿", "主力净比(%)": 1}}'

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    data = _raw_data(); data["SYMBOL"] = "SH000001"; data["NAME"] = "上证指数"
    fp = StringIO()
    await research.build_trading_data(fp, "SH000001", data)
    assert asked == []
    assert "上证指数" in fp.getvalue()
