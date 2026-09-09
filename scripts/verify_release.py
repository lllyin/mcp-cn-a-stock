#!/usr/bin/env python3
"""上线后的数据验证层。一次运行、一份报告，两个部分各回答一个问题。

**实时探活**——维度在不在
    不钉日期，按三批固定标的把每个工具都调一遍，然后按维度契约逐项检查：该有的
    段落在不在、在了有没有值。回答的是"线上现在缺什么"。必须实时，因为资金流只有
    "今天"这一个口径，钉了日期它本就没有，那样就永远查不出它到底取不取得到。

**基线比对**——数字对不对
    用 verification/baseline/ 里的历史归档重放同样的调用，逐项比对。这一边一律把
    日期钉死（归档命令里没有 date= 的，就从归档报告自己的数据日期反推补上），
    所以已收盘的字段应当逐字相同——对不上就是真漂了，不是行情动了。

两部分不能互相替代：实时探活拿不到"以前是什么样"，基线比对拿不到"实时资金流现在
取不取得到"。所以默认两边都跑，出一份报告。

只读，不改任何服务状态。

用法：

    python scripts/verify_release.py                      # 两部分都跑（默认）
    python scripts/verify_release.py --skip-baseline      # 只看维度在不在
    python scripts/verify_release.py --skip-probe         # 只看数字对不对
    python scripts/verify_release.py --only brief,full    # 限定工具

退出码：0 全过；1 有失败/缺维/不一致；2 脚本自己跑不起来（找不到 mcporter 等）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: AGENTS.md 第四条：服务及其 Chromium、Xvfb 等子进程的合计峰值上限。
#: 和 scripts/loadtest_mcp.py 用同一个数，两边报告才可比。
MEMORY_BUDGET_MIB = 500.0
DEFAULT_BASELINE_DIR = PROJECT_ROOT / "verification" / "baseline"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "verification" / "reports"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "cn-stock-mcp.log"
#: 找 mcporter 配置的顺序：MCPORTER_CONFIG 环境变量 > --config > 下面这个惯例位置。
#: 不要在这里写死某台机器的绝对路径——以 root 跑时 ``Path.home()`` 就是 ``/root``，
#: 再列一条 ``/root/...`` 是冗余的。别的部署形态请用环境变量或 --config。
DEFAULT_CONFIG_CANDIDATES = (
    Path.home() / ".openclaw" / "workspace" / "config" / "mcporter.json",
)

ALL_TOOLS = (
    "brief",
    "medium",
    "full",
    "kline_daily",
    "kline_range",
    "tech",
    "market_breadth",
    "market_events",
)


# ── 一、维度契约 ────────────────────────────────────────────────
# 报告里"本该有什么"。这是完整性检查的全部依据，也是这个脚本最需要跟着业务改的
# 部分：渲染层加了一维就往这里加一行，否则少了也没人知道。
#
# source 是这一维背后的上游源，缺失时报告直接给出该去看哪一层，省一次翻代码。


# 维度契约、类别判定和降级提示语在包里，服务渲染完当场比对一次读的是同一张表
# （见 finmcp/report_contract.py）。两处各存一份的话，同一件事发版闸门报一个可用率、
# health 工具报另一个，两个数都没法用。


def _load_report_contract():
    """把那张表拿进来，但**不为它装齐整个服务**。

    ``import finmcp.report_contract`` 会先执行 ``finmcp/__init__.py``，那一句
    ``from .mcp_app import mcp_app`` 把 FastMCP、pandas、pydantic 全拉进来。而这个
    闸门自己只用标准库、取数靠 mcporter 子进程——要求装齐服务依赖才能跑，等于把
    "任何 python3 都能跑闸门"这条废掉；偏偏最需要它的时候（服务环境本身可疑）
    正是装不全的时候。

    所以：装得全就正常 import，和服务共用同一个模块对象；装不全就按文件路径只加载
    ``report_contract`` 这一个模块——它是纯标准库的，同一个文件，同一张表。
    """
    try:
        import finmcp.report_contract as module
        return module, "finmcp 包"
    except ImportError:
        path = PROJECT_ROOT / "finmcp" / "report_contract.py"
        spec = importlib.util.spec_from_file_location("finmcp_report_contract", path)
        module = importlib.util.module_from_spec(spec)
        # 先登记再执行：dataclasses 装饰类型字段时要 sys.modules[cls.__module__]，
        # 不登记会在 class Dimension 那一行抛 AttributeError: 'NoneType'。
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module, str(path)


_contract, CONTRACT_SOURCE = _load_report_contract()

ALL_CLASSES = _contract.ALL_CLASSES
CONTRACT = _contract.CONTRACT
DEGRADED_MARKERS = _contract.DEGRADED_MARKERS
Dimension = _contract.Dimension
ETF = _contract.ETF
INDEX = _contract.INDEX
STOCK = _contract.STOCK
STOCK_ONLY = _contract.STOCK_ONLY
classify = _contract.classify
is_index = _contract.is_index


def rate_text(value: float) -> str:
    """百分比文案。**绝不向上取整到 100%**。

    `:.0f` 会把 528/530 = 99.62% 印成 `100%`，于是表头声称满分、明细写着少了 2 项，
    同一行里自相矛盾——而这是发布前的闸门，读表头的人多半不会再去数明细。
    同理不把 0.4% 印成 `0%`：那会让"几乎全挂"看着像"全挂"，两者要采取的行动不同。

    所以一律向下取整到 0.1，只有真的等于 100 才写 100。少报一点是安全的方向：
    闸门宁可拦下一个本可以放行的版本，也不该放行一个看着满分的残缺版本。
    """
    if value >= 100.0:
        return "100"
    if value <= 0.0:
        return "0"
    floored = math.floor(value * 10) / 10.0
    if floored <= 0.0:                      # 0 < value < 0.1
        return "<0.1"
    return f"{floored:.0f}" if floored.is_integer() else f"{floored:.1f}"





# ── 二、调用 ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CallSpec:
    tool: str
    args: dict[str, str]
    label: str = ""

    def command(self, config: Path, timeout_ms: int) -> list[str]:
        argv = ["mcporter", "call", "cn-stock", self.tool]
        argv += [f"{key}={value}" for key, value in self.args.items()]
        argv += ["--config", str(config), "--output", "text", "--timeout", str(timeout_ms)]
        return argv

    def describe(self) -> str:
        rendered = " ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.tool} {rendered}".strip()


@dataclass
class CallResult:
    spec: CallSpec
    exit_code: int
    payload: str
    stderr: str
    elapsed: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and bool(self.payload.strip())


def run_call(spec: CallSpec, config: Path, timeout_ms: int) -> CallResult:
    started = dt.datetime.now()
    try:
        completed = subprocess.run(
            spec.command(config, timeout_ms),
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000 + 30,
        )
        code, out, err = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired:
        code, out, err = 124, "", "本地等待超时（比 --timeout 还长）"
    except FileNotFoundError:
        raise SystemExit("找不到 mcporter，先装上或把它放进 PATH")
    return CallResult(
        spec=spec,
        exit_code=code,
        payload=out,
        stderr=err.strip(),
        elapsed=(dt.datetime.now() - started).total_seconds(),
    )


def run_calls(
    specs: list[CallSpec], config: Path, timeout_ms: int, concurrency: int
):
    """逐个产出 ``(下标, 结果)``，**谁先完成先产出**。

    这里必须是生成器。原先是 ``list(pool.map(...))``，要等 22 个调用全部结束才返回，
    调用方一行进度都打不出来——默认每个调用 120 秒上限、并发 2，最坏情况是
    22/2 × 150s ≈ 27 分钟的纯静默。而"静默 27 分钟"和"卡死了"，从终端上看是一模一样的，
    没人会等到它自己出来。

    但产出顺序换成完成顺序之后，"第 n 个结果对应第 n 个 spec"就不成立了，而基线重放
    正是按位置把结果配回 baseline 的。配错的后果不是报错而是**静默失真**：拿 A 的基线
    去比 B 的新输出，每份文档都成了"整段新增 + 整段缺失"，一致率崩到 17%，却一条
    值变化都没有——看着像上游全挂了，实际只是配对错位。所以下标必须跟着结果一起走，
    由调用方按它排回去，不能靠约定。
    """
    if concurrency <= 1:
        for index, spec in enumerate(specs):
            yield index, run_call(spec, config, timeout_ms)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(run_call, spec, config, timeout_ms): index
            for index, spec in enumerate(specs)
        }
        for future in concurrent.futures.as_completed(futures):
            yield futures[future], future.result()


# ── 三、载荷解析 ────────────────────────────────────────────────
# 工具分两类：brief/medium/full/tech/market_* 返回 JSON，kline_* 直接返回
# markdown。下面统一拆成"若干篇有名字的文档"，比对和完整性检查都只认这个形状。


@dataclass
class Payload:
    documents: dict[str, str]                 # 文档名 -> 正文（完整性检查看这个）
    structures: dict[str, object] = field(default_factory=dict)  # 能解析成对象的留一份
    errors: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    broken_json: bool = False                 # 看着像 JSON 但解析不了

    @property
    def symbols(self) -> list[str]:
        return list(self.documents)


def parse_payload(text: str) -> Payload:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return Payload(documents={"（正文）": stripped})
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # 归档时被按大小截断过的文件会落在这里。当成文本比对只会得到满屏噪音，
        # 所以标出来，由调用方跳过。
        return Payload(documents={"（正文）": stripped}, broken_json=True)

    # 有些归档的载荷被包了一层 {"result": "..."}——mcporter 对返回标量的工具
    # （kline_daily / kline_range）在 json 输出模式下就是这个形状，而重放用的是
    # text 模式，拿到的是裸正文。不脱这层壳，两边一个是 JSON 一个是 markdown，
    # 会报成"整份缺失"。
    if set(data) == {"result"} and isinstance(data["result"], str):
        return Payload(documents={"（正文）": data["result"].strip()})

    documents: dict[str, str] = {}
    structures: dict[str, object] = {}
    reports = data.get("reports")
    if isinstance(reports, dict) and reports:
        for symbol, report in reports.items():
            if isinstance(report, str):
                documents[symbol] = report
            else:
                # tech 的 reports[symbol] 是对象，留着原样好做按路径的结构比对。
                documents[symbol] = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                structures[symbol] = report
    else:
        # market_breadth / market_events：整份就是一篇文档。
        documents["（整份）"] = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        structures["（整份）"] = data

    warnings = data.get("warnings") or []
    return Payload(
        documents=documents,
        structures=structures,
        errors=data.get("errors") or {},
        warnings=warnings if isinstance(warnings, list) else [warnings],
    )


# ── 四、完整性检查 ──────────────────────────────────────────────


@dataclass
class MissingDimension:
    symbol: str
    dimension: Dimension
    degraded_note: str = ""    # 有段落但内容是降级说明

    @property
    def verdict(self) -> str:
        if self.degraded_note:
            return f"⚠️ {self.degraded_note}"
        return "❌ 该有却没有"


@dataclass
class Completeness:
    """一次调用的维度账：应检多少、正当缺席多少、真缺多少。"""

    expected: int = 0
    findings: list[MissingDimension] = field(default_factory=list)

    @property
    def bad(self) -> list[MissingDimension]:
        return list(self.findings)

    @property
    def graded(self) -> int:
        return self.expected

    @property
    def available(self) -> int:
        return self.graded - len(self.bad)


def check_completeness(tool: str, payload: Payload) -> Completeness:
    """按维度契约检查一份返回。探活一律实时，所以这里没有"正当缺席"这回事。

    唯一的例外由 ``applies_to`` / ``applies_when`` 处理：ETF 没有财务报表和市盈率，
    指数连市值都没有，科创50 没有资金流向页面——那不是缺失，是这个标的本来就没有
    这一维。前者按类别判，后者按标的判。
    """
    dimensions = CONTRACT.get(tool)
    result = Completeness()
    if not dimensions:
        return result
    for symbol, document in payload.documents.items():
        for dimension in dimensions:
            if not dimension.applies(symbol):
                continue
            result.expected += 1
            if dimension.marker not in document:
                result.findings.append(MissingDimension(symbol, dimension))
                continue
            note = _degraded_note(document, dimension)
            if note:
                result.findings.append(MissingDimension(symbol, dimension, note))
    return result


def _degraded_note(document: str, dimension: Dimension) -> str:
    """段落在，但里面只有一句"没有数据"——这也算这一维没拿到。"""
    if not dimension.marker.startswith("##"):
        return ""
    _, _, tail = document.partition(dimension.marker)
    body = tail.split("\n#", 1)[0]
    for marker, explanation in DEGRADED_MARKERS.items():
        if marker in body:
            return explanation
    if not body.strip():
        return "段落为空"
    return ""


# ── 五、归档解析与重放 ──────────────────────────────────────────

# 只认「命令：」那一行。归档的一级标题也长得像调用（"# mcporter call cn-stock brief"），
# 但它不带参数，匹配上去会得到一个没有 symbol 的空调用。
_COMMAND_LINE = re.compile(
    r"^- 命令：.*?mcporter call cn-stock (\S+)(.*)$", re.MULTILINE
)
_ARG = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
_REPORT_DATE = re.compile(r"^- 数据日期:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_QUOTE_DATE = re.compile(r'"quote_date":\s*"(\d{4}-\d{2}-\d{2})"')
# 每次调用都会变、且与数据正确性无关的字段。比对前剔掉，否则每份都"有差异"。
_VOLATILE_PREFIXES = ('"timestamp"', '"fetched_at"', '"generated_at"', '"as_of"')


@dataclass
class Baseline:
    path: Path
    spec: CallSpec
    payload: Payload
    replay_spec: CallSpec | None      # 补好日期之后真正拿去重放的调用
    skip_reason: str = ""
    date_added: bool = False          # 日期是脚本补的，不是归档命令自带的
    intraday_capture: bool = False    # 归档是盘中抓的，当日那根 bar 还没定盘
    captured_on: str = ""             # 归档抓取那天，用来判断"此刻口径"能不能比

    @property
    def live_stale(self) -> bool:
        """归档不是今天抓的，那"此刻口径"的字段（市值、市盈率、换手率）没法比。

        抓取日不明时按"不能比"处理：宁可少报几行，也不要拿一堆必然不同的数字去
        冒充回归差异。
        """
        return self.captured_on != dt.date.today().isoformat()


def load_baseline(path: Path) -> Baseline | None:
    text = path.read_text(encoding="utf-8")
    match = _COMMAND_LINE.search(text)
    if match is None or "## 原始返回" not in text:
        return None
    tool = match.group(1)
    args = dict(_ARG.findall(match.group(2)))
    body = text.split("## 原始返回", 1)[1].strip()
    payload = parse_payload(body)
    spec = CallSpec(tool=tool, args=args, label=path.name)

    replay_spec, skip_reason = _pin_date(spec, payload)
    date_added = replay_spec is not None and "date" not in spec.args and "date" in replay_spec.args
    intraday = _captured_intraday(text, replay_spec)
    captured = _CAPTURED_AT.search(text)
    return Baseline(
        path, spec, payload, replay_spec, skip_reason, date_added, intraday,
        captured.group(1) if captured else "",
    )


_CAPTURED_AT = re.compile(r"^- 查询时间：(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})", re.MULTILINE)


def _captured_intraday(text: str, replay_spec: CallSpec | None) -> bool:
    """归档是不是在自己查的那一天的盘中抓的。

    是的话，归档里"当日"那一行是盘中快照，而钉日期的重放拿到的是收盘定盘值，
    两者本来就不相等，连带 5/20/60 日的均值、振幅、累计涨跌全都会跟着差一点。
    这种差异不是缺陷，但也证明不了什么，报告里要单独说清楚，别混进结论。
    """
    if replay_spec is None:
        return False
    captured = _CAPTURED_AT.search(text)
    if captured is None:
        return False
    day, hour, minute = captured.groups()
    if day != replay_spec.args.get("date"):
        return False
    return (int(hour), int(minute)) < (15, 0)


def _pin_date(spec: CallSpec, payload: Payload) -> tuple[CallSpec | None, str]:
    """把重放的日期钉死。钉不住的就不比，避免拿今天的行情去对昨天的账。"""
    if payload.broken_json:
        return None, "归档的 JSON 不完整（归档时被截断），比不了"
    if spec.tool in ("kline_daily", "kline_range", "market_events"):
        if any(key in spec.args for key in ("date", "end_date")):
            return spec, ""
        return None, "归档命令没有日期参数，重放结果会随行情变动"
    if spec.tool == "market_breadth":
        return None, "全市场实时快照，没有日期参数，天然不可重放"
    if "date" in spec.args:
        return spec, ""

    # 归档是当时的实时查询，但报告自己写着数据日期；用它把重放钉回同一天。
    dates = _REPORT_DATE.findall("\n".join(payload.documents.values()))
    dates += _QUOTE_DATE.findall("\n".join(payload.documents.values()))
    unique = sorted(set(dates))
    if len(unique) != 1:
        return None, f"归档里的数据日期不唯一（{unique or '无'}），钉不住"
    return CallSpec(spec.tool, {**spec.args, "date": unique[0]}, spec.label), ""


# ── 六、比对 ────────────────────────────────────────────────────


@dataclass
class LineDiff:
    kind: str          # 缺失 / 新增 / 值变化 / 实时口径 / 复权漂移 / 已知差异 / 缺数据
    key: str
    old: str = ""
    new: str = ""
    note: str = ""     # 已知差异的原因


@dataclass
class DocumentDiff:
    document: str
    diffs: list[LineDiff]
    drift_note: str = ""     # 整体等比漂移的判定结果

    @property
    def clean(self) -> bool:
        return not self.diffs

    #: 不算"回归漂移"的类别，各有各的理由，都在 _classify 和 _LIVE_VALUE_KEY 那里
    #: 写清楚了。它们仍然出现在报告里，只是不进回归一致率。
    NOT_DRIFT = ("实时口径", "复权漂移", "已知差异", "缺数据")

    @property
    def hard(self) -> list[LineDiff]:
        """真正算回归漂移的差异——钉死日期之后本该一样却变了的数字。"""
        return [diff for diff in self.diffs if diff.kind not in self.NOT_DRIFT]

    @property
    def gaps(self) -> list[LineDiff]:
        """整段没取到。算可用性，不算漂移。"""
        return [diff for diff in self.diffs if diff.kind == "缺数据"]

    @property
    def known(self) -> list[LineDiff]:
        return [diff for diff in self.diffs if diff.kind == "已知差异"]


def _line_key(line: str) -> str:
    """一行的身份。左边相同就算同一项，只是值变了。"""
    stripped = line.strip()
    head, sep, _ = stripped.partition(":")
    if sep and not stripped.startswith("|"):
        return head.strip()
    if stripped.startswith("|"):
        # 表格行用第一列当键，这样某一天的指标变了能定位到那一天。
        return stripped.split("|")[1].strip() if stripped.count("|") > 1 else stripped
    return stripped


def _line_value(line: str) -> str:
    stripped = line.strip()
    head, sep, tail = stripped.partition(":")
    return tail.strip() if sep and not stripped.startswith("|") else stripped


def _meaningful(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return not stripped.startswith(_VOLATILE_PREFIXES)


def _flatten(node, prefix: str = "") -> dict[str, str]:
    """把 JSON 摊平成 路径 -> 值。路径唯一，所以差异能落到具体那一项。

    结构化的返回（tech 的指标数组、market_events 的事件池）不能按行比：同一个
    "close" 会出现几十次，按行归并只会得到一条没法定位的"close 变了"。
    """
    if isinstance(node, dict):
        flat: dict[str, str] = {}
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(node, list):
        flat = {}
        for index, value in enumerate(node):
            flat.update(_flatten(value, f"{prefix}[{index}]"))
        return flat
    if isinstance(node, bool) or node is None:
        return {prefix: json.dumps(node, ensure_ascii=False)}
    if isinstance(node, (int, float)):
        # 10 和 10.0 是同一个数。json.dumps 会把它们写成不同的字符串，逐字比就
        # 会把上游一次 int/float 的表示变化报成几十处"值变化"。
        return {prefix: _canonical_number(float(node))}
    return {prefix: json.dumps(node, ensure_ascii=False)}


def _canonical_number(value: float) -> str:
    """数值的规范写法：整数去掉尾巴上的 .0，其余用最短往返表示。"""
    if value == int(value) and abs(value) < 2**53:
        return str(int(value))
    return repr(value)


#: 一个完整的数字 token 恰好是负零。前后都要求不是数字或小数点，免得从
#: ``-0.01`` 里切出一个 ``-0``。
_NEGATIVE_ZERO = re.compile(r"(?<![0-9.])-(0(?:\.0+)?)(?![0-9.])")


def _canonical_text(text: str) -> str:
    """把负零写成正零。

    两个资金流来源在"四舍五入之后是零"的值上符号不一致：主源给的是浮点数，
    -0.000038 格式化成两位小数就是 ``-0.00%``；页面兜底取的是页面已经渲染好的
    文本，同一个值是 ``0.00%``。数值上两者相等，报成"值变化"是假阳性。

    只等同数值完全相同的写法，所以不会开盲区——真的 ``-0.01`` 对 ``0.01``
    照旧会报出来。
    """
    return _NEGATIVE_ZERO.sub(r"\1", text)


def _volatile_path(path: str) -> bool:
    tail = path.rsplit(".", 1)[-1]
    return tail in ("timestamp", "fetched_at", "generated_at", "as_of")


_ARRAY_INDEX = re.compile(r"\[\d+\]")


def _collapse(diffs: list[LineDiff]) -> list[LineDiff]:
    """把 events[0..99].forecast_type 这一族合成一行。

    加一个字段，摊平之后就是几十上百条一模一样的差异。逐条列出来只会把别的差异
    挤出视野，而"哪个字段变了"才是要看的，出现几次不是。
    """
    grouped: dict[tuple[str, str], list[LineDiff]] = {}
    for diff in diffs:
        grouped.setdefault((diff.kind, _ARRAY_INDEX.sub("[]", diff.key)), []).append(diff)
    collapsed: list[LineDiff] = []
    for (kind, pattern), members in grouped.items():
        if len(members) == 1:
            collapsed.append(members[0])
            continue
        first = members[0]
        collapsed.append(
            LineDiff(kind, f"{pattern} ×{len(members)}", old=first.old, new=first.new)
        )
    return collapsed


def compare_structures(old, new, name: str) -> DocumentDiff:
    old_flat = {k: v for k, v in _flatten(old).items() if not _volatile_path(k)}
    new_flat = {k: v for k, v in _flatten(new).items() if not _volatile_path(k)}
    diffs: list[LineDiff] = []
    for path, value in old_flat.items():
        if path not in new_flat:
            diffs.append(LineDiff("缺失", path, old=value))
        elif new_flat[path] != value:
            diffs.append(LineDiff("值变化", path, old=value, new=new_flat[path]))
    for path, value in new_flat.items():
        if path not in old_flat:
            diffs.append(LineDiff("新增", path, new=value))
    # drift_note 要在合并之前算，合并会丢掉大部分样本点。
    note = _drift_note(diffs)
    collapsed = _collapse(diffs)
    _classify(name, collapsed)
    return DocumentDiff(name, collapsed, drift_note=note)


# ── 已知的上游差异 ─────────────────────────────────────────────
# 两个上游源对同一个字段给出不同的值，而这不是本项目能修的。逐条记下来：
# 匹配到的差异降级成"已知差异"，不计入回归一致率，但仍然在报告里列出来。
#
# 每条都必须带 ``bound``——已核实的最大相对偏差。超出这个界就重新算成真差异，
# 因为那说明性质变了，不再是当初核实过的那件事。默默无条件忽略一个字段，等于
# 在这一层上开了个永久的盲区。
#
# 加条目之前先把成因查清楚并写进 reason，别拿它当"消掉红字"的开关。


@dataclass(frozen=True)
class KnownDifference:
    key: str          # 匹配 LineDiff.key 的正则
    reason: str
    bound: float      # 允许的最大相对偏差；0 表示只允许一模一样的文本差异
    document: str = ".*"


@dataclass(frozen=True)
class PendingDisagreement:
    """一处已经发现、但还没裁决的跨源分歧。

    和 ``KNOWN_DIFFERENCES`` 分开是有意的：那份是"已核实、可以忽略"，这份是
    "看见了、还不知道谁对"。混在一起的话，前者会把后者盖掉——一处没查清的分歧
    被当成已知差异放行，就再也没人回头看它了。

    ``verdict`` 空着表示待裁决；裁决完要么搬进 KNOWN_DIFFERENCES（可忽略），
    要么改代码（择优），然后从这里删掉。
    """

    what: str          # 哪个标的的哪个字段
    values: str        # 各源分别是多少
    gap: str           # 相对差
    found: str         # 发现日期
    verdict: str = ""  # 裁决结论；空 = 待裁决


#: 跨源分歧登记处。发现一处记一处，别直接下判断——判据往往不在代码里，
#: 得拿券商行情或第三方去核对。
PENDING_DISAGREEMENTS = (
    PendingDisagreement(
        what="SZ399006 创业板指 · 成交量/成交额（全部周期）",
        values="东财/同花顺 200,462,510 手 · 5036.48亿；腾讯/新浪 193,413,042 手 · 4998.09亿",
        gap="量 3.52%，额 0.76%",
        found="2026-09-05",
        verdict="同花顺定案，东财对。指数改走 KLINE_PROVIDERS_INDEX（同花顺优先）",
    ),
    PendingDisagreement(
        what="SZ000333 美的 · MA60/MA120/MA240（前复权）",
        values="券商(同花顺+平安) 82.23/78.73/76.03；同花顺 82.229/78.734/76.026；"
               "腾讯/东财 82.242/78.780/76.091",
        gap="0.015% ~ 0.080%",
        found="2026-09-05",
        verdict="准的是同花顺，但量级可忽略；个股这一条按稳定性排，仍让腾讯优先",
    ),
    PendingDisagreement(
        what="板块资金流 · 行业板块是一棵树摊平的名单",
        values="东财 m:90+t:2 返回 496 个，同时含申万一级（传媒）、二级（证券Ⅱ）和"
               "三级（证券Ⅲ）。两个端点给的是同一批 496 个——**不是**源之间的分歧，"
               "是这份数据本身就是树。直接取 Top N 会父子同榜、同一笔钱数两遍："
               "电子 -817.95亿 里含子板块 半导体 -602.46亿；证券Ⅱ 与 证券Ⅲ 都是 32.35亿",
        gap="十行里没有十个独立板块",
        found="2026-09-05",
        verdict="层级进契约（SectorFlow.level），由申万行业分类补，排名只排一层。"
                "默认排二级——东财官网那张榜就是申万二级（3 页 50 行全是二级，"
                "一个一级都没有），拿官网三个口径的 HTML 逐位比过，当日/5日/10日"
                "各前 10 名的板块名和金额全部一致；补不到分级就如实标注",
    ),
)


KNOWN_DIFFERENCES = (
    KnownDifference(
        # kline 的文档名是"（正文）"，K线数据那个标题在 key 的段落部分里，所以
        # 只约束 key 不约束 document。
        key=r"日?K线数据.*› - 成交额",
        reason="腾讯的成交额精度到 100 元，新浪和东财给的是精确值"
        "（SH600362 2026-08-28：3,452,443,817 对 3,452,443,800）",
        bound=1e-6,
    ),
    KnownDifference(
        key=r"成交[量额]",
        document=r"^SZ399006$",
        reason="创业板指的成交量/成交额东财对、腾讯和新浪都偏低：量低 3.2%~4.4%，"
        "额低 0.5%~0.8%。2026-09-04 用同花顺定案——同花顺 5036.5亿 / 2亿手，"
        "东财 5036.48亿 / 200,462,500手，腾讯和新浪都是 4998.09亿 / 193,413,042手，"
        "两家一字不差（同源，互相校验不了）。上证/深证/科创50 三个指数两边 0.00% 相同，"
        "只有创业板指这一个标的有分歧。免费源里没有第二个给对数的，所以走兜底时"
        "这一维就是偏低的，改不了——差额部分隐含均价 5.45 元/股，而创业板指整体"
        "均价 25.84 元/股，东财多统计了一批低价品种",
        bound=0.05,
    ),
    KnownDifference(
        key=r"成交量",
        reason="同花顺的指数成交量给的是股（精度比手高两位），换成手再四舍五入到"
        "万手的两位小数时，可能比直接报手的东财高一档。实例：上证指数 2026-08-28"
        "当日 51058.16 对 51058.17，相对差 2e-7。只影响显示的最后一位，不影响任何"
        "计算——各周期均量用的是换算前的值",
        bound=1e-5,
    ),
    KnownDifference(
        key=r"(120|240)日均[量额]",
        reason="长窗口均值：窗口里某一天被上游修正过，摊到 120/240 个交易日上",
        bound=0.001,
    ),
)


def _known_reason(document: str, diff: LineDiff) -> str:
    """这处差异是不是已核实的上游差异。是就返回原因，不是返回空串。"""
    for entry in KNOWN_DIFFERENCES:
        if not re.search(entry.document, document) or not re.search(entry.key, diff.key):
            continue
        ratios = _ratios(diff)
        if not ratios:
            # 取不到数值对（文本变了或数字个数都不一样），只在 bound 为 0 时放行。
            return entry.reason if entry.bound == 0 else ""
        if max(abs(r - 1) for r in ratios) <= entry.bound:
            return entry.reason
    return ""


# 归档是当时的实时查询，重放是脚本钉了日期的——资金流那一段必然对不上，而且是
# 脚本自己造成的。不滤掉的话每个标的白白多出六七行"差异"，把真问题埋了。
_LIVE_ONLY_KEY = re.compile(r"净流入|标的名称|指定日期查询暂不展示实时资金流向")

# 这些维度的口径是"此刻"，不是报告里那个数据日期：市值 = 总股本 × 现价，换手率
# 的分母也是当下的流通股本，市盈率的分子是市值、分母的 TTM 窗口也在滚动。钉着
# 日期重放，它们照样跟着今天走，值不同是正常的。
#
# 判据是"归档抓的那天 ≠ 今天"，不是"脚本补了日期"。第一版搞错了这一点：归档命令
# 自带 date= 的那几份不触发降级，于是 08-21 抓的市值拿去和今天的比，刷出十几行
# 假差异。日期钉在哪一天不影响市值的口径——它永远是当下。
#
# "整段消失"仍然是问题，所以只降级值变化，缺失和新增照常报。
_LIVE_VALUE_KEY = re.compile(r"总市值|流通市值|市盈率|市净率|净资产收益率|换手")


@dataclass(frozen=True)
class RenamedItem:
    """一处**纯改名**：条目名换了，值一个字没变。

    差异器按「段落 › 条目名」做键，改个名字就变成"旧键缺失 + 新键新增"两条计分
    差异——而两边的数值完全相同。2026-09-06 就是这样：`含今日` 改成 `含当日`
    一个字，8 份基线 × 5 个周期 × 2 条 = 80 行差异，回归一致率从 99% 掉到 48%，
    没有一个数字变过。

    **只登记纯改名。** 口径变了不能进来——那是真漂移，正是这套比对要抓的东西。
    登记之后按新名字配对，比的才是数值本身；值真变了照样报出来。
    """

    old: str        # 旧条目名，正则
    new: str        # 新条目名，替换式
    reason: str
    since: str


#: 条目改名登记处。基线是历史账本，不为一次改名重采（重采会把当时的降级状态一起
#: 固化进去）；差异器认识改名，账本就还能继续用。
RENAMED_ITEMS = (
    RenamedItem(
        old=r"^(- \d+日总换手) \(含今日\)$",
        new=r"\1 (含当日)",
        reason="「今日」在非交易日是假话——周末查出来那一栏说的是上一个交易日。"
               "报告里其余各处一律用「当日」，这里跟着统一",
        since="2026-09-06",
    ),
    RenamedItem(
        old=r"^- 今日(主力|超大单|大单|中单|小单)净流入$",
        new=r"- 当日\1净流入",
        reason="同上。只换词不删前缀：删了行的形状就变了，下游按标签取数的得改结构",
        since="2026-09-06",
    ),
)


def _canonical_key(key: str) -> str:
    """把旧条目名换成现在的名字，好让改过名的两边配得上。"""
    for item in RENAMED_ITEMS:
        renamed = re.sub(item.old, item.new, key)
        if renamed != key:
            return renamed
    return key


def _index_by_section(lines: list[str]) -> dict[str, list[str]]:
    """行的身份要带上所属段落，否则同名行会被并成一条。

    `- 当日` 在 brief 里出现六次（价格、涨跌幅、振幅、成交量、成交额、换手率），
    只按行首归并的话，六行合成一行，报告里旧值新值都取第一个，看着一模一样却
    标着"有差异"。加上段落名之后每一项才落得到实处。
    """
    indexed: dict[str, list[str]] = {}
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("# ")
        item = _canonical_key(_line_key(line))
        key = f"{section} › {item}" if section else item
        indexed.setdefault(key, []).append(_line_value(line))
    return indexed


def compare_documents(
    old: str,
    new: str,
    name: str,
    *,
    drop_live_only: bool = False,
    demote_live_values: bool = False,
) -> DocumentDiff:
    def keep(line: str) -> bool:
        if not _meaningful(line):
            return False
        return not (drop_live_only and _LIVE_ONLY_KEY.search(line))

    old_lines = [line for line in old.splitlines() if keep(line)]
    new_lines = [line for line in new.splitlines() if keep(line)]
    # 比对用归一化后的文本，展示仍用原样：报告里要看到上游到底写了什么。
    if [_canonical_text(line) for line in old_lines] == [
        _canonical_text(line) for line in new_lines
    ]:
        return DocumentDiff(name, [])

    old_map = _index_by_section(old_lines)
    new_map = _index_by_section(new_lines)

    diffs: list[LineDiff] = []
    for key, values in old_map.items():
        if key not in new_map:
            diffs.append(LineDiff("缺失", key, old=values[0]))
        elif [_canonical_text(v) for v in new_map[key]] != [
            _canonical_text(v) for v in values
        ]:
            kind = "值变化"
            if demote_live_values and _LIVE_VALUE_KEY.search(key):
                kind = "实时口径"
            diffs.append(LineDiff(kind, key, old=values[0], new=new_map[key][0]))
    for key, values in new_map.items():
        if key not in old_map:
            diffs.append(LineDiff("新增", key, new=values[0]))

    diffs = _collapse_lost_sections(diffs)
    _classify(name, diffs)
    note = _drift_note(diffs)
    factor = _adjustment_drift(diffs)
    if factor is not None:
        note = (
            f"复权基准变化 ratio≈{factor:.4f}"
            f"（{_split_hint(factor)}）——价格与成交量那几行按这个比例整体缩放，"
            "已折叠；比例对不上已知公司行为的话才要查"
        )
        rounding = 0
        for diff in diffs:
            if diff.kind != "值变化":
                continue
            if _SCALES_WITH_ADJUSTMENT.search(diff.key):
                diff.kind = "复权漂移"
            elif _DERIVED_FROM_PRICE.search(diff.key) and _rounding_level(diff):
                diff.kind = "复权漂移"
                rounding += 1
        if rounding:
            note += f"；涨跌幅/振幅另有 {rounding} 行只差在末位，是价格缩放后的精度损失"
    return DocumentDiff(name, diffs, drift_note=note)


def _classify(document: str, diffs: list[LineDiff]) -> None:
    """给差异贴上不计入回归漂移的两个类别。就地改 kind。

    ``已知差异``  两个上游源对同一字段口径或精度不同，本项目改不了
    ``缺数据``    整段没取到。这是可用性问题，回归指标量的是"数字变没变"，
                  两件事混在一个分数里会互相矛盾——完整率说 100% 而回归说不一致。
    """
    for diff in diffs:
        if diff.kind in ("实时口径", "复权漂移"):
            continue
        if "整段不见了" in diff.key:
            diff.kind = "缺数据"
            continue
        reason = _known_reason(document, diff)
        if reason:
            diff.kind = "已知差异"
            diff.note = reason


def _collapse_lost_sections(diffs: list[LineDiff]) -> list[LineDiff]:
    """整段消失是一条事实，不是 N 行差异。

    full 的历史资金流向是一张 60 行的表：拿不到时逐行列出来就是 62 行"缺失"，
    把同一份报告里别的差异全挤出视野，而要知道的只是"这一段没了"。
    段落里只要有一行是新增或值变化，就不折叠——那说明段落还在，是内容变了。
    """
    by_section: dict[str, list[LineDiff]] = {}
    for diff in diffs:
        by_section.setdefault(diff.key.split(" › ")[0], []).append(diff)
    collapsed: list[LineDiff] = []
    for section, members in by_section.items():
        if len(members) >= 5 and all(d.kind == "缺失" for d in members):
            collapsed.append(
                LineDiff("缺失", f"{section} › 整段不见了（{len(members)} 行）",
                         old=members[0].old)
            )
        else:
            collapsed.extend(members)
    return collapsed


# 价格按复权因子缩放，成交量按它的倒数缩放（拆分之后股数变多），两类都跟着走。
_SCALES_WITH_ADJUSTMENT = re.compile(r"价格 ›|成交量")
# 这几类是价格算出来的比值，复权不改变它们——但价格缩小之后小数位数不够了，
# 算出来的百分比会在末位上抖一下。1:3 拆分把 3.241 变成 1.080 就是这么丢的精度。
_DERIVED_FROM_PRICE = re.compile(r"涨跌幅 ›|振幅 ›|换手")
# 末位抖动的判据：每一对数字都差不到 0.1 个百分点。放宽了会盖住真错误。
_ROUNDING_TOLERANCE = 0.1


def _rounding_level(diff: LineDiff) -> bool:
    old_numbers = _NUMBER.findall(diff.old.replace(",", ""))
    new_numbers = _NUMBER.findall(diff.new.replace(",", ""))
    if not old_numbers or len(old_numbers) != len(new_numbers):
        return False
    return all(
        abs(float(old_text) - float(new_text)) < _ROUNDING_TOLERANCE
        for old_text, new_text in zip(old_numbers, new_numbers)
    )


def _split_hint(factor: float) -> str:
    """0.5 就说 1:2，0.3333 就说 1:3，对不上整数比就只报比例。"""
    if factor <= 0:
        return "比例为负，不像复权"
    implied = 1 / factor
    nearest = round(implied)
    if nearest >= 2 and abs(implied - nearest) < 0.02:
        return f"约合 1:{nearest} 拆分"
    return "非整数比，更像分红除权"


_NUMBER = re.compile(r"-?\d+\.?\d*")
# 复权因子只作用在价格上：均价、最高、最低都按同一个比例缩放，而涨跌幅、振幅、
# 换手率是比值，除权前后不变。所以只在价格行上找这个比例，混进比值行会把方差
# 撑大到测不出来——第一版就是这么漏掉 512480 那次 1:2 拆分的。
_PRICE_KEY = re.compile(r"价格 ›")


def _ratios(diff: LineDiff) -> list[float]:
    old_numbers = _NUMBER.findall(diff.old.replace(",", ""))
    new_numbers = _NUMBER.findall(diff.new.replace(",", ""))
    if len(old_numbers) != len(new_numbers):
        return []
    found = []
    for old_text, new_text in zip(old_numbers, new_numbers):
        old_value, new_value = float(old_text), float(new_text)
        if abs(old_value) > 1e-9:
            found.append(new_value / old_value)
    return found


def _adjustment_drift(diffs: list[LineDiff]) -> float | None:
    """价格整段按同一个比例缩放了，说明前复权基准变了（拆分或分红）。

    这是外部公司行为，不是取数错了：1:2 拆分会让历史每一根 K 线的价格都减半，
    一份 brief 能刷出三十行"不一致"，真问题就藏不住了。返回那个比例。

    风险要写明：如果哪天真有个 bug 把价格整体缩放了，这里也会认成复权。所以报告
    里要把比例显式打出来，让人对得上公司行为，而不是默默折叠掉。
    """
    ratios = [
        value
        for diff in diffs
        if diff.kind == "值变化" and _PRICE_KEY.search(diff.key)
        for value in _ratios(diff)
    ]
    if len(ratios) < 6:
        return None
    average = sum(ratios) / len(ratios)
    if abs(average) < 1e-9 or abs(average - 1) < 1e-3:
        return None
    if (max(ratios) - min(ratios)) / abs(average) > 0.02:
        return None
    return average


def _drift_note(diffs: list[LineDiff]) -> str:
    """整份文档的数值同比例变化，比价格那一段更宽的一张网。"""
    ratios = [value for diff in diffs if diff.kind == "值变化" for value in _ratios(diff)]
    if len(ratios) < 6:
        return ""
    average = sum(ratios) / len(ratios)
    if abs(average) < 1e-9:
        return ""
    spread = (max(ratios) - min(ratios)) / abs(average)
    if spread < 0.01 and abs(average - 1) > 1e-4:
        return f"疑似整体等比漂移 ratio≈{average:.6f}（分红除权改了前复权基准是常见原因）"
    return ""


# ── 七、日志现状 ────────────────────────────────────────────────
# 报告里"当前用的什么数据源、哪些不可用"这一段的依据。全部来自服务自己打的日志，
# 不做推断：抓不到的就写抓不到。

_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (\w+) (.*)$")

LOG_SIGNALS: tuple[tuple[str, str, str], ...] = (
    (r"HTTP channel mode=(\S+) reason=(\S+)", "通道", "出站 HTTP 通道模式"),
    (r"HTTP channel suspending impersonation", "降级", "伪装通道被暂停，改走原生 requests"),
    (r"HTTP channel degraded to direct reason=(\S+)", "降级", "通道降级为直连"),
    (r"Source breaker opened source=(\S+)", "熔断", "上游源熔断打开"),
    (r"Source breaker closed source=(\S+)", "恢复", "上游源熔断关闭"),
    (r"incomplete_sources=(\S+)", "缺源", "报告不完整，未进缓存"),
    (r"Cache initialised ns=(\S+)", "缓存", "某个缓存命名空间起来了"),
    (r"(\S+) 用了 [\d.]+ 秒前的旧值", "旧值兜底",
     "上游取不到，用了缓存里的旧值——输出里会标注，但连续出现说明上游有问题"),
    # 没有网关的部署上这条会一直出现，是正常的兜底而不是故障：东财的 base_info
    # 需要网关，市值那一组由腾讯补。它消失了才说明网关回来了。
    (
        r"基本数据来源 (\S+)",
        "回退",
        "基本数据走了非东财的源（市值/市盈率由兜底源提供）",
    ),
    (r"(腾讯|新浪)历史行情 fallback 失败", "回退失败", "K 线兜底源失败"),
    # 判定本身没错，只是 ratio 贴到了合理带的边。拆分过的标的必然如此：前复权把
    # 收盘压低了几倍，而成交额没被压，ratio 就跟着掉到 1/拆分比。看到它先去对
    # 第四节里那个标的的复权基准变化比例，对得上就是同一件事。
    (
        r"历史行情成交量量级异常",
        "提示",
        "成交量单位 ratio 贴到合理带边缘；拆分/除权的标的必然如此，判定仍是对的",
    ),
    (r"历史行情字段不完整", "存疑", "兜底源缺字段"),
    (r"盘中行情来源 (\S+) 取数失败", "回退", "盘中行情来源失败，换下一个"),
    (r"盘中行情跨源不一致", "跨源不一致", "两个源对同一字段给出的值超出容差，见「跨源分歧待裁决」"),
    (r"资金流向页面兜底成功 (\S+) rows=(\d+)", "回退", "资金流走了浏览器页面兜底"),
    (r"资金流向页面兜底跳过", "跳过", "资金流兜底没跑，原因见样例（熔断/名额满/无此页面）"),
    (r"资金流向页面兜底失败", "回退失败", "浏览器兜底也没拿到"),
    (r"资金流向页面接口被拒", "风控", "东财资金流接口拒绝了请求"),
    (r"outcome=blocked_captcha", "风控", "页面弹出滑块验证"),
    (r"how=reload", "重试", "同一个 tab 刷新了一次"),
    (r"Failed symbol .*error=(.+)$", "失败", "单个标的取数失败"),
)


@dataclass
class LogScan:
    channel: str = "未知（日志里没有 HTTP channel mode= 这一行）"
    signals: dict[str, list[str]] = field(default_factory=dict)
    lines_scanned: int = 0
    available: bool = True
    note: str = ""
    diagnostics: dict = field(default_factory=dict)


def scan_log(path: Path, since: dt.datetime) -> LogScan:
    scan = LogScan()
    if not path.exists():
        scan.available = False
        scan.note = f"日志不存在：{path}"
        return scan

    # 通道模式是启动时打的，多半早于本次运行，所以全文找最后一条。
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = _LOG_LINE.match(line)
        if parsed is None:
            continue
        stamp_text, _level, message = parsed.groups()
        channel = re.search(r"HTTP channel mode=\S+ reason=\S+", message)
        if channel:
            scan.channel = channel.group(0)
        try:
            stamp = dt.datetime.strptime(stamp_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if stamp < since:
            continue
        scan.lines_scanned += 1
        for pattern, category, explanation in LOG_SIGNALS:
            if re.search(pattern, message):
                scan.signals.setdefault(f"{category}｜{explanation}", []).append(
                    message[:160]
                )
    scan.diagnostics = _scan_diagnostics(path, since)
    return scan


# ── 诊断：把每次排查都要手挖的东西一次收全 ──────────────────────
# 这一节刻意冗余。前两轮定位问题时，每个结论都要重新写一段脚本去扒日志：哪个源
# 吃掉了时间、通道什么时候降级、资金流的名额到底卡在哪、页面加载多久。既然日志里
# 本来就有，就在出报告的时候一次算完——多几行表格换下次不用再扒一遍。


def _scan_diagnostics(path: Path, since: dt.datetime) -> dict:
    """从服务日志里算出耗时分布、闸门计数和降级窗口。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = []
    for line in text.splitlines():
        parsed = _LOG_LINE.match(line)
        if parsed is None:
            continue
        try:
            stamp = dt.datetime.strptime(parsed.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if stamp >= since:
            lines.append((stamp, parsed.group(3)))

    out: dict = {"window": len(lines)}
    # 覆盖率要用的运行区间。起点用 since（脚本开跑的时刻）而不是第一条日志，
    # 否则一次开头就降级的运行会把自己那段算没。
    out["span"] = (since, max((t for t, _ in lines), default=since))

    # 每个上游源的 service/queue 分布。这是"谁吃掉了时间"的唯一直接答案。
    sources: dict[str, list[tuple[float, float]]] = {}
    for _, message in lines:
        m = re.search(
            r"Data task (\w+) .*?queue=([\d.]+)s service=([\d.]+)s", message
        )
        if m:
            sources.setdefault(m.group(1), []).append(
                (float(m.group(3)), float(m.group(2)))
            )
    out["sources"] = sources

    # 资金流兜底的完整账：主源失败 -> 兜底成功/跳过(按原因)/被拒。
    gates: dict[str, int] = {}
    for pattern, label in (
        (r"获取资金流向数据失败", "主源失败"),
        (r"资金流向页面兜底成功", "兜底成功"),
        (r"资金流向页面接口被拒", "兜底被拒"),
        (r"资金流向页面兜底失败", "兜底失败"),
        (r"资金流向页面兜底无历史数据", "兜底无历史"),
    ):
        n = sum(1 for _, m in lines if re.search(pattern, m))
        if n:
            gates[label] = n
    for _, message in lines:
        m = re.search(r"资金流向页面兜底跳过 \S+: (.+)", message)
        if m:
            # 把具体秒数抹掉再归并，否则每条都是一个独立的原因。
            reason = re.sub(r"[\d.]+s", "Ns", m.group(1))[:44]
            gates[f"兜底跳过：{reason}"] = gates.get(f"兜底跳过：{reason}", 0) + 1
    out["fund_flow_gates"] = gates

    # 页面加载：how / outcome / 耗时 / 等信号量。等信号量是并发上限的直接体感。
    loads = [
        (m.group(1), m.group(2), float(m.group(3)), float(m.group(4)))
        for _, message in lines
        if (m := re.search(
            # outcome 里有空格（today=True history=121），不能用 \S+ 截。
            r"how=(\w+) outcome=(.+?) semaphore_wait=([\d.]+)s service=([\d.]+)s",
            message,
        ))
    ]
    out["page_loads"] = loads

    # 通道降级与熔断：什么时候、开了多久、窗口里落了多少次取数。
    events = []
    for stamp, message in lines:
        if "suspending impersonation" in message:
            seconds = re.search(r"for ([\d.]+)s", message)
            hold = float(seconds.group(1)) if seconds else 0.0
            events.append((stamp, "通道暂停伪装", f"{hold:.0f}s", hold))
        elif "Source breaker opened" in message:
            src = re.search(r"source=(\S+)", message)
            cooldown = re.search(r"cooldown=([\d.]+)s", message)
            events.append((stamp, "熔断打开", src.group(1) if src else "",
                           float(cooldown.group(1)) if cooldown else 0.0))
        elif "Source breaker closed" in message:
            src = re.search(r"source=(\S+)", message)
            events.append((stamp, "熔断关闭", src.group(1) if src else "", 0.0))
    out["events"] = events
    return out


def _env_number(name: str, default: float) -> float:
    """从环境变量或 .env 读一个数。脚本独立跑，读不到就用默认值。"""
    raw = os.getenv(name)
    if raw is None:
        env_file = PROJECT_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(f"{name}="):
                    raw = stripped.split("=", 1)[1].strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _render_disagreements(scan: "LogScan") -> list[str]:
    """跨源分歧待裁决。

    两个来源：登记在 ``PENDING_DISAGREEMENTS`` 里的（人工发现、等裁决），以及本次
    运行日志里的跨源校验告警（``INTRADAY_QUOTE_CROSS_CHECK_PCT`` 打开时才有）。

    这一节**不进可用率**：一处分歧不代表数据缺了，它代表"有两个说法、还没定谁对"。
    把它算进分数会逼着人为了绿而草率裁决，那正好是反的。
    """
    # 从 .env 读而不是 import finmcp：这个脚本是独立跑的，不在包的搜索路径上；
    # 而且它验的是**服务**的配置，服务读的就是 .env。
    tolerance = _env_number("INTRADAY_QUOTE_CROSS_CHECK_PCT", 0.0)

    lines = ["## 跨源分歧待裁决", ""]
    lines.append("> 和「已知差异」分开：那份是已核实、可以忽略的；这份是看见了、"
                 "还不知道谁对。混在一起的话，一处没查清的分歧会被当成已知差异放行，"
                 "然后再没人回头看它。裁决完要么搬进 KNOWN_DIFFERENCES，要么改代码，"
                 "然后从登记处删掉。")
    lines.append("")

    pending = [d for d in PENDING_DISAGREEMENTS if not d.verdict]
    settled = [d for d in PENDING_DISAGREEMENTS if d.verdict]

    lines.append("| 状态 | 标的 · 字段 | 各源取值 | 相对差 | 发现 | 裁决 |")
    lines.append("| --- | --- | --- | ---: | --- | --- |")
    for item in pending + settled:
        mark = "⏳ 待裁决" if not item.verdict else "✅ 已裁决"
        lines.append(f"| {mark} | {item.what} | {item.values} | {item.gap} "
                     f"| {item.found} | {item.verdict or '—'} |")
    lines.append("")

    # 本次运行的实时跨源校验
    hits = []
    if scan.available:
        for sample in (scan.signals.get("跨源不一致") or []):
            hits.append(sample)
    if tolerance <= 0:
        lines.append("- 盘中行情的跨源校验**没开**（`INTRADAY_QUOTE_CROSS_CHECK_PCT=0`），"
                     "所以这一节没有本次运行的新发现——是没在看，不是没有分歧。"
                     "怀疑某个源口径不对时把它调成 1 再跑一轮。")
    elif hits:
        lines.append(f"- 本次运行发现 {len(hits)} 处实时行情跨源不一致：")
        for sample in hits[:5]:
            lines.append(f"  - `{sample[:160]}`")
    else:
        lines.append(f"- 本次运行开着跨源校验（容差 {tolerance}%），"
                     "没有超出容差的字段。")
    lines.append("")
    return lines


def _render_diagnostics(scan: "LogScan") -> list[str]:
    """诊断一节。全部来自服务日志，脚本不额外采集，所以开销为零。"""
    diag = getattr(scan, "diagnostics", None)
    lines = ["## 诊断：时间花在哪、闸门拦了什么", ""]
    if not scan.available or not diag:
        lines.append(f"没有可用的服务日志：{scan.note or '未知'}。")
        lines.append("")
        return lines
    lines.append("这一节是从服务日志里算出来的，不额外采集。定位问题时最先看它——"
                 "可用率说「缺没缺」，耗时说「慢不慢」，这里说「为什么」。")
    lines.append("")

    sources = diag.get("sources") or {}
    if sources:
        total = sum(sum(v for v, _ in rows) for rows in sources.values()) or 1.0
        lines.append("### 各上游源的耗时")
        lines.append("")
        lines.append("| 源 | 次数 | service P50 | P90 | 最慢 | 合计 | 占比 | queue 最长 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name in sorted(sources, key=lambda n: -sum(v for v, _ in sources[n])):
            rows = sources[name]
            svc = [v for v, _ in rows]
            queue = [q for _, q in rows]
            lines.append(
                f"| {name} | {len(rows)} | {_percentile(svc, 0.5):.2f}s | "
                f"{_percentile(svc, 0.9):.2f}s | {max(svc):.2f}s | {sum(svc):.1f}s | "
                f"{sum(svc)/total:.0%} | {max(queue):.2f}s |"
            )
        lines.append("")
        lines.append("> P50 和 P90 差一个数量级，就是「平时很快、坏起来很慢」的双峰——"
                     "该去看下面的降级窗口，而不是调这个源的超时。"
                     "queue 是等线程池的时间，它长说明并发上限被别的源占满了。")
        lines.append("")

    gates = diag.get("fund_flow_gates") or {}
    if gates:
        lines.append("### 资金流兜底的去向")
        lines.append("")
        lines.append("| 环节 | 次数 |")
        lines.append("| --- | ---: |")
        for label in sorted(gates, key=lambda k: -gates[k]):
            lines.append(f"| {label} | {gates[label]} |")
        got, failed = gates.get("兜底成功", 0), gates.get("主源失败", 0)
        if failed:
            lines.append(f"| **兜底救回率** | **{got}/{failed} = {got/failed:.0%}** |")
        lines.append("")
        lines.append("> 分母是「主源失败」的全部次数，不为任何原因打折。"
                     "「没有资金流向页面」是当前这条兜底源的属性，不是标的的属性——"
                     "2026-09-04 服务器开着网关采的 logs/s1_index.json 里，"
                     "SH000688 的今日主力净流入是 -42.67亿，换条源就有了。"
                     "要下判断先看跳过的原因：「名额已满」是容量不够，调并发；"
                     "「熔断器打开」是上游在拒，调并发没用；「没有资金流向页面」"
                     "是这条源覆盖不到，得换源或补一条。")
        lines.append("")

    loads = diag.get("page_loads") or []
    if loads:
        svc = [x[3] for x in loads]
        wait = [x[2] for x in loads]
        hows: dict[str, int] = {}
        outs: dict[str, int] = {}
        for how, outcome, _, _ in loads:
            hows[how] = hows.get(how, 0) + 1
            key = outcome.split("=")[0] if "=" in outcome else outcome
            outs[outcome if not outcome.startswith("today") else "today=…"] = (
                outs.get(outcome if not outcome.startswith("today") else "today=…", 0) + 1
            )
        lines.append("### 浏览器页面加载")
        lines.append("")
        lines.append(f"- 共 {len(loads)} 次；加载耗时 P50 {_percentile(svc, 0.5):.2f}s、"
                     f"P90 {_percentile(svc, 0.9):.2f}s、最慢 {max(svc):.2f}s")
        lines.append(f"- 等浏览器信号量 P50 {_percentile(wait, 0.5):.2f}s、"
                     f"最长 {max(wait):.2f}s")
        lines.append("- 方式：" + "，".join(f"{k} {v} 次" for k, v in sorted(hows.items())))
        lines.append("- 结果：" + "，".join(f"{k} {v} 次" for k, v in sorted(outs.items())))
        lines.append("")
        lines.append("> `reload` 出现说明第一次没拿到数据、同一个 tab 又刷了一次；"
                     "它为 0 表示上游健康，重试预算一分钱没花。"
                     "等信号量长而加载不慢，才是并发上限卡住了。")
        lines.append("")

    events = diag.get("events") or []
    if events:
        lines.append("### 降级与熔断事件")
        lines.append("")
        run_start, run_end = diag.get("span") or (None, None)
        run_seconds = (run_end - run_start).total_seconds() if run_start else 0.0
        lines.append("| 时刻 | 事件 | 详情 | 覆盖本次运行 |")
        lines.append("| --- | --- | --- | ---: |")
        for stamp, kind, detail, hold in events:
            share = "—"
            if hold and run_seconds > 0:
                closed = next(
                    (t for t, k, d, _ in events
                     if k == "熔断关闭" and d == detail and t > stamp),
                    None,
                )
                end = min(closed or (stamp + dt.timedelta(seconds=hold)), run_end)
                covered = max(0.0, (end - max(stamp, run_start)).total_seconds())
                share = f"{covered / run_seconds:.0%}（{covered:.0f}/{run_seconds:.0f}s）"
            lines.append(f"| {stamp:%H:%M:%S} | {kind} | {detail} | {share} |")
        lines.append("")
        lines.append("> 通道暂停伪装之后，对东财那几台主机的请求会退回原生 requests，"
                     "而它们恰恰拒绝原生 requests——所以这段窗口里东财是必败的。"
                     "**先看覆盖率再看第六节的耗时**：覆盖率高就说明那些数字量的是"
                     "降级路径，不是正常路径，不能拿去和别的版本比。"
                     "覆盖率按「事件时刻 + 时长」和运行区间取交集算，中途有对应的"
                     "「熔断关闭」就按实际关闭时刻截断。")
        lines.append("")
    return lines


# ── 八、探活套件 ────────────────────────────────────────────────


# 实时探活的标的。三批固定不变，因为它们和开着网关的服务器上采过的那三份
# （logs/s1_index.json、s2_cap.json、s3_edge.json）是同一批标的——标的对齐了，
# 这一层的输出才能直接和"东财路径的正确答案"逐字比，而不是只能自说自话。
# 换标的之前先想清楚拿什么当参照。
#
# 分批是照生产的形状来的：一次调用最多四个标的，而"四个标的抢一个兜底名额"这件事
# 只有成批发出来才看得见。
#
# 三批各自要验的东西：
#   指数    成交量单位推断整条绕过（指数的"收盘"是点位不是股价），资金流是大盘口径，
#           SH000688 没有资金流向页面
#   市值    全流通（茅台）/ A+H（平安，总市值 1.7 倍于流通）/ 有非流通（美的）/
#           科创板（中芯，4.3 倍），四种股本结构
#   边界    创业板（宁德）/ ETF（半导体，本该没有市盈率市净率）/ 超高市盈率
#           （中国卫星 985，分母接近 0 时口径最敏感）/ 高市净率（北方华创 11.42）
LIVE_BATCHES = (
    ("四大指数", "SH000001,SZ399001,SZ399006,SH000688"),
    ("市值口径", "SH600519,SH601318,SZ000333,SH688981"),
    ("边界情形", "SZ300750,SH512480,SH600118,SZ002371"),
)


def probe_suite(tools: set[str]) -> list[CallSpec]:
    """不钉日期的实时探活：每个工具都跑，标的按上面三批走。

    和钉日期那套的区别不只是少一个参数：

      - 资金流向本来就只有"今天"这一个口径，钉了日期就没有，所以钉日期那套把它
        算作正当缺席。实时调用下它**该有**，缺了就是真缺，见 ``live`` 参数怎么
        影响 DEGRADED_MARKERS 的判定。
      - 历史资金流向同理，只有 full 的实时调用才拿得到完整的那张表。

    所以这一套是回答"线上到底缺哪些维度"的那一套，钉日期那套回答的是"数字有没有
    漂"。两者都要跑，不能互相替代。
    """
    specs: list[CallSpec] = []
    for label, symbols in LIVE_BATCHES:
        specs.append(CallSpec("brief", {"symbol": symbols}, f"brief {label}"))
        specs.append(CallSpec("medium", {"symbol": symbols}, f"medium {label}"))
        specs.append(
            CallSpec("full", {"symbol": symbols, "fund_flow_limit": "60"}, f"full {label}")
        )
        specs.append(CallSpec("tech", {"symbol": symbols, "days": "30"}, f"tech {label}"))
    # kline 生产上只按单标的调用，各类各挑一个（都在上面三批里）
    today = dt.date.today().isoformat()
    for symbol in ("SH600519", "SH512480", "SH000001", "SZ002371"):
        # kline_daily 的 date 是必填的——这个工具本身就是按日寻址的，"实时"对它
        # 不成立，给今天就是它的实时口径。
        specs.append(
            CallSpec("kline_daily", {"symbol": symbol, "date": today},
                     f"kline_daily {symbol}")
        )
        specs.append(
            CallSpec(
                "kline_range",
                {"symbol": symbol, "start_date": _shift(today, -30), "end_date": today},
                f"kline_range {symbol}",
            )
        )
    specs.append(CallSpec("market_breadth", {}, "market_breadth"))
    specs.append(
        CallSpec("market_events", {"date": today, "sources": "lhb,limit_up"}, "market_events")
    )
    return [spec for spec in specs if spec.tool in tools]


def _shift(date: str, days: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def capture_baseline(result: CallResult, directory: Path, config: Path) -> Path:
    """把一次调用冻成归档，格式与 logs/mcporter 的一致，下次就能当基线用。

    这一层的价值是随时间累积的：今天确认没问题的输出，正是下一次改动要对的账。
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    tag = result.spec.args.get("symbol", "").replace(",", "_") or result.spec.tool
    path = directory / f"{stamp}_{result.spec.tool}_{tag}.md"
    rendered = " ".join(f"{k}={v}" for k, v in result.spec.args.items())
    path.write_text(
        "\n".join(
            [
                f"# mcporter call cn-stock {result.spec.tool}",
                "",
                f"- 查询时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                f"- 状态：{'SUCCESS' if result.ok else 'FAILED'}",
                f"- 工具：cn-stock {result.spec.tool}",
                f"- 标的：{result.spec.args.get('symbol', '')}",
                f"- 命令：export MCPORTER_CONFIG={config} && "
                f"mcporter call cn-stock {result.spec.tool} {rendered}".rstrip(),
                f"- 退出码：{result.exit_code}",
                "",
                "## 原始返回",
                "",
                result.payload.strip(),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


# ── 九、报告 ────────────────────────────────────────────────────


def _kind_summary(diffs: list[DocumentDiff]) -> str:
    """把不算漂移的那几类按类别数出来。

    原先一律标成"实时口径"，可 NOT_DRIFT 现在有四类，标签跟着就不准了——报告里
    写着"实时口径"而实际是"整段缺数据"，比不写更糟。
    """
    counts: dict[str, int] = {}
    for document in diffs:
        for diff in document.diffs:
            if diff.kind in DocumentDiff.NOT_DRIFT:
                counts[diff.kind] = counts.get(diff.kind, 0) + 1
    if not counts:
        return ""
    return "、".join(f"{n} 处{kind}" for kind, n in sorted(counts.items()))


def _focus(old: str, new: str, width: int = 78) -> tuple[str, str]:
    """两个长值只在中间某处不同时，截开头会截出两个一模一样的片段。

    真出现过：`- 当日` 那一行前 44 个字符完全相同，报告里两列长得一样，看的人只能
    去翻原文。所以从第一个不同的字符往前退一点开始截。
    """
    if len(old) <= width and len(new) <= width:
        return old, new
    limit = min(len(old), len(new))
    first = next((i for i in range(limit) if old[i] != new[i]), limit)
    start = max(0, first - 12)
    prefix = "…" if start else ""
    return (
        prefix + old[start : start + width],
        prefix + new[start : start + width],
    )


@dataclass
class Score:
    """三个可用率，外加一个综合分。定义写在报告里，免得只看到一个数字。"""

    tools_ok: int = 0
    tools_total: int = 0
    dims_ok: int = 0
    dims_total: int = 0
    # 分母是"参与比对的文档"而不是"基线文件"：kline_daily 我挑了 4 份单标的基线，
    # 它们的唯一差异是同一件上游精度差，按文件计分就占掉 4/13 = 31 个百分点，
    # 一个问题被算了四次。
    docs_ok: int = 0
    docs_total: int = 0
    gaps: int = 0          # 整段没取到的文档数，单独报，不进回归一致率
    known: int = 0         # 命中已核实上游差异的文档数

    @staticmethod
    def _pct(ok: int, total: int) -> float:
        return 100.0 if total == 0 else ok * 100.0 / total

    @property
    def tool_rate(self) -> float:
        return self._pct(self.tools_ok, self.tools_total)

    @property
    def dimension_rate(self) -> float:
        return self._pct(self.dims_ok, self.dims_total)

    @property
    def baseline_rate(self) -> float:
        return self._pct(self.docs_ok, self.docs_total)

    @property
    def overall(self) -> float:
        """取跑过的那几项里最低的那个。

        平均会把"一个源整层挂了"稀释成看着还行的 85 分。这层是发布前的闸门，
        闸门该按最短的那块板算。没跑的项不参与——0/0 算成 100% 再拿去取 min，
        等于让"没测"冒充"测过且通过"。
        """
        rates = [self.tool_rate, self.dimension_rate]
        if self.docs_total:
            rates.append(self.baseline_rate)
        return min(rates)

    @property
    def verdict(self) -> str:
        if self.overall < 90.0:
            return "❌ 不可发布"
        if self.overall < 99.0:
            return "⚠️ 有降级，确认原因后再发"
        if self.gaps:
            # 三个分数都满，但有文档整段没取到数据。不当成漂移（那是可用性），
            # 也不能给个干净的通过——先判断是偶发还是系统性。
            return "⚠️ 数字没漂，但有数据缺口，确认是偶发还是系统性再发"
        return "✅ 可发布"


# 四大指数走的是和个股不同的代码路径：成交量单位推断整条绕过（指数的"收盘"是
# 点位不是股价），资金流向来自大盘口径而不是个股口径，SH000688 更是压根没有资金
# 流向页面。以前的成交量小两个数量级就是栽在这儿，所以单独拎出来看。
CORE_INDICES = {
    "SH000001": "上证指数",
    "SZ399001": "深证成指",
    "SZ399006": "创业板指",
    "SH000688": "科创50",
}


def _render_matrix(
    probes: list[tuple[CallResult, Payload, Completeness]],
) -> list[str]:
    """维度 × 标的 的矩阵。

    缺失明细那张表是按"发现"排的，一个标的缺五维就是五行；要回答"线上到底缺什么"
    得反过来看：一行一维，一列一标的，空白处一眼就出来。

    同一个标的在 brief / medium / full 里都出现过，**只有每个工具都拿到了才算 ✅**。
    原先取的是"最好的那次"，理由是"只要有一个工具拿到，这一维就是取得到的"——听着
    成立，实际制造了自相矛盾的报告：2026-09-05 那份里缺失明细列了 8 项，矩阵却全绿，
    而可用率(按 工具×标的×维度 计)又确实扣了分。三处说法不一致，读的人只能挨个去
    核对。

    工具之间结果不一致时给一个独立符号 ◐，这样"这一维取得到"和"这一次没取到"两件事
    都还在，不用牺牲其中一件。
    """
    # (标的, 维度) -> 判定符号。工具之间不一致时降级成 ◐，不再取最好的那个。
    grid: dict[str, dict[str, str]] = {}
    dims: list[str] = []
    for result, payload, completeness in probes:
        contract = CONTRACT.get(result.spec.tool)
        if not contract or not result.ok:
            continue
        bad = {(m.symbol, m.dimension.name): m for m in completeness.findings}
        for symbol in payload.documents:
            row = grid.setdefault(symbol, {})
            for dimension in contract:
                if dimension.name not in dims:
                    dims.append(dimension.name)
                if not dimension.applies(symbol):
                    row.setdefault(dimension.name, "·")
                    continue
                item = bad.get((symbol, dimension.name))
                mark = "✅" if item is None else ("⚠️" if item.degraded_note else "❌")
                seen = row.get(dimension.name)
                if seen is None or seen == "·":
                    row[dimension.name] = mark
                elif seen != mark:
                    # 工具之间不一致：有的拿到了、有的没有。给 ◐，别让任何一边消失。
                    row[dimension.name] = "◐"
    if not grid:
        return []

    symbols = sorted(grid, key=lambda s: (classify(s), s))
    lines = ["## 三、维度 × 标的 矩阵", ""]
    lines.append(
        "✅ 每个工具都拿到了　◐ 有的工具拿到、有的没有（见缺失明细）　"
        "⚠️ 段落在但没值　❌ 该有却没有　· 这类标的本来就没有这一维"
    )
    lines.append("")
    lines.append("| 维度 | 上游源 | " + " | ".join(symbols) + " |")
    lines.append("| --- | --- | " + " | ".join("---" for _ in symbols) + " |")
    sources = {d.name: d.source for group in CONTRACT.values() for d in group}
    for dimension in dims:
        cells = [grid[s].get(dimension, "·") for s in symbols]
        if all(c == "·" for c in cells):
            continue
        lines.append(
            f"| {dimension} | {sources.get(dimension, '-')} | " + " | ".join(cells) + " |"
        )
    lines.append("")
    return lines


def _index_flow_verdict(tool: str, symbol: str, document: str) -> str:
    """指数专项里"资金流向"那一列的判定。

    三个坑都踩过：

    一、按工具判。``tech`` 的契约里根本没有资金流向这一维,拿同一套逻辑去判它,
    结果是三个指数都被标成"❌ 无"——它们不是缺,是这个工具本来就不产出这一维。

    二、别用全文子串。原先判的是 ``"净流入" in document``,而 ``full`` 的历史
    资金流向**表头**里就有"净流入"三个字,于是 SH000688 在 full 里被判成"✅ 有"、
    在 brief/medium 里被判成"✅ 没有这个页面"——同一个标的同一件事两种说法。
    改成认段落标记 + 降级文案,和 check_completeness 用同一套判据,不会再分叉。

    三、"没有这一维"和"有这一维但没取到"要分开。前者是 ``—``,后者才是 ❌。
    """
    flow = next(
        (d for d in CONTRACT.get(tool, ()) if d.name == "资金流向"), None
    )
    if flow is None:
        return "—"                      # 这个工具不产出资金流向
    if not flow.applies(symbol):
        return "—"                      # 这一类标的没有这一维
    if flow.marker not in document:
        return "❌ 段落都不在"
    # 只看这一段的正文，不看全文——上面第二条踩的就是这个坑。
    body = document.partition(flow.marker)[2].split("\n#", 1)[0]
    if "指定日期查询暂不展示实时资金流向" in body:
        # 钉了日期本就不展示实时资金流，不是缺。探活不该钉日期，钉了是调用参数
        # 的问题，那由维度矩阵去报，这一列只如实说明为什么这里没有数值。
        return "✅ 钉日期不展示"
    note = _degraded_note(document, flow)
    return "✅ 有" if not note else f"❌ {note}"


def _render_index_section(
    probes: list[tuple[CallResult, Payload, Completeness]],
    regressions: list[tuple[Baseline, CallResult | None, list[DocumentDiff]]],
) -> list[str]:
    lines = ["## 四、四大指数专项", ""]
    lines.append(
        "指数不走个股那条路：成交量单位推断整条绕过（指数的「收盘」是点位不是股价），"
        "资金流向是大盘口径，SH000688 没有资金流向页面。这几条都出过问题，单列。"
    )
    lines.append("")

    rows: list[str] = []
    for result, payload, completeness in probes:
        for symbol, document in payload.documents.items():
            if symbol not in CORE_INDICES:
                continue
            bad = [item for item in completeness.bad if item.symbol == symbol]
            volume = _extract(document, "## 成交量(万手)", "- 当日")
            amount = _extract(document, "## 成交额(亿)", "- 当日")
            close = _extract(document, "## 价格", "- 当日").split(" ")[0]
            flow = _index_flow_verdict(result.spec.tool, symbol, document)
            # tech 没有维度契约，`bad` 必然是空的——那不是"全都有"，是"一项都没查"。
            # 打 ✅ 会让人以为查过了。
            mark = (
                "—" if not CONTRACT.get(result.spec.tool)
                else "✅" if not bad
                else "❌ " + "、".join(sorted({item.dimension.name for item in bad}))
            )
            rows.append(
                f"| {symbol} {CORE_INDICES[symbol]} | {result.spec.tool} | {close} | "
                f"{volume} | {amount} | {flow} | {mark} |"
            )
    if rows:
        lines.append("| 指数 | 工具 | 收盘点位 | 成交量(万手) | 成交额(亿) | 资金流向 | 维度 |")
        lines.append("| --- | --- | ---: | ---: | ---: | --- | --- |")
        lines.extend(rows)
        lines.append("")
        lines.append(
            "> 成交量的量级自己就是一道校验：上证和深证的单日成交量在万手口径下是"
            "五位数（四五万手量级），创业板指是一万多，科创50 是几百到一千多。"
            "整体掉两个数量级，就是成交量单位推断又把指数当成个股除了 100——"
            "这个 bug 出过一次，11 个指数全中。"
        )
    else:
        lines.append("- 本次没有探到四大指数（用了 `--only` 限定工具？）。")
    lines.append("")

    index_diffs = [
        (baseline, diff)
        for baseline, _, diffs in regressions
        for diff in diffs
        if diff.document in CORE_INDICES
    ]
    if index_diffs:
        lines.append("### 指数与旧数据的差异")
        lines.append("")
        lines.append("| 指数 | 基线 | 差异 |")
        lines.append("| --- | --- | --- |")
        for baseline, diff in index_diffs:
            summary = "✅ 一致" if not diff.hard else f"❌ {len(diff.hard)} 处"
            lines.append(
                f"| {diff.document} {CORE_INDICES[diff.document]} | "
                f"{baseline.path.name[:34]} | {summary} |"
            )
        lines.append("")
    return lines


def _extract(document: str, section: str, prefix: str) -> str:
    """从报告里挖出某个段落下的某一行的值。挖不到就返回占位，别抛。"""
    _, _, tail = document.partition(section)
    for line in tail.split("\n#", 1)[0].splitlines():
        if line.startswith(prefix):
            return line.partition(":")[2].strip() or "—"
    return "—"


def render_report(
    started: dt.datetime,
    config: Path,
    probes: list[tuple[CallResult, Payload, Completeness]],
    regressions: list[tuple[Baseline, CallResult | None, list[DocumentDiff]]],
    scan: LogScan,
    watch: "MemoryWatch | None" = None,
) -> tuple[str, bool, str]:
    lines: list[str] = []

    probe_failures = [result for result, _, _ in probes if not result.ok]
    bad_missing = [item for _, _, c in probes for item in c.bad]
    dirty = [
        baseline
        for baseline, _, diffs in regressions
        if any(diff.hard for diff in diffs)
    ]
    replay_failures = [
        baseline for baseline, result, _ in regressions if result is not None and not result.ok
    ]
    comparable = [b for b, result, _ in regressions if result is not None]
    all_docs = [d for _, result, diffs in regressions if result is not None for d in diffs]
    docs_dirty = [d for d in all_docs if d.hard]
    docs_gap = [d for d in all_docs if d.gaps]
    docs_known = [d for d in all_docs if d.known]
    failed = bool(probe_failures or bad_missing or docs_dirty or replay_failures)

    score = Score(
        tools_ok=len(probes) - len(probe_failures),
        tools_total=len(probes),
        dims_ok=sum(c.available for _, _, c in probes),
        dims_total=sum(c.graded for _, _, c in probes),
        # 重放整份失败的，它那些文档一份都没进 all_docs，所以要额外扣掉
        docs_ok=len(all_docs) - len(docs_dirty),
        docs_total=len(all_docs) + len(replay_failures),
        gaps=len(docs_gap),
        known=len(docs_known),
    )

    lines.append("# 上线数据验证报告")
    lines.append("")
    lines.append(f"## 结论：可用率 **{rate_text(score.overall)}%**　{score.verdict}")
    lines.append("")
    lines.append("| 指标 | 分数 | 明细 |")
    lines.append("| --- | ---: | --- |")
    lines.append(
        f"| 工具可用率 | {rate_text(score.tool_rate)}% | "
        f"{score.tools_ok}/{score.tools_total} 个调用拿到了返回 |"
    )
    lines.append(
        f"| 维度完整率 | {rate_text(score.dimension_rate)}% | "
        f"{score.dims_ok}/{score.dims_total} 项该有的数据真的有"
        "（实时探活，见第三节矩阵）|"
    )
    if score.docs_total:
        lines.append(
            f"| 回归一致率 | {rate_text(score.baseline_rate)}% | "
            f"{score.docs_ok}/{score.docs_total} 份基线文档重放后没有未解释的漂移"
            + (f"；{score.known} 份命中已核实的上游差异，不计分" if score.known else "")
            + " |"
        )
    else:
        # 没跑就写没跑。0/0 算成 100% 再摆出来，比不摆更容易误导。
        lines.append(
            "| 回归一致率 | 未跑 | 这一轮没做基线比对"
            "（`--live` 的实时输出没有可比的旧数据，或用了 `--skip-baseline`）|"
        )
    if score.gaps:
        lines.append(
            f"| 数据缺口 | —— | {score.gaps} 份文档整段数据没取到，"
            "单列不计入回归分（那是可用性问题，不是数字漂了） |"
        )
    lines.append("")
    lines.append(
        "> 综合分取三项里最低的那个：平均会把「某个源整层挂了」稀释成看着还行的分数，"
        "而这层是发布前的闸门，闸门按最短的那块板算。"
        "≥99% 可发布，≥90% 要确认原因，低于 90% 不发。"
    )
    lines.append("")
    if score.docs_total:
        lines.append(
            "> 回归一致率的分母是**文档**而不是基线文件。kline_daily 那 4 份单标的基线"
            "只差在同一件上游精度上，按文件计分会让一个问题占掉 4/13；按文档算它仍然是"
            "四份，但和别的工具的四十多份文档放在一起，权重才对得上它的实际影响。"
        )
    lines.append("")
    if failed:
        lines.append("**主要问题**")
        lines.append("")
        by_source: dict[str, int] = {}
        for item in bad_missing:
            by_source[item.dimension.source] = by_source.get(item.dimension.source, 0) + 1
        for source, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
            names = sorted({item.dimension.name for item in bad_missing
                            if item.dimension.source == source})
            lines.append(f"- ❌ `{source}` 源没取到 {count} 项：{'、'.join(names)}")
        for result in probe_failures:
            lines.append(f"- ❌ `{result.spec.describe()[:60]}` 调用失败")
        if docs_dirty:
            lines.append(
                f"- ❌ {len(docs_dirty)} 份基线文档有未解释的漂移，见第四节"
            )
        if docs_gap:
            lines.append(
                f"- ⚠️ {len(docs_gap)} 份基线文档整段数据没取到（可用性，不是漂移）"
            )
        if replay_failures:
            lines.append(f"- ❌ {len(replay_failures)} 份基线重放失败")
        lines.append("")
    lines.append(
        "两部分各回答一个问题，不能互相替代：**实时探活**（第二、三、四节）看维度"
        "在不在，**基线比对**（第五节）看数字对不对。前者拿不到「以前是什么样」，"
        "后者拿不到「实时资金流现在取不取得到」。"
    )
    lines.append("")
    lines.append(f"- 运行时间：{started:%Y-%m-%d %H:%M:%S}")
    lines.append(f"- mcporter 配置：`{config}`")
    lines.append("")

    lines.append("## 一、当前数据源现状")
    lines.append("")
    if not scan.available:
        lines.append(f"- {scan.note}，这一节无从判断。")
    else:
        lines.append(f"- 出站通道：`{scan.channel}`")
        lines.append(f"- 本次运行窗口内的服务日志：{scan.lines_scanned} 行")
        if scan.signals:
            lines.append("")
            lines.append("| 类别 | 说明 | 次数 | 样例 |")
            lines.append("| --- | --- | ---: | --- |")
            for label, samples in sorted(scan.signals.items()):
                category, _, explanation = label.partition("｜")
                sample = samples[0].replace("|", "\\|")
                lines.append(
                    f"| {category} | {explanation} | {len(samples)} | `{sample[:110]}` |"
                )
        else:
            lines.append("- 窗口内没有任何回退、熔断或风控信号，全部走的主源。")
    lines.append("")

    lines.append("## 二、实时探活：维度在不在")
    lines.append("")
    lines.append(
        "不钉日期，按三批固定标的把每个工具都调一遍。必须实时——资金流只有「今天」"
        "这一个口径，钉了日期它本就没有，那样就永远查不出它到底取不取得到。"
    )
    lines.append("")
    lines.append("| 工具 | 调用 | 结果 | 耗时 | 维度完整率 | 缺失维度 |")
    lines.append("| --- | --- | --- | ---: | ---: | --- |")
    for result, payload, completeness in probes:
        status = "✅ OK" if result.ok else f"❌ 失败(exit={result.exit_code})"
        if result.ok and payload.errors:
            status = f"⚠️ 部分失败({len(payload.errors)} 个标的)"
        rate = (
            "—"
            if completeness.graded == 0
            else f"{rate_text(completeness.available * 100 / completeness.graded)}%"
        )
        names = sorted({item.dimension.name for item in completeness.bad})
        summary = "、".join(names) if names else "—"
        lines.append(
            f"| {result.spec.tool} | {result.spec.label or result.spec.describe()} | "
            f"{status} | {result.elapsed:.1f}s | {rate} | {summary} |"
        )
    lines.append("")

    detail = [(result, c) for result, _, c in probes if c.findings]
    if detail:
        lines.append("### 缺失明细")
        lines.append("")
        lines.append("| 工具 | 标的 | 维度 | 上游源 | 判定 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for result, completeness in detail:
            for item in completeness.findings:
                lines.append(
                    f"| {result.spec.tool} | {item.symbol} | {item.dimension.name} | "
                    f"{item.dimension.source} | {item.verdict} |"
                )
        lines.append("")

    for result, payload, _ in probes:
        if payload.errors or payload.warnings:
            lines.append(f"- `{result.spec.describe()}` 返回了 "
                         f"errors={json.dumps(payload.errors, ensure_ascii=False)[:200]} "
                         f"warnings={json.dumps(payload.warnings, ensure_ascii=False)[:200]}")
    if any(not result.ok for result, _, _ in probes):
        lines.append("")
        for result, _, _ in probes:
            if not result.ok:
                lines.append(f"- `{result.spec.describe()}` 失败：{result.stderr[:300] or '无 stderr'}")
    lines.append("")

    lines.extend(_render_matrix(probes))
    lines.extend(_render_index_section(probes, regressions))

    lines.append("## 五、基线比对：数字对不对")
    lines.append("")
    lines.append(
        "用 `verification/baseline/` 里的历史归档重放同样的调用。这一边一律把日期"
        "钉死，所以已收盘的字段应当逐字相同——对不上就是真漂了，不是行情动了。"
    )
    lines.append("")
    if not regressions:
        lines.append("- 没有可用基线（`verification/baseline/` 为空或全部跳过）。")
    else:
        lines.append("| 基线 | 工具 | 重放 | 结果 |")
        lines.append("| --- | --- | --- | --- |")
        for baseline, result, diffs in regressions:
            replay = "—"
            if baseline.replay_spec is not None:
                rendered = baseline.replay_spec.describe()
                # market_events 的 symbols= 能有五十个标的，整条塞进表格就没法看了。
                replay = rendered if len(rendered) <= 90 else rendered[:88] + "…"
            if result is None:
                verdict = f"跳过：{baseline.skip_reason}"
            elif not result.ok:
                verdict = f"**重放失败** exit={result.exit_code}"
            else:
                hard = sum(len(diff.hard) for diff in diffs)
                summary = _kind_summary(diffs)
                tail = f"，另有 {summary}（均不计分）" if summary else ""
                if not hard:
                    verdict = f"一致{tail}"
                elif baseline.intraday_capture:
                    verdict = (
                        f"**{hard} 处不同**（归档是盘中快照，当日 bar 未定盘，"
                        f"数值差异不可比）{tail}"
                    )
                else:
                    verdict = f"**{hard} 处不同**{tail}"
            lines.append(f"| {baseline.path.name} | {baseline.spec.tool} | `{replay}` | {verdict} |")
        lines.append("")

        for baseline, result, diffs in regressions:
            # 只列真有差异的文档。折叠掉的那些（实时口径、复权漂移）在上面的汇总
            # 行里已经报过次数，这里再列一遍只会配出一张空表。
            bad = [diff for diff in diffs if diff.hard]
            if not bad:
                continue
            lines.append(f"### {baseline.path.name}")
            lines.append("")
            for document in bad:
                summary = _kind_summary([document])
                lines.append(
                    f"**{document.document}**：{len(document.hard)} 处"
                    + (f"（另有 {summary}，已折叠）" if summary else "")
                )
                if document.drift_note:
                    lines.append(f"- {document.drift_note}")
                lines.append("")
                lines.append("| 类型 | 项 | 旧 | 新 |")
                lines.append("| --- | --- | --- | --- |")
                for diff in document.hard[:25]:
                    old_text, new_text = _focus(diff.old, diff.new)
                    lines.append(
                        f"| {diff.kind} | {diff.key[:48]} | `{old_text}` | `{new_text}` |"
                    )
                if len(document.hard) > 25:
                    lines.append(f"| … | 另有 {len(document.hard) - 25} 处 | | |")
                lines.append("")
    lines.append("")

    if watch is not None:
        all_calls = [result for result, _, _ in probes]
        all_calls += [r for _, r, _ in regressions if r is not None]
        section = _render_performance(watch, all_calls)
        # 编号跟着走：性能第六、分歧第七、诊断第八、说明第九。
        section[0] = "## 六、性能：耗时与内存"
        lines.extend(section)

    disagreements = _render_disagreements(scan)
    disagreements[0] = f"## {'七' if watch is not None else '五'}、跨源分歧待裁决"
    lines.extend(disagreements)

    diagnostics = _render_diagnostics(scan)
    diagnostics[0] = f"## {'八' if watch is not None else '六'}、诊断：时间花在哪、闸门拦了什么"
    lines.extend(diagnostics)

    lines.append("## 九、怎么看这份报告" if watch is not None else "## 七、怎么看这份报告")
    lines.append("")
    lines.append("**一份报告，两个部分**")
    lines.append("")
    lines.append("| 部分 | 验什么 | 怎么验 | 看哪里 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append("| 实时探活 | 维度在不在 | 不钉日期，三批十二个标的过一遍全部工具，"
                 "按维度契约逐项检查 | 第二、三、四节；分数是「维度完整率」|")
    lines.append("| 基线比对 | 数字对不对 | 钉死日期重放历史归档，逐项比对 | "
                 "第五节；分数是「回归一致率」|")
    lines.append("")
    lines.append("**判定符号**")
    lines.append("")
    lines.append("| 符号 | 含义 |")
    lines.append("| --- | --- |")
    lines.append("| ✅ | 正常，或正当缺席（有明确理由，不扣分） |")
    lines.append("| ⚠️ | 降级：段落在但没有值，多半是某个源没取到 |")
    lines.append("| ❌ | 该有却没有，或与旧数据真的对不上 |")
    lines.append("")
    lines.append("**差异分四类**")
    lines.append("")
    lines.append("| 类型 | 含义 | 要不要查 |")
    lines.append("| --- | --- | --- |")
    lines.append("| 值变化 | 钉死日期重放，已收盘的数字却变了 | **要**。这是这层的主要产出 |")
    lines.append("| 缺失 / 新增 | 渲染层加减了字段 | 加字段一般是预期内的；减字段要确认是有意的 |")
    lines.append(
        "| 实时口径 | 市值、市盈率、市净率、换手率——口径是「此刻」而不是那个数据日期，"
        "钉日期也会跟着今天走 | 不用。但整段消失仍按缺失算 |"
    )
    lines.append(
        "| 复权漂移 | 拆分或分红改了前复权基准，价格和成交量按同一比例整体缩放 | "
        "只核对一件事：比例对不对得上那只标的的已知公司行为 |"
    )
    lines.append("")
    lines.append("**几个术语**")
    lines.append("")
    lines.append("- **钉日期**：重放时给调用加上 `date=`，问的是那一天的收盘数据，"
                 "而不是此刻的实时行情。归档命令自带 `date=` 就直接用，没有的话从归档"
                 "报告里的「数据日期」反推补上——不钉住就等于拿今天的行情去对昨天的账。")
    lines.append("- **本来就没有这一维**：矩阵里的 `·`。ETF 没有财务报表和市盈率，"
                 "指数连市值都没有——那不是缺失，不进可用率的分母。判据在维度契约的"
                 "`applies_to` 上。")
    lines.append("- **已知差异**：两个上游源对同一字段口径或精度不同，本项目改不了，"
                 "逐条记在脚本的 `KNOWN_DIFFERENCES` 里。每条都带一个已核实的最大"
                 "相对偏差，超出就重新算成真差异——无条件忽略一个字段等于在这一层"
                 "开个永久盲区。")
    lines.append("- **数据缺口**：整段没取到。算可用性，不算漂移，所以单列一行而不是"
                 "进回归分——回归量的是「数字变没变」，两件事混在一个分数里会互相矛盾："
                 "完整率说 100% 而回归说不一致。")
    lines.append("")
    lines.append("- 基线过期了就从 `logs/mcporter/` 拷收盘后的新归档进 "
                 "`verification/baseline/`，文件名不限，脚本按文件里的「命令：」那一行"
                 "反解调用；选基线的规则见该目录下的 README。")
    lines.append("")
    # 第三个返回值给控制台用，和报告标题同一句式：先分数、再定性。
    # 控制台常常是唯一被看到的输出，只给"有降级"而不给分数，等于要人去翻文件。
    return "\n".join(lines), failed, f"可用率 {rate_text(score.overall)}%　{score.verdict}"


# ── 十、入口 ────────────────────────────────────────────────────


def resolve_config(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.exists():
            raise SystemExit(f"mcporter 配置不存在：{explicit}")
        return explicit
    from_env = os.environ.get("MCPORTER_CONFIG")
    if from_env and Path(from_env).exists():
        return Path(from_env)
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit(
        "找不到 mcporter 配置。用 --config 指定，或设置 MCPORTER_CONFIG。"
    )


# ── 性能：耗时与内存 ────────────────────────────────────────────
# 这一节回答的既不是"维度在不在"也不是"数字对不对"，而是"这一版跑起来什么样"。
# 分开列的理由和前两部分一样：把它混进可用率会让一个慢但正确的版本看起来像坏了。


PID_FILE = PROJECT_ROOT / "cn-stock-mcp.pid"


def _percentile(values: list[float], q: float) -> float:
    """最近秩法。样本量小的时候插值只会造出一个没人观测到的数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def service_pid() -> int | None:
    """服务进程号。先读 start.sh 写的 pidfile，读不到再按命令行找。"""
    try:
        pid = int(PID_FILE.read_text().split()[0])
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,args="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and "finmcp" in parts[1] and "verify_release" not in parts[1]:
            try:
                return int(parts[0])
            except ValueError:
                continue
    return None


_HAS_PROC = Path("/proc/self/stat").exists()
_CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _parse_proc_stat(raw: str):
    """把一行 /proc/<pid>/stat 解析成 (pid, ppid, rss_kib, comm, cpu_seconds)。

    单独拎出来是为了能测：这条路只在 Linux 上跑，而 Linux 正是部署环境——
    开发机（macOS）根本走不到它，不测就等于裸奔上线。

    解析的坑在 comm：它是进程名，**带括号且可能含空格甚至括号本身**
    （``(Web Content)``、``(a (b))``），所以必须按第一个 " (" 和最后一个 ") "
    切，不能整行 split()。字段序号见 proc(5)：ppid 是第 4 个字段，utime/stime
    第 14/15，rss（页数）第 24——都从 1 数起，去掉 pid 和 comm 之后要各减 2。
    """
    head, sep, tail = raw.partition(" (")
    if not sep:
        return None
    comm, sep, rest = tail.rpartition(") ")
    if not sep:
        return None
    fields = rest.split()
    if len(fields) < 22:
        return None
    try:
        pid = int(head)
        ppid = int(fields[1])
        utime, stime = int(fields[11]), int(fields[12])
        rss_pages = int(fields[21])
    except ValueError:
        return None
    page_kib = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4
    return pid, ppid, rss_pages * page_kib, comm, (utime + stime) / _CLOCK_TICKS


def _process_table() -> tuple[dict, dict]:
    """(pid -> (ppid, rss_kib, comm, cpu_seconds), ppid -> [pid])。

    Linux 上直读 /proc，不起子进程：``ps`` 每次要 fork+exec，实测 32.5ms CPU，
    1Hz 下就是单核的 3.25%——在 2 核这一档的机器上，测量工具自己吃掉这么多是不合适的,
    而且它测的正是"这台机器忙不忙"。读 /proc 是纯文件读，成本低两个数量级。
    macOS 没有 /proc，退回 ``ps``（那里只有开发机在跑，成本无所谓）。

    顺带取 CPU 时间：内存回答"占了多少",CPU 回答"是不是算力打满了"。
    2026-09-05 那轮就需要这个来分辨——单标的工具变快、多标的变慢,到底是机器忙
    还是上游慢。
    """
    procs: dict[int, tuple[int, int, str, float]] = {}
    kids: dict[int, list[int]] = {}
    if _HAS_PROC:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as handle:
                    raw = handle.read().decode("utf-8", "replace")
            except OSError:
                continue
            row = _parse_proc_stat(raw)
            if row is None:
                continue
            pid, ppid, rss_kib, comm, cpu = row
            procs[pid] = (ppid, rss_kib, comm, cpu)
            kids.setdefault(ppid, []).append(pid)
        if procs:
            return procs, kids
        # /proc 在但一条都读不出来（hidepid 之类的挂载选项）。这台机器我测不到，
        # 所以留一条退路，别让采样静默变成空。
        procs.clear()
        kids.clear()

    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,rss=,comm="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return procs, kids
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        procs[pid] = (ppid, rss, parts[3], 0.0)
        kids.setdefault(ppid, []).append(pid)
    return procs, kids


_BROWSER_HINTS = ("chrome", "chromium", "headless_shell", "Xvfb")


def _pss_kib(pid: int) -> float | None:
    """读 ``/proc/<pid>/smaps_rollup`` 的 Pss，单位 KiB；读不到返回 None。

    为什么非要它：RSS 把共享页在每个进程里各算一次，而 Chromium 是一个主进程加
    七八个共享同一份代码段和字体缓存的渲染进程——逐进程相加会把同一块内存算七八遍。
    2026-09-06 实测 RSS 合计 1482 MiB，而机器级曲线只涨了约 840 MiB，
    虚高 1.76 倍。PSS 把共享页按共享它的进程数均摊，加起来才等于"这棵树真正占了多少"。

    ``smaps_rollup`` 是内核直接给的汇总（不是 ``smaps`` 那样一段段自己加），代价是
    一次页表遍历，十几个进程每秒一轮可以忽略。只有 Linux 有；macOS 上没有等价物，
    所以那里只能退回 RSS，报告里必须说清是哪一种。
    """
    try:
        with open(f"/proc/{pid}/smaps_rollup", "rb") as handle:
            for line in handle:
                if line.startswith(b"Pss:"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


class TreeSample(NamedTuple):
    """一次采样。``pss_mib`` 为 0 表示这个平台或权限下读不到 PSS。"""

    rss_mib: float
    processes: int
    browser_rss_mib: float
    browser_processes: int
    cpu_seconds: float
    pss_mib: float = 0.0
    #: 成功读到 PSS 的进程数。小于 processes 就说明 PSS 那列是不完整的，
    #: 不能拿去和预算比——宁可不给数，也不给一个偏低的数。
    pss_processes: int = 0


def tree_rss(pid: int) -> TreeSample:
    """采一次服务进程树。

    浏览器那部分单列，因为它是峰值的主要来源，也是 BROWSER_MAX_PAGES
    这个旋钮直接作用的地方——两个数放在一起才看得出上调的代价落在哪。
    """
    procs, kids = _process_table()
    if pid not in procs:
        return TreeSample(0.0, 0, 0.0, 0, 0.0)
    total = browser = 0
    count = browser_count = 0
    pss = 0.0
    pss_count = 0
    cpu = 0.0
    seen: set[int] = set()
    stack = [pid]
    while stack:
        current = stack.pop()
        if current in seen or current not in procs:
            continue
        seen.add(current)
        _, rss, comm, proc_cpu = procs[current]
        total += rss
        cpu += proc_cpu
        count += 1
        # 只对树里的进程读 smaps_rollup。_process_table 扫的是全机进程，
        # 对每一个都读一次页表汇总，代价就不是可以忽略的了。
        measured = _pss_kib(current)
        if measured is not None:
            pss += measured
            pss_count += 1
        if any(hint.lower() in comm.lower() for hint in _BROWSER_HINTS):
            browser += rss
            browser_count += 1
        stack.extend(kids.get(current, ()))
    return TreeSample(
        total / 1024, count, browser / 1024, browser_count, cpu,
        pss / 1024, pss_count,
    )


@dataclass
class MemoryWatch:
    """跑分期间按固定间隔采样服务进程树。

    采样而不是只取首尾：浏览器页面用完即关，峰值只在页面加载的那两三秒里存在，
    首尾两次采样必然错过它。间隔默认 1 秒——比一次页面加载（服务器实测 p50 2.3s）
    短，够抓到峰值，又不至于让 ps 本身成为负载。
    """

    interval: float = 1.0
    pid: int | None = None
    samples: list[tuple[float, int, float, int, float]] = field(default_factory=list)
    note: str = ""
    _thread: object = None
    _stop: object = None

    def start(self) -> None:
        self.pid = service_pid()
        if self.pid is None:
            self.note = "没找到服务进程（pidfile 不存在，命令行里也没匹配到）"
            return
        import threading

        self._stop = threading.Event()

        def loop():
            while not self._stop.wait(self.interval):
                sample = tree_rss(self.pid)
                if sample[1]:
                    self.samples.append(sample)

        self.samples.append(tree_rss(self.pid))
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=5)
        if self.pid is not None and not self.samples:
            self.note = "采样期间进程树读不到，服务可能中途重启了"

    @property
    def ok(self) -> bool:
        return bool(self.samples)

    def summary(self) -> dict:
        totals = [s[0] for s in self.samples]
        browsers = [s[2] for s in self.samples]
        cpus = [s[4] for s in self.samples]
        peak_at = max(range(len(self.samples)), key=lambda i: self.samples[i][0])
        # 累计 CPU 秒是单调的，首尾之差就是这段窗口里真正烧掉的算力。
        cpu_used = max(0.0, cpus[-1] - cpus[0]) if len(cpus) > 1 else 0.0
        span = max(1e-9, (len(self.samples) - 1) * self.interval)

        # PSS 只在**每一个**进程都读到时才算数。少读一个就是偏低，而偏低的内存数
        # 比没有内存数更危险——它会让一个超预算的版本看着合格。
        complete = [s for s in self.samples
                    if len(s) > 6 and s[6] and s[6] == s[1]]
        pss = [s[5] for s in complete]
        return {
            "first": totals[0],
            "peak": max(totals),
            "last": totals[-1],
            "mean": sum(totals) / len(totals),
            "peak_processes": self.samples[peak_at][1],
            "browser_peak": max(browsers) if browsers else 0.0,
            "browser_peak_processes": max(s[3] for s in self.samples),
            "samples": len(self.samples),
            "cpu_seconds": cpu_used,
            "cpu_cores": cpu_used / span,
            "span": span,
            "pss_peak": max(pss) if pss else None,
            "pss_mean": (sum(pss) / len(pss)) if pss else None,
            "pss_last": pss[-1] if pss else None,
            "pss_samples": len(pss),
        }


def _render_performance(watch: "MemoryWatch", calls: list[CallResult]) -> list[str]:
    """性能一节。耗时按工具分组，内存给进程树峰值。"""
    lines = ["## 性能：耗时与内存", ""]   # 标题由调用方按报告编号覆盖
    lines.append("和前两部分分开看：这一节不进可用率。一个慢但数字全对的版本，"
                 "可用率应当照样是满分，而它慢这件事要在这里看得见。")
    lines.append("")

    ok_calls = [c for c in calls if c.ok]
    if not ok_calls:
        lines.append("本次没有成功的调用，无耗时可统计。")
        lines.append("")
        return lines

    lines.append("### 调用耗时")
    lines.append("")
    lines.append("| 工具 | 次数 | 平均 | P50 | P90 | P95 | 最慢 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    by_tool: dict[str, list[float]] = {}
    for call in ok_calls:
        by_tool.setdefault(call.spec.tool, []).append(call.elapsed)
    for tool in sorted(by_tool, key=lambda t: -_percentile(by_tool[t], 0.9)):
        values = by_tool[tool]
        lines.append(
            f"| {tool} | {len(values)} | {sum(values)/len(values):.2f}s | "
            f"{_percentile(values, 0.5):.2f}s | {_percentile(values, 0.9):.2f}s | "
            f"{_percentile(values, 0.95):.2f}s | {max(values):.2f}s |"
        )
    everything = [c.elapsed for c in ok_calls]
    lines.append(
        f"| **全部** | **{len(everything)}** | **{sum(everything)/len(everything):.2f}s** | "
        f"**{_percentile(everything, 0.5):.2f}s** | **{_percentile(everything, 0.9):.2f}s** | "
        f"**{_percentile(everything, 0.95):.2f}s** | **{max(everything):.2f}s** |"
    )
    lines.append("")
    lines.append("> 这是 mcporter 端到端的墙钟时间，含进程启动和传输，比服务日志里的 "
                 "`cost=` 大一截。跨版本比要用同一个口径，别拿它跟日志里的数直接比。")
    lines.append("")

    lines.append("### 服务进程树内存")
    lines.append("")
    if not watch.ok:
        lines.append(f"未采到：{watch.note or '原因不明'}。")
        lines.append("")
        return lines
    info = watch.summary()
    lines.append(f"| 指标 | 值 |")
    lines.append(f"| --- | ---: |")
    has_pss = info["pss_peak"] is not None
    lines.append(f"| 起始 | {info['first']:.0f} MiB |")
    if has_pss:
        # PSS 在前、RSS 在后：能跟预算比的是前者，后者只是上界。
        budget = MEMORY_BUDGET_MIB
        verdict = "✅ 在预算内" if info["pss_peak"] <= budget else "❌ 超预算"
        lines.append(f"| **峰值 PSS** | **{info['pss_peak']:.0f} MiB**"
                     f"（预算 {budget:.0f} MiB，{verdict}）|")
        lines.append(f"| 峰值 RSS 合计 | {info['peak']:.0f} MiB"
                     f"（{info['peak_processes']} 进程，含重复计的共享页）|")
        lines.append(f"| 均值 PSS | {info['pss_mean']:.0f} MiB |")
        lines.append(f"| 结束 PSS | {info['pss_last']:.0f} MiB |")
    else:
        lines.append(f"| **峰值 RSS 合计** | **{info['peak']:.0f} MiB**"
                     f"（{info['peak_processes']} 进程）|")
        lines.append(f"| 均值 | {info['mean']:.0f} MiB |")
        lines.append(f"| 结束 | {info['last']:.0f} MiB |")
    lines.append(f"| 其中浏览器峰值 RSS | {info['browser_peak']:.0f} MiB"
                 f"（{info['browser_peak_processes']} 进程）|")
    if info["cpu_seconds"] > 0:
        lines.append(f"| **CPU** | **{info['cpu_seconds']:.0f}s / {info['span']:.0f}s"
                     f" = 平均占 {info['cpu_cores']:.2f} 核** |")
    lines.append(f"| 采样 | 每 {watch.interval:.0f}s 一次，共 {info['samples']} 次 |")
    lines.append("")
    if info["cpu_seconds"] > 0:
        lines.append("> CPU 那一行回答的是「慢是因为算力打满，还是因为上游慢」。"
                     "接近核数就是算力打满；远低于核数而调用又慢，那是在等网络。"
                     "只统计服务进程树，不含本脚本自己和它拉起的 mcporter。")
        lines.append("")
    if has_pss:
        lines.append(f"> 口径：PSS 读自 `/proc/<pid>/smaps_rollup`，共享页按共享它的进程数均摊，"
                     f"整棵树加起来等于「实际占了多少」，可以直接和预算比。"
                     f"RSS 那一行是逐进程相加，Chromium 的代码段会被算七八遍——"
                     f"本次两者相差 {info['peak'] / max(info['pss_peak'], 1e-9):.2f} 倍，"
                     f"留着它是为了看 `BROWSER_MAX_PAGES` 这类旋钮的代价落在哪。"
                     f"PSS 采到 {info['pss_samples']}/{info['samples']} 次"
                     f"（只在整棵树都读到时才计入，缺一个就整次作废，"
                     f"偏低的内存数比没有更危险）。")
    else:
        lines.append("> 口径：`ps` 的 RSS 逐进程相加，共享页在每个进程里各算一次，"
                     "所以这个数是**偏高的上界**（Chromium 上实测约 1.8 倍），"
                     f"不能直接拿去和 {MEMORY_BUDGET_MIB:.0f} MiB 的预算比。"
                     "本次没拿到 PSS——它要读 `/proc/<pid>/smaps_rollup`，"
                     "只有 Linux 有，且要有权限。要跟预算硬比，得在目标部署环境上跑。")
    lines.append("")
    lines.append("> 浏览器那一行单列，是因为峰值基本由它决定：页面用完即关，所以峰值只在"
                 "页面加载的那两三秒里存在，采样间隔必须比一次加载短才抓得到。")
    lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="mcporter 配置路径")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--report", type=Path, help="报告输出路径")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--only", help="只跑这些工具，逗号分隔")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument(
        "--capture",
        nargs="?",
        const=str(DEFAULT_BASELINE_DIR),
        help="把本次探活的成功返回冻成归档，留给下一次改动当基线",
    )
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--memory-interval",
        type=float,
        default=1.0,
        help="服务进程树 RSS 的采样间隔，秒；置 0 关闭内存采样",
    )
    parser.add_argument("--timeout-ms", type=int, default=120000)
    args = parser.parse_args()

    if shutil.which("mcporter") is None:
        raise SystemExit("找不到 mcporter，先装上或把它放进 PATH")

    config = resolve_config(args.config)
    tools = set(args.only.split(",")) if args.only else set(ALL_TOOLS)
    started = dt.datetime.now().replace(microsecond=0)

    print(f"[验证] 配置={config}")
    print(f"[验证] 工具={','.join(sorted(tools))}")

    watch = MemoryWatch(interval=args.memory_interval)
    if args.memory_interval > 0:
        watch.start()
        print(f"[验证] 内存采样 pid={watch.pid or '未找到'} 间隔={args.memory_interval}s")
    else:
        watch.note = "--memory-interval 0，本次没开内存采样"

    probes: list[tuple[CallResult, Payload, Completeness]] = []
    if not args.skip_probe:
        specs = probe_suite(tools)
        print(f"[验证] 探活 {len(specs)} 个调用…"
              f"（并发 {args.concurrency}，单个上限 {args.timeout_ms / 1000:.0f}s；"
              f"下面按完成先后逐行打印）", flush=True)
        ordered: list[tuple[int, CallResult, Payload, Completeness]] = []
        for index, result in run_calls(specs, config, args.timeout_ms, args.concurrency):
            payload = parse_payload(result.payload)
            completeness = (
                check_completeness(result.spec.tool, payload) if result.ok
                else Completeness()
            )
            ordered.append((index, result, payload, completeness))
            flag = "OK " if result.ok else "FAIL"
            rate = (
                "" if completeness.graded == 0
                else f"  维度 {completeness.available}/{completeness.graded}"
            )
            print(f"  [{len(ordered)}/{len(specs)}] [{flag}] "
                  f"{result.spec.describe()[:60]}  {result.elapsed:.1f}s{rate}", flush=True)

        # 产出是完成顺序，报告和归档要的是 specs 的原顺序——按下标排回去。
        ordered.sort(key=lambda item: item[0])
        probes = [(result, payload, c) for _, result, payload, c in ordered]

        if args.capture:
            hard = [item for _, _, c in probes for item in c.bad]
            if hard:
                print(f"[验证] 有 {len(hard)} 处维度缺失，不冻结基线——"
                      "把不完整的输出当成下次的账本只会把问题固化下来")
            else:
                for result, _, _ in probes:
                    if result.ok:
                        print(f"  [冻结] {capture_baseline(result, Path(args.capture), config).name}")

    regressions: list[tuple[Baseline, CallResult | None, list[DocumentDiff]]] = []
    if not args.skip_baseline:
        files = sorted(p for p in args.baseline.glob("*.md") if p.name != "README.md")
        print(f"[验证] 基线 {len(files)} 份，来自 {args.baseline}")
        pending: list[Baseline] = []
        for path in files:
            baseline = load_baseline(path)
            if baseline is None:
                print(f"  [跳过] {path.name}：不是 mcporter 归档格式")
                continue
            if baseline.spec.tool not in tools:
                continue
            if baseline.replay_spec is None:
                print(f"  [跳过] {path.name}：{baseline.skip_reason}")
                regressions.append((baseline, None, []))
                continue
            pending.append(baseline)

        # 按下标取回对应的 baseline。这里绝不能 zip(pending, results)：results 是
        # 完成顺序，zip 会把先跑完的结果配给排在前面的基线。
        replays = run_calls(
            [b.replay_spec for b in pending], config, args.timeout_ms, args.concurrency
        )
        for index, result in replays:
            baseline = pending[index]
            if not result.ok:
                print(f"  [FAIL] {baseline.path.name} 重放失败 exit={result.exit_code}")
                regressions.append((baseline, result, []))
                continue
            fresh = parse_payload(result.payload)
            diffs = []
            for name, old_text in baseline.payload.documents.items():
                old_structure = baseline.payload.structures.get(name)
                new_structure = fresh.structures.get(name)
                if old_structure is not None and new_structure is not None:
                    diffs.append(compare_structures(old_structure, new_structure, name))
                else:
                    diffs.append(
                        compare_documents(
                            old_text,
                            fresh.documents.get(name, ""),
                            name,
                            drop_live_only=baseline.date_added,
                            demote_live_values=baseline.live_stale,
                        )
                    )
            bad = sum(len(diff.hard) for diff in diffs)
            summary = _kind_summary(diffs)
            note = "" if not bad else f"  {bad} 处不同"
            note += "" if not summary else f"（另 {summary}）"
            print(f"  [{'OK ' if not bad else 'DIFF'}] {baseline.path.name}{note}")
            regressions.append((baseline, result, diffs))

        # 追加顺序是完成顺序，报告要的是文件名顺序——否则同一组基线两次跑出来的
        # 报告行序不一样，diff 两份报告会全是噪声。
        regressions.sort(key=lambda item: item[0].path.name)

    watch.stop()
    scan = scan_log(args.log, started)
    report, failed, verdict = render_report(
        started, config, probes, regressions, scan, watch
    )

    destination = args.report or (DEFAULT_REPORT_DIR / f"{started:%Y%m%d_%H%M%S}.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report, encoding="utf-8")
    print()
    print(f"[验证] 报告已写入 {destination}")
    # 与报告里那一行同一个判定，别一边说"全部通过"一边在报告里标着数据缺口。
    # 和报告标题同一句式：先给分数，再给定性。控制台常常是唯一被看到的输出。
    print(f"[验证] 结论：{verdict}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
