"""APK identity-change contract: temporary stores and passive observations only."""
import asyncio
from types import SimpleNamespace
import pytest
from qtrade_agent.store import Store
from qtrade_agent.models import Message,Session
from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qidian.presence import NativePresenceProbe
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.bus.bus import Bus,_Job
from qtrade_agent.models import Command
from relay.gateway_paths import InstitutionKeys
from relay.file_gateway import GatewayAuth
from tests.test_institution_native_presence import FakeUI,row

@pytest.fixture
def identities(tmp_path):
    s=Store(str(tmp_path/'identity.db')).open()
    s.ensure_account('qd01','qidian',state='running',self_uid='10001')
    s.ensure_account('qd02','qidian',state='running',self_uid='20001')
    yield s
    s.close()

def test_probe_preserves_changed_uid_without_declaring_old_identity_online():
    r=asyncio.run(NativePresenceProbe(FakeUI(uid='10002'))(row()))
    assert r['state']=='offline' and r['code']=='NATIVE_ACCOUNT_CHANGED'
    assert r['self_uid']=='10002'

def test_missing_identity_is_unknown_not_logout():
    r=asyncio.run(NativePresenceProbe(FakeUI(uid=None))(row()))
    assert r['state']=='unknown' and r['code']=='NATIVE_IDENTITY_UNAVAILABLE'

def test_cas_replaces_identity_preserves_old_epoch_and_hides_history_from_new_uid(identities):
    s=identities
    for aid,uid in [('qd01','10001'),('qd02','20001')]:
        s.ingest(Message(account_id=aid,channel='qidian',session=Session(aid,'90001','private'),
            dir='in',type='text',text='old-'+aid,ts_ms=1800000000000,source='qidian_db',sender_id='90001',ext_msg_id='old-'+aid))
    before=[dict(r) for r in s.con.execute('SELECT * FROM messages ORDER BY id')]
    updated=s.observe_qidian_identity('qd01','10001','10002')
    assert updated['self_uid']=='10002'
    assert updated['login_mode']=='manual' and not updated['remember'] and updated['credential_ref'] is None
    assert [dict(r) for r in s.con.execute('SELECT * FROM messages ORDER BY id')]==before
    assert s.count_messages('qd01')==0 and s.list_messages('qd01')==[]
    assert s.list_sessions(account_id='qd01')==[]
    assert s.native_identity('qd01')['epoch']==1
    assert s.con.execute("SELECT count(*) FROM messages WHERE account_id='qd02'").fetchone()[0]==1
    assert s.observe_qidian_identity('qd01','10001','10003') is None
    assert s.get_account('qd01')['self_uid']=='10002'

def test_new_identity_worker_ignores_old_outbox_and_never_resends_old_unknown(identities,tmp_path):
    from qtrade_agent.events import Events,message_payload
    from relay.protocol import digest
    async def run():
        now=1800000000.0;native=[];uploads=[]
        class Client:
            def __init__(self,path):self.queue_identity=('synthetic',path)
            def upload(self,name,blob):uploads.append((name,blob))
            def pending(self):return 0
        host=InstitutionCollectorHost(store=identities,bus=SimpleNamespace(supports_xunjia_v22_guard=True),
            loop=asyncio.get_running_loop(),keys=InstitutionKeys.from_auth(GatewayAuth('synthetic',b'0123456789abcdef')),
            config={'workbench_name':'desk','contract':'its-relay-path-v1'},
            settings=SimpleNamespace(workbench_id='wb-identity'),data_dir=tmp_path,
            legacy_inbox_disabled=lambda:True,friends_provider=lambda _:[],organization_provider=lambda _:None,
            clock=lambda:now,client_factory=Client)
        old=host._make_host(identities.get_account('qd01'))
        message=Message(account_id='qd01',channel='qidian',session=Session('qd01','90001','private'),
            dir='in',type='text',text='old identity private fact',ts_ms=int(now*1000),source='qidian_db',sender_id='90001',ext_msg_id='old-native')
        identities.ingest(message)
        Events(identities).emit('message',payload=message_payload(message,late_after_s=60),account_id='qd01',channel='qidian')
        command={'owner_user_id':old.binding.owner_user_id,'account_uid':'10001','workbench_id':'wb-identity',
            'peer_id':'90001','command_id':'old-unknown','control_version':1,'text':'synthetic authorized text',
            'content_hash':digest('synthetic'),'expires_at':now+120,'transport':'oss'}
        result=old.ledger.execute(command,lambda c:(native.append(c) or {'status':'unknown','evidence':'synthetic-native-outcome-unknown'}))
        assert result['status']=='unknown' and len(native)==1
        with old.ledger._db() as db:
            before=dict(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-unknown',)).fetchone())
        identities.observe_qidian_identity('qd01','10001','10002')
        current=host._make_host(identities.get_account('qd01'))
        assert current.binding.account_uid=='10002' and current.state_path!=old.state_path
        await current.run_once()
        await current.run_once()
        with current._db() as db:assert db.execute('SELECT COUNT(*) FROM host_event').fetchone()[0]==0
        assert uploads==[] and len(native)==1
        assert old.ledger.execute(command,lambda c:native.append(c))['status']=='unknown'
        assert len(native)==1
        with old.ledger._db() as db:
            assert dict(db.execute('SELECT * FROM rx_command WHERE command_id=?',('old-unknown',)).fetchone())==before
    asyncio.run(run())

@pytest.mark.parametrize('invalid',['','bad','1000','1'*21,None,True])
def test_invalid_identity_does_not_mutate_account(identities,invalid):
    assert identities.observe_qidian_identity('qd01','10001',invalid) is None
    assert identities.get_account('qd01')['self_uid']=='10001'

def test_change_stops_old_host_before_commit_and_publishes_new_directory(identities,tmp_path):
    async def run():
        clock=[1800000000];calls=[]
        async def observe(_):return {'state':'offline','code':'NATIVE_ACCOUNT_CHANGED','self_uid':'10002'}
        class Host:
            _active=True;_intake_ok=True
            async def start(self):self._active=True;calls.append('start-new')
            async def stop(self):
                assert identities.get_account('qd01')['self_uid']=='10001'
                self._active=False;calls.append('stop-old')
        settings=SimpleNamespace(workbench_id='wb-a',view=lambda:{'workbench_name':'desk'},saved=lambda:None)
        h=InstitutionCollectorHost(store=identities,bus=None,loop=asyncio.get_running_loop(),
            keys=InstitutionKeys.from_auth(GatewayAuth('synthetic',b'0123456789abcdef')),
            config={'workbench_name':'desk','contract':'its-relay-path-v1'},settings=settings,data_dir=tmp_path,
            legacy_inbox_disabled=lambda:True,friends_provider=lambda _:[],organization_provider=lambda _:None,
            clock=lambda:clock[0],presence_provider=observe,host_factory=lambda _:Host())
        h.active=True;h.hosts[('qd01','10001')]=Host()
        # Only inspect one slot; another native user must remain untouched.
        original=h.rows;h.rows=lambda:[r for r in original() if r['id']=='qd01']
        h.presence[('qd01','10001')]={'state':'online','confirmed_at':clock[0]}
        await h.probe_presence_once()
        assert not h.online('qd01','10001') and identities.get_account('qd01')['self_uid']=='10001'
        clock[0]+=10;await h.probe_presence_once()
        assert calls==['stop-old'] and identities.get_account('qd01')['self_uid']=='10002'
        await h.reconcile_accounts()
        assert calls==['stop-old','start-new']
        directory=h.directory_rows()
        assert [a['account_uid'] for a in directory]==['10002'] and directory[0]['online']
        assert identities.get_account('qd02')['self_uid']=='20001'
    asyncio.run(run())

def test_bus_rejects_old_identity_job_before_native_invocation():
    async def run():
        b=Bus.__new__(Bus);results=[]
        b._load_account=lambda _:Account(id='qd01',channel='qidian',state='running',self_uid='10002')
        b._finalize=lambda job,result:results.append(result)
        job=_Job(Command('qd01','send_text',{'session':'90001','text':'must not send'}),
                 Account(id='qd01',channel='qidian',state='running',self_uid='10001'),asyncio.get_running_loop().create_future())
        await b._execute(job)
        assert len(results)==1 and results[0].code=='GATE_BLOCKED'
        assert results[0].error.reason=='native_account_changed'
    asyncio.run(run())
