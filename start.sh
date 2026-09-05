#!/usr/bin/env bash

# A股数据 MCP 服务启动脚本
set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 配置
APP_NAME="cn-stock-mcp"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/cn-stock-mcp.log"
PID_FILE="$SCRIPT_DIR/cn-stock-mcp.pid"
XVFB_PID_FILE="$SCRIPT_DIR/cn-stock-mcp-xvfb.pid"
XVFB_LOG_FILE="$LOG_DIR/cn-stock-mcp-xvfb.log"
PORT=8686

# 创建日志目录
mkdir -p "$LOG_DIR"

# .env 是给 Python 进程读的（入口 load_dotenv(override=True)），但 XVFB_* 是本脚本
# 自己在用，写进 .env 就成了静默失效的配置。所以这里按 key 单独取一次。
# 不用 `source .env`：那会把文件里任意一行当命令执行，一行手滑就能改掉 PATH。
env_file_value() {
    [ -f "$SCRIPT_DIR/.env" ] || return 0
    sed -n "s/^[[:space:]]*$1=//p" "$SCRIPT_DIR/.env" \
        | tail -n 1 \
        | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# 取一项配置。配置名不带前缀写，前缀由 ENV_PREFIX 决定——和 Python 侧的
# qtf_mcp.config.env() 同一套规则，两边不能各认各的。
# .env 优先于 shell 环境变量，也是为了和 Python 侧一致：入口是
# load_dotenv(override=True)。同一个文件在两处按相反的优先级解释，迟早坑人。
conf() {
    local prefix key value
    prefix="$(env_file_value ENV_PREFIX)"
    prefix="${prefix:-${ENV_PREFIX:-}}"
    key="${prefix}$1"
    value="$(env_file_value "$key")"
    [ -n "$value" ] || value="${!key:-}"
    printf '%s' "$value"
}

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
else
    echo "错误: 虚拟环境不存在，请先运行: python3 -m venv .venv"
    exit 1
fi

# 检查是否已在运行
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "服务已在运行 (PID: $PID)"
        echo "访问地址: http://localhost:$PORT/cnstock/mcp"
        echo "查看日志: tail -f $LOG_FILE"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

cleanup_stale_xvfb_pid() {
    if [ -f "$XVFB_PID_FILE" ]; then
        local xvfb_pid xvfb_display xvfb_command
        read -r xvfb_pid xvfb_display < "$XVFB_PID_FILE" || true
        xvfb_command=$(ps -p "$xvfb_pid" -o args= 2>/dev/null || true)
        if [ -z "$xvfb_display" ] || [[ "$xvfb_command" != *"Xvfb $xvfb_display "* ]]; then
            rm -f "$XVFB_PID_FILE"
        fi
    fi
}

stop_managed_xvfb() {
    if [ -f "$XVFB_PID_FILE" ]; then
        local xvfb_pid xvfb_display xvfb_command
        read -r xvfb_pid xvfb_display < "$XVFB_PID_FILE" || true
        xvfb_command=$(ps -p "$xvfb_pid" -o args= 2>/dev/null || true)
        if [ -n "$xvfb_display" ] && [[ "$xvfb_command" == *"Xvfb $xvfb_display "* ]]; then
            kill "$xvfb_pid" 2>/dev/null || true
        fi
        rm -f "$XVFB_PID_FILE"
    fi
}

start_xvfb_if_needed() {
    [ "$(uname -s)" = "Linux" ] || return 0
    [ -z "${DISPLAY:-}" ] || return 0

    cleanup_stale_xvfb_pid
    if [ -f "$XVFB_PID_FILE" ]; then
        local existing_xvfb_pid existing_display
        read -r existing_xvfb_pid existing_display < "$XVFB_PID_FILE"
        export DISPLAY="$existing_display"
        echo "复用项目虚拟显示 $DISPLAY (PID: $existing_xvfb_pid)"
        return 0
    fi
    if ! command -v Xvfb > /dev/null 2>&1; then
        echo "警告: 未检测到 DISPLAY 或 Xvfb；同花顺数据源不可用时将自动回退到 efinance。"
        return 0
    fi

    local display_number display socket_path xvfb_pid screen
    display_number="$(conf XVFB_DISPLAY_NUMBER)"
    display_number="${display_number:-99}"
    if ! [[ "$display_number" =~ ^[0-9]+$ ]]; then
        echo "警告: XVFB_DISPLAY_NUMBER 必须是数字；将使用默认显示号 99。"
        display_number=99
    fi
    while [ "$display_number" -le 109 ]; do
        socket_path="/tmp/.X11-unix/X${display_number}"
        if [ ! -e "$socket_path" ]; then
            break
        fi
        display_number=$((display_number + 1))
    done
    if [ "$display_number" -gt 109 ]; then
        echo "警告: :99-:109 均被占用；同花顺数据源不可用时将自动回退到 efinance。"
        return 0
    fi

    display=":${display_number}"
    screen="$(conf XVFB_SCREEN)"
    Xvfb "$display" -screen 0 "${screen:-1920x1080x24}" -nolisten tcp \
        > "$XVFB_LOG_FILE" 2>&1 &
    xvfb_pid=$!
    echo "$xvfb_pid $display" > "$XVFB_PID_FILE"
    sleep 1
    if ! ps -p "$xvfb_pid" > /dev/null 2>&1; then
        echo "警告: Xvfb 启动失败；详情见 $XVFB_LOG_FILE。服务将使用 efinance 备用源。"
        rm -f "$XVFB_PID_FILE"
        return 0
    fi

    export DISPLAY="$display"
    echo "已启动虚拟显示 $DISPLAY (PID: $xvfb_pid)"
}

start_xvfb_if_needed

# 启动服务
echo "正在启动 $APP_NAME 服务..."
echo "日志文件: $LOG_FILE"

# 如果未通过 pip install -e . 安装，则直接使用 python main.py
if command -v cn-stock-mcp >/dev/null 2>&1; then
    EXEC_CMD=(cn-stock-mcp)
else
    EXEC_CMD=(python main.py)
fi

nohup "${EXEC_CMD[@]}" --transport http --port "$PORT" > "$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

# 等待服务启动
sleep 2

# 检查启动状态
if ps -p "$PID" > /dev/null 2>&1; then
    echo "✅ 服务启动成功!"
    echo "   PID: $PID"
    echo "   访问地址: http://localhost:$PORT/cnstock/mcp"
    echo "   查看日志: tail -f $LOG_FILE"
    echo "   停止服务: ./stop.sh"
else
    echo "❌ 服务启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    stop_managed_xvfb
    exit 1
fi
