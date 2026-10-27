"""Confirmation capture must carry its own Account, never regular-poll globals."""
import pytest

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.models import Command
from tests.test_bus import FakeSender,PEER
from tests.test_integration_wiring_common import make_agent


async def test_bus_confirmation_reaches_injected_capture_factory_with_its_actual_account_and_serial(tmp_path,clock,maindb,monkeypatch):
    agent,_fakes=make_agent(tmp_path,clock=clock)
    try:
        clock.auto_step_ms=200
        agent.store.ensure_account('qd01','qidian',state='running',self_uid=maindb.self_uin)
        seen=[]
        def factory(uid,acct):
            seen.append((uid,acct))
            return LocalSqliteMainDb(maindb.path)
        agent._maindb_factory=factory
        adapter=agent.adapters['qidian'];adapter._sender=FakeSender(maindb,clock,latency_ms=1)
        original_load=agent.bus._load_account
        def account(account_id):
            value=original_load(account_id)
            if value is not None:value.extra['adb_serial']='synthetic-confirm-serial-'+account_id
            return value
        monkeypatch.setattr(agent.bus,'_load_account',account)
        # Seed only the normal DB bootstrap/table map; never call poll_all or
        # establish any shared current-account context for confirmation.
        maindb.insert_text(PEER,'synthetic history',time_s=clock.now_s-3600)
        agent.poller.poll_maindb(account('qd01'))
        seen.clear();polls=[];original_poll=adapter.poll
        async def capture(acct,*,only_sessions=None):
            polls.append(acct)
            return await original_poll(acct,only_sessions=only_sessions)
        monkeypatch.setattr(adapter,'poll',capture)
        result=await agent.bus.submit(Command('qd01','send_text',
            {'session':'qd01:'+PEER,'text':'SYNTHETIC_ACCOUNT_CAPTURE'},idempotency_key='capture-account-proof'))
        assert result.code=='DELIVERED' and polls and seen
        assert all(uid==maindb.self_uin and acct is polls[index]
                   for index,(uid,acct) in enumerate(seen)), 'Capture must receive the Account from this confirmation invocation'
        assert all(acct.extra['adb_serial']=='synthetic-confirm-serial-qd01' for _uid,acct in seen)
        assert len(adapter._sender.calls)==1
    finally:
        await agent.bus.close()
        agent.store.close()


@pytest.mark.parametrize('conflict', ['account_uid', 'stored_uid'])
async def test_capture_known_uid_conflict_never_reaches_injected_factory(tmp_path, clock, conflict):
    from qtrade_agent.adapters.base import Account
    agent, _ = make_agent(tmp_path, clock=clock)
    try:
        uid = '3000000011'
        agent.store.ensure_account('qd01', 'qidian', state='running', self_uid=uid if conflict == 'account_uid' else '3000000022')
        acct = Account('qd01', 'qidian', 'running', self_uid='3000000022' if conflict == 'account_uid' else uid)
        calls = []
        agent._maindb_factory = lambda uid, acct: calls.append((uid, acct))
        with pytest.raises(ValueError, match='Capture UID conflicts'):
            agent._maindb_for_uid(uid, acct)
        assert calls == []
    finally:
        await agent.bus.close()
        agent.store.close()


async def test_native_uid_can_select_capture_while_cached_account_uid_is_empty(tmp_path, clock):
    from qtrade_agent.adapters.base import Account
    agent, _ = make_agent(tmp_path, clock=clock)
    try:
        agent.store.ensure_account('qd01', 'qidian', state='running')
        acct = Account('qd01', 'qidian', 'running', self_uid=None, extra={'adb_serial': 'synthetic-bootstrap-serial'})
        sentinel = object(); calls = []
        def factory(uid, received):
            calls.append((uid, received))
            return sentinel
        agent._maindb_factory = factory
        assert agent._maindb_for_uid('3000000011', acct) is sentinel
        assert calls == [('3000000011', acct)]
        assert agent.store.get_account_full('qd01')['self_uid'] is None
        assert acct.self_uid is None
    finally:
        await agent.bus.close()
        agent.store.close()


async def test_concurrent_poll_and_gap_capture_keep_each_explicit_account(tmp_path, clock):
    import asyncio
    from qtrade_agent.adapters.base import Account
    from tests.conftest import FakeMainDb
    agent, _ = make_agent(tmp_path, clock=clock)
    databases = {}
    seen = []
    accounts = [Account('qd01', 'qidian', 'running', self_uid='3000000011', extra={'adb_serial': 'synthetic-serial-one'}),
                Account('qd02', 'qidian', 'running', self_uid='3000000022', extra={'adb_serial': 'synthetic-serial-two'})]
    try:
        for acct in accounts:
            agent.store.ensure_account(acct.id, 'qidian', state='running', self_uid=acct.self_uid)
            db = FakeMainDb(str(tmp_path / (acct.id+'.db')), self_uin=acct.self_uid)
            db.insert_text('123456', 'synthetic group history', time_s=clock.now_s-3600, group=True)
            databases[acct.self_uid] = db
        def factory(uid, acct):
            seen.append((uid, acct, acct.extra['adb_serial']))
            return LocalSqliteMainDb(databases[uid].path)
        agent._maindb_factory = factory
        adapter = agent.adapters['qidian']
        await asyncio.gather(*(adapter.poll(acct) for acct in accounts))
        await asyncio.gather(*(adapter.check_group_gaps(acct) for acct in accounts))
        assert len(seen) == 4
        for acct in accounts:
            own = [(uid, received, serial) for uid, received, serial in seen if received is acct]
            assert own == [(acct.self_uid, acct, acct.extra['adb_serial'])]*2
            assert agent.store.cursor_get(acct.id, 'qidian_bootstrap').value == acct.self_uid
        assert agent.store.count_messages('qd01') == agent.store.count_messages('qd02') == 0
    finally:
        await agent.bus.close()
        agent.store.close()
