"""既有登录接入合同：只读核验现存身份和解密能力，绝不重新登录或取钥。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from qtrade_agent.adapters.wechat.client import WeChatCallFailed, WeChatNotReady
from qtrade_agent.config import AgentConfig
from tests.test_wechat_login import make_flow
from tests.test_wechat_rig import make_wechat_rig


@pytest.fixture
def existing(tmp_path):
    cfg = AgentConfig()
    cfg = replace(cfg, accounts=replace(cfg.accounts, qr_max_wait_s=5))
    rig = make_wechat_rig(tmp_path, state="stopped", cfg=cfg)
    rig.store.con.execute("UPDATE accounts SET login_mode='manual', wxid='wxid_demo01' WHERE id='wx01'")
    assert rig.slot.claim("wx01", "ls_existing")
    yield rig
    rig.store.close()


def assert_preserved(rig):
    destructive = {"/wa/v1/wechat/login/start", "/wa/v1/wechat/login/cancel",
                   "/wa/v1/wechat/key/retry", "/wa/v1/wechat/logout"}
    assert [(method, path) for method, path, _body in rig.fake.calls if path.split("?", 1)[0] in destructive] == []
    assert rig.purged == [], "复用既有登录态没有本轮临时进程可清理，不能回收既有 chatlog/密钥"
    assert rig.fake.chatlog["running"] is True
    assert rig.fake.chatlog["key_ok"] is True


def assert_refused(rig, result):
    assert_preserved(rig)
    assert result != "running"
    assert not any(state == "running" for _aid, state, _code, _ls in rig.transitions)
    assert rig.store.get_account_full("wx01")["state"] != "running"
    assert rig.slot.view().holder == ""


@pytest.mark.parametrize("phase", ["idle", "ready"])
async def test_manual_existing_login_checks_read_and_promotes_without_relogin(existing, phase):
    rig = existing
    rig.fake.phase = phase
    before = deepcopy((rig.fake.wechat, rig.fake.chatlog, rig.fake.key))
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_preserved(rig)
    assert result == "running"
    row = rig.store.get_account_full("wx01")
    assert (row["wxid"], row["self_uid"], row["state"]) == ("wxid_demo01", "wxid_demo01", "running")
    assert (rig.slot.view().pending, rig.slot.view().holder) == ("", "wx01")
    assert rig.fake.bound == {"wxid_demo01": "wx01"}
    paths = [path for _method, path, _body in rig.fake.calls]
    assert "/wa/v1/wechat/read?limit=1" in paths
    read_index = paths.index("/wa/v1/wechat/read?limit=1")
    assert "/wa/v1/wechat/status" in paths[:read_index]
    assert "/wa/v1/wechat/status" in paths[read_index + 1:]
    assert before == (rig.fake.wechat, rig.fake.chatlog, rig.fake.key)
    assert [(state, code) for _aid, state, code, _ls in rig.transitions] == [
        ("provisioning", None), ("starting", None), ("logging_in", None), ("running", None)]


@pytest.mark.parametrize("field", ["enabled", "wechat.running", "wechat.logged_in", "chatlog.running", "chatlog.key_ok"])
@pytest.mark.parametrize("value", [False, None, 1, "true"])
async def test_manual_requires_strict_boolean_health(existing, monkeypatch, field, value):
    rig = existing
    payload = await rig.client.status()
    parts = field.split(".")
    target = payload if len(parts) == 1 else payload[parts[0]]
    target[parts[-1]] = value
    monkeypatch.setattr(rig.client, "status", AsyncMock(return_value=payload))
    rig.fake.calls.clear()
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert not rig.fake.bound


@pytest.mark.parametrize("section", ["wechat", "chatlog"])
async def test_manual_missing_health_section_is_refused(existing, monkeypatch, section):
    rig = existing
    payload = await rig.client.status()
    payload[section] = None
    monkeypatch.setattr(rig.client, "status", AsyncMock(return_value=payload))
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)


@pytest.mark.parametrize("wxid", [None, "", "wxid_other"])
async def test_manual_requires_nonempty_matching_live_identity(existing, wxid):
    rig = existing
    rig.fake.wechat["wxid"] = wxid
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    row = rig.store.get_account_full("wx01")
    assert (row["wxid"], row["self_uid"]) == ("wxid_demo01", "wxid_demo01")
    assert not rig.fake.bound


async def test_manual_self_uid_mismatch_is_not_overwritten(existing):
    rig = existing
    rig.store.con.execute("UPDATE accounts SET self_uid='wxid_other' WHERE id='wx01'")
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert rig.store.get_account_full("wx01")["self_uid"] == "wxid_other"
    assert not rig.fake.bound


@pytest.mark.parametrize("phase", ["narrator", "qrcode", "identified", "keytry", "key_failed", "unexpected"])
async def test_manual_active_or_unknown_login_phase_is_refused(existing, phase):
    rig = existing
    rig.fake.phase = phase
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert not rig.fake.bound
    assert rig.fake.phase == phase


@pytest.mark.parametrize("method", ["status", "login_status", "read"])
@pytest.mark.parametrize("failure", [WeChatNotReady("user_agent_offline"), WeChatCallFailed(503, {}, "probe")])
async def test_manual_probe_failure_never_falls_back_to_key_workflow(existing, monkeypatch, method, failure):
    rig = existing
    monkeypatch.setattr(rig.client, method, AsyncMock(side_effect=failure))
    result = await make_flow(rig, bind_retry_max=1).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert not rig.fake.bound


@pytest.mark.parametrize("changed_field, value", [("wxid", "wxid_switched"), ("logged_in", False)])
async def test_manual_rechecks_live_identity_after_read_before_binding(existing, monkeypatch, changed_field, value):
    rig = existing
    read = rig.client.read

    async def switch_during_read(**kwargs):
        rows = await read(**kwargs)
        rig.fake.wechat[changed_field] = value
        return rows

    monkeypatch.setattr(rig.client, "read", switch_during_read)
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert not rig.fake.bound
    assert rig.store.get_account_full("wx01")["wxid"] == "wxid_demo01"


async def test_manual_new_identity_is_backfilled_using_normal_bind(existing):
    rig = existing
    rig.store.con.execute("UPDATE accounts SET wxid=NULL, self_uid=NULL WHERE id='wx01'")
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_preserved(rig)
    assert result == "running"
    row = rig.store.get_account_full("wx01")
    assert (row["wxid"], row["self_uid"]) == ("wxid_demo01", "wxid_demo01")
    assert rig.fake.bound == {"wxid_demo01": "wx01"}
    assert rig.slot.view().holder == "wx01"


async def test_manual_existing_profile_merges_and_promotes_old_account(existing):
    rig = existing
    rig.store.con.execute("UPDATE accounts SET wxid=NULL, self_uid=NULL WHERE id='wx01'")
    rig.store.ensure_account("wx02", "wechat", state="stopped", login_mode="manual", self_uid="wxid_demo01")
    rig.store.upsert_runtime("wx02", kind=rig.store.RUNTIME_KIND["wechat"], now_ms=rig.clock())
    rig.store.con.execute("UPDATE accounts SET wxid='wxid_demo01' WHERE id='wx02'")
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_preserved(rig)
    assert result == "running"
    temp = rig.store.get_account_full("wx01")
    assert temp["merged_into"] == "wx02" and temp["deleted_ms"] is not None
    assert rig.fake.bound == {"wxid_demo01": "wx02"}
    assert rig.slot.view().holder == "wx02"
    assert rig.store.get_account_full("wx02")["state"] == "running"
    assert len(rig.store.list_audit(action="account.wechat_merge")) == 1


async def test_manual_bind_conflict_keeps_existing_winagent_account_identity(existing):
    rig = existing
    rig.fake.bind_conflict = "wx05"
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_preserved(rig)
    assert result == "running"
    assert rig.store.get_account_full("wx01")["merged_into"] == "wx05"
    assert rig.slot.view().holder == "wx05"
    assert rig.store.get_account_full("wx05")["state"] == "running"
    assert len(rig.store.list_audit(action="account.wechat_bind_conflict")) == 1


async def test_manual_bind_failure_never_promotes_or_destroys_existing_keys(existing):
    rig = existing
    rig.fake.fail_paths["/wa/v1/wechat/bind"] = 999
    result = await make_flow(rig, bind_retry_max=1).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert any(path.endswith("/bind") for _method, path, _body in rig.fake.calls)


async def test_qrcode_retains_original_login_and_key_phase_flow(existing):
    rig = existing
    rig.store.con.execute("UPDATE accounts SET login_mode='qrcode' WHERE id='wx01'")
    rig.fake.phases_script = [{"phase": "keytry", "key": {"stage": "img"}},
                              {"phase": "keytry", "key": {"stage": "relogin"}},
                              {"phase": "identified", "wxid": "wxid_demo01"}, {"phase": "ready"}]
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert result == "running"
    assert len([path for _method, path, _body in rig.fake.calls if path.endswith("/login/start")]) == 1
    codes = [code for _aid, _state, code, _ls in rig.transitions]
    assert "WAIT_KEY_IMG" in codes and "WAIT_KEY_RELOGIN" in codes
    assert rig.fake.bound == {"wxid_demo01": "wx01"}
    assert rig.slot.view().holder == "wx01"
