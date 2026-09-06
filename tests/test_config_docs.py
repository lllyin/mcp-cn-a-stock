"""配置名和默认值在代码、.env.example、README 之间必须一致。

这一组测试是有来历的：一次改名之后，`.env.example` 里还留着
`FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS=0.5`，而代码已经是 8——照文档抄一份 .env 出来
部署，等名额的上限就比一次页面加载还短，一批 4 个标的里的最后一个结构上永远排不到
资金流。文档漂移在这个项目里不是「注释过期」，是会掉数据的。

同一批检查还盯住另外两种漏法：代码新增了配置但文档没写（部署方压根不知道有这一项），
以及 `.env.example` 里留着代码已经不读的名字（写了也不生效，静默失效）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 前缀由 ENV_PREFIX 统一决定，本身不能再带前缀，所以不参与名字集合的比对。
_META = {"ENV_PREFIX"}

# 第三方约定的环境变量，不是本项目的配置项。
_FOREIGN = {"REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}

# 只作为旧名字继续认、但不再对外推荐的配置：代码里读，文档里故意不写。
# 写进文档等于把它们重新变成两套并列的正式名字，下一个人不知道该配哪个。
_LEGACY = {"AKSHARE_PROXY_IP", "AKSHARE_PROXY_PASSWORD", "AKSHARE_PROXY_PORT"}


#: 按命名空间派生的配置名：``CACHE_<命名空间>_TTL_SECONDS`` / ``_MAX_ENTRIES``。
#: 代码里用 f-string 拼名字（config.cache_ttl / cache_max_entries），扫不出来；
#: 而八个命名空间 × 两项 = 十六行文档，写进 README 也没人会逐行看。所以这一族按
#: **模式**校验：文档必须写清模式本身（见下面那条测试），具体名字不逐个比对。
_NAMESPACED_CACHE = re.compile(r"^CACHE_[A-Z0-9]+_(TTL_SECONDS|MAX_ENTRIES)$")


def _drop_namespaced(names: dict) -> dict:
    return {k: v for k, v in names.items() if not _NAMESPACED_CACHE.match(k)}


def test_the_namespaced_cache_override_pattern_is_documented():
    """这一族不逐个比对，那就必须保证模式本身写在文档里，否则等于没写。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CACHE_<命名空间>_MAX_ENTRIES" in readme
    assert "CACHE_<命名空间>_TTL_SECONDS" in env_example


def _code_configs() -> dict[str, str | None]:
    """扫出代码实际读取的配置名，以及能直接读到的默认值字面量。

    默认值只在写成 ``env("NAME", "字面量")`` 时才取得到；像
    ``_parse_bool(env("NAME"), True)`` 这种默认值在外层的，值记为 None，
    只比对名字。
    """
    found: dict[str, str | None] = {}
    patterns = (
        # env("NAME") / env("NAME", "默认值") / env("NAME", 默认值)
        # 最后那个分支是嵌套的 env(...)（旧名字兜底），此时取不到字面量默认值，只比名字。
        re.compile(
            r'\benv\(\s*"([A-Z0-9_]+)"\s*'
            r'(?:,\s*(?:"([^"]*)"|([\d.]+)|env\([^)]*\)))?\s*\)'
        ),
        # market_breadth 的两个小包装
        re.compile(r'_env_flag\(\s*"([A-Z0-9_]+)"\s*\)()()'),
        re.compile(r'_env_float\(\s*"([A-Z0-9_]+)"\s*,\s*()([\d.]+)\)'),
        # provider 链的顺序开关
        re.compile(r'PROVIDER_ORDER_ENV = "([A-Z0-9_]+)"()()'),
    )
    for path in sorted((ROOT / "finmcp").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                name = match.group(1)
                if name in _FOREIGN or name in _LEGACY:
                    continue
                default = match.group(2) or match.group(3) or None
                # 同名多处出现时，保留第一个带默认值的。
                if found.get(name) is None:
                    found[name] = default
    # start.sh 自己读的两项，Python 侧看不到。
    start_sh = (ROOT / "start.sh").read_text(encoding="utf-8")
    for name in re.findall(r'\bconf ([A-Z0-9_]+)', start_sh):
        found.setdefault(name, None)
    return {k: v for k, v in found.items() if k not in _META}


def _env_example() -> dict[str, str]:
    """.env.example 里的配置名和取值。注释掉的示例行也算已文档化。"""
    found: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            line = line[1:].strip()
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip()
    return {k: v for k, v in found.items() if k not in _META}


def _readme_configs() -> dict[str, str | None]:
    """README 配置表里的配置名，以及括号里写的默认值。"""
    found: dict[str, str | None] = {}
    for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*`([A-Z0-9_]+)`\s*\|(.*)$", line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        default = re.search(r"默认\s*`([^`]*)`", rest)
        found[name] = default.group(1) if default else None
    return {k: v for k, v in found.items() if k not in _META}


def test_env_example_covers_exactly_what_the_code_reads():
    code = set(_drop_namespaced(_code_configs()))
    doc = set(_drop_namespaced(_env_example()))
    assert not code - doc, f".env.example 缺少代码在读的配置: {sorted(code - doc)}"
    assert not doc - code, f".env.example 写了代码不读的配置: {sorted(doc - code)}"


def test_readme_covers_exactly_what_the_code_reads():
    code = set(_drop_namespaced(_code_configs()))
    doc = set(_drop_namespaced(_readme_configs()))
    assert not code - doc, f"README 缺少代码在读的配置: {sorted(code - doc)}"
    assert not doc - code, f"README 写了代码不读的配置: {sorted(doc - code)}"


def _same_value(left: str, right: str) -> bool:
    """`300` 和 `300.0` 是同一个默认值，别为写法判失败。"""
    if left == right:
        return True
    try:
        return float(left) == float(right)
    except ValueError:
        return False


@pytest.mark.parametrize("name,expected", sorted(
    (k, v) for k, v in _drop_namespaced(_code_configs()).items() if v is not None
))
def test_env_example_default_matches_the_code(name, expected):
    documented = _env_example()[name]
    assert _same_value(documented, expected), f"{name}: 文档 {documented!r} / 代码 {expected!r}"


def test_readme_and_env_example_agree_on_defaults():
    env_example, readme = _env_example(), _readme_configs()
    mismatched = {
        name: (value, env_example[name])
        for name, value in readme.items()
        if value is not None and name in env_example and env_example[name] != ""
        # 空字符串两边写法不同：.env.example 写 `NAME=`，README 写「默认空」。
        and not _same_value(value, env_example[name])
    }
    assert not mismatched, f"README 与 .env.example 默认值不一致: {mismatched}"
