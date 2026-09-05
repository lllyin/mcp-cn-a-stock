"""平台 → 能力 → 归一 的底座。

设计说明在 docs/data-provider-architecture.md，改这个文件之前先读它。一句话版本：
一个上游（东财/腾讯/新浪/同花顺/雪球）是一个 ``Platform``，它声明自己能提供哪些
**能力**（kline / quote / basic_info / …）；每个维度按自己的环境变量决定用哪些平台、
按什么顺序；``resolve()`` 走完逐级回退和交叉合成。

这一层刻意**不假设 HTTP**：``fund_flow_page`` 走浏览器、``efinance`` 是个库、交易
日历的兜底是纯计算——它们都是平台。HTTP 相关的东西在 ``HttpPlatform`` 里。
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..config import env

logger = logging.getLogger("finmcp")

_DISABLED = {"", "off", "none", "0", "false"}

#: 这些异常的含义是"这个平台不认识这个标的"，不是"这次没取到"。区分开，调用方
#: 才能说"数据源不支持"而不是误导性的"未找到数据"。
UNSUPPORTED_ERRORS = (KeyError, IndexError, ValueError)


# ── 能力契约 ────────────────────────────────────────────────────
#
# 每个能力有且只有一种归一后的数据结构。谁提供这个能力都得归一到它——这是"平台
# 可以随便换"的全部前提：调用方只认契约，不认是谁给的。
#
# 契约在这里登记之后，resolve() 会对每个平台的返回值实际校验一遍。校验不过不是
# 抛异常，是当成"这个平台没给出可用结果"往下一个平台走——一个写坏的新 provider
# 应该降级到旧的，而不是把脏数据灌进报告。

_CONTRACTS: dict[str, tuple] = {}


def define_capability(capability: str, contract, *, describe: str = "") -> None:
    """维度侧声明：这个能力归一后长什么样。

    ``contract`` 可以是一个类型（用 isinstance 判），也可以是一个 ``(value) -> bool``
    的校验函数。K 线这种返回 DataFrame 的用后者——光判类型说明不了列对不对，而
    列不对正是新接一个源最容易出的错。
    """
    _CONTRACTS[capability] = (contract, describe or getattr(contract, "__name__", str(contract)))


def contract_of(capability: str):
    entry = _CONTRACTS.get(capability)
    return entry[0] if entry else None


def _honours_contract(capability: str, value) -> tuple:
    """(合不合契约, 不合时的说明)。没登记契约就不校验。"""
    entry = _CONTRACTS.get(capability)
    if entry is None:
        return True, ""
    contract, describe = entry
    try:
        ok = isinstance(value, contract) if isinstance(contract, type) else bool(contract(value))
    except Exception as error:
        return False, f"校验函数自己抛了 {type(error).__name__}: {error}"
    return ok, "" if ok else f"不符合 {capability} 的契约（要求 {describe}）"


class Platform(abc.ABC):
    """一个上游数据源。

    子类要给 ``name``（配置里写的标识符）、``label``（中文，打日志用）、
    ``capabilities``，以及每个声明了的能力对应的 ``fetch_<capability>`` 方法。
    """

    name: str = ""
    label: str = ""
    capabilities: frozenset = frozenset()

    def supports(self, capability: str, request) -> bool:
        """这个平台能不能处理这个请求。默认全能处理。

        返回 False 的请求**连发都不发**。腾讯对北交所代码抛 KeyError，靠
        try/except 去发现等于每个北交所标的每次白付一个往返。
        """
        return True

    def degraded(self) -> bool:
        """此刻这个平台是不是已知必败。默认不是。

        和熔断的区别：熔断要数够 N 次失败才知道，这个现在就知道。东财的实例——
        伪装通道一进冷却，push2/push2his 的请求就退回原生 requests，而它们被接管的
        理由正是拒绝原生 requests，不用数也知道必败。
        """
        return False

    def fetcher(self, capability: str) -> Optional[Callable]:
        return getattr(self, f"fetch_{capability}", None)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} {sorted(self.capabilities)}>"


@dataclass(frozen=True)
class Resolved:
    """取数结果，外加"谁给的"。

    ``platform`` 不是给日志看的装饰：编排要靠它做判断——东财给的 K 线不补当日
    实时 bar，兜底源给的要补。
    """

    value: object
    platform: str
    merged_from: tuple = ()

    @property
    def source(self) -> str:
        """合成时记成 ``eastmoney+tencent``，日志里看得出这份数据是拼的。"""
        return "+".join(self.merged_from) if self.merged_from else self.platform


_PLATFORMS: dict[str, Platform] = {}


def register(platform: Platform, *, replace: bool = False) -> None:
    """把一个平台挂进注册表。

    注册时校验声明与实现一致：声明了 kline 却没写 ``fetch_kline``，在这里就报错，
    而不是等到线上少一个源才发现。
    """
    if not platform.name:
        raise ValueError(f"平台没有 name：{platform!r}")
    if not replace and platform.name in _PLATFORMS:
        raise ValueError(f"平台重名：{platform.name}")
    missing = [c for c in platform.capabilities if platform.fetcher(c) is None]
    if missing:
        raise ValueError(
            f"平台 {platform.name} 声明了 {missing} 但没有对应的 fetch_ 方法"
        )
    _PLATFORMS[platform.name] = platform


def unregister(name: str) -> None:
    _PLATFORMS.pop(name, None)


def registered(capability: Optional[str] = None) -> tuple:
    """已注册的平台名。给了能力就只列能提供该能力的。"""
    if capability is None:
        return tuple(_PLATFORMS)
    return tuple(n for n, p in _PLATFORMS.items() if capability in p.capabilities)


def get(name: str) -> Optional[Platform]:
    """按名字取平台。给测试和排查用，不必去动注册表内部。"""
    return _PLATFORMS.get(name)


def configured_order(capability: str, env_name: str, default: tuple) -> tuple:
    """按配置解析某个能力的平台顺序。

    未知名字、或者平台不提供这个能力，都只是忽略并告警——配错一个名字不该让服务
    起不来，只该少一个源。
    """
    raw = env(env_name)
    if raw is None:
        names = default
    elif raw.strip().lower() in _DISABLED:
        return ()
    else:
        names = tuple(part.strip() for part in raw.split(",") if part.strip())

    order = []
    for name in names:
        platform = _PLATFORMS.get(name)
        if platform is None:
            logger.warning(
                "%s 里的 %s 不是已注册的平台，已忽略；可用：%s",
                env_name, name, ",".join(registered(capability)) or "（空）",
            )
        elif capability not in platform.capabilities:
            logger.warning(
                "%s 里的 %s 不提供 %s 能力，已忽略", env_name, name, capability
            )
        else:
            order.append(name)
    return tuple(order)


@dataclass
class _Attempt:
    """一次 resolve 的过程记录，只用于日志和 status。"""

    order: tuple
    considered: list = field(default_factory=list)


def resolve(
    capability: str,
    request,
    *,
    order: tuple,
    merge: Optional[Callable] = None,
    enough: Optional[Callable] = None,
    status: Optional[dict] = None,
    breaker_for: Optional[Callable] = None,
) -> Optional[Resolved]:
    """按顺序问每个平台，直到够了。

    ``merge`` 为 None 时是"第一个给出非空结果的赢"（K 线、盘中行情是这种）；给了
    函数就把后面的结果合进前面的（基本数据是这种：A 给了名称、B 给了市值，合起来
    才完整）。``enough`` 决定"够不够"，默认拿到任何非空结果就停；基本数据要求必须
    有市值，否则东财 snapshot 一通就返回，市值那一组永远轮不到腾讯去补。

    四道闸门从便宜到贵：``degraded()`` 零成本 → 熔断读内存 → ``supports()`` 纯计算
    → 最后才发请求。
    """
    local = {} if status is None else status
    accumulated = None
    merged_from: list = []

    for name in order:
        platform = _PLATFORMS.get(name)
        if platform is None:
            continue
        fetch = platform.fetcher(capability)
        if fetch is None:
            continue

        if platform.degraded():
            local[f"{name}_degraded"] = True
            logger.debug("%s 已知降级，跳过 %s", name, capability)
            continue

        breaker = breaker_for(name) if breaker_for else None
        if breaker is not None and breaker.should_skip():
            local[f"{name}_breaker_open"] = True
            continue

        if not platform.supports(capability, request):
            local[f"{name}_unsupported"] = True
            logger.debug("%s 不覆盖这个请求，跳过 %s", name, capability)
            continue

        try:
            value = fetch(request)
        except Exception as error:
            if isinstance(error, UNSUPPORTED_ERRORS):
                local[f"{name}_unsupported"] = True
            if breaker is not None:
                breaker.record(success=False)
            logger.warning("%s取%s失败: %s", platform.label, capability, error)
            continue

        if breaker is not None:
            breaker.record(success=True)
        if _is_empty(value):
            continue

        ok, why = _honours_contract(capability, value)
        if not ok:
            # 归一没做对，当成这个平台没给结果。降级到下一个源，而不是让脏数据
            # 进报告——报告里一个形状不对的字段，比少一个源难查得多。
            logger.warning("%s 提供的 %s %s，已跳过", platform.label, capability, why)
            local[f"{name}_contract_violation"] = why
            continue

        merged_from.append(name)
        accumulated = value if merge is None else merge(accumulated, value)
        if enough is None or enough(accumulated):
            return Resolved(
                value=accumulated,
                platform=merged_from[0],
                merged_from=tuple(merged_from),
            )
        logger.debug("%s 给的 %s 还不够，继续下一个平台", name, capability)

    considered = [n for n in order if n in _PLATFORMS]
    if considered and all(local.get(f"{n}_unsupported") for n in considered):
        # 每个平台都不认这个请求，是覆盖缺口，不是一次安静的失败。
        local["unsupported"] = True
    if accumulated is None:
        return None
    # 走完全部平台仍不"够"，但拿到了部分结果——半个结果也比没有强。
    return Resolved(
        value=accumulated, platform=merged_from[0], merged_from=tuple(merged_from)
    )


def _is_empty(value) -> bool:
    """None、空表、空序列都算"这次没有"，继续问下一个平台。

    不能只判 None：AkShare 在窗口内没有数据时给的是空 DataFrame，不是 None，
    而 DataFrame 没有真值语义，直接 ``if not value`` 会抛 ValueError。
    """
    if value is None:
        return True
    empty = getattr(value, "empty", None)
    if empty is not None:
        return bool(empty)
    if isinstance(value, (list, tuple, dict, set, str)):
        return len(value) == 0
    return False


__all__ = [
    "Platform",
    "contract_of",
    "define_capability",
    "Resolved",
    "UNSUPPORTED_ERRORS",
    "configured_order",
    "get",
    "register",
    "registered",
    "resolve",
    "unregister",
]
