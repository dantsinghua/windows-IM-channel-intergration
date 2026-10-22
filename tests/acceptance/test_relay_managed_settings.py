"""Current collector credentials: real local settings/vault, synthetic pending only.
Retired shared MQ manager cases are mapped to the current independent collector API.
"""
from __future__ import annotations
import asyncio,hashlib,json,os,stat
from pathlib import Path
from types import SimpleNamespace
import pytest
from starlette.testclient import TestClient
from conftest import make_rig
from qtrade_agent.ossbox.collector_settings import CollectorReportingSettings
from qtrade_agent.ossbox.errors import OssboxSettingsError
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.store import Store
from qtrade_agent.vault_client import FakeVault
from tests.test_collector_reporting_settings import synthetic_pending_connection
KEY='synthetic-remote-business-key';SM4='0123456789abcdef';ROTATED='fedcba9876543210'
BASE='https://remote-mq.example.invalid';WORKBENCH='synthetic-managed-workbench'
FIELDS={'workbench_name':'Synthetic collector','base_url':BASE,'key':KEY,'sm4_key':SM4}
def private_write(path,text):path.write_text(text,encoding='utf-8');path.chmod(0o600)
def revision(path):return hashlib.sha256(path.read_bytes()).hexdigest()
@pytest.fixture
def world(tmp_path,monkeypatch):
    synthetic_pending_connection(monkeypatch)
    from relay.file_gateway import FileGatewayClient
    store=Store(str(tmp_path/'collector.sqlite3')).open();vault=FakeVault()
    settings=CollectorReportingSettings(store,vault,seed={'workbench_id':WORKBENCH})
    local=tmp_path/'unrelated-local-auth.yaml';private_write(local,'key: unrelated-synthetic-auth\n')
    calls=[]
    def request(client,operation,*args,**kwargs):
        assert operation=='pending','Settings must not consume/upload or send native text'
        calls.append((client.base_url,client.auth.key,client.auth.sm4_key))
        return 200,{},json.dumps({'pending':7}).encode()
    monkeypatch.setattr(FileGatewayClient,'_request',request)
    asyncio.run(settings.save(FIELDS,actor='synthetic-test'))
    yield SimpleNamespace(root=tmp_path,store=store,vault=vault,settings=settings,local=local,calls=calls)
    store.close()

def test_current_view_and_pending_use_saved_remote_credentials_without_legacy_manager(world):
    settings=world.settings;before=world.store.settings_list_prefix('');local=world.local.read_bytes()
    view=settings.view();assert view['configured'] and view['base_url']==BASE
    result=asyncio.run(settings.test({}));assert result['authenticated']
    assert world.calls==[(BASE,KEY,SM4.encode())]
    assert world.store.settings_list_prefix('')==before and world.local.read_bytes()==local
    for secret in (KEY,SM4):assert secret not in json.dumps([view,result])

def test_current_sm4_rotation_updates_vault_revision_not_unrelated_files(world):
    settings=world.settings;before=settings.saved();local=world.local.read_bytes()
    asyncio.run(settings.save({'sm4_key':ROTATED},actor='synthetic-test'))
    assert settings.saved()['revision']!=before['revision']
    assert asyncio.run(settings._credentials())=={'key':KEY,'sm4_key':ROTATED}
    assert world.local.read_bytes()==local and world.calls==[]
    assert KEY not in json.dumps(settings.saved()) and ROTATED not in json.dumps(settings.saved())

def test_changed_business_key_is_saved_without_recreating_retired_router(world):
    original=world.settings.workbench_id
    asyncio.run(world.settings.save({'key':'synthetic-new-institution'},actor='synthetic-test'))
    assert asyncio.run(world.settings._credentials())['key']=='synthetic-new-institution'
    assert world.settings.workbench_id==original and world.calls==[]
    assert not hasattr(world.settings,'host')

def test_another_editor_change_is_seen_by_existing_view_and_pending(world):
    other=CollectorReportingSettings(world.store,world.vault)
    asyncio.run(other.save({'base_url':'https://updated.example.invalid','sm4_key':ROTATED},actor='synthetic-editor'))
    assert world.settings.view()['base_url']=='https://updated.example.invalid'
    assert asyncio.run(world.settings.test({}))['authenticated']
    assert world.calls[-1]==('https://updated.example.invalid',KEY,ROTATED.encode())

def test_concurrent_saves_use_one_coherent_current_credential_record(world):
    async def run():
        await asyncio.gather(world.settings.save({'key':'synthetic-key-a','sm4_key':SM4},actor='a'),
                             world.settings.save({'key':'synthetic-key-b','sm4_key':ROTATED},actor='b'))
        return await world.settings._credentials()
    final=asyncio.run(run())
    assert final in ({'key':'synthetic-key-a','sm4_key':SM4},{'key':'synthetic-key-b','sm4_key':ROTATED})
    assert world.settings.saved()['workbench_id']==WORKBENCH and world.calls==[]

def test_saving_credentials_does_not_mark_unstarted_runtime_applied(world):
    settings=world.settings
    assert settings.applied_revision is None
    asyncio.run(settings.save({'sm4_key':ROTATED},actor='synthetic-test'))
    assert settings.applied_revision is None and settings.saved()['revision']
    assert settings.view()['configured'] and world.calls==[]

def test_identical_current_save_keeps_revision_and_does_not_call_gateway(world):
    before=world.settings.saved();credentials=asyncio.run(world.settings._credentials())
    asyncio.run(world.settings.save(FIELDS,actor='synthetic-test'))
    assert world.settings.saved()==before and asyncio.run(world.settings._credentials())==credentials
    assert world.calls==[]


@pytest.mark.parametrize("succeeds", [True, False])
def test_api_applied_requires_successful_current_host_and_matching_revision(tmp_path, monkeypatch, succeeds):
    for name in tuple(os.environ):
        if name.startswith(("OSS_", "OSSBOX_", "XUNJIA_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XUNJIA_COLLECTOR_VERSION", "2.2")
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "1")
    rig = make_rig(tmp_path)
    settings = rig.agent.collector_reporting_settings
    class Host(InstitutionCollectorHost):
        def __init__(self):
            self.active = False
            self.applied_revision = None
        async def start(self):
            if not succeeds:
                raise RuntimeError("synthetic host start failure")
            self.active = True
            self.applied_revision = settings.saved()["revision"]
            settings.applied_revision = self.applied_revision
        async def stop(self): self.active = False
        def status(self):
            return {"contract": "its-relay-path-v1", "active": self.active,
                "applied_revision": self.applied_revision}
    host = Host()
    async def factory(cls, selected, **kwargs):
        assert selected is settings and kwargs["legacy_inbox_disabled"]() is True
        return host
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", classmethod(factory))
    async def run():
        try:
            assert settings.applied_revision is None
            await settings.save(FIELDS, actor="independent-acceptance")
            current = settings.saved()["revision"]
            await rig.agent._configure_xunjia_v22()
            assert settings.applied_revision == (current if succeeds else None)
            rig.store.upsert_api_client(app_id="revision-admin", name="independent-test", level="admin", token="revision-isolated-admin")
            with TestClient(rig.agent.create_api(), client=("127.0.0.1", 41000)) as client:
                headers = {"Authorization": "Bearer revision-isolated-admin"}
                response = client.get("/api/v1/settings/ossbox", headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["applied"] is succeeds
                await settings.save({"sm4_key": ROTATED}, actor="independent-rotation")
                rotated = client.get("/api/v1/settings/ossbox", headers=headers)
                assert rotated.status_code == 200
                assert rotated.json()["applied"] is False
                assert settings.applied_revision == (current if succeeds else None)
            assert not rig.agent._started and not rig.agent.scheduler._started
            if not succeeds:
                assert rig.agent.oss_start_error == "institution_collector_configuration_unavailable"
        finally:
            await rig.agent.stop()
    asyncio.run(run())
