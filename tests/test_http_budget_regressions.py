"""Real requests dispatch/redirects; upstream responses and gateways stay offline."""
import io
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

from finmcp import request_control as control
from finmcp.datasource import gateway, http_channel as channel

URL = 'https://push2.eastmoney.com/api/qt/stock/get'
PROXY = 'http://fixture.invalid:47000'


@pytest.mark.parametrize('proxied', [False, True])
def test_repeated_sends_borrow_owner_pools_and_close_them_only_with_owner(monkeypatch, proxied):
    session = requests.sessions.Session()
    session.trust_env = False
    owner = session.adapters['https://']
    owner.max_retries = requests.adapters.Retry(total=5)
    pool_clear = Mock(wraps=owner.poolmanager.clear)
    monkeypatch.setattr(owner.poolmanager, 'clear', pool_clear)
    proxy_manager = owner.proxy_manager_for(PROXY) if proxied else None
    proxy_clear = Mock(wraps=proxy_manager.clear) if proxied else None
    if proxied:
        monkeypatch.setattr(proxy_manager, 'clear', proxy_clear)
    # Copying HTTPAdapter invokes __setstate__/init_poolmanager. Count accidental
    # pool construction as well as the identity of the pools actually used.
    pool_initialisations = []
    initialise = HTTPAdapter.init_poolmanager
    def initialise_spy(self, *args, **kwargs):
        pool_initialisations.append(self)
        initialise(self, *args, **kwargs)
    monkeypatch.setattr(HTTPAdapter, 'init_poolmanager', initialise_spy)
    sent = []
    def urlopen(pool, method, url, **kwargs):
        sent.append((pool, kwargs['retries'].total))
        return HTTPResponse(body=io.BytesIO(b'{"data":{"value":1}}'),
                            status=200, preload_content=False)
    monkeypatch.setattr(HTTPSConnectionPool, 'urlopen', urlopen)
    try:
        for _ in range(3):
            kwargs = {'timeout': 8}
            if proxied:
                kwargs['proxies'] = {'https': PROXY}
            response = channel._bounded_plain_request(
                requests.sessions.Session, session, 'GET', URL, kwargs)
            assert response.json() == {'data': {'value': 1}}
            response.close()
        assert len(sent) == 3
        assert len({id(pool) for pool, _ in sent}) == 1
        assert [retries for _, retries in sent] == [0, 0, 0]
        assert owner.max_retries.total == 5
        assert pool_initialisations == []
        assert pool_clear.call_count == 0
        if proxied:
            assert owner.proxy_manager[PROXY] is proxy_manager
            assert proxy_clear.call_count == 0
        # Closing a request's facade cannot close the Session's shared pools.
        with control.budget_scope(16) as budget:
            view = channel._BudgetAdapter(owner, budget)
            assert view.adapter.poolmanager is owner.poolmanager
            assert view.adapter.proxy_manager is owner.proxy_manager
            view.close()
        assert pool_clear.call_count == 0
        if proxied:
            assert proxy_clear.call_count == 0
    finally:
        session.close()
    assert pool_clear.call_count == 1
    if proxied:
        assert proxy_clear.call_count == 1


@pytest.mark.parametrize('calls', [4, 20, 40])
def test_parallel_sends_keep_pools_retry_configuration_and_payload(monkeypatch, calls):
    from concurrent.futures import ThreadPoolExecutor
    session = requests.sessions.Session()
    session.trust_env = False
    owner = session.adapters['https://']
    owner.max_retries = requests.adapters.Retry(total=5)
    seen = []
    def send(adapter, request, **kwargs):
        seen.append((adapter.poolmanager, adapter.max_retries.total))
        response = requests.Response()
        response.status_code = 200
        response._content = request.url.encode()
        response.request = request
        return response
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    def bounded(index):
        with control.budget_scope(100):
            return channel._bounded_plain_request(requests.sessions.Session,
                session, 'GET', URL, {'params': {'symbol': index}, 'timeout': 8}).content
    try:
        # Two stable native baselines, with the same URL/response fixtures.
        baseline = [session.get(URL, params={'symbol': i}).content for i in range(calls)]
        assert baseline == [session.get(URL, params={'symbol': i}).content for i in range(calls)]
        seen.clear()
        with ThreadPoolExecutor(max_workers=4) as executor:
            assert list(executor.map(bounded, range(calls))) == baseline
        assert len(seen) == calls
        assert all(pool is owner.poolmanager and retries == 0 for pool, retries in seen)
        assert owner.max_retries.total == 5
    finally:
        session.close()


@pytest.fixture
def redirect_chain(monkeypatch):
    clock = [0.0]
    calls = []
    sends = []
    auth_notes = []
    cancel = threading.Event()
    state = {'failure': 'redirect'}
    def response(request=None, status=200):
        result = requests.Response()
        result.status_code = status
        result._content = b'{"data":{"value":1}}'
        result.request = request
        result.url = request.url if request is not None else URL
        return result
    def send(adapter, request, **kwargs):
        sends.append(kwargs['timeout'].total)
        if state['failure'] == 'read_timeout':
            raise requests.exceptions.ReadTimeout('injected read timeout')
        # Transport finishes within its allowance; redirect processing can use
        # the remainder. The next real Session.send then detects the expiry.
        clock[0] += 7
        result = response(request, 302)
        result.headers['Location'] = URL + '?redirect=' + str(len(sends))
        def release():
            clock[0] += 2
        result.raw = SimpleNamespace(release_conn=release, close=lambda: None)
        if state['failure'] == 'cancel':
            cancel.set()
        return result
    def paid_send(*args, **kwargs):
        calls.append(control.current_budget().remaining())
        return response()
    monkeypatch.setattr(control.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    monkeypatch.setattr(gateway, 'get_gateway_client', lambda: SimpleNamespace(request=paid_send))
    monkeypatch.setattr(channel, '_auto_proxy', True)
    monkeypatch.setattr(channel, '_auto_proxy_gateway', 'fixture.invalid')
    monkeypatch.setattr(channel, '_auto_proxy_token', 'fixture')
    monkeypatch.setattr(channel, '_auto_proxy_states', {})
    monkeypatch.setattr(channel, 'AUTO_PROXY_AFTER_FAILURES', 2)
    monkeypatch.setattr(channel, '_note_auth_outcome', lambda *a, **kw: auth_notes.append(kw))
    channel._auto_proxy_state('push2.eastmoney.com')['active'] = True
    def run(seconds=100):
        with requests.sessions.Session() as session:
            session.trust_env = False
            with control.budget_scope(seconds, cancel_event=cancel):
                return channel._plain_then_gateway(requests.sessions.Session,
                    session, 'GET', URL, {'timeout': 8}, track_auth=True)
    return SimpleNamespace(run=run, clock=clock, calls=calls, sends=sends,
                           auth_notes=auth_notes, state=state)


def test_plain_stage_expiry_uses_gateway_with_remaining_parent_budget(redirect_chain):
    case = redirect_chain
    assert case.run().status_code == 200
    assert case.sends == [16, 7]
    assert case.calls == [100 - case.clock[0]]
    assert case.calls[0] > 0
    # Stage expiry is not evidence that the cookies are bad.
    assert case.auth_notes == []


def test_expired_parent_never_starts_gateway(redirect_chain):
    with pytest.raises(control.BudgetExceeded):
        redirect_chain.run(16)
    assert redirect_chain.calls == []
    assert redirect_chain.auth_notes == []


def test_cancelled_redirect_chain_never_starts_gateway(redirect_chain):
    redirect_chain.state['failure'] = 'cancel'
    with pytest.raises(control.RequestCancelled):
        redirect_chain.run()
    assert redirect_chain.calls == []
    assert redirect_chain.auth_notes == []


@pytest.mark.parametrize('gate', ['disabled', 'inactive', 'fflow_in_chain'])
def test_stage_expiry_preserves_gateway_eligibility(monkeypatch, redirect_chain, gate):
    if gate == 'disabled':
        monkeypatch.setattr(channel, '_auto_proxy', False)
    elif gate == 'inactive':
        channel._auto_proxy_state('push2.eastmoney.com')['active'] = False
    else:
        monkeypatch.setattr(gateway, 'path_family', lambda url: 'fflow')
        monkeypatch.setattr(channel, '_fflow_gateway_in_chain_cached', lambda: True)
    with pytest.raises(control.BudgetExceeded):
        redirect_chain.run()
    assert redirect_chain.calls == []


def test_ordinary_timeout_still_falls_back(redirect_chain):
    redirect_chain.state['failure'] = 'read_timeout'
    assert redirect_chain.run().status_code == 200
    assert len(redirect_chain.calls) == 1
