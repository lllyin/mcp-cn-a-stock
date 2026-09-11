"""Outbound HTTP channel for upstream quote hosts.

Some upstream hosts drop connections from plain HTTP clients, which surfaces as
an empty body and a JSON decode error rather than a clean transport failure.
Two channels can work around that, and both do it by rewriting the same
``requests`` module attributes -- so exactly one may be installed. This module
is the single place that decides which one, and the only place that installs.

Install order matters: ``efinance.shared.tickflow_prompt.CustomedSession`` is
declared as ``class CustomedSession(requests.Session)`` at import time and
``efinance.utils.session`` is a module-level instance of it, so the channel must
be installed before ``import efinance`` or that shared session never sees it.
"""

import logging
import os
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

import requests as std_requests

from ..config import (
    IMPERSONATE_BROWSER,
    IMPERSONATE_RETRY,
    IMPERSONATE_SUSPEND_AFTER_FAILURES,
    IMPERSONATE_SUSPEND_SECONDS,
    IMPERSONATE_TIMEOUT_SECONDS,
    HttpModeError,
    resolve_http_mode,
)
from ..observability import log_context
from . import eastmoney_auth

logger = logging.getLogger("finmcp")

# Hosts whose plain-client requests get refused. Everything else -- Tonghuashun,
# and the Eastmoney datacenter/push2ex hosts used by public_events -- passes
# straight through to the unmodified requests implementation.
IMPERSONATED_HOSTS = (
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "fund.eastmoney.com",
    "emweb.securities.eastmoney.com",
)

# akshare-proxy-patch claims this attribute on the requests module. Reading it
# tells us whether the proxy channel is already in place; we keep our own backup
# under a private name so uninstalling either channel stays unambiguous.
_PROXY_PATCH_MARKER = "_OriginalSession"
_OWN_MARKER = "_qtf_original_session"

_state_lock = threading.Lock()
_installed_mode: Optional[str] = None
_installed_reason: Optional[str] = None
_thread_local = threading.local()
# Attributes replaced by the impersonate channel, kept so tests can restore them.
_restore: dict = {}
# Mode-specific fields for the startup line, filled in by the installing branch.
_installed_detail: dict = {}
# Cooldown state for the impersonated path; see _record_impersonation.
_breaker_lock = threading.Lock()
_breaker = {"failures": 0, "suspended_until": 0.0}


def installed_mode() -> Optional[str]:
    """Return the channel mode currently installed in this process."""
    return _installed_mode


def describe_installed_channel() -> str:
    """Return a one-line startup summary of the channel actually in effect.

    Installation happens when the datasource module is imported, which is before
    the startup banner runs, so the entry point reports it from here rather than
    the channel logging the same thing twice.
    """
    if _installed_mode is None:
        return "mode=none reason=not_installed"
    parts = [f"mode={_installed_mode}", f"reason={_installed_reason}"]
    parts += [f"{key}={value}" for key, value in _installed_detail.items()]
    parts.append(f"hooked_hosts={len(IMPERSONATED_HOSTS)}")
    # 网关模式由第三方插件完整接管；其余模式才使用本项目的凭据层。
    parts.append(
        "eastmoney_auth=proxy_managed"
        if _installed_mode == "proxy" else eastmoney_auth.describe()
    )
    return " ".join(parts)


def _is_impersonated(url) -> bool:
    """Whether this URL targets a hooked host.

    Matches on the parsed hostname, not a substring of the whole URL: a query
    parameter that happens to mention a hooked host must not divert a request
    aimed at Tonghuashun or at the Eastmoney datacenter/push2ex hosts.
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    if (parts.hostname or "").lower() not in IMPERSONATED_HOSTS:
        return False
    # Page assets are served normally and gain nothing from impersonation.
    return not parts.path.lower().endswith((".js", ".html"))


#: 要求凭据的那批 API 走 plain requests 时必须自带一套自洽的现代 Chrome 头。
#: 上游对这批端点按请求身份分档服务（2026-09-11 服务器实测，同一出口 IP 同一凭据）：
#: curl 的现代 Chrome TLS + Chrome/152 头拿到真值；Chrome/81 的头被直接拒连；而
#: Python TLS 配 Chrome/81 头**不拒也不给真值**——返回 200 和一份资金流金额扰动过
#: 的副本（收盘价、涨跌幅正确，主力/超大/大/中/小单偏差 30%-50%），比拒绝更难发现。
#: AkShare 硬编码的就是 Chrome/81，所以在凭据层**强制覆盖**成与浏览器一致的自洽头。
#:
#: 为什么连调用方显式给的 UA 也覆盖：这批主机的分档把"头和 TLS 指纹不自洽"当成
#: 可疑身份，而最常来的调用方 AkShare 恰恰显式写着 Chrome/81——尊重调用方等于
#: 给扰动副本开门。别的调用方若真需要另一种身份，这批主机上也没有成立的场景：
#: 它们只认现代 Chrome。这里改的是出站头，不是调用方的代码。
_AUTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
#: 身份头，**强制覆盖**调用方自带的同名头。上游按这一组判请求身份，self-consistent
#: 才是真值档；AkShare 硬编码的 Chrome/81 恰恰是扰动档的身份。缺了任何一项都算
#: 不自洽（老 UA 配新 client-hint、新 UA 缺 client-hint 都是矛盾体）。
_AUTH_IDENTITY_HEADERS = {
    "User-Agent": _AUTH_UA,
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
#: 装饰性头，缺了才补：它们不参与分档，调用方显式给的有自己的语义（测试靠一个
#: 自定义 Referer 断言"其他头原样保留"），覆盖它没有收益。
_AUTH_DEFAULT_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://data.eastmoney.com/",
}


def _with_auth_headers(url: str, kwargs: dict) -> dict:
    """给要求凭据的请求对齐身份：身份头强制覆盖，装饰性头缺了才补。

    只对 needs_auth 的主机生效，普通主机的调用方头原样不动。对这批主机，
    覆盖身份头就是本函数存在的意义（见上面 _AUTH_IDENTITY_HEADERS 的注释）。
    Cookie 不在这里，仍由 _with_auth_cookie 按"调用方给就不覆盖"处理。
    """
    if not eastmoney_auth.needs_auth(url):
        return kwargs
    headers = dict(kwargs.get("headers") or {})
    changed = False
    for name, value in {**_AUTH_DEFAULT_HEADERS, **_AUTH_IDENTITY_HEADERS}.items():
        for key in list(headers):
            if key.lower() == name.lower():
                if name in _AUTH_DEFAULT_HEADERS:
                    continue            # 装饰性头：调用方给的不动
                del headers[key]        # 身份头：大小写不同的同名头先删掉
        if not any(key.lower() == name.lower() for key in headers):
            headers[name] = value
            changed = True
    return {**kwargs, "headers": headers} if changed else kwargs


def _with_auth_cookie(url, kwargs: dict) -> tuple[dict, bool]:
    """给要求凭据的主机补上 ``Cookie`` 头，并返回是否用了本层凭据。

    只在调用方没有自己给 Cookie 时才补：显式传了 Cookie 的调用方知道自己在干什么，
    这一层不该覆盖它。取凭据这条路本身不抛异常，拿不到就原样返回——凭据是可用性
    优化，不能变成新的失败点。

    Cookie 之外同时补一套自洽的现代 Chrome 头（见 ``_with_auth_headers``）：没有
    这套头时 plain requests + AkShare 的旧 UA 会拿到上游的扰动副本，那是安静的数据
    质量问题，不能靠源排序或熔断发现。
    """
    header = eastmoney_auth.cookie_header(url)
    kwargs = _with_auth_headers(url, kwargs)
    if not header:
        return kwargs, False
    headers = dict(kwargs.get("headers") or {})
    if any(name.lower() == "cookie" for name in headers):
        return kwargs, False
    headers["Cookie"] = header
    return {**kwargs, "headers": headers}, True


def _note_auth_outcome(url, *, success: bool) -> None:
    """记一次凭据成败，**绝不把异常放回请求路径**。

    这是记账，不是取数。记账把一次本来成功的请求弄崩，是这个项目踩过的形态
    （维度统计写坏过一份已经渲染好的报告）。所以整段包起来，最坏是这一次没记上。
    """
    try:
        eastmoney_auth.note_outcome(url, success=success)
    except Exception:
        logger.debug("eastmoney_auth 记账失败 url=%s", url, exc_info=True)


def _plain_with_auth_outcome(
    base_cls, session, method, url, kwargs: dict, *, track_auth: bool
):
    """走原生 requests，并把结果记进凭据的成败账。

    这几个主机拒绝请求的形态是直接断连（curl 报 ``Empty reply from server``），在
    requests 里是 ``ConnectionError``，不是一个 200 空体——所以按异常和状态码判就够，
    不去读响应体：读 ``.content`` 会把 ``stream=True`` 的调用方弄坏。

    状态码用 ``getattr`` 取：这一层会包住别人的 Session，而"响应"未必是 requests 的
    Response（桩、别的通道的返回类型都可能）。取不到就不记这一笔，而不是崩在记账上。
    """
    try:
        response = base_cls.request(session, method, url, **kwargs)
    except Exception:
        if track_auth:
            _note_auth_outcome(url, success=False)
        raise
    status = getattr(response, "status_code", None)
    if track_auth and status is not None:
        _note_auth_outcome(url, success=status == 200)
    return response


def _install_auth_cookies() -> bool:
    """在 direct / proxy 模式下补一层只加 Cookie 的包装。

    impersonate 模式**不**走这里：那个模式把 ``requests.get/post/request`` 换成了
    闭包，闭包里直接 new 出 ImpersonateSession，看不见后装的包装类。所以那个模式的
    注入放在 ``ImpersonateSession.request`` 内部，一处盖住伪装和裸重放两条分支。

    凭据层关掉时**一个字节都不改** requests。``direct`` 的契约是"原生 requests，
    不经任何转发"，很多测试正是靠它才能断言业务逻辑而不是通道行为；为一个此刻不会
    注入任何东西的包装破掉那个契约，是白付一层间接。
    """
    if not eastmoney_auth.enabled():
        return False
    base_cls = std_requests.Session
    # setdefault：impersonate 分支已经把原始的那几个存进去了，这里不能覆盖成它的
    # 替身，否则 uninstall 之后 requests 上留着的是伪装类。
    _restore.setdefault("Session", base_cls)
    _restore.setdefault("get", std_requests.get)
    _restore.setdefault("post", std_requests.post)
    _restore.setdefault("request", std_requests.request)

    class AuthCookieSession(base_cls):
        def request(self, method, url, **kwargs):
            kwargs, track_auth = _with_auth_cookie(url, kwargs)
            return _plain_with_auth_outcome(
                base_cls, self, method, url, kwargs, track_auth=track_auth
            )

    def auth_get(url, params=None, **kwargs):
        with AuthCookieSession() as session:
            return session.get(url, params=params, **kwargs)

    def auth_post(url, data=None, json=None, **kwargs):
        with AuthCookieSession() as session:
            return session.post(url, data=data, json=json, **kwargs)

    def auth_request(method, url, **kwargs):
        with AuthCookieSession() as session:
            return session.request(method, url, **kwargs)

    std_requests.Session = AuthCookieSession
    std_requests.get = auth_get
    std_requests.post = auth_post
    std_requests.request = auth_request
    return True


def _cffi_session(impersonate: str):
    """Return this thread's curl_cffi session.

    One session per worker thread: curl_cffi sessions are not thread safe, and
    the data executor is bounded, so the number of live sessions is bounded too.
    """
    session = getattr(_thread_local, "cffi_session", None)
    if session is None:
        from curl_cffi import requests as cffi_requests

        session = cffi_requests.Session(impersonate=impersonate)
        _thread_local.cffi_session = session
    return session


def _impersonation_suspended() -> bool:
    """Whether the impersonated path is currently on cooldown."""
    with _breaker_lock:
        if _breaker["suspended_until"] <= time.monotonic():
            return False
        return True


def impersonated_hosts_degraded() -> bool:
    """走 IMPERSONATED_HOSTS 的请求此刻是不是注定失败。

    只有 ``impersonate`` 模式装了伪装通道；它一旦进入冷却，对这几个主机的请求
    就退回原生 requests，而这几个主机被列进 ``IMPERSONATED_HOSTS`` 的**理由**
    正是它们拒绝原生 requests。也就是说冷却期内这些请求是已知必败的。

    这个信息原先只有通道层自己知道。2026-09-05 线上日志里的代价：

        13:52:46  暂停伪装 300s -> 退回 plain requests
        13:52:48~13:53:01  10 次 K 线失败
        13:53:11~13:53:12  8 次 K 线调用，各 12.9~14.7s
        13:53:12  eastmoney_kline 熔断器这才打开

    26 秒里每个请求都在为一条已知必败的通道付满重试预算（RETRY×TIMEOUT 最坏
    24s），只因为每个源要用自己的连续失败计数独立"重新发现"这件事。让源直接问
    通道，就不用再发现一遍。

    还有一层二阶效应：源熔断的冷却（120s）比通道冷却（300s）短，所以源的半开
    探测必然落在通道仍然降级的窗口里、必然失败、再买一个 120s。查通道状态同时
    消掉这个空转。
    """
    return _installed_mode == "impersonate" and _impersonation_suspended()


def _record_impersonation(*, success: bool) -> None:
    """Trip a cooldown once impersonation is failing for every request.

    Each failure costs the retry budget plus the plain-requests replay, so an
    environment where impersonation can never work (no route, blocked profile)
    would otherwise pay that on every single call. Production evidence: with the
    proxy unreachable, hooked hosts went from sub-second to 7-11s each while the
    unhooked source was unaffected.
    """
    with _breaker_lock:
        if success:
            _breaker["failures"] = 0
            return
        if _breaker["suspended_until"] > time.monotonic():
            # 冷却已经开始，这些是熔断前就发出去、现在才失败返回的请求。再计一次
            # 只会把冷却往后推，并重复打一条读起来像"又失败了一整轮"的 WARNING。
            return
        _breaker["failures"] += 1
        if _breaker["failures"] < IMPERSONATE_SUSPEND_AFTER_FAILURES:
            return
        _breaker["failures"] = 0
        _breaker["suspended_until"] = time.monotonic() + IMPERSONATE_SUSPEND_SECONDS
    logger.warning(
        "HTTP channel suspending impersonation for %ss after %s consecutive "
        "failures; falling back to plain requests",
        IMPERSONATE_SUSPEND_SECONDS,
        IMPERSONATE_SUSPEND_AFTER_FAILURES,
    )


def _resolve_proxies(session, url) -> dict:
    """Mirror requests' proxy resolution for curl_cffi.

    curl_cffi has no ``trust_env`` and defaults ``proxies`` to empty, so without
    this the impersonated hosts would silently stop using the proxy the rest of
    the process goes through -- on a host that only reaches upstream via a local
    proxy, every impersonated attempt would be doomed before it starts.
    """
    proxies = dict(getattr(session, "proxies", None) or {})
    if getattr(session, "trust_env", True):
        environ_proxies = std_requests.utils.get_environ_proxies(url, no_proxy=None)
        for scheme, proxy in environ_proxies.items():
            proxies.setdefault(scheme, proxy)
    return proxies


def _resolve_verify(session):
    """Mirror requests' TLS verification settings for curl_cffi.

    Bypassing ``Session.request`` also bypasses the environment merge that would
    have picked up ``REQUESTS_CA_BUNDLE``; curl only reads ``CURL_CA_BUNDLE``, so
    behind a TLS-inspecting proxy the impersonated attempts would fail
    verification while the plain path succeeded.
    """
    verify = getattr(session, "verify", True)
    if verify is True and getattr(session, "trust_env", True):
        bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
        if bundle:
            return bundle
    return verify


def _install_impersonate(
    retry: int = IMPERSONATE_RETRY,
    timeout: float = IMPERSONATE_TIMEOUT_SECONDS,
    impersonate: str = IMPERSONATE_BROWSER,
) -> bool:
    """Route impersonated hosts through curl_cffi, leaving everything else alone.

    Returns False when curl_cffi is unavailable, so the caller can degrade to
    ``direct`` rather than fail to start.
    """
    try:
        from curl_cffi import requests as cffi_requests  # noqa: F401
    except ImportError:
        return False

    # Captured once, and never re-read from the module afterwards: a later patch
    # that repoints the module attribute must not turn this into a self-call.
    original_session_cls = std_requests.Session
    setattr(std_requests, _OWN_MARKER, original_session_cls)
    _restore["Session"] = std_requests.Session
    _restore["get"] = std_requests.get
    _restore["post"] = std_requests.post
    _restore["request"] = std_requests.request

    class ImpersonateSession(original_session_cls):
        def request(self, method, url, **kwargs):
            # 凭据在最前面补：伪装分支和裸重放分支都要带上。这两条正是同一批主机的
            # 两条出路，只给一条带等于让另一条继续被拒。
            kwargs, track_auth = _with_auth_cookie(url, kwargs)
            if not _is_impersonated(url) or _impersonation_suspended():
                kwargs.pop("impersonate", None)
                return _plain_with_auth_outcome(
                    original_session_cls, self, method, url, kwargs,
                    track_auth=track_auth,
                )

            attempt_kwargs = dict(kwargs)
            attempt_kwargs["timeout"] = timeout
            proxies = _resolve_proxies(self, url)
            if proxies:
                attempt_kwargs["proxies"] = proxies
            attempt_kwargs.setdefault("verify", _resolve_verify(self))
            outcome = "non_200"
            for attempt in range(retry):
                try:
                    response = _cffi_session(impersonate).request(
                        method, url, **attempt_kwargs
                    )
                    if response.status_code == 200:
                        _record_impersonation(success=True)
                        if track_auth:
                            _note_auth_outcome(url, success=True)
                        return response
                    outcome = f"status_{response.status_code}"
                except Exception as exc:
                    outcome = f"{type(exc).__name__}: {exc}"[:200]
                    # A broken session cannot be reused for the retry.
                    _thread_local.cffi_session = None
                if attempt + 1 < retry:
                    time.sleep(0.3)

            # Without this the only visible error is the plain replay's, which
            # hides why impersonation failed and made a proxy misconfiguration
            # look like an upstream outage.
            request_id, tool, symbol = log_context()
            logger.debug(
                "Impersonation failed request_id=%s tool=%s symbol=%s host=%s "
                "attempts=%s proxied=%s outcome=%s; replaying via plain requests",
                request_id,
                tool,
                symbol,
                urlsplit(url).hostname,
                retry,
                bool(proxies),
                outcome,
            )

            _record_impersonation(success=False)
            # Replaying through plain requests keeps the failure shape identical
            # to the unpatched build: callers still see requests' own response
            # and exception types, which efinance and AkShare branch on.
            kwargs.pop("impersonate", None)
            # 只按最终结果记凭据成败：伪装失败、普通请求成功时凭据显然仍然可用，
            # 不能因为中间路径失败就触发重采。
            return _plain_with_auth_outcome(
                original_session_cls, self, method, url, kwargs,
                track_auth=track_auth,
            )

    def impersonate_get(url, params=None, **kwargs):
        with ImpersonateSession() as session:
            return session.get(url, params=params, **kwargs)

    def impersonate_post(url, data=None, json=None, **kwargs):
        with ImpersonateSession() as session:
            return session.post(url, data=data, json=json, **kwargs)

    def impersonate_request(method, url, **kwargs):
        with ImpersonateSession() as session:
            return session.request(method, url, **kwargs)

    std_requests.Session = ImpersonateSession
    std_requests.get = impersonate_get
    std_requests.post = impersonate_post
    std_requests.request = impersonate_request
    return True


def _install_proxy(gateway, token, retry: int) -> None:
    """Install akshare-proxy-patch with its own hook list."""
    import akshare_proxy_patch

    akshare_proxy_patch.install_patch(
        gateway,
        auth_token=token,
        retry=retry,
        hook_domains=list(IMPERSONATED_HOSTS),
    )
    _installed_detail.update(
        gateway=gateway,
        token="configured" if token else "missing",
        retry=retry,
        patch_version=akshare_proxy_patch.__version__,
    )


def install_http_channel(
    requested=None,
    proxy_enabled: bool = False,
    proxy_gateway=None,
    proxy_token=None,
    proxy_retry: int = 30,
) -> str:
    """Resolve and install exactly one outbound HTTP channel.

    Raises HttpModeError only for an explicit ``proxy`` request without a
    gateway; every automatic resolution degrades instead of stopping startup.
    """
    global _installed_mode, _installed_reason

    mode, reason = resolve_http_mode(requested, proxy_enabled, proxy_gateway)
    with _state_lock:
        if _installed_mode is not None:
            logger.warning(
                "HTTP channel already installed mode=%s ignored=%s",
                _installed_mode,
                mode,
            )
            return _installed_mode

        _installed_detail.clear()
        if mode == "impersonate" and hasattr(std_requests, _PROXY_PATCH_MARKER):
            # Another component already rewired requests; stacking on top of it
            # would silently disable whichever channel installed first.
            logger.warning(
                "HTTP channel degraded to direct reason=requests_already_patched "
                "requested=%s",
                mode,
            )
            _installed_mode = "direct"
            _installed_reason = "requests_already_patched"
            return _installed_mode

        if mode == "proxy":
            _install_proxy(proxy_gateway, proxy_token, proxy_retry)
        elif mode == "impersonate":
            if _install_impersonate():
                _installed_detail.update(
                    profile=IMPERSONATE_BROWSER,
                    retry=IMPERSONATE_RETRY,
                    timeout=f"{IMPERSONATE_TIMEOUT_SECONDS}s",
                )
            else:
                logger.warning(
                    "HTTP channel degraded to direct reason=curl_cffi_unavailable "
                    "requested=impersonate"
                )
                mode, reason = "direct", "curl_cffi_unavailable"

        if mode == "direct":
            # impersonate 自己在 ImpersonateSession 里注入；proxy 由第三方插件完整接管。
            # 两者都不再套一层 requests 包装。
            _install_auth_cookies()

        # The effective channel is reported once by the startup banner via
        # describe_installed_channel(); only degradations are logged here.
        _installed_mode = mode
        _installed_reason = reason
        logger.debug("HTTP channel installed %s", describe_installed_channel())

    # 锁外读盘：盘上有一份没过期的就直接用，省掉一次页面加载。
    #
    # 这里**只读盘、不采集**。安装发生在 cn_stock_source 的 import 期，在这里起一次
    # 采集就等于"import 这个模块会拉起一个 Chromium"——测试和任何只想调个函数的
    # 脚本都要付。采集改由首次真正打到这几个主机的请求懒触发（cookie_header），
    # 那时它是后台的，不占请求路径。
    if mode != "proxy":
        eastmoney_auth.load_cached()
    return mode


def uninstall_http_channel() -> None:
    """Restore the impersonate channel's rewrites. Intended for tests.

    The proxy channel owns its own uninstall path, so this only reverses what
    the impersonate channel replaced.
    """
    global _installed_mode, _installed_reason

    with _state_lock:
        for name, original in _restore.items():
            setattr(std_requests, name, original)
        _restore.clear()
        _installed_detail.clear()
        if hasattr(std_requests, _OWN_MARKER):
            delattr(std_requests, _OWN_MARKER)
        _thread_local.cffi_session = None
        _installed_mode = None
        _installed_reason = None
    with _breaker_lock:
        _breaker.update(failures=0, suspended_until=0.0)


__all__ = [
    "HttpModeError",
    "IMPERSONATED_HOSTS",
    "describe_installed_channel",
    "impersonated_hosts_degraded",
    "install_http_channel",
    "installed_mode",
    "uninstall_http_channel",
]
