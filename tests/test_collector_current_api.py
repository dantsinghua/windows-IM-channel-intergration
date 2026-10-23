"""Supplemental same-loop ASGI contract, separate from the retained TestClient gate."""
import os
import socket
from urllib.request import OpenerDirector

import httpx
import pytest

from tests.conftest import make_rig
from tests.test_collector_reporting_settings import FIELDS, BASE, DRAFT, KEY, SM4


URL = "/api/v1/settings/ossbox"
ADMIN = {"Authorization": "Bearer synthetic-current-admin"}


@pytest.fixture
async def api(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("XUNJIA_", "OSS_", "OSSBOX_", "STS_")):
            monkeypatch.delenv(name)
    def forbidden(*args, **kwargs):
        pytest.fail("Current ASGI contract forbids network, publication and native sends")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    rig = make_rig(tmp_path)
    for level in ("admin", "read", "write"):
        rig.store.upsert_api_client(app_id="current-" + level, name="synthetic", level=level,
            token="synthetic-current-" + level)
    historical = tmp_path / "retired-external-auth.yaml"
    historical.write_bytes(b"synthetic-historical-credentials-preserve\n")
    rig.historical = historical
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rig.agent.create_api(),
            client=("127.0.0.1", 41000)), base_url="http://offline-asgi.test") as client:
        try:
            yield rig, client
        finally:
            await rig.agent.stop()


def snapshot(rig):
    return (rig.store.settings_list_prefix(""), {name: entry.value for name, entry in rig.vault.entries.items()},
        rig.historical.read_bytes())


@pytest.mark.parametrize("selector", ["collector", "legacy", "sts", "unknown", ""])
@pytest.mark.parametrize("method", ["GET", "PUT", "POST"])
async def test_every_removed_contract_selector_fails_without_persistence(api, selector, method):
    rig, client = api
    before = snapshot(rig)
    path = URL + ("/test" if method == "POST" else "") + "?contract=" + selector
    response = await client.request(method, path, headers=ADMIN, **({} if method == "GET" else {"json": FIELDS}))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["reason"] == "reporting_contract_invalid"
    assert snapshot(rig) == before
    assert rig.agent.xunjia_v22_host is None


@pytest.mark.parametrize("field", ["sts_url", "bucket", "endpoint", "allow_http", "auth_key", "config_yaml",
    "config_file", "auth_file", "workbench_id", "owner_user_id", "account_uid", "uplink_url", "downlink_url"])
@pytest.mark.parametrize("suffix", ["", "/test"])
async def test_retired_and_foreign_fields_fail_before_vault_or_network(api, field, suffix):
    rig, client = api
    before = snapshot(rig)
    response = await client.request("POST" if suffix else "PUT", URL + suffix, headers=ADMIN,
        json={**FIELDS, field: "synthetic-rejected-private-canary"})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["reason"] == "collector_unknown_field"
    assert "synthetic-rejected-private-canary" not in response.text
    assert snapshot(rig) == before


@pytest.mark.parametrize("level", ["read", "write"])
@pytest.mark.parametrize("method", ["GET", "PUT", "POST"])
async def test_settings_still_require_admin_for_each_operation(api, level, method):
    rig, client = api
    before = snapshot(rig)
    path = URL + ("/test" if method == "POST" else "")
    response = await client.request(method, path, headers={"Authorization": "Bearer synthetic-current-" + level},
        **({} if method == "GET" else {"json": FIELDS}))
    assert response.status_code == 403
    assert snapshot(rig) == before


async def test_current_save_rename_and_url_change_preserve_identity_and_external_state(api):
    rig, client = api
    before_file = rig.historical.read_bytes()
    empty = await client.get(URL, headers=ADMIN)
    assert empty.status_code == 200
    original = empty.json()["relay"]["workbench_id"]
    response = await client.put(URL, headers=ADMIN, json=FIELDS)
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is False
    changed = await client.put(URL, headers=ADMIN, json={"workbench_name": "Synthetic renamed", "base_url": DRAFT})
    assert changed.status_code == 200, changed.text
    current = await client.get(URL, headers=ADMIN)
    data = current.json()
    assert data["mode"] == "its_relay" and data["state"] == "pending_alignment"
    assert data["relay"]["workbench_id"] == original
    assert data["relay"]["workbench_name"] == "Synthetic renamed" and data["relay"]["base_url"] == DRAFT
    assert data["relay"]["saved"] is True and data["relay"]["configured"] is True
    assert data["applied"] is False and not data["institution_runtime"]
    assert "data" not in data and "legacy_available" not in data and "config_file" not in data["relay"]
    assert all(secret not in response.text + changed.text + current.text for secret in (KEY, SM4))
    assert rig.historical.read_bytes() == before_file
    assert rig.agent.xunjia_v22_host is None
    assert not rig.agent._started and not rig.agent.scheduler._started


@pytest.mark.parametrize("old_url", ["http://collector.example.invalid", BASE + "/its-relay"])
async def test_saved_old_current_draft_can_be_read_and_repaired_but_not_used(api, old_url):
    rig, client = api
    assert (await client.put(URL, headers=ADMIN, json=FIELDS)).status_code == 200
    saved = rig.agent.collector_reporting_settings.saved()
    rig.store.settings_set("collector.reporting", {**saved, "base_url": old_url})
    before = snapshot(rig)
    read = await client.get(URL, headers=ADMIN)
    assert read.status_code == 200
    assert read.json()["relay"]["base_url"] == old_url
    assert read.json()["relay"]["configured"] is False
    for method, path, body in (("POST", URL + "/test", {}), ("PUT", URL, {"workbench_name": "Renamed"})):
        response = await client.request(method, path, headers=ADMIN, json=body)
        assert response.status_code == 400
        assert response.json()["error"]["reason"] == "collector_url_invalid"
        assert snapshot(rig) == before
    fixed = await client.put(URL, headers=ADMIN, json={**FIELDS, "base_url": DRAFT})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["relay"]["workbench_id"] == saved["workbench_id"]
    assert fixed.json()["relay"]["configured"] is True
    assert rig.historical.read_bytes() == before[2]
