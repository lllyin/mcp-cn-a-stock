#!/usr/bin/env bash
# 在东财能通的机器（部署服务器）上采一份同刻数据，用来确定备用源的口径。
#
# 为什么必须在服务器上采：本机的东财 push2 `f=` 端点被封，ef.stock.get_base_info
# 全程 JSONDecodeError，所以总市值/流通市值/市净率/市盈率(动) 这一组根本取不到，
# 没法和腾讯并排。而口径对不上是换源最大的风险——数字会在两次查询之间跳变。
#
# 同刻是关键：价格一直在动，隔几分钟采的两份数据算出来的市值和 PE 都没法比。
# 所以腾讯那份在 mcporter 调用前后各抓一次，用前后两次把价格漂移夹住。
#
# 只读，不改任何服务状态，不动 .env，不重启服务。
#
# 用法（在服务器的项目根目录下）：
#   bash scripts/collect_caliber_data.sh
# 跑完会打印一个 .tgz 路径，scp 回来即可。

set -u
cd "$(cd "$(dirname "$0")/.." && pwd)"

STAMP=$(date +%Y%m%d_%H%M%S)
OUT="/tmp/cn-stock-caliber-$STAMP"
mkdir -p "$OUT"

export MCPORTER_CONFIG="${MCPORTER_CONFIG:-/root/.openclaw/workspace/config/mcporter.json}"
[ -f "$MCPORTER_CONFIG" ] || export MCPORTER_CONFIG="$HOME/.openclaw/workspace/config/mcporter.json"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# 每类各挑几个，因为口径差异往往只在某一类上暴露：
#   全流通 / 有大量非流通 / A+H / 科创板（总市值远大于流通市值）
#   ETF、指数（没有市盈率市净率）、北交所、极高市盈率（分母接近 0 时口径最敏感）
BATCHES=(
  "SH600519,SH601318,SZ000333,SZ000651"
  "SZ300750,SZ300308,SH688981,SH688012"
  "SH600000,SZ002371,SH600118,SH600316"
  "SH512480,SZ159995,BJ920021,SH603986"
  "SH000001,SZ399001,SZ399006,SH000688"
)
ALL_TENCENT="sh600519,sh601318,sz000333,sz000651,sz300750,sz300308,sh688981,sh688012,\
sh600000,sz002371,sh600118,sh600316,sh512480,sz159995,bj920021,sh603986,\
sh000001,sz399001,sz399006,sh000688"
STOCKS="600519 601318 000333 000651 300750 300308 688981 688012 600000 002371 600118 600316 603986 512480"

echo "[1/6] 腾讯长表（调用前）"
curl -s --max-time 15 "http://qt.gtimg.cn/q=$ALL_TENCENT" > "$OUT/tencent_before.txt"
date +%s > "$OUT/tencent_before.ts"

echo "[2/6] 服务返回（东财路径）"
for index in "${!BATCHES[@]}"; do
  mcporter call cn-stock brief "symbol=${BATCHES[$index]}" \
    --config "$MCPORTER_CONFIG" --output text --timeout 180000 \
    > "$OUT/brief_$index.json" 2> "$OUT/brief_$index.err"
  echo "      批 $index 完成"
done
# full 只要一个标的，用来看财务表和历史资金流在服务器上是什么样
mcporter call cn-stock full symbol=SH600519 fund_flow_limit=60 \
  --config "$MCPORTER_CONFIG" --output text --timeout 180000 \
  > "$OUT/full_SH600519.json" 2>&1
mcporter call cn-stock medium symbol=SH600519 \
  --config "$MCPORTER_CONFIG" --output text --timeout 120000 \
  > "$OUT/medium_SH600519.json" 2>&1
mcporter call cn-stock tech symbol=SH600519 days=30 \
  --config "$MCPORTER_CONFIG" --output text --timeout 120000 \
  > "$OUT/tech_SH600519.json" 2>&1

echo "[3/6] 腾讯长表（调用后，用来夹住价格漂移）"
curl -s --max-time 15 "http://qt.gtimg.cn/q=$ALL_TENCENT" > "$OUT/tencent_after.txt"
date +%s > "$OUT/tencent_after.ts"

echo "[4/6] 东财原始值（口径的 ground truth，绕过渲染层的四舍五入）"
"$PY" - "$OUT" $STOCKS <<'PYEOF' > "$OUT/eastmoney_raw.log" 2>&1
import json, os, sys
os.environ["TQDM_DISABLE"] = "1"
import efinance as ef

out, codes = sys.argv[1], sys.argv[2:]
dump = {}
for code in codes:
    entry = {}
    for name, call in (("base_info", ef.stock.get_base_info),
                       ("quote_snapshot", ef.stock.get_quote_snapshot)):
        try:
            series = call(code)
            entry[name] = (None if series is None or series.empty
                           else {str(k): (None if v != v else v) for k, v in series.items()})
        except Exception as exc:
            entry[name] = {"__error__": f"{type(exc).__name__}: {exc}"}
    try:
        frame = ef.stock.get_belong_board(code)
        entry["belong_board"] = (None if frame is None or frame.empty
                                 else frame.to_dict("records"))
    except Exception as exc:
        entry["belong_board"] = {"__error__": f"{type(exc).__name__}: {exc}"}
    dump[code] = entry
    print(f"  {code} 完成")
with open(f"{out}/eastmoney_raw.json", "w", encoding="utf-8") as handle:
    json.dump(dump, handle, ensure_ascii=False, indent=2, default=str)
PYEOF

echo "[5/6] 同花顺 F10 概念页（行业概念的备用源，看板块体系差多少）"
for code in 600519 000333 688981 512480; do
  curl -s --max-time 15 -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36" \
    "https://basic.10jqka.com.cn/$code/concept.html" > "$OUT/ths_concept_$code.html"
done

echo "[6/6] 服务日志窗口与环境"
tail -600 logs/cn-stock-mcp.log > "$OUT/service.log" 2>/dev/null
{
  echo "date=$(date -Iseconds)"
  echo "host=$(hostname)"
  echo "python=$($PY -V 2>&1)"
  grep -hE '^(CN_STOCK|AKSHARE)' .env 2>/dev/null | sed -E 's/(TOKEN|PASSWORD)=.*/\1=<已脱敏>/'
} > "$OUT/env.txt"

tar czf "$OUT.tgz" -C /tmp "$(basename "$OUT")"
echo
echo "采完了：$OUT.tgz  ($(du -h "$OUT.tgz" | cut -f1))"
echo "scp 回来就行。里面没有令牌和密码。"
