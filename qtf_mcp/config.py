"""
Configuration settings for the QTF MCP server.
"""

import datetime
import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# AkShare Proxy Patch Configuration
AKSHARE_PROXY_IP = os.getenv("AKSHARE_PROXY_GATEWAY") or os.getenv("AKSHARE_PROXY_IP")
AKSHARE_PROXY_PASSWORD = os.getenv("AKSHARE_PROXY_TOKEN") or os.getenv("AKSHARE_PROXY_PASSWORD")
AKSHARE_PROXY_RETRY = int(os.getenv("AKSHARE_PROXY_RETRY", os.getenv("AKSHARE_PROXY_PORT", "30")))
# Backward-compatible alias. Historically this variable was named PORT, but
# akshare-proxy-patch treats the third argument as retry count.
AKSHARE_PROXY_PORT = AKSHARE_PROXY_RETRY

# Synchronous AkShare/efinance calls are I/O bound. Keep the executor bounded,
# while allowing deployments to tune it for their upstream capacity.
DATA_FETCH_MAX_WORKERS = max(1, int(os.getenv("CN_STOCK_DATA_FETCH_MAX_WORKERS", "8")))
# Bound submitted and running work separately from the executor's unbounded
# internal queue. The default keeps one queued task per worker at saturation.
DATA_FETCH_MAX_IN_FLIGHT = max(
    1,
    int(os.getenv("CN_STOCK_DATA_FETCH_MAX_IN_FLIGHT", "16")),
)

# Bound concurrent report batches before they fan out into data and browser work.
BATCH_QUERY_CONCURRENCY = max(
    1,
    int(os.getenv("CN_STOCK_BATCH_QUERY_CONCURRENCY", "2")),
)

# Financial abstracts normally change only after periodic reports are published.
# Cache successful results to keep recurring batch scans off the upstream API.
FINANCE_CACHE_TTL_SECONDS = max(
    0.0,
    float(os.getenv("CN_STOCK_FINANCE_CACHE_TTL_SECONDS", "21600")),
)
FINANCE_CACHE_MAX_ENTRIES = max(
    1,
    int(os.getenv("CN_STOCK_FINANCE_CACHE_MAX_ENTRIES", "512")),
)

# --- Report cache (qtf_mcp/cache.py) ---
# A rendered report is reusable only inside the market epoch that produced it,
# so the cache never changes what a tool would return. Disabling the master
# switch removes the cache from the call path entirely.
_FALSEY = {"0", "false", "no", "off", "disabled", "none", ""}


def _parse_bool(raw, default: bool) -> bool:
    """Treat the usual spellings of "off" as off.

    This is the emergency switch for a cache that can serve an unchanged report
    for many hours, so an operator typing ``off`` or ``FALSE`` must not silently
    get the opposite of what they intended.
    """
    if raw is None:
        return default
    return str(raw).strip().lower() not in _FALSEY


REPORT_CACHE_ENABLED = _parse_bool(os.getenv("CN_STOCK_REPORT_CACHE_ENABLED"), True)
# Inside the trading session the numbers keep moving, so reuse is bounded by a
# short TTL that only collapses bursts. Set to 0 to never reuse intraday.
REPORT_CACHE_LIVE_TTL_SECONDS = max(
    0.0,
    float(os.getenv("CN_STOCK_REPORT_CACHE_LIVE_TTL_SECONDS", "60")),
)
REPORT_CACHE_MAX_ENTRIES = max(
    1,
    int(os.getenv("CN_STOCK_REPORT_CACHE_MAX_ENTRIES", "512")),
)
# Second tier surviving restarts. Closed epochs span 16h (64h over a weekend),
# so an in-memory-only cache loses most of its value on any redeploy.
REPORT_CACHE_DISK_ENABLED = _parse_bool(
    os.getenv("CN_STOCK_REPORT_CACHE_DISK_ENABLED"), True
)
# Resolved against the package root, not the daemon's CWD: the sweeper deletes
# directories under here, so a relative value read from a copied .env must not
# land somewhere unexpected.
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_CACHE_DIR = os.path.normpath(
    os.path.join(_PROJECT_ROOT, os.getenv("CN_STOCK_REPORT_CACHE_DIR") or ".runtime/report-cache")
)


def _parse_hhmm(raw, default: datetime.time) -> datetime.time:
    """Parse a four-digit HHMM clock, falling back to ``default``."""
    text = str(raw or "").strip()
    if len(text) != 4 or not text.isdigit():
        return default
    try:
        return datetime.time(int(text[:2]), int(text[2:]))
    except ValueError:
        return default


# When the post-close settle buffer ends and full-epoch reuse begins. The market
# closes at 15:00, but the Eastmoney fund-flow page finalises a few minutes
# later, so the default leaves a 30-minute buffer. cache.py clamps this into
# [15:00, 17:00]; see the note there for why values outside that range are unsafe.
REPORT_CACHE_SETTLE_TIME = _parse_hhmm(
    os.getenv("CN_STOCK_REPORT_CACHE_SETTLE_HHMM"), datetime.time(15, 30)
)

# --- Market Indices Configuration ---
import json
_CONF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "confs"))
_INDICES_FILE = os.path.join(_CONF_DIR, "indices.json")

SH_INDICES: set[str] = set()
SZ_INDICES: set[str] = set()
ALL_INDICES: set[str] = set()

try:
    if os.path.exists(_INDICES_FILE):
        with open(_INDICES_FILE, "r", encoding="utf-8") as f:
            _conf = json.load(f)
            SH_INDICES = set(_conf.get("sh_indices", []))
            SZ_INDICES = set(_conf.get("sz_indices", []))
            ALL_INDICES = SH_INDICES | SZ_INDICES
except Exception:
    # 基础兜底名单
    SH_INDICES = {"000001", "000300", "000016", "000905", "000688", "000852"}
    SZ_INDICES = {"399001", "399006", "399005", "399300", "399007"}
    ALL_INDICES = SH_INDICES | SZ_INDICES
