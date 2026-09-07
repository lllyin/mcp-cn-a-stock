#!/usr/bin/env python3
"""装完之后的自检：确认跑起来的那份代码带得动它的参考数据。

为什么需要它
------------
装成包之后，服务跑的是 site-packages 里的副本，不是仓库。两者的差别本地永远
照不出来——本地 ``start.sh`` 走的是 ``python main.py``，包就在仓库里。

2026-09-06 就是这么炸的：``confs/`` 从来没进过包，装成包之后
``config.SH_INDICES`` 空掉，``SH000001`` 被"纠正"成 ``SZ000001``，上证指数的报告
里装的是平安银行的数据。不报错、不降级、整份都是另一只证券——比整段缺数据危险
得多，而且是靠人工比对基线才发现的。

这个脚本要在**装完、起服务之前**跑。它断言的两件事都是"错数据"的前置条件，
不是"少数据"：名单空了会取错标的，代码表空了会认错名字。

怎么跑
------
必须用 ``python scripts/postinstall_check.py``（脚本文件形式），且 CWD 是仓库根：

* 脚本文件形式让 ``sys.path[0]`` 变成 ``<repo>/scripts``，仓库根**不在**搜索路径上，
  ``import finmcp`` 只能命中 site-packages 里的副本——这正是要验的那一份。
  换成 ``python -c`` 或管道喂 stdin，``sys.path[0]`` 是 CWD，仓库里的 ``finmcp/``
  会把副本盖住，自检就成了假的。
* CWD 是仓库根，是为了和 ``start.sh`` 一致（它会 ``cd "$SCRIPT_DIR"``）。旧布局的
  分支用 CWD 相对路径读 ``confs/markets.json``，换个目录跑就会误报失败。
"""

import importlib
import sys


def main() -> int:
    package = sys.argv[1] if len(sys.argv) > 1 else "finmcp"

    try:
        pkg = importlib.import_module(package)
        config = importlib.import_module(f"{package}.config")
        symbols = importlib.import_module(f"{package}.symbols")
    except Exception as exc:  # 子包缺失也在这里露出来
        print(f"  ❌ 导入 {package} 失败 {type(exc).__name__}: {exc}")
        print("     多半是包没装全（显式 packages 列表不带子包）或没装上")
        return 1

    symbols.load_symbols()
    indices = getattr(config, "SH_INDICES", None) or ()
    codes = symbols.SYMBOLS_SHSZ

    print(f"  包路径     {pkg.__file__}")
    print(f"  指数名单   {len(indices)} 个")
    print(f"  代码表     {len(codes)} 条")

    problems = []
    if not indices:
        problems.append(
            "指数名单为空——confs/indices.json 没跟着装过去。"
            "SH000xxx 的沪深归属靠它判定，空名单会把上证指数报成平安银行"
        )
    if not codes:
        problems.append("代码表为空——confs/markets.json 没跟着装过去，标的名字会全空")

    for problem in problems:
        print(f"  ❌ {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
