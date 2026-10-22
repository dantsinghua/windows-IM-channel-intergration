"""One collector-owned contract; retired files and uncertain state stay unchanged."""
from tests.test_collector_reporting_settings import collector_rig, FIELDS, URL, H
from relay.file_gateway import FileGatewayClient


def test_retired_configuration_is_not_loaded_or_exposed_as_current(collector_rig):
    rig = collector_rig
    data = rig.client.get(URL, headers=H()).json()
    assert data["mode"] == "its_relay"
    assert "workbench_name" in data["relay"]
    assert "config_file" not in data["relay"] and "data" not in data
    assert "legacy_available" not in data
    assert data["relay"]["configured"] is False
    assert rig.agent.collector_reporting_settings.saved() is None


def test_current_save_and_key_rotation_preserve_retired_files_and_unknown_state(collector_rig):
    rig = collector_rig
    before = {p: p.read_bytes() for p in rig.protected_paths}
    assert rig.client.put(URL, headers=H(), json=FIELDS).status_code == 200
    response = rig.client.put(URL, headers=H(), json={"sm4_key": "fedcba9876543210"})
    assert response.status_code == 200
    assert response.json()["relay"]["workbench_name"] == FIELDS["workbench_name"]
    assert response.json()["state"] == "pending_alignment"
    assert response.json()["applied"] is False
    assert {p: p.read_bytes() for p in rig.protected_paths} == before
    assert rig.agent.xunjia_v22_host is None


def test_draft_pending_does_not_persist_or_activate_retired_configuration(collector_rig, monkeypatch):
    rig = collector_rig
    monkeypatch.setattr(FileGatewayClient, "_request", lambda *a, **k: (200, {}, b'{"pending":0}'))
    before = {p: p.read_bytes() for p in rig.protected_paths}
    response = rig.client.post(URL + "/test", headers=H(), json=FIELDS)
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert rig.agent.collector_reporting_settings.saved() is None
    assert rig.client.get(URL, headers=H()).json()["relay"]["configured"] is False
    assert {p: p.read_bytes() for p in rig.protected_paths} == before


def test_unsetting_institution_flag_cannot_restore_old_settings_contract(collector_rig, monkeypatch):
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "0")
    data = collector_rig.client.get(URL, headers=H()).json()
    assert "workbench_name" in data["relay"] and "config_file" not in data["relay"]
    assert data["applied"] is False
