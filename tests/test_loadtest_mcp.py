"""scripts/loadtest_mcp.py 闭环驱动自己的逻辑。

只测驱动：并发度守不守、截止后还发不发、工具混合按不按权重、吞吐怎么算。
不起实例、不碰网络——调用函数换成假的。
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "loadtest_mcp", Path(__file__).resolve().parents[1] / "scripts" / "loadtest_mcp.py"
)
loadtest = importlib.util.module_from_spec(_SPEC)
sys.modules["loadtest_mcp"] = loadtest  # dataclass 要能在 sys.modules 里找到所属模块
_SPEC.loader.exec_module(loadtest)

POOL = [f"SH60000{i}" for i in range(8)]
MIX = [("brief", 4, 1)]


def _tracker() -> dict:
    return {"in_flight": 0, "peak": 0, "started": [], "tools": []}


def _fake_call(hold: float, tracker: dict):
    async def call(url, tool, symbols, timeout):
        tracker["in_flight"] += 1
        tracker["peak"] = max(tracker["peak"], tracker["in_flight"])
        tracker["started"].append(time.perf_counter())
        tracker["tools"].append(tool)
        await asyncio.sleep(hold)
        tracker["in_flight"] -= 1
        return loadtest.CallResult(
            tool=tool, started_at=time.time(), elapsed=hold, ok=True, reports=len(symbols)
        )

    return call


def test_closed_loop_holds_concurrency_and_refills(monkeypatch):
    monkeypatch.setattr(loadtest, "CLOSED_LOOP_STAGGER_S", 0.0)
    tracker = _tracker()
    results = asyncio.run(
        loadtest.run_closed_loop("u", 3, 0.3, MIX, POOL, 5.0, call=_fake_call(0.05, tracker))
    )
    # 在途数恒等于并发数：既不超发，也不会发满 3 个就停
    assert tracker["peak"] == 3
    assert len(results) > 3
    assert all(r.ok for r in results)


def test_closed_loop_stops_launching_at_deadline(monkeypatch):
    monkeypatch.setattr(loadtest, "CLOSED_LOOP_STAGGER_S", 0.0)
    tracker = _tracker()
    duration, hold = 0.2, 0.05
    t0 = time.perf_counter()
    asyncio.run(
        loadtest.run_closed_loop("u", 2, duration, MIX, POOL, 5.0, call=_fake_call(hold, tracker))
    )
    total = time.perf_counter() - t0
    # 截止后不再发起新调用；已在途的跑完才返回，所以最多多出一次调用的时长
    assert all(started - t0 < duration + 0.05 for started in tracker["started"])
    assert duration <= total < duration + hold + 0.2


def test_closed_loop_staggers_worker_start(monkeypatch):
    """每个 worker 起步前按 slot × stagger 错开。

    **不看墙钟。** 原先是量前三次调用的时刻差，那样必然偶发：worker 睡到
    (workers-1)*stagger 才醒、醒来先查截止时间，机器一卡它就一次调用都发不出，
    断言取 started[2] 抛 IndexError。把 duration 从 0.15s 提到 0.4s 只是把能扛的
    事件循环停顿从 140ms 抬到 380ms，减少了但消除不了——这条测试本质依赖时间。

    改成记录 worker 向 asyncio.sleep 要了多久：那就是错开这件事本身，与机器快慢无关。
    """
    stagger, workers = 0.05, 3
    monkeypatch.setattr(loadtest, "CLOSED_LOOP_STAGGER_S", stagger)
    delays = []
    real_sleep = asyncio.sleep

    async def spy(delay, *args, **kwargs):
        delays.append(delay)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", spy)
    tracker = _tracker()
    asyncio.run(
        loadtest.run_closed_loop("u", workers, 0.05, MIX, POOL, 5.0,
                                 call=_fake_call(0.0, tracker))
    )
    # 假调用的 hold 是 0，所以非零的那些延时只可能来自错开。
    assert sorted({d for d in delays if d}) == [stagger, stagger * (workers - 1)], delays
    assert delays.count(0) >= 1, "第一个 worker 应当立刻起步（slot 0 × stagger = 0）"


def test_closed_loop_follows_tool_mix(monkeypatch):
    monkeypatch.setattr(loadtest, "CLOSED_LOOP_STAGGER_S", 0.0)
    tracker = _tracker()
    mix = [("brief", 4, 1), ("full", 4, 1)]
    asyncio.run(
        loadtest.run_closed_loop("u", 2, 0.2, mix, POOL, 5.0, call=_fake_call(0.02, tracker))
    )
    counts = {tool: tracker["tools"].count(tool) for tool in ("brief", "full")}
    # 轮转取工具，两者相差不超过 1
    assert counts["brief"] > 0 and counts["full"] > 0
    assert abs(counts["brief"] - counts["full"]) <= 1


def test_closed_loop_throughput_counts_successes_over_actual_elapsed():
    ok = loadtest.CallResult("brief", 0.0, 1.0, True)
    bad = loadtest.CallResult("brief", 0.0, 1.0, False, failure="client_timeout")
    # 6 次成功 / 30 秒 = 12 次/分，失败的不算吞吐
    assert loadtest.closed_loop_throughput([ok] * 6 + [bad], 0.0, 30.0) == 12.0


def test_default_steps_by_mode():
    assert loadtest.default_steps(closed_loop=True) == [1, 5, 10]
    assert loadtest.default_steps(closed_loop=False) == [5, 10, 15, 20, 30]


def test_parse_args_closed_loop_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["loadtest_mcp.py", "--closed-loop", "--steps", "1,5"])
    args = loadtest.parse_args()
    assert args.closed_loop is True
    assert args.steps == [1, 5]

    monkeypatch.setattr(sys, "argv", ["loadtest_mcp.py"])
    args = loadtest.parse_args()
    assert args.closed_loop is False
    assert args.steps is None  # 缺省由 default_steps 按模式补
    assert args.memory_budget == loadtest.MEMORY_BUDGET_MIB

    monkeypatch.setattr(sys, "argv", ["loadtest_mcp.py", "--memory-budget", "0"])
    assert loadtest.parse_args().memory_budget == 0


# ── 内存口径 ─────────────────────────────────────────────────────────


def _sample(at, rss, pss):
    return loadtest.MemorySample(at=at, rss_mib=rss, pss_mib=pss, processes=5)


def test_memory_report_prefers_pss_and_flags_budget():
    peak = loadtest.MemoryPeak(rss_mib=1482.0, pss_mib=840.0, samples=90, pss_samples=90)
    report = loadtest.memory_report(peak, budget=500.0)
    # 和预算比的是 PSS；RSS 合计另列，只当上界
    assert report["memory_metric"] == "pss"
    assert report["peak_mem_mib"] == 840.0
    assert report["peak_rss_mib"] == 1482.0
    assert report["over_budget"] is True


def test_peak_of_falls_back_to_rss_when_no_sample_has_pss():
    peak = loadtest.peak_of([_sample(1.0, 300.0, None), _sample(2.0, 420.0, None)])
    report = loadtest.memory_report(peak)
    assert report["memory_metric"] == "rss"
    assert report["peak_mem_mib"] == 420.0
    assert report["pss_samples"] == 0 and report["samples"] == 2


def test_peak_of_drops_incomplete_pss_samples_but_keeps_their_rss():
    window = [_sample(1.0, 600.0, 350.0), _sample(2.0, 900.0, None), _sample(3.0, 700.0, 400.0)]
    peak = loadtest.peak_of(window)
    # 没读全的那次 PSS 作废（宁可不给数也不给偏低的数），RSS 照常计入
    assert peak.pss_mib == 400.0
    assert peak.rss_mib == 900.0
    assert (peak.pss_samples, peak.samples) == (2, 3)


def test_peak_of_empty_window():
    peak = loadtest.peak_of([])
    assert peak.value == 0.0 and peak.metric == "rss"


def test_should_abort_respects_memory_budget_flag():
    report = {
        "memory": {"peak_mem_mib": 840.0, "memory_metric": "pss"},
        "server": {"hard_refusals": 0},
        "calls": 5,
        "transport_failures": 0,
        "upstream_failure_ratio": 0.2,
    }
    reason = loadtest.should_abort(report, 0.2, None, memory_budget=500.0)
    assert reason is not None and "840.0 MiB" in reason and "PSS" in reason
    # 0 = 只记录不中止
    assert loadtest.should_abort(report, 0.2, None, memory_budget=0) is None


def test_pss_reader_returns_none_for_missing_process():
    assert loadtest._pss_kib(2**22 + 12345) is None


# ── .env 会盖掉注入 ─────────────────────────────────────────────────


def test_shadowed_by_dotenv_lists_keys_present_in_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BATCH_CONCURRENCY=2\n# CACHE_DIR=/x\nFETCH_MAX_WORKERS=\n", encoding="utf-8")
    overrides = {"BATCH_CONCURRENCY": "4", "CACHE_DIR": "/tmp/c", "FETCH_MAX_WORKERS": "8"}
    # 注释掉的不算；写成 NAME= 的空值也算，override=True 会把空串写进环境
    assert loadtest.shadowed_by_dotenv(overrides, env) == ["BATCH_CONCURRENCY", "FETCH_MAX_WORKERS"]


def test_shadowed_by_dotenv_without_env_file(tmp_path):
    assert loadtest.shadowed_by_dotenv({"BATCH_CONCURRENCY": "4"}, tmp_path / ".env") == []
