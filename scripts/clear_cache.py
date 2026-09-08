#!/usr/bin/env python3
"""丢掉磁盘缓存里的纪元目录。给 ``./start.sh --clear-cache`` 用。

## 什么时候需要它

**代码没变、但缓存里的数是坏的**。上游某一次给了个错值，正好在那个纪元里被写进
磁盘层，之后重启也带不走它——磁盘层是跨重启的，而闭市纪元没有 TTL，周五傍晚写
进去的错值能一路服务到周一开盘（64 小时）。

部署后**不需要**加这个参数。渲染指纹（``finmcp.cache._render_fingerprint``）哈希
整个包，代码一变缓存键就跟着变，部署前写的条目自动够不着。而且指纹是内容寻址不是
删除：回滚到旧版本时指纹变回旧值，那批条目在保留期内还能继续用，手动清过就没了。

## 只删 ``epoch-`` 开头的目录

和 ``Cache._sweep_disk`` 同一条规则，理由也一样：``CACHE_DIR`` 是可配置的，有人把
它指到一个已经有别的东西的目录时，这个脚本不该毁掉那些东西。所以不是
``rm -rf $CACHE_DIR``，而是逐个 namespace 进去、只删缓存自己建的那些目录。
"""

from __future__ import annotations

import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def import_finmcp():
    """按**服务自己**的方式解析 ``finmcp``，否则清的是另一个目录。

    ``CACHE_DIR`` 是相对 ``finmcp/config.py`` 的位置算的（见那里的 ``_PROJECT_ROOT``），
    所以"仓库那份"和"site-packages 那份"指向两个**不同的** ``.runtime/cache``。而
    ``start.sh`` 起服务时的分支是：装了 ``cn-stock-mcp`` 就跑它（用 site-packages 那份），
    没装才 ``python main.py``（用仓库那份）。

    这里必须照抄同一个优先级。踩过：一开始在模块顶上写死 ``sys.path.insert(REPO_ROOT)``，
    发布形态的部署上就会去清仓库里那个**空目录**，然后打印"没有可清的纪元目录"——
    看着像成功，服务真正在用的那份一个字节没动。

    ``python scripts/clear_cache.py`` 的 ``sys.path[0]`` 是 ``scripts/``，仓库根本来就
    不在搜索路径上，所以第一次 import 只可能找到装好的那份；找不到才退回仓库。
    """
    try:
        import finmcp  # noqa: F401
    except ModuleNotFoundError:
        sys.path.insert(0, REPO_ROOT)
        import finmcp  # noqa: F401
    return finmcp


def clear(root: str, prefix: str) -> tuple[int, int]:
    """删掉 ``root`` 下每个 namespace 里的纪元目录，返回（目录数，字节数）。"""
    removed = freed = 0
    try:
        namespaces = sorted(os.listdir(root))
    except FileNotFoundError:
        return 0, 0
    for namespace in namespaces:
        ns_dir = os.path.join(root, namespace)
        if not os.path.isdir(ns_dir):
            continue
        for name in sorted(os.listdir(ns_dir)):
            if not name.startswith(prefix):
                continue
            path = os.path.join(ns_dir, name)
            if not os.path.isdir(path):
                continue
            for base, _dirs, files in os.walk(path):
                for entry in files:
                    try:
                        freed += os.path.getsize(os.path.join(base, entry))
                    except OSError:
                        pass
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            print(f"  删除 {namespace}/{name}")
    return removed, freed


def main() -> int:
    finmcp = import_finmcp()
    from finmcp.cache import EPOCH_DIR_PREFIX
    from finmcp.config import CACHE_DIR

    # 两行都要打出来。清错目录时唯一能看出来的就是这里——"没有可清的纪元目录"
    # 本身既可能是真的干净，也可能是清到了另一棵树上。
    print(f"finmcp  : {finmcp.__file__}")
    print(f"缓存目录: {CACHE_DIR}")
    removed, freed = clear(CACHE_DIR, EPOCH_DIR_PREFIX)
    if removed:
        print(f"✅ 清掉 {removed} 个纪元目录，释放 {freed / 1024 / 1024:.1f} MiB")
    else:
        print("（没有可清的纪元目录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
