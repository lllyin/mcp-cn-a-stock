"""scripts/verify_release.py 自己的逻辑。

这层是给别的改动兜底的，它自己判错了比没有更糟——会给出"一致"的假保证。所以
解析、钉日期、归并、降级这几处都要有测试。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "verify_release", Path(__file__).resolve().parents[1] / "scripts" / "verify_release.py"
)
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_release"] = verify
_SPEC.loader.exec_module(verify)


def _archive(command: str, body: str, captured: str = "2026-08-21 20:02:48") -> str:
    return "\n".join(
        [
            "# mcporter call cn-stock brief",
            "",
            f"- 查询时间：{captured}",
            "- 状态：SUCCESS",
            f"- 命令：export MCPORTER_CONFIG=/root/x.json && mcporter call cn-stock {command}",
            "- 退出码：0",
            "",
            "## 原始返回",
            "",
            body,
            "",
        ]
    )


def _index_report(fund_flow: str = "- 沪深两市主力净流入: 106.6445亿  主力净占比: 0.33%") -> str:
    """一份形状完整的指数 brief。指数没有市值、市盈率和换手率，本来就该缺。"""
    return "\n".join(
        [
            "# 基本数据",
            "",
            "- 股票代码: SH000001",
            "- 股票名称: 上证指数",
            "- 数据日期: 2026-06-12",
            "",
            "# 交易数据",
            "",
            "## 价格",
            "- 当日: 4031.510 开盘: 4017.860 最高: 4060.270 最低: 4008.180",
            "",
            "## 涨跌幅",
            "- 当日: 0.35%",
            "",
            "## 振幅",
            "- 当日: 1.29%",
            "",
            "## 成交量(万手)",
            "- 当日: 51677.75",
            "",
            "## 成交额(亿)",
            "- 当日: 7284.06",
            "",
            "## 资金流向",
            fund_flow,
            "",
        ]
    )


class TestArchiveParsing:
    def test_the_command_line_wins_over_the_title(self, tmp_path):
        """标题也长得像调用，但它不带参数——匹配上去会得到一个没有 symbol 的空调用。"""
        path = tmp_path / "a.md"
        path.write_text(
            _archive("brief symbol=SH600519 date=2026-08-20", "{}"), encoding="utf-8"
        )
        baseline = verify.load_baseline(path)
        assert baseline.spec.tool == "brief"
        assert baseline.spec.args == {"symbol": "SH600519", "date": "2026-08-20"}

    def test_a_file_without_a_command_is_not_a_baseline(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text("# 随便什么\n\n## 原始返回\n\n{}\n", encoding="utf-8")
        assert verify.load_baseline(path) is None

    def test_truncated_json_is_skipped_rather_than_compared(self, tmp_path):
        """归档按大小截断过。当文本比对只会得到满屏噪音。"""
        path = tmp_path / "a.md"
        path.write_text(
            _archive("tech symbol=SH600519 days=30", '{"reports": {"SH600519": {"a": 1'),
            encoding="utf-8",
        )
        baseline = verify.load_baseline(path)
        assert baseline.replay_spec is None
        assert "截断" in baseline.skip_reason


class TestDatePinning:
    def test_a_command_without_a_date_is_pinned_from_the_report(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text(
            _archive(
                "brief symbol=SH600519",
                '{"reports": {"SH600519": "# 基本数据\\n- 数据日期: 2026-08-20\\n"}}',
            ),
            encoding="utf-8",
        )
        baseline = verify.load_baseline(path)
        assert baseline.replay_spec.args["date"] == "2026-08-20"
        assert baseline.date_added

    def test_conflicting_report_dates_cannot_be_pinned(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text(
            _archive(
                "brief symbol=SH600519,SZ000333",
                '{"reports": {"SH600519": "- 数据日期: 2026-08-20\\n",'
                ' "SZ000333": "- 数据日期: 2026-08-19\\n"}}',
            ),
            encoding="utf-8",
        )
        baseline = verify.load_baseline(path)
        assert baseline.replay_spec is None
        assert "不唯一" in baseline.skip_reason

    def test_market_breadth_is_never_replayable(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text(_archive("market_breadth ", '{"up_count": 1}'), encoding="utf-8")
        baseline = verify.load_baseline(path)
        assert baseline.replay_spec is None

    def test_an_intraday_capture_is_flagged(self, tmp_path):
        """盘中抓的归档，当日 bar 还没定盘，数值差异说明不了任何事。"""
        path = tmp_path / "a.md"
        path.write_text(
            _archive(
                "brief symbol=SH600519",
                '{"reports": {"SH600519": "- 数据日期: 2026-06-15\\n"}}',
                captured="2026-06-15 10:35:35",
            ),
            encoding="utf-8",
        )
        assert verify.load_baseline(path).intraday_capture

    def test_a_capture_after_the_close_is_not_flagged(self, tmp_path):
        path = tmp_path / "a.md"
        path.write_text(
            _archive(
                "brief symbol=SH600519",
                '{"reports": {"SH600519": "- 数据日期: 2026-06-15\\n"}}',
                captured="2026-06-15 15:33:15",
            ),
            encoding="utf-8",
        )
        assert not verify.load_baseline(path).intraday_capture


class TestDocumentDiff:
    def test_identical_documents_are_clean(self):
        text = "# 基本数据\n- 总市值: 1亿\n"
        assert verify.compare_documents(text, text, "x").clean

    def test_same_named_lines_in_different_sections_stay_apart(self):
        """`- 当日` 在 brief 里出现六次，不带段落名会被并成一条，旧新都取第一个。"""
        old = "## 价格\n- 当日: 10.0\n## 振幅\n- 当日: 1.0%\n"
        new = "## 价格\n- 当日: 10.0\n## 振幅\n- 当日: 2.0%\n"
        diff = verify.compare_documents(old, new, "x")
        assert [(d.kind, d.key, d.old, d.new) for d in diff.diffs] == [
            ("值变化", "振幅 › - 当日", "1.0%", "2.0%")
        ]

    def test_a_dropped_line_is_a_miss_not_a_change(self):
        diff = verify.compare_documents("- 总市值: 1亿\n- 市净率: 2\n", "- 市净率: 2\n", "x")
        assert [(d.kind, d.key) for d in diff.diffs] == [("缺失", "- 总市值")]

    def test_the_live_fund_flow_block_is_dropped_when_we_pinned_the_date(self):
        """归档是实时查的，重放是钉日期的，这一段必然对不上——是脚本自己造成的。"""
        old = "## 资金流向\n- 今日主力净流入: 1.85亿  主力净占比: 2.99%\n"
        new = "## 资金流向\n- 指定日期查询暂不展示实时资金流向\n"
        assert verify.compare_documents(old, new, "x", drop_live_only=True).clean
        assert not verify.compare_documents(old, new, "x").clean

    def test_market_cap_value_changes_are_demoted_not_hidden(self):
        """市值口径是"此刻"，钉哪一天都跟着今天走；但整段消失仍然是问题。

        判据是"归档不是今天抓的"（``demote_live_values``），不是"脚本补了日期"。
        第一版按后者判，于是归档命令自带 date= 的那几份不触发降级，08-21 抓的市值
        拿去和今天的比，刷出十几行假差异。
        """
        old = "# 基本数据\n- 总市值: 2831.54亿\n"
        new = "# 基本数据\n- 总市值: 2617.93亿\n"
        diff = verify.compare_documents(old, new, "x", demote_live_values=True)
        assert [d.kind for d in diff.diffs] == ["实时口径"]
        assert diff.hard == []

        # 归档就是今天抓的话，市值本该一致，变了就是真差异
        same_day = verify.compare_documents(old, new, "x", demote_live_values=False)
        assert [d.kind for d in same_day.hard] == ["值变化"]

        gone = verify.compare_documents(old, "# 基本数据\n", "x", demote_live_values=True)
        assert [d.kind for d in gone.hard] == ["缺失"]

    def test_live_staleness_is_decided_by_the_capture_day(self, tmp_path):
        import datetime as dt

        today = dt.date.today().isoformat()
        path = tmp_path / "a.md"
        path.write_text(
            _archive("brief symbol=SH600519 date=2026-08-20", "{}",
                     captured=f"{today} 20:02:48"),
            encoding="utf-8",
        )
        assert not verify.load_baseline(path).live_stale

        path.write_text(
            _archive("brief symbol=SH600519 date=2026-08-20", "{}",
                     captured="2026-08-21 20:02:48"),
            encoding="utf-8",
        )
        assert verify.load_baseline(path).live_stale


def _price_report(scale: float = 1.0, pct: str = "8.59%") -> str:
    """一份只含价格与比值的报告。scale 模拟前复权基准变化。"""
    rows = [(35.400, 34.230, 35.860), (31.610, 0, 35.860), (29.322, 0, 35.860),
            (27.128, 0, 35.860), (32.962, 0, 53.320), (36.361, 0, 65.370)]
    lines = ["## 价格"]
    for index, (close, _open, high) in enumerate(rows):
        label = "- 当日" if index == 0 else f"- {[5, 20, 60, 120, 240][index - 1]}日均价"
        lines.append(f"{label}: {close * scale:.3f} 最高: {high * scale:.3f}")
    lines += ["## 涨跌幅", f"- 当日: {pct}"]
    return "\n".join(lines) + "\n"


class TestAdjustmentDrift:
    def test_a_split_is_named_with_its_ratio(self):
        """1:2 拆分会让价格整段减半，一份 brief 能刷出三十行"不一致"。"""
        diff = verify.compare_documents(_price_report(), _price_report(0.5), "x")
        assert "复权基准变化" in diff.drift_note
        assert "1:2 拆分" in diff.drift_note
        assert [d.kind for d in diff.diffs] == ["复权漂移"] * 6
        assert diff.hard == []

    def test_a_one_to_three_split_is_recognised_too(self):
        diff = verify.compare_documents(_price_report(), _price_report(1 / 3), "x")
        assert "1:3 拆分" in diff.drift_note

    def test_rounding_noise_in_derived_ratios_rides_along(self):
        """价格缩小之后小数位不够了，算出来的百分比会在末位抖一下。"""
        diff = verify.compare_documents(
            _price_report(pct="8.59%"), _price_report(0.5, pct="8.62%"), "x"
        )
        assert diff.hard == []
        assert "精度损失" in diff.drift_note

    def test_a_real_change_in_a_ratio_still_surfaces(self):
        """折叠只到末位为止：涨跌幅真的变了 3 个点，还得报出来。"""
        diff = verify.compare_documents(
            _price_report(pct="8.59%"), _price_report(0.5, pct="11.59%"), "x"
        )
        assert [(d.kind, d.key) for d in diff.hard] == [("值变化", "涨跌幅 › - 当日")]

    def test_prices_that_did_not_scale_are_not_excused(self):
        """只有一行价格变了，不成比例，不能算复权。"""
        old = "## 价格\n- 当日: 10.000 最高: 11.000\n- 5日均价: 9.000 最高: 11.000\n"
        new = "## 价格\n- 当日: 99.000 最高: 11.000\n- 5日均价: 9.000 最高: 11.000\n"
        diff = verify.compare_documents(old, new, "x")
        assert [d.kind for d in diff.hard] == ["值变化"]


class TestScore:
    def test_the_overall_score_is_the_weakest_link(self):
        """平均会把"一个源整层挂了"稀释成看着还行的分数。"""
        score = verify.Score(
            tools_ok=9, tools_total=9, dims_ok=77, dims_total=100,
            baselines_ok=1, baselines_total=7,
        )
        assert score.tool_rate == 100.0
        assert round(score.overall) == 14
        assert score.verdict.startswith("❌")

    def test_a_clean_run_is_publishable(self):
        score = verify.Score(9, 9, 100, 100, 7, 7)
        assert score.overall == 100.0
        assert score.verdict.startswith("✅")

    def test_a_small_degradation_asks_for_confirmation(self):
        score = verify.Score(9, 9, 95, 100, 7, 7)
        assert score.verdict.startswith("⚠️")

    def test_nothing_measured_does_not_divide_by_zero(self):
        assert verify.Score().overall == 100.0


class TestStructureDiff:
    def test_a_new_field_across_an_array_collapses_to_one_row(self):
        old = {"events": [{"code": "A"}, {"code": "B"}, {"code": "C"}]}
        new = {"events": [{"code": c, "forecast_type": None} for c in "ABC"]}
        diff = verify.compare_structures(old, new, "x")
        assert [(d.kind, d.key) for d in diff.diffs] == [
            ("新增", "events[].forecast_type ×3")
        ]

    def test_a_single_field_keeps_its_exact_path(self):
        diff = verify.compare_structures({"a": 1}, {"a": 1, "revision_safe": True}, "x")
        assert [(d.kind, d.key) for d in diff.diffs] == [("新增", "revision_safe")]

    def test_the_call_timestamp_is_not_a_difference(self):
        old = {"timestamp": "2026-08-21 02:14:40", "up_count": 1}
        new = {"timestamp": "2026-09-04 17:52:30", "up_count": 1}
        assert verify.compare_structures(old, new, "x").clean


class TestDriftNote:
    def test_a_consistent_ratio_across_many_lines_is_called_out(self):
        """分红除权改了前复权基准，历史每一行都动，但那不是取数错了。"""
        old = "\n".join(f"- d{i}: {100 + i}.00" for i in range(10))
        new = "\n".join(f"- d{i}: {(100 + i) * 0.9:.2f}" for i in range(10))
        diff = verify.compare_documents(old, new, "x")
        assert "等比漂移" in diff.drift_note

    def test_unrelated_changes_get_no_drift_note(self):
        old = "\n".join(f"- d{i}: {100 + i}.00" for i in range(10))
        new = "\n".join(f"- d{i}: {100 + i * 7}.00" for i in range(10))
        assert verify.compare_documents(old, new, "x").drift_note == ""


class TestCompleteness:
    def test_a_missing_dimension_names_its_upstream_source(self):
        payload = verify.Payload(documents={"SH600519": "# 基本数据\n- 股票代码: SH600519\n"})
        result = verify.check_completeness("brief", payload)
        sources = {item.dimension.name: item.dimension.source for item in result.findings}
        assert sources["总市值"] == "realtime"
        assert sources["换手率"] == "realtime(流通市值)"
        assert result.available < result.graded

    def test_indices_are_not_faulted_for_dimensions_they_never_have(self):
        result = verify.check_completeness("brief", verify.Payload({"SH000001": _index_report()}))
        assert result.findings == []
        assert result.available == result.graded == result.expected

    def test_a_section_that_only_says_it_has_no_data_counts_as_degraded(self):
        """段落标题在、内容是一句"没有"——只查标题会把降级当成正常。"""
        document = _index_report(fund_flow="- 盘中实时数据暂时不可用")
        result = verify.check_completeness("brief", verify.Payload({"SH000001": document}))
        assert [(m.dimension.name, m.degraded_note) for m in result.findings] == [
            ("资金流向", "盘中回退整层被跳过或熔断")
        ]
        assert result.bad and result.findings[0].verdict.startswith("⚠️")

    def test_an_empty_section_is_also_degraded(self):
        document = _index_report(fund_flow="")
        result = verify.check_completeness("brief", verify.Payload({"SH000001": document}))
        assert [(m.dimension.name, m.degraded_note) for m in result.findings] == [
            ("资金流向", "段落为空")
        ]

    def test_a_pinned_date_query_is_not_penalised_for_having_no_live_flow(self):
        """钉了日期就没有实时资金流可展示，这不是故障，不能扣可用率的分。"""
        document = _index_report(fund_flow="- 指定日期查询暂不展示实时资金流向")
        result = verify.check_completeness("brief", verify.Payload({"SH000001": document}))
        assert result.bad == []
        assert result.benign == 1
        assert result.available == result.graded == result.expected - 1
        assert result.findings[0].verdict.startswith("✅")


class TestFocus:
    def test_truncation_starts_near_the_first_difference(self):
        """前 44 个字符相同的两个值，从头截会截出两个一模一样的片段。"""
        old = "x" * 60 + "AAA"
        new = "x" * 60 + "BBB"
        left, right = verify._focus(old, new, width=20)
        assert left != right


class TestProbeSuite:
    def test_every_tool_is_probed(self):
        specs = verify.probe_suite("2026-09-03", set(verify.ALL_TOOLS))
        assert {spec.tool for spec in specs} == set(verify.ALL_TOOLS)

    def test_the_range_probe_asks_for_a_past_window(self):
        spec = next(s for s in verify.probe_suite("2026-09-03", {"kline_range"}))
        assert spec.args["start_date"] < spec.args["end_date"] == "2026-09-03"

    def test_the_default_probe_date_avoids_weekends(self):
        import datetime as dt

        # 2026-09-07 是周一，前一天是周日，要退到上周五。
        assert verify.last_settled_trading_day(dt.date(2026, 9, 7)) == "2026-09-04"


def test_index_detection_matches_the_service():
    assert verify.is_index("SH000001") and verify.is_index("SZ399006")
    assert not verify.is_index("SH600519") and not verify.is_index("SZ300750")
    assert not verify.is_index("SH512480")


def test_symbol_classes():
    assert verify.classify("SH600519") == verify.STOCK
    assert verify.classify("SZ300750") == verify.STOCK
    assert verify.classify("SH512480") == verify.ETF
    assert verify.classify("SZ159995") == verify.ETF
    assert verify.classify("SH000001") == verify.INDEX
    assert verify.classify("SZ399006") == verify.INDEX


def test_an_etf_is_not_faulted_for_having_no_financials():
    """ETF 没有财务报表、市盈率、行业概念——服务端对 1xxxxx/5xxxxx 直接不取。

    不按标的类别放行的话，一次 full + ETF 的探活会凭空报出十项"缺失"。
    """
    document = (
        "# 基本数据\n- 股票代码: SH512480\n- 股票名称: 半导体ETF\n- 数据日期: 2026-09-03\n"
        "# 交易数据\n## 价格\n- 当日: 0.976\n## 涨跌幅\n- 当日: -2.79%\n"
        "## 振幅\n- 当日: 5.38%\n## 成交量(万手)\n- 当日: 1697.28\n"
        "## 成交额(亿)\n- 当日: 16.83\n## 资金流向\n- 今日主力净流入: -4.92亿\n"
    )
    result = verify.check_completeness("medium", verify.Payload({"SH512480": document}))
    assert [m.dimension.name for m in result.bad] == []
