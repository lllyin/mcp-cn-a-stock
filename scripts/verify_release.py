#!/usr/bin/env python3
"""上线后的数据验证层：探活、维度完整性、与旧数据回归比对。

改了取数逻辑、换了数据源、挪了机器之后跑一遍，它回答三个问题：

  1. 还回不回数据    每个工具都调一遍，看退出码、errors 和 warnings
  2. 数据齐不齐      按维度契约逐项检查报告，缺哪一维就指出该看哪个上游源
  3. 和以前一不一样  用 verification/baseline/ 里的历史归档重放同样的调用，逐行比对

只读，不改任何服务状态。重放一律把日期钉死（归档命令里没有 date= 的，就从归档
报告自己的数据日期反推补上），所以已收盘的字段应当逐字相同——对不上就是真漂了，
不是行情动了。

用法：

    python scripts/verify_release.py                      # 全跑
    python scripts/verify_release.py --skip-baseline      # 只探活和完整性
    python scripts/verify_release.py --skip-probe         # 只跑回归比对
    python scripts/verify_release.py --only brief,full    # 限定工具
    python scripts/verify_release.py --probe-date 2026-08-21

退出码：0 全过；1 有失败/缺维/不一致；2 脚本自己跑不起来（找不到 mcporter 等）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE_DIR = PROJECT_ROOT / "verification" / "baseline"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "verification" / "reports"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "cn-stock-mcp.log"
DEFAULT_CONFIG_CANDIDATES = (
    Path.home() / ".openclaw" / "workspace" / "config" / "mcporter.json",
    Path("/root/.openclaw/workspace/config/mcporter.json"),
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


STOCK, ETF, INDEX = "stock", "etf", "index"
ALL_CLASSES = frozenset((STOCK, ETF, INDEX))
# 只有个股才有的那些维度。ETF 没有市盈率、市净率、财务报表和所属行业（服务端在
# `_fetch_finance_sync` 里对 1xxxxx/5xxxxx 直接返回 None）；指数连市值都没有。
# 不分类的话，一次 full + ETF 的探活会凭空报出 10 项"缺失"，全是误报。
STOCK_ONLY = frozenset((STOCK,))


@dataclass(frozen=True)
class Dimension:
    name: str
    marker: str          # 在报告文本里的行首特征
    source: str          # 由哪个上游源提供
    applies_to: frozenset = ALL_CLASSES


_BASIC = (
    Dimension("股票代码", "- 股票代码:", "realtime"),
    Dimension("股票名称", "- 股票名称:", "realtime"),
    Dimension("数据日期", "- 数据日期:", "kline"),
    Dimension("行业概念", "- 行业概念:", "realtime", applies_to=STOCK_ONLY),
    Dimension("总市值", "- 总市值:", "realtime", applies_to=STOCK_ONLY),
    Dimension("流通市值", "- 流通市值:", "realtime", applies_to=STOCK_ONLY),
    Dimension("市盈率(静)", "- 市盈率(静):", "realtime", applies_to=STOCK_ONLY),
    Dimension("市盈率(动)", "- 市盈率(动):", "realtime", applies_to=STOCK_ONLY),
    Dimension("市净率", "- 市净率:", "realtime", applies_to=STOCK_ONLY),
    Dimension("净资产收益率", "- 净资产收益率:", "realtime", applies_to=STOCK_ONLY),
)

_TRADING = (
    Dimension("价格", "## 价格", "kline"),
    Dimension("涨跌幅", "## 涨跌幅", "kline"),
    Dimension("振幅", "## 振幅", "kline"),
    Dimension("成交量", "## 成交量(万手)", "kline"),
    Dimension("成交额", "## 成交额(亿)", "kline"),
    Dimension("资金流向", "## 资金流向", "fund_flow"),
    # 换手率 = 成交量 / 流通股本，分母来自 realtime 的市值，所以 realtime 挂了
    # 表现是"换手率整段不见了"，而不是数字不对。
    Dimension("换手率", "## 换手率", "realtime(流通市值)", applies_to=STOCK_ONLY),
)

# 财务报表只有个股有；历史资金流向个股和 ETF 都有页面，指数里 SH000688 压根没有
# 页面，所以整类排除；技术指标算的是 K 线，三类都有。
_FINANCE = (Dimension("财务数据", "# 财务数据", "finance", STOCK_ONLY),)
_HISTORY_FLOW = (
    Dimension("历史资金流向", "## 历史资金流向", "fund_flow", frozenset((STOCK, ETF))),
)
_TECHNICAL = (Dimension("技术指标", "# 技术指标", "kline"),)

CONTRACT: dict[str, tuple[Dimension, ...]] = {
    "brief": _BASIC + _TRADING,
    "medium": _BASIC + _TRADING + _FINANCE,
    "full": _BASIC + _TRADING + _HISTORY_FLOW + _FINANCE + _TECHNICAL,
}

# 报告里这些句子说明某一维是"渲染出来了但没有值"。有值和有段落标题是两回事，
# 只查标题会把降级当成正常。
#
# benign=True 的是正当缺席：不是故障，也不该扣可用率的分。查询里写了 date= 就是
# 在查那一天的收盘数据，实时资金流本来就没有当天之外的口径，不展示是对的。
DEGRADED_MARKERS = {
    "指定日期查询暂不展示实时资金流向": (
        "查询指定了 date=，问的是那天的收盘数据，实时资金流没有当天之外的口径",
        True,
    ),
    "盘中实时数据暂时不可用": ("盘中回退整层被跳过或熔断", False),
    "暂无数据": ("该维度取到空值", False),
    "获取失败": ("该维度取数失败", False),
}

# 指数判定与服务端保持一致，见 cn_stock_source._INDEX_CODE_PREFIXES。
_INDEX_PREFIXES = {"sh": ("000",), "sz": ("399",), "bj": ("899",)}


def is_index(symbol: str) -> bool:
    normalized = (symbol or "").lower()
    return normalized[2:].startswith(_INDEX_PREFIXES.get(normalized[:2], ()))


def classify(symbol: str) -> str:
    """个股 / ETF / 指数。ETF 的判据与服务端一致：六位码以 1 或 5 开头。"""
    if is_index(symbol):
        return INDEX
    return ETF if (symbol or "")[2:].startswith(("1", "5")) else STOCK


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
) -> list[CallResult]:
    if concurrency <= 1:
        return [run_call(spec, config, timeout_ms) for spec in specs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(lambda spec: run_call(spec, config, timeout_ms), specs))


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
    benign: bool = False       # 正当缺席，不算故障也不扣分

    @property
    def verdict(self) -> str:
        if self.benign:
            return f"✅ {self.degraded_note}"
        if self.degraded_note:
            return f"⚠️ {self.degraded_note}"
        return "❌ 该有却没有"


@dataclass
class Completeness:
    """一次调用的维度账：应检多少、正当缺席多少、真缺多少。"""

    expected: int = 0
    benign: int = 0
    findings: list[MissingDimension] = field(default_factory=list)

    @property
    def bad(self) -> list[MissingDimension]:
        return [item for item in self.findings if not item.benign]

    @property
    def graded(self) -> int:
        """算分的分母：应检的减去正当缺席的。"""
        return max(0, self.expected - self.benign)

    @property
    def available(self) -> int:
        return self.graded - len(self.bad)


def check_completeness(tool: str, payload: Payload) -> Completeness:
    dimensions = CONTRACT.get(tool)
    result = Completeness()
    if not dimensions:
        return result
    for symbol, document in payload.documents.items():
        symbol_class = classify(symbol)
        for dimension in dimensions:
            if symbol_class not in dimension.applies_to:
                continue
            result.expected += 1
            if dimension.marker not in document:
                result.findings.append(MissingDimension(symbol, dimension))
                continue
            note, benign = _degraded_note(document, dimension)
            if note:
                result.findings.append(MissingDimension(symbol, dimension, note, benign))
                if benign:
                    result.benign += 1
    return result


def _degraded_note(document: str, dimension: Dimension) -> tuple[str, bool]:
    """段落在，但里面只有一句"没有数据"——这也算这一维没拿到。"""
    if not dimension.marker.startswith("##"):
        return "", False
    _, _, tail = document.partition(dimension.marker)
    body = tail.split("\n#", 1)[0]
    for marker, (explanation, benign) in DEGRADED_MARKERS.items():
        if marker in body:
            return explanation, benign
    if not body.strip():
        return "段落为空", False
    return "", False


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
    kind: str          # 缺失 / 新增 / 值变化
    key: str
    old: str = ""
    new: str = ""


@dataclass
class DocumentDiff:
    document: str
    diffs: list[LineDiff]
    drift_note: str = ""     # 整体等比漂移的判定结果

    @property
    def clean(self) -> bool:
        return not self.diffs

    @property
    def hard(self) -> list[LineDiff]:
        """真正该看的差异。

        实时口径是钉日期造成的，复权漂移是公司行为造成的——两类都不是这次改动的
        问题，进了结论只会把真差异挤出视野。它们在 drift_note 和计数里仍然可见。
        """
        return [diff for diff in self.diffs if diff.kind not in ("实时口径", "复权漂移")]


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
    return {prefix: json.dumps(node, ensure_ascii=False)}


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
    return DocumentDiff(name, _collapse(diffs), drift_note=note)


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
        key = f"{section} › {_line_key(line)}" if section else _line_key(line)
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
    if old_lines == new_lines:
        return DocumentDiff(name, [])

    old_map = _index_by_section(old_lines)
    new_map = _index_by_section(new_lines)

    diffs: list[LineDiff] = []
    for key, values in old_map.items():
        if key not in new_map:
            diffs.append(LineDiff("缺失", key, old=values[0]))
        elif new_map[key] != values:
            kind = "值变化"
            if demote_live_values and _LIVE_VALUE_KEY.search(key):
                kind = "实时口径"
            diffs.append(LineDiff(kind, key, old=values[0], new=new_map[key][0]))
    for key, values in new_map.items():
        if key not in old_map:
            diffs.append(LineDiff("新增", key, new=values[0]))

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
    return scan


# ── 八、探活套件 ────────────────────────────────────────────────


def probe_suite(date: str, tools: set[str]) -> list[CallSpec]:
    """每个工具至少一发，标的覆盖主板/创业板/科创板/ETF/指数。

    日期钉在一个已收盘的交易日上，这样两次跑出来应当一致，脚本自己也可复现。
    """
    stocks = "SH600519,SZ000333,SZ300750,SH688981"
    indices = "SH000001,SZ399001,SZ399006,SH000688"
    specs = [
        CallSpec("brief", {"symbol": stocks, "date": date}, "个股 brief"),
        CallSpec("brief", {"symbol": indices, "date": date}, "指数 brief"),
        CallSpec("medium", {"symbol": "SH600519", "date": date}, "medium"),
        CallSpec("full", {"symbol": "SH600519,SH512480", "date": date}, "full + ETF"),
        CallSpec("kline_daily", {"symbol": "SH600519", "date": date}, "kline_daily"),
        CallSpec(
            "kline_range",
            {"symbol": "SH600519", "start_date": _shift(date, -14), "end_date": date},
            "kline_range",
        ),
        CallSpec("tech", {"symbol": "SH600519", "days": "30", "date": date}, "tech"),
        CallSpec("market_breadth", {}, "market_breadth"),
        CallSpec("market_events", {"date": date, "sources": "lhb,limit_up"}, "market_events"),
    ]
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


def last_settled_trading_day(today: dt.date | None = None) -> str:
    """上一个已收盘的交易日（只避开周末，不查节假日；节假日会自然回退到空数据）。"""
    day = (today or dt.date.today()) - dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day.isoformat()


# ── 九、报告 ────────────────────────────────────────────────────


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
    baselines_ok: int = 0
    baselines_total: int = 0

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
        return self._pct(self.baselines_ok, self.baselines_total)

    @property
    def overall(self) -> float:
        """取三项里最低的那个。

        平均会把"一个源整层挂了"稀释成看着还行的 85 分。这层是发布前的闸门，
        闸门该按最短的那块板算。
        """
        return min(self.tool_rate, self.dimension_rate, self.baseline_rate)

    @property
    def verdict(self) -> str:
        if self.overall >= 99.0:
            return "✅ 可发布"
        if self.overall >= 90.0:
            return "⚠️ 有降级，确认原因后再发"
        return "❌ 不可发布"


# 四大指数走的是和个股不同的代码路径：成交量单位推断整条绕过（指数的"收盘"是
# 点位不是股价），资金流向来自大盘口径而不是个股口径，SH000688 更是压根没有资金
# 流向页面。以前的成交量小两个数量级就是栽在这儿，所以单独拎出来看。
CORE_INDICES = {
    "SH000001": "上证指数",
    "SZ399001": "深证成指",
    "SZ399006": "创业板指",
    "SH000688": "科创50",
}


def _render_index_section(
    probes: list[tuple[CallResult, Payload, Completeness]],
    regressions: list[tuple[Baseline, CallResult | None, list[DocumentDiff]]],
) -> list[str]:
    lines = ["## 三、四大指数专项", ""]
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
            if "净流入" in document:
                flow = "✅ 有"
            elif "指定日期查询暂不展示实时资金流向" in document:
                flow = "✅ 钉日期不展示"
            elif symbol == "SH000688":
                flow = "✅ 科创50 没有这个页面"
            else:
                flow = "❌ 无"
            mark = "✅" if not bad else "❌ " + "、".join(
                sorted({item.dimension.name for item in bad})
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
) -> tuple[str, bool]:
    lines: list[str] = []

    probe_failures = [result for result, _, _ in probes if not result.ok]
    bad_missing = [item for _, _, c in probes for item in c.bad]
    benign_missing = sum(c.benign for _, _, c in probes)
    dirty = [
        baseline
        for baseline, _, diffs in regressions
        if any(diff.hard for diff in diffs)
    ]
    replay_failures = [
        baseline for baseline, result, _ in regressions if result is not None and not result.ok
    ]
    comparable = [b for b, result, _ in regressions if result is not None]
    failed = bool(probe_failures or bad_missing or dirty or replay_failures)

    score = Score(
        tools_ok=len(probes) - len(probe_failures),
        tools_total=len(probes),
        dims_ok=sum(c.available for _, _, c in probes),
        dims_total=sum(c.graded for _, _, c in probes),
        baselines_ok=len(comparable) - len(dirty) - len(replay_failures),
        baselines_total=len(comparable),
    )

    lines.append("# 上线数据验证报告")
    lines.append("")
    lines.append(f"## 结论：{score.verdict}　可用率 **{score.overall:.0f}%**")
    lines.append("")
    lines.append("| 指标 | 分数 | 明细 |")
    lines.append("| --- | ---: | --- |")
    lines.append(
        f"| 工具可用率 | {score.tool_rate:.0f}% | "
        f"{score.tools_ok}/{score.tools_total} 个调用拿到了返回 |"
    )
    lines.append(
        f"| 维度完整率 | {score.dimension_rate:.0f}% | "
        f"{score.dims_ok}/{score.dims_total} 项该有的数据真的有"
        + (f"；另有 {benign_missing} 项正当缺席，不计分" if benign_missing else "")
        + " |"
    )
    lines.append(
        f"| 回归一致率 | {score.baseline_rate:.0f}% | "
        f"{score.baselines_ok}/{score.baselines_total} 份基线重放后一致 |"
    )
    lines.append("")
    lines.append(
        "> 综合分取三项里最低的那个：平均会把「某个源整层挂了」稀释成看着还行的分数，"
        "而这层是发布前的闸门，闸门按最短的那块板算。"
        "≥99% 可发布，≥90% 要确认原因，低于 90% 不发。"
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
        if dirty:
            lines.append(f"- ❌ {len(dirty)} 份基线重放后与旧数据不一致，见第四节")
        if replay_failures:
            lines.append(f"- ❌ {len(replay_failures)} 份基线重放失败")
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

    lines.append("## 二、工具探活与维度完整性")
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
            else f"{completeness.available * 100 / completeness.graded:.0f}%"
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

    lines.extend(_render_index_section(probes, regressions))

    lines.append("## 四、与旧数据比对")
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
                live = sum(len(diff.diffs) - len(diff.hard) for diff in diffs)
                tail = f"，另有 {live} 处实时口径差异（不可比）" if live else ""
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
                live = len(document.diffs) - len(document.hard)
                lines.append(
                    f"**{document.document}**：{len(document.hard)} 处"
                    + (f"（另有 {live} 处实时口径差异，已折叠）" if live else "")
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

    lines.append("## 五、怎么看这份报告")
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
    lines.append("- **正当缺席**：钉了日期就没有实时资金流可展示，这不是故障，"
                 "不进可用率的分母。")
    lines.append("")
    lines.append("- 基线过期了就从 `logs/mcporter/` 拷收盘后的新归档进 "
                 "`verification/baseline/`，文件名不限，脚本按文件里的「命令：」那一行"
                 "反解调用；选基线的规则见该目录下的 README。")
    lines.append("")
    return "\n".join(lines), failed


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="mcporter 配置路径")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--report", type=Path, help="报告输出路径")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--probe-date", help="探活用的已收盘交易日，默认取上一个工作日")
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
    parser.add_argument("--timeout-ms", type=int, default=120000)
    args = parser.parse_args()

    if shutil.which("mcporter") is None:
        raise SystemExit("找不到 mcporter，先装上或把它放进 PATH")

    config = resolve_config(args.config)
    tools = set(args.only.split(",")) if args.only else set(ALL_TOOLS)
    probe_date = args.probe_date or last_settled_trading_day()
    started = dt.datetime.now().replace(microsecond=0)

    print(f"[验证] 配置={config}")
    print(f"[验证] 探活日期={probe_date} 工具={','.join(sorted(tools))}")

    probes: list[tuple[CallResult, Payload, Completeness]] = []
    if not args.skip_probe:
        specs = probe_suite(probe_date, tools)
        print(f"[验证] 探活 {len(specs)} 个调用…")
        for result in run_calls(specs, config, args.timeout_ms, args.concurrency):
            payload = parse_payload(result.payload)
            completeness = (
                check_completeness(result.spec.tool, payload) if result.ok
                else Completeness()
            )
            probes.append((result, payload, completeness))
            flag = "OK " if result.ok else "FAIL"
            rate = (
                "" if completeness.graded == 0
                else f"  维度 {completeness.available}/{completeness.graded}"
            )
            print(f"  [{flag}] {result.spec.describe()[:66]}  {result.elapsed:.1f}s{rate}")

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

        results = run_calls(
            [b.replay_spec for b in pending], config, args.timeout_ms, args.concurrency
        )
        for baseline, result in zip(pending, results):
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
            live = sum(len(diff.diffs) - len(diff.hard) for diff in diffs)
            note = "" if not bad else f"  {bad} 处不同"
            note += "" if not live else f"（另 {live} 处实时口径）"
            print(f"  [{'OK ' if not bad else 'DIFF'}] {baseline.path.name}{note}")
            regressions.append((baseline, result, diffs))

    scan = scan_log(args.log, started)
    report, failed = render_report(started, config, probes, regressions, scan)

    destination = args.report or (DEFAULT_REPORT_DIR / f"{started:%Y%m%d_%H%M%S}.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(report, encoding="utf-8")
    print()
    print(f"[验证] 报告已写入 {destination}")
    print(f"[验证] 结论：{'有问题' if failed else '全部通过'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
