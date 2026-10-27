"""Offline regressions for the current collector; temporary SQLite and fake Bus only."""
import asyncio
import base64
import json
import threading
from types import SimpleNamespace

import pytest

from qtrade_agent.app import AgentApp
from qtrade_agent.events import Events, message_payload
from qtrade_agent.models import Message, Session
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host
from qtrade_agent.store import Store
from relay.protocol import Binding, ProtocolError
from relay.qtrade_native_port import QTradeNativePort

NOW = 1_800_000_000


@pytest.fixture
def store(tmp_path):
    value = Store(str(tmp_path / 'native.db')).open()
    value.ensure_account('qd01', 'qidian', state='running', self_uid='10001')
    value.ensure_account('wx01', 'wechat', state='running', self_uid='wxid_native')
    yield value
    value.close()


def incoming(channel='qidian', peer='90001', native='native-1', kind='private', mtype='text'):
    aid = 'wx01' if channel == 'wechat' else 'qd01'
    return Message(account_id=aid, channel=channel, session=Session(aid, peer, kind),
                   dir='in', type=mtype, text='synthetic', ts_ms=NOW * 1000,
                   source='chatlog' if channel == 'wechat' else 'qidian_db',
                   sender_id='native-sender', ext_msg_id=native)


def binding(channel='qidian'):
    return Binding('synthetic-owner', 'wxid_native' if channel == 'wechat' else '10001',
                   'synthetic-wb', b'a' * 16, b'b' * 32, channel=channel)


def test_default_configure_ignores_removed_version_and_environment_switches(monkeypatch):
    monkeypatch.delenv('XUNJIA_COLLECTOR_VERSION', raising=False)
    monkeypatch.delenv('XUNJIA_INSTITUTION_COLLECTOR', raising=False)
    calls = []
    class Host(InstitutionCollectorHost):
        async def start(self): calls.append('start')
    host = object.__new__(Host)
    async def create(cls, settings, **kw):
        assert kw['legacy_inbox_disabled']() is True
        assert callable(kw['presence_provider'])
        assert callable(kw['capture_refresher'])
        calls.append('construct')
        return host
    monkeypatch.setattr(InstitutionCollectorHost, 'from_settings', classmethod(create))
    app = SimpleNamespace(xunjia_v22_host=None, collector_reporting_settings=SimpleNamespace(state='pending'),
        store=None, bus=None, data_dir='synthetic', clock=lambda: NOW * 1000,
        _xunjia_institution_friends=lambda _: [], _xunjia_v22_contacts=lambda _: {},
        _xunjia_institution_history_source=lambda _: None, _xunjia_institution_presence=lambda _: None,
        _xunjia_institution_media=lambda _: {}, _capture_wechat_media_refresher=lambda *args: None)
    asyncio.run(AgentApp._configure_xunjia_v22(app))
    assert app.xunjia_v22_host is host and calls == ['construct', 'start']


def test_identity_switch_preserves_history_and_namespaces_cursors(store):
    old = store.ingest(incoming())
    store.cursor_set('qd01', 'qidian_bootstrap', 100, '10001')
    store.observe_qidian_identity('qd01', '10001', '10002')
    assert store.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    assert store.get_message(old.id) is None and store.count_messages('qd01') == 0
    assert store.list_sessions(account_id='qd01') == []
    assert store.cursor_get('qd01', 'qidian_bootstrap') is None
    new = store.ingest(incoming())
    assert old.id != new.id and store.count_messages('qd01') == 1
    assert store.query_messages(account_id='qd01')[0][0]['identity_epoch'] == 1
    assert store.con.execute('SELECT COUNT(*) FROM cursors').fetchone()[0] == 1
    store.observe_qidian_identity('qd01', '10002', '10001')
    assert store.get_message(old.id) is None and store.get_message(new.id) is None
    assert store.count_messages('qd01') == 0


def test_old_unknown_idempotency_cannot_be_deleted_or_reported_done(store):
    store.insert_command(trace_id='trace-old', account_id='qd01', op='send_text', args_json='{}',
                         idempotency_key='unknown-native', confirm=True, timeout_ms=1000,
                         transport='http', actor='synthetic', ip=None, now_ms=NOW * 1000)
    store.idem_claim(account_id='qd01', key='unknown-native', op='send_text', args_hash='a',
                     trace_id='trace-old', now_ms=NOW * 1000, ttl_days=7)
    store.observe_qidian_identity('qd01', '10001', '10002')
    assert store.idem_get('qd01', 'unknown-native')['status'] == 'IDENTITY_FENCED'
    assert store.get_command('trace-old') is None and store.list_commands('qd01') == []
    assert store.con.execute('SELECT COUNT(*) FROM commands').fetchone()[0] == 1
    store.idem_delete('qd01', 'unknown-native')
    store.idem_finish(account_id='qd01', key='unknown-native', status='DONE', result_code='DELIVERED', now_ms=NOW * 1000)
    row = store.con.execute('SELECT * FROM idempotency').fetchone()
    assert row['status'] == 'SENDING' and row['identity_epoch'] == 0
    assert not store.idem_claim(account_id='qd01', key='unknown-native', op='send_text', args_hash='a',
                                trace_id='new', now_ms=NOW * 1000, ttl_days=7)


@pytest.mark.parametrize('peer,kind,mtype', [('friend', 'private', 'text'), ('room@chatroom', 'group', 'text'),
                                         ('room@chatroom', 'group', 'image')])
def test_wechat_native_facts_keep_real_session_sender_and_type(store, peer, kind, mtype):
    loop = asyncio.new_event_loop()
    try:
        port = QTradeNativePort(binding=binding('wechat'), store=store, bus=None, loop=loop,
                                legacy_inbox_disabled=lambda: True)
        message = incoming('wechat', peer=peer, kind=kind, mtype=mtype)
        store.ingest(message)
        payload = message_payload(message, late_after_s=60)
        payload['session_id'] = payload['session']['id']; payload['ts_ms'] = NOW * 1000
        data = port.normalize_native_message(payload, live=True)
        assert port._native_peer(data['peer_id']) == peer
        assert data['session']['native_id'] == peer and data['session']['kind'] == kind
        assert data['channel'] == 'wechat' and data['type'] == mtype
        assert data['sender']['id'] == 'native-sender'
        assert not data['historical']
    finally:
        loop.close()


def test_new_identity_rejects_old_outbox_replay_even_with_same_slot(store, tmp_path):
    message = incoming(); store.ingest(message)
    events = Events(store)
    old_seq = events.emit('message', account_id='qd01', channel='qidian',
                          payload=message_payload(message, late_after_s=60))
    store.observe_qidian_identity('qd01', '10001', '10002')
    loop = asyncio.new_event_loop()
    current = Binding('synthetic-owner', '10002', 'synthetic-wb', b'a' * 16, b'b' * 32)
    port = QTradeNativePort(binding=current, store=store, bus=None, loop=loop,
                           legacy_inbox_disabled=lambda: True, identity_epoch=1)
    ledger = SimpleNamespace(begin_inbound=lambda *args: pytest.fail('old event reached ledger'),
                             permission=lambda _: True)
    bridge = SimpleNamespace(binding=current, ledger=ledger, adapters={}, echo_wait_seconds=1,
                             native_reconcile=port.native_reconcile)
    host = XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop,
        binding=current, bridge=bridge, port=port, state_path=tmp_path / 'new-host.db',
        legacy_inbox_disabled=lambda: True)
    try:
        host._replay()
        with host._db() as db:
            assert db.execute('SELECT COUNT(*) FROM host_event').fetchone()[0] == 0
        assert store.con.execute('SELECT COUNT(*) FROM events_outbox').fetchone()[0] == 1
        payload = dict(store.con.execute('SELECT payload_json FROM events_outbox WHERE seq=?', (old_seq,)).fetchone())
        old = json.loads(payload['payload_json'])
        old.update(session_id=old['session']['id'], ts_ms=NOW * 1000, source_seq=old_seq)
        with pytest.raises(ProtocolError, match='identity epoch'):
            port.normalize_native_message(old)
    finally:
        loop.close()


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    assert not thread.is_alive()
    loop.close()


@pytest.mark.parametrize('confirmed,expected', [(True, 'sent'), (False, 'unknown')])
def test_wechat_group_send_uses_native_bus_and_requires_receipt(store, running_loop, confirmed, expected):
    bound = binding('wechat'); calls = []
    class Bus:
        supports_xunjia_v22_guard = True
        async def submit(self, cmd):
            assert cmd._xunjia_native_guard() is True
            calls.append(cmd.args)
            return SimpleNamespace(ok=True, code='DELIVERED' if confirmed else 'ACCEPTED',
                data={'ext_msg_id': 'room@chatroom:42', 'confirmed_by': 'chatlog'} if confirmed else {})
    port = QTradeNativePort(binding=bound, store=store, bus=Bus(), loop=running_loop,
        legacy_inbox_disabled=lambda: True, execution_permission=lambda _: True)
    cmd = dict(owner_user_id=bound.owner_user_id, account_uid=bound.account_uid, workbench_id=bound.workbench_id,
        channel='wechat', command_id='synthetic-send', peer_id=port._relay_peer('room@chatroom'), text='synthetic',
        content_hash='a' * 64, control_version=1, expires_at=NOW + 60, transport='oss')
    assert port.native_send(cmd)['status'] == expected
    assert calls == [{'session': 'room@chatroom', 'text': 'synthetic'}]


def test_media_manifest_is_durable_and_deduplicates_pending_then_ready(tmp_path):
    # Only the current media pipeline reads a ready local image; no downloader runs here.
    raw = b'local-image-bytes'
    media = SimpleNamespace(get=lambda _: {'size': len(raw)}, read_file=lambda _: raw)
    store = SimpleNamespace(get_message=lambda _: {'media_json': json.dumps([{'media_id': 9, 'kind': 'image'}])})
    facts = AgentApp._xunjia_institution_media(SimpleNamespace(store=store, media=media), {'native_message_id': 'image-1'})
    assert facts['media_state'] == 'ready'
    assert base64.b64decode(facts['attachments'][0]['content_base64']) == raw
    media.read_file = lambda _: None
    pending = AgentApp._xunjia_institution_media(SimpleNamespace(store=store, media=media), {'native_message_id': 'image-1'})
    assert pending['media_state'] == 'pending' and pending['attachments'] == []


def test_large_original_image_resumes_after_restart_and_reassembles_out_of_order(tmp_path):
    import hashlib
    from relay.gateway_paths import CHUNK_BYTES
    from relay.institution_archive import InstitutionArchive
    raw = b'x' * (2 * 1024 * 1024 + 39)
    sha = hashlib.sha256(raw).hexdigest()
    bound = binding('wechat'); chunks = []; publications = []; interrupted = [True]
    class Adapter:
        def publish_manifest(self, payload, message_id=None):
            if payload['op'] == 'media_chunk':
                if payload['position'] == 1 and interrupted[0]:
                    interrupted[0] = False
                    raise OSError('synthetic interrupted upload')
                chunks.append(payload.copy())
            else:
                publications.append(payload.copy())
    native = []
    def provider(event):
        return {'attachments': [{'name': 'original.png', 'sha256': sha, 'size': len(raw)}],
                'media': [{'kind': 'image', 'state': 'ready'}], 'media_state': 'chunked', '_media_blobs': {sha: raw}}
    loop = asyncio.new_event_loop()
    store = Store(str(tmp_path / 'source.db')).open()
    def build(media_provider):
        ledger = SimpleNamespace(permission=lambda _: True)
        port = SimpleNamespace(binding=bound, channel='wechat', native_reconcile=lambda _: None, _account=lambda: {'id': 'wx01'})
        bridge = SimpleNamespace(binding=bound, ledger=ledger, adapters={'oss': Adapter()}, echo_wait_seconds=1,
                                 publish_native=lambda data, **kw: native.append(data))
        return XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop,
            binding=bound, bridge=bridge, port=port, state_path=tmp_path / 'host.db',
            legacy_inbox_disabled=lambda: True, media_provider=media_provider)
    try:
        host = build(provider)
        event = {'event_id': 'original-image', 'ext_msg_id': 'room:42', 'peer_id': 'wxpeer-cm9vbUBjaGF0cm9vbQ',
                 'media': [{'kind': 'image', 'state': 'pending'}], 'channel': 'wechat', 'historical': False,
                 'native_account_uid': bound.account_uid, 'identity_epoch': 0}
        with host._db() as db:
            db.execute('INSERT INTO host_event(event_id,body) VALUES(?,?)', (event['event_id'], json.dumps(event)))
        with pytest.raises(OSError, match='interrupted'):
            host._flush_native()
        assert native == [] and len(chunks) == 1
        assert (tmp_path / 'native-media' / sha).read_bytes() == raw
        # Source is unavailable after restart; only the immutable local spool remains.
        host = build(lambda _: {'attachments': [], 'media': [{'kind': 'image', 'state': 'pending'}], 'media_state': 'pending'})
        host._flush_native()
        assert len(chunks) == (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
        assert [p['position'] for p in chunks].count(0) == 1
        assert len(native) == 1 and native[0]['media_state'] == 'chunked'
        assert publications[0]['op'] == 'native_media' and publications[0]['media_state'] == 'chunked'
        assert publications[0]['attachments'] == [{'name': 'original.png', 'sha256': sha, 'size': len(raw)}]
        with host._db() as db:
            assert db.execute('SELECT COUNT(*) FROM host_media').fetchone()[0] == 0
        archive = InstitutionArchive(tmp_path / 'received', clock=lambda: NOW)
        for payload in reversed(chunks):
            archive.accept({'workbench_id': bound.workbench_id, 'account_uid': bound.account_uid,
                            'channel': 'wechat', 'payload': payload})
        archive.accept({'workbench_id': bound.workbench_id, 'account_uid': bound.account_uid,
                        'channel': 'wechat', 'payload': chunks[0]})
        assembled = list((tmp_path / 'received').rglob(sha))
        assert len(assembled) == 1 and assembled[0].read_bytes() == raw
    finally:
        store.close(); loop.close()


def test_delayed_old_reader_cannot_ingest_or_advance_new_identity_cursor(store):
    from qtrade_agent.store import CursorUpdate
    message = incoming()
    message._native_account_uid = '10001'
    store.observe_qidian_identity('qd01', '10001', '10002')
    with pytest.raises(ValueError, match='native_identity_changed'):
        store.ingest(message)
    with pytest.raises(ValueError, match='native_identity_changed'):
        store.ingest_batch([], CursorUpdate('qd01', 'qidian_rowid:90001', 10, native_account_uid='10001'))
    with pytest.raises(ValueError, match='native_identity_changed'):
        store.cursor_set('qd01', 'qidian_rowid:90001', 10, native_account_uid='10001')
    assert store.count_messages('qd01') == 0
    assert store.cursor_get('qd01', 'qidian_rowid:90001') is None


def test_filehelper_directory_is_based_on_current_native_sessions_and_keeps_private_kind(store):
    from types import MethodType
    async def run():
        current = ['wxid_native']
        class Native:
            async def status(self):
                return {'enabled': True, 'wechat': {'running': True, 'logged_in': True, 'wxid': current[0]},
                        'chatlog': {'running': True, 'key_ok': True}}
            async def sessions(self,*,limit):
                assert limit==5000
                return [{'userName': 'filehelper'}, {'userName': 'real-room@chatroom', 'nickName': 'group'}]
        app = SimpleNamespace(wechat_client=Native(), accounts=SimpleNamespace(busy=lambda _: False),
                              store=store, xunjia_v22_host=SimpleNamespace(loop=asyncio.get_running_loop()))
        app._xunjia_institution_presence = MethodType(AgentApp._xunjia_institution_presence, app)
        app._xunjia_wechat_directory = MethodType(AgentApp._xunjia_wechat_directory, app)
        row = store.get_account('wx01')
        contacts = await asyncio.to_thread(AgentApp._xunjia_institution_friends, app, row)
        assert contacts[0] == {'native_id': 'filehelper', 'peer_id': 'wxpeer-ZmlsZWhlbHBlcg',
                               'kind': 'private', 'name': '文件传输助手'}
        assert contacts[1]['kind'] == 'group' and contacts[1]['native_id'] == 'real-room@chatroom'
        async def no_sessions(*,limit):
            assert limit==5000
            return []
        app.wechat_client.sessions = no_sessions
        assert await asyncio.to_thread(AgentApp._xunjia_institution_friends, app, row) == []
        current[0] = 'wxid_another'
        with pytest.raises(RuntimeError, match='native_identity_unverified'):
            await app._xunjia_wechat_directory(row)
    asyncio.run(run())


def test_pre_epoch_orphan_outbox_from_old_deleted_identity_never_relabels_current_uid(store, tmp_path):
    message = incoming(); store.ingest(message)
    Events(store).emit('message', account_id='qd01', channel='qidian', payload=message_payload(message, late_after_s=60))
    # Reproduce the already-deployed pre-epoch behavior: old native rows were deleted,
    # while the slot was switched and its event journal remained.
    store.con.execute("DELETE FROM messages WHERE account_id='qd01'")
    store.con.execute("UPDATE accounts SET self_uid='10002' WHERE id='qd01'")
    loop = asyncio.new_event_loop()
    current = Binding('synthetic-owner', '10002', 'synthetic-wb', b'a' * 16, b'b' * 32)
    port = QTradeNativePort(binding=current, store=store, bus=None, loop=loop, legacy_inbox_disabled=lambda: True)
    ledger = SimpleNamespace(permission=lambda _: True, begin_inbound=lambda *args: pytest.fail('orphan crossed identity boundary'))
    bridge = SimpleNamespace(binding=current, ledger=ledger, adapters={}, echo_wait_seconds=1)
    host = XunjiaV22Host(store=store, bus=SimpleNamespace(supports_xunjia_v22_guard=True), loop=loop,
        binding=current, bridge=bridge, port=port, state_path=tmp_path / 'pre-epoch-host.db', legacy_inbox_disabled=lambda: True)
    try:
        host._replay()
        with host._db() as db:
            assert db.execute('SELECT COUNT(*) FROM host_event').fetchone()[0] == 0
        assert store.replay_outbox(0) == []
        assert store.con.execute('SELECT COUNT(*) FROM events_outbox').fetchone()[0] == 1
    finally:
        loop.close()


def test_removed_private_config_and_url_environment_are_never_loaded(tmp_path, monkeypatch):
    from pathlib import Path
    from tests.test_api_ext import Rig
    from qtrade_agent.ossbox.collector_settings import CollectorReportingSettings
    retired = tmp_path / 'retired-private.json'
    retired.write_text(json.dumps({'owner_user_id': 'synthetic-retired-owner', 'seal_key': 'not-a-key',
                                  'sign_key': 'not-a-key', 'workbench_id': 'must-not-reuse'}))
    monkeypatch.setenv('XUNJIA_COLLECTOR_CONFIG_FILE', str(retired))
    monkeypatch.setenv('XUNJIA_FILE_GATEWAY_URL', 'http://retired.invalid')
    monkeypatch.setenv('XUNJIA_INSTITUTION_CONFIG', str(tmp_path / 'absent-tls.json'))
    read = Path.read_text
    def checked(path, *args, **kwargs):
        assert path != retired, 'retired private configuration must not be read'
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', checked)
    rig = Rig(tmp_path)
    try:
        settings = rig.agent.collector_reporting_settings
        assert settings._seed == {'base_url': 'https://its-relay.yingmi.cn'}
        assert settings.workbench_id != 'must-not-reuse'
        recreated = CollectorReportingSettings(rig.agent.store, rig.agent.vault)
        assert recreated.workbench_id == settings.workbench_id
    finally:
        rig.close()


@pytest.mark.parametrize('kind,account_id', [('messages_export', 'qd01'), ('diagnostics', None)])
def test_followup_job_create_preserves_existing_job_schema_and_lifecycle(store, kind, account_id):
    columns = {row['name'] for row in store.con.execute('PRAGMA table_info(jobs)')}
    assert 'identity_epoch' not in columns
    job_id = store.job_create(kind=kind, actor='synthetic-operator', account_id=account_id,
                              params={'scope': 'synthetic'}, now_ms=NOW * 1000)
    row = store.job_get(job_id)
    assert row['state'] == 'queued' and row['account_id'] == account_id
    assert json.loads(row['params_json']) == {'scope': 'synthetic'}
    assert row['expires_ms'] == NOW * 1000 + store.job_retention_days * 86_400_000
    store.job_start(job_id, now_ms=NOW * 1000 + 1)
    store.job_finish(job_id, ok=True, result={'done': True}, now_ms=NOW * 1000 + 2)
    assert store.job_get(job_id)['state'] == 'succeeded'


def test_followup_native_identity_cas_audits_exact_facts_once(store):
    assert store.list_audit('account.native_identity_changed') == []
    changed = store.observe_qidian_identity('qd01', '10001', '10002', now_ms=NOW * 1000)
    assert changed['self_uid'] == '10002'
    rows = store.list_audit('account.native_identity_changed')
    assert len(rows) == 1
    assert rows[0]['account_id'] == 'qd01' and rows[0]['kind'] == 'system'
    assert json.loads(rows[0]['detail_json']) == {'old_uid': '10001', 'new_uid': '10002',
                                                'identity_epoch': 1, 'account_id': 'qd01'}
    assert store.observe_qidian_identity('qd01', '10001', '10002') is None
    assert store.observe_qidian_identity('qd01', '10002', '10002') is None
    assert store.observe_qidian_identity('qd01', '10001', '10003') is None
    assert len(store.list_audit('account.native_identity_changed')) == 1
