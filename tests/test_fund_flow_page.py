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
