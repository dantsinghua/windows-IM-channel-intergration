"""既有微信接入的竞争和失败收口；保持首批47例冻结断言原样。"""
from __future__ import annotations

import pytest

from tests.test_wechat_existing_login import assert_preserved, assert_refused, existing
from tests.test_wechat_login import make_flow


@pytest.mark.parametrize("phase", ["qrcode", "keytry"])
async def test_read_probe_cannot_take_over_login_started_during_read(existing, monkeypatch, phase):
    rig = existing
    read = rig.client.read

    async def start_another_login_during_read(**kwargs):
        rows = await read(**kwargs)
        rig.fake.phase = phase
        rig.fake.login_session_id = "ls_another"
        return rows

    monkeypatch.setattr(rig.client, "read", start_another_login_during_read)
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert not rig.fake.bound
    assert rig.fake.phase == phase
    assert rig.fake.login_session_id == "ls_another"


async def test_merge_then_bind_failure_releases_pending_on_surviving_account(existing):
    rig = existing
    rig.store.con.execute("UPDATE accounts SET wxid=NULL, self_uid=NULL WHERE id='wx01'")
    rig.store.ensure_account("wx02", "wechat", state="stopped", login_mode="manual", self_uid="wxid_demo01")
    rig.store.upsert_runtime("wx02", kind=rig.store.RUNTIME_KIND["wechat"], now_ms=rig.clock())
    rig.store.con.execute("UPDATE accounts SET wxid='wxid_demo01' WHERE id='wx02'")
    rig.fake.fail_paths["/wa/v1/wechat/bind"] = 999
    result = await make_flow(rig, bind_retry_max=1).run("wx01", "ls_existing")
    assert_preserved(rig)
    assert result != "running"
    temp = rig.store.get_account_full("wx01")
    assert temp["merged_into"] == "wx02" and temp["deleted_ms"] is not None
    assert rig.store.get_account_full("wx02")["state"] == "stopped"
    assert rig.transitions[-1][0:2] == ("wx02", "stopped")
    slot = rig.slot.view()
    assert (slot.pending, slot.pending_login_session_id, slot.pending_expires_ms, slot.holder) == ("", "", None, "")
    assert not any(state == "running" for _aid, state, _code, _ls in rig.transitions)


async def test_failed_slot_promotion_cannot_publish_running(existing, monkeypatch):
    rig = existing
    monkeypatch.setattr(rig.slot, "promote", lambda *_args, **_kwargs: False)
    result = await make_flow(rig).run("wx01", "ls_existing")
    assert_refused(rig, result)
    assert rig.slot.view().pending == ""
