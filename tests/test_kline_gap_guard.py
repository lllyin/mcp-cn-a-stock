"""K 线契约里的缺口闸门。

实际发生过：``SH000688 涨跌幅`` 报成 +20.17%（真实 +2.41%）。成因：同花顺
某个年份文件取失败被静默跳过，源「成功」返回一条**断裂**的序列，链路不回退。今天前面
那一根从 09-04 的 1577.36 变成 1344.07，涨跌幅是跨缺口算的；同一份报告里 240 日均价
1540 → 1148、240 日最高 2255 → 1626（等于当天最高，历史高点整段没了）。

断裂比缺列危险：缺列会让契约当场判失败、链路回退；断裂的序列列是齐的、每个数值也在
合理区间，没有任何东西会拦它。这个文件钉结果侧那道闸门（对所有 K 线源生效）；源侧的
那道在 test_tonghuashun_kline.py。

**闸门数的是交易日，不是自然日**，所以下面的构造都得按交易日历来读：长假在闸门眼里
是 0，而"缺了 N 根"才是缺口。第一版数自然日、阈值 15，那只是"别把长假当缺口"的代理
指标——实测春节能到 11 个自然日，只剩 4 天余量。
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd
import pytest

from finmcp.datasource import kline_source, platform as pf, trading_calendar
from finmcp.datasource.kline_frame import FALLBACK_FRAME_COLUMNS


#: 一份写死的日历：2026-01-05 起的工作日，扣掉春节（02-16 .. 02-23 不开市）。
#: 必须注入——真日历来自 sina，测试机上可能既没有网络也没有 .runtime 缓存，那时
#: load() 给的是 weekday 兜底平台的空名单，长假会被数成 6 而不是 0，断言就飘了。
_SPRING_FESTIVAL = frozenset(datetime.date(2026, 2, d) for d in range(16, 24))
CAL = trading_calendar.Calendar(
    days=frozenset(
        d for d in pd.date_range("2024-01-01", "2026-12-31", freq="B").date
        if d not in _SPRING_FESTIVAL
    ),
    covers_through=datetime.date(2026, 12, 31),
    source="test",
)


@pytest.fixture(autouse=True)
def _fixed_calendar(monkeypatch):
    """全项目取日历只有 load() 一个入口，换掉它就够。"""
    monkeypatch.setattr(trading_calendar, "load", lambda **kw: CAL)


@pytest.fixture(autouse=True)
def _clean_gap_flags():
    """两个 ContextVar 各自复位。

    直接调 ``_has_no_gap`` 会把 ``_GAP_REJECTED`` 留成 True（它没有作用域，只在
    ``resolve`` 的那一段窗口里有意义，``resolve`` 每次进来都会先置 False，所以生产
    路径不受影响）。不复位的话下面那条「用完不泄漏」的断言测的是上一个测试的残留。
    """
    flags = (kline_source._ALLOW_GAPS, kline_source._GAP_REJECTED)
    for flag in flags:
        flag.set(False)
    yield
    for flag in flags:
        flag.set(False)


def _frame(days) -> pd.DataFrame:
    """一份列齐了的归一后日线表，好让契约的判断只落在缺口上。"""
    days = list(days)
    frame = pd.DataFrame({"日期": days})
    for column in FALLBACK_FRAME_COLUMNS:
        if column != "日期":
            frame[column] = [1.0] * len(days)
    return frame


def _run(start: str, days: int) -> list:
    """连续 ``days`` 个工作日，当交易日用。"""
    return pd.date_range(start, periods=days, freq="B").date.tolist()


def test_a_continuous_series_passes():
    assert kline_source._has_no_gap(_frame(_run("2026-01-05", 60))) is True


def test_a_long_holiday_is_structurally_zero():
    """2026 年春节：02-13 收盘到 02-24 开盘，11 个自然日，但一个交易日都没缺。

    这一条是换成交易日历的全部理由——数自然日时它是「11 天的洞，靠阈值 15 勉强放过」，
    数交易日时它就是 0，不靠任何阈值。
    """
    days = [datetime.date(2026, 2, 13), datetime.date(2026, 2, 24)]
    assert (days[1] - days[0]).days == 11, "自然日看是个 11 天的洞"
    assert trading_calendar.missing_trading_days(*days, CAL) == 0, "交易日看一根没缺"
    assert kline_source._has_no_gap(_frame(days)) is True


def test_the_degraded_calendar_still_lets_holidays_through():
    """日历取不到时退回按星期数：春节被数成 6，仍在默认阈值 10 之内。

    这一条守的是"日历是个优化，它挂了不该让闸门开始误判"。6 是 2015 年以来所有长假
    的最坏值（实测 32 个长假）。
    """
    blind = trading_calendar.Calendar(days=frozenset(),
                                      covers_through=datetime.date(2026, 12, 31),
                                      source="weekday")
    missing = trading_calendar.missing_trading_days(
        datetime.date(2026, 2, 13), datetime.date(2026, 2, 24), blind)
    assert missing == 6
    assert missing < kline_source.KLINE_MAX_GAP_TRADING_DAYS


def test_a_missing_year_is_caught():
    """今天那条序列的形状：一年在、中间一整年没了、今年在。"""
    days = _run("2024-09-02", 20) + _run("2026-06-01", 20)
    assert kline_source._has_no_gap(_frame(days)) is False


def test_the_threshold_is_counted_in_trading_days(monkeypatch):
    """阈值的单位必须是交易日：11 个自然日的春节缺 0 根，而缺 11 根要能被拦下。"""
    monkeypatch.setattr(kline_source, "KLINE_MAX_GAP_TRADING_DAYS", 10)
    holiday = [datetime.date(2026, 2, 13), datetime.date(2026, 2, 24)]
    assert kline_source._has_no_gap(_frame(holiday)) is True

    run = trading_calendar.trading_days(datetime.date(2026, 3, 2),
                                        datetime.date(2026, 5, 29), CAL)
    # 正好缺 10 根 -> 放行（等于上限）；缺 11 根 -> 拦下
    assert kline_source._has_no_gap(_frame([run[0]] + run[11:])) is True
    assert kline_source._has_no_gap(_frame([run[0]] + run[12:])) is False


def test_a_short_halt_is_tolerated():
    """停牌几天是真实数据，不该让这只票每次都多付一轮回退。"""
    run = trading_calendar.trading_days(datetime.date(2026, 3, 2),
                                        datetime.date(2026, 5, 29), CAL)
    assert kline_source._has_no_gap(_frame([run[0]] + run[4:])) is True   # 缺 3 根


def test_it_is_the_contract_that_fails_so_the_chain_falls_back():
    """真正要守的是这条：缺口让契约判失败，platform.resolve 才会问下一个源。"""
    good = _frame(_run("2026-01-05", 30))
    broken = _frame(_run("2024-09-02", 15) + _run("2026-01-05", 15))
    assert kline_source._honours_kline_contract(good) is True
    assert kline_source._honours_kline_contract(broken) is False


def test_a_missing_column_still_fails_first():
    """缺列那条判据不能被新加的缺口检查挤掉。"""
    frame = _frame(_run("2026-01-05", 30)).drop(columns=["最高"])
    assert kline_source._honours_kline_contract(frame) is False


def test_an_unsorted_frame_is_judged_on_its_dates_not_its_row_order():
    """判据是日期之间的间隔，与行的排列无关——源不保证给的是升序。"""
    days = _run("2026-01-05", 30)
    shuffled = _frame(days[15:] + days[:15])
    assert kline_source._has_no_gap(shuffled) is True

    holed = _run("2024-09-02", 15) + _run("2026-01-05", 15)
    assert kline_source._has_no_gap(_frame(holed[15:] + holed[:15])) is False


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(kline_source, "KLINE_MAX_GAP_TRADING_DAYS", 0)
    days = _run("2024-09-02", 10) + _run("2026-01-05", 10)
    assert kline_source._has_no_gap(_frame(days)) is True


@pytest.mark.parametrize("rows", [0, 1])
def test_too_few_rows_is_not_a_gap(rows):
    """kline_daily 只请求一天，一行的序列没有「相邻」可言，不能判失败。"""
    assert kline_source._has_no_gap(_frame(_run("2026-09-07", 1)[:rows])) is True


def test_a_frame_without_a_date_column_is_left_alone():
    """这一维的契约先查列；没有日期列时缺口检查不该抢着判死。"""
    assert kline_source._has_no_gap(pd.DataFrame({"收盘": [1.0, 2.0]})) is True


def test_it_logs_why(caplog):
    days = _run("2024-09-02", 10) + _run("2026-01-05", 10)
    with caplog.at_level(logging.WARNING, logger="finmcp"):
        kline_source._has_no_gap(_frame(days))
    messages = [record.getMessage() for record in caplog.records]
    assert any("缺口" in message for message in messages), (
        "判失败必须留下原因，否则只看得到链路回退了、看不出为什么"
    )
    assert any("2026-01-05" in message for message in messages), "日志要指出断点落在哪"


# --- 缺口是偏好，不是硬条件 ---------------------------------------------------
#
# 带缺口的序列有两种成因，闸门本身分不开：源坏了（换一个源就好），或者这只票真的
# 长期停牌（每个源都一样）。后一种如果硬拦，K 线和均线整段消失——比均线偏一点严重
# 得多。所以下面这几条钉的是"全都拦掉之后还要放行"。


class _Fake(pf.Platform):
    capabilities = frozenset({"kline"})

    def __init__(self, name, frame):
        self.name, self.label, self._frame = name, name, frame
        self.calls = 0

    def fetch_kline(self, request):
        self.calls += 1
        return self._frame


def _install(monkeypatch, *platforms):
    for platform in platforms:
        pf.register(platform, replace=True)
    monkeypatch.setenv("KLINE_PROVIDERS", ",".join(p.name for p in platforms))
    monkeypatch.setenv("KLINE_PROVIDERS_INDEX", ",".join(p.name for p in platforms))
    yield_names = [p.name for p in platforms]
    monkeypatch.setattr(kline_source, "DEFAULT_PROVIDER_ORDER", tuple(yield_names))
    return [p.name for p in platforms]


def _kline_request():
    return kline_source.KlineRequest(
        code="600000", start_date="2026-09-01", end_date="2026-09-02",
        adjust="qfq", symbol="SH600000",
    )


GAPPED = lambda: _frame(_run("2024-09-02", 15) + _run("2026-01-05", 15))          # noqa: E731
WHOLE = lambda: _frame(_run("2026-01-05", 30))                                    # noqa: E731


def test_a_gapped_source_loses_to_a_whole_one(monkeypatch):
    """09-07 那次就是这一条：同花顺断裂，腾讯是全的，应该换到腾讯。"""
    broken, good = _Fake("broken", GAPPED()), _Fake("good", WHOLE())
    _install(monkeypatch, broken, good)
    try:
        result = kline_source.resolve(_kline_request())
        assert result is not None and result.provider == "good"
    finally:
        pf.unregister("broken"); pf.unregister("good")


def test_when_every_source_gaps_the_data_still_comes_back(monkeypatch):
    """真实长期停牌：缺口在数据里，每个源都一样。不能因此丢掉整个 K 线维度。"""
    a, b = _Fake("a", GAPPED()), _Fake("b", GAPPED())
    _install(monkeypatch, a, b)
    status: dict = {}
    try:
        result = kline_source.resolve(_kline_request(), status=status)
        assert result is not None, "全部带缺口时硬拦会让 K 线、均线整段消失"
        assert len(result.frame) == 30
        assert status.get("kline_gap_tolerated") is True, "放行了要留痕"
    finally:
        pf.unregister("a"); pf.unregister("b")


def test_a_pure_failure_does_not_pay_for_a_second_round(monkeypatch):
    """没被缺口拦过就不重问——网络全挂的时候重问一遍也是全失败，白付一轮请求。"""
    class Dead(_Fake):
        def fetch_kline(self, request):
            self.calls += 1
            raise RuntimeError("connection reset")

    dead = Dead("dead", None)
    _install(monkeypatch, dead)
    try:
        assert kline_source.resolve(_kline_request()) is None
        assert dead.calls == 1
    finally:
        pf.unregister("dead")


def test_the_lenient_round_does_not_leak_to_the_next_call(monkeypatch):
    """宽松模式是 ContextVar 且用完就 reset：不然一只停牌票会让后面的标的都不查缺口。"""
    gapped = _Fake("gapped", GAPPED())
    _install(monkeypatch, gapped)
    try:
        assert kline_source.resolve(_kline_request()) is not None
        assert kline_source._ALLOW_GAPS.get() is False
        assert kline_source._GAP_REJECTED.get() is False
        # 闸门恢复了才算真的没泄漏
        assert kline_source._honours_kline_contract(GAPPED()) is False
    finally:
        pf.unregister("gapped")
