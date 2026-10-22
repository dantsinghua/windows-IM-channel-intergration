"""Current settings do not load retired transport credentials or historical bindings."""
import asyncio
import json
import pytest
from tests.test_collector_reporting_settings import collector_rig, FIELDS, URL, H
from qtrade_agent.ossbox.institution import InstitutionCollectorHost


def test_old_config_files_are_only_historical_and_cannot_supply_credentials(collector_rig):
    rig = collector_rig
    before = {path: path.read_bytes() for path in rig.protected_paths}
    response = rig.client.get(URL, headers=H())
    assert response.status_code == 200
    assert response.json()["relay"]["configured"] is False
    assert rig.agent.collector_reporting_settings.saved() is None
    assert not rig.agent.vault.reads
    assert {path: path.read_bytes() for path in rig.protected_paths} == before
    assert not rig.agent._started and not rig.agent.scheduler._started


@pytest.mark.parametrize("legacy_profile", [
    {"revision": "invalid", "credentials_ref": "vault://account/forbidden"},
    {"revision": "a" * 32, "credentials_ref": "vault://settings/ossbox/" + "a" * 32 + "/credentials",
        "sts_url": "https://old-sts.example.invalid", "allow_http": False, "bucket": "old-bucket"},
])
def test_historical_sts_profile_and_offline_vault_cannot_supply_or_veto_current_host(collector_rig, monkeypatch, legacy_profile):
    rig = collector_rig
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "1")
    calls = []
    class Host(InstitutionCollectorHost):
        def __init__(self): pass
        async def start(self): calls.append("host-start")
        async def stop(self): pass
    host = Host()
    async def factory(cls, settings, **kwargs):
        assert settings is rig.agent.collector_reporting_settings
        assert kwargs["legacy_inbox_disabled"]() is True and "box" not in kwargs
        calls.append("current-factory")
        return host
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", classmethod(factory))
    rig.store.settings_set("ossbox.config", legacy_profile)
    rig.agent.vault.offline = True
    async def run():
        try:
            await rig.agent._configure_xunjia_v22()
            assert rig.agent.xunjia_v22_host is host
            assert calls == ["current-factory", "host-start"]
            assert not rig.agent.vault.reads
            assert rig.store.settings_get("ossbox.config") == legacy_profile
            assert not rig.agent._started and not rig.agent.scheduler._started
        finally:
            await host.stop()
            rig.agent.xunjia_v22_host = None
    asyncio.run(run())


def test_current_save_never_writes_deployment_auth_or_unknown_journal(collector_rig):
    rig = collector_rig
    before = {path: path.read_bytes() for path in rig.protected_paths}
    response = rig.client.put(URL, headers=H(), json=FIELDS)
    assert response.status_code == 200, response.text
    changed = rig.client.put(URL, headers=H(), json={"key": "synthetic-rotated-key"})
    assert changed.status_code == 200
    assert changed.json()["applied"] is False
    assert {path: path.read_bytes() for path in rig.protected_paths} == before
    assert rig.agent.xunjia_v22_host is None
    assert "synthetic-rotated-key" not in json.dumps(rig.store.settings_list_prefix(""))
