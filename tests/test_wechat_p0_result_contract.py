"""P0 T01/T02/T06: isolated real adapter/Bus and peer result contracts."""
from __future__ import annotations

import importlib
import json
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from qtrade_agent.adapters.wechat.client import WeChatCallFailed
from qtrade_agent.api.serialize import stored_result_view
from qtrade_agent.bus.bus import canonical_args_hash
from qtrade_agent.config import AgentConfig
from qtrade_agent.models import Command, CommandError, CommandResult, Message, Session
from tests.test_wechat_rig import make_wechat_rig
from tests.conftest import Clock

PEER = Path('/home/anlin/.local/state/xunjia-negotiation-implementation-20261023/candidate-0038/app')


def peer_result(result):
    # Read-only peer runtime; importing this module does not start its service.
    sys.path.insert(0, str(PEER))
    try:
        return importlib.import_module('relay.qtrade_native_port').QTradeNativePort._result(result)
    finally:
        sys.path.remove(str(PEER))


async def rejected(rig, monkeypatch):
    async def refuse(**kwargs):
        raise WeChatCallFailed(404, {'error': {'message': 'Synthetic target has no trusted name',
                                             'reason': 'target_not_found'}}, '/wa/v1/wechat/send')
    monkeypatch.setattr(rig.client, 'send', refuse)
    bus = rig.make_bus()
    result = await bus.submit(Command('wx01', 'send_text', {'session': 'wx01:p', 'text': 'Synthetic P0'},
                                      idempotency_key='p0-reject'))
    return bus, result


async def test_pre_execution_404_survives_real_adapter_bus_and_peer(tmp_path, monkeypatch):
    rig = make_wechat_rig(tmp_path)
    bus, result = await rejected(rig, monkeypatch)
    try:
        assert result.code == 'TARGET_NOT_FOUND'
        assert result.error.reason == 'target_not_found'
        assert result.error.message == 'Synthetic target has no trusted name'
        assert result.error.retryable is False
        assert peer_result(result)['status'] == 'failed'
        row = rig.store.con.execute('SELECT * FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()
        assert row['state'] == 'FAILED' and row['ext_msg_id'] is None and row['confirmed_by'] is None
        assert rig.fake.sent == []
    finally:
        await bus.close()
        rig.store.close()


async def test_command_list_and_detail_have_identical_failure_fields(tmp_path, monkeypatch):
    rig = make_wechat_rig(tmp_path)
    bus, result = await rejected(rig, monkeypatch)
    try:
        detail = stored_result_view(rig.store.get_command_result(result.trace_id))
        listed = stored_result_view(rig.store.list_commands('wx01')[0])
        assert detail['error']['message'] == 'Synthetic target has no trusted name'
        assert listed['error'] == detail['error']
    finally:
        await bus.close()
        rig.store.close()


async def test_attempted_send_without_native_evidence_stays_unknown_and_does_not_resend(tmp_path):
    rig = make_wechat_rig(tmp_path)
    rig.fake.send_result = {'ok': False, 'code': 'SEND_FAILED', 'ext_msg_id': None, 'confirm_ms': None}
    bus = rig.make_bus()
    try:
        def command():
            return Command('wx01', 'send_text', {'session': 'wx01:p', 'text': 'Synthetic P0'},
                           idempotency_key='p0-unknown')
        result = await bus.submit(command())
        assert peer_result(result)['status'] == 'unknown'
        row = rig.store.con.execute('SELECT * FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()
        assert row['state'] == 'UNCONFIRMED' and row['ext_msg_id'] is None
        await bus.submit(command())
        assert len(rig.fake.sent) == 1
    finally:
        await bus.close()
        rig.store.close()


@pytest.mark.parametrize('failure', ['http500', 'unknown-exception'])
async def test_ambiguous_native_failure_is_unconfirmed_not_failed_or_retryable(tmp_path, monkeypatch, failure):
    rig = make_wechat_rig(tmp_path)
    async def ambiguous(**kwargs):
        rig.fake.sent.append(kwargs)
        if failure == 'http500':
            raise WeChatCallFailed(500, {'error': {'message': 'Synthetic ambiguous failure'}}, '/wa/v1/wechat/send')
        raise RuntimeError('Synthetic response lost after input')
    monkeypatch.setattr(rig.client, 'send', ambiguous)
    bus = rig.make_bus()
    try:
        result = await bus.submit(Command('wx01', 'send_text', {'session': 'wx01:p', 'text': 'Synthetic P0'},
            idempotency_key='ambiguous-key'))
        row = rig.store.con.execute('SELECT * FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()
        assert row['state'] == 'UNCONFIRMED'
        assert result.error.retryable is False and result.error.needs_human is True
        assert peer_result(result)['status'] == 'unknown'
        assert len(rig.fake.sent) == 1
    finally:
        await bus.close()
        rig.store.close()


@pytest.mark.parametrize('case', ['no-error', 'false-without-reason', 'proven-before-input'])
async def test_bus_failure_metadata_only_allows_proven_pre_execution_failure(tmp_path, monkeypatch, case):
    rig = make_wechat_rig(tmp_path)
    calls = []
    async def result_only(account, command):
        calls.append(command.trace_id)
        known = case == 'proven-before-input'
        return CommandResult(ok=False, code='SEND_FAILED', trace_id=command.trace_id, source='chatlog',
            data={} if case == 'no-error' else {'native_action_attempted': False, 'reason': 'synthetic_preflight' if known else ''},
            error=None if case == 'no-error' else CommandError('Synthetic failure', reason='synthetic_preflight' if known else ''))
    monkeypatch.setattr(rig.adapter, 'send', result_only)
    bus = rig.make_bus()
    def command():
        return Command('wx01', 'send_text', {'session': 'wx01:p', 'text': 'Synthetic P0'}, idempotency_key='metadata-key')
    try:
        result = await bus.submit(command())
        row = rig.store.con.execute('SELECT * FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()
        assert row['state'] == ('FAILED' if case == 'proven-before-input' else 'UNCONFIRMED')
        assert peer_result(result)['status'] == ('failed' if case == 'proven-before-input' else 'unknown')
        if case != 'proven-before-input':
            assert result.error.retryable is False and result.error.needs_human is True
        await bus.submit(command())
        assert len(calls) == 1
    finally:
        await bus.close()
        rig.store.close()


async def test_close_after_native_input_preserves_unknown_key_and_restart_never_resends(tmp_path, monkeypatch):
    cfg = AgentConfig()
    cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=50, send_min_interval_ms=0, send_rand_extra_ms=0),
                  wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))
    rig = make_wechat_rig(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=10))
    entered = asyncio.Event()
    original_send = rig.client.send
    async def started_native(**kwargs):
        rig.fake.sent.append(kwargs)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(rig.client, 'send', started_native)
    def command():
        return Command('wx01', 'send_text', {'session': 'wx01:p', 'text': 'Interrupted synthetic input'},
                       idempotency_key='shutdown-key')
    bus = rig.make_bus()
    submission = asyncio.create_task(bus.submit(command()))
    await asyncio.wait_for(entered.wait(), 2)
    await bus.close()
    result = await submission
    state = rig.store.con.execute('SELECT state FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()[0]
    previous = rig.store.idem_get('wx01', 'shutdown-key')
    monkeypatch.setattr(rig.client, 'send', original_send)
    rig.fake.send_result = {'ok': False, 'code': 'SEND_FAILED', 'ext_msg_id': None, 'confirm_ms': None}
    resumed = rig.make_bus()
    try:
        replay = await resumed.submit(command())
        assert state == 'UNCONFIRMED'
        assert previous is not None and previous['trace_id'] == result.trace_id
        assert peer_result(result)['status'] == 'unknown' and peer_result(replay)['status'] == 'unknown'
        assert len(rig.fake.sent) == 1
    finally:
        await resumed.close()
        rig.store.close()


async def test_submit_timeout_after_native_input_is_unconfirmed_nonretryable_and_keeps_key(tmp_path, monkeypatch):
    cfg = AgentConfig()
    cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=50))
    rig = make_wechat_rig(tmp_path, cfg=cfg)
    entered = asyncio.Event()
    async def started_native(**kwargs):
        rig.fake.sent.append(kwargs)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(rig.client, 'send', started_native)
    bus = rig.make_bus()
    try:
        result = await bus.submit(Command('wx01', 'send_text', {'session': 'wx01:p', 'text': 'Submit budget exceeded'},
            timeout_ms=20, idempotency_key='submit-timeout-key'))
        assert entered.is_set()
        row = rig.store.con.execute('SELECT state FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()
        assert row['state'] == 'UNCONFIRMED'
        assert result.error.retryable is False and result.error.needs_human is True
        assert peer_result(result)['status'] == 'unknown'
        assert rig.store.idem_get('wx01', 'submit-timeout-key')['trace_id'] == result.trace_id
        assert len(rig.fake.sent) == 1
        assert bus.account_quiescent('wx01') is False, 'An active native call must remain visible during timeout reconciliation'
    finally:
        await bus.close()
        rig.store.close()


async def test_pending_readback_recovery_gives_later_records_a_turn(tmp_path):
    cfg = AgentConfig()
    cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=50, send_min_interval_ms=0, send_rand_extra_ms=0),
                  wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))
    rig = make_wechat_rig(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=10))
    bus = rig.make_bus()
    results = []
    try:
        for index in range(6):
            rig.fake.send_result = {'ok': True, 'code': 'DELIVERED', 'ext_msg_id': f'p:{101+index}', 'confirm_ms': 1}
            results.append(await bus.submit(Command('wx01', 'send_text',
                {'session': 'wx01:p', 'text': f'Synthetic pending {index}'}, idempotency_key=f'pending-key-{index}')))
        assert all(peer_result(result)['status'] == 'unknown' for result in results)
        assert len(rig.fake.sent) == 6
        rig.fake.add_row(talker='p', content='Synthetic pending 4', seq=105, ts_ms=rig.clock(), is_self=True)
        for _ in range(3):
            await rig.adapter.poll(rig.account(), only_sessions=['p'])
        proof = rig.store.confirmed_outbound_for_trace(results[4].trace_id)
        assert proof is not None and proof['ext_msg_id'] == 'p:105'
        assert rig.store.confirmed_outbound_for_trace(results[0].trace_id) is None
        assert len(rig.fake.sent) == 6
    finally:
        await bus.close()
        rig.store.close()


async def test_adapter_does_not_invent_native_row_from_request_text(tmp_path):
    rig = make_wechat_rig(tmp_path)
    rig.fake.send_result = {'ok': True, 'code': 'DELIVERED', 'ext_msg_id': 'p:987', 'confirm_ms': 1}
    try:
        await rig.adapter.send(rig.account(), Command('wx01', 'send_text',
            {'session': 'wx01:p', 'text': 'Request-only text'}, trace_id='p0-no-invention', idempotency_key='p0-no-invention'))
        rows = rig.store.con.execute('SELECT * FROM messages WHERE ext_msg_id=?', ('p:987',)).fetchall()
        assert not rows, 'A response ID without a read native row must not manufacture chatlog evidence'
    finally:
        rig.store.close()


@pytest.mark.parametrize('status', ['ABANDONED', 'SENDING'])
async def test_recovered_unconfirmed_key_is_not_deleted_or_resent(tmp_path, status):
    rig = make_wechat_rig(tmp_path)
    args = {'session': 'wx01:p', 'text': 'Recovered synthetic text'}
    rig.store.idem_claim(account_id='wx01', key='interrupted-key', op='send_text',
        args_hash=canonical_args_hash(args), trace_id='original-trace', now_ms=rig.clock(), ttl_days=7)
    rig.store.idem_finish(account_id='wx01', key='interrupted-key', status=status,
        result_code=None, now_ms=rig.clock())
    bus = rig.make_bus()
    try:
        result = await bus.submit(Command('wx01', 'send_text', args, idempotency_key='interrupted-key'))
        assert rig.fake.sent == [], 'Recovery must query evidence, never issue a second native send'
        assert peer_result(result)['status'] == 'unknown'
        assert rig.store.idem_get('wx01', 'interrupted-key')['trace_id'] == 'original-trace'
    finally:
        await bus.close()
        rig.store.close()


@pytest.mark.parametrize('proof_trace', ['original-trace', 'another-trace'])
async def test_recovery_only_accepts_native_evidence_bound_to_original_trace(tmp_path, proof_trace):
    rig = make_wechat_rig(tmp_path)
    args = {'session': 'wx01:p', 'text': 'Recovered synthetic text'}
    rig.store.idem_claim(account_id='wx01', key='interrupted-key', op='send_text',
        args_hash=canonical_args_hash(args), trace_id='original-trace', now_ms=rig.clock(), ttl_days=7)
    rig.store.idem_finish(account_id='wx01', key='interrupted-key', status='ABANDONED',
        result_code=None, now_ms=rig.clock())
    proof = Message(account_id='wx01', channel='wechat', session=Session('wx01', 'p', 'private'),
        dir='out', type='text', text=args['text'], ts_ms=rig.clock(), source='chatlog',
        self=True, state='DELIVERED', ext_msg_id='p:42', trace_id=proof_trace)
    message_id = rig.store.ingest(proof, now_ms=rig.clock()).id
    # Fixture represents previously persisted native proof, not a live command.
    rig.store.con.execute("UPDATE messages SET confirmed_by='chatlog', confirmed_ms=? WHERE id=?", (rig.clock(), message_id))
    rig.store.con.commit()
    bus = rig.make_bus()
    try:
        result = await bus.submit(Command('wx01', 'send_text', args, idempotency_key='interrupted-key'))
        assert not rig.fake.sent
        assert peer_result(result)['status'] == ('sent' if proof_trace == 'original-trace' else 'unknown')
        assert rig.store.idem_get('wx01', 'interrupted-key')['trace_id'] == 'original-trace'
    finally:
        await bus.close()
        rig.store.close()


def pending_message(rig, trace='bind-trace', text='Exact synthetic payload'):
    msg = Message(account_id='wx01', channel='wechat', session=Session('wx01', 'p', 'private'),
        dir='out', type='text', text=text, ts_ms=rig.clock(), source='chatlog', self=True,
        state='SENDING', trace_id=trace, idempotency_key=trace + '-key')
    return rig.store.ingest(msg, now_ms=rig.clock()).id


def native_message(rig, **changes):
    msg = Message(**{'account_id': 'wx01', 'channel': 'wechat', 'session': Session('wx01', 'p', 'private'),
        'dir': 'out', 'type': 'text', 'text': 'Exact synthetic payload', 'ts_ms': rig.clock(),
        'source': 'chatlog', 'self': True, 'state': 'DELIVERED', 'ext_msg_id': 'p:99',
        'raw_ref': 'synthetic-native-ref', 'sender_id': 'wxid_demo01', 'sender_name': 'Native Sender', **changes})
    msg._native_account_uid = 'wxid_demo01'
    return msg


@pytest.mark.parametrize('body', ['Exact synthetic payload', ' Exact synthetic payload ', 'Exact  synthetic payload'])
def test_poll_never_claims_pending_command_by_text_or_normalized_fingerprint(tmp_path, body):
    rig = make_wechat_rig(tmp_path)
    try:
        pending = pending_message(rig)
        received = rig.store.ingest(native_message(rig, text=body), now_ms=rig.clock())
        assert received.id != pending
        assert rig.store.message_state(pending)['state'] == 'SENDING'
        assert rig.store.count_messages('wx01') == 2
    finally:
        rig.store.close()


@pytest.mark.parametrize('poll_first', [True, False])
def test_explicit_trace_binding_merges_only_duplicate_native_row_and_preserves_facts(tmp_path, poll_first):
    rig = make_wechat_rig(tmp_path)
    try:
        history = rig.store.ingest(native_message(rig, ext_msg_id='p:88', text='Unrelated history', ts_ms=rig.clock()-10000), now_ms=rig.clock()).id
        pending = pending_message(rig)
        native = native_message(rig)
        if poll_first:
            rig.store.ingest(native, now_ms=rig.clock())
        assert rig.store.bind_wechat_readback('bind-trace', native, now_ms=rig.clock()) == pending
        rig.store.ingest(replace(native, id=None), now_ms=rig.clock())
        assert rig.store.bind_wechat_readback('bind-trace', native, now_ms=rig.clock()) == pending
        bound = dict(rig.store.con.execute('SELECT * FROM messages WHERE id=?', (pending,)).fetchone())
        assert bound['state'] == 'DELIVERED' and bound['confirmed_by'] == 'chatlog'
        assert bound['raw_ref'] == 'synthetic-native-ref' and bound['sender_name'] == 'Native Sender'
        assert bound['text'] == native.text and bound['ts_ms'] == native.ts_ms
        assert bound['trace_id'] == 'bind-trace' and bound['idempotency_key'] == 'bind-trace-key'
        assert rig.store.con.execute('SELECT id FROM messages WHERE id=?', (history,)).fetchone()
        assert rig.store.count_messages('wx01') == 2
        count = rig.store.con.execute('SELECT msg_count FROM sessions WHERE id=?', ('wx01:p',)).fetchone()[0]
        assert count == 2
        found = rig.store.con.execute("SELECT m.id FROM messages m JOIN messages_fts f ON f.rowid=m.rowid WHERE messages_fts MATCH 'synthetic'").fetchall()
        assert [r[0] for r in found] == [pending]
    finally:
        rig.store.close()


@pytest.mark.parametrize('mismatch', ['account', 'session', 'text', 'self', 'source', 'ext', 'wrong-prefix', 'negative-seq', 'zero-seq', 'invalid-seq', 'other-trace'])
def test_explicit_binding_rejects_mismatched_native_facts(tmp_path, mismatch):
    rig = make_wechat_rig(tmp_path)
    try:
        pending = pending_message(rig)
        changes = {'account': {'account_id': 'wx02'}, 'session': {'session': Session('wx01', 'other', 'private')},
            'text': {'text': ' Exact synthetic payload '}, 'self': {'self': False},
            'source': {'source': 'uia'}, 'ext': {'ext_msg_id': None}, 'wrong-prefix': {'ext_msg_id': 'other:99'},
            'negative-seq': {'ext_msg_id': 'p:-1'}, 'zero-seq': {'ext_msg_id': 'p:0'},
            'invalid-seq': {'ext_msg_id': 'p:NaN'}, 'other-trace': {'trace_id': 'different-command'}}[mismatch]
        assert rig.store.bind_wechat_readback('bind-trace', native_message(rig, **changes), now_ms=rig.clock()) is None
        assert rig.store.message_state(pending)['state'] == 'SENDING'
    finally:
        rig.store.close()


def test_one_native_id_cannot_be_bound_to_two_traces(tmp_path):
    rig = make_wechat_rig(tmp_path)
    try:
        first = pending_message(rig, 'first')
        second = pending_message(rig, 'second')
        native = native_message(rig)
        assert rig.store.bind_wechat_readback('first', native, now_ms=rig.clock()) == first
        assert rig.store.bind_wechat_readback('second', native, now_ms=rig.clock()) is None
        assert rig.store.message_state(second)['state'] == 'SENDING'
        assert rig.store.count_messages('wx01') == 2
    finally:
        rig.store.close()


@pytest.mark.parametrize('uid', [None, '', 'wxid_other'])
def test_binding_requires_verified_current_native_account_identity(tmp_path, uid):
    rig = make_wechat_rig(tmp_path)
    try:
        pending = pending_message(rig)
        native = native_message(rig)
        native._native_account_uid = uid
        assert rig.store.bind_wechat_readback('bind-trace', native, now_ms=rig.clock()) is None
        assert rig.store.message_state(pending)['state'] == 'SENDING'
    finally:
        rig.store.close()


@pytest.mark.parametrize('boundary', ['exact', 'uid-changed', 'epoch-changed', 'no-native-id'])
async def test_late_exact_native_proof_survives_restart_but_never_crosses_identity(tmp_path, boundary):
    cfg = AgentConfig()
    cfg = replace(cfg, bus=replace(cfg.bus, confirm_timeout_wechat_ms=50, send_min_interval_ms=0, send_rand_extra_ms=0),
                  wechat_adapter=replace(cfg.wechat_adapter, confirm_poll_interval_ms=1))
    clock = Clock(auto_step_ms=10)
    rig = make_wechat_rig(tmp_path, cfg=cfg, clock=clock)
    no_id = boundary == 'no-native-id'
    rig.fake.send_result = {'ok': not no_id, 'code': 'SEND_FAILED' if no_id else 'DELIVERED',
                           'ext_msg_id': None if no_id else 'p:99', 'confirm_ms': None if no_id else 1}
    bus = rig.make_bus()
    args = {'session': 'wx01:p', 'text': 'Synthetic delayed evidence'}
    result = await bus.submit(Command('wx01', 'send_text', args, idempotency_key='late-key'))
    assert peer_result(result)['status'] == 'unknown'
    assert len(rig.fake.sent) == 1
    row_id = rig.store.con.execute('SELECT id FROM messages WHERE trace_id=?', (result.trace_id,)).fetchone()[0]
    record = rig.store.settings_get('wechat.readback.' + result.trace_id)
    if not no_id:
        assert record['ext'] == 'p:99'
        assert args['text'] not in json.dumps(record)
    await bus.close()
    rig.store.close()

    restored = make_wechat_rig(tmp_path, cfg=cfg, clock=clock)
    try:
        if boundary == 'uid-changed':
            restored.store.con.execute("UPDATE accounts SET self_uid='wxid_changed' WHERE id='wx01'")
            restored.store.con.commit()
            restored.fake.wechat['wxid'] = 'wxid_changed'
        elif boundary == 'epoch-changed':
            restored.store.settings_set('relay.identity.wx01', {'epoch': 1, 'uid': 'wxid_demo01'})
        restored.fake.add_row(talker='p', content=args['text'], seq=99, ts_ms=clock(), is_self=True)
        await restored.adapter.poll(restored.account(), only_sessions=['p'])
        before_events = len(restored.events_of('message'))
        await restored.adapter.poll(restored.account(), only_sessions=['p'])
        state = restored.store.con.execute('SELECT state, ext_msg_id FROM messages WHERE id=?', (row_id,)).fetchone()
        assert state['state'] == ('DELIVERED' if boundary == 'exact' else 'UNCONFIRMED')
        assert state['ext_msg_id'] == ('p:99' if boundary == 'exact' else None)
        assert not restored.fake.sent
        assert len(restored.events_of('message')) == before_events
        if boundary == 'exact':
            assert restored.store.count_messages('wx01') == 1
        elif no_id:
            assert restored.store.count_messages('wx01') == 2, 'Unattributed native facts must still be collected'
    finally:
        restored.store.close()
