"""Cancellation ownership and nested gateway budgets; all upstream I/O is fake."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter

from finmcp import request_control as control
from finmcp.datasource import cn_stock_source as source, gateway, http_channel as channel


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_who', ['none', 'leader', 'follower'])
async def test_fund_flow_shared_tail_cancel_isolation(monkeypatch, cancel_who):
    data_source = source.CNStockDataSource()
    entered = asyncio.Event()
    release = asyncio.Event()
    follower_joined = asyncio.Event()
    attempts = []
    async def inner(*args):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            entered.set()
            await release.wait()
        return {'fixture': 'complete'}, '0'
    def wait_budget(request_id):
        follower_joined.set()
        return 3.0
    monkeypatch.setattr(data_source, '_run_fund_flow_tail_inner', inner)
    monkeypatch.setattr(source, '_fund_flow_page_wait_budget', wait_budget)
    monkeypatch.setattr(source, '_fund_flow_needs_more', lambda *args: False)
    async def call():
        return await data_source._fetch_fund_flow_tail(
            '600519', 'SH600519', None, None, (), None, None)
    leader = asyncio.create_task(call())
    await asyncio.wait_for(entered.wait(), 1)
    follower = asyncio.create_task(call())
    await asyncio.wait_for(follower_joined.wait(), 1)
    if cancel_who != 'none':
        (leader if cancel_who == 'leader' else follower).cancel()
        # Let cancellation propagate through the real tail wrapper before the
        # fake provider is allowed to complete.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(leader, follower, return_exceptions=True), 1)
    evidence = {'cancel_who': cancel_who,
                'results': [type(r).__name__ for r in results],
                'cancelling': [leader.cancelling(), follower.cancelling()],
                'attempts': len(attempts)}
    assert len(attempts) == 1, evidence
    assert not source._get_fund_flow_tail_inflight()
    if cancel_who == 'leader':
        assert isinstance(results[0], asyncio.CancelledError)
        assert not isinstance(results[1], BaseException), evidence
    elif cancel_who == 'follower':
        assert not isinstance(results[0], BaseException), evidence
        assert isinstance(results[1], asyncio.CancelledError)
    else:
        assert all(not isinstance(r, BaseException) for r in results), evidence


@pytest.mark.parametrize('failure', ['redirect_stage_budget', 'read_timeout', 'parent_expired', 'cancelled'])
def test_gateway_send_sub_budget_keeps_remaining_exit_attempts(monkeypatch, failure):
    clock = [0.0]
    attempts = []
    sends = []
    cancelled = threading.Event()
    invalidated = []
    url = 'https://push2.eastmoney.com/api/qt/stock/get'
    class FakeAuth:
        auth_budget_seconds = 0.0
        calls = 0
        def authenticate(self):
            self.calls += 1
            return gateway.GatewayAuth(proxy=f'http://fixture-{self.calls}.invalid:47000', cookie='', user_agent='fixture')
        def invalidate(self, auth):
            invalidated.append(auth)
    transport = FakeAuth()
    client = gateway.GatewayClient(transport, exit_retries=3,
        wait_seconds=0, auth_retry_backoff_seconds=0)
    monkeypatch.setattr(control.time, 'monotonic', lambda: clock[0])
    def adapter_send(adapter, request, **kwargs):
        sends.append(kwargs['timeout'].total)
        if len(attempts) == 1:
            if failure == 'read_timeout':
                raise requests.exceptions.ReadTimeout('offline injected failure')
            clock[0] += 7
            if failure == 'cancelled':
                cancelled.set()
            response = requests.Response()
            response.status_code = 302
            response._content = b''
            response.url = request.url
            response.request = request
            response.headers['Location'] = url + '?redirect=' + str(len(sends))
            def release_conn():
                clock[0] += 2
            response.raw = SimpleNamespace(release_conn=release_conn, close=lambda: None)
            return response
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"rc":0,"data":{"value":1}}'
        response.headers['Content-Type'] = 'application/json'
        response.url = request.url
        response.request = request
        return response
    monkeypatch.setattr(HTTPAdapter, 'send', adapter_send)
    def send(method, url, **kwargs):
        attempts.append(control.current_budget().remaining())
        with requests.sessions.Session() as session:
            session.trust_env = False
            return channel._bounded_plain_request(
                requests.sessions.Session, session, method, url, kwargs)
    with control.budget_scope(16 if failure == 'parent_expired' else 100):
        response = client.request('GET', url, send, timeout=8, cancel_event=cancelled)
    evidence = {'failure': failure, 'configured_attempts': 3,
                'gateway_attempts': len(attempts), 'adapter_sends': len(sends),
                'elapsed': clock[0],
                'gateway_budget_remaining': client.leader_budget(8) - clock[0],
                'response': response.status_code if response is not None else None}
    if failure in {'parent_expired', 'cancelled'}:
        assert response is None and len(attempts) == 1, evidence
    else:
        assert response is not None and response.status_code == 200, evidence
        assert len(attempts) == 2 and attempts[1] <= attempts[0], evidence
    if failure == 'read_timeout':
        assert len(invalidated) == 1 and transport.calls == 2
    else:
        assert invalidated == [] and transport.calls == 1
    assert all(state.cooldown_until == 0 for state in client._states.values())


@pytest.mark.asyncio
async def test_one_client_disconnect_must_not_cancel_another_batch(monkeypatch):
    import importlib
    app = importlib.import_module('finmcp.mcp_app')
    data_source = source.CNStockDataSource()
    leader_entered = asyncio.Event()
    follower_joined = asyncio.Event()
    sibling_entered = asyncio.Event()
    finish = asyncio.Event()
    disconnect_a = asyncio.Event()
    disconnect_b = asyncio.Event()
    sibling_cancelled = []
    async def inner(*args):
        leader_entered.set()
        await finish.wait()
        return {'fixture': 'complete'}, '0'
    def wait_budget(request_id):
        follower_joined.set()
        return 3.0
    async def load(symbol, *args, **kwargs):
        if symbol == 'SH600519':
            await data_source._fetch_fund_flow_tail(
                '600519', symbol, None, None, (), None, None)
        else:
            sibling_entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                sibling_cancelled.append(symbol)
                raise
        return {'SYMBOL': symbol}
    async def render(buf, symbol, raw_data, **kwargs):
        buf.write('flow:' + symbol)
    monkeypatch.setattr(app.research, 'build_basic_data', lambda *a: None)
    monkeypatch.setattr(app.research, 'build_trading_data', render)
    monkeypatch.setattr(app, '_get_batch_query_admission', lambda: admission)
    admission = app.BatchQueryAdmission(2)
    monkeypatch.setattr(app.research, 'load_raw_data', load)
    monkeypatch.setattr(app.research, 'is_realtime_fund_flow_window', lambda *a: False)
    monkeypatch.setattr(data_source, '_run_fund_flow_tail_inner', inner)
    monkeypatch.setattr(source, '_fund_flow_page_wait_budget', wait_budget)
    monkeypatch.setattr(source, '_fund_flow_needs_more', lambda *a: False)
    first = asyncio.create_task(app.fetch_batch_reports('SH600519', 'brief', '',
        request_id='offline-client-a', client_disconnect_event=disconnect_a))
    await asyncio.wait_for(leader_entered.wait(), 1)
    second = asyncio.create_task(app.fetch_batch_reports('SH600519,SH600036', 'brief', '',
        request_id='offline-client-b', client_disconnect_event=disconnect_b))
    await asyncio.wait_for(follower_joined.wait(), 1)
    await asyncio.wait_for(sibling_entered.wait(), 1)
    disconnect_a.set()
    await asyncio.sleep(0.03)
    finish.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
    evidence = {'clients_disconnected': [disconnect_a.is_set(), disconnect_b.is_set()],
                'batch_results': [type(r).__name__ for r in results],
                'other_batch_siblings_cancelled': sibling_cancelled}
    assert isinstance(results[0], asyncio.CancelledError)
    assert not isinstance(results[1], BaseException), evidence

    assert results[1].errors == {}
    assert results[1].reports == {s: 'flow:' + s for s in ('SH600519', 'SH600036')}
    assert sibling_cancelled == []
    assert admission.active == admission.waiting == 0


@pytest.mark.asyncio
async def test_follower_timeout_preserves_leader_and_runs_independent_fallback(monkeypatch):
    datasource = source.CNStockDataSource()
    entered, finish = asyncio.Event(), asyncio.Event()
    attempts = []

    async def inner(*args):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            await finish.wait()
        return {'fixture': 'complete'}, '0'

    monkeypatch.setattr(datasource, '_run_fund_flow_tail_inner', inner)
    monkeypatch.setattr(source, '_fund_flow_page_wait_budget', lambda _: 0.1)

    def call():
        return datasource._fetch_fund_flow_tail('600519', 'SH600519', None, None, (), None, None)

    leader = asyncio.create_task(call())
    await asyncio.wait_for(entered.wait(), 1)
    result = await asyncio.wait_for(call(), 1)
    assert result == ({'fixture': 'complete'}, '0', 'leader')
    assert len(attempts) == 2 and not leader.done()
    finish.set()
    assert await leader == result
    assert not source._get_fund_flow_tail_inflight()


@pytest.mark.asyncio
@pytest.mark.parametrize('consumers', [1, 2])
async def test_last_consumer_cancels_tail_and_old_cleanup_cannot_remove_new_flight(monkeypatch, consumers):
    datasource = source.CNStockDataSource()
    entered, joined, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup, fresh_entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []

    async def inner(*args):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopping.set()
                await cleanup.wait()
                raise
        fresh_entered.set()
        await finish.wait()
        return {'fixture': 'fresh'}, '0'

    def wait_budget(request_id):
        joined.set()
        return 3.0

    monkeypatch.setattr(datasource, '_run_fund_flow_tail_inner', inner)
    monkeypatch.setattr(source, '_fund_flow_page_wait_budget', wait_budget)

    def call():
        return datasource._fetch_fund_flow_tail('600519', 'SH600519', None, None, (), None, None)

    clients = [asyncio.create_task(call())]
    await asyncio.wait_for(entered.wait(), 1)
    registry = source._get_fund_flow_tail_inflight()
    old = registry['SH600519']
    if consumers == 2:
        clients.append(asyncio.create_task(call()))
        await asyncio.wait_for(joined.wait(), 1)
    for client in clients:
        client.cancel()
    results = await asyncio.gather(*clients, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    await asyncio.wait_for(stopping.wait(), 1)
    assert not registry

    # Old task is still closing resources; a new request must get a fresh task.
    fresh_client = asyncio.create_task(call())
    await asyncio.wait_for(fresh_entered.wait(), 1)
    fresh = registry['SH600519']
    assert fresh is not old
    cleanup.set()
    await asyncio.gather(old.task, return_exceptions=True)
    assert registry['SH600519'] is fresh
    finish.set()
    assert await fresh_client == ({'fixture': 'fresh'}, '0', 'leader')
    assert not registry and len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_follower', [False, True])
async def test_shared_task_cancellation_is_distinct_from_own_cancellation(monkeypatch, cancel_follower):
    datasource = source.CNStockDataSource()
    entered, joined = asyncio.Event(), asyncio.Event()
    attempts = []

    async def inner(*args):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            await asyncio.Event().wait()
        return {'fixture': 'recovered'}, '0'

    def wait_budget(request_id):
        joined.set()
        return 3.0

    monkeypatch.setattr(datasource, '_run_fund_flow_tail_inner', inner)
    monkeypatch.setattr(source, '_fund_flow_page_wait_budget', wait_budget)

    def call():
        return datasource._fetch_fund_flow_tail('600519', 'SH600519', None, None, (), None, None)

    leader = asyncio.create_task(call())
    await asyncio.wait_for(entered.wait(), 1)
    follower = asyncio.create_task(call())
    await asyncio.wait_for(joined.wait(), 1)
    if cancel_follower:
        follower.cancel()
    source._get_fund_flow_tail_inflight()['SH600519'].task.cancel()
    results = await asyncio.gather(leader, follower, return_exceptions=True)
    assert results[0] == ({'fixture': 'recovered'}, '0', 'leader')
    if cancel_follower:
        assert isinstance(results[1], asyncio.CancelledError)
        assert len(attempts) == 2  # 已取消的客户端不能再回源。
    else:
        assert results[1] == ({'fixture': 'recovered'}, '0', 'leader')
        assert len(attempts) == 3
