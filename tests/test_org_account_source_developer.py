"""Synthetic account-bound organization source regressions; no real ADB."""
import sys
from types import SimpleNamespace
import pytest
from qtrade_agent.adapters.qidian.org import read_account_org, OrgReadError
from qtrade_agent.app import AgentApp
from qtrade_agent.store.store import Store


def remote(uid='3000000001'):
    return SimpleNamespace(path='/data/data/com.tencent.qidian/databases/'+uid+'.db',
        adb='synthetic-adb',serial='synthetic-device',server_port=16000,_shell=lambda _: '',
        exists=lambda:True,list_contact_source_rows=lambda:[])


def test_remote_organization_uses_current_material_without_old_key_config(tmp_path,monkeypatch):
    calls=[]
    result={'org_state':'ready','org':{'departments':[],'members':[]},'org_updated_at':123,'org_error':None}
    def read(source,uid,**kwargs):calls.append((source,uid));return result
    monkeypatch.setitem(sys.modules,'qtrade_agent.adapters.qidian.org_native',SimpleNamespace(read_native_org=read))
    source=remote()
    assert read_account_org(source,'3000000001',tmp_path/'no-old-config.json')==result
    assert calls==[(source,'3000000001')]


def test_same_account_in_two_active_sources_is_rejected_before_any_native_read(tmp_path):
    store=Store(str(tmp_path/'agent.db')).open()
    try:
        for slot in ('qd01','qd02'):store.ensure_account(slot,'qidian',state='running',self_uid='3000000001')
        app=object.__new__(AgentApp);app.store=store
        app._maindb_for_uid=lambda *args:pytest.fail('conflict must reject before native source access')
        with pytest.raises(OrgReadError,match='ORG_ACCOUNT_SOURCE_CONFLICT'):
            app._xunjia_v22_contacts(store.get_account_full('qd01'))
    finally:store.close()


def test_factory_cannot_redirect_current_account_to_another_native_source(tmp_path):
    store=Store(str(tmp_path/'agent.db')).open()
    try:
        store.ensure_account('qd01','qidian',state='running',self_uid='3000000001')
        store.upsert_runtime('qd01',kind='redroid',adb_serial='current-device',container_id='current-container')
        app=object.__new__(AgentApp);app.store=store
        source=remote();source.exists=lambda:pytest.fail('wrong source must not be read')
        app._maindb_for_uid=lambda *args:source
        with pytest.raises(OrgReadError,match='ORG_ACCOUNT_SOURCE_CHANGED'):
            app._xunjia_v22_contacts(store.get_account_full('qd01'))
    finally:store.close()


def test_stopped_old_slot_does_not_block_current_account_and_multiple_uids_are_independent(tmp_path):
    store=Store(str(tmp_path/'agent.db')).open()
    try:
        store.ensure_account('qd01','qidian',state='stopped',self_uid='3000000001')
        store.ensure_account('qd02','qidian',state='running',self_uid='3000000001')
        store.ensure_account('qd03','qidian',state='running',self_uid='3000000002')
        store.upsert_runtime('qd02',kind='redroid',adb_serial='device-a',container_id='container-a')
        store.upsert_runtime('qd03',kind='redroid',adb_serial='device-b',container_id='container-b')
        app=object.__new__(AgentApp);app.store=store
        a=app._xunjia_org_source_identity(store.get_account_full('qd02'))
        b=app._xunjia_org_source_identity(store.get_account_full('qd03'))
        assert a[1]=='3000000001' and a[2]=='device-a'
        assert b[1]=='3000000002' and b[2]=='device-b'
        store.upsert_runtime('qd02',kind='redroid',adb_serial='new-device',container_id='new-container')
        assert app._xunjia_org_source_identity(store.get_account_full('qd02'))!=a
        assert app._xunjia_org_source_identity(store.get_account_full('qd03'))==b
    finally:store.close()
