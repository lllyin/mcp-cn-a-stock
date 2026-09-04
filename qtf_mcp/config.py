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
# 默认关闭：网关是付费的，每次认证都计积分，而 impersonate 通道在同样的东财主机
# 上已经能独立取到数据。没有显式开启的部署不应该在第一次调用时就开始扣费。
AKSHARE_PROXY_ENABLED = _parse_bool(os.getenv("AKSHARE_PROXY_ENABLED"), False)
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
# Upper bound on waiting for the historical table to fill. It is only a
# backstop: the wait aborts as soon as a fund-flow request is refused, so a
# blocked page costs nothing regardless of this value. Measured 2026-09-04: a
# warm browser fills the table in 0.51s, a cold start needs over 4s, so a small
# fixed budget silently returned an empty table on the first load of a process.
FUND_FLOW_PAGE_TABLE_WAIT_SECONDS = max(
    0.5,
    float(os.getenv("CN_STOCK_FUND_FLOW_PAGE_TABLE_WAIT_SECONDS", "15")),
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

# 一次请求内允许的页面加载次数。
#
# 定 2 的依据重测过一次。原注释写的是「8 轮全新浏览器,第三次一次都没多救回来」,
# 但那次实测早于 TABLE_WAIT_SECONDS 改到 15s 的修复,而那条注释自己写着"小预算
# 会静默返回空表"——当时量到的失败里混着表没填完的超时,依据不成立。
#
# 2026-09-04 重测,16 个沪深标的串行、逐次记录结果:
#
#   第 1 次加载命中   14 个
#   第 2 次加载命中    1 个   ← SH601318: captcha -> reload -> ok,只有 reload 救得回来
#   全部失败           1 个   ← SH600519: 三次全是 captcha,第三次也没救回来
#
# 结论和原来一致但依据换了:2 是对的——第 2 次（同一个 tab 上 reload）确实能救
# 回标的,第 3 次（开新 tab）在两次实测里都是零收益,而它要多付一次开页面。
#
# 改名的原因:这个值原先只在"本进程还没成功取到过数据"时生效,成功过一次之后
# 预算就塌到 1（只 goto、不 reload）。那个区分站不住:
#
#   - 依据上站不住。它假设"成功过一次说明上游在放行",而项目自己的注释记着被拒
#     是逐次随机的、8 轮里有 3 轮当场重试就能成功。
#   - 代价是实测的。2026-09-04 并发 4×4 实测,批 1 有一个标的成功之后,后面
#     SH603986 和 SH600030 都只加载了一次就拿着 history=0 放弃了。
#   - 收益是零。重试只在"这次没拿到想要的数据"时才发生,顺利路径一次都不多花,
#     所以省不下任何东西。
#
# 旧环境变量名继续认,部署里已经配着的不用改。
FUND_FLOW_PAGE_MAX_LOADS = max(
    1,
    int(
        os.getenv("CN_STOCK_FUND_FLOW_PAGE_MAX_LOADS")
        or os.getenv("CN_STOCK_FUND_FLOW_PAGE_COLD_ATTEMPTS")
        or "2"
    ),
)
# 兼容旧名字。
FUND_FLOW_PAGE_COLD_ATTEMPTS = FUND_FLOW_PAGE_MAX_LOADS

# 把无头浏览器的自报特征改成普通浏览器的样子。默认开。
#
# 起因是实测发现 sec-ch-ua 在每个请求头里写着 "HeadlessChrome";v="145" —— 这不是
# 细微指纹而是自报身份，而且和我们原先硬编码的 UA（Chrome/120）自相矛盾；在 Linux
# 服务器上还会变成 UA 说 Macintosh、sec-ch-ua-platform 说 Linux 的第二重矛盾。
#
# 实测 2026-09-04 三个方案的指纹与内存（浏览器进程树 footprint，四个标的）：
#   现状 headless_shell + 硬编码 UA : sec-ch-ua 说 HeadlessChrome，63.7/95 MiB
#   换完整 Chromium 新无头          : 指纹全对，但 323/401 MiB（+260，超预算）
#   本方案（CDP 覆盖 + locale）     : 指纹全对，65.7/111 MiB（+2/+16）
# 所以走本方案。置 0 可一键退回原样，用于对照或伪装反而招致拦截时回滚。
FUND_FLOW_PAGE_DISGUISE = _parse_bool(
    os.getenv("CN_STOCK_FUND_FLOW_PAGE_DISGUISE"), True
)

# 对外声明哪个平台：auto | real | macos | windows。
#   auto  Windows 和 macOS 照实报，其余（服务器上就是 Linux）统一报 macOS。
#         Linux 桌面在真实访客里占比极低，照实报等于自带一个少数派特征。
#   real  照实报，用于在部署机上做对照
# 注意代价：声明 macOS 之后 WebGL renderer 和字体列表仍是 Linux 的样子，若对端
# 交叉核对到那一层，声明 macOS 反而更可疑。所以要在部署机上用 blocked_captcha
# 的占比比一比 auto 与 real，别凭感觉定。
FUND_FLOW_PAGE_CLAIM_PLATFORM = (
    os.getenv("CN_STOCK_FUND_FLOW_PAGE_CLAIM_PLATFORM") or "auto"
).strip().lower()

def _parse_range_ms(raw, default: str) -> tuple[float, float]:
    """把 "250,350" 解析成 (下界, 上界) 毫秒。

    只写一个数就是固定值，写 "0" 就是关闭，顺序写反也认。数字写坏了直接在启动时
    抛 ValueError：与本文件其它数值项一致，宁可起不来，也别让运维以为自己配上了。
    """
    text = str(raw).strip() if raw not in (None, "") else default
    values = sorted(max(0.0, float(part)) for part in text.split(",") if part.strip())
    if not values:
        raise ValueError(f"区间为空: {raw!r}")
    return values[0], values[-1]


# 同一个 tab 上 reload 之前的随机等待区间，毫秒，写作 "下界,上界"。只作用在重试
# 路径上：那一次已经没拿到数据、本来就要再付一次页面加载，所以顺利路径一秒都不
# 多花。睡的次数是每个 tab 的那次 reload 各一次，即 ⌊COLD_ATTEMPTS/2⌋ 次，
# 默认就是每个标的每次请求最多多等一次 350ms。
#
# 为什么随机而不是固定：没拿到数据后 0 毫秒就刷新同一个页面，本身是个机器节奏。
# 收益没有实测数据支撑——本机出口 IP 处于持续封锁态，量不出命中率变化；写下这一点
# 是为了以后别把它当成已验证的结论。代价可量化，见下面副作用一条。
#
# 副作用要连带看隔壁阈值：资金流兜底走这条路时，名额是在整个 fetch_history_page
# 外面持有的，多睡 300ms 就多占 300ms，而 FALLBACK_WAIT_SECONDS 只有 0.5s，
# 可能让"名额已满跳过"更容易触发。重试路径本身少见，所以判断是可以接受；
# 真在日志里看到跳过变多，把这两个值一起调。置 0 关闭。
FUND_FLOW_PAGE_RETRY_DELAY_MS = _parse_range_ms(
    os.getenv("CN_STOCK_FUND_FLOW_PAGE_RETRY_DELAY_MS"), "250,350"
)

# 调试开关，默认关。开启后浏览器有头运行、抓完不关页面，用于人工观察页面到底
# 渲染成了什么样。两者都会显著抬高内存（每个页面是一个独立渲染进程），只在排查
# 时开；Linux 上有头模式需要 DISPLAY，start.sh 会拉起 Xvfb。
FUND_FLOW_PAGE_HEADFUL = _parse_bool(
    os.getenv("CN_STOCK_FUND_FLOW_PAGE_HEADFUL"), False
)
FUND_FLOW_PAGE_KEEP_PAGES = _parse_bool(
    os.getenv("CN_STOCK_FUND_FLOW_PAGE_KEEP_PAGES"), False
)

# 解析结果的复用窗口。页面级单飞只能合并并发的加载，而实时预取和资金流兜底在
# 一次请求里是先后发生的（实测相隔约 4 秒），于是同一个页面被加载两次。每次加载
# 都可能再被拒一次，不只是一次 Chromium 开销。默认与报告缓存的盘中 TTL 对齐，
# 不引入超出既有约定的陈旧度。置 0 关闭复用。
FUND_FLOW_PAGE_REUSE_SECONDS = max(
    0.0,
    float(os.getenv("CN_STOCK_FUND_FLOW_PAGE_REUSE_SECONDS", "30")),
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
