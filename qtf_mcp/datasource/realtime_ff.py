import asyncio
import json
import logging
import os
import time
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..config import ALL_INDICES
from ..observability import log_context

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
    return f"https://data.eastmoney.com/zjlx/{symbol}.html"


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


# ── 单个 Symbol 抓取 ──────────────────────────────────────
async def fetch_single(symbol: str, context: BrowserContext) -> dict:
    wait_started_at = time.perf_counter()
    await SEMAPHORE.acquire()
    semaphore_wait = time.perf_counter() - wait_started_at
    service_started_at = time.perf_counter()
    try:
        url = get_fund_flow_url(symbol)
        if url is None:
            return {"error": "暂无实时资金流向", "url": ""}

        page = await context.new_page()

        try:
            # 拦截无用资源，降低带宽和 CPU 消耗
            async def block_route(route):
                await route.abort()

            for pattern in BLOCKED_PATTERNS:
                await page.route(pattern, block_route)

            await page.goto(url, wait_until="domcontentloaded", timeout=25000)

            # 先等页面框架出现
            await page.wait_for_selector("text=今日主力净流入", timeout=10000)

            # 再等 Ajax 数据真正填入（超时则认为停牌/非交易时段，直接读当前值）
            try:
                await page.wait_for_function(WAIT_FOR_DATA_JS, timeout=12000)
            except Exception:
                # 超时：停牌股 / 非交易时段，数据本身就是空，继续解析拿到的值即可
                pass

            raw = await page.evaluate(PARSE_JS)

            def to_ratio(v: str) -> float:
                try:
                    return float(str(v).replace("%", ""))
                except Exception:
                    return 0.0

            return {
                "标的名称":      get_fund_flow_display_name(symbol, raw["name"]),
                "主力净流入":    raw["f62"],
                "主力净比(%)":   to_ratio(raw["f184"]),
                "超大单净流入":  raw["f66"],
                "超大单净比(%)": to_ratio(raw["f69"]),
                "大单净流入":    raw["f72"],
                "大单净比(%)":   to_ratio(raw["f75"]),
                "中单净流入":    raw["f78"],
                "中单净比(%)":   to_ratio(raw["f81"]),
                "小单净流入":    raw["f84"],
                "小单净比(%)":   to_ratio(raw["f87"]),
            }

        except Exception as e:
            return {"error": str(e), "url": url}

        finally:
            await page.close()  # page 用完立即释放，context/browser 保留复用
    finally:
        SEMAPHORE.release()
        request_id, tool, _ = log_context()
        logger.info(
            "Realtime fund flow page request_id=%s tool=%s symbol=%s "
            "semaphore_wait=%.3fs service=%.3fs",
            request_id,
            tool,
            symbol,
            semaphore_wait,
            time.perf_counter() - service_started_at,
        )


async def _fetch_single_with_context(symbol: str) -> dict:
    context = await get_context()
    return await fetch_single(symbol, context)


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
