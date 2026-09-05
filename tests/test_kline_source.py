"""K 线取数层的插拔契约。

这一组测试盯的不是某个源取得对不对，而是**架构承诺兑现没有**（AGENTS.md §二）：
接一个新源只用写一个类加一行注册，不改调用链、不改别的 provider、不加 if/else。
"接新源" 这件事将来会真的发生（同花顺、雪球），所以它得有测试守着。
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from qtf_mcp.datasource import kline_source


@pytest.fixture(autouse=True)
def clean_registry():
    """每个用例跑在自己的注册表上，互不影响。"""
    saved = dict(kline_source._PROVIDERS)
    yield
    kline_source._PROVIDERS.clear()
    kline_source._PROVIDERS.update(saved)


def _request(symbol="SH600000", adjust="qfq"):
    return kline_source.KlineRequest(
        code=symbol[2:], start_date="2026-09-01", end_date="2026-09-02",
        adjust=adjust, symbol=symbol,
    )


def _frame(close=10.0):
    return pd.DataFrame([{
        "日期": datetime.date(2026, 9, 2), "开盘": 9.9, "收盘": close,
        "最高": 10.1, "最低": 9.8, "成交量": 100.0, "成交额": 1000.0,
        "振幅": 3.0, "涨跌幅": 1.0, "涨跌额": 0.1, "换手率": 0.5,
    }])


class _Stub(kline_source.KlineProvider):
    def __init__(self, name, frame=None, *, raises=None, supports=True):
        self.name = name
        self.label = name
        self._frame = frame
        self._raises = raises
        self._supports = supports
        self.calls = 0

    def supports(self, request):
        return self._supports

    def fetch(self, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._frame


# --- 接新源不用改任何既有代码 -----------------------------------------------


def test_a_brand_new_source_only_needs_a_class_and_a_register(monkeypatch):
    """这就是"接同花顺/雪球"的全部动作：定义、注册、写进配置。"""
    kline_source._PROVIDERS.clear()
    kline_source.register(_Stub("tonghuashun", _frame(close=42.0)))
    monkeypatch.setenv("KLINE_PROVIDERS", "tonghuashun")

    result = kline_source.resolve(_request())

    assert result is not None
    assert result.provider == "tonghuashun"
    assert result.frame["收盘"].iloc[0] == 42.0


def test_the_configured_order_decides_who_wins(monkeypatch):
    kline_source._PROVIDERS.clear()
    kline_source.register(_Stub("a", _frame(close=1.0)))
    kline_source.register(_Stub("b", _frame(close=2.0)))

    monkeypatch.setenv("KLINE_PROVIDERS", "a,b")
    assert kline_source.resolve(_request()).provider == "a"
    monkeypatch.setenv("KLINE_PROVIDERS", "b,a")
    assert kline_source.resolve(_request()).provider == "b"


def test_the_whole_tier_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("KLINE_PROVIDERS", "off")
    assert kline_source.configured_order() == ()
    assert kline_source.resolve(_request()) is None


def test_an_unknown_name_is_ignored_not_fatal(monkeypatch):
    """配错一个名字不该让服务起不来，只该少一个源。"""
    kline_source._PROVIDERS.clear()
    kline_source.register(_Stub("a", _frame()))
    monkeypatch.setenv("KLINE_PROVIDERS", "nope,a")

    assert kline_source.configured_order() == ("a",)


# --- 逐级回退 ---------------------------------------------------------------


def test_an_empty_result_falls_through_not_just_a_failure(monkeypatch):
    """空结果和抛异常一样要往下走——"没给出结果"不等于"出错了"。"""
    kline_source._PROVIDERS.clear()
    empty = _Stub("a", pd.DataFrame())
    kline_source.register(empty)
    kline_source.register(_Stub("b", _frame(close=7.0)))
    monkeypatch.setenv("KLINE_PROVIDERS", "a,b")

    result = kline_source.resolve(_request())

    assert empty.calls == 1
    assert result.provider == "b" and result.frame["收盘"].iloc[0] == 7.0


def test_an_exception_does_not_stop_the_chain(monkeypatch):
    kline_source._PROVIDERS.clear()
    kline_source.register(_Stub("a", raises=RuntimeError("boom")))
    kline_source.register(_Stub("b", _frame()))
    monkeypatch.setenv("KLINE_PROVIDERS", "a,b")

    assert kline_source.resolve(_request()).provider == "b"


# --- supports()：不认的标的连请求都不发 --------------------------------------


def test_an_unsupported_symbol_costs_no_request(monkeypatch):
    """腾讯对北交所抛 KeyError，靠异常发现等于每次白付一个往返。"""
    kline_source._PROVIDERS.clear()
    picky = _Stub("picky", _frame(), supports=False)
    kline_source.register(picky)
    kline_source.register(_Stub("open", _frame(close=3.0)))
    monkeypatch.setenv("KLINE_PROVIDERS", "picky,open")

    status: dict = {}
    result = kline_source.resolve(_request(), status=status)

    assert picky.calls == 0
    assert status["picky_unsupported"] is True
    assert result.provider == "open"


def test_all_sources_rejecting_is_a_coverage_gap_not_a_quiet_failure(monkeypatch):
    """全都不认 → unsupported，工具会说"数据源不支持"而不是"未找到数据"。"""
    kline_source._PROVIDERS.clear()
    kline_source.register(_Stub("a", raises=KeyError("no such code")))
    kline_source.register(_Stub("b", supports=False))
    monkeypatch.setenv("KLINE_PROVIDERS", "a,b")

    status: dict = {}
    assert kline_source.resolve(_request(), status=status) is None
    assert status["unsupported"] is True


def test_a_plain_failure_is_not_a_coverage_gap(monkeypatch):
    """一次网络失败不该被说成"这个源不支持这个标的"。"""
    kline_source._PROVIDERS.clear()
    kline_source.register(_Stub("a", raises=RuntimeError("timeout")))
    monkeypatch.setenv("KLINE_PROVIDERS", "a")

    status: dict = {}
    assert kline_source.resolve(_request(), status=status) is None
    assert status.get("unsupported") is None


# --- 内置的两个源仍在表里 ----------------------------------------------------


def test_the_builtin_sources_are_registered_and_default_in_order():
    assert set(kline_source.registered()) >= {"tencent", "sina"}
    assert kline_source.DEFAULT_PROVIDER_ORDER == ("tencent", "sina")


def test_provider_is_the_public_way_to_reach_one():
    assert kline_source.provider("tencent").label == "腾讯"
    assert kline_source.provider("没有这个源") is None
