import importlib

import pandas as pd
import pytest

from finmcp import cache
from finmcp.datasource import public_events
from finmcp.datasource.public_events import (
    PublicEventPoolResponse,
    fetch_public_market_events_sync,
    get_public_market_events,
    normalize_query_date,
    parse_public_event_sources,
)

events_module = importlib.import_module("finmcp.datasource.public_events")


class FakeAk:
    def stock_zt_pool_em(self, date):
        return pd.DataFrame([
            {
                "代码": "300001",
                "名称": "测试涨停",
                "涨跌幅": 20.0,
                "流通市值": 1_000,
                "总市值": 2_000,
                "换手率": 8.0,
                "封板资金": 100,
                "首次封板时间": "093000",
                "最后封板时间": "145500",
                "炸板次数": 1,
                "连板数": 2,
                "所属行业": "测试行业",
            }
        ])

    def stock_zt_pool_strong_em(self, date):
        return pd.DataFrame()

    def stock_zt_pool_previous_em(self, date):
        return pd.DataFrame()

    def stock_zt_pool_zbgc_em(self, date):
        return pd.DataFrame()

    def stock_lhb_detail_em(self, start_date, end_date):
        return pd.DataFrame([
            {
                "代码": "600001",
                "名称": "测试龙虎榜",
                "上榜日": pd.Timestamp("2026-08-20"),
                "解读": "机构买入",
                "涨跌幅": 9.9,
                "龙虎榜净买额": 20,
                "龙虎榜买入额": 80,
                "龙虎榜卖出额": 60,
                "龙虎榜成交额": 140,
                "市场总成交额": 1_000,
                "换手率": 10,
                "上榜原因": "涨幅偏离",
                "上榜后5日": 99,
            },
            {
                "代码": "600001",
                "名称": "测试龙虎榜",
                "上榜日": pd.Timestamp("2026-08-20"),
                "龙虎榜成交额": 100,
                "上榜后5日": -99,
            },
            {"代码": "123001", "名称": "测试转债", "龙虎榜成交额": 500},
        ])

    def stock_notice_report(self, symbol, date):
        return pd.DataFrame([
            {
                "代码": "002001",
                "名称": "测试公告",
                "公告标题": "关于重大合同的公告",
                "公告类型": "重大事项",
                "公告日期": pd.Timestamp("2026-08-20"),
                "网址": "https://example.test/notice",
            }
        ])

    def stock_yjyg_em(self, date):
        return pd.DataFrame([
            {
                "股票代码": "002001",
                "股票简称": "测试公告",
                "公告日期": pd.Timestamp("2026-08-20"),
                "预测指标": "归属于上市公司股东的净利润",
                "业绩变动": "预计盈利，同比增长10%至20%",
                "预测数值": 100_000_000,
                "业绩变动幅度": 15,
                "业绩变动原因": "订单增长",
                "预告类型": "略增",
                "上年同期值": 87_000_000,
            },
            {
                "股票代码": "600002",
                "股票简称": "窗口外",
                "公告日期": pd.Timestamp("2026-08-10"),
                "预测指标": "归属于上市公司股东的净利润",
                "预告类型": "预增",
            },
        ])


def test_parses_sources_and_date():
    assert parse_public_event_sources("lhb,limit_up,lhb") == ["lhb", "limit_up"]
    assert normalize_query_date("20260820") == ("2026-08-20", "20260820")
    with pytest.raises(ValueError, match="不支持"):
        parse_public_event_sources("future_returns")


def test_normalizes_structured_events_and_excludes_future_fields():
    result = fetch_public_market_events_sync(
        "2026-08-20",
        "20260820",
        ["lhb", "limit_up", "announcements"],
        1,
        [],
        200,
        FakeAk(),
    )

    assert result.as_of_safe is True
    assert [status.status for status in result.source_statuses] == ["SUCCESS"] * 3
    assert {event.symbol for event in result.events} == {"SH600001", "SZ300001", "SZ002001"}
    lhb = next(event for event in result.events if event.source == "lhb")
    assert lhb.lhb_turnover_amount == 140
    assert lhb.net_buy_to_market_pct == 2
    assert "上榜后" not in result.model_dump_json()


def test_filters_keywords_and_caps_rows_with_warning():
    result = fetch_public_market_events_sync(
        "2026-08-20",
        "20260820",
        ["announcements", "strong"],
        2,
        ["重大合同"],
        1,
        FakeAk(),
    )

    assert [event.symbol for event in result.events] == ["SZ002001"]
    assert result.source_statuses[0].raw_row_count == 2
    assert result.source_statuses[0].matched_row_count == 2
    assert result.source_statuses[0].returned_row_count == 1
    assert any("仅返回前 1 条" in warning for warning in result.warnings)
    assert any("strong" in warning and "空结果" in warning for warning in result.warnings)


def test_filters_normalized_symbols_before_response_cap():
    result = fetch_public_market_events_sync(
        "2026-08-20",
        "20260820",
        ["lhb", "limit_up", "announcements"],
        1,
        [],
        1,
        FakeAk(),
        {"SZ002001"},
    )

    assert [event.symbol for event in result.events] == ["SZ002001"]
    assert [status.returned_row_count for status in result.source_statuses] == [0, 0, 1]


def test_normalizes_point_in_time_earnings_forecast():
    result = fetch_public_market_events_sync(
        "2026-08-20",
        "20260820",
        ["earnings_forecast"],
        3,
        [],
        200,
        FakeAk(),
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.symbol == "SZ002001"
    assert event.event_date == "2026-08-20"
    assert event.report_period == "2026-06-30"
    assert event.forecast_type == "略增"
    assert event.forecast_change_pct == 15
    assert event.title == "业绩预告：略增；归属于上市公司股东的净利润，变动幅度中值15.00%"
    assert result.revision_safe is False
    assert any("自有归档" in warning for warning in result.warnings)


def test_nan_text_is_normalized_to_null():
    frame = FakeAk.stock_yjyg_em(FakeAk(), "20260630")
    frame.loc[0, "业绩变动原因"] = float("nan")

    class NanAk(FakeAk):
        def stock_yjyg_em(self, date):
            return frame

    result = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["earnings_forecast"], 3, [], 200, NanAk()
    )

    assert result.events[0].change_reason is None


@pytest.mark.asyncio
async def test_market_events_tool_delegates(monkeypatch):
    import importlib

    app_module = importlib.import_module("finmcp.mcp_app")

    expected = PublicEventPoolResponse(
        query_date="2026-08-20",
        fetched_at="2026-08-20 15:10:00",
        sources_requested=["lhb"],
        source_statuses=[],
        events=[],
        warnings=[],
    )
    calls = []

    async def fake_get_public_market_events(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(app_module, "get_public_market_events", fake_get_public_market_events)
    result = await app_module.market_events(
        date="2026-08-20",
        sources="lhb",
        announcement_lookback_days=2,
        keywords="订单",
        max_rows_per_source=50,
        symbols="SH600001",
    )

    assert result is expected
    assert calls == [{
        "date": "2026-08-20",
        "sources": "lhb",
        "announcement_lookback_days": 2,
        "keywords": "订单",
        "max_rows_per_source": 50,
        "symbols": "SH600001",
    }]


@pytest.mark.asyncio
async def test_public_event_requests_have_bounded_concurrency(monkeypatch):
    import asyncio
    import importlib
    import threading
    import time

    module = importlib.import_module("finmcp.datasource.public_events")
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_fetch(*args):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return PublicEventPoolResponse(
            query_date="2026-08-20",
            fetched_at="2026-08-20 15:10:00",
            sources_requested=["lhb"],
            source_statuses=[],
            events=[],
            warnings=[],
        )

    monkeypatch.setattr(module, "fetch_public_market_events_sync", fake_fetch)
    await asyncio.gather(*[
        get_public_market_events("2026-08-20", sources="lhb")
        for _ in range(10)
    ])
    assert max_active == 3


# --- max_rows_per_source 的默认值与上限 --------------------------------------


@pytest.mark.asyncio
async def test_row_cap_default_is_1000(monkeypatch):
    seen = {}

    def fake_sync(iso, compact, sources, lookback, keywords, cap, module, symbols):
        seen["cap"] = cap
        return events_module.PublicEventPoolResponse(
            query_date=iso,
            fetched_at="2026-09-03 00:00:00",
            revision_safe=True,
            sources_requested=list(sources),
            source_statuses=[],
            events=[],
            warnings=[],
        )

    monkeypatch.setattr(events_module, "fetch_public_market_events_sync", fake_sync)

    await events_module.get_public_market_events(date="2026-07-15", sources="lhb")

    assert seen["cap"] == 1000


@pytest.mark.asyncio
async def test_row_cap_accepts_the_value_that_used_to_fail(monkeypatch):
    """2026-09-03 有一次 max_rows_per_source=4000 因旧上限被拒。"""
    seen = {}

    def fake_sync(iso, compact, sources, lookback, keywords, cap, module, symbols):
        seen["cap"] = cap
        return events_module.PublicEventPoolResponse(
            query_date=iso,
            fetched_at="2026-09-03 00:00:00",
            revision_safe=False,
            sources_requested=list(sources),
            source_statuses=[],
            events=[],
            warnings=[],
        )

    monkeypatch.setattr(events_module, "fetch_public_market_events_sync", fake_sync)

    await events_module.get_public_market_events(
        date="2026-07-15", sources="earnings_forecast", max_rows_per_source=4000
    )

    assert seen["cap"] == 4000


@pytest.mark.asyncio
async def test_row_cap_still_bounded():
    """保留上限：单次响应已出现 1.8 MB，无界会威胁 500 MiB 峰值内存约束。"""
    with pytest.raises(ValueError, match="1-10000"):
        await events_module.get_public_market_events(
            date="2026-07-15", sources="lhb", max_rows_per_source=10001
        )
    with pytest.raises(ValueError, match="1-10000"):
        await events_module.get_public_market_events(
            date="2026-07-15", sources="lhb", max_rows_per_source=0
        )


# --- 缓存：按 (源, 日期) --------------------------------------------------------
#
# 不按整次请求缓：keywords / symbols / max_rows_per_source 都是本地过滤参数，
# 进 key 就是无界 key 空间。按 (源, 日期) 缓 key 空间有界，公告还能让不同 lookback
# 的查询共用条目。实测钉过去日期 7 个源 + 回看 3 天：14.88s → 0.76s。


def test_a_past_date_gets_an_immutable_epoch():
    """那天的池子永远不会再变，给恒定纪元，永不失效。"""
    assert public_events._epoch_for("lhb", "20260820") == "date-2026-08-20"


def test_today_follows_the_market_epoch():
    """当天的还在盘后陆续发布，不能钉死。"""
    import datetime

    today = datetime.date.today().strftime("%Y%m%d")
    assert public_events._epoch_for("lhb", today) is None


def test_earnings_forecast_never_gets_an_immutable_epoch():
    """它是报告期的当前快照，会被后续修订覆盖——revision_safe=False 就是这个意思。"""
    assert public_events._epoch_for("earnings_forecast", "20260630") is None


def test_each_source_and_day_is_one_upstream_call():
    """缓存的最小单位。公告那个循环本来就是按天的，逐天缓存于是天然共用条目。"""
    calls = []

    class FakeAk:
        def stock_notice_report(self, symbol, date):
            calls.append(date)
            return pd.DataFrame([{"代码": "600000", "名称": "浦发银行",
                                  "公告标题": "t", "公告日期": "2026-08-20"}])

    public_events._fetch_source(FakeAk(), "announcements", "20260820", 3, False)
    assert calls == ["20260820", "20260819", "20260818"]


def test_a_json_round_trip_does_not_change_the_normalised_records():
    """缓的是原始表，往返之后归一结果必须一字不差——否则缓存就改了返回内容。"""
    frame = pd.DataFrame([{
        "代码": "600000", "名称": "浦发银行", "上榜日": "2026-08-20",
        "解读": "买一", "收盘价": 10.5, "涨跌幅": 3.2, "龙虎榜净买额": 1.2e8,
    }])
    ns = cache.namespace(public_events.CACHE_NAMESPACE)
    restored = ns.decode(ns.encode(frame))
    before = public_events._normalize_source("lhb", frame, "2026-08-20")
    after = public_events._normalize_source("lhb", restored, "2026-08-20")
    assert [r.model_dump() for r in before] == [r.model_dump() for r in after]


def test_an_empty_frame_is_not_cached():
    """空结果可能只是这次没取到，缓住就把它固化成"那天没有事件"了。"""
    ns = cache.namespace(public_events.CACHE_NAMESPACE)
    assert ns.cacheable(pd.DataFrame(), None) is False
    assert ns.cacheable(pd.DataFrame([{"a": 1}]), None) is True


def test_injected_ak_modules_bypass_the_cache():
    """测试路径注入的是桩，别把桩数据腌进进程级缓存。"""
    calls = []

    class FakeAk:
        def stock_lhb_detail_em(self, start_date, end_date):
            calls.append(1)
            return pd.DataFrame([{"代码": "600000", "名称": "x", "上榜日": "2026-08-20"}])

    fake = FakeAk()
    for _ in range(3):
        public_events._fetch_source(fake, "lhb", "20260820", 1, False, use_cache=False)
    assert len(calls) == 3


# ── 带缓存那条路：取不到 ≠ 当天没有 ──────────────────────────────────
#
# 下面这组之前一条都没有：18 个测试全走 use_cache=False，而 use_cache=True 才是
# 生产路径。缺的正是它，所以"源崩了报 SUCCESS"这个 bug 一直没被测出来。


class _Boom:
    """上游炸给你看。"""

    def __init__(self, error=None):
        self.error = error or RuntimeError("上游 502")
        self.calls = 0

    def stock_lhb_detail_em(self, start_date, end_date):
        self.calls += 1
        raise self.error

    def stock_notice_report(self, symbol, date):
        self.calls += 1
        # 只有最早那天炸，其余正常——用来验证"少一天不能不吭声"
        if date == "20260818":
            raise self.error
        return pd.DataFrame([{"代码": "600000", "名称": "x", "公告标题": "t",
                              "公告日期": date}])


def _cached(monkeypatch):
    """强制走生产的带缓存路径。

    ``use_cache=use_direct_requests``，而注入了 ak_module 就意味着 use_direct_requests
    是 False，所以公开入口没法直接测到这条路。
    """
    real = public_events._fetch_source
    monkeypatch.setattr(public_events, "_fetch_source",
                        lambda *a, **kw: real(*a, **{**kw, "use_cache": True}))


def test_a_crashed_source_is_failed_not_an_empty_day(monkeypatch):
    """源崩了要报 FAILED，不能报"SUCCESS，0 条"。

    ``get_or_load`` 咽掉异常返回 None 是它对别的调用方的正当契约（回退是常态）。
    照单收下当成空表，响应就会说"SUCCESS / raw_row_count=0"再附一句"空结果不代表
    当日无事件"——源崩了却被描述成当天可能真没事件，调用方无从分辨。
    """
    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["lhb"], 1, [], 1000, ak_module=_Boom())
    status = response.source_statuses[0]
    assert status.status == "FAILED"
    assert "502" in (status.error or "")            # 原始异常没被吞成一句泛化的话
    assert not any("不代表当日无事件" in w for w in response.warnings)


def test_a_genuinely_empty_day_stays_successful(monkeypatch):
    """当天真没有事件仍然是 SUCCESS——别把上面那条修成"空表一律算失败"。

    能分开是因为本命名空间 cacheable 拒收空表：真空返回的是包着空表的 Entry，
    只有取不到才给 None。
    """
    class Quiet:
        def stock_lhb_detail_em(self, start_date, end_date):
            return pd.DataFrame()

    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["lhb"], 1, [], 1000, ak_module=Quiet())
    status = response.source_statuses[0]
    assert status.status == "SUCCESS" and status.raw_row_count == 0
    assert any("不代表当日无事件" in w for w in response.warnings)


def test_an_upstream_returning_none_is_also_a_failure(monkeypatch):
    """上游给 None 同样是没取到，不是当天没有。"""
    class Nothing:
        def stock_lhb_detail_em(self, start_date, end_date):
            return None

    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["lhb"], 1, [], 1000, ak_module=Nothing())
    assert response.source_statuses[0].status == "FAILED"


def test_serving_a_stale_pool_says_so(monkeypatch):
    """旧值兜底时数据是好的，但源此刻同样是崩的——按 get_or_load 的约定要标出来。

    不标的话响应是干净的 SUCCESS，看不出这份池子可能不含之后新增的事件。
    """
    monkeypatch.setattr(cache, "get_or_load", lambda *a, **kw: cache.Entry(
        value=pd.DataFrame([{"代码": "600000", "名称": "x", "上榜日": "2026-08-20"}]),
        fresh=False, age_seconds=930.0))
    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["lhb"], 1, [], 1000, ak_module=_Boom())
    status = response.source_statuses[0]
    assert status.status == "SUCCESS" and status.raw_row_count == 1   # 旧值照常返回
    assert any("缓存值" in w and "16 分钟" in w for w in response.warnings)


def test_a_fresh_hit_is_not_flagged_stale(monkeypatch):
    """fresh=True 不能也挂上旧值告警，否则告警天天有、等于没有。"""
    monkeypatch.setattr(cache, "get_or_load", lambda *a, **kw: cache.Entry(
        value=pd.DataFrame([{"代码": "600000", "名称": "x", "上榜日": "2026-08-20"}]),
        fresh=True, age_seconds=0.0))
    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["lhb"], 1, [], 1000, ak_module=_Boom())
    assert not any("缓存值" in w for w in response.warnings)


def test_one_source_failing_does_not_sink_the_others(monkeypatch):
    """一个源崩了，别的源照常返回——回退是常态。"""
    class Mixed(_Boom):
        def stock_zt_pool_em(self, date):
            return pd.DataFrame([{"代码": "300001", "名称": "涨停", "涨跌幅": 20.0}])

    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["lhb", "limit_up"], 1, [], 1000, ak_module=Mixed())
    by_source = {s.source: s.status for s in response.source_statuses}
    assert by_source == {"lhb": "FAILED", "limit_up": "SUCCESS"}
    assert [e.source for e in response.events] == ["limit_up"]


def test_a_partial_announcement_window_keeps_what_it_got(monkeypatch):
    """公告少取到一天，其余几天照常返回,并且**说出来**缺了哪天。

    两个极端都不对：静默丢掉那天就是悄悄给了个更小的池子；为一天失败判整源失败、
    一条不返回，是把已经拿到手的数据又扔掉——同样让数据变少（AGENTS §一）。
    """
    _cached(monkeypatch)
    boom = _Boom()          # 只有 20260818 那天炸，其余两天正常
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["announcements"], 3, [], 1000, ak_module=boom)
    status = response.source_statuses[0]
    assert status.status == "SUCCESS"
    assert status.raw_row_count == 2, "另外两天的公告被一起扔了"
    assert any("20260818" in w and "没取到" in w for w in response.warnings), \
        "少了一天却没吭声"


def test_an_announcement_window_that_fails_completely_is_a_failure(monkeypatch):
    """全部天都取不到就没有"部分"可言了，那就是整源失败。"""
    class AllBad(_Boom):
        def stock_notice_report(self, symbol, date):
            raise self.error

    _cached(monkeypatch)
    response = fetch_public_market_events_sync(
        "2026-08-20", "20260820", ["announcements"], 3, [], 1000, ak_module=AllBad())
    assert response.source_statuses[0].status == "FAILED"
