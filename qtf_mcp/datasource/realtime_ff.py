import asyncio
import json
import logging
import os
import platform
import re
import time
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..config import (
    ALL_INDICES,
    FUND_FLOW_PAGE_COLD_ATTEMPTS,
    FUND_FLOW_PAGE_DISGUISE,
    FUND_FLOW_PAGE_HEADFUL,
    FUND_FLOW_PAGE_KEEP_PAGES,
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

# 2C4G 建议并发数不超过 2
SEMAPHORE = asyncio.Semaphore(2)
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

# 真实构建自报的身份，只探一次。键是 UA 里的大版本号和平台，用来拼出与本机
# 一致的 client hints —— 平台必须取真实值，否则又会变成 Linux 上说 macOS。
_identity: dict | None = None


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
    match = re.search(r"(?:Headless)?Chrome/(\d+)", ua)
    major = match.group(1) if match else ""
    ch_platform = probe.get("chPlatform") or ""
    if not ch_platform:
        ch_platform = "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "macOS"
    _identity = {
        # HeadlessChrome 就是那句自报身份，只把它换掉，其余原样保留。
        "userAgent": ua.replace("HeadlessChrome", "Chrome"),
        "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
        "platform": probe.get("platform") or "",
        "userAgentMetadata": {
            "brands": [
                {"brand": "Not_A Brand", "version": "8"},
                {"brand": "Chromium", "version": major},
                {"brand": "Google Chrome", "version": major},
            ],
            "fullVersion": f"{major}.0.0.0",
            "platform": ch_platform,
            "platformVersion": "",
            "architecture": "arm" if "arm" in platform.machine().lower() else "x86",
            "model": "",
            "mobile": False,
        },
    }
    logger.info(
        "资金流向页面伪装身份 ua=%s ch_platform=%s",
        _identity["userAgent"][:70],
        ch_platform,
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
    if not FUND_FLOW_PAGE_DISGUISE:
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
                    headless=not FUND_FLOW_PAGE_HEADFUL,
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
                if FUND_FLOW_PAGE_DISGUISE:
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
                if FUND_FLOW_PAGE_DISGUISE:
                    await new_context.add_init_script(_HEADLESS_GAPS_SCRIPT)
            except BaseException:
                await _close_started_browser(new_playwright, new_browser)
                raise

            # 仅在完整初始化成功后发布，避免其他任务看到半初始化状态。
            _playwright = new_playwright
            _browser = new_browser
            _context = new_context
        return _context


async def close_browser():
    """服务退出时调用，清理资源"""
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
      早先 8 轮全新浏览器的观察也是"成功集中在前两次尝试"。``COLD_ATTEMPTS=2``
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


# 本进程是否成功从这个页面取到过数据。只用来决定"还要不要多试一次"，不代表
# 上游给了本会话任何长期放行——实测被拒是逐次随机的。
_session_warm = False


def session_is_warm() -> bool:
    """本进程是否已经成功从这个页面取到过数据。"""
    return _session_warm


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


async def load_fund_flow_page(symbol: str, context: BrowserContext) -> FundFlowPage:
    """加载一次页面，解析出今日与历史两块。

    两块都在同一个页面上，且都由 ``/fflow/`` 接口填充，所以分两次加载既浪费一次
    Chromium，又多一次被拒的机会。两个等待并发进行，实时路径的耗时上限因此与
    合并前一致。
    """
    global _session_warm

    wait_started_at = time.perf_counter()
    await SEMAPHORE.acquire()
    semaphore_wait = time.perf_counter() - wait_started_at
    service_started_at = time.perf_counter()
    outcome = "error"
    # 在 try 之外绑定，好让 finally 里的日志无论成败都能带上实际访问的地址。
    url = None
    try:
        url = get_fund_flow_url(symbol)
        if url is None:
            outcome = "unsupported"
            raise FundFlowPageUnavailable(symbol)

        page = await context.new_page()
        # 必须在 goto 之前：覆盖是 per-target 的，导航之后再装，这一次请求的
        # sec-ch-ua 已经带着 HeadlessChrome 发出去了。
        await disguise_page(page)
        refused: list[str] = []
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

        try:
            # 拦截无用资源，降低带宽和 CPU 消耗
            async def block_route(route):
                await route.abort()

            for pattern in BLOCKED_PATTERNS:
                await page.route(pattern, block_route)

            page.on("requestfailed", on_request_failed)
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await asyncio.gather(
                _wait_for_today(page, today_refused),
                _wait_for_history(page, history_refused),
            )
            content = await page.content()
        finally:
            if FUND_FLOW_PAGE_KEEP_PAGES:
                # 调试模式：留着页面供人工观察。每个页面是一个渲染进程，会持续
                # 占内存，所以只在排查时开。
                logger.info("调试模式保留页面 symbol=%s url=%s", symbol, url)
            else:
                await page.close()  # page 用完立即释放，context/browser 保留复用

        try:
            parsed = parse_fund_flow_page(content)
        except FundFlowPageError:
            parsed = None

        # 页面渲染成功但两块都没值，同时相关请求被拒。停牌和开盘前也会得到空值，
        # 但那时不会有请求失败，所以两个条件必须同时成立才算"被拒"。
        got_nothing = parsed is None or (not parsed.history and not parsed.has_today)
        if got_nothing and refused:
            captcha = parsed is not None and parsed.captcha_present
            outcome = "blocked_captcha" if captcha else "blocked"
            reason = (
                "东财风控要求滑块验证（页面已弹出验证框），过验证前接口不会返回数据"
                if captcha
                else "页面数据区为空"
            )
            raise FundFlowPageRefused(
                f"{symbol} 资金流接口拒绝了 {len(refused)} 个请求（空响应），{reason}"
            ) from None
        if parsed is None:
            raise FundFlowPageError(f"{symbol} 页面既无今日数据也无历史表")

        if not got_nothing:
            _session_warm = True
        outcome = f"today={parsed.has_today} history={len(parsed.history)}"
        return parsed
    finally:
        SEMAPHORE.release()
        request_id, tool, _ = log_context()
        logger.info(
            "Realtime fund flow page request_id=%s tool=%s symbol=%s url=%s "
            "outcome=%s semaphore_wait=%.3fs service=%.3fs",
            request_id,
            tool,
            symbol,
            url or "-",
            outcome,
            semaphore_wait,
            time.perf_counter() - service_started_at,
        )


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


async def _load_page_shared(
    symbol: str, *, require_history: bool = False, require_today: bool = False
) -> FundFlowPage:
    """加载页面；会话还冷时按配置多试几次。

    重试条件是"这次拿到的还不满足调用方"，不只是"被拒"：一次加载可能只拿到两块
    中的一块，而调用方要的恰好是另一块。

    次数上限见 FUND_FLOW_PAGE_COLD_ATTEMPTS 的注释——实测第三次不再带来成功，
    所以默认只有两次。本进程一旦成功取过数，就只试一次。
    """
    context = await get_context()
    attempts = FUND_FLOW_PAGE_COLD_ATTEMPTS if not _session_warm else 1
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            page = await load_fund_flow_page(symbol, context)
        except FundFlowPageRefused as e:
            last_error = e
            if attempt < attempts:
                logger.info(
                    "资金流向页面被拒，重试 symbol=%s 第%d/%d次",
                    symbol,
                    attempt + 1,
                    attempts,
                )
                continue
            raise
        if _satisfies(page, require_history, require_today) or attempt == attempts:
            return page
        logger.info(
            "资金流向页面数据不全，重试 symbol=%s 第%d/%d次 今日=%s 历史=%d",
            symbol,
            attempt + 1,
            attempts,
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
