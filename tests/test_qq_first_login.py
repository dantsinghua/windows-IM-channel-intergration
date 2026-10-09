"""05 K1-K6/U3、R6-83：首次扫码、取消/迟到、身份和正式装配。"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from qtrade_agent.runtime.backends import docker_run_argv
from test_qq_first_login_common import first_login_rig, no_external_io, png, start_waiting, states


async def test_first_start_produces_fresh_png_with_two_minute_expiry(first_login_rig):
    r = first_login_rig
    p = await start_waiting(r)
    assert p.get('qrcode_png_b64'), '首次启动必须提供可显示的新二维码'
    assert r.qr.calls == [(r.id, True)]
    assert base64.b64decode(p['qrcode_png_b64']) == png(1)
    assert int(datetime.fromisoformat(p['expires_at']).timestamp() * 1000) == r.clock() + 120000
    assert states(r)[:4] == ['created', 'provisioning', 'starting', 'login_required']
    assert r.accounts.get(r.id)['state'] == 'login_required'
    assert r.accounts.get(r.id)['self_uid'] is None
    spec = r.containers.containers[f'qtrade-{r.id}'].spec
    assert all(f'127.0.0.1:{host}:{inside}' in docker_run_argv(spec) for host, inside in spec.ports.items())


async def test_thirty_second_refresh_never_runs_early(first_login_rig):
    r = first_login_rig
    before = await start_waiting(r)
    r.clock.advance(29999)
    await r.accounts.poll_qq_logins()
    assert len(r.qr.calls) == 1
    r.clock.advance(1)
    await r.accounts.poll_qq_logins()
    after = r.accounts.prompt(r.id)
    assert after['qrcode_png_b64'] != before['qrcode_png_b64']
    assert after['login_session_id'] == before['login_session_id']


async def test_get_15_returns_new_code_and_stale_session_has_no_side_effect(first_login_rig):
    r = first_login_rig
    before = await start_waiting(r)
    r.store.upsert_api_client(app_id='console', name='test', level='admin', token='fake-qq-token')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=r.agent.create_api()), base_url='http://test') as client:
        headers = {'Authorization': 'Bearer fake-qq-token'}
        response = await client.get(f'/api/v1/accounts/{r.id}/prompt', headers=headers)
        assert response.status_code == 200
        assert response.json()['qrcode_png_b64'] != before['qrcode_png_b64']
        count = len(r.qr.calls)
        stale = await client.get(f'/api/v1/accounts/{r.id}/prompt?login_session_id=obsolete', headers=headers)
        assert stale.status_code == 200 and stale.json()['kind'] is None
        assert len(r.qr.calls) == count


@pytest.mark.parametrize('failure', ['error', 'invalid_png', 'same_png'])
async def test_refresh_failure_removes_old_code_and_exposes_retry(first_login_rig, failure, caplog):
    r = first_login_rig
    previous = await start_waiting(r)
    if failure == 'error':
        r.qr.error = RuntimeError('private-upstream-body-' + previous['qrcode_png_b64'])
    else:
        r.qr.value = b'not png' if failure == 'invalid_png' else base64.b64decode(previous['qrcode_png_b64'])
    result = await r.accounts.refresh_prompt(r.id)
    assert not result.get('qrcode_png_b64')
    assert '重试' in result['text']
    assert 'private-upstream-body' not in caplog.text
    assert previous['qrcode_png_b64'] not in caplog.text


async def test_scan_reconnects_new_onebot_identity_before_running(first_login_rig):
    r = first_login_rig
    await start_waiting(r)
    r.first.online = True
    r.second.nickname = 'confirmed-after-restart'
    await r.accounts.poll_qq_logins()
    row = r.accounts.get(r.id)
    assert row['state'] == 'running' and row['self_uid'] == '123456789'
    assert row['self_nick'] == 'confirmed-after-restart'
    assert r.first.close_calls == 1 and len(r.issued) == 2
    assert 'get_login_info' in r.second.actions
    assert states(r)[-2:] == ['logging_in', 'running']
    assert r.accounts.prompt(r.id) == {'kind': None}
    assert r.fs.read_json(f'{r.agent.runtime.data_dir(r.id)}/napcat_config/webui.json')['disableWebUI'] is True


@pytest.mark.parametrize('uid', [False, True, 0, -1, '', ' 123456789', '１２３４', '1.2', None])
async def test_non_numeric_or_boolean_identity_never_completes(first_login_rig, uid):
    r = first_login_rig
    await start_waiting(r)
    r.first.online = True
    r.first.self_id = uid
    await r.accounts.poll_qq_logins()
    assert r.accounts.get(r.id)['state'] == 'login_required'
    assert r.accounts.get(r.id)['self_uid'] is None
    assert ('stop', f'qtrade-{r.id}') not in r.containers.calls


async def test_wrong_scan_identity_rejected_without_container_closing(first_login_rig):
    r = first_login_rig
    await start_waiting(r)
    r.first.online, r.first.self_id = True, 999999999
    await r.accounts.poll_qq_logins()
    row = r.accounts.get(r.id)
    assert row['state'] == 'error' and row['state_code'] == 'BAD_CREDENTIAL'
    assert row['self_uid'] is None
    assert ('stop', f'qtrade-{r.id}') not in r.containers.calls


@pytest.mark.parametrize('online,uid', [(False, 123456789), (True, 999999999)])
async def test_post_close_offline_or_wrong_identity_cannot_be_running(first_login_rig, online, uid):
    r = first_login_rig
    await start_waiting(r)
    r.first.online, r.second.online, r.second.self_id = True, online, uid
    await r.accounts.poll_qq_logins()
    row = r.accounts.get(r.id)
    assert row['state'] == 'error' and row['state_code'] == 'ONEBOT_UNREACHABLE'
    assert row['self_uid'] is None


async def test_quick_login_still_emits_all_states_without_qr(first_login_rig):
    r = first_login_rig
    r.first.online = True
    await start_waiting(r)
    assert states(r) == ['created', 'provisioning', 'starting', 'login_required', 'logging_in', 'running']
    assert r.qr.calls == []


async def test_stop_start_with_saved_login_keeps_complete_state_sequence(first_login_rig):
    r = first_login_rig
    r.first.online = True
    await start_waiting(r)
    await r.accounts.stop(r.id, actor='test')
    await r.accounts.wait_idle(r.id)
    count = len(states(r))
    await start_waiting(r)
    assert states(r)[count:] == ['provisioning', 'starting', 'login_required', 'logging_in', 'running']
    assert r.qr.calls == []


async def test_expired_saved_login_returns_qr_without_recreating_or_deleting_volume(first_login_rig):
    r = first_login_rig
    r.first.online = True
    await start_waiting(r)
    await r.accounts.stop(r.id, actor='test')
    await r.accounts.wait_idle(r.id)
    name = f'qtrade-{r.id}'
    cid = r.containers.containers[name].id
    r.second.online = False
    r.containers.calls.clear()
    prompt = await start_waiting(r)
    assert prompt.get('qrcode_png_b64')
    assert r.containers.containers[name].id == cid
    assert not any(action in ('create', 'remove') for action, _ in r.containers.calls)


async def test_timeout_stops_container_and_ceases_qr_refresh(first_login_rig):
    r = first_login_rig
    await start_waiting(r)
    r.clock.advance(120000)
    await r.accounts.poll_qq_logins()
    row = r.accounts.get(r.id)
    assert row['state'] == 'stopped' and row['state_reason'] == '扫码超时,已停止'
    assert not r.containers.containers[f'qtrade-{r.id}'].running
    count = len(r.qr.calls)
    r.clock.advance(30000)
    await r.accounts.poll_qq_logins()
    assert len(r.qr.calls) == count


@pytest.mark.parametrize('ending', ['cancel', 'stop', 'timeout', 'new_session'])
async def test_late_qr_cannot_restore_ended_attempt(first_login_rig, ending):
    r = first_login_rig
    old = await start_waiting(r)
    r.qr.entered.clear()
    r.qr.gate = asyncio.Event()
    refresh = asyncio.create_task(r.accounts.refresh_prompt(r.id))
    await asyncio.wait_for(r.qr.entered.wait(), 1)
    if ending in ('cancel', 'new_session'):
        await r.accounts.login_cancel(r.id, old['login_session_id'], actor='test')
    elif ending == 'stop':
        await r.accounts.stop(r.id, actor='test')
        await r.accounts.wait_idle(r.id)
    else:
        r.clock.advance(120000)
        await r.accounts.poll_qq_logins()
    if ending == 'new_session':
        r.second.online = False
        gate = r.qr.gate
        r.qr.gate = None
        await r.accounts.login(r.id, {}, actor='test')
        await r.accounts.wait_idle(r.id)
        newest = r.accounts.prompt(r.id)
        assert newest['login_session_id'] != old['login_session_id']
    else:
        gate = r.qr.gate
        newest = r.accounts.prompt(r.id)
    gate.set()
    assert await refresh == {'kind': None}
    assert r.accounts.prompt(r.id) == newest
    assert r.accounts.get(r.id)['self_uid'] is None


async def test_offline_requires_explicit_12_and_h08_does_not_override_scan(first_login_rig):
    r = first_login_rig
    await start_waiting(r)
    for _ in range(3):
        r.clock.advance(35000)
        await r.agent.qqhealth.check()
    assert r.accounts.get(r.id)['state_code'] == 'WAIT_QRCODE'
    assert not r.agent.qqhealth.last
    r.first.online = True
    await r.accounts.poll_qq_logins()
    r.accounts.mark_offline(r.id, 'TOKEN_EXPIRED')
    before = list(r.containers.calls)
    count = len(r.qr.calls)
    await r.accounts.poll_qq_logins()
    assert r.containers.calls == before and len(r.qr.calls) == count
    r.second.online = False
    await r.accounts.login(r.id, {}, actor='test')
    await r.accounts.wait_idle(r.id)
    assert len(r.qr.calls) == count + 1
    assert r.accounts.get(r.id)['state_code'] == 'WAIT_QRCODE'


async def test_empty_account_rebuild_preserves_volumes_configuration_and_limits(first_login_rig, monkeypatch):
    r = first_login_rig
    r.store.con.execute('UPDATE accounts SET identity_json=? WHERE id=?', ('{}', r.id))
    await start_waiting(r)
    name = f'qtrade-{r.id}'
    previous = r.containers.containers[name].spec
    original_remove = r.containers.remove
    removed = []
    async def remove(name, *, volumes):
        removed.append(volumes)
        await original_remove(name, volumes=volumes)
    monkeypatch.setattr(r.containers, 'remove', remove)
    r.fs.put(f'{r.agent.runtime.data_dir(r.id)}/qq_data/fingerprint', 17)
    r.first.online = True
    await r.accounts.poll_qq_logins()
    current = r.containers.containers[name].spec
    assert r.accounts.get(r.id)['state'] == 'running'
    assert current.env['ACCOUNT'] == '123456789' and removed == [False]
    assert (current.volumes, current.ports, current.image, current.mem_limit_mb) == (previous.volumes, previous.ports, previous.image, previous.mem_limit_mb)
    assert r.fs.files[f'{r.agent.runtime.data_dir(r.id)}/qq_data/fingerprint'] == 17


@pytest.mark.parametrize('empty_identity', [True, False])
@pytest.mark.parametrize('wrong_field', ['container_id', 'container_name'])
async def test_foreign_container_ownership_denies_rebuild_and_mutation(first_login_rig, wrong_field, empty_identity):
    r = first_login_rig
    if empty_identity:
        r.store.con.execute('UPDATE accounts SET identity_json=? WHERE id=?', ('{}', r.id))
    await start_waiting(r)
    r.store.upsert_runtime(r.id, kind='napcat', **{wrong_field: 'foreign'})
    before = json.dumps(r.fs.json_files, sort_keys=True)
    r.containers.calls.clear()
    r.first.online = True
    await r.accounts.poll_qq_logins()
    assert r.accounts.get(r.id)['state'] == 'error'
    assert not any(call[0] in ('stop', 'remove', 'create', 'start') for call in r.containers.calls)
    assert json.dumps(r.fs.json_files, sort_keys=True) == before


async def test_registered_uid_but_missing_container_account_rebuilds_before_running(first_login_rig):
    r = first_login_rig
    await start_waiting(r)
    name = f'qtrade-{r.id}'
    r.containers.containers[name].spec.env = {}
    r.first.online = True
    await r.accounts.poll_qq_logins()
    assert ('remove', name) in r.containers.calls
    assert r.containers.containers[name].spec.env.get('ACCOUNT') == '123456789'
    assert r.accounts.get(r.id)['state'] == 'running'


@pytest.mark.parametrize('ending', ['cancel', 'stop', 'timeout', 'new_session'])
async def test_late_identity_response_cannot_complete_ended_attempt(first_login_rig, monkeypatch, ending):
    r = first_login_rig
    old = await start_waiting(r)
    r.first.online = True
    client = r.agent.adapters['qq'].session_of(r.id).client
    original = client.call_action
    entered, release = asyncio.Event(), asyncio.Event()
    async def gated(action, *args, **kw):
        if action == 'get_login_info':
            entered.set()
            await release.wait()
            return {'user_id': 123456789, 'nickname': 'late'}
        return await original(action, *args, **kw)
    monkeypatch.setattr(client, 'call_action', gated)
    poll = asyncio.create_task(r.accounts.poll_qq_logins())
    await asyncio.wait_for(entered.wait(), 1)
    if ending in ('cancel', 'new_session'):
        await r.accounts.login_cancel(r.id, old['login_session_id'], actor='test')
    elif ending == 'stop':
        await r.accounts.stop(r.id, actor='test')
        await r.accounts.wait_idle(r.id)
    else:
        r.clock.advance(120000)
        await r.accounts.poll_qq_logins()
    if ending == 'new_session':
        r.second.online = False
        await r.accounts.login(r.id, {}, actor='test')
        await r.accounts.wait_idle(r.id)
    row_before = r.accounts.get(r.id)
    calls_before = list(r.containers.calls)
    release.set()
    await poll
    assert r.accounts.get(r.id) == row_before
    assert r.containers.calls == calls_before
    assert r.accounts.get(r.id)['self_uid'] is None


async def test_first_start_writes_onebot_configuration_and_retains_token(first_login_rig):
    r = first_login_rig
    path = f'{r.agent.runtime.data_dir(r.id)}/napcat_config/onebot11_123456789.json'
    r.fs.write_json(path, {'kept': 'custom', 'network': {'websocketServers': [{'port': 3001, 'token': 'fake-retained-token'}]}})
    await start_waiting(r)
    config = r.fs.read_json(path)
    ws = next(s for s in config['network']['websocketServers'] if s['port'] == 3001)
    assert config['kept'] == 'custom' and ws['enable'] and ws['reportSelfMessage']
    assert ws['token'] == r.first.received_token == 'fake-retained-token'
    assert any(s['port'] == 3000 and s['enable'] for s in config['network']['httpServers'])
    assert f'{r.agent.runtime.data_dir(r.id)}/napcat_config/napcat.json' in r.fs.json_files


async def test_k6_expiry_closes_only_at_deadline(first_login_rig):
    r = first_login_rig
    r.first.online = True
    await start_waiting(r)
    until = r.clock() + 600000
    await r.accounts.set_webui(r.id, True, until_ms=until, actor='test')
    r.containers.calls.clear()
    r.clock.advance(599999)
    await r.accounts.expire_qq_webui()
    assert not r.containers.calls
    r.clock.advance(1)
    await r.accounts.expire_qq_webui()
    assert ('stop', f'qtrade-{r.id}') in r.containers.calls
    assert ('start', f'qtrade-{r.id}') in r.containers.calls
    assert r.store.get_runtime(r.id)['webui_published_until_ms'] is None
    assert not r.agent.runtime.napcat_webui_enabled(r.accounts.get(r.id))


async def test_k6_failed_close_keeps_registration_until_actual_retry(first_login_rig, monkeypatch):
    r = first_login_rig
    r.first.online = True
    await start_waiting(r)
    until = r.clock() + 600000
    await r.accounts.set_webui(r.id, True, until_ms=until, actor='test')
    original_stop = r.containers.stop
    monkeypatch.setattr(r.containers, 'stop', AsyncMock(return_value=None))
    r.clock.advance(600000)
    await r.accounts.expire_qq_webui()
    assert r.store.get_runtime(r.id)['webui_published_until_ms'] == until
    assert r.containers.containers[f'qtrade-{r.id}'].running
    monkeypatch.setattr(r.containers, 'stop', original_stop)
    await r.accounts.expire_qq_webui()
    assert r.store.get_runtime(r.id)['webui_published_until_ms'] is None


def test_default_app_wires_real_login_backend_without_starting_services(tmp_path, clock, no_external_io, monkeypatch):
    import qtrade_agent.app as app_module
    from qtrade_agent.adapters.qq.login import NapCatLoginBackend
    from qtrade_agent.config import AgentConfig
    from qtrade_agent.runtime import FakeAdb, FakeContainers
    from qtrade_agent.runtime.runtime import FakeFs
    from qtrade_agent.vault_client import FakeVault
    from qtrade_agent.webhook import FakeHttp
    from qtrade_agent.winagent_client import FakeWinAgent
    containers, wa = FakeContainers(), FakeWinAgent()
    monkeypatch.setattr(app_module, 'DockerCliBackend', lambda: containers)
    agent = app_module.AgentApp(AgentConfig(), db_path=str(tmp_path / 'assembly.db'), data_dir=str(tmp_path),
                               clock=clock, adb=FakeAdb(), fs=FakeFs(), vault=FakeVault(), http=FakeHttp(),
                               winagent_transport=wa, winagent_base_url='http://winagent.fake:17610',
                               winagent_token=wa.token, wsl_total_mb=11264).open()
    try:
        assert isinstance(agent.accounts._qq_login_backend, NapCatLoginBackend)
        assert agent.accounts._qq_login_backend._token_for == agent.runtime.napcat_webui_token
        assert agent.accounts._qq_login_backend._read_png == agent.runtime.read_napcat_qrcode
        assert agent.scheduler.jobs['qq_login_probe'].fn == agent.accounts.poll_qq_logins
        assert agent.scheduler.jobs['qq_webui_expiry'].fn == agent.accounts.expire_qq_webui
        assert containers.calls == []
    finally:
        agent.store.close()
