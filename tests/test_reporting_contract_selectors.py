"""Explicit editor selection never replaces the live legacy host or its credentials."""
from tests.test_collector_reporting_settings import collector_rig, FIELDS, URL, H
from types import SimpleNamespace


def test_legacy_host_keeps_default_contract_after_independent_draft_save(collector_rig):
    rig = collector_rig
    host = SimpleNamespace(_active=True)
    rig.agent.xunjia_v22_host = host
    try:
        result = rig.client.put(URL + "?contract=collector", headers=H(), json=FIELDS)
        assert result.status_code == 200
        assert result.json()["relay"]["workbench_name"] == FIELDS["workbench_name"]
        assert result.json()["legacy_available"] is True
        assert result.json()["applied"] is False
        assert rig.agent.xunjia_v22_host is host
        assert rig.client.get(URL, headers=H()).json()["relay"]["config_file"] == "config.yaml"
        local_revision = rig.agent.collector_reporting_settings.saved()["revision"]
        rotated = rig.client.put(URL, headers=H(), json={"sm4_key": "fedcba9876543210"})
        assert rotated.status_code == 200
        assert rotated.json()["relay"]["config_file"] == "config.yaml"
        assert rig.agent.collector_reporting_settings.saved()["revision"] == local_revision
        assert rig.agent.xunjia_v22_host is host
    finally:
        rig.agent.xunjia_v22_host = None


def test_explicit_editors_remain_independently_accessible(collector_rig):
    rig = collector_rig
    before = {p: p.read_bytes() for p in rig.protected_paths}
    first = rig.client.get(URL + "?contract=collector", headers=H())
    assert first.status_code == 200 and "workbench_name" in first.json()["relay"]
    assert rig.client.put(URL + "?contract=collector", headers=H(), json=FIELDS).status_code == 200
    assert rig.client.put(URL + "?contract=collector", headers=H(), json={"sm4_key": "fedcba9876543210"}).status_code == 200
    assert {p: p.read_bytes() for p in rig.protected_paths} == before
    assert rig.client.get(URL + "?contract=legacy", headers=H()).json()["relay"]["config_file"] == "config.yaml"
    assert rig.client.get(URL + "?contract=collector", headers=H()).json()["relay"]["workbench_name"] == FIELDS["workbench_name"]
    assert rig.agent.xunjia_v22_host is None


def test_invalid_selector_fails_closed_before_any_save(collector_rig):
    rig = collector_rig
    before = {p: p.read_bytes() for p in rig.protected_paths}
    for method in ["get", "put", "post"]:
        path = URL + ("/test" if method == "post" else "") + "?contract=unknown"
        response = getattr(rig.client, method)(path, headers=H(), **({} if method == "get" else {"json": FIELDS}))
        assert response.status_code == 400
        assert response.json()["error"]["reason"] == "reporting_contract_invalid"
    assert rig.agent.collector_reporting_settings.saved() is None
    assert {p: p.read_bytes() for p in rig.protected_paths} == before
