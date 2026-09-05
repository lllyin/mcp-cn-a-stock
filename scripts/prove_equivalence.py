#!/usr/bin/env python3
"""证明一次重构没有改变任何返回数据。

用法：

    # 改代码之前
    python scripts/prove_equivalence.py capture before
    # 改完之后
    python scripts/prove_equivalence.py capture after
    python scripts/prove_equivalence.py diff before after

调用集合覆盖两类，缺一不可（AGENTS.md §二）：

  - **钉日期的历史查询**：`verification/baseline/` 里 14 份归档的原命令，逐字
    重放。这一类的返回应当完全确定，是等价性判定的主力。
  - **收盘后的实时查询**：不钉日期的 brief/medium/full/tech。市值、市盈率这几
    维的口径是"此刻"，所以它们本来就可能在两次运行之间变——判定时会自动把
    "同一版代码跑两次也不一样"的字段剔掉，剩下的才算数。

两个坑，踩过：

  - **报告缓存必须关掉。** 不关的话"改完"那次直接读"改之前"写的字节，等价性
    是假的。脚本会检查服务是不是以 REPORT_CACHE_ENABLED=0 起的，不是就拒绝跑。
  - **一次 capture 要跑两遍。** 只跑一遍分不清"这个字段变了是因为我改了代码"
    还是"它本来每次就不一样"。跑两遍能把后者标出来，diff 时自动排除。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".runtime" / "equivalence"
BASELINE_DIR = ROOT / "verification" / "baseline"

# 不钉日期的实时调用。标的与 verify_release.LIVE_BATCHES 同一批，理由见那里。
LIVE_CALLS = [
    ("brief", {"symbol": "SH000001,SZ399001,SZ399006,SH000688"}),
    ("medium", {"symbol": "SH000001,SZ399001,SZ399006,SH000688"}),
    ("full", {"symbol": "SH000001,SZ399001,SZ399006,SH000688"}),
    ("tech", {"symbol": "SH000001,SZ399001,SZ399006,SH000688"}),
    ("brief", {"symbol": "SH600519,SH601318,SZ000333,SH688981"}),
    ("medium", {"symbol": "SH600519,SH601318,SZ000333,SH688981"}),
    ("full", {"symbol": "SH600519,SH601318,SZ000333,SH688981"}),
    ("brief", {"symbol": "SZ300750,SH512480,SH600118,SZ002371"}),
    ("full", {"symbol": "SZ300750,SH512480,SH600118,SZ002371"}),
    # 北交所：腾讯 KeyError、新浪能给，是兜底链里唯一由新浪独占的分支，
    # 重构 K 线那一层时最容易被漏掉的就是它。
    ("brief", {"symbol": "BJ920021"}),
    ("kline_daily", {"symbol": "BJ920021", "date": "2026-09-04"}),
]

_CMD = re.compile(r"^- 命令：.*?mcporter call cn-stock (\w+)\s*(.*)$", re.M)
_REPORT_DATE = re.compile(r"^- 数据日期:\s*(\d{4}-\d{2}-\d{2})", re.M)


def baseline_calls() -> list[tuple[str, dict]]:
    """从归档里反解出工具和参数，和 verify_release 用同一份基线、同一套钉日期规则。

    归档命令里没有 ``date=`` 的，从归档报告自己写着的"数据日期"反推补上——不补的话
    这一类就退化成实时查询，而它本来是等价性判定的主力（实时那一半天生会飘）。
    第一版就漏了这一步，结果 14 份归档里有一半在跟着当天行情走。
    """
    calls = []
    for path in sorted(BASELINE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = _CMD.search(text)
        if not match:
            continue
        tool, rest = match.group(1), match.group(2).strip()
        args = {}
        for token in rest.split():
            if "=" in token:
                key, _, value = token.partition("=")
                args[key] = value
        if "date" not in args and "start_date" not in args:
            # 归档正文里的报告是 JSON 字符串，"\n- 数据日期" 是转义的换行不是真换行，
            # 直接对整个文件跑正则一条也匹配不上。先解出 JSON 再找。
            body = text.partition("## 原始返回")[2].strip()
            try:
                reports = json.loads(body).get("reports", {})
            except (json.JSONDecodeError, AttributeError):
                reports = {}
            dates = sorted(set(_REPORT_DATE.findall("\n".join(map(str, reports.values())))))
            if len(dates) == 1:
                args["date"] = dates[0]
            elif tool != "market_breadth":
                print(f"  ⚠️ {path.name} 钉不住日期（数据日期 {dates or '无'}），"
                      f"这一份按实时查询处理")
        calls.append((tool, args))
    return calls


def call(tool: str, args: dict) -> str:
    """打一次 mcporter，返回原始 stdout。失败也记下来——失败方式变了同样是行为变了。"""
    argv = ["mcporter", "call", "cn-stock", tool]
    argv += [f"{k}={v}" for k, v in args.items()]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=ROOT)
    return done.stdout if done.returncode == 0 else f"«EXIT {done.returncode}»\n{done.stderr}"


def _documents(raw: str) -> dict[str, str]:
    """把一次返回拆成 标的 -> 文档。拆不开就整份当一份。"""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"（整份）": raw}
    reports = payload.get("reports") if isinstance(payload, dict) else None
    if isinstance(reports, dict):
        return {symbol: str(text) for symbol, text in reports.items()}
    # 墙钟字段每次都不一样，整份比对时先摘掉，否则一切都是"变了"。
    # fetched_at 是 market_breadth 的抓取时刻，和 timestamp 同一类。
    if isinstance(payload, dict):
        for field in ("timestamp", "fetched_at"):
            payload.pop(field, None)
    return {"（整份）": json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)}


def _lines(document: str) -> dict[str, str]:
    """把一份报告拆成 键 -> 行。键带上小节名，重名行不会互相盖掉。"""
    out, section, seen = {}, "", {}
    for line in document.splitlines():
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
            continue
        if not line.strip():
            continue
        head = line.split(":", 1)[0].split("|")[0].strip(" -|")
        key = f"{section}›{head}"
        seen[key] = seen.get(key, 0) + 1
        out[f"{key}#{seen[key]}"] = line
    return out


def _refuse_if_cache_is_on() -> None:
    """报告缓存开着就别跑——比出来的"等价"是缓存自己和自己相等。

    读 .env 而不是问服务：服务没有把配置吐出来的接口，而入口是
    load_dotenv(override=True)，.env 的取值就是服务实际用的那个。
    """
    env_file = ROOT / ".env"
    raw = ""
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("REPORT_CACHE_ENABLED="):
                raw = line.split("=", 1)[1].strip()
    if raw.lower() not in {"0", "false", "no", "off"}:
        sys.exit(
            "拒绝执行：.env 里 REPORT_CACHE_ENABLED 不是 0。\n"
            "先写 REPORT_CACHE_ENABLED=0 再 ./stop.sh && ./start.sh，比完记得改回来。"
        )


def capture(label: str) -> Path:
    _refuse_if_cache_is_on()
    calls = [(t, a, "baseline") for t, a in baseline_calls()]
    calls += [(t, a, "live") for t, a in LIVE_CALLS]
    runs = []
    for attempt in (1, 2):
        run = {}
        for index, (tool, args, kind) in enumerate(calls):
            name = f"{index:02d}:{tool}:{','.join(f'{k}={v}' for k, v in args.items())}"
            print(f"  [{attempt}/2] {name}", flush=True)
            for symbol, document in _documents(call(tool, args)).items():
                run[f"{name}›{symbol}"] = document
        runs.append(run)

    # 同一版代码跑两遍就不一样的键，是它自己不确定，不能拿来判等价。
    volatile = sorted(
        key
        for key in set(runs[0]) | set(runs[1])
        for a, b in [(runs[0].get(key), runs[1].get(key))]
        if a != b
    )
    path = OUT_DIR / f"{label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"documents": runs[1], "volatile": volatile}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    digest = hashlib.sha256(
        json.dumps({k: v for k, v in sorted(runs[1].items()) if k not in volatile},
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    print(f"\n[{label}] 文档 {len(runs[1])} 份，其中自身不稳定 {len(volatile)} 份")
    print(f"[{label}] 稳定部分哈希 {digest}")
    print(f"[{label}] 写入 {path}")
    return path


def diff(before_label: str, after_label: str) -> int:
    before = json.loads((OUT_DIR / f"{before_label}.json").read_text(encoding="utf-8"))
    after = json.loads((OUT_DIR / f"{after_label}.json").read_text(encoding="utf-8"))
    # 任一侧不稳定就不判——它证明不了任何事。
    volatile = set(before["volatile"]) | set(after["volatile"])
    a, b = before["documents"], after["documents"]

    only_before = sorted(set(a) - set(b) - volatile)
    only_after = sorted(set(b) - set(a) - volatile)
    changed = sorted(k for k in set(a) & set(b) - volatile if a[k] != b[k])

    print(f"比对 {before_label} → {after_label}")
    print(f"  可判定文档 {len(set(a) & set(b) - volatile)} 份，跳过自身不稳定 {len(volatile)} 份")
    if not (only_before or only_after or changed):
        print("  ✅ 逐字等价")
        return 0
    for key in only_before:
        print(f"  ❌ 后一次没有：{key}")
    for key in only_after:
        print(f"  ❌ 后一次多出：{key}")
    for key in changed:
        la, lb = _lines(a[key]), _lines(b[key])
        rows = [k for k in set(la) | set(lb) if la.get(k) != lb.get(k)]
        print(f"  ❌ {key}  {len(rows)} 行不同")
        for row in sorted(rows)[:6]:
            print(f"       {row}")
            print(f"         前: {la.get(row, '«无»')}")
            print(f"         后: {lb.get(row, '«无»')}")
    return 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "capture":
        return 0 if capture(sys.argv[2]) else 1
    if len(sys.argv) >= 4 and sys.argv[1] == "diff":
        return diff(sys.argv[2], sys.argv[3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
