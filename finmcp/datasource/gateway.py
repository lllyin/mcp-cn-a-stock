"""付费网关传输的抽象层。

akshare-proxy-patch 是第一个实现；未来有更好的代理库时，写一个实现
``GatewayTransport`` 的类并加进 ``GATEWAY_TRANSPORT`` 的可选值即可。

这一层只管"怎么通过付费网关把请求发出去"：取凭据、复用凭据、作废坏出口、
并发闸、响应校验。**什么时候**该走网关是编排层（provider 链）的决定，不在这里。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple, Optional, Protocol
from urllib.parse import urlsplit

from ..config import (
    AUTO_PROXY_COOLDOWN_SECONDS,
    AUTO_PROXY_DATA_COOLDOWN_SECONDS,
    GATEWAY_AUTH_REUSE_SECONDS,
    GATEWAY_EXIT_RETRIES,
    GATEWAY_SINGLEFLIGHT_WAIT_SECONDS,
)

logger = logging.getLogger("finmcp")


_request_cancel_local = threading.local()


def set_current_request_cancel_event(event):
    """Bind the current executor thread's request-cancel signal."""
    previous = getattr(_request_cancel_local, "event", None)
    _request_cancel_local.event = event
    return previous


def restore_current_request_cancel_event(previous) -> None:
    """Restore the executor thread's previous request-cancel signal."""
    if previous is None:
        try:
            del _request_cancel_local.event
        except AttributeError:
            pass
    else:
        _request_cancel_local.event = previous


def current_request_cancel_event():
    """Return the cancellation signal bound to the current worker thread."""
    return getattr(_request_cancel_local, "event", None)


class GatewayAuth(NamedTuple):
    """一份网关出口凭据：代理地址、配套 Cookie、配套 UA。三者一组，拆用无效。"""

    proxy: str
    cookie: str
    user_agent: str


class GatewayTransport(Protocol):
    """网关实现的契约。authenticate 自己管认证缓存和并发合并。"""

    name: str

    def authenticate(self) -> Optional[GatewayAuth]:
        """取一份出口凭据；网关不可用返回 None。"""

    def invalidate(self, auth: GatewayAuth) -> None:
        """作废这一份凭据（出口被拒/连不上）。只作废这一代，不碰别代。"""


class AkshareProxyTransport:
    """akshare-proxy-patch 的认证接口。

    插件的两个既有行为在这里兜住：

    - 认证失败时**原样返回过期的旧数据**（``_cache.expire_at`` 置 0 但返回
      ``_cache.data``）——所以"拿到 auth"不等于"刚认证成功"，新鲜度由
      ``GatewayClient`` 自己记。
    - 任何一次非 200 都会把插件缓存作废，接口被拒时认证按重试次数消耗而不是
      按 28s 窗口。``GatewayClient`` 的复用层把这件事盖住。
    """

    name = "akshare_proxy_patch"
    #: 一次认证最多要多久：插件取凭据用的是 timeout=(1.5, 3)，连接加读取。
    #: GatewayClient 用它算领头请求的最坏耗时。
    auth_budget_seconds = 1.5 + 3

    def __init__(self, gateway: str, token: str) -> None:
        self._auth_url = f"http://{gateway}:47001/api/akshare-auth"
        self._token = token

    def authenticate(self) -> Optional[GatewayAuth]:
        import akshare_proxy_patch

        data = akshare_proxy_patch.get_auth_config_with_cache(self._auth_url, self._token)
        if not data or not data.get("proxy"):
            return None
        return GatewayAuth(
            proxy=data["proxy"],
            cookie=data.get("cookie") or "",
            user_agent=data.get("ua") or "",
        )

    def invalidate(self, auth: GatewayAuth) -> None:
        try:
            import akshare_proxy_patch

            # 只作废这一代：并发下另一请求刚拿到的新出口不受牵连。
            cached = akshare_proxy_patch._cache.data
            if cached and cached.get("proxy") == auth.proxy:
                akshare_proxy_patch._cache.expire_at = 0
        except Exception:  # noqa: BLE001 - 插件不在时没有二级缓存可失效
            pass


def path_family(url: str) -> str:
    """同一主机上的不同接口族分开记账：K 线成功不能误恢复资金流的状态。"""
    path = urlsplit(url).path
    if "/fflow/" in path:
        return "fflow"
    if "kline" in path:
        return "kline"
    return "other"


# --- 端点感知的响应校验 -----------------------------------------------------
#
# HTTP 200 不等于拿到了数据：东财风控会回 200 + 拦截页（HTML）、空体、或
# ``{"rc": 102, "data": null}`` 这类业务拒绝。传输层只验结构——"内容是不是
# 这只标的该有的"是 provider 的事（停牌标的空 klines 是合法答案，不能在这里
# 判死）。没登记的端点用默认：JSON dict 即结构有效。


def _fflow_payload_ok(payload: dict) -> bool:
    # rc=0 但 klines 为空是异常形态：东财对"没数据"的合法回答是 rc=100，
    # 空 klines 更像扰动副本。宁可作废一个出口，不把空数据记成恢复。
    data = payload.get("data")
    return (isinstance(data, dict) and isinstance(data.get("klines"), list)
            and bool(data["klines"]))


def _data_payload_ok(payload: dict) -> bool:
    return isinstance(payload.get("data"), dict) and bool(payload["data"])


_ENDPOINT_VALIDATORS = {
    "/api/qt/stock/fflow/daykline/get": _fflow_payload_ok,
    "/api/qt/stock/fflow/kline/get": _fflow_payload_ok,
    "/api/qt/stock/get": _data_payload_ok,
    "/api/qt/clist/get": _data_payload_ok,
    # kline/get 不在这里：停牌标的空 klines 是合法答案，不能用"必须有 klines"判死。
}


def response_ok(url: str, response) -> bool:
    """这个响应算不算"拿到了有效数据"。

    只看响应头和已下载完的正文，**不主动读流**：``stream=True`` 的响应体还没
    下载，读了会破坏调用方。取不到头/桩对象一律放行，交给调用方解析兜底。
    """
    if getattr(response, "status_code", None) != 200:
        return False
    try:
        content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
    except Exception:  # noqa: BLE001 - 桩/别的通道的响应类型不进校验
        return True
    if "html" in content_type:
        return False
    if "json" not in content_type:
        # 取不到头、或 JSONP（text/javascript，正文不是合法 JSON）都不在这里
        # 加码判断，交给调用方的解析兜底。
        return True
    if not getattr(response, "_content_consumed", True):
        return True  # 流式响应体未下载，不在这里读
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - JSON 接口解析失败即无效
        return False
    if not isinstance(payload, dict) or "rc" not in payload:
        return False
    validator = _ENDPOINT_VALIDATORS.get(urlsplit(url).path)
    if validator is None:
        return True
    return validator(payload)


class _PathState:
    __slots__ = ("cooldown_until", "in_flight", "leader_done", "leader_ok")

    def __init__(self) -> None:
        self.cooldown_until = 0.0
        self.in_flight = False
        self.leader_done = threading.Event()
        self.leader_ok = False


class GatewayClient:
    """显式网关传输：编排层调到就走网关，不依赖失败计数。

    - 凭据复用：一份出口凭据用到失败或达到 ``reuse_seconds`` 上限为止，不按
      插件的 28s 固定轮换（实测一份凭据能稳定服务数分钟，28s 是插件的保守值）。
    - 并发闸：同一 (host, 接口族) 一次只允许一个网关请求在飞；其余有界等待
      leader 的结果——leader 成功就用同一份凭据自己发，失败/超时就返回 None
      走原回退链。等待有上界，且线程结果在批次取消后会被丢弃，不会成为新的
      堆积点。**等多久由调用方定**：后面还有别的源可退的（通道层）用短等待；
      链尾没有回退的（编排层显式调用）等满 leader 的最坏耗时，见 ``leader_budget``。
    - 冷却：``exit_retries`` 次尝试全部用尽才进冷却；这一轮里认证服务一个新出口都
      没给过用长冷却，给过（坏的是出口本身）用短冷却换出口。
    """

    def __init__(
        self,
        transport: GatewayTransport,
        *,
        reuse_seconds: float = GATEWAY_AUTH_REUSE_SECONDS,
        cooldown_seconds: float = AUTO_PROXY_COOLDOWN_SECONDS,
        data_cooldown_seconds: float = AUTO_PROXY_DATA_COOLDOWN_SECONDS,
        wait_seconds: float = GATEWAY_SINGLEFLIGHT_WAIT_SECONDS,
        exit_retries: int = GATEWAY_EXIT_RETRIES,
        auth_retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._transport = transport
        self._reuse_seconds = reuse_seconds
        self._cooldown_seconds = cooldown_seconds
        self._data_cooldown_seconds = data_cooldown_seconds
        self._wait_seconds = wait_seconds
        self._exit_retries = max(1, exit_retries)
        # 拿不到新出口时，隔多久再要一次。认证请求自己有 4.5s 超时，超时那种失败本身
        # 就等过了；这个间隔防的是连接被立刻拒绝那种毫秒级失败把认证服务打成连发。
        self._auth_retry_backoff = max(0.0, auth_retry_backoff_seconds)
        self._lock = threading.Lock()
        self._auth: Optional[GatewayAuth] = None
        self._auth_at = 0.0
        self._last_failed_proxy: Optional[str] = None
        self._states: dict = {}
        # 按线程数真正发出去的请求：编排层要知道这一级是真付了，还是被并发闸/
        # 冷却挡下、一个请求都没发。
        self._local = threading.local()

    def leader_budget(self, timeout) -> float:
        """一个 leader 最坏要用多久：每换一个出口都可能先认证一次，再把请求超时用满。

        requests 的数字超时对连接和读取各生效一次，所以单次请求按两倍算；
        ``(connect, read)`` 元组按两项之和。按最大值定、不按分位数：等的人比
        leader 先放弃，那一次就白等了、还空手回去（AGENTS.md §五）。
        """
        if isinstance(timeout, (tuple, list)):
            send = float(sum(part for part in timeout if part is not None))
        else:
            send = 2 * float(timeout)
        auth = float(getattr(self._transport, "auth_budget_seconds", 0.0))
        # 每次尝试：认证 + 请求；两次尝试之间最多再隔一个认证重试间隔。
        return (self._exit_retries * (auth + send)
                + (self._exit_retries - 1) * self._auth_retry_backoff)

    def sends_in_current_thread(self) -> int:
        """本线程累计真正发出去的网关请求数（含失败的那几次）。"""
        return getattr(self._local, "sends", 0)

    # -- 凭据复用 ----------------------------------------------------------

    def _acquire_auth(self) -> Optional[GatewayAuth]:
        with self._lock:
            if self._auth is not None and \
                    time.monotonic() - self._auth_at < self._reuse_seconds:
                return self._auth
        # 认证是网络调用，不能捏着锁做——会挡住所有 host 的状态操作。
        auth = self._transport.authenticate()
        if auth is not None:
            with self._lock:
                # 认证层把刚失败的那个出口原样吐回来（插件缓存失效时返回旧数据），
                # 等于没有新出口可用——按认证不可用处理。
                if auth.proxy == self._last_failed_proxy:
                    auth = None
                else:
                    self._auth = auth
                    self._auth_at = time.monotonic()
        return auth

    def _drop_auth(self, auth: Optional[GatewayAuth]) -> None:
        if auth is None:
            return
        self._transport.invalidate(auth)
        with self._lock:
            if self._auth == auth:
                self._auth = None
                self._auth_at = 0.0
            self._last_failed_proxy = auth.proxy

    # -- 状态 ---------------------------------------------------------------

    def _state(self, key) -> _PathState:
        return self._states.setdefault(key, _PathState())

    def _cool_down(self, key, seconds: float, reason: str) -> None:
        with self._lock:
            state = self._state(key)
            state.cooldown_until = time.monotonic() + seconds
        logger.warning("gateway_state host=%s family=%s state=cooldown seconds=%s reason=%s",
                       key[0], key[1], seconds, reason)

    # -- 请求 ---------------------------------------------------------------

    def request(self, method: str, url: str, send, *, follower_wait: Optional[float] = None,
                cancel_event: Optional[threading.Event] = None, **kwargs):
        """走网关发一次请求。成功返回 response；不可用/失败返回 None。

        ``send(method, url, **kwargs)`` 由调用方提供，决定用哪个 session 发——
        通道层传原始 requests.Session（绕过自己装的包装），脚本可以传任何
        兼容 requests 的对象。

        ``follower_wait``：已有 leader 在飞时最多等它多久，缺省用构造时的
        ``wait_seconds``。放弃的代价由调用方判断：后面还有源可退就短等，链尾就
        传 ``leader_budget(timeout)``。

        ``cancel_event`` 由异步请求层绑定。同步网关等待不能直接感知
        ``asyncio`` 任务取消，所以这里用短间隔轮询让已断开的请求及时退出。
        """
        wait_seconds = self._wait_seconds if follower_wait is None else follower_wait
        host = (urlsplit(url).hostname or "?").lower()
        key = (host, path_family(url))
        with self._lock:
            state = self._state(key)
            if state.cooldown_until > time.monotonic():
                return None
            if state.in_flight:
                leader_done = state.leader_done
            else:
                state.in_flight = True
                state.leader_done.clear()
                state.leader_ok = False
                leader_done = None

        if leader_done is not None:
            wait_result = _wait_for_event(leader_done, wait_seconds, cancel_event)
            if wait_result == "cancelled":
                logger.debug("gateway_skip host=%s family=%s reason=request_cancelled",
                             host, key[1])
                return None
            if wait_result == "timeout":
                logger.debug("gateway_skip host=%s family=%s reason=leader_timeout waited=%.1fs",
                             host, key[1], wait_seconds)
                return None
            with self._lock:
                if not state.leader_ok:
                    logger.debug("gateway_skip host=%s family=%s reason=leader_failed",
                                 host, key[1])
                    return None
            # leader 证明出口可用，本次用同一份凭据自己发（不共享响应正文——
            # 不同标的的应答不能互用）。
            return self._attempt(
                method, url, send, kwargs, key, leader=False,
                cancel_event=cancel_event,
            )

        try:
            return self._attempt(
                method, url, send, kwargs, key, leader=True,
                cancel_event=cancel_event,
            )
        finally:
            with self._lock:
                state.in_flight = False
                state.leader_done.set()

    def _attempt(self, method, url, send, kwargs, key, *, leader: bool,
                 cancel_event: Optional[threading.Event] = None):
        """发一次网关请求，出口死了就换新的重试，连续失败才冷却。

        网关出口是住宅代理，有一定比例的当场死亡（2026-09-15 实测约 15%）。
        一个死出口就冷却 30s 的话，网关会频繁整段不可用；换成"死了立刻换新的
        重试 N 次"，把单次请求的失败率从 15% 压到 0.15^N 量级。

        **拿不到新出口也只算一次失败**，隔一小段再要，N 次都要不到才冷却。以前是
        认证一失败立刻冷却 300s：插件的认证请求超时 3s 就把旧缓存（刚死的那个出口）
        原样吐回来，一次 3 秒的抖动换来 5 分钟这一族网关停摆——12 天里 17 次，其中
        至少 8 次别的接口族 60 秒内就重新拿到了出口。失败的认证不发凭据、不花积分。

        冷却长短看这一轮里认证服务给没给过**新**出口：给过，说明它活着，坏的是出口，
        用短冷却；一个都没给才是认证不可用，用长冷却。
        """
        host = key[0]
        auth_failed = False       # 有过一次"拿不到新出口"
        fresh_after_failure = False  # 失败之后认证服务给过新出口——它活着
        for attempt in range(self._exit_retries):
            if cancel_event is not None and cancel_event.is_set():
                return None
            auth = self._acquire_auth()
            if auth is None:
                if cancel_event is not None and cancel_event.is_set():
                    return None
                auth_failed = True
                logger.warning(
                    "gateway_failure host=%s family=%s path=%s attempt=%d/%d error=authentication_unavailable",
                    host, key[1], urlsplit(url).path, attempt + 1, self._exit_retries,
                )
                if attempt + 1 < self._exit_retries and self._auth_retry_backoff:
                    interrupted = (
                        cancel_event.wait(self._auth_retry_backoff)
                        if cancel_event is not None
                        else False
                    )
                    if interrupted:
                        return None
                    if cancel_event is None:
                        time.sleep(self._auth_retry_backoff)
                continue  # 没拿到出口，隔一下再要
            if cancel_event is not None and cancel_event.is_set():
                return None
            if attempt > 0:
                # 前一次失败时已作废旧凭据，这里拿到的必然是认证服务新给的。
                fresh_after_failure = True
            retry_kwargs = dict(kwargs)
            headers = dict(retry_kwargs.get("headers") or {})
            if auth.user_agent:
                headers["User-Agent"] = auth.user_agent
            if auth.cookie:
                headers["Cookie"] = auth.cookie
            retry_kwargs["headers"] = headers
            retry_kwargs["proxies"] = {"http": auth.proxy, "https": auth.proxy}
            retry_kwargs.pop("impersonate", None)

            started = time.perf_counter()
            self._local.sends = self.sends_in_current_thread() + 1
            try:
                response = send(method, url, **retry_kwargs)
            except Exception as exc:
                if cancel_event is not None and cancel_event.is_set():
                    self._drop_auth(auth)
                    return None
                logger.warning(
                    "gateway_failure host=%s family=%s path=%s attempt=%d/%d error=%s",
                    host, key[1], urlsplit(url).path, attempt + 1, self._exit_retries,
                    _sanitize(str(exc), auth),
                )
                self._drop_auth(auth)
                continue  # 出口死了，换一个新的重试
            elapsed = time.perf_counter() - started
            if cancel_event is not None and cancel_event.is_set():
                if not response_ok(url, response):
                    self._drop_auth(auth)
                return None
            if response_ok(url, response):
                if leader:
                    with self._lock:
                        self._state(key).leader_ok = True
                logger.info(
                    "gateway_success host=%s family=%s path=%s elapsed=%.2fs",
                    host, key[1], urlsplit(url).path, elapsed,
                )
                return response
            logger.warning(
                "gateway_failure host=%s family=%s path=%s status=%s attempt=%d/%d elapsed=%.2fs",
                host, key[1], urlsplit(url).path,
                getattr(response, "status_code", None), attempt + 1, self._exit_retries, elapsed,
            )
            self._drop_auth(auth)
            continue  # 响应无效，换一个新的重试
        # N 次全部用尽才冷却
        if auth_failed and not fresh_after_failure:
            self._cool_down(key, self._cooldown_seconds, "authentication_unavailable")
        else:
            self._cool_down(key, self._data_cooldown_seconds, "data_failure")
        return None


def _wait_for_event(event: threading.Event, timeout: float,
                    cancel_event: Optional[threading.Event]):
    """Wait for ``event`` with a bounded, cancellation-aware polling interval."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        if event.wait(timeout=min(0.05, remaining)):
            return "done"


def _sanitize(text: str, auth: Optional[GatewayAuth]) -> str:
    """异常原文里的出口地址带凭据（user:pass@host），进日志前抹掉。"""
    text = text[:200]
    if auth is not None and auth.proxy:
        text = text.replace(auth.proxy, "<proxy>")
    return text


_client: Optional[GatewayClient] = None
_client_lock = threading.Lock()


def get_gateway_client() -> Optional[GatewayClient]:
    """按配置构造共享的 GatewayClient。网关没配置（地址/令牌缺失）时返回 None。"""
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        from ..config import AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD, GATEWAY_TRANSPORT

        if not AKSHARE_PROXY_IP or not AKSHARE_PROXY_PASSWORD:
            return None
        if GATEWAY_TRANSPORT != AkshareProxyTransport.name:
            logger.warning("未知 GATEWAY_TRANSPORT=%s，网关回退不可用", GATEWAY_TRANSPORT)
            return None
        _client = GatewayClient(AkshareProxyTransport(AKSHARE_PROXY_IP, AKSHARE_PROXY_PASSWORD))
        return _client


def gateway_sends_in_current_thread() -> int:
    """共享客户端在本线程累计发出去的请求数；网关没配置时恒为 0。"""
    client = _client
    return client.sends_in_current_thread() if client is not None else 0


def reset_gateway_client() -> None:
    """测试用：丢掉共享实例，下一个调用按当前配置重建。"""
    global _client
    with _client_lock:
        _client = None
