#!/usr/bin/env bash

# A股数据 MCP 服务停止脚本

PID_FILE="/tmp/cn-stock-mcp.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "正在停止服务 (PID: $PID)..."
        kill "$PID"
        sleep 1
        
        # 等待进程结束
        for i in {1..10}; do
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
    PID=$(pgrep -f "cn-stock-mcp" || true)
    if [ -n "$PID" ]; then
        echo "找到进程 $PID，正在停止..."
        kill "$PID"
        echo "✅ 服务已停止"
    else
        echo "服务未运行"
    fi
fi
