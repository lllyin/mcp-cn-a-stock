"""同花顺日 K：年份文件取不到要抛、未上市年份要跳、盘中占位行要跳。

依据是 2026-09-07 的两件事：本机 2024、2025 两年文件返回 502 被当成"没上市"静默跳过，科创50
只剩 164 根、报告少了 240 日五行；部署机上科创50 2026 年文件盘中多一行
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
