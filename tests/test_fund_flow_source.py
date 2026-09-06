"""个股 / 指数资金流在平台层上的接线。

盯四件事：契约、请求怎么归一成东财认的写法、两级顺序（完整历史优先、不完整只留
行数多的）、push2delay 那一行和 AkShare 的表逐字相同。注册表和四道闸门归 test_platform.py。
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from finmcp import cache
from finmcp.datasource import cn_stock_source as source_module
from finmcp.datasource import fund_flow_source as ffs
from finmcp.datasource import platform as pf
from finmcp.datasource.platforms import eastmoney

COLUMNS = list(ffs.FUND_FLOW_COLUMNS)

# push2delay 2026-09-06 对 0.399006 的原样返回
DELAY_KLINE = (
    "2026-09-04,-7200599040.0,8721076224.0,-1520476160.0,-3684253696.0,-3516345344.0,"
    "-1.43,1.73,-0.30,-0.73,-0.70,3286.55,-0.78,0.00,0.00"
)


def _frame(rows: int = 3) -> pd.DataFrame:
    if rows == 0:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame([
        {**{c: float(i) for c in COLUMNS[1:]}, "日期": datetime.date(2026, 9, 1 + i)}
        for i in range(rows)
    ])[COLUMNS]


# --- 契约 -------------------------------------------------------------------


def test_the_contract_is_registered():
    assert pf.contract_of(ffs.CAPABILITY) is ffs._honours_contract


def test_a_full_table_honours_the_contract():
    assert ffs._honours_contract(ffs.FundFlowHistory(_frame())) is True


def test_a_missing_column_or_empty_table_violates_it():
    assert ffs._honours_contract(ffs.FundFlowHistory(_frame().drop(columns=["小单净流入-净占比"]))) is False
    assert ffs._honours_contract(ffs.FundFlowHistory(_frame(0))) is False
    assert ffs._honours_contract(_frame()) is False          # 裸表不行，要带 complete
    assert ffs._honours_contract({"日期": []}) is False


# --- 请求：本项目的写法 → 东财认的写法 -----------------------------------------


@pytest.mark.parametrize("code,symbol,is_index,exchange,secid", [
    ("600519", "SH600519", False, "sh", "1.600519"),
    ("000333", "SZ000333", False, "sz", "0.000333"),
    ("920021", "BJ920021", False, "sz", "0.920021"),    # AkShare 里 bj 和 sz 同为市场号 0
    ("000688", "SH000688", True, "sh", "1.000688"),
    ("399006", "SZ399006", True, "sz", "0.399006"),
    ("159326", None, False, "sz", "0.159326"),
])
def test_exchange_and_secid_follow_the_old_rule(code, symbol, is_index, exchange, secid):
    request = ffs.FundFlowRequest(code=code, symbol=symbol, is_index=is_index)
    assert (request.exchange, request.secid) == (exchange, secid)


# --- 顺序：完整历史优先，不完整只留行数多的 ---------------------------------


@pytest.fixture
def registry():
    saved = dict(pf._PLATFORMS)
    pf._PLATFORMS.clear()
    yield pf._PLATFORMS
    pf._PLATFORMS.clear()
    pf._PLATFORMS.update(saved)


class _Stub(pf.Platform):
    def __init__(self, name, value=None, raises=None):
        self.name = self.label = name
        self.capabilities = frozenset({ffs.CAPABILITY})
        self._value, self._raises, self.calls = value, raises, 0

    def fetch_fund_flow(self, request):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._value


REQUEST = ffs.FundFlowRequest(code="000688", symbol="SH000688", is_index=True)


def test_a_complete_history_stops_the_chain(registry):
    full = _Stub("a", ffs.FundFlowHistory(_frame(5), complete=True))
    later = _Stub("b", ffs.FundFlowHistory(_frame(1), complete=False))
    pf.register(full); pf.register(later)
    result = ffs.resolve(REQUEST, order=("a", "b"))
    assert result.provider == "a" and result.complete is True and len(result.frame) == 5
    assert later.calls == 0        # 新股只有 5 行也是"全部历史"，不该再问下一个


def test_a_failed_primary_falls_through_to_the_partial_backup(registry):
    pf.register(_Stub("a", raises=ConnectionError("push2his refused")))
    pf.register(_Stub("b", ffs.FundFlowHistory(_frame(1), complete=False)))
    result = ffs.resolve(REQUEST, order=("a", "b"))
    assert result.provider == "b" and result.complete is False and len(result.frame) == 1


def test_partial_results_keep_the_longer_one(registry):
    pf.register(_Stub("a", ffs.FundFlowHistory(_frame(1), complete=False)))
    pf.register(_Stub("b", ffs.FundFlowHistory(_frame(3), complete=False)))
    pf.register(_Stub("c", ffs.FundFlowHistory(_frame(2), complete=False)))
    result = ffs.resolve(REQUEST, order=("a", "b", "c"))
    assert len(result.frame) == 3 and result.complete is False
    assert result.provider == "a+b+c"   # 合成时记全，日志里看得出是拼的


def test_nothing_when_every_source_fails(registry):
    pf.register(_Stub("a", raises=ConnectionError("x")))
    pf.register(_Stub("b", None))
    assert ffs.resolve(REQUEST, order=("a", "b")) is None


def test_default_order_and_env_switch(monkeypatch):
    assert ffs.DEFAULT_PROVIDER_ORDER == ("eastmoney", "eastmoney_delay")
    monkeypatch.setenv(ffs.PROVIDER_ORDER_ENV, "eastmoney")
    assert ffs.configured_order() == ("eastmoney",)
    monkeypatch.setenv(ffs.PROVIDER_ORDER_ENV, "off")
    assert ffs.configured_order() == ()


# --- push2delay：和 AkShare 的表逐字相同 -----------------------------------------


def _akshare_reference(klines: list) -> pd.DataFrame:
    """``ak.stock_individual_fund_flow`` 拿到 klines 之后做的事，逐行照抄。"""
    temp_df = pd.DataFrame([item.split(",") for item in klines])
    temp_df.columns = [
        "日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额", "大单净流入-净额",
        "超大单净流入-净额", "主力净流入-净占比", "小单净流入-净占比", "中单净流入-净占比",
        "大单净流入-净占比", "超大单净流入-净占比", "收盘价", "涨跌幅", "-", "-",
    ]
    temp_df = temp_df[COLUMNS]
    temp_df["日期"] = pd.to_datetime(temp_df["日期"], errors="coerce").dt.date
    for column in COLUMNS[1:]:
        temp_df[column] = pd.to_numeric(temp_df[column], errors="coerce")
    return temp_df


def test_delay_frame_matches_akshare_step_for_step():
    ours = eastmoney._fund_flow_frame([DELAY_KLINE])
    pd.testing.assert_frame_equal(ours, _akshare_reference([DELAY_KLINE]), check_dtype=True, check_exact=True)
    assert ours["日期"].iloc[0] == datetime.date(2026, 9, 4)
    assert ours["主力净流入-净额"].iloc[0] == -7200599040.0
    assert ours["收盘价"].iloc[0] == 3286.55


def test_delay_platform_returns_a_partial_history(monkeypatch):
    platform = pf.get("eastmoney_delay")
    seen = {}

    def fake_get(secid):
        seen["secid"] = secid
        return {"data": {"code": "000688", "klines": [DELAY_KLINE]}}

    monkeypatch.setattr(type(platform), "_get", staticmethod(fake_get))
    history = platform.fetch_fund_flow(REQUEST)
    assert seen == {"secid": "1.000688"}
    assert history.complete is False and history.rows == 1
    assert ffs._honours_contract(history) is True


def test_delay_platform_gives_nothing_for_an_empty_payload(monkeypatch):
    platform = pf.get("eastmoney_delay")
    monkeypatch.setattr(type(platform), "_get", staticmethod(lambda secid: {"data": None}))
    assert platform.fetch_fund_flow(REQUEST) is None


def test_delay_platform_is_not_tied_to_the_impersonation_channel():
    """push2delay 不在伪装通道的接管名单里，通道冷却与它无关。"""
    assert pf.get("eastmoney_delay").degraded() is False
    assert "fund_flow" in pf.get("eastmoney").capabilities


def test_eastmoney_platform_calls_akshare_the_same_way_as_before(monkeypatch):
    import akshare as ak

    seen = {}

    def fake(stock, market):
        seen.update(stock=stock, market=market)
        return _frame(4)

    monkeypatch.setattr(ak, "stock_individual_fund_flow", fake)
    history = pf.get("eastmoney").fetch_fund_flow(REQUEST)
    assert seen == {"stock": "000688", "market": "sh"}
    assert history.complete is True and history.rows == 4


# --- 编排：什么时候还要去页面 -------------------------------------------------


def test_page_fallback_runs_for_failures_and_partials_only():
    needs = source_module._fund_flow_needs_page
    assert needs(source_module._fetch_failure("fund_flow")) is True
    assert needs({"fund_flow": _frame(1), "is_market": False, "complete": False}) is True
    assert needs({"fund_flow": _frame(5), "is_market": False, "complete": True}) is False
    assert needs({"fund_flow": _frame(5), "is_market": False}) is False   # 页面/老缓存给的，没这个键 = 全份
    assert needs(None) is False


def test_fetch_fund_flow_sync_reports_the_partial_backup(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", lambda **k: (_ for _ in ()).throw(ConnectionError("refused")))
    delay = pf.get("eastmoney_delay")
    monkeypatch.setattr(type(delay), "_get", staticmethod(lambda secid: {"data": {"klines": [DELAY_KLINE]}}))

    result = source_module.CNStockDataSource()._fetch_fund_flow_sync("000688", "SH000688")
    assert result["complete"] is False and result["is_market"] is False
    assert len(result["fund_flow"]) == 1
    assert source_module._fund_flow_needs_page(result) is True


def test_fetch_fund_flow_sync_marks_the_primary_complete(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", lambda **k: _frame(6))
    result = source_module.CNStockDataSource()._fetch_fund_flow_sync("600519", "SH600519")
    assert result["complete"] is True and len(result["fund_flow"]) == 6


def test_fetch_fund_flow_sync_fails_when_every_source_is_dry(monkeypatch):
    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", lambda **k: pd.DataFrame())
    delay = pf.get("eastmoney_delay")
    monkeypatch.setattr(type(delay), "_get", staticmethod(lambda secid: {"data": {"klines": []}}))
    result = source_module.CNStockDataSource()._fetch_fund_flow_sync("000688", "SH000688")
    assert source_module._is_fetch_failure(result)


# --- 缓存编解码要把 complete 带过去 -------------------------------------------


def test_cache_codec_round_trips_the_complete_flag():
    value = {"fund_flow": _frame(1), "is_market": False, "complete": False}
    decoded = cache._decode_fund_flow(cache._encode_fund_flow(value))
    assert decoded["complete"] is False and len(decoded["fund_flow"]) == 1
    # 老条目没有这个键：按全份读
    assert cache._decode_fund_flow({"rows": [], "is_market": False})["complete"] is True
