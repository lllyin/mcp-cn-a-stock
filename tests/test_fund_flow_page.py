"""东财资金流向页面解析。

fixture 是 2026-09-03 从 data.eastmoney.com/zjlx/300408.html 取下的真实
片段（121 行历史）。同一天线上实例通过接口渲染出的那张表也在下面，用来证明
页面兜底与接口主源渲染结果逐字一致。
"""

from io import StringIO
from pathlib import Path

import pandas as pd
import pytest

from qtf_mcp.datasource.cn_stock_source import CNStockDataSource
from qtf_mcp.datasource.fund_flow_page import (
    FundFlowPageError,
    HISTORY_COLUMNS,
    TODAY_FIELDS,
    parse_amount,
    parse_fund_flow_page,
    parse_percent,
    parse_price,
)
from qtf_mcp.research import build_historical_fund_flow_data

FIXTURE = Path(__file__).parent / "fixtures" / "eastmoney_zjlx_300408.html"

# 2026-09-03 线上实例（接口主源可用）渲染出的历史资金流向表，最新 15 行。
PUBLISHED_ROWS = """\
| 2026-09-03 | 110.91 | 2.82% | 1.59亿 | 3.37% | -4725.87万 | -1.00% | 2.06亿 | 4.37% | -8426.28万 | -1.79% | -7447.11万 | -1.58% |
| 2026-09-02 | 107.87 | -1.24% | -5.30亿 | -14.76% | -1.92亿 | -5.35% | -3.38亿 | -9.41% | 1.71亿 | 4.76% | 3.59亿 | 10.00% |
| 2026-09-01 | 109.22 | -5.03% | -3.22亿 | -7.38% | 1729.10万 | 0.40% | -3.40亿 | -7.77% | 5348.37万 | 1.22% | 2.69亿 | 6.15% |
| 2026-08-31 | 115.00 | 2.46% | -2.09亿 | -3.79% | 1.67亿 | 3.02% | -3.76亿 | -6.81% | -9537.97万 | -1.73% | 3.05亿 | 5.52% |
| 2026-08-28 | 112.24 | -0.21% | 5.20亿 | 7.33% | 2.26亿 | 3.19% | 2.94亿 | 4.14% | -2.20亿 | -3.10% | -3.00亿 | -4.23% |
| 2026-08-27 | 112.48 | 3.49% | 5.09亿 | 10.05% | 3.43亿 | 6.77% | 1.66亿 | 3.28% | -7694.56万 | -1.52% | -4.32亿 | -8.53% |
| 2026-08-26 | 108.69 | -0.38% | -1.08亿 | -3.57% | -1945.89万 | -0.64% | -8837.44万 | -2.93% | -1.85亿 | -6.14% | 2.93亿 | 9.70% |
| 2026-08-25 | 109.10 | 0.15% | -2.14亿 | -5.27% | -5037.28万 | -1.24% | -1.63亿 | -4.03% | 2.50亿 | 6.16% | -3609.66万 | -0.89% |
| 2026-08-24 | 108.94 | -4.35% | -9.31亿 | -16.65% | -2.40亿 | -4.30% | -6.91亿 | -12.35% | 2.63亿 | 4.70% | 6.69亿 | 11.96% |
| 2026-08-21 | 113.90 | 2.14% | 3422.53万 | 0.71% | -2273.08万 | -0.47% | 5695.60万 | 1.19% | 5103.86万 | 1.06% | -8526.39万 | -1.77% |
| 2026-08-20 | 111.51 | 1.74% | -2.34亿 | -4.44% | -1.85亿 | -3.51% | -4909.32万 | -0.93% | 3725.54万 | 0.71% | 1.97亿 | 3.74% |
| 2026-08-19 | 109.60 | -11.61% | -16.54亿 | -17.00% | -7.50亿 | -7.71% | -9.04亿 | -9.29% | 3.37亿 | 3.46% | 13.17亿 | 13.54% |
| 2026-08-18 | 123.99 | -4.73% | -5.50亿 | -5.91% | -5.39亿 | -5.79% | -1109.74万 | -0.12% | 3086.37万 | 0.33% | 5.19亿 | 5.58% |
| 2026-08-17 | 130.15 | 0.82% | -5.61亿 | -6.84% | -1.68亿 | -2.05% | -3.93亿 | -4.80% | -1790.96万 | -0.22% | 5.79亿 | 7.06% |
| 2026-08-14 | 129.09 | 0.54% | -3.69亿 | -5.46% | -2.05亿 | -3.03% | -1.64亿 | -2.43% | 5923.59万 | 0.88% | 3.10亿 | 4.58% |"""


@pytest.fixture(scope="module")
def page():
    return parse_fund_flow_page(FIXTURE.read_text(encoding="utf-8"))


# --- 数值解析 ---------------------------------------------------------------


class TestParseAmount:
    def test_units(self):
        assert parse_amount("1.59亿") == pytest.approx(1.59e8)
        assert parse_amount("-4725.87万") == pytest.approx(-47258700.0)
        assert parse_amount("1234元") == pytest.approx(1234.0)
        assert parse_amount("1.2万亿") == pytest.approx(1.2e12)

    def test_bare_number(self):
        assert parse_amount("159000000") == pytest.approx(1.59e8)

    def test_thousands_separator(self):
        assert parse_amount("1,234.56万") == pytest.approx(12345600.0)

    def test_placeholders_are_none_not_zero(self):
        """停牌或非交易时段是占位符。当成 0 会让报告声称净流入为零。"""
        for text in ("", "-", "--", "—", None, "  "):
            assert parse_amount(text) is None

    def test_garbage_is_none(self):
        assert parse_amount("abc亿") is None
        assert parse_amount("1.2.3万") is None

    def test_round_trip_is_lossless(self):
        """页面数值已按两位小数预格式化，报告也按两位小数渲染。"""

        def render(value):
            return (
                f"{value / 1e8:.2f}亿"
                if abs(value) >= 1e8
                else f"{value / 1e4:.2f}万"
            )

        for text in ("1.59亿", "-4725.87万", "9999.99万", "1.00亿", "-16.54亿"):
            assert render(parse_amount(text)) == text


class TestParsePercentAndPrice:
    def test_percent_keeps_the_percentage_number(self):
        """保留 3.37 而不是 0.0337：下游统一乘 0.01，兜底不能自带另一套量纲。"""
        assert parse_percent("3.37%") == pytest.approx(3.37)
        assert parse_percent("-11.61%") == pytest.approx(-11.61)
        assert parse_percent("0.00%") == pytest.approx(0.0)

    def test_percent_placeholders(self):
        assert parse_percent("--") is None
        assert parse_percent("") is None

    def test_price(self):
        assert parse_price("110.91") == pytest.approx(110.91)
        assert parse_price("-") is None


# --- 页面结构 ---------------------------------------------------------------


class TestPageStructure:
    def test_name_and_code_from_title(self, page):
        assert page.name == "三环集团"
        assert page.code == "300408"

    def test_history_row_count(self, page):
        assert len(page.history) == 121

    def test_history_is_chronological(self, page):
        dates = [row.date for row in page.history]
        assert dates == sorted(dates)
        assert page.history[-1].date == "2026-09-03"
        assert page.history[0].date == "2026-03-12"

    def test_units_are_yuan_and_percent(self, page):
        latest = page.history[-1]
        assert latest.close == pytest.approx(110.91)
        assert latest.pct_chg == pytest.approx(2.82)
        assert latest.amounts[0] == pytest.approx(1.59e8)
        assert latest.ratios[0] == pytest.approx(3.37)
        assert latest.amounts[1] == pytest.approx(-47258700.0)

    def test_five_tiers_per_row(self, page):
        for row in page.history:
            assert len(row.amounts) == 5
            assert len(row.ratios) == 5

    def test_records_use_akshare_column_names(self, page):
        """列名与 stock_individual_fund_flow 一致，兜底才能是 drop-in。"""
        record = page.history[-1].as_record()
        assert set(record) == set(HISTORY_COLUMNS)
        assert record["主力净流入-净额"] == pytest.approx(1.59e8)
        assert record["小单净流入-净占比"] == pytest.approx(-1.58)

    def test_today_absent_on_a_history_only_fragment(self, page):
        """fixture 只含历史表。today 必须是 None，不能是一串 0。"""
        assert page.today is None
        assert page.today_text == {}


class TestTodayBlock:
    """今日一栏。

    fixture 里没有这一块，所以用最小片段构造，选择器沿用 realtime_ff.py 已经
    依赖的 ``td[data-field="fNN"]``，两处指向同一张表。
    """

    def _html(self, values: dict) -> str:
        cells = "".join(
            f'<td data-field="{fid}">{text}</td>' for fid, text in values.items()
        )
        return f'<div class="title">三环集团(300408)资金流向</div><table><tr>{cells}</tr></table>'

    def test_parses_all_ten_fields(self):
        values = {
            "f62": "1.59亿",
            "f184": "3.37%",
            "f66": "-4725.87万",
            "f69": "-1.00%",
            "f72": "2.06亿",
            "f75": "4.37%",
            "f78": "-8426.28万",
            "f81": "-1.79%",
            "f84": "-7447.11万",
            "f87": "-1.58%",
        }
        page = parse_fund_flow_page(self._html(values))

        assert page.today["主力净流入-净额"] == pytest.approx(1.59e8)
        assert page.today["主力净流入-净占比"] == pytest.approx(3.37)
        assert page.today["小单净流入-净额"] == pytest.approx(-74471100.0)
        assert set(page.today) == set(TODAY_FIELDS.values())

    def test_keeps_raw_text_for_verbatim_output(self):
        """realtime_ff 目前把原文直接输出，保留原文才能不改现有渲染。"""
        page = parse_fund_flow_page(self._html({"f62": "1.59亿", "f184": "3.37%"}))
        assert page.today_text["f62"] == "1.59亿"

    def test_placeholders_stay_none(self):
        page = parse_fund_flow_page(self._html({"f62": "--", "f184": "3.37%"}))
        assert page.today["主力净流入-净额"] is None
        assert page.today["主力净流入-净占比"] == pytest.approx(3.37)


class TestDefensiveParsing:
    def test_empty_html_raises(self):
        with pytest.raises(FundFlowPageError, match="页面为空"):
            parse_fund_flow_page("")

    def test_page_without_data_raises(self):
        with pytest.raises(FundFlowPageError, match="既无今日数据也无历史表"):
            parse_fund_flow_page("<html><body><p>系统繁忙，请稍后再试</p></body></html>")

    def test_rows_of_unexpected_width_are_skipped(self):
        """宁可少一行，也不能按位错位把净占比当净额填进去。"""
        html = (
            '<div class="title">三环集团(300408)历史资金流向一览</div>'
            '<div id="table_ls"><table><tbody>'
            "<tr><td>暂无数据</td></tr>"
            "<tr><td>2026-09-03</td>"
            + "".join(
                f"<td><span class=\"red\">{v}</span></td>"
                for v in (
                    "110.91", "2.82%", "1.59亿", "3.37%", "-4725.87万", "-1.00%",
                    "2.06亿", "4.37%", "-8426.28万", "-1.79%", "-7447.11万", "-1.58%",
                )
            )
            + "</tr>"
            "<tr><td>2026-09-02</td><td>107.87</td><td>-1.24%</td></tr>"
            "</tbody></table></div>"
        )
        page = parse_fund_flow_page(html)

        assert [row.date for row in page.history] == ["2026-09-03"]

    def test_non_date_first_cell_is_skipped(self):
        html = (
            '<div id="table_ls"><table><tbody><tr>'
            + "".join(f"<td>{i}</td>" for i in range(13))
            + "</tr></tbody></table></div>"
            '<table><tr><td data-field="f62">1.00亿</td></tr></table>'
        )
        page = parse_fund_flow_page(html)

        assert page.history == []
        assert page.today["主力净流入-净额"] == pytest.approx(1.0e8)


# --- 与接口主源的渲染等价性 --------------------------------------------------


def test_page_renders_the_same_table_as_the_api_source(page):
    """页面兜底与接口主源渲染出的历史表逐字一致。

    走的是完整链路：解析页面 -> AkShare 同列名的 DataFrame ->
    _build_fund_flow_history -> build_historical_fund_flow_data。兜底与主源
    共用同一条转换和渲染逻辑，所以这里比对的是端到端结果。
    """
    frame = pd.DataFrame(page.history_records())
    dataset = CNStockDataSource()._build_fund_flow_history(
        frame, "SZ300408", is_market=False
    )
    assert dataset is not None

    fp = StringIO()
    build_historical_fund_flow_data(fp, {"_DS_FUND_FLOW": dataset}, limit=15)
    rendered = [
        line.strip()
        for line in fp.getvalue().splitlines()
        if line.startswith("| 20")
    ]

    assert rendered == PUBLISHED_ROWS.splitlines()


# --- 与合并前 PARSE_JS 的输出等价性 ------------------------------------------

FULL_PAGE = Path(__file__).parent / "fixtures" / "eastmoney_zjlx_full_300408.html"


def _legacy_realtime_dict(html: str, symbol: str) -> dict:
    """按合并前 PARSE_JS + to_ratio 的规则算一遍，作为等价性基准。

    合并前是在浏览器里 evaluate 取 10 个 ``td[data-field]`` 的 innerText，占位符
    回落成 "0"，占比用 float(去掉%) 且失败算 0.0，名称取第一个 .title 的原文。
    """
    import re

    from qtf_mcp.datasource.realtime_ff import get_fund_flow_display_name

    def get(field_id: str) -> str:
        m = re.search(
            rf'<td[^>]*data-field="{field_id}"[^>]*>(.*?)</td>', html, re.S
        )
        txt = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
        return txt if txt and txt not in ("-", "--") else "0"

    def to_ratio(value: str) -> float:
        try:
            return float(str(value).replace("%", ""))
        except Exception:
            return 0.0

    title = re.search(r'<div[^>]*class=.title.[^>]*>(.*?)</div>', html, re.S)
    name = re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else ""

    return {
        "标的名称": get_fund_flow_display_name(symbol, name),
        "主力净流入": get("f62"),
        "主力净比(%)": to_ratio(get("f184")),
        "超大单净流入": get("f66"),
        "超大单净比(%)": to_ratio(get("f69")),
        "大单净流入": get("f72"),
        "大单净比(%)": to_ratio(get("f75")),
        "中单净流入": get("f78"),
        "中单净比(%)": to_ratio(get("f81")),
        "小单净流入": get("f84"),
        "小单净比(%)": to_ratio(get("f87")),
    }


def test_merged_load_reproduces_the_legacy_realtime_dict():
    """合并成一次加载后，今日资金流的返回结构必须逐字不变。

    fixture 是 2026-09-03 抓下的完整页面（今日块 + 121 行历史）。
    """
    from qtf_mcp.datasource.realtime_ff import _page_to_realtime_dict

    html = FULL_PAGE.read_text(encoding="utf-8")
    parsed = parse_fund_flow_page(html)

    assert _page_to_realtime_dict("SZ300408", parsed) == _legacy_realtime_dict(
        html, "SZ300408"
    )


def test_merged_load_yields_both_blocks_from_one_page():
    """同一份 HTML 同时产出今日和历史，这是合并的全部意义。"""
    parsed = parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))

    assert parsed.today is not None
    assert len(parsed.history) == 121
    assert parsed.title_text == "三环集团(300408)"


def test_placeholders_fall_back_to_zero_like_the_legacy_script():
    """停牌时页面是 -- ，合并前会输出 "0"，合并后必须一样。"""
    from qtf_mcp.datasource.realtime_ff import _page_to_realtime_dict

    cells = "".join(
        f'<td data-field="{fid}">--</td>' for fid in TODAY_FIELDS
    )
    html = f'<div class="title">测试股(000001)</div><table><tr>{cells}</tr></table>'
    parsed = parse_fund_flow_page(html)

    result = _page_to_realtime_dict("SZ000001", parsed)

    assert result["主力净流入"] == "0"
    assert result["主力净比(%)"] == 0.0


# --- 两个调用方必须落到同一个页面和同一次加载 --------------------------------


class TestPageIdentityAcrossCallers:
    """实时路径给纯代码、资金流兜底给带前缀的规范代码。

    2026-09-04 09:22 的日志里，同一次请求把同一个页面加载了两次：
    ``symbol=300408`` 拿到 ``history=120``，而 ``symbol=SZ300408`` 是
    ``outcome=error`` —— 后者拼出的是 /zjlx/SZ300408.html，一个不存在的页面。
    """

    def test_prefixed_symbol_resolves_to_the_same_url(self):
        from qtf_mcp.datasource.realtime_ff import get_fund_flow_url

        expected = "https://data.eastmoney.com/zjlx/300408.html"
        assert get_fund_flow_url("300408") == expected
        assert get_fund_flow_url("SZ300408") == expected
        assert get_fund_flow_url("SH600547") == (
            "https://data.eastmoney.com/zjlx/600547.html"
        )

    def test_both_callers_share_one_singleflight_key(self):
        from qtf_mcp.datasource.realtime_ff import page_key

        assert page_key("300408") == page_key("SZ300408") == "300408"
        assert page_key("600547") == page_key("SH600547") == "600547"

    def test_index_pages_are_unchanged(self):
        from qtf_mcp.datasource.realtime_ff import get_fund_flow_url, page_key

        assert get_fund_flow_url("000001") == (
            "https://data.eastmoney.com/zjlx/zs000001.html"
        )
        assert get_fund_flow_url("dpzjlx") == (
            "https://data.eastmoney.com/zjlx/dpzjlx.html"
        )
        assert page_key("dpzjlx") == "dpzjlx"

    @pytest.mark.asyncio
    async def test_one_load_serves_both_key_spellings(self, monkeypatch):
        import asyncio

        from qtf_mcp.datasource import realtime_ff

        loads = []

        async def fake_load(symbol, **kwargs):
            loads.append(symbol)
            await asyncio.sleep(0.02)
            return parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))

        monkeypatch.setattr(realtime_ff, "_load_page_shared", fake_load)
        realtime_ff._page_inflight.clear()

        first, second = await asyncio.gather(
            realtime_ff.fetch_page_shared("300408"),
            realtime_ff.fetch_page_shared("SZ300408"),
        )

        assert len(loads) == 1
        assert first is second
        assert len(first.history) == 121


class TestRefusalSignals:
    """哪些请求失败才算"这块数据取不到"。

    2026-09-04 09:49 实测：push2/api/qt/stock/get 失败的同时，今日一栏照样填出
    9443.9402万，因为今日只依赖 fflow/kline/get。把 qt/stock/get 当成失败信号，
    会在它失败时提前放弃等待，让今日一栏变成一串 0 —— 而历史表不受影响，于是
    出现"历史有值、实时没值"这种不可能的组合。
    """

    def test_quote_endpoint_is_not_a_today_signal(self):
        from qtf_mcp.datasource.realtime_ff import (
            HISTORY_ENDPOINTS,
            TODAY_ENDPOINTS,
        )

        quote = "https://push2.eastmoney.com/api/qt/stock/get?cb=quotedelaytip0"
        assert not any(part in quote for part in TODAY_ENDPOINTS)
        assert not any(part in quote for part in HISTORY_ENDPOINTS)

    def test_each_block_watches_only_its_own_endpoint(self):
        from qtf_mcp.datasource.realtime_ff import (
            HISTORY_ENDPOINTS,
            TODAY_ENDPOINTS,
        )

        today = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?cb=x"
        history = (
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?cb=x"
        )

        assert any(part in today for part in TODAY_ENDPOINTS)
        assert not any(part in today for part in HISTORY_ENDPOINTS)
        assert any(part in history for part in HISTORY_ENDPOINTS)
        assert not any(part in history for part in TODAY_ENDPOINTS)


class TestLoadRetry:
    """第一次加载被拒时重试一次。

    实测形状：新 context 的第 1 次加载两个端点全被拒，第 2、3 次全部成功
    （2026-09-03 是 [0, 121, 121] 行，2026-09-04 复测一致）。不重试的话，
    进程起来后的第一次请求必然拿不到资金流向。
    """

    @pytest.mark.asyncio
    async def test_retries_after_a_refusal(self, monkeypatch):
        from qtf_mcp.datasource import realtime_ff

        attempts = []
        good = parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))

        async def flaky(symbol, context, *, loads=1, satisfies=None):
            attempts.append(loads)
            if len(attempts) == 1:
                raise realtime_ff.FundFlowPageBlocked("cold")
            return good

        monkeypatch.setattr(realtime_ff, "load_fund_flow_page", flaky)
        monkeypatch.setattr(realtime_ff, "get_context", _fake_context)
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 4)

        page = await realtime_ff._load_page_shared("300408")

        # 预算 4 次加载 = 两个 tab，每个 tab 拿到 2 次（goto + reload）
        assert attempts == [2, 2]
        assert len(page.history) == 121

async def _fake_context():
    return object()


class TestEmptyTodayBlockIsNotSuccess:
    """十档全是占位符时，has_today 必须是 False。

    2026-09-04 10:06 的日志：outcome=today=True history=0，两次加载都这样，
    报告里今日一栏全是 0。因为占位符以 None 存进字典，字典非空就被当成有数据，
    于是既不抛 FundFlowPageBlocked、也就不会触发重试和熔断。

    解析器本身不抛错：停牌和开盘前同样是占位符，那是正常状态，老逻辑输出 0。
    """

    def _html(self, values: dict) -> str:
        cells = "".join(
            f'<td data-field="{fid}">{text}</td>' for fid, text in values.items()
        )
        return (
            '<div class="title">三环集团(300408)</div>'
            f"<table><tr>{cells}</tr></table>"
        )

    def test_all_placeholders_have_no_today_data(self):
        page = parse_fund_flow_page(self._html({fid: "--" for fid in TODAY_FIELDS}))

        assert page.today is not None      # 字典还在，供逐字输出用
        assert page.has_today is False     # 但没有任何值

    def test_empty_cells_have_no_today_data(self):
        page = parse_fund_flow_page(self._html({fid: "" for fid in TODAY_FIELDS}))

        assert page.has_today is False

    def test_one_real_value_counts_as_data(self):
        """部分档位缺失是正常的，不能因为有 None 就整块丢掉。"""
        values = {fid: "--" for fid in TODAY_FIELDS}
        values["f62"] = "1.59亿"

        page = parse_fund_flow_page(self._html(values))

        assert page.has_today is True
        assert page.today["主力净流入-净额"] == pytest.approx(1.59e8)
        assert page.today["超大单净流入-净额"] is None


class TestPageReuseRespectsWhatTheCallerNeeds:
    """复用不能把一次残缺的加载固化下来。

    2026-09-04 10:48：实时路径拿到 today=True history=0，兜底复用了这份结果，
    报告于是有实时资金流、没有历史资金流。两块由不同端点填充、会独立失败，而
    那些端点是间歇性可用的，隔几秒重新加载相当有机会拿到。
    """

    def _page(self, *, history: int, today: bool):
        from qtf_mcp.datasource.fund_flow_page import FundFlowRow

        full = parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))
        rows = full.history[:history] if history else []
        return type(full)(
            name=full.name,
            code=full.code,
            title_text=full.title_text,
            today=full.today if today else None,
            today_text=full.today_text if today else {},
            history=list(rows) if rows else [],
        )

    def _seed(self, page):
        from qtf_mcp.datasource import realtime_ff

        realtime_ff._page_cache.clear()
        realtime_ff._remember_page("300408", page)
        return realtime_ff

    def test_a_history_less_page_is_not_reused_for_history(self):
        module = self._seed(self._page(history=0, today=True))

        assert module._cached_page(
            "300408", require_history=True, require_today=False
        ) is None

    def test_the_same_page_is_still_reused_for_today(self):
        module = self._seed(self._page(history=0, today=True))

        assert module._cached_page(
            "300408", require_history=False, require_today=True
        ) is not None

    def test_a_today_less_page_is_not_reused_for_today(self):
        module = self._seed(self._page(history=5, today=False))

        assert module._cached_page(
            "300408", require_history=False, require_today=True
        ) is None

    def test_a_complete_page_serves_both(self):
        module = self._seed(self._page(history=5, today=True))

        assert module._cached_page(
            "300408", require_history=True, require_today=False
        ) is not None
        assert module._cached_page(
            "300408", require_history=False, require_today=True
        ) is not None

    def test_reuse_expires(self, monkeypatch):
        from qtf_mcp.datasource import realtime_ff

        module = self._seed(self._page(history=5, today=True))
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_REUSE_SECONDS", 0.0)

        assert module._cached_page(
            "300408", require_history=False, require_today=False
        ) is None


class TestLoadAttempts:
    """本进程还没取到过数据时多试一次。

    重试条件是"还不满足调用方"，不只是"被拒"：一次加载可能只拿回两块中的一块，
    而调用方要的恰好是另一块。次数上限见 FUND_FLOW_PAGE_MAX_LOADS——8 轮实测
    第三次不再带来成功，所以默认只有两次。
    """

    def _pages(self):
        full = parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))
        partial = type(full)(
            name=full.name, code=full.code, title_text=full.title_text,
            today=full.today, today_text=full.today_text, history=[],
        )
        return partial, full

    @pytest.mark.asyncio
    async def test_retries_until_the_requirement_is_met(self, monkeypatch):
        """第二次只拿到今日，要历史的调用方应该继续试。"""
        from qtf_mcp.datasource import realtime_ff

        partial, full = self._pages()
        results = [partial, full]
        seen = []

        async def flaky(symbol, context, *, loads=1, satisfies=None):
            item = results[len(seen)]
            seen.append(item)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(realtime_ff, "load_fund_flow_page", flaky)
        monkeypatch.setattr(realtime_ff, "get_context", _fake_context)
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 3)

        page = await realtime_ff._load_page_shared("300408", require_history=True)

        # 预算 3 = 第一个 tab 2 次 + 第二个 tab 1 次
        assert len(seen) == 2
        assert len(page.history) == 121

    @pytest.mark.asyncio
    async def test_stops_at_the_configured_ceiling(self, monkeypatch):
        """次数用完就返回手上的结果，不无限试。"""
        from qtf_mcp.datasource import realtime_ff

        partial, _ = self._pages()
        seen = []

        async def always_partial(symbol, context, *, loads=1, satisfies=None):
            seen.append(symbol)
            return partial

        monkeypatch.setattr(realtime_ff, "load_fund_flow_page", always_partial)
        monkeypatch.setattr(realtime_ff, "get_context", _fake_context)
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 3)

        page = await realtime_ff._load_page_shared("300408", require_history=True)

        assert len(seen) == 2       # 3 次加载预算 -> 2 个 tab
        assert page.history == []

    @pytest.mark.asyncio
    async def test_a_satisfied_first_attempt_does_not_retry(self, monkeypatch):
        from qtf_mcp.datasource import realtime_ff

        _, full = self._pages()
        seen = []

        async def good(symbol, context, *, loads=1, satisfies=None):
            seen.append(symbol)
            return full

        monkeypatch.setattr(realtime_ff, "load_fund_flow_page", good)
        monkeypatch.setattr(realtime_ff, "get_context", _fake_context)
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 3)

        await realtime_ff._load_page_shared("300408", require_history=True)

        assert len(seen) == 1

# --- 风控滑块痕迹 -----------------------------------------------------------
# 只用来解释"已经失败了"，绝不用来判定失败，所以这里同时钉住"正常页面不误报"。


def test_captcha_marker_is_detected():
    """被拒页面上滑块模态框的 iframe 会被认出来。"""
    html = (
        '<div class="title">三环集团(300408)</div>'
        '<div class="popwscps_d"><div class="popwscps_d_shadow"></div>'
        '<iframe class="popwscps_d_iframe" '
        'src="https://i.eastmoney.com/websitecaptcha/slidervalid"></iframe></div>'
        '<td data-field="f62"></td>'
    )
    page = parse_fund_flow_page(html)

    assert page.captcha_present is True
    assert page.has_today is False


def test_captcha_library_alone_is_not_a_marker():
    """popwscpc.js 是库，正常页面也会加载，不能当作被拦截的证据。"""
    html = (
        '<div class="title">三环集团(300408)</div>'
        '<script src="https://i.eastmoney.com/websitecaptcha/build/popwscpc.js">'
        "</script>"
        '<td data-field="f62">1.59亿</td>'
    )
    page = parse_fund_flow_page(html)

    assert page.captcha_present is False
    assert page.has_today is True


def test_captured_page_has_no_captcha_marker():
    """真实成功抓取的页面不该被判成有滑块。"""
    for name in ("eastmoney_zjlx_300408.html", "eastmoney_zjlx_full_300408.html"):
        fixture = Path(__file__).parent / "fixtures" / name
        assert parse_fund_flow_page(fixture.read_text(encoding="utf-8")).captcha_present is False
