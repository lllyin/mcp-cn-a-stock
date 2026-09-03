"""
Configuration settings for the QTF MCP server.
"""

import datetime
import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

_FALSEY = {"0", "false", "no", "off", "disabled", "none", ""}


def _parse_bool(raw, default: bool) -> bool:
    """Parse common operator spellings for an environment switch."""
    if raw is None:
        return default
    return str(raw).strip().lower() not in _FALSEY


# AkShare Proxy Patch Configuration
AKSHARE_PROXY_ENABLED = _parse_bool(os.getenv("AKSHARE_PROXY_ENABLED"), True)
AKSHARE_PROXY_IP = os.getenv("AKSHARE_PROXY_GATEWAY") or os.getenv("AKSHARE_PROXY_IP")
AKSHARE_PROXY_PASSWORD = os.getenv("AKSHARE_PROXY_TOKEN") or os.getenv("AKSHARE_PROXY_PASSWORD")
AKSHARE_PROXY_RETRY = int(os.getenv("AKSHARE_PROXY_RETRY", os.getenv("AKSHARE_PROXY_PORT", "30")))
# Backward-compatible alias. Historically this variable was named PORT, but
# akshare-proxy-patch treats the third argument as retry count.
AKSHARE_PROXY_PORT = AKSHARE_PROXY_RETRY

# --- Outbound HTTP channel (qtf_mcp/datasource/http_channel.py) ---
# Some upstream quote hosts drop connections from plain HTTP clients, so requests
# to them are issued through one of three channels. The modes are mutually
# exclusive: the two non-plain implementations rewrite the same requests module
# attributes, so installing both would silently leave only the last one active.
#   proxy       - akshare-proxy-patch: authorised gateway, rotating egress, cookies
#   impersonate - local connection with a browser TLS fingerprint, no gateway
#   direct      - local connection with plain requests, i.e. the behaviour
#                 before this switch existed
#   auto        - proxy when the gateway is usable, otherwise impersonate
HTTP_MODES = ("auto", "proxy", "impersonate", "direct")
HTTP_MODE_DEFAULT = "auto"
# Accepted spelling for operators who think of the channel as a feature switch.
HTTP_MODE_ALIASES = {"off": "direct"}
# Requests per target host before giving up and replaying through plain requests.
HTTP_IMPERSONATE_RETRY = max(1, int(os.getenv("CN_STOCK_HTTP_IMPERSONATE_RETRY", "3")))
HTTP_IMPERSONATE_TIMEOUT = max(
    1.0,
    float(os.getenv("CN_STOCK_HTTP_IMPERSONATE_TIMEOUT", "8")),
)
# curl_cffi browser profile to impersonate. Fixed rather than random so a
# per-thread session can keep reusing its TLS connection.
HTTP_IMPERSONATE_PROFILE = os.getenv("CN_STOCK_HTTP_IMPERSONATE_PROFILE") or "chrome"
# Consecutive fully-failed hosts before the impersonated path goes on cooldown.
# Without it, an environment where impersonation can never succeed pays the
# retry budget plus the plain-requests replay on every single call.
HTTP_IMPERSONATE_FAILURE_THRESHOLD = max(
    1,
    int(os.getenv("CN_STOCK_HTTP_IMPERSONATE_FAILURE_THRESHOLD", "4")),
)
HTTP_IMPERSONATE_COOLDOWN = max(
    0.0,
    float(os.getenv("CN_STOCK_HTTP_IMPERSONATE_COOLDOWN_SECONDS", "300")),
)


# --- Upstream source breaker (qtf_mcp/datasource/cn_stock_source.py) ---
# Eastmoney rate-limits per endpoint: on 2026-09-03 the K-line and fund-flow
# endpoints on push2his refused this egress IP for over half an hour while the
# host's other paths stayed reachable. Every request then burned the full
# provider chain -- efinance retries, three impersonated attempts, AkShare
# retries -- before reaching the Tencent fallback that served it in ~0.2s.
# Skipping a source that is provably refusing saves 1.5-3.7s per request.
SOURCE_BREAKER_ENABLED = _parse_bool(os.getenv("CN_STOCK_SOURCE_BREAKER_ENABLED"), True)
# Consecutive failures before a source is skipped. Historical baseline is a
# scattered ~1.5% failure rate, so three in a row is 0.003% by chance; a real
# block produced 111 consecutive failures.
SOURCE_BREAKER_THRESHOLD = max(1, int(os.getenv("CN_STOCK_SOURCE_BREAKER_THRESHOLD", "3")))
# Cooldown before one request is allowed through to probe. Half-open probing
# means this value only bounds recovery latency, not the cost of staying open.
SOURCE_BREAKER_COOLDOWN_SECONDS = max(
    1.0,
    float(os.getenv("CN_STOCK_SOURCE_BREAKER_COOLDOWN_SECONDS", "120")),
)


# --- Fund-flow page fallback (qtf_mcp/datasource/fund_flow_page.py) ---
# When the Eastmoney fund-flow endpoint refuses us, the same data is on
# data.eastmoney.com/zjlx/<code>.html, which the browser tier can already load.
# That path costs no gateway credits, but it costs a Chromium page load, so it
# must never become the steady state under load: with the endpoint failing for
# every symbol, an unbounded fallback would put four page loads per request
# behind a semaphore of two.
FUND_FLOW_PAGE_FALLBACK_ENABLED = _parse_bool(
    os.getenv("CN_STOCK_FUND_FLOW_PAGE_FALLBACK_ENABLED"), True
)
# Page loads allowed to run at once, on top of whatever the realtime tier is
# doing. Kept below the browser semaphore so the fallback cannot starve the
# intraday realtime path, which has no alternative source at all.
FUND_FLOW_PAGE_FALLBACK_CONCURRENCY = max(
    1,
    int(os.getenv("CN_STOCK_FUND_FLOW_PAGE_FALLBACK_CONCURRENCY", "1")),
)
# How long a request waits for a fallback slot before giving up and rendering
# the "no fund-flow data" line. Skipping is the right answer under load: queueing
# here would trade a missing section for a much slower response.
FUND_FLOW_PAGE_FALLBACK_WAIT_SECONDS = max(
    0.0,
    float(os.getenv("CN_STOCK_FUND_FLOW_PAGE_FALLBACK_WAIT_SECONDS", "0.5")),
)
# How long to wait for the historical table to fill after the page loads. The
# table is Ajax-filled from the same endpoint the API path uses, so when that
# endpoint is refusing, this wait is paid in full and buys nothing: on
# 2026-09-03 a futile attempt turned a 6.7s request into 20.1s. It either fills
# in a second or two or not at all, so the budget is small.
FUND_FLOW_PAGE_TABLE_WAIT_SECONDS = max(
    0.5,
    float(os.getenv("CN_STOCK_FUND_FLOW_PAGE_TABLE_WAIT_SECONDS", "4")),
)
# Consecutive futile page loads before the fallback is skipped entirely. Lower
# than the HTTP source breaker because each attempt costs a Chromium page load
# rather than a sub-second request, so two wasted attempts already outweigh
# what a third could recover.
FUND_FLOW_PAGE_FALLBACK_FAILURE_THRESHOLD = max(
    1,
    int(os.getenv("CN_STOCK_FUND_FLOW_PAGE_FALLBACK_FAILURE_THRESHOLD", "2")),
)
FUND_FLOW_PAGE_FALLBACK_COOLDOWN_SECONDS = max(
    1.0,
    float(os.getenv("CN_STOCK_FUND_FLOW_PAGE_FALLBACK_COOLDOWN_SECONDS", "300")),
)


class HttpModeError(ValueError):
    """Raised when an explicitly requested channel mode cannot be honoured."""


def resolve_http_mode(
    requested=None,
    proxy_enabled: bool = AKSHARE_PROXY_ENABLED,
    proxy_gateway=AKSHARE_PROXY_IP,
) -> tuple[str, str]:
    """Return the effective channel mode plus the reason, for startup logging.

    ``auto`` never raises: an unusable gateway degrades to ``impersonate`` so
    that a partial configuration cannot stop the service from starting. Only an
    explicit ``proxy`` request is strict, because there the intent is stated.
    """
    raw = requested if requested is not None else os.getenv("CN_STOCK_HTTP_MODE")
    mode = str(raw or "").strip().lower()
    mode = HTTP_MODE_ALIASES.get(mode, mode)
    if not mode:
        mode = HTTP_MODE_DEFAULT
    if mode not in HTTP_MODES:
        return HTTP_MODE_DEFAULT, f"invalid_value:{mode}"

    if mode == "proxy":
        if not proxy_gateway:
            raise HttpModeError(
                "CN_STOCK_HTTP_MODE=proxy requires AKSHARE_PROXY_GATEWAY; "
                "use auto to fall back to impersonate instead."
            )
        return "proxy", "requested"
    if mode in ("impersonate", "direct"):
        return mode, "requested"

    if not proxy_enabled:
        return "impersonate", "auto:proxy_disabled"
    if not proxy_gateway:
        return "impersonate", "auto:proxy_gateway_missing"
    return "proxy", "auto:proxy_configured"

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
REPORT_CACHE_ENABLED = _parse_bool(os.getenv("CN_STOCK_REPORT_CACHE_ENABLED"), True)
# 盘中数值持续变动，复用受短 TTL 约束，只用于合并突发重复请求。置 0 则盘中绝不复用。
# 默认 30 秒是陈旧度与积分的折中：基于下游真实捕获比对，60 秒窗口内主力净流入的
# P90 相对漂移为 21%，30 秒窗口降至 7.7%，而代价只是约 2.8 个百分点的积分降幅。
REPORT_CACHE_LIVE_TTL_SECONDS = max(
    0.0,
    float(os.getenv("CN_STOCK_REPORT_CACHE_LIVE_TTL_SECONDS", "30")),
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
