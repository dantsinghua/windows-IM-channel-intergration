"""Fake consumer lifecycle only: never create real hosts, queues, or native I/O."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from qtrade_agent.ossbox.institution import InstitutionCollectorHost


def row(aid, uid='10001', state='running'):
    return {'id':aid,'self_uid':uid,'state':state,'channel':'qidian'}


class FakeHost:
    def __init__(self, aid, events, *, stop_fails=False, gate=None):
        self.aid, self.events, self.stop_fails, self.gate = aid, events, stop_fails, gate
        self.ledger = SimpleNamespace(binding=SimpleNamespace(channel='qidian'))
    async def start(self): self.events.append(('start',self.aid))
    async def stop(self):
        self.events.append(('stop-begin',self.aid))
        if self.stop_fails: raise RuntimeError('synthetic-private-stop-detail')
        if self.gate is not None: await self.gate.wait()
        self.events.append(('stop-done',self.aid))


def collector(rows, existing, events):
    value = object.__new__(InstitutionCollectorHost)
    value.rows = lambda: rows
    value.active = True
    value.presence_provider = None
    value.presence = {}
    value.clock = lambda: 1800000000
    value.hosts = dict(existing)
    value.histories = {pair:object() for pair in existing}
    value.errors = {}
    value.publisher = SimpleNamespace(request_sync=lambda *args: events.append(('sync',args[0])))
    value._make_host = lambda r: FakeHost(r['id'],events)
    return value


def test_old_consumer_remains_tracked_until_stop_finishes_before_replacement():
    async def run():
        events, rows = [], [row('old',state='stopped'),row('new'),row('other',uid='20002')]
        original = deepcopy(rows)
        gate = asyncio.Event()
        old, other = FakeHost('old',events,gate=gate), FakeHost('other',events)
        value = collector(rows,{('old','10001'):old,('other','20002'):other},events)
        task = asyncio.create_task(value.reconcile_accounts())
        await asyncio.sleep(0)
        assert ('stop-begin','old') in events and ('start','new') not in events
        assert value.hosts[('old','10001')] is old and ('old','10001') in value.histories
        gate.set()
        await task
        assert events.index(('stop-done','old')) < events.index(('start','new'))
        assert ('old','10001') not in value.hosts and ('old','10001') not in value.histories
        assert value.hosts[('other','20002')] is other and ('stop-begin','other') not in events
        assert rows == original
    asyncio.run(run())


@pytest.mark.parametrize('old_row_present',[True,False])
def test_stop_failure_keeps_tracking_blocks_same_uid_and_allows_other_uid(old_row_present):
    async def run():
        events = []
        rows = ([row('old',state='stopped')] if old_row_present else []) + [row('new'),row('other',uid='20002')]
        old = FakeHost('old',events,stop_fails=True)
        value = collector(rows,{('old','10001'):old},events)
        history = value.histories[('old','10001')]
        await value.reconcile_accounts()
        assert value.hosts[('old','10001')] is old and value.histories[('old','10001')] is history
        assert ('new','10001') not in value.hosts and ('start','new') not in events
        assert ('other','20002') in value.hosts
        assert value.errors['old'] == 'RuntimeError'
        old.stop_fails = False
        await value.reconcile_accounts()
        assert ('old','10001') not in value.hosts and ('new','10001') in value.hosts
        assert 'old' not in value.errors
    asyncio.run(run())


def test_multiple_active_sources_retire_all_consumers_and_preserve_other_uid():
    async def run():
        events = []
        rows = [row('a'),row('b',state='degraded'),row('other',uid='20002')]
        existing = {(r['id'],r['self_uid']):FakeHost(r['id'],events) for r in rows}
        value = collector(rows,existing,events)
        await value.reconcile_accounts()
        assert set(value.hosts) == {('other','20002')}
        assert ('stop-done','a') in events and ('stop-done','b') in events
        assert not any(kind=='start' for kind,aid in events)
    asyncio.run(run())


def test_all_stopped_sources_retire_without_deleting_source_rows():
    async def run():
        events, rows = [], [row('old',state='stopped')]
        value = collector(rows,{('old','10001'):FakeHost('old',events)},events)
        await value.reconcile_accounts()
        assert value.hosts == {} and value.histories == {}
        assert rows == [row('old',state='stopped')]
    asyncio.run(run())
