#!/usr/bin/env bash

# A股数据 MCP 服务停止脚本

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/cn-stock-mcp.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "正在停止服务 (PID: $PID)..."
        kill "$PID"
        sleep 1
        
        # 等待进程结束
        for _ in {1..10}; do
            if ! ps -p "$PID" > /dev/null 2>&1; then
                echo "✅ 服务已停止"
                rm -f "$PID_FILE"
                exit 0
            fi
            sleep 1
        done
        
        # 强制终止
        echo "强制终止进程..."
        kill -9 "$PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "✅ 服务已强制停止"
    else
        echo "服务未运行"
        rm -f "$PID_FILE"
    fi
else
    echo "未找到 PID 文件，尝试查找进程..."
    
    # 尽可能精准地匹配进程参数，避免误杀无关进程和其他无关同名字符串相关进程
    PIDS=$( (pgrep -f "cn-stock-mcp"; pgrep -f "main.py --transport http") 2>/dev/null | sort -u || true )
    
    if [ -n "$PIDS" ]; then
        echo "找到遗留进程，正在停止..."
        for PID in $PIDS; do
            # 排除当前脚本进程本身及当前进程父进程，防止误杀
            if [ "$PID" != "$$" ] && [ "$PID" != "$PPID" ]; then
                kill "$PID" 2>/dev/null || true
            fi
        done
        echo "✅ 服务已尝试停止所有匹配的进程"
    else
        echo "服务未运行"
    fi
fi
