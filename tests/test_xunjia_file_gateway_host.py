"""QTrade assembly contract for its-relay, with no network or account start."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from qtrade_agent.ossbox.xunjia_v22 import XunjiaV22Host


def private_config(tmp_path, monkeypatch, **gateway_changes):
    (tmp_path / 'seal').write_text((b'k' * 16).hex())
    (tmp_path / 'sign').write_text((b's' * 32).hex())
    (tmp_path / 'auth.yaml').write_text(json.dumps({'auth': {
        'key': 'synthetic-business-key', 'sm4_key': '0123456789abcdef'}}))
    gateway = {'uplink_url': 'https://up.invalid',
               'downlink_url': 'https://down.invalid', 'auth_file': 'auth.yaml',
               **gateway_changes}
    config = {'owner_user_id': 'synthetic-owner', 'account_uid': 'synthetic-account',
              'workbench_id': 'synthetic-wb', 'seal_key_file': 'seal',
              'sign_key_file': 'sign', 'file_gateway': gateway}
    path = tmp_path / 'collector.json'
    path.write_text(json.dumps(config))
    monkeypatch.setenv('XUNJIA_COLLECTOR_VERSION', '2.2')
    monkeypatch.setenv('XUNJIA_COLLECTOR_CONFIG_FILE', str(path))
    monkeypatch.delenv('OSS_WORKBENCH_ID', raising=False)


def assemble(tmp_path):
    return XunjiaV22Host.from_env(
        store=SimpleNamespace(outbox_seq_bounds=lambda: (0, 0)),
        bus=SimpleNamespace(supports_xunjia_v22_guard=True),
        loop=asyncio.get_running_loop(), box=None, data_dir=tmp_path / 'data',
        legacy_inbox_disabled=lambda: True)


def test_gateway_host_without_oss_or_sts_keeps_native_guard_disabled(tmp_path, monkeypatch):
    private_config(tmp_path, monkeypatch)
    async def run():
        host = assemble(tmp_path)
        carrier = host.bridge.adapters['oss']
        assert carrier.role == 'collector'
        assert carrier.inbound.base_url == 'https://down.invalid'
        assert carrier.outbound.base_url == 'https://up.invalid'
        assert host.bridge.object_store is None
        assert not host.allow_native_send and not host.port.allow_native_send
        assert host.port.execution_permission == host.permission
        assert not host.permission({}) and not host._active
        assert host.bridge.native_send({'business_version': '2.2'})['status'] == 'failed'
    asyncio.run(run())


@pytest.mark.parametrize('changes,code', [
    ({'downlink_url': 'https://up.invalid'}, 'SEPARATE_QUEUES_REQUIRED'),
    ({'allow_shared_test_queue': True}, 'CONFIG_INVALID'),
])
def test_gateway_host_rejects_shared_fifo_and_production_escape_hatch(
        tmp_path, monkeypatch, changes, code):
    private_config(tmp_path, monkeypatch, **changes)
    async def run():
        with pytest.raises(RuntimeError, match=code):
            assemble(tmp_path)
    asyncio.run(run())


def test_native_permission_rechecks_gateway_receive_journal(tmp_path, monkeypatch):
    private_config(tmp_path, monkeypatch)
    async def run():
        host = assemble(tmp_path)
        host.allow_native_send = host._active = True
        monkeypatch.setattr(host.ledger, 'permission', lambda command: True)
        carrier = host.bridge.adapters['oss']
        assert host.permission({})
        with carrier.store.db() as db:
            db.execute('INSERT INTO fg_download(scope,state) VALUES(?,?)',
                       (carrier.store.scope, 'RECEIVING'))
        assert not host.permission({}), 'An uncertain destructive download blocks the final native guard'
        assert host.port.execution_permission({}) is False
    asyncio.run(run())
