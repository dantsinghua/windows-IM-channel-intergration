"""Legacy auth and explicit workstation configuration remain separate contracts."""
from tests.test_collector_reporting_settings import collector_rig, FIELDS, URL, H
from relay.file_gateway import FileGatewayClient


def test_deployed_legacy_config_is_not_replaced_by_empty_local_draft(collector_rig):
    rig = collector_rig
    data = rig.client.get(URL, headers=H()).json()
    assert data["relay"]["config_file"] == "config.yaml"
    assert data["relay"]["configured"] is True
    assert "workbench_name" not in data["relay"]
    assert rig.agent.collector_reporting_settings.saved() is None


def test_explicit_workstation_save_and_later_key_rotation_preserve_legacy_files(collector_rig):
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


def test_new_form_draft_test_does_not_persist_or_switch_legacy_configuration(collector_rig, monkeypatch):
    rig = collector_rig
    monkeypatch.setattr(FileGatewayClient, "_request", lambda *a, **k: (200, {}, b'{"pending":0}'))
    before = {p: p.read_bytes() for p in rig.protected_paths}
    response = rig.client.post(URL + "/test", headers=H(), json=FIELDS)
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert rig.agent.collector_reporting_settings.saved() is None
    assert rig.client.get(URL, headers=H()).json()["relay"]["config_file"] == "config.yaml"
    assert {p: p.read_bytes() for p in rig.protected_paths} == before


def test_explicit_institution_mode_never_falls_back_to_legacy_auth(collector_rig, monkeypatch):
    rig = collector_rig
    monkeypatch.setenv("XUNJIA_INSTITUTION_COLLECTOR", "1")
    data = rig.client.get(URL, headers=H()).json()
    assert "workbench_name" in data["relay"]
    assert "config_file" not in data["relay"]
    assert data["applied"] is False
