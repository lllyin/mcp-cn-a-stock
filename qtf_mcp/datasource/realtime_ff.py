import asyncio
import contextlib
import json
import logging
import os
import platform
import random
import re
import time
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..config import (
    ALL_INDICES,
    BROWSER_CLAIM_PLATFORM,
    BROWSER_DISGUISE,
    BROWSER_HEADFUL,
    BROWSER_IDLE_TIMEOUT_SECONDS,
    BROWSER_KEEP_PAGES,
    BROWSER_MAX_PAGES,
    FUND_FLOW_PAGE_MAX_LOADS,
    FUND_FLOW_PAGE_RETRY_DELAY_MS,
    FUND_FLOW_PAGE_REUSE_SECONDS,
    FUND_FLOW_PAGE_TABLE_WAIT_SECONDS,
)
from ..observability import log_context
from .fund_flow_page import (
    HISTORY_TABLE_ID,
    FundFlowPage,
    FundFlowPageError,
    parse_fund_flow_page,
    parse_percent,
)

logger = logging.getLogger("qtf_mcp")

# ── 全局单例 ──────────────────────────────────────────────
_playwright = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_lock = asyncio.Lock()

# 正在借用浏览器的调用方数量，以及空闲回收的定时器。
#
# 引用计数不是为了限并发（那是 SEMAPHORE 的事），而是为了让空闲回收不会在别人
# 用到一半时把浏览器拆掉。少了它就有一个真实的竞态：get_context() 返回后就释放
# 了 _lock，调用方随后才 new_page()，定时器如果落在这个窗口里，new_page() 抛异常
# 会被计成兜底失败、喂给熔断器——一个省内存的改动反过来制造数据丢失。
_browser_users = 0
_idle_timer: asyncio.TimerHandle | None = None
_idle_task: asyncio.Task | None = None

# 整个浏览器同时开着的页面数上限。页面在这段区间内创建也在区间内关闭，所以这个
# 值同时就是"同时几个渲染进程"，是峰值内存的直接决定项。见配置项的实测数据。
SEMAPHORE = asyncio.Semaphore(BROWSER_MAX_PAGES)
_inflight: dict[str, asyncio.Task[dict]] = {}
_inflight_waiters: dict[str, int] = {}
_inflight_keep_alive: dict[str, bool] = {}

# 需要拦截的无用资源
BLOCKED_PATTERNS = [
    "**/*.{png,jpg,jpeg,gif,css,woff,woff2,ico,svg,mp4,webp}",
    "**/analytics*",
    "**/tracking*",
    "**/stat.*",
    "**/log.*",
    "**/*baidu*",
    "**/*cnzz*",
    "**/*umeng*",
    "**/*google*",
    "**/*advertisement*",
]

# 以数据就绪作为信号的等待脚本（等5个主字段同时非空非占位符）
WAIT_FOR_DATA_JS = """
    () => {
        const fields = ['f62', 'f66', 'f72', 'f78', 'f84'];
        return fields.every(fid => {
            const el = document.querySelector(`td[data-field="${fid}"]`);
            if (!el) return false;
            const txt = el.innerText.trim();
            return txt !== '' && txt !== '-' && txt !== '--';
        });
    }
"""

# 数据解析脚本
PARSE_JS = """
    () => {
        const get = (fid) => {
            const el = document.querySelector(`td[data-field="${fid}"]`);
            const txt = el ? el.innerText.trim() : '';
            return (txt && txt !== '-' && txt !== '--') ? txt : '0';
        };
        const titleEl = document.querySelector('.title') || document.querySelector('h1');
        return {
            name:  titleEl ? titleEl.innerText.trim() : '',
            f62:  get('f62'),  f184: get('f184'),
            f66:  get('f66'),  f69:  get('f69'),
            f72:  get('f72'),  f75:  get('f75'),
            f78:  get('f78'),  f81:  get('f81'),
            f84:  get('f84'),  f87:  get('f87'),
        };
    }
"""


INDEX_FUND_FLOW_URLS = {
    "000001": "https://data.eastmoney.com/zjlx/zs000001.html",
    "399001": "https://data.eastmoney.com/zjlx/zs399001.html",
    "399006": "https://data.eastmoney.com/zjlx/zs399006.html",
}

INDEX_FUND_FLOW_NAMES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
}


def get_fund_flow_url(symbol: str) -> str | None:
    """Return the Eastmoney fund-flow page URL for a stock or index."""
    pure_code = "".join(filter(str.isdigit, symbol))
    if pure_code in INDEX_FUND_FLOW_URLS:
        return INDEX_FUND_FLOW_URLS[pure_code]
    if symbol == "dpzjlx":
        return "https://data.eastmoney.com/zjlx/dpzjlx.html"
    if pure_code in ALL_INDICES:
        return None
    # 用纯代码而不是入参：页面路径是 /zjlx/300408.html，带交易所前缀的
    # /zjlx/SZ300408.html 是一个不存在的页面，会静默返回一个没有数据区的框架。
    return f"https://data.eastmoney.com/zjlx/{pure_code}.html"


def page_key(symbol: str) -> str:
    """页面级单飞的键。

    同一个页面会被两个调用方以不同写法请求——实时路径给纯代码，资金流兜底给
    带前缀的规范代码——归一之后它们才会共享同一次加载，而不是各加载一次。
    """
    if symbol == "dpzjlx":
        return symbol
    return "".join(filter(str.isdigit, symbol)) or symbol


def get_fund_flow_display_name(symbol: str, parsed_name: str) -> str:
    """Return a stable display name for fund-flow output."""
    pure_code = "".join(filter(str.isdigit, symbol))
    if pure_code in INDEX_FUND_FLOW_NAMES:
        return INDEX_FUND_FLOW_NAMES[pure_code]
    return parsed_name or symbol


# ── Browser 单例管理 ──────────────────────────────────────
async def _close_started_browser(playwright, browser: Browser | None) -> None:
    """关闭尚未发布为全局单例的浏览器资源。"""
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            logger.warning("清理未完成初始化的 Chromium 失败", exc_info=True)
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            logger.warning("清理未完成初始化的 Playwright 失败", exc_info=True)


# 无头构建缺失、而真实 Chrome 上一定存在的几项。只补检测脚本必查的这几个，
# 不做通用 stealth：补得越多，越可能被"属性描述符/toString 特征"反查出来，
# 半成品的伪装比不伪装更显眼。plugins 那段带条件，所以换成完整构建后自动让路。
_HEADLESS_GAPS_SCRIPT = """
(() => {
  if (!window.chrome) {
    window.chrome = {
      runtime: {},
      loadTimes: function () { return {}; },
      csi: function () { return {}; },
      app: { isInstalled: false },
    };
  }
  if (navigator.plugins.length === 0) {
    const mk = (name) => ({
      name, filename: 'internal-pdf-viewer',
      description: 'Portable Document Format', length: 1,
    });
    const plugins = [
      mk('PDF Viewer'), mk('Chrome PDF Viewer'), mk('Chromium PDF Viewer'),
      mk('Microsoft Edge PDF Viewer'), mk('WebKit built-in PDF'),
    ];
    Object.defineProperty(navigator, 'plugins', {
      get: () => plugins, configurable: true,
    });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => [
        { type: 'application/pdf', suffixes: 'pdf', description: '' },
        { type: 'text/pdf', suffixes: 'pdf', description: '' },
      ],
      configurable: true,
    });
    Object.defineProperty(navigator, 'pdfViewerEnabled', {
      get: () => true, configurable: true,
    });
  }
})();
"""

# 声明某个平台时，UA 里那段平台 token 和 navigator.platform 必须跟着一起改，
# 否则就是新的自相矛盾。这两串是 Chrome 冻结后的固定写法，真实浏览器就长这样：
# Apple Silicon 上的 Chrome 也照样报 "Intel Mac OS X 10_15_7"。
_UA_PLATFORM_TOKEN = {
    "macOS": "Macintosh; Intel Mac OS X 10_15_7",
    "Windows": "Windows NT 10.0; Win64; x64",
}
_NAVIGATOR_PLATFORM = {"macOS": "MacIntel", "Windows": "Win32"}
# 声明某个平台就得给个说得通的系统版本，空字符串本身也是特征。Linux 上真实
# Chrome 报的是内核版本，这里给一个常见的 LTS 值。
_PLATFORM_VERSION = {"macOS": "15.6.0", "Windows": "10.0.0", "Linux": "6.8.0"}
# 桌面上常见、风控见惯了的平台。Linux 桌面份额极低，一个自称 Linux 的访客本身
# 就是少数派特征，所以不在这个名单里的一律对外声明 macOS。
_COMMON_DESKTOP_PLATFORMS = ("Windows", "macOS")

# 真实构建自报的身份，只探一次。
_identity: dict | None = None


def _claimed_platform(real: str) -> str:
    """决定对外声明哪个平台。

    默认规则（``auto``）：Windows 和 macOS 照实报，其余——服务器上就是 Linux——
    统一报 macOS。Linux 桌面在真实访客里占比极低，照实报等于自带一个少数派特征。

    代价要写明：声明 macOS 之后，WebGL renderer（Linux 上是 SwiftShader/Mesa）和
    字体列表仍然是 Linux 的样子。如果对端交叉核对到那一层，声明 macOS 反而比照实
    报更可疑。所以留了 ``real`` 选项，好在部署机上用 blocked_captcha 的占比做对照。
    """
    configured = (BROWSER_CLAIM_PLATFORM or "auto").strip().lower()
    if configured == "real":
        return real
    if configured in ("macos", "mac"):
        return "macOS"
    if configured == "windows":
        return "Windows"
    return real if real in _COMMON_DESKTOP_PLATFORMS else "macOS"


async def _browser_identity(context: BrowserContext) -> dict:
    """读一次真实 UA 与平台，拼出自洽的 UA / UA-CH 覆盖参数。"""
    global _identity
    if _identity is not None:
        return _identity
    page = await context.new_page()
    try:
        probe = await page.evaluate(
            "() => ({ua: navigator.userAgent, platform: navigator.platform,"
            " chPlatform: navigator.userAgentData"
            " ? navigator.userAgentData.platform : ''})"
        )
    finally:
        await page.close()

    ua = str(probe.get("ua") or "")
    match = re.search(r"(?:Headless)?Chrome/([\d.]+)", ua)
    full_version = match.group(1) if match else ""
    major = full_version.split(".")[0] if full_version else ""

    real_platform = probe.get("chPlatform") or ""
    if not real_platform:
        real_platform = (
            "Windows" if "Windows" in ua
            else "Linux" if "Linux" in ua
            else "macOS" if "Mac" in ua
            else ""
        )
    claimed = _claimed_platform(real_platform)

    # HeadlessChrome 是那句自报身份；平台 token 是第一个括号里的内容。两处一起改，
    # 版本号原样保留——报一个比引擎新的版本会被特性检测抓出来。
    ua = ua.replace("HeadlessChrome", "Chrome")
    token = _UA_PLATFORM_TOKEN.get(claimed)
    if token and claimed != real_platform:
        ua = re.sub(r"\([^)]*\)", f"({token})", ua, count=1)

    _identity = {
        "userAgent": ua,
        "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
        "platform": _NAVIGATOR_PLATFORM.get(claimed) or probe.get("platform") or "",
        "userAgentMetadata": {
            "brands": [
                {"brand": "Not_A Brand", "version": "8"},
                {"brand": "Chromium", "version": major},
                {"brand": "Google Chrome", "version": major},
            ],
            "fullVersion": full_version or f"{major}.0.0.0",
            "platform": claimed,
            "platformVersion": _PLATFORM_VERSION.get(claimed, ""),
            "architecture": "arm" if "arm" in platform.machine().lower() else "x86",
            "model": "",
            "mobile": False,
        },
    }
    logger.info(
        "资金流向页面伪装身份 platform=%s(真实 %s) ua=%s",
        claimed,
        real_platform or "?",
        _identity["userAgent"],
    )
    return _identity


async def disguise_page(page) -> None:
    """把 UA 与 client hints 一起改成自洽的非 Headless。

    ``Network.setUserAgentOverride`` 是 per-target 的，context 上装一次不会被后建
    的页面继承——实测在第一个页面上装完，后面新建页面的 sec-ch-ua 依旧是
    ``HeadlessChrome``。所以每个页面导航之前都要装一次。

    这条是这批伪装里唯一有明确机制的：``sec-ch-ua`` 在每个请求头里写着
    ``"HeadlessChrome";v="145"``，是自报身份，不是什么细微指纹。
    """
    if not BROWSER_DISGUISE:
        return
    try:
        context = page.context
        identity = await _browser_identity(context)
        session = await context.new_cdp_session(page)
        await session.send("Network.setUserAgentOverride", identity)
    except Exception:
        # 伪装失败不该让取数失败：拿不到数据的代价远大于指纹暴露。
        logger.debug("资金流向页面伪装失败，按原样继续", exc_info=True)


async def get_context() -> BrowserContext:
    global _playwright, _browser, _context
    async with _lock:
        if _browser is None or not _browser.is_connected():
            new_playwright = None
            new_browser = None
            try:
                new_playwright = await async_playwright().start()
                new_browser = await new_playwright.chromium.launch(
                    headless=not BROWSER_HEADFUL,
                    args=[
                        # 不带这一条时 navigator.webdriver 为 true，东财的资金流
                        # 接口对页面发出的 /fflow/ 请求直接空响应。实测同一时间、
                        # 8 只沪深标的各加载一次：不带 0/8，带上 7/8，与有头模式
                        # 的 7/8 持平。所以服务器上不需要有头，也不需要 Xvfb。
                        "--disable-blink-features=AutomationControlled",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-default-apps",
                        "--no-first-run",
                        "--mute-audio",
                    ],
                )
                # 不再硬编码 UA 字符串。硬编码解决不了问题还制造新问题：它只改
                # navigator.userAgent，sec-ch-ua 仍由真实构建给出，于是一个请求里
                # UA 说 Chrome/120、client hints 说 HeadlessChrome/145，自相矛盾；
                # 在 Linux 服务器上更糟，UA 说 Macintosh 而 sec-ch-ua-platform 说
                # Linux。一致的伪装在 _disguise_page 里按真实版本号现算。
                context_options = {
                    "java_script_enabled": True,
                    "bypass_csp": True,
                }
                if BROWSER_DISGUISE:
                    context_options.update(
                        # 中文财经站的访客不会只带 en-US。locale 同时决定
                        # navigator.language(s) 和 Accept-Language 请求头。
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        # 默认 1280x720 是 Playwright 的值，桌面浏览器少见；同时
                        # 让 outerWidth 不再等于 innerWidth。
                        viewport={"width": 1920, "height": 1080},
                        screen={"width": 1920, "height": 1080},
                    )
                new_context = await new_browser.new_context(**context_options)
                if BROWSER_DISGUISE:
                    await new_context.add_init_script(_HEADLESS_GAPS_SCRIPT)
            except BaseException:
                await _close_started_browser(new_playwright, new_browser)
                raise

            # 仅在完整初始化成功后发布，避免其他任务看到半初始化状态。
            _playwright = new_playwright
            _browser = new_browser
            _context = new_context
        return _context


@contextlib.asynccontextmanager
async def browser_lease():
    """借出浏览器上下文，借用期间空闲回收不会把它拆掉。

    计数刻意在 ``get_context()`` **之前**加，而不是之后：浏览器还不存在时把计数
    加上没有坏处（回收器只在计数为 0 时才拆），但反过来就有一个窗口——建好之后、
    计数加上之前，定时器可以插进来把它关掉。

    一次借用要盖住这个标的的全部重试。中途换浏览器意味着 tab 和 CDP 覆盖一起失效，
    而重试正是被拒之后最需要稳定的时候。
    """
    global _browser_users
    async with _lock:
        _cancel_idle_timer()
        _browser_users += 1
    try:
        yield await get_context()
    finally:
        async with _lock:
            _browser_users -= 1
            if _browser_users <= 0:
                _browser_users = 0
                _arm_idle_timer()


def _cancel_idle_timer() -> None:
    """撤掉待决的回收定时器。调用方必须已持有 ``_lock``。"""
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None


def _arm_idle_timer() -> None:
    """最后一个借用者离开时排一个回收定时器。调用方必须已持有 ``_lock``。

    用 ``call_later`` 而不是轮询循环：到点即拆，空闲期一次也不唤醒。全仓现有的
    ``create_task`` 全是请求内的，这是唯一一个常驻定时器，所以刻意做成"没有借用
    者时才存在"——有人在用的时候它是被撤掉的状态。
    """
    global _idle_timer
    _cancel_idle_timer()
    if BROWSER_IDLE_TIMEOUT_SECONDS <= 0 or _browser is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _idle_timer = loop.call_later(BROWSER_IDLE_TIMEOUT_SECONDS, _on_idle_timeout)


def _on_idle_timeout() -> None:
    """定时器回调。留住任务引用，否则可能在跑完之前被 GC 掉。"""
    global _idle_task
    _idle_task = asyncio.ensure_future(_close_if_idle())


async def _close_if_idle() -> None:
    async with _lock:
        if _browser_users > 0 or _browser is None:
            # 定时器排下之后又来了请求。撤销由 browser_lease 负责，这里只是兜底。
            return
        logger.info(
            "浏览器空闲 %.0f 分钟，回收实例", BROWSER_IDLE_TIMEOUT_SECONDS / 60
        )
        await _close_browser_locked()


async def close_browser():
    """服务退出时调用，清理资源。"""
    async with _lock:
        _cancel_idle_timer()
        await _close_browser_locked()


async def _close_browser_locked():
    """真正的拆除。调用方必须已持有 ``_lock``。"""
    global _playwright, _browser, _context, _identity
    # 身份是从具体这个构建探出来的，换浏览器要重探。
    _identity = None
    if _browser:
        await _browser.close()
        _browser = None
        _context = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


class FundFlowPageUnavailable(RuntimeError):
    """该标的没有资金流向页面（三大指数之外的指数）。"""


class FundFlowPageRefused(RuntimeError):
    """页面加载成功，但资金流接口拒绝了请求。

    观察到的现象，不含机制推断：对 ``/fflow/`` 的请求在发出约 240 毫秒后以
    ``net::ERR_EMPTY_RESPONSE`` 失败——连接建立后服务端一个字节都没回就关闭。
    这不是慢：等满 10 秒也没有迟到的响应，而页面自己只轮询今日、从不重发历史。
    页面框架照常渲染，数据区留空。

    2026-09-04 15:50 抓到了机制：被拒时页面同时跑完整套滑块验证，且全部 200 ——
    ``websitecaptcha/api/checkuser``、``websitecaptcha/slidervalid``、
    ``smartvcode2.../Titan/api/captcha/get``、``icon_slide.png``，DOM 里挂着
    ``<iframe class="popwscps_d_iframe">``。也就是说风控判定这个出口 IP 需要过
    滑块，空响应就是风控本身，滑块是它给的解法。``captcha_present`` 就是从这些
    痕迹认出来的，只用于把原因写进错误信息。

    这种拒绝至少有两种时长，别把它们当成一回事：

    - **瞬时**：立刻重试就过。2026-09-04 15:51 的 SH512480 第一次
      ``blocked_captcha``、593 毫秒后的第二次拿到 ``today=True history=121``。
      早先 8 轮全新浏览器的观察也是"成功集中在前两次尝试"。``MAX_LOADS=2``
      就是为这一种设的，删掉它会白丢这些本可以拿到的数据。
    - **持续**：分钟级，重试无用。2026-09-04 15:35-15:50 实测背靠背 10 次全拒、
      静默 75 秒后 4 次全拒、16 分钟后仍拒。这一种只能等，多试只是白付页面加载。

    两者在单次日志里无法区分，所以现在的策略是"最多试两次然后放弃"——对瞬时态
    足够，对持续态最多浪费一次。要给持续态加长冷却，得先在部署机上采够"进入
    风控 -> 恢复"的时间分布，否则冷却会在风控解除后继续空转。

    有头模式加上不关页面之所以"稳定能取到"，是因为人能看见并手动过掉滑块，
    过完的会话被保留下来复用——不是有头本身躲过了检测。实测无人值守的有头模式
    每次都丢弃页面时是 0/12，比无头还差。
    """


# 兼容旧名字。
FundFlowPageBlocked = FundFlowPageRefused


async def _race_with_refusal(coro, refused: asyncio.Event):
    """等 coro 完成，但一旦资金流接口被拒就立刻放弃。

    固定超时在这里是个两难：给短了，冷启动的页面来不及填表（实测 4 秒不够、
    需要十几秒）；给长了，端点被拒时每次都白等满。用"被拒"这个事件抢答就不用
    折中——正常时按页面自己的速度返回（热加载约 0.5 秒），被拒时立即返回。
    """
    task = asyncio.ensure_future(coro)
    refusal = asyncio.ensure_future(refused.wait())
    try:
        await asyncio.wait(
            {task, refusal}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for pending in (task, refusal):
            if not pending.done():
                pending.cancel()
        # 取一次异常，避免 "never retrieved" 告警。
        if task.done() and not task.cancelled():
            task.exception()


# 页面上两块数据由不同端点填充，而且会独立失败：2026-09-04 09:49 的抓包里
# fflow/kline/get 返回 200 而 fflow/daykline/get 被拒，页面就是今日有值、历史
# 为空。所以抢答必须各盯各的，否则一个失败会连累另一个。
#
# 只认 /fflow/：同一次抓包里 qt/stock/get 也失败了，而今日一栏照样填出了
# 9443.9402万——它是页头行情和延时提示（cb=quotedelaytip0）用的，不供给这两块
# 数据。把它算成失败信号会凭空掐掉今日的等待，让今日一栏变成一串 0。
TODAY_ENDPOINTS = ("/fflow/kline/get",)
HISTORY_ENDPOINTS = ("/fflow/daykline/get",)


async def _wait_for_today(page, refused: asyncio.Event) -> None:
    """等今日一栏的 Ajax 填充完成。超时不算错：停牌或非交易时段本就是空的。"""

    async def wait():
        try:
            await page.wait_for_selector("text=今日主力净流入", timeout=10000)
            await page.wait_for_function(WAIT_FOR_DATA_JS, timeout=12000)
        except Exception:
            pass

    await _race_with_refusal(wait(), refused)


async def _wait_for_history(page, refused: asyncio.Event) -> None:
    """等历史表的 Ajax 填充完成。"""

    async def wait():
        try:
            await page.wait_for_selector(
                f"#{HISTORY_TABLE_ID} tbody tr",
                timeout=int(FUND_FLOW_PAGE_TABLE_WAIT_SECONDS * 1000),
            )
        except Exception:
            pass

    await _race_with_refusal(wait(), refused)


async def load_fund_flow_page(
    symbol: str,
    context: BrowserContext,
    *,
    loads: int = 1,
    satisfies=None,
) -> FundFlowPage:
    """加载页面，解析出今日与历史两块。

    今日和历史都在同一个页面上、都由 ``/fflow/`` 接口填充，所以分两次加载既浪费
    一次 Chromium，又多一次被拒的机会。两个等待并发进行，耗时上限与合并前一致。

    ``loads`` 是这一个 tab 允许的加载次数：第一次 goto，没拿到（想要的）数据就在
    同一个 tab 上 reload。实测 reload 便宜一半——同一标的第二次加载 p50 0.240s，
    而关掉再开新 tab 是 0.452s，且 CDP 的 UA 覆盖能活过 reload、不用重装。

    reload 刻意留在同一段信号量持有区间内：整个浏览器同时开着的 tab 数是靠
    ``SEMAPHORE`` 隐式限住的（页面在这段区间里创建也在这段区间里关闭），tab 一旦
    活过这段区间，那个上限就失效了——4 个标的并发时会变成 4 个 tab、每个约 120 MiB。
    """
    wait_started_at = time.perf_counter()
    await SEMAPHORE.acquire()
    semaphore_wait = time.perf_counter() - wait_started_at
    outcome = "error"
    url = None
    how = "-"
    started_at = time.perf_counter()
    # 这一次加载有没有自己记过日志。日志是一次加载一行，而 unsupported 和加载途中
    # 抛出的异常根本走不到那一行，靠这个标记在 finally 里补记，别让它们在日志里消失。
    logged = True
    try:
        url = get_fund_flow_url(symbol)
        if url is None:
            outcome = "unsupported"
            logged = False
            raise FundFlowPageUnavailable(symbol)

        page = await context.new_page()
        try:
            # 必须在 goto 之前：覆盖是 per-target 的，导航之后再装，这一次请求的
            # sec-ch-ua 已经带着 HeadlessChrome 发出去了。reload 不用重装。
            await disguise_page(page)

            # 拦截无用资源，降低带宽和 CPU。路由是页面级的，reload 之后依旧生效。
            async def block_route(route):
                await route.abort()

            for pattern in BLOCKED_PATTERNS:
                await page.route(pattern, block_route)

            last_refusal = None
            parsed = None
            for index in range(max(1, loads)):
                if index:
                    delay = await _sleep_before_retry()
                    logger.info(
                        "资金流向页面没数据，同一 tab reload symbol=%s 第%d/%d次 "
                        "等待=%.0fms",
                        symbol,
                        index + 1,
                        loads,
                        delay * 1000,
                    )
                started_at = time.perf_counter()
                how = "reload" if index else "new_tab"
                outcome = "error"
                logged = False
                parsed, last_refusal = await _load_once(
                    page, symbol, url, reload=bool(index)
                )
                outcome = (
                    "blocked_captcha"
                    if last_refusal is not None and last_refusal.captcha
                    else "blocked" if last_refusal is not None
                    else f"today={parsed.has_today} history={len(parsed.history)}"
                )
                _log_page_load(
                    symbol,
                    url,
                    how,
                    outcome,
                    semaphore_wait if not index else 0.0,
                    time.perf_counter() - started_at,
                )
                logged = True
                if parsed is None:
                    continue
                if satisfies is None or satisfies(parsed):
                    return parsed
            if parsed is not None:
                # 拿到了数据但不满足调用方要的那一块，交给上层决定要不要换 tab 再试。
                return parsed
            raise last_refusal or FundFlowPageError(
                f"{symbol} 页面既无今日数据也无历史表"
            )
        finally:
            if BROWSER_KEEP_PAGES:
                # 调试模式：留着页面供人工观察。每个页面是一个独立渲染进程，实测
                # 约 120 MiB，会持续占着，只在排查时开。
                logger.info("调试模式保留页面 symbol=%s url=%s", symbol, url)
            else:
                await page.close()
    finally:
        if not logged:
            _log_page_load(
                symbol,
                url or "-",
                how,
                outcome,
                semaphore_wait,
                time.perf_counter() - started_at,
            )
        SEMAPHORE.release()


def _log_page_load(
    symbol: str,
    url: str,
    how: str,
    outcome: str,
    semaphore_wait: float,
    service: float,
) -> None:
    """一次页面加载记一行。同一个 tab 上的 reload 也是一行，用 how 区分。

    semaphore_wait 只在这个 tab 的第一次加载上有意义：reload 时名额早已在手。
    """
    request_id, tool, _ = log_context()
    logger.info(
        "Realtime fund flow page request_id=%s tool=%s symbol=%s url=%s "
        "how=%s outcome=%s semaphore_wait=%.3fs service=%.3fs",
        request_id,
        tool,
        symbol,
        url,
        how,
        outcome,
        semaphore_wait,
        service,
    )


class _PageRefusal(FundFlowPageRefused):
    """带上"是不是滑块"的被拒，好让日志分开 blocked 和 blocked_captcha。"""

    def __init__(self, message: str, *, captcha: bool) -> None:
        super().__init__(message)
        self.captcha = captcha


async def _load_once(page, symbol: str, url: str, *, reload: bool):
    """在给定页面上跑一次加载，返回 (解析结果 或 None, 被拒异常 或 None)。"""
    refused: list = []
    today_refused = asyncio.Event()
    history_refused = asyncio.Event()

    def on_request_failed(request) -> None:
        # 被拒的特征是资金流接口被直接断连，而页面框架本身加载成功。
        failed_url = request.url
        if any(part in failed_url for part in TODAY_ENDPOINTS):
            refused.append(failed_url)
            today_refused.set()
        elif any(part in failed_url for part in HISTORY_ENDPOINTS):
            refused.append(failed_url)
            history_refused.set()

    # 监听器现挂现摘：refused 是这一次的账，不能跨 reload 累计。
    page.on("requestfailed", on_request_failed)
    try:
        if reload:
            await page.reload(wait_until="domcontentloaded", timeout=25000)
        else:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.gather(
            _wait_for_today(page, today_refused),
            _wait_for_history(page, history_refused),
        )
        content = await page.content()
    finally:
        page.remove_listener("requestfailed", on_request_failed)

    try:
        parsed = parse_fund_flow_page(content)
    except FundFlowPageError:
        parsed = None

    # 页面渲染成功但两块都没值，同时相关请求被拒。停牌和开盘前也会得到空值，
    # 但那时不会有请求失败，所以两个条件必须同时成立才算"被拒"。
    got_nothing = parsed is None or (not parsed.history and not parsed.has_today)
    if got_nothing and refused:
        captcha = parsed is not None and parsed.captcha_present
        reason = (
            "东财风控要求滑块验证（页面已弹出验证框），过验证前接口不会返回数据"
            if captcha
            else "页面数据区为空"
        )
        return None, _PageRefusal(
            f"{symbol} 资金流接口拒绝了 {len(refused)} 个请求（空响应），{reason}",
            captcha=captcha,
        )
    if parsed is None:
        return None, None
    return parsed, None


def _page_to_realtime_dict(symbol: str, page: FundFlowPage) -> dict:
    """把解析结果转成实时资金流的既有返回结构。

    净额取原样文本、占比取解析后的百分数，与之前 PARSE_JS 的行为逐字一致，包括
    占位符回落成 ``"0"``。名称取页面上第一个 ``.title`` 的原文，也和
    ``document.querySelector('.title')`` 一致。
    """
    text = page.today_text
    # 与 PARSE_JS 的 get() 一致：空串和 - / -- 都回落成 "0"，而不是把占位符原样
    # 输出。这一层是给报告直接打印的，不是给计算用的。
    placeholders = {"", "-", "--"}

    def amount(field_id: str) -> str:
        raw = (text.get(field_id) or "").strip()
        return "0" if raw in placeholders else raw

    def ratio(field_id: str) -> float:
        value = parse_percent(text.get(field_id, ""))
        return 0.0 if value is None else value

    return {
        "标的名称":      get_fund_flow_display_name(symbol, page.title_text),
        "主力净流入":    amount("f62"),
        "主力净比(%)":   ratio("f184"),
        "超大单净流入":  amount("f66"),
        "超大单净比(%)": ratio("f69"),
        "大单净流入":    amount("f72"),
        "大单净比(%)":   ratio("f75"),
        "中单净流入":    amount("f78"),
        "中单净比(%)":   ratio("f81"),
        "小单净流入":    amount("f84"),
        "小单净比(%)":   ratio("f87"),
    }


# ── 页面级单飞 ────────────────────────────────────────────
# 外层 _inflight 管的是消费者取消语义（预取可以被丢弃）；这一层管的是"同一个页面
# 不要为今日和历史各加载一次"。两者时间上会重叠：实时预取在 process_item 开头就
# 启动，而资金流兜底在数据 gather 之后才决定要不要走。
_page_inflight: dict[str, asyncio.Task] = {}
# 已解析结果的短期复用：key -> (完成时刻, 结果)。单飞只覆盖并发，这一层覆盖
# "一次请求里两个用途先后要同一个页面"。
_page_cache: dict[str, tuple[float, FundFlowPage]] = {}


def _cached_page(
    key: str, *, require_history: bool, require_today: bool
) -> FundFlowPage | None:
    """取可复用的解析结果，但只在它满足调用方的需求时。

    页面的两块由不同端点填充、会独立失败，所以一次加载可能只拿到其中一块。把这
    种残缺结果交给需要另一块的调用方，等于用缓存把一次失败固化下来：而那两个端点
    是间歇性可用的，隔几秒重新加载相当有机会拿到。2026-09-04 10:48 就是如此——
    实时路径拿到 today=True history=0，兜底复用了它，于是报告有实时、没历史。
    """
    if FUND_FLOW_PAGE_REUSE_SECONDS <= 0:
        return None
    entry = _page_cache.get(key)
    if entry is None:
        return None
    cached_at, page = entry
    if time.monotonic() - cached_at > FUND_FLOW_PAGE_REUSE_SECONDS:
        _page_cache.pop(key, None)
        return None
    if require_history and not page.history:
        return None
    if require_today and not page.has_today:
        return None
    return page


def _remember_page(key: str, page: FundFlowPage) -> None:
    if FUND_FLOW_PAGE_REUSE_SECONDS <= 0:
        return
    _page_cache[key] = (time.monotonic(), page)
    # 只保留还在窗口内的条目：标的数不设上限，靠过期回收即可。
    deadline = time.monotonic() - FUND_FLOW_PAGE_REUSE_SECONDS
    for stale in [k for k, (at, _) in _page_cache.items() if at < deadline]:
        _page_cache.pop(stale, None)


def _complete_page_inflight(symbol: str, task: asyncio.Task) -> None:
    if _page_inflight.get(symbol) is task:
        _page_inflight.pop(symbol, None)
    if not task.cancelled():
        task.exception()  # 取一次异常，避免"never retrieved"告警


def _satisfies(page: FundFlowPage, require_history: bool, require_today: bool) -> bool:
    if require_history and not page.history:
        return False
    if require_today and not page.has_today:
        return False
    return True


async def _sleep_before_retry() -> float:
    """reload 之前随机等一小会儿，返回实际等待的秒数（供日志核对）。

    没拿到数据后 0 毫秒就刷新同一个页面是个机器节奏。这一觉睡在信号量持有区间
    内——tab 必须留着才能 reload，换不来名额，只能认这点占用；区间上界因此要和
    FALLBACK_WAIT_SECONDS 一起看。
    """
    low, high = FUND_FLOW_PAGE_RETRY_DELAY_MS
    if high <= 0:
        return 0.0
    delay = random.uniform(low, high) / 1000
    await asyncio.sleep(delay)
    return delay


async def _load_page_shared(
    symbol: str, *, require_history: bool = False, require_today: bool = False
) -> FundFlowPage:
    """加载页面；没拿到调用方要的那一块时按配置多试几次。

    重试条件是"这次拿到的还不满足调用方"，不只是"被拒"：一次加载可能只拿到两块
    中的一块，而调用方要的恰好是另一块。

    次数上限见 FUND_FLOW_PAGE_MAX_LOADS 的注释——它数的是页面加载次数，
    不是 tab 数。一个 tab 消耗两次（goto + reload），所以默认 2 就是"一个 tab 试
    两次"，与改动前的总加载次数一致；调到 3 才会开第二个 tab。

        1 次  新 tab + goto
        2 次  同一个 tab reload
        3 次  关掉，开下一个 tab + goto
        4 次  reload
    """
    # 整段重试盖在一次借用里：中途被空闲回收拆掉浏览器，会让 tab 和 CDP 覆盖一起
    # 失效，而被拒之后的重试正是最需要稳定的时候。
    async with browser_lease() as context:
        budget = FUND_FLOW_PAGE_MAX_LOADS
        predicate = lambda page: _satisfies(page, require_history, require_today)
        last_error = None
        used = 0

        while used < budget:
            # 每个 tab 最多两次加载：goto，没数据就 reload。剩余预算不足就少给。
            loads = min(2, budget - used)
            used += loads
            try:
                page = await load_fund_flow_page(
                    symbol, context, loads=loads, satisfies=predicate
                )
            except FundFlowPageRefused as e:
                last_error = e
                if used < budget:
                    logger.info(
                        "资金流向页面被拒，换一个 tab symbol=%s 已用%d/%d次加载",
                        symbol,
                        used,
                        budget,
                    )
                    continue
                raise
            if predicate(page) or used >= budget:
                return page
            logger.info(
                "资金流向页面数据不全，换一个 tab symbol=%s 已用%d/%d次加载 今日=%s 历史=%d",
                symbol,
                used,
                budget,
                page.has_today,
                len(page.history),
            )

        raise last_error if last_error else FundFlowPageError(f"{symbol} 页面加载失败")


async def fetch_page_shared(
    symbol: str, *, require_history: bool = False, require_today: bool = False
) -> FundFlowPage:
    """同一标的的页面加载只做一次，今日与历史两个用途共享结果。

    require_* 声明调用方要哪一块：只影响能否复用既有结果，不影响并发合并——同一
    时刻的两个等待者拿到的本来就是同一次加载，再加载一遍不会有不同结果。
    """
    key = page_key(symbol)
    cached = _cached_page(
        key, require_history=require_history, require_today=require_today
    )
    if cached is not None:
        logger.debug("资金流向页面复用解析结果 symbol=%s key=%s", symbol, key)
        return cached
    task = _page_inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            _load_page_shared(
                symbol, require_history=require_history, require_today=require_today
            )
        )
        _page_inflight[key] = task
        task.add_done_callback(
            lambda completed, k=key: _complete_page_inflight(k, completed)
        )
    # shield：一个等待者被取消不能中断另一个等待者需要的加载。
    page = await asyncio.shield(task)
    _remember_page(key, page)
    return page


# ── 单个 Symbol 抓取 ──────────────────────────────────────
async def fetch_single(symbol: str, context: BrowserContext) -> dict:
    """取今日资金流。返回结构与合并前完全一致，包括失败时的 error 形态。"""
    try:
        return _page_to_realtime_dict(symbol, await load_fund_flow_page(symbol, context))
    except FundFlowPageUnavailable:
        return {"error": "暂无实时资金流向", "url": ""}
    except Exception as e:
        return {"error": str(e), "url": get_fund_flow_url(symbol) or ""}


async def fetch_history_page(symbol: str) -> FundFlowPage:
    """取历史资金流。失败一律抛异常，由调用方决定是否降级。"""
    return await fetch_page_shared(symbol, require_history=True)


async def _fetch_single_with_context(symbol: str) -> dict:
    try:
        page = await fetch_page_shared(symbol, require_today=True)
        return _page_to_realtime_dict(symbol, page)
    except FundFlowPageUnavailable:
        return {"error": "暂无实时资金流向", "url": ""}
    except Exception as e:
        return {"error": str(e), "url": get_fund_flow_url(symbol) or ""}


def _complete_inflight(symbol: str, task: asyncio.Task[dict]) -> None:
    """Remove a completed shared fetch from the in-flight registry."""
    if _inflight.get(symbol) is task:
        _inflight.pop(symbol, None)
        _inflight_waiters.pop(symbol, None)
        _inflight_keep_alive.pop(symbol, None)
    if not task.cancelled():
        task.exception()


async def fetch_single_shared(
    symbol: str,
    *,
    keep_alive_on_cancel: bool = True,
) -> dict:
    """Deduplicate only simultaneous live fetches for the same symbol."""
    request_id, tool, _ = log_context()
    task = _inflight.get(symbol)
    role = "follower"
    if task is None or task.done():
        task = asyncio.create_task(_fetch_single_with_context(symbol))
        _inflight[symbol] = task
        _inflight_waiters[symbol] = 0
        _inflight_keep_alive[symbol] = keep_alive_on_cancel
        task.add_done_callback(lambda completed, key=symbol: _complete_inflight(key, completed))
        role = "leader"
    elif keep_alive_on_cancel:
        # A normal request attaching to a prefetch promotes the shared task to
        # the existing disconnect-safe behavior.
        _inflight_keep_alive[symbol] = True

    _inflight_waiters[symbol] = _inflight_waiters.get(symbol, 0) + 1
    started_at = time.perf_counter()
    try:
        # Shield the shared fetch so cancelling one waiter never interrupts
        # another waiter for the same symbol.
        result = await asyncio.shield(task)
        logger.info(
            "Realtime fund flow result request_id=%s tool=%s symbol=%s "
            "singleflight_role=%s wait=%.3fs outcome=%s",
            request_id,
            tool,
            symbol,
            role,
            time.perf_counter() - started_at,
            "error" if "error" in result else "success",
        )
        return result
    finally:
        if _inflight.get(symbol) is task:
            remaining = max(0, _inflight_waiters.get(symbol, 1) - 1)
            if remaining > 0:
                _inflight_waiters[symbol] = remaining
            else:
                _inflight_waiters.pop(symbol, None)
                if (
                    not _inflight_keep_alive.get(symbol, True)
                    and not task.done()
                ):
                    # Prefetch-only work has no consumer left. Remove it before
                    # cancellation so a new request cannot attach to a task
                    # that is already cancelling.
                    _inflight.pop(symbol, None)
                    _inflight_keep_alive.pop(symbol, None)
                    task.cancel()


# ── 主入口 ────────────────────────────────────────────────
async def get_fund_flow(
    symbols: list,
    *,
    keep_alive_on_cancel: bool = True,
) -> str:
    """
    批量查询资金流向。返回 JSON 字符串以保持与测试版完全一致。
    symbols 示例: ["dpzjlx", "000333", "600900", "300750"]
    """
    if not symbols:
        return json.dumps({}, ensure_ascii=False)

    tasks = [
        fetch_single_shared(sym, keep_alive_on_cancel=keep_alive_on_cancel)
        for sym in symbols
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = {}
    for sym, res in zip(symbols, raw_results):
        if isinstance(res, Exception):
            results[sym] = {"error": str(res)}
        else:
            results[sym] = res

    return json.dumps(results, ensure_ascii=False, indent=2)
