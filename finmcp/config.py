"""
Configuration settings for the QTF MCP server.
"""

import datetime
import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

_FALSEY = {"0", "false", "no", "off", "disabled", "none", ""}

# 所有配置项统一不带前缀。想和别的程序共存、担心重名时，设 ENV_PREFIX，例如
#
#     ENV_PREFIX=CNSTOCK_
#
# 之后全部配置就读 CNSTOCK_<NAME>。前缀这件事只发生在下面这一个函数里，声明处
# 一律写裸名字——原先每一项都顶着 CN_STOCK_ 前缀，读起来吵，改起来还要改几十处。
ENV_PREFIX = os.getenv("ENV_PREFIX", "")


def env(name: str, default=None):
    """按配置名取值。名字不带前缀写，前缀由 ENV_PREFIX 统一决定。"""
    return os.getenv(f"{ENV_PREFIX}{name}", default)


def _parse_bool(raw, default: bool) -> bool:
    """Parse common operator spellings for an environment switch."""
    if raw is None:
        return default
    return str(raw).strip().lower() not in _FALSEY


# AkShare Proxy Patch Configuration
# 默认关闭：网关是付费的，每次认证都计积分，而 impersonate 通道在同样的东财主机
# 上已经能独立取到数据。没有显式开启的部署不应该在第一次调用时就开始扣费。
# 这一组保留 AKSHARE_PROXY_ 前缀：它们配的是第三方插件 akshare-proxy-patch，
# 前缀就是插件的身份，去掉之后看不出这几项跟哪个组件走。
AKSHARE_PROXY_ENABLED = _parse_bool(env("AKSHARE_PROXY_ENABLED"), False)
AKSHARE_PROXY_IP = env("AKSHARE_PROXY_GATEWAY") or env("AKSHARE_PROXY_IP")
AKSHARE_PROXY_PASSWORD = env("AKSHARE_PROXY_TOKEN") or env("AKSHARE_PROXY_PASSWORD")
AKSHARE_PROXY_RETRY = int(env("AKSHARE_PROXY_RETRY", env("AKSHARE_PROXY_PORT", "30")))
# Backward-compatible alias. Historically this variable was named PORT, but
# akshare-proxy-patch treats the third argument as retry count.
AKSHARE_PROXY_PORT = AKSHARE_PROXY_RETRY

# --- Outbound HTTP channel (finmcp/datasource/http_channel.py) ---
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
IMPERSONATE_RETRY = max(1, int(env("IMPERSONATE_RETRY", "3")))
IMPERSONATE_TIMEOUT_SECONDS = max(1.0, float(env("IMPERSONATE_TIMEOUT_SECONDS", "8")))
# curl_cffi browser profile to impersonate. Fixed rather than random so a
# per-thread session can keep reusing its TLS connection.
IMPERSONATE_BROWSER = env("IMPERSONATE_BROWSER") or "chrome"
# Consecutive requests that exhausted their retries before the impersonated path
# goes on cooldown -- counted per request, not per host. Without it, an
# environment where impersonation can never succeed pays the retry budget plus
# the plain-requests replay on every single call.
IMPERSONATE_SUSPEND_AFTER_FAILURES = max(
    1,
    int(env("IMPERSONATE_SUSPEND_AFTER_FAILURES", "4")),
)
IMPERSONATE_SUSPEND_SECONDS = max(
    0.0,
    float(env("IMPERSONATE_SUSPEND_SECONDS", "300")),
)


# --- Upstream source breaker (finmcp/datasource/cn_stock_source.py) ---
# Eastmoney rate-limits per endpoint: on 2026-09-03 the K-line and fund-flow
# endpoints on push2his refused this egress IP for over half an hour while the
# host's other paths stayed reachable. Every request then burned the full
# provider chain -- efinance retries, three impersonated attempts, AkShare
# retries -- before reaching the Tencent fallback that served it in ~0.2s.
# Skipping a source that is provably refusing saves 1.5-3.7s per request.
SOURCE_BREAKER_ENABLED = _parse_bool(env("SOURCE_BREAKER_ENABLED"), True)
# Consecutive failures before a source is skipped. Historical baseline is a
# scattered ~1.5% failure rate, so three in a row is 0.003% by chance; a real
# block produced 111 consecutive failures.
SOURCE_BREAKER_OPEN_AFTER_FAILURES = max(1, int(env("SOURCE_BREAKER_OPEN_AFTER_FAILURES", "3")))
# Cooldown before one request is allowed through to probe. Half-open probing
# means this value only bounds recovery latency, not the cost of staying open.
SOURCE_BREAKER_COOLDOWN_SECONDS = max(
    1.0,
    float(env("SOURCE_BREAKER_COOLDOWN_SECONDS", "120")),
)


# --- Fund-flow page fallback (finmcp/datasource/fund_flow_page.py) ---
# When the Eastmoney fund-flow endpoint refuses us, the same data is on
# data.eastmoney.com/zjlx/<code>.html, which the browser tier can already load.
# That path costs no gateway credits, but it costs a Chromium page load, so it
# must never become the steady state under load: with the endpoint failing for
# every symbol, an unbounded fallback would put four page loads per request
# behind a semaphore of two.
FUND_FLOW_PAGE_ENABLED = _parse_bool(env("FUND_FLOW_PAGE_ENABLED"), True)
# 同时允许几次页面加载。2026-09-04 实测（一批 4 标的 × 3 批，逐次记录）：
#
#   名额  获取率    最慢一批   单次加载
#     1   12/12     12.6s     ~2.3s
#     2   12/12      4.6s     ~2.3s     ← 取这个
#     4   12/12      5.4s      4.6s     并发再高就开始互相拖慢
#
# 并发不降获取率——风控没有因为同一出口 IP 并发而加严，所以"不敢并发"这个顾虑
# 不成立。4 不取：获取率没涨、单次加载反而变慢、内存最贵。
#
# 2026-09-05 部署机实测把它推到 3：那一轮 5 次丢失**全部**是"名额已满"，一次
# 上游拒绝都没有（29 次页面加载 29 次成功、零被拒零验证码），也就是说剩下的损失
# 纯粹是容量问题。用日志里的占用区间做离散事件回放：
#
#   名额  争用批次里服务到的标的数
#     2   7/11    ← 与实测吻合
#     3   9/11    多救回 2 个
#     4   11/11   多救回 4 个
#
# 取 3 不取 4：4 要把浏览器页面上限也推到 4，多两个并发渲染进程，而 §四 的
# 500 MiB 判定还没在 Ubuntu 上复测过；3 只多一个。必须和 BROWSER_MAX_PAGES
# 一起提——只提这一个，浏览器信号量会立刻变成新的瓶颈，收益为零。
#
# 2026-09-05 复盘：上面那段回放当时把"名额"当成了主要旋钮，其实不是。名额只决定
# 「前几个能立刻拿到」，第 N+1 个能不能等到，取决于 W 和 hold 的关系——见下一项。
# 名额留在 3 是因为它对慢尾巴更稳：批 4 标的、名额 3 时第 4 个只等一个 hold，
# p90 也在 W 之内；名额 2 时第 4 个要等两个 hold，p90 下就超了。
FUND_FLOW_PAGE_CONCURRENCY = max(
    1,
    int(env("FUND_FLOW_PAGE_CONCURRENCY", "3")),
)
# 单个标的等一个名额的上限。
#
# 原值 0.5s 是这条链上最贵的一个数：实测每个标的持有名额约 2.26s（要重试的到
# 6.6s，名额是在整个 fetch_history_page 外面持有的），于是
#
#     W / hold = 0.5 / 2.26 = 0.22
#
# 一批 4 个标的同时到达，算术上最多 1 个排得到，另外 3 个在碰到上游之前就被判了
# 缺数据。这不是负载下的偶发，是必然。补全优先，就得让 W 至少大于 hold。
#
# 但单纯放大 W 会把补全问题换成无界延迟问题：10×4 = 40 个标的、名额 1 个，就是
# 40×2.26 ≈ 90s。所以 W 只管单个标的，整体上界交给下面的请求预算。两者都置 0
# 可以退回"不等，直接跳过"。
#
# 2026-09-05 部署机实测把它从 3s 提到 8s。上一次我调错了旋钮：算出 W/hold 不对
# 之后去提名额（1→2→3），但决定成败的一直是 W 和 hold 的关系，名额只决定"前几个
# 能立刻拿到"。服务器上 hold 是 p50 3.38s / p90 7.49s（本机只有 2.26s，所以本机
# 量不出这个问题），于是：
#
#     一批 4 个标的，前 3 个立刻拿到名额，第 4 个要等一个 hold
#     第 4 个需等 3.38s  vs  W=3.0s  ->  超上限，必被跳过
#
# **每一批 4 标的的第 4 个，结构上永远拿不到名额。** 那一轮丢的 4 项资金流正好
# 全部来自这里（名额已满 2 + 请求预算用尽 2），一次上游拒绝都没有。
#
# 取 8 是为了盖住 p90 的 7.49s，不是盖 p50——盖 p50 只是把必丢变成一半丢。
# 代价：需要兜底且满批时，尾部多等 3.4s（p90 情形 7.5s）。那一轮全程只有 20 次
# 页面加载，这条路不热，按第一条"数据完整 > 性能"这个换法是划算的。
FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS = max(
    0.0,
    float(env("FUND_FLOW_PAGE_QUEUE_WAIT_SECONDS", "8")),
)
# 一次请求里所有标的加起来最多为等名额花掉多少秒。
#
# 要解决的是"同一个请求里的标的在互相抢名额"：mcp_app 对 raw_symbols 是
# asyncio.gather 全并发，4 个标的各自独立去抢，3 个输给了自己的兄弟。按标的
# 计时无法表达"这一批整体值得等多久",所以预算按 request_id 归集。
#
# 这是个**截止时间**不是配额：从这一批第一个标的进来时开始计时，每个标的拿到的
# 耐心是 min(W, 剩余)。所以它必须大于 W，否则 W 提上去也会立刻被它削回来。
#
# 2026-09-05 随 W 从 3s 提到 8s 一起，这里从 8s 提到 15s。同一轮里有 2 项资金流
# 是被"请求预算已用尽"挡掉的（另 2 项是"名额已满"），说明 8s 对满批已经不够。
# 15s 的量级：4 个标的、名额 3 个，最坏是第 4 个等一个 p90 hold(7.5s) 再加自己的
# 加载，落在 15s 内。40 个标的的大批仍然会在 15s 处截断——延迟有界这一点不变。
# 置 0 关闭请求级预算,退回纯按标的计时。
FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS = max(
    0.0,
    float(env("FUND_FLOW_PAGE_REQUEST_BUDGET_SECONDS", "15")),
)
# Upper bound on waiting for the historical table to fill. It is only a
# backstop: the wait aborts as soon as a fund-flow request is refused, so a
# blocked page costs nothing regardless of this value. Measured 2026-09-04: a
# warm browser fills the table in 0.51s, a cold start needs over 4s, so a small
# fixed budget silently returned an empty table on the first load of a process.
FUND_FLOW_PAGE_TABLE_WAIT_SECONDS = max(
    0.5,
    float(env("FUND_FLOW_PAGE_TABLE_WAIT_SECONDS", "15")),
)
# 关掉整层兜底之前允许多少次白付的页面加载。
#
# 原值是"连续 2 次 + 冷却 300s"。它在名额只有 1 个的时候被实测证伪：一批里只有
# 1 个标的真的碰到上游,"连续 2 次失败"就不再是信号而是噪声。2026-09-04 把逐次
# 实测结果重放过熔断器:
#
#   预算 3   SH600519 ❌  SH601318 ✅第2次  其余 14 个 ✅第1次  ->  15/16 = 94%
#   预算 1   SH600519 ❌  SH601318 ❌  ⚡熔断打开 -> 后面 14 个全跳过  ->  0/16 = 0%
#
# 同一份上游行为,2 次噪声换来整层停 5 分钟。而同一批标的绕开闸门实测可获取
# 15/16 = 94%,单次被拒率只有 12.5% —— 这个量级的失败是噪声,不该触发停摆。
#
# 所以改成滑动窗口计数:60 秒内累计 4 次失败才开,冷却 60s。窗口计数比连续计数
# 更贴合"逐次随机被拒"这个已实测的上游行为:连续计数会被一次成功清零,也会被
# 两次噪声凑满,两头都不准。
FUND_FLOW_PAGE_OPEN_AFTER_FAILURES = max(
    1,
    int(env("FUND_FLOW_PAGE_OPEN_AFTER_FAILURES", "4")),
)
# 失败计数的滑动窗口秒数。置 0 退回原来的"连续失败"语义。
FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS = max(
    0.0,
    float(env("FUND_FLOW_PAGE_FAILURE_WINDOW_SECONDS", "60")),
)
# 冷却从 300s 降到 60s。300s 的原意是"被拒是分钟级的,等久点省页面加载",但那是
# 在阈值 2 容易误触的前提下;阈值改成窗口计数后误触少了,冷却长反而是纯损失——
# 实测持续封锁态确实是分钟级,60s 足够避开一轮,又不会在风控解除后继续空转 4 分钟。
FUND_FLOW_PAGE_COOLDOWN_SECONDS = max(
    1.0,
    float(env("FUND_FLOW_PAGE_COOLDOWN_SECONDS", "60")),
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
# 这一项原先只在"本进程还没成功取到过数据"时生效,成功过一次之后预算就塌到 1
# （只 goto、不 reload）。那个区分站不住:
#
#   - 依据上站不住。它假设"成功过一次说明上游在放行",而项目自己的注释记着被拒
#     是逐次随机的、8 轮里有 3 轮当场重试就能成功。
#   - 代价是实测的。2026-09-04 并发 4×4 实测,批 1 有一个标的成功之后,后面
#     SH603986 和 SH600030 都只加载了一次就拿着 history=0 放弃了。
#   - 收益是零。重试只在"这次没拿到想要的数据"时才发生,顺利路径一次都不多花,
#     所以省不下任何东西。
FUND_FLOW_PAGE_MAX_LOADS = max(1, int(env("FUND_FLOW_PAGE_MAX_LOADS", "2")))

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
BROWSER_DISGUISE = _parse_bool(
    env("BROWSER_DISGUISE"), True
)

# 对外声明哪个平台：auto | real | macos | windows。
#   auto  Windows 和 macOS 照实报，其余（服务器上就是 Linux）统一报 macOS。
#         Linux 桌面在真实访客里占比极低，照实报等于自带一个少数派特征。
#   real  照实报，用于在部署机上做对照
# 注意代价：声明 macOS 之后 WebGL renderer 和字体列表仍是 Linux 的样子，若对端
# 交叉核对到那一层，声明 macOS 反而更可疑。所以要在部署机上用 blocked_captcha
# 的占比比一比 auto 与 real，别凭感觉定。
BROWSER_CLAIM_PLATFORM = (
    env("BROWSER_CLAIM_PLATFORM") or "auto"
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
# 多花。睡的次数是每个 tab 的那次 reload 各一次，即 ⌊MAX_LOADS/2⌋ 次，
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
    env("FUND_FLOW_PAGE_RETRY_DELAY_MS"), "250,350"
)

# 调试开关，默认关。开启后浏览器有头运行、抓完不关页面，用于人工观察页面到底
# 渲染成了什么样。两者都会显著抬高内存（每个页面是一个独立渲染进程），只在排查
# 时开；Linux 上有头模式需要 DISPLAY，start.sh 会拉起 Xvfb。
BROWSER_HEADFUL = _parse_bool(
    env("BROWSER_HEADFUL"), False
)
BROWSER_KEEP_PAGES = _parse_bool(
    env("BROWSER_KEEP_PAGES"), False
)

# 解析结果的复用窗口。页面级单飞只能合并并发的加载，而实时预取和资金流兜底在
# 一次请求里是先后发生的（实测相隔约 4 秒），于是同一个页面被加载两次。每次加载
# 都可能再被拒一次，不只是一次 Chromium 开销。默认与报告缓存的盘中 TTL 对齐，
# 不引入超出既有约定的陈旧度。置 0 关闭复用。
FUND_FLOW_PAGE_REUSE_SECONDS = max(
    0.0,
    float(env("FUND_FLOW_PAGE_REUSE_SECONDS", "30")),
)

# 整个浏览器同时开着的页面数上限。这也是峰值内存的直接决定项——页面在信号量
# 持有区间内创建、也在区间内关闭,所以"同时几个页面"就是"同时几个渲染进程"。
#
# 原值 2 的由来是部署机规格：2C4G 上建议并发不超过 2,那是 CPU 侧的约束——每个
# 渲染进程要跑页面上的 JS 和图表。下面的内存数据是另一条独立的约束,两条都要满足。
#
# 2026-09-04 单棵干净的 headless 树实测每页边际内存（macOS RSS）:
#
#   空载 -> 1 页  +378 MiB   含一次性 renderer/GPU 初始化
#        -> 2 页  +127 MiB   ← 每多一个并发页的边际代价
#        -> 3 页  +137 MiB
#        -> 4 页   +85 MiB
#   全部关闭后回落到 +25.9 MiB,不漏
#
# 2026-09-05 随 FUND_FLOW_PAGE_CONCURRENCY 一起从 2 提到 3。两者必须
# 一起动：兜底名额和这个上限是串联的两道闸门，只提其中一个，另一个立刻变成新的
# 瓶颈，收益为零。收益的量化见那一项的注释（争用批次里多服务 2 个标的）。
#
# 代价是多一个并发渲染进程，按上表的边际值约 +130 MiB。这个数是 macOS RSS，会把
# 共享框架页在每个进程里重复计入，偏高；Ubuntu 上的真实峰值仍需按项目口径复测。
# scripts/verify_release.py 的性能一节现在会打印进程树峰值 RSS，就是为了让这次
# 上调的代价能在部署机上直接读出来，而不是靠推断。
#
# 不再往上提到 4：那要多两个渲染进程，而 §四 的 500 MiB 判定还没清。
BROWSER_MAX_PAGES = max(
    1,
    int(env("BROWSER_MAX_PAGES", "3")),
)

# 多久没人用就把浏览器整个拆掉,秒。置 0 关闭空闲回收。
#
# 收益（2026-09-04 实测）:一次页面加载之后常驻的浏览器进程树是 257.6 MiB,
# close_browser() 用 0.03s 就回收到 0 MiB / 0 进程,不留残余。257.6 MiB 是 §四
# 预算 500 MiB 的一半,收盘后到次日开盘有 18 个小时,这半份预算是白占的。
# 附带收益:实测"4 个页面全部关闭后仍回落 +25.9 MiB",即每轮留下约 26 MiB 的
# 慢渗漏,定期整体拆掉能把它清零。
#
# 代价:空闲后第一个请求多付 +2.19s（冷 2.88s vs 热 0.69s,其中建浏览器
# 0.38~0.65s）。收益远大于代价,量级清楚。
#
# 取 90 分钟是为了盖住午休（11:30-13:00 正好 90 分钟）。注意这是个边界值:真的
# 一整个午休零调用时会恰好在 13:00 前后拆掉,开盘第一个请求付那 2.2s。要确保
# 盖过午休就设 95 分钟以上,但收盘后也会跟着多留一段。
#
# 与 P0 的联动（重要）:拆掉浏览器等于会话回到全新冷态,所以 close_browser() 必须
# 连带复位 _session_warm,否则标志还是 True 而预算塌到 1,只 goto 不 reload,这条
# 特性会反过来降低获取率。
BROWSER_IDLE_TIMEOUT_SECONDS = max(
    0.0,
    float(env("BROWSER_IDLE_TIMEOUT_SECONDS", "5400")),
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
    raw = requested if requested is not None else env("HTTP_CHANNEL")
    mode = str(raw or "").strip().lower()
    mode = HTTP_MODE_ALIASES.get(mode, mode)
    if not mode:
        mode = HTTP_MODE_DEFAULT
    if mode not in HTTP_MODES:
        return HTTP_MODE_DEFAULT, f"invalid_value:{mode}"

    if mode == "proxy":
        if not proxy_gateway:
            raise HttpModeError(
                "HTTP_CHANNEL=proxy requires AKSHARE_PROXY_GATEWAY; "
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

# 盘中行情跨源交叉校验：拿到第一个可用报价之后，再问剩下的源一遍，超过这个百分比
# 就打一条 WARNING。置 0 关闭（默认）。
#
# 默认关是因为它有明确代价：正常路径上 resolve() 问到第一个就停，开了之后每个标的
# 要多问一次网络源。2026-09-05 那轮兜底走了 52 次，开着就是 +52 次上游请求。
#
# 什么时候值得开：怀疑某个源的口径不对的时候。真实例子——创业板指的成交量东财
# 20046.25 万手、腾讯 19341.30 万手，差 3.5%，这件事是靠人工三方比对花了几个钟头
# 才定位的；开着这个开关，日志里当场就有一行。
INTRADAY_QUOTE_CROSS_CHECK_PCT = max(
    0.0,
    float(env("INTRADAY_QUOTE_CROSS_CHECK_PCT", "0")),
)

# 交易日历的进程内缓存时长。交易日历是提前一年公布的，一天刷一次绰绰有余；
# 取一次实测 0.18s / 8797 行 / 69 KiB，缓存住之后对每次判断是零成本。
TRADING_CALENDAR_TTL_SECONDS = max(
    0.0,
    float(env("TRADING_CALENDAR_TTL_SECONDS", "86400")),
)

# 板块分级表的进程内缓存时长。行业分类一年动一两次，一天刷一次绰绰有余；
# 取一次实测 31+131 行、约 2.4s，缓存住之后对每次查询是零成本。
SECTOR_TAXONOMY_TTL_SECONDS = max(
    0.0,
    float(env("SECTOR_TAXONOMY_TTL_SECONDS", "86400")),
)

# Synchronous AkShare/efinance calls are I/O bound. Keep the executor bounded,
# while allowing deployments to tune it for their upstream capacity.
FETCH_MAX_WORKERS = max(1, int(env("FETCH_MAX_WORKERS", "8")))
# Bound submitted and running work separately from the executor's unbounded
# internal queue. The default keeps one queued task per worker at saturation.
FETCH_MAX_IN_FLIGHT = max(
    1,
    int(env("FETCH_MAX_IN_FLIGHT", "16")),
)

# Bound concurrent report batches before they fan out into data and browser work.
BATCH_CONCURRENCY = max(
    1,
    int(env("BATCH_CONCURRENCY", "2")),
)

# Financial abstracts normally change only after periodic reports are published.
# Cache successful results to keep recurring batch scans off the upstream API.
FINANCE_CACHE_TTL_SECONDS = max(
    0.0,
    float(env("FINANCE_CACHE_TTL_SECONDS", "21600")),
)
FINANCE_CACHE_MAX_ENTRIES = max(
    1,
    int(env("FINANCE_CACHE_MAX_ENTRIES", "512")),
)

# --- Report cache (finmcp/cache.py) ---
# A rendered report is reusable only inside the market epoch that produced it,
# so the cache never changes what a tool would return. Disabling the master
# switch removes the cache from the call path entirely.
REPORT_CACHE_ENABLED = _parse_bool(env("REPORT_CACHE_ENABLED"), True)
# 盘中数值持续变动，复用受短 TTL 约束，只用于合并突发重复请求。置 0 则盘中绝不复用。
# 默认 30 秒是陈旧度与积分的折中：基于下游真实捕获比对，60 秒窗口内主力净流入的
# P90 相对漂移为 21%，30 秒窗口降至 7.7%，而代价只是约 2.8 个百分点的积分降幅。
REPORT_CACHE_INTRADAY_TTL_SECONDS = max(
    0.0,
    float(env("REPORT_CACHE_INTRADAY_TTL_SECONDS", "30")),
)
REPORT_CACHE_MAX_ENTRIES = max(
    1,
    int(env("REPORT_CACHE_MAX_ENTRIES", "512")),
)
# Second tier surviving restarts. Closed epochs span 16h (64h over a weekend),
# so an in-memory-only cache loses most of its value on any redeploy.
REPORT_CACHE_DISK_ENABLED = _parse_bool(env("REPORT_CACHE_DISK_ENABLED"), True)
# Resolved against the package root, not the daemon's CWD: the sweeper deletes
# directories under here, so a relative value read from a copied .env must not
# land somewhere unexpected.
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
REPORT_CACHE_DIR = os.path.normpath(
    os.path.join(_PROJECT_ROOT, env("REPORT_CACHE_DIR") or ".runtime/report-cache")
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
    env("REPORT_CACHE_SETTLE_TIME"), datetime.time(15, 30)
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
