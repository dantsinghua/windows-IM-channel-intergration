"""工作台/企点槽位目录预览验收；不发布、不远端发现、不读取通讯录正文。"""
from __future__ import annotations

import pytest

from qtrade_agent.store import Store
from qtrade_agent.vault_client import FakeVault
from tests.test_api_ext import H, P, TOK_R, TOK_W
from tests.test_collector_reporting_settings import (
    FIELDS, NAME, collector_rig, local, reporting_class, save,
)


def account(store, uid, *, label="企点合成账号", channel="qidian"):
    created = store.create_account(channel=channel, label=label, login_mode="qrcode", quota_mb=1024)
    store.transition(created["id"], "stopped", self_uid=uid)
    return created["id"]


def by_slot(catalog):
    return {row["account_id"]: row for row in catalog["accounts"]}


def test_catalog_is_explicitly_unpublished_local_preview(local):
    save(local.settings, FIELDS)
    slot = account(local.store, "3000000001")
    view = local.settings.catalog()
    wb = local.settings.view()["workbench_id"]
    assert view["workbench_id"] == wb
    assert view["workbench_name"] == NAME
    assert view["index_path"] == f"wb/{wb}/index.json"
    assert view["published"] is False and view["alignment_required"] is True
    assert by_slot(view)[slot] == {"account_id": slot, "account_uid": "3000000001", "channel": "qidian",
        "account_nick": "企点合成账号", "collector_id": slot,
        "book_path": f"wb/{wb}/{slot}/qidian/3000000001/contacts.json"}


def test_two_stores_with_same_name_and_same_qidian_uid_have_different_directories(local, tmp_path):
    save(local.settings, FIELDS)
    account(local.store, "3000000001")
    second = Store(str(tmp_path / "second-workbench.sqlite3")).open()
    try:
        other = reporting_class()(second, FakeVault())
        save(other, FIELDS)
        account(second, "3000000001")
        left, right = local.settings.catalog(), other.catalog()
        assert left["workbench_name"] == right["workbench_name"] == NAME
        assert left["workbench_id"] != right["workbench_id"]
        assert left["index_path"] != right["index_path"]
        assert left["accounts"][0]["book_path"] != right["accounts"][0]["book_path"]
    finally:
        second.close()


def test_rename_keeps_workbench_id_and_existing_account_directories(local):
    save(local.settings, FIELDS)
    account(local.store, "3000000001")
    before = local.settings.catalog()
    save(local.settings, {"workbench_name": "改名后的采集工作台"})
    reopened = reporting_class()(local.store, local.vault)
    after = reopened.catalog()
    assert after["workbench_name"] == "改名后的采集工作台"
    assert after["workbench_id"] == before["workbench_id"]
    assert after["index_path"] == before["index_path"]
    assert after["accounts"] == before["accounts"]


def test_same_store_multiple_qidian_accounts_are_not_reduced_to_current_owner(local):
    save(local.settings, FIELDS)
    first = account(local.store, "3000000001", label="第一企点")
    second = account(local.store, "3000000002", label="第二企点")
    rows = by_slot(local.settings.catalog())
    assert set(rows) == {first, second}
    assert rows[first]["account_uid"] == "3000000001"
    assert rows[second]["account_uid"] == "3000000002"
    assert rows[first]["book_path"] != rows[second]["book_path"]


def test_duplicate_uid_slots_are_always_isolated_and_adding_slot_keeps_old_path(local):
    save(local.settings, FIELDS)
    first = account(local.store, "3000000001")
    original = by_slot(local.settings.catalog())[first]["book_path"]
    second = account(local.store, "3000000001")
    rows = by_slot(local.settings.catalog())
    assert set(rows) == {first, second}
    assert rows[first]["book_path"] == original
    assert rows[first]["book_path"] != rows[second]["book_path"]
    assert rows[first]["collector_id"] == first
    assert rows[second]["collector_id"] == second
    local.store.transition(second, "stopped", deleted_ms=1)
    assert by_slot(local.settings.catalog())[first]["book_path"] == original


def test_catalog_excludes_unidentified_and_deleted_slots_and_other_channels(local):
    save(local.settings, FIELDS)
    active = account(local.store, "3000000001")
    local.store.create_account(channel="qidian", label="尚未识别", login_mode="qrcode", quota_mb=1024)
    deleted = account(local.store, "3000000002")
    local.store.transition(deleted, "stopped", deleted_ms=1)
    account(local.store, "wx-synthetic", channel="wechat")
    account(local.store, "12345678", channel="qq")
    assert set(by_slot(local.settings.catalog())) == {active}


def test_catalog_uses_local_metadata_without_messages_or_session_reads(local, monkeypatch):
    save(local.settings, FIELDS)
    account(local.store, "3000000001")
    def forbidden(*args, **kwargs):
        pytest.fail("目录预览不得读取通讯录/消息正文")
    monkeypatch.setattr(local.store, "list_sessions", forbidden)
    monkeypatch.setattr(local.store, "list_messages", forbidden)
    monkeypatch.setattr(local.store, "query_messages", forbidden)
    before = local.store.settings_list_prefix("")
    view = local.settings.catalog()
    assert len(view["accounts"]) == 1
    assert local.store.settings_list_prefix("") == before


def test_trusted_existing_workbench_id_is_preserved_without_binding_owner(local):
    # 构建全新 store，避免给既有本地身份补灌外部 seed。
    store = Store().open()
    try:
        settings = reporting_class()(store, FakeVault(), seed={"workbench_id": "trusted-existing-wb"})
        save(settings, FIELDS)
        account(store, "3000000001")
        view = settings.catalog()
        assert view["workbench_id"] == "trusted-existing-wb"
        assert view["index_path"] == "wb/trusted-existing-wb/index.json"
    finally:
        store.close()


def test_admin_catalog_route_returns_only_local_unpublished_directory(collector_rig):
    rig = collector_rig
    saved = rig.client.put(f"{P}/settings/ossbox", headers=H(), json=FIELDS)
    assert saved.status_code == 200, saved.text
    first = account(rig.store, "3000000001")
    second = account(rig.store, "3000000002")
    before = {path: path.read_bytes() for path in rig.protected_paths}
    response = rig.client.get(f"{P}/collector/catalog", headers=H())
    assert response.status_code == 200, response.text
    view = response.json()["data"]
    assert set(by_slot(view)) == {first, second}
    assert view["published"] is False and view["alignment_required"] is True
    assert {path: path.read_bytes() for path in rig.protected_paths} == before
    assert rig.agent.xunjia_v22_host is None
    assert not rig.agent.scheduler._started and not rig.agent._started
    assert set(view["accounts"][0]) == {"account_id", "account_uid", "channel", "account_nick", "collector_id", "book_path"}


@pytest.mark.parametrize("token", [TOK_R, TOK_W, "unregistered-synthetic-token"])
def test_catalog_route_is_admin_only(collector_rig, token):
    response = collector_rig.client.get(f"{P}/collector/catalog", headers=H(token))
    assert response.status_code == (401 if token == "unregistered-synthetic-token" else 403), response.text
