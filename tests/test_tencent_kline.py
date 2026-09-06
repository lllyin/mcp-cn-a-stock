"""腾讯日 K 自己发请求之后，要和 ``ak.stock_zh_a_hist_tx`` 逐字等价。

这里盯三件事：归一步骤和 AkShare 相同（含 dtype）、翻页在该停的时候停、
腾讯不认的代码仍然以 KeyError 冒出去。样例行是 2026-09-06 从接口原样抄下来的。
"""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from finmcp.datasource.platforms import tencent

# 原始行：[日期, 开, 收, 高, 低, 成交量, {}, 换手率, 成交额, ...]。第 6 位是占位 dict，
# 尾巴上个股是一个空串、科创/指数是两个 "0.00"，列数不齐，建表时要能容下。
SZ002875_QFQ = [
    ["2026-09-01", "14.59", "14.15", "14.86", "13.86", "142644.00", {}, "6.72", "20151.92", ""],
    ["2026-09-02", "14.12", "14.08", "14.34", "13.88", "90275.00", {}, "4.25", "12701.98", ""],
    ["2026-09-03", "14.10", "13.74", "14.31", "13.68", "83091.00", {}, "3.91", "11546.61", ""],
    ["2026-09-04", "13.77", "13.62", "13.94", "13.50", "64651.00", {}, "3.04", "8892.54", ""],
]
SH688981_DAY = [
    ["2026-09-02", "124.58", "122.55", "124.58", "122.00", "28545334.00", {}, "1.43", "351840.35", "0.00", "0.00"],
    ["2026-09-03", "123.77", "123.87", "124.86", "122.90", "22153826.00", {}, "1.11", "274693.61", "0.00", "0.00"],
    ["2026-09-04", "124.98", "121.14", "125.75", "120.55", "31271765.00", {}, "1.56", "384796.35", "0.00", "0.00"],
]
SH000001_DAY = [
    ["2026-09-02", "3963.07", "3941.39", "3965.81", "3932.25", "516472775.00", {}, "1.07", "83536775.59", "0.00", "0.00"],
    ["2026-09-03", "3952.79", "3942.09", "3968.11", "3930.45", "496990189.00", {}, "1.03", "81988235.04", "0.00", "0.00"],
]
SZ000001_DAY = [
    ["2026-09-02", "11.92", "11.91", "11.99", "11.85", "892248.00", {}, "0.46", "106326.18", "0.00", "0.00"],
    ["2026-09-03", "11.88", "11.88", "12.08", "11.83", "1105134.00", {}, "0.57", "132423.03", "0.00", "0.00"],
]


def _akshare_reference(pages: list, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """``ak.stock_zh_a_hist_tx`` 取到原始行之后做的事，逐行照抄，作为等价的参照。"""
    big_df = pd.DataFrame()
    for page in pages:
        big_df = pd.concat([big_df, pd.DataFrame(page)], ignore_index=True)
    big_df = big_df.iloc[:, [0, 1, 2, 3, 4, 5, 7, 8]]
    big_df.columns = ["date", "open", "close", "high", "low", "volume", "turnover", "amount"]
    big_df["date"] = pd.to_datetime(big_df["date"], errors="coerce").dt.date
    for column in ("open", "close", "high", "low", "volume", "turnover", "amount"):
        big_df[column] = pd.to_numeric(big_df[column], errors="coerce")
    if not symbol.startswith(("sh688", "sz399", "sh000", "sz000")):
        big_df["volume"] = big_df["volume"] * 100
    big_df["turnover"] = big_df["turnover"] / 100
    big_df["amount"] = big_df["amount"] * 10000
    big_df.drop_duplicates(inplace=True, ignore_index=True)
    big_df.index = pd.to_datetime(big_df["date"], errors="coerce")
    big_df.sort_index(inplace=True)
    big_df = big_df[start_date:end_date]
    big_df.reset_index(inplace=True, drop=True)
    return big_df


@pytest.mark.parametrize("symbol,rows", [
    ("sz002875", SZ002875_QFQ),   # 主板个股：成交量 ×100
    ("sh688981", SH688981_DAY),   # 科创板：AkShare 认为已经是股，不乘
    ("sh000001", SH000001_DAY),   # 上证指数：不乘
    ("sz000001", SZ000001_DAY),   # 深市 000 开头个股：AkShare 的怪癖，也不乘——照抄
])
def test_rows_to_frame_matches_akshare_step_for_step(symbol, rows):
    ours = tencent._rows_to_frame(rows, symbol, "20260901", "20260904")
    reference = _akshare_reference([rows], symbol, "20260901", "20260904")
    pd.testing.assert_frame_equal(ours, reference, check_dtype=True, check_exact=True)


def test_window_is_cut_after_normalisation_like_akshare():
    ours = tencent._rows_to_frame(SZ002875_QFQ, "sz002875", "20260902", "20260903")
    reference = _akshare_reference([SZ002875_QFQ], "sz002875", "20260902", "20260903")
    pd.testing.assert_frame_equal(ours, reference)
    assert ours["date"].tolist() == [datetime.date(2026, 9, 2), datetime.date(2026, 9, 3)]


def test_overlapping_pages_give_the_same_frame_as_akshare_concatenation():
    """AkShare 按年取的页会重叠、靠 drop_duplicates 去重；这里按日期去重再建表，结果要一样。"""
    page_a = SZ002875_QFQ[:3]
    page_b = SZ002875_QFQ[1:]
    deduped: dict = {}
    for page in (page_b, page_a):
        for row in page:
            deduped.setdefault(row[0], row)
    ours = tencent._rows_to_frame([deduped[d] for d in sorted(deduped)], "sz002875", "20260901", "20260904")
    reference = _akshare_reference([page_a, page_b], "sz002875", "20260901", "20260904")
    pd.testing.assert_frame_equal(ours, reference)
    assert ours["date"].tolist() == [datetime.date(2026, 9, d) for d in (1, 2, 3, 4)]


# --- 翻页 -------------------------------------------------------------------


def _paged(pages_by_end: dict, calls: list | None = None):
    """按 ``param`` 里的 end 日期返回对应的页，记录请求。"""
    def get(params):
        symbol, _, start, end, count, adjust = params["param"].split(",")
        if calls is not None:
            calls.append((start, end, int(count), adjust))
        page = pages_by_end.get(end, [])
        key = "day" if adjust == "" else "qfqday"
        return {"data": {symbol: {key: page}}}
    return get


def _synthetic_rows(first: datetime.date, count: int) -> list:
    return [
        [(first + datetime.timedelta(days=i)).isoformat(), "1", "1", "1", "1", "100.00", {}, "0.10", "1.00", ""]
        for i in range(count)
    ]


def test_a_window_that_fits_one_page_costs_one_request():
    calls = []
    rows = tencent._fetch_kline_rows(
        "sz002875", "2026-09-01", "2026-09-04", "qfq",
        get=_paged({"2026-09-04": SZ002875_QFQ}, calls),
    )
    assert [row[0] for row in rows] == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert calls == [("2026-09-01", "2026-09-04", 640, "qfq")]


def test_rows_outside_the_window_are_dropped():
    """接口以 end 为锚往前给满 640 根，start 基本不起作用，早于 start 的要裁掉。"""
    rows = tencent._fetch_kline_rows(
        "sz002875", "2026-09-03", "2026-09-04", "qfq",
        get=_paged({"2026-09-04": SZ002875_QFQ}),
    )
    assert [row[0] for row in rows] == ["2026-09-03", "2026-09-04"]


def test_a_full_page_that_does_not_reach_start_pages_backwards():
    """满 640 根且最早那根仍晚于 start，就把 end 换成它前一天再取一页。"""
    newer_first = datetime.date(2024, 1, 8)
    newer = _synthetic_rows(newer_first, 640)               # 2024-01-08 .. 2025-10-08
    older = _synthetic_rows(datetime.date(2022, 4, 8), 640)  # 2022-04-08 .. 2024-01-07
    calls = []
    rows = tencent._fetch_kline_rows(
        "sz002875", "2023-01-01", "2025-10-08", "qfq",
        get=_paged({"2025-10-08": newer, "2024-01-07": older}, calls),
    )
    assert [c[1] for c in calls] == ["2025-10-08", "2024-01-07"]
    assert rows[0][0] == "2023-01-01" and rows[-1][0] == "2025-10-08"
    assert len(rows) == len({row[0] for row in rows})       # 无重复日期


def test_paging_stops_on_a_short_page():
    rows = tencent._fetch_kline_rows(
        "sz002875", "2020-01-01", "2026-09-04", "qfq",
        get=_paged({"2026-09-04": SZ002875_QFQ}),           # 只有 4 根 → 上市不久，不再翻
    )
    assert len(rows) == 4


def test_paging_stops_on_an_empty_page():
    older = []
    newer = _synthetic_rows(datetime.date(2024, 1, 8), 640)
    calls = []
    rows = tencent._fetch_kline_rows(
        "sz002875", "2020-01-01", "2025-10-08", "qfq",
        get=_paged({"2025-10-08": newer, "2024-01-07": older}, calls),
    )
    assert len(calls) == 2 and len(rows) == 640


def test_an_unknown_symbol_raises_key_error_like_akshare():
    """腾讯不认的代码：响应 data 里没有那个键。平台层据 KeyError 判"不支持"，措辞才对。"""
    def get(params):
        return {"data": {}}
    with pytest.raises(KeyError):
        tencent._fetch_kline_rows("bj920021", "2026-09-01", "2026-09-04", "qfq", get=get)


def test_index_without_adjusted_series_uses_the_day_key():
    """指数没有复权序列，请求 qfq 响应里只有 day；AkShare 的取键顺序是 day 优先。"""
    assert tencent._series_of({"day": [1], "qt": {}}, "qfq") == [1]
    assert tencent._series_of({"hfqday": [2]}, "hfq") == [2]
    assert tencent._series_of({"qfqday": [3]}, "qfq") == [3]
    with pytest.raises(KeyError):
        tencent._series_of({"qt": {}}, "qfq")


# --- 平台入口 -----------------------------------------------------------------


def test_fetch_kline_asks_for_the_widened_window_in_akshare_notation(monkeypatch):
    from finmcp.datasource import kline_source

    seen = {}

    def fake_history(symbol, start_date, end_date, adjust):
        seen.update(symbol=symbol, start=start_date, end=end_date, adjust=adjust)
        return pd.DataFrame(columns=tencent._FRAME_COLUMNS)

    monkeypatch.setattr(tencent, "_history_frame", fake_history)
    request = kline_source.KlineRequest(
        code="600519", start_date="2026-09-01", end_date="2026-09-04", adjust="none", symbol="SH600519",
    )
    assert tencent.TencentPlatform().fetch_kline(request) is None
    # 和 AkShare 的入参写法一致：YYYYMMDD、不复权是空串、起点往前推 20 天
    assert seen == {"symbol": "sh600519", "start": "20260812", "end": "20260904", "adjust": ""}


def test_history_frame_without_rows_is_an_empty_frame(monkeypatch):
    monkeypatch.setattr(tencent, "_passes_akshare_gate", lambda symbol: True)
    monkeypatch.setattr(tencent, "_fetch_kline_rows", lambda *a, **k: [])
    frame = tencent._history_frame("sz002875", "20260901", "20260904", "qfq")
    assert frame.empty and list(frame.columns) == tencent._FRAME_COLUMNS


# --- "认不认这个代码"这道门要和 AkShare 一样 -----------------------------------


def _gate_responses(week_trends_data, probe_data, calls):
    """按 URL 分发：weekTrends 给一个，320 根探测给一个。"""
    def get(url, params):
        calls.append(url.rsplit("/", 1)[-1])
        if url == tencent._WEEK_TRENDS_URL:
            return {"code": 0, "msg": "ok", "data": week_trends_data}
        return {"data": probe_data}
    return get


@pytest.fixture(autouse=True)
def _clear_gate_cache():
    # 抓住真函数：有的测试会把模块属性换成 lambda，teardown 时属性可能还没换回来
    gate = tencent._passes_akshare_gate
    gate.cache_clear()
    yield
    gate.cache_clear()


def test_a_symbol_with_week_trends_passes_without_probing(monkeypatch):
    calls = []
    monkeypatch.setattr(tencent, "_get_payload", _gate_responses([{"x": 1}], {}, calls))
    assert tencent._passes_akshare_gate("sz002875") is True
    assert calls == ["weekTrends"]


def test_beijing_codes_are_rejected_the_way_akshare_rejects_them(monkeypatch):
    """weekTrends 空、探测响应只有 qfqday 没有 day：AkShare 在这里 KeyError，我们也判不放行。

    这决定北交所继续由新浪服务（成交额精确到元），不会改判给腾讯（万元）。
    """
    calls = []
    probe = {"bj920021": {"qfqday": [["2026-09-04", "9.19"]], "qt": {}}}
    monkeypatch.setattr(tencent, "_get_payload", _gate_responses([], probe, calls))
    assert tencent._passes_akshare_gate("bj920021") is False
    assert calls == ["weekTrends", "get"]
    with pytest.raises(KeyError):
        tencent._history_frame("bj920021", "20260901", "20260904", "qfq")


def test_a_probe_with_a_day_series_passes(monkeypatch):
    probe = {"sh510300": {"day": [["2026-09-04", "4.1"]]}}
    monkeypatch.setattr(tencent, "_get_payload", _gate_responses([], probe, []))
    assert tencent._passes_akshare_gate("sh510300") is True


def test_a_probe_with_an_empty_day_series_is_rejected(monkeypatch):
    """AkShare 在这里取 ``["day"][0]`` 抛 IndexError，同样归为"不认这个代码"。"""
    probe = {"sz113707": {"day": [], "qt": {}}}
    monkeypatch.setattr(tencent, "_get_payload", _gate_responses([], probe, []))
    assert tencent._passes_akshare_gate("sz113707") is False


def test_a_non_jsonp_response_is_a_plain_failure_not_unsupported(monkeypatch):
    class _Response:
        text = "<html>502 Bad Gateway</html>"

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response())
    with pytest.raises(RuntimeError, match="不是 JSONP"):
        tencent._get_payload(tencent._KLINE_URL, {})


def test_an_unknown_code_raises_key_error_from_the_probe(monkeypatch):
    monkeypatch.setattr(tencent, "_get_payload", _gate_responses([], {}, []))
    with pytest.raises(KeyError):
        tencent._passes_akshare_gate("sz999999")


def test_the_gate_is_asked_once_per_symbol(monkeypatch):
    calls = []
    monkeypatch.setattr(tencent, "_get_payload", _gate_responses([{"x": 1}], {}, calls))
    tencent._passes_akshare_gate("sz002875")
    tencent._passes_akshare_gate("sz002875")
    assert calls == ["weekTrends"]
