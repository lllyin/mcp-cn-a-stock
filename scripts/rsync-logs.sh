#!/usr/bin/env bash

# 把远端环境的服务日志和发布验证报告同步到当前环境，供 health / 排查 / 回放使用。
#
#     bash scripts/rsync-logs.sh --server root@HOST
#     bash scripts/rsync-logs.sh --server root@HOST --mirror     # 与远端完全一致
#     bash scripts/rsync-logs.sh --server root@HOST --dry-run
#
# ## 为什么落到 .server-logs/ 而不是仓库自己的 logs/
#
# 仓库根的 logs/ 是**当前环境的服务正在写**的目录：当前日志、start.sh 归档的历次日志，
# health 工具直接读它算可用率和耗时。把远端同步到那里，当前环境的运行记录就被远端的
# 覆盖了，而两者是不同机器的数据——混在一起之后 health 算出来的东西没有意义。
# 所以远端的东西一律进 .server-logs/，和当前环境的分开放。
#
# ## 为什么默认不删（和参考脚本不同）
#
# 远端的 LOG_RETENTION_DAYS 默认 3 天，超过就清掉。用 --delete 做镜像的话，
# 每同步一次就把本地攒下的、远端已经轮转掉的旧日志一起删了——那正好抵消了
# "把日志同步下来分析"这件事本身。所以默认只增不删；确实要一份和远端逐字相同的
# 快照时再加 --mirror。
#
# ## SSH 连接复用
#
# 两个目录共用一条 ControlMaster 连接，省掉第二次握手；退出时显式关掉，
# 不给后台留一条悬着的连接。

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)

SERVER="${OPENCLAW_SERVER:-}"
# 远端仓库目录。**没有静默默认**：猜错了会静默同步到另一个项目的目录，
# 拿到一份看着正常、其实来自别处的日志。必须显式给，或者设环境变量。
REMOTE_DIR="${CN_STOCK_REMOTE_DIR:-}"
DELETE=0
DRY_RUN=0

usage() {
    cat <<'EOF'
用法:
  bash scripts/rsync-logs.sh --server root@HOST [--remote-dir PATH] [--mirror] [--dry-run]

参数:
  --server SERVER      远端地址，形如 root@10.0.0.1 或 ssh config 里的别名
  --remote-dir PATH    远端仓库根目录（含 logs/ 和 verification/reports/）
  --mirror             与远端保持一致：远端没有的本地也删。默认只增不删
  --dry-run            只列出会传哪些文件，不真的传

环境变量:
  OPENCLAW_SERVER      --server 的默认值
  CN_STOCK_REMOTE_DIR  --remote-dir 的默认值

同步到:
  <仓库根>/.server-logs/logs/           远端的服务日志（含 start.sh 的历次归档）
  <仓库根>/.server-logs/reports/        远端的 verification/reports/
  <仓库根>/.server-logs/probe-tuning/   远端的探针调优结果（facts/browser/推荐值）

  probe-tuning 只同步分析产物，缓存目录（arm-*/cache、verify-cache）与凭据文件
  （.runtime/eastmoney-auth.json）明确排除：前者大且离开那台机器无意义，后者
  是可用凭据，不该离开它所属的环境。
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server)
            [ $# -ge 2 ] || { echo "缺少 --server 的值。" >&2; exit 2; }
            SERVER="$2"; shift 2 ;;
        --remote-dir)
            [ $# -ge 2 ] || { echo "缺少 --remote-dir 的值。" >&2; exit 2; }
            REMOTE_DIR="$2"; shift 2 ;;
        --mirror) DELETE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$SERVER" ]; then
    echo "缺少服务器地址：--server root@HOST，或设 OPENCLAW_SERVER。" >&2
    exit 2
fi
if [ -z "$REMOTE_DIR" ]; then
    echo "缺少远端仓库目录：--remote-dir /path/to/mcp-cn-a-stock，或设 CN_STOCK_REMOTE_DIR。" >&2
    echo "（不提供默认值：猜错路径会静默同步到别的项目，拿到一份来源不对的日志。）" >&2
    exit 2
fi
command -v rsync >/dev/null 2>&1 || { echo "未找到 rsync，请先安装。" >&2; exit 1; }

REMOTE_DIR="${REMOTE_DIR%/}"
LOCAL_ROOT="$REPO_DIR/.server-logs"

CONTROL_DIR=$(mktemp -d "/tmp/cn-stock-rsync-logs.XXXXXX")
CONTROL_PATH="$CONTROL_DIR/control-%C"
# ConnectTimeout 不能省：地址写错或机器不在时，默认会挂到 TCP 自己超时（分钟级），
# 而这个脚本多半是手动跑的，那时候人只会以为它卡住了。
SSH_COMMAND="ssh -o ControlMaster=auto -o ControlPersist=60 -o ConnectTimeout=10 -o ControlPath=$CONTROL_PATH"

cleanup() {
    ssh -o "ControlPath=$CONTROL_PATH" -O exit "$SERVER" >/dev/null 2>&1 || true
    rm -rf -- "$CONTROL_DIR"
}
trap cleanup EXIT

sync_directory() {
    local label="$1" remote_dir="$2" local_dir="$3" excludes="${4:-}"
    mkdir -p "$local_dir"

    local -a opts=(-avz --human-readable --partial)
    local ex
    for ex in $excludes; do opts+=(--exclude="$ex"); done
    [ "$DELETE" -eq 1 ] && opts+=(--delete)
    [ "$DRY_RUN" -eq 1 ] && opts+=(--dry-run)

    echo "------------------------------------------"
    echo "同步: $label"
    echo "  远端: $SERVER:$remote_dir"
    echo "  本地: $local_dir"

    # 一个目录失败不该让整个脚本挂掉——另一个还能同步。但**要说清是哪一级失败的**，
    # 否则"连不上机器"和"机器上没这个目录"会被同一句话盖住，排查方向正好相反。
    local code=0
    rsync "${opts[@]}" -e "$SSH_COMMAND" "$SERVER:$remote_dir" "$local_dir" || code=$?
    if [ "$code" -eq 0 ]; then
        echo "  ✅ 完成"
        return 0
    fi
    # 变量一律用 ${} 界定：紧跟其后的是全角标点时，不加花括号 bash 会把多字节字符
    # 的首字节当成变量名的一部分，配上 set -u 就是 "unbound variable" 当场中断。
    case "$code" in
        255|127) echo "  ⚠️ 连不上 ${SERVER}（SSH 层失败）——查地址、密钥、网络" >&2 ;;
        23|24)   echo "  ⚠️ 远端没有 ${remote_dir}，或部分文件读不到——查路径和权限" >&2 ;;
        *)       echo "  ⚠️ rsync 退出码 ${code}" >&2 ;;
    esac
    return 1
}

echo "=========================================="
echo "同步远端的日志与验证报告"
echo "=========================================="
echo "服务器:   $SERVER"
echo "远端仓库: $REMOTE_DIR"
echo "落到:     $LOCAL_ROOT/"
echo "策略:     $([ "$DELETE" -eq 1 ] && echo '镜像（--delete，远端没有的本地也删）' || echo '只增不删（默认）')"
[ "$DRY_RUN" -eq 1 ] && echo "模式:     dry-run，不会真的传"
echo ""

failed=0
sync_directory "服务日志" "$REMOTE_DIR/logs/" "$LOCAL_ROOT/logs/" || failed=$((failed + 1))
echo ""
sync_directory "验证报告" "$REMOTE_DIR/verification/reports/" "$LOCAL_ROOT/reports/" || failed=$((failed + 1))
echo ""
sync_directory "探针调优结果" "$REMOTE_DIR/.runtime/probe-tuning/" "$LOCAL_ROOT/probe-tuning/" \
    "cache verify-cache *.lock" || failed=$((failed + 1))

echo ""
if [ "$failed" -gt 0 ]; then
    echo "有 $failed 个目录没同步成功，见上面的警告。"
    exit 1
fi
echo "全部同步完成：$LOCAL_ROOT/"
echo ""
echo "拿远端日志渲染一份 health 报告（health 默认读当前环境的日志，这里显式指过去）："
cat <<EOF
  ./.venv/bin/python -c "
from finmcp import health_report, log_digest
print(health_report.render(log_digest.digest(
    '$LOCAL_ROOT/logs/cn-stock-mcp.log', since='startup')))"
EOF
