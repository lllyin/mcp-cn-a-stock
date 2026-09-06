"""Epoch-bound report cache.

The cache is deliberately independent of the datasource layer: it stores the
*rendered* output of a tool and hands it back only inside the market epoch that
produced it. An epoch is a window in which regenerating the report would read
the same upstream numbers and take the same rendering branch, so a hit returns
what a live call would have returned.

Everything here is inert when ``REPORT_CACHE_ENABLED`` is false — call sites
fall through to the original path with no extra work.

Two invariants keep this honest, and both are load-bearing:

1. **One epoch is live at a time.** ``market_phase`` is a total function of the
   clock, so every tool sees the same epoch token at the same instant. The
   memory tier never has to reconcile two namespaces, and the disk tier can
   retire whole directories.
2. **A full-reuse epoch never starts at the instant its data freezes.** The
   upstream feeds finalise a few minutes after each session boundary, so each
   boundary is followed by a short TTL-bounded buffer window carrying its own
   epoch token.

Wall-clock dependencies in the render path, and how each is covered:

- ``research.load_raw_data`` derives its fetch window from ``now() + 1 day``.
  Covered by putting the window date in the key, which splits the cache at
  midnight.
- ``research.today_volume_est_ratio`` is constant inside every epoch except
  LIVE, which is why LIVE reuse is TTL-bounded.
- ``market_session.is_realtime_fund_flow_window`` flips at ``WARMUP_TIME`` and
  ``FINAL_TIME``; both are epoch boundaries, so an epoch never mixes the
  Playwright and AkShare renderings.
- ``research.build_fund_flow`` prints the newest fund-flow row, and that row
  lands some minutes after the ``FINAL_TIME`` flip. Covered by the evening
  buffer window.

时段边界本身不在这里，在 ``market_session``——它是全项目唯一的定义处，
``research`` 也读同一份。
- ``research.has_today_fund_flow_from_api`` compares against today's date but is
  only reachable from LIVE/LUNCH/POSTCLOSE, which never span midnight.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import asyncio
import threading
import time
import dataclasses
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .config import (
    CACHE_DIR,
    CACHE_DISK_ENABLED,
    CACHE_ENABLED,
    CACHE_INTRADAY_TTL_SECONDS,
    CACHE_STALE_ON_ERROR,
    cache_max_entries,
    cache_ttl,
)
from .market_session import (
    PHASE_CLOSED,
    PHASE_LIVE,
    PHASE_LUNCH,
    PHASE_POSTCLOSE,
    now_shanghai as _as_shanghai,
    phase_and_epoch,
)
from .version import __version__

logger = logging.getLogger("finmcp")

# A report carrying one of these markers recorded a transient upstream failure.
# Caching it would pin the failure for the rest of the epoch.
TRANSIENT_MARKERS = (
    "[实时抓取失败]",
    "[实时调用异常]",
    "盘中实时数据暂时不可用",
    "Error during processing:",
)

# Disk directories are named with this prefix so the sweeper can never delete
# something it did not create, even if REPORT_CACHE_DIR points at a shared path.
EPOCH_DIR_PREFIX = "epoch-"
# Retire directories well past the longest epoch (Friday evening to Monday
# open is 64h) so a sweep never removes data another phase may still read.
DISK_RETENTION_SECONDS = 5 * 24 * 3600
DISK_SWEEP_INTERVAL_SECONDS = 3600


def _render_fingerprint() -> str:
    """Identify the rendering inputs, so a deploy cannot serve pre-deploy output.

    The disk tier outlives restarts and a closed epoch runs for up to 64h, so
    without this a rendering fix shipped in the evening would stay invisible
    until the next session opened.

    ``confs/indices.json`` is covered because it decides, through
    ``config.ALL_INDICES``, whether a symbol renders down the index branch or
    the stock branch (``research.get_realtime_fund_flow_target``). Editing it is
    a rendering change even though no ``.py`` file moved.

    ``market_session.py`` is in the list because the session boundaries decide
    which fund-flow branch a report takes — moving one is a rendering change.
    """
    parts = [__version__]
    here = os.path.dirname(os.path.abspath(__file__))
    # (path, required): a missing source file is a broken install and should
    # degrade to "always miss"; a missing optional config is a normal state and
    # must hash to a stable marker, or the fingerprint would change every boot.
    sources = [
        (os.path.join(here, name), True)
        for name in ("research.py", "mcp_app.py", "cache.py", "config.py",
                     "market_session.py")
    ]
    sources.append((os.path.join(here, os.pardir, "confs", "indices.json"), False))
    for path, required in sources:
        name = os.path.basename(path)
        try:
            with open(path, "rb") as handle:
                parts.append(hashlib.sha1(handle.read()).hexdigest())
        except OSError:
            parts.append(
                f"{name}:unreadable:{time.time()}" if required else f"{name}:absent"
            )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


RENDER_FINGERPRINT = _render_fingerprint()


#: 兼容旧名字：外部按 cache.market_phase 调过；定义在 market_session。
market_phase = phase_and_epoch


@dataclass(frozen=True)
class Namespace:
    """一个缓存域的全部声明。

    各有各的额度，互不挤占——``market_events`` 一条 1.6 MiB，和个股报告混在同一份
    512 条额度里会把报告条目全挤掉。

    失效口径只有两种，用 ``epoch_bound`` 区分：

    - **跟市场**（``epoch_bound=True``）：行情、资金流这些，盘中一直在变、收盘冻住。
      ``ttl_seconds`` 是软过期，**只在盘中纪元生效**；收盘后的纪元里数据已经冻结，
      再设软过期只会白打上游。
    - **按时长**（``epoch_bound=False``）：交易日历、行业分级这些，和交易时段无关。
    """

    name: str
    max_entries: int
    epoch_bound: bool = True
    ttl_seconds: float = 0.0
    #: 硬过期上限，0 = 只受 epoch 约束。软过期之后还能用多久，由它兜底。
    max_age_seconds: float = 0.0
    disk: bool = False
    #: 值 → 可 JSON 化。None 表示值本身就能 JSON 化（str/dict/list）。
    encode: Optional[Callable[[Any], Any]] = None
    decode: Optional[Callable[[Any], Any]] = None
    #: 哪些结果不该写进去（失败、降级）。签名 ``(value, key) -> bool``。
    cacheable: Optional[Callable[..., bool]] = None


_NAMESPACES: dict[str, Namespace] = {}


def register_namespace(namespace: Namespace) -> Namespace:
    """登记一个缓存域，并让配置能按名字覆盖它的 TTL 和上限。"""
    resolved = dataclasses.replace(
        namespace,
        ttl_seconds=cache_ttl(namespace.name, namespace.ttl_seconds),
        max_entries=cache_max_entries(namespace.name, namespace.max_entries),
    )
    _NAMESPACES[resolved.name] = resolved
    return resolved


def namespace(name: str) -> Optional[Namespace]:
    return _NAMESPACES.get(name)


@dataclass(frozen=True)
class Entry:
    """一次取用的结果，外加"它有多新"。

    ``fresh=False`` 表示软过期之后刷新失败、用的是旧值——调用方**必须**在输出里
    标注。悄悄返回旧数据比少一段数据更糟：少一段看得见，旧一天看不见。
    """

    value: Any
    fresh: bool = True
    age_seconds: float = 0.0

    @property
    def age_text(self) -> str:
        seconds = int(self.age_seconds)
        if seconds < 60:
            return f"{seconds} 秒"
        if seconds < 3600:
            return f"{seconds // 60} 分钟"
        if seconds < 86400:
            return f"{seconds // 3600} 小时"
        return f"{seconds // 86400} 天"


#: 报告：唯一缓存**渲染结果**而不是上游数据的命名空间。理由是它的渲染本身要跑
#: 指标计算，重做不便宜，而参数只有 (symbol, date, fund_flow_limit)，key 空间有界。
REPORT_NAMESPACE = register_namespace(Namespace(
    name="report",
    max_entries=512,
    epoch_bound=True,
    ttl_seconds=CACHE_INTRADAY_TTL_SECONDS,
    disk=True,
))


@dataclass(frozen=True)
class CacheKey:
    tool: str
    symbol: str
    params: str
    epoch: str
    window: str
    phase: str

    def digest(self) -> str:
        raw = "|".join(
            (
                RENDER_FINGERPRINT,
                self.tool,
                self.symbol,
                self.params,
                self.epoch,
                self.window,
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _fingerprint(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)


def _parse_date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def build_key(
    tool: str,
    symbol: str,
    params: dict[str, Any],
    *,
    query_date: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> CacheKey:
    """Build the key for one rendered unit.

    Every tool rides the market epoch, including explicitly past-dated queries.
    Pinning a past date does not make a result stable: report tools still print a
    live 总市值 / 流通市值 / 市盈率(动) because ``_fetch_realtime_sync`` ignores
    the date, and forward-adjusted (qfq) history is re-based *during* an ex-date,
    not overnight, so a "settled" fast path would serve pre-rebase prices for the
    rest of that trading day.

    ``query_date`` only fixes the fetch window; when it is absent the window
    comes from ``now() + 1 day``, so today's date goes in the key instead.
    """
    now = _as_shanghai(now)
    phase, epoch = market_phase(now)
    explicit = _parse_date(query_date)
    window = explicit.isoformat() if explicit else now.date().isoformat()

    return CacheKey(
        tool=tool,
        symbol=symbol,
        params=_fingerprint(params),
        epoch=epoch,
        window=window,
        phase=phase,
    )


#: TTL 型命名空间的纪元占位。它们不跟市场走，所有条目共用一个恒定 token。
_TTL_EPOCH = "ttl"


def key_for(
    ns: str,
    key: str,
    *,
    epoch: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> CacheKey:
    """通用命名空间的 key。

    ``epoch`` 显式传入用于钉过去日期的查询：传 ``date-2026-08-20`` 之后这条永不
    跨纪元失效，因为那天的数据不会再变。这类条目的 phase 记成 CLOSED——它没有
    "盘中"可言，不该被盘中 TTL 约束。

    ``key`` 里只放**影响上游请求**的参数。渲染参数（top/level/keywords 这些）一律
    不进：板块资金流那次的教训，进了就是 900 个 key，不进只有 9 个。
    """
    if epoch is not None:
        return CacheKey(tool=ns, symbol=key, params="", epoch=epoch,
                        window=epoch, phase=PHASE_CLOSED)
    declared = _NAMESPACES.get(ns)
    if declared is not None and not declared.epoch_bound:
        # TTL 型命名空间不看市场时钟——它的新鲜度和交易时段无关。
        # 这不只是省一次计算：交易日历自己就是一个 TTL 命名空间，而市场纪元要靠
        # 交易日历才算得出来。让它去问纪元就是无限递归。
        return CacheKey(tool=ns, symbol=key, params="", epoch=_TTL_EPOCH,
                        window=_TTL_EPOCH, phase=PHASE_CLOSED)
    moment = _as_shanghai(now)
    phase, current = market_phase(moment)
    return CacheKey(tool=ns, symbol=key, params="", epoch=current,
                    window=moment.date().isoformat(), phase=phase)


def is_cacheable_report(
    text: str,
    *,
    phase: Optional[str] = None,
    fund_flow_lagging: bool = False,
) -> bool:
    """这份渲染结果能不能写进缓存。

    两件事：

    1. **瞬时失败不进缓存。** 一个纪元长达 64 小时，把一次上游抖动腌进去，
       整个周末就都是那个样子。

    2. **当日资金流还在路上时，不进 CLOSED 纪元。** CLOSED 纪元长达 16-64 小时，
       而 AkShare 的当日资金流行要到收盘后一段时间才落地——``MARKET_EPOCH_FINAL_TIME``
       配早了，这一段就会缺，然后被冻一整晚。

       有了这道守卫，``final`` 配早的代价从**数据缺失**降到**少命中几次缓存**——
       没落地就继续留在 POSTCLOSE 的短 TTL 里，落地了自然进。

    ``fund_flow_lagging`` 由调用方算好传进来（``research.fund_flow_lag``），
    不在这里从渲染结果里正则抠日期：抠出来的东西依赖标题措辞，措辞一改守卫就
    悄悄失效了，而失效是看不出来的。
    """
    if not text or not text.strip():
        return False
    if any(marker in text for marker in TRANSIENT_MARKERS):
        return False
    if phase == PHASE_CLOSED and fund_flow_lagging:
        return False
    return True


class Cache:
    """一个命名空间的两层缓存：有界内存层 + 可选磁盘层。

    内存层存**活对象**，磁盘层存编码后的 JSON——内存命中不付编解码代价。
    """

    def __init__(
        self,
        ns: Optional[Namespace] = None,
        *,
        enabled: bool = CACHE_ENABLED,
        live_ttl_seconds: Optional[float] = None,
        max_entries: Optional[int] = None,
        disk_enabled: bool = CACHE_DISK_ENABLED,
        directory: str = CACHE_DIR,
        stale_on_error: bool = CACHE_STALE_ON_ERROR,
    ):
        self.ns = ns or REPORT_NAMESPACE
        # 总开关只管**跟市场走**的命名空间。TTL 型的（交易日历、行业分类）不受它
        # 影响，理由是它们不是"某次查询的结果"，是加载一次的参考数据：
        #
        #   - 关掉它不改变任何输出——同一份名单，读缓存和重新取得到的完全一样，
        #     所以对 prove_equivalence 的等价性证明没有任何贡献。
        #   - 关掉它的代价是灾难性的：交易日历决定市场纪元，而纪元每次算 phase
        #     都要用——实测 CACHE_ENABLED=0 时三次 is_trading_day 就打了三次上游，
        #     0.51s。生产里等于每个请求都多付几次网络往返。
        #
        # 想强制重取用 clear()，那是"重置"该做的事，不是总开关。
        self.enabled = enabled or not self.ns.epoch_bound
        self.live_ttl_seconds = (
            self.ns.ttl_seconds if live_ttl_seconds is None else live_ttl_seconds
        )
        self.max_entries = self.ns.max_entries if max_entries is None else max_entries
        self.disk_enabled = disk_enabled and self.ns.disk
        # 每个命名空间一个子目录：清扫器各扫各的，一个域的条目不会被另一个域的
        # 清扫顺手带走。
        self.directory = os.path.join(directory, self.ns.name)
        self.stale_on_error = stale_on_error
        self._entries: dict[str, tuple[float, str, Any]] = {}
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._last_sweep_at = 0.0
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.stale_serves = 0

    # -- policy ----------------------------------------------------------

    def _hard_expired(self, key: CacheKey, created_at: float, created_epoch: str) -> bool:
        """硬过期：条目作废，任何情况下都不返回。

        纪元变了就是变了——昨天 15:00 的收盘快照到今天 10:30 只旧了 19.5 小时，
        任何以"天"为量级的时长上限都会放行它，但它描述的是**另一个交易日**，
        不是"旧"，是答非所问。
        """
        if self.ns.epoch_bound and created_epoch != key.epoch:
            return True
        if self.ns.max_age_seconds > 0:
            return (time.time() - created_at) > self.ns.max_age_seconds
        return False

    def _fresh(self, key: CacheKey, created_at: float, created_epoch: str) -> bool:
        """软过期：到了就想刷新，但刷不到还能用（见 get_or_load）。"""
        if self._hard_expired(key, created_at, created_epoch):
            return False
        if not self.ns.epoch_bound:
            return self.live_ttl_seconds <= 0 or (
                time.time() - created_at) <= self.live_ttl_seconds
        # 跟市场的命名空间：TTL 只在盘中生效。收盘后数据已冻结，再设软过期
        # 只会白打上游。
        if key.phase != PHASE_LIVE:
            return True
        if self.live_ttl_seconds <= 0:
            return False
        return (time.time() - created_at) <= self.live_ttl_seconds

    # -- memory tier -----------------------------------------------------

    def _prune_locked(self, *, reserve: bool) -> None:
        """Enforce the size bound only.

        Entries from a retired epoch are rejected by ``_fresh`` on read, so
        eviction does not need to reason about which epoch is current — doing so
        would make the tier hold exactly one namespace and turn any future second
        namespace into mutual eviction.
        """
        target = self.max_entries - 1 if reserve else self.max_entries
        while len(self._entries) > target:
            oldest = min(self._entries, key=lambda d: self._entries[d][0])
            self._entries.pop(oldest, None)

    # -- disk tier -------------------------------------------------------

    def _epoch_dir(self, epoch: str) -> str:
        safe = epoch.replace(os.sep, "_").replace("/", "_")
        return os.path.join(self.directory, f"{EPOCH_DIR_PREFIX}{safe}")

    def _sweep_disk(self) -> None:
        """Retire epoch directories this cache created and no longer needs.

        Only ``epoch-`` prefixed directories are ever removed, and only once they
        are older than the longest possible epoch, so pointing REPORT_CACHE_DIR
        at a populated directory cannot destroy anything.
        """
        now = time.time()
        with self._lock:
            if now - self._last_sweep_at < DISK_SWEEP_INTERVAL_SECONDS:
                return
            self._last_sweep_at = now

        try:
            names = os.listdir(self.directory)
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError:
            logger.debug("Cache sweep skipped ns=%s", self.ns.name, exc_info=True)
            return

        for name in names:
            if not name.startswith(EPOCH_DIR_PREFIX):
                continue
            path = os.path.join(self.directory, name)
            try:
                if not os.path.isdir(path):
                    continue
                if now - os.path.getmtime(path) < DISK_RETENTION_SECONDS:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            logger.debug("Cache retired epoch directory ns=%s %s", self.ns.name, name)

    def _disk_read(self, key: CacheKey) -> Optional[tuple[float, str, Any]]:
        path = os.path.join(self._epoch_dir(key.epoch), f"{key.digest()}.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
            return None
        except OSError:
            logger.debug("Cache disk read failed ns=%s", self.ns.name, exc_info=True)
            return None
        if payload.get("epoch") != key.epoch:
            return None
        value = payload.get("value")
        if self.ns.decode is not None:
            try:
                value = self.ns.decode(value)
            except Exception:
                # 旧版本写下的形状对不上，当作没缓存过。一条坏条目不该让这次查询失败。
                logger.debug("%s 缓存条目解码失败，忽略", self.ns.name, exc_info=True)
                return None
            if value is None:
                return None
        return float(payload.get("created_at", 0.0)), key.epoch, value

    def _disk_write(self, key: CacheKey, created_at: float, value: Any) -> None:
        directory = self._epoch_dir(key.epoch)
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle_fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            payload = value if self.ns.encode is None else self.ns.encode(value)
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "epoch": key.epoch,
                        "created_at": created_at,
                        "tool": key.tool,
                        "symbol": key.symbol,
                        "value": payload,
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.replace(temp_path, os.path.join(directory, f"{key.digest()}.json"))
            temp_path = None
        except Exception:
            # A cache write must never surface as a tool error.
            logger.debug("Cache disk write failed ns=%s", self.ns.name, exc_info=True)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # -- public API ------------------------------------------------------

    def get(self, key: CacheKey) -> Optional[Any]:
        """Return a cached value, or None. Never raises.

        A cache fault must degrade to a miss: call sites sit inside the
        per-symbol try blocks, so an exception here would turn a serviceable
        request into a reported error.
        """
        try:
            return self._get(key)
        except Exception:
            logger.warning("Cache read failed ns=%s key=%s",
                           key.tool, key.symbol, exc_info=True)
            return None

    def _get(self, key: CacheKey) -> Optional[Any]:
        if not self.enabled:
            return None
        digest = key.digest()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is not None and self._fresh(key, entry[0], entry[1]):
                self.hits += 1
                return entry[2]
            # 只丢硬过期的。软过期的要留着——上游取不到时它就是兜底那份，
            # 在这里顺手 pop 掉，get_stale 就永远找不到东西了。
            if entry is not None and self._hard_expired(key, entry[0], entry[1]):
                self._entries.pop(digest, None)

        if not self.disk_enabled:
            with self._lock:
                self.misses += 1
            return None

        self._sweep_disk()
        loaded = self._disk_read(key)
        if loaded is None or not self._fresh(key, loaded[0], loaded[1]):
            with self._lock:
                self.misses += 1
            return None

        with self._lock:
            self._prune_locked(reserve=digest not in self._entries)
            self._entries[digest] = loaded
            self.hits += 1
        return loaded[2]

    def put(self, key: CacheKey, value: Any) -> None:
        """Store a value. Never raises.

        Call sites store *after* the response has been rendered, and several sit
        inside a try block that converts exceptions into a per-symbol error, so a
        write fault here would discard a report that was already produced
        successfully.
        """
        try:
            self._put(key, value)
        except Exception:
            logger.warning("Cache write failed ns=%s key=%s",
                           key.tool, key.symbol, exc_info=True)

    def _put(self, key: CacheKey, value: Any) -> None:
        if not self.enabled or value is None:
            return
        if key.phase == PHASE_LIVE and self.live_ttl_seconds <= 0:
            return
        # 失败和降级的结果不进缓存：一个纪元长达 64 小时，把一次瞬时降级腌进去，
        # 整个周末就都是那个样子。
        if self.ns.cacheable is not None and not self.ns.cacheable(value, key):
            return
        digest = key.digest()
        created_at = time.time()
        with self._lock:
            self._prune_locked(reserve=digest not in self._entries)
            self._entries[digest] = (created_at, key.epoch, value)
            self.stores += 1
        if self.disk_enabled:
            self._sweep_disk()
            self._disk_write(key, created_at, value)

    # -- 单飞与旧值 --------------------------------------------------------

    def age_of(self, digest: str) -> float:
        entry = self._entries.get(digest)
        return 0.0 if entry is None else max(0.0, time.time() - entry[0])

    def claim(self, digest: str) -> Optional[threading.Event]:
        """占坑。返回 None 表示这一轮由本调用方去取；返回 Event 表示别人在取，等它。"""
        with self._lock:
            existing = self._inflight.get(digest)
            if existing is not None:
                return existing
            self._inflight[digest] = threading.Event()
            return None

    def release(self, digest: str) -> None:
        with self._lock:
            event = self._inflight.pop(digest, None)
        if event is not None:
            event.set()

    def get_stale(self, key: CacheKey) -> Optional[tuple]:
        """软过期但没硬过期的旧值，连同它的岁数。

        只在上游取不到时才该调它——**硬过期的一律不给**，那是跨纪元的数据，
        不是"旧"，是答非所问。
        """
        if not self.enabled or not self.stale_on_error:
            return None
        entry = self._entries.get(key.digest())
        if entry is None or self._hard_expired(key, entry[0], entry[1]):
            return None
        return entry[2], max(0.0, time.time() - entry[0])

    def clear(self) -> None:
        """清空这个命名空间，内存和磁盘都清。

        磁盘也要清，否则 ``clear()`` 之后下一次读还会把旧条目从盘上捞回来——
        "重置"就不是重置了，``load(force=True)`` 也不会真的重取。
        """
        with self._lock:
            self._entries.clear()
            self._inflight.clear()
            self.hits = self.misses = self.stores = self.stale_serves = 0
            self._last_sweep_at = 0.0
        if not self.ns.disk:
            return
        try:
            for name in os.listdir(self.directory):
                if name.startswith(EPOCH_DIR_PREFIX):
                    shutil.rmtree(os.path.join(self.directory, name), ignore_errors=True)
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            logger.debug("Cache clear skipped disk ns=%s", self.ns.name, exc_info=True)


# ── 命名空间实例 ────────────────────────────────────────────────

_caches: dict[str, Cache] = {}
_cache_lock = threading.Lock()


def cache_for(name: str) -> Cache:
    """取某个命名空间的缓存实例，按需创建。"""
    existing = _caches.get(name)
    if existing is not None:
        return existing
    with _cache_lock:
        existing = _caches.get(name)
        if existing is None:
            ns = _NAMESPACES.get(name)
            if ns is None:
                raise KeyError(f"没有登记过的缓存命名空间：{name}")
            existing = Cache(ns)
            _caches[name] = existing
            logger.info(
                "Cache initialised ns=%s enabled=%s ttl=%.0fs max_entries=%s "
                "disk=%s epoch_bound=%s render=%s",
                ns.name, existing.enabled, existing.live_ttl_seconds,
                existing.max_entries, existing.disk_enabled, ns.epoch_bound,
                RENDER_FINGERPRINT,
            )
    return existing


ReportCache = Cache


def get_report_cache() -> Cache:
    """报告命名空间。外部按这个名字调过，保留。"""
    return cache_for(REPORT_NAMESPACE.name)


def set_report_cache(cache: Optional[Cache]) -> None:
    """替换进程级的报告缓存。测试用，生产不用。"""
    with _cache_lock:
        if cache is None:
            _caches.pop(REPORT_NAMESPACE.name, None)
        else:
            _caches[REPORT_NAMESPACE.name] = cache


def reset_caches() -> None:
    """清掉所有命名空间的实例。给测试用。"""
    with _cache_lock:
        _caches.clear()


def stats() -> dict:
    """每个命名空间的命中情况。接进 verify_release 的诊断一节。"""
    return {
        name: {
            "hits": c.hits, "misses": c.misses, "stores": c.stores,
            "stale_serves": c.stale_serves, "entries": len(c._entries),
        }
        for name, c in _caches.items()
    }


# ── 取用 API：查—取—存一次完成，内建单飞 ──────────────────────────


def get_or_load(
    ns: str,
    key: str,
    loader: Callable[[], Any],
    *,
    epoch: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> Optional[Entry]:
    """同步版。给已经跑在线程里的调用方（分级、日历、财务、market_events）。

    **单飞**：同一个 ``(ns, key)`` 并发进来只放一个 loader 出去，其余等结果。
    没有它就是缓存踩踏——缓存一空，所有在等的请求同时穿透到上游，而那正是最容易
    触发风控的时刻。实测过：冷进程并发两次调板块资金流，申万分级表被取了两份。

    **软过期后刷新失败继续用旧值**（``CACHE_STALE_ON_ERROR``），返回的 Entry
    ``fresh=False``，调用方必须在输出里标注。

    返回 None 表示 loader 没给出结果，也没有可用的旧值。
    """
    cache = cache_for(ns)
    cache_key = key_for(ns, key, epoch=epoch, now=now)
    digest = cache_key.digest()

    hit = cache.get(cache_key)
    if hit is not None:
        return Entry(value=hit, fresh=True, age_seconds=cache.age_of(digest))

    waiter = cache.claim(digest)
    if waiter is not None:
        # 别人正在取同一份，等它。醒来之后再查一次缓存即可。
        waiter.wait(timeout=_INFLIGHT_WAIT_SECONDS)
        hit = cache.get(cache_key)
        if hit is not None:
            return Entry(value=hit, fresh=True, age_seconds=cache.age_of(digest))

    try:
        value = loader()
    except Exception:
        logger.warning("%s 取数失败 key=%s", ns, key, exc_info=True)
        value = None
    finally:
        cache.release(digest)

    if value is not None:
        cache.put(cache_key, value)
        return Entry(value=value, fresh=True, age_seconds=0.0)

    stale = cache.get_stale(cache_key)
    if stale is not None:
        cache.stale_serves += 1
        logger.info("%s 用了 %.0f 秒前的旧值 key=%s（上游当前不可用）",
                    ns, stale[1], key)
        return Entry(value=stale[0], fresh=False, age_seconds=stale[1])
    return None


async def aget_or_load(
    ns: str,
    key: str,
    loader: Callable[[], Awaitable[Any]],
    *,
    epoch: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> Optional[Entry]:
    """异步版。给跑在事件循环上的调用方（报告、市场宽度）。

    单飞用 ``asyncio.Task`` 而不是 ``threading.Lock``——把锁带进事件循环会把整个
    服务堵住。``shield`` 那句是要紧的：一个等待者被取消，不能中断别人需要的加载。
    """
    cache = cache_for(ns)
    cache_key = key_for(ns, key, epoch=epoch, now=now)
    digest = cache_key.digest()

    hit = cache.get(cache_key)
    if hit is not None:
        return Entry(value=hit, fresh=True, age_seconds=cache.age_of(digest))

    tasks = _async_inflight.setdefault(id(asyncio.get_running_loop()), {})
    task = tasks.get(digest)
    if task is None or task.done():
        task = asyncio.ensure_future(loader())
        tasks[digest] = task
        task.add_done_callback(lambda done, d=digest: tasks.pop(d, None)
                               if tasks.get(d) is done else None)
    try:
        value = await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("%s 取数失败 key=%s", ns, key, exc_info=True)
        value = None

    if value is not None:
        cache.put(cache_key, value)
        return Entry(value=value, fresh=True, age_seconds=0.0)

    stale = cache.get_stale(cache_key)
    if stale is not None:
        cache.stale_serves += 1
        logger.info("%s 用了 %.0f 秒前的旧值 key=%s（上游当前不可用）",
                    ns, stale[1], key)
        return Entry(value=stale[0], fresh=False, age_seconds=stale[1])
    return None


#: 单飞等待的上限。等不到就自己去取——宁可多打一次上游，也不能把调用方挂死在
#: 一个可能永远不会 set 的事件上。
_INFLIGHT_WAIT_SECONDS = 30.0
_async_inflight: dict[int, dict] = {}
