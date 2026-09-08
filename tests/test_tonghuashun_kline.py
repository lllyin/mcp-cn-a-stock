"""同花顺日 K：年份文件取不到要抛、未上市年份要跳、盘中占位行要跳。

依据是 2026-09-07 的两件事：2024、2025 两年文件返回 502 被当成"没上市"静默跳过，科创50
只剩 164 根、报告少了 240 日五行；科创50 2026 年文件盘中多一行
``20260907,,,,1577.36,0,,0.000,,,0``，``float('')`` 让整个源报错落到腾讯，当天 6 次。
"""

from __future__ import annotations

import pytest

from finmcp.datasource import kline_source
from finmcp.datasource.platforms import tonghuashun

ROWS_2026 = (
    "20260105,1360.10,1380.20,1350.30,1370.40,753925100,74189842000.00,1.074,,,0;"
    "20260106,1370.40,1390.00,1360.00,1385.50,700000000,70000000000.00,1.000,,,0;"
    "20260107,1385.50,1400.00,1380.00,1395.00,650000000,65000000000.00,0.950,,,0"
)
ROWS_2024 = (
    "20240902,1000.10,1010.20,990.30,1005.40,500000000,50000000000.00,0.700,,,0;"
    "20240903,1005.40,1015.00,995.00,1010.00,510000000,51000000000.00,0.710,,,0"
)
PLACEHOLDER = "20260907,,,,1577.36,0,,0.000,,,0"


def _jsonp(year: str, data: str) -> str:
    return f'quotebridge_v6_line_hs_1B0688_01_{year}({{"data":"{data}"}})'


class _Response:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _session(monkeypatch, by_year: dict, calls: list | None = None):
    """按 URL 里的年份给响应；没配的年份给 200 空数据。"""
    import requests

    class Session:
        def get(self, url, headers=None, timeout=None):
            year = url.rsplit("/", 1)[-1].split(".")[0]
            if calls is not None:
                calls.append(year)
            return by_year.get(year, _Response(200, _jsonp(year, "")))

    monkeypatch.setattr(requests, "Session", Session)


def _request():
    return kline_source.KlineRequest(
        code="000688", start_date="2024-09-08", end_date="2026-09-07", adjust="qfq", symbol="SH000688",
    )


def test_a_5xx_year_file_is_a_failure_not_a_missing_year(monkeypatch):
    """502 抛出去，让链路落到腾讯把 483 根补齐；以前静默跳过，只剩 164 根。"""
    _session(monkeypatch, {
        "2024": _Response(502, "<html><head><title>502 Bad Gateway</title></head></html>"),
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    })
    with pytest.raises(RuntimeError, match="2024 年文件 HTTP 502"):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())


def test_a_404_year_is_not_listed_yet_and_is_skipped(monkeypatch):
    """C马矿 2026-09-01 上市，2025 年文件是 404 空正文：那不是错，后面的年份照常用。"""
    calls: list = []
    _session(monkeypatch, {
        "2024": _Response(404, ""),
        "2025": _Response(404, ""),
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    }, calls)
    frame = tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert calls == ["2024", "2025", "2026"]
    assert len(frame) == 3


def test_a_404_in_the_middle_is_a_gap_not_a_missing_year(monkeypatch):
    """同一个 404，判据是「已经取到过行没有」——years 是升序遍历（旧→新）。

    科创50 从 2020 年就有，它的 2025 年 404 只能是取数失败。静默跳过会得到一条断裂的
    序列而链路不回退（源「成功」了）：2026-09-07 14:56 的 +20.17% 就是这么来的。
    """
    calls: list = []
    _session(monkeypatch, {
        "2024": _Response(200, _jsonp("2024", ROWS_2024)),
        "2025": _Response(404, ""),
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    }, calls)
    with pytest.raises(RuntimeError, match="2025 年文件 404"):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert calls == ["2024", "2025"], "抛出去就不该再取后面的年份"


def test_the_gap_error_says_it_is_a_gap(monkeypatch):
    """出错信息要说清是缺口，否则下一个人会照旧当成「未上市」再改回 continue。"""
    _session(monkeypatch, {
        "2024": _Response(200, _jsonp("2024", ROWS_2024)),
        "2025": _Response(404, ""),
    })
    with pytest.raises(RuntimeError, match="这是缺口不是未上市"):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())


def test_a_200_that_is_not_jsonp_is_a_failure(monkeypatch):
    _session(monkeypatch, {"2024": _Response(200, "<html>maintenance</html>")})
    with pytest.raises(RuntimeError, match="不是 JSONP"):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())


def test_an_empty_data_year_is_fine(monkeypatch):
    """科创50 2019 年文件是 200 加空 data：还没发布的年份，正常。"""
    _session(monkeypatch, {
        "2024": _Response(200, _jsonp("2024", "")),
        "2025": _Response(200, _jsonp("2025", "")),
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    })
    frame = tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert len(frame) == 3


def test_the_intraday_placeholder_row_is_skipped(monkeypatch):
    """开高低为空的那一行是占位，不是数据；以前 float('') 让整个源报错。"""
    _session(monkeypatch, {
        "2026": _Response(200, _jsonp("2026", ROWS_2026 + ";" + PLACEHOLDER)),
    })
    frame = tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert len(frame) == 3
    assert str(frame["日期"].iloc[-1]).startswith("2026-01-07")


def test_rows_skip_placeholders_and_tolerate_empty_amounts():
    payload = {"data": "20260105,1.0,1.2,0.9,1.1,,,;" + PLACEHOLDER}
    rows = tonghuashun.TonghuashunPlatform._rows(payload)
    assert len(rows) == 1
    assert rows[0]["成交量"] == 0.0 and rows[0]["成交额"] == 0.0 and rows[0]["换手率"] == 0.0


# --- 源级总预算 ---------------------------------------------------------------
#
# 为什么需要它：这个源按**年份**取文件，一个 2 年窗口要 3 个，每个各自一次
# DNS + connect + read。所以一次取数的最坏耗时是「文件数 × 单次超时 × 重试」，
# 而文件数随窗口线性增长——在加这一层之前它没有任何上界。
#
# 2026-09-08 线上的账：d.10jqka.com.cn 间歇挂起的那几分钟，三个指数的取数各花
# 89.33s / 89.03s / 79.53s（同日 343 次取数 p50 2.72s、p90 7.58s、p95 15.73s），
# 批次因此 105.8s，被调用方 75s 的 timeout 杀掉、重试一次，服务端留下两条
# ERROR Stateless session crashed。


class _SlowSession:
    """每次 get 花掉 ``cost`` 秒（推假时钟），并记下被传进来的 timeout。"""

    def __init__(self, clock, cost, by_year, calls):
        self.clock, self.cost, self.by_year, self.calls = clock, cost, by_year, calls

    def get(self, url, headers=None, timeout=None):
        year = url.rsplit("/", 1)[-1].split(".")[0]
        self.calls.append((year, timeout))
        self.clock.advance(self.cost)
        return self.by_year.get(year, _Response(200, _jsonp(year, "")))


class _Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def _slow(monkeypatch, cost, by_year=None, budget=45.0):
    """把时钟和 session 都换掉，返回 calls 供断言。"""
    import requests

    clock, calls = _Clock(), []
    monkeypatch.setattr(tonghuashun.time, "monotonic", clock)
    monkeypatch.setattr(tonghuashun, "KLINE_TONGHUASHUN_BUDGET_SECONDS", budget)
    monkeypatch.setattr(
        requests, "Session",
        lambda: _SlowSession(clock, cost, by_year or {}, calls),
    )
    return calls


def test_budget_exhausted_raises_instead_of_returning_a_short_series(monkeypatch):
    """预算用尽必须抛，**不能**拿已取到的行凑一份返回。

    凑一份返回就是一条**断裂的序列**：列是齐的、每个数值都在合理区间，源"成功"
    返回，没有任何东西会拦它——而涨跌幅会跨缺口计算、均线全错。SH000688 报成
    +20.17%（真实 +2.41%）就是这么来的。抛出去让链路落到腾讯，那里给的是完整序列。
    """
    # 每个请求 25s：2024 之后剩 20s，2025 之后剩 -5s，2026 那一年发不出去。
    calls = _slow(monkeypatch, cost=25.0, budget=45.0, by_year={
        "2024": _Response(200, _jsonp("2024", ROWS_2024)),
    })
    with pytest.raises(RuntimeError, match="总预算"):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert [year for year, _ in calls] == ["2024", "2025"]


def test_the_last_request_cannot_overshoot_the_budget(monkeypatch):
    """单次 timeout 被削到预算剩余量——不然预算只是个建议。

    每个请求 18s：2024 起始剩 45s、2025 剩 27s，两次都够用满 15s 的单次超时；
    2026 只剩 9s，timeout 必须跟着降到 9，否则那一个请求能探出预算 6 秒。
    """
    calls = _slow(monkeypatch, cost=18.0, budget=45.0, by_year={
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    })
    tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert [t for _, t in calls] == [15, 15, pytest.approx(9.0)]


def test_a_doomed_request_is_not_sent(monkeypatch):
    """预算剩不到 _MIN_USEFUL_SLICE 就别发了——注定超时的请求只是推迟失败。"""
    calls = _slow(monkeypatch, cost=44.0, budget=45.0)
    with pytest.raises(RuntimeError, match="总预算"):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert len(calls) == 1, f"只该发出第一个请求，实际 {calls}"


def test_a_fast_fetch_is_untouched(monkeypatch):
    """正常路径（p50 2.72s）不该被预算碰到一下。"""
    _slow(monkeypatch, cost=1.0, budget=45.0, by_year={
        "2024": _Response(200, _jsonp("2024", ROWS_2024)),
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    })
    frame = tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert len(frame) == 3


def test_budget_zero_disables_the_cap(monkeypatch):
    """置 0 退回加这一层之前的行为，单次超时原样用满。"""
    calls = _slow(monkeypatch, cost=40.0, budget=0.0, by_year={
        "2026": _Response(200, _jsonp("2026", ROWS_2026)),
    })
    tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    assert [t for _, t in calls] == [15, 15, 15]


def test_the_retry_spends_the_budget_not_a_second_full_timeout(monkeypatch):
    """年份文件重试被关在预算里。

    ``_YEAR_FILE_RETRIES`` 当初只算了 502 的成本（约 150ms，重试很便宜），没算
    **超时**的成本——一次超时付满 15s，重试就是 30s，最坏情况是「文件数 × 超时 × 2」。
    预算把这个乘法关掉了。
    """
    calls = _slow(monkeypatch, cost=20.0, budget=45.0, by_year={
        "2024": _Response(502, "<html>502</html>"),
    })
    with pytest.raises(RuntimeError):
        tonghuashun.TonghuashunPlatform().fetch_kline(_request())
    # 2024 首次 20s + 重试 20s = 40s，剩 5s；下一个请求削到 5s 后预算见底。
    assert [year for year, _ in calls][:2] == ["2024", "2024"]
    assert sum(1 for year, _ in calls if year == "2024") == 2


def test_budget_leaves_room_for_a_multi_year_window():
    """预算至少要装得下两个跑满单次超时的请求。

    装不下就退化成「比单次超时还严的单请求超时」：一个跨年窗口的第二个年份文件必然
    被砍，而砍掉的是**本来能成功**的取数——指数的成交量随之退到腾讯口径、低约 3.5%。
    这是拿正确的数换耗时，方向反了（AGENTS.md §一：数据完整 > 功能正确 > 性能）。

    具体该设多少和环境有关，用 `probe_tuning.py tonghuashun` 量；这里只守住下界。
    """
    from finmcp.config import KLINE_TONGHUASHUN_BUDGET_SECONDS

    assert KLINE_TONGHUASHUN_BUDGET_SECONDS > tonghuashun._REQUEST_TIMEOUT * 2
