"""第五批接线:新端点的正例 + 负例(工作流 #38~#47、资源 #70/#71、系统 #77/#81/#102/#109/#107、HMAC 公网入站 §3.5)。

每个端点至少一条正例(拿到该拿的)与一条负例(级别不够 / 入参不合法 / 目标不存在)。
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json

import pytest

from qtrade_agent.hmac_inbound import canonical_string
from qtrade_agent.vault_client import _Entry
from tests.test_integration_wiring_common import TOKEN_ADMIN, TOKEN_READ, TOKEN_WRITE, H, close_rig, make_rig

YAML_OK = """
name: daily
steps:
  - id: wait
    op: sleep
    args:
      seconds: 0
"""
YAML_BAD = """
name: daily
steps:
  - id: loop
    op: send_text
    for_each: sessions
"""


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    yield r
    close_rig(r)


def _make_workflow(rig, name: str = "daily") -> str:
    r = rig.client.post("/api/v1/workflows", headers=H(), json={"name": name, "yaml": YAML_OK})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


# ---------------------------------------------------------------- #38~#42 定义 CRUD
def test_workflow_create_and_list_and_get(rig):
    wid = _make_workflow(rig)
    r = rig.client.get("/api/v1/workflows", headers=H(TOKEN_READ))
    assert r.status_code == 200 and [w["id"] for w in r.json()["data"]] == [wid]
    assert "yaml" not in r.json()["data"][0]                      # #38:列表不含 yaml 全文(G-09)
    r = rig.client.get(f"/api/v1/workflows/{wid}", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["data"]["yaml"].strip().startswith("name: daily")


def test_workflow_create_rejects_bad_yaml_with_line_numbers(rig):
    """#39:解析校验失败 ``400`` **带行号**;R-13 砍掉的 ``for_each`` 报错不静默忽略。"""
    r = rig.client.post("/api/v1/workflows", headers=H(), json={"name": "bad", "yaml": YAML_BAD})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "workflow_invalid"
    errs = r.json()["error"]["errors"]
    assert errs and all("line" in e and "message" in e for e in errs)


def test_workflow_create_needs_admin(rig):
    r = rig.client.post("/api/v1/workflows", headers=H(TOKEN_WRITE), json={"name": "x", "yaml": YAML_OK})
    assert r.status_code == 403


def test_workflow_get_unknown_is_404(rig):
    assert rig.client.get("/api/v1/workflows/nope", headers=H()).status_code == 404


def test_workflow_put_bumps_version(rig):
    wid = _make_workflow(rig)
    before = rig.client.get(f"/api/v1/workflows/{wid}", headers=H()).json()["data"]["version"]
    r = rig.client.put(f"/api/v1/workflows/{wid}", headers=H(), json={"yaml": YAML_OK + "\n"})
    assert r.status_code == 200 and r.json()["data"]["version"] == before + 1


def test_workflow_put_rejects_missing_yaml(rig):
    wid = _make_workflow(rig)
    r = rig.client.put(f"/api/v1/workflows/{wid}", headers=H(), json={})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_yaml"


def test_workflow_delete_ok_and_404(rig):
    wid = _make_workflow(rig)
    assert rig.client.delete(f"/api/v1/workflows/{wid}", headers=H()).status_code == 200
    assert rig.client.delete(f"/api/v1/workflows/{wid}", headers=H()).status_code == 404


# ---------------------------------------------------------------- #43~#46 运行
def test_workflow_run_and_status_and_runs(rig):
    wid = _make_workflow(rig)
    r = rig.client.post(f"/api/v1/workflows/{wid}/run", headers=H(TOKEN_WRITE), json={"args": {}})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    r = rig.client.get(f"/api/v1/workflows/runs/{run_id}", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["run"]["run_id"] == run_id and isinstance(r.json()["steps"], list)
    r = rig.client.get(f"/api/v1/workflows/{wid}/runs", headers=H(TOKEN_READ))
    assert r.status_code == 200 and [x["run_id"] for x in r.json()["data"]] == [run_id]
    assert [e["payload"]["status"] for e in rig.events_of("workflow")][0] == "started"


def test_workflow_run_unknown_workflow_is_404(rig):
    assert rig.client.post("/api/v1/workflows/nope/run", headers=H(), json={}).status_code == 404


def test_workflow_cancel_and_resume_unknown_run_is_404(rig):
    assert rig.client.post("/api/v1/workflows/runs/nope/cancel", headers=H()).status_code == 404
    assert rig.client.post("/api/v1/workflows/runs/nope/resume", headers=H()).status_code == 404


def test_workflow_validate_ok_and_errors(rig):
    r = rig.client.post("/api/v1/workflows/validate", headers=H(TOKEN_READ), json={"yaml": YAML_OK})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["errors"] == []
    r = rig.client.post("/api/v1/workflows/validate", headers=H(TOKEN_READ), json={"yaml": YAML_BAD})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["errors"]
    r = rig.client.post("/api/v1/workflows/validate", headers=H(TOKEN_READ), json={})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_yaml"


# ---------------------------------------------------------------- #70 / #71 资源
def test_resources_precheck_ok_and_bad_channel(rig):
    r = rig.client.post("/api/v1/resources/precheck", headers=H(TOKEN_READ), json={"channel": "qidian"})
    assert r.status_code == 200 and r.json()["can_add"] is True and r.json()["alternatives"] == []
    r = rig.client.post("/api/v1/resources/precheck", headers=H(TOKEN_READ), json={"channel": "telegram"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_channel"


def test_resources_calibrate_is_a_job(rig):
    """#71:**总控裁决按 00 §11.21 [JOB] 走 `202 {job_id}`**(00 优先于 02 §3.4.6 的同步写法);
    `apply` 缺省 false ⇒ 只算建议、不写 `resource_pools`。"""
    r = rig.client.post("/api/v1/resources/calibrate", headers=H(), json={})
    assert r.status_code == 202 and r.json()["job_id"]
    job_id = r.json()["job_id"]
    _drain(rig, lambda: rig.store.job_get(job_id)["state"] != "running")
    got = rig.client.get(f"/api/v1/jobs/{job_id}", headers=H(TOKEN_READ)).json()["data"]
    assert got["state"] == "succeeded" and got["result"]["scope"] == "global" and "evidence" in got["result"]
    assert rig.store.pool_get("wsl")["source"] == "default"                 # apply=false:一个字都没写回
    assert rig.client.post("/api/v1/resources/calibrate", headers=H(TOKEN_WRITE), json={}).status_code == 403


# ---------------------------------------------------------------- #77 / #81 / #102 / #109 / #107
def test_system_metrics_has_flat_last_cleanup_keys(rig):
    """R6-30 逐字:``disk_watermark.last_cleanup_at`` / ``last_cleanup_freed_mb`` 是**扁平两键**,不嵌套子对象。"""
    r = rig.client.get("/api/v1/system/metrics", headers=H(TOKEN_READ))
    assert r.status_code == 200
    dw = r.json()["disk_watermark"]
    assert "last_cleanup_at" in dw and "last_cleanup_freed_mb" in dw and "last_cleanup" not in dw
    assert rig.client.get("/api/v1/system/metrics").status_code == 401


def test_system_backup_writes_a_file(rig, tmp_path):
    r = rig.client.post("/api/v1/system/backup", headers=H())
    assert r.status_code == 200 and r.json()["size"] > 0
    assert rig.client.post("/api/v1/system/backup", headers=H(TOKEN_WRITE)).status_code == 403


def test_public_endpoint_is_null_until_probed(rig):
    """``[api] public_ip_check_interval_s`` 默认 0=关 ⇒ 没探过时三个值为 null,而不是编一个。"""
    r = rig.client.get("/api/v1/system/public-endpoint", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["public_ip"] is None and r.json()["checked_at"] is None
    assert r.json()["probe"]["unreachable_rounds"] == 0


def test_system_cleanup_run_is_a_job_with_60s_dedup(rig):
    r = rig.client.post("/api/v1/system/cleanup/run", headers=H(TOKEN_WRITE))
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    _drain(rig, lambda: rig.store.job_get(job_id)["state"] != "running")
    got = rig.client.get(f"/api/v1/jobs/{job_id}", headers=H(TOKEN_READ))
    assert got.status_code == 200 and got.json()["data"]["state"] == "succeeded"
    assert got.json()["data"]["result"]["freed_mb"] >= 0
    assert [e["payload"]["state"] for e in rig.events_of("job")] == ["succeeded"]

    again = rig.client.post("/api/v1/system/cleanup/run", headers=H(TOKEN_WRITE))
    assert again.status_code == 409 and again.json()["error"]["reason"] == "cleanup_too_frequent"


def test_jobs_unknown_is_404(rig):
    assert rig.client.get("/api/v1/jobs/nope", headers=H(TOKEN_READ)).status_code == 404


# ---------------------------------------------------------------- §3.5 公网入站 HMAC
def _hmac_client(rig, *, app_id: str = "partner", level: str = "write", secret: str = "s3cr3t",
                 ip_allow: list[str] | None = None) -> str:
    rig.store.upsert_api_client(app_id=app_id, name=app_id, level=level, token=f"tok-{app_id}")
    rig.store.con.execute("UPDATE api_clients SET auth_kind='hmac', secret_ref=?, ip_allow_json=? WHERE app_id=?",
                          (f"vault://api/{app_id}", json.dumps(ip_allow or []), app_id))
    rig.agent.vault.entries[f"api/{app_id}"] = _Entry(secret, "api")
    return secret


def _signed(rig, method: str, path: str, body: bytes, *, app_id: str = "partner", secret: str = "s3cr3t",
            ts: int | None = None, nonce: str = "n" * 24) -> dict[str, str]:
    ts = ts if ts is not None else rig.clock() // 1000
    canonical = canonical_string(method, path, "", body, ts, nonce)
    sig = _hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"X-QT-AppId": app_id, "X-QT-Timestamp": str(ts), "X-QT-Nonce": nonce, "X-QT-Signature": f"v1={sig}"}


def test_hmac_inbound_accepts_signed_command_and_defaults_to_async(rig):
    """§3.5 末行:公网入站默认 ``async:true`` ⇒ ``202 {trace_id}``;``Date`` 头带服务端时间让对方校时。"""
    _hmac_client(rig)
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    body = json.dumps({"op": "get_state", "args": {}}).encode()
    path = "/api/v1/accounts/qd01/commands"
    r = rig.client.post(path, content=body, headers={**_signed(rig, "POST", path, body), "Content-Type": "application/json"})
    assert r.status_code == 202 and r.json()["accepted"] is True and r.json()["trace_id"]
    assert r.headers.get("Date")


def test_hmac_inbound_rejects_bad_signature(rig):
    _hmac_client(rig)
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    body = json.dumps({"op": "get_state", "args": {}}).encode()
    path = "/api/v1/accounts/qd01/commands"
    h = _signed(rig, "POST", path, body)
    h["X-QT-Signature"] = "v1=" + "0" * 64
    r = rig.client.post(path, content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"


def test_hmac_inbound_rejects_timestamp_skew_with_date_header(rig):
    """时钟容差超限 ⇒ 401 且 ``error.message`` 逐字 ``timestamp skew``;``Date`` 头必须在,否则对方没法自校。"""
    _hmac_client(rig)
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    body = b"{}"
    path = "/api/v1/accounts/qd01/commands"
    h = _signed(rig, "POST", path, body, ts=rig.clock() // 1000 - 3600)
    r = rig.client.post(path, content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 401 and r.json()["error"]["message"] == "timestamp skew"
    assert r.headers.get("Date")


def test_hmac_inbound_rejects_nonce_replay(rig):
    _hmac_client(rig)
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    body = json.dumps({"op": "get_state", "args": {}}).encode()
    path = "/api/v1/accounts/qd01/commands"
    h = _signed(rig, "POST", path, body, nonce="replay-nonce-0123456789")
    assert rig.client.post(path, content=body, headers={**h, "Content-Type": "application/json"}).status_code == 202
    r = rig.client.post(path, content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 401 and r.json()["error"]["message"] == "nonce replay"


def test_hmac_inbound_rejects_ip_outside_allowlist(rig):
    _hmac_client(rig, ip_allow=["10.0.0.0/8"])
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    body = b"{}"
    path = "/api/v1/accounts/qd01/commands"
    r = rig.client.post(path, content=body, headers={**_signed(rig, "POST", path, body), "Content-Type": "application/json"})
    assert r.status_code == 403 and r.json()["error"]["reason"] == "ip_not_allowed"


def test_bearer_path_still_synchronous(rig):
    """Bearer 调用**不**被 HMAC 的默认异步影响:没写 ``async`` 就同步等结果。"""
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    r = rig.client.post("/api/v1/accounts/qd01/commands", headers=H(TOKEN_ADMIN), json={"op": "get_state", "args": {}})
    assert r.status_code == 200 and r.json()["code"] == "OK"


# ---------------------------------------------------------------- 小工具
def _spawn(rig, job_id, body):
    rig.agent.spawn_job(job_id, "diagnostics", body)


def _drain(rig, done) -> None:
    """作业 task 跑在 TestClient 的 portal 事件循环里(不是本测试的循环),所以要在那个循环里让出。"""
    import asyncio
    for _ in range(200):
        if done():
            return
        rig.client.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("作业没在预期时间内收口")


# ---------------------------------------------------------------- console 交接:两条端点冲突 + 三个数据缺口
def test_health_checks_has_per_account_subkey(rig):
    """缺口 1:#72 的全局 `checks` 一个键不动,另补 `checks.accounts:{<id>:{H04…H08}}`(01 §2.7.3.4 的五个状态点)。"""
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    r = rig.client.get("/api/v1/system/health", headers=H(TOKEN_READ))
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert {"H02", "H03", "H13", "H24"} <= set(checks)                 # 02 原有的全局键都还在
    assert set(checks["accounts"]["qd01"]) == {"H04", "H05", "H06", "H07", "H08"}
    assert checks["accounts"]["qd01"]["H07"] == "unknown"              # 没有前台画面流(执行体未装配):不检查,不假装 ok


def test_health_per_account_reports_firing(rig):
    from qtrade_agent.alerts import H04_CONTAINER_EXITED
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    rig.agent.alerts.firing(H04_CONTAINER_EXITED, subject="account:qd01", account_id="qd01")
    checks = rig.client.get("/api/v1/system/health", headers=H(TOKEN_READ)).json()["checks"]
    assert checks["accounts"]["qd01"]["H04"] == "firing"


def test_health_checks_and_alerts_are_same_source(rig):
    """D-04:同一次 `#72` 响应里 `checks.H02='firing'` 而 `alerts=[]` 是自相矛盾 ——
    02 §3.7 把 `H02_WINAGENT_API_DOWN` / `H03_DOCKERD_DOWN` 登记成告警码(crit,subject=host/wsl,事件族 alert),
    探活判离线时必须同时进 `alerts.active`;反过来 dockerd 正常时不得凭空多一条 H03。"""
    from qtrade_agent.alerts import H02_WINAGENT_API_DOWN, H03_DOCKERD_DOWN
    rig.wechat._base.offline = True                                    # ping 走委托对象,offline 设在它身上
    for _ in range(3):                                                 # R-09 去抖:3 次才判离线
        rig.client.portal.call(rig.agent.winagent_probe)
    rig.client.portal.call(rig.agent.dockerd_probe)
    body = rig.client.get("/api/v1/system/health", headers=H(TOKEN_READ)).json()
    codes = {a["code"]: a for a in body["alerts"]}
    assert body["checks"]["H02"] == "firing" and codes[H02_WINAGENT_API_DOWN]["subject"] == "host"
    assert codes[H02_WINAGENT_API_DOWN]["severity"] == "crit"
    assert body["checks"]["H03"] == "ok" and H03_DOCKERD_DOWN not in codes


def test_metrics_exposes_per_process_cpu_pct(rig):
    """缺口 2:02 的三个内存键保留,另补 `ours.procs_detail[{name,rss_mb,cpu_pct}]`;monitor 没采 ⇒ 值 null,不编造。"""
    ours = rig.client.get("/api/v1/system/metrics", headers=H(TOKEN_READ)).json()["ours"]
    assert set(ours["procs"]) == {"agent_mb", "winagent_mb", "console_mb"}
    assert [p["name"] for p in ours["procs_detail"]] == ["agent", "winagent", "console"]
    assert all(p["cpu_pct"] is None and p["rss_mb"] is None for p in ours["procs_detail"])


def test_settings_mail_group_has_four_scopes(rig):
    """缺口 3:#88 的 `mail` 组 = 总开关 + 四块同构 scopes(全局默认 + 三通道);密码只写不读。"""
    r = rig.client.get("/api/v1/settings/mail", headers=H())
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data["scopes"]) == {"default", "qidian", "qq", "wechat"}
    assert data["enabled"] is False and all(not s["override"] for s in data["scopes"].values())
    w = rig.client.put("/api/v1/settings/mail", headers=H(),
                       json={"scopes": {"qidian": {"override": True, "inbound": {"host": "imap.example.com", "secret": "p@ss"},
                                                   "outbound": {}}}})
    assert w.status_code == 200 and "qidian" in w.json()["written"]
    got = rig.client.get("/api/v1/settings/mail", headers=H()).json()["data"]["scopes"]["qidian"]
    assert got["override"] is True and got["inbound"]["host"] == "imap.example.com"
    assert "secret" not in got["inbound"]                              # #88:只回引用不回值


def test_settings_mail_rejects_unknown_scope_and_read_level(rig):
    assert rig.client.get("/api/v1/settings/mail", headers=H(TOKEN_READ)).status_code == 403
    r = rig.client.put("/api/v1/settings/mail", headers=H(), json={"scopes": {"telegram": {}}})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_scopes"


def _observed_row(rig, **kw):
    row = {"id": kw.get("id", 1), "account_id": kw.get("account_id", "qd01"), "channel": kw.get("channel", "qidian"),
           "remote_host": kw.get("remote_host", "im.qq.example"), "remote_ip": kw.get("remote_ip", "203.0.113.7"),
           "remote_port": kw.get("remote_port", 443), "proto": "tcp", "side": "container",
           "first_seen_ms": rig.clock(), "last_seen_ms": rig.clock(), "hits": 7, "adopted_ms": None}
    rig.wechat._base.observed.append(row)
    return row


def test_probe_adopt_takes_observed_ids_not_targets(rig):
    """冲突 A:#76b 收的是**采样行 id**(02 为准);传 01 的 `{targets}` 回 400 并指明改用 `observed_ids`。"""
    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"targets": ["qidian_a:443"]})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "use_observed_ids"
    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": ["1"]})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_observed_ids"
    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": [1, 1]})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "duplicate_ids"


def test_probe_adopt_goes_through_when_winagent_has_the_endpoint(rig):
    """#76b 的正常路径:转发 WinAgent 的 `PUT /wa/v1/probes/adopt`,写 `adopted_ms` + `probe.targets`,回 `{adopted, targets}`。"""
    row = _observed_row(rig)
    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": [row["id"]]})
    assert r.status_code == 200 and r.json()["adopted"] == [row["id"]]
    # 🔴 裁决 A-15/A-16:`targets` 元素 = `"host:port"`(**无 `channel_` 前缀**),通道维度在 `hosts_by_channel`
    assert r.json()["targets"] == ["im.qq.example:443"]
    assert r.json()["hosts_by_channel"]["qidian_hosts"] == ["im.qq.example:443"]
    assert r.json()["hosts_by_channel"]["qq_hosts"] == []
    assert rig.wechat._base.observed[0]["adopted_ms"] is not None
    assert [a["action"] for a in rig.store.list_audit("settings.update")] == ["settings.update"]


def test_probes_observed_rows_carry_the_stable_id(rig):
    """采样行的稳定 id = 02 §3.2 DDL 的 `id` 列(控制台不得用行下标顶,rulings (ch))。"""
    _observed_row(rig, id=42)
    r = rig.client.get("/api/v1/system/probes", headers=H(TOKEN_READ), params={"kind": "observed"})
    assert r.status_code == 200 and r.json()["kind"] == "observed"
    assert [x["id"] for x in r.json()["data"]] == [42]


def test_probes_result_forwards_to_winagent(rig):
    rig.wechat._base.probe_results.append({"target": "qidian_im:443", "result": "OK", "latency_ms": 12})
    r = rig.client.get("/api/v1/system/probes", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["kind"] == "result" and r.json()["data"][0]["result"] == "OK"
    assert rig.client.get("/api/v1/system/probes", headers=H(TOKEN_READ),
                          params={"kind": "bogus"}).status_code == 400


def test_probes_report_the_upstream_gap_until_winagent_catches_up(rig):
    """WinAgent 还没补这两个端点(回 404)时:**诚实报上游缺口**,两个 reason 分得开 —— 不假装有数据、也不混成「上游挂了」。"""
    rig.wechat._base.observed_supported = False
    r = rig.client.get("/api/v1/system/probes", headers=H(TOKEN_READ), params={"kind": "observed"})
    assert r.status_code == 503 and r.json()["error"]["reason"] == "wa_observed_endpoint_missing"
    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": [1]})
    assert r.status_code == 503 and r.json()["error"]["reason"] == "wa_adopt_endpoint_missing"
    rig.wechat._base.offline = True                              # 上游挂了是另一个 reason(非微信路径走 _base)
    r = rig.client.get("/api/v1/system/probes", headers=H(TOKEN_READ), params={"kind": "observed"})
    assert r.status_code == 503 and r.json()["error"]["reason"] == "winagent_offline"


def test_selftest_latest_and_by_run_id(rig):
    """冲突 B:#79 按 run_id 保留,另补「最近一轮」兄弟端点;没跑过回 data:null(不是 404)。"""
    r = rig.client.get("/api/v1/system/selftest", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["data"] is None
    assert rig.client.get("/api/v1/system/selftest/nope", headers=H(TOKEN_READ)).status_code == 404
    rig.store.settings_set("system.selftest.r1", {"redroid_boot_ms": 42000, "napcat_ok": True}, actor="test")
    rig.store.settings_set("system.selftest.last", "r1", actor="test")
    r = rig.client.get("/api/v1/system/selftest", headers=H(TOKEN_READ))
    assert r.json()["run_id"] == "r1" and r.json()["data"]["redroid_boot_ms"] == 42000
    assert rig.client.get("/api/v1/system/selftest/r1", headers=H(TOKEN_READ)).json()["data"]["napcat_ok"] is True


def test_danger_ten_are_in_the_capability_catalog(rig):
    """🔴 02 §3.10 的 danger 十项必须在目录里:`expand_allow_ops(["*"])` 与 HMAC 的 `op_allowed` 都靠它把高危项排除在星号外。"""
    from qtrade_agent.hmac_inbound import expand_allow_ops, load_danger_ops
    danger = load_danger_ops()
    assert {"account_switch", "account_stop", "account_delete", "account_purge", "messages_purge", "settings_write",
            "vault_write", "workflow_run", "mail_cleanup_run", "system_wsl_restart"} == danger
    all_ops = {c["op"] for c in rig.client.get("/api/v1/capabilities", headers=H(TOKEN_READ)).json()["data"]}
    assert not (expand_allow_ops(["*"], all_ops, danger) & danger)      # 星号一个高危项都不放行
    assert "system_cleanup_run" in expand_allow_ops(["*"], all_ops, danger)   # R6-24:它 danger=false,星号放行


def test_system_ops_are_not_channel_capabilities(rig):
    """(co):系统/账号管理类 op 的 `channels` 三格恒 `not_applicable`,否则 R6-57 ⑧「静态集 = 目录 × supported」会逼适配器声明它们。"""
    caps = {c["op"]: c for c in rig.client.get("/api/v1/capabilities", headers=H(TOKEN_READ)).json()["data"]}
    for op in ("account_stop", "settings_write", "workflow_run", "system_cleanup_run"):
        assert set(caps[op]["channels"].values()) == {"not_applicable"}, op
    qq_only = rig.client.get("/api/v1/capabilities", headers=H(TOKEN_READ), params={"channel": "qq"}).json()["data"]
    assert {c["op"] for c in qq_only} == set(rig.agent.adapters["qq"].capabilities)


# ---------------------------------------------------------------- HMAC 适用全端点 + #108 + #25
def test_hmac_works_on_read_endpoints_too(rig):
    """§3.4 通用行:HMAC 是与 Bearer **并列的鉴权方式,适用于 `/api/v1` 全部端点**,不只写指令那两个。

    签验提到 middleware 做,级别判定仍留在端点 ⇒ read 级 app 打 admin 端点照样 403。
    """
    _hmac_client(rig, app_id="reader-hmac", level="read")
    path = "/api/v1/accounts"
    h = _signed(rig, "GET", path, b"", app_id="reader-hmac")
    r = rig.client.get(path, headers=h)
    assert r.status_code == 200 and r.headers.get("Date")
    path2 = "/api/v1/workflows"
    h2 = _signed(rig, "GET", path2, b"", app_id="reader-hmac", nonce="n2" * 12)
    assert rig.client.get(path2, headers=h2).status_code == 200            # 读类端点也走 HMAC
    h3 = _signed(rig, "POST", "/api/v1/system/backup", b"", app_id="reader-hmac", nonce="n3" * 12)
    assert rig.client.post("/api/v1/system/backup", headers=h3).status_code == 403   # 级别不够仍 403


def test_job_cancel_endpoint(rig):
    """#108:取消 `queued/running`;终态 → 409 `NOT_CANCELLABLE`;不存在 → 404。"""
    job_id = rig.store.job_create(kind="messages_export", actor="token:console")
    r = rig.client.post(f"/api/v1/jobs/{job_id}/cancel", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert rig.store.job_get(job_id)["state"] == "cancelled"
    assert [e["payload"]["state"] for e in rig.events_of("job")] == ["cancelled"]
    again = rig.client.post(f"/api/v1/jobs/{job_id}/cancel", headers=H(TOKEN_WRITE))
    assert again.status_code == 409 and again.json()["code"] == "NOT_CANCELLABLE"
    assert rig.client.post("/api/v1/jobs/nope/cancel", headers=H(TOKEN_WRITE)).status_code == 404


def test_job_cancel_really_cancels_the_running_task(rig):
    """#108 不能只改库里的字:在跑的 task 必须**真被 cancel**,否则「已取消」的作业还在写库。"""
    import asyncio
    started, finished = [], []

    async def body():
        started.append(1)
        await asyncio.sleep(30)
        finished.append(1)
        return {}

    job_id = rig.store.job_create(kind="diagnostics", actor="token:console")
    rig.client.portal.call(lambda: _spawn(rig, job_id, body))
    _drain(rig, lambda: bool(started))
    r = rig.client.post(f"/api/v1/jobs/{job_id}/cancel", headers=H(TOKEN_WRITE))
    assert r.status_code == 200
    assert rig.store.job_get(job_id)["state"] == "cancelled" and finished == []
    assert [e["payload"]["state"] for e in rig.events_of("job")] == ["cancelled"]


def test_single_account_calibrate(rig):
    """#25:单账号自校准 → **`202 {job_id}`**(与 #71 同一 job 契约,总控裁决);
    结果并进 `resource_pools.calibration_json.per_account`,**不改 `quota_json`**。"""
    import json as _json
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    r = rig.client.post("/api/v1/accounts/qd01/calibrate", headers=H(), json={})
    assert r.status_code == 202 and r.json()["job_id"]
    job_id = r.json()["job_id"]
    _drain(rig, lambda: rig.store.job_get(job_id)["state"] != "running")
    got = rig.client.get(f"/api/v1/jobs/{job_id}", headers=H(TOKEN_READ)).json()["data"]
    assert got["state"] == "succeeded" and got["result"]["channel"] == "qidian"
    blob = _json.loads(rig.store.pool_get("wsl")["calibration_json"])
    assert blob["per_account"]["qd01"]["run_id"] == job_id
    assert rig.store.pool_get("wsl")["quota"]["qidian"] == 2560              # 单账号校准**不改整池配额**
    assert rig.client.post("/api/v1/accounts/qd01/calibrate", headers=H(TOKEN_WRITE), json={}).status_code == 403
    assert rig.client.post("/api/v1/accounts/nope/calibrate", headers=H(), json={}).status_code == 404


# ══════════════════════════════════════════════════════════ C-1 实测采样闭环(04 §3.4 + 02 #76b,裁决 A-14~A-17)
async def test_sample_rows_are_aggregated_by_agent_and_written_once(rig):
    """04 §3.4:``POST /probe {mode:'sample'}`` **只回不落库**,由 Agent 聚合后经 ``PUT /probes {kind:'observed'}``
    写**一次** —— 各写各的会让同一条连接的 ``hits`` 被算两遍。回写方补 ``side``/``channel``/``account_id``(A-17)。"""
    wa = rig.wechat._base
    wa.sample_rows = [{"pid_name": "WeChat.exe", "ip": "203.205.254.1", "port": 443, "proto": "tcp",
                       "samples": 3, "hostname": "long.weixin.qq.com", "resolved_by": "dns"},
                      {"pid_name": "svchost.exe", "ip": "20.190.1.1", "port": 443, "proto": "tcp", "samples": 1}]
    rig.store.ensure_account("wx01", "wechat", state="running")
    out = await rig.agent.sample_observed()
    assert out["written"] == 2 and [r["side"] for r in out["rows"]] == ["windows", "windows"]
    assert out["rows"][0]["channel"] == "wechat" and out["rows"][0]["account_id"] == "wx01"
    assert "channel" not in out["rows"][1] or out["rows"][1].get("channel") is None   # 认不出的留 NULL,不猜
    assert len(wa.observed) == 2 and wa.observed[0]["hits"] == 3
    await rig.agent.sample_observed()                       # 同一条连接再采一轮
    assert len(wa.observed) == 2 and wa.observed[0]["hits"] == 6      # 唯一索引上累加,不新增行


async def test_adopt_replaces_the_whole_table_and_empty_clears_it(rig):
    """🔴 裁决 A-14(04 §2.8.4「**替换整表不追加**,让用户能删旧项」):``observed_ids`` 是采纳后的**全集**,
    掉出集合的已采纳行取消采纳;``[]`` = 清空正式目标表(02 #76b 现写的「非空整数数组」需按此改)。"""
    a = _observed_row(rig, id=1, remote_host="a.example", account_id="qd01")
    b = _observed_row(rig, id=2, remote_host="b.example", account_id="qd02")
    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": [1, 2]})
    assert sorted(r.json()["adopted"]) == [1, 2] and len(r.json()["targets"]) == 2

    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": [2]})
    assert r.json()["adopted"] == [2] and r.json()["targets"] == ["b.example:443"]
    assert a["adopted_ms"] is None and b["adopted_ms"] is not None    # 掉出集合的取消采纳

    r = rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": []})
    assert r.status_code == 200 and r.json()["adopted"] == [] and r.json()["targets"] == []
    assert b["adopted_ms"] is None


def test_observed_rows_carry_in_config_and_the_official_targets(rig):
    """04 §2.8.4 的面板要靠 ``in_config`` 默认只勾新增项 ⇒ ``#76 ?kind=observed`` 必须把
    ``targets``(正式目标)与行上的 ``in_config`` 一起带回,不能只回行。"""
    _observed_row(rig, id=1, remote_host="a.example")
    _observed_row(rig, id=2, remote_host="b.example")
    rig.client.put("/api/v1/settings/probe", headers=H(), json={"observed_ids": [1]})
    r = rig.client.get("/api/v1/system/probes?kind=observed", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["targets"] == ["a.example:443"]
    assert [row["in_config"] for row in r.json()["data"]] == [True, False]
