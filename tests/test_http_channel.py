"""Outbound HTTP channel mode resolution and installation tests."""

import builtins
import importlib
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
