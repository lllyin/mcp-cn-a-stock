import asyncio
import importlib
import stat
from types import SimpleNamespace

import pandas as pd
import pytest

from qtf_mcp.datasource.market_breadth import (
    MARKET_BREADTH_RANGES,
    MarketBreadthBucket,
    MarketBreadthData,
    MarketBreadthUnavailable,
    TonghuashunAuth,
    TonghuashunAuthError,
    TonghuashunCooldownError,
    TonghuashunPlaywrightProvider,
    _build_tonghuashun_auth,
    _tonghuashun_browser_args,
    build_market_breadth_distribution,
    get_market_breadth,
    parse_tonghuashun_market_breadth,
)

app_module = importlib.import_module("qtf_mcp.mcp_app")
market_module = importlib.import_module("qtf_mcp.datasource.market_breadth")


TONGHUASHUN_PAYLOAD = {
    "zdt_data": {
        "zd_time": ["09:30", "15:00"],
        "last_zdt": {"ztzs": 56, "dtzs": 76},
    },
    "zdfb_data": {
        "zdfb": [455, 331, 532, 884, 1433, 1367, 338, 80, 33, 75],
        "znum": 1768,
        "dnum": 3635,
    },
}


def make_data(source: str = "test") -> MarketBreadthData:
    return MarketBreadthData(
        source=source,
        fetched_at="2026-07-30 15:01:00",
        trade_date="2026-07-30",
        market_time="15:00",
        up_count=1768,
        down_count=3635,
        flat_count=125,
        limit_up_count=56,
        limit_down_count=76,
        distribution=tuple(
            MarketBreadthBucket(label=label, count=count)
            for label, count in zip(
                MARKET_BREADTH_RANGES,
                [455, 331, 532, 884, 1433, 1367, 338, 80, 33, 75],
            )
        ),
    )


def test_parse_tonghuashun_market_breadth():
    result = parse_tonghuashun_market_breadth(TONGHUASHUN_PAYLOAD)

    assert result.source == "tonghuashun_web"
    assert result.trade_date is None
    assert result.up_count == 1768
    assert result.down_count == 3635
    assert result.flat_count == 125
    assert result.limit_up_count == 56
    assert result.limit_down_count == 76
    assert result.market_time == "15:00"
    assert [bucket.count for bucket in result.distribution] == TONGHUASHUN_PAYLOAD["zdfb_data"]["zdfb"]


def test_build_tonghuashun_auth_keeps_only_v_cookie_and_user_agent():
    result = _build_tonghuashun_auth(
        [
            {"name": "other", "value": "ignored"},
            {"name": "v", "value": "auth-token"},
        ],
        "Chrome test agent",
    )
    assert result == TonghuashunAuth(
        v_cookie="auth-token",
        user_agent="Chrome test agent",
    )


def test_auth_cache_round_trip_uses_private_permissions(tmp_path):
    cache_path = tmp_path / "runtime" / "tonghuashun-auth.json"
    provider = TonghuashunPlaywrightProvider(auth_cache_path=cache_path)
    auth = TonghuashunAuth("persisted-token", "Chrome persisted agent")

    provider._save_cached_auth_sync(auth)

    assert provider._load_cached_auth_sync() == auth
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    assert "persisted-token" in cache_path.read_text(encoding="utf-8")
    provider.close()


def test_browser_sandbox_is_only_disabled_explicitly(monkeypatch):
    monkeypatch.delenv("CN_STOCK_CHROME_NO_SANDBOX", raising=False)
    assert "--no-sandbox" not in _tonghuashun_browser_args()

    monkeypatch.setenv("CN_STOCK_CHROME_NO_SANDBOX", "1")
    assert "--no-sandbox" in _tonghuashun_browser_args()


def test_parse_tonghuashun_market_breadth_uses_explicit_source_date():
    payload = dict(TONGHUASHUN_PAYLOAD, trade_date="2026-07-30")

    result = parse_tonghuashun_market_breadth(payload)

    assert result.trade_date == "2026-07-30"


@pytest.mark.asyncio
async def test_bootstrap_auth_closes_browser(monkeypatch):
    class FakePage:
        async def goto(self, *args, **kwargs):
            return SimpleNamespace(status=200)

        async def evaluate(self, expression):
            assert expression == "navigator.userAgent"
            return "Chrome test agent"

        async def wait_for_timeout(self, milliseconds):
            raise AssertionError("v Cookie should be available immediately")

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def cookies(self, page_url):
            return [
                {"name": "other", "value": "ignored"},
                {"name": "v", "value": "auth-token"},
            ]

    class FakeBrowser:
        def __init__(self):
            self.closed = False

        async def new_context(self, **kwargs):
            return FakeContext()

        async def close(self):
            self.closed = True

    class FakePlaywrightManager:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    browser = FakeBrowser()
    provider = TonghuashunPlaywrightProvider()

    async def fake_launch(playwright):
        return browser

    monkeypatch.setattr(market_module, "async_playwright", FakePlaywrightManager)
    monkeypatch.setattr(provider, "_launch_browser", fake_launch)

    result = await provider._bootstrap_auth()

    assert result.v_cookie == "auth-token"
    assert browser.closed is True
    provider.close()


@pytest.mark.asyncio
async def test_provider_reuses_cached_auth_for_http_requests(monkeypatch):
    provider = TonghuashunPlaywrightProvider()
    provider._auth = TonghuashunAuth("cached-token", "Chrome test agent")
    request_auth = []

    async def unexpected_bootstrap():
        raise AssertionError("cached auth should avoid Playwright")

    def fake_request(auth):
        request_auth.append(auth)
        return TONGHUASHUN_PAYLOAD

    monkeypatch.setattr(provider, "_bootstrap_auth", unexpected_bootstrap)
    monkeypatch.setattr(provider, "_request_payload_sync", fake_request)

    await provider.fetch()
    await provider.fetch()

    assert request_auth == [provider._auth, provider._auth]
    provider.close()


@pytest.mark.asyncio
async def test_provider_reuses_persisted_auth_after_restart(monkeypatch, tmp_path):
    cache_path = tmp_path / "tonghuashun-auth.json"
    original_provider = TonghuashunPlaywrightProvider(auth_cache_path=cache_path)
    original_provider._save_cached_auth_sync(
        TonghuashunAuth("persisted-token", "Chrome persisted agent"),
    )
    original_provider.close()

    restarted_provider = TonghuashunPlaywrightProvider(auth_cache_path=cache_path)

    async def unexpected_bootstrap():
        raise AssertionError("persisted auth should avoid Playwright")

    monkeypatch.setattr(restarted_provider, "_bootstrap_auth", unexpected_bootstrap)
    monkeypatch.setattr(
        restarted_provider,
        "_request_payload_sync",
        lambda auth: TONGHUASHUN_PAYLOAD,
    )

    result = await restarted_provider.fetch()

    assert result.source == "tonghuashun_web"
    assert restarted_provider._auth == TonghuashunAuth(
        "persisted-token",
        "Chrome persisted agent",
    )
    restarted_provider.close()


@pytest.mark.asyncio
async def test_provider_refreshes_auth_once_after_403(monkeypatch, tmp_path):
    cache_path = tmp_path / "tonghuashun-auth.json"
    provider = TonghuashunPlaywrightProvider(auth_cache_path=cache_path)
    stale_auth = TonghuashunAuth("stale-token", "Chrome stale agent")
    fresh_auth = TonghuashunAuth("fresh-token", "Chrome fresh agent")
    provider._auth = stale_auth
    provider._save_cached_auth_sync(stale_auth)
    bootstrap_count = 0
    request_auth = []

    async def fake_bootstrap():
        nonlocal bootstrap_count
        bootstrap_count += 1
        return fresh_auth

    def fake_request(auth):
        request_auth.append(auth)
        if auth == stale_auth:
            raise TonghuashunAuthError("HTTP 403")
        return TONGHUASHUN_PAYLOAD

    monkeypatch.setattr(provider, "_bootstrap_auth", fake_bootstrap)
    monkeypatch.setattr(provider, "_request_payload_sync", fake_request)

    result = await provider.fetch()

    assert result.up_count == 1768
    assert bootstrap_count == 1
    assert request_auth == [stale_auth, fresh_auth]
    assert provider._load_cached_auth_sync() == fresh_auth
    provider.close()


@pytest.mark.asyncio
async def test_provider_ignores_rejected_cache_when_deletion_fails(monkeypatch, tmp_path):
    cache_path = tmp_path / "tonghuashun-auth.json"
    provider = TonghuashunPlaywrightProvider(auth_cache_path=cache_path)
    stale_auth = TonghuashunAuth("stale-token", "Chrome stale agent")
    fresh_auth = TonghuashunAuth("fresh-token", "Chrome fresh agent")
    provider._auth = stale_auth
    provider._save_cached_auth_sync(stale_auth)
    bootstrap_count = 0
    request_auth = []

    async def fake_bootstrap():
        nonlocal bootstrap_count
        bootstrap_count += 1
        return fresh_auth

    def fake_request(auth):
        request_auth.append(auth)
        if auth == stale_auth:
            raise TonghuashunAuthError("HTTP 403")
        return TONGHUASHUN_PAYLOAD

    def failed_delete(auth):
        raise OSError("cache is read-only")

    monkeypatch.setattr(provider, "_bootstrap_auth", fake_bootstrap)
    monkeypatch.setattr(provider, "_request_payload_sync", fake_request)
    monkeypatch.setattr(provider, "_delete_cached_auth_if_stale_sync", failed_delete)

    result = await provider.fetch()

    assert result.up_count == 1768
    assert bootstrap_count == 1
    assert request_auth == [stale_auth, fresh_auth]
    assert provider._load_cached_auth_sync() == fresh_auth
    provider.close()


@pytest.mark.asyncio
async def test_concurrent_provider_requests_bootstrap_once(monkeypatch, tmp_path):
    provider = TonghuashunPlaywrightProvider(
        auth_cache_path=tmp_path / "tonghuashun-auth.json",
    )
    bootstrap_count = 0

    async def fake_bootstrap():
        nonlocal bootstrap_count
        bootstrap_count += 1
        await asyncio.sleep(0.01)
        return TonghuashunAuth("shared-token", "Chrome test agent")

    monkeypatch.setattr(provider, "_bootstrap_auth", fake_bootstrap)
    monkeypatch.setattr(
        provider,
        "_request_payload_sync",
        lambda auth: TONGHUASHUN_PAYLOAD,
    )

    results = await asyncio.gather(*(provider.fetch() for _ in range(3)))

    assert bootstrap_count == 1
    assert [result.up_count for result in results] == [1768, 1768, 1768]
    provider.close()


@pytest.mark.asyncio
async def test_provider_instances_share_persisted_auth(monkeypatch, tmp_path):
    cache_path = tmp_path / "tonghuashun-auth.json"
    providers = [
        TonghuashunPlaywrightProvider(auth_cache_path=cache_path),
        TonghuashunPlaywrightProvider(auth_cache_path=cache_path),
    ]
    bootstrap_count = 0

    async def fake_bootstrap():
        nonlocal bootstrap_count
        bootstrap_count += 1
        await asyncio.sleep(0.01)
        return TonghuashunAuth("shared-token", "Chrome test agent")

    for provider in providers:
        monkeypatch.setattr(provider, "_bootstrap_auth", fake_bootstrap)
        monkeypatch.setattr(
            provider,
            "_request_payload_sync",
            lambda auth: TONGHUASHUN_PAYLOAD,
        )

    results = await asyncio.gather(*(provider.fetch() for provider in providers))

    assert bootstrap_count == 1
    assert [result.up_count for result in results] == [1768, 1768]
    assert providers[0]._auth == providers[1]._auth
    for provider in providers:
        provider.close()


@pytest.mark.asyncio
async def test_provider_cools_down_after_failure(monkeypatch, tmp_path):
    provider = TonghuashunPlaywrightProvider(
        cooldown_seconds=300,
        auth_cache_path=tmp_path / "tonghuashun-auth.json",
    )
    clock = [1000.0]
    bootstrap_count = 0

    async def failed_bootstrap():
        nonlocal bootstrap_count
        bootstrap_count += 1
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr(market_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(provider, "_bootstrap_auth", failed_bootstrap)

    with pytest.raises(RuntimeError, match="browser unavailable"):
        await provider.fetch()
    with pytest.raises(TonghuashunCooldownError, match="冷却中"):
        await provider.fetch()
    assert bootstrap_count == 1

    clock[0] += 301
    with pytest.raises(RuntimeError, match="browser unavailable"):
        await provider.fetch()
    assert bootstrap_count == 2
    provider.close()


@pytest.mark.asyncio
async def test_provider_replaces_corrupt_auth_cache(monkeypatch, tmp_path):
    cache_path = tmp_path / "tonghuashun-auth.json"
    cache_path.write_text("not-json", encoding="utf-8")
    provider = TonghuashunPlaywrightProvider(auth_cache_path=cache_path)
    bootstrap_count = 0
    fresh_auth = TonghuashunAuth("fresh-token", "Chrome fresh agent")

    async def fake_bootstrap():
        nonlocal bootstrap_count
        bootstrap_count += 1
        return fresh_auth

    monkeypatch.setattr(provider, "_bootstrap_auth", fake_bootstrap)
    monkeypatch.setattr(
        provider,
        "_request_payload_sync",
        lambda auth: TONGHUASHUN_PAYLOAD,
    )

    await provider.fetch()

    assert bootstrap_count == 1
    assert provider._load_cached_auth_sync() == fresh_auth
    provider.close()


def test_build_market_breadth_distribution_boundary_rules():
    percentages = pd.Series(
        [-8.01, -8, -6, -4, -2, -0.01, 0, 2, 2.01, 4, 4.01, 6, 6.01, 8, 8.01]
    )

    result = build_market_breadth_distribution(percentages)

    assert [bucket.count for bucket in result] == [1, 1, 1, 1, 2, 2, 2, 2, 2, 1]


@pytest.mark.asyncio
async def test_get_market_breadth_falls_back_to_next_provider():
    class FailedProvider:
        name = "failed"

        async def fetch(self):
            raise RuntimeError("upstream failed")

    class WorkingProvider:
        name = "working"

        async def fetch(self):
            return make_data("working")

    result = await get_market_breadth([FailedProvider(), WorkingProvider()])

    assert result.source == "working"
    assert result.warnings == ("failed 不可用: upstream failed",)


@pytest.mark.asyncio
async def test_get_market_breadth_raises_when_all_providers_fail():
    class FailedProvider:
        def __init__(self, name):
            self.name = name

        async def fetch(self):
            raise RuntimeError("upstream failed")

    with pytest.raises(MarketBreadthUnavailable, match="first 不可用"):
        await get_market_breadth([FailedProvider("first"), FailedProvider("second")])


@pytest.mark.asyncio
async def test_default_market_breadth_calls_share_result_cache(monkeypatch):
    class CountingProvider:
        name = "counting"

        def __init__(self):
            self.calls = 0

        async def fetch(self):
            self.calls += 1
            return make_data("counting")

    provider = CountingProvider()
    monkeypatch.setattr(market_module, "DEFAULT_MARKET_BREADTH_PROVIDERS", (provider,))
    monkeypatch.setattr(market_module, "_market_breadth_cache", None)

    first, second = await asyncio.gather(get_market_breadth(), get_market_breadth())

    assert first is second
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_injected_providers_bypass_result_cache(monkeypatch):
    class CountingProvider:
        name = "counting"

        def __init__(self):
            self.calls = 0

        async def fetch(self):
            self.calls += 1
            return make_data("counting")

    provider = CountingProvider()
    monkeypatch.setattr(
        market_module,
        "_market_breadth_cache",
        (float("inf"), make_data("cached")),
    )

    first = await get_market_breadth([provider])
    second = await get_market_breadth([provider])

    assert first.source == second.source == "counting"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_market_breadth_tool_returns_structured_response(monkeypatch):
    async def fake_get_market_breadth():
        return make_data("fake")

    monkeypatch.setattr(app_module, "get_market_breadth", fake_get_market_breadth)

    response = await app_module.market_breadth()

    assert response.source == "fake"
    assert response.up_count == 1768
    assert response.limit_down_count == 76
    assert len(response.distribution) == 10
    assert response.distribution[0].range == "跌停 ~ -8%"


def test_market_breadth_tool_is_registered():
    tools = asyncio.run(app_module.mcp_app.list_tools())

    assert "market_breadth" in {tool.name for tool in tools}
