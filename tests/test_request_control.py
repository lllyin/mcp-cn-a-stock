"""Offline checks for deadline propagation and cancelled synchronous workers."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from finmcp import request_control as control
from finmcp.datasource import gateway, http_channel as channel


def test_library_180_seconds_and_adapter_retries_cannot_bypass_bounds(monkeypatch):
    original = requests.sessions.Session
    session = original()
    adapter = HTTPAdapter(max_retries=5)
    session.mount('https://', adapter)
    observed = []

    def send(self, request, **kwargs):
        observed.append((self.max_retries.total, kwargs['timeout']))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data":{"price":1}}'
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, 'send', send)
    response = channel._bounded_plain_request(original, session, 'GET',
        'https://push2.eastmoney.com/api/qt/stock/get', {'timeout':180})
    retries, timeout = observed[0]
    assert response.json()['data']['price'] == 1
    assert retries == 0 and adapter.max_retries.total == 5
    assert timeout.connect_timeout <= 8 and timeout.read_timeout <= 8
    assert timeout.total <= 16


def test_gateway_auth_and_retries_share_parent_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(control.time, 'monotonic', lambda: now[0])
    calls = []
    class Transport:
        auth_budget_seconds = 4.5
        def authenticate(self):
            calls.append('auth')
            now[0] += 4
            return gateway.GatewayAuth('http://unused.invalid:1', '', '')
        def invalidate(self, auth): pass
    client = gateway.GatewayClient(Transport(), exit_retries=5)
    def send(method, url, **kwargs):
        calls.append(kwargs['timeout'])
        assert kwargs['timeout'] <= 1
        now[0] += 1
        raise OSError('injected timeout')
    with control.budget_scope(5):
        assert client.request('GET', 'https://push2.eastmoney.com/api/qt/stock/get', send) is None
    assert calls == ['auth', 1]
    assert all(state.cooldown_until == 0 for state in client._states.values())


def test_cancel_interrupts_retry_backoff():
    cancelled = threading.Event()
    done = threading.Event()
    def worker():
        previous = control.set_cancel_event(cancelled)
        try:
            with pytest.raises(control.RequestCancelled): control.sleep(60)
        finally:
            control.set_cancel_event(previous)
            done.set()
    thread = threading.Thread(target=worker)
    thread.start()
    cancelled.set()
    try:
        assert done.wait(.5)
    finally:
        thread.join(1)
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_cancelled_queued_worker_never_starts_the_loader(monkeypatch):
    from finmcp.datasource import cn_stock_source as source
    entered = threading.Event()
    release = threading.Event()
    calls = []
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(source, '_executor', pool)
    monkeypatch.setattr(source, '_get_data_fetch_slots', lambda: slots)
    slots = asyncio.Semaphore(2)
    def first():
        entered.set()
        release.wait(1)
    a = asyncio.create_task(source._run_in_executor(first))
    while not entered.is_set(): await asyncio.sleep(0)
    b = asyncio.create_task(source._run_in_executor(lambda: calls.append('unexpected')))
    await asyncio.sleep(0)
    b.cancel()
    with pytest.raises(asyncio.CancelledError): await b
    release.set()
    await a
    pool.shutdown(wait=True)
    await asyncio.sleep(0)
    assert calls == [] and slots._value == 2


def test_cancelled_kline_does_not_start_another_source(monkeypatch):
    from finmcp.datasource import cn_stock_source as source
    cancelled = threading.Event()
    previous = control.set_cancel_event(cancelled)
    ds = source.CNStockDataSource()
    def primary(*args, **kwargs):
        cancelled.set()
        raise OSError('injected disconnect')
    monkeypatch.setattr(source._KLINE_BREAKER, 'should_skip', lambda: False)
    monkeypatch.setattr(source.ef.stock, 'get_quote_history', primary)
    monkeypatch.setattr(ds, '_fetch_fallback_kline_sync', lambda *args: pytest.fail('cancelled request started another source'))
    try:
        with pytest.raises(control.RequestCancelled):
            ds._fetch_kline_sync('600519', '2026-01-01', '2026-09-29', symbol='SH600519')
    finally:
        control.set_cancel_event(previous)


def test_cancelled_successful_send_keeps_usable_gateway_auth():
    cancelled = threading.Event()
    invalidated = []
    auth = gateway.GatewayAuth('http://unused.invalid:1', '', '')
    transport = SimpleNamespace(authenticate=lambda:auth,
                                invalidate=lambda a:invalidated.append(a))
    client = gateway.GatewayClient(transport)
    def send(*args, **kwargs):
        cancelled.set()
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"data":{"price":1}}'
        return response
    assert client.request('GET', 'https://push2.eastmoney.com/api/qt/stock/get',
                          send, cancel_event=cancelled) is None
    assert invalidated == [] and client._auth == auth


def test_total_timeout_object_keeps_its_tighter_total_limit():
    from urllib3.util import Timeout
    bounded = control.capped_timeout(Timeout(total=3), 8)
    assert bounded.total == 3
    assert control.timeout_seconds(bounded) == 3
    bounded = control.capped_timeout(Timeout(), 8)
    assert bounded.connect_timeout == bounded.read_timeout == 8
    assert bounded.total == 16


def test_budget_expiry_does_not_invalidate_a_healthy_exit():
    auth = gateway.GatewayAuth('http://unused.invalid:1', '', '')
    invalidated = []
    client = gateway.GatewayClient(SimpleNamespace(
        authenticate=lambda:auth, invalidate=lambda a:invalidated.append(a)))
    def send(*a, **kw): raise control.BudgetExceeded('injected local expiry')
    assert client.request('GET', 'https://push2.eastmoney.com/api/qt/stock/get', send) is None
    assert invalidated == [] and client._auth == auth
    assert all(state.cooldown_until == 0 for state in client._states.values())
