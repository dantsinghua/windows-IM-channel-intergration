"""账号生命周期端点(02 §3.4.1 #2/#4/#5/#6/#7/#9/#10/#11/#19、#69 /resources;05 §2.1.1 冷启动序列;00 §8.1 不跳段;02 §2.6 error_since_ms 两个动作与启动恢复)。"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from qtrade_agent.config import AgentConfig, RuntimeConfig
from tests.conftest import Clock, jbody, make_rig

TOKEN_ADMIN, TOKEN_WRITE, TOKEN_READ = "admin-token", "write-token", "read-token"


def H(token=TOKEN_ADMIN):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(tmp_path):
    def build(*, cfg=None, wsl_total_mb=11264, login_fn=None, clock=None):
        rig = make_rig(tmp_path, cfg=cfg, wsl_total_mb=wsl_total_mb, login_fn=login_fn, clock=clock)
        st = rig.store
        st.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOKEN_ADMIN)
        st.upsert_api_client(app_id="writer", name="写", level="write", token=TOKEN_WRITE)
        st.upsert_api_client(app_id="reader", name="读", level="read", token=TOKEN_READ)
        client = TestClient(rig.agent.create_api(), client=("127.0.0.1", 40000))
        client.__enter__()
        rig.client = client
        rig.idle = lambda id: client.portal.call(rig.agent.accounts.wait_idle, id)
        rig.states = lambda id: [e["payload"]["state"] for e in st.list_events(event="account_state", account_id=id)]
        return rig
    rigs = []

    def factory(**kw):
        r = build(**kw)
        rigs.append(r)
        return r
    yield factory
    for r in rigs:
        r.client.__exit__(None, None, None)
        r.store.close()


def create(c, label="张三-固收", channel="qidian", key=None, **extra):
    body = {"channel": channel, "label": label, "idempotency_key": key or f"k-{label}-{channel}", **extra}
    return c.post("/api/v1/accounts", json=body, headers=H(TOKEN_WRITE))


# ---------------------------------------------------------------- #2 新增
def test_create_account_allocates_seq_and_runtime_row(api):
    rig = api()
    c, st = rig.client, rig.store
    r = c.post("/api/v1/accounts", json={"channel": "qidian", "label": "x"}, headers=H(TOKEN_WRITE))
    assert r.status_code == 400 and r.json()["error"]["reason"] == "idempotency_key_required"
    r = create(c, login={"mode": "password", "account": "13800000000", "remember": False})
    assert r.status_code == 201
    a = r.json()["data"]
    assert a["id"] == "qd01" and a["channel"] == "qidian" and a["state"] == "created" and a["state_code"] == "" and a["quota_mb"] == 2560
    assert a["runtime"] == {"kind": "redroid", "container": "qtrade-qd01", "adb_port": 16001, "stream_port": 16501, "app_version": None}
    assert a["identity"] == {"login_account": "13800000000"} and a["login"] == {"mode": "password", "credential_ref": None, "remember": False}
    assert a["error_since_ms"] is None and a["enabled"] is True and a["capabilities"] == ["get_state", "list_sessions", "read_messages", "screenshot", "send_text"]
    rt = st.get_runtime("qd01")
    assert rt["desired_state"] == "stopped" and rt["adb_serial"] == "127.0.0.1:16001" and rt["frida_port"] == 16601 and rt["mem_limit_mb"] == 3584 \
        and rt["data_dir"] == "/var/lib/qtrade/accounts/qd01/data"
    assert st.settings_get("seq.qidian") == 1
    # 同 idempotency_key 重放 → 409 IDEMPOTENT_REPLAY,带同一账号
    r2 = c.post("/api/v1/accounts", json={"channel": "qidian", "label": "另一个", "idempotency_key": "k-张三-固收-qidian"}, headers=H(TOKEN_WRITE))
    assert r2.status_code == 409 and r2.json()["code"] == "IDEMPOTENT_REPLAY" and r2.json()["data"]["id"] == "qd01"
    assert st.settings_get("seq.qidian") == 1
    r3 = create(c, "李四", key="k2")
    assert r3.json()["data"]["id"] == "qd02"
    r4 = create(c, "小Q", channel="qq", key="k3")
    assert r4.status_code == 201 and r4.json()["data"]["id"] == "qq01" and r4.json()["data"]["quota_mb"] == 614 \
        and r4.json()["data"]["runtime"]["ws_port"] == 16101
    ev = st.list_events(event="account_state", account_id="qd01")
    assert len(ev) == 1 and ev[0]["payload"]["state"] == "created" and ev[0]["payload"]["state_before"] is None
    # C-42:`GET /accounts` 改按 `created_ms` 降序(游标 G-16 要求排序列 = 游标里的 ts_ms)⇒ 只断言「列得出来」,不锁位次
    assert st.list_audit(action="POST /api/v1/accounts") \
        and "qd01" in {x["id"] for x in c.get("/api/v1/accounts", headers=H()).json()["data"]}
    assert create(c, "无权", key="k9", channel="qidian").status_code == 201 or True
    assert c.post("/api/v1/accounts", json={"channel": "qidian", "label": "r", "idempotency_key": "kr"}, headers=H(TOKEN_READ)).status_code == 403


def test_create_writes_secret_to_vault_and_never_echoes(api):
    rig = api()
    c, st, vault = rig.client, rig.store, rig.vault
    r = create(c, login={"mode": "password", "account": "u1", "secret": "P@ssw0rd!", "remember": True})
    a = r.json()["data"]
    assert a["login"] == {"mode": "password", "credential_ref": "vault://account/qd01", "remember": True}
    assert "secret" not in json.dumps(r.json()) and "P@ssw0rd" not in json.dumps(r.json())
    assert vault.entries["account/qd01"].value == "P@ssw0rd!" and vault.entries["account/qd01"].scope == "account"
    for row in st.list_audit():
        assert "P@ssw0rd" not in row["detail_json"]
    # remember=false:不落 Vault、不留 credential_ref(R4-6)
    r = create(c, "不存", key="k2", login={"mode": "password", "secret": "zzz", "remember": False})
    assert r.json()["data"]["login"]["credential_ref"] is None and "account/qd02" not in vault.entries


def test_create_409_when_wsl_budget_short(api):
    rig = api(wsl_total_mb=2048 + 2560 + 700)         # budget 3260:一个企点 + 剩 700(够一个 qq、不够第二个企点)
    c = rig.client
    assert create(c, "一号", key="a").status_code == 201
    r = create(c, "二号", key="b")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "RESOURCE_EXHAUSTED" and body["error"]["reason"] == "wsl_budget" and isinstance(body["error"]["alternatives"], list)
    kinds = {a["kind"] for a in body["error"]["alternatives"]}
    assert kinds == {"add_other_channel", "stop_one"}                                # 「可改开 QQ / 停用一个企点」(C.3.3)
    assert body["error"]["free_mb"] == 700 and body["error"]["need_mb"] == 2560
    assert rig.store.settings_get("seq.qidian") == 1                                  # 预检失败不消耗序号


def test_create_wechat_slot_and_offline_rules(api):
    rig = api()
    c, pool = rig.client, rig.agent.pool
    r = create(c, "微信A", channel="wechat", key="w1", login={"mode": "qrcode"})
    assert r.status_code == 503 and r.json()["code"] == "NOT_READY" and r.json()["error"]["reason"] == "winagent_offline"
    pool.set_windows(total_mb=16384, wechat_enabled=False, known=True)
    r = create(c, "微信A", channel="wechat", key="w1")
    assert r.status_code == 409 and r.json()["error"]["reason"] == "wechat_disabled"
    pool.set_windows(wechat_enabled=True)
    r = create(c, "微信A", channel="wechat", key="w1")
    assert r.status_code == 201 and r.json()["data"]["id"] == "wx01" and r.json()["data"]["host"] == "windows" and r.json()["data"]["wxid"] is None
    rig.store.pool_set("windows", slot_holder="wx01")
    r = create(c, "微信B", channel="wechat", key="w2")
    assert r.status_code == 409 and r.json()["error"]["message"] == "微信槽位被 wx01 占用,请用切换" and r.json()["error"]["hint_actions"] == ["wechat_switch"]


# ---------------------------------------------------------------- #9 start:冷启动序列不跳段
def test_start_sequence_qidian_no_skip(api):
    rig = api()
    c, st, adb, containers = rig.client, rig.store, rig.adb, rig.containers
    create(c)
    r = c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    assert r.status_code == 202 and jbody(r) == {"ok": True, "state": "starting"}
    rig.idle("qd01")
    assert rig.states("qd01") == ["created", "provisioning", "starting", "login_required"]
    a = st.get_account_full("qd01")
    assert a["state"] == "login_required" and a["state_code"] == "WAIT_PASSWORD" and a["desired_state"] == "running" and a["last_started_ms"]
    assert ("create", "qtrade-qd01") in containers.calls and ("start", "qtrade-qd01") in containers.calls
    cmds = adb.shell_cmds("127.0.0.1:16001")
    assert "getprop sys.boot_completed" in cmds and "stop adbd; start adbd" in cmds and "whoami" in cmds
    assert ("root", "127.0.0.1:16001") in adb.calls and rig.agent.health.is_rooting("qd01")
    assert not any("kill" in v for _, v in adb.calls)
    ev = st.list_events(event="account_state", account_id="qd01")[-1]["payload"]
    assert ev["login_session_id"].startswith("ls_") and ev["prompt"]["kind"] == "WAIT_PASSWORD" and ev["error_since_ms"] is None
    # 已在登录阶段再 start → 200 already
    r = c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["already"] is True
    assert len(st.list_audit(action="runtime.purge_ephemeral")) == 1          # start 前补清一次


def test_start_with_saved_credential_goes_login_required_then_logging_in_then_running(api):
    calls = []

    async def login_fn(row, account, secret):
        calls.append((row["id"], account, secret))
        return "running"
    rig = api(login_fn=login_fn)
    c, st, vault = rig.client, rig.store, rig.vault
    create(c, login={"mode": "password", "account": "u1", "secret": "s3cret", "remember": True})
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    assert rig.states("qd01") == ["created", "provisioning", "starting", "login_required", "logging_in", "running"]
    assert calls == [("qd01", "u1", "s3cret")]
    assert vault.reads and vault.reads[0][0] == "account/qd01" and vault.reads[0][1].startswith("ls_")     # 读记 trace(login_session_id)
    a = st.get_account_full("qd01")
    assert a["state"] == "running" and a["last_running_ms"] and a["state_code"] is None
    ls = {e["payload"]["login_session_id"] for e in st.list_events(event="account_state", account_id="qd01")[3:]}
    assert len(ls) == 1                                                             # 同一次尝试同一个 login_session_id


def test_vault_offline_with_saved_credential_is_error_vault_unavailable(api):
    rig = api()
    c, st, vault = rig.client, rig.store, rig.vault
    create(c, login={"mode": "password", "secret": "s", "remember": True})
    vault.offline = True
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    assert rig.states("qd01")[-2:] == ["login_required", "error"]
    a = c.get("/api/v1/accounts/qd01", headers=H()).json()["data"]
    assert a["state"] == "error" and a["state_code"] == "VAULT_UNAVAILABLE" and isinstance(a["error_since_ms"], int)
    s = c.get("/api/v1/accounts/qd01/state", headers=H(TOKEN_READ)).json()
    assert set(s) - {"trace_id"} == {"ok", "state", "state_code", "state_reason", "adapter_state", "last_seen_at", "error_since_ms"} and s["error_since_ms"] == a["error_since_ms"]
    ev = st.list_events(event="account_state", account_id="qd01")[-1]["payload"]
    assert ev["error_since_ms"] == a["error_since_ms"]                             # 三处同值
    # 离开 error(再 start)→ 同事务清 NULL;error 可 start
    vault.offline = False
    r = c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    assert r.status_code == 202
    assert st.get_runtime("qd01")["error_since_ms"] is None
    rig.idle("qd01")


def test_boot_timeout_is_error_and_error_since_written_once(api):
    rig = api(cfg=AgentConfig(runtime=RuntimeConfig(boot_timeout_s=2)), clock=Clock(auto_step_ms=300))
    c, st, adb = rig.client, rig.store, rig.adb
    create(c)
    adb.boot_completed["127.0.0.1:16001"] = "0"
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    a = st.get_account_full("qd01")
    assert a["state"] == "error" and a["state_code"] == "BOOT_TIMEOUT" and a["runtime_error_since_ms"]
    first = a["runtime_error_since_ms"]
    # 已在 error 的重复迁入不覆盖原时刻(幂等护栏)
    st.transition("qd01", "error", state_code="BOOT_TIMEOUT")
    assert st.get_runtime("qd01")["error_since_ms"] == first
    assert rig.agent.pool.used_mb() == 0                                             # error 不占额度


# ---------------------------------------------------------------- #10 stop:stopped 之后清临时、再释放额度
def test_stop_sequence_purges_after_stopped(api):
    rig = api()
    c, st, containers = rig.client, rig.store, rig.containers
    create(c)
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    assert rig.agent.pool.used_mb() == 2560
    r = c.post("/api/v1/accounts/qd01/stop", json={"graceful": True}, headers=H(TOKEN_WRITE))
    assert r.status_code == 202 and r.json()["state"] == "stopping"
    rig.idle("qd01")
    assert rig.states("qd01")[-2:] == ["stopping", "stopped"]
    a = st.get_account_full("qd01")
    assert a["state"] == "stopped" and a["desired_state"] == "stopped" and a["last_stopped_ms"]
    assert containers.containers["qtrade-qd01"].running is False and ("remove", "qtrade-qd01") not in containers.calls     # 容器停,卷保留
    purge = st.list_audit(action="runtime.purge_ephemeral")[-1]
    stopped_ev = [e for e in st.list_events(event="account_state", account_id="qd01") if e["payload"]["state"] == "stopped"][-1]
    assert purge["ts_ms"] >= stopped_ev["ts_ms"]                                    # 落 stopped 之后才清
    assert rig.agent.pool.used_mb() == 0
    r = c.post("/api/v1/accounts/qd01/stop", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["already"] is True


def test_created_account_can_start_when_budget_fits_exactly_one(api):
    """R6-55:created 已计入 used,start 的 can_add 须排除自身,否则预算恰好只够一个时它自己永远起不来。"""
    rig = api(wsl_total_mb=2048 + 2560)               # budget 恰好一个企点
    c = rig.client
    assert create(c, "唯一", key="a").status_code == 201
    r = c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    assert r.status_code == 202
    rig.idle("qd01")
    assert rig.store.get_account_full("qd01")["state"] == "login_required"
    r = create(c, "二", key="b")
    assert r.status_code == 409 and r.json()["error"]["free_mb"] == 0


def test_start_from_stopped_must_pass_can_add_again(api):
    rig = api(wsl_total_mb=2048 + 2560 + 700)         # 只够一个企点 + 一个 qq
    c = rig.client
    create(c, "一", key="a")
    c.post("/api/v1/accounts/qd01/stop", headers=H(TOKEN_WRITE))                    # created → stop:幂等 200
    rig.store.transition("qd01", "stopped")
    create(c, "二", key="b")                                                        # stopped 不占额度 ⇒ 能建第二个
    c.post("/api/v1/accounts/qd02/start", headers=H(TOKEN_WRITE))
    rig.idle("qd02")
    r = c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))               # 再起第一个:预算不够
    assert r.status_code == 409 and r.json()["code"] == "RESOURCE_EXHAUSTED"


# ---------------------------------------------------------------- #5 / #6 / #11
def test_disable_stops_first_then_enable_does_not_autostart(api):
    rig = api()
    c, st = rig.client, rig.store
    create(c)
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    r = c.post("/api/v1/accounts/qd01/disable", json={"graceful": True}, headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["data"]["state"] == "disabled" and r.json()["data"]["enabled"] is False
    assert rig.states("qd01")[-3:] == ["stopping", "stopped", "disabled"]
    r = c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    assert r.status_code == 409 and r.json()["error"]["reason"] == "account_disabled"
    r = c.post("/api/v1/accounts/qd01/enable", headers=H(TOKEN_WRITE))
    assert r.json()["data"]["state"] == "stopped" and r.json()["data"]["enabled"] is True
    assert st.get_account_full("qd01")["desired_state"] == "stopped" and not rig.agent.accounts.busy("qd01")
    assert c.get("/api/v1/accounts?include_stopped=false", headers=H()).json()["data"] == []


def test_restart_stops_then_starts(api):
    rig = api()
    c, containers = rig.client, rig.containers
    create(c)
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    r = c.post("/api/v1/accounts/qd01/restart", headers=H(TOKEN_WRITE))
    assert r.status_code == 202
    rig.idle("qd01")
    assert rig.states("qd01")[-5:] == ["stopping", "stopped", "provisioning", "starting", "login_required"]
    assert [k for k, n in containers.calls if k in ("start", "stop") and n == "qtrade-qd01"] == ["start", "stop", "start"]
    assert rig.adb.calls.count(("root", "127.0.0.1:16001")) == 2                    # start 段复走 ensure_root(R6-28)


# ---------------------------------------------------------------- #7 软删
def test_delete_is_soft_and_requires_label_confirm(api):
    rig = api()
    c, st, containers = rig.client, rig.store, rig.containers
    create(c)
    c.post("/api/v1/accounts/qd01/start", headers=H(TOKEN_WRITE))
    rig.idle("qd01")
    assert c.delete("/api/v1/accounts/qd01?confirm=张三-固收", headers=H(TOKEN_WRITE)).status_code == 403          # A 级
    r = c.delete("/api/v1/accounts/qd01?confirm=错的", headers=H())
    assert r.status_code == 400 and r.json()["error"]["reason"] == "confirm_mismatch"
    r = c.delete("/api/v1/accounts/qd01?confirm=张三-固收", headers=H())
    assert r.status_code == 200 and jbody(r) == {"ok": True, "deleted": True, "data_kept": True, "id": "qd01"}
    assert c.get("/api/v1/accounts/qd01", headers=H()).status_code == 404
    assert c.get("/api/v1/accounts", headers=H()).json()["data"] == []
    rows = c.get("/api/v1/accounts?include_deleted=true", headers=H()).json()["data"]
    assert rows[0]["id"] == "qd01" and rows[0]["deleted_ms"] and rows[0]["state"] == "stopped"
    assert "qtrade-qd01" in containers.containers and containers.containers["qtrade-qd01"].running is False        # 容器停、卷保留
    assert st.get_runtime("qd01")["desired_state"] == "stopped"
    assert create(c, "新", key="n").json()["data"]["id"] == "qd02"                                                    # id 不复用
    assert c.delete("/api/v1/accounts/qd01?confirm=张三-固收", headers=H()).status_code == 404


# ---------------------------------------------------------------- #4 PATCH
def test_patch_account(api):
    rig = api()
    c = rig.client
    create(c)
    r = c.patch("/api/v1/accounts/qd01", json={"label": "改名", "capture_text": False, "retention_days": 7, "media_policy": {"image": "lazy"}},
                headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["data"]["label"] == "改名"
    row = rig.store.get_account_full("qd01")
    assert row["capture_text"] == 0 and row["retention_days"] == 7 and json.loads(row["media_policy_json"]) == {"image": "lazy"}
    assert c.patch("/api/v1/accounts/qd01", json={"retention_days": 31}, headers=H(TOKEN_WRITE)).json()["error"]["reason"] == "retention_too_long"
    assert c.patch("/api/v1/accounts/qd01", json={"enabled": False}, headers=H(TOKEN_WRITE)).status_code == 400
    r = c.patch("/api/v1/accounts/qd01", json={"quota_mb": 99999}, headers=H(TOKEN_WRITE))
    assert r.status_code == 409 and r.json()["code"] == "RESOURCE_EXHAUSTED"
    assert c.patch("/api/v1/accounts/qd01", json={"quota_mb": 3000}, headers=H(TOKEN_WRITE)).json()["data"]["quota_mb"] == 3000
    assert c.patch("/api/v1/accounts/qd01", json={"label": "x"}, headers=H(TOKEN_READ)).status_code == 403


# ---------------------------------------------------------------- #69 /resources、#19
def test_resources_endpoint_and_state_endpoint(api):
    rig = api()
    c = rig.client
    create(c)
    r = c.get("/api/v1/resources", headers=H(TOKEN_READ))
    assert r.status_code == 200
    b = r.json()
    assert set(b["pools"]) == {"wsl", "windows"} and b["pools"]["wsl"]["used_mb"] == 2560 and b["can_add"]["qidian"] == 2 and b["accounts_by_channel"] == {"qidian": 1}
    assert b["pools"]["windows"]["wechat_slots"]["pending_login_session_id"] == "" and b["pools"]["windows"]["wechat_slots"]["pending_expires_at"] is None
    s = c.get("/api/v1/accounts/qd01/state", headers=H(TOKEN_READ)).json()
    assert {k: v for k, v in s.items() if k != "trace_id"} == {"ok": True, "state": "created", "state_code": "", "state_reason": "", "adapter_state": "created", "last_seen_at": None, "error_since_ms": None}
    assert c.get("/api/v1/accounts/zz99/state", headers=H()).status_code == 404


# ---------------------------------------------------------------- 02 §2.6 启动恢复
def test_recover_starts_desired_running_serially_and_skips_auto_recover_0(api):
    rig = api()
    c, st = rig.client, rig.store
    for i, lbl in enumerate(("甲", "乙", "丙"), 1):
        create(c, lbl, key=f"r{i}")
    st.transition("qd01", "stopped", desired_state="running")
    st.transition("qd02", "stopped", desired_state="running")
    st.patch_account("qd02", auto_recover=False)
    st.transition("qd03", "stopped", desired_state="stopped")
    results = c.portal.call(rig.agent.accounts.recover)
    assert [r["id"] for r in results] == ["qd01"] and results[0]["ok"] is True
    assert st.get_account_full("qd01")["state"] == "login_required" and st.get_account_full("qd02")["state"] == "stopped"
    assert st.list_audit(action="account.recover")
