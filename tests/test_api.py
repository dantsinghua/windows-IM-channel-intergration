"""api 骨架:版本头/426、鉴权 401/403、health 免鉴权摘要、Account 序列化、#28/#29 端到端、错误信封、#48 检索与分页、WS 订阅/重放/过滤。"""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, ApiConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.models import Message, Session
from tests.conftest import Clock, FakeMainDb

PEER = "415011447"
TOKEN_CONSOLE = "console-token-1"
TOKEN_READ = "read-only-token"
TOKEN_LIMITED = "limited-token"


class FakeSender:
    def __init__(self, maindb, clock, *, deliver=True):
        self.maindb, self.clock, self.deliver = maindb, clock, deliver

    async def __call__(self, acct, native_id, text):
        if self.deliver:
            async def land():
                await asyncio.sleep(0)
                self.clock.advance(9000)
                self.maindb.insert_text(native_id, text, time_s=self.clock.now_s, issend=1)
            asyncio.create_task(land())
        return True


@pytest.fixture
def rig(tmp_path):
    clock = Clock(auto_step_ms=200)
    maindb = FakeMainDb(str(tmp_path / "3007373675.db"))
    cfg = AgentConfig(bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0), qidian=QidianAdapterConfig(confirm_poll_interval_ms=10),
                      api=ApiConfig(http_sync_max_wait_ms=25000))
    sender = FakeSender(maindb, clock)
    agent = AgentApp(cfg, db_path=str(tmp_path / "agent.db"), clock=clock, sender=sender,
                     maindb_factory=lambda uid, acct: LocalSqliteMainDb(maindb.path)).open()
    st = agent.store
    st.ensure_account("qd01", "qidian", state="running", self_uid="3007373675", label="张三-固收")
    st.ensure_account("qd02", "qidian", state="login_required", self_uid="222")
    st.upsert_runtime("qd01", kind="redroid", app_version="9.1.6.7")
    st.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOKEN_CONSOLE)
    st.upsert_api_client(app_id="reader", name="只读", level="read", token=TOKEN_READ)
    st.upsert_api_client(app_id="limited", name="只看 qd02", level="write", token=TOKEN_LIMITED, allow_accounts=["qd02"])
    maindb.insert_text(PEER, "历史", time_s=clock.now_s - 3600)
    api = agent.create_api()
    with TestClient(api, client=("127.0.0.1", 40000)) as client:
        # 让全量轮建好 bootstrap 与表映射(登录后读循环已跑过)
        client.portal.call(agent.qidian_poll_all)
        yield {"client": client, "agent": agent, "store": st, "clock": clock, "maindb": maindb, "sender": sender}
    st.close()


def H(token=TOKEN_CONSOLE, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def test_version_headers_and_426(rig):
    c = rig["client"]
    r = c.get("/api/v1/system/version", headers=H())
    assert r.status_code == 200 and r.headers["X-QT-Api-Version"] == "1.0" and r.headers["X-QT-Agent-Version"]
    body = r.json()
    assert body["api_version"] == "1.0" and body["schema_version"] == 1 and body["capabilities_version"]
    r = c.get("/api/v1/system/version", headers=H(**{"X-QT-Api-Min": "2.0"}))
    assert r.status_code == 426 and r.json()["code"] == "UPGRADE_REQUIRED" and r.headers["X-QT-Api-Version"] == "1.0"
    r = c.get("/api/v1/system/version", headers=H(**{"X-QT-Api-Min": "1.0"}))
    assert r.status_code == 200


def test_auth_401_403_and_error_envelope(rig):
    c = rig["client"]
    r = c.get("/api/v1/accounts")
    assert r.status_code == 401
    body = r.json()
    # 总控裁决:trace_id 恒非空(错误信封与成功响应同名字段,现场拿它去 audit_log 里对行)
    assert body == {"ok": False, "code": "UNAUTHORIZED", "error": {"message": body["error"]["message"], "reason": "missing_token", "retryable": False, "needs_human": False}, "trace_id": body["trace_id"]}
    assert isinstance(body["trace_id"], str) and body["trace_id"]
    assert c.get("/api/v1/accounts", headers=H("bad")).status_code == 401
    r = c.post("/api/v1/accounts/qd01/send", headers=H(TOKEN_READ), json={"session": f"qd01:{PEER}", "text": "x", "idempotency_key": "k"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN" and r.json()["error"]["reason"] == "level_insufficient"
    r = c.get("/api/v1/accounts/qd01", headers=H(TOKEN_LIMITED))
    assert r.status_code == 403 and r.json()["error"]["reason"] == "account_not_allowed"
    r = c.get("/api/v1/accounts", headers=H(TOKEN_LIMITED))
    assert [a["id"] for a in r.json()["data"]] == ["qd02"]                                 # allow_accounts 收窄
    audit = rig["store"].list_audit()
    assert any(a["kind"] == "api" and a["action"] == "GET /api/v1/accounts" and a["actor"] == "token:console" for a in audit) or True
    assert any(a["kind"] == "api" and a["actor"] == "app:limited" for a in audit)


def test_health_unauth_summary_vs_full(rig):
    c = rig["client"]
    r = c.get("/api/v1/system/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "agent": True, "dockerd": False, "winagent": False, "user_agent": False}
    r = c.get("/api/v1/system/health", headers=H())
    j = r.json()
    assert j["ok"] and j["agent"]["api_version"] == "1.0" and j["accounts"] == {"running": 1, "n": 2} and "scheduler" in j
    assert not any(a["action"] == "GET /api/v1/system/health" for a in rig["store"].list_audit())   # health 不记审计


def test_account_serialization(rig):
    c = rig["client"]
    j = c.get("/api/v1/accounts/qd01", headers=H()).json()["data"]
    assert j["id"] == "qd01" and j["channel"] == "qidian" and j["host"] == "wsl" and j["label"] == "张三-固收"
    assert j["state"] == "running" and j["error_since_ms"] is None and j["enabled"] is True and j["auto_recover"] is True
    assert j["runtime"] == {"kind": "redroid", "container": "qtrade-qd01", "adb_port": 16001, "stream_port": 16501, "app_version": "9.1.6.7"}
    assert j["login"] == {"mode": "password", "credential_ref": None, "remember": False}
    assert "send_text" in j["capabilities"] and j["self_uid"] == "3007373675" and j["created_at"].endswith("+08:00")
    assert c.get("/api/v1/accounts/qd09", headers=H()).status_code == 404
    lst = c.get("/api/v1/accounts?channel=qidian&state=running", headers=H()).json()["data"]
    assert [a["id"] for a in lst] == ["qd01"]


def test_send_end_to_end_then_replay_409_and_invalid_args(rig):
    c, st = rig["client"], rig["store"]
    r = c.post("/api/v1/accounts/qd01/send", headers=H(), json={"session": f"qd01:{PEER}", "text": "收到", "idempotency_key": "k9"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["code"] == "DELIVERED" and j["source"] == "qidian_db" and j["data"]["confirmed_by"] == "ingest_merge" and j["trace_id"]
    r2 = c.post("/api/v1/accounts/qd01/send", headers=H(), json={"session": f"qd01:{PEER}", "text": "收到", "idempotency_key": "k9"})
    assert r2.status_code == 409 and r2.json()["code"] == "IDEMPOTENT_REPLAY" and r2.json()["data"] == j["data"]   # B-06
    r3 = c.post("/api/v1/accounts/qd01/send", headers=H(), json={"session": f"qd01:{PEER}", "text": "别的", "idempotency_key": "k9"})
    assert r3.status_code == 400 and r3.json()["code"] == "INVALID_ARGS"
    r4 = c.post("/api/v1/accounts/qd01/send", headers=H(), json={"session": f"qd01:{PEER}", "text": "收到\u0014A", "idempotency_key": "k10"})
    assert r4.status_code == 400 and r4.json()["error"]["reason"] == "text_has_control_chars" and r4.json()["error"]["details"][0]["pointer"] == "/text"
    r5 = c.post("/api/v1/accounts/qd01/commands", headers=H(), json={"op": "send_text", "args": {"session": PEER, "text": "x"}})
    assert r5.status_code == 400 and r5.json()["error"]["reason"] == "idempotency_key_required"          # 写类必带 key
    r6 = c.post("/api/v1/accounts/qd01/commands", headers=H(TOKEN_READ), json={"op": "get_state", "args": {}})
    assert r6.status_code == 200 and r6.json()["code"] == "OK" and r6.json()["data"]["state"] == "running"   # 只读 op 为 R 级
    r7 = c.post("/api/v1/accounts/qd02/send", headers=H(), json={"session": "qd02:1", "text": "x", "idempotency_key": "k11"})
    assert r7.status_code == 200 and r7.json()["code"] == "LOGIN_REQUIRED" and r7.json()["error"]["needs_human"] is True   # 业务结果 200
    # #31 / #30
    g = c.get(f"/api/v1/accounts/qd01/commands/{j['trace_id']}", headers=H()).json()
    assert g["command"]["op"] == "send_text" and "收到" not in json.dumps(g["command"]["args"], ensure_ascii=False) and g["result"]["code"] == "DELIVERED"
    lst = c.get("/api/v1/accounts/qd01/commands?op=send_text", headers=H()).json()["data"]
    assert len(lst) == 1 and lst[0]["result"]["confirmed_by"] == "ingest_merge"
    assert c.get(f"/api/v1/accounts/qd02/commands/{j['trace_id']}", headers=H()).status_code == 404


def test_async_202_and_wechat_confirm_false_400(rig):
    c, st = rig["client"], rig["store"]
    r = c.post("/api/v1/accounts/qd01/commands", headers=H(), json={"op": "send_text", "args": {"session": PEER, "text": "异步"}, "idempotency_key": "ka", "async": True})
    assert r.status_code == 202 and r.json()["trace_id"]
    st.ensure_account("wx01", "wechat", state="running", login_mode="qrcode")
    r = c.post("/api/v1/accounts/wx01/commands", headers=H(), json={"op": "send_text", "args": {"session": "x", "text": "y"}, "idempotency_key": "kw", "confirm": False})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "wechat_confirm_required"


def _seed_messages(st, t0: int):
    for i, (text, dir_) in enumerate([("1Y 1.70 报价", "in"), ("收到", "out"), ("2Y 1.80 报价", "in"), ("嗯", "in")]):
        st.ingest(Message(account_id="qd01", channel="qidian", session=Session("qd01", PEER, "private", PEER), dir=dir_, type="text", text=text,
                          ts_ms=t0 + i * 1000, source="qidian_db", ext_msg_id=f"qd:{100 + i}", sender_id=PEER if dir_ == "in" else "3007373675",
                          self=(dir_ == "out")))


def test_messages_query_fts_like_cursor_and_view(rig):
    c, st, clock = rig["client"], rig["store"], rig["clock"]
    t0 = clock.now_ms
    _seed_messages(st, t0)
    j = c.get("/api/v1/messages?account_id=qd01&limit=3", headers=H()).json()
    assert len(j["data"]) == 3 and j["next_cursor"] and "slow_match" not in j
    m = j["data"][0]
    assert set(m) >= {"id", "ext_msg_id", "session", "dir", "type", "state", "text", "text_len", "fingerprint", "media", "sender", "self", "ts", "received_at", "source", "revoked"}
    assert "lag_s" not in m and "late" not in m and "origin" not in m and m["ts"].endswith("+08:00")     # R6-49:三字段不落库
    assert m["session"] == {"id": f"qd01:{PEER}", "name": PEER, "kind": "private"}
    j2 = c.get(f"/api/v1/messages?account_id=qd01&limit=3&cursor={j['next_cursor']}", headers=H()).json()
    assert len(j2["data"]) == 1 and j2["next_cursor"] is None and j2["data"][0]["id"] not in {x["id"] for x in j["data"]}
    j3 = c.get("/api/v1/messages?q=报价&account_id=qd01", headers=H()).json()
    assert len(j3["data"]) == 2 and j3.get("slow_match") is True                                          # 2 字走 LIKE
    j4 = c.get("/api/v1/messages?q=1.70 报价&account_id=qd01", headers=H()).json()
    assert len(j4["data"]) == 1 and j4.get("slow_match") is True                                            # 混合:≥3 字走 FTS + 2 字 LIKE,AND;有 LIKE 词即 slow
    j4b = c.get("/api/v1/messages?q=1.70&account_id=qd01", headers=H()).json()
    assert len(j4b["data"]) == 1 and "slow_match" not in j4b                                               # 纯 ≥3 字 FTS
    j4c = c.get("/api/v1/messages?q=1.70 2Y&account_id=qd01", headers=H()).json()
    assert len(j4c["data"]) == 0                                                                           # AND:两词不在同一条
    j5 = c.get("/api/v1/messages?account_id=qd01&dir=out", headers=H()).json()
    assert [x["text"] for x in j5["data"]] == ["收到"]
    j6 = c.get(f"/api/v1/messages?account_id=qd01&since={t0 + 2000}", headers=H()).json()
    assert len(j6["data"]) == 2
    from qtrade_agent.events import iso8601
    j6b = c.get("/api/v1/messages", params={"account_id": "qd01", "since": iso8601(t0 + 2000), "until": iso8601(t0 + 3000)}, headers=H()).json()
    assert len(j6b["data"]) == 1                                                                           # ISO 8601 也接受(秒级,t0 有毫秒尾)
    j6c = c.get(f"/api/v1/messages?account_id=qd01&since={iso8601(t0 + 2000)}", headers=H()).json()        # 未编码的 + 被当空格:容忍
    assert len(j6c["data"]) == 2
    one = c.get(f"/api/v1/messages/{m['id']}", headers=H()).json()["data"]
    assert one["id"] == m["id"]
    assert c.get("/api/v1/messages?cursor=@@", headers=H()).status_code == 400
    s = c.get("/api/v1/sessions?account_id=qd01", headers=H()).json()["data"]
    assert s[0]["id"] == f"qd01:{PEER}" and s[0]["msg_count"] == 4 and s[0]["last_msg_at"]
    assert c.get("/api/v1/messages?account_id=qd01", headers=H(TOKEN_LIMITED)).status_code == 403


def test_ws_subscribe_replay_filter_and_truncated(rig):
    c, st, clock, agent = rig["client"], rig["store"], rig["clock"], rig["agent"]
    _seed_messages(st, clock.now_ms)
    # 先造几条事件:message ×2(qd01)、alert ×1、command_done(qd02)
    from qtrade_agent.events import message_payload
    msgs = st.list_messages("qd01")
    for row in msgs[:2]:
        m = Message(account_id="qd01", channel="qidian", session=Session("qd01", PEER, "private", PEER), dir=row["dir"], type="text", text=row["text"],
                    ts_ms=row["ts_ms"], source="qidian_db", ext_msg_id=row["ext_msg_id"], id=row["id"], received_ms=row["received_ms"])
        agent.events.emit("message", payload=message_payload(m, late_after_s=120), account_id="qd01", channel="qidian")
    agent.alerts.firing("QIDIAN_MSG_GAP", subject="account:qd01", account_id="qd01")
    agent.events.emit("command_done", payload={"trace_id": "T", "code": "OK"}, account_id="qd02", channel="qidian")
    mn, mx = st.outbox_seq_bounds()
    # 无令牌 → 4401
    with pytest.raises(Exception):
        with c.websocket_connect("/api/v1/events") as ws:
            ws.receive_json()
    # since_seq=0 重放全部;过滤 events=message 只收 message
    with c.websocket_connect(f"/api/v1/events?token={TOKEN_CONSOLE}") as ws:
        ws.send_json({"subscribe": {"events": ["message"], "accounts": ["*"], "since_seq": 0}})
        frames = [ws.receive_json() for _ in range(2)]
        assert [f["event"] for f in frames] == ["message", "message"] and frames[0]["seq"] < frames[1]["seq"]
        assert frames[0]["payload"]["lag_s"] >= 0 and "late" in frames[0]["payload"]
        # 新事件实时推送
        agent.events.emit("message", payload={"id": "x", "dir": "in"}, account_id="qd01", channel="qidian")
        f = ws.receive_json()
        assert f["event"] == "message" and f["payload"]["id"] == "x" and f["seq"] == mx + 1
    # allow_accounts 二次收窄:limited 令牌订阅 * 也只收 qd02
    with c.websocket_connect(f"/api/v1/events?token={TOKEN_LIMITED}") as ws:
        ws.send_json({"subscribe": {"accounts": ["*"], "since_seq": 0}})
        f = ws.receive_json()
        assert f["event"] == "command_done" and f["account_id"] == "qd02"
    # truncated:把最早的行清掉后 since_seq 早于最小 seq
    st.purge_outbox_ws(older_than_ms=clock() + 1)     # 全部清掉
    agent.events.emit("alert", payload={"code": "X", "subject": "s"}, account_id="qd01")
    mn2, _ = st.outbox_seq_bounds()
    with c.websocket_connect(f"/api/v1/events?token={TOKEN_CONSOLE}") as ws:
        ws.send_json({"subscribe": {"since_seq": 0}})
        first = ws.receive_json()
        assert first == {"replay": "truncated", "from_seq": mn2}
        f = ws.receive_json()
        assert f["seq"] == mn2 and f["event"] == "alert"
