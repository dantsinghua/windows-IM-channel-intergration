"""微信登录会话流(05 §2.4.4 完整流程 / §2.4.4a 取钥时序 / §2.4.2.1 分配·回填·合并·回滚;02 §3.6 #31/#33/#33c)。"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from qtrade_agent.adapters.wechat import WechatLoginFlow
from tests.test_wechat_rig import make_wechat_rig


def make_flow(rig, *, bind_retry_max: int = 12, step_ms: int = 1000) -> WechatLoginFlow:
    async def sleep(sec: float) -> None:
        rig.clock.advance(int(sec * 1000) or step_ms)

    def transition(id: str, state: str, **kw: Any) -> dict[str, Any]:
        return rig.slot._transition(id, state, **kw)

    return WechatLoginFlow(store=rig.store, client=rig.client, slot=rig.slot, cfg=rig.cfg, transition=transition,
                           clock=rig.clock, sleep=sleep, status_interval_s=1.0, bind_retry_max=bind_retry_max)


def codes(rig) -> list[tuple[str, Optional[str]]]:
    return [(state, code) for _id, state, code, _ls in rig.transitions]


def mk(rig, id: str, *, state: str = "stopped", wxid: Optional[str] = None) -> str:
    rig.store.ensure_account(id, "wechat", state=state, login_mode="qrcode")
    rig.store.upsert_runtime(id, kind=rig.store.RUNTIME_KIND["wechat"], now_ms=rig.clock())
    if wxid:
        rig.store.con.execute("UPDATE accounts SET wxid=?, self_uid=? WHERE id=?", (wxid, wxid, id))
    return id


# ---------------------------------------------------------------------- 正线
async def test_happy_path_walks_phases_and_promotes_slot(tmp_path):
    """narrator → qrcode → identified(bind)→ ready;状态序列不跳段(00 §8.1),成功才 ``pending → holder``。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.bound.clear()
    rig.fake.phases_script = [
        {"phase": "narrator", "countdown_s": 300},
        {"phase": "qrcode"},
        {"phase": "identified", "wxid": "wxid_demo01"},
        {"phase": "ready"},
    ]
    assert await make_flow(rig).run("wx01", "ls_A") == "running"
    assert codes(rig) == [("provisioning", None), ("starting", None), ("login_required", "WAIT_NARRATOR"),
                          ("login_required", "WAIT_QRCODE"), ("logging_in", None), ("running", None)]
    assert rig.slot.view().holder == "wx01" and rig.slot.view().pending == ""
    row = rig.store.get_account_full("wx01")
    assert row["wxid"] == "wxid_demo01" and row["self_uid"] == "wxid_demo01" and row["state"] == "running"
    assert rig.fake.bound == {"wxid_demo01": "wx01"}
    rig.store.close()


async def test_two_key_stages_are_two_independent_codes(tmp_path):
    """🔴 05 §2.4.4a:``WAIT_KEY_IMG``(打开图片,≈60s)与 ``WAIT_KEY_RELOGIN``(退出重登,≈30s)
    是**两个独立码、不许合成**——指示完全不同,顺序做反整轮作废。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.phases_script = [
        {"phase": "keytry", "key": {"stage": "img"}},
        {"phase": "keytry", "key": {"stage": "relogin"}},
        {"phase": "ready", "wxid": "wxid_demo01"},
    ]
    await make_flow(rig).run("wx01", "ls_A")
    assert ("login_required", "WAIT_KEY_IMG") in codes(rig)
    assert ("login_required", "WAIT_KEY_RELOGIN") in codes(rig)
    prompts = [p for p in _prompts(rig) if p and p.get("kind", "").startswith("WAIT_KEY")]
    assert {p["kind"]: p["countdown_s"] for p in prompts} == {"WAIT_KEY_IMG": 60, "WAIT_KEY_RELOGIN": 30}
    rig.store.close()


def _prompts(rig) -> list[Optional[dict[str, Any]]]:
    import json
    return [json.loads(e["payload_json"]).get("prompt") for e in rig.store.list_events(event="account_state")]


async def test_same_phase_does_not_spam_events(tmp_path):
    """同一相位重复出现不重复发事件(01 靠 ``login_session_id`` 区分尝试,不靠事件条数)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.phases_script = [{"phase": "qrcode"}] * 4 + [{"phase": "ready", "wxid": "wxid_demo01"}]
    await make_flow(rig).run("wx01", "ls_A")
    assert codes(rig).count(("login_required", "WAIT_QRCODE")) == 1
    rig.store.close()


# ---------------------------------------------------------------------- 取钥失败
async def test_key_failed_goes_degraded_releases_pending_and_never_binds(tmp_path):
    """05 §2.4.7 / §2.4.2.1 第 2 步:只拿到一把 / 全 DLL 失败 ⇒ ``degraded(KEY_FAIL)``、
    **失败即同步释放 pending + 回收中间产物**、**不进 bind**。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.bound.clear()
    rig.fake.phases_script = [{"phase": "keytry", "key": {"stage": "img"}},
                              {"phase": "key_failed", "key": {"ok": False, "error": "只获取到数据库密钥"}}]
    assert await make_flow(rig).run("wx01", "ls_A") == "degraded"
    row = rig.store.get_account_full("wx01")
    assert row["state"] == "degraded" and row["state_code"] == "KEY_FAIL"
    s = rig.slot.view()
    assert (s.pending, s.pending_expires_ms, s.pending_login_session_id, s.holder) == ("", None, "", "")
    assert rig.purged == ["wx01"]
    assert rig.fake.bound == {}
    assert not any(p.endswith("/bind") for _m, p, _b in rig.fake.calls)
    rig.store.close()


# ---------------------------------------------------------------------- 回填 / 合并 / bind 409
async def test_new_wxid_is_backfilled_onto_temp_row(tmp_path):
    """05 §2.4.2.1 第 3 步 a):新 wxid ⇒ 回填 ``self_uid``/``wxid``,续走 bind。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.bound.clear()
    rig.fake.phases_script = [{"phase": "identified", "wxid": "wxid_new"}, {"phase": "ready"}]
    await make_flow(rig).run("wx01", "ls_A")
    row = rig.store.get_account_full("wx01")
    assert row["wxid"] == "wxid_new" and row["merged_into"] is None
    rig.store.close()


async def test_existing_profile_merges_temp_row_into_old_id(tmp_path):
    """05 §2.4.2.1 第 3 步 b):命中老档案 ⇒ 临时行 ``merged_into=<老 id>`` + ``deleted_ms``(软删、永不复用),
    槽位 ``pending``/``holder`` 改指老 id,``running`` 挂老 id。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02", wxid="wxid_old")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.bound.clear()
    rig.fake.phases_script = [{"phase": "identified", "wxid": "wxid_old"}, {"phase": "ready"}]
    assert await make_flow(rig).run("wx01", "ls_A") == "running"
    temp = rig.store.get_account_full("wx01")
    assert temp["merged_into"] == "wx02" and temp["deleted_ms"] is not None
    assert rig.slot.view().holder == "wx02"
    assert rig.store.get_account_full("wx02")["state"] == "running"
    assert rig.fake.bound == {"wxid_old": "wx02"}
    assert len(rig.store.list_audit(action="account.wechat_merge")) == 1
    rig.store.close()


async def test_bind_409_adopts_winagent_side_account_id(tmp_path):
    """05 §2.4.2.1 回滚表末行:``bind`` 回 409 ⇒ **以 winagent.db 已有的 account_id 为准**,
    临时行软删并改用 409 带回的 id;``accounts`` 无此 id 则按该 id 重建一行(两库唯一一处「从表反向修主表」)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.bound.clear()
    rig.fake.bind_conflict = "wx05"
    rig.fake.phases_script = [{"phase": "identified", "wxid": "wxid_demo01"}, {"phase": "ready"}]
    assert await make_flow(rig).run("wx01", "ls_A") == "running"
    assert rig.store.get_account_full("wx01")["merged_into"] == "wx05"
    rebuilt = rig.store.get_account_full("wx05")
    assert rebuilt is not None and rebuilt["wxid"] == "wxid_demo01" and rebuilt["state"] == "running"
    assert len(rig.store.list_audit(action="account.wechat_bind_conflict")) == 1
    rig.store.close()


async def test_bind_retry_then_exhausted_goes_error_and_releases_pending(tmp_path):
    """05 §2.4.2.1 第 4 步:``bind`` 网络失败/503 ⇒ 每 5 s 重试(幂等),超 ``bind_retry_max`` ⇒
    ``error(VAULT_UNAVAILABLE)`` 同款环境类错误 + 失败即释放 pending。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.phases_script = [{"phase": "identified", "wxid": "wxid_demo01"}] * 40
    rig.fake.fail_paths["/wa/v1/wechat/bind"] = 999               # bind 恒不可达(网络失败/503)
    assert await make_flow(rig, bind_retry_max=3).run("wx01", "ls_A") == "error"
    row = rig.store.get_account_full("wx01")
    assert row["state"] == "error" and row["state_code"] == "VAULT_UNAVAILABLE"
    assert rig.slot.view().pending == "" and rig.purged == ["wx01"]
    rig.store.close()


# ---------------------------------------------------------------------- 回滚 / 超时
async def test_login_start_unavailable_goes_stopped_and_releases_pending(tmp_path):
    """05 §2.4.2.1 回滚表第 1 行:读 wxid 前失败(会话代理不在线)⇒ 临时行 ``stopped`` + 槽位 pending 同步置空 + 回收。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.user_agent = False
    assert await make_flow(rig).run("wx01", "ls_A") == "stopped"
    assert rig.store.get_account_full("wx01")["state"] == "stopped"
    assert rig.slot.view().pending == "" and rig.purged == ["wx01"]
    rig.store.close()


async def test_qr_wait_timeout_goes_stopped(tmp_path):
    """05 §2.4.2.1「断连期间的规则」:超 ``[accounts] qr_max_wait_s`` 转 ``stopped``、释放 pending。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.phase = "qrcode"
    flow = make_flow(rig, step_ms=rig.cfg.accounts.qr_max_wait_s * 1000)
    assert await flow.run("wx01", "ls_A") == "stopped"
    assert rig.store.get_account_full("wx01")["state"] == "stopped"
    assert rig.slot.view().pending == ""
    rig.store.close()


async def test_status_poll_hiccup_is_retried_not_fatal(tmp_path):
    """#33 轮询单次失败不终止本次尝试(会话代理短暂抖动),下一拍继续。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.fake.fail_paths["/wa/v1/wechat/login/status"] = 2      # 只读类重试 1 次后仍失败 ⇒ 本拍跳过、下一拍继续
    rig.fake.phases_script = [{"phase": "qrcode"}, {"phase": "ready", "wxid": "wxid_demo01"}]
    assert await make_flow(rig).run("wx01", "ls_A") == "running"
    rig.store.close()


async def test_require_wechat_guard(tmp_path):
    from qtrade_agent.adapters.wechat.login import require_wechat
    from qtrade_agent.api.auth import ApiError
    rig = make_wechat_rig(tmp_path)
    rig.store.ensure_account("qd01", "qidian", state="stopped")
    assert require_wechat(rig.store.get_account_full("wx01"))["id"] == "wx01"
    with pytest.raises(ApiError):
        require_wechat(rig.store.get_account_full("qd01"))
    with pytest.raises(ApiError):
        require_wechat(None)
    rig.store.close()
