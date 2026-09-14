"""Outbound HTTP channel mode resolution and installation tests."""

import builtins
import importlib
import logging
import types

import pytest
import requests as std_requests

from finmcp.config import HttpModeError, resolve_http_mode

channel = importlib.import_module("finmcp.datasource.http_channel")


@pytest.fixture(autouse=True)
def restore_requests(monkeypatch):
    """Every test must leave the requests module exactly as it found it.

    进来之前先摘掉别人留在 ``requests`` 上的代理补丁标记。这一整个文件测的是
    "channel 自己怎么装"，前提是 ``requests`` 干净；标记还在的话
    ``install_http_channel("impersonate")`` 会按设计降级成 direct，21 个用例
    集体判 ``requests_already_patched``——那是产品行为对、测试前提被破坏。

    真实来源：``tests_research/test_etf.py`` 顶层的
    ``akshare_proxy_patch.install_patch(...)`` 在 pytest **收集阶段**就跑了。
    ``testpaths`` 已经把那个目录挡在默认收集之外，但这里仍然自己兜一层——
    顺序相关的测试早晚会被下一个人用别的方式再触发一次。

    摘除必须走 ``monkeypatch`` 而不是手写 delattr/setattr：本 fixture 的清理跑在
    ``monkeypatch.undo()`` **之前**，手写还原会把 degrade 那条用例自己插的标记
    提前删掉，undo 再去 delattr 就 AttributeError。挂到同一个 monkeypatch 栈上，
    两次改动按相反顺序退栈，先退用例的、再退这里的，才是对的。
    """
    monkeypatch.delattr(std_requests, channel._PROXY_PATCH_MARKER, raising=False)
    # 进来时也要卸一次，不只是出去时。
    #
    # ``install_http_channel`` 在 ``cn_stock_source`` 的**导入期**就跑过一次了（那是
    # 它必须早于 ``import efinance`` 的代价），所以进程里 ``_installed_mode`` 一开始
    # 就是 conftest 钉的 direct。本文件里第一个调 install 的用例会撞上"already
    # installed"直接拿到 direct，断言自己装的那个模式就会失败——而它究竟是哪个用例
    # 取决于执行顺序，表现为"单跑这条红、整包跑可能绿"。先卸干净，每个用例都从
    # 没有通道的状态开始。
    channel.uninstall_http_channel()
    before = (
        std_requests.Session,
        std_requests.get,
        std_requests.post,
        std_requests.request,
    )
    yield
    channel.uninstall_http_channel()
    assert (
        std_requests.Session,
        std_requests.get,
        std_requests.post,
        std_requests.request,
    ) == before


# --- mode resolution -------------------------------------------------------


@pytest.fixture
def unset_mode_env(monkeypatch):
    """Drop the pinned test mode so the shipped default is what gets exercised."""
    monkeypatch.delenv("HTTP_CHANNEL", raising=False)


@pytest.mark.parametrize(
    "requested,proxy_enabled,gateway,expected",
    [
        # requested=None means "read the environment", which the fixture unsets,
        # so these rows assert the default that a fresh deployment would get.
        (None, True, "10.0.0.1", "proxy"),
        (None, False, "10.0.0.1", "impersonate"),
        (None, True, None, "impersonate"),
        (None, False, None, "impersonate"),
        ("auto", True, "10.0.0.1", "proxy"),
        ("impersonate", True, "10.0.0.1", "impersonate"),
        ("direct", True, "10.0.0.1", "direct"),
        ("proxy", False, "10.0.0.1", "proxy"),
        ("PROXY", False, "10.0.0.1", "proxy"),
        # off is kept as an alias because operators reach for it to mean "plain".
        ("off", True, "10.0.0.1", "direct"),
        ("  OFF  ", True, "10.0.0.1", "direct"),
    ],
)
def test_mode_resolution_matrix(
    unset_mode_env, requested, proxy_enabled, gateway, expected
):
    mode, _ = resolve_http_mode(requested, proxy_enabled, gateway)
    assert mode == expected


def test_environment_overrides_the_default(monkeypatch):
    monkeypatch.setenv("HTTP_CHANNEL", "direct")
    assert resolve_http_mode(None, True, "10.0.0.1") == ("direct", "requested")


def test_auto_reason_explains_the_choice(unset_mode_env):
    assert resolve_http_mode(None, True, "10.0.0.1")[1] == "auto:proxy_configured"
    assert resolve_http_mode(None, False, "10.0.0.1")[1] == "auto:proxy_disabled"
    assert resolve_http_mode(None, True, None)[1] == "auto:proxy_gateway_missing"


def test_unknown_mode_degrades_to_auto_without_raising():
    mode, reason = resolve_http_mode("turbo", False, None)
    assert mode == "auto"
    assert reason == "invalid_value:turbo"


def test_explicit_proxy_without_gateway_fails_fast():
    with pytest.raises(HttpModeError):
        resolve_http_mode("proxy", True, None)


def test_auto_never_raises_on_partial_proxy_config():
    """A half-configured gateway must not stop the service from starting."""
    assert resolve_http_mode("auto", True, "")[0] == "impersonate"


def test_proxy_enabled_auto_keeps_local_channel_for_request_fallback():
    assert resolve_http_mode("auto", "auto", "gateway") == (
        "impersonate", "auto:request_fallback"
    )


@pytest.mark.parametrize("value", ["0", "false", "off", "disabled", ""])
def test_proxy_enabled_string_false_values_keep_local_channel(value):
    assert resolve_http_mode("auto", value, "gateway")[0] == "impersonate"


# --- installation ----------------------------------------------------------


def test_direct_mode_leaves_requests_untouched():
    before = std_requests.Session
    assert channel.install_http_channel("direct") == "direct"
    assert std_requests.Session is before
    assert channel.installed_mode() == "direct"


def test_impersonate_mode_replaces_requests_entry_points():
    assert channel.install_http_channel("impersonate") == "impersonate"
    assert std_requests.Session is not getattr(std_requests, "_qtf_original_session")
    assert issubclass(std_requests.Session, getattr(std_requests, "_qtf_original_session"))
    assert channel.installed_mode() == "impersonate"


def test_impersonate_mode_is_installed_once():
    assert channel.install_http_channel("impersonate") == "impersonate"
    installed = std_requests.Session
    assert channel.install_http_channel("impersonate") == "impersonate"
    assert std_requests.Session is installed


def test_impersonate_degrades_to_direct_without_curl_cffi(monkeypatch):
    """A missing optional wheel must not stop the service from starting."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError("curl_cffi missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    before = std_requests.Session

    assert channel._install_impersonate() is False
    assert channel.install_http_channel("impersonate") == "direct"
    assert std_requests.Session is before


def test_impersonate_refuses_to_stack_on_the_proxy_patch(monkeypatch):
    """Two channels rewriting requests would silently disable one of them."""
    monkeypatch.setattr(std_requests, "_OriginalSession", std_requests.Session, raising=False)
    before = std_requests.Session

    assert channel.install_http_channel("impersonate") == "direct"
    assert std_requests.Session is before


# --- request routing -------------------------------------------------------


def _install_impersonate_with_fake_cffi(monkeypatch, responses):
    """Install the impersonate channel with a scripted curl_cffi session."""
    calls = []

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    class FakeSession:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome)

    monkeypatch.setattr(channel, "_cffi_session", lambda impersonate: FakeSession())
    monkeypatch.setattr(channel.time, "sleep", lambda _seconds: None)
    assert channel.install_http_channel("impersonate") == "impersonate"
    return calls


def test_passthrough_hosts_never_reach_curl_cffi(monkeypatch):
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [])
    seen = []

    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(
        original,
        "request",
        lambda self, method, url, **kwargs: seen.append(url) or "passthrough",
    )

    session = std_requests.Session()
    # Tonghuashun plus the two Eastmoney hosts public_events uses: all untouched.
    for url in (
        "https://d.10jqka.com.cn/v6/line/x/x.js",
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "https://push2ex.eastmoney.com/getTopicZTPool",
    ):
        assert session.request("GET", url) == "passthrough"

    assert calls == []
    assert len(seen) == 3


def test_impersonated_host_uses_curl_cffi(monkeypatch):
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])

    response = std_requests.Session().request(
        "GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][2]["timeout"] == channel.IMPERSONATE_TIMEOUT_SECONDS


def test_page_assets_on_impersonated_hosts_pass_through(monkeypatch):
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [])
    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(
        original, "request", lambda self, method, url, **kwargs: "passthrough"
    )

    assert (
        std_requests.Session().request("GET", "https://push2.eastmoney.com/app.js")
        == "passthrough"
    )
    assert calls == []


def test_retries_then_replays_through_plain_requests(monkeypatch):
    """Exhausted retries must surface requests' own behaviour, not curl_cffi's."""
    boom = RuntimeError("connection reset")
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [boom, 500, boom])
    original = getattr(std_requests, "_qtf_original_session")
    replayed = []
    monkeypatch.setattr(
        original,
        "request",
        lambda self, method, url, **kwargs: replayed.append(kwargs) or "plain",
    )

    result = std_requests.Session().request(
        "GET", "https://push2.eastmoney.com/api/qt/stock/get", params={"secid": "1.600000"}
    )

    assert result == "plain"
    assert len(calls) == channel.IMPERSONATE_RETRY
    # The replay keeps the caller's kwargs, without curl_cffi-only additions.
    assert replayed == [{"params": {"secid": "1.600000"}}]


def test_plain_eastmoney_replay_gets_a_bounded_default_timeout(monkeypatch):
    seen = []

    class Original:
        @staticmethod
        def request(session, method, url, **kwargs):
            seen.append(kwargs)
            return types.SimpleNamespace(status_code=200)

    monkeypatch.setattr(channel.eastmoney_auth, "needs_auth", lambda url: True)
    channel._plain_with_auth_outcome(
        Original, object(), "GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", {}, track_auth=False
    )
    assert seen == [{"timeout": channel.EASTMONEY_FALLBACK_TIMEOUT_SECONDS}]


def test_explicit_eastmoney_timeout_is_preserved(monkeypatch):
    seen = []

    class Original:
        @staticmethod
        def request(session, method, url, **kwargs):
            seen.append(kwargs)
            return types.SimpleNamespace(status_code=200)

    monkeypatch.setattr(channel.eastmoney_auth, "needs_auth", lambda url: True)
    channel._plain_with_auth_outcome(
        Original, object(), "GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", {"timeout": 2.5}, track_auth=False
    )
    assert seen == [{"timeout": 2.5}]


def test_failed_impersonation_replay_is_bounded(monkeypatch):
    _install_impersonate_with_fake_cffi(
        monkeypatch, [RuntimeError("blocked")] * channel.IMPERSONATE_RETRY
    )
    original = getattr(std_requests, "_qtf_original_session")
    replayed = []
    monkeypatch.setattr(channel.eastmoney_auth, "needs_auth", lambda url: True)
    monkeypatch.setattr(channel.eastmoney_auth, "cookie_header", lambda url: "")
    monkeypatch.setattr(
        original,
        "request",
        lambda self, method, url, **kwargs: replayed.append(kwargs)
        or types.SimpleNamespace(status_code=200),
    )

    response = std_requests.Session().request(
        "GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    )

    assert response.status_code == 200
    assert replayed[0]["timeout"] == channel.EASTMONEY_FALLBACK_TIMEOUT_SECONDS


def test_auto_proxy_request_gets_the_same_timeout(monkeypatch):
    channel._auto_proxy = True
    channel._auto_proxy_gateway = "gateway"
    channel._auto_proxy_token = "token"
    channel._auto_proxy_states.clear()
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    channel._auto_proxy_state("push2his.eastmoney.com")["active"] = True
    fake_patch = types.SimpleNamespace(
        get_auth_config_with_cache=lambda *args: {"proxy": "http://proxy", "cookie": "nid18=value"}
    )
    monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
    seen = []

    class Original:
        @staticmethod
        def request(session, method, request_url, **kwargs):
            seen.append(kwargs)
            return types.SimpleNamespace(status_code=200)

    response = channel._auto_proxy_request(Original, object(), "GET", url, {})
    assert response.status_code == 200
    assert seen[0]["timeout"] == channel.EASTMONEY_FALLBACK_TIMEOUT_SECONDS


def test_auto_uses_gateway_only_after_local_request_fails(monkeypatch):
    calls = _install_impersonate_with_fake_cffi(
        monkeypatch, [RuntimeError("blocked")] * channel.IMPERSONATE_RETRY
    )
    original = getattr(std_requests, "_qtf_original_session")
    local_calls = []
    proxy_calls = []

    monkeypatch.setattr(
        original,
        "request",
        lambda self, method, url, **kwargs: local_calls.append(kwargs)
        or (_ for _ in ()).throw(ConnectionError("blocked")),
    )
    monkeypatch.setattr(channel, "_auto_proxy", True)
    monkeypatch.setattr(channel, "_auto_proxy_gateway", "gateway")
    monkeypatch.setattr(channel, "_auto_proxy_token", "token")
    monkeypatch.setattr(
        channel,
        "_auto_proxy_request",
        lambda *args: proxy_calls.append(args) or types.SimpleNamespace(status_code=200),
    )

    response = std_requests.Session().request(
        "GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    )

    assert response.status_code == 200
    assert len(local_calls) == 1
    assert len(proxy_calls) == 1


def test_auto_does_not_use_gateway_when_local_request_succeeds(monkeypatch):
    calls = _install_impersonate_with_fake_cffi(
        monkeypatch, [RuntimeError("blocked")] * channel.IMPERSONATE_RETRY
    )
    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(original, "request", lambda *args, **kwargs: "local")
    proxy_calls = []
    monkeypatch.setattr(channel, "_auto_proxy", True)
    monkeypatch.setattr(
        channel, "_auto_proxy_request", lambda *args: proxy_calls.append(args)
    )

    assert std_requests.Session().request(
        "GET", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    ) == "local"
    assert proxy_calls == []


def test_auto_proxy_is_bounded_by_failure_threshold_and_cooldown(monkeypatch):
    channel._auto_proxy = True
    channel._auto_proxy_gateway = "gateway"
    channel._auto_proxy_token = "token"
    channel._auto_proxy_states.clear()
    auth_calls = []

    fake_patch = types.SimpleNamespace(
        get_auth_config_with_cache=lambda *args: auth_calls.append(args)
        or {"proxy": "http://proxy", "cookie": "nid18=value"}
    )
    monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
    original = getattr(std_requests, "_qtf_original_session", std_requests.Session)
    monkeypatch.setattr(original, "request", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("blocked")))

    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    assert channel._auto_proxy_request(original, object(), "GET", url, {}) is None
    assert auth_calls == []
    for _ in range(channel.AUTO_PROXY_AFTER_FAILURES):
        channel._record_auto_proxy_local_failure(url)
    state = channel._auto_proxy_states["push2his.eastmoney.com"]
    assert state["active"] is True
    assert channel._auto_proxy_request(original, object(), "GET", url, {}) is None
    assert len(auth_calls) == 1
    assert state["cooldown_until"] > channel.time.monotonic()
    assert channel._auto_proxy_request(original, object(), "GET", url, {}) is None
    assert len(auth_calls) == 1


def test_auto_proxy_state_is_per_host(monkeypatch):
    """per-host 状态机的核心：一个 host 的失败/恢复不影响另一个。

    起因是全局版本的实测：push2delay 的本地成功把 push2his 刚攒出来的
    网关回退状态清掉，fflow 的失败计数永远攒不到阈值，38 次主源失败
    一次都没走到网关。
    """
    channel._auto_proxy = True
    channel._auto_proxy_states.clear()
    url_his = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    url_p2 = "https://push2.eastmoney.com/api/qt/stock/get"
    for _ in range(channel.AUTO_PROXY_AFTER_FAILURES - 1):
        channel._record_auto_proxy_local_failure(url_his)
    channel._record_auto_proxy_local_failure(url_p2)  # 另一个 host 的失败

    state_his = channel._auto_proxy_states["push2his.eastmoney.com"]
    state_p2 = channel._auto_proxy_states["push2.eastmoney.com"]
    assert state_his["failures"] == channel.AUTO_PROXY_AFTER_FAILURES - 1
    assert state_p2["failures"] == 1

    # push2 的本地成功只清它自己，push2his 攒的计数还在
    channel._record_auto_proxy_local_success(url_p2)
    assert state_p2["active"] is False and state_p2["failures"] == 0
    assert state_his["failures"] == channel.AUTO_PROXY_AFTER_FAILURES - 1

    # push2his 攒满阈值激活；push2 不会被连带激活
    channel._record_auto_proxy_local_failure(url_his)
    assert state_his["active"] is True
    assert state_p2["active"] is False


def test_broken_cffi_session_is_not_reused(monkeypatch):
    channel._thread_local.cffi_session = object()
    _install_impersonate_with_fake_cffi(monkeypatch, [RuntimeError("reset"), 200])

    response = std_requests.Session().request(
        "GET", "https://fund.eastmoney.com/api/x"
    )

    assert response.status_code == 200


def test_module_level_helpers_are_rewired(monkeypatch):
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200, 200, 200])

    assert std_requests.get("https://push2.eastmoney.com/a").status_code == 200
    assert std_requests.post("https://push2.eastmoney.com/b").status_code == 200
    assert std_requests.request("GET", "https://push2.eastmoney.com/c").status_code == 200
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]


def test_session_stays_constructible_for_public_events(monkeypatch):
    """public_events clones AkShare functions around requests.Session().get."""
    _install_impersonate_with_fake_cffi(monkeypatch, [200])
    namespace = types.SimpleNamespace(get=std_requests.Session().get)

    assert namespace.get("https://push2.eastmoney.com/api").status_code == 200


# --- startup reporting -----------------------------------------------------


def test_startup_line_reports_the_effective_channel():
    channel.install_http_channel("direct")

    summary = channel.describe_installed_channel()

    assert "mode=direct" in summary
    assert "reason=requested" in summary
    assert f"hooked_hosts={len(channel.IMPERSONATED_HOSTS)}" in summary


def test_startup_line_includes_impersonate_parameters(monkeypatch):
    _install_impersonate_with_fake_cffi(monkeypatch, [])

    summary = channel.describe_installed_channel()

    assert "mode=impersonate" in summary
    assert f"profile={channel.IMPERSONATE_BROWSER}" in summary
    assert f"retry={channel.IMPERSONATE_RETRY}" in summary
    assert f"timeout={channel.IMPERSONATE_TIMEOUT_SECONDS}s" in summary


def test_startup_line_reports_the_degraded_channel(monkeypatch):
    """An operator must not read mode=impersonate when it silently degraded."""
    monkeypatch.setattr(channel, "_install_impersonate", lambda *a, **k: False)

    assert channel.install_http_channel("impersonate") == "direct"

    summary = channel.describe_installed_channel()
    assert "mode=direct" in summary
    assert "reason=curl_cffi_unavailable" in summary
    assert "profile=" not in summary


def test_startup_line_reports_auto_resolution(monkeypatch):
    monkeypatch.delenv("HTTP_CHANNEL", raising=False)
    _install_impersonate_with_fake_cffi(monkeypatch, [])
    channel.uninstall_http_channel()
    monkeypatch.setattr(channel, "_install_impersonate", lambda *a, **k: True)

    channel.install_http_channel(None, proxy_enabled=False, proxy_gateway=None)

    assert "reason=auto:proxy_disabled" in channel.describe_installed_channel()


def test_startup_line_before_any_install():
    channel.uninstall_http_channel()
    assert channel.describe_installed_channel() == "mode=none reason=not_installed"


# --- proxy inheritance and cooldown ----------------------------------------


def test_impersonated_request_inherits_the_environment_proxy(monkeypatch):
    """curl_cffi has no trust_env, so the channel must pass proxies explicitly."""
    # 先把环境里所有代理变量清掉，再设自己的那个。
    #
    # ``getproxies_environment()`` 是把变量名统一成小写之后遍历 ``os.environ`` 的，
    # 所以只设 ``HTTPS_PROXY`` 盖不住开发机上常见的小写 ``https_proxy``——那边的值
    # 多一个结尾斜杠，这里就断言失败。测试不该因为跑它的人配了系统代理而变红。
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                 "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])

    std_requests.Session().request("GET", "https://push2.eastmoney.com/api/qt/stock/get")

    assert calls[0][2]["proxies"]["https"] == "http://127.0.0.1:7897"


def test_session_proxies_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])

    session = std_requests.Session()
    session.proxies = {"https": "http://10.0.0.9:3128"}
    session.request("GET", "https://push2.eastmoney.com/api/qt/stock/get")

    assert calls[0][2]["proxies"]["https"] == "http://10.0.0.9:3128"


def test_no_proxies_key_when_none_configured(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(channel.std_requests.utils, "get_environ_proxies", lambda *a, **k: {})
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])

    std_requests.Session().request("GET", "https://push2.eastmoney.com/api/qt/stock/get")

    assert "proxies" not in calls[0][2]


def test_repeated_failures_suspend_impersonation(monkeypatch):
    """A doomed environment must stop paying the retry budget on every call."""
    threshold = channel.IMPERSONATE_SUSPEND_AFTER_FAILURES
    boom = RuntimeError("no route")
    responses = [boom] * (channel.IMPERSONATE_RETRY * (threshold + 1))
    calls = _install_impersonate_with_fake_cffi(monkeypatch, responses)
    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(
        original, "request", lambda self, method, url, **kwargs: "plain"
    )

    session = std_requests.Session()
    for _ in range(threshold):
        assert session.request("GET", "https://push2.eastmoney.com/a") == "plain"

    attempts_before = len(calls)
    # Cooldown is now active: no further curl_cffi attempts, straight to plain.
    assert session.request("GET", "https://push2.eastmoney.com/a") == "plain"
    assert len(calls) == attempts_before
    assert channel._impersonation_suspended() is True


def test_failures_in_flight_at_suspension_do_not_re_arm_it(caplog):
    """熔断已经打开时，晚到的失败不该再计一次。

    这些请求是熔断前就发出去的，只是现在才失败返回。再计数会把冷却终点往后推，
    并重复打一条读起来像"又失败了一整轮"的 WARNING——生产日志里就出现过两条
    相隔 31 毫秒的 suspending。
    """
    import logging

    channel._breaker["failures"] = 0
    channel._breaker["suspended_until"] = 0.0
    caplog.set_level(logging.WARNING, logger="finmcp")

    for _ in range(channel.IMPERSONATE_SUSPEND_AFTER_FAILURES):
        channel._record_impersonation(success=False)
    suspended_until = channel._breaker["suspended_until"]
    assert caplog.text.count("suspending impersonation") == 1

    for _ in range(channel.IMPERSONATE_SUSPEND_AFTER_FAILURES * 2):
        channel._record_impersonation(success=False)

    assert channel._breaker["suspended_until"] == suspended_until
    assert caplog.text.count("suspending impersonation") == 1


def test_cooldown_expires(monkeypatch):
    threshold = channel.IMPERSONATE_SUSPEND_AFTER_FAILURES
    boom = RuntimeError("no route")
    _install_impersonate_with_fake_cffi(
        monkeypatch, [boom] * (channel.IMPERSONATE_RETRY * threshold)
    )
    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(
        original, "request", lambda self, method, url, **kwargs: "plain"
    )

    session = std_requests.Session()
    for _ in range(threshold):
        session.request("GET", "https://push2.eastmoney.com/a")
    assert channel._impersonation_suspended() is True

    monkeypatch.setattr(
        channel.time, "monotonic", lambda: channel._breaker["suspended_until"] + 1
    )
    assert channel._impersonation_suspended() is False


def test_a_success_resets_the_failure_streak(monkeypatch):
    threshold = channel.IMPERSONATE_SUSPEND_AFTER_FAILURES
    boom = RuntimeError("flaky")
    responses = []
    for _ in range(threshold - 1):
        responses += [boom] * channel.IMPERSONATE_RETRY
    responses.append(200)
    responses += [boom] * channel.IMPERSONATE_RETRY
    _install_impersonate_with_fake_cffi(monkeypatch, responses)
    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(
        original, "request", lambda self, method, url, **kwargs: "plain"
    )

    session = std_requests.Session()
    for _ in range(threshold - 1):
        session.request("GET", "https://push2.eastmoney.com/a")
    assert session.request("GET", "https://push2.eastmoney.com/a").status_code == 200

    session.request("GET", "https://push2.eastmoney.com/a")
    assert channel._impersonation_suspended() is False


# --- host matching ---------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://push2his.eastmoney.com/api/qt/stock/kline/get", True),
        ("https://push2.eastmoney.com/api/qt/stock/get?secid=1.600000", True),
        ("https://fund.eastmoney.com/api/x", True),
        ("https://emweb.securities.eastmoney.com/api/x", True),
        # Documented pass-through hosts must stay pass-through even when a query
        # parameter mentions a hooked host.
        ("https://datacenter-web.eastmoney.com/api/data/get?cb=push2.eastmoney.com", False),
        ("https://push2ex.eastmoney.com/getTopicZTPool", False),
        ("https://d.10jqka.com.cn/x?ref=https://push2.eastmoney.com/a", False),
        # Page assets gain nothing from impersonation.
        ("https://push2.eastmoney.com/app.js", False),
        ("https://push2.eastmoney.com/index.html?x=1", False),
        # Hostname comparison is case insensitive and port tolerant.
        ("https://PUSH2.EastMoney.com/api/x", True),
        ("", False),
    ],
)
def test_only_hooked_hostnames_are_impersonated(url, expected):
    assert channel._is_impersonated(url) is expected


def test_verify_follows_requests_ca_bundle(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp.pem")
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])

    std_requests.Session().request("GET", "https://push2.eastmoney.com/api/x")

    assert calls[0][2]["verify"] == "/etc/ssl/corp.pem"


def test_session_verify_false_is_honoured(monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])

    session = std_requests.Session()
    session.verify = False
    session.request("GET", "https://push2.eastmoney.com/api/x")

    assert calls[0][2]["verify"] is False


def test_impersonation_failure_reason_is_logged(monkeypatch, caplog):
    """The plain replay's error alone hid a proxy misconfiguration in production."""
    import logging

    _install_impersonate_with_fake_cffi(
        monkeypatch, [RuntimeError("no route")] * channel.IMPERSONATE_RETRY
    )
    original = getattr(std_requests, "_qtf_original_session")
    monkeypatch.setattr(original, "request", lambda self, m, u, **k: "plain")
    caplog.set_level(logging.DEBUG, logger="finmcp")

    std_requests.Session().request("GET", "https://push2his.eastmoney.com/api/x")

    assert "Impersonation failed" in caplog.text
    assert "host=push2his.eastmoney.com" in caplog.text
    assert "RuntimeError: no route" in caplog.text


# --- 通道降级要能被源查询到 -------------------------------------------------


class TestImpersonatedHostsDegraded:
    def test_false_when_the_channel_is_healthy(self, monkeypatch):
        monkeypatch.setattr(channel, "_installed_mode", "impersonate")
        channel._breaker.update(failures=0, suspended_until=0.0)
        assert not channel.impersonated_hosts_degraded()

    def test_true_while_impersonation_is_suspended(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(channel, "_installed_mode", "impersonate")
        channel._breaker.update(failures=0, suspended_until=_time.monotonic() + 60)
        try:
            assert channel.impersonated_hosts_degraded()
        finally:
            channel._breaker.update(failures=0, suspended_until=0.0)

    def test_false_in_other_modes(self, monkeypatch):
        """只有 impersonate 模式装了伪装通道，别的模式下这个冷却没有意义。"""
        import time as _time
        channel._breaker.update(failures=0, suspended_until=_time.monotonic() + 60)
        try:
            for mode in ("proxy", "direct", None):
                monkeypatch.setattr(channel, "_installed_mode", mode)
                assert not channel.impersonated_hosts_degraded(), mode
        finally:
            channel._breaker.update(failures=0, suspended_until=0.0)

    def test_it_clears_once_the_cooldown_expires(self, monkeypatch):
        import time as _time
        monkeypatch.setattr(channel, "_installed_mode", "impersonate")
        channel._breaker.update(failures=0, suspended_until=_time.monotonic() + 0.01)
        _time.sleep(0.02)
        assert not channel.impersonated_hosts_degraded()


# --- 凭据层对要求凭据的主机强制覆盖自洽头 -------------------------------------
#
# 起因：AkShare 显式硬编码 Chrome/81 的 UA，而 Python TLS 配 Chrome/81 头恰好是
# 上游发"扰动副本"的那一档。旧实现只补缺失字段、尊重调用方显式 UA，等于对最需要
# 防线的 akshare 路径一个字节都不生效——注释和实现互相矛盾。覆盖只对 needs_auth
# 的主机生效，普通主机的调用方头原样不动。


def test_auth_host_overrides_the_callers_ua(monkeypatch):
    monkeypatch.setattr(channel.eastmoney_auth, "needs_auth", lambda url: True)
    kwargs = channel._with_auth_headers(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        {"headers": {"User-Agent": "Chrome/81.0.4044.138"}},
    )
    sent = kwargs["headers"]
    assert sent["User-Agent"] == channel._AUTH_UA
    assert "Chrome/81" not in sent["User-Agent"]
    # 客户端提示头一并补齐，整套自洽
    assert sent["sec-ch-ua"] == channel._AUTH_IDENTITY_HEADERS["sec-ch-ua"]
    assert sent["Referer"] == "https://data.eastmoney.com/"


def test_non_auth_hosts_keep_the_callers_headers():
    kwargs = channel._with_auth_headers(
        "https://qt.gtimg.cn/q=sh600489",
        {"headers": {"User-Agent": "something-custom"}},
    )
    assert kwargs == {"headers": {"User-Agent": "something-custom"}}


def test_override_is_case_insensitive_and_single():
    # 大小写不同的同名头要被换掉而不是并存，requests 会把两个都发出去
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(channel.eastmoney_auth, "needs_auth", lambda url: True)
        kwargs = channel._with_auth_headers(
            "https://push2.eastmoney.com/api/qt/stock/get",
            {"headers": {"user-agent": "Chrome/81"}},
        )
        sent = kwargs["headers"]
        assert sent["User-Agent"] == channel._AUTH_UA
        assert sum(1 for k in sent if k.lower() == "user-agent") == 1
    finally:
        monkeypatch.undo()


def test_auth_host_preserves_decorative_headers(monkeypatch):
    # 分档只看身份头：调用方显式给的 Referer/Accept 有自己的语义，原样保留；
    # 身份头仍然强制覆盖，两件事互不越界
    monkeypatch.setattr(channel.eastmoney_auth, "needs_auth", lambda url: True)
    kwargs = channel._with_auth_headers(
        "https://push2.eastmoney.com/api/qt/stock/get",
        {"headers": {"User-Agent": "Chrome/81",
                     "Referer": "https://x/", "Accept": "application/json"}},
    )
    sent = kwargs["headers"]
    assert sent["User-Agent"] == channel._AUTH_UA
    assert sent["Referer"] == "https://x/"
    assert sent["Accept"] == "application/json"


class TestAutoProxyRecoveryAndRotation:
    """恢复迟滞与坏出口轮换。

    2026-09-13 上游环境实测：网关能救 kline（10 次恢复），但坏出口会让请求
    白付一次认证；而旧版"一次本地成功立即退出回退"会让状态来回抖。
    """

    URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

    def _activate(self):
        channel._auto_proxy = True
        channel._auto_proxy_states.clear()
        for _ in range(channel.AUTO_PROXY_AFTER_FAILURES):
            channel._record_auto_proxy_local_failure(self.URL)
        return channel._auto_proxy_states["push2his.eastmoney.com"]

    def test_recovery_needs_spaced_consecutive_successes(self):
        state = self._activate()
        channel._record_auto_proxy_local_success(self.URL)
        assert state["active"] is True and state["local_successes"] == 1
        channel._record_auto_proxy_local_success(self.URL)  # 间隔内：不累计
        assert state["local_successes"] == 1
        state["last_probe_at"] -= channel.AUTO_PROXY_RECOVERY_INTERVAL_SECONDS + 1
        channel._record_auto_proxy_local_success(self.URL)
        assert state["local_successes"] == 2
        state["last_probe_at"] -= channel.AUTO_PROXY_RECOVERY_INTERVAL_SECONDS + 1
        channel._record_auto_proxy_local_success(self.URL)
        assert state["active"] is False and state["local_successes"] == 0

    def test_a_local_failure_during_active_resets_progress(self):
        state = self._activate()
        channel._record_auto_proxy_local_success(self.URL)
        assert state["local_successes"] == 1
        channel._record_auto_proxy_local_failure(self.URL)
        assert state["local_successes"] == 0 and state["active"] is True

    def test_a_gateway_data_failure_rotates_the_exit(self, monkeypatch):
        """出口拿到了但请求没成 → 作废认证换新出口 + 短冷却，不是 300 秒。"""
        channel._auto_proxy = True
        channel._auto_proxy_gateway = "gateway"
        channel._auto_proxy_token = "token"
        channel._auto_proxy_states.clear()
        channel._auto_proxy_state("push2his.eastmoney.com")["active"] = True
        channel._auto_proxy_auth = {"proxy": "http://stale", "cookie": "nid18=stale"}
        channel._auto_proxy_auth_at = channel.time.monotonic()
        auth_calls = []
        fake_patch = types.SimpleNamespace(
            get_auth_config_with_cache=lambda *args: auth_calls.append(args)
            or {"proxy": "http://fresh", "cookie": "nid18=fresh"}
        )
        monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
        original = getattr(std_requests, "_qtf_original_session", std_requests.Session)
        monkeypatch.setattr(
            original, "request",
            lambda *args, **kwargs: types.SimpleNamespace(status_code=503),
        )
        assert channel._auto_proxy_request(
            original, object(), "GET", self.URL, {}
        ) is None
        assert channel._auto_proxy_auth is None  # 坏出口已作废
        state = channel._auto_proxy_states["push2his.eastmoney.com"]
        assert state["cooldown_until"] <= channel.time.monotonic() + channel.AUTO_PROXY_DATA_COOLDOWN_SECONDS + 1

    def test_a_gateway_success_keeps_the_cached_auth(self, monkeypatch):
        channel._auto_proxy = True
        channel._auto_proxy_gateway = "gateway"
        channel._auto_proxy_token = "token"
        channel._auto_proxy_states.clear()
        channel._auto_proxy_state("push2his.eastmoney.com")["active"] = True
        good_auth = {"proxy": "http://good", "cookie": "nid18=good"}
        channel._auto_proxy_auth = good_auth
        channel._auto_proxy_auth_at = channel.time.monotonic()
        fake_patch = types.SimpleNamespace(
            get_auth_config_with_cache=lambda *args: pytest.fail("好出口不该重新认证")
        )
        monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
        original = getattr(std_requests, "_qtf_original_session", std_requests.Session)
        monkeypatch.setattr(
            original, "request",
            lambda *args, **kwargs: types.SimpleNamespace(status_code=200),
        )
        response = channel._auto_proxy_request(original, object(), "GET", self.URL, {})
        assert response.status_code == 200
        assert channel._auto_proxy_auth is good_auth  # 好出口继续复用

    def test_an_attempt_is_logged_with_request_context(self, monkeypatch, caplog):
        channel._auto_proxy = True
        channel._auto_proxy_gateway = "gateway"
        channel._auto_proxy_token = "token"
        channel._auto_proxy_states.clear()
        state = channel._auto_proxy_state("push2his.eastmoney.com")
        state["active"] = True
        fake_patch = types.SimpleNamespace(
            get_auth_config_with_cache=lambda *args: {"proxy": "http://p", "cookie": "c"}
        )
        monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
        original = getattr(std_requests, "_qtf_original_session", std_requests.Session)
        monkeypatch.setattr(
            original, "request",
            lambda *args, **kwargs: types.SimpleNamespace(status_code=200),
        )
        with caplog.at_level(logging.WARNING, logger="finmcp"):
            channel._auto_proxy_request(original, object(), "GET", self.URL, {})
        assert any(
            "auto_proxy_attempt host=push2his.eastmoney.com" in r.message
            for r in caplog.records
        )

    def test_auto_proxy_enabled_does_not_break_a_local_success(self, monkeypatch):
        """回归钉子：per-host 重构曾漏改成功路径的无参调用，_auto_proxy=True 时
        每次伪装成功都 TypeError，被重试循环吞掉后整条链降级。"""
        channel._auto_proxy = True
        channel._auto_proxy_states.clear()
        calls = _install_impersonate_with_fake_cffi(monkeypatch, [200])
        original = getattr(std_requests, "_qtf_original_session")
        monkeypatch.setattr(
            original, "request",
            lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("blocked")),
        )
        response = std_requests.Session().request("GET", self.URL)
        assert response.status_code == 200
        assert len(calls) == 1  # 一次成功，不重试

    def test_recovery_clears_the_failure_count(self):
        """恢复后 failures 必须归零：留着它，恢复后第一次失败就会立即重新激活，
        "三次连续失败"的门槛形同虚设。"""
        state = self._activate()
        for _ in range(channel.AUTO_PROXY_RECOVERY_PROBES):
            channel._record_auto_proxy_local_success(self.URL)
            state["last_probe_at"] -= channel.AUTO_PROXY_RECOVERY_INTERVAL_SECONDS + 1
        assert state["active"] is False
        assert state["failures"] == 0
        channel._record_auto_proxy_local_failure(self.URL)
        assert state["active"] is False  # 一次失败不该立刻重新激活
        assert state["failures"] == 1

    def test_uninstall_clears_the_per_host_states(self):
        self._activate()
        assert channel._auto_proxy_states
        channel.uninstall_http_channel()
        assert channel._auto_proxy_states == {}

    def test_gateway_is_scoped_to_eastmoney_api_hosts(self):
        """同花顺主机的失败不进网关：积分只花在东财上（09-13 实测漏过 2 次）。"""
        channel._auto_proxy = True
        channel._auto_proxy_states.clear()
        channel._record_auto_proxy_local_failure("https://d.10jqka.com.cn/v6/realhead/hs_600519/last.js")
        assert "d.10jqka.com.cn" not in channel._auto_proxy_states
        for _ in range(10):
            channel._record_auto_proxy_local_failure("https://d.10jqka.com.cn/x")
        assert channel._auto_proxy_states == {}

    def test_a_repeated_bad_exit_is_treated_as_auth_unavailable(self, monkeypatch):
        """重新认证仍吐回同一个坏出口（插件缓存在重认证失败时原样返回旧数据），
        不能记成"刚获取的认证"，要走认证不可用的长冷却。"""
        stale = {"proxy": "http://stale-exit", "cookie": "nid18=stale"}
        channel._auto_proxy = True
        channel._auto_proxy_gateway = "gateway"
        channel._auto_proxy_token = "token"
        channel._auto_proxy_states.clear()
        state = channel._auto_proxy_state("push2his.eastmoney.com")
        state["active"] = True
        state["last_failed_proxy"] = "http://stale-exit"
        channel._auto_proxy_auth = stale
        channel._auto_proxy_auth_at = 0.0  # 缓存过期，强制重新认证
        reauth = []
        fake_patch = types.SimpleNamespace(
            get_auth_config_with_cache=lambda *args: reauth.append(args) or stale
        )
        monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
        original = getattr(std_requests, "_qtf_original_session", std_requests.Session)
        monkeypatch.setattr(
            original, "request",
            lambda *args, **kwargs: pytest.fail("同一个坏出口不该再发数据请求"),
        )
        assert channel._auto_proxy_request(
            original, object(), "GET", self.URL, {}
        ) is None
        assert len(reauth) == 1  # 重新认证过，但吐回的还是坏出口
        assert state["cooldown_until"] > channel.time.monotonic() + channel.AUTO_PROXY_DATA_COOLDOWN_SECONDS

    def test_the_failure_log_masks_proxy_credentials(self, monkeypatch, caplog):
        """出口地址带 user:pass@host，异常原文进日志前必须抹掉。"""
        secret = "http://user:secret-pass@exit-gw:8080"
        channel._auto_proxy = True
        channel._auto_proxy_gateway = "gateway"
        channel._auto_proxy_token = "token"
        channel._auto_proxy_states.clear()
        state = channel._auto_proxy_state("push2his.eastmoney.com")
        state["active"] = True
        channel._auto_proxy_auth = {"proxy": secret, "cookie": "c"}
        channel._auto_proxy_auth_at = channel.time.monotonic()
        fake_patch = types.SimpleNamespace(
            get_auth_config_with_cache=lambda *args: {"proxy": secret, "cookie": "c"}
        )
        monkeypatch.setitem(__import__("sys").modules, "akshare_proxy_patch", fake_patch)
        original = getattr(std_requests, "_qtf_original_session", std_requests.Session)
        monkeypatch.setattr(
            original, "request",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ConnectionError(f"proxy connect failed at {secret}")),
        )
        with caplog.at_level(logging.WARNING, logger="finmcp"):
            channel._auto_proxy_request(original, object(), "GET", self.URL, {})
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "secret-pass" not in joined and "<proxy>" in joined
        assert channel._auto_proxy_auth is None  # 本代凭据已作废
