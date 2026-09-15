"""网关传输抽象层（gateway.py）的测试。

不碰真实网关：transport 用假的，send 用桩。要守住的语义：
- 凭据复用到失败或上限，不按固定周期轮换
- 同一 (host, 接口族) 只有一个网关请求在飞，其余有界等待
- 200 但正文无效（拦截页/业务拒绝/空数据）不算成功
- 失败只作废这一代凭据，不碰别代
"""

import threading
import time
import types

import pytest

from finmcp.datasource import gateway
from finmcp.datasource.gateway import GatewayAuth, GatewayClient, response_ok


URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=1.600519"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519"


class FakeTransport:
    name = "fake"

    def __init__(self, exits=None):
        self._exits = list(exits or ["http://proxy-a:1"])
        self.auth_calls = 0
        self.invalidated = []

    def authenticate(self):
        self.auth_calls += 1
        if not self._exits:
            return None
        proxy = self._exits.pop(0) if len(self._exits) > 1 else self._exits[0]
        return GatewayAuth(proxy=proxy, cookie="nid18=x", user_agent="UA")

    def invalidate(self, auth):
        self.invalidated.append(auth.proxy)


def ok_response(payload=None):
    return types.SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/json"},
        _content_consumed=True,
        json=lambda: payload if payload is not None else
        {"rc": 0, "data": {"klines": ["2026-09-11,1,2,3,4"]}},
    )


def make_client(transport=None, **kwargs):
    return GatewayClient(transport or FakeTransport(), **kwargs)


class TestResponseOk:
    def test_valid_fflow_payload(self):
        assert response_ok(URL, ok_response()) is True

    def test_html_block_page_is_invalid(self):
        response = types.SimpleNamespace(
            status_code=200, headers={"Content-Type": "text/html"})
        assert response_ok(URL, response) is False

    def test_rc_nonzero_with_null_data_is_invalid(self):
        assert response_ok(URL, ok_response({"rc": 102, "data": None})) is False

    def test_rc_zero_with_null_data_is_invalid(self):
        assert response_ok(URL, ok_response({"rc": 0, "data": None})) is False

    def test_rc_zero_with_empty_klines_is_invalid(self):
        # rc=0 但 klines 为空是异常形态：东财对"没数据"的合法回答是 rc=100，
        # 空 klines 更像扰动副本。宁可作废一个出口，不把空数据记成恢复。
        assert response_ok(URL, ok_response({"rc": 0, "data": {"klines": []}})) is False

    def test_missing_rc_is_invalid(self):
        assert response_ok(URL, ok_response({"data": {"klines": [1]}})) is False

    def test_unregistered_endpoint_accepts_any_json_dict(self):
        url = "https://push2.eastmoney.com/api/qt/other"
        assert response_ok(url, ok_response({"rc": 0, "whatever": 1})) is True

    def test_stub_response_without_headers_passes(self):
        assert response_ok(URL, types.SimpleNamespace(status_code=200)) is True

    def test_non_200_is_invalid(self):
        assert response_ok(URL, types.SimpleNamespace(status_code=500)) is False


class TestAuthReuse:
    def test_auth_is_reused_within_the_cap(self):
        transport = FakeTransport()
        client = make_client(transport)
        send = lambda *args, **kwargs: ok_response()

        client.request("GET", URL, send)
        client.request("GET", URL, send)

        assert transport.auth_calls == 1

    def test_auth_is_refreshed_after_the_cap(self):
        transport = FakeTransport()
        client = make_client(transport, reuse_seconds=0.5)
        send = lambda *args, **kwargs: ok_response()

        client.request("GET", URL, send)
        time.sleep(0.6)
        client.request("GET", URL, send)

        assert transport.auth_calls == 2

    def test_failure_invalidates_only_that_generation(self):
        transport = FakeTransport(exits=["http://proxy-a:1", "http://proxy-b:1"])
        client = make_client(transport, data_cooldown_seconds=0.01, exit_retries=1)

        assert client.request("GET", URL, lambda *a, **k: (_ for _ in ()).throw(
            ConnectionError("refused"))) is None
        assert transport.invalidated == ["http://proxy-a:1"]

        time.sleep(0.02)  # 过数据冷却
        # 下一次用新出口成功
        response = client.request("GET", URL, lambda *a, **k: ok_response())
        assert response is not None
        assert transport.auth_calls == 2

    def test_same_proxy_reissued_after_failure_counts_as_auth_unavailable(self):
        # 认证层把刚失败的出口原样吐回来（插件缓存失效时返回旧数据），
        # 等于没有新出口——按认证不可用走长冷却，不能记成"刚拿到认证"。
        transport = FakeTransport(exits=["http://proxy-a:1"])  # 永远给同一个
        client = make_client(transport, cooldown_seconds=300, data_cooldown_seconds=0.01)

        assert client.request("GET", URL, lambda *a, **k: (_ for _ in ()).throw(
            ConnectionError("refused"))) is None
        time.sleep(0.02)  # 过数据冷却
        assert client.request("GET", URL, lambda *a, **k: ok_response()) is None
        assert transport.auth_calls == 2  # 第二次认证了但被拒收
        state = client._states[("push2his.eastmoney.com", "fflow")]
        assert state.cooldown_until > time.monotonic() + 100  # 长冷却

    def test_auth_unavailable_goes_to_long_cooldown(self):
        transport = FakeTransport(exits=[])
        transport._exits = []
        client = make_client(transport, cooldown_seconds=300)

        assert client.request("GET", URL, lambda *a, **k: ok_response()) is None
        state = client._states[("push2his.eastmoney.com", "fflow")]
        assert state.cooldown_until > time.monotonic() + 100


class TestSingleflight:
    def test_follower_waits_for_leader_then_sends_with_the_same_auth(self):
        transport = FakeTransport()
        client = make_client(transport, wait_seconds=2)
        release = threading.Event()
        leader_started = threading.Event()
        results = {}
        follower_sends = []

        def leader_send(*args, **kwargs):
            leader_started.set()
            release.wait(5)
            return ok_response()

        def follower_send(*args, **kwargs):
            follower_sends.append(1)
            return ok_response()

        leader = threading.Thread(
            target=lambda: results.setdefault("leader", client.request("GET", URL, leader_send)))
        leader.start()
        assert leader_started.wait(2)
        follower = threading.Thread(
            target=lambda: results.setdefault("follower", client.request("GET", URL, follower_send)))
        follower.start()
        time.sleep(0.2)
        assert not follower_sends  # leader 在飞，follower 等着
        release.set()
        leader.join(2)
        follower.join(2)

        assert results["leader"].status_code == 200
        assert results["follower"].status_code == 200
        assert len(follower_sends) == 1       # leader 成功后自己发了一次
        assert transport.auth_calls == 1      # 认证只发生一次

    def test_follower_gets_none_when_leader_fails(self):
        transport = FakeTransport()
        client = make_client(transport, wait_seconds=2, data_cooldown_seconds=30)
        leader_started = threading.Event()
        results = {}
        follower_sends = []

        def leader_send(*args, **kwargs):
            leader_started.set()
            time.sleep(0.2)
            raise ConnectionError("refused")

        leader = threading.Thread(
            target=lambda: results.setdefault("leader", client.request("GET", URL, leader_send)))
        leader.start()
        assert leader_started.wait(2)
        follower = threading.Thread(
            target=lambda: results.setdefault(
                "follower", client.request("GET", URL,
                                           lambda *a, **k: follower_sends.append(1) or ok_response())))
        follower.start()
        leader.join(2)
        follower.join(2)

        assert results["leader"] is None
        assert results["follower"] is None
        assert not follower_sends  # leader 失败，follower 不再发

    def test_cooldown_blocks_without_sending(self):
        transport = FakeTransport()
        client = make_client(transport, data_cooldown_seconds=30)
        client.request("GET", URL, lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        sends = []

        assert client.request("GET", URL, lambda *a, **k: sends.append(1)) is None
        assert not sends


class TestExitRotation:
    def test_a_dead_exit_is_rotated_within_the_same_request(self):
        """出口当场死亡不该冷却 30s——换一个新出口重试，同一请求内补上。"""
        transport = FakeTransport(exits=["http://dead:1", "http://live:1"])
        client = make_client(transport, exit_retries=3)
        sends = []

        def send(*args, **kwargs):
            sends.append(kwargs["proxies"]["http"])
            if "dead" in kwargs["proxies"]["http"]:
                raise ConnectionError("exit died")
            return ok_response()

        response = client.request("GET", URL, send)
        assert response is not None
        assert sends == ["http://dead:1", "http://live:1"]  # 死出口后立刻换活的
        assert transport.invalidated == ["http://dead:1"]
        # 没进冷却：下一个请求还能走网关
        state = client._states[("push2his.eastmoney.com", "fflow")]
        assert state.cooldown_until == 0.0

    def test_cooldown_only_after_all_retries_fail(self):
        """连续几个出口都失败才冷却。"""
        transport = FakeTransport(exits=["http://dead:1"])  # 永远给同一个死出口
        client = make_client(transport, exit_retries=3, data_cooldown_seconds=30)
        sends = []

        def send(*args, **kwargs):
            sends.append(1)
            raise ConnectionError("exit died")

        assert client.request("GET", URL, send) is None
        assert len(sends) == 1  # 同一个死出口被 last_failed_proxy 拒收，只发了一次
        state = client._states[("push2his.eastmoney.com", "fflow")]
        assert state.cooldown_until > time.monotonic()


class TestPathFamily:
    def test_kline_and_fflow_have_separate_states(self):
        transport = FakeTransport(exits=["http://proxy-a:1", "http://proxy-b:1"])
        client = make_client(transport, data_cooldown_seconds=30, exit_retries=1)
        # fflow 失败进冷却
        client.request("GET", URL, lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        # kline 不受牵连
        response = client.request("GET", KLINE_URL, lambda *a, **k: ok_response())
        assert response is not None


class TestClientFactory:
    def test_missing_config_returns_none(self, monkeypatch):
        gateway.reset_gateway_client()
        monkeypatch.setattr("finmcp.config.AKSHARE_PROXY_IP", None)
        monkeypatch.setattr("finmcp.config.AKSHARE_PROXY_PASSWORD", None)
        assert gateway.get_gateway_client() is None

    def test_unknown_transport_returns_none(self, monkeypatch):
        gateway.reset_gateway_client()
        monkeypatch.setattr("finmcp.config.AKSHARE_PROXY_IP", "gateway")
        monkeypatch.setattr("finmcp.config.AKSHARE_PROXY_PASSWORD", "token")
        monkeypatch.setattr("finmcp.config.GATEWAY_TRANSPORT", "no_such_thing")
        assert gateway.get_gateway_client() is None
        gateway.reset_gateway_client()


@pytest.fixture(autouse=True)
def _reset_client():
    gateway.reset_gateway_client()
    yield
    gateway.reset_gateway_client()
