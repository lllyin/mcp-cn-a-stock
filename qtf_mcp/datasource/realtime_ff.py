import asyncio
import json
import logging
import os
import time
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..config import ALL_INDICES, FUND_FLOW_PAGE_TABLE_WAIT_SECONDS
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


async def get_context() -> BrowserContext:
    global _playwright, _browser, _context
    async with _lock:
        if _browser is None or not _browser.is_connected():
            new_playwright = None
            new_browser = None
            try:
                new_playwright = await async_playwright().start()
                new_browser = await new_playwright.chromium.launch(
                    headless=True,
                    args=[
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
                new_context = await new_browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    java_script_enabled=True,
                    bypass_csp=True,
                )
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
    global _playwright, _browser, _context
    if _browser:
        await _browser.close()
        _browser = None
        _context = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


class FundFlowPageUnavailable(RuntimeError):
    """该标的没有资金流向页面（三大指数之外的指数）。"""


class FundFlowPageBlocked(RuntimeError):
    """页面加载成功，但资金流接口被风控拦截。

    风控表现为对 ``/fflow/`` 请求直接断连（``net::ERR_EMPTY_RESPONSE``）并要求
    人过一次滑块，页面框架照常渲染、数据区留空。放行是按浏览器会话给的：
    2026-09-03 手工过完滑块后，那个实例持续正常出数，而同一时刻新起的实例仍然
    全部为空。所以这不是重试能解决的失败，调用方应当长时间退避。
    """


# 本进程是否已有被风控放行的会话。盘中实时路径从 09:15 起持续加载这个页面，会话
# 一直是热的；而窗口外冷启动的第一次加载才是最容易撞上滑块的那次。
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


# 页面上两块数据由不同端点填充，而且会独立失败：2026-09-03 抓包里
# push2/…/fflow/kline/get（盘中曲线）被拒的同时，push2his/…/fflow/daykline/get
# （历史表）返回 200。所以抢答必须各盯各的，否则一个失败会连累另一个。
TODAY_ENDPOINTS = ("/fflow/kline/get", "/qt/stock/get")
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
    Chromium，又多一次撞风控的机会——风控是按会话放行的，页面加载次数本身就是
    风险。两个等待并发进行，实时路径的耗时上限因此与合并前一致。
    """
    global _session_warm

    wait_started_at = time.perf_counter()
    await SEMAPHORE.acquire()
    semaphore_wait = time.perf_counter() - wait_started_at
    service_started_at = time.perf_counter()
    outcome = "error"
    try:
        url = get_fund_flow_url(symbol)
        if url is None:
            outcome = "unsupported"
            raise FundFlowPageUnavailable(symbol)

        page = await context.new_page()
        refused: list[str] = []
        today_refused = asyncio.Event()
        history_refused = asyncio.Event()

        def on_request_failed(request) -> None:
            # 风控的特征是资金流接口被直接断连，而页面框架本身加载成功。
            url = request.url
            if any(part in url for part in TODAY_ENDPOINTS):
                refused.append(url)
                today_refused.set()
            elif any(part in url for part in HISTORY_ENDPOINTS):
                refused.append(url)
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
            await page.close()  # page 用完立即释放，context/browser 保留复用

        try:
            parsed = parse_fund_flow_page(content)
        except FundFlowPageError:
            if refused:
                outcome = "blocked"
                raise FundFlowPageBlocked(
                    f"{symbol} 资金流接口被拒 {len(refused)} 次，页面数据区为空；"
                    "本进程会话未获风控放行"
                ) from None
            raise

        _session_warm = True
        outcome = f"today={parsed.today is not None} history={len(parsed.history)}"
        return parsed
    finally:
        SEMAPHORE.release()
        request_id, tool, _ = log_context()
        logger.info(
            "Realtime fund flow page request_id=%s tool=%s symbol=%s "
            "outcome=%s semaphore_wait=%.3fs service=%.3fs",
            request_id,
            tool,
            symbol,
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


def _complete_page_inflight(symbol: str, task: asyncio.Task) -> None:
    if _page_inflight.get(symbol) is task:
        _page_inflight.pop(symbol, None)
    if not task.cancelled():
        task.exception()  # 取一次异常，避免"never retrieved"告警


async def _load_page_shared(symbol: str) -> FundFlowPage:
    context = await get_context()
    return await load_fund_flow_page(symbol, context)


async def fetch_page_shared(symbol: str) -> FundFlowPage:
    """同一标的的并发页面加载只做一次，今日与历史两个用途共享结果。"""
    key = page_key(symbol)
    task = _page_inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_load_page_shared(symbol))
        _page_inflight[key] = task
        task.add_done_callback(
            lambda completed, k=key: _complete_page_inflight(k, completed)
        )
    # shield：一个等待者被取消不能中断另一个等待者需要的加载。
    return await asyncio.shield(task)


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
    return await fetch_page_shared(symbol)


async def _fetch_single_with_context(symbol: str) -> dict:
    try:
        return _page_to_realtime_dict(symbol, await fetch_page_shared(symbol))
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
