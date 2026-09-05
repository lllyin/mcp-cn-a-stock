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
            docs_ok=1, docs_total=7,
        )
        assert score.tool_rate == 100.0
        assert round(score.overall) == 14
        assert score.verdict.startswith("❌")

    def test_a_clean_run_is_publishable(self):
        score = verify.Score(9, 9, 100, 100, docs_ok=7, docs_total=7)
        assert score.overall == 100.0
        assert score.verdict.startswith("✅")

    def test_a_data_gap_downgrades_a_full_score(self):
        """三个分数都满但有文档整段没取到数据，不能给干净的通过。

        缺数据不算漂移（那是可用性），但也不该被一个 100% 盖过去——先判断是偶发
        还是系统性。
        """
        score = verify.Score(9, 9, 100, 100, docs_ok=7, docs_total=7, gaps=4)
        assert score.overall == 100.0
        assert score.verdict.startswith("⚠️") and "数据缺口" in score.verdict

    def test_a_small_degradation_asks_for_confirmation(self):
        score = verify.Score(9, 9, 95, 100, docs_ok=7, docs_total=7)
        assert score.verdict.startswith("⚠️")

    def test_nothing_measured_does_not_divide_by_zero(self):
        assert verify.Score().overall == 100.0

    def test_a_regression_pass_that_did_not_run_does_not_count_as_passing(self):
        """0/0 算成 100% 再拿去取 min，等于让"没测"冒充"测过且通过"。

        --live 就是这个情形：实时输出没有可比的旧数据，那一项根本没跑。
        """
        ran = verify.Score(9, 9, 90, 100, docs_ok=1, docs_total=10)
        assert round(ran.overall) == 10        # 跑了，10% 拉低总分
        not_ran = verify.Score(9, 9, 90, 100, docs_ok=0, docs_total=0)
        assert round(not_ran.overall) == 90    # 没跑，不参与


class TestPayloadShapes:
    def test_a_result_wrapper_is_unwrapped(self):
        """归档里 kline 的载荷是 {"result": "..."}（mcporter 的 json 输出模式），
        而重放用 text 模式拿到裸正文。不脱壳就会报成"整份缺失"。
        """
        wrapped = verify.parse_payload('{"result": "# SH600362 K线数据\\n\\n共 3 个交易日"}')
        bare = verify.parse_payload("# SH600362 K线数据\n\n共 3 个交易日")
        assert wrapped.documents == bare.documents
        assert verify.compare_documents(
            wrapped.documents["（正文）"], bare.documents["（正文）"], "x"
        ).clean

    def test_a_reports_envelope_is_not_mistaken_for_a_wrapper(self):
        payload = verify.parse_payload('{"reports": {"SH600519": "x"}, "errors": {}}')
        assert list(payload.documents) == ["SH600519"]


class TestNumericCanonicalisation:
    def test_int_and_float_spellings_of_the_same_number_agree(self):
        """上游把 10 写成 10.0 不是数据变化。逐字比会把它报成几十处"值变化"。"""
        old = {"events": [{"seal_amount": 17932200, "pct": 10}]}
        new = {"events": [{"seal_amount": 17932200.0, "pct": 10.0}]}
        assert verify.compare_structures(old, new, "x").clean

    def test_a_real_numeric_change_still_shows(self):
        diff = verify.compare_structures({"a": 10}, {"a": 10.5}, "x")
        assert [(d.kind, d.key, d.old, d.new) for d in diff.diffs] == [
            ("值变化", "a", "10", "10.5")
        ]

    def test_booleans_and_nulls_are_not_numbers(self):
        assert verify.compare_structures({"a": True}, {"a": 1}, "x").diffs
        assert verify.compare_structures({"a": None}, {"a": 0}, "x").diffs


class TestKnownDifferences:
    """已核实的上游差异降级不计分，但每条都有偏差上界，超出就重新算成真差异。"""

    def test_the_tencent_amount_rounding_is_known(self):
        old = "# SH600362 2026-08-28 日K线数据 (前复权)\n- 成交额: 3452443817.00\n"
        new = "# SH600362 2026-08-28 日K线数据 (前复权)\n- 成交额: 3452443800.00\n"
        diff = verify.compare_documents(old, new, "（正文）")
        assert diff.hard == []
        assert [d.kind for d in diff.known] == ["已知差异"]
        assert "100 元" in diff.known[0].note

    def test_a_deviation_past_the_recorded_bound_is_a_real_difference(self):
        """成交额差 1% 就不是"精度到 100 元"那件事了，不能继续放行。"""
        old = "# SH600362 2026-08-28 日K线数据 (前复权)\n- 成交额: 3452443817.00\n"
        new = "# SH600362 2026-08-28 日K线数据 (前复权)\n- 成交额: 3400000000.00\n"
        assert [d.kind for d in verify.compare_documents(old, new, "（正文）").hard] == [
            "值变化"
        ]

    def test_the_chinext_volume_gap_is_known_only_for_that_index(self):
        old = "## 成交量(万手)\n- 当日: 19364.88\n"
        new = "## 成交量(万手)\n- 当日: 18615.42\n"          # 低 3.9%
        assert verify.compare_documents(old, new, "SZ399006").hard == []
        # 同样的偏差出现在深证成指上就不放行——核实过的只有创业板指一个标的
        assert [d.kind for d in verify.compare_documents(old, new, "SZ399001").hard] == [
            "值变化"
        ]

    def test_every_entry_carries_a_reason_and_a_bound(self):
        """条目是拿来记已核实结论的，不是拿来消红字的开关。"""
        assert verify.KNOWN_DIFFERENCES
        for entry in verify.KNOWN_DIFFERENCES:
            assert len(entry.reason) > 20, entry.key
            assert 0 <= entry.bound < 0.1, entry.key


class TestLostSectionCollapse:
    def test_a_whole_section_vanishing_is_one_row(self):
        """full 的历史资金流向是 60 行的表，拿不到时逐行列出会把别的差异挤没。"""
        old = "## 历史资金流向\n" + "\n".join(f"| 2026-08-{d:02d} | x |" for d in range(1, 21))
        diff = verify.compare_documents(old, "# 别的\n- a: 1\n", "x")
        # 整段消失算"缺数据"——可用性问题，不进回归漂移
        assert len(diff.gaps) == 1 and "21 行" in diff.gaps[0].key
        assert not [d for d in diff.hard if "整段不见了" in d.key]

    def test_a_section_that_only_changed_is_not_collapsed(self):
        """段落还在、只是内容变了，就得逐行报——那才是要查的。"""
        old = "## 价格\n" + "\n".join(f"- d{i}: {i}.0" for i in range(8))
        new = "## 价格\n" + "\n".join(f"- d{i}: {i}.5" for i in range(8))
        diff = verify.compare_documents(old, new, "x")
        assert all("整段不见了" not in d.key for d in diff.diffs)
        assert len(diff.diffs) == 8

    def test_a_few_missing_lines_are_still_listed_individually(self):
        old = "## 价格\n- a: 1\n- b: 2\n- c: 3\n"
        diff = verify.compare_documents(old, "## 价格\n- a: 1\n", "x")
        assert sorted(d.key for d in diff.hard) == ["价格 › - b", "价格 › - c"]


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

    def test_every_no_data_phrase_in_the_renderer_is_covered(self):
        """这张表漏一句，那一维就会被标成"有数据"——假保证比没有更糟。

        2026-09-04 就漏过 ``暂无资金流向数据``：四个标的的资金流实际是空的，
        而维度矩阵把它们全标成了 ✅。所以这里直接去 research.py 里数一遍。
        """
        import ast
        import re as _re

        source = (Path(__file__).resolve().parents[1] / "finmcp" / "research.py").read_text(
            encoding="utf-8"
        )
        phrases = set()
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"
                    and node.args):
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if _re.search(r"暂无|不可用|获取失败|暂不", first.value):
                        phrases.add(first.value.strip().lstrip("- "))
        missing = sorted(p for p in phrases if p not in verify.DEGRADED_MARKERS)
        assert not missing, f"research.py 里这些提示语没进 DEGRADED_MARKERS: {missing}"

    def test_an_empty_section_is_also_degraded(self):
        document = _index_report(fund_flow="")
        result = verify.check_completeness("brief", verify.Payload({"SH000001": document}))
        assert [(m.dimension.name, m.degraded_note) for m in result.findings] == [
            ("资金流向", "段落为空")
        ]

    def test_a_pinned_date_in_a_probe_is_a_bug_not_an_excuse(self):
        """探活一律不钉日期，所以这句话出现就说明调用参数错了。

        它曾经被当成"正当缺席"放行——那是钉日期探活时代的逻辑。实时探活下放行
        它，等于让"实时资金流取不取得到"这件事永远查不出来。
        """
        document = _index_report(fund_flow="- 指定日期查询暂不展示实时资金流向")
        result = verify.check_completeness("brief", verify.Payload({"SH000001": document}))
        assert len(result.bad) == 1
        assert result.available == result.graded - 1
        assert result.findings[0].verdict.startswith("⚠️")
        assert "钉了日期" in result.findings[0].degraded_note


class TestFocus:
    def test_truncation_starts_near_the_first_difference(self):
        """前 44 个字符相同的两个值，从头截会截出两个一模一样的片段。"""
        old = "x" * 60 + "AAA"
        new = "x" * 60 + "BBB"
        left, right = verify._focus(old, new, width=20)
        assert left != right


class TestProbeSuite:
    def test_every_tool_is_probed(self):
        specs = verify.probe_suite(set(verify.ALL_TOOLS))
        assert {spec.tool for spec in specs} == set(verify.ALL_TOOLS)

    def test_the_probe_never_pins_a_date_on_the_report_tools(self):
        """brief/medium/full/tech 钉了日期就查不出实时资金流取不取得到。

        kline_daily 是例外——它的 date 是必填的，那个工具本身就是按日寻址的。
        """
        for spec in verify.probe_suite({"brief", "medium", "full", "tech"}):
            assert "date" not in spec.args, spec.describe()

    def test_the_symbols_are_pinned_to_the_three_captured_sets(self):
        """标的要和服务器采过的那三份对齐，否则实时输出没有参照可比。"""
        assert len(verify.LIVE_BATCHES) == 3
        symbols = {s for _, group in verify.LIVE_BATCHES for s in group.split(",")}
        assert symbols >= {"SH000001", "SZ399001", "SZ399006", "SH000688"}   # 四大指数
        assert "SH512480" in symbols                                        # ETF
        assert len(symbols) == 12

    def test_the_range_probe_asks_for_a_past_window(self):
        spec = next(s for s in verify.probe_suite({"kline_range"}))
        assert spec.args["start_date"] < spec.args["end_date"]


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


# --- 负零不是漂移 -----------------------------------------------------------
# 两个资金流来源在"四舍五入后是零"的值上符号不一致：主源是浮点数，-0.000038
# 格式化成两位小数是 -0.00%；页面兜底取页面已渲染好的文本，同一个值是 0.00%。


class TestNegativeZero:
    def test_negative_zero_is_the_same_number(self):
        assert verify._canonical_text("| -1.10万 | -0.00% |") == "| -1.10万 | 0.00% |"
        assert verify._canonical_text("-0.000") == "0.000"
        assert verify._canonical_text("-0") == "0"

    def test_a_real_negative_is_untouched(self):
        """只等同数值相同的写法，不能把真的负数抹成正数。"""
        for text in ("-0.01", "-0.10%", "-1.10万", "-10.0", "-0.001"):
            assert verify._canonical_text(text) == text

    def test_a_document_differing_only_in_zero_sign_is_clean(self):
        old = "# 历史资金流向\n| 2026-06-30 | -1.10万 | -0.00% | 2.08亿 |"
        new = "# 历史资金流向\n| 2026-06-30 | -1.10万 | 0.00% | 2.08亿 |"
        assert verify.compare_documents(old, new, "t").clean

    def test_a_real_change_still_reports(self):
        old = "# 历史资金流向\n| 2026-06-30 | -1.10万 | -0.01% | 2.08亿 |"
        new = "# 历史资金流向\n| 2026-06-30 | -1.10万 | 0.01% | 2.08亿 |"
        assert not verify.compare_documents(old, new, "t").clean


# --- 资金流向按类别判，不按具体标的判 ---------------------------------------


class TestFundFlowApplicability:
    """这里曾经有一份"有资金流页面的指数"名单，把 SH000688 判成"本来就没有"。

    2026-09-04 服务器开着网关采的 logs/s1_index.json 推翻了它：同一个 SH000688，
    今日主力净流入 -42.67亿。那份名单记的是**某一条源**的覆盖范围，不是标的的
    属性，于是那台机器上一处真实的缺失被记成了满分。判据回到只按类别。
    """

    def test_every_class_is_expected_to_have_it(self):
        flow = [d for d in verify.CONTRACT["brief"] if d.name == "资金流向"]
        assert flow
        for symbol in ("SH600519", "SH512480", "SH000001", "SH000688", "BJ920021"):
            assert flow[0].applies(symbol), symbol

    def test_a_missing_section_is_counted_for_every_symbol(self):
        for symbol in ("SH000688", "SH000001", "SH600519"):
            payload = verify.Payload(
                documents={symbol: f"# 基本数据\n- 股票代码: {symbol}\n"}
            )
            result = verify.check_completeness("brief", payload)
            assert any(
                f.dimension.name == "资金流向" for f in result.findings
            ), symbol

    def test_history_flow_is_expected_for_indices_too(self):
        """实测 full 对 SH000001 和 SZ399006 都渲染了完整历史表，排除等于不计分。"""
        flow = [d for d in verify.CONTRACT["full"] if d.name == "历史资金流向"]
        assert flow and flow[0].applies("SH000001")


# --- 性能一节 --------------------------------------------------------------


class TestPerformanceSection:
    def test_percentile_uses_nearest_rank(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert verify._percentile(values, 0.5) == 3.0
        assert verify._percentile(values, 0.9) == 5.0
        assert verify._percentile([], 0.5) == 0.0
        assert verify._percentile([7.0], 0.9) == 7.0

    def _call(self, tool, elapsed, ok=True):
        spec = verify.CallSpec(tool=tool, args={})
        return verify.CallResult(
            spec=spec, exit_code=0 if ok else 1,
            payload="x" if ok else "", stderr="", elapsed=elapsed,
        )

    def test_timings_are_grouped_per_tool_and_overall(self):
        calls = [self._call("brief", 1.0), self._call("brief", 3.0),
                 self._call("full", 10.0)]
        text = "\n".join(verify._render_performance(verify.MemoryWatch(), calls))
        assert "| brief | 2 |" in text
        assert "| full | 1 |" in text
        assert "**全部**" in text and "**3**" in text

    def test_failed_calls_do_not_pollute_the_timings(self):
        calls = [self._call("brief", 1.0), self._call("brief", 99.0, ok=False)]
        text = "\n".join(verify._render_performance(verify.MemoryWatch(), calls))
        assert "| brief | 1 |" in text
        assert "99.00s" not in text

    def test_it_says_so_when_memory_was_not_sampled(self):
        watch = verify.MemoryWatch()
        watch.note = "没找到服务进程"
        text = "\n".join(verify._render_performance(watch, [self._call("brief", 1.0)]))
        assert "未采到" in text and "没找到服务进程" in text

    def test_memory_summary_reports_peak_not_just_endpoints(self):
        """峰值只在页面加载那两三秒里存在，只看首尾必然错过。"""
        watch = verify.MemoryWatch()
        watch.samples = [
            (100.0, 3, 0.0, 0, 10.0),
            (800.0, 9, 700.0, 6, 14.0),
            (120.0, 3, 0.0, 0, 16.0),
        ]
        info = watch.summary()
        assert info["peak"] == 800.0 and info["peak_processes"] == 9
        assert info["browser_peak"] == 700.0
        assert info["first"] == 100.0 and info["last"] == 120.0
        # 累计 CPU 秒是单调的，首尾之差才是这段窗口烧掉的算力
        assert info["cpu_seconds"] == 6.0
        assert info["cpu_cores"] == pytest.approx(3.0)   # 6s CPU / 2s 窗口

    def test_no_successful_call_degrades_gracefully(self):
        text = "\n".join(verify._render_performance(verify.MemoryWatch(), []))
        assert "没有成功的调用" in text


# --- 指数专项的"资金流向"一列 ---------------------------------------------
# 三个坑都真出过：tech 没这一维却被判 ❌、full 的历史表头里有"净流入"导致
# SH000688 被判成 ✅ 有、"没有这一维"和"有但没取到"混成同一个符号。


class TestIndexFlowVerdict:
    FULL_688 = ("# 基本数据\n## 资金流向\n- 暂无实时资金流向\n"
                "## 历史资金流向\n| 日期 | 主力净流入 |\n")

    def test_a_tool_without_the_dimension_says_dash(self):
        assert verify._index_flow_verdict("tech", "SH000001", "## 资金流向\n- 今日主力净流入: 1亿\n") == "—"
        assert verify._index_flow_verdict("tech", "SH000688", self.FULL_688) == "—"

    def test_kechuang50_reads_the_same_in_every_tool(self):
        """同一个标的同一件事，brief / medium / full 不能三种说法。"""
        verdicts = {
            verify._index_flow_verdict(tool, "SH000688",
                                       "## 资金流向\n- 暂无实时资金流向\n" if tool != "full" else self.FULL_688)
            for tool in ("brief", "medium", "full")
        }
        # 科创50 在本机的兜底源上确实没取到，就如实报没取到——它不是"本来
        # 就没有"。三个工具口径一致这件事不变。
        assert verdicts == {"❌ 实时资金流没取到（主源被拒且页面兜底也没成）"}

    def test_a_history_table_header_no_longer_fakes_a_hit(self):
        """原来判的是全文 '净流入'，而历史表**表头**里就有这三个字。"""
        assert "✅ 有" not in verify._index_flow_verdict("full", "SH000688", self.FULL_688)

    def test_real_data_and_real_absence_are_distinguishable(self):
        assert verify._index_flow_verdict(
            "brief", "SH000001", "## 资金流向\n- 今日主力净流入: -224.7亿\n") == "✅ 有"
        assert verify._index_flow_verdict(
            "brief", "SH000001", "## 资金流向\n- 暂无实时资金流向\n").startswith("❌")
        assert verify._index_flow_verdict("brief", "SH000001", "# 基本数据\n") == "❌ 段落都不在"

    def test_the_pinned_date_note_is_scoped_to_the_section(self):
        """别再用全文子串——这正是上一个 bug 的成因。"""
        doc = "## 资金流向\n- 今日主力净流入: 1亿\n\n# 附注\n指定日期查询暂不展示实时资金流向\n"
        assert verify._index_flow_verdict("brief", "SH000001", doc) == "✅ 有"


# --- 诊断一节 --------------------------------------------------------------


class TestDiagnostics:
    LOG = (
        "2026-09-05 14:46:23,001 DEBUG Data task _fetch_kline_sync request_id=r tool=brief "
        "symbol=SH600519 admission=0.000s queue=1.00s service=11.50s\n"
        "2026-09-05 14:46:24,001 DEBUG Data task _fetch_kline_sync request_id=r tool=brief "
        "symbol=SH600519 admission=0.000s queue=0.10s service=0.40s\n"
        "2026-09-05 14:46:25,001 WARNING HTTP channel suspending impersonation for 300.0s "
        "after 4 consecutive failures; falling back to plain requests\n"
        "2026-09-05 14:46:26,001 WARNING 获取资金流向数据失败 600519: boom\n"
        "2026-09-05 14:46:27,001 INFO 资金流向页面兜底成功 SH600519 rows=120 cost=2.24s\n"
        "2026-09-05 14:46:28,001 INFO 资金流向页面兜底跳过 SH000688: 该标的没有资金流向页面\n"
        "2026-09-05 14:46:29,001 INFO 资金流向页面兜底跳过 SZ399006: 兜底名额已满(上限 3)，等 3.0s 未排到\n"
        "2026-09-05 14:46:30,001 INFO Realtime fund flow page request_id=r tool=brief "
        "symbol=SH600519 url=http://x how=new_tab outcome=today=True history=121 "
        "semaphore_wait=0.500s service=2.100s\n"
        "2026-09-05 14:46:45,001 WARNING Source breaker opened source=eastmoney_kline channel=x\n"
    )

    def _scan(self, tmp_path, since=None):
        path = tmp_path / "svc.log"
        path.write_text(self.LOG, encoding="utf-8")
        import datetime as dt
        return verify.scan_log(path, since or dt.datetime(2026, 9, 5, 14, 46, 22))

    def test_sources_are_aggregated_with_queue(self, tmp_path):
        diag = self._scan(tmp_path).diagnostics
        rows = diag["sources"]["_fetch_kline_sync"]
        assert sorted(v for v, _ in rows) == [0.40, 11.50]
        assert max(q for _, q in rows) == 1.00

    def test_fund_flow_gates_split_by_reason(self, tmp_path):
        gates = self._scan(tmp_path).diagnostics["fund_flow_gates"]
        assert gates["主源失败"] == 1 and gates["兜底成功"] == 1
        assert gates["兜底跳过：该标的没有资金流向页面"] == 1
        # 秒数被抹掉再归并，否则每条都是一个独立原因
        assert any("名额已满" in k and "Ns" in k for k in gates)

    def test_page_loads_survive_a_spaced_outcome(self, tmp_path):
        """outcome 里有空格（today=True history=121），用 \\S+ 会截断。"""
        loads = self._scan(tmp_path).diagnostics["page_loads"]
        assert len(loads) == 1
        how, outcome, wait, service = loads[0]
        assert how == "new_tab" and "history=121" in outcome
        assert (wait, service) == (0.5, 2.1)

    def test_degradation_events_are_ordered_with_detail(self, tmp_path):
        events = self._scan(tmp_path).diagnostics["events"]
        kinds = [(k, d) for _, k, d, _ in events]
        assert kinds == [("通道暂停伪装", "300s"), ("熔断打开", "eastmoney_kline")]

    def test_lines_before_the_run_are_ignored(self, tmp_path):
        import datetime as dt
        diag = self._scan(tmp_path, dt.datetime(2026, 9, 5, 14, 46, 44)).diagnostics
        assert not diag.get("sources")
        assert len(diag["events"]) == 1

    def test_the_coverage_column_says_how_much_of_the_run_was_degraded(self):
        """降级窗口盖住多少运行时间，决定第六节的耗时能不能当基准。

        2026-09-05 18:36 那次：运行 101s，伪装通道在第 2 秒暂停 300s——覆盖 98%，
        也就是那一份报告里的耗时全都量的是降级路径。这个数原先要自己拿日志算。
        """
        import datetime as dt

        span = (dt.datetime(2026, 9, 5, 18, 36, 47), dt.datetime(2026, 9, 5, 18, 38, 28))
        scan = verify.LogScan(diagnostics={
            "span": span,
            "events": [
                (dt.datetime(2026, 9, 5, 18, 36, 49), "通道暂停伪装", "300s", 300.0),
                (dt.datetime(2026, 9, 5, 18, 36, 57), "熔断打开", "eastmoney_kline", 120.0),
            ],
        })
        text = "\n".join(verify._render_diagnostics(scan))
        assert "98%（99/101s）" in text
        assert "90%（91/101s）" in text

    def test_an_early_close_truncates_the_coverage(self):
        """熔断没走满冷却就关了，按实际关闭时刻算，不按冷却时长算。"""
        import datetime as dt

        scan = verify.LogScan(diagnostics={
            "span": (dt.datetime(2026, 9, 5, 18, 36, 47), dt.datetime(2026, 9, 5, 18, 38, 28)),
            "events": [
                (dt.datetime(2026, 9, 5, 18, 36, 57), "熔断打开", "eastmoney_kline", 120.0),
                (dt.datetime(2026, 9, 5, 18, 37, 27), "熔断关闭", "eastmoney_kline", 0.0),
            ],
        })
        assert "30%（30/101s）" in "\n".join(verify._render_diagnostics(scan))

    def test_it_renders_without_a_log(self):
        scan = verify.LogScan(available=False, note="日志不存在")
        text = "\n".join(verify._render_diagnostics(scan))
        assert "没有可用的服务日志" in text and "日志不存在" in text

    def test_the_rendered_table_carries_the_shares(self, tmp_path):
        text = "\n".join(verify._render_diagnostics(self._scan(tmp_path)))
        assert "_fetch_kline_sync" in text and "兜底救回率" in text
        assert "通道暂停伪装" in text and "new_tab 1 次" in text


# --- /proc 解析（只在 Linux 上跑，所以更要测）------------------------------
# 开发机是 macOS，这条路本地一次都走不到，而部署机全靠它。


class TestProcStatParsing:
    #: 一行真实形状的 /proc/<pid>/stat：pid comm state ppid ... utime stime ... rss
    def _stat(self, pid=42, comm="python3", ppid=7, utime=300, stime=100, rss=25600):
        fields = ["0"] * 50
        fields[0] = "R"            # state（去掉 pid/comm 之后的第 1 个字段）
        fields[1] = str(ppid)      # ppid
        fields[11] = str(utime)
        fields[12] = str(stime)
        fields[21] = str(rss)      # rss，单位是页
        return f"{pid} ({comm}) " + " ".join(fields)

    def test_it_reads_ppid_rss_and_cpu(self):
        row = verify._parse_proc_stat(self._stat())
        pid, ppid, rss_kib, comm, cpu = row
        assert (pid, ppid, comm) == (42, 7, "python3")
        assert rss_kib > 0                       # 页数 × 页大小
        assert cpu == pytest.approx(400 / verify._CLOCK_TICKS)

    def test_a_comm_with_spaces_does_not_shift_the_fields(self):
        """进程名里有空格时整行 split() 会把后面所有字段错位。"""
        row = verify._parse_proc_stat(self._stat(comm="Web Content"))
        assert row[3] == "Web Content"
        assert row[1] == 7                       # ppid 没被挤走

    def test_a_comm_with_parentheses_is_handled(self):
        row = verify._parse_proc_stat(self._stat(comm="weird (name)"))
        assert row[3] == "weird (name)" and row[1] == 7

    def test_a_chromium_comm_is_recognised_as_browser(self):
        assert any(h.lower() in "chrome_crashpad".lower() for h in verify._BROWSER_HINTS)

    def test_malformed_lines_are_skipped_not_raised(self):
        for bad in ("", "no-parens-here", "42 (x) R", "notapid (x) " + " ".join(["0"] * 50)):
            assert verify._parse_proc_stat(bad) is None


# --- 矩阵要和缺失明细、可用率三者一致 -------------------------------------
# 2026-09-05 那份报告里缺失明细列了 8 项、可用率扣到 98%，矩阵却全绿——因为矩阵
# 取的是"跨工具最好的那次"。三处说法不一致，读的人只能挨个核对。


class TestMatrixAgreesWithFindings:
    def _probe(self, tool, symbol, document):
        spec = verify.CallSpec(tool, {"symbol": symbol})
        result = verify.CallResult(spec=spec, exit_code=0, payload=document,
                                   stderr="", elapsed=1.0)
        payload = verify.Payload(documents={symbol: document})
        return result, payload, verify.check_completeness(tool, payload)

    #: 一份个股报告，可以按需抽掉某一维
    def _doc(self, with_pb=True):
        parts = ["# 基本数据", "- 股票代码: SZ000333", "- 股票名称: 美的",
                 "- 数据日期: 2026-09-05", "- 行业概念: 家电",
                 "- 总市值: 1亿", "- 流通市值: 1亿",
                 "- 市盈率(静): 10", "- 市盈率(动): 9"]
        if with_pb:
            parts += ["- 市净率: 2", "- 净资产收益率: 10%"]
        parts += ["## 价格", "- 当日: 1", "## 涨跌幅", "- 当日: 1%",
                  "## 振幅", "- 当日: 1%", "## 成交量(万手)", "- 当日: 1",
                  "## 成交额(亿)", "- 当日: 1",
                  "## 资金流向", "- 今日主力净流入: 1亿",
                  "## 换手率", "- 当日: 1%"]
        return "\n".join(parts) + "\n"

    def test_a_dimension_one_tool_missed_is_not_green(self):
        """brief 没拿到、medium 拿到了——不能显示成 ✅。"""
        probes = [self._probe("brief", "SZ000333", self._doc(with_pb=False)),
                  self._probe("medium", "SZ000333", self._doc(with_pb=True))]
        text = "\n".join(verify._render_matrix(probes))
        row = next(l for l in text.splitlines() if l.startswith("| 市净率 "))
        assert "◐" in row, row
        assert "✅" not in row, row

    def test_all_tools_ok_stays_green(self):
        probes = [self._probe(t, "SZ000333", self._doc()) for t in ("brief", "medium")]
        row = next(l for l in "\n".join(verify._render_matrix(probes)).splitlines()
                   if l.startswith("| 市净率 "))
        assert "✅" in row and "◐" not in row

    def test_all_tools_missed_keeps_the_hard_mark(self):
        probes = [self._probe(t, "SZ000333", self._doc(with_pb=False))
                  for t in ("brief", "medium")]
        row = next(l for l in "\n".join(verify._render_matrix(probes)).splitlines()
                   if l.startswith("| 市净率 "))
        assert "❌" in row and "◐" not in row

    def test_the_legend_explains_the_new_symbol(self):
        probes = [self._probe("brief", "SZ000333", self._doc())]
        text = "\n".join(verify._render_matrix(probes))
        assert "◐" in text and "有的工具拿到" in text

    def test_a_matrix_cell_never_contradicts_the_findings(self):
        """凡是缺失明细里出现过的 (标的,维度)，矩阵那一格就不能是 ✅。"""
        probes = [self._probe("brief", "SZ000333", self._doc(with_pb=False)),
                  self._probe("medium", "SZ000333", self._doc(with_pb=True))]
        flagged = {(f.symbol, f.dimension.name)
                   for _, _, c in probes for f in c.findings}
        text = "\n".join(verify._render_matrix(probes))
        for _, dim in flagged:
            row = next(l for l in text.splitlines() if l.startswith(f"| {dim} "))
            assert "✅" not in row, f"{dim} 在缺失明细里，矩阵却是 ✅：{row}"
