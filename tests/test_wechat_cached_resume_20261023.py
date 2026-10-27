"""Bound QR slots resume verified cached readers without restarting key workflows."""
from copy import deepcopy
from unittest.mock import AsyncMock

import asyncio
import pytest

from qtrade_agent.adapters.wechat.client import WeChatCallFailed, WeChatNotReady
from tests.test_wechat_existing_login import existing
from tests.test_wechat_login import make_flow


def preserve_readers(rig):
    forbidden = {"/wa/v1/wechat/login/start", "/wa/v1/wechat/login/cancel",
                 "/wa/v1/wechat/key/retry", "/wa/v1/wechat/logout"}
    assert not [(m, p) for m, p, _ in rig.fake.calls if p.split("?", 1)[0] in forbidden]
    assert rig.purged == []


def as_bound_qr(rig):
    rig.store.con.execute("UPDATE accounts SET login_mode='qrcode' WHERE id='wx01'")
    rig.fake.phase = "idle"
    return rig


@pytest.mark.parametrize("phase", ["idle", "ready"])
async def test_bound_qr_reuses_cached_readers_and_checks_live_identity(existing, phase):
    rig = as_bound_qr(existing)
    rig.fake.phase = phase
    before = deepcopy((rig.fake.wechat, rig.fake.chatlog, rig.fake.key))
    result = await make_flow(rig).run("wx01", "ls_existing")
    preserve_readers(rig)
    assert result == "running"
    assert (rig.fake.wechat, rig.fake.chatlog, rig.fake.key) == before
    assert rig.store.get_account_full("wx01")["login_mode"] == "qrcode"
    assert rig.slot.view().holder == "wx01"
    assert any(path == "/wa/v1/wechat/read?limit=1" for _m, path, _b in rig.fake.calls)


async def test_bound_qr_uid_mismatch_never_rekeys_or_binds(existing):
    rig = as_bound_qr(existing)
    rig.fake.wechat["wxid"] = "wxid_another"
    assert await make_flow(rig).run("wx01", "ls_existing") != "running"
    preserve_readers(rig)
    assert not rig.fake.bound
    assert rig.store.get_account_full("wx01")["wxid"] == "wxid_demo01"


async def test_bound_qr_identity_change_during_read_never_falls_back(existing, monkeypatch):
    rig = as_bound_qr(existing)
    read = rig.client.read
    async def change(**kwargs):
        result = await read(**kwargs)
        rig.fake.wechat["wxid"] = "wxid_switched"
        return result
    monkeypatch.setattr(rig.client, "read", change)
    assert await make_flow(rig).run("wx01", "ls_existing") != "running"
    preserve_readers(rig)
    assert not rig.fake.bound


@pytest.mark.parametrize("value", [False, None, 1, "true"])
async def test_bound_qr_unusable_or_unknown_key_never_starts_key_workflow(existing, monkeypatch, value):
    rig = as_bound_qr(existing)
    status = await rig.client.status()
    status["chatlog"]["key_ok"] = value
    monkeypatch.setattr(rig.client, "status", AsyncMock(return_value=status))
    assert await make_flow(rig).run("wx01", "ls_existing") != "running"
    preserve_readers(rig)
    assert not rig.fake.bound


@pytest.mark.parametrize("failure", [WeChatNotReady("user_agent_offline"), WeChatCallFailed(503, {}, "probe")])
async def test_bound_qr_status_failure_is_not_treated_as_logout(existing, monkeypatch, failure):
    rig = as_bound_qr(existing)
    monkeypatch.setattr(rig.client, "status", AsyncMock(side_effect=failure))
    assert await make_flow(rig).run("wx01", "ls_existing") != "running"
    preserve_readers(rig)
    assert not rig.fake.bound


@pytest.mark.parametrize("phase", ["qrcode", "keytry", "unexpected"])
async def test_bound_qr_existing_phase_conflict_stops_without_restart(existing, phase):
    rig = as_bound_qr(existing)
    rig.fake.phase = phase
    assert await make_flow(rig).run("wx01", "ls_existing") != "running"
    preserve_readers(rig)
    assert rig.fake.phase == phase
    assert not rig.fake.bound


async def test_bound_qr_cancelled_probe_does_not_restart_or_publish_running(existing, monkeypatch):
    rig = as_bound_qr(existing)
    monkeypatch.setattr(rig.client, "status", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await make_flow(rig).run("wx01", "ls_existing")
    preserve_readers(rig)
    assert not any(state == "running" for _aid, state, _code, _ls in rig.transitions)


async def test_explicitly_logged_out_bound_slot_keeps_normal_qr_start(existing, monkeypatch):
    rig = as_bound_qr(existing)
    rig.fake.wechat["logged_in"] = False
    start = AsyncMock(side_effect=WeChatCallFailed(503, {}, "synthetic_start_unavailable"))
    monkeypatch.setattr(rig.client, "login_start", start)
    assert await make_flow(rig).run("wx01", "ls_existing") == "stopped"
    start.assert_awaited_once_with(account_id="wx01", login_session_id="ls_existing")


@pytest.mark.parametrize("mode", ["manual", "qrcode"])
async def test_cancel_claim_release_cannot_purge_cached_readers(existing, monkeypatch, mode):
    rig = as_bound_qr(existing)
    rig.store.con.execute("UPDATE accounts SET login_mode=? WHERE id='wx01'", (mode,))
    monkeypatch.setattr(rig.client, "status", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await make_flow(rig).run("wx01", "ls_existing")
    # AccountService.login_cancel awaits the task, then performs this cleanup.
    await rig.slot.release_pending("wx01", reason="user_cancel", actor="synthetic-user")
    preserve_readers(rig)
    assert rig.slot.view().pending == ""


async def test_unbound_first_login_keeps_normal_qr_start(existing, monkeypatch):
    rig = as_bound_qr(existing)
    rig.store.con.execute("UPDATE accounts SET wxid=NULL,self_uid=NULL WHERE id='wx01'")
    start = AsyncMock(side_effect=WeChatCallFailed(503, {}, "synthetic_start_unavailable"))
    monkeypatch.setattr(rig.client, "login_start", start)
    assert await make_flow(rig).run("wx01", "ls_existing") == "stopped"
    start.assert_awaited_once_with(account_id="wx01", login_session_id="ls_existing")

