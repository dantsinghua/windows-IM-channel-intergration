"""Synthetic same-UID source selection; retained records are never modified."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from qtrade_agent.ossbox.institution import InstitutionCollectorHost


def row(aid, uid='10001', state='running', channel='qidian'):
    return {'id': aid, 'self_uid': uid, 'state': state, 'channel': channel, 'self_nick': aid}


def host(rows):
    value = object.__new__(InstitutionCollectorHost)
    value.rows = lambda: rows
    value.active = True
    value.workbench = 'synthetic-source-wb'
    value.clock = lambda: 1800000000
    value.presence_provider = object()
    value.presence = {(r['id'],r['self_uid']):{'state':'online','confirmed_at':1799999999} for r in rows}
    value.hosts = {(r['id'],r['self_uid']):SimpleNamespace(_active=True,_intake_ok=True) for r in rows}
    return value


@pytest.mark.parametrize('state', ['running','degraded'])
def test_stopped_previous_source_does_not_hide_unique_current_source(state):
    rows = [row('aaa-old',state='stopped'),row('zzz-current',state=state),row('other',uid='20002')]
    original = deepcopy(rows)
    value = host(rows)
    assert value.online('zzz-current','10001')
    assert not value.online('aaa-old','10001')
    directory = {r['account_uid']:r for r in value.directory_rows()}
    assert directory['10001']['account_id'] == 'zzz-current' and directory['10001']['online']
    assert directory['20002']['account_id'] == 'other' and directory['20002']['online']
    assert rows == original


def test_multiple_active_sources_still_refuse_online_without_affecting_other_uid():
    rows = [row('old',state='stopped'),row('active-a'),row('active-b',state='degraded'),row('other',uid='20002')]
    value = host(rows)
    assert not value.online('active-a','10001') and not value.online('active-b','10001')
    directory = {r['account_uid']:r for r in value.directory_rows()}
    assert not directory['10001']['online'] and directory['10001']['capabilities'] == []
    assert directory['20002']['online']


def test_all_stopped_rows_stay_offline_and_retained():
    rows = [row('first',state='stopped'),row('second',state='stopped')]
    original = deepcopy(rows)
    value = host(rows)
    assert not any(value.online(r['id'],'10001') for r in rows)
    [entry] = value.directory_rows()
    assert entry['account_id'] == 'first' and not entry['online']
    assert rows == original


@pytest.mark.parametrize('guard', ['native-offline','native-expired','intake-stopped','host-stopped','collector-stopped'])
def test_unique_active_selection_preserves_existing_liveness_guards(guard):
    value = host([row('old',state='stopped'),row('current')])
    if guard == 'native-offline':value.presence[('current','10001')]['state'] = 'offline'
    if guard == 'native-expired':value.presence[('current','10001')]['confirmed_at'] = 1799999940
    if guard == 'intake-stopped':value.hosts[('current','10001')]._intake_ok = False
    if guard == 'host-stopped':value.hosts[('current','10001')]._active = False
    if guard == 'collector-stopped':value.active = False
    [entry] = value.directory_rows()
    assert entry['account_id'] == 'current' and not entry['online']
    assert entry['capabilities'] == []


def test_same_uid_in_distinct_channels_keeps_channel_scope():
    value = host([row('qd-old',state='stopped'),row('qd-new'),row('wx',channel='wechat')])
    assert value.online('qd-new','10001') and value.online('wx','10001')
    directory = {r['channel']:r for r in value.directory_rows()}
    assert directory['qidian']['account_id'] == 'qd-new' and directory['qidian']['online']
    assert directory['wechat']['account_id'] == 'wx' and directory['wechat']['online']
