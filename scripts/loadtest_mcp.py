#!/usr/bin/env python3
"""MCP 负载测试：真实上游、真实传输、阶梯加压直到上游开始拒绝。

刻意不桩任何东西。客户端走 streamable HTTP 并且每次调用新建会话，与
mcporter 的行为一致；服务端走真实的 efinance / AkShare / Playwright 链路。
所以测出来的就是线上会遇到的行为，包括上游限流。

加压是阶梯式的，每一档跑固定时长后再决定是否继续，并在出现限流特征时立刻
停止。这样得到的是"曲线的拐点"，而不是把出口 IP 一次性刷死。

用法（先起一个隔离实例，再压它）：

    python scripts/loadtest_mcp.py --launch --port 8790 \\
        --steps 5,10,15,20,30 --step-seconds 90

只做一次冒烟（不加压，验证脚本本身）：

    python scripts/loadtest_mcp.py --launch --port 8790 --steps 3 --step-seconds 20

扫某个配置项（--env 注入被测实例，可重复）：

    for c in 2 3 4; do
        python scripts/loadtest_mcp.py --launch --port $((8790 + c)) \\
            --env BATCH_CONCURRENCY=$c \\
            --steps 15,22,30 --step-seconds 70 --seed 20260903
    done

各档用同一个 --seed 才可比：标的序列和发起节奏都由它决定。

报告里的"次/分钟"只对压测机成立。要折算到部署机（2 核 4G Ubuntu），用
cpu_seconds_per_call 配合 scripts/cpu_ref.py 量出的单核降级系数：

    部署机上限(次/分钟) = 核数 x 60 x 可用率 / (cpu_seconds_per_call x 降级系数)

cores_busy_avg 是同一件事的直读值：它在 8 核机上是小数，在 2 核机上乘以 4
就是实际利用率。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

MEMORY_BUDGET_MIB = 500.0  # AGENTS.md：含 Chromium、Xvfb 子进程的合计峰值上限
# 批次排队 P95 超过这个值就不算"可稳定支撑"：请求已经在等准入，加压只会让
# 队列更深，端到端耗时随之线性恶化。
QUEUE_TOLERANCE_S = 1.0

# 明确的限流状态码，出现即判定被限。
HARD_REFUSAL_PATTERNS = ("status_429", "status_403")
# 这条链路本来就会零星出现的形态：空响应体导致 json 解析失败、连接被关闭。
# 它们不能单独作为限流依据——本机在空载时就有基线，必须和基线比。
SOFT_REFUSAL_PATTERNS = (
    "Expecting value: line 1 column 1",
    "RemoteDisconnected",
    "curl: (56)",
)
# urllib3 的重试噪音，一次失败会打多行，计数时必须排除。
RETRY_NOISE_PREFIX = "Retrying ("
BREAKER_PATTERN = "suspending impersonation"

DATA_TASK_RE = re.compile(
    r"Data task (\S+) request_id=\S+ tool=(\S+) symbol=\S+ "
    r"admission=([\d.]+)s queue=([\d.]+)s service=([\d.]+)s"
)
BATCH_RELEASED_RE = re.compile(
    r"Batch query released .*?queue=([\d.]+)s service=([\d.]+)s total=([\d.]+)s"
)
BROWSER_RE = re.compile(r"Realtime fund flow page .*?semaphore_wait=([\d.]+)s service=([\d.]+)s")
IMPERSONATION_RE = re.compile(r"Impersonation failed .*?outcome=([^;]+);")
UPSTREAM_FAIL_RE = re.compile(r"WARNING (获取\S+?失败) (\S+?):")


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarise(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 3),
        "p50": round(percentile(values, 0.5), 3),
        "p90": round(percentile(values, 0.9), 3),
        "p95": round(percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


# ── 被测进程的内存采样 ────────────────────────────────────────────────


def _child_pids(pid: int) -> list[int]:
    """递归收集子进程，Chromium 是 Playwright 的子进程。"""
    found: list[int] = []
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        result = subprocess.run(
            ["pgrep", "-P", str(current)], capture_output=True, text=True
        )
        for token in result.stdout.split():
            child = int(token)
            found.append(child)
            frontier.append(child)
    return found


def _parse_cputime(raw: str) -> float:
    """把 ps 的 cputime（[[HH:]MM:]SS[.ss]）解析成秒。"""
    parts = raw.strip().split(":")
    if not parts or not parts[0]:
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def process_tree_cpu_seconds(pid: int) -> float:
    """进程树累计 CPU 时间。

    子进程退出后它的 CPU 时间会从累计值里消失，所以这个指标对短命子进程
    （Chromium）偏低；主进程为主的场景足够准。
    """
    pids = [pid, *_child_pids(pid)]
    result = subprocess.run(
        ["ps", "-o", "cputime=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
    )
    return sum(_parse_cputime(line) for line in result.stdout.splitlines() if line.strip())


def process_tree_rss_mib(pid: int) -> float:
    """返回进程树的合计 RSS（MiB）。用 ps/pgrep 以保证 Ubuntu 与 macOS 一致。"""
    pids = [pid, *_child_pids(pid)]
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
    )
    total_kib = sum(int(line) for line in result.stdout.split() if line.isdigit())
    return total_kib / 1024.0


class MemorySampler:
    """后台按秒采样进程树 RSS，记录峰值。"""

    def __init__(self, pid: int, interval: float = 1.0):
        self.pid = pid
        self.interval = interval
        self.samples: list[tuple[float, float]] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append((time.time(), process_tree_rss_mib(self.pid)))
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    def peak_between(self, started_at: float, ended_at: float) -> float:
        window = [rss for at, rss in self.samples if started_at <= at <= ended_at]
        return round(max(window), 1) if window else 0.0


# ── 服务端日志解析 ────────────────────────────────────────────────────


@dataclass
class LogWindow:
    """一个加压档位内从服务端日志提取的指标。"""

    upstream_failures: dict[str, int] = field(default_factory=dict)
    impersonation_failures: dict[str, int] = field(default_factory=dict)
    breaker_trips: int = 0
    hard_refusals: int = 0
    soft_refusals: int = 0
    batch_queue: list[float] = field(default_factory=list)
    batch_service: list[float] = field(default_factory=list)
    browser_wait: list[float] = field(default_factory=list)
    browser_service: list[float] = field(default_factory=list)
    task_admission: dict[str, list[float]] = field(default_factory=dict)
    task_service: dict[str, list[float]] = field(default_factory=dict)
    cache_hits: int = 0
    cache_skips: int = 0

    def to_dict(self) -> dict:
        return {
            "upstream_failures": self.upstream_failures,
            "impersonation_failures": self.impersonation_failures,
            "breaker_trips": self.breaker_trips,
            "hard_refusals": self.hard_refusals,
            "soft_refusals": self.soft_refusals,
            "batch_queue_s": summarise(self.batch_queue),
            "batch_service_s": summarise(self.batch_service),
            "browser_semaphore_wait_s": summarise(self.browser_wait),
            "browser_service_s": summarise(self.browser_service),
            "task_admission_s": {k: summarise(v) for k, v in self.task_admission.items()},
            "task_service_s": {k: summarise(v) for k, v in self.task_service.items()},
            "report_cache_hits": self.cache_hits,
            "report_cache_skips": self.cache_skips,
        }


class LogReader:
    """按偏移量增量读取服务端日志，把每一档的日志切开统计。"""

    def __init__(self, path: Path):
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0

    def read_new(self) -> LogWindow:
        window = LogWindow()
        if not self.path.exists():
            return window
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()

        for line in chunk.splitlines():
            if BREAKER_PATTERN in line:
                window.breaker_trips += 1
            if RETRY_NOISE_PREFIX not in line:
                if any(pattern in line for pattern in HARD_REFUSAL_PATTERNS):
                    window.hard_refusals += 1
                elif any(pattern in line for pattern in SOFT_REFUSAL_PATTERNS):
                    window.soft_refusals += 1
            if "Report cache hit" in line:
                window.cache_hits += 1
            if "Report cache skipped" in line:
                window.cache_skips += 1

            match = UPSTREAM_FAIL_RE.search(line)
            if match:
                key = match.group(1)
                window.upstream_failures[key] = window.upstream_failures.get(key, 0) + 1

            match = IMPERSONATION_RE.search(line)
            if match:
                key = match.group(1).strip()[:60]
                window.impersonation_failures[key] = (
                    window.impersonation_failures.get(key, 0) + 1
                )

            match = DATA_TASK_RE.search(line)
            if match:
                op = match.group(1)
                window.task_admission.setdefault(op, []).append(float(match.group(3)))
                window.task_service.setdefault(op, []).append(float(match.group(5)))

            match = BATCH_RELEASED_RE.search(line)
            if match:
                window.batch_queue.append(float(match.group(1)))
                window.batch_service.append(float(match.group(2)))

            match = BROWSER_RE.search(line)
            if match:
                window.browser_wait.append(float(match.group(1)))
                window.browser_service.append(float(match.group(2)))

        return window


# ── 客户端 ───────────────────────────────────────────────────────────


@dataclass
class CallResult:
    tool: str
    started_at: float
    elapsed: float
    ok: bool
    reports: int = 0
    errors: int = 0
    warnings: int = 0
    failure: str | None = None


async def call_tool_once(url: str, tool: str, symbols: list[str], timeout: float) -> CallResult:
    """新建一个 MCP 会话执行一次调用，与 mcporter 的单次调用行为一致。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    started_at = time.time()
    clock = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        tool, {"symbol": ",".join(symbols)}
                    )
        elapsed = time.perf_counter() - clock
        payload = {}
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {}
                break
        return CallResult(
            tool=tool,
            started_at=started_at,
            elapsed=elapsed,
            ok=not result.isError,
            reports=len(payload.get("reports") or {}),
            errors=len(payload.get("errors") or {}),
            warnings=len(payload.get("warnings") or []),
        )
    except asyncio.TimeoutError:
        return CallResult(tool, started_at, timeout, False, failure="client_timeout")
    except Exception as error:
        return CallResult(
            tool,
            started_at,
            time.perf_counter() - clock,
            False,
            failure=f"{type(error).__name__}: {error}"[:160],
        )


def parse_tool_mix(raw: str) -> list[tuple[str, int, int]]:
    """解析 "brief:4=70,medium:4=30" 成 (tool, symbols_per_call, weight)。"""
    mix = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        spec, _, weight = part.partition("=")
        tool, _, count = spec.partition(":")
        mix.append((tool.strip(), int(count or 4), int(weight or 1)))
    if not mix:
        raise ValueError("tool-mix 不能为空")
    return mix


# ── 加压驱动 ─────────────────────────────────────────────────────────


async def run_step(
    url: str,
    rate_per_min: int,
    duration: float,
    mix: list[tuple[str, int, int]],
    pool: list[str],
    timeout: float,
    max_in_flight: int,
) -> tuple[list[CallResult], bool]:
    """按固定速率开环发压。返回结果与是否发生饱和。

    开环而非固定并发：线上就是"每分钟 N 次打进来"，固定并发会把排队隐藏掉。
    """
    interval = 60.0 / rate_per_min
    tools = [item for item in mix for _ in range(item[2])]
    tasks: list[asyncio.Task] = []
    saturated = False
    deadline = time.perf_counter() + duration
    index = 0

    while time.perf_counter() < deadline:
        # 已完成的任务保留在列表里，最后统一 gather 取结果。
        in_flight = sum(1 for task in tasks if not task.done())
        if in_flight >= max_in_flight:
            saturated = True
            break
        tool, per_call, _ = tools[index % len(tools)]
        index += 1
        symbols = random.sample(pool, per_call)
        tasks.append(asyncio.create_task(call_tool_once(url, tool, symbols, timeout)))
        await asyncio.sleep(interval)

    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results), saturated


def step_report(
    rate: int,
    results: list[CallResult],
    window: LogWindow,
    peak_mib: float,
    symbols_per_call: float,
    cpu_seconds: float = 0.0,
) -> dict:
    latencies = [r.elapsed for r in results if r.ok]
    failures = [r for r in results if not r.ok]
    business_errors = sum(r.errors for r in results)
    # 每个标的约 3 个命中主机的请求（K线、资金流、实时行情；财务走同花顺）。
    expected_upstream = max(1.0, len(results) * symbols_per_call * 3)
    upstream_total = sum(window.upstream_failures.values())
    return {
        "target_rate_per_min": rate,
        "calls": len(results),
        "expected_upstream_requests": round(expected_upstream),
        "upstream_failure_ratio": round(upstream_total / expected_upstream, 4),
        "ok": len(results) - len(failures),
        "transport_failures": len(failures),
        "failure_kinds": sorted({r.failure for r in failures if r.failure}),
        "business_error_symbols": business_errors,
        "latency_s": summarise(latencies),
        "peak_rss_mib": peak_mib,
        "peak_rss_over_budget": peak_mib > MEMORY_BUDGET_MIB,
        "cpu_seconds": round(cpu_seconds, 2),
        # 可移植指标：目标机核数 × 60 / 这个值 = 该机型的吞吐上限（次/分钟）
        "cpu_seconds_per_call": round(cpu_seconds / max(1, len(results)), 3),
        "server": window.to_dict(),
    }


def should_abort(
    report: dict, error_rate_limit: float, baseline_ratio: float | None
) -> str | None:
    """判定是否停止加压。

    软性失败必须和基线档比，不能看绝对值：这条链路空载时就有基线失败率，
    实测过 kline 在无压力下也有约四成走了腾讯 fallback。只有失败率显著高于
    基线，才是加压把上游推过阈值，而不是环境本来就在抖。
    """
    server = report["server"]
    # 伪装熔断在上游已被限的环境里是稳定态而不是加压信号：它只说明"该源不可用"，
    # 每一档都会触发，用它中止会让加压在第一档就停下。只记录，不中止。
    if server["hard_refusals"]:
        return f"出现明确限流状态码 {server['hard_refusals']} 次"
    if report["peak_rss_over_budget"]:
        return f"峰值内存 {report['peak_rss_mib']} MiB 超过 {MEMORY_BUDGET_MIB} MiB"
    if report["calls"] and report["transport_failures"] / report["calls"] > error_rate_limit:
        return "客户端失败率超阈值"
    ratio = report["upstream_failure_ratio"]
    if baseline_ratio is not None and ratio > max(baseline_ratio * 3, baseline_ratio + 0.15):
        return f"上游失败率 {ratio:.1%} 显著高于基线 {baseline_ratio:.1%}"
    return None


# ── 被测实例 ─────────────────────────────────────────────────────────


def launch_instance(
    port: int, log_path: Path, cache_dir: Path, overrides: dict = None
) -> subprocess.Popen:
    """起一个隔离实例：独立端口、独立日志、独立报告缓存目录。"""
    env = dict(os.environ)
    env["REPORT_CACHE_DIR"] = str(cache_dir)
    for key, value in (overrides or {}).items():
        env[key] = value
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "main.py", "--transport", "http", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return process


async def wait_ready(log_path: Path, process: subprocess.Popen, timeout: float = 40.0) -> str:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"实例启动失败，退出码 {process.returncode}")
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if "Starting MCP app" in text:
                channel = next(
                    (line for line in text.splitlines() if "HTTP channel mode=" in line),
                    "",
                )
                return channel.split("HTTP channel ")[-1] if channel else "unknown"
        await asyncio.sleep(0.5)
    raise RuntimeError("实例启动超时")


# ── 主流程 ───────────────────────────────────────────────────────────


async def main_async(args: argparse.Namespace) -> int:
    _quiet_client_logs()
    from finmcp.symbols import SYMBOLS_SHSZ, load_symbols

    load_symbols()
    pool = [
        symbol
        for symbol in SYMBOLS_SHSZ
        if symbol.startswith(("SH6", "SZ00", "SZ30"))
    ]
    random.seed(args.seed)
    random.shuffle(pool)
    pool = pool[: args.symbol_pool]
    mix = parse_tool_mix(args.tool_mix)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "server.log"
    cache_dir = out_dir / "report-cache"

    process = None
    url = args.url or f"http://localhost:{args.port}/cnstock/mcp"
    if args.launch:
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        overrides = dict(
            item.split("=", 1) for item in (args.env or []) if "=" in item
        )
        process = launch_instance(args.port, log_path, cache_dir, overrides)
        if overrides:
            print("实例环境覆盖:", overrides)
        channel = await wait_ready(log_path, process)
        print(f"实例已启动 port={args.port} {channel}")
        server_pid = process.pid
    else:
        if args.server_log is None or args.server_pid is None:
            print("不使用 --launch 时必须提供 --server-log 与 --server-pid", file=sys.stderr)
            return 2
        log_path = Path(args.server_log)
        server_pid = args.server_pid

    if "8686" in url and not args.allow_default_port:
        print("拒绝压测默认端口 8686；用 --launch 起隔离实例，或加 --allow-default-port",
              file=sys.stderr)
        return 2

    reader = LogReader(log_path)
    sampler = MemorySampler(server_pid)
    sampler.start()

    symbols_per_call = sum(item[1] * item[2] for item in mix) / sum(item[2] for item in mix)
    baseline_ratio: float | None = None

    run = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": url,
        "tool_mix": args.tool_mix,
        "symbol_pool": len(pool),
        "steps": [],
        "ceiling_rate_per_min": None,
        "aborted": None,
    }

    try:
        for rate in args.steps:
            print(f"\n── {rate} 次/分钟，{args.step_seconds}s ──")
            reader.read_new()  # 丢掉上一档残留
            started = time.time()
            cpu_before = process_tree_cpu_seconds(server_pid)
            results, saturated = await run_step(
                url,
                rate,
                args.step_seconds,
                mix,
                pool,
                args.call_timeout,
                args.max_in_flight or rate * 4,
            )
            # 让最后几笔的日志落盘
            await asyncio.sleep(1.0)
            window = reader.read_new()
            peak = sampler.peak_between(started, time.time())
            cpu_used = max(0.0, process_tree_cpu_seconds(server_pid) - cpu_before)
            report = step_report(
                rate, results, window, peak, symbols_per_call, cpu_used
            )
            report["saturated"] = saturated
            # 平均占核数是折算到部署机的关键：8 核机上 15 次/分只有 12% 利用率，
            # 同样的负载在 2 核机上已经吃掉半台机器。
            report["cores_busy_avg"] = round(cpu_used / max(1.0, args.step_seconds), 2)
            run["steps"].append(report)

            latency = report["latency_s"]
            print(
                f"  完成 {report['ok']}/{report['calls']}  "
                f"P50={latency.get('p50', 0)}s P95={latency.get('p95', 0)}s "
                f"max={latency.get('max', 0)}s  峰值内存={peak} MiB"
            )
            print(
                f"  CPU {report['cpu_seconds_per_call']}s/次"
                f"  平均占核 {report['cores_busy_avg']}"
            )
            if window.upstream_failures:
                print(f"  上游失败: {window.upstream_failures}")
            if window.impersonation_failures:
                print(f"  伪装失败: {window.impersonation_failures}")
            if window.breaker_trips:
                print(f"  熔断触发: {window.breaker_trips} 次（上游已被限，属稳定态）")

            reason = should_abort(report, args.abort_error_rate, baseline_ratio)
            if baseline_ratio is None:
                baseline_ratio = report["upstream_failure_ratio"]
                run["baseline_upstream_failure_ratio"] = baseline_ratio
                print(f"  基线上游失败率: {baseline_ratio:.1%}（后续档位与它比较）")
            if saturated:
                reason = reason or "在途请求堆积，服务端已饱和"
            if reason:
                run["aborted"] = {"rate_per_min": rate, "reason": reason}
                print(f"  停止加压：{reason}")
                break
            # 只看有没有触发中止条件，会把"批次排队 P95 58 秒、端到端 P95 67 秒"
            # 的档位也报成可稳定支撑。排队一出现就说明超出服务能力了。
            queue_p95 = (report["server"].get("batch_queue_s") or {}).get("p95") or 0.0
            if queue_p95 <= QUEUE_TOLERANCE_S:
                run["ceiling_rate_per_min"] = rate
    finally:
        await sampler.stop()
        if process is not None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
            print("\n实例已停止")

    run["peak_rss_mib"] = round(max((rss for _, rss in sampler.samples), default=0.0), 1)
    report_path = out_dir / "loadtest.json"
    report_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "\n| 速率/分 | 完成 | P50 | P95 | max | 峰值内存 | CPU秒/次 | 平均占核 | 上游失败 |"
    )
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for step in run["steps"]:
        latency = step["latency_s"]
        print(
            f"| {step['target_rate_per_min']} | {step['ok']}/{step['calls']} "
            f"| {latency.get('p50', 0)}s | {latency.get('p95', 0)}s "
            f"| {latency.get('max', 0)}s | {step['peak_rss_mib']} MiB "
            f"| {step['cpu_seconds_per_call']} | {step.get('cores_busy_avg', 0)} "
            f"| {sum(step['server']['upstream_failures'].values())} |"
        )
    print(
        f"\n可稳定支撑: {run['ceiling_rate_per_min']} 次/分钟"
        f"（批次排队 P95 <= {QUEUE_TOLERANCE_S}s 的最高档）"
    )
    if run["aborted"]:
        print(f"停止原因: {run['aborted']['reason']}（在 {run['aborted']['rate_per_min']} 次/分钟）")
    print(f"明细: {report_path}")
    return 0


def _quiet_client_logs() -> None:
    """客户端库的每请求 INFO 日志会淹没进度输出。"""
    import logging

    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="MCP 端点；缺省用 --port 拼 localhost")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--launch", action="store_true", help="自动起一个隔离实例并在结束时停止")
    parser.add_argument("--server-log", help="不用 --launch 时的服务端日志路径")
    parser.add_argument("--server-pid", type=int, help="不用 --launch 时的服务端 PID")
    parser.add_argument("--allow-default-port", action="store_true")
    parser.add_argument(
        "--steps",
        type=lambda raw: [int(x) for x in raw.split(",") if x.strip()],
        default=[5, 10, 15, 20, 30],
        help="加压档位，单位次/分钟",
    )
    parser.add_argument("--step-seconds", type=float, default=90.0)
    parser.add_argument("--tool-mix", default="brief:4=1", help='形如 "brief:4=70,full:4=30"')
    parser.add_argument("--symbol-pool", type=int, default=400, help="参与轮换的标的数")
    parser.add_argument("--call-timeout", type=float, default=120.0)
    parser.add_argument("--abort-error-rate", type=float, default=0.2)
    parser.add_argument("--max-in-flight", type=int, default=0, help="0 表示取速率的 4 倍")
    parser.add_argument("--seed", type=int, default=20260903)
    parser.add_argument(
        "--env", action="append", help="注入被测实例的环境变量，可重复，形如 KEY=VALUE"
    )
    parser.add_argument("--out-dir", default=".runtime/loadtest")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
