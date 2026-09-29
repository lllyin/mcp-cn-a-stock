"""Demand-aware page completion, using the saved HTML and no browser/network."""
import asyncio
import contextlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from finmcp.datasource import realtime_ff as ff
from finmcp.datasource.fund_flow_page import TODAY_FIELDS, parse_fund_flow_page

FULL = (Path(__file__).parent / 'fixtures/eastmoney_zjlx_full_300408.html').read_text()


@pytest.fixture
def page_source(monkeypatch):
    soup = BeautifulSoup(FULL, 'html.parser')
    soup.find(id='table_ls').decompose()
    early_html = str(soup)
    today = asyncio.Event()
    history = asyncio.Event()
    closed = asyncio.Event()
    opened = []
    xhr = []

    class Page:
        context = None
        def on(self, *args): pass
        def remove_listener(self, *args): pass
        async def goto(self, *args, **kwargs): pass
        async def route(self, *args): pass
        async def content(self): return FULL if history.is_set() else state['early_html']
        async def close(self): closed.set()

    class Context:
        async def new_page(self):
            opened.append(1)
            return Page()

    @contextlib.asynccontextmanager
    async def lease():
        yield Context()

    async def today_wait(*args): await today.wait()
    async def history_wait(*args): await history.wait()
    async def noop(*args): pass
    async def history_xhr(*args):
        xhr.append(1)
        return []

    state = dict(early_html=early_html, today=today, history=history,
                 closed=closed, opened=opened, xhr=xhr)
    monkeypatch.setattr(ff, 'browser_lease', lease)
    monkeypatch.setattr(ff, 'disguise_page', noop)
    monkeypatch.setattr(ff, '_wait_for_today', today_wait)
    monkeypatch.setattr(ff, '_wait_for_history', history_wait)
    monkeypatch.setattr(ff, '_fetch_history_in_page', history_xhr)
    monkeypatch.setattr(ff.eastmoney_auth, 'remember_from_context', noop)
    monkeypatch.setattr(ff, 'BROWSER_KEEP_PAGES', False)
    monkeypatch.setattr(ff, 'FUND_FLOW_PAGE_MAX_LOADS', 1)
    monkeypatch.setattr(ff, 'SEMAPHORE', asyncio.Semaphore(3))
    monkeypatch.setattr(ff, '_PAGE_BREAKER', SimpleNamespace(
        should_skip=lambda:False, record=lambda **kw:None))
    for name in ('_page_inflight', '_page_inflight_waiters', '_page_progress', '_page_cache'):
        monkeypatch.setattr(ff, name, {})
    return state


@pytest.mark.asyncio
async def test_today_returns_before_history_and_closes_unused_tab(page_source, caplog):
    s = page_source
    s['today'].set()
    page = await asyncio.wait_for(ff.fetch_page_shared('300408', require_today=True), .5)
    await asyncio.wait_for(s['closed'].wait(), .5)
    assert page.has_complete_today and not page.history
    assert not s['history'].is_set()
    assert s['xhr'] == [] and s['opened'] == [1]
    loads = [r.message for r in caplog.records if 'Realtime fund flow page ' in r.message]
    assert len(loads) == 1 and 'outcome=today=True' in loads[0]
    assert ff._page_inflight == {} and ff._page_progress == {}
    expected = parse_fund_flow_page(FULL)
    assert ff._page_to_realtime_dict('300408', page) == ff._page_to_realtime_dict('300408', expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['today', 'history'])
async def test_today_and_history_share_one_tab_but_finish_independently(page_source, first):
    s = page_source
    def spawn(kind):
        return asyncio.create_task(ff.fetch_page_shared(
            'SZ300408' if kind == 'history' else '300408',
            require_history=kind == 'history', require_today=kind == 'today'))
    a = spawn(first)
    await asyncio.sleep(0)
    b = spawn('history' if first == 'today' else 'today')
    brief, full = (a, b) if first == 'today' else (b, a)
    s['today'].set()
    early = await asyncio.wait_for(brief, .5)
    assert early.has_complete_today
    assert not full.done() and not s['closed'].is_set()
    s['history'].set()
    final = await asyncio.wait_for(full, .5)
    assert final.history_records() == parse_fund_flow_page(FULL).history_records()
    assert len(final.history) == 121 and s['opened'] == [1]
    assert s['closed'].is_set() and s['xhr'] == []
    assert len(ff._page_cache['300408'][1].history) == 121


@pytest.mark.asyncio
async def test_partial_today_waits_for_remaining_fields(page_source):
    s = page_source
    soup = BeautifulSoup(s['early_html'], 'html.parser')
    soup.find('td', attrs={'data-field':'f81'}).string = '--'
    s['early_html'] = str(soup)
    s['today'].set()
    brief = asyncio.create_task(ff.fetch_page_shared('300408', require_today=True))
    for _ in range(8): await asyncio.sleep(0)
    assert not brief.done()
    s['history'].set()
    page = await asyncio.wait_for(brief, .5)
    assert page.has_complete_today
    assert ff._page_to_realtime_dict('300408', page) == ff._page_to_realtime_dict('300408', parse_fund_flow_page(FULL))


@pytest.mark.asyncio
async def test_cancelled_today_waiter_does_not_cancel_full(page_source):
    s = page_source
    brief = asyncio.create_task(ff.fetch_page_shared('300408', require_today=True))
    full = asyncio.create_task(ff.fetch_page_shared('300408', require_history=True))
    for _ in range(4): await asyncio.sleep(0)
    brief.cancel()
    with pytest.raises(asyncio.CancelledError): await brief
    s['today'].set()
    s['history'].set()
    page = await asyncio.wait_for(full, .5)
    assert len(page.history) == 121 and s['opened'] == [1]


@pytest.mark.asyncio
async def test_last_consumer_cancel_reclaims_tab_and_waiters(page_source):
    s = page_source
    full = asyncio.create_task(ff.fetch_page_shared('300408', require_history=True))
    for _ in range(5): await asyncio.sleep(0)
    full.cancel()
    with pytest.raises(asyncio.CancelledError): await full
    await asyncio.wait_for(s['closed'].wait(), .5)
    assert ff._page_inflight == {} and ff._page_progress == {}
    assert ff.SEMAPHORE._value == 3


def test_ready_script_covers_the_same_fields_as_parser():
    assert set(re.findall(r'f\d+', ff.WAIT_FOR_DATA_JS)) == set(TODAY_FIELDS)


@pytest.mark.asyncio
async def test_history_arriving_during_tab_close_gets_its_own_attempt(page_source, monkeypatch):
    """The join-after-early-completion race must not silently lose full history."""
    s = page_source
    committed = asyncio.Event()
    finish = asyncio.Event()
    calls = []
    async def fake_load(symbol, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            progress = ff._active_page_progress.get()
            page = parse_fund_flow_page(s['early_html'])
            progress.today.set_result(page)
            progress.today_only_complete = True
            committed.set()
            await finish.wait()
            return page
        return parse_fund_flow_page(FULL)
    monkeypatch.setattr(ff, '_load_page_shared', fake_load)
    brief = asyncio.create_task(ff.fetch_page_shared('300408', require_today=True))
    await committed.wait()
    full = asyncio.create_task(ff.fetch_page_shared('300408', require_history=True))
    await asyncio.sleep(0)
    finish.set()
    assert (await brief).has_complete_today
    assert len((await asyncio.wait_for(full, .5)).history) == 121
    assert len(calls) == 2 and calls[-1]['require_history']


@pytest.mark.asyncio
async def test_today_fallback_still_loads_when_realtime_breaker_is_open(page_source, monkeypatch):
    s = page_source
    monkeypatch.setattr(ff._PAGE_BREAKER, 'should_skip', lambda: True)
    with pytest.raises(ff.FundFlowPageRefused):
        await ff.fetch_page_shared('300408', require_today=True)
    assert not s['opened']
    s['today'].set()
    page = await ff.fetch_page_shared('300408', require_today=True, bypass_breaker=True)
    assert page.has_complete_today and s['opened'] == [1]


@pytest.mark.asyncio
async def test_early_dom_history_is_not_published_as_ready_history(page_source):
    s = page_source
    # The rows are in the DOM, but the independent history readiness signal has
    # not arrived. This also covers a partially populated same-day row.
    s['early_html'] = FULL
    s['today'].set()
    today = await ff.fetch_page_shared('300408', require_today=True)
    assert today.has_complete_today and today.history == []
    assert ff._cached_page('300408', require_history=True, require_today=False) is None
    full = asyncio.create_task(ff.fetch_page_shared('300408', require_history=True))
    for _ in range(5): await asyncio.sleep(0)
    assert not full.done()
    s['history'].set()
    assert len((await full).history) == 121
