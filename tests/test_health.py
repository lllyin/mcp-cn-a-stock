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


# --- 趋势：两个真踩过的坑 -------------------------------------------------------


def _series(pairs):
    return log_digest.Series([(f"2026-09-09 {t},000"[:19], v) for t, v in pairs])


def test_trend_needs_enough_samples_at_all():
    """样本不够就不判。十来个样本的 p90 是噪音。"""
    assert _series([(f"10:{i // 60:02d}:{i % 60:02d}", 1.0) for i in range(20)]).trend() is None


def test_trend_needs_enough_samples_on_each_half_not_just_in_total():
    """总数够、但集中在前半段——这种也不能判。

    按时间对半之后后半可能只剩几个样本，拿它们的 p90 跟前半比就是在比噪音。
    总数门槛拦不住这种形状:下面 105 个样本远超门槛，后半却只有 5 个。
    """
    dense = [(f"10:00:{s:02d}", 1.0) for s in range(60)] + \
            [(f"10:01:{s:02d}", 1.0) for s in range(40)]
    tail = [("11:00:00", 9.0), ("11:15:00", 9.0), ("11:30:00", 9.0),
            ("11:45:00", 9.0), ("12:00:00", 9.0)]
    assert _series(dense + tail).trend() is None


def test_trend_splits_by_time_not_by_sample_count():
    """按样本数对半是错的。

    真实日志里 10:10–10:38 有一段密集采样、之后是稀疏的正常调用，按样本数对半
    分出来基线 26 分钟、当前 4 小时 46 分，跨度差 11 倍。那段密集调用缓存全热、
    天然快，于是算出 +251% 的"性能下降"——纯粹是采样密度的假象。
    """
    dense = [(f"10:00:{s:02d}", 1.0) for s in range(60)]                   # 一分钟内 60 个快的
    sparse = [(f"{10 + m // 60:02d}:{m % 60:02d}:00", 2.0)                 # 之后 80 分钟每分钟一个慢的
              for m in range(1, 81)]
    trend = _series(dense + sparse).trend()
    assert trend is not None
    # 按样本数对半会切成"前半 70 个、后半 70 个"，基线里几乎全是那批天然快的，
    # 算出接近 +100% 的假性能下降。按时间中点（10:40）切，两边都以 2.0s 为主，
    # 真实结论是"没变化"。
    assert trend["change_pct"] == 0
    assert trend["baseline"]["n"] != trend["current"]["n"]  # 时间等长，样本数本就不该相等


def test_trend_never_compares_across_market_epochs():
    """跨时段不可比。盘中走缓存和页面，收盘后重新取数，本来就不是一回事。"""
    live = [(f"10:{m:02d}:00", 1.0) for m in range(60)]
    evening = [(f"17:{m:02d}:00", 9.0) for m in range(60)]
    epoch_of = lambda stamp: "live" if stamp[11:13] < "16" else "evening"   # noqa: E731
    trend = _series(live + evening).trend(epoch_of)
    assert trend is not None
    assert trend["epoch"] == "evening"          # 只在最近那个够样本的纪元内比
    assert trend["baseline"]["max"] == 9.0      # 没把盘中那 1.0s 混进来


def test_trend_skips_the_percentage_when_the_baseline_is_tiny():
    """基线 p90 接近 0 时不给百分比——多数是缓存命中的阶段，除出来的 ±100% 没意义。"""
    fast = [(f"10:{m:02d}:00", 0.0) for m in range(60)]
    slow = [(f"11:{m:02d}:00", 0.4) for m in range(60)]
    trend = _series(fast + slow).trend()
    assert trend is not None and trend["change_pct"] is None


# --- 结论 --------------------------------------------------------------------


def _data(**over):
    # window.files 不能省：读不到日志时判定要走"无数据"分支，省掉它这里就全是
    # ❓，测不到真正的阈值。
    base = {"window": {"from": "a", "to": "b", "version": "2.0.0",
                       "files": ["cn-stock-mcp.log"], "path": "/x/cn-stock-mcp.log"},
            "symbols": [{"symbol": "SZ000333"}],
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
