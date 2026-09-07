"""文案不绑定某一台机器。

这是个开源项目，代码、文档、脚本输出都会被素不相识的人读到。「部署机」「本机」这类
说法对他们没有意义——它指的是**当初测这个数的人手上那台机器**，而读者手上不是那台。
真正该写下来的是**条件**：机房出口 IP 还是家宽出口、2 核还是 8 核、装成包跑还是从
仓库跑。条件才是可对照、可复现、可迁移的。

保留什么：**带日期的实测数字要留着**（AGENTS.md §六 要求把取值依据写进注释）。
禁的只是"哪台机器"，不是"多少"。所以

    ✗  2026-09-05 部署机实测把它推到 3
    ✓  2026-09-05 在 2 核 4G 云主机上实测把它推到 3

后者信息更多：读者能判断这个数对自己适不适用。

``scripts/probe_tuning.py`` 另有一条更严的（``test_probe_tuning.py``）：它产出的报告和
lint 文案里连日期都不许有，因为那是给别人机器看的推荐值，不该混进别处的测试历史。
"""

from __future__ import annotations

import pathlib
import re

import pytest

#: 指某一台具体机器的说法。换成条件描述——见模块文档。
#: ``部署机器`` 是正常中文（"部署机器规格"），不算；``本机直连``/``本机出口`` 说的是
#: "本地直连 vs 经网关出口"，是机制不是机器，也不算。
_MACHINE_WORDS = (
    re.compile(r"部署机(?!器)"),
    re.compile(r"本机(?!直连|出口)"),
    re.compile(r"我的机器|我这台|我那台"),
)

#: 私人绝对路径。示例路径请写 ``/path/to/...``，或用 ``$HOME`` / ``Path.home()``。
_PRIVATE_PATHS = (
    re.compile(r"/Users/[a-z][a-z0-9_.-]+", re.I),
    re.compile(r"/root/\.[a-z]"),
)

_SUFFIXES = {".py", ".md", ".sh", ".toml", ".example", ".cfg", ".yaml", ".yml"}

#: 归档不改：``verification/baseline/*.md`` 记的是**当时实际执行的那条命令**，改了它
#: 就不是归档了（连带 verify_release 的复放也会对不上）。reports/ 同理，是产出物。
#: ``.runtime`` / ``private`` / ``tests_research`` 不进发布。
_SKIP_PARTS = {
    ".git", ".venv", ".runtime", "private", "tests_research",
    "node_modules", "__pycache__", "build", "dist", ".pytest_cache",
}


def _tracked_files():
    root = pathlib.Path(__file__).resolve().parents[1]
    for path in root.rglob("*"):
        if path.suffix not in _SUFFIXES or not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_PARTS:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("verification/baseline/") or rel.startswith("verification/reports/"):
            continue
        if rel == "tests/test_no_machine_coupling.py":       # 本文件就是那张禁用词表
            continue
        yield rel, path


#: 逐行豁免。只有一种正当用途：这条规则自己的实现（禁用词表里必然写着禁用词）。
#: 用行内标记而不是整文件排除——整文件排除会顺带放过那个文件里真正的问题。
_ALLOW = "noqa: machine-coupling"


def _hits(patterns, text: str):
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if _ALLOW in line:
            continue
        for pattern in patterns:
            match = pattern.search(line)
            if match:
                out.append(f"{number}: …{line.strip()[:96]}…  ←「{match.group(0)}」")
                break
    return out


@pytest.mark.parametrize("patterns,what", [
    (_MACHINE_WORDS, "指某一台机器的说法"),
    (_PRIVATE_PATHS, "私人绝对路径"),
])
def test_nothing_in_the_repo_is_pinned_to_one_machine(patterns, what):
    offenders = {}
    for rel, path in _tracked_files():
        found = _hits(patterns, path.read_text(encoding="utf-8", errors="replace"))
        if found:
            offenders[rel] = found
    assert not offenders, (
        f"下面这些地方有{what}，开源读者读不懂，请改成条件描述"
        f"（机房/家宽出口 IP、核数内存、装包还是源码跑）：\n"
        + "\n".join(f"  {rel}\n    " + "\n    ".join(lines)
                    for rel, lines in sorted(offenders.items()))
    )


def test_the_guard_would_actually_catch_something():
    """护栏自己要能抓到东西，否则它只是一条永远通过的测试。"""
    assert _hits(_MACHINE_WORDS, "# 2026-09-05 部署机实测把它推到 3")
    assert _hits(_MACHINE_WORDS, "# 本机实测 2/16")
    assert _hits(_PRIVATE_PATHS, 'Path("/root/.openclaw/workspace/config.json")')
    assert _hits(_PRIVATE_PATHS, "cd /Users/someone/repos/x")
    # 而这些是允许的
    assert not _hits(_MACHINE_WORDS, "# 2026-09-05 在 2 核 4G 云主机上实测把它推到 3")
    assert not _hits(_MACHINE_WORDS, "#   direct  本机直连 + 原生 requests")
    assert not _hits(_MACHINE_WORDS, "# 上游对本机出口 IP 的待遇")
    assert not _hits(_MACHINE_WORDS, "# 原值 2 的由来是部署机器规格")
    assert not _hits(_PRIVATE_PATHS, "cd /path/to/mcp-cn-a-stock")
    assert not _hits(_PRIVATE_PATHS, 'MCPORTER_CONFIG="$HOME/.openclaw/config.json"')
