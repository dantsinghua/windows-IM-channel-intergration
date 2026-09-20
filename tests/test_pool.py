"""pool(02 §2.2.5):used 只算 host=wsl & enabled & state ∉ {stopped,disabled,error};can_add 三通道;行级 claim;snapshot 形态(00 §7.6 + R6-6 空值口径)。"""
from __future__ import annotations

from qtrade_agent.config import AgentConfig
from tests.conftest import make_rig


def _mk(rig, id, channel, state, enabled=True):
    rig.store.ensure_account(id, channel, state=state, login_mode="password" if channel == "qidian" else "qrcode")
    if not enabled:
        rig.store.con.execute("UPDATE accounts SET enabled=0 WHERE id=?", (id,))
    return id


def test_used_mb_counts_only_active_wsl_accounts(rig3):
    pool = rig3.agent.pool
    _mk(rig3, "qd01", "qidian", "running")
    _mk(rig3, "qd02", "qidian", "stopped")
    _mk(rig3, "qd03", "qidian", "disabled", enabled=False)
    _mk(rig3, "qd04", "qidian", "error")
    _mk(rig3, "qd05", "qidian", "created")           # created 计入(尚未 stopped)
    _mk(rig3, "qq01", "qq", "login_required")
    assert pool.used_mb() == 2560 * 2 + 614
    assert pool.wsl_budget() == 11264 - 2048 and pool.free_mb() == 9216 - 5734


def test_can_add_and_alternatives(rig3):
    pool = rig3.agent.pool
    _mk(rig3, "qd01", "qidian", "running")
    _mk(rig3, "qd02", "qidian", "running")
    _mk(rig3, "qd03", "qidian", "running")            # used 7680, free 1536
    ok, reason, alts = pool.can_add("qidian")
    assert ok is False and reason == "wsl_budget"
    kinds = {a["kind"] for a in alts}
    assert "add_other_channel" in kinds and "stop_one" in kinds
    stop_one = [a for a in alts if a["kind"] == "stop_one"][0]
    assert stop_one["account_ids"] == ["qd01", "qd02", "qd03"] and stop_one["need_mb"] == 2560 and stop_one["free_mb"] == 1536
    assert pool.can_add("qq") == (True, "", [])
    rig3.store.transition("qd03", "stopped")           # stopped 不占额度
    assert pool.can_add("qidian")[0] is True


def test_reserve_is_row_level_claim(rig3):
    pool = rig3.agent.pool
    for i in range(1, 4):
        _mk(rig3, f"qd0{i}", "qidian", "running")
    assert pool.reserve("qq01", "qq") is True          # 1536 ≥ 614
    assert pool.reserve("qd04", "qidian") is False     # 1536 < 2560


def test_snapshot_shape_and_empty_value_convention(rig3):
    pool = rig3.agent.pool
    _mk(rig3, "qd01", "qidian", "running")
    s = pool.snapshot()
    assert s["pools"]["wsl"] == {"total_mb": 11264, "reserved_mb": 2048, "used_mb": 2560, "free_mb": 6656}
    w = s["pools"]["windows"]
    assert w["wechat_slots"] == {"used": 0, "max": 1, "holder": "", "pending": "", "pending_expires_at": None, "pending_login_session_id": ""}
    assert w["status"] == "unknown" and s["quota_mb"] == {"qidian": 2560, "qq": 614, "wechat": 1536}
    assert s["can_add"] == {"qidian": 2, "qq": 10, "wechat": 0} and set(s["realtime"]) == {"wsl_anon_mb", "win_available_mb"}


def test_can_add_wechat_requires_winagent_snapshot_and_free_slot(rig3):
    pool = rig3.agent.pool
    assert pool.can_add("wechat") == (False, "winagent_offline", [])
    pool.set_windows(total_mb=16384, wechat_enabled=False, known=True)
    assert pool.can_add("wechat")[:2] == (False, "wechat_disabled")
    pool.set_windows(wechat_enabled=True)
    assert pool.can_add("wechat") == (True, "", [])
    rig3.store.pool_set("windows", slot_holder="wx01")
    ok, reason, alts = pool.can_add("wechat")
    assert (ok, reason) == (False, "slot_held") and alts == [{"kind": "wechat_switch", "holder": "wx01"}]
    assert pool.snapshot()["pools"]["windows"]["wechat_slots"]["holder"] == "wx01"
    rig3.store.pool_set("windows", slot_holder="")
    assert rig3.store.wechat_slot_claim("wx02", "ls_x", expires_ms=rig3.clock() + 600_000) is True
    assert rig3.store.wechat_slot_claim("wx03", "ls_y", expires_ms=rig3.clock() + 600_000) is False    # pending 非空即抢不到
    sl = pool.snapshot()["pools"]["windows"]["wechat_slots"]
    assert sl["pending"] == "wx02" and sl["pending_login_session_id"] == "ls_x" and sl["pending_expires_at"]
    assert rig3.store.wechat_slot_release_pending("wx02") == 1
    sl = pool.snapshot()["pools"]["windows"]["wechat_slots"]
    assert sl == {"used": 0, "max": 1, "holder": "", "pending": "", "pending_expires_at": None, "pending_login_session_id": ""}


def test_pool_rows_seeded_from_config_only_once(tmp_path):
    from qtrade_agent.config import PoolConfig
    rig = make_rig(tmp_path, cfg=AgentConfig(pool=PoolConfig(quota_qidian_mb=3000)))
    assert rig.store.pool_get("wsl")["quota"]["qidian"] == 3000 and rig.store.pool_get("windows")["reserved_mb"] == 4096
    rig.store.pool_set("wsl", quota_json='{"qidian":2600,"qq":614,"wechat":1536}')
    rig.store.close()
    rig2 = make_rig(tmp_path, cfg=AgentConfig(pool=PoolConfig(quota_qidian_mb=3000)))
    assert rig2.store.pool_get("wsl")["quota"]["qidian"] == 2600            # 表是真值,toml 只是首次默认(C-43)
    rig2.store.close()
