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
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .config import (
    REPORT_CACHE_DIR,
    REPORT_CACHE_DISK_ENABLED,
    REPORT_CACHE_ENABLED,
    REPORT_CACHE_INTRADAY_TTL_SECONDS,
    REPORT_CACHE_MAX_ENTRIES,
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


def is_cacheable_report(text: str) -> bool:
    """Reject renderings that captured a transient upstream failure."""
    if not text or not text.strip():
        return False
    return not any(marker in text for marker in TRANSIENT_MARKERS)


class ReportCache:
    """Two-tier epoch-bound cache: bounded memory over an optional disk tier."""

    def __init__(
        self,
        *,
        enabled: bool = REPORT_CACHE_ENABLED,
        live_ttl_seconds: float = REPORT_CACHE_INTRADAY_TTL_SECONDS,
        max_entries: int = REPORT_CACHE_MAX_ENTRIES,
        disk_enabled: bool = REPORT_CACHE_DISK_ENABLED,
        directory: str = REPORT_CACHE_DIR,
    ):
        self.enabled = enabled
        self.live_ttl_seconds = live_ttl_seconds
        self.max_entries = max_entries
        self.disk_enabled = disk_enabled
        self.directory = directory
        self._entries: dict[str, tuple[float, str, Any]] = {}
        self._lock = threading.Lock()
        self._last_sweep_at = 0.0
        self.hits = 0
        self.misses = 0
        self.stores = 0

    # -- policy ----------------------------------------------------------

    def _fresh(self, key: CacheKey, created_at: float, created_epoch: str) -> bool:
        if created_epoch != key.epoch:
            return False
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
            logger.debug("Report cache sweep skipped", exc_info=True)
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
            logger.debug("Report cache retired epoch directory %s", name)

    def _disk_read(self, key: CacheKey) -> Optional[tuple[float, str, Any]]:
        path = os.path.join(self._epoch_dir(key.epoch), f"{key.digest()}.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
            return None
        except OSError:
            logger.debug("Report cache disk read failed", exc_info=True)
            return None
        if payload.get("epoch") != key.epoch:
            return None
        return float(payload.get("created_at", 0.0)), key.epoch, payload.get("value")

    def _disk_write(self, key: CacheKey, created_at: float, value: Any) -> None:
        directory = self._epoch_dir(key.epoch)
        temp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            handle_fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "epoch": key.epoch,
                        "created_at": created_at,
                        "tool": key.tool,
                        "symbol": key.symbol,
                        "value": value,
                    },
                    handle,
                    ensure_ascii=False,
                )
            os.replace(temp_path, os.path.join(directory, f"{key.digest()}.json"))
            temp_path = None
        except Exception:
            # A cache write must never surface as a tool error.
            logger.debug("Report cache disk write failed", exc_info=True)
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
            logger.warning("Report cache read failed tool=%s symbol=%s",
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
            if entry is not None:
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
            logger.warning("Report cache write failed tool=%s symbol=%s",
                           key.tool, key.symbol, exc_info=True)

    def _put(self, key: CacheKey, value: Any) -> None:
        if not self.enabled or value is None:
            return
        if key.phase == PHASE_LIVE and self.live_ttl_seconds <= 0:
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

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = self.misses = self.stores = 0
            self._last_sweep_at = 0.0


_cache: Optional[ReportCache] = None
_cache_lock = threading.Lock()


def get_report_cache() -> ReportCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = ReportCache()
                logger.info(
                    "Report cache initialised enabled=%s live_ttl=%.0fs settle=%s "
                    "max_entries=%s disk=%s dir=%s render=%s",
                    _cache.enabled,
                    _cache.live_ttl_seconds,
                    SETTLE.strftime("%H:%M"),
                    _cache.max_entries,
                    _cache.disk_enabled,
                    _cache.directory,
                    RENDER_FINGERPRINT,
                )
    return _cache


def set_report_cache(cache: Optional[ReportCache]) -> None:
    """Replace the process-wide cache. Tests use this; production does not."""
    global _cache
    with _cache_lock:
        _cache = cache
