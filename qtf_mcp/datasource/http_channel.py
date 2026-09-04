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
    HTTP_IMPERSONATE_COOLDOWN,
    HTTP_IMPERSONATE_FAILURE_THRESHOLD,
    HTTP_IMPERSONATE_PROFILE,
    HTTP_IMPERSONATE_RETRY,
    HTTP_IMPERSONATE_TIMEOUT,
    HttpModeError,
    resolve_http_mode,
)
from ..observability import log_context

logger = logging.getLogger("qtf_mcp")

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
        if _breaker["failures"] < HTTP_IMPERSONATE_FAILURE_THRESHOLD:
            return
        _breaker["failures"] = 0
        _breaker["suspended_until"] = time.monotonic() + HTTP_IMPERSONATE_COOLDOWN
    logger.warning(
        "HTTP channel suspending impersonation for %ss after %s consecutive "
        "failures; falling back to plain requests",
        HTTP_IMPERSONATE_COOLDOWN,
        HTTP_IMPERSONATE_FAILURE_THRESHOLD,
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
    retry: int = HTTP_IMPERSONATE_RETRY,
    timeout: float = HTTP_IMPERSONATE_TIMEOUT,
    impersonate: str = HTTP_IMPERSONATE_PROFILE,
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
            if not _is_impersonated(url) or _impersonation_suspended():
                kwargs.pop("impersonate", None)
                return original_session_cls.request(self, method, url, **kwargs)

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
            return original_session_cls.request(self, method, url, **kwargs)

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
                    profile=HTTP_IMPERSONATE_PROFILE,
                    retry=HTTP_IMPERSONATE_RETRY,
                    timeout=f"{HTTP_IMPERSONATE_TIMEOUT}s",
                )
            else:
                logger.warning(
                    "HTTP channel degraded to direct reason=curl_cffi_unavailable "
                    "requested=impersonate"
                )
                mode, reason = "direct", "curl_cffi_unavailable"

        # The effective channel is reported once by the startup banner via
        # describe_installed_channel(); only degradations are logged here.
        _installed_mode = mode
        _installed_reason = reason
        logger.debug("HTTP channel installed %s", describe_installed_channel())
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
    "install_http_channel",
    "installed_mode",
    "uninstall_http_channel",
]
