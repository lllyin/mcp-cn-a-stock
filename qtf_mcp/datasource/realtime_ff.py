import asyncio
import json
import os
import time
from playwright.async_api import async_playwright, Browser, BrowserContext

from ..config import ALL_INDICES

# ── 全局单例 ──────────────────────────────────────────────
_playwright = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_lock = asyncio.Lock()

# 2C4G 建议并发数不超过 2
SEMAPHORE = asyncio.Semaphore(2)

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


# ── Browser 单例管理 ──────────────────────────────────────
async def get_context() -> BrowserContext:
    global _playwright, _browser, _context
    async with _lock:
        if _browser is None or not _browser.is_connected():
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
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
            _context = await _browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                java_script_enabled=True,
                bypass_csp=True,
            )
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
    async with SEMAPHORE:
        page = await context.new_page()

        # 提取纯数字部分进行判断 (如 SH000001 -> 000001)
        pure_code = "".join(filter(str.isdigit, symbol))
        # 如果是特殊标识 'dpzjlx' 或者是已知的沪深指数代码，则走大盘页面模板
        is_index_page = symbol == "dpzjlx" or pure_code in ALL_INDICES
        
        url = (
            "https://data.eastmoney.com/zjlx/dpzjlx.html"
            if is_index_page
            else f"https://data.eastmoney.com/zjlx/{symbol}.html"
        )

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
                "标的名称":      raw["name"] or symbol,
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


# ── 主入口 ────────────────────────────────────────────────
async def get_fund_flow(symbols: list) -> str:
    """
    批量查询资金流向。返回 JSON 字符串以保持与测试版完全一致。
    symbols 示例: ["dpzjlx", "000333", "600900", "300750"]
    """
    if not symbols:
        return json.dumps({}, ensure_ascii=False)

    context = await get_context()
    tasks = [fetch_single(sym, context) for sym in symbols]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = {}
    for sym, res in zip(symbols, raw_results):
        if isinstance(res, Exception):
            results[sym] = {"error": str(res)}
        else:
            results[sym] = res

    print(results)
    return json.dumps(results, ensure_ascii=False, indent=2)
