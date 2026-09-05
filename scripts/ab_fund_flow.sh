#!/usr/bin/env bash
# 资金流向获取率的 A/B 测试：16 个沪深标的，4 批 × 4 个，并发发出。
#
# 为什么这么测：东财的风控是有状态的，几分钟前后的两次测量本来就不可比。所以每个
# 臂跑两轮、轮次交替（A B A B），把噪音摆出来，而不是拿单次结果下结论。
#
# 用法：scripts/ab_fund_flow.sh <轮次标签> <HEADFUL> <KEEP_PAGES>
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LABEL="$1"
HEADFUL="$2"
KEEP_PAGES="$3"
OUT="/tmp/ab_${LABEL}"
mkdir -p "$OUT"

BATCHES=(
  "SH600519,SZ000333,SZ300750,SH688981"
  "SH601318,SZ000651,SZ300059,SH688111"
  "SH600036,SZ002594,SZ300760,SH688008"
  "SH601899,SZ000858,SZ300124,SH512480"
)

# 备份只在不存在时创建：第一版每轮都覆盖它，跑到第二轮时"原始 .env"已经是上一轮
# 改过的了，中途一中断就永久丢掉了本机的调试开关。由 runner 负责最后还原并删除。
BACKUP=".env.ab-backup"
[ -f "$BACKUP" ] || cp .env "$BACKUP"

# 报告缓存会把第二轮直接命中掉，量不出取数成功率，所以整轮关掉。
{
  grep -vE '^BROWSER_(HEADFUL|KEEP_PAGES)=|^REPORT_CACHE_ENABLED=' "$BACKUP"
  echo "BROWSER_HEADFUL=$HEADFUL"
  echo "BROWSER_KEEP_PAGES=$KEEP_PAGES"
  echo "REPORT_CACHE_ENABLED=0"
} > .env

./stop.sh >/dev/null 2>&1
sleep 1
: > logs/cn-stock-mcp.log
./start.sh >/dev/null 2>&1
sleep 8

export MCPORTER_CONFIG="${MCPORTER_CONFIG:-$HOME/.openclaw/workspace/config/mcporter.json}"
started=$(date +%s)
for index in "${!BATCHES[@]}"; do
  mcporter call cn-stock brief "symbol=${BATCHES[$index]}" \
    --config "$MCPORTER_CONFIG" --output text --timeout 180000 \
    > "$OUT/batch$index.json" 2> "$OUT/batch$index.err" &
done
wait
elapsed=$(( $(date +%s) - started ))

cp logs/cn-stock-mcp.log "$OUT/service.log"

python3 - "$OUT" "$LABEL" "$elapsed" <<'PY'
import json, pathlib, re, sys

out, label, elapsed = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
got, missing, failed = [], [], []
for path in sorted(out.glob("batch*.json")):
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        failed.append(path.name)
        continue
    for symbol, report in (data.get("reports") or {}).items():
        (got if "净流入" in report else missing).append(symbol)

log = (out / "service.log").read_text(encoding="utf-8", errors="replace")
def count(pattern):
    return len(re.findall(pattern, log))

print(json.dumps({
    "label": label,
    "elapsed_seconds": int(elapsed),
    "symbols": len(got) + len(missing),
    "with_fund_flow": len(got),
    "without": sorted(missing),
    "failed_batches": failed,
    "log": {
        "new_tab": count(r"how=new_tab"),
        "reload": count(r"how=reload"),
        "outcome_ok": count(r"outcome=today="),
        "blocked": count(r"outcome=blocked "),
        "blocked_captcha": count(r"outcome=blocked_captcha"),
        "fallback_ok": count(r"资金流向页面兜底成功"),
        "fallback_skip": count(r"资金流向页面兜底跳过"),
        "fallback_fail": count(r"资金流向页面兜底失败"),
        "breaker_open": count(r"Source breaker opened"),
    },
}, ensure_ascii=False))
PY
