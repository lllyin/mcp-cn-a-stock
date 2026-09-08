#!/usr/bin/env python3
"""探测调优：量出这台机器上哪些配置该偏离默认值，产出 .env.recommended 和一份报告。

要回答的问题
------------
同一份代码跑在不同机器上，出口 IP 的待遇、浏览器指纹的待遇、页面加载耗时、内存余量都
不一样，结论还可能相反：一台机器上要伪装身份才拿得到资金流页面，另一台上原样身份才是
唯一全通的；一台机器上合适的重试次数搬到另一台，会把偶发的拒绝放大成整批滑块。所以每台
机器该自己量一次、量完再决定，不要把别处的 .env 整份搬过来。

只量四类真正随机器变的配置，其余不碰：

    身份   BROWSER_DISGUISE                       两种身份交错加载同一批页面，比首 tab 成功率
    重试   FUND_FLOW_PAGE_MAX_LOADS               逐 tab 条件恢复率 + 批内首 tab 成功率的衰减
    等待   FUND_FLOW_PAGE_QUEUE_WAIT / BUDGET     成功加载耗时 p90（页面加载慢的机器要给更长的等待）
    页数   BROWSER_MAX_PAGES / PAGE_CONCURRENCY   同时开 1/2/3 页的进程树内存（只有 PSS 能和 500 MiB 比）

不量的：熔断阈值与冷却——被拒状态跟着浏览器指纹走、不随时间恢复，冷却是止损加半开探测，
按"恢复时间"调它无意义；缓存 TTL 与纪元时刻——业务策略；provider 顺序——那是准确度判断，
这里只报不可达、不重排；FETCH_MAX_WORKERS / BATCH_CONCURRENCY——受上游而不是这台机器的 CPU
约束，默认值照用。

判据是边际的、带样本量门槛的（AGENTS 第五条：判不出就保持默认）：

  * 身份：每臂至少 12 个 episode，首 tab 成功率差 20 个百分点以上才切。n=12 能判出 12/12 对
    8/12（p=0.65 时 12 连中的概率 0.6%），判不出 11/12 对 10/12。
  * 重试：首 tab ≥ 95% 就是 2——重试从未用到，留一次当保险。否则从第 2 个 tab 起，条件恢复率
    ≥ 15%（到达该 tab 的 episode ≥ 5）且批内后半的首 tab 成功率没比前半掉 20 个百分点以上，
    才多给一个。被拒后再开的每一个 tab 都在消耗同一出口的频率额度：实测见过的一条曲线是首 tab
    16%、第 2 个 tab 救回 19%、第 3 个只有 5%，同一批后到的标的则被压到 0——第 3 个 tab 换来的
    远少于它压掉的。
  * 等待：成功加载 ≥ 10 次，等待 = ceil(p90 + 0.5) 夹在 [3, 15]，预算 = 等待 + ceil(p90)；离默认
    不到 2 秒不改。
  * 页数：只在读到 PSS（Linux）时给：(500 − 服务不含浏览器 − 浏览器开 1 页) / 每页边际 + 1，
    夹在 [1, 3]。macOS 只有 RSS，RSS 把共享页在每个渲染进程里重复计入，不能拿去和预算比。

代价与边界
----------
  * browser 一轮约 5 到 8 分钟、48 到 150 次页面加载（--load-budget 是硬上限），全打在这台机器
    的出口 IP 上。交易日 09:15-11:30、13:00-15:00 拒绝运行，除非 --force。最合适的窗口是
    15:05-15:55：盘已收、页面路径还在用（16:00 之后资金流切回读接口，那之后 verify_release
    验不到浏览器那段）。
  * 结论有时效：风控按指纹和频率，上游也会改。建议每周一轮，或 verify_release 的资金流完整率
    连续两次低于 90% 时重探。
  * 只读：不 stop/start 线上服务、不改 .env，缓存目录指到结果目录。verify 子命令用 ENV_PREFIX
    起隔离实例，同样不碰线上。推荐值由人比对后应用，不要整份覆盖。

用法
----
    .venv/bin/python scripts/probe_tuning.py all                          # facts → browser → tonghuashun → recommend
    .venv/bin/python scripts/probe_tuning.py facts
    .venv/bin/python scripts/probe_tuning.py browser [--identities legacy,disguise] [--batches 6] [--max-tabs 3] [--force]
    .venv/bin/python scripts/probe_tuning.py tonghuashun [--rounds 6] [--window-days 730]
    .venv/bin/python scripts/probe_tuning.py recommend [--env .env] [--service-base-mib 243]
    .venv/bin/python scripts/probe_tuning.py verify --env-file <dir>/.env.recommended [--only brief,medium,full]

结果目录缺省 .runtime/probe-tuning/<时间戳>/，--out-dir 指定或复用（recommend 读它、verify 写进它）。
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import datetime as dt
import json
import math
import os
import platform as platform_module
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 以脚本文件形式运行时 sys.path[0] 是 scripts/，仓库根不在搜索路径上；探测量的就是这份检出的代码。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_OUT_ROOT = PROJECT_ROOT / ".runtime" / "probe-tuning"
PID_FILE = PROJECT_ROOT / "cn-stock-mcp.pid"
XVFB_PID_FILE = PROJECT_ROOT / "cn-stock-mcp-xvfb.pid"
#: AGENTS 第四条：服务及其 Chromium 等子进程的合计峰值上限。与 loadtest / verify_release 同一个数。
MEMORY_BUDGET_MIB = 500.0
#: 服务不含浏览器时进程树大小的参考值（一台 2 核 4G Ubuntu 上实测 243 MiB）。这台机器上量不到
#: 线上服务时用它，--service-base-mib 可覆盖。
SERVICE_BASE_MIB_DOCUMENTED = 243.0

# 探测覆盖的配置项及其默认值（与 finmcp/config.py、.env.example 一致；这里不 import config，
# 免得 recommend 这种纯离线步骤也把整套取数层连同出站通道一起拉起来）。
DEFAULTS = {
    "BROWSER_DISGUISE": "0",
    "FUND_FLOW_PAGE_MAX_LOADS": "2",
    "FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS": "8",
    "FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS": "15",
    "BROWSER_MAX_PAGES": "3",
    "FUND_FLOW_PAGE_CONCURRENCY": "3",
    "HTTP_CHANNEL": "auto",
    "FUND_FLOW_PAGE_COOLDOWN_SECONDS": "60",
    "FUND_FLOW_PAGE_OPEN_AFTER_FAILURES": "4",
    "BROWSER_HEADFUL": "0",
    "BROWSER_KEEP_PAGES": "0",
    "CACHE_ENABLED": "1",
    "KLINE_TONGHUASHUN_BUDGET_SECONDS": "45",
}

# ── 判据阈值 ──────────────────────────────────────────────────────
MIN_EPISODES_PER_ARM = 12
DISGUISE_MARGIN = 0.20
SATURATED_FIRST_TAB = 0.95
RECOVERY_MIN = 0.15
RECOVERY_MIN_N = 5
DECLINE_MAX = 0.20
MIN_SERVICE_SAMPLES = 10
QUEUE_WAIT_RANGE = (3, 15)
BUDGET_MAX = 40

#: 同花顺 K 线总预算的判据。预算要坐在**成功**取数的最大耗时之上——砍掉一次本来能
#: 成功的取数，指数的成交量就退到腾讯口径、低约 3.5%，那是拿正确的数换耗时。
#: 所以用 max 而不是分位数，再乘一个余量。
THS_MIN_SAMPLES = 12
THS_MARGIN = 1.5
THS_BUDGET_RANGE = (20, 120)
#: 一次取数跨几年就发几个请求，所以窗口长度直接决定最坏耗时。默认量 2 年，
#: 和报告里 240 日均量要的跨度同量级。
THS_WINDOW_DAYS = 730
#: 只量指数：tonghuashun 在 KLINE_PROVIDERS_INDEX 里排第一，个股走腾讯，量了用不上。
THS_SYMBOLS = ("SH000001", "SZ399001", "SZ399006", "SH000688")

#: 32 只两市大盘股，都有 zjlx 页面。固定名单是为了跨机器、跨日期可比。
DEFAULT_SYMBOLS = (
    "600519", "000333", "300750", "688981", "601318", "000651", "300059", "688111",
    "600036", "002594", "300760", "688008", "601899", "000858", "300124", "600026",
    "600938", "601138", "601872", "603893", "600900", "000001", "002475", "601166",
    "600276", "000725", "300015", "688012", "601012", "002415", "300274", "600030",
)
IDENTITIES = ("legacy", "disguise")
IDENTITY_LABEL = {"legacy": "原样（main）", "disguise": "伪装"}


def _now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(values: list, q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarise_values(values: list) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.5), 2),
        "p90": round(percentile(values, 0.9), 2),
        "max": round(max(values), 2),
    }


# ── 进程树内存 ────────────────────────────────────────────────────
# 和 loadtest / verify_release 同一口径：RSS 逐进程相加是上界；PSS 把共享页均摊，才是能和
# 预算比的数，只有 Linux 有。少读一个进程的 PSS 就整列作废——偏低的内存数比没有更危险。

_BROWSER_COMM_MARKERS = ("chrom", "headless_shell", "headless-shell")


def _ps_table() -> dict:
    result = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,rss=,comm="], capture_output=True, text=True, timeout=10
    )
    procs: dict = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss_kib = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        procs[pid] = (ppid, rss_kib / 1024.0, parts[3])
    return procs


#: 这台机器支不支持 PSS。拿当前进程探一次——它一定存在，所以这个判断只反映
#: "有没有 /proc/<pid>/smaps_rollup"，不会被"某个子进程刚退出"污染。
_PSS_SUPPORTED = os.path.exists(f"/proc/{os.getpid()}/smaps_rollup")


# 为什么要把"进程没了"和"读不到 PSS"分开：浏览器那一档有 8 个以上 Chromium 进程在
# 不断起落，只要有一个撞上"列进程树"和"读 smaps"之间的空隙，旧写法就把整个样本判废。
# 实测过一轮：facts 那一档读到了 121.9 MiB PSS，而同一次运行的 browser 三级内存阶梯
# 全是"（无 PSS）"，于是报告写成"这台机器读不到 PSS"。结果是唯一能判 BROWSER_MAX_PAGES
# 的机器上这一项永远出不了结论，而内存正是它要守的那条线。
def _pss_mib(pid: int) -> Optional[float]:
    """该进程的 PSS，单位 MiB。

    **进程已经退出时返回 0.0 而不是 None。** 这两件事必须分开：``None`` 的含义是
    "这台机器读不到 PSS"，会让整棵树的样本作废；而一个在"列进程树"和"读 smaps"
    之间退出的进程，到测量那一刻本来就不占内存，记 0 才是对的。

    混在一起的代价是实打实的，见函数上面那段注释。
    """
    if not _PSS_SUPPORTED:
        return None
    try:
        with open(f"/proc/{pid}/smaps_rollup", "rb") as handle:
            for line in handle:
                if line.startswith(b"Pss:"):
                    return float(line.split()[1]) / 1024.0
    except (FileNotFoundError, ProcessLookupError):
        return 0.0          # 采样与读取之间退出了，不占内存
    except (OSError, ValueError, IndexError):
        return None         # 权限不足之类：这台机器读不到，样本作废
    return None             # smaps_rollup 在但没有 Pss 行（内核太老）


def tree_memory(root_pid: int) -> dict:
    """采一次以 root_pid 为根的进程树。浏览器那部分单列——它是峰值的主要来源。"""
    procs = _ps_table()
    children = defaultdict(list)
    for pid, (ppid, _, _) in procs.items():
        children[ppid].append(pid)
    members: list = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid not in procs:
            continue
        members.append(pid)
        stack.extend(children.get(pid, []))
    browser = [p for p in members if any(m in procs[p][2].lower() for m in _BROWSER_COMM_MARKERS)]
    pss_total = 0.0
    pss_browser = 0.0
    complete = bool(members)
    for pid in members:
        measured = _pss_mib(pid)
        if measured is None:
            complete = False
            break
        pss_total += measured
        if pid in browser:
            pss_browser += measured
    return {
        "at": time.time(),
        "processes": len(members),
        "rss_mib": round(sum(procs[p][1] for p in members), 1),
        "browser_processes": len(browser),
        "browser_rss_mib": round(sum(procs[p][1] for p in browser), 1),
        "pss_mib": round(pss_total, 1) if complete else None,
        "browser_pss_mib": round(pss_browser, 1) if complete else None,
        "metric": "pss" if complete else "rss",
    }


def peak_memory(root_pid: int, seconds: float = 3.0, interval: float = 0.5) -> dict:
    """采几次取峰值；PSS 只在每次都读全时才给。"""
    samples = []
    deadline = time.perf_counter() + seconds
    while True:
        samples.append(tree_memory(root_pid))
        if time.perf_counter() >= deadline:
            break
        time.sleep(interval)
    peak = max(samples, key=lambda s: s["rss_mib"])
    pss = [s["pss_mib"] for s in samples if s["pss_mib"] is not None]
    peak = dict(peak)
    peak["pss_mib"] = max(pss) if len(pss) == len(samples) else None
    peak["metric"] = "pss" if peak["pss_mib"] is not None else "rss"
    peak["samples"] = len(samples)
    return peak


# ── facts：机器与网络 ─────────────────────────────────────────────

_EM_UT = "b2884a393a59ad64002292a3e90d46a5"
_UA_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
_UA_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")


@dataclass(frozen=True)
class Probe:
    name: str
    host: str
    url: str
    kind: str                       # json | jsonp | text:<needle> | html
    channels: tuple = ("direct",)
    headers: dict = field(default_factory=lambda: {"User-Agent": _UA_MAC})
    #: 证书链不完整的站（swsresearch）：校验失败后再试一次不校验，和代码里的回退一致。
    insecure_retry: bool = False
    role: str = ""


def _probes(year: Optional[int] = None) -> tuple:
    year = year or dt.date.today().year
    em = {"User-Agent": _UA_WIN}
    return (
        Probe("push2 行情 ulist", "push2.eastmoney.com",
              f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001&fields=f12,f14,f2&ut={_EM_UT}",
              "json", ("direct", "impersonate"), em, role="基本数据、板块资金流（伪装通道接管）"),
        Probe("push2his 资金流日线", "push2his.eastmoney.com",
              f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=1&klt=101&secid=1.000001"
              f"&fields1=f1,f2,f3,f7&fields2=f51,f52&ut={_EM_UT}",
              "json", ("direct", "impersonate"), em, role="资金流历史、K 线主源（伪装通道接管）"),
        Probe("push2delay 资金流分钟线", "push2delay.eastmoney.com",
              f"https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid=1.000001"
              f"&fields1=f1,f2,f3,f7&fields2=f51,f52&ut={_EM_UT}",
              "json", ("direct",), em, role="无页面标的的盘中实时资金流、资金流当日行补齐"),
        Probe("data.eastmoney dataapi 板块资金流", "data.eastmoney.com",
              "https://data.eastmoney.com/dataapi/bkzj/getbkzj?key=f62&code=m%3A90%2Bt%3A2",
              "json", ("direct",), role="板块资金流降级源"),
        Probe("zjlx 页面框架", "data.eastmoney.com",
              "https://data.eastmoney.com/zjlx/600519.html", "html", ("direct",), role="资金流页面兜底的页面本身"),
        Probe("腾讯行情", "qt.gtimg.cn", "https://qt.gtimg.cn/q=sh000001",
              "text:v_sh000001=", ("direct",), role="基本数据兜底、盘中行情"),
        Probe("腾讯日 K", "proxy.finance.qq.com",
              "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?_var=kline_dayqfq&param=sh000001,day,,,3,qfq",
              "text:kline_dayqfq", ("direct",), role="K 线兜底"),
        Probe("同花顺日 K", "d.10jqka.com.cn", f"https://d.10jqka.com.cn/v6/line/hs_1A0001/01/{year}.js",
              "jsonp", ("direct",),
              {"User-Agent": _UA_MAC, "Referer": "https://stockpage.10jqka.com.cn/", "Accept": "*/*"},
              role="指数 K 线首选源"),
        Probe("乐咕乐股 申万分级", "legulegu.com", "https://legulegu.com/stockdata/sw-industry-overview",
              "html", ("direct",), role="板块分级首选源（机房 IP 常被 302 到人机验证）"),
        Probe("申万研究所 分级接口", "www.swsresearch.com",
              "https://www.swsresearch.com/institute-sw/api/index_publish/current/?page=1&page_size=1&indextype=%E4%B8%80%E7%BA%A7%E8%A1%8C%E4%B8%9A",
              "json", ("direct",), {"User-Agent": _UA_WIN}, insecure_retry=True, role="板块分级第二源"),
    )


_REFUSAL_MARKERS = ("connection reset", "remote end closed", "empty reply", "connection closed",
                    "curl: (56)", "curl: (52)", "curl: (35)", "broken pipe", "connection aborted",
                    "recv failure", "eof occurred")
_TIMEOUT_MARKERS = ("timed out", "timeout", "curl: (28)")
_DNS_MARKERS = ("could not resolve", "name or service not known", "nodename nor servname", "curl: (6)")
_TLS_MARKERS = ("certificate", "ssl", "tls")
_BLOCK_MARKERS = ("human-challenge", "captcha", "challenge", "verify")


def classify_http(status: Optional[int], location: Optional[str], text: str,
                  error: Optional[str], kind: str) -> str:
    """把一次探测归成一个词。refused 是东财对出口 IP 的典型表现（断连、空响应）。"""
    if error:
        lowered = error.lower()
        if any(m in lowered for m in _DNS_MARKERS):
            return "dns_error"
        if any(m in lowered for m in _REFUSAL_MARKERS):
            return "refused"
        if any(m in lowered for m in _TIMEOUT_MARKERS):
            return "timeout"
        if any(m in lowered for m in _TLS_MARKERS):
            return "tls_error"
        return "error"
    if status is None:
        return "error"
    if 300 <= status < 400:
        target = (location or "").lower()
        return "blocked" if any(m in target for m in _BLOCK_MARKERS) else f"redirect_{status}"
    if status in (403, 412, 429):
        return "blocked"
    if status != 200:
        return f"http_{status}"
    body = text or ""
    lowered = body[:20000].lower()
    if kind == "json":
        try:
            payload = json.loads(body)
        except ValueError:
            return "blocked" if any(m in lowered for m in _BLOCK_MARKERS) else "not_json"
        if isinstance(payload, dict) and payload.get("data") not in (None, {}, []):
            return "ok"
        return "empty"
    if kind == "jsonp":
        return "ok" if "(" in body and ")" in body else "empty"
    if kind.startswith("text:"):
        return "ok" if kind[5:] in body else "empty"
    if kind == "html":
        if any(m in lowered for m in _BLOCK_MARKERS) and "zjlx" not in lowered:
            return "blocked"
        return "ok" if body.strip() else "empty"
    return "ok"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _fetch_direct(url: str, headers: dict, timeout: float, insecure: bool = False) -> tuple:
    """urllib 直连。不用 requests：finmcp.datasource 一被导入就把 requests 接到伪装通道上，
    直连探测就不再是直连。"""
    opener = urllib.request.build_opener(
        _NoRedirect,
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context() if insecure else None),
    )
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.headers.get("Location"), response.read(200_000).decode("utf-8", "replace"), None
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read(20_000).decode("utf-8", "replace")
        except Exception:
            pass
        return error.code, error.headers.get("Location") if error.headers else None, body, None
    except Exception as error:  # URLError、socket.timeout、ssl 错误都归这里
        return None, None, "", f"{type(error).__name__}: {error}"


def _fetch_impersonate(url: str, headers: dict, timeout: float) -> tuple:
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None, None, "", "ImportError: curl_cffi 未安装"
    try:
        session = cffi_requests.Session(impersonate="chrome")
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        return response.status_code, response.headers.get("Location"), response.text, None
    except Exception as error:
        return None, None, "", f"{type(error).__name__}: {error}"


def probe_once(probe: Probe, channel: str, timeout: float = 8.0) -> dict:
    started = time.perf_counter()
    fetch = _fetch_impersonate if channel == "impersonate" else _fetch_direct
    status, location, text, error = fetch(probe.url, probe.headers, timeout)
    klass = classify_http(status, location, text, error, probe.kind)
    note = ""
    if klass == "tls_error" and probe.insecure_retry and channel == "direct":
        status, location, text, error = _fetch_direct(probe.url, probe.headers, timeout, insecure=True)
        klass = classify_http(status, location, text, error, probe.kind)
        note = "证书校验失败，不校验可达（代码里同样回退）" if klass == "ok" else "证书校验失败，不校验也不通"
    return {
        "probe": probe.name,
        "host": probe.host,
        "role": probe.role,
        "channel": channel,
        "status": status,
        "class": klass,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "location": location,
        "error": (error or "")[:160],
        "note": note,
    }


def reachability(probes: tuple = None, timeout: float = 8.0) -> list:
    probes = probes or _probes()
    jobs = [(p, ch) for p in probes for ch in p.channels]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda job: probe_once(job[0], job[1], timeout), jobs))
    return results


def egress_ip(timeout: float = 5.0) -> Optional[str]:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        status, _, text, error = _fetch_direct(url, {"User-Agent": "curl/8.0"}, timeout)
        if status == 200 and text.strip():
            return text.strip()[:64]
    return None


def _meminfo() -> dict:
    total = available = None
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) / 1024.0
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) / 1024.0
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            total = int(out.stdout.strip()) / 1024 / 1024
        except Exception:
            pass
    return {"mem_total_mib": round(total) if total else None,
            "mem_available_mib": round(available) if available else None}


def machine_facts() -> dict:
    facts = {
        "hostname": socket.gethostname(),
        "system": platform_module.system(),
        "release": platform_module.release(),
        "machine": platform_module.machine(),
        "python": platform_module.python_version(),
        "cores": os.cpu_count(),
        "display": os.environ.get("DISPLAY"),
        "xvfb": shutil.which("Xvfb"),
        **_meminfo(),
    }
    try:
        import importlib.metadata as metadata

        facts["playwright"] = metadata.version("playwright")
    except Exception:
        facts["playwright"] = None
    facts["chromium"] = None
    if facts["playwright"]:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                path = p.chromium.executable_path
                facts["chromium"] = path if Path(path).exists() else f"未安装（{path}）"
        except Exception as error:
            facts["chromium"] = f"不可用：{type(error).__name__}"
    return facts


def cpu_ref_total_ms(timeout: float = 180.0) -> Optional[float]:
    """跑 scripts/cpu_ref.py 取 total 一行，毫秒。两台机器相除就是单核降级系数。"""
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "cpu_ref.py")],
            capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_ROOT),
        )
    except Exception:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("total"):
            try:
                return float(line.split(":")[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None


def _pid_alive(pid_file: Path) -> Optional[int]:
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def live_service() -> Optional[dict]:
    """线上服务此刻的进程树内存（只读）。拆掉浏览器后的基数是页数决策的输入。"""
    pid = _pid_alive(PID_FILE)
    if pid is None:
        return None
    sample = tree_memory(pid)
    sample["pid"] = pid
    metric = sample["metric"]
    total = sample["pss_mib"] if metric == "pss" else sample["rss_mib"]
    browser = sample["browser_pss_mib"] if metric == "pss" else sample["browser_rss_mib"]
    sample["service_base_mib"] = round(total - browser, 1)
    sample["service_base_source"] = (
        "线上服务此刻不含浏览器进程" if sample["browser_processes"] == 0 else "线上服务此刻减去浏览器进程")
    return sample


def proxy_configured(values: dict) -> bool:
    enabled = (values.get("AKSHARE_PROXY_ENABLED") or "0").strip().lower() not in ("0", "false", "no", "off", "")
    gateway = (values.get("AKSHARE_PROXY_GATEWAY") or values.get("AKSHARE_PROXY_IP") or "").strip()
    return enabled and bool(gateway)


def _dotenv(path: Path) -> dict:
    if not path.exists():
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def run_facts(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[facts] 机器…", flush=True)
    facts = {"started": _now_text(), "machine": machine_facts()}
    env_values = _dotenv(PROJECT_ROOT / ".env")
    facts["proxy_configured"] = proxy_configured({**env_values, **os.environ})
    # 系统代理会让"直连"其实经代理出去，而 curl_cffi 不一定跟着走——两条通道的结果要对着它看。
    facts["proxy_env"] = {k: v.split("@")[-1] for k, v in os.environ.items()
                          if k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    print(f"[facts] 出口 IP…", flush=True)
    facts["egress_ip"] = egress_ip()
    print(f"[facts] 可达性（{len(_probes())} 个源）…", flush=True)
    facts["reachability"] = reachability()
    for row in facts["reachability"]:
        print(f"  {row['class']:>10}  {row['latency_ms']:>5} ms  {row['channel']:<11} {row['probe']}"
              + (f"  {row['note']}" if row["note"] else "") + (f"  {row['error']}" if row["error"] else ""))
    facts["live_service"] = live_service()
    if facts["live_service"]:
        s = facts["live_service"]
        print(f"[facts] 线上服务 pid={s['pid']} 进程树 {s['rss_mib']} MiB RSS"
              + (f" / {s['pss_mib']} MiB PSS" if s["pss_mib"] is not None else "")
              + f"，浏览器进程 {s['browser_processes']} 个，不含浏览器基数 {s['service_base_mib']} MiB")
    else:
        print("[facts] 线上服务没在跑（没有 cn-stock-mcp.pid 或进程不存在），页数决策用文档里的 243 MiB 基数")
    if not getattr(args, "skip_cpu_ref", False):
        print("[facts] 单核基准 scripts/cpu_ref.py…", flush=True)
        facts["cpu_ref_total_ms"] = cpu_ref_total_ms()
    facts["finished"] = _now_text()
    write_json(out_dir / "facts.json", facts)
    print(f"[facts] 写入 {out_dir / 'facts.json'}")
    return 0


# ── browser：身份、重试、耗时、内存 ───────────────────────────────


def in_trading_session(now: dt.datetime, is_trading_day: Callable[[dt.date], bool],
                       warmup: dt.time = dt.time(9, 15), lunch_start: dt.time = dt.time(11, 30),
                       lunch_end: dt.time = dt.time(13, 0), close: dt.time = dt.time(15, 0)) -> bool:
    """交易日的 09:15-11:30、13:00-15:00。探测的页面加载会和线上调用抢同一个出口 IP 的频率额度。"""
    if not is_trading_day(now.date()):
        return False
    clock = now.time()
    return warmup <= clock < lunch_start or lunch_end <= clock < close


class BrowserProbe:
    """用线上同一套身份定义和同一个单次加载函数，起自己的 Chromium 逐 tab 记账。"""

    def __init__(self, *, identities, batches, max_tabs, load_budget, pause, concurrency,
                 symbols, memory_pages, log=print):
        self.identities = list(identities)
        self.batches = batches
        self.max_tabs = max_tabs
        self.load_budget = load_budget
        self.pause = pause
        self.concurrency = concurrency
        self.symbols = list(symbols)
        self.memory_pages = memory_pages
        self.log = log
        self.records: list = []
        self.loads_used = 0
        self.budget_hit = False
        self.memory: dict = {}

    def _pick(self, rnd: int, arm_index: int, size: int = 4) -> list:
        start = (rnd * len(self.identities) + arm_index) * size
        return [self.symbols[(start + i) % len(self.symbols)] for i in range(size)]

    async def _episode(self, rf, ctx, identity: str, rnd: int, symbol: str, sem: asyncio.Semaphore) -> None:
        url = rf.get_fund_flow_url(symbol)
        for tab in range(1, self.max_tabs + 1):
            if self.loads_used >= self.load_budget:
                self.budget_hit = True
                return
            async with sem:
                page = await ctx.new_page()
                started = time.time()
                clock = time.perf_counter()
                outcome, refused_blocks, rows, captcha = "error", set(), 0, False
                try:
                    await rf.disguise_page(page, enabled=(identity == "disguise"))

                    async def block_route(route):
                        await route.abort()

                    for pattern in rf.BLOCKED_PATTERNS:
                        await page.route(pattern, block_route)
                    parsed, refusal, refused_blocks = await rf._load_once(page, symbol, url, reload=False)
                    if refusal is not None:
                        captcha = bool(getattr(refusal, "captcha", False))
                        outcome = "refused_captcha" if captcha else "refused"
                    elif parsed is not None and parsed.has_today:
                        outcome = "today"
                        rows = len(parsed.history)
                    elif "today" in refused_blocks:
                        # 历史到了、今日被接口拒：线上不换 tab（再来只是再被拒一次），这里同样结束 episode，
                        # 但记成被拒——对身份判据它是被拒，不是"盘前没数据"。
                        outcome = "refused_today"
                        rows = len(parsed.history) if parsed is not None else 0
                    else:
                        outcome = "nodata"
                        rows = len(parsed.history) if parsed is not None else 0
                except Exception as error:
                    outcome = f"error:{type(error).__name__}"
                finally:
                    service = time.perf_counter() - clock
                    try:
                        await page.close()
                    except Exception:
                        pass
                self.loads_used += 1
                self.records.append({
                    "identity": identity, "round": rnd, "symbol": symbol, "tab": tab,
                    "outcome": outcome, "service_s": round(service, 3), "started": started,
                    "refused_blocks": sorted(refused_blocks), "history_rows": rows, "captcha": captcha,
                })
                self.log(f"  {identity:<8} r{rnd} {symbol} tab{tab} {outcome:<16} {service:5.1f}s"
                         + (f" 被拒块={','.join(sorted(refused_blocks))}" if refused_blocks else ""))
            if not outcome.startswith("refused") or outcome == "refused_today":
                return
        return

    async def _memory_ladder(self, rf, ctx, identity: str, baseline: dict) -> list:
        ladder = [{"pages": 0, **{k: v for k, v in tree_memory(os.getpid()).items() if k != "at"}}]
        for k in range(1, self.memory_pages + 1):
            if self.loads_used + k > self.load_budget:
                self.budget_hit = True
                break
            pages: list = []

            async def hold(symbol):
                page = await ctx.new_page()
                pages.append(page)
                try:
                    await rf.disguise_page(page, enabled=(identity == "disguise"))
                    await page.goto(rf.get_fund_flow_url(symbol), wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass

            await asyncio.gather(*(hold(s) for s in self.symbols[-k:]))
            self.loads_used += k
            peak = await asyncio.to_thread(peak_memory, os.getpid(), 3.0, 0.5)
            for page in pages:
                try:
                    await page.close()
                except Exception:
                    pass
            ladder.append({"pages": k, **{key: v for key, v in peak.items() if key != "at"}})
            self.log(f"  内存 同时 {k} 页：进程树 {peak['rss_mib']} MiB RSS"
                     + (f" / {peak['pss_mib']} MiB PSS" if peak["pss_mib"] is not None else "（无 PSS）"))
            await asyncio.sleep(1.0)
        for row in ladder:
            row["browser_delta_rss_mib"] = round(row["rss_mib"] - baseline["rss_mib"], 1)
            row["browser_delta_pss_mib"] = (
                round(row["pss_mib"] - baseline["pss_mib"], 1)
                if row.get("pss_mib") is not None and baseline.get("pss_mib") is not None else None)
        return ladder

    async def run(self) -> dict:
        from playwright.async_api import async_playwright

        from finmcp.datasource import realtime_ff as rf

        started = _now_text()
        baseline = tree_memory(os.getpid())
        async with async_playwright() as p:
            contexts = {}
            for identity in self.identities:
                disguise = identity == "disguise"
                browser = await p.chromium.launch(headless=True, args=rf.launch_arguments(disguise))
                ctx = await browser.new_context(**rf.context_options(disguise))
                if disguise:
                    await ctx.add_init_script(rf._HEADLESS_GAPS_SCRIPT)
                contexts[identity] = (browser, ctx)
            sem = asyncio.Semaphore(self.concurrency)
            try:
                for rnd in range(self.batches):
                    order = self.identities if rnd % 2 == 0 else list(reversed(self.identities))
                    for arm_index, identity in enumerate(order):
                        if self.loads_used >= self.load_budget:
                            self.budget_hit = True
                            break
                        symbols = self._pick(rnd, arm_index)
                        self.log(f"[browser] 第 {rnd + 1}/{self.batches} 轮 {IDENTITY_LABEL[identity]} {','.join(symbols)}")
                        await asyncio.gather(*(self._episode(rf, contexts[identity][1], identity, rnd, s, sem)
                                               for s in symbols))
                    if self.budget_hit:
                        self.log(f"[browser] 加载预算 {self.load_budget} 用完，停止")
                        break
                    if rnd < self.batches - 1 and self.pause > 0:
                        await asyncio.sleep(self.pause)
                if self.memory_pages > 0 and not self.budget_hit:
                    identity = "legacy" if "legacy" in contexts else self.identities[0]
                    for other, (browser, _) in list(contexts.items()):
                        if other != identity:
                            await browser.close()
                            contexts.pop(other)
                    await asyncio.sleep(1.5)
                    self.log(f"[browser] 内存阶梯（{IDENTITY_LABEL[identity]}，同时 1..{self.memory_pages} 页）")
                    self.memory = {"baseline": baseline, "identity": identity,
                                   "ladder": await self._memory_ladder(rf, contexts[identity][1], identity, baseline)}
            finally:
                for browser, _ in contexts.values():
                    try:
                        await browser.close()
                    except Exception:
                        pass
        return {
            "started": started, "finished": _now_text(),
            "identities": self.identities, "batches": self.batches, "max_tabs": self.max_tabs,
            "load_budget": self.load_budget, "loads_used": self.loads_used, "budget_hit": self.budget_hit,
            "concurrency": self.concurrency, "records": self.records, "memory": self.memory,
        }


def summarise_browser(records: list) -> dict:
    """每种身份一份：首 tab 成功率、逐 tab 条件恢复率、最终成功率、耗时、批内衰减。纯函数。"""
    by_identity: dict = defaultdict(list)
    for record in records:
        by_identity[record["identity"]].append(record)
    summary = {}
    for identity, recs in by_identity.items():
        episodes: dict = defaultdict(list)
        for record in recs:
            episodes[(record["round"], record["symbol"])].append(record)
        ordered = sorted(episodes.values(), key=lambda tabs: min(t["started"] for t in tabs))
        for tabs in ordered:
            tabs.sort(key=lambda t: t["tab"])
        n = len(ordered)

        def ok_at(tabs, k):
            return any(t["tab"] == k and t["outcome"] == "today" for t in tabs)

        max_tab = max((t["tab"] for t in recs), default=1)
        reached = {k: sum(1 for tabs in ordered if any(t["tab"] >= k for t in tabs)) for k in range(1, max_tab + 1)}
        success_at = {k: sum(1 for tabs in ordered if ok_at(tabs, k)) for k in range(1, max_tab + 1)}
        recovery = {k: (round(success_at[k] / reached[k], 3) if reached[k] else None) for k in range(2, max_tab + 1)}
        first_ok = success_at.get(1, 0)
        final_ok = sum(1 for tabs in ordered if any(t["outcome"] == "today" for t in tabs))
        half = n // 2
        first_half = ordered[:half]
        second_half = ordered[half:]
        rate = lambda part: (sum(1 for tabs in part if ok_at(tabs, 1)) / len(part)) if part else None
        first_half_rate, second_half_rate = rate(first_half), rate(second_half)
        service_ok = [t["service_s"] for t in recs if t["outcome"] == "today"]
        service_refused = [t["service_s"] for t in recs if t["outcome"].startswith("refused")]
        span = max(t["started"] + t["service_s"] for t in recs) - min(t["started"] for t in recs)
        summary[identity] = {
            "episodes": n,
            "loads": len(recs),
            "loads_per_min": round(len(recs) / span * 60, 1) if span > 0 else None,
            "first_tab_ok": first_ok,
            "first_tab_rate": round(first_ok / n, 3) if n else None,
            "reached": reached,
            "success_at": success_at,
            "recovery": recovery,
            "final_ok": final_ok,
            "final_rate": round(final_ok / n, 3) if n else None,
            "refused_tabs": sum(1 for t in recs if t["outcome"].startswith("refused")),
            "captcha_tabs": sum(1 for t in recs if t["captcha"]),
            "nodata_episodes": sum(1 for tabs in ordered if tabs[-1]["outcome"] == "nodata"),
            "error_episodes": sum(1 for tabs in ordered if tabs[-1]["outcome"].startswith("error")),
            "refused_blocks": {
                "today": sum(1 for t in recs if "today" in t["refused_blocks"]),
                "history": sum(1 for t in recs if "history" in t["refused_blocks"]),
            },
            "service_ok_s": summarise_values(service_ok),
            "service_refused_s": summarise_values(service_refused),
            "first_half_rate": None if first_half_rate is None else round(first_half_rate, 3),
            "second_half_rate": None if second_half_rate is None else round(second_half_rate, 3),
            "decline": (round(first_half_rate - second_half_rate, 3)
                        if first_half_rate is not None and second_half_rate is not None else None),
        }
    return summary


def run_browser(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 线上 .env 里的等待阈值就是这台机器在用的；缓存目录和调试开关一律隔离。
    for key, value in _dotenv(PROJECT_ROOT / ".env").items():
        os.environ.setdefault(key, value)
    os.environ["CACHE_DIR"] = str(out_dir / "cache")
    os.environ["CACHE_DISK_ENABLED"] = "0"
    os.environ["BROWSER_HEADFUL"] = "0"
    os.environ["BROWSER_KEEP_PAGES"] = "0"

    from finmcp import market_session
    from finmcp.datasource import trading_calendar

    import logging

    finmcp_logger = logging.getLogger("finmcp")
    for handler in list(finmcp_logger.handlers):
        handler.setLevel(logging.WARNING)
    file_handler = logging.FileHandler(out_dir / "browser.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    finmcp_logger.addHandler(file_handler)
    finmcp_logger.setLevel(logging.DEBUG)

    now = market_session.now_shanghai().replace(tzinfo=None)
    if in_trading_session(now, trading_calendar.is_trading_day, warmup=market_session.WARMUP_TIME) and not args.force:
        print(f"现在 {now:%H:%M} 是盘中，浏览器探测会和线上调用抢同一个出口 IP 的额度，不跑。"
              "要强行跑加 --force；合适的窗口是 15:05-15:55 或开盘前。", file=sys.stderr)
        return 2
    identities = [i.strip() for i in args.identities.split(",") if i.strip()]
    unknown = [i for i in identities if i not in IDENTITIES]
    if unknown:
        print(f"未知身份 {unknown}，可选 {','.join(IDENTITIES)}", file=sys.stderr)
        return 2
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else list(DEFAULT_SYMBOLS)
    probe = BrowserProbe(
        identities=identities, batches=args.batches, max_tabs=args.max_tabs, load_budget=args.load_budget,
        pause=args.pause, concurrency=args.concurrency, symbols=symbols, memory_pages=args.memory_pages,
    )
    worst = args.batches * len(identities) * 4 * args.max_tabs + args.memory_pages * (args.memory_pages + 1) // 2
    print(f"[browser] 身份 {identities}，{args.batches} 轮 × 4 标的，每 episode 最多 {args.max_tabs} 个 tab；"
          f"加载最少 {args.batches * len(identities) * 4} 次、最多 min({worst}, 预算 {args.load_budget}) 次")
    result = asyncio.run(probe.run())
    result["summary"] = summarise_browser(result["records"])
    result["env"] = {k: v for k, v in os.environ.items() if k.startswith(("BROWSER_", "FUND_FLOW_PAGE_"))}
    write_json(out_dir / "browser.json", result)
    for identity, s in result["summary"].items():
        print(f"[browser] {IDENTITY_LABEL[identity]}：首 tab {s['first_tab_ok']}/{s['episodes']}"
              f"，最终 {s['final_ok']}/{s['episodes']}，条件恢复 {s['recovery']}，"
              f"成功加载 p50/p90 {s['service_ok_s'].get('p50')}/{s['service_ok_s'].get('p90')} s，"
              f"批内前半/后半 {s['first_half_rate']}/{s['second_half_rate']}")
    print(f"[browser] 加载 {result['loads_used']} 次，写入 {out_dir / 'browser.json'}")
    return 0


# ── tonghuashun：K 线源的总预算 ───────────────────────────────────
#
# 量的是 platforms/tonghuashun.py 的 fetch_kline 端到端耗时，逐次记成功/失败。
# 预算要坐在**成功**取数的最大耗时之上，所以只有成功的那些进判据；失败的耗时只用来
# 说明「不设预算时最坏能拖多久」。


def probe_tonghuashun_once(symbol: str, window_days: int) -> dict:
    """跑一次真实的 fetch_kline，返回耗时和结果。不走缓存，也不碰服务。"""
    import datetime as _dt

    from finmcp.datasource import kline_source
    from finmcp.datasource.platforms import tonghuashun

    end = _dt.date.today()
    request = kline_source.KlineRequest(
        code="".join(c for c in symbol if c.isdigit()),
        start_date=(end - _dt.timedelta(days=window_days)).strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        adjust="qfq",
        symbol=symbol,
    )
    started = time.monotonic()
    try:
        frame = tonghuashun.TonghuashunPlatform().fetch_kline(request)
    except Exception as error:                      # noqa: BLE001 - 逐次记账，别中断整轮
        return {"symbol": symbol, "seconds": round(time.monotonic() - started, 3),
                "ok": False, "rows": 0, "error": f"{type(error).__name__}: {error}"[:160]}
    seconds = round(time.monotonic() - started, 3)
    rows = 0 if frame is None else len(frame)
    return {"symbol": symbol, "seconds": seconds, "ok": rows > 0, "rows": rows,
            "error": None if rows else "返回空"}


def run_tonghuashun(args) -> int:
    """按固定名单反复取 K 线，量出当前环境到同花顺的耗时分布。"""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 指数优先：它们才是 tonghuashun 排第一的那一类，个股走腾讯，量了也用不上。
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or list(THS_SYMBOLS)
    records = []
    for round_no in range(args.rounds):
        for symbol in symbols:
            record = probe_tonghuashun_once(symbol, args.window_days) | {"round": round_no + 1}
            records.append(record)
            print(f"[tonghuashun] 第 {record['round']}/{args.rounds} 轮 {symbol:10s} "
                  f"{record['seconds']:6.2f}s {'ok ' + str(record['rows']) + ' 根' if record['ok'] else record['error']}")
            if args.pause:
                time.sleep(args.pause)
    ok = [r["seconds"] for r in records if r["ok"]]
    bad = [r["seconds"] for r in records if not r["ok"]]
    result = {"generated": _now_text(), "window_days": args.window_days,
              "symbols": symbols, "rounds": args.rounds,
              "records": records,
              "ok": summarise_values(ok), "failed": summarise_values(bad)}
    write_json(out_dir / "tonghuashun.json", result)
    print(f"[tonghuashun] 成功 {len(ok)}/{len(records)}："
          f"p50 {result['ok'].get('p50')}s、p90 {result['ok'].get('p90')}s、max {result['ok'].get('max')}s")
    if bad:
        print(f"[tonghuashun] 失败 {len(bad)} 次，耗时 max {result['failed'].get('max')}s"
              f"（不设预算时最坏能拖这么久）")
    print(f"[tonghuashun] 写入 {out_dir / 'tonghuashun.json'}")
    return 0


def decide_tonghuashun_budget(measured: Optional[dict]) -> Decision:
    """预算 = 成功取数的最大耗时 × 余量，夹在区间内。

    用 max 不用分位数：判据是"不能砍掉本来能成功的取数"，分位数按定义会砍掉尾部。
    """
    key = "KLINE_TONGHUASHUN_BUDGET_SECONDS"
    stats = (measured or {}).get("ok") or {}
    n = stats.get("n", 0)
    if n < THS_MIN_SAMPLES:
        return Decision(key, DEFAULTS[key], "inconclusive",
                        f"成功取数只有 {n} 次，不足 {THS_MIN_SAMPLES}，保持默认")
    slowest = stats["max"]
    raw = math.ceil(slowest * THS_MARGIN)
    low, high = THS_BUDGET_RANGE
    value = int(min(high, max(low, raw)))
    # 夹过就得说，不然读的人会以为 value 是 max × 余量算出来的。这台机器网络快时
    # raw 会小到个位数，落到下限；反过来慢到 raw > high 时落到上限，那说明这条链路
    # 本来就该换源，而不是把预算继续放大。
    if raw < low:
        how = f"取 max × {THS_MARGIN} = {raw}s，低于下限 {low}s，按下限"
    elif raw > high:
        how = f"取 max × {THS_MARGIN} = {raw}s，高于上限 {high}s，按上限——这条链路慢到该换源了"
    else:
        how = f"取 max × {THS_MARGIN} = {value}s"
    failed = (measured or {}).get("failed") or {}
    if not failed.get("n"):
        tail = "；本轮没有失败取数，预算是给上游抽风那天留的"
    elif value < failed["max"]:
        tail = f"；失败 {failed['n']} 次、最长 {failed['max']}s，预算能把它们截在 {value}s"
    else:
        # 推荐值高过失败耗时，这一轮它一次都不会触发。不改推荐值——把预算压到成功
        # 取数之下就是在丢数据；该做的是换源或者查这条链路为什么慢。
        tail = (f"；失败 {failed['n']} 次、最长 {failed['max']}s，**短于推荐值**，"
                f"这个预算截不住它们——成功和失败的耗时分不开，先查链路而不是压预算")
    return Decision(key, str(value), "measured",
                    f"成功取数 {n} 次，p50 {stats['p50']}s、p90 {stats['p90']}s、"
                    f"max {slowest}s；{how}{tail}")


# ── recommend：规则 ───────────────────────────────────────────────


@dataclass
class Decision:
    key: str
    value: str
    status: str        # measured | default | inconclusive | unmeasured
    evidence: str

    @property
    def default(self) -> str:
        return DEFAULTS.get(self.key, "")

    @property
    def deviates(self) -> bool:
        return self.value != self.default


def _pct(x) -> str:
    return "--" if x is None else f"{x * 100:.0f}%"


def decide_disguise(summary: dict) -> Decision:
    key = "BROWSER_DISGUISE"
    legacy, disguise = summary.get("legacy"), summary.get("disguise")
    if not legacy or not disguise:
        measured = legacy or disguise
        if not measured:
            return Decision(key, "0", "unmeasured", "没有浏览器探测数据，保持默认")
        which = "原样" if legacy else "伪装"
        return Decision(key, "0", "unmeasured",
                        f"只测了{which}身份（首 tab {_pct(measured['first_tab_rate'])}），没有对照，保持默认")
    n = min(legacy["episodes"], disguise["episodes"])
    text = (f"原样 {legacy['first_tab_ok']}/{legacy['episodes']} = {_pct(legacy['first_tab_rate'])}，"
            f"伪装 {disguise['first_tab_ok']}/{disguise['episodes']} = {_pct(disguise['first_tab_rate'])}")
    if n < MIN_EPISODES_PER_ARM:
        return Decision(key, "0", "inconclusive", f"{text}；每臂不足 {MIN_EPISODES_PER_ARM} 个 episode，判不出，保持默认")
    diff = disguise["first_tab_rate"] - legacy["first_tab_rate"]
    if diff >= DISGUISE_MARGIN:
        return Decision(key, "1", "measured", f"{text}；伪装高 {diff * 100:.0f} 个百分点，超过判据 {DISGUISE_MARGIN * 100:.0f}")
    if -diff >= DISGUISE_MARGIN:
        return Decision(key, "0", "measured", f"{text}；原样高 {-diff * 100:.0f} 个百分点，伪装反而被拒得更多")
    return Decision(key, "0", "default", f"{text}；差 {abs(diff) * 100:.0f} 个百分点，在判据 {DISGUISE_MARGIN * 100:.0f} 以内，保持默认")


def decide_max_loads(arm: Optional[dict], max_tabs: int) -> Decision:
    key = "FUND_FLOW_PAGE_MAX_LOADS"
    if not arm or arm["episodes"] < MIN_EPISODES_PER_ARM:
        n = arm["episodes"] if arm else 0
        return Decision(key, "2", "inconclusive", f"只有 {n} 个 episode，不足 {MIN_EPISODES_PER_ARM}，保持默认")
    first = arm["first_tab_rate"]
    head = f"首 tab {arm['first_tab_ok']}/{arm['episodes']} = {_pct(first)}"
    if first >= SATURATED_FIRST_TAB:
        return Decision(key, "2", "measured", f"{head}，重试基本用不到；留 2 当保险：被拒一次还能换一个 tab，代价只落在那个失败标的上（多付一次加载）")
    curve = "、".join(f"第 {k} 个 tab {arm['success_at'].get(k, 0)}/{arm['reached'].get(k, 0)}"
                     for k in range(2, max_tabs + 1) if arm["reached"].get(k))
    if arm["decline"] is not None and arm["decline"] > DECLINE_MAX:
        return Decision(key, "2", "measured",
                        f"{head}；批内首 tab 成功率前半 {_pct(arm['first_half_rate'])} 后半 {_pct(arm['second_half_rate'])}，"
                        f"掉了 {arm['decline'] * 100:.0f} 个百分点——重试在喂风控，不多给。条件恢复：{curve or '无'}")
    k = 1
    for tab in range(2, max_tabs + 1):
        reached = arm["reached"].get(tab, 0)
        rec = arm["recovery"].get(tab)
        if reached < RECOVERY_MIN_N or rec is None:
            break
        if rec >= RECOVERY_MIN:
            k = tab
        else:
            break
    value = max(2, k)
    return Decision(key, str(value), "measured",
                    f"{head}；条件恢复：{curve or '无'}。允许到第 {value} 个 tab（判据：条件恢复 ≥ {RECOVERY_MIN * 100:.0f}%、到达 ≥ {RECOVERY_MIN_N}）")


def decide_queue_wait(arm: Optional[dict]) -> tuple:
    wait_key, budget_key = "FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS", "FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS"
    svc = (arm or {}).get("service_ok_s") or {}
    if svc.get("n", 0) < MIN_SERVICE_SAMPLES:
        why = f"成功加载只有 {svc.get('n', 0)} 次，不足 {MIN_SERVICE_SAMPLES}，保持默认"
        return Decision(wait_key, DEFAULTS[wait_key], "inconclusive", why), Decision(budget_key, DEFAULTS[budget_key], "inconclusive", why)
    p90 = svc["p90"]
    wait = int(min(QUEUE_WAIT_RANGE[1], max(QUEUE_WAIT_RANGE[0], math.ceil(p90 + 0.5))))
    default_wait = int(DEFAULTS[wait_key])
    if abs(wait - default_wait) < 2:
        why = f"成功加载 p50 {svc['p50']} s / p90 {p90} s（n={svc['n']}），推得 {wait}，与默认 {default_wait} 相差不到 2 秒，保持默认"
        return (Decision(wait_key, DEFAULTS[wait_key], "default", why),
                Decision(budget_key, DEFAULTS[budget_key], "default", "等待保持默认，预算跟着不动"))
    budget = int(min(BUDGET_MAX, max(wait + 3, wait + math.ceil(p90))))
    why = f"成功加载 p50 {svc['p50']} s / p90 {p90} s（n={svc['n']}）；等待要盖住 p90，否则一批 4 个里的第 4 个结构上排不到"
    return (Decision(wait_key, str(wait), "measured", why),
            Decision(budget_key, str(budget), "measured", f"等待 {wait} 加一次 p90 加载 {math.ceil(p90)}，满批最坏情形落在其内"))


def decide_max_pages(memory: Optional[dict], service_base_mib: Optional[float], base_source: str) -> tuple:
    pages_key, conc_key = "BROWSER_MAX_PAGES", "FUND_FLOW_PAGE_CONCURRENCY"
    ladder = (memory or {}).get("ladder") or []
    if len(ladder) < 3:
        why = "没有内存阶梯数据（至少要同时 1 页和 2 页两档），保持默认"
        return Decision(pages_key, DEFAULTS[pages_key], "unmeasured", why), Decision(conc_key, DEFAULTS[conc_key], "unmeasured", "与 BROWSER_MAX_PAGES 同值")
    if any(row.get("browser_delta_pss_mib") is None for row in ladder):
        rss = "、".join(f"{row['pages']} 页 {row['browser_delta_rss_mib']} MiB" for row in ladder[1:])
        why = (f"这台机器读不到 PSS（macOS 或无 /proc 权限），只有 RSS：{rss}。RSS 把共享页在每个渲染进程里重复计入，"
               f"不能和 {MEMORY_BUDGET_MIB:g} MiB 预算比，保持默认；在能读到 PSS 的 Linux 机器上跑才有结论")
        return Decision(pages_key, DEFAULTS[pages_key], "unmeasured", why), Decision(conc_key, DEFAULTS[conc_key], "unmeasured", "与 BROWSER_MAX_PAGES 同值")
    by_pages = {row["pages"]: row["browser_delta_pss_mib"] for row in ladder}
    one = by_pages.get(1)
    steps = [by_pages[k] - by_pages[k - 1] for k in sorted(by_pages) if k >= 2]
    marginal = sum(steps) / len(steps) if steps else None
    base = service_base_mib if service_base_mib is not None else SERVICE_BASE_MIB_DOCUMENTED
    if one is None or marginal is None or marginal <= 0:
        why = f"阶梯数据不足以算边际（{by_pages}），保持默认"
        return Decision(pages_key, DEFAULTS[pages_key], "inconclusive", why), Decision(conc_key, DEFAULTS[conc_key], "inconclusive", "与 BROWSER_MAX_PAGES 同值")
    headroom = MEMORY_BUDGET_MIB - base - one
    fit = 1 + int(math.floor(headroom / marginal)) if headroom > 0 else 0
    value = max(1, min(3, fit))
    why = (f"服务不含浏览器 {base:.0f} MiB（{base_source}），浏览器开 1 页 {one:.0f} MiB PSS，每多一页 {marginal:.0f} MiB；"
           f"预算 {MEMORY_BUDGET_MIB:g} 内放得下 {fit} 页，夹到 [1, 3] 取 {value}")
    status = "measured"
    return (Decision(pages_key, str(value), status, why),
            Decision(conc_key, str(value), status, "与 BROWSER_MAX_PAGES 同值——两道串联的闸门，只提一个另一个立刻成瓶颈"))


def channel_notes(facts: Optional[dict]) -> list:
    """从可达性结果得出的说明。只解释，不重排 provider 顺序。"""
    if not facts:
        return ["没有 facts.json，可达性未探测"]
    rows = facts.get("reachability") or []
    klass = {(r["host"], r["channel"]): r["class"] for r in rows}
    notes = []

    def c(host, channel="direct"):
        return klass.get((host, channel), "未测")

    direct_ok = {h for h in ("push2.eastmoney.com", "push2his.eastmoney.com") if c(h) == "ok"}
    imp_ok = {h for h in ("push2.eastmoney.com", "push2his.eastmoney.com") if c(h, "impersonate") == "ok"}
    if facts.get("proxy_configured"):
        notes.append("网关已配置：HTTP_CHANNEL=auto 会走 proxy，下面直连/伪装的结果只在网关不可用时才起作用")
    if len(direct_ok) == 2:
        notes.append("push2/push2his 直连可达：HTTP_CHANNEL=auto 在网关关闭时走伪装通道，direct 在这台机器上也能用")
    elif len(imp_ok) == 2:
        notes.append(f"push2/push2his 直连 {c('push2.eastmoney.com')}/{c('push2his.eastmoney.com')}、伪装通道可达："
                     "HTTP_CHANNEL=auto（网关关闭时即 impersonate）是正确的")
    else:
        notes.append(f"push2 直连 {c('push2.eastmoney.com')} 伪装 {c('push2.eastmoney.com', 'impersonate')}；"
                     f"push2his 直连 {c('push2his.eastmoney.com')} 伪装 {c('push2his.eastmoney.com', 'impersonate')}："
                     "东财 HTTP 层在这台机器上不稳，基本数据靠 tencent、资金流靠 push2delay 与页面兜底，"
                     "确认 FUND_FLOW_PROVIDERS 含 eastmoney_delay、BASIC_INFO_PROVIDERS 含 tencent")
    if c("push2delay.eastmoney.com") != "ok":
        notes.append(f"push2delay {c('push2delay.eastmoney.com')}：科创50 等无页面标的的盘中实时资金流会是「暂无」，资金流当日行也补不上")
    if c("legulegu.com") != "ok":
        sws = c("www.swsresearch.com")
        notes.append(f"乐咕乐股 {c('legulegu.com')}，申万研究所 {sws}："
                     + ("分级由 swsresearch 补齐（默认顺序已含）" if sws == "ok" else "两个分级源都不通，板块资金流会退回全部板块一起排"))
    for host, label in (("d.10jqka.com.cn", "同花顺日 K"), ("qt.gtimg.cn", "腾讯行情"), ("proxy.finance.qq.com", "腾讯日 K"),
                        ("data.eastmoney.com", "data.eastmoney（dataapi / zjlx 页面）")):
        bad = [r for r in rows if r["host"] == host and r["class"] != "ok"]
        if bad:
            notes.append(f"{label} {', '.join(f'{r['probe']}={r['class']}' for r in bad)}：该源在这台机器上取不到，链路会落到下一个源")
    return notes


_FALSEY = {"0", "false", "no", "off", "disabled", "none", ""}


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() not in _FALSEY


def lint_env(values: dict, decisions: dict) -> list:
    """线上 .env 与推荐值、与调试残留的比对。返回 [{key, value, level, finding}]。"""
    findings = []

    def add(key, level, finding):
        findings.append({"key": key, "value": values.get(key), "level": level, "finding": finding})

    if _truthy(values.get("BROWSER_HEADFUL")):
        add("BROWSER_HEADFUL", "high", "调试开关残留：有头浏览器每页多占内存、慢一倍，服务器上应为 0")
    if _truthy(values.get("BROWSER_KEEP_PAGES")):
        add("BROWSER_KEEP_PAGES", "high", "调试开关残留：留着的页面每分钟打东财 17 次接口，每页约 120 MiB")
    if "CACHE_ENABLED" in values and not _truthy(values["CACHE_ENABLED"]):
        add("CACHE_ENABLED", "medium", "全部缓存关着：这是 prove_equivalence 的测试形态，线上每次调用都打上游")
    for key in values:
        if key.startswith("CN_STOCK_"):
            add(key, "medium", "1.x 前缀的旧名字，2.0.0 不再读它，写了等于没写")
    try:
        loads = int(values.get("FUND_FLOW_PAGE_MAX_LOADS", DEFAULTS["FUND_FLOW_PAGE_MAX_LOADS"]))
    except ValueError:
        loads = None
    rec_loads = int(decisions["FUND_FLOW_PAGE_MAX_LOADS"].value) if "FUND_FLOW_PAGE_MAX_LOADS" in decisions else 2
    if loads is not None and loads > rec_loads:
        decision = decisions.get("FUND_FLOW_PAGE_MAX_LOADS")
        why = (decision.evidence if decision is not None and decision.status == "measured"
               else "被拒后再开的每一个 tab 都在消耗同一出口的频率额度，同一批里后到的标的更容易被拒，"
                    "条件恢复率又逐 tab 递减；多给的重试换来的少、压掉的多")
        add("FUND_FLOW_PAGE_MAX_LOADS", "high", f"高于推荐值 {rec_loads}：{why}")
    try:
        cooldown = float(values.get("FUND_FLOW_PAGE_COOLDOWN_SECONDS", DEFAULTS["FUND_FLOW_PAGE_COOLDOWN_SECONDS"]))
        if cooldown < 60:
            add("FUND_FLOW_PAGE_COOLDOWN_SECONDS", "medium", "低于默认 60：熔断打开后不到一分钟就放行，而被拒的出口在这么短的时间里很少恢复，短冷却只是多付加载")
    except ValueError:
        pass
    reported = {f["key"] for f in findings}
    for key, decision in decisions.items():
        if key not in values or key in reported:
            continue
        if values[key].strip() == decision.value:
            continue
        if decision.status == "measured":
            add(key, "high", f"线上 {values[key]}，实测推荐 {decision.value}：{decision.evidence}")
        elif values[key].strip() == decision.default:
            add(key, "low", "与默认值相同，可以删掉这一行")
        else:
            add(key, "medium", f"线上 {values[key]} 偏离默认 {decision.default}，探测未能判定（{decision.status}）：{decision.evidence}")
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["level"]], f["key"]))
    return findings


def render_env(decisions: list, meta: dict) -> str:
    lines = [
        f"# 由 scripts/probe_tuning.py 于 {meta.get('generated')} 在 {meta.get('hostname')} 上生成。",
        "# 只列探测覆盖的配置；没列的一律用 .env.example 的默认值。每一项上面是依据。",
        "# 用法：把与线上 .env 不同的行改过去，重启，跑 scripts/verify_release.py 与上一份报告对比。",
        "# 不要整份覆盖线上 .env——网关、代理这些不在探测范围内的项以线上为准。",
        "",
    ]
    for decision in decisions:
        marker = {"measured": "实测", "default": "默认", "inconclusive": "判不出，保持默认", "unmeasured": "未测，保持默认"}[decision.status]
        lines.append(f"# [{marker}] {decision.evidence}")
        lines.append(f"{decision.key}={decision.value}")
        lines.append("")
    return "\n".join(lines)


def render_report(*, facts: Optional[dict], browser: Optional[dict], decisions: list, lint: list,
                  notes: list, meta: dict) -> str:
    L = []
    L.append(f"# 探测调优报告 {meta.get('generated')}")
    L.append("")
    L.append(f"- 机器：{meta.get('hostname')}；出口 IP：{(facts or {}).get('egress_ip') or '未取到'}"
             + (f"；系统代理 {facts['proxy_env']}" if (facts or {}).get("proxy_env") else ""))
    if facts:
        m = facts["machine"]
        L.append(f"- {m.get('system')} {m.get('release')} {m.get('machine')}，{m.get('cores')} 核，内存 {m.get('mem_total_mib')} MiB"
                 f"（可用 {m.get('mem_available_mib')}），Python {m.get('python')}，Playwright {m.get('playwright')}，"
                 f"Chromium {m.get('chromium')}，Xvfb {'有' if m.get('xvfb') else '无'}，DISPLAY {m.get('display') or '未设'}")
        if facts.get("cpu_ref_total_ms"):
            L.append(f"- 单核基准 cpu_ref total {facts['cpu_ref_total_ms']:.0f} ms（与压测机相除即降级系数）")
        s = facts.get("live_service")
        if s:
            L.append(f"- 线上服务 pid {s['pid']}：进程树 {s['rss_mib']} MiB RSS"
                     + (f" / {s['pss_mib']} MiB PSS" if s.get("pss_mib") is not None else "")
                     + f"，浏览器进程 {s['browser_processes']} 个，不含浏览器 {s['service_base_mib']} MiB")
    L.append("")
    L.append("## 一、推荐配置")
    L.append("")
    L.append("| 配置项 | 推荐 | 默认 | 状态 | 依据 |")
    L.append("|---|---|---|---|---|")
    for d in decisions:
        L.append(f"| {d.key} | **{d.value}** | {d.default} | {d.status} | {d.evidence} |")
    L.append("")
    L.append("状态：measured 实测判定；default 实测后差异在判据内、保持默认；inconclusive 样本不够；unmeasured 没测这一项。")
    L.append("")
    L.append("## 二、线上 .env 比对")
    L.append("")
    if lint:
        L.append("| 级别 | 配置项 | 线上值 | 发现 |")
        L.append("|---|---|---|---|")
        for f in lint:
            L.append(f"| {f['level']} | {f['key']} | {f['value']} | {f['finding']} |")
    else:
        L.append("线上 .env 没有与推荐值冲突的项，也没有调试残留。")
    L.append("")
    L.append("## 三、可达性与通道")
    L.append("")
    for note in notes:
        L.append(f"- {note}")
    if facts and facts.get("reachability"):
        L.append("")
        L.append("| 源 | 通道 | 结果 | 耗时 | 备注 |")
        L.append("|---|---|---|---|---|")
        for r in facts["reachability"]:
            extra = r["note"] or r["error"] or (f"→ {r['location']}" if r.get("location") else "")
            L.append(f"| {r['probe']} | {r['channel']} | {r['class']} | {r['latency_ms']} ms | {extra} |")
    L.append("")
    L.append("## 四、浏览器探测")
    L.append("")
    if browser and browser.get("summary"):
        L.append(f"- {browser['started']} → {browser['finished']}，{browser['batches']} 轮，每 episode 最多 {browser['max_tabs']} 个 tab，"
                 f"加载 {browser['loads_used']} 次" + ("（预算用尽）" if browser.get("budget_hit") else ""))
        L.append("")
        L.append("| 身份 | episode | 首 tab | 第 2 tab 条件恢复 | 第 3 tab 条件恢复 | 最终 | 滑块 tab | 成功加载 p50/p90 | 批内前半/后半 | 加载/分 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for identity, s in browser["summary"].items():
            rec2 = s["recovery"].get("2") if isinstance(next(iter(s["recovery"]), None), str) else s["recovery"].get(2)
            rec3 = s["recovery"].get("3") if isinstance(next(iter(s["recovery"]), None), str) else s["recovery"].get(3)
            reached = {int(k): v for k, v in s["reached"].items()}
            success = {int(k): v for k, v in s["success_at"].items()}
            L.append(f"| {IDENTITY_LABEL.get(identity, identity)} | {s['episodes']} | {s['first_tab_ok']}/{s['episodes']} ({_pct(s['first_tab_rate'])}) "
                     f"| {success.get(2, 0)}/{reached.get(2, 0)} ({_pct(rec2)}) | {success.get(3, 0)}/{reached.get(3, 0)} ({_pct(rec3)}) "
                     f"| {s['final_ok']}/{s['episodes']} | {s['captcha_tabs']} | {s['service_ok_s'].get('p50', '--')}/{s['service_ok_s'].get('p90', '--')} s "
                     f"| {_pct(s['first_half_rate'])}/{_pct(s['second_half_rate'])} | {s['loads_per_min']} |")
        mem = (browser.get("memory") or {}).get("ladder")
        if mem:
            L.append("")
            L.append("| 同时页数 | 进程树 RSS | 进程树 PSS | 浏览器增量 RSS | 浏览器增量 PSS |")
            L.append("|---|---|---|---|---|")
            for row in mem:
                L.append(f"| {row['pages']} | {row['rss_mib']} | {row.get('pss_mib') if row.get('pss_mib') is not None else '--'} "
                         f"| {row['browser_delta_rss_mib']} | {row['browser_delta_pss_mib'] if row.get('browser_delta_pss_mib') is not None else '--'} |")
    else:
        L.append("没有浏览器探测数据（browser 子命令没跑或被盘中守卫拒绝）。")
    L.append("")
    L.append("## 五、下一步")
    L.append("")
    L.append("1. 把 `.env.recommended` 里与线上不同的行改进 .env（不要整份覆盖），重启服务。")
    L.append("2. `python scripts/verify_release.py --skip-baseline`，与上一份报告比资金流完整率和第六节耗时。")
    L.append("3. 不动线上也能先验：`python scripts/probe_tuning.py verify --env-file <本目录>/.env.recommended`，"
             "用 ENV_PREFIX 起隔离实例跑 verify_release。")
    L.append("4. 并发上限要单独量：`python scripts/loadtest_mcp.py --launch --port 8790 --closed-loop --steps 1,5,10 "
             "--env BROWSER_MAX_PAGES=<推荐值> --env FUND_FLOW_PAGE_CONCURRENCY=<推荐值>`。")
    L.append("5. 每周重跑一轮，或 verify_release 资金流完整率连续两次低于 90% 时重跑。")
    L.append("")
    return "\n".join(L)


def build_decisions(facts: Optional[dict], browser: Optional[dict], service_base_mib: Optional[float] = None,
                    tonghuashun: Optional[dict] = None) -> list:
    summary = (browser or {}).get("summary") or {}
    # JSON 往返后 reached/success_at/recovery 的键是字符串，这里统一成 int。
    for arm in summary.values():
        for name in ("reached", "success_at", "recovery"):
            arm[name] = {int(k): v for k, v in (arm.get(name) or {}).items()}
    disguise = decide_disguise(summary)
    chosen = "disguise" if disguise.value == "1" else ("legacy" if "legacy" in summary else next(iter(summary), None))
    arm = summary.get(chosen) if chosen else None
    max_tabs = (browser or {}).get("max_tabs") or 3
    loads = decide_max_loads(arm, max_tabs)
    wait, budget = decide_queue_wait(arm)
    live = (facts or {}).get("live_service") or {}
    if service_base_mib is not None:
        base, base_source = service_base_mib, "--service-base-mib 指定"
    elif live.get("metric") == "pss" and live.get("service_base_mib") is not None:
        base, base_source = live["service_base_mib"], live.get("service_base_source", "线上服务")
    else:
        base, base_source = None, f"参考值 {SERVICE_BASE_MIB_DOCUMENTED:g} MiB：这台机器上没量到线上服务的 PSS，--service-base-mib 可覆盖"
    pages, conc = decide_max_pages((browser or {}).get("memory"), base, base_source)
    channel = Decision("HTTP_CHANNEL", "auto", "default",
                       "网关可用走 proxy，否则伪装通道；伪装通道再被拒时运行时自己暂停并退回，无需按机器改")
    ths = decide_tonghuashun_budget(tonghuashun)
    return [disguise, loads, wait, budget, pages, conc, channel, ths]


def run_recommend(args) -> int:
    out_dir = Path(args.out_dir)
    facts = read_json(out_dir / "facts.json")
    browser = read_json(out_dir / "browser.json")
    tonghuashun = read_json(out_dir / "tonghuashun.json")
    if facts is None and browser is None:
        print(f"{out_dir} 里没有 facts.json 也没有 browser.json，先跑 facts / browser 或 all", file=sys.stderr)
        return 2
    decisions = build_decisions(facts, browser, getattr(args, "service_base_mib", None), tonghuashun)
    env_path = Path(args.env)
    values = _dotenv(env_path)
    lint = lint_env(values, {d.key: d for d in decisions})
    notes = channel_notes(facts)
    meta = {"generated": _now_text(),
            "hostname": (facts or {}).get("machine", {}).get("hostname") or socket.gethostname()}
    (out_dir / ".env.recommended").write_text(render_env(decisions, meta), encoding="utf-8")
    (out_dir / "report.md").write_text(
        render_report(facts=facts, browser=browser, decisions=decisions, lint=lint, notes=notes, meta=meta),
        encoding="utf-8")
    write_json(out_dir / "decisions.json", {"decisions": [asdict(d) | {"default": d.default} for d in decisions],
                                            "lint": lint, "notes": notes, "meta": meta})
    print(f"[recommend] {'配置项':<38} {'推荐':>6} {'默认':>6}  状态")
    for d in decisions:
        flag = " ←" if d.deviates else ""
        print(f"[recommend] {d.key:<38} {d.value:>6} {d.default:>6}  {d.status}{flag}")
    if lint:
        print(f"[recommend] 线上 {env_path} 有 {len(lint)} 条发现：")
        for f in lint:
            print(f"  [{f['level']}] {f['key']}={f['value']}  {f['finding']}")
    else:
        print(f"[recommend] 线上 {env_path} 与推荐无冲突")
    for note in notes:
        print(f"[recommend] {note}")
    print(f"[recommend] 写入 {out_dir / '.env.recommended'} 与 {out_dir / 'report.md'}")
    return 0


# ── verify：用候选配置起隔离实例，跑 verify_release ────────────────


def prefixed_environment(base_env: dict, dotenv: dict, candidate: dict, prefix: str, cache_dir: Path) -> dict:
    """线上 .env 的值全部换成带前缀的名字，再叠上候选值。

    main.py 用 load_dotenv(override=True) 把 .env 灌进环境，注入的同名变量会被盖掉——loadtest
    只能提醒"被 .env 遮住"。config.env() 只读 ENV_PREFIX + 名字，所以给实例一个前缀，.env 里
    裸名字的值就一个都读不到，读到的全是这里写的。
    """
    if "ENV_PREFIX" in dotenv:
        raise SystemExit(".env 里写了 ENV_PREFIX，隔离实例的前缀会被它盖掉，先去掉再跑 verify")
    env = dict(base_env)
    env["ENV_PREFIX"] = prefix
    for key, value in {**dotenv, **candidate}.items():
        if value is not None:
            env[f"{prefix}{key}"] = value
    env[f"{prefix}CACHE_DIR"] = str(cache_dir)
    return env


def run_verify(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env_file = Path(args.env_file)
    if not env_file.exists():
        print(f"候选配置不存在：{env_file}", file=sys.stderr)
        return 2
    if shutil.which("mcporter") is None:
        print("找不到 mcporter，verify_release 跑不起来", file=sys.stderr)
        return 2
    candidate = _dotenv(env_file)
    dotenv = _dotenv(PROJECT_ROOT / ".env")
    env = prefixed_environment(os.environ, dotenv, candidate, "PROBE_", out_dir / "verify-cache")
    if not env.get("DISPLAY"):
        xvfb_pid = _pid_alive(XVFB_PID_FILE)
        if xvfb_pid:
            env["DISPLAY"] = f":{dotenv.get('XVFB_DISPLAY_NUMBER', '99')}"
    log_path = out_dir / "verify-server.log"
    print(f"[verify] 候选 {len(candidate)} 项：{candidate}")
    print(f"[verify] 起隔离实例 port={args.port}，ENV_PREFIX=PROBE_，日志 {log_path}")
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, "main.py", "--transport", "http", "--port", str(args.port)],
            cwd=str(PROJECT_ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
        )
    peak = {"rss_mib": 0.0, "pss_mib": None, "metric": "rss"}
    stop = threading.Event()

    def sample():
        pss_all = True
        while not stop.is_set():
            s = tree_memory(process.pid)
            peak["rss_mib"] = max(peak["rss_mib"], s["rss_mib"])
            if s["pss_mib"] is None:
                pss_all = False
            elif pss_all:
                peak["pss_mib"] = max(peak["pss_mib"] or 0.0, s["pss_mib"])
            if not pss_all:
                peak["pss_mib"] = None
            stop.wait(1.0)

    sampler = threading.Thread(target=sample, daemon=True)
    try:
        deadline = time.perf_counter() + args.timeout
        while True:
            if process.poll() is not None:
                print(f"[verify] 实例启动失败，退出码 {process.returncode}，见 {log_path}", file=sys.stderr)
                return 2
            text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            if "Starting MCP app" in text:
                break
            if time.perf_counter() > deadline:
                print("[verify] 实例启动超时", file=sys.stderr)
                return 2
            time.sleep(0.5)
        sampler.start()
        config_path = out_dir / "mcporter.json"
        config_path.write_text(json.dumps({"mcpServers": {"cn-stock": {"baseUrl": f"http://127.0.0.1:{args.port}/cnstock/mcp"}}},
                                          indent=2), encoding="utf-8")
        report_path = out_dir / "verify-report.md"
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "verify_release.py"), "--skip-baseline",
                   "--config", str(config_path), "--report", str(report_path), "--log", str(log_path),
                   "--memory-interval", "0"]
        if args.only:
            command += ["--only", args.only]
        print(f"[verify] {' '.join(command)}")
        code = subprocess.call(command, cwd=str(PROJECT_ROOT))
        stop.set()
        peak["metric"] = "pss" if peak["pss_mib"] is not None else "rss"
        value = peak["pss_mib"] if peak["metric"] == "pss" else peak["rss_mib"]
        print(f"[verify] verify_release 退出码 {code}；隔离实例进程树峰值 {value:.0f} MiB（{'PSS' if peak['metric'] == 'pss' else 'RSS 合计，上界'}），"
              f"预算 {MEMORY_BUDGET_MIB:g}；报告 {report_path}")
        write_json(out_dir / "verify.json", {"candidate": candidate, "exit_code": code, "peak_memory": peak,
                                             "report": str(report_path), "finished": _now_text()})
        return code
    finally:
        stop.set()
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        print("[verify] 隔离实例已停止")


# ── 入口 ──────────────────────────────────────────────────────────


def run_all(args) -> int:
    code = run_facts(args)
    if code:
        return code
    code = run_browser(args)
    if code:
        print("[all] 浏览器探测没跑成，仍按 facts 出推荐（浏览器相关项会标为未测）")
    if run_tonghuashun(args):
        print("[all] 同花顺探测没跑成，K 线预算会保持默认")
    return run_recommend(args)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    default_out = str(DEFAULT_OUT_ROOT / dt.datetime.now().strftime("%Y%m%d-%H%M%S"))

    def common(p):
        p.add_argument("--out-dir", default=default_out, help="结果目录，缺省 .runtime/probe-tuning/<时间戳>")

    p_facts = sub.add_parser("facts", help="机器、出口、各源各通道可达性、线上服务内存基数")
    common(p_facts)
    p_facts.add_argument("--skip-cpu-ref", action="store_true")

    p_browser = sub.add_parser("browser", help="两种身份交错加载页面，逐 tab 记账；同时开 1..N 页量内存")
    common(p_browser)
    p_browser.add_argument("--identities", default="legacy,disguise")
    p_browser.add_argument("--batches", type=int, default=6, help="每种身份的轮数，每轮 4 个标的")
    p_browser.add_argument("--max-tabs", type=int, default=3, help="一个 episode 被拒后最多换到第几个 tab")
    p_browser.add_argument("--load-budget", type=int, default=150, help="整轮页面加载的硬上限")
    p_browser.add_argument("--pause", type=float, default=20.0, help="轮与轮之间歇几秒")
    p_browser.add_argument("--concurrency", type=int, default=3, help="同时开的 tab 数，与 BROWSER_MAX_PAGES 同量级")
    p_browser.add_argument("--memory-pages", type=int, default=3, help="内存阶梯最多同时几页；0 关闭")
    p_browser.add_argument("--symbols", default="", help="逗号分隔的六位代码，缺省 32 只大盘股")
    p_browser.add_argument("--force", action="store_true", help="盘中也跑（会和线上调用抢出口额度）")

    p_ths = sub.add_parser("tonghuashun", help="反复取指数 K 线，量当前环境到同花顺的耗时分布，定总预算")
    common(p_ths)
    p_ths.add_argument("--rounds", type=int, default=6, help="每个标的重复几轮")
    p_ths.add_argument("--symbols", default="", help="逗号分隔的带前缀代码，缺省四大指数")
    p_ths.add_argument("--window-days", type=int, default=THS_WINDOW_DAYS,
                       help="取数窗口的自然日数；跨几年就发几个请求，直接决定最坏耗时")
    p_ths.add_argument("--pause", type=float, default=1.0, help="每次之间歇几秒，别把对方打出限流")

    p_rec = sub.add_parser("recommend", help="按判据出 .env.recommended、report.md，并比对线上 .env")
    common(p_rec)
    p_rec.add_argument("--env", default=str(PROJECT_ROOT / ".env"), help="要比对的线上 .env 路径")
    p_rec.add_argument("--service-base-mib", type=float, default=None,
                       help="服务不含浏览器时的进程树 PSS（MiB）；这台机器上没跑线上服务时用它算页数")

    p_verify = sub.add_parser("verify", help="用候选配置以 ENV_PREFIX 起隔离实例，跑 verify_release")
    common(p_verify)
    p_verify.add_argument("--env-file", required=True)
    p_verify.add_argument("--port", type=int, default=8790)
    p_verify.add_argument("--only", default="brief,medium,full", help="传给 verify_release 的 --only；空串则全部工具")
    p_verify.add_argument("--timeout", type=float, default=60.0, help="等实例就绪的秒数")

    p_all = sub.add_parser("all", help="facts → browser → tonghuashun → recommend")
    common(p_all)
    for source in (p_facts, p_browser, p_ths, p_rec):
        for action in source._actions:
            if action.dest in ("help", "out_dir") or any(o in {a.option_strings[0] for a in p_all._actions if a.option_strings} for o in action.option_strings):
                continue
            p_all._add_action(action)
    return parser.parse_args(argv)


#: 这个脚本要用的、只装在项目虚拟环境里的包。名字 → 提示里显示的用途。
#: 全部按 `import x` 的写法写，别写 pip 包名（python-dotenv 的模块名是 dotenv）。
_VENV_ONLY_IMPORTS = {
    "dotenv": "读 .env",
    "requests": "可达性探测",
    "playwright": "浏览器探测",
}


def _require_venv() -> None:
    """缺依赖就在开头拦住，并告诉怎么跑。

    默认用法写的是 `python scripts/probe_tuning.py`，而系统 python3 里没有这几个包，
    于是会跑到第三层函数才 ModuleNotFoundError——那时候 `[facts] 机器…` 已经打出来了，
    看起来像"跑起来了又坏了"。开头拦住，错误信息里直接给能用的命令。
    """
    import importlib.util

    missing = [name for name in _VENV_ONLY_IMPORTS
               if importlib.util.find_spec(name) is None]
    if not missing:
        return
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    detail = "、".join(f"{n}（{_VENV_ONLY_IMPORTS[n]}）" for n in missing)
    hint = (f"{venv_python} {' '.join(sys.argv[1:] and ['scripts/probe_tuning.py'] + sys.argv[1:] or ['scripts/probe_tuning.py'])}"
            if venv_python.exists()
            else "先跑 ./install.sh 建虚拟环境")
    print(f"缺少依赖：{detail}\n"
          f"这个脚本要用项目虚拟环境里的包，系统 python3 里没有。请改成：\n"
          f"    {hint}", file=sys.stderr)
    raise SystemExit(2)


def main(argv=None) -> int:
    args = parse_args(argv)
    _require_venv()
    handlers = {"facts": run_facts, "browser": run_browser, "recommend": run_recommend,
                "tonghuashun": run_tonghuashun, "verify": run_verify, "all": run_all}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
