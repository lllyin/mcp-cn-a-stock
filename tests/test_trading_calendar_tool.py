"""交易日历工具的三态边界、区间模式与降级告警。

这个工具的灵魂在边界上：``is_trading_day`` 必须是三态，"日历没发布到"渲染成
"不开市"是这个领域最经典的错误。公共层的 ``Calendar.covers_from``（下边界）
就是为它补的——原先 ``knows()`` 只查上边界，一份只覆盖今年的名单会把去年
判成整年休市。
"""

import datetime as dt

import pytest

from finmcp.datasource import trading_calendar as tcl
from finmcp.mcp_app import build_trading_calendar_response


def _calendar(source="sina", through=dt.date(2026, 9, 30)):
    """2026-09 全月的周一到周五名单，来源可换。"""
    days, d = set(), dt.date(2026, 9, 1)
    while d <= dt.date(2026, 9, 30):
        if d.weekday() < 5:
            days.add(d)
        d += dt.timedelta(days=1)
    return tcl.Calendar(days=frozenset(days), covers_through=through, source=source)


class TestSingleMode:
    def test_a_covered_trading_day_is_true_with_neighbors(self):
        r = build_trading_calendar_response("2026-09-11", None, None, 2, 2, False, calendar=_calendar())
        assert r.is_trading_day is True and r.knows is True
        assert r.previous_trading_day == "2026-09-10" and r.next_trading_day == "2026-09-14"
        assert r.nearby_trading_days == {
            "back": ["2026-09-09", "2026-09-10"],
            "forward": ["2026-09-14", "2026-09-15"],
        }
        assert r.market_phase is None  # 查询日不是今天
        assert r.warnings == []

    def test_a_covered_weekend_is_false(self):
        r = build_trading_calendar_response("2026-09-12", None, None, 0, 0, False, calendar=_calendar())
        assert r.is_trading_day is False
        assert r.previous_trading_day == "2026-09-11" and r.next_trading_day == "2026-09-14"

    def test_beyond_coverage_is_null_not_false(self):
        """10 月的事日历还没发布：是"不知道"，绝不是"不开市"。"""
        r = build_trading_calendar_response("2026-10-05", None, None, 0, 0, False, calendar=_calendar())
        assert r.is_trading_day is None and r.knows is False
        assert r.next_trading_day is None
        assert any("未覆盖" in w for w in r.warnings)

    def test_before_the_list_starts_is_also_unknown(self):
        """下边界：去年的日期不能因为"不在名单里"被判成整年休市。"""
        r = build_trading_calendar_response("2026-08-10", None, None, 0, 0, False, calendar=_calendar())
        assert r.is_trading_day is None and r.knows is False
        assert r.calendar_coverage["from"] == "2026-09-01"

    def test_market_phase_only_attaches_for_today(self):
        today = dt.datetime(2026, 9, 11, 14, 0)
        r = build_trading_calendar_response("2026-09-11", None, None, 0, 0, True,
                                            now=today, calendar=_calendar())
        assert r.market_phase is not None and r.market_phase.phase in ("live", "lunch", "postclose", "closed")
        other = build_trading_calendar_response("2026-09-10", None, None, 0, 0, True,
                                                now=today, calendar=_calendar())
        assert other.market_phase is None

    def test_nearby_lists_never_cross_the_coverage_edge(self):
        """边界日附近取 nearby：猜出来的日子不能往外给。"""
        r = build_trading_calendar_response("2026-09-30", None, None, 0, 5, False, calendar=_calendar())
        assert r.nearby_trading_days["forward"] == []  # 10 月不在名单里，一个都不猜
        assert any("未覆盖" in w for w in r.warnings) is False or r.knows  # 09-30 本身仍已知


class TestRangeMode:
    @pytest.mark.parametrize("end,expected", [
        ("2026-08-31", ["2026-08-31"]),
        ("2026-09-02", ["2026-08-31", "2026-09-01", "2026-09-02"]),
    ])
    def test_range_before_coverage_uses_announced_weekday_fallback(self, end, expected):
        r = build_trading_calendar_response(
            None, "2026-08-29", end, 0, 0, False, calendar=_calendar())
        assert r.trading_days == expected
        assert r.trading_day_count == len(expected)
        assert r.knows is False
        assert any("按星期推断" in warning for warning in r.warnings)

    def test_lists_trading_days_with_counts(self):
        r = build_trading_calendar_response(None, "2026-09-07", "2026-09-11", 0, 0, False, calendar=_calendar())
        assert r.trading_days == ["2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
        assert r.trading_day_count == 5 and r.natural_days == 5

    def test_a_holiday_gap_shows_as_natural_days_not_trading_days(self):
        # 09-05/06 是周末：自然日 7，交易日 5
        r = build_trading_calendar_response(None, "2026-09-05", "2026-09-11", 0, 0, False, calendar=_calendar())
        assert r.trading_day_count == 5 and r.natural_days == 7

    def test_range_partially_out_of_coverage_warns(self):
        r = build_trading_calendar_response(None, "2026-09-25", "2026-10-09", 0, 0, False, calendar=_calendar())
        assert r.knows is False
        assert any("超出日历覆盖" in w for w in r.warnings)


class TestDegradedSources:
    def test_weekday_source_carries_a_warning(self):
        r = build_trading_calendar_response("2026-09-11", None, None, 0, 0, False, calendar=_calendar("weekday"))
        assert r.source == "weekday"
        assert any("降级" in w for w in r.warnings)

    def test_no_calendar_at_all_warns_and_falls_back_to_weekday(self, monkeypatch):
        # calendar=None 的语义是"交给公共层 load"——这里桩掉它模拟日历整层失败；
        # 真实缓存里往往有一份能用的名单，所以不能靠传 None 表达"没有"。
        # finmcp.mcp_app 这个包属性被同名 FastMCP 实例占着，模块得从 sys.modules 拿。
        import sys
        module = sys.modules["finmcp.mcp_app"]
        monkeypatch.setattr(module.trading_calendar_layer, "load", lambda **k: None)
        r = build_trading_calendar_response("2026-09-11", None, None, 0, 0, False, calendar=None)
        assert r.source == "weekday-fallback"
        assert r.is_trading_day is True  # 09-11 是周五，按星期推断不算错
        assert any("取不到" in w for w in r.warnings)


class TestValidation:
    @pytest.mark.parametrize(("d", "s", "e"), [
        ("2026-09-01", "2026-09-07", None),   # date 与区间同给
        (None, "2026-09-07", None),           # 区间只给一半
        (None, None, "2026-09-07"),
    ])
    def test_mutually_exclusive_params_are_rejected(self, d, s, e):
        with pytest.raises(ValueError):
            build_trading_calendar_response(d, s, e, 0, 0, False, calendar=_calendar())

    def test_a_bad_date_format_gets_a_readable_error(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            build_trading_calendar_response("2026-09-99", None, None, 0, 0, False, calendar=_calendar())

    def test_a_reversed_range_is_rejected(self):
        with pytest.raises(ValueError, match="晚于"):
            build_trading_calendar_response(None, "2026-09-11", "2026-09-07", 0, 0, False, calendar=_calendar())

    def test_back_forward_are_clamped(self):
        r = build_trading_calendar_response("2026-09-11", None, None, 500, -3, False, calendar=_calendar())
        assert len(r.nearby_trading_days["back"]) <= 30


class TestCalendarContract:
    def test_covers_from_is_the_list_start(self):
        assert _calendar().covers_from == dt.date(2026, 9, 1)

    def test_covers_from_is_none_for_an_empty_list(self):
        assert tcl.Calendar(days=frozenset(), covers_through=dt.date(2026, 1, 1)).covers_from is None
