"""浏览器身份回到 main、被拒不 reload、给页面自重试留时间、实时路径接熔断器。

依据是 2026-09-07 盘外两次交错 A/B（机房出口 IP，每种身份 36 次加载）：main 原样身份 36/36，
只加 AutomationControlled 26/36，完整伪装 23/36；首加载被拒后靠新 tab 救回 7 次、reload 3 次；
12 次在接口首次断连后 1.9 到 3.7 秒内由页面脚本自己重发拿到数据。细节见
private/priorities-2026-09-07.md 与 docs/technical-details.md 页面兜底一节。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from finmcp.datasource import realtime_ff
from finmcp.datasource.fund_flow_page import FundFlowPage, parse_fund_flow_page

FULL_PAGE = Path(__file__).parent / "fixtures" / "eastmoney_zjlx_full_300408.html"


# --- 浏览器身份 -------------------------------------------------------------


class _FakeContext:
    def __init__(self):
        self.init_scripts = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)


class _FakeBrowser:
    def __init__(self):
        self.context_kwargs = None
        self.context = _FakeContext()

    def is_connected(self):
        return True

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context

    async def close(self):
        pass


class _FakePlaywright:
    def __init__(self, browser):
        self.browser = browser
        self.launch_kwargs = None
        self.chromium = self

    async def start(self):
        return self

    async def stop(self):
        pass

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


def _fresh_browser_globals(monkeypatch, playwright):
    monkeypatch.setattr(realtime_ff, "async_playwright", lambda: playwright)
    monkeypatch.setattr(realtime_ff, "_playwright", None)
    monkeypatch.setattr(realtime_ff, "_browser", None)
    monkeypatch.setattr(realtime_ff, "_context", None)


@pytest.mark.asyncio
async def test_the_default_identity_is_the_main_branch_one(monkeypatch):
    """不开伪装：不带 AutomationControlled，固定 Chrome/120 UA，不注入、不改 locale。"""
    browser = _FakeBrowser()
    playwright = _FakePlaywright(browser)
    _fresh_browser_globals(monkeypatch, playwright)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)

    await realtime_ff.get_context()

    args = playwright.launch_kwargs["args"]
    assert "--disable-blink-features=AutomationControlled" not in args
    assert args == realtime_ff._LAUNCH_ARGS
    assert browser.context_kwargs["user_agent"] == realtime_ff.LEGACY_UA
    assert "Chrome/120.0.0.0" in realtime_ff.LEGACY_UA
    assert "locale" not in browser.context_kwargs
    assert "viewport" not in browser.context_kwargs
    assert browser.context.init_scripts == []


@pytest.mark.asyncio
async def test_the_disguise_is_opt_in_and_brings_the_flag_with_it(monkeypatch):
    browser = _FakeBrowser()
    playwright = _FakePlaywright(browser)
    _fresh_browser_globals(monkeypatch, playwright)
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", True)

    await realtime_ff.get_context()

    assert playwright.launch_kwargs["args"][0] == "--disable-blink-features=AutomationControlled"
    assert browser.context_kwargs["locale"] == "zh-CN"
    assert "user_agent" not in browser.context_kwargs       # UA 由 CDP 覆盖按真实版本现算
    assert browser.context.init_scripts == [realtime_ff._HEADLESS_GAPS_SCRIPT]


def test_disguise_defaults_off_in_config(monkeypatch):
    """默认值就是 main 的身份；要对照才显式打开。不 reload 模块：那会把 http_channel 等
    在导入时抓住的常量换掉，让别的测试文件按顺序失败。"""
    from finmcp import config

    monkeypatch.delenv("BROWSER_DISGUISE", raising=False)
    assert config._parse_bool(config.env("BROWSER_DISGUISE"), False) is False
    assert config.BROWSER_DISGUISE is False


# --- 被拒后的等待 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_still_waits_for_the_page_to_retry(monkeypatch):
    """接口断连不等于没数据：页面脚本会自己重发，几秒内到的照样算拿到。"""
    monkeypatch.setattr(realtime_ff, "REFUSAL_GRACE_SECONDS", 0.5)
    refused = asyncio.Event()
    refused.set()
    arrived = []

    async def fills_shortly():
        await asyncio.sleep(0.05)
        arrived.append(True)

    await realtime_ff._race_with_refusal(fills_shortly(), refused)

    assert arrived == [True]


@pytest.mark.asyncio
async def test_the_grace_period_is_bounded(monkeypatch):
    monkeypatch.setattr(realtime_ff, "REFUSAL_GRACE_SECONDS", 0.1)
    refused = asyncio.Event()
    refused.set()

    async def never_fills():
        await asyncio.sleep(30)

    started = time.perf_counter()
    await realtime_ff._race_with_refusal(never_fills(), refused)

    assert time.perf_counter() - started < 1.0


# --- 一个 tab 里的加载阶梯 ------------------------------------------------------


class _Tab:
    def __init__(self):
        self.closed = False

    async def route(self, *_a, **_k):
        pass

    async def close(self):
        self.closed = True


class _TabContext:
    def __init__(self):
        self.tabs = []

    async def new_page(self):
        tab = _Tab()
        self.tabs.append(tab)
        return tab


def _pages():
    full = parse_fund_flow_page(FULL_PAGE.read_text(encoding="utf-8"))
    today_only = FundFlowPage(
        name=full.name, code=full.code, title_text=full.title_text,
        today=full.today, today_text=full.today_text, history=[],
    )
    return full, today_only


@pytest.fixture
def quiet_tab(monkeypatch):
    monkeypatch.setattr(realtime_ff, "BROWSER_DISGUISE", False)
    monkeypatch.setattr(realtime_ff, "BROWSER_KEEP_PAGES", False)

    async def no_sleep():
        return 0.0

    monkeypatch.setattr(realtime_ff, "_sleep_before_retry", no_sleep)


@pytest.mark.asyncio
async def test_a_refused_load_is_not_reloaded_in_the_same_tab(monkeypatch, quiet_tab):
    """刷新带不走滑块，新 tab 才带得走：被拒就抛给上层，不在同一个 tab 上 reload。"""
    seen = []

    async def refused(page, symbol, url, *, reload):
        seen.append(reload)
        return None, realtime_ff._PageRefusal("拒", captcha=True), {"today", "history"}

    monkeypatch.setattr(realtime_ff, "_load_once", refused)
    context = _TabContext()
    stats: dict = {}

    with pytest.raises(realtime_ff.FundFlowPageRefused):
        await realtime_ff.load_fund_flow_page(
            "300408", context, loads=2, satisfies=lambda p: True, stats=stats
        )

    assert seen == [False]                       # 只有一次 goto，没有 reload
    assert stats == {"loads": 1, "refused": {"today", "history"}}
    assert context.tabs[0].closed


@pytest.mark.asyncio
async def test_a_refused_missing_block_is_not_retried(monkeypatch, quiet_tab):
    """今日到了、历史被接口拒了：再加载只是再被拒一次，把今日交出去。"""
    _, today_only = _pages()
    seen = []

    async def today_but_history_refused(page, symbol, url, *, reload):
        seen.append(reload)
        return today_only, None, {"history"}

    monkeypatch.setattr(realtime_ff, "_load_once", today_but_history_refused)
    stats: dict = {}

    page = await realtime_ff.load_fund_flow_page(
        "300408", _TabContext(), loads=2, satisfies=lambda p: bool(p.history), stats=stats
    )

    assert page is today_only
    assert seen == [False]
    assert stats["refused"] == {"history"}


@pytest.mark.asyncio
async def test_an_empty_but_unrefused_page_still_reloads(monkeypatch, quiet_tab):
    """没被拒只是没填完（冷启动、开盘前）：同一个 tab reload 的老路保留。"""
    full, today_only = _pages()
    empty = FundFlowPage(name=full.name, code=full.code, title_text=full.title_text)
    results = [(empty, None, set()), (full, None, set())]
    seen = []

    async def slow_fill(page, symbol, url, *, reload):
        seen.append(reload)
        return results[len(seen) - 1]

    monkeypatch.setattr(realtime_ff, "_load_once", slow_fill)
    stats: dict = {}

    page = await realtime_ff.load_fund_flow_page(
        "300408", _TabContext(), loads=2, satisfies=lambda p: p.has_today, stats=stats
    )

    assert page is full
    assert seen == [False, True]
    assert stats["loads"] == 2


# --- 跨 tab 的预算与熔断 ----------------------------------------------------------


async def _fake_context():
    return object()


@pytest.fixture
def shared_loader(monkeypatch):
    monkeypatch.setattr(realtime_ff, "get_context", _fake_context)
    realtime_ff._PAGE_BREAKER.reset()
    yield
    realtime_ff._PAGE_BREAKER.reset()


@pytest.mark.asyncio
async def test_a_refusal_costs_one_load_and_the_next_tab_gets_the_rest(monkeypatch, shared_loader):
    """预算 2：第一个 tab 被拒只花 1 次，第二个 tab 拿剩下的 1 次。原先按 2 记账，第二个 tab 轮不到。"""
    full, _ = _pages()
    attempts = []

    async def flaky(symbol, context, *, loads=1, satisfies=None, stats=None):
        attempts.append(loads)
        stats["loads"] = 1
        stats["refused"] = set()
        if len(attempts) == 1:
            stats["refused"] = {"today", "history"}
            raise realtime_ff._PageRefusal("拒", captcha=True)
        return full

    monkeypatch.setattr(realtime_ff, "load_fund_flow_page", flaky)
    monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 2)

    page = await realtime_ff._load_page_shared("300408", require_today=True)

    assert attempts == [2, 1]
    assert len(page.history) == 121
    assert realtime_ff._PAGE_BREAKER.is_open is False


@pytest.mark.asyncio
async def test_a_refused_history_does_not_open_another_tab_nor_trip_the_breaker(monkeypatch, shared_loader):
    """历史被接口拒了：把今日交出去，不换 tab；daykline 在 main 身份下也有一半被拒，不能拿它熔断。"""
    _, today_only = _pages()
    attempts = []

    async def history_refused(symbol, context, *, loads=1, satisfies=None, stats=None):
        attempts.append(loads)
        stats["loads"] = 1
        stats["refused"] = {"history"}
        return today_only

    monkeypatch.setattr(realtime_ff, "load_fund_flow_page", history_refused)
    monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 3)

    page = await realtime_ff._load_page_shared("300408", require_history=True)

    assert attempts == [2]
    assert page is today_only
    assert realtime_ff._PAGE_BREAKER.is_open is False


@pytest.mark.asyncio
async def test_repeated_refusals_pause_the_browser_layer(monkeypatch, shared_loader):
    """实时那条路原先没有熔断器：滑块一出照样一个标的接一个标的地连发。"""
    attempts = []

    async def always_refused(symbol, context, *, loads=1, satisfies=None, stats=None):
        attempts.append(symbol)
        stats["loads"] = 1
        stats["refused"] = {"today", "history"}
        raise realtime_ff._PageRefusal("拒", captcha=True)

    monkeypatch.setattr(realtime_ff, "load_fund_flow_page", always_refused)
    monkeypatch.setattr(realtime_ff, "FUND_FLOW_PAGE_MAX_LOADS", 1)
    monkeypatch.setattr(realtime_ff._PAGE_BREAKER, "threshold", 2)
    monkeypatch.setattr(realtime_ff._PAGE_BREAKER, "window", 60.0)

    for symbol in ("600026", "600938"):
        with pytest.raises(realtime_ff.FundFlowPageRefused):
            await realtime_ff._load_page_shared(symbol, require_today=True)
    assert realtime_ff._PAGE_BREAKER.is_open

    with pytest.raises(realtime_ff.FundFlowPageRefused, match="熔断中"):
        await realtime_ff._load_page_shared("601138", require_today=True)

    assert attempts == ["600026", "600938"]      # 熔断期内一次页面都不加载
