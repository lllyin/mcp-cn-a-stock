#!/usr/bin/env bash

# 一次性安装：Python 依赖 + Chromium。
# 装完用 ./start.sh 启动、./stop.sh 停止——启动脚本不做安装，两件事分开。
#
#   ./install.sh          只装运行需要的
#   ./install.sh --dev    连测试依赖一起装（要改代码的用这个）
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WITH_DEV=0
for arg in "$@"; do
    case "$arg" in
        --dev) WITH_DEV=1 ;;
        -h|--help)
            sed -n '3,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: $arg（可用: --dev）"; exit 1 ;;
    esac
done

echo "=== 1/2 安装 Python 依赖 ==="
if command -v uv > /dev/null 2>&1; then
    if [ "$WITH_DEV" = "1" ]; then
        uv sync --extra dev
    else
        uv sync
    fi
else
    # 没有 uv 就用标准 venv + pip，装出来的结果一样，只是慢一些。
    if [ ! -f ".venv/bin/activate" ]; then
        echo "创建虚拟环境 .venv ..."
        python3 -m venv .venv
    fi
    .venv/bin/pip install --upgrade pip > /dev/null
    if [ "$WITH_DEV" = "1" ]; then
        .venv/bin/pip install '.[dev]'
    else
        .venv/bin/pip install .
    fi
fi

if [ ! -f ".venv/bin/activate" ]; then
    echo "❌ 虚拟环境创建失败，请检查上面的输出。"
    exit 1
fi
source ".venv/bin/activate"
echo "✅ 依赖安装完成"

echo
echo "=== 2/2 安装 Chromium ==="
# 探测走 Playwright 自己的路径解析，不去猜 ms-playwright 缓存目录：那个目录能被
# PLAYWRIGHT_BROWSERS_PATH 改掉，猜错的代价是每次安装都重下 150 MB。
chromium_status=0
python - > /dev/null 2>&1 <<'PY' || chromium_status=$?
import os, sys

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sys.exit(2)
try:
    with sync_playwright() as p:
        found = os.path.exists(p.chromium.executable_path)
except Exception:
    sys.exit(2)
sys.exit(0 if found else 1)
PY

if [ "$chromium_status" -eq 0 ]; then
    echo "✅ Chromium 已安装，跳过"
elif [ "$chromium_status" -eq 2 ]; then
    echo "⚠️  Playwright 不可用，跳过 Chromium 安装。"
    echo "    盘中资金流和 market_breadth 会回退到备用源，其余功能不受影响。"
else
    echo "正在下载 Chromium（约 150 MB）..."
    if [ "$(uname -s)" = "Linux" ]; then
        # Ubuntu 上还缺一批系统库，--with-deps 会一并装上；需要 root。
        if [ "$(id -u)" = "0" ]; then
            playwright install --with-deps chromium
        else
            playwright install chromium
            echo "提示: 无桌面的 Ubuntu 还需要系统依赖和 xvfb，缺了请执行"
            echo "      sudo \$(which playwright) install --with-deps chromium"
            echo "      sudo apt-get install -y xvfb"
        fi
    else
        playwright install chromium
    fi
    echo "✅ Chromium 安装完成"
fi

echo
echo "=== 自检 ==="
# 装完必须验一次"跑起来的那份代码能不能读到自己的参考数据"。装成包之后服务跑的
# 是 site-packages 里的副本，本地 `python main.py` 永远照不出差别——2026-09-06
# 曾经 confs/ 没跟着装过去，指数名单空掉，上证指数报成了平安银行，一路跑到
# 验证报告才被人工比对发现。这一步就是为了让它当场失败。
PKG="$(ls -d finmcp qtf_mcp 2>/dev/null | head -n 1)"
if [ -n "$PKG" ] && [ -f "scripts/postinstall_check.py" ]; then
    # 脚本文件形式，不要改成 python -c：那样 sys.path[0] 是 CWD，仓库里的包会把
    # site-packages 的副本盖住，自检就成了假的。
    if .venv/bin/python scripts/postinstall_check.py "$PKG"; then
        echo "✅ 自检通过"
    else
        echo "❌ 自检失败，**先别启动服务**——它会返回错数据而不是报错。"
        exit 1
    fi
fi

echo
echo "安装完成。启动服务："
echo "  ./start.sh"
