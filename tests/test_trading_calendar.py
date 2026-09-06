"""交易日历，以及它接进去的四个判断点。

这一维之前是四处各写各的 ``weekday() < 5``，其中一处连星期都不看。这组测试盯两件
事：日历本身答得对；以及"取不到日历时永远不比接入之前差"——那是接入的前提。
"""

from __future__ import annotations

import datetime

import pytest

from finmcp import research as _research
from finmcp.datasource import platform as pf
from finmcp.datasource import trading_calendar as tc

#: conftest 的 autouse fixture 会把 research.is_realtime_fund_flow_window 换成桩，
#: 这里在导入期先抓住真身，测它本身时放回去。
_REAL_WINDOW = _research.is_realtime_fund_flow_window

# 2026 年 9 月的真实交易日（周一到周五，无节假日），外加国庆和春节两段休市。
SEPT = frozenset(datetime.date(2026, 9, d) for d in
                 (1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18,
                  21, 22, 23, 24, 25, 28, 29, 30))
OCT = frozenset(datetime.date(2026, 10, d) for d in (8, 9, 12, 13, 14, 15, 16))
FAKE = tc.Calendar(days=SEPT | OCT, covers_through=datetime.date(2026, 12, 31),
                   source="test")


@pytest.fixture(autouse=True)
def clean_cache():
    tc.reset_cache()
    yield
    tc.reset_cache()


# --- 日历本身 ---------------------------------------------------------------


def test_a_weekday_holiday_is_not_a_trading_day():
    """国庆是工作日，但不开市——这正是 weekday() < 5 判错的那一类。"""
    assert tc.is_trading_day(datetime.date(2026, 10, 1), FAKE) is False
    assert tc.is_trading_day(datetime.date(2026, 10, 8), FAKE) is True


def test_a_weekend_is_not_a_trading_day():
    assert tc.is_trading_day(datetime.date(2026, 9, 5), FAKE) is False


def test_previous_trading_day_skips_the_whole_holiday():
    """10-08 的上一个交易日是 09-30，不是 10-07。"""
    assert tc.previous_trading_day(datetime.date(2026, 10, 8), FAKE) == datetime.date(2026, 9, 30)


def test_next_trading_day_skips_the_whole_holiday():
    assert tc.next_trading_day(datetime.date(2026, 9, 30), FAKE) == datetime.date(2026, 10, 8)


def test_trading_days_in_a_range_excludes_the_holiday():
    got = tc.trading_days(datetime.date(2026, 9, 28), datetime.date(2026, 10, 12), FAKE)
    assert got == [datetime.date(2026, 9, 28), datetime.date(2026, 9, 29),
                   datetime.date(2026, 9, 30), datetime.date(2026, 10, 8),
                   datetime.date(2026, 10, 9), datetime.date(2026, 10, 12)]


# --- 边界：不知道的时候必须说不知道 ------------------------------------------


def test_a_date_beyond_the_calendar_falls_back_to_weekday():
    """日历只到 2026-12-31，问 2027 年不能拿"不在名单里"当"不开市"。"""
    beyond = datetime.date(2027, 3, 1)          # 周一
    assert FAKE.knows(beyond) is False
    assert tc.is_trading_day(beyond, FAKE) is True                      # 周一
    assert tc.is_trading_day(datetime.date(2027, 3, 5), FAKE) is True   # 周五
    assert tc.is_trading_day(datetime.date(2027, 3, 6), FAKE) is False  # 周六


def test_no_calendar_at_all_falls_back_to_weekday(monkeypatch):
    """取不到日历时的行为必须和接入之前一模一样，否则这次接入就是负收益。"""
    monkeypatch.setattr(tc, "load", lambda: None)
    assert tc.is_trading_day(datetime.date(2026, 10, 1)) is True    # 周四，退回按星期
    assert tc.is_trading_day(datetime.date(2026, 9, 5)) is False    # 周六
    assert tc.previous_trading_day(datetime.date(2026, 9, 7)) == datetime.date(2026, 9, 4)


def test_the_loop_terminates_even_if_the_calendar_says_nothing_is_open():
    """日历异常时这两个循环也必须停，不能挂住调用方。"""
    empty = tc.Calendar(days=frozenset({datetime.date(1990, 1, 1)}),
                        covers_through=datetime.date(2099, 12, 31), source="broken")
    got = tc.previous_trading_day(datetime.date(2026, 9, 7), empty)
    assert isinstance(got, datetime.date)


# --- 降级是一个平台，不是 if/else --------------------------------------------


def test_the_weekday_fallback_is_a_registered_platform():
    assert "weekday" in pf.registered(tc.CAPABILITY)
    assert "sina" in pf.registered(tc.CAPABILITY)
    assert tc.DEFAULT_PROVIDER_ORDER == ("sina", "weekday")


def test_the_whole_layer_can_be_turned_off(monkeypatch):
    monkeypatch.setenv(tc.PROVIDER_ORDER_ENV, "off")
    tc.reset_cache()
    assert tc.load() is None
    # 关掉之后仍然要能回答，只是退回按星期
    assert tc.is_trading_day(datetime.date(2026, 10, 1)) is True


def test_a_failing_source_falls_through_to_the_weekday_platform(monkeypatch):
    class Broken(pf.Platform):
        name = label = "broken"
        capabilities = frozenset({tc.CAPABILITY})

        def fetch_trading_calendar(self, request):
            raise RuntimeError("上游挂了")

    pf.register(Broken(), replace=True)
    try:
        monkeypatch.setenv(tc.PROVIDER_ORDER_ENV, "broken,weekday")
        tc.reset_cache()
        calendar = tc.load()
        assert calendar is not None and calendar.source == "weekday"
    finally:
        pf.unregister("broken")


# --- 接进去的四个判断点 ------------------------------------------------------


def test_the_live_fund_flow_window_is_closed_on_a_non_trading_day(monkeypatch):
    """2026-09-05 周六 15:11 那一轮，服务为一个不存在的交易日抓了 66 次页面。"""
    from finmcp import research

    # conftest 有个 autouse fixture 把这个函数整个换成了返回 False 的桩（免得测试
    # 真去拉浏览器）。这一条测的就是它本身，得先把真身放回来。
    monkeypatch.setattr(research, "is_realtime_fund_flow_window",
                        research.is_realtime_fund_flow_window.__wrapped__
                        if hasattr(research.is_realtime_fund_flow_window, "__wrapped__")
                        else _REAL_WINDOW)
    monkeypatch.setattr(research.trading_calendar, "load", lambda: FAKE)
    saturday = datetime.datetime(2026, 9, 5, 15, 11)
    friday = datetime.datetime(2026, 9, 4, 15, 11)
    national_day = datetime.datetime(2026, 10, 1, 11, 0)
    assert research.is_realtime_fund_flow_window(saturday) is False
    assert research.is_realtime_fund_flow_window(national_day) is False
    assert research.is_realtime_fund_flow_window(friday) is True


def test_a_holiday_is_a_closed_epoch_anchored_on_the_last_trading_day(monkeypatch):
    """国庆整周原来被判成盘中，缓存退化成 30 秒 TTL。"""
    from finmcp import cache

    monkeypatch.setattr(tc, "load", lambda: FAKE)
    for day, hour in ((datetime.date(2026, 10, 1), 11), (datetime.date(2026, 10, 7), 14)):
        phase, epoch = cache.market_phase(
            datetime.datetime(day.year, day.month, day.day, hour, 0)
        )
        assert phase == cache.PHASE_CLOSED
        # 整个假期锚在同一个交易日上，纪元不再天天变，磁盘缓存也就不会天天作废
        assert epoch == "closed-2026-09-30"


def test_a_trading_day_still_reports_live(monkeypatch):
    from finmcp import cache

    monkeypatch.setattr(tc, "load", lambda: FAKE)
    phase, _ = cache.market_phase(datetime.datetime(2026, 9, 4, 11, 0))
    assert phase == cache.PHASE_LIVE


def test_market_breadth_rides_the_epoch_not_a_flat_ttl():
    """节假日整段是一个纪元，只打一次上游。

    原先是平 TTL：非交易日也 300 秒一刷，而那几天数据根本不动——一个周末的纪元
    长达 64 小时，按纪元走打 1 次，按平 TTL 打 768 次。
    """
    from finmcp import cache as cache_module
    from finmcp.datasource import market_breadth

    ns = cache_module.namespace(market_breadth.CACHE_NAMESPACE)
    assert ns.epoch_bound is True
    assert ns.ttl_seconds == 15.0     # 盘中才受它约束
    assert ns.disk is False           # 只有一条记录，不值得多一份反序列化风险
