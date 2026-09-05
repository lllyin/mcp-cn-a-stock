"""K 线这一维在平台层上的接线。

注册表、逐级回退、四道闸门那些通用机制归 test_platform.py 管；这里只盯这一维
自己的东西：请求怎么归一成各平台认的写法、K 线的契约是什么、配置怎么接。
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from finmcp.datasource import kline_source, platform as pf
from finmcp.datasource.kline_frame import FALLBACK_FRAME_COLUMNS


def _request(symbol="SH600000", adjust="qfq"):
    return kline_source.KlineRequest(
        code=symbol[2:], start_date="2026-09-01", end_date="2026-09-02",
        adjust=adjust, symbol=symbol,
    )


# --- 请求：本项目的写法 → 各平台认的写法 -------------------------------------


@pytest.mark.parametrize("symbol,expected", [
    ("SH600519", "sh600519"),
    ("SZ399006", "sz399006"),
    ("BJ920021", "bj920021"),
])
def test_the_request_normalises_the_symbol(symbol, expected):
    assert _request(symbol).prefixed == expected


def test_the_fetch_window_is_widened_before_the_requested_start():
    """首行的前收盘价必须来自请求区间之前的交易日，否则涨跌幅只能填 0。

    kline_daily 只请求一天，首行就是唯一一行——不往前多取，那一天的涨跌幅、振幅、
    涨跌额三列全是 0。
    """
    assert _request().fetch_start == "20260812"
    assert _request().requested_start == datetime.date(2026, 9, 1)


# --- 契约：归一后必须是带标准列的日线表 --------------------------------------


def test_the_contract_requires_the_standard_columns():
    ok = pd.DataFrame([{c: 1 for c in FALLBACK_FRAME_COLUMNS}])
    assert kline_source._honours_kline_contract(ok) is True


def test_a_frame_missing_a_column_violates_the_contract():
    """列不对正是接新源最容易出的错，而且肉眼看不出来。"""
    missing = pd.DataFrame([{c: 1 for c in FALLBACK_FRAME_COLUMNS if c != "成交额"}])
    assert kline_source._honours_kline_contract(missing) is False


def test_a_non_frame_violates_the_contract():
    assert kline_source._honours_kline_contract({"日期": []}) is False
    assert kline_source._honours_kline_contract("不是表") is False


def test_the_contract_is_registered_with_the_platform_layer():
    assert pf.contract_of(kline_source.CAPABILITY) is kline_source._honours_kline_contract


def test_a_platform_returning_a_bad_frame_is_skipped_not_trusted(monkeypatch):
    """写坏的新平台应该降级到旧的，而不是把缺列的表灌进报告。"""
    class Broken(pf.Platform):
        name = label = "broken"
        capabilities = frozenset({kline_source.CAPABILITY})

        def fetch_kline(self, request):
            return pd.DataFrame([{"日期": datetime.date(2026, 9, 2), "收盘": 10.0}])

    good = pd.DataFrame([{c: 1 for c in FALLBACK_FRAME_COLUMNS}])

    class Good(pf.Platform):
        name = label = "good"
        capabilities = frozenset({kline_source.CAPABILITY})

        def fetch_kline(self, request):
            return good

    pf.register(Broken(), replace=True)
    pf.register(Good(), replace=True)
    try:
        status: dict = {}
        got = kline_source.resolve(_request(), order=("broken", "good"), status=status)
        assert got is not None and got.provider == "good"
        assert "broken_contract_violation" in status
    finally:
        pf.unregister("broken")
        pf.unregister("good")


# --- 配置接线 ---------------------------------------------------------------


def test_the_builtin_platforms_provide_this_capability():
    assert set(kline_source.registered()) >= {"tencent", "sina", "tonghuashun"}


def test_indices_and_the_rest_use_different_orders():
    """判据是"哪一家更贴近主源东财"，两边都是实测出来的。

    指数：创业板指成交量东财/同花顺一致，腾讯/新浪低 3.52%。
    个股：美的 120 日均价东财/腾讯一致，同花顺 -0.059%（前复权基准不同）。
    """
    assert kline_source.DEFAULT_PROVIDER_ORDER == ("tencent", "sina")
    assert kline_source.INDEX_PROVIDER_ORDER == ("tonghuashun", "tencent", "sina")
    assert kline_source.configured_order(_request("SZ399006"))[0] == "tonghuashun"
    assert kline_source.configured_order(_request("SH600519"))[0] == "tencent"


@pytest.mark.parametrize("symbol,expected", [
    ("SH000001", True), ("SZ399006", True), ("BJ899050", True),
    ("SH600519", False), ("SH512480", False), ("BJ920021", False),
])
def test_the_request_knows_whether_it_is_an_index(symbol, expected):
    assert _request(symbol).is_index is expected


def test_the_configured_order_is_read_from_the_env(monkeypatch):
    monkeypatch.setenv("KLINE_PROVIDERS", "tonghuashun,tencent")
    assert kline_source.configured_order() == ("tonghuashun", "tencent")
    monkeypatch.setenv("KLINE_PROVIDERS_INDEX", "tencent")
    assert kline_source.configured_order(_request("SZ399006")) == ("tencent",)


def test_the_whole_tier_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("KLINE_PROVIDERS", "off")
    assert kline_source.configured_order() == ()
    assert kline_source.resolve(_request()) is None


def test_provider_is_the_public_way_to_reach_one():
    assert kline_source.provider("tencent").label == "腾讯"
    assert kline_source.provider("没有这个源") is None
