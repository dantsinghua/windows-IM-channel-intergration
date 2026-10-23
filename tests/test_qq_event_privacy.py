"""05 K7：二维码只在实时事件内存/API，持久化与 webhook 永远没有副本。"""
from __future__ import annotations

import base64
import json

from qtrade_agent.events import Events
from qtrade_agent.store import Store
from qtrade_agent.webhook import FakeHttp, WebhookDispatcher
from test_qq_first_login_common import first_login_rig, no_external_io, png, start_waiting


async def test_qr_live_frame_has_png_but_replay_webhook_sqlite_and_log_do_not(tmp_path, clock, no_external_io, caplog):
    store = Store(str(tmp_path / 'privacy.db'), clock=clock).open()
    try:
        store.con.execute("INSERT INTO webhooks(id,name,url,secret_ref,created_ms,updated_ms) VALUES(?,?,?,?,?,?)",
                          ('fake', 'fake', 'https://fake.invalid/webhook', 'vault://fake', clock(), clock()))
        http = FakeHttp()
        dispatcher = WebhookDispatcher(store, http=http, secret_provider=lambda row: 'fake', clock=clock)
        events = Events(store, on_emit=dispatcher.fanout)
        secret = base64.b64encode(png(71)).decode()
        payload = {'state': 'login_required', 'login_session_id': 'fake-current',
                   'prompt': {'kind': 'WAIT_QRCODE', 'qrcode_png_b64': secret, 'expires_at': 'fake-expiry'}}
        seq = events.emit('account_state', account_id='qq01', channel='qq', payload=payload, now_ms=clock())
        live = await events.next_frame()
        assert live['payload']['prompt']['qrcode_png_b64'] == secret
        assert payload['prompt']['qrcode_png_b64'] == secret
        replay = events.replay(seq - 1)
        assert secret not in json.dumps(replay)
        rows = [dict(row) for row in store.con.execute('SELECT target,payload_json FROM events_outbox')]
        assert {row['target'] for row in rows} == {'ws', 'webhook:fake'}
        assert all(secret not in row['payload_json'] for row in rows)
        assert 'qrcode_png_b64' not in json.dumps(rows)
        await dispatcher.tick()
        assert len(http.requests) == 1
        assert secret.encode() not in http.requests[0]['body']
        assert secret not in '\n'.join(store.con.iterdump()) + caplog.text
    finally:
        store.close()


async def test_real_account_flow_does_not_persist_qr_in_any_table_or_file(first_login_rig, caplog):
    r = first_login_rig
    prompt = await start_waiting(r)
    secret = prompt['qrcode_png_b64']
    assert secret not in '\n'.join(r.store.con.iterdump())
    assert secret not in json.dumps(r.fs.json_files) + caplog.text
    assert not any(path.endswith('.png') for path in r.fs.files)
    assert not list(r.tmp_path.rglob('*.png'))


async def test_unconfirmed_identity_cannot_ingest_messages_but_confirmed_session_can(first_login_rig):
    r = first_login_rig
    await start_waiting(r)
    def event(mid):
        return {'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                'message_id': mid, 'user_id': 888888, 'self_id': 123456789,
                'time': r.clock.now_s, 'sender': {'user_id': 888888, 'nickname': 'fake'},
                'message': [{'type': 'text', 'data': {'text': 'fake fixture'}}]}
    # 经真正 OneBot 事件接线到归一化层；直接 await 分发避免依赖调度睡眠。
    adapter = r.agent.adapters['qq']
    await adapter._on_event_by_id(r.id, event(11))
    assert r.store.list_messages(r.id) == []
    r.first.online = True
    await r.accounts.poll_qq_logins()
    await adapter._on_event_by_id(r.id, event(12))
    assert len(r.store.list_messages(r.id)) == 1
