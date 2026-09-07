"""holiday-cn 换算成交易日名单。

这个源最容易踩的错是把**调休上班日**当成交易日：``isOffDay=False`` 在劳动法上算
工作日，但交易所照旧不开。踩了的后果很隐蔽——报告里凭空多出几根 K 线，每一根的
数值都在合理区间，肉眼查不出来。所以下面第一条就钉它。

全部离线：真实的一致率（2024-01-02 .. 2026-12-31 共 728 天，对 sina 权威名单
727/728 = 99.863%，唯一差异是 2024-02-09 除夕交易所多休一天）写在
``platforms/holiday_cn.py`` 的模块文档里，不适合在单测里连 GitHub。
"""

from __future__ import annotations

import datetime
import json

import pytest

from finmcp.datasource import platform as pf


def _payload(year: int, days) -> str:
    return json.dumps({"year": year, "days": days})


class _Response:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _session(monkeypatch, by_year: dict, calls: list | None = None):
    import requests

    class Session:
        def get(self, url, timeout=None):
            year = url.rsplit("/", 1)[-1].split(".")[0]
            if calls is not None:
                calls.append(year)
            return by_year.get(year, _Response(404, ""))

    monkeypatch.setattr(requests, "Session", Session)


def _fixed_today(monkeypatch, day: datetime.date):
    """``fetch_trading_calendar`` 用 today() 决定取哪几年，钉住它测试才可重复。"""
    import finmcp.datasource.platforms.holiday_cn as mod

    class _Date(datetime.date):
        @classmethod
        def today(cls):
            return day

    monkeypatch.setattr(mod.datetime, "date", _Date)


def _fetch(through=datetime.date(2026, 12, 31)):
    from finmcp.datasource.trading_calendar import CalendarRequest

    return pf.get("holiday_cn").fetch_trading_calendar(CalendarRequest(through=through))


def test_a_make_up_workday_is_not_a_trading_day(monkeypatch):
    """2026-02-14 是周六调休上班日：劳动法算工作日，交易所不开，不能进名单。"""
    _fixed_today(monkeypatch, datetime.date(2026, 2, 20))
    _session(monkeypatch, {"2026": _Response(200, _payload(2026, [
        {"name": "春节", "date": "2026-02-16", "isOffDay": True},
        {"name": "春节", "date": "2026-02-17", "isOffDay": True},
        {"name": "春节", "date": "2026-02-14", "isOffDay": False},   # 调休上班
    ]))})
    calendar = _fetch()
    assert datetime.date(2026, 2, 14) not in calendar.days, "调休上班日不是交易日"
    assert datetime.date(2026, 2, 16) not in calendar.days, "放假日不是交易日"
    assert datetime.date(2026, 2, 18) in calendar.days, "假期之后照常开市"


def test_weekends_are_never_trading_days(monkeypatch):
    _fixed_today(monkeypatch, datetime.date(2026, 6, 1))
    _session(monkeypatch, {"2026": _Response(200, _payload(2026, [
        {"name": "元旦", "date": "2026-01-01", "isOffDay": True},
    ]))})
    calendar = _fetch()
    assert datetime.date(2026, 6, 6) not in calendar.days      # 周六
    assert datetime.date(2026, 6, 7) not in calendar.days      # 周日
    assert datetime.date(2026, 6, 5) in calendar.days          # 周五


def test_a_year_that_is_not_published_yet_is_skipped_not_fatal(monkeypatch):
    """明年的放假安排在国务院公告前不存在，404 不是失败。"""
    _fixed_today(monkeypatch, datetime.date(2026, 12, 1))
    calls: list = []
    _session(monkeypatch, {
        "2026": _Response(200, _payload(2026, [
            {"name": "元旦", "date": "2026-01-01", "isOffDay": True}])),
    }, calls)
    calendar = _fetch(through=datetime.date(2027, 3, 1))
    assert "2027" in calls, "该问一次明年"
    assert calendar is not None
    assert calendar.covers_through == datetime.date(2026, 12, 31), \
        "只声明真取到了的年份，报大了会让边界外的查询把「名单里没有」当成「不开市」"


def test_it_gives_up_when_no_year_could_be_fetched(monkeypatch):
    """一年都没取到就返回 None，让链路落到 weekday，而不是给一份空名单。"""
    _fixed_today(monkeypatch, datetime.date(2026, 6, 1))
    _session(monkeypatch, {})
    assert _fetch() is None


def test_one_broken_year_does_not_sink_the_others(monkeypatch):
    _fixed_today(monkeypatch, datetime.date(2026, 6, 1))
    _session(monkeypatch, {
        "2025": _Response(500, "gateway error"),
        "2026": _Response(200, _payload(2026, [
            {"name": "国庆", "date": "2026-10-01", "isOffDay": True}])),
    })
    calendar = _fetch()
    assert calendar is not None
    assert datetime.date(2026, 10, 1) not in calendar.days
    assert calendar.covers_through == datetime.date(2026, 12, 31)


def test_garbage_rows_are_skipped(monkeypatch):
    """上游格式变了不该让整个源报错——它是降级路径，得比主源更耐脏。"""
    _fixed_today(monkeypatch, datetime.date(2026, 6, 1))
    _session(monkeypatch, {"2026": _Response(200, _payload(2026, [
        {"name": "元旦", "date": "2026-01-01", "isOffDay": True},
        {"name": "坏的", "date": "not-a-date", "isOffDay": True},
        {"name": "缺字段", "isOffDay": True},
        "整条不是对象",
    ]))})
    calendar = _fetch()
    assert calendar is not None
    assert datetime.date(2026, 1, 1) not in calendar.days
    assert datetime.date(2026, 1, 2) in calendar.days


def test_a_body_without_days_is_a_failure(monkeypatch):
    _fixed_today(monkeypatch, datetime.date(2026, 6, 1))
    _session(monkeypatch, {"2026": _Response(200, json.dumps({"year": 2026}))})
    assert _fetch() is None


def test_it_is_registered_between_sina_and_weekday():
    from finmcp.datasource import trading_calendar as tc

    assert "holiday_cn" in pf.registered("trading_calendar")
    order = tc.DEFAULT_PROVIDER_ORDER
    assert order.index("sina") < order.index("holiday_cn") < order.index("weekday"), (
        "顺序是判据：sina 是交易所权威名单，holiday_cn 是国务院安排换算（差 1 天），"
        "weekday 只知道星期（728 天里错 56 天）"
    )


@pytest.mark.parametrize("source", ["holiday_cn"])
def test_the_contract_is_honoured(monkeypatch, source):
    """归一后必须是 Calendar，否则 resolve 会把它当成没给结果。"""
    from finmcp.datasource.trading_calendar import Calendar

    _fixed_today(monkeypatch, datetime.date(2026, 6, 1))
    _session(monkeypatch, {"2026": _Response(200, _payload(2026, [
        {"name": "元旦", "date": "2026-01-01", "isOffDay": True}]))})
    assert isinstance(_fetch(), Calendar)
