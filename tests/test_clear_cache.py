"""``./start.sh --clear-cache`` 背后那个脚本。

它是**破坏性**的，所以测试的重点不是"删干净了没有"，而是"该留的有没有留下"：
``CACHE_DIR`` 可以配置到任意目录，指到一个已经有别的东西的目录时，这个脚本不该
毁掉那些东西。规则和 ``Cache._sweep_disk`` 一致——只删 ``epoch-`` 开头的目录。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

import clear_cache  # noqa: E402


def _epoch(root, namespace, epoch, size=1024):
    path = root / namespace / epoch
    path.mkdir(parents=True)
    (path / "abc123.json").write_text("x" * size, encoding="utf-8")
    return path


def test_removes_epoch_dirs_across_namespaces(tmp_path):
    _epoch(tmp_path, "report", "epoch-closed-2026-09-07")
    _epoch(tmp_path, "fund_flow", "epoch-live-2026-09-08")
    removed, freed = clear_cache.clear(str(tmp_path), "epoch-")
    assert removed == 2
    assert freed == 2048
    assert not (tmp_path / "report" / "epoch-closed-2026-09-07").exists()
    assert not (tmp_path / "fund_flow" / "epoch-live-2026-09-08").exists()


def test_keeps_everything_that_is_not_an_epoch_dir(tmp_path):
    """指到一个有别人东西的目录时，不许毁掉那些东西。"""
    _epoch(tmp_path, "report", "epoch-closed-2026-09-07")
    (tmp_path / "report" / "别人的目录").mkdir(parents=True)
    (tmp_path / "report" / "别人的目录" / "重要.txt").write_text("留着", encoding="utf-8")
    (tmp_path / "顶层文件.txt").write_text("也留着", encoding="utf-8")

    clear_cache.clear(str(tmp_path), "epoch-")

    assert (tmp_path / "report" / "别人的目录" / "重要.txt").read_text(encoding="utf-8") == "留着"
    assert (tmp_path / "顶层文件.txt").read_text(encoding="utf-8") == "也留着"


def test_missing_cache_dir_is_not_an_error(tmp_path):
    """第一次启动时缓存目录还不存在——那不是错误，别让 start.sh 退出。"""
    assert clear_cache.clear(str(tmp_path / "还没建"), "epoch-") == (0, 0)


def test_uses_the_same_prefix_as_the_sweeper():
    """两处用同一个常量。分叉了就会出现"扫得掉但清不掉"的目录。"""
    from finmcp.cache import EPOCH_DIR_PREFIX

    assert EPOCH_DIR_PREFIX == "epoch-"


def test_start_sh_stops_when_clearing_fails():
    """清缓存失败必须拦住启动。

    带着一份说不清状态的缓存跑，比不启动更难查——报告里的数对不上时，你不知道
    是代码的问题还是那半份没清掉的缓存。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = open(os.path.join(here, "start.sh"), encoding="utf-8").read()
    assert "scripts/clear_cache.py" in script
    clause = script.split("scripts/clear_cache.py", 1)[1][:200]
    assert "exit 1" in clause


@pytest.mark.parametrize("flag", ["--clear-cache", "-h", "--help"])
def test_start_sh_recognises_the_flags(flag):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = open(os.path.join(here, "start.sh"), encoding="utf-8").read()
    assert flag in script


def _run_start(*args):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        ["bash", os.path.join(here, "start.sh"), *args],
        capture_output=True, text=True, cwd=here, timeout=60,
    )


def test_unknown_flag_is_rejected():
    """手滑打错一个字母不能被忽略。

    忽略的话服务照常起来、缓存一个字节没动，而人以为清过了——比不启动难查得多。
    """
    done = _run_start("--clear-cahce")
    assert done.returncode == 1
    assert "不认识的参数" in done.stdout + done.stderr


def test_help_needs_no_venv_and_exits_clean():
    """``--help`` 要在检查虚拟环境之前就返回，装坏了的机器上也得能看用法。"""
    done = _run_start("--help")
    assert done.returncode == 0
    assert "--clear-cache" in done.stdout


# --- 清哪一棵树 ---------------------------------------------------------


def test_prefers_the_installed_package_over_the_repo(monkeypatch):
    """装了就用装的那份，和 ``start.sh`` 起服务的分支一致。

    ``CACHE_DIR`` 相对 ``finmcp/config.py`` 的位置算，所以仓库那份和 site-packages
    那份指向两个不同的 ``.runtime/cache``。发布形态的部署上服务跑的是
    ``cn-stock-mcp``（site-packages），这里要是把仓库根塞进 ``sys.path``，清的就是
    仓库里那个空目录，还会打印"没有可清的纪元目录"——看着像成功。
    """
    sentinel = types.ModuleType("finmcp")
    sentinel.__file__ = "/somewhere/site-packages/finmcp/__init__.py"
    monkeypatch.setitem(sys.modules, "finmcp", sentinel)
    before = list(sys.path)

    assert clear_cache.import_finmcp() is sentinel
    assert sys.path == before, "能 import 到就不该动 sys.path"


def test_repo_root_is_only_inserted_in_the_fallback_branch():
    """顶层不许有 ``sys.path.insert``——那会让上面那条保证失效。"""
    tree = ast.parse(open(clear_cache.__file__, encoding="utf-8").read())
    # 只看**导入时会执行**的语句。函数体里的不算——兜底分支正是写在那里面的，
    # 而 ast.walk 会一路走进函数体，直接 walk 会把它自己也判成违规。
    executed_on_import = [
        node for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node in executed_on_import:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and ast.unparse(inner.func) == "sys.path.insert":
                raise AssertionError(
                    f"第 {inner.lineno} 行：import 时就会执行的 sys.path.insert；"
                    "只能放在 import finmcp 失败的兜底分支里"
                )


def test_reports_which_tree_it_cleared(capsys, monkeypatch, tmp_path):
    """必须打出 finmcp 的位置和缓存目录。

    清错树时唯一能看出来的就是这两行——"没有可清的纪元目录"本身既可能是真的
    干净，也可能是清到了另一棵树上。
    """
    fake = types.ModuleType("finmcp")
    fake.__file__ = str(tmp_path / "finmcp" / "__init__.py")
    cache_mod = types.ModuleType("finmcp.cache")
    cache_mod.EPOCH_DIR_PREFIX = "epoch-"
    config_mod = types.ModuleType("finmcp.config")
    config_mod.CACHE_DIR = str(tmp_path / "缓存")
    for name, module in (("finmcp", fake), ("finmcp.cache", cache_mod),
                         ("finmcp.config", config_mod)):
        monkeypatch.setitem(sys.modules, name, module)

    assert clear_cache.main() == 0
    out = capsys.readouterr().out
    assert fake.__file__ in out
    assert str(tmp_path / "缓存") in out
