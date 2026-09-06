"""市场时段：边界配置、校验，以及两个消费方读的是不是同一份。

边界本身的正确性归这里；报告缓存怎么用纪元归 test_report_cache.py。
"""

from __future__ import annotations

import datetime
import importlib

import pytest

from finmcp import config
from finmcp import market_session as ms

MONDAY = datetime.date(2026, 8, 17)


def at(clock, day=MONDAY, *, minus_minutes=0):
    """MONDAY 那天的某个时刻。minus_minutes 用 timedelta 算，免得 minute-1 变成 -1。"""
    if isinstance(clock, int):
        clock = datetime.time(clock, 0)
    moment = datetime.datetime.combine(day, clock)
    return moment - datetime.timedelta(minutes=minus_minutes)


def _reload(**env):
    """按给定环境变量重载配置和本模块，返回重载后的 market_session。"""
    importlib.reload(config)
    return importlib.reload(ms)


# --- 交易所事实不是配置项 -----------------------------------------------------


def test_the_exchange_timetable_is_not_configurable():
    """09:30/11:30/13:00/15:00 是交易所规则，不是可调的经验值。"""
    assert ms.OPEN == datetime.time(9, 30)
    assert ms.LUNCH_START == datetime.time(11, 30)
    assert ms.LUNCH_END == datetime.time(13, 0)
    assert ms.CLOSE == datetime.time(15, 0)


# --- 边界校验：越界夹回并告警，服务照常起 --------------------------------------


@pytest.mark.parametrize("configured,expected", [
    (datetime.time(9, 15), datetime.time(9, 15)),
    (datetime.time(9, 30), datetime.time(9, 30)),
    (datetime.time(7, 0), datetime.time(7, 0)),
    (datetime.time(10, 0), datetime.time(9, 30)),   # 晚于开盘 → 夹到开盘
    (datetime.time(6, 0), datetime.time(7, 0)),     # 早得离谱 → 夹到 07:00
])
def test_warmup_is_clamped(monkeypatch, configured, expected):
    """warmup 晚于开盘，则 09:30→warmup 这段真在交易却被判成 CLOSED，
    会把昨天纪元的数据当成今天的发出去。"""
    monkeypatch.setattr(ms, "MARKET_EPOCH_WARMUP_TIME", configured, raising=False)
    monkeypatch.setattr(ms, "OPEN", ms.OPEN)
    assert ms._clamp(configured, ms._EARLIEST_WARMUP, ms.OPEN, "warmup") == expected


@pytest.mark.parametrize("configured,expected", [
    (datetime.time(15, 30), datetime.time(15, 30)),
    (datetime.time(15, 0), datetime.time(15, 0)),
    (datetime.time(9, 30), datetime.time(15, 0)),   # 早于收盘 → 夹到收盘
    (datetime.time(14, 59), datetime.time(15, 0)),
])
def test_settle_is_clamped_to_at_least_the_close(configured, expected):
    """早于收盘会把仍在变动的连续竞价折进完全复用纪元，而那时
    research.today_volume_est_ratio 还在动。"""
    assert ms._clamp(configured, ms.CLOSE, ms.FINAL_TIME, "settle") == expected


def test_settle_is_clamped_to_at_most_final():
    """settle 晚于 final 会让两个纪元次序颠倒。"""
    assert ms._clamp(datetime.time(23, 0), ms.CLOSE, ms.FINAL_TIME, "settle") == ms.FINAL_TIME


def test_a_clamped_boundary_warns(caplog):
    """夹回去可以，但不能不吭声——配错了得能从日志里看出来。"""
    with caplog.at_level("WARNING", logger="finmcp"):
        ms._clamp(datetime.time(23, 0), ms.CLOSE, datetime.time(17, 0), "TEST_TIME")
    assert "TEST_TIME" in caplog.text


def test_the_clamp_order_is_final_then_settle(monkeypatch):
    """final 先夹进 [收盘, 23:00]，settle 再夹进 [收盘, final]。

    反过来两者互相依赖，夹不出确定的结果。这里用一组互相冲突的配置验证顺序：
    settle=1800 且 final=1600 时，final 合法、settle 必须被夹到 1600。
    """
    monkeypatch.setattr(config, "MARKET_EPOCH_SETTLE_TIME", datetime.time(18, 0))
    monkeypatch.setattr(config, "MARKET_EPOCH_FINAL_TIME", datetime.time(16, 0))
    monkeypatch.setattr(ms, "MARKET_EPOCH_SETTLE_TIME", datetime.time(18, 0))
    monkeypatch.setattr(ms, "MARKET_EPOCH_FINAL_TIME", datetime.time(16, 0))
    warmup, settle, final, _ = ms._resolve_boundaries()
    assert final == datetime.time(16, 0)
    assert settle == datetime.time(16, 0)


# --- 配置一路贯通 -------------------------------------------------------------


@pytest.mark.parametrize("name,env_value,attr,expected", [
    ("MARKET_EPOCH_WARMUP_TIME", "0920", "WARMUP_TIME", datetime.time(9, 20)),
    ("MARKET_EPOCH_SETTLE_TIME", "1600", "SETTLE_TIME", datetime.time(16, 0)),
    ("MARKET_EPOCH_FINAL_TIME", "1800", "FINAL_TIME", datetime.time(18, 0)),
    ("MARKET_EPOCH_WARMUP_TIME", "garbage", "WARMUP_TIME", datetime.time(9, 15)),
    ("MARKET_EPOCH_SETTLE_TIME", "0930", "SETTLE_TIME", datetime.time(15, 0)),
])
def test_each_boundary_is_wired_to_its_env_var(monkeypatch, name, env_value, attr, expected):
    monkeypatch.setenv(name, env_value)
    try:
        reloaded = _reload()
        assert getattr(reloaded, attr) == expected
    finally:
        monkeypatch.delenv(name, raising=False)
        _reload()


def test_the_buffer_is_wired_and_bounded(monkeypatch):
    monkeypatch.setenv("MARKET_EPOCH_BUFFER_MINUTES", "10")
    try:
        reloaded = _reload()
        assert reloaded.BUFFER == datetime.timedelta(minutes=10)
        assert reloaded.LUNCH_SETTLE == datetime.time(11, 40)
    finally:
        monkeypatch.delenv("MARKET_EPOCH_BUFFER_MINUTES", raising=False)
        _reload()


# --- 边界移动之后纪元跟着移动 --------------------------------------------------


def test_moving_settle_moves_the_postclose_boundary(monkeypatch):
    """settle 往后挪，postclose 的起点跟着挪；final 不受影响。"""
    moved = datetime.time(15, 45)
    monkeypatch.setattr(ms, "SETTLE_TIME", moved)
    assert ms.phase_and_epoch(at(moved, minus_minutes=1))[0] == ms.PHASE_LIVE
    assert ms.phase_and_epoch(at(moved))[0] == ms.PHASE_POSTCLOSE


def test_settle_equal_to_final_leaves_no_postclose_window(monkeypatch):
    """settle=final 是合法的极端值：postclose 窗口为空，全部走 live TTL。"""
    monkeypatch.setattr(ms, "SETTLE_TIME", ms.FINAL_TIME)
    assert ms.phase_and_epoch(at(ms.FINAL_TIME, minus_minutes=1))[0] == ms.PHASE_LIVE


# --- 不变量 1：一个纪元不横跨 warmup / final -----------------------------------


def test_no_epoch_straddles_the_fund_flow_flip():
    """纪元横跨翻转点，同一个纪元里就会出现两种形状的报告——而"命中与否不改变
    返回内容"正是报告缓存赖以成立的前提。

    翻转点两侧一分钟必须落在不同纪元。
    """
    for boundary in (ms.WARMUP_TIME, ms.FINAL_TIME):
        before = ms.phase_and_epoch(at(boundary, minus_minutes=1))[1]
        after = ms.phase_and_epoch(at(boundary))[1]
        assert before != after, f"{boundary} 两侧落在同一个纪元里了"

    # settle 不在此列：它两侧都是同一个交易日的数据，只是复用策略不同（live TTL
    # 对 postclose），报告形状一样，所以允许 token 变而不要求分支变。


# --- 两个消费方读的是同一份 ----------------------------------------------------


def test_the_fund_flow_window_matches_the_boundaries():
    """窗口就是 [warmup, final)。这两个边界同时是纪元边界，见模块开头。"""
    warm, flip = ms.WARMUP_TIME, ms.FINAL_TIME
    assert ms.is_realtime_fund_flow_window(at(warm)) is True
    assert ms.is_realtime_fund_flow_window(at(warm, minus_minutes=1)) is False
    assert ms.is_realtime_fund_flow_window(at(flip, minus_minutes=1)) is True
    assert ms.is_realtime_fund_flow_window(at(flip)) is False


def test_the_window_is_defined_exactly_once():
    """各写一遍就会漂：改一处不改另一处，纪元就横跨翻转点了。

    查源码而不是查属性——conftest 有个 autouse fixture 会把 research 上那个名字
    换成桩，查属性会看到桩。这里要钉的是"只有一处 def"。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "finmcp"
    definitions = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "def is_realtime_fund_flow_window" in path.read_text(encoding="utf-8")
    ]
    assert definitions == ["market_session.py"], f"定义处不止一个：{definitions}"


def test_cache_reads_the_same_definition():
    from finmcp import cache

    assert cache.market_phase.__module__ == "finmcp.market_session"
    assert cache.market_phase.__name__ == "phase_and_epoch"


def test_a_non_trading_day_is_never_a_fund_flow_window():
    """2026-09-05 周六 15:11 那一轮，服务为一个不存在的交易日拉起 Chromium：
    66 次页面加载、65 次撞验证码，还把出口 IP 打热。"""
    saturday = datetime.datetime(2026, 9, 5, 15, 11)
    assert ms.is_realtime_fund_flow_window(saturday) is False
    assert ms.phase_and_epoch(saturday)[0] == ms.PHASE_CLOSED
