#!/usr/bin/env bash
# 逐个标的串行查，让每一次兜底都真的跑起来。
#
# 为什么要有这一版：4 批并发的口径下，16 个标的抢 1 个兜底名额（FALLBACK_CONCURRENCY=1，
# 等 0.5s 就放弃），11-15 个连页面都没打开过。那个数字量的是名额限制，不是取数成功率，
# 拿它比较版本或 headless/headful 是在比噪音。串行之后每个标的都独占名额，
# 分母才是"真的试了 16 次"。
#
# 用法：scripts/ab_fund_flow_serial.sh <标签> <HEADFUL> <KEEP_PAGES>
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LABEL="$1"; HEADFUL="$2"; KEEP_PAGES="$3"
OUT="/tmp/abs_${LABEL}"; rm -rf "$OUT"; mkdir -p "$OUT"

SYMBOLS=(
  SH600519 SZ000333 SZ300750 SH688981
  SH601318 SZ000651 SZ300059 SH688111
  SH600036 SZ002594 SZ300760 SH688008
  SH601899 SZ000858 SZ300124 SH512480
)

BACKUP=".env.ab-backup"
[ -f "$BACKUP" ] || cp .env "$BACKUP"
{
  grep -vE '^CN_STOCK_FUND_FLOW_PAGE_(HEADFUL|KEEP_PAGES)=|^CN_STOCK_REPORT_CACHE_ENABLED=' "$BACKUP"
  echo "CN_STOCK_FUND_FLOW_PAGE_HEADFUL=$HEADFUL"
  echo "CN_STOCK_FUND_FLOW_PAGE_KEEP_PAGES=$KEEP_PAGES"
  echo "CN_STOCK_REPORT_CACHE_ENABLED=0"
} > .env

./stop.sh >/dev/null 2>&1
sleep 1
: > logs/cn-stock-mcp.log
./start.sh >/dev/null 2>&1
sleep 8

export MCPORTER_CONFIG="${MCPORTER_CONFIG:-$HOME/.openclaw/workspace/config/mcporter.json}"
started=$(date +%s)
for symbol in "${SYMBOLS[@]}"; do
  mcporter call cn-stock brief "symbol=$symbol" \
    --config "$MCPORTER_CONFIG" --output text --timeout 120000 \
    > "$OUT/$symbol.json" 2>/dev/null
done
elapsed=$(( $(date +%s) - started ))
cp logs/cn-stock-mcp.log "$OUT/service.log"

python3 - "$OUT" "$LABEL" "$elapsed" <<'PY'
import json, pathlib, re, sys
out, label, elapsed = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
got, missing = [], []
for path in sorted(out.glob("*.json")):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        missing.append(path.stem + "(解析失败)")
        continue
    for symbol, report in (data.get("reports") or {}).items():
        (got if "净流入" in report else missing).append(symbol)
log = (out / "service.log").read_text(encoding="utf-8", errors="replace")
c = lambda p: len(re.findall(p, log))
print(json.dumps({
    "label": label, "elapsed_seconds": int(elapsed),
    "symbols": len(got) + len(missing), "with_fund_flow": len(got),
    "without": sorted(missing),
    "log": {
        "page_loads": c(r"Realtime fund flow page"),
        "reload": c(r"how=reload"),
        "ok": c(r"outcome=today="),
        "blocked": c(r"outcome=blocked "),
        "captcha": c(r"outcome=blocked_captcha"),
        "fallback_ok": c(r"资金流向页面兜底成功"),
        "skip_slot": c(r"兜底名额已满"),
        "skip_breaker": c(r"兜底跳过 \S+: 熔断器打开"),
        "breaker_open": c(r"Source breaker opened"),
    },
}, ensure_ascii=False))
PY
