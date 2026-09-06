"""缓存底座：命名空间、软/硬过期、单飞、旧值兜底。

这些是 docs/cache-design.md §七 列的不变量，破一条就说明实现走偏了。
报告缓存自己的行为归 test_report_cache.py。
"""

from __future__ import annotations

import asyncio
import datetime
import threading
import time

import pytest

from finmcp import cache as cache_module
from finmcp.cache import (
    Cache,
    Entry,
    Namespace,
    aget_or_load,
    get_or_load,
    key_for,
    register_namespace,
)

MONDAY = datetime.date(2026, 8, 17)


def at(hour, minute=0):
    return datetime.datetime.combine(MONDAY, datetime.time(hour, minute))


@pytest.fixture
def ns(tmp_path, request):
    """一个用完即弃的命名空间，避免各测试互相污染。"""
    name = f"t{abs(hash(request.node.name)) % 100000}"
    namespace = register_namespace(Namespace(name=name, max_entries=4, ttl_seconds=60))
    cache = Cache(namespace, directory=str(tmp_path))
    cache_module._caches[name] = cache
    yield name
    cache_module._caches.pop(name, None)
    cache_module._NAMESPACES.pop(name, None)


# --- 命名空间：各有各的额度 ---------------------------------------------------


def test_namespaces_do_not_evict_each_other(tmp_path):
    """market_events 一条 1.6 MiB，和个股报告挤同一份额度会把报告条目全挤掉。"""
    small = Cache(register_namespace(Namespace(name="nsA", max_entries=1)),
                  directory=str(tmp_path))
    big = Cache(register_namespace(Namespace(name="nsB", max_entries=8)),
                directory=str(tmp_path))
    try:
        for i in range(5):
            small.put(key_for("nsA", f"k{i}", now=at(18)), f"v{i}")
            big.put(key_for("nsB", f"k{i}", now=at(18)), f"v{i}")
        assert len(small._entries) <= 1
        assert len(big._entries) == 5
    finally:
        for name in ("nsA", "nsB"):
            cache_module._NAMESPACES.pop(name, None)


def test_each_namespace_gets_its_own_disk_directory(tmp_path):
    cache = Cache(register_namespace(Namespace(name="nsC", max_entries=2, disk=True)),
                  directory=str(tmp_path))
    try:
        assert cache.directory.endswith("nsC")
    finally:
        cache_module._NAMESPACES.pop("nsC", None)


# --- 软过期 / 硬过期 ----------------------------------------------------------


def test_a_hard_expired_entry_is_never_returned(tmp_path):
    """跨纪元的旧值不是"旧"，是另一个交易日的数据——任何情况下都不给。"""
    cache = Cache(register_namespace(Namespace(name="nsD", max_entries=4)),
                  directory=str(tmp_path))
    try:
        cache.put(key_for("nsD", "k", now=at(18)), "周一收盘那份")
        tuesday = datetime.datetime.combine(
            datetime.date(2026, 8, 18), datetime.time(10, 30))
        later = key_for("nsD", "k", now=tuesday)
        assert cache.get(later) is None
        assert cache.get_stale(later) is None      # 连兜底都不给
    finally:
        cache_module._NAMESPACES.pop("nsD", None)


def test_max_age_hard_expires_a_ttl_namespace(tmp_path):
    cache = Cache(
        register_namespace(Namespace(name="nsE", max_entries=4, epoch_bound=False,
                                     ttl_seconds=1, max_age_seconds=2)),
        directory=str(tmp_path))
    try:
        key = key_for("nsE", "k", now=at(18))
        cache.put(key, "值")
        cache._entries[key.digest()] = (time.time() - 5, key.epoch, "值")
        assert cache.get(key) is None
        assert cache.get_stale(key) is None        # 超了硬上限，兜底也不给
    finally:
        cache_module._NAMESPACES.pop("nsE", None)


def test_intraday_ttl_only_applies_to_live_epochs(tmp_path):
    """收盘后数据已冻结，再设软过期只会白打上游。"""
    cache = Cache(register_namespace(Namespace(name="nsF", max_entries=4, ttl_seconds=1)),
                  directory=str(tmp_path))
    try:
        closed = key_for("nsF", "k", now=at(18))
        cache.put(closed, "收盘那份")
        cache._entries[closed.digest()] = (time.time() - 3600, closed.epoch, "收盘那份")
        assert cache.get(closed) == "收盘那份"      # TTL 早过了，但纪元没变

        live = key_for("nsF", "k2", now=at(10))
        cache.put(live, "盘中那份")
        cache._entries[live.digest()] = (time.time() - 3600, live.epoch, "盘中那份")
        assert cache.get(live) is None              # 盘中才受 TTL 约束
    finally:
        cache_module._NAMESPACES.pop("nsF", None)


# --- get_or_load ---------------------------------------------------------------


def test_a_second_call_does_not_hit_upstream(ns):
    calls = []

    def loader():
        calls.append(1)
        return "值"

    first = get_or_load(ns, "k", loader, now=at(18))
    second = get_or_load(ns, "k", loader, now=at(18))
    assert first.value == second.value == "值"
    assert first.fresh and second.fresh
    assert len(calls) == 1


def test_a_loader_returning_none_yields_none(ns):
    assert get_or_load(ns, "k", lambda: None, now=at(18)) is None


def test_a_raising_loader_does_not_propagate(ns):
    """缓存层的取数失败不该把调用方打挂——它自己会处理"没取到"。"""
    def boom():
        raise RuntimeError("上游挂了")

    assert get_or_load(ns, "k", boom, now=at(18)) is None


def test_a_pinned_epoch_never_expires(ns):
    """钉了过去日期，那天的数据不会再变，跨纪元也不该失效。"""
    calls = []
    for now in (at(18), datetime.datetime(2026, 12, 25, 10, 0)):
        get_or_load(ns, "k", lambda: calls.append(1) or "值",
                    epoch="date-2026-08-20", now=now)
    assert len(calls) == 1


# --- 旧值兜底 ------------------------------------------------------------------


def test_a_stale_value_is_served_when_the_loader_fails(ns):
    """软过期后刷新失败，继续用旧的——但必须标 fresh=False。"""
    cache = cache_module.cache_for(ns)
    key = key_for(ns, "k", now=at(10))
    cache.put(key, "旧值")
    cache._entries[key.digest()] = (time.time() - 3600, key.epoch, "旧值")

    entry = get_or_load(ns, "k", lambda: None, now=at(10))
    assert entry is not None
    assert entry.value == "旧值"
    assert entry.fresh is False
    assert entry.age_seconds > 3000
    assert cache.stale_serves == 1


def test_stale_serving_can_be_turned_off(ns):
    cache = cache_module.cache_for(ns)
    cache.stale_on_error = False
    key = key_for(ns, "k", now=at(10))
    cache._entries[key.digest()] = (time.time() - 3600, key.epoch, "旧值")
    assert get_or_load(ns, "k", lambda: None, now=at(10)) is None


@pytest.mark.parametrize("seconds,expected", [
    (5, "5 秒"), (300, "5 分钟"), (7200, "2 小时"), (200000, "2 天"),
])
def test_age_text_is_human_readable(seconds, expected):
    """调用方要把它写进报告，得是人话。"""
    assert Entry(value="x", fresh=False, age_seconds=seconds).age_text == expected


# --- 单飞 ----------------------------------------------------------------------


def test_concurrent_callers_only_hit_upstream_once(ns):
    """缓存踩踏：缓存一空，所有在等的请求同时穿透到上游——最容易触发风控的时刻。

    实测过：冷进程并发两次调板块资金流，申万分级表被取了两份。
    """
    calls = []
    started = threading.Event()

    def slow_loader():
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return "值"

    results = []
    threads = [threading.Thread(
        target=lambda: results.append(get_or_load(ns, "k", slow_loader, now=at(18))))
        for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(calls) == 1, f"上游被打了 {len(calls)} 次"
    assert all(r is not None and r.value == "值" for r in results)


@pytest.mark.asyncio
async def test_async_concurrent_callers_only_hit_upstream_once(ns):
    calls = []

    async def slow_loader():
        calls.append(1)
        await asyncio.sleep(0.1)
        return "值"

    results = await asyncio.gather(
        *(aget_or_load(ns, "k", slow_loader, now=at(18)) for _ in range(4)))
    assert len(calls) == 1
    assert all(r.value == "值" for r in results)


@pytest.mark.asyncio
async def test_one_cancelled_waiter_does_not_break_the_others(ns):
    """shield：一个等待者被取消，不能中断别人需要的加载。"""
    async def slow_loader():
        await asyncio.sleep(0.15)
        return "值"

    doomed = asyncio.create_task(aget_or_load(ns, "k", slow_loader, now=at(18)))
    survivor = asyncio.create_task(aget_or_load(ns, "k", slow_loader, now=at(18)))
    await asyncio.sleep(0.02)
    doomed.cancel()
    result = await survivor
    assert result is not None and result.value == "值"


# --- 总开关 --------------------------------------------------------------------


def test_the_master_switch_stops_reads_and_writes(ns):
    """prove_equivalence.py 的"关掉缓存再比对"要的就是这个语义。"""
    cache = cache_module.cache_for(ns)
    cache.enabled = False
    calls = []
    for _ in range(3):
        get_or_load(ns, "k", lambda: calls.append(1) or "值", now=at(18))
    assert len(calls) == 3
    assert cache._entries == {}


# --- 不缓失败/降级结果 ----------------------------------------------------------


def test_a_namespace_can_refuse_to_cache_some_values(tmp_path):
    """一个纪元长达 64 小时，把一次瞬时降级腌进去就是整个周末都那样。"""
    cache = Cache(
        register_namespace(Namespace(
            name="nsG", max_entries=4,
            cacheable=lambda value, key: not getattr(value, "partial", False))),
        directory=str(tmp_path))
    try:
        class Board:
            def __init__(self, partial):
                self.partial = partial

        key = key_for("nsG", "k", now=at(18))
        cache.put(key, Board(partial=True))
        assert cache.get(key) is None
        cache.put(key, Board(partial=False))
        assert cache.get(key) is not None
    finally:
        cache_module._NAMESPACES.pop("nsG", None)


# --- 单例 ----------------------------------------------------------------------


def test_the_report_cache_singleton_can_actually_be_built():
    """这条是补的：get_report_cache() 曾因为引用了一个被搬走的常量而 NameError，
    而全套测试都没发现——它们都用 set_report_cache 注入自己的实例，从不走这条路。
    """
    cache_module.reset_caches()
    try:
        cache = cache_module.get_report_cache()
        assert cache.ns.name == "report"
    finally:
        cache_module.reset_caches()
