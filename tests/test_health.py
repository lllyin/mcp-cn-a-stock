"""``health`` 工具的三层:维度契约、日志解析、大屏渲染。

这一组测试大半是**开发过程中真踩到的坑**变来的,不是补充说明。趋势那两条尤其:
第一版按样本数对半分、跨纪元比,在真实日志上算出 +251% 的"性能下降",全是假象。
一个把噪音报成性能下降的健康工具,比没有这个工具更糟——人会照着它去改配置。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from finmcp import health_report, log_digest, report_contract as rc

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


# --- 维度契约 ----------------------------------------------------------------


def test_expected_excludes_what_a_class_structurally_lacks():
    """ETF 没有财务报表、指数没有市值——那不是缺失,不进分母。"""
    stock = {d.name for d in rc.expected("full", "SZ000333")}
    etf = {d.name for d in rc.expected("full", "SH512480")}
    index = {d.name for d in rc.expected("full", "SH000001")}
    assert "财务数据" in stock and "财务数据" not in etf and "财务数据" not in index
    assert "总市值" in stock and "总市值" not in index
    # 历史资金流向对三类都算——实测 full 对指数确实渲染得出来，排除等于把
    # 真实拿到的数据不计分。
    assert {"历史资金流向"} <= stock & etf & index


def test_scan_reports_present_and_missing():
    text = "# 基本数据\n- 股票代码: SH512480\n- 股票名称: X\n- 数据日期: 2026-09-09\n## 价格\n"
    present, missing = rc.scan(text, "brief", "SH512480")
    assert {"股票代码", "股票名称", "数据日期", "价格"} <= set(present)
    assert "成交量" in missing and "资金流向" in missing
    assert len(present) + len(missing) == len(rc.expected("brief", "SH512480"))


def test_degraded_markers_are_found():
    """有段落标题 ≠ 有值。只查标题会把降级当成正常。"""
    assert rc.degraded_in("## 资金流向\n- 暂无资金流向数据\n") == ["暂无资金流向数据"]
    assert rc.degraded_in("## 资金流向\n- 当日主力净流入: 1.2亿\n") == []


def test_the_service_and_the_release_gate_read_the_same_table():
    """两处各存一份的话，同一件事两个数，谁都不敢信。"""
    sys.path.insert(0, str(_SCRIPTS))
    import verify_release

    assert verify_release.CONTRACT is rc.CONTRACT
    assert verify_release.DEGRADED_MARKERS is rc.DEGRADED_MARKERS
    assert verify_release.classify is rc.classify


def _contract_signature(contract) -> str:
    payload = repr([(tool, [(d.name, d.marker) for d in dims])
                    for tool, dims in sorted(contract.items())])
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def test_the_gate_still_runs_as_a_plain_script(tmp_path):
    """``python scripts/verify_release.py`` 得能直接跑。

    起子进程是必须的：pytest 会把 rootdir 放进 sys.path，在测试进程里 import 得到
    **不代表**命令行跑得起来。上面那条就是这么漏的——它一直绿着，而真正敲
    ``python3 scripts/verify_release.py`` 的人拿到 ModuleNotFoundError。
    cwd 也要挪出仓库，否则当前目录会顺带把 finmcp 送进 sys.path。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(_SCRIPTS / "verify_release.py"), "--help"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def test_the_gate_works_without_the_service_stack(tmp_path):
    """装不全服务依赖时闸门仍要能跑，且读到的是同一张表。

    ``import finmcp.report_contract`` 会先执行 ``finmcp/__init__.py``，把 FastMCP、
    pandas、pydantic 整个拉进来。为一张纯数据的表就要求装齐服务依赖是本末倒置——
    最需要这个闸门的时候（服务环境本身可疑）正是装不全的时候。
    """
    program = textwrap.dedent(f"""
        import importlib.abc, sys
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "finmcp" or fullname.startswith("finmcp."):
                    raise ImportError("服务依赖装不全（测试模拟）")
                return None
        sys.meta_path.insert(0, Block())
        sys.path.insert(0, {str(_SCRIPTS)!r})
        import hashlib
        import verify_release as v
        print(v.CONTRACT_SOURCE)
        payload = repr([(t, [(d.name, d.marker) for d in ds])
                        for t, ds in sorted(v.CONTRACT.items())])
        print(hashlib.sha256(payload.encode()).hexdigest()[:12])
    """)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "-c", program],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    source, signature = proc.stdout.strip().splitlines()
    assert source.endswith("report_contract.py")          # 走的确实是兜底那条路
    assert signature == _contract_signature(rc.CONTRACT)  # 拿到的是同一张表，不是走样的副本


# --- 日志解析 ----------------------------------------------------------------


def _line(stamp, text):
    return f"2026-09-09 {stamp},000 INFO {text}"


def _symbol_line(stamp, symbol, total, present=None, expected_n=None, missing="-",
                 tool="brief", degraded="-"):
    tail = ""
    if present is not None:
        tail = f" present={present}/{expected_n} missing={missing} degraded={degraded}"
    return _line(stamp, f"Finished symbol request_id=r1 tool={tool} symbol={symbol} "
                        f"raw_data=1.0s render=1.0s total={total}s chars=900{tail}")


def _write(tmp_path, lines):
    path = tmp_path / "cn-stock-mcp.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_availability_is_measured_when_the_log_carries_dimensions(tmp_path):
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
        _symbol_line("10:00:02", "SH512480", 2.0, present=8, expected_n=9, missing="资金流向"),
    ])
    data = log_digest.digest(log)
    assert data["availability"]["method"] == "measured"
    assert data["availability"]["expected"] == 26
    assert data["availability"]["present"] == 25
    assert data["missing"][0]["dimension"] == "资金流向"
    assert data["missing"][0]["symbols"] == ["SH512480"]


def test_availability_falls_back_to_inference_on_old_logs(tmp_path):
    """老日志没有 present= 字段时按上游失败推算，并且要**说出来**。

    标着 measured 的推算值是最坏的结果：看着精确，实际看不见"源成功返回但字段
    为空"那一类——沪市 ETF 的资金流就是那样丢的，日志里一条失败都没有。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0),
        _line("10:00:01", "Report cache skipped request_id=r1 tool=brief "
                          "symbol=SZ000333 incomplete_sources=fund_flow"),
    ])
    data = log_digest.digest(log)
    assert data["availability"]["method"] == "inferred"
    assert data["failed_sources"] == {"fund_flow": 1}
    assert "推算" in health_report.render(data)


def test_symbol_filter_also_filters_latency(tmp_path):
    """按标的过滤时耗时也要跟着过滤，否则报告里的"最慢一次"是别的标的的。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
        _symbol_line("10:00:02", "SH000688", 99.0, present=11, expected_n=11),
        _line("10:00:01", "Data task _fetch_kline_sync request_id=r1 tool=brief "
                          "symbol=SZ000333 admission=0.0s queue=0.0s service=1.5s"),
        _line("10:00:02", "Data task _fetch_kline_sync request_id=r1 tool=brief "
                          "symbol=SH000688 admission=0.0s queue=0.0s service=88.0s"),
    ])
    everything = log_digest.digest(log)
    assert everything["latency"]["K 线取数"]["stats"]["n"] == 2
    only = log_digest.digest(log, symbol="SZ000333")
    assert only["latency"]["K 线取数"]["stats"]["n"] == 1
    assert only["latency"]["K 线取数"]["stats"]["max"] == 1.5
    assert len(only["symbols"]) == 1


def test_batch_rows_are_dropped_when_filtering_by_symbol(tmp_path):
    """一批覆盖多个标的，按单个标的过滤时把整批耗时算进来是错的。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
        _line("10:00:03", "Batch query released request_id=r1 tool=brief "
                          "symbols=SZ000333,SH000688 queue=0.0s service=9.0s total=9.5s"),
    ])
    assert log_digest.digest(log)["latency"]["整批总计"]["stats"]["n"] == 1
    assert log_digest.digest(log, symbol="SZ000333")["latency"]["整批总计"]["stats"] is None


# --- 逐小时比：本小时 vs 上一小时 -------------------------------------------
#
# 这一组是三版设计换下来的。前两版都在真实日志上算出过假的"性能下降"：按样本数
# 对半（密集采样那段缓存全热、天然快）算出 +251%；跨市场时段直接对半算出
# +98% ~ +787%。第三版按时间中点对半，数对了但切分点取决于日志跨度——换个
# since 同一批数据能得出不同的涨跌，而且同一列里七个阶段其实是七个切分点。
# 现在按整点小时分桶：边界是绝对的，不用向读者解释切分点在哪。


def _series(pairs):
    return log_digest.Series([(f"2026-09-09 {t}", v) for t, v in pairs])


def _hour(h, n, value, minute_from=0):
    return [(f"{h:02d}:{minute_from + i:02d}:00", value) for i in range(n)]


def test_no_trend_with_only_one_hour_of_data():
    assert _series(_hour(10, 40, 1.0)).trend() is None


def test_the_comparison_is_hour_against_hour():
    trend = _series(_hour(10, 30, 1.0) + _hour(11, 30, 2.0)).trend()
    assert trend is not None
    assert (trend["previous"][11:], trend["hour"][11:]) == ("10", "11")
    assert trend["baseline"]["p90"] == 1.0 and trend["current"]["p90"] == 2.0
    assert round(trend["change_pct"]) == 100


def test_a_gap_hour_is_not_treated_as_the_previous_hour():
    """11 点一次调用都没有时，不能拿 10 点的数当"上一小时"——那是在编。"""
    assert _series(_hour(10, 30, 1.0) + _hour(12, 30, 2.0)).trend() is None


def test_both_hours_need_enough_samples():
    """样本少于门槛时 p90 基本就是最大值，拿两个最大值比涨跌是在比噪音。"""
    few = log_digest.MIN_HOUR_SAMPLES - 1
    assert _series(_hour(10, 30, 1.0) + _hour(11, few, 2.0)).trend() is None
    assert _series(_hour(10, few, 1.0) + _hour(11, 30, 2.0)).trend() is None
    assert _series(_hour(10, 30, 1.0)
                   + _hour(11, log_digest.MIN_HOUR_SAMPLES, 2.0)).trend() is not None


def test_the_answer_does_not_move_when_the_query_window_grows():
    """同一批数据，多读进来几小时历史，"本小时 vs 上一小时"必须还是那个数。

    这正是换掉"窗口对半"的理由：对半分的切分点取决于日志跨度，把 since 从 30m
    调到 today 就能让同一段数据算出不同的涨跌，而读者会以为性能变了。
    """
    recent = _hour(13, 30, 1.0) + _hour(14, 30, 2.0)
    narrow = _series(recent).trend()
    wide = _series(_hour(9, 50, 9.0) + _hour(10, 50, 0.1) + recent).trend()
    assert narrow["change_pct"] == wide["change_pct"]
    assert (narrow["previous"], narrow["hour"]) == (wide["previous"], wide["hour"])


def test_a_cross_phase_comparison_is_flagged_not_refused():
    """小时边界和市场时段边界不重合，按纪元筛桶会把桶自己切碎。

    所以跨时段照样给数，但标出来——盘中和收盘后走的不是一条路，那个涨跌多半是
    换了时段而不是变慢了。
    """
    epoch_of = lambda s: "live" if s[11:13] < "16" else "evening"   # noqa: E731
    same = _series(_hour(10, 30, 1.0) + _hour(11, 30, 1.0)).trend(epoch_of)
    across = _series(_hour(15, 30, 1.0) + _hour(16, 30, 1.0)).trend(epoch_of)
    assert same["cross_phase"] is False
    assert across["cross_phase"] is True
    assert across["change_pct"] is not None, "标注就够了，不该拒绝给数"


def test_the_percentage_is_skipped_when_the_baseline_is_tiny():
    """基线 p90 接近 0 时不给百分比——多是缓存命中的阶段，除出来的 ±100% 没意义。"""
    trend = _series(_hour(10, 30, 0.0) + _hour(11, 30, 0.4)).trend()
    assert trend is not None and trend["change_pct"] is None


# --- 逐小时可用率 ------------------------------------------------------------


def test_hourly_availability_says_when_it_started(tmp_path):
    """合起来的一个百分比分不出"一小时前坏过、现在好了"和"正在坏"。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
        _symbol_line("10:30:01", "SH512480", 2.0, present=8, expected_n=9,
                     missing="资金流向"),
        _symbol_line("11:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    data = log_digest.digest(log)
    hours = {r["hour"][11:]: r for r in data["hours"]}
    assert hours["10"]["rate"] < 1 and hours["10"]["missing"] == {"资金流向": 1}
    assert hours["11"]["rate"] == 1.0
    assert [r["hour"][11:] for r in data["hours"]] == ["11", "10"], "新的要排前面"
    report = health_report.render(data)
    assert "## 逐小时可用率" in report
    assert "| 10:00 |" in report and "| 11:00 |" in report


def test_hours_without_measured_fields_are_left_out(tmp_path):
    """老日志不知道缺了什么，按"零缺失"计入会把那一小时抬成满分。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0),                   # 老格式
        _symbol_line("11:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    assert [r["hour"][11:] for r in log_digest.digest(log)["hours"]] == ["11"]


def test_hourly_table_sorts_newest_first_even_if_rows_arrive_shuffled():
    """上游顺序靠不住（缓存、别的组装路径），展示顺序在渲染层自己定。"""
    hours = [{"hour": "2026-09-09 10", "symbols": 1, "expected": 17, "present": 17,
              "rate": 1.0, "missing": {}},
             {"hour": "2026-09-09 12", "symbols": 1, "expected": 17, "present": 17,
              "rate": 1.0, "missing": {}},
             {"hour": "2026-09-09 11", "symbols": 1, "expected": 17, "present": 17,
              "rate": 1.0, "missing": {}}]
    report = "\n".join(health_report._hourly_table({"hours": hours}))
    labels = [l.split("|")[1].strip() for l in report.splitlines()
              if l.startswith("| ") and ":00" in l]
    assert labels == ["12:00", "11:00", "10:00"], "新的排前面，且不随输入顺序变"


def test_hourly_table_adds_date_prefix_when_window_crosses_days():
    """窗口跨两天以上时，光写 21:00 分不出是哪天的 21 点。"""
    hours = [{"hour": "2026-09-09 21", "symbols": 1, "expected": 17, "present": 17,
              "rate": 1.0, "missing": {}},
             {"hour": "2026-09-09 10", "symbols": 1, "expected": 17, "present": 17,
              "rate": 1.0, "missing": {}},
             {"hour": "2026-09-08 21", "symbols": 1, "expected": 17, "present": 17,
              "rate": 1.0, "missing": {}}]
    report = "\n".join(health_report._hourly_table({"hours": hours}))
    assert "| 09-09 21:00 |" in report
    assert "| 09-09 10:00 |" in report
    assert "| 09-08 21:00 |" in report
    # 同一天内不重复日期——上面那条同日测试已经钉住 "| 10:00 |" 的形态。


def _lat_stats():
    """耗时段测试用的统计值：字段跟 digest 输出一致。"""
    return {"n": 10, "avg": 1.2, "p50": 1.0, "p90": 2.0, "p95": 2.5, "max": 3.0}


def test_latency_trend_note_prefixes_dates_when_hours_cross_days():
    """00 点比的是前一天 23 点，光写小时读者会当成同一天。"""
    data = _data(latency={"brief": {
        "stats": _lat_stats(),
        "trend": {"hour": "2026-09-09 00", "previous": "2026-09-08 23",
                  "change_pct": 10.0, "cross_phase": False}}})
    report = "\n".join(health_report._latency_section(data))
    assert "09-09 00:00 这一小时" in report
    assert "09-08 23:00 那一小时" in report


def test_slow_table_prefixes_dates_when_window_crosses_days():
    """窗口跨天时，最慢几次的时刻也要带日期；同一天内不加。"""
    slow = {"symbol": "SZ000333", "tool": "brief", "at": "2026-09-08 23:40:12",
            "total": 25.0, "present": 17, "expected": 17, "missing": []}
    window = {"version": "2.0.0", "files": ["cn-stock-mcp.log"],
              "path": "/x/cn-stock-mcp.log"}
    crossed = _data(window={**window, "from": "2026-09-08 21:00",
                            "to": "2026-09-09 09:00"},
                    symbols=[slow],
                    latency={"brief": {"stats": _lat_stats(), "trend": None}})
    same_day = _data(window={**window, "from": "2026-09-09 09:00",
                             "to": "2026-09-09 15:00"},
                     symbols=[{**slow, "at": "2026-09-09 10:40:12"}],
                     latency={"brief": {"stats": _lat_stats(), "trend": None}})
    assert "| 09-08 23:40:12 |" in "\n".join(health_report._latency_section(crossed))
    assert "| 10:40:12 |" in "\n".join(health_report._latency_section(same_day))


def test_events_span_prefixes_dates_when_an_event_crosses_days():
    """事件首末跨天时时段写全日期；同一天内不加，免得每行都拖个前缀。"""
    crossed = _data(events={"source_switch": {
        "count": 3, "first": "2026-09-08 21:15", "last": "2026-09-09 09:40",
        "detail": "换源", "items": None}})
    same_day = _data(events={"source_switch": {
        "count": 2, "first": "2026-09-09 10:05", "last": "2026-09-09 11:30",
        "detail": "换源", "items": None}})
    assert "09-08 21:15 → 09-09 09:40" in "\n".join(health_report._events_section(crossed))
    line = next(l for l in "\n".join(health_report._events_section(same_day)).splitlines()
                if "source_switch" in l)
    assert "09-" not in line


# --- 结论 --------------------------------------------------------------------


def _verdict_line(report: str) -> str:
    """结论那一行。按特征找，不按行号——行号会随排版调整而漂。"""
    return next(l for l in report.splitlines() if l.startswith("**") and l.endswith("**"))


def _kpi_row(report: str) -> str:
    """抬头那张 KPI 表的数据行。"""
    lines = report.splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith("| 时间窗 |"))
    return lines[header + 2]


def _data(**over):
    # window.files 不能省：读不到日志时判定要走"无数据"分支，省掉它这里就全是
    # ❓，测不到真正的阈值。
    base = {"window": {"from": "a", "to": "b", "version": "2.0.0",
                       "files": ["cn-stock-mcp.log"], "path": "/x/cn-stock-mcp.log"},
            # 字段要跟真实 digest 一致：渲染"最慢的几次"会读 total 和 at
            "symbols": [{"symbol": "SZ000333", "tool": "brief", "at": "2026-09-09 10:00:01",
                         "total": 1.1, "present": 17, "expected": 17, "missing": []}],
            "availability": {"rate": 1.0, "expected": 10, "present": 10,
                             "method": "measured"},
            "missing": [], "degraded": {}, "failed_sources": {},
            "latency": {}, "events": {}}
    base.update(over)
    return base


def test_verdict_is_ok_when_nothing_is_wrong():
    assert health_report._verdict(_data())[0] == "✅"


def test_verdict_warns_below_the_availability_threshold():
    icon, why = health_report._verdict(_data(availability={"rate": 0.94, "method": "measured"}))
    assert icon == "⚠️" and "94" in why


def test_verdict_is_bad_on_a_crash():
    icon, _ = health_report._verdict(_data(events={"session_crash": {"count": 2}}))
    assert icon == "❌"


def test_verdict_flags_a_long_tail_even_when_availability_is_perfect():
    """可用率满分但有一次 89 秒——那也是问题，调用方那边先看到的是超时。"""
    icon, why = health_report._verdict(_data(latency={
        "K 线取数": {"stats": {"n": 100, "avg": 4.0, "p50": 2.0, "p90": 7.0,
                            "p95": 15.0, "max": 89.3}, "trend": None}}))
    assert icon == "⚠️" and "89" in why


@pytest.mark.parametrize("since,expect", [
    ("startup", None),
    ("30m", dt.datetime(2026, 9, 9, 9, 30)),
    ("2h", dt.datetime(2026, 9, 9, 8, 0)),
    ("today", dt.datetime(2026, 9, 9, 0, 0)),
    ("2026-09-08 13:00", dt.datetime(2026, 9, 8, 13, 0)),
    ("胡说八道", None),
])
def test_since_parsing(since, expect):
    assert log_digest.resolve_since(since, dt.datetime(2026, 9, 9, 10, 0)) == expect



# --- 没有数据 ≠ 没有问题 ------------------------------------------------------


def test_an_unreadable_log_is_not_a_green_light(tmp_path):
    """读不到日志时不能报"一切正常"。

    统计全是 0，照常算下去每一项都合格。一块什么都没接的大屏亮着绿灯，比没有这块屏
    更糟。而且这不是假想：LOG_FILE 的默认值按包的位置推，发布形态安装时指向
    site-packages 下并不存在的路径，没经 start.sh 导入变量就正好落进来。
    """
    data = log_digest.digest(str(tmp_path / "nope.log"))
    icon, why = health_report._verdict(data)
    assert icon == "❓" and "读不到日志" in why
    assert "✅" not in health_report.render(data)
    assert "LOG_FILE" in health_report.render(data)   # 说清楚该去查什么


def test_an_empty_but_readable_log_says_there_is_nothing_to_judge(tmp_path):
    """日志读到了但窗口内没有调用——同样没有判断依据，别说"正常"。"""
    path = tmp_path / "cn-stock-mcp.log"
    path.write_text("2026-09-09 10:00:00,000 INFO 服务启动\n", encoding="utf-8")
    icon, why = health_report._verdict(log_digest.digest(str(path)))
    assert icon == "❓" and "没有" in why


def test_a_real_log_still_gets_a_real_verdict(tmp_path):
    """别把上面两条修成"一律 ❓"——有数据时该给结论还是要给。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    assert health_report._verdict(log_digest.digest(log))[0] == "✅"


def test_latency_alone_still_counts_as_data(tmp_path):
    """标的全失败时 symbols 是空的，但 Data task 那些行还在——服务确实在干活。

    只看 symbols 会把这种情况判成"没有可判断的依据"，而那正是最该给出结论的时候。
    """
    log = _write(tmp_path, [
        _line("10:00:01", "Data task _fetch_kline_sync request_id=r1 tool=brief "
                          "symbol=SZ000333 admission=0.0s queue=0.0s service=88.0s"),
    ])
    icon, why = health_report._verdict(log_digest.digest(log))
    assert icon != "❓", why


def test_the_report_says_which_log_it_read(tmp_path):
    """写文件名，不写路径。

    LOG_FILE 可被 .env 覆盖（main.py 是 load_dotenv(override=True)），指到一个
    **存在但过期**的日志时不会报错。露出破绽的是抬头的 from → to 时间戳，不是路径
    ——路径印出来等于把服务器目录结构发给调用方，见下面那条。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    report = health_report.render(log_digest.digest(log))
    assert "数据来自 cn-stock-mcp.log" in report
    assert log not in report


# --- 单维可用率 --------------------------------------------------------------


def _dims(data):
    return {r["dimension"]: r for r in data["dimensions"]}


def test_the_denominator_follows_the_tool_not_the_call_count(tmp_path):
    """brief 不要求历史资金流向，full 要求——同一维在两个工具下分母不同。

    按"调用了几次"当分母会让 brief 把历史资金流向记成缺失，而它本来就不该有。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17, tool="brief"),
        _symbol_line("10:00:02", "SZ000333", 2.0, present=20, expected_n=20, tool="full"),
    ])
    dims = _dims(log_digest.digest(log))
    assert dims["历史资金流向"]["expected"] == 1     # 只有那次 full 算数
    assert dims["价格"]["expected"] == 2            # 两个工具都要求
    assert dims["历史资金流向"]["rate"] == 1.0


def test_the_denominator_also_follows_the_symbol_class(tmp_path):
    """ETF 没有财务报表、指数没有市值——不进分母，不是缺失。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=20, expected_n=20, tool="full"),
        _symbol_line("10:00:02", "SH512480", 2.0, present=11, expected_n=11, tool="full"),
        _symbol_line("10:00:03", "SH000001", 2.0, present=11, expected_n=11, tool="full"),
    ])
    dims = _dims(log_digest.digest(log))
    assert dims["财务数据"]["expected"] == 1        # 只有个股
    assert dims["总市值"]["expected"] == 1
    assert dims["历史资金流向"]["expected"] == 3    # 三类都算
    assert all(r["rate"] == 1.0 for r in dims.values())


def test_a_degraded_section_counts_as_not_returned(tmp_path):
    """段落在但写着"暂无…"要算没拿到——问的是返回了数据没有。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17,
                     degraded="暂无资金流向数据"),
        _symbol_line("10:00:02", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    flow = _dims(log_digest.digest(log))["资金流向"]
    assert (flow["expected"], flow["degraded"], flow["got"]) == (2, 1, 1)
    assert flow["rate"] == 0.5
    assert flow["symbols"] == ["SZ000333"]


def test_a_pinned_date_query_is_not_a_fund_flow_failure(tmp_path):
    """钉日期的查询没有"实时"资金流可言——正当缺席，摘出分母，不算降级。

    真踩过：一批钉日期的基线重放让资金流可用率从 100% 掉到 64.3%，而什么都没坏。
    换任何源结果都一样，和 ETF 没有财务报表是同一类。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17,
                     degraded="指定日期查询暂不展示实时资金流向"),
        _symbol_line("10:00:02", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    data = log_digest.digest(log)
    flow = _dims(data)["资金流向"]
    assert (flow["expected"], flow["degraded"], flow["rate"]) == (1, 0, 1.0)
    assert data["not_applicable"] == {"指定日期查询暂不展示实时资金流向": 1}
    assert data["degraded"] == {}                       # 没混进降级
    assert "正当缺席" in health_report.render(data)      # 但要说出来，不是悄悄扣掉


def test_lines_without_the_measured_fields_stay_out_of_the_denominator(tmp_path):
    """老日志没有 present= 时不知道它缺了什么，按"零缺失"计入会把可用率抬高。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0),                       # 老格式
        _symbol_line("10:00:02", "SZ000333", 2.0, present=16, expected_n=17,
                     missing="资金流向"),
    ])
    flow = _dims(log_digest.digest(log))["资金流向"]
    assert flow["expected"] == 1 and flow["rate"] == 0.0


def test_the_attribution_tables_agree_with_the_contract():
    """归属表写错一个字就会把降级记到无辜的维度头上，或者永远匹配不上。"""
    names = {d.name for dims in rc.CONTRACT.values() for d in dims}
    for table in (rc.DEGRADED_DIMENSION, rc.NOT_APPLICABLE_MARKERS):
        assert set(table) <= set(rc.DEGRADED_MARKERS), "标记不在 DEGRADED_MARKERS 里"
        assert set(table.values()) <= names, "归到了契约里没有的维度"
    assert not (set(rc.DEGRADED_DIMENSION) & set(rc.NOT_APPLICABLE_MARKERS)), \
        "同一句既算降级又算正当缺席"


# --- 别把服务器路径带出去 -----------------------------------------------------


def test_the_report_never_leaks_the_server_path(tmp_path):
    """报告会发给 MCP 调用方，绝对路径会把服务器的目录结构一起带出去。

    读没读错文件由抬头的 from → to 时间戳露出来（读到过期或别的实例的日志，窗口
    结束时刻就对不上刚才那次调用），不需要为此把路径印出来。
    """
    deep = tmp_path / "SECRETDIR" / "acme-prod-01" / "srv"
    deep.mkdir(parents=True)
    log = _write(deep, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    data = log_digest.digest(log)
    report = health_report.render(data)
    blob = json.dumps(data, ensure_ascii=False, default=str)

    for secret in ("SECRETDIR", "acme-prod-01", str(tmp_path)):
        assert secret not in report, f"渲染输出里带出了 {secret}"
        assert secret not in blob, f"digest 结构里带出了 {secret}"
    assert "cn-stock-mcp.log" in report          # 文件名还是要写，只是不带路径


def test_an_unreadable_log_reports_the_name_not_the_path(tmp_path):
    """读不到时同样只写文件名。该查什么由"请检查 LOG_FILE"这句指出来。"""
    _, why = health_report._verdict(log_digest.digest(str(tmp_path / "deep" / "nope.log")))
    assert "nope.log" in why and "LOG_FILE" in why
    assert str(tmp_path) not in why


# --- 结论的优先级：按对数据的影响排 -------------------------------------------


def test_a_silent_caliber_change_reaches_the_verdict():
    """口径变了要上结论。

    数据还在、可用率照样满分，但同一个字段换了含义——报告上看不出来，比"取不到"
    更难发现。踩过：一次 1572/1572 全绿的运行，头条写的是"fund_flow 源失败 38 次"
    （兜底已补齐），而"指数 K 线换源、成交量口径变了 16 次"只出现在最底下的事件表里。
    """
    icon, why = health_report._verdict(_data(events={"caliber_change": {"count": 16}}))
    assert icon == "⚠️" and "口径" in why


def test_a_covered_source_failure_ranks_behind_the_caliber_change():
    icon, why = health_report._verdict(_data(
        events={"caliber_change": {"count": 16}}, failed_sources={"fund_flow": 38}))
    assert why.index("口径") < why.index("fund_flow"), why
    assert "兜底" in why, "没说清数据其实是齐的"


def test_a_crash_is_stated_before_the_softer_problems():
    """最重的排最前。读者只看第一句时，那一句得是最要紧的。"""
    _, why = health_report._verdict(_data(
        events={"session_crash": {"count": 1}, "caliber_change": {"count": 3}},
        availability={"rate": 0.9, "method": "measured"}))
    assert why.startswith("会话崩溃"), why


def test_a_covered_source_failure_is_not_reported_as_missing_data():
    """维度一处不缺时，"缺失最多"那一格该写"无"。

    原先的条件是"没有缺失就退回源级"，于是一次全绿的运行在这一格写着
    "fund_flow（源）38 次"，而那 38 次已经被兜底补上了——同一份报告里
    可用率 100%、缺失 38 次，自相矛盾。
    """
    report = health_report.render(_data(failed_sources={"fund_flow": 38}))
    kpi = _kpi_row(report)
    assert "| 无 " in kpi, kpi
    assert "38" not in kpi


def test_an_old_log_still_falls_back_to_source_level():
    """没有维度级明细时退回源级——那时写"无"才是撒谎。"""
    report = health_report.render(_data(
        availability={"rate": None, "method": "inferred"},
        failed_sources={"fund_flow": 38}))
    assert "fund_flow（源） 38 次" in _kpi_row(report)


# --- 大屏：先给问题 ----------------------------------------------------------


def test_every_dimension_gets_its_own_row(tmp_path):
    """满分的维度也要单独成行。

    曾经把 100% 的行折成一句汇总，理由是"大屏要先给问题"——结果把这一节存在的理由
    弄没了，它就是给人逐维核对的。差的排前面并加粗就够了，不需要靠删行。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    data = log_digest.digest(log)
    report = health_report.render(data)
    for row in data["dimensions"]:
        assert f"| {row['dimension']} | {row['source']} |" in report, row["dimension"]
    assert "维全部拿到" not in report, "又折叠了"


def test_a_failing_dimension_still_gets_its_own_row(tmp_path):
    """别把上面那条修成"一律折叠"——有问题的必须单独成行，带上标的。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
        _symbol_line("10:00:02", "SH512480", 2.0, present=8, expected_n=9, missing="资金流向"),
    ])
    report = health_report.render(log_digest.digest(log))
    row = [l for l in report.splitlines() if l.startswith("| 资金流向 |")]
    assert row and "SH512480" in row[0], report
    header = [l for l in report.splitlines() if l.startswith("| 维度 |")][0]
    assert len(row[0].strip("|").split("|")) == len(header.strip("|").split("|"))


def test_the_latency_footnote_names_the_two_hours_it_compared(tmp_path):
    """列名和脚注都不许再叫"环比"。

    会计意义的环比是"与上一期相比"，而它比的是本小时对上一小时——现在名副其实了，
    但脚注仍要写清是哪两个小时，否则读者还是得猜。
    """
    lines = []
    for hour in (10, 11):
        for i in range(30):
            lines.append(_line(f"{hour}:{i:02d}:00",
                               "Data task _fetch_kline_sync request_id=r tool=brief "
                               "symbol=SZ000333 admission=0.0s queue=0.0s service=1.0s"))
    report = health_report.render(log_digest.digest(_write(tmp_path, lines)))
    assert "| 本小时 vs 上一小时 |" in report
    assert "环比" not in report
    assert "11:00 这一小时的 p90 比 10:00 那一小时" in report


def test_no_latency_rows_means_no_table_at_all(tmp_path):
    """一行数据都没有时别画个空表——只有表头的表读者会以为渲染坏了。"""
    log = _write(tmp_path, [_line("10:00:00", "cn-stock-mcp version=2.0.0")])
    report = health_report.render(log_digest.digest(log))
    assert "窗口内没有取数记录" in report
    assert "| 阶段 | 次数 |" not in report


def test_no_calls_at_all_does_not_claim_the_rate_was_inferred(tmp_path):
    """一次调用都没有时 method 也是 inferred，但那时说"按上游失败推算"是无稽之谈。"""
    log = _write(tmp_path, [_line("10:00:00", "cn-stock-mcp version=2.0.0")])
    report = health_report.render(log_digest.digest(log))
    assert "推算" not in report
    assert "没有报告类调用" in report


def test_a_clean_run_with_source_switches_does_not_claim_everything_is_normal():
    """数据齐、没变口径，但期间换过源——写"一切正常"和下面的事件表自相矛盾。

    也不能升成 ⚠️：网关关闭时浏览器兜底是稳态，天天亮黄灯的告警等于没有告警。
    """
    icon, why = health_report._verdict(_data(events={
        "impersonate_cooldown": {"count": 1}, "source_breaker_open": {"count": 1}}))
    assert icon == "✅"
    assert "换过 2 次源" in why and "一切正常" not in why


def test_a_genuinely_clean_run_still_says_everything_is_normal():
    """别把上面那条修成"永远不说正常"。"""
    assert health_report._verdict(_data())[1] == "一切正常"


# --- 归档日志：轮转与聚合 -----------------------------------------------------


def _touch(path, days_ago=0, body="x\n"):
    path.write_text(body, encoding="utf-8")
    when = dt.datetime.now() - dt.timedelta(days=days_ago, hours=1)
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def _rotation_snippet() -> str:
    """从 start.sh 里抽出轮转那一段，原样跑。

    抄一份到测试里就成了"测试我抄的那份"——真正出货的那段改了测试照样绿。
    锚点是那两行，start.sh 重构掉它们时这条测试会直接报错，那时本来就该回来看一眼。
    """
    script = (Path(__file__).resolve().parents[1] / "start.sh").read_text(encoding="utf-8")
    start = script.index('if [ -s "$LOG_FILE" ]; then')
    end = script.index('echo "日志文件: $LOG_FILE"')
    return script[start:end]


def _run_rotation(tmp_path, retention="3"):
    log_dir = tmp_path / "logs"
    log_file = log_dir / "cn-stock-mcp.log"
    program = (f'set -e\nLOG_DIR={log_dir!s}\nLOG_FILE={log_file!s}\n'
               f'LOG_RETENTION_DAYS={retention}\n' + _rotation_snippet())
    proc = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return sorted(p.name for p in log_dir.iterdir())


def test_rotation_keeps_one_archive_per_start(tmp_path):
    """归档名带启动时刻。原先固定叫 .bak 只留一代，一天重启两次就把更早那次冲掉。"""
    (tmp_path / "logs").mkdir()
    _touch(tmp_path / "logs" / "cn-stock-mcp.log", body="上一轮\n")
    names = _run_rotation(tmp_path)
    archives = [n for n in names if n != "cn-stock-mcp.log"]
    assert len(archives) == 1
    assert re.fullmatch(r"cn-stock-mcp\.log\.\d{8}-\d{6}", archives[0]), archives


def test_rotation_prunes_by_age_and_spares_other_logs(tmp_path):
    """保留近三天，更早的清掉。

    文件名不带日期含义——清理看的是 mtime，跟名字里写什么无关，所以这里故意用
    ``.aged<N>`` 命名，免得读者以为按名字算。当前那份没有后缀，绝不能被匹配到；
    同目录下别的日志（Xvfb 那份）也不能被误删。
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for days in (1, 2, 3, 4, 9):
        _touch(log_dir / f"cn-stock-mcp.log.aged{days}", days_ago=days)
    _touch(log_dir / "cn-stock-mcp.log.bak", days_ago=30)     # 旧命名也归这条管
    _touch(log_dir / "cn-stock-mcp-xvfb.log", days_ago=30)    # 别名文件不许被误删
    _touch(log_dir / "cn-stock-mcp.log", body="当前\n")

    names = _run_rotation(log_dir.parent)
    assert "cn-stock-mcp-xvfb.log" in names, "清理误伤了别的日志"
    kept = {n for n in names if ".aged" in n or n.endswith(".bak")}
    assert kept == {"cn-stock-mcp.log.aged1", "cn-stock-mcp.log.aged2",
                    "cn-stock-mcp.log.aged3"}, sorted(kept)
    # 当前那份被改名成了新归档；重建空文件是 nohup 重定向干的，不在这段里。
    fresh = [n for n in names if re.fullmatch(r"cn-stock-mcp\.log\.\d{8}-\d{6}", n)]
    assert len(fresh) == 1
    assert (log_dir / fresh[0]).read_text() == "当前\n"


def test_zero_retention_keeps_no_archive_at_all(tmp_path):
    """0 的意思是一份不留。

    find 的 ``-mtime +0`` 是"超过 24 小时"，当天刚归档的那份删不掉——0 必须单独处理，
    否则文档写着"不保留"而实际上留着今天的。
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _touch(log_dir / "cn-stock-mcp.log.20260909-120000")      # 刚归档的
    _touch(log_dir / "cn-stock-mcp.log", body="当前\n")
    names = _run_rotation(log_dir.parent, retention="0")
    assert [n for n in names if n != "cn-stock-mcp.log"] == []


def test_archived_logs_are_aggregated_by_default(tmp_path):
    log = _write(tmp_path, [_symbol_line("11:00:01", "SZ000333", 2.0, present=17, expected_n=17)])
    _touch(tmp_path / "cn-stock-mcp.log.20260909-100000",
           body=_symbol_line("10:00:01", "SH600519", 2.0, present=17, expected_n=17) + "\n")
    assert len(log_digest.digest(log)["symbols"]) == 2
    assert len(log_digest.digest(log, archived=False)["symbols"]) == 1


def test_files_are_ordered_by_mtime_not_by_name(tmp_path):
    """窗口两头取的是第一条和最后一条时刻，顺序错了两头就错。

    按名字排靠不住：旧的 ``.bak`` 和新的 ``.20260909-152319`` 混在一起时，
    字典序会把 ``.bak``（其实最旧）排到最新那批之后。
    """
    log = _write(tmp_path, [_symbol_line("12:00:01", "SZ000333", 2.0, present=17, expected_n=17)])
    _touch(tmp_path / "cn-stock-mcp.log.bak", days_ago=2,
           body=_symbol_line("09:00:01", "SH600519", 2.0, present=17, expected_n=17) + "\n")
    _touch(tmp_path / "cn-stock-mcp.log.20260909-110000", days_ago=1,
           body=_symbol_line("11:00:01", "SH601318", 2.0, present=17, expected_n=17) + "\n")
    window = log_digest.digest(log)["window"]
    assert window["files"] == ["cn-stock-mcp.log.bak",
                              "cn-stock-mcp.log.20260909-110000",
                              "cn-stock-mcp.log"]
    assert window["from"].endswith("09:00:01") and window["to"].endswith("12:00:01")


def test_the_header_says_how_many_archives_were_read(tmp_path):
    """窗口一下子从几分钟变成几天，读者得知道是因为把归档也算了。"""
    log = _write(tmp_path, [_symbol_line("11:00:01", "SZ000333", 2.0, present=17, expected_n=17)])
    _touch(tmp_path / "cn-stock-mcp.log.20260909-100000",
           body=_symbol_line("10:00:01", "SH600519", 2.0, present=17, expected_n=17) + "\n")
    assert "+ 1 份归档" in health_report.render(log_digest.digest(log))
    assert "（未计归档）" in health_report.render(log_digest.digest(log, archived=False))


def test_an_unreadable_log_directory_falls_back_to_the_current_file(tmp_path):
    """别让整个工具挂在一次 listdir 上。"""
    log = _write(tmp_path, [_symbol_line("11:00:01", "SZ000333", 2.0, present=17, expected_n=17)])
    assert log_digest.log_files(log, "startup", archived=True)
    assert log_digest.log_files(str(tmp_path / "gone" / "x.log"), "startup") == []


# --- 排版规矩 ----------------------------------------------------------------


def _sections(report: str) -> list:
    return [l[3:] for l in report.splitlines() if l.startswith("## ")]


def test_the_availability_percentage_is_on_the_first_line(tmp_path):
    """可用率是这份报告的那个数，扫第一行就该看到，不该往下找一节。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=16, expected_n=17,
                     missing="资金流向"),
    ])
    report = health_report.render(log_digest.digest(log))
    first = report.splitlines()[0]
    assert first.startswith("# 服务健康")
    assert "94.1%" in first, first
    assert "## 可用率" not in report, "标题里有了就别再单开一节"


def test_section_headings_are_section_names_not_content(tmp_path):
    """`## ⚠️ 资金流向缺了 2 次` 当标题读起来像备注。

    小标题该是"各维度可用率""耗时"这种 section 名；结论是内容，用粗体行。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=16, expected_n=17,
                     missing="资金流向"),
    ])
    report = health_report.render(log_digest.digest(log))
    assert not any(("⚠️" in s or "✅" in s or "❌" in s or "❓" in s) for s in _sections(report)), \
        _sections(report)
    verdict = _verdict_line(report)
    assert verdict.startswith("**⚠️ "), verdict
    assert "资金流向缺了 1 次" in verdict
    assert verdict.count("94.1%") == 0, "标题里已经有这个数了，别再说一遍"


def test_tables_come_before_their_notes(tmp_path):
    """有表格的一节，数据在上、备注在下——不该先读三行解释才看到数。"""
    lines = []
    for hour in (10, 11):
        for i in range(30):
            lines.append(_line(f"{hour}:{i:02d}:00",
                               "Data task _fetch_kline_sync request_id=r tool=brief "
                               "symbol=SZ000333 admission=0.0s queue=0.0s service=1.0s"))
    lines.append(_symbol_line("11:00:01", "SZ000333", 2.0, present=17, expected_n=17,
                              degraded="指定日期查询暂不展示实时资金流向"))
    report = health_report.render(log_digest.digest(_write(tmp_path, lines)))

    body = report.splitlines()
    for heading in ("各维度可用率", "耗时"):
        at = body.index(f"## {heading}")
        rest = body[at + 1:]
        stop = next((i for i, l in enumerate(rest) if l.startswith("## ")), len(rest))
        block = rest[:stop]
        table = next(i for i, l in enumerate(block) if l.startswith("|"))
        quotes = [i for i, l in enumerate(block) if l.startswith(">")]
        assert quotes, f"{heading} 一节没有备注"
        assert min(quotes) > table, f"{heading} 的备注跑到表格前面了"


def test_consecutive_notes_do_not_merge_into_one_paragraph(tmp_path):
    """连续的 "> " 行在 Markdown 里会并成同一段，两条备注挤成一句读不出是两件事。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17,
                     degraded="指定日期查询暂不展示实时资金流向"),
    ])
    report = health_report.render(log_digest.digest(log))
    assert "\n>\n> " in report, "两条备注之间没有垫空引用行"


def test_the_window_edges_do_not_depend_on_line_order(tmp_path):
    """窗口两头取 min/max，不取"第一条 / 最后一条"。

    聚合归档时读的是好几个文件，而且服务是多线程写日志，同一秒内的行本来就可能
    乱序——按出现顺序取的话，末尾一条稍早的行就能把窗口尾巴拽回去。
    """
    log = _write(tmp_path, [
        _symbol_line("12:00:00", "SZ000333", 2.0, present=17, expected_n=17),
        _symbol_line("15:00:00", "SZ000333", 2.0, present=17, expected_n=17),
        _line("13:00:00", "cn-stock-mcp version=2.0.0"),      # 乱序的一行落在末尾
    ])
    window = log_digest.digest(log)["window"]
    assert window["from"].endswith("12:00:00")
    assert window["to"].endswith("15:00:00"), window["to"]


def test_the_provenance_line_comes_right_after_the_title(tmp_path):
    """版本 / 指纹 / 读了哪些文件是这份报告的**出处**，紧跟标题。

    底下每个数都只在这个前提下成立。曾经把它放到 KPI 表下面当脚注，那要读者读完数
    再回头确认前提——顺序反了。
    """
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    lines = health_report.render(log_digest.digest(log)).splitlines()
    assert lines[0].startswith("# 服务健康")
    assert lines[2].startswith("> 版本 "), lines[:5]
    assert "数据来自 cn-stock-mcp.log" in lines[2]
    # 结论和 KPI 表都排在它后面
    assert lines.index(_verdict_line("\n".join(lines))) > 2
    assert "版本 " not in "\n".join(lines[3:]), "元信息只该出现一次"
