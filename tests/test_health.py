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


# --- 结论 --------------------------------------------------------------------


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
    kpi = [l for l in report.splitlines() if l.startswith("| **")][0]
    assert "| 无 " in kpi, kpi
    assert "38" not in kpi


def test_an_old_log_still_falls_back_to_source_level():
    """没有维度级明细时退回源级——那时写"无"才是撒谎。"""
    report = health_report.render(_data(
        availability={"rate": None, "method": "inferred"},
        failed_sources={"fund_flow": 38}))
    kpi = [l for l in report.splitlines() if l.startswith("| **")][0]
    assert "fund_flow（源） 38 次" in kpi, kpi


# --- 大屏：先给问题 ----------------------------------------------------------


def test_all_green_dimensions_collapse_to_one_line(tmp_path):
    """二十行 100% 会把有问题的那行挤到屏幕外，而大屏的用处就是一眼看到问题。"""
    log = _write(tmp_path, [
        _symbol_line("10:00:01", "SZ000333", 2.0, present=17, expected_n=17),
    ])
    report = health_report.render(log_digest.digest(log))
    assert "维全部拿到" in report
    assert "| 价格 | kline |" not in report          # 满分的不单独占行
    assert "该有" in report                          # 分母范围还在，"有没有缺"照样答得上


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
