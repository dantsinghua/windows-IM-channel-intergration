"""登录阶段端点 #12/#13/#14/#15/#16b、掉线登记与提醒(05 §2.5.4 / §2.0 login_session_id)、#20 能力矩阵、#22 账号级设置、#23 批量。"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from qtrade_agent.alerts import ACCOUNT_OFFLINE
from qtrade_agent.config import AccountsConfig, AgentConfig
from tests.conftest import Clock, make_rig

TA, TW, TR = "admin-token", "write-token", "read-token"


def H(token=TA):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(tmp_path):
    rigs = []

    def build(*, login_fn=None, cfg=None, clock=None):
        rig = make_rig(tmp_path, login_fn=login_fn, cfg=cfg, clock=clock or Clock(auto_step_ms=50))
        st = rig.store
        st.upsert_api_client(app_id="console", name="控制台", level="admin", token=TA)
        st.upsert_api_client(app_id="writer", name="写", level="write", token=TW)
        st.upsert_api_client(app_id="reader", name="读", level="read", token=TR)
        client = TestClient(rig.agent.create_api(), client=("127.0.0.1", 40000))
        client.__enter__()
        rig.client = client
        rig.idle = lambda id: client.portal.call(rig.agent.accounts.wait_idle, id)
        rig.states = lambda id: [e["payload"]["state"] for e in st.list_events(event="account_state", account_id=id)]
        rigs.append(rig)
        return rig
    yield build
    for r in rigs:
        r.client.__exit__(None, None, None)
        r.store.close()


def create_and_start(rig, label="张三", **login):
    c = rig.client
    r = c.post("/api/v1/accounts", json={"channel": "qidian", "label": label, "idempotency_key": f"k-{label}", "login": {"mode": "password", **login}}, headers=H(TW))
    aid = r.json()["data"]["id"]
    c.post(f"/api/v1/accounts/{aid}/start", headers=H(TW))
    rig.idle(aid)
    assert rig.store.get_account_full(aid)["state"] == "login_required"
    return aid


# ---------------------------------------------------------------- #12 login
def test_login_requires_secret_or_saved_credential_and_runs_login_fn(api):
    seen = []

    async def login_fn(row, account, secret):
        seen.append((account, secret))
        return "running"
    rig = api(login_fn=login_fn)
    c, st = rig.client, rig.store
    aid = create_and_start(rig, account="u1")
    r = c.post(f"/api/v1/accounts/{aid}/login", json={}, headers=H(TW))
    assert r.status_code == 400 and r.json()["error"]["reason"] == "secret_required"
    r = c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "pw", "remember": False}, headers=H(TW))
    assert r.status_code == 202 and r.json()["state"] == "logging_in" and r.json()["login_session_id"].startswith("ls_")
    ls = r.json()["login_session_id"]
    rig.idle(aid)
    assert seen == [("u1", "pw")] and st.get_account_full(aid)["state"] == "running"
    assert rig.states(aid)[-3:] == ["login_required", "logging_in", "running"]
    assert "account/qd01" not in rig.vault.entries and st.get_account_full(aid)["credential_ref"] is None     # remember=false 不落 Vault
    evs = [e for e in st.list_events(event="account_state", account_id=aid) if e["payload"]["login_session_id"] == ls]
    assert [e["payload"]["state"] for e in evs] == ["logging_in", "running"]
    for row in st.list_audit():
        assert "pw" not in row["detail_json"] or row["action"] != "account.login"
    r = c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "pw"}, headers=H(TW))
    assert r.status_code == 409 and r.json()["error"]["reason"] == "already_running"


def test_login_with_remember_saves_to_vault_and_uses_it_next_time(api):
    async def login_fn(row, account, secret):
        return "WAIT_SMS" if secret == "pw" else None
    rig = api(login_fn=login_fn)
    c, st = rig.client, rig.store
    aid = create_and_start(rig)
    r = c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "pw", "remember": True, "account": "13800000000"}, headers=H(TW))
    assert r.status_code == 202
    rig.idle(aid)
    a = st.get_account_full(aid)
    assert a["state"] == "login_required" and a["state_code"] == "WAIT_SMS" and a["remember"] == 1 and a["credential_ref"] == f"vault://account/{aid}"
    assert rig.vault.entries[f"account/{aid}"].value == "pw" and json.loads(a["identity_json"])["login_account"] == "13800000000"
    p = c.get(f"/api/v1/accounts/{aid}/prompt", headers=H(TR)).json()
    assert p["kind"] == "WAIT_SMS" and p["login_session_id"].startswith("ls_")
    # 再次 login 不带 secret:用 Vault 里的
    r = c.post(f"/api/v1/accounts/{aid}/login", json={}, headers=H(TW))
    assert r.status_code == 202
    rig.idle(aid)
    assert rig.vault.reads[-1][0] == f"account/{aid}"
    # Vault 离线时不回退成让人输入:error(VAULT_UNAVAILABLE) + 503
    rig.vault.offline = True
    r = c.post(f"/api/v1/accounts/{aid}/login", json={}, headers=H(TW))
    assert r.status_code == 503 and r.json()["error"]["reason"] == "vault_unavailable"
    assert st.get_account_full(aid)["state"] == "error" and st.get_account_full(aid)["state_code"] == "VAULT_UNAVAILABLE"


def test_login_bad_credential_flags_vault_and_errors(api):
    async def login_fn(row, account, secret):
        return "bad_credential"
    rig = api(login_fn=login_fn)
    c, st = rig.client, rig.store
    aid = create_and_start(rig)
    assert c.put(f"/api/v1/accounts/{aid}/credential", json={"secret": "old", "remember": True}, headers=H(TW)).status_code == 200
    assert c.post(f"/api/v1/accounts/{aid}/login", json={}, headers=H(TW)).status_code == 202
    rig.idle(aid)
    a = st.get_account_full(aid)
    assert a["state"] == "error" and a["state_code"] == "BAD_CREDENTIAL"
    assert rig.vault.entries[f"account/{aid}"].suspect is True and st.get_runtime(aid)["suspect_credential"] == 1
    assert c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "x"}, headers=H(TW)).json()["error"]["reason"] == "bad_state"   # error 态先 start


# ---------------------------------------------------------------- #13 / #14
def test_put_and_delete_credential_do_not_login(api):
    rig = api()
    c, st = rig.client, rig.store
    aid = create_and_start(rig)
    r = c.put(f"/api/v1/accounts/{aid}/credential", json={"account": "u9", "secret": "s9", "remember": True}, headers=H(TW))
    assert r.status_code == 200 and "s9" not in r.text
    a = r.json()["data"]
    assert a["login"] == {"mode": "password", "credential_ref": f"vault://account/{aid}", "remember": True} and a["state"] == "login_required"
    assert rig.vault.entries[f"account/{aid}"].value == "s9" and st.get_runtime(aid)["suspect_credential"] == 0
    r = c.delete(f"/api/v1/accounts/{aid}/credential", headers=H(TW))
    assert r.json()["data"]["login"] == {"mode": "password", "credential_ref": None, "remember": False} and f"account/{aid}" not in rig.vault.entries
    assert c.put(f"/api/v1/accounts/{aid}/credential", json={"remember": True}, headers=H(TW)).status_code == 400
    assert c.put(f"/api/v1/accounts/{aid}/credential", json={"secret": "s"}, headers=H(TR)).status_code == 403


# ---------------------------------------------------------------- #15 / #16b
def test_prompt_and_login_cancel(api):
    started = []

    async def login_fn(row, account, secret):
        started.append(1)
        import asyncio
        await asyncio.sleep(30)                       # 卡住,等取消
        return "running"
    rig = api(login_fn=login_fn)
    c, st = rig.client, rig.store
    aid = create_and_start(rig)
    p = c.get(f"/api/v1/accounts/{aid}/prompt", headers=H(TR)).json()
    assert p["kind"] == "WAIT_PASSWORD" and p["text"] and p["login_session_id"].startswith("ls_")
    assert c.get(f"/api/v1/accounts/{aid}/prompt?login_session_id=ls_OLD", headers=H(TR)).json()["kind"] is None
    r = c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "pw"}, headers=H(TW))
    ls = r.json()["login_session_id"]
    assert c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "pw"}, headers=H(TW)).json()["error"]["reason"] == "busy"
    r = c.post(f"/api/v1/accounts/{aid}/login/cancel", json={"login_session_id": "ls_STALE"}, headers=H(TW))
    assert r.status_code == 200 and r.json() == {"ok": True, "cancelled": False, "stale": True, "current_login_session_id": ls}      # 不误杀
    assert st.get_account_full(aid)["state"] == "logging_in"
    r = c.post(f"/api/v1/accounts/{aid}/login/cancel", json={"login_session_id": ls}, headers=H(TW))
    assert r.json() == {"ok": True, "cancelled": True, "stale": False}
    a = st.get_account_full(aid)
    assert a["state"] == "login_required" and a["state_code"] == "WAIT_PASSWORD" and not rig.agent.accounts.busy(aid)
    assert c.get(f"/api/v1/accounts/{aid}/prompt?login_session_id={ls}", headers=H(TR)).json()["kind"] is None       # 那一次已结束
    assert c.get(f"/api/v1/accounts/{aid}/prompt", headers=H(TR)).json()["kind"] == "WAIT_PASSWORD"
    r = c.post(f"/api/v1/accounts/{aid}/login/cancel", json={}, headers=H(TW))
    assert r.status_code == 409 and r.json()["code"] == "NOT_APPLICABLE"                                              # 无进行中尝试
    assert st.list_audit(action="account.login_cancel")[0]["detail_json"].find(ls) > 0
    # 新一次 login 换新 id
    ls2 = c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "pw"}, headers=H(TW)).json()["login_session_id"]
    assert ls2 != ls
    c.post(f"/api/v1/accounts/{aid}/login/cancel", json={}, headers=H(TW))          # 不带 id = 取消当前这次(兼容路径)
    assert st.get_account_full(aid)["state"] == "login_required"
    ev = st.list_events(event="account_state", account_id=aid)
    assert ev[-1]["payload"]["login_session_id"] == ls2


def test_running_account_prompt_is_null_and_stopped_cannot_login(api):
    rig = api()
    c, st = rig.client, rig.store
    aid = create_and_start(rig)
    rig.agent.accounts.transition(aid, "running")
    assert c.get(f"/api/v1/accounts/{aid}/prompt", headers=H(TR)).json() == {"ok": True, "kind": None}
    st.transition(aid, "stopped")
    assert c.post(f"/api/v1/accounts/{aid}/login", json={"secret": "x"}, headers=H(TW)).json()["error"]["reason"] == "bad_state"


# ---------------------------------------------------------------- 05 §2.5.4 掉线登记 + 提醒
def test_mark_offline_and_login_remind(api):
    clock = Clock(auto_step_ms=0)
    rig = api(cfg=AgentConfig(accounts=AccountsConfig(login_remind_interval_s=300)), clock=clock)
    c, st, acc, alerts = rig.client, rig.store, rig.agent.accounts, rig.agent.alerts
    aid = create_and_start(rig)
    acc.transition(aid, "running")
    with pytest.raises(ValueError):
        acc.mark_offline(aid, "WAIT_SMS")
    row = acc.mark_offline(aid, "KICKED")
    assert row["state"] == "login_required" and row["state_code"] == "KICKED"
    rt = st.get_runtime(aid)
    assert rt["last_offline_code"] == "KICKED" and rt["last_offline_ms"] == clock.now_ms and rt["offline_count_1h"] == 1
    ev = st.list_events(event="account_state", account_id=aid)[-1]
    assert ev["payload"]["prompt"] == {"kind": "KICKED", "text": "请点「登录」重新登录"} and ev["payload"]["login_session_id"] == "" and ev["trace_id"]
    assert alerts.active[(ACCOUNT_OFFLINE, f"account:{aid}")].severity == "warn"
    assert c.get(f"/api/v1/accounts/{aid}/prompt", headers=H(TR)).json()["kind"] == "KICKED"
    # 提醒:同 trace_id,每 300 s 一条,不计入告警
    trace = ev["trace_id"]
    assert c.portal.call(acc.login_remind) == 0
    clock.advance(301_000)
    assert c.portal.call(acc.login_remind) == 1
    ev2 = st.list_events(event="account_state", account_id=aid)[-1]
    assert ev2["trace_id"] == trace and ev2["payload"]["remind"] is True and ev2["payload"]["prompt"]["kind"] == "KICKED"
    assert len(alerts.active) == 1
    # 1 小时内第 3 次掉线升 crit
    for _ in range(2):
        acc.transition(aid, "running")
        clock.advance(60_000)
        acc.mark_offline(aid, "LOGGED_OUT")
    assert st.get_runtime(aid)["offline_count_1h"] == 3 and alerts.active[(ACCOUNT_OFFLINE, f"account:{aid}")].severity == "crit"
    acc.transition(aid, "running")
    clock.advance(3700_000)
    acc.mark_offline(aid, "TOKEN_EXPIRED")
    assert st.get_runtime(aid)["offline_count_1h"] == 1                              # 滚动 1 小时窗


# ---------------------------------------------------------------- #20 / #22 / #23
def test_capabilities_matrix_follows_state(api):
    rig = api()
    c = rig.client
    aid = create_and_start(rig)
    r = c.get(f"/api/v1/accounts/{aid}/capabilities", headers=H(TR)).json()
    assert r["matrix"] == {"get_state": "supported", "read_messages": "supported", "send_text": "not_applicable"} and r["capabilities"] == ["get_state", "read_messages"]
    rig.agent.accounts.transition(aid, "running")
    r = c.get(f"/api/v1/accounts/{aid}/capabilities", headers=H(TR)).json()
    assert r["matrix"]["send_text"] == "supported" and r["capabilities"] == ["get_state", "read_messages", "send_text"]


def test_patch_settings_columns_json_and_validation(api):
    rig = api()
    c, st = rig.client, rig.store
    aid = create_and_start(rig)
    body = {"send": {"min_interval_ms": 2000, "max_per_minute": 10}, "sessions": {"allowlist": ["415011447"]}, "gates": {"custom": []},
            "auto_recover": False, "capture_text": False, "retention_days": 14, "auto_stop_on_pressure": True, "mail_route_id": None}
    r = c.patch(f"/api/v1/accounts/{aid}/settings", json=body, headers=H(TW))
    assert r.status_code == 200
    row = st.get_account_full(aid)
    assert row["auto_recover"] == 0 and row["capture_text"] == 0 and row["retention_days"] == 14
    s = json.loads(row["settings_json"])
    assert s == {"send": {"min_interval_ms": 2000, "max_per_minute": 10}, "sessions": {"allowlist": ["415011447"]}, "gates": {"custom": []},
                 "auto_stop_on_pressure": True, "mail_route_id": None}
    assert c.patch(f"/api/v1/accounts/{aid}/settings", json={"retention_days": 31}, headers=H(TW)).json()["error"]["reason"] == "retention_too_long"
    assert c.patch(f"/api/v1/accounts/{aid}/settings", json={"label": "x"}, headers=H(TW)).json()["error"]["reason"] == "bad_field"
    assert c.patch(f"/api/v1/accounts/{aid}/settings", json={"mail_route_id": 99}, headers=H(TW)).json()["error"]["reason"] == "mail_route_mismatch"
    now = rig.clock()
    st.con.execute("INSERT INTO mail_routes(id, channel, account_id, created_ms, updated_ms) VALUES (5, 'qq', NULL, ?, ?)", (now, now))
    st.con.execute("INSERT INTO mail_routes(id, channel, account_id, created_ms, updated_ms) VALUES (6, 'qidian', NULL, ?, ?)", (now, now))
    assert c.patch(f"/api/v1/accounts/{aid}/settings", json={"mail_route_id": 5}, headers=H(TW)).status_code == 400            # 别的通道
    assert c.patch(f"/api/v1/accounts/{aid}/settings", json={"mail_route_id": 6}, headers=H(TW)).status_code == 200            # 同通道级
    assert st.list_audit(action="settings.update")
    assert c.patch(f"/api/v1/accounts/{aid}/settings", json={"auto_recover": True}, headers=H(TR)).status_code == 403


def test_batch_explicit_ids_serial_and_partial_failure(api):
    rig = api()
    c, st = rig.client, rig.store
    for lbl in ("甲", "乙"):
        c.post("/api/v1/accounts", json={"channel": "qidian", "label": lbl, "idempotency_key": lbl}, headers=H(TW))
    assert c.post("/api/v1/accounts/batch", json={"ids": ["*"], "action": "start"}, headers=H(TW)).json()["error"]["reason"] == "bad_ids"
    assert c.post("/api/v1/accounts/batch", json={"ids": ["qd01"], "action": "purge"}, headers=H(TW)).json()["error"]["reason"] == "bad_action"
    r = c.post("/api/v1/accounts/batch", json={"ids": ["qd01", "qd09", "qd02"], "action": "start"}, headers=H(TW))
    assert r.status_code == 200
    res = r.json()["results"]
    assert res["qd01"] == {"ok": True, "code": "OK"} and res["qd09"]["code"] == "TARGET_NOT_FOUND" and res["qd02"]["ok"] is True
    assert st.get_account_full("qd01")["state"] == "login_required" and st.get_account_full("qd02")["state"] == "login_required"
    assert rig.agent.runtime.max_concurrent_starts == 1
    r = c.post("/api/v1/accounts/batch", json={"ids": ["qd01", "qd02"], "action": "disable"}, headers=H(TW)).json()["results"]
    assert all(v["ok"] for v in r.values()) and st.get_account_full("qd02")["state"] == "disabled"
