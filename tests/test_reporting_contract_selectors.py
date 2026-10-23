"""The stable settings route rejects every removed contract selector and field."""
import pytest
from tests.test_collector_reporting_settings import collector_rig, FIELDS, URL, H


@pytest.mark.parametrize("selector", ["collector", "legacy", "sts", "unknown", ""])
@pytest.mark.parametrize("method", ["get", "put", "post"])
def test_removed_selector_is_rejected_before_reads_or_mutation(collector_rig, selector, method):
    rig = collector_rig
    before = rig.store.settings_list_prefix("")
    vault = dict(rig.agent.vault.entries)
    files = {p: p.read_bytes() for p in rig.protected_paths}
    path = URL + ("/test" if method == "post" else "") + "?contract=" + selector
    response = getattr(rig.client, method)(path, headers=H(), **({} if method == "get" else {"json": FIELDS}))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["reason"] == "reporting_contract_invalid"
    assert rig.store.settings_list_prefix("") == before
    assert dict(rig.agent.vault.entries) == vault
    assert {p: p.read_bytes() for p in rig.protected_paths} == files


@pytest.mark.parametrize("field", ["sts_url", "allow_http", "endpoint", "bucket", "auth_key", "config_yaml",
    "config_file", "auth_file", "uplink_url", "downlink_url", "workbench_id", "owner_user_id", "account_uid"])
@pytest.mark.parametrize("suffix", ["", "/test"])
def test_old_or_foreign_owned_fields_are_rejected_without_mutation(collector_rig, field, suffix):
    rig = collector_rig
    before = rig.store.settings_list_prefix("")
    vault = dict(rig.agent.vault.entries)
    files = {p: p.read_bytes() for p in rig.protected_paths}
    method = rig.client.post if suffix else rig.client.put
    response = method(URL + suffix, headers=H(), json={**FIELDS, field: "synthetic-rejected-canary"})
    assert response.status_code == 400, response.text
    assert "synthetic-rejected-canary" not in response.text
    assert rig.store.settings_list_prefix("") == before
    assert dict(rig.agent.vault.entries) == vault
    assert {p: p.read_bytes() for p in rig.protected_paths} == files
    assert rig.agent.xunjia_v22_host is None
