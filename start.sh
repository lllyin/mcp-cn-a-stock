#!/usr/bin/env bash

# A股数据 MCP 服务启动脚本
# 日志目录: /var/log/cn-stock-mcp/

set -e

# 配置
APP_NAME="cn-stock-mcp"
LOG_DIR="/var/log/cn-stock-mcp"
LOG_FILE="$LOG_DIR/cn-stock-mcp.log"
PID_FILE="/tmp/cn-stock-mcp.pid"
PORT=8686

# 创建日志目录
mkdir -p "$LOG_DIR"

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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

# 启动服务
echo "正在启动 $APP_NAME 服务..."
echo "日志文件: $LOG_FILE"

nohup cn-stock-mcp --transport http > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$PID_FILE"

# 等待服务启动
sleep 2

# 检查启动状态
if ps -p "$PID" > /dev/null 2>&1; then
    echo "✅ 服务启动成功!"
    echo "   PID: $PID"
    echo "   访问地址: http://localhost:$PORT/cnstock/mcp"
    echo "   查看日志: tail -f $LOG_FILE"
    echo "   停止服务: kill $PID"
else
    echo "❌ 服务启动失败，请检查日志: $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
