"""微信单槽位状态机(02 §2.2.5 槽位 + 05 §2.4.2 R-04 / R6-6 / R4-8 / R5-4;基线 §11.18 [SLOT])。"""
from __future__ import annotations

import pytest

from qtrade_agent.api.auth import ApiError
from qtrade_agent.wechat_slot import CANCEL_AUDIT, REAPER_AUDIT, SWITCH_AUDIT, TAKEOVER_AUDIT
from tests.test_wechat_rig import make_wechat_rig


def mk(rig, id: str, state: str = "stopped") -> str:
    rig.store.ensure_account(id, "wechat", state=state, login_mode="qrcode")
    rig.store.upsert_runtime(id, kind=rig.store.RUNTIME_KIND["wechat"], now_ms=rig.clock())
    return id


# ---------------------------------------------------------------------- claim / 三列同生同灭
async def test_claim_writes_three_columns_in_one_update(tmp_path):
    """R6-6:``slot_pending`` / ``slot_pending_expires_ms`` / ``slot_pending_login_session_id`` **同一条 UPDATE 一起写**;
    TTL = ``now + [wechat] slot_pending_ttl_s×1000``(agent.toml 的 [wechat],§15b N-2)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    now = rig.clock()
    assert rig.slot.claim("wx01", "ls_A") is True
    s = rig.slot.view()
    assert (s.pending, s.pending_login_session_id) == ("wx01", "ls_A")
    assert s.pending_expires_ms == now + rig.cfg.wechat.slot_pending_ttl_s * 1000
    assert rig.slot.pending_remaining_s() == rig.cfg.wechat.slot_pending_ttl_s
    rig.store.close()


async def test_claim_is_row_level_and_rejects_second_claimer(tmp_path):
    """R-08 §2.3.1 ③:``WHERE slot_holder='' AND slot_pending=''`` 的条件 UPDATE,``rowcount==1`` 判抢到。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02")
    assert rig.slot.claim("wx01", "ls_A") is True
    assert rig.slot.claim("wx02", "ls_B") is False
    assert rig.slot.view().pending == "wx01"
    rig.store.close()


async def test_empty_string_convention_for_three_text_columns(tmp_path):
    """🔴 空值口径(R6-6):三个字符串字段无值时一律 ``""``;**只有 ``pending_expires_ms`` 是 None**。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    s = rig.slot.view()
    assert (s.holder, s.pending, s.pending_login_session_id) == ("", "", "")
    assert s.pending_expires_ms is None and s.used == 0
    snap = rig.pool.snapshot()["pools"]["windows"]["wechat_slots"]
    assert snap["holder"] == "" and snap["pending"] == "" and snap["pending_login_session_id"] == ""
    assert snap["pending_expires_at"] is None
    rig.store.close()


async def test_release_pending_clears_three_columns_and_purges(tmp_path):
    """①失败即释放(不等 TTL):三列一起清 + **同步回收中间产物**(否则下次绑定读到脏状态)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    assert await rig.slot.release_pending("wx01", reason="key_fail") == 1
    s = rig.slot.view()
    assert (s.pending, s.pending_expires_ms, s.pending_login_session_id) == ("", None, "")
    assert rig.purged == ["wx01"]
    assert [a["action"] for a in rig.store.list_audit(action=CANCEL_AUDIT)] == [CANCEL_AUDIT]
    rig.store.close()


async def test_promote_only_on_success_and_clears_attempt_id(tmp_path):
    """**成功**才 ``pending → holder``;尝试 id 随 pending 一起清(``holder`` 不记 ``login_session_id``)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    assert rig.slot.promote("wx01") is True
    s = rig.slot.view()
    assert s.holder == "wx01" and s.used == 1
    assert (s.pending, s.pending_expires_ms, s.pending_login_session_id) == ("", None, "")
    assert rig.slot.promote("wx01") is False          # 没有 pending 了,不能再转
    rig.store.close()


async def test_holder_has_no_ttl_and_reaper_never_touches_it(tmp_path):
    """🔴 ``holder`` 代表真实登录态,**绝不自动释放**、也不给它 TTL(自动登出会误踢正在用的号)。"""
    rig = make_wechat_rig(tmp_path, state="running")
    rig.slot.claim("wx01", "ls_A")
    rig.slot.promote("wx01")
    rig.clock.advance(rig.cfg.wechat.slot_pending_ttl_s * 1000 * 100)
    assert await rig.slot.reap() == []
    assert rig.slot.view().holder == "wx01"
    rig.store.close()


# ---------------------------------------------------------------------- ② reaper
async def test_reaper_releases_expired_pending_and_audits(tmp_path):
    """②TTL 到期由 ``wechat_slot_reaper`` 每 60 s 扫:三列一起清 + ``account_state`` 事件 + 审计 ``slot_pending_expired`` + 回收。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.clock.advance(rig.cfg.wechat.slot_pending_ttl_s * 1000 - 1)
    assert await rig.slot.reap() == []                # 未到点不动
    rig.clock.advance(2)
    assert await rig.slot.reap() == ["wx01"]
    s = rig.slot.view()
    assert (s.pending, s.pending_expires_ms, s.pending_login_session_id) == ("", None, "")
    assert rig.purged == ["wx01"]
    detail = rig.store.list_audit(action=REAPER_AUDIT)
    assert len(detail) == 1 and "ls_A" in detail[0]["detail_json"]
    assert rig.store.get_account_full("wx01")["state"] == "stopped"
    rig.store.close()


async def test_reaper_is_idempotent(tmp_path):
    """**幂等**:条件一旦命中即置 NULL,重入不会重复释放。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.slot.claim("wx01", "ls_A")
    rig.clock.advance(rig.cfg.wechat.slot_pending_ttl_s * 1000 + 1)
    assert await rig.slot.reap() == ["wx01"]
    assert await rig.slot.reap() == []
    assert len(rig.store.list_audit(action=REAPER_AUDIT)) == 1
    rig.store.close()


# ---------------------------------------------------------------------- 接管(R4-8 + R5-4)
async def test_takeover_needs_both_threshold_and_confirm(tmp_path):
    """R5-4:判据 = ``error_since_ms`` 超 ``slot_error_takeover_s`` **且** ``confirm:true``;
    缺一律 ``409 RESOURCE_EXHAUSTED / reason='slot_held_by_error'``,**绝不自动接管**。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02")
    rig.slot.claim("wx02", "ls_H")
    rig.slot.promote("wx02")
    rig.store.transition("wx02", "error", state_code="KEY_FAIL", now_ms=rig.clock())

    with pytest.raises(ApiError) as e:                # 未达阈值 + 未确认
        await rig.slot.switch("wx01")
    assert e.value.http_status == 409 and e.value.reason == "slot_held_by_error"

    rig.clock.advance(rig.cfg.wechat.slot_error_takeover_s * 1000)
    ok, seconds = rig.slot.takeover_check()
    assert ok is True and seconds >= rig.cfg.wechat.slot_error_takeover_s
    with pytest.raises(ApiError) as e2:               # 达阈值但没带 confirm:true ⇒ 仍 409
        await rig.slot.switch("wx01")
    assert e2.value.reason == "slot_held_by_error" and "已故障" in e2.value.message
    assert rig.slot.view().holder == "wx02"
    rig.store.close()


async def test_takeover_with_confirm_stops_failed_holder_then_claims(tmp_path):
    """收到 ``confirm:true`` 且判定通过:先把故障 holder 停成 ``stopped``(令 ``slot_holder=''``)再抢;
    claim 的 ``WHERE slot_holder=''`` **不放宽**。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02")
    rig.slot.claim("wx02", "ls_H")
    rig.slot.promote("wx02")
    rig.store.transition("wx02", "error", now_ms=rig.clock())
    rig.clock.advance(rig.cfg.wechat.slot_error_takeover_s * 1000)

    stopped: list[str] = []

    async def stop_account(id):
        stopped.append(id)
        rig.store.transition(id, "stopped", now_ms=rig.clock())

    res = await rig.slot.switch("wx01", confirm=True, stop_account=stop_account)
    assert stopped == ["wx02"] and res["holder_before"] == "wx02" and res["target"] == "wx01"
    s = rig.slot.view()
    assert s.holder == "" and s.pending == "wx01" and s.pending_login_session_id == res["login_session_id"]
    assert len(rig.store.list_audit(action=TAKEOVER_AUDIT)) == 1
    rig.store.close()


async def test_takeover_never_fires_when_error_since_ms_is_null(tmp_path):
    """``error_since_ms IS NULL`` ⇒ 当前不在 error,接管分支恒不可进(R6-4 / §2.6 迁出 error 同事务清 NULL)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02")
    rig.slot.claim("wx02", "ls_H")
    rig.slot.promote("wx02")
    rig.store.transition("wx02", "error", now_ms=rig.clock())
    rig.clock.advance(rig.cfg.wechat.slot_error_takeover_s * 1000)
    rig.store.transition("wx02", "running", now_ms=rig.clock())      # 自己恢复了 ⇒ error_since_ms 清 NULL
    assert rig.slot.takeover_check() == (False, None)
    rig.store.close()


# ---------------------------------------------------------------------- #17/#18 切换
async def test_normal_switch_drains_logs_out_and_claims(tmp_path):
    """05 §2.4.5 ①~⑤:排空(上限 ``switch_drain_timeout_s``)→ 停 chatlog + 登出(#34)→ holder ``stopped`` → ``pending=目标``。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02", state="running")
    rig.slot.claim("wx02", "ls_H")
    rig.slot.promote("wx02")
    drained: list[tuple[str, float]] = []
    stopped: list[str] = []

    async def drain(id, timeout_s):
        drained.append((id, timeout_s))
        return True

    async def stop_account(id):
        stopped.append(id)
        rig.store.transition(id, "stopped", now_ms=rig.clock())

    res = await rig.slot.switch("wx01", stop_account=stop_account, drain=drain)
    assert drained == [("wx02", float(rig.slot.wechat_cfg.switch_drain_timeout_s))]
    assert rig.fake.logout_calls == 1 and stopped == ["wx02"]
    assert rig.slot.view().pending == "wx01" and res["holder_before"] == "wx02"
    assert len(rig.store.list_audit(action=SWITCH_AUDIT)) == 1
    rig.store.close()


async def test_switch_to_self_has_empty_drain_step(tmp_path):
    """05 §2.5.4 微信掉线重登:「目标就是自己,排空步骤天然为空」—— 仍先腾空 holder 再 claim,不调登出。"""
    rig = make_wechat_rig(tmp_path, state="login_required")
    rig.slot.claim("wx01", "ls_A")
    rig.slot.promote("wx01")
    stopped: list[str] = []

    async def stop_account(id):
        stopped.append(id)

    res = await rig.slot.switch("wx01", stop_account=stop_account)
    assert stopped == ["wx01"] and rig.fake.logout_calls == 0
    assert rig.slot.view() .pending == "wx01" and res["holder_before"] == "wx01"
    rig.store.close()


async def test_switch_while_pending_is_409(tmp_path):
    """C-13:切换中再调 ⇒ 409(槽位已有进行中的尝试)。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02")
    rig.slot.claim("wx02", "ls_B")
    with pytest.raises(ApiError) as e:
        await rig.slot.switch("wx01")
    assert e.value.http_status == 409 and e.value.reason == "slot_pending"
    rig.store.close()


async def test_switch_rejects_non_wechat_and_missing_account(tmp_path):
    rig = make_wechat_rig(tmp_path, state="stopped")
    rig.store.ensure_account("qd01", "qidian", state="stopped")
    with pytest.raises(ApiError) as e:
        await rig.slot.switch("qd01")
    assert e.value.http_status == 409 and e.value.reason == "not_wechat"
    with pytest.raises(ApiError) as e2:
        await rig.slot.switch("wx09")
    assert e2.value.http_status == 404
    rig.store.close()


async def test_switch_failure_releases_pending_and_does_not_log_original_back_in(tmp_path):
    """05 §2.4.5 末句:切换期间任意一步失败 ⇒ 槽位空、目标回 ``stopped``;**不会自动把原号登回去**。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    mk(rig, "wx02", state="running")
    rig.slot.claim("wx02", "ls_H")
    rig.slot.promote("wx02")

    async def stop_account(id):
        rig.store.transition(id, "stopped", now_ms=rig.clock())

    async def begin_login(target, ls):
        raise RuntimeError("微信没起来")

    with pytest.raises(RuntimeError):
        await rig.slot.switch("wx01", stop_account=stop_account, drain=None, begin_login=begin_login)
    s = rig.slot.view()
    assert s.holder == "" and s.pending == "" and s.pending_login_session_id == ""
    assert rig.store.get_account_full("wx01")["state"] == "stopped"
    assert rig.store.get_account_full("wx02")["state"] == "stopped"      # 原号没有被自动登回
    rig.store.close()


async def test_switch_generates_login_session_id_with_ls_prefix(tmp_path):
    """00 §6 / R3-11:``login_session_id`` = ``ls_`` + ULID,贯穿这一次尝试。"""
    rig = make_wechat_rig(tmp_path, state="stopped")
    res = await rig.slot.switch("wx01")
    assert res["login_session_id"].startswith("ls_") and len(res["login_session_id"]) == 29
    assert rig.slot.view().pending_login_session_id == res["login_session_id"]
    rig.store.close()
