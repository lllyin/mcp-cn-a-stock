"""页面里那张历史表空了的时候，自己在同一个页面里把它取回来。

## 这条路存在的理由

页面填历史表用的是 JSONP——``<script src="...fflow/daykline/get?cb=jQuery...">``。
东财会拒掉这种请求（浏览器侧是 ``net::ERR_EMPTY_RESPONSE``），却放行同一个 URL 的
XHR。2026-09-12 在同一个页面、同一时刻、用页面自己那个 URL 实测：页面的 JSONP 失败，
``fetch()`` 连续三次 HTTP 200、120 行；同期服务端直连该接口是 0/15。

所以这不是"重试"：重试同一条 JSONP 只会再被拒一次，页面自己已经试过了。

## 这里守什么

1. **字段顺序**。上游给的是「主力、小单、中单、大单、超大单」，``FundFlowRow`` 用的是
   「主力、超大单、大单、中单、小单」。照抄下标不报错、不缺字段，只是每一档都记错人，
   所以用 ``主力 == 超大单 + 大单`` 这条结构不变量守。
2. **失败不许扩散**。这是给空表补数的额外路径，它自己失败只能回到"没有历史"，
   不能把这次页面加载的其余成果带下水。
3. **secid 只有一套算法**。两处各拼一套正是线上那个 ``SH512480 -> 0.512480`` 的成因。
"""

import asyncio
import json
import logging
import re

import pytest

from finmcp.datasource import realtime_ff
from finmcp.datasource.fund_flow_page import rows_from_klines

# 2026-09-12 从 SH600519 实拉的两行，一个字没改。
REAL_KLINES = [
    "2026-09-10,-374051552.0,-3037.0,374054592.0,-149683648.0,-224367904.0,"
    "-15.40,-0.00,15.40,-6.16,-9.24,1285.13,-0.45,0,0",
    "2026-09-11,-188044816.0,-208537.0,188253344.0,-25435488.0,-162609328.0,"
    "-4.24,-0.00,4.25,-0.57,-3.67,1275.16,-0.78,0,0",
]


# --- 字段顺序 ---------------------------------------------------------------


def test_the_five_tiers_land_on_the_right_names():
    """上游顺序和 FundFlowRow 的顺序不同，错位了也不会报错——只会每一档都记错人。"""
    rows = rows_from_klines(REAL_KLINES)
    assert len(rows) == 2
    last = rows[-1]
    assert last.date == "2026-09-11"
    main, extra_large, large, medium, small = last.amounts
    assert main == pytest.approx(-188044816.0)
    assert extra_large == pytest.approx(-162609328.0)
    assert large == pytest.approx(-25435488.0)
    assert medium == pytest.approx(188253344.0)
    assert small == pytest.approx(-208537.0)


@pytest.mark.parametrize("index", [0, 1])
def test_the_structural_invariants_hold(index):
    """主力 = 超大单 + 大单，且五档净额合计约为零。错位会同时破坏这两条。"""
    row = rows_from_klines(REAL_KLINES)[index]
    main, extra_large, large, medium, small = row.amounts
    assert main == pytest.approx(extra_large + large, abs=1.0)
    assert main + medium + small == pytest.approx(0.0, abs=100.0)


def test_ratios_follow_the_same_order_as_amounts():
    row = rows_from_klines(REAL_KLINES)[-1]
    assert row.ratios == pytest.approx((-4.24, -3.67, -0.57, 4.25, -0.00))


def test_close_and_change_come_through():
    row = rows_from_klines(REAL_KLINES)[-1]
    assert row.close == pytest.approx(1275.16)
    assert row.pct_chg == pytest.approx(-0.78)


def test_the_record_matches_what_the_page_parser_produces():
    """下游 _build_fund_flow_history 按列名消费，两条路必须给出同一套键。"""
    record = rows_from_klines(REAL_KLINES)[-1].as_record()
    assert record["日期"] == "2026-09-11"
    assert record["主力净流入-净额"] == pytest.approx(-188044816.0)
    assert record["超大单净流入-净额"] == pytest.approx(-162609328.0)
    assert record["小单净流入-净占比"] == pytest.approx(-0.00)


def test_rows_are_in_date_order():
    rows = rows_from_klines(REAL_KLINES)
    assert [r.date for r in rows] == ["2026-09-10", "2026-09-11"]


# --- 坏数据 -----------------------------------------------------------------


@pytest.mark.parametrize("klines", [None, [], (), ["", ",,,", "只有一列"]])
def test_nothing_usable_gives_an_empty_list(klines):
    assert rows_from_klines(klines) == []


def test_a_short_row_is_skipped_not_padded():
    """少几列的行跳过而不是补零：一行坏数据混进历史表比少一行危险得多。"""
    rows = rows_from_klines(["2026-09-11,1,2,3", REAL_KLINES[1]])
    assert [r.date for r in rows] == ["2026-09-11"]
    assert rows[0].amounts[0] == pytest.approx(-188044816.0)  # 留下的是完整那行


def test_a_row_whose_amounts_are_all_unparseable_is_skipped():
    bad = "2026-09-11,-,-,-,-,-,-,-,-,-,-,-,-,0,0"
    assert rows_from_klines([bad]) == []


def test_a_partially_unparseable_row_keeps_none_not_zero():
    """"没有"和"0"是两回事，占位符不能变成 0。"""
    row = rows_from_klines([
        "2026-09-11,-188044816.0,-208537.0,188253344.0,-25435488.0,-162609328.0,"
        "-4.24,-0.00,4.25,-0.57,-3.67,-,-,0,0"
    ])[0]
    assert row.close is None and row.pct_chg is None


# --- secid ------------------------------------------------------------------


@pytest.mark.parametrize("symbol,secid", [
    ("SH600519", "1.600519"),
    ("SZ300408", "0.300408"),
    # 沪市 5 开头的基金：按纯代码猜市场会判成深市，线上因此整维丢过数据。
    ("SH512480", "1.512480"),
    ("SH588000", "1.588000"),
    ("SZ159995", "0.159995"),
    ("SZ000001", "0.000001"),
])
def test_secid_comes_from_the_one_shared_algorithm(symbol, secid):
    assert f"secid={secid}&" in realtime_ff._history_api_url(symbol)


@pytest.mark.parametrize("symbol", ["", "ABC", "沪深300"])
def test_a_symbol_without_digits_has_no_url(symbol):
    assert realtime_ff._history_api_url(symbol) is None


def test_the_url_asks_for_the_full_history():
    url = realtime_ff._history_api_url("SH600519")
    assert "lmt=0" in url and "klt=101" in url
    # 五档净额和净占比都要，缺了下游的列就填不满。
    for field in ("f51", "f55", "f61", "f63"):
        assert field in url


# --- 页面内取数 -------------------------------------------------------------


class _Page:
    """够用的假页面：evaluate 按脚本返回，或抛出。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.seen = []

    async def evaluate(self, script, arg=None):
        self.seen.append(arg)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _payload(klines):
    return {"data": {"code": "600519", "name": "贵州茅台", "klines": klines}}


def test_a_good_payload_becomes_rows():
    page = _Page(_payload(REAL_KLINES))
    rows = asyncio.run(realtime_ff._fetch_history_in_page(page, "SH600519"))
    assert [r.date for r in rows] == ["2026-09-10", "2026-09-11"]
    # 传进 JS 的就是这个标的的 URL，不是别人的。
    assert "secid=1.600519" in page.seen[0][0]


@pytest.mark.parametrize("outcome", [
    None,                               # fetch 里 catch 住了，返回 null
    {},                                 # 没有 data
    {"data": None},
    {"data": {}},                       # 没有 klines
    {"data": {"klines": None}},
    {"data": {"klines": []}},
    "不是字典",
    RuntimeError("注入：页面已经关了"),
])
def test_any_failure_degrades_to_no_history(outcome):
    """这条路自己失败只能回到"没有历史"，不能把整次页面加载带下水。"""
    page = _Page(outcome)
    assert asyncio.run(realtime_ff._fetch_history_in_page(page, "SH600519")) == []


def test_a_symbol_without_a_url_never_touches_the_page():
    page = _Page(_payload(REAL_KLINES))
    assert asyncio.run(realtime_ff._fetch_history_in_page(page, "ABC")) == []
    assert page.seen == []


def test_the_payload_is_json_serialisable_as_the_browser_would_send_it():
    """evaluate 的返回值要过一次 JSON 通道，别依赖只有 Python 才有的类型。"""
    page = _Page(json.loads(json.dumps(_payload(REAL_KLINES))))
    rows = asyncio.run(realtime_ff._fetch_history_in_page(page, "SH600519"))
    assert len(rows) == 2


# --- 接进一次真实加载 -------------------------------------------------------

from pathlib import Path                                         # noqa: E402

from finmcp.datasource.fund_flow_page import HISTORY_TABLE_ID    # noqa: E402

FULL_PAGE = Path(__file__).parent / "fixtures" / "eastmoney_zjlx_full_300408.html"


def _page_html(*, with_history: bool) -> str:
    html = FULL_PAGE.read_text(encoding="utf-8")
    if with_history:
        return html
    # 只清 id=table_ls 那张历史表的行，模拟"页面的 JSONP 被拒、表没填上"。
    # 不能无差别清空所有 <tbody>——今日块也在一个 tbody 里，一起清掉就不是
    # "历史缺了"而是"整页都空了"，测的就不是这里想测的东西。
    start = html.index(f'id="{HISTORY_TABLE_ID}"')
    body = html.index("<tbody>", start)
    end = html.index("</tbody>", body) + len("</tbody>")
    return html[:body] + "<tbody></tbody>" + html[end:]


class _LoadPage:
    """够用的假页面，带 evaluate。"""

    def __init__(self, html, evaluate_result):
        self.html = html
        self.evaluate_result = evaluate_result
        self.evaluated = 0
        self._listeners = {}

    async def goto(self, url, **_kw):
        pass

    async def reload(self, **_kw):
        pass

    async def content(self):
        return self.html

    async def evaluate(self, script, arg=None):
        self.evaluated += 1
        return self.evaluate_result

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self._listeners.get(event, []).remove(handler)


@pytest.fixture
def quiet_waits(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(realtime_ff, "_wait_for_today", noop)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", noop)


def test_an_empty_history_table_is_filled_from_the_page(quiet_waits):
    """页面的 JSONP 没填上表，我们在同一个页面里自己取回来。"""
    page = _LoadPage(_page_html(with_history=False), _payload(REAL_KLINES))
    parsed, refusal, blocks = asyncio.run(
        realtime_ff._load_once(page, "SZ300408", "https://x/", reload=False))

    assert refusal is None
    assert parsed is not None
    assert [r.date for r in parsed.history] == ["2026-09-10", "2026-09-11"]
    assert "history" not in blocks          # 补上了就不该再报"这块被拒"
    assert page.evaluated == 1


def test_a_filled_table_does_not_trigger_an_extra_fetch(quiet_waits):
    """页面自己填上了就别再多发一次请求——这条路只为补空表存在。"""
    page = _LoadPage(_page_html(with_history=True), _payload(REAL_KLINES))
    parsed, _, _ = asyncio.run(
        realtime_ff._load_once(page, "SZ300408", "https://x/", reload=False))

    assert len(parsed.history) == 121       # 页面原样解析出来的那些
    assert page.evaluated == 0


def test_a_failed_in_page_fetch_leaves_the_rest_of_the_load_intact(quiet_waits):
    """补历史失败，今日块和页头行情照样要交出去。"""
    page = _LoadPage(_page_html(with_history=False), None)
    parsed, refusal, _ = asyncio.run(
        realtime_ff._load_once(page, "SZ300408", "https://x/", reload=False))

    assert refusal is None
    assert parsed is not None and parsed.has_today
    assert parsed.history == []


def test_the_in_page_fetch_cannot_break_the_load(quiet_waits):
    """注入一次已知故障：页面内取数抛异常，这次加载仍须正常返回。"""
    page = _LoadPage(_page_html(with_history=False), RuntimeError("注入：页面没了"))
    parsed, refusal, _ = asyncio.run(
        realtime_ff._load_once(page, "SZ300408", "https://x/", reload=False))

    assert refusal is None and parsed is not None and parsed.has_today


# --- 失败必须留痕 -----------------------------------------------------------
#
# 2026-09-13 线上踩到：页面历史表空了 10 次，而这条补空表的路一条日志都没留，
# 于是查不出是"没进来补"还是"补了没补上"——两者排查方向完全相反。
# 原因是这个函数有三条静默 return 加一条只打 DEBUG 的。AGENTS §二：每一级失败
# 都要留下能定位到源的日志。
#
# 级别是 DEBUG：结果由调用方的 outcome=...history=N 在 INFO 记着，这里补的是
# **为什么**。main.py 把 finmcp 这个 logger 固定设成 DEBUG，所以照样会落盘。


class _EvalPage:
    """按给定值回应 page.evaluate；给异常就抛。"""

    def __init__(self, result):
        self.result = result
        self.evaluated = 0

    async def evaluate(self, *_args, **_kwargs):
        self.evaluated += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("symbol,result,marker", [
    ("ABC",       None,                          "拼不出 secid"),
    ("SH600519",  RuntimeError("注入：页面没了"), "失败"),
    ("SH600519",  None,                          "页面返回 null"),
    ("SH600519",  "一个字符串",                   "意料之外的类型"),
    ("SH600519",  {"rc": 1, "data": None},       "空表"),
    # 三种失败原因必须各自可辨：处置完全不同（换源 / 调预算 / 查 ut 和 secid）。
    ("SH600519",  {"__fail": "http", "status": 403, "ms": 12},       "HTTP 403"),
    ("SH600519",  {"__fail": "throw", "name": "TypeError",
                   "message": "Failed to fetch", "ms": 47},          "TypeError"),
    ("SH600519",  {"__fail": "throw", "name": "AbortError",
                   "message": "aborted", "ms": 8001},                "AbortError"),
])
def test_every_failure_path_says_why(symbol, result, marker, caplog):
    with caplog.at_level(logging.DEBUG, logger="finmcp"):
        rows = asyncio.run(
            realtime_ff._fetch_history_in_page(_EvalPage(result), symbol))
    assert rows == []
    assert marker in caplog.text, f"这条失败路径没留痕：{marker}"


def test_the_success_path_says_how_many(caplog):
    page = _EvalPage(_payload(REAL_KLINES))
    with caplog.at_level(logging.DEBUG, logger="finmcp"):
        rows = asyncio.run(realtime_ff._fetch_history_in_page(page, "SH600519"))
    assert rows
    assert "页面内取回历史资金流" in caplog.text and f"rows={len(rows)}" in caplog.text


def test_entering_the_rescue_branch_is_visible(quiet_waits, caplog):
    """光看 outcome=history=0 分不出"没进来补"和"补了没补上"，所以入口也要留一行。"""
    page = _LoadPage(_page_html(with_history=False), _payload(REAL_KLINES))
    with caplog.at_level(logging.DEBUG, logger="finmcp"):
        asyncio.run(realtime_ff._load_once(page, "SZ300408", "https://x/", reload=False))
    assert "页面历史表为空，尝试在页面内取回" in caplog.text


def test_a_filled_table_stays_silent(quiet_waits, caplog):
    """页面自己填上了就别喊——这条日志只在真的走了补救路径时才该出现。"""
    page = _LoadPage(_page_html(with_history=True), _payload(REAL_KLINES))
    with caplog.at_level(logging.DEBUG, logger="finmcp"):
        asyncio.run(realtime_ff._load_once(page, "SZ300408", "https://x/", reload=False))
    assert "页面历史表为空" not in caplog.text


def test_the_browser_side_script_always_returns_a_reason():
    """守那段 JS 的契约：失败路径必须带原因回来，不能 return null。

    上面那些用例都是直接喂 Python 侧结构、从不执行 JS，所以 JS 里悄悄改回
    ``return null`` 它们一条都发现不了——而 JS 恰恰是会静默退化的那半。这里退而
    求其次断言源码形态；**运行时行为在真浏览器里验过一次**（2026-09-13）：
    连不上的主机给 ``TypeError: Failed to fetch``、404 给 ``{__fail:'http',status:404}``、
    响应是 HTML 时给 ``SyntaxError: Unexpected token '<'``（滑块/拦截页就是这形状）。
    """
    import inspect

    source = inspect.getsource(realtime_ff._fetch_history_in_page)
    script = source.split('"""async ([u, ms]) => {', 1)[1].split('}"""', 1)[0]

    assert "__fail: 'http'" in script and "status: r.status" in script
    assert "__fail: 'throw'" in script and "e.name" in script
    assert "ms: Date.now() - t0" in script, "要带耗时，才分得出立刻被拒和耗到超时"
    # 失败路径一个 bare null 都不许留——那正是这次线上查不出原因的成因。
    assert "return null" not in script


def test_the_python_side_understands_every_reason_the_script_can_send():
    """JS 能发出的每一种 __fail，Python 侧都要有对应的日志分支。"""
    import inspect

    source = inspect.getsource(realtime_ff._fetch_history_in_page)
    script = source.split('"""async ([u, ms]) => {', 1)[1].split('}"""', 1)[0]
    kinds = set(re.findall(r"__fail: '([a-z]+)'", script))
    assert kinds == {"http", "throw"}, f"JS 多出了没人处理的失败种类：{kinds}"
