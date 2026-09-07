"""交易日历的公共门面。

门面最容易烂的方式是**和实现悄悄脱节**：实现加了个函数，门面没转出去，下一个人
以为公共入口里没这个能力，于是自己又写一遍 ``weekday() < 5``——那正是这一层要
消灭的东西。所以这里盯两件事：转出去的东西齐不齐、以及底层有没有反向依赖门面。
"""

from __future__ import annotations

import datetime
import pathlib

from finmcp import market_calendar
from finmcp.datasource import trading_calendar


def test_the_facade_re_exports_everything_the_implementation_declares():
    """实现的 ``__all__`` 必须全部出现在门面里，一个不少。"""
    missing = set(trading_calendar.__all__) - set(market_calendar.__all__)
    assert not missing, (
        f"实现里有 {sorted(missing)} 没转出到公共门面——"
        "调用方看不到就会自己再写一遍判断"
    )


def test_the_facade_does_not_invent_names():
    """反过来也要齐：门面里的每个名字都得真的指向实现。"""
    for name in market_calendar.__all__:
        assert hasattr(market_calendar, name), name
        assert getattr(market_calendar, name) is getattr(trading_calendar, name), (
            f"{name} 在门面和实现里不是同一个对象"
        )


def test_the_datasource_layer_never_imports_the_facade():
    """底层反向依赖门面会把循环导入引回来（datasource/__init__ 导入期就建数据源）。"""
    offenders = []
    for path in pathlib.Path("finmcp/datasource").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "market_calendar" in text:
            offenders.append(str(path))
    assert not offenders, (
        f"{offenders} 引用了公共门面；datasource 下面请直接用 "
        "`from . import trading_calendar`"
    )


def test_the_public_helpers_work_through_the_facade():
    """门面不是空壳：拿一份写死的日历走一遍，四个判断都要通。"""
    days = frozenset(
        d for d in (datetime.date(2026, 9, 1) + datetime.timedelta(days=i) for i in range(30))
        if d.weekday() < 5 and d != datetime.date(2026, 9, 10)     # 造一天休市
    )
    cal = market_calendar.Calendar(days=days,
                                   covers_through=datetime.date(2026, 12, 31),
                                   source="test")
    assert market_calendar.is_trading_day(datetime.date(2026, 9, 10), cal) is False
    assert market_calendar.is_trading_day(datetime.date(2026, 9, 11), cal) is True
    assert market_calendar.previous_trading_day(datetime.date(2026, 9, 11), cal) == \
        datetime.date(2026, 9, 9)
    assert market_calendar.next_trading_day(datetime.date(2026, 9, 9), cal) == \
        datetime.date(2026, 9, 11)
    assert datetime.date(2026, 9, 10) not in market_calendar.trading_days(
        datetime.date(2026, 9, 7), datetime.date(2026, 9, 11), cal)
    assert market_calendar.missing_trading_days(
        datetime.date(2026, 9, 9), datetime.date(2026, 9, 11), cal) == 0
