"""
CN stock data source tests.
"""

import asyncio
import datetime
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

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted, *args):
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

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted, *args):
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

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted, *args):
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

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted, *args):
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

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted, *args):
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
        lambda code, start_date, end_date, adjust, symbol, *args: _sample_kline_frame(),
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
    frame = source_module._normalize_volume_to_lots(
        _tencent_frame(89817200.0), "600000"
    )

    assert frame["成交量"].iloc[0] == pytest.approx(898172.0)


def test_tencent_volume_already_in_lots_is_left_alone():
    """sz000333 实测：volume 148,596 约为 成交额/收盘价 的百分之一，已是手。"""
    frame = source_module._normalize_volume_to_lots(
        _tencent_frame(148596.0, amount=1302253100.0, close=87.25), "000333"
    )

    assert frame["成交量"].iloc[0] == pytest.approx(148596.0)


def test_tencent_volume_decision_survives_close_vwap_gap():
    """用收盘价代替 VWAP 的误差远小于 100 倍的判定间隔。"""
    for close_bias in (0.9, 1.1):
        frame = source_module._normalize_volume_to_lots(
            _tencent_frame(89817200.0, close=9.27 * close_bias), "600000"
        )
        assert frame["成交量"].iloc[0] == pytest.approx(898172.0)


def test_tencent_volume_ignores_rows_without_turnover():
    """停牌行的成交额为 0，不能参与判定。"""
    import pandas as pd

    frame = _tencent_frame(89817200.0, rows=4)
    frame.loc[0, ["成交量", "成交额"]] = 0.0
    frame.loc[1, "成交额"] = 0.0

    result = source_module._normalize_volume_to_lots(frame, "600000")

    assert result["成交量"].iloc[2] == pytest.approx(898172.0)
    assert result["成交量"].iloc[0] == pytest.approx(0.0)


def test_tencent_volume_untouched_when_nothing_traded():
    frame = _tencent_frame(0.0, amount=0.0)

    result = source_module._normalize_volume_to_lots(frame, "600000")

    assert result["成交量"].tolist() == [0.0, 0.0, 0.0]


def test_tencent_volume_logs_an_unexpected_magnitude(caplog):
    """量级既不像股也不像手时要留下痕迹，而不是静默猜一个。"""
    import logging

    caplog.set_level(logging.WARNING, logger="qtf_mcp")
    source_module._normalize_volume_to_lots(_tencent_frame(89817200.0 / 8), "600000")

    assert "成交量量级异常" in caplog.text


# --- 腾讯 fallback 的派生列 -------------------------------------------------


def _fake_tx_frame():
    """腾讯接口的原始列名，含请求区间之前的一行。"""
    import pandas as pd

    return pd.DataFrame(
        {
            "date": [
                datetime.date(2026, 8, 31),
                datetime.date(2026, 9, 1),
                datetime.date(2026, 9, 2),
            ],
            "open": [10.0, 10.2, 10.6],
            "close": [10.0, 10.5, 11.0],
            "high": [10.1, 10.6, 11.2],
            "low": [9.9, 10.1, 10.5],
            "volume": [1000.0, 1000.0, 1000.0],
            "amount": [1000000.0, 1050000.0, 1100000.0],
            "turnover": [0.01, 0.01, 0.01],
        }
    )


def _patch_tx(monkeypatch, frame=None, seen=None):
    import akshare

    def fake_tx(symbol, start_date, end_date, adjust="", **kwargs):
        if seen is not None:
            seen["start_date"] = start_date
            seen["end_date"] = end_date
        source = (_fake_tx_frame() if frame is None else frame).copy()
        # 与真实接口一致地按区间裁剪，否则测不出"加宽取数"这一步
        lower = datetime.datetime.strptime(start_date, "%Y%m%d").date()
        upper = datetime.datetime.strptime(end_date, "%Y%m%d").date()
        return source[(source["date"] >= lower) & (source["date"] <= upper)]

    monkeypatch.setattr(akshare, "stock_zh_a_hist_tx", fake_tx)


def test_tencent_fallback_widens_the_fetch_window(monkeypatch):
    """首行的前收盘价必须来自请求区间之前的交易日。"""
    seen = {}
    _patch_tx(monkeypatch, seen=seen)

    CNStockDataSource()._fetch_tencent_kline_sync(
        "600000", "2026-09-01", "2026-09-02", "qfq", "SH600000"
    )

    assert seen["start_date"] == "20260812"
    assert seen["end_date"] == "20260902"


def test_tencent_fallback_derives_first_row_from_the_prior_close(monkeypatch):
    _patch_tx(monkeypatch)

    frame = CNStockDataSource()._fetch_tencent_kline_sync(
        "600000", "2026-09-01", "2026-09-02", "qfq", "SH600000"
    )

    # 前置行被裁掉，但它的收盘价参与了首行的派生计算。
    assert frame["日期"].tolist() == [datetime.date(2026, 9, 1), datetime.date(2026, 9, 2)]
    assert frame["涨跌额"].iloc[0] == pytest.approx(0.5)
    assert frame["涨跌幅"].iloc[0] == pytest.approx(5.0)
    assert frame["振幅"].iloc[0] == pytest.approx(5.0)


def test_tencent_fallback_single_day_is_not_zeroed(monkeypatch):
    """kline_daily 只请求一天，修复前这三列恒为 0。"""
    _patch_tx(monkeypatch)

    frame = CNStockDataSource()._fetch_tencent_kline_sync(
        "600000", "2026-09-02", "2026-09-02", "qfq", "SH600000"
    )

    assert len(frame) == 1
    assert frame["涨跌额"].iloc[0] == pytest.approx(0.5)
    assert frame["涨跌幅"].iloc[0] == pytest.approx(4.7619, abs=1e-4)


def test_tencent_fallback_returns_none_when_window_has_no_rows(monkeypatch):
    _patch_tx(monkeypatch)

    assert (
        CNStockDataSource()._fetch_tencent_kline_sync(
            "600000", "2026-09-10", "2026-09-11", "qfq", "SH600000"
        )
        is None
    )


# --- 东财取数熔断 -----------------------------------------------------------


@pytest.fixture
def kline_breaker():
    breaker = source_module._KLINE_BREAKER
    breaker.reset()
    yield breaker
    breaker.reset()


def _failing_eastmoney(monkeypatch, calls):
    """让东财那一级必然失败，并记录被调用次数。"""

    def boom(*args, **kwargs):
        calls.append("eastmoney")
        raise RuntimeError("push2his refused")

    monkeypatch.setattr(source_module.ef.stock, "get_quote_history", boom)


def test_breaker_opens_after_threshold_and_skips_eastmoney(monkeypatch, kline_breaker):
    calls = []
    _failing_eastmoney(monkeypatch, calls)
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    datasource = CNStockDataSource()

    for _ in range(kline_breaker.threshold):
        assert datasource._fetch_kline_sync(
            "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
        ) is not None
    assert len(calls) == kline_breaker.threshold
    assert kline_breaker.is_open is True

    # 熔断后不再触碰东财，但仍然返回腾讯数据
    for _ in range(5):
        assert datasource._fetch_kline_sync(
            "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
        ) is not None
    assert len(calls) == kline_breaker.threshold


def test_breaker_stays_closed_below_threshold(monkeypatch, kline_breaker):
    calls = []
    _failing_eastmoney(monkeypatch, calls)
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    datasource = CNStockDataSource()

    for _ in range(kline_breaker.threshold - 1):
        datasource._fetch_kline_sync(
            "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
        )

    assert kline_breaker.is_open is False
    assert len(calls) == kline_breaker.threshold - 1


def test_a_success_clears_the_failure_streak(monkeypatch, kline_breaker):
    """零散失败不该累积成熔断——历史基线本来就有约 1.5% 的失败率。"""
    outcomes = ["fail"] * (kline_breaker.threshold - 1) + ["ok"] + ["fail"] * (
        kline_breaker.threshold - 1
    )

    def flaky(*args, **kwargs):
        if outcomes.pop(0) == "fail":
            raise RuntimeError("transient")
        return _sample_kline_frame()

    monkeypatch.setattr(source_module.ef.stock, "get_quote_history", flaky)
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    datasource = CNStockDataSource()

    while outcomes:
        datasource._fetch_kline_sync(
            "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
        )

    assert kline_breaker.is_open is False


def test_half_open_probe_closes_the_breaker_on_recovery(monkeypatch, kline_breaker):
    calls = []
    state = {"healthy": False}

    def provider(*args, **kwargs):
        calls.append("eastmoney")
        if not state["healthy"]:
            raise RuntimeError("push2his refused")
        return _sample_kline_frame()

    monkeypatch.setattr(source_module.ef.stock, "get_quote_history", provider)
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    datasource = CNStockDataSource()
    fetch = lambda: datasource._fetch_kline_sync(
        "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
    )

    for _ in range(kline_breaker.threshold):
        fetch()
    assert kline_breaker.is_open is True
    opened_calls = len(calls)

    # 冷却未到：所有请求都跳过东财
    fetch()
    assert len(calls) == opened_calls

    # 冷却到期：只放行一个探测请求
    kline_breaker._open_until = time.monotonic() - 0.001
    state["healthy"] = True
    fetch()
    assert len(calls) == opened_calls + 1
    assert kline_breaker.is_open is False

    # 已恢复：后续请求正常走东财
    fetch()
    assert len(calls) == opened_calls + 2


def test_failed_probe_buys_another_cooldown(monkeypatch, kline_breaker):
    calls = []
    _failing_eastmoney(monkeypatch, calls)
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    datasource = CNStockDataSource()
    fetch = lambda: datasource._fetch_kline_sync(
        "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
    )

    for _ in range(kline_breaker.threshold):
        fetch()
    kline_breaker._open_until = time.monotonic() - 0.001

    fetch()  # 探测，仍然失败
    assert len(calls) == kline_breaker.threshold + 1
    assert kline_breaker.is_open is True

    fetch()  # 新的冷却期内不再探测
    assert len(calls) == kline_breaker.threshold + 1


def test_breaker_can_be_disabled(monkeypatch, kline_breaker):
    calls = []
    _failing_eastmoney(monkeypatch, calls)
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    monkeypatch.setattr(source_module, "SOURCE_BREAKER_ENABLED", False)
    datasource = CNStockDataSource()

    for _ in range(kline_breaker.threshold + 3):
        datasource._fetch_kline_sync(
            "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", False
        )

    assert kline_breaker.is_open is False
    assert len(calls) == kline_breaker.threshold + 3


def test_skipped_fetch_returns_the_same_shape(monkeypatch, kline_breaker):
    """跳过东财后的返回结构必须和正常路径一致。"""
    monkeypatch.setattr(
        CNStockDataSource,
        "_fetch_tencent_kline_sync",
        lambda self, *a, **k: _sample_kline_frame(),
    )
    datasource = CNStockDataSource()
    kline_breaker._open_until = time.monotonic() + 60

    result = datasource._fetch_kline_sync(
        "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", True
    )

    assert set(result) == {"adjusted", "unadj", "adjust_type"}
    assert result["adjust_type"] == "qfq"
    assert not result["adjusted"].empty


def test_breaker_does_not_touch_fund_flow_or_finance(monkeypatch, kline_breaker):
    """熔断只覆盖 K 线；资金流和财务没有等价兜底，不能被跳过。"""
    kline_breaker._open_until = time.monotonic() + 60
    seen = []
    monkeypatch.setattr(
        source_module.CNStockDataSource,
        "_fetch_fund_flow_sync",
        lambda self, code, symbol=None: seen.append("fund_flow"),
    )
    monkeypatch.setattr(
        source_module.CNStockDataSource,
        "_fetch_finance_sync",
        lambda self, code, symbol=None: seen.append("finance"),
    )
    datasource = CNStockDataSource()

    datasource._fetch_fund_flow_sync("600000", "SH600000")
    datasource._fetch_finance_sync("600000", "SH600000")

    assert seen == ["fund_flow", "finance"]


# --- 新浪第三级兜底 ---------------------------------------------------------


def _sina_frame():
    """新浪 stock_zh_a_daily 的原始列名与口径：volume 为股，turnover 为小数。"""
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-08-31", periods=3).date,
            "open": [102.0, 102.5, 102.49],
            "high": [103.0, 103.2, 103.59],
            "low": [99.0, 99.5, 99.5],
            "close": [102.0, 102.4, 102.29],
            "volume": [2000000.0, 2500000.0, 2538601.0],
            "amount": [204000000.0, 256000000.0, 258183863.0],
            "outstanding_share": [139394050.0] * 3,
            "turnover": [0.0143, 0.0179, 0.0182],
        }
    )


def test_sina_fallback_serves_symbols_tencent_rejects(monkeypatch):
    """腾讯对多数北交所代码抛 KeyError，新浪能取到，缺了这一级就会返回"未找到"。"""
    import akshare

    monkeypatch.setattr(
        akshare, "stock_zh_a_hist_tx", lambda **k: (_ for _ in ()).throw(KeyError("day"))
    )
    monkeypatch.setattr(akshare, "stock_zh_a_daily", lambda **k: _sina_frame())

    status = {}
    frame = CNStockDataSource()._fetch_fallback_kline_sync(
        "920438", "2026-09-01", "2026-09-03", "qfq", "BJ920438", status
    )

    assert frame is not None and not frame.empty
    assert status.get("unsupported") is None
    assert list(frame.columns) == source_module.FALLBACK_FRAME_COLUMNS


def test_sina_volume_is_normalised_to_lots(monkeypatch):
    import akshare

    monkeypatch.setattr(
        akshare, "stock_zh_a_hist_tx", lambda **k: (_ for _ in ()).throw(KeyError("day"))
    )
    monkeypatch.setattr(akshare, "stock_zh_a_daily", lambda **k: _sina_frame())

    # 假数据的最后一行是 2026-09-02
    frame = CNStockDataSource()._fetch_fallback_kline_sync(
        "920438", "2026-09-02", "2026-09-02", "qfq", "BJ920438"
    )

    # 258,183,863 / 102.29 ≈ 2,524,038 股，说明原始列是股 → 应换成手
    assert frame["成交量"].iloc[-1] == pytest.approx(25386.01)
    assert frame["换手率"].iloc[-1] == pytest.approx(1.82, abs=0.01)


def test_unsupported_only_when_every_fallback_rejects(monkeypatch):
    import akshare

    monkeypatch.setattr(
        akshare, "stock_zh_a_hist_tx", lambda **k: (_ for _ in ()).throw(IndexError("oob"))
    )
    monkeypatch.setattr(
        akshare, "stock_zh_a_daily", lambda **k: (_ for _ in ()).throw(KeyError("date"))
    )

    status = {}
    assert CNStockDataSource()._fetch_fallback_kline_sync(
        "113707", "2026-09-01", "2026-09-03", "qfq", "SZ113707", status
    ) is None
    assert status["unsupported"] is True


def test_empty_window_is_not_reported_as_unsupported(monkeypatch):
    """两个源都能服务这个标的、只是区间内没有交易，不能说成"不支持"。"""
    import akshare

    empty = pd.DataFrame()
    monkeypatch.setattr(akshare, "stock_zh_a_hist_tx", lambda **k: empty)
    monkeypatch.setattr(akshare, "stock_zh_a_daily", lambda **k: empty)

    status = {}
    assert CNStockDataSource()._fetch_fallback_kline_sync(
        "600000", "2026-09-01", "2026-09-03", "qfq", "SH600000", status
    ) is None
    assert status.get("unsupported") is None


def test_simple_kline_reports_unsupported_distinctly(monkeypatch):
    import akshare

    monkeypatch.setattr(source_module.ef.stock, "get_quote_history", lambda *a, **k: None)
    monkeypatch.setattr(akshare, "stock_zh_a_hist", lambda **k: pd.DataFrame())
    monkeypatch.setattr(
        akshare, "stock_zh_a_hist_tx", lambda **k: (_ for _ in ()).throw(KeyError("day"))
    )
    monkeypatch.setattr(
        akshare, "stock_zh_a_daily", lambda **k: (_ for _ in ()).throw(KeyError("date"))
    )

    result = CNStockDataSource().fetch_kline_simple_sync(
        "BJ920438", "2026-09-01", "2026-09-03", "qfq"
    )

    assert result is not None
    assert result["unsupported"] is True
    assert result["data"] == []


def test_simple_kline_returns_none_for_a_quiet_window(monkeypatch):
    import akshare

    monkeypatch.setattr(source_module.ef.stock, "get_quote_history", lambda *a, **k: None)
    monkeypatch.setattr(akshare, "stock_zh_a_hist", lambda **k: pd.DataFrame())
    monkeypatch.setattr(akshare, "stock_zh_a_hist_tx", lambda **k: pd.DataFrame())
    monkeypatch.setattr(akshare, "stock_zh_a_daily", lambda **k: pd.DataFrame())

    assert CNStockDataSource().fetch_kline_simple_sync(
        "SH600000", "2026-09-01", "2026-09-03", "qfq"
    ) is None


# --- 资金流向页面兜底 --------------------------------------------------------


def _page_fallback_datasource(monkeypatch, fund_flow_result):
    """装好一个除资金流向外都成功的数据源。"""
    datasource = CNStockDataSource()

    def fake_kline(code, start_date, end_date, adjust, symbol, include_unadjusted, *args):
        frame = _sample_kline_frame()
        return {"adjusted": frame, "unadj": frame, "adjust_type": adjust}

    monkeypatch.setattr(datasource, "_fetch_kline_sync", fake_kline)
    monkeypatch.setattr(datasource, "_fetch_finance_sync", lambda code, symbol: None)
    monkeypatch.setattr(
        datasource,
        "_fetch_realtime_sync",
        lambda code, symbol: {"info": {"股票简称": "三环集团", "最新价": 110.91}},
    )
    monkeypatch.setattr(
        datasource, "_fetch_fund_flow_sync", lambda code, symbol: fund_flow_result
    )
    return datasource


def _captured_page():
    from pathlib import Path

    from qtf_mcp.datasource.fund_flow_page import parse_fund_flow_page

    fixture = Path(__file__).parent / "fixtures" / "eastmoney_zjlx_300408.html"
    return parse_fund_flow_page(fixture.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_page_fallback_is_not_used_while_the_api_works(monkeypatch):
    """主源正常时一次页面都不该加载——兜底不能变成常态开销。"""
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module

    datasource = _page_fallback_datasource(
        monkeypatch, {"fund_flow": pd.DataFrame(_captured_page().history_records())}
    )

    async def unexpected(symbol):
        raise AssertionError(f"主源可用时不应加载页面: {symbol}")

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", unexpected)

    result = await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert result.fetch_failures == []
    assert result.fund_flow_history is not None


@pytest.mark.asyncio
async def test_page_fallback_supplies_history_and_clears_the_failure(monkeypatch):
    """兜底成功后不能再留失败标记，否则报告整体不进缓存。"""
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module

    datasource = _page_fallback_datasource(
        monkeypatch, source_module._fetch_failure("fund_flow")
    )
    page = _captured_page()
    loaded = []

    async def fake_page(symbol):
        loaded.append(symbol)
        return page

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", fake_page)

    result = await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert loaded == ["SZ300408"]
    assert result.fetch_failures == []
    assert len(result.fund_flow_history["DATE"]) == 121
    # 今日数值取历史表最后一行，与主源同一条路径。
    assert result.fund_main_amount[-1] == pytest.approx(1.59e8)
    assert result.fund_main_ratio[-1] == pytest.approx(0.0337)
    assert result.is_market is False


@pytest.mark.asyncio
async def test_page_fallback_is_skipped_when_the_browser_tier_is_full(monkeypatch):
    """没有空位就直接放弃：排队会把"缺一段"换成"整体变慢"。"""
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module

    datasource = _page_fallback_datasource(
        monkeypatch, source_module._fetch_failure("fund_flow")
    )

    exhausted = asyncio.Semaphore(1)
    await exhausted.acquire()
    monkeypatch.setattr(source_module, "_get_fund_flow_page_slots", lambda: exhausted)
    monkeypatch.setattr(source_module, "FUND_FLOW_PAGE_FALLBACK_WAIT_SECONDS", 0.01)

    async def unexpected(symbol):
        raise AssertionError("没有空位时不应加载页面")

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", unexpected)

    result = await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert result.fetch_failures == ["fund_flow"]


@pytest.mark.asyncio
async def test_page_fallback_can_be_disabled(monkeypatch):
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module

    datasource = _page_fallback_datasource(
        monkeypatch, source_module._fetch_failure("fund_flow")
    )
    monkeypatch.setattr(source_module, "FUND_FLOW_PAGE_FALLBACK_ENABLED", False)

    async def unexpected(symbol):
        raise AssertionError("开关关闭时不应加载页面")

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", unexpected)

    result = await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert result.fetch_failures == ["fund_flow"]


@pytest.mark.asyncio
async def test_page_fallback_survives_a_page_error(monkeypatch):
    """页面加载失败要退回今天的行为，不能把异常抛给调用方。"""
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module

    datasource = _page_fallback_datasource(
        monkeypatch, source_module._fetch_failure("fund_flow")
    )

    async def failing(symbol):
        raise TimeoutError("Timeout 25000ms exceeded")

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", failing)

    result = await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert result.fetch_failures == ["fund_flow"]
    assert result.fund_flow_history is None


@pytest.mark.asyncio
async def test_page_fallback_releases_its_slot_after_a_failure(monkeypatch):
    """失败也要归还名额，否则一次超时就永久关掉了兜底。"""
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module

    datasource = _page_fallback_datasource(
        monkeypatch, source_module._fetch_failure("fund_flow")
    )
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(source_module, "_get_fund_flow_page_slots", lambda: slots)

    async def failing(symbol):
        raise TimeoutError("boom")

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", failing)

    await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert not slots.locked()


@pytest.mark.asyncio
async def test_page_fallback_ignores_an_empty_history(monkeypatch):
    from qtf_mcp.datasource import realtime_ff as realtime_ff_module
    from qtf_mcp.datasource.fund_flow_page import FundFlowPage

    datasource = _page_fallback_datasource(
        monkeypatch, source_module._fetch_failure("fund_flow")
    )

    async def empty_page(symbol):
        return FundFlowPage(name="三环集团", code="300408")

    monkeypatch.setattr(realtime_ff_module, "fetch_history_page", empty_page)

    result = await datasource.fetch_stock_data("SZ300408", "2024-01-01", "2026-09-03")

    assert result.fetch_failures == ["fund_flow"]
