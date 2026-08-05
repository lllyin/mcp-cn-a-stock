"""
Configuration settings for the QTF MCP server.
"""

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
