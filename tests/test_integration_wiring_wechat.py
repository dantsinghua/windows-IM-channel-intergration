"""第五批接线:微信通道端到端(#17/#18 切换、登录流、槽位 reaper、读循环节拍、健康两条、login_cancel 回收)。

规格:02 §2.2.5 槽位 / §3.4.1 #16b/#17/#18;05 §2.4.2/§2.4.4/§2.4.5/§2.5.4。全程只碰 ``FakeWeChatWinAgent``。
"""
from __future__ import annotations

import pytest

from tests.test_integration_wiring_common import TOKEN_READ, H, close_rig, make_rig


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    yield r
    close_rig(r)


def _wechat(rig, aid: str = "wx01", *, state: str = "stopped", wxid: str | None = None):
    """直接建一行微信账号;顺带把 ``settings.seq.wechat`` 推到该 seq,免得后面 #18 再建号时撞 ``(channel, seq)`` 唯一键。"""
    seq = int(aid[2:])
    if int(rig.store.settings_get("seq.wechat") or 0) < seq:
        rig.store.settings_set("seq.wechat", seq, actor="test")
    rig.store.ensure_account(aid, "wechat", state=state, login_mode="qrcode", self_uid=wxid)
    rig.store.upsert_runtime(aid, kind=rig.store.RUNTIME_KIND["wechat"], desired_state="stopped")
    if wxid:
        rig.store.con.execute("UPDATE accounts SET wxid=? WHERE id=?", (wxid, aid))
    return rig.store.get_account_full(aid)


def _slot(rig) -> dict:
    return rig.store.pool_get("windows")


# ---------------------------------------------------------------- start = claim + 登录流
async def test_wechat_start_claims_slot_and_reaches_running(rig):
    """05 §2.4.2:start ⇒ 行级 claim 槽位 → ``WechatLoginFlow`` 驱动相位 → ``ready`` 时 ``pending → holder`` 并 running。"""
    _wechat(rig)
    rig.wechat.phase = "ready"
    await rig.agent.accounts.start("wx01", actor="token:console")
    await rig.agent.accounts.wait_idle("wx01")
    assert rig.store.get_account_full("wx01")["state"] == "running"
    assert _slot(rig)["slot_holder"] == "wx01" and _slot(rig)["slot_pending"] == ""


async def test_second_wechat_start_is_refused_by_pool(rig):
    """单槽:holder 已占 ⇒ ``can_add('wechat')`` 拒,``409 slot_held`` 并给 ``wechat_switch`` 这条出路。"""
    _wechat(rig)
    _wechat(rig, "wx02")
    rig.wechat.phase = "ready"
    await rig.agent.accounts.start("wx01", actor="token:console")
    await rig.agent.accounts.wait_idle("wx01")
    r = rig.client.post("/api/v1/accounts/wx02/start", headers=H())
    assert r.status_code == 409 and r.json()["error"]["reason"] == "slot_held"
    assert r.json()["error"]["hint_actions"] == ["wechat_switch"]


def test_pool_alternatives_kind_stays_in_enum(rig):
    """R6-54 钉死 ``alternatives[].kind ∈ {add_other_channel, stop_one, wechat_switch}``;
    原实现在 ``slot_pending`` 分支给的 ``wait_or_cancel`` 在枚举外,已收进 ``wechat_switch``。"""
    _wechat(rig)
    assert rig.store.wechat_slot_claim("wx01", "ls-x", rig.clock() + 600_000) is True
    ok, reason, alts = rig.agent.pool.can_add("wechat")
    assert ok is False and reason == "slot_pending"
    assert [a["kind"] for a in alts] == ["wechat_switch"]


# ---------------------------------------------------------------- #17 switch
async def test_switch_endpoint_moves_slot_to_target(rig):
    """#17:排空 → 停 chatlog/登出(#34)→ holder ``stopped`` → ``pending=目标`` → 目标进登录流。"""
    _wechat(rig)
    _wechat(rig, "wx02")
    rig.wechat.phase = "ready"
    await rig.agent.accounts.start("wx01", actor="token:console")
    await rig.agent.accounts.wait_idle("wx01")
    logout_before = rig.wechat.logout_calls

    r = rig.client.post("/api/v1/accounts/wx02/switch", headers=H(), json={})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True and body["holder_before"] == "wx01" and body["target"] == "wx02" and body["login_session_id"]
    assert rig.wechat.logout_calls == logout_before + 1              # 05 §2.4.5 ②:#34 logout 内含「先停 chatlog 再关微信」
    await rig.agent.accounts.wait_idle("wx02")
    assert rig.store.get_account_full("wx01")["state"] == "stopped"
    assert _slot(rig)["slot_holder"] == "wx02"


def test_switch_on_non_wechat_is_not_applicable(rig):
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    r = rig.client.post("/api/v1/accounts/qd01/switch", headers=H(), json={})
    assert r.status_code == 409 and r.json()["error"]["reason"] == "not_wechat"


def test_switch_needs_write_level(rig):
    _wechat(rig)
    r = rig.client.post("/api/v1/accounts/wx01/switch", headers=H(TOKEN_READ), json={})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


async def test_switch_from_error_holder_requires_confirm(rig):
    """R5-4:holder 处于 ``error`` 且超 ``slot_error_takeover_s`` 时**不自动抢**;不带 ``confirm:true`` 恒 409 ``slot_held_by_error``。"""
    _wechat(rig)
    _wechat(rig, "wx02")
    rig.wechat.phase = "ready"
    await rig.agent.accounts.start("wx01", actor="token:console")
    await rig.agent.accounts.wait_idle("wx01")
    rig.agent.accounts.transition("wx01", "error", state_code="KEY_FAIL", state_reason="造一个故障 holder")
    rig.clock.advance(rig.agent.cfg.wechat.slot_error_takeover_s * 1000 + 1000)

    r = rig.client.post("/api/v1/accounts/wx02/switch", headers=H(), json={})
    assert r.status_code == 409 and r.json()["error"]["reason"] == "slot_held_by_error"
    assert r.json()["error"]["error_seconds"] >= rig.agent.cfg.wechat.slot_error_takeover_s

    r = rig.client.post("/api/v1/accounts/wx02/switch", headers=H(), json={"confirm": True})
    assert r.status_code == 202 and r.json()["holder_before"] == "wx01"
    await rig.agent.accounts.wait_idle("wx02")
    assert _slot(rig)["slot_holder"] == "wx02"


# ---------------------------------------------------------------- #18 switch_new
async def test_switch_new_creates_row_then_switches(rig):
    """#18 / R-23:立刻建 ``wxNN`` 行(``state='created'``、``wxid=NULL``)再走同一条 switch;槽位 ``pending=wxNN`` 不是 "new"。"""
    r = rig.client.post("/api/v1/accounts/switch", headers=H(), json={"target": "new", "label": "新号"})
    assert r.status_code == 202
    target = r.json()["target"]
    assert target.startswith("wx")
    row = rig.store.get_account_full(target)
    assert row is not None and row["channel"] == "wechat" and row["wxid"] is None
    assert _slot(rig)["slot_pending"] in (target, "") or _slot(rig)["slot_holder"] == target
    await rig.agent.accounts.wait_idle(target)


def test_switch_new_rejects_other_targets(rig):
    r = rig.client.post("/api/v1/accounts/switch", headers=H(), json={"target": "wx07"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_target"


async def test_switch_new_works_while_slot_is_held(rig):
    """#18 的语义就是「槽位被占着也要新建一个号去顶」——建行这步不得被 ``can_add('wechat')`` 拦死。"""
    _wechat(rig)
    rig.wechat.phase = "ready"
    await rig.agent.accounts.start("wx01", actor="token:console")
    await rig.agent.accounts.wait_idle("wx01")
    r = rig.client.post("/api/v1/accounts/switch", headers=H(), json={"target": "new"})
    assert r.status_code == 202 and r.json()["holder_before"] == "wx01"
    await rig.agent.accounts.wait_idle(r.json()["target"])


# ---------------------------------------------------------------- #16b 取消 pending:必须回收中间产物
async def test_login_cancel_releases_pending_and_purges(rig):
    """02 §2.2.5 ①:释放 pending 时**同步回收**该次绑定的中间产物。
    原实现的 ``if row["host"] == "wsl"`` 对微信恒假(微信账号 ``host='windows'``)⇒ 永不回收,已改走 ``WechatSlot.release_pending``。"""
    _wechat(rig)
    ls = "ls-cancel-1"
    assert rig.store.wechat_slot_claim("wx01", ls, rig.clock() + 600_000) is True
    purged: list[str] = []
    rig.agent.wechat_slot._purge = lambda row: _record(purged, row["id"])
    res = await rig.agent.accounts.login_cancel("wx01", ls, actor="token:console")
    assert res == {"cancelled": True, "stale": False}
    assert _slot(rig)["slot_pending"] == "" and _slot(rig)["slot_pending_expires_ms"] is None
    assert purged == ["wx01"]
    actions = [r["action"] for r in rig.store.con.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "slot_pending_cancelled" in actions


async def test_login_cancel_with_stale_session_is_noop(rig):
    _wechat(rig)
    assert rig.store.wechat_slot_claim("wx01", "ls-current", rig.clock() + 600_000) is True
    res = await rig.agent.accounts.login_cancel("wx01", "ls-old", actor="token:console")
    assert res == {"cancelled": False, "stale": True, "current_login_session_id": "ls-current"}
    assert _slot(rig)["slot_pending"] == "wx01"


# ---------------------------------------------------------------- T-11 续期 + reaper
async def test_login_flow_renews_pending_ttl(rig):
    """T-11:``slot_pending_ttl_s``(600)< ``qr_max_wait_s``(1800)⇒ 登录流在世期间必须续期,否则扫码超 10 分钟被 reaper 误杀。"""
    _wechat(rig)
    rig.wechat.phases_script = [{"phase": "qrcode"}, {"phase": "qrcode"}, {"phase": "ready"}]
    task = rig.agent.accounts._spawn("wx01", rig.agent.wechat_login.run("wx01", "ls-renew"))
    rig.store.wechat_slot_claim("wx01", "ls-renew", rig.clock() + 1000)     # 故意给一个 1 秒就过期的 TTL
    await task
    assert rig.store.get_account_full("wx01")["state"] == "running"


async def test_slot_reaper_releases_only_expired_pending(rig):
    _wechat(rig)
    assert rig.store.wechat_slot_claim("wx01", "ls-x", rig.clock() + 600_000) is True
    assert await rig.agent.scheduler.run_once("wechat_slot_reaper") is True
    assert _slot(rig)["slot_pending"] == "wx01"                              # 没过期:不动
    rig.clock.advance(601_000)
    await rig.agent.scheduler.run_once("wechat_slot_reaper")
    assert _slot(rig)["slot_pending"] == ""


# ---------------------------------------------------------------- 读循环节拍(05 §2.4.4 ⑦)
async def test_wechat_poll_all_respects_per_account_interval(rig):
    """平时 ``[adapters.wechat] poll_interval_s``=5 s;每秒一轮的 scheduler 任务自己判到点,不到点不打扰 WinAgent。"""
    _wechat(rig, state="running")
    rig.wechat.add_row(talker="wxid_peer", content="你好")
    await rig.agent.scheduler.run_once("wechat_poll_all")
    first = len([c for c in rig.wechat.calls if "/wechat/read" in c[1]])
    assert first >= 1
    await rig.agent.scheduler.run_once("wechat_poll_all")                    # 没到 5 s:本轮跳过
    assert len([c for c in rig.wechat.calls if "/wechat/read" in c[1]]) == first
    rig.clock.advance(5_001)
    await rig.agent.scheduler.run_once("wechat_poll_all")
    assert len([c for c in rig.wechat.calls if "/wechat/read" in c[1]]) > first


# ---------------------------------------------------------------- 健康两条(05 §2.5.4)
async def test_screen_locked_degrades_and_unlock_recovers(rig):
    """锁屏 ⇒ ``degraded(SCREEN_LOCKED)``;解锁自动回 ``running``(**不需要扫码,这不是登录**)。"""
    _wechat(rig, state="running")
    rig.wechat.screen_locked = True
    await rig.agent.scheduler.run_once("health_wechat")
    row = rig.store.get_account_full("wx01")
    assert row["state"] == "degraded" and row["state_code"] == "SCREEN_LOCKED"
    rig.wechat.screen_locked = False
    await rig.agent.scheduler.run_once("health_wechat")
    assert rig.store.get_account_full("wx01")["state"] == "running"


async def test_key_fail_degrades_and_retries_key_at_most_three_times_per_hour(rig):
    """chatlog 挂、微信在线 ⇒ ``degraded(KEY_FAIL)`` 且自动试钥(``key_retry_per_hour=3``);超限停在 degraded 并 alert(error)。
    (R6-91:前提是「跑起来过的号」—— 运行中的微信号必然已回填 wxid,夹具按真实前提带上。)"""
    _wechat(rig, state="running", wxid="wxid_running")
    rig.wechat.chatlog = {**rig.wechat.chatlog, "key_ok": False}
    for _ in range(5):
        await rig.agent.scheduler.run_once("health_wechat")
    row = rig.store.get_account_full("wx01")
    assert row["state"] == "degraded" and row["state_code"] == "KEY_FAIL"
    assert rig.wechat.key_retry_calls == 3
    sev = [e["payload"]["severity"] for e in rig.events_of("alert") if e["payload"]["code"] == "ACCOUNT_OFFLINE"]
    assert "error" in sev


async def test_key_fail_on_never_completed_first_login_is_not_auto_retried(rig):
    """R6-91(2026-10-10 真机):首登卡在取钥(未回填 wxid)的号,巡检**不**自动 key/retry ——
    取钥要人重登,自动重试只会每 10 s 把用户正在做的那一轮拆掉;这类号由控制台「重新取钥」重跑登录流。"""
    _wechat(rig, state="degraded")
    rig.store.con.execute("UPDATE accounts SET state_code='KEY_FAIL' WHERE id='wx01'")
    rig.wechat.chatlog = {**rig.wechat.chatlog, "key_ok": False}
    for _ in range(3):
        await rig.agent.scheduler.run_once("health_wechat")
    assert rig.wechat.key_retry_calls == 0


@pytest.mark.parametrize("code", ["KEY_FAIL", "WAIT_UI_TREE"])
def test_degraded_first_login_wechat_can_be_restarted(rig, code):
    """R6-91:首登卡在取钥 / UI 树的微信号(degraded,未回填 wxid)允许 #9 start 重跑登录流(不登出微信),
    否则只能删号重建;其它 degraded 码维持「already」不重跑。"""
    _wechat(rig, state="degraded")
    rig.store.con.execute("UPDATE accounts SET state_code=? WHERE id='wx01'", (code,))
    r = rig.client.post("/api/v1/accounts/wx01/start", headers=H(), json={})
    assert r.status_code == 202, r.text
    assert r.json().get("already") is not True
    _wechat(rig, "wx02", state="degraded")
    rig.store.con.execute("UPDATE accounts SET state_code='SCREEN_LOCKED' WHERE id='wx02'")
    r2 = rig.client.post("/api/v1/accounts/wx02/start", headers=H(), json={})
    assert r2.status_code == 200 and r2.json().get("already") is True


def _record(bucket: list[str], value: str):
    async def done() -> None:
        bucket.append(value)
    return done()
