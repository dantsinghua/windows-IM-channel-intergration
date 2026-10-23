"""QTrade assembly contract for its-relay, with no network or account start."""
import asyncio
from types import SimpleNamespace

import pytest

from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host
from relay.protocol import Binding, ProtocolError, digest
from relay.collector_bridge import CollectorBridge
from relay.qtrade_native_port import QTradeNativePort
from server.institution_transport import InstitutionTransport
from server.receiver_ledger import ReceiverLedger
from server.transports import TransportUnavailable


def assemble(tmp_path, *, same_queue=False):
    """Current account-path carrier and reusable receiver core; no env factory."""
    binding = Binding('synthetic-owner', 'synthetic-account', 'synthetic-wb', b'k' * 16, b's' * 32)
    root = tmp_path / 'current-receiver'
    ledger = ReceiverLedger(root / 'receiver.sqlite3', binding)
    inbound = SimpleNamespace(queue_identity=('synthetic-relay', 'downlink'))
    outbound = SimpleNamespace(queue_identity=('synthetic-relay', 'downlink' if same_queue else 'uplink'))
    carrier = InstitutionTransport(binding, inbound, outbound, root / 'publication.sqlite3',
        role='collector', institution_owner='synthetic-owner')
    store = SimpleNamespace(outbox_seq_bounds=lambda: (0, 0))
    bus = SimpleNamespace(supports_xunjia_v22_guard=True)
    port = QTradeNativePort(binding=binding, store=store, bus=bus, loop=asyncio.get_running_loop(),
        legacy_inbox_disabled=lambda: True, execution_permission=ledger.permission)
    bridge = CollectorBridge(ledger=ledger, adapters={'oss': carrier},
        native_send=port.native_send, native_reconcile=port.native_reconcile)
    return XunjiaV22Host(store=store, bus=bus, loop=asyncio.get_running_loop(), binding=binding,
        bridge=bridge, port=port, state_path=root / 'host.sqlite3', legacy_inbox_disabled=lambda: True)


def test_current_carrier_keeps_inactive_native_guard_closed(tmp_path):
    async def run():
        host = assemble(tmp_path)
        carrier = host.bridge.adapters['oss']
        assert carrier.role == 'collector'
        assert carrier.inbound.queue_identity != carrier.outbound.queue_identity
        with pytest.raises(TransportUnavailable, match='DIRECT_OSS_RETIRED_USE_FILE_GATEWAY'):
            CollectorBridge(ledger=host.ledger, adapters=host.bridge.adapters,
                            native_send=host.port.native_send,
                            native_reconcile=host.port.native_reconcile, object_store=object())
        assert host.port.execution_permission == host.permission
        assert not host.permission({}) and not host._active
        assert host.bridge.native_send({'business_version': '2.2'})['status'] == 'failed'
    asyncio.run(run())


def test_current_carrier_rejects_shared_fifo(tmp_path):
    async def run():
        with pytest.raises(RuntimeError, match='SEPARATE_QUEUES_REQUIRED'):
            assemble(tmp_path, same_queue=True)
    asyncio.run(run())


def command_for(host, command_id='synthetic-command'):
    return {'owner_user_id': host.binding.owner_user_id,
            'account_uid': host.binding.account_uid, 'workbench_id': host.binding.workbench_id,
            'command_id': command_id, 'peer_id': 'synthetic-peer',
            'text': 'synthetic offline text', 'content_hash': digest('synthetic offline text'),
            'control_version': 1, 'expires_at': host.ledger.clock() + 60,
            'transport': 'oss', 'business_version': '2.2', 'source_watermark': 0}


@pytest.mark.parametrize('state', ['RECEIVING', 'UNKNOWN'])
def test_historical_download_uncertainty_preserves_business_unknown_guard(tmp_path, state):
    async def run():
        host = assemble(tmp_path)
        host._active = True
        carrier = host.bridge.adapters['oss']
        with carrier.store.db() as db:
            db.execute('INSERT INTO fg_download(scope,state) VALUES(?,?)',
                       (carrier.store.scope, state))
        calls = []
        def native(command):
            assert host.port.execution_permission(command) is True
            calls.append(command['command_id'])
            return {'status': 'unknown', 'evidence': 'synthetic-native-outcome-unknown'}
        command = command_for(host)
        assert not host.permission(command), 'An unclaimed command is never authorized'
        assert host.ledger.execute(command, native)['status'] == 'unknown'
        assert host.port.execution_permission(command) is False
        restarted = type(host.ledger)(host.ledger.path, host.binding, clock=host.ledger.clock)
        assert restarted.execute(command, native)['status'] == 'unknown'
        assert calls == [command['command_id']], 'Business UNKNOWN must not execute twice after restart'
        later = command_for(host, 'synthetic-later-command')
        assert restarted.execute(later, native)['status'] == 'unknown'
        assert calls == [command['command_id'], later['command_id']]
        with carrier.store.db() as db:
            rows = db.execute('SELECT state FROM fg_download WHERE scope=?', (carrier.store.scope,)).fetchall()
        assert [row['state'] for row in rows] == [state], 'Historical file uncertainty remains recorded'
    asyncio.run(run())


@pytest.mark.parametrize('barrier', ['owner_user_id', 'account_uid', 'workbench_id',
                                          'unpublished-inbound', 'newer-watermark', 'cancel'])
def test_guarded_default_preserves_identity_watermark_and_cancel(tmp_path, barrier):
    async def run():
        host = assemble(tmp_path)
        host._active = True
        command = command_for(host)
        def forbidden_native(command):
            pytest.fail('Rejected command reached native execution')
        if barrier in ('owner_user_id', 'account_uid', 'workbench_id'):
            command[barrier] = 'another-synthetic-identity'
            with pytest.raises(ProtocolError):
                host.ledger.execute(command, forbidden_native)
        else:
            if barrier == 'cancel':
                host.ledger.cancel(command)
            else:
                host.ledger.begin_inbound('synthetic-event', command['peer_id'])
                if barrier == 'newer-watermark':
                    host.ledger.finish_inbound_journal('synthetic-event', command['peer_id'], 1)
            assert host.ledger.execute(command, forbidden_native)['status'] == 'cancelled'
            assert host.port.execution_permission(command) is False
    asyncio.run(run())
