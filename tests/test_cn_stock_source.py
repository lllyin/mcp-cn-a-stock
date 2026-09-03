"""
CN stock data source tests.
"""

import asyncio
import threading
import time

import pandas as pd
import pytest

from qtf_mcp.datasource import cn_stock_source as source_module
from qtf_mcp.datasource.cn_stock_source import CNStockDataSource
from qtf_mcp.datasource.base import DataSource, FetchRequirements, StockData
from qtf_mcp import datafeed


def _sample_kline_frame():
    return pd.DataFrame(
        [
            {
                "日期": pd.Timestamp("2026-06-16").date(),
                "开盘": 10.0,
                "收盘": 10.2,
                "最高": 10.5,
                "最低": 9.8,
                "成交量": 1_000_000,
                "成交额": 10_200_000.0,
                "振幅": 7.0,
                "涨跌幅": 2.0,
                "涨跌额": 0.2,
                "换手率": 1.0,
            }
        ]
    )


@pytest.mark.asyncio
async def test_executor_in_flight_limit_bounds_submitted_work(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def blocking_call():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1

    slots = asyncio.Semaphore(2)
    monkeypatch.setattr(source_module, "_get_data_fetch_slots", lambda: slots)

    await asyncio.gather(*(source_module._run_in_executor(blocking_call) for _ in range(8)))

    assert peak == 2


@pytest.mark.asyncio
async def test_executor_cancellation_holds_slot_until_thread_finishes(monkeypatch):
    slots = asyncio.Semaphore(1)
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def first_call():
        first_started.set()
        release_first.wait(timeout=1)

    def second_call():
        second_started.set()

    monkeypatch.setattr(source_module, "_get_data_fetch_slots", lambda: slots)

    first = asyncio.create_task(source_module._run_in_executor(first_call))
    while not first_started.is_set():
        await asyncio.sleep(0.001)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(source_module._run_in_executor(second_call))
    await asyncio.sleep(0.02)
    assert not second_started.is_set()

    release_first.set()
    await second
    assert second_started.is_set()


def test_executor_limiter_can_be_reused_across_event_loops(monkeypatch):
    monkeypatch.setattr(source_module, "DATA_FETCH_MAX_IN_FLIGHT", 1)

    def blocking_call():
        time.sleep(0.005)

    async def run_wave():
        await asyncio.gather(
            source_module._run_in_executor(blocking_call),
            source_module._run_in_executor(blocking_call),
        )

    asyncio.run(run_wave())
    asyncio.run(run_wave())


@pytest.mark.asyncio
async def test_technical_requirements_skip_unused_sources(monkeypatch):
    datasource = CNStockDataSource()
    calls = []

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted):
        calls.append(("kline", include_unadjusted))
        frame = _sample_kline_frame()
        return {"adjusted": frame, "unadj": frame, "adjust_type": adjust}

    def fake_realtime(code, symbol):
        calls.append(("realtime", symbol))
        return {"info": {"股票简称": "测试股票", "最新价": 10.2}}

    def unexpected(*args, **kwargs):
        raise AssertionError("unused data source should not be called")

    monkeypatch.setattr(datasource, "_fetch_kline_sync", fake_kline)
    monkeypatch.setattr(datasource, "_fetch_realtime_sync", fake_realtime)
    monkeypatch.setattr(datasource, "_fetch_finance_sync", unexpected)
    monkeypatch.setattr(datasource, "_fetch_fund_flow_sync", unexpected)

    result = await datasource.fetch_stock_data_with_requirements(
        "SH600000",
        "2024-01-01",
        "2026-06-17",
        requirements=FetchRequirements.technical(),
    )

    assert sorted(name for name, _ in calls) == ["kline", "realtime"]
    assert ("kline", False) in calls
    assert result.name == "测试股票"
    assert result.close.tolist() == [10.2]


@pytest.mark.asyncio
async def test_requirements_fall_back_for_legacy_datasource(monkeypatch):
    calls = []

    class LegacyDataSource(DataSource):
        @property
        def name(self):
            return "legacy"

        async def fetch_stock_data(self, symbol, start_date, end_date):
            calls.append((symbol, start_date, end_date))
            return StockData(symbol=symbol)

        async def fetch_stock_list(self):
            return []

    monkeypatch.setattr(datafeed, "get_datasource", lambda: LegacyDataSource())

    result = await datafeed.load_data_msd(
        "SH600000",
        "2026-01-01",
        "2026-01-02",
        requirements=FetchRequirements.technical(),
    )

    assert result == {}
    assert calls == [("SH600000", "2026-01-01", "2026-01-02")]


@pytest.mark.asyncio
async def test_default_requirements_keep_complete_fetch_plan(monkeypatch):
    datasource = CNStockDataSource()
    calls = []

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted):
        calls.append(("kline", include_unadjusted))
        frame = _sample_kline_frame()
        return {"adjusted": frame, "unadj": frame, "adjust_type": adjust}

    def fake_finance(code, symbol):
        calls.append(("finance", symbol))
        return None

    def fake_fund_flow(code, symbol):
        calls.append(("fund_flow", symbol))
        return None

    def fake_realtime(code, symbol):
        calls.append(("realtime", symbol))
        return {"info": {"股票简称": "测试股票", "最新价": 10.2}}

    monkeypatch.setattr(datasource, "_fetch_kline_sync", fake_kline)
    monkeypatch.setattr(datasource, "_fetch_finance_sync", fake_finance)
    monkeypatch.setattr(datasource, "_fetch_fund_flow_sync", fake_fund_flow)
    monkeypatch.setattr(datasource, "_fetch_realtime_sync", fake_realtime)

    await datasource.fetch_stock_data("SH600000", "2024-01-01", "2026-06-17")

    assert sorted(name for name, _ in calls) == [
        "finance",
        "fund_flow",
        "kline",
        "realtime",
    ]
    assert ("kline", True) in calls


@pytest.mark.asyncio
async def test_source_failure_is_propagated_for_cache_safety(monkeypatch):
    datasource = CNStockDataSource()

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted):
        frame = _sample_kline_frame()
        return {"adjusted": frame, "unadj": frame, "adjust_type": adjust}

    monkeypatch.setattr(datasource, "_fetch_kline_sync", fake_kline)
    monkeypatch.setattr(
        datasource,
        "_fetch_finance_sync",
        lambda code, symbol: source_module._fetch_failure("finance"),
    )
    monkeypatch.setattr(datasource, "_fetch_fund_flow_sync", lambda code, symbol: None)
    monkeypatch.setattr(
        datasource,
        "_fetch_realtime_sync",
        lambda code, symbol: {"info": {"股票简称": "测试股票", "最新价": 10.2}},
    )

    result = await datasource.fetch_stock_data("SH600123", "2024-01-01", "2026-06-17")

    assert result.fetch_failures == ["finance"]
    assert result.to_dict()["_DS_FETCH_FAILURES"] == ["finance"]


@pytest.mark.asyncio
async def test_etf_unsupported_finance_is_not_a_fetch_failure(monkeypatch):
    datasource = CNStockDataSource()

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted):
        frame = _sample_kline_frame()
        return {"adjusted": frame, "unadj": frame, "adjust_type": adjust}

    monkeypatch.setattr(datasource, "_fetch_kline_sync", fake_kline)
    monkeypatch.setattr(datasource, "_fetch_fund_flow_sync", lambda code, symbol: None)
    monkeypatch.setattr(
        datasource,
        "_fetch_realtime_sync",
        lambda code, symbol: {"info": {"股票简称": "ETF", "最新价": 1.2}},
    )

    result = await datasource.fetch_stock_data("SZ159326", "2024-01-01", "2026-06-17")

    assert result.fetch_failures == []
    assert "_DS_FETCH_FAILURES" not in result.to_dict()


def test_simple_kline_skips_unadjusted_copy(monkeypatch):
    datasource = CNStockDataSource()
    seen = {}

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted):
        seen["include_unadjusted"] = include_unadjusted
        frame = _sample_kline_frame()
        return {"adjusted": frame, "unadj": frame, "adjust_type": adjust}

    monkeypatch.setattr(datasource, "_fetch_kline_sync", fake_kline)

    result = datasource.fetch_kline_simple_sync(
        "SH600000", "2026-06-16", "2026-06-16", "qfq"
    )

    assert seen["include_unadjusted"] is False
    assert result["data"][0]["收盘"] == 10.2


def test_simple_kline_uses_tencent_fallback_after_provider_failure(monkeypatch):
    datasource = CNStockDataSource()

    def provider_failure(*args, **kwargs):
        raise TypeError("unexpected impersonate")

    monkeypatch.setattr(source_module.ef.stock, "get_quote_history", provider_failure)
    monkeypatch.setattr(
        datasource,
        "_fetch_tencent_kline_sync",
        lambda code, start_date, end_date, adjust, symbol: _sample_kline_frame(),
    )

    result = datasource.fetch_kline_simple_sync(
        "SH600000", "2026-06-16", "2026-06-16", "qfq"
    )

    assert result["data"][0]["收盘"] == 10.2


def test_tencent_kline_fallback_normalizes_columns(monkeypatch):
    datasource = CNStockDataSource()
    import akshare as ak

    monkeypatch.setattr(
        ak,
        "stock_zh_a_hist_tx",
        lambda **kwargs: pd.DataFrame([
            {
                "date": "2026-06-15", "open": 10.0, "close": 10.0,
                "high": 10.2, "low": 9.8, "volume": 1000,
                "amount": 10000.0, "turnover": 0.01,
            },
            {
                "date": "2026-06-16", "open": 10.1, "close": 11.0,
                "high": 11.2, "low": 10.0, "volume": 2000,
                "amount": 21000.0, "turnover": 0.02,
            },
        ]),
    )

    frame = datasource._fetch_tencent_kline_sync(
        "600000", "2026-06-15", "2026-06-16", "qfq", "SH600000"
    )

    assert list(frame.columns) == [
        "日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额",
        "振幅", "涨跌幅", "涨跌额", "换手率",
    ]
    assert frame.iloc[1]["涨跌幅"] == pytest.approx(10.0)
    assert frame.iloc[1]["换手率"] == pytest.approx(2.0)


def test_etf_fund_flow_uses_stock_individual_fund_flow(monkeypatch):
    datasource = CNStockDataSource()
    seen = {}

    def fake_stock_individual_fund_flow(stock, market):
        seen["stock"] = stock
        seen["market"] = market
        return pd.DataFrame(
            [
                {
                    "日期": pd.Timestamp("2026-06-16").date(),
                    "收盘价": 2.137,
                    "涨跌幅": 3.59,
                    "主力净流入-净额": 186277584.0,
                    "主力净流入-净占比": 11.80,
                }
            ]
        )

    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", fake_stock_individual_fund_flow)

    result = datasource._fetch_fund_flow_sync("159326", "SZ159326")

    assert seen == {"stock": "159326", "market": "sz"}
    assert result is not None
    assert result["is_market"] is False
    assert len(result["fund_flow"]) == 1


def test_etf_finance_and_dividend_still_skipped():
    datasource = CNStockDataSource()

    assert datasource._fetch_finance_sync("159326", "SZ159326") is None
    assert datasource._fetch_dividend_sync("159326") is None


def test_core_index_fund_flow_uses_specific_index_flow(monkeypatch):
    datasource = CNStockDataSource()
    seen = {}

    def fake_stock_individual_fund_flow(stock, market):
        seen["stock"] = stock
        seen["market"] = market
        return pd.DataFrame(
            [
                {
                    "日期": pd.Timestamp("2026-06-16").date(),
                    "收盘价": 4102.94,
                    "涨跌幅": 1.72,
                    "主力净流入-净额": 3294404608.0,
                    "主力净流入-净占比": 0.39,
                    "超大单净流入-净额": 3213139968.0,
                    "超大单净流入-净占比": 0.38,
                    "大单净流入-净额": 81264640.0,
                    "大单净流入-净占比": 0.01,
                    "中单净流入-净额": -1892114432.0,
                    "中单净流入-净占比": -0.23,
                    "小单净流入-净额": -1402290176.0,
                    "小单净流入-净占比": -0.17,
                }
            ]
        )

    def fake_stock_market_fund_flow():
        raise AssertionError("stock_market_fund_flow should not be used for SZ399006")

    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", fake_stock_individual_fund_flow)
    monkeypatch.setattr(ak, "stock_market_fund_flow", fake_stock_market_fund_flow)

    result = datasource._fetch_fund_flow_sync("399006", "SZ399006")

    assert seen == {"stock": "399006", "market": "sz"}
    assert result is not None
    assert result["is_market"] is False

    history = datasource._build_fund_flow_history(
        result["fund_flow"],
        "SZ399006",
        result["is_market"],
    )
    assert history is not None
    assert history["CLOSE"][0] == 4102.94
    assert history["PCT_CHG"][0] == 0.0172
    assert history["A_A"][0] == 3294404608.0


def test_small_index_fund_flow_is_enabled(monkeypatch):
    datasource = CNStockDataSource()
    seen = {}

    def fake_stock_individual_fund_flow(stock, market):
        seen["stock"] = stock
        seen["market"] = market
        return pd.DataFrame(
            [
                {
                    "日期": pd.Timestamp("2026-06-16").date(),
                    "收盘价": 1730.99,
                    "涨跌幅": 3.82,
                    "主力净流入-净额": 2929126656.0,
                    "主力净流入-净占比": 4.76,
                }
            ]
        )

    import akshare as ak

    monkeypatch.setattr(ak, "stock_individual_fund_flow", fake_stock_individual_fund_flow)

    result = datasource._fetch_fund_flow_sync("000688", "SH000688")

    assert seen == {"stock": "000688", "market": "sh"}
    assert result is not None
    assert result["is_market"] is False

    history = datasource._build_fund_flow_history(
        result["fund_flow"],
        "SH000688",
        result["is_market"],
    )
    assert history is not None
    assert history["CLOSE"][0] == 1730.99
    assert history["PCT_CHG"][0] == 0.0382


@pytest.mark.asyncio
async def test_finance_cache_hit_bypasses_executor_and_returns_copy(monkeypatch):
    datasource = CNStockDataSource()
    calls = 0
    source_module._finance_cache.clear()

    async def fake_run_in_executor(func, *args):
        nonlocal calls
        calls += 1
        return {"finance": pd.DataFrame([{"报告期": "2025-12-31", "净利润": "1亿"}])}

    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    first = await datasource._fetch_finance_cached("600001", "SH600001")
    first["finance"].loc[0, "净利润"] = "已修改"
    second = await datasource._fetch_finance_cached("600001", "SH600001")

    assert calls == 1
    assert second["finance"].loc[0, "净利润"] == "1亿"
    assert first["finance"] is not second["finance"]


@pytest.mark.asyncio
async def test_finance_cold_cache_singleflight(monkeypatch):
    datasource = CNStockDataSource()
    calls = 0
    release = asyncio.Event()
    source_module._finance_cache.clear()

    async def fake_run_in_executor(func, *args):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"finance": pd.DataFrame([{"净利润": "1亿"}])}

    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    first = asyncio.create_task(datasource._fetch_finance_cached("600002", "SH600002"))
    second = asyncio.create_task(datasource._fetch_finance_cached("600002", "SH600002"))
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls == 1
    assert first_result["finance"].equals(second_result["finance"])
    assert first_result["finance"] is not second_result["finance"]


@pytest.mark.asyncio
async def test_finance_background_fetch_populates_cache_after_caller_cancellation(monkeypatch):
    datasource = CNStockDataSource()
    release = asyncio.Event()
    source_module._finance_cache.clear()

    async def fake_run_in_executor(func, *args):
        await release.wait()
        return {"finance": pd.DataFrame([{"净利润": "1亿"}])}

    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    caller = asyncio.create_task(
        datasource._fetch_finance_cached("600004", "SH600004")
    )
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert source_module._finance_cache["SH600004"][1]["finance"].empty is False


@pytest.mark.asyncio
async def test_finance_cache_separates_markets_for_same_code(monkeypatch):
    datasource = CNStockDataSource()
    calls = []
    source_module._finance_cache.clear()

    async def fake_run_in_executor(func, code, symbol):
        calls.append(symbol)
        return {"finance": pd.DataFrame([{"市场": symbol[:2]}])}

    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    sh_result = await datasource._fetch_finance_cached("000001", "SH000001")
    sz_result = await datasource._fetch_finance_cached("000001", "SZ000001")

    assert calls == ["SH000001", "SZ000001"]
    assert sh_result["finance"].loc[0, "市场"] == "SH"
    assert sz_result["finance"].loc[0, "市场"] == "SZ"


@pytest.mark.asyncio
async def test_finance_cache_expiry_refetches(monkeypatch):
    datasource = CNStockDataSource()
    calls = 0
    source_module._finance_cache.clear()
    monkeypatch.setattr(source_module, "FINANCE_CACHE_TTL_SECONDS", 21600)
    source_module._finance_cache["SH600001"] = (
        100.0,
        {"finance": pd.DataFrame([{"净利润": "旧值"}])},
    )

    async def fake_run_in_executor(func, *args):
        nonlocal calls
        calls += 1
        return {"finance": pd.DataFrame([{"净利润": "新值"}])}

    monkeypatch.setattr(source_module.time, "monotonic", lambda: 100.0 + 21601)
    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    result = await datasource._fetch_finance_cached("600001", "SH600001")

    assert calls == 1
    assert result["finance"].loc[0, "净利润"] == "新值"


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_result", [None, {"finance": pd.DataFrame()}])
async def test_finance_cache_does_not_store_failed_or_empty_results(monkeypatch, upstream_result):
    datasource = CNStockDataSource()
    source_module._finance_cache.clear()

    async def fake_run_in_executor(func, *args):
        return upstream_result

    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    await datasource._fetch_finance_cached("600001", "SH600001")

    assert "SH600001" not in source_module._finance_cache


@pytest.mark.asyncio
async def test_finance_cache_prunes_expired_and_oldest_entries(monkeypatch):
    datasource = CNStockDataSource()
    source_module._finance_cache.clear()
    monkeypatch.setattr(source_module, "FINANCE_CACHE_TTL_SECONDS", 21600)
    monkeypatch.setattr(source_module, "FINANCE_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(source_module.time, "monotonic", lambda: 30000.0)
    source_module._finance_cache.update(
        {
            "expired": (1.0, {"finance": pd.DataFrame([{"值": 1}])}),
            "older": (29000.0, {"finance": pd.DataFrame([{"值": 2}])}),
            "newer": (29500.0, {"finance": pd.DataFrame([{"值": 3}])}),
        }
    )

    async def fake_run_in_executor(func, *args):
        return {"finance": pd.DataFrame([{"值": 4}])}

    monkeypatch.setattr(source_module, "_run_in_executor", fake_run_in_executor)

    await datasource._fetch_finance_cached("fresh", "SH600003")

    assert set(source_module._finance_cache) == {"newer", "SH600003"}


# --- 腾讯 fallback 的成交量单位 ---------------------------------------------


def _tencent_frame(volume, amount=841972900.0, close=9.27, rows=3):
    """构造归一化前的腾讯行情帧；默认取 sh600000 2026-09-03 的实测数值。"""
    import pandas as pd

    return pd.DataFrame(
        {
            "日期": pd.date_range("2026-09-01", periods=rows).date,
            "开盘": [close] * rows,
            "收盘": [close] * rows,
            "最高": [close] * rows,
            "最低": [close] * rows,
            "成交量": [volume] * rows,
            "成交额": [amount] * rows,
        }
    )


def test_tencent_volume_in_shares_is_converted_to_lots():
    """sh600000 实测：volume 89,817,200 与 成交额/收盘价 同量级，即单位是股。"""
    frame = source_module._normalize_tencent_volume(
        _tencent_frame(89817200.0), "600000"
    )

    assert frame["成交量"].iloc[0] == pytest.approx(898172.0)


def test_tencent_volume_already_in_lots_is_left_alone():
    """sz000333 实测：volume 148,596 约为 成交额/收盘价 的百分之一，已是手。"""
    frame = source_module._normalize_tencent_volume(
        _tencent_frame(148596.0, amount=1302253100.0, close=87.25), "000333"
    )

    assert frame["成交量"].iloc[0] == pytest.approx(148596.0)


def test_tencent_volume_decision_survives_close_vwap_gap():
    """用收盘价代替 VWAP 的误差远小于 100 倍的判定间隔。"""
    for close_bias in (0.9, 1.1):
        frame = source_module._normalize_tencent_volume(
            _tencent_frame(89817200.0, close=9.27 * close_bias), "600000"
        )
        assert frame["成交量"].iloc[0] == pytest.approx(898172.0)


def test_tencent_volume_ignores_rows_without_turnover():
    """停牌行的成交额为 0，不能参与判定。"""
    import pandas as pd

    frame = _tencent_frame(89817200.0, rows=4)
    frame.loc[0, ["成交量", "成交额"]] = 0.0
    frame.loc[1, "成交额"] = 0.0

    result = source_module._normalize_tencent_volume(frame, "600000")

    assert result["成交量"].iloc[2] == pytest.approx(898172.0)
    assert result["成交量"].iloc[0] == pytest.approx(0.0)


def test_tencent_volume_untouched_when_nothing_traded():
    frame = _tencent_frame(0.0, amount=0.0)

    result = source_module._normalize_tencent_volume(frame, "600000")

    assert result["成交量"].tolist() == [0.0, 0.0, 0.0]


def test_tencent_volume_logs_an_unexpected_magnitude(caplog):
    """量级既不像股也不像手时要留下痕迹，而不是静默猜一个。"""
    import logging

    caplog.set_level(logging.WARNING, logger="qtf_mcp")
    source_module._normalize_tencent_volume(_tencent_frame(89817200.0 / 8), "600000")

    assert "成交量量级异常" in caplog.text
