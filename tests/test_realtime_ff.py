"""
Realtime fund-flow page routing tests.
"""

import asyncio
import datetime
import importlib
import logging
from io import StringIO

import numpy as np
import pytest
from pathlib import Path

from finmcp import research
from finmcp.config import _parse_range_ms
from finmcp.datasource import realtime_ff
from finmcp.datasource.realtime_ff import get_fund_flow_display_name, get_fund_flow_url

app_module = importlib.import_module("finmcp.mcp_app")


def test_core_indices_use_specific_index_pages():
    assert get_fund_flow_url("000001") == "https://data.eastmoney.com/zjlx/zs000001.html"
    assert get_fund_flow_url("SH000001") == "https://data.eastmoney.com/zjlx/zs000001.html"
    assert get_fund_flow_url("399001") == "https://data.eastmoney.com/zjlx/zs399001.html"
    assert get_fund_flow_url("SZ399001") == "https://data.eastmoney.com/zjlx/zs399001.html"
    assert get_fund_flow_url("399006") == "https://data.eastmoney.com/zjlx/zs399006.html"
    assert get_fund_flow_url("SZ399006") == "https://data.eastmoney.com/zjlx/zs399006.html"


def test_indices_without_realtime_pages_return_none():
    assert get_fund_flow_url("000688") is None
    assert get_fund_flow_url("SH000688") is None
    assert get_fund_flow_url("dpzjlx") == "https://data.eastmoney.com/zjlx/dpzjlx.html"


def test_stock_uses_stock_page():
    assert get_fund_flow_url("300308") == "https://data.eastmoney.com/zjlx/300308.html"


def test_core_indices_use_stable_display_names():
    assert get_fund_flow_display_name("000001", "沪深资金流向") == "上证指数"
    assert get_fund_flow_display_name("399001", "沪深资金流向") == "深证成指"
    assert get_fund_flow_display_name("399006", "沪深资金流向") == "创业板指"
    assert get_fund_flow_display_name("300308", "中际旭创") == "中际旭创"


@pytest.mark.asyncio
async def test_cancelled_browser_launch_stops_partial_playwright(monkeypatch):
    launch_started = asyncio.Event()

    class FakeChromium:
        async def launch(self, **kwargs):
            launch_started.set()
            await asyncio.Event().wait()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
            self.stopped = False

        async def start(self):
            return self

        async def stop(self):
            self.stopped = True

    fake_playwright = FakePlaywright()
    monkeypatch.setattr(realtime_ff, "async_playwright", lambda: fake_playwright)
    monkeypatch.setattr(realtime_ff, "_playwright", None)
    monkeypatch.setattr(realtime_ff, "_browser", None)
    monkeypatch.setattr(realtime_ff, "_context", None)

    startup = asyncio.create_task(realtime_ff.get_context())
    await launch_started.wait()
    startup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await startup

    assert fake_playwright.stopped
    assert realtime_ff._playwright is None
    assert realtime_ff._browser is None
    assert realtime_ff._context is None


@pytest.mark.asyncio
async def test_cancelled_context_creation_closes_partial_browser(monkeypatch):
    context_started = asyncio.Event()

    class FakeBrowser:
        def __init__(self):
            self.closed = False

        async def new_context(self, **kwargs):
            context_started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    fake_browser = FakeBrowser()

    class FakeChromium:
        async def launch(self, **kwargs):
            return fake_browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
            self.stopped = False

        async def start(self):
            return self

        async def stop(self):
            self.stopped = True

    fake_playwright = FakePlaywright()
    monkeypatch.setattr(realtime_ff, "async_playwright", lambda: fake_playwright)
    monkeypatch.setattr(realtime_ff, "_playwright", None)
    monkeypatch.setattr(realtime_ff, "_browser", None)
    monkeypatch.setattr(realtime_ff, "_context", None)

    startup = asyncio.create_task(realtime_ff.get_context())
    await context_started.wait()
    startup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await startup

    assert fake_browser.closed
    assert fake_playwright.stopped
    assert realtime_ff._playwright is None
    assert realtime_ff._browser is None
    assert realtime_ff._context is None


@pytest.mark.asyncio
async def test_realtime_fund_flow_singleflight_only_while_inflight(monkeypatch):
    calls = 0
    release = asyncio.Event()
    realtime_ff._inflight.clear()

    async def fake_fetch(symbol):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"标的名称": symbol, "主力净流入": "1亿"}

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    first = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    second = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    fresh_result = await realtime_ff.fetch_single_shared("300408")

    assert calls == 2
    assert first_result == second_result == fresh_result


@pytest.mark.asyncio
async def test_realtime_fetch_survives_disconnected_leader_for_retry(monkeypatch):
    calls = 0
    release = asyncio.Event()
    realtime_ff._inflight.clear()

    async def fake_fetch(symbol):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"标的名称": symbol, "主力净流入": "1亿"}

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    disconnected = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    await asyncio.sleep(0)
    disconnected.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnected

    retry = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    await asyncio.sleep(0)
    release.set()

    assert await retry == {"标的名称": "300408", "主力净流入": "1亿"}
    assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_prefetch_stops_unshared_underlying_fetch(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    realtime_ff._inflight.clear()
    realtime_ff._inflight_waiters.clear()
    realtime_ff._inflight_keep_alive.clear()

    async def fake_fetch(symbol):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    prefetch = asyncio.create_task(
        realtime_ff.get_fund_flow(
            ["300408"],
            keep_alive_on_cancel=False,
        )
    )
    await started.wait()
    shared = realtime_ff._inflight["300408"]

    prefetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prefetch
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert shared.cancelled()
    assert "300408" not in realtime_ff._inflight
    assert "300408" not in realtime_ff._inflight_waiters
    assert "300408" not in realtime_ff._inflight_keep_alive


@pytest.mark.asyncio
async def test_cancelled_prefetch_keeps_fetch_used_by_another_waiter(monkeypatch):
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    realtime_ff._inflight.clear()
    realtime_ff._inflight_waiters.clear()
    realtime_ff._inflight_keep_alive.clear()

    async def fake_fetch(symbol):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"标的名称": symbol, "主力净流入": "1亿"}

    monkeypatch.setattr(realtime_ff, "_fetch_single_with_context", fake_fetch)

    prefetch = asyncio.create_task(
        realtime_ff.get_fund_flow(
            ["300408"],
            keep_alive_on_cancel=False,
        )
    )
    await started.wait()
    shared = realtime_ff._inflight["300408"]
    follower = asyncio.create_task(realtime_ff.fetch_single_shared("300408"))
    while realtime_ff._inflight_waiters.get("300408") != 2:
        await asyncio.sleep(0)

    prefetch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await prefetch

    assert realtime_ff._inflight["300408"] is shared
    assert not shared.cancelled()

    release.set()
    assert await follower == {"标的名称": "300408", "主力净流入": "1亿"}
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["brief", "medium", "full"])
async def test_all_markdown_report_modes_use_playwright_during_live_window(monkeypatch, mode):
    calls = []

    async def fake_get_fund_flow(symbols, **kwargs):
        calls.append(symbols)
        return '{"300408": {"标的名称": "三环集团", "主力净流入": "1亿", "主力净比(%)": 1}}'

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    data = {
        "SYMBOL": "SZ300408",
        "DATE": np.array([int(datetime.datetime(2026, 8, 5).timestamp() * 1e9)]),
        "OPEN": np.array([10.0]),
        "HIGH": np.array([10.5]),
        "LOW": np.array([9.8]),
        "CLOSE": np.array([10.2]),
        "VOL": np.array([10000.0]),
        "AMOUNT": np.array([1000000.0]),
    }
    output = StringIO()

    await research.build_trading_data(
        output,
        "SZ300408",
        data,
        include_historical_fund_flow=mode == "full",
    )

    assert calls == [["300408"]]
    assert "主力净流入: 1亿" in output.getvalue()


def _live_window_raw_data(symbol: str = "SZ300408"):
    return {
        "SYMBOL": symbol,
        "NAME": "三环集团",
        "DATE": np.array([int(datetime.datetime(2026, 8, 5).timestamp() * 1e9)]),
        "OPEN": np.array([10.0]),
        "HIGH": np.array([10.5]),
        "LOW": np.array([9.8]),
        "CLOSE": np.array([10.2]),
        "VOLUME": np.array([10000.0]),
        "AMOUNT": np.array([1000000.0]),
    }


_LIVE_PAYLOAD = (
    '{"300408": {"标的名称": "三环集团", "主力净流入": "1亿", "主力净比(%)": 1}}'
)


def test_prefetch_skipped_when_scraping_would_not_run(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    assert research.start_realtime_fund_flow_prefetch("SZ300408", "2026-06-05") is None
    assert research.start_realtime_fund_flow_prefetch("SH000688") is None

    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: False)
    assert research.start_realtime_fund_flow_prefetch("SZ300408") is None


def test_prefetch_target_matches_report_target():
    for symbol in ("SZ300408", "SH600519", "SH000001", "SZ399006", "SH000688", "SH512480"):
        assert research.resolve_realtime_fund_flow_target(
            symbol
        ) == research.get_realtime_fund_flow_target(symbol, {})


@pytest.mark.asyncio
async def test_live_scrape_starts_before_base_data_completes(monkeypatch):
    order = []

    async def fake_get_fund_flow(symbols, **kwargs):
        order.append(f"scrape_started:{symbols}")
        await asyncio.sleep(0)
        return _LIVE_PAYLOAD

    async def fake_load_raw_data(symbol, end_date=None, who="", requirements=None):
        order.append("raw_started")
        await asyncio.sleep(0.02)
        order.append("raw_finished")
        return _live_window_raw_data(symbol)

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(app_module.research, "load_raw_data", fake_load_raw_data)

    response = await app_module.fetch_batch_reports("SZ300408", "brief", "")

    assert response.errors == {}
    assert "主力净流入: 1亿" in response.reports["SZ300408"]
    assert order.count("scrape_started:['300408']") == 1
    assert order.index("scrape_started:['300408']") < order.index("raw_finished")


@pytest.mark.asyncio
async def test_prefetched_report_is_identical_to_inline_report(monkeypatch):
    calls = []

    async def fake_get_fund_flow(symbols, **kwargs):
        calls.append(symbols)
        return _LIVE_PAYLOAD

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    inline = StringIO()
    await research.build_trading_data(inline, "SZ300408", _live_window_raw_data())

    prefetch = research.start_realtime_fund_flow_prefetch("SZ300408")
    assert prefetch is not None
    prefetched = StringIO()
    await research.build_trading_data(
        prefetched,
        "SZ300408",
        _live_window_raw_data(),
        realtime_fund_flow=prefetch,
    )

    assert prefetched.getvalue() == inline.getvalue()
    assert calls == [["300408"], ["300408"]]


@pytest.mark.asyncio
async def test_report_refetches_when_prefetch_target_differs(monkeypatch):
    calls = []

    async def fake_get_fund_flow(symbols, **kwargs):
        calls.append(symbols)
        return '{"dpzjlx": {"标的名称": "沪深两市", "主力净流入": "2亿", "主力净比(%)": 2}}'

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    prefetch = research.RealtimeFundFlowPrefetch(
        "300408",
        asyncio.create_task(asyncio.sleep(0, result=_LIVE_PAYLOAD)),
    )
    data = _live_window_raw_data()
    data["IS_MARKET"] = True
    output = StringIO()

    await research.build_trading_data(
        output,
        "SZ300408",
        data,
        realtime_fund_flow=prefetch,
    )

    assert calls == [["dpzjlx"]]
    assert "沪深两市主力净流入: 2亿" in output.getvalue()
    prefetch.discard()


@pytest.mark.asyncio
async def test_prefetch_failure_matches_inline_failure_output(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    async def failing_get_fund_flow(symbols, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(research, "get_fund_flow", failing_get_fund_flow)
    inline = StringIO()
    await research.build_trading_data(inline, "SZ300408", _live_window_raw_data())

    prefetch = research.start_realtime_fund_flow_prefetch("SZ300408")
    assert prefetch is not None
    prefetched = StringIO()
    await research.build_trading_data(
        prefetched,
        "SZ300408",
        _live_window_raw_data(),
        realtime_fund_flow=prefetch,
    )

    assert prefetched.getvalue() == inline.getvalue()
    assert "[实时调用异常] boom" in prefetched.getvalue()


@pytest.mark.asyncio
async def test_unused_prefetch_is_discarded_without_task_warnings(monkeypatch):
    started = []

    async def fake_get_fund_flow(symbols, **kwargs):
        started.append(symbols)
        await asyncio.sleep(5)
        return _LIVE_PAYLOAD

    async def empty_load_raw_data(symbol, end_date=None, who="", requirements=None):
        await asyncio.sleep(0.02)
        return {}

    monkeypatch.setattr(research, "get_fund_flow", fake_get_fund_flow)
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)
    monkeypatch.setattr(app_module.research, "load_raw_data", empty_load_raw_data)

    response = await app_module.fetch_batch_reports("SZ300408", "brief", "")

    assert "未找到证券代码" in response.errors["SZ300408"]
    assert started == [["300408"]]
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


@pytest.mark.asyncio
async def test_discard_consumes_failed_prefetch_exception(monkeypatch):
    monkeypatch.setattr(research, "is_realtime_fund_flow_window", lambda now=None: True)

    async def failing_get_fund_flow(symbols, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(research, "get_fund_flow", failing_get_fund_flow)
    prefetch = research.start_realtime_fund_flow_prefetch("SZ300408")
    assert prefetch is not None
    await asyncio.sleep(0)

    prefetch.discard()

    assert isinstance(prefetch.task.exception(), RuntimeError)


# --- 无头特征伪装 -----------------------------------------------------------
# 起因：sec-ch-ua 在每个请求头里写着 "HeadlessChrome";v="145"，是自报身份。
# 这里钉住"身份是从真实构建现算的"，不是硬编码——硬编码在 Linux 服务器上会变成
# UA 说 Macintosh、sec-ch-ua-platform 说 Linux 的新矛盾。


class _FakePage:
    def __init__(self, ua, platform_value, ch_platform):
        self._values = {"ua": ua, "platform": platform_value, "chPlatform": ch_platform}
        self.context = None
        self.closed = False

    async def evaluate(self, _script):
        return dict(self._values)

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self._page = page
        page.context = self
        self.sent = []

    async def new_page(self):
        return self._page

    async def new_cdp_session(self, _page):
        context = self

        class _Session:
            async def send(self, method, params):
                context.sent.append((method, params))

        return _Session()


LINUX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/145.0.7632.6 Safari/537.36"
)
MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/145.0.7632.6 Safari/537.36"
)
WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/145.0.7632.6 Safari/537.36"
)


@pytest.mark.asyncio
async def test_version_is_kept_whole_and_headless_is_dropped(monkeypatch):
    """版本号要完整保留：报一个比引擎新的版本会被特性检测抓出来。"""
    monkeypatch.setattr(realtime_ff, "_identity", None)
    monkeypatch.setattr(realtime_ff, "BROWSER_CLAIM_PLATFORM", "real")
    context = _FakeContext(_FakePage(MAC_UA, "MacIntel", "macOS"))

    identity = await realtime_ff._browser_identity(context)

    assert "HeadlessChrome" not in identity["userAgent"]
    assert "Chrome/145.0.7632.6" in identity["userAgent"]
    assert identity["userAgentMetadata"]["fullVersion"] == "145.0.7632.6"
    brands = [b["brand"] for b in identity["userAgentMetadata"]["brands"]]
    assert "HeadlessChrome" not in brands
    assert "Google Chrome" in brands
    assert identity["acceptLanguage"].startswith("zh-CN")


@pytest.mark.asyncio
async def test_linux_claims_macos_by_default(monkeypatch):
    """Linux 桌面份额极低，照实报等于自带一个少数派特征。

    改平台就得三处一起改，否则只是把一个矛盾换成另一个：UA 里的平台 token、
    navigator.platform、client hints 的 platform。
    """
    monkeypatch.setattr(realtime_ff, "_identity", None)
    monkeypatch.setattr(realtime_ff, "BROWSER_CLAIM_PLATFORM", "auto")
    context = _FakeContext(_FakePage(LINUX_UA, "Linux x86_64", "Linux"))

    identity = await realtime_ff._browser_identity(context)
    meta = identity["userAgentMetadata"]

    assert "Macintosh; Intel Mac OS X 10_15_7" in identity["userAgent"]
    assert "Linux" not in identity["userAgent"]
    assert identity["platform"] == "MacIntel"
    assert meta["platform"] == "macOS"
    assert meta["platformVersion"] == "15.6.0"
    # 版本号不能被平台替换弄丢
    assert "Chrome/145.0.7632.6" in identity["userAgent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ua,nav_platform,ch_platform",
    [(MAC_UA, "MacIntel", "macOS"), (WINDOWS_UA, "Win32", "Windows")],
)
async def test_common_desktop_platforms_are_reported_truthfully(
    monkeypatch, ua, nav_platform, ch_platform
):
    monkeypatch.setattr(realtime_ff, "_identity", None)
    monkeypatch.setattr(realtime_ff, "BROWSER_CLAIM_PLATFORM", "auto")
    context = _FakeContext(_FakePage(ua, nav_platform, ch_platform))

    identity = await realtime_ff._browser_identity(context)

    assert identity["userAgentMetadata"]["platform"] == ch_platform
    assert identity["platform"] == nav_platform


@pytest.mark.asyncio
async def test_claim_platform_real_keeps_linux(monkeypatch):
    """留 real 是为了在部署机上做对照，不能被 auto 的规则覆盖掉。"""
    monkeypatch.setattr(realtime_ff, "_identity", None)
    monkeypatch.setattr(realtime_ff, "BROWSER_CLAIM_PLATFORM", "real")
    context = _FakeContext(_FakePage(LINUX_UA, "Linux x86_64", "Linux"))

    identity = await realtime_ff._browser_identity(context)

    assert "X11; Linux x86_64" in identity["userAgent"]
    assert identity["userAgentMetadata"]["platform"] == "Linux"
    assert identity["userAgentMetadata"]["platformVersion"] == "6.8.0"


@pytest.mark.asyncio
async def test_identity_is_probed_once(monkeypatch):
    monkeypatch.setattr(realtime_ff, "_identity", None)
    page = _FakePage("Chrome/140.0.0.0", "MacIntel", "macOS")
    context = _FakeContext(page)

    first = await realtime_ff._browser_identity(context)
    second = await realtime_ff._browser_identity(context)

    assert first is second


@pytest.mark.asyncio
async def test_disguise_page_sends_the_override(monkeypatch):
    monkeypatch.setattr(realtime_ff, "_identity", None)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", True)
    page = _FakePage("HeadlessChrome/145.0.0.0", "MacIntel", "macOS")
    context = _FakeContext(page)

    await realtime_ff.disguise_page(page)

    assert [m for m, _ in context.sent] == ["Network.setUserAgentOverride"]


@pytest.mark.asyncio
async def test_disguise_is_skipped_when_switched_off(monkeypatch):
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    page = _FakePage("HeadlessChrome/145.0.0.0", "MacIntel", "macOS")
    context = _FakeContext(page)

    await realtime_ff.disguise_page(page)

    assert context.sent == []


@pytest.mark.asyncio
async def test_disguise_failure_never_breaks_the_fetch(monkeypatch):
    """拿不到数据的代价远大于指纹暴露，伪装失败必须放行。"""
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", True)
    monkeypatch.setattr(realtime_ff, "_identity", None)

    class _Boom:
        context = None

        async def evaluate(self, _script):
            raise RuntimeError("CDP 挂了")

    boom = _Boom()
    boom.context = _FakeContext(_FakePage("x", "y", "z"))

    await realtime_ff.disguise_page(boom)   # 不抛


async def _noop_context():
    return None


# --- 没数据先 reload 一次 ---------------------------------------------------
# 唯一新增的逻辑就这一条：页面打开没拿到数据，先在同一个 tab 上刷一次，刷了还没有
# 才关掉开下一个 tab。reload 留在同一段信号量持有区间内 —— tab 一旦活过这段区间，
# "整个浏览器同时最多 2 个 tab"这个隐式上限就失效了。


class _FakeTabPage:
    """够用的假页面：记录 goto / reload / close 的顺序。"""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []
        self.closed = False
        self._listeners = {}

    async def goto(self, url, **_kwargs):
        self.calls.append("goto")

    async def reload(self, **_kwargs):
        self.calls.append("reload")

    async def content(self):
        return self.contents.pop(0) if self.contents else ""

    async def route(self, *_a, **_k):
        pass

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self._listeners.get(event, []).remove(handler)

    async def close(self):
        self.closed = True


class _TabPageContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


EMPTY_HTML = '<div class="title">三环集团(300408)</div><td data-field="f62"></td>'


def _full_html():
    return (Path(__file__).parent / "fixtures" / "eastmoney_zjlx_full_300408.html").read_text(
        encoding="utf-8"
    )


async def _no_wait(*_a, **_k):
    return None


@pytest.mark.asyncio
async def test_reloads_the_same_tab_before_opening_another(monkeypatch):
    """第一次没数据 -> 同一个 tab reload，不是新开一个。"""
    page = _FakeTabPage([EMPTY_HTML, _full_html()])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
    monkeypatch.setattr(realtime_ff, "_sleep_before_retry", lambda: _resolved(0.3))

    parsed = await realtime_ff.load_fund_flow_page(
        "300408", _TabPageContext(page), loads=2,
        satisfies=lambda p: bool(p.history),
    )

    assert page.calls == ["goto", "reload"]
    assert len(parsed.history) == 121
    assert page.closed                      # 用完即关，tab 不越出这段区间


@pytest.mark.asyncio
async def test_an_unsupported_symbol_still_logs_one_line(monkeypatch, caplog):
    """日志改成一次加载一行之后，走不到那一行的两条路要靠 finally 补记。

    科创50 没有资金流向页面，每次查询都走这条路；它在日志里消失了就等于看不见
    这层被调用过多少次。
    """
    monkeypatch.setattr(realtime_ff, "get_fund_flow_url", lambda _s: None)

    with caplog.at_level(logging.INFO, logger="finmcp.datasource.realtime_ff"):
        with pytest.raises(realtime_ff.FundFlowPageUnavailable):
            await realtime_ff.load_fund_flow_page("SH000688", _TabPageContext(None))

    lines = [r.getMessage() for r in caplog.records if "Realtime fund flow page" in r.getMessage()]
    assert len(lines) == 1
    assert "outcome=unsupported" in lines[0] and "how=-" in lines[0]


@pytest.mark.asyncio
async def test_a_load_that_blows_up_still_logs_one_line(monkeypatch, caplog):
    """goto 超时之类的异常也要留下 outcome=error，否则只剩上游一句笼统的失败。"""
    page = _FakeTabPage([])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)

    async def boom(*_a, **_k):
        raise TimeoutError("goto 超时")

    monkeypatch.setattr(realtime_ff, "_load_once", boom)

    with caplog.at_level(logging.INFO, logger="finmcp.datasource.realtime_ff"):
        with pytest.raises(TimeoutError):
            await realtime_ff.load_fund_flow_page("300408", _TabPageContext(page))

    lines = [r.getMessage() for r in caplog.records if "Realtime fund flow page" in r.getMessage()]
    assert len(lines) == 1
    assert "outcome=error" in lines[0] and "how=new_tab" in lines[0]
    assert page.closed


@pytest.mark.asyncio
async def test_each_load_logs_exactly_one_line(monkeypatch, caplog):
    """goto + reload = 两行，靠 how 区分；不能少记也不能被 finally 重复补记。"""
    page = _FakeTabPage([EMPTY_HTML, _full_html()])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
    monkeypatch.setattr(realtime_ff, "_sleep_before_retry", lambda: _resolved(0.0))

    with caplog.at_level(logging.INFO, logger="finmcp.datasource.realtime_ff"):
        await realtime_ff.load_fund_flow_page(
            "300408", _TabPageContext(page), loads=2,
            satisfies=lambda p: bool(p.history),
        )

    lines = [r.getMessage() for r in caplog.records if "Realtime fund flow page" in r.getMessage()]
    assert len(lines) == 2
    assert "how=new_tab" in lines[0] and "how=reload" in lines[1]
    # 名额是这个 tab 一开始就拿到的，reload 时不该再报一次等待
    assert "semaphore_wait=0.000s" in lines[1]


@pytest.mark.asyncio
async def test_one_load_never_reloads(monkeypatch):
    """预算只有一次时不 reload——会话已经热了就不该多付一次加载。"""
    page = _FakeTabPage([_full_html()])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)

    await realtime_ff.load_fund_flow_page("300408", _TabPageContext(page), loads=1)

    assert page.calls == ["goto"]


@pytest.mark.asyncio
async def test_a_satisfied_first_load_does_not_reload(monkeypatch):
    """第一次就拿到了想要的，不该白刷一次。"""
    page = _FakeTabPage([_full_html()])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)

    await realtime_ff.load_fund_flow_page(
        "300408", _TabPageContext(page), loads=2,
        satisfies=lambda p: bool(p.history),
    )

    assert page.calls == ["goto"]


@pytest.mark.asyncio
async def test_the_tab_is_closed_even_when_every_load_fails(monkeypatch):
    """两次都没数据也要关掉，否则 tab 会活过信号量区间、上限失效。"""
    page = _FakeTabPage([EMPTY_HTML, EMPTY_HTML])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
    monkeypatch.setattr(realtime_ff, "_sleep_before_retry", lambda: _resolved(0.0))

    # 两次都没数据：解析本身是成功的（页面框架在，数据区空），所以按契约把这份
    # 不满足要求的结果交回上层，由它决定换不换 tab——不在这里抛。
    parsed = await realtime_ff.load_fund_flow_page(
        "300408", _TabPageContext(page), loads=2,
        satisfies=lambda p: bool(p.history),
    )

    assert parsed.history == [] and parsed.has_today is False
    assert page.calls == ["goto", "reload"]
    assert page.closed


@pytest.mark.asyncio
async def test_the_semaphore_is_released_after_both_loads(monkeypatch):
    """reload 在同一段持有区间内，结束后必须把名额还回去。"""
    page = _FakeTabPage([EMPTY_HTML, _full_html()])
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
    monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
    monkeypatch.setattr(realtime_ff, "_sleep_before_retry", lambda: _resolved(0.0))

    before = realtime_ff.SEMAPHORE._value
    await realtime_ff.load_fund_flow_page(
        "300408", _TabPageContext(page), loads=2,
        satisfies=lambda p: bool(p.history),
    )
    assert realtime_ff.SEMAPHORE._value == before


# --- reload 之前的随机等待 -------------------------------------------------
# 只作用在重试路径上：那一次已经被拒、本来就要再付一次页面加载。


@pytest.mark.asyncio
async def test_retry_delay_lands_in_the_configured_range(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(realtime_ff.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_RETRY_DELAY_MS", (250.0, 350.0))

    for _ in range(40):
        delay = await realtime_ff._sleep_before_retry()
        assert 0.250 <= delay <= 0.350
    assert len(slept) == 40
    # 随机而不是固定：40 次里不该只有一个值
    assert len(set(slept)) > 1


@pytest.mark.asyncio
async def test_retry_delay_can_be_switched_off(monkeypatch):
    async def boom(_seconds):
        raise AssertionError("置 0 时不该 sleep")

    monkeypatch.setattr(realtime_ff.asyncio, "sleep", boom)
    monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_RETRY_DELAY_MS", (0.0, 0.0))

    assert await realtime_ff._sleep_before_retry() == 0.0


class TestRetryDelayParsing:
    """区间写成一个 "下界,上界" 字符串，解析要宽进严出。"""

    def test_two_values(self):
        assert _parse_range_ms("250,350", "1,2") == (250.0, 350.0)

    def test_spaces_are_tolerated(self):
        assert _parse_range_ms(" 250 , 350 ", "1,2") == (250.0, 350.0)

    def test_single_value_means_a_fixed_delay(self):
        assert _parse_range_ms("300", "1,2") == (300.0, 300.0)

    def test_reversed_order_still_works(self):
        assert _parse_range_ms("350,250", "1,2") == (250.0, 350.0)

    def test_zero_switches_it_off(self):
        assert _parse_range_ms("0", "1,2") == (0.0, 0.0)

    def test_negative_is_clamped_not_rejected(self):
        assert _parse_range_ms("-50,350", "1,2") == (0.0, 350.0)

    def test_unset_falls_back_to_the_default(self):
        assert _parse_range_ms(None, "250,350") == (250.0, 350.0)
        assert _parse_range_ms("", "250,350") == (250.0, 350.0)

    def test_garbage_raises_instead_of_silently_defaulting(self):
        # 配错了要在启动时就炸，别让运维以为自己配上了。
        with pytest.raises(ValueError):
            _parse_range_ms("250ms,350ms", "1,2")
        with pytest.raises(ValueError):
            _parse_range_ms(",", "1,2")


async def _resolved(value):
    return value


# --- P0：加载预算不受"之前成功过"影响 -------------------------------------
# 原先的逻辑是本进程成功取到过一次数据就把预算塌到 1（只 goto、不 reload）。
# 那个区分在依据上和代价上都站不住，见 FUND_FLOW_PAGE_MAX_LOADS 的注释。


class TestLoadBudget:
    @pytest.mark.asyncio
    async def test_an_empty_page_still_gets_its_reload(self, monkeypatch):
        """第一次拿到空页面（不是被拒）也要 reload。

        这条路是真实存在的：页面框架渲染成功、解析成功、两块都空、且没有任何
        请求被拒（停牌、开盘前、上游静默返回空都是这样）。原先的代码把这种空
        页面也算成"取到过数据"，于是本进程后面所有标的都只加载一次。
        """
        page = _FakeTabPage([EMPTY_HTML, _full_html()])
        monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
        monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
        monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
        monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
        monkeypatch.setattr(realtime_ff, "_sleep_before_retry", lambda: _resolved(0.0))
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 2)

        async def one_context():
            return _TabPageContext(page)

        monkeypatch.setattr(realtime_ff, "get_context", one_context)

        parsed = await realtime_ff._load_page_shared("300408", require_history=True)

        assert page.calls == ["goto", "reload"]
        assert len(parsed.history) == 121

    @pytest.mark.asyncio
    async def test_an_earlier_success_does_not_shrink_a_later_budget(
        self, monkeypatch
    ):
        """前一个标的成功，不能让后一个标的少一次重试。

        这正是并发 4×4 实测里丢掉 SH603986 和 SH600030 的原因：批 1 有一个标的
        成功之后，它们都只加载了一次就拿着 history=0 放弃了。
        """
        monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
        monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
        monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
        monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
        monkeypatch.setattr(realtime_ff, "_sleep_before_retry", lambda: _resolved(0.0))
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 2)
        realtime_ff._page_cache.clear()

        first = _FakeTabPage([_full_html()])
        second = _FakeTabPage([EMPTY_HTML, _full_html()])
        pages = [first, second]

        async def next_context():
            return _TabPageContext(pages.pop(0))

        monkeypatch.setattr(realtime_ff, "get_context", next_context)

        await realtime_ff._load_page_shared("300408", require_history=True)
        assert first.calls == ["goto"]          # 一次就拿到，不多花

        realtime_ff._page_cache.clear()
        await realtime_ff._load_page_shared("600519", require_history=True)
        assert second.calls == ["goto", "reload"]   # 预算没被前一个标的吃掉

    @pytest.mark.asyncio
    async def test_the_happy_path_still_costs_one_load(self, monkeypatch):
        """预算是上限不是配额：一次就拿到就不再加载，顺利路径零额外开销。"""
        page = _FakeTabPage([_full_html()])
        monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)
        monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
        monkeypatch.setattr(realtime_ff, "_wait_for_today", _no_wait)
        monkeypatch.setattr(realtime_ff, "_wait_for_history", _no_wait)
        monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 2)
        realtime_ff._page_cache.clear()

        async def one_context():
            return _TabPageContext(page)

        monkeypatch.setattr(realtime_ff, "get_context", one_context)
        await realtime_ff._load_page_shared("300408", require_history=True)

        assert page.calls == ["goto"]


# --- P4：90 分钟空闲回收 -------------------------------------------------
# 回收本身（close_browser）早就写好了，缺的是"什么时候调"，以及别在别人用到
# 一半的时候调。


class _FakeBrowserForIdle:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


@pytest.fixture
def idle_browser(monkeypatch):
    """装一个假的已建好的浏览器，并保证测试结束后全局状态复原。"""
    browser = _FakeBrowserForIdle()
    monkeypatch.setattr(realtime_ff, "_browser", browser)
    monkeypatch.setattr(realtime_ff, "_context", object())
    monkeypatch.setattr(realtime_ff, "_playwright", None)
    monkeypatch.setattr(realtime_ff, "_browser_users", 0)
    monkeypatch.setattr(realtime_ff, "_idle_timer", None)
    yield browser
    if realtime_ff._idle_timer is not None:
        realtime_ff._idle_timer.cancel()
        realtime_ff._idle_timer = None


class TestIdleTeardown:
    @pytest.mark.asyncio
    async def test_the_lease_blocks_teardown_while_in_use(
        self, monkeypatch, idle_browser
    ):
        """借用期间到点也不能拆——这正是那个会制造数据丢失的竞态。"""
        monkeypatch.setattr(realtime_ff, "BROWSER_IDLE_TIMEOUT_SECONDS", 3600)

        async def fake_get_context():
            return realtime_ff._context

        monkeypatch.setattr(realtime_ff, "get_context", fake_get_context)

        async with realtime_ff.browser_lease():
            assert realtime_ff._browser_users == 1
            # 借用期间定时器必须是撤掉的状态
            assert realtime_ff._idle_timer is None
            await realtime_ff._close_if_idle()
            assert idle_browser.closed is False

        # 最后一个借用者离开，定时器排上
        assert realtime_ff._browser_users == 0
        assert realtime_ff._idle_timer is not None

    @pytest.mark.asyncio
    async def test_teardown_happens_once_nobody_holds_it(
        self, monkeypatch, idle_browser
    ):
        monkeypatch.setattr(realtime_ff, "BROWSER_IDLE_TIMEOUT_SECONDS", 3600)

        await realtime_ff._close_if_idle()

        assert idle_browser.closed is True
        assert realtime_ff._browser is None
        assert realtime_ff._context is None

    @pytest.mark.asyncio
    async def test_zero_switches_idle_teardown_off(self, monkeypatch, idle_browser):
        monkeypatch.setattr(realtime_ff, "BROWSER_IDLE_TIMEOUT_SECONDS", 0)

        async def fake_get_context():
            return realtime_ff._context

        monkeypatch.setattr(realtime_ff, "get_context", fake_get_context)

        async with realtime_ff.browser_lease():
            pass

        assert realtime_ff._idle_timer is None
        assert idle_browser.closed is False

    @pytest.mark.asyncio
    async def test_the_lease_releases_even_when_the_body_raises(
        self, monkeypatch, idle_browser
    ):
        """计数漏减一次，浏览器就永远拆不掉了。"""
        monkeypatch.setattr(realtime_ff, "BROWSER_IDLE_TIMEOUT_SECONDS", 3600)

        async def fake_get_context():
            return realtime_ff._context

        monkeypatch.setattr(realtime_ff, "get_context", fake_get_context)

        with pytest.raises(RuntimeError):
            async with realtime_ff.browser_lease():
                raise RuntimeError("boom")

        assert realtime_ff._browser_users == 0

    @pytest.mark.asyncio
    async def test_the_lease_releases_even_when_launching_fails(
        self, monkeypatch, idle_browser
    ):
        """建浏览器本身失败也要还计数——否则一次启动失败就锁死回收。"""
        monkeypatch.setattr(realtime_ff, "BROWSER_IDLE_TIMEOUT_SECONDS", 3600)

        async def boom():
            raise RuntimeError("launch failed")

        monkeypatch.setattr(realtime_ff, "get_context", boom)

        with pytest.raises(RuntimeError):
            async with realtime_ff.browser_lease():
                pass

        assert realtime_ff._browser_users == 0

    @pytest.mark.asyncio
    async def test_a_second_lease_cancels_the_pending_timer(
        self, monkeypatch, idle_browser
    ):
        """定时器排下之后又来了请求，必须撤掉，不能让它在用到一半时开火。"""
        monkeypatch.setattr(realtime_ff, "BROWSER_IDLE_TIMEOUT_SECONDS", 3600)

        async def fake_get_context():
            return realtime_ff._context

        monkeypatch.setattr(realtime_ff, "get_context", fake_get_context)

        async with realtime_ff.browser_lease():
            pass
        armed = realtime_ff._idle_timer
        assert armed is not None

        async with realtime_ff.browser_lease():
            assert armed.cancelled()
            assert realtime_ff._idle_timer is None
