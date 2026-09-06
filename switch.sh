#!/usr/bin/env bash

# 切换分支并完整重装。
#
#     ./switch.sh main
#     ./switch.sh feat/v2.0.0
#
# 为什么需要这个脚本：装成包之后，**跑的是 site-packages 里的副本，不是仓库**。
# `git checkout` 只改仓库，不改正在跑的东西——不重装等于没切。
#
# 而重装必须先清干净。2026-09-06 踩过两次：
#   1. build/lib 是暂存目录、构建之间不清理，把 4 月的 qtf_mcp 打进了轮子；
#   2. 改包名之后不卸旧的，finmcp/ 和 qtf_mcp/ 在 site-packages 里共存，
#      main.py 被后装的那个覆盖，手上是个新旧混合体。
#
# 装法按分支定，不是随便选的：main 的 pyproject 写的是 packages = ["qtf_mcp"]
# （显式列表不带子包）且 confs/ 在仓库根不在包里，**非 editable 装必炸**。
# 判据是仓库里有没有 <包>/confs——有就说明打包缺陷已修，可以按发布形态装。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    echo "用法: ./switch.sh <分支名>"
    echo "当前: $(git rev-parse --abbrev-ref HEAD)"
    exit 1
fi

if ! git rev-parse --verify --quiet "$TARGET" > /dev/null; then
    echo "❌ 分支不存在: $TARGET"
    exit 1
fi

# 脏工作区直接拒绝。切分支会重装，重装会删 site-packages 里的东西；
# 这时候再丢掉未提交的改动，两件事叠在一起没法收拾。
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "❌ 工作区有未提交的改动，先处理掉再切："
    git status --short --untracked-files=no
    exit 1
fi

PY="$SCRIPT_DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "❌ 虚拟环境不存在，先运行 ./install.sh"; exit 1; }
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"

echo "=== 1/5 停服务 ==="
./stop.sh || true

echo "=== 2/5 切到 $TARGET ==="
git checkout "$TARGET"
git pull --ff-only 2>/dev/null || echo "（没有上游或不能快进，跳过 pull）"

echo "=== 3/5 清干净 ==="
# 不走 pip uninstall：uv 建的 venv 里常常没有 pip（实测 `python -m pip` 直接
# ModuleNotFoundError）。按路径删更可靠，也不依赖 RECORD 文件是否完整。
rm -rf build/ dist/ ./*.egg-info
for leftover in finmcp qtf_mcp main.py data.py; do
    rm -rf "${SITE:?}/$leftover"
done
rm -rf "${SITE:?}"/cn_stock_mcp-*.dist-info \
       "${SITE:?}"/__editable__*cn_stock_mcp* \
       "${SITE:?}"/__pycache__/main.*.pyc
echo "已清理 $SITE 下的旧安装"

echo "=== 4/5 安装 ==="
PKG="$(ls -d finmcp qtf_mcp 2>/dev/null | head -n 1)"
[ -n "$PKG" ] || { echo "❌ 仓库里既没有 finmcp/ 也没有 qtf_mcp/"; exit 1; }

if [ -d "$PKG/confs" ]; then
    echo "包内带 confs，按发布形态装（非 editable）"
    ./install.sh
else
    echo "包内没有 confs（$PKG 是旧布局），只能 editable 装——"
    echo "非 editable 会缺子包、且读不到 confs/indices.json"
    if command -v uv > /dev/null 2>&1; then
        uv pip install -e .
    else
        "$PY" -m pip install -e .
    fi
fi

echo "=== 5/5 装完自检 ==="
# 用脚本文件形式跑，别改成 python -c：脚本文件让 sys.path[0] 变成 <repo>/scripts，
# 仓库根不在搜索路径上，验的才是 site-packages 里那份副本。详见脚本内的注释。
"$PY" scripts/postinstall_check.py "$PKG" \
    || { echo "❌ 自检失败，**不要启动服务**"; exit 1; }
echo "✅ 自检通过"

echo
echo "=== 启动 ==="
./start.sh
