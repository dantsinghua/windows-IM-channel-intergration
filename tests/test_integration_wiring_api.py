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


def test_resources_calibrate_dry_run_and_level(rig):
    """#71 同步返回建议值;``apply`` 缺省 false ⇒ 只回建议、不写 ``resource_pools``。"""
    r = rig.client.post("/api/v1/resources/calibrate", headers=H(), json={})
    assert r.status_code == 200 and r.json()["ok"] is True and "evidence" in r.json()
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
def _drain(rig, done) -> None:
    """作业 task 跑在 TestClient 的 portal 事件循环里(不是本测试的循环),所以要在那个循环里让出。"""
    import asyncio
    for _ in range(200):
        if done():
            return
        rig.client.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("作业没在预期时间内收口")
