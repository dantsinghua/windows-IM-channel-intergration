"""Synthetic audit regressions; no external services or real IM accounts."""
import asyncio
import hashlib
import hmac
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import AsyncMock
import httpx
import pytest
from qtrade_agent.hmac_inbound import canonical_string
from qtrade_agent.models import Command, Message, Session
from qtrade_agent.bus.bus import _Job, canonical_args_hash
from qtrade_agent.adapters.base import Account
from qtrade_agent.mail.service import MailService
from qtrade_agent.mail.config import MailConfig
from qtrade_agent.mail.mail_store import MailStore

def seed(rig, **kw):
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="12345678")
    rig.store.upsert_api_client(app_id="audit", name="audit", level="write", token="synthetic-audit-token", **kw)

def transport(rig, host="127.0.0.1"):
    return httpx.ASGITransport(app=rig.agent.create_api(), client=(host, 40111))

HEADERS={"Authorization": "Bearer synthetic-audit-token"}

@pytest.mark.asyncio
async def test_bearer_rejects_outside_configured_cidr(rig3):
    seed(rig3, ip_allow=["127.0.0.1/32"])
    async with httpx.AsyncClient(transport=transport(rig3,"192.0.2.20"),base_url="http://test") as c:
        r=await c.get("/api/v1/resources",headers=HEADERS)
        assert r.status_code==403
        assert r.json()["error"]["reason"]=="ip_not_allowed"

@pytest.mark.asyncio
async def test_bearer_accepts_inside_configured_cidr(rig3):
    seed(rig3, ip_allow=["127.0.0.1/32"])
    async with httpx.AsyncClient(transport=transport(rig3),base_url="http://test") as c:
        assert (await c.get("/api/v1/resources",headers=HEADERS)).status_code==200

@pytest.mark.asyncio
async def test_rotated_hmac_secret_is_not_bearer(rig3):
    secret="synthetic-old-hmac-key"
    rig3.store.api_client_create(app_id="hm",name="hm",auth_kind="hmac",level="write",token="synthetic-new-key")
    rig3.store.settings_set("api_clients.grace",{"hm":{"old_hash":hashlib.sha256(secret.encode()).hexdigest(),"until_ms":rig3.clock()+600000}})
    async with httpx.AsyncClient(transport=transport(rig3),base_url="http://test") as c:
        r=await c.get("/api/v1/resources",headers={"Authorization":"Bearer "+secret})
        assert r.status_code==401

@pytest.mark.asyncio
async def test_hmac_operation_scope_is_enforced_at_route(rig3):
    seed(rig3)
    secret="synthetic-hmac-key"
    await rig3.agent.vault.put("api/hm",secret,scope="api")
    rig3.store.api_client_create(app_id="hm",name="hm",auth_kind="hmac",level="write",token=secret,secret_ref="vault://api/hm",allow_ops=["get_state"])
    path="/api/v1/accounts/qd01/commands"
    raw=json.dumps({"op":"send_text","args":{"session":"123456","text":"synthetic"},"idempotency_key":"scope-test","async":False}).encode()
    ts=rig3.clock()//1000; nonce="audit-hmac-nonce-0123456789"
    sig=hmac.new(secret.encode(),canonical_string("POST",path,"",raw,ts,nonce).encode(),hashlib.sha256).hexdigest()
    headers={"X-QT-AppId":"hm","X-QT-Timestamp":str(ts),"X-QT-Nonce":nonce,"X-QT-Signature":"v1="+sig,"Content-Type":"application/json"}
    async with httpx.AsyncClient(transport=transport(rig3),base_url="http://test") as c:
        r=await c.post(path,content=raw,headers=headers)
        assert r.status_code==403
        assert r.json()["error"]["reason"]=="op_not_allowed"

@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_oversize_body_rejected_before_auth(rig3,declared):
    seen=[]
    async def chunks():
        for i in range(20):
            seen.append(i); yield b"x"*(512*1024)
    headers={"X-QT-AppId":"missing","X-QT-Timestamp":"1","X-QT-Nonce":"a"*24,"X-QT-Signature":"v1="+"0"*64}
    if declared: headers["Content-Length"]=str(10*1024*1024)
    async with httpx.AsyncClient(transport=transport(rig3),base_url="http://test") as c:
        r=await c.post("/api/v1/accounts/qd01/commands",content=chunks(),headers=headers)
        assert r.status_code==413
        assert len(seen)<20

@pytest.mark.asyncio
async def test_failed_idempotency_replay_preserves_failure(rig3):
    seed(rig3); s=rig3.store; args={"session":"123456","text":"synthetic"}
    s.insert_command(trace_id="audit-original",account_id="qd01",op="send_text",args_json="{}",idempotency_key="audit-key",confirm=True,timeout_ms=30000,transport="local",actor="audit",ip=None,now_ms=1)
    s.idem_claim(account_id="qd01",key="audit-key",op="send_text",args_hash=canonical_args_hash(args),trace_id="audit-original",now_ms=2,ttl_days=7)
    s.finish_command(trace_id="audit-original",ok=False,code="SEND_FAILED",data={},cost_ms=0,source="ui",error_message="synthetic failure",retryable=True,needs_human=False,confirmed_by=None,confirm_ms=None,now_ms=3)
    s.idem_finish(account_id="qd01",key="audit-key",status="DONE",result_code="SEND_FAILED",now_ms=4)
    r=await rig3.agent.bus.submit(Command("qd01","send_text",args,idempotency_key="audit-key"))
    assert r.code=="IDEMPOTENT_REPLAY" and r.ok is False
    assert r.error is not None and r.error.message=="synthetic failure"

def test_abandoned_smtp_send_requires_review_not_resend(rig3):
    ms=MailStore(rig3.store)
    oid=ms.outbox_enqueue(kind="message",to_addrs="synthetic@example.invalid",subject="audit",body_text="synthetic",rfc_message_id="<audit@example.invalid>",dedup_key="audit-mail",template_version="v1")
    ms.outbox_update(oid,status="SENDING")
    MailService(rig3.store,MailConfig())
    row=ms.outbox_get(oid)
    assert row["status"]=="DEAD"
    assert "unknown" in (row["last_error"] or "").lower()
    assert ms.outbox_due(now_ms=rig3.clock()+100000)==[]

def test_shared_connection_read_waits_for_transaction_rollback(rig3):
    seed(rig3); ready=threading.Event()
    def read():
        ready.set(); return rig3.store.get_account("qd01")["state"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with rig3.store._tx() as c:
                c.execute("UPDATE accounts SET state=? WHERE id=?",("error","qd01"))
                result=pool.submit(read); assert ready.wait(2)
                import time; time.sleep(0.03)
                assert not result.done(), "reader saw uncommitted shared-connection state"
                raise RuntimeError("rollback synthetic transaction")
        except RuntimeError: pass
        assert result.result(timeout=2)=="running"

@pytest.mark.asyncio
async def test_account_scope_is_applied_before_message_paging(rig3):
    seed(rig3,allow_accounts=["qd01"])
    rig3.store.ensure_account("qd02","qidian",state="running",self_uid="87654321")
    for i,aid in enumerate(("qd01","qd02","qd02")):
        rig3.store.ingest(Message(account_id=aid,channel="qidian",session=Session(aid,"123456","private"),dir="in",type="text",text="synthetic",ts_ms=10000+i,source="qidian_db",ext_msg_id="qd:audit"+str(i)))
    async with httpx.AsyncClient(transport=transport(rig3),base_url="http://test") as c:
        r=await c.get("/api/v1/messages?limit=2",headers=HEADERS)
        assert r.status_code==200
        assert len(r.json()["data"])==1
        assert r.json()["data"][0]["account_id"]=="qd01"

@pytest.mark.asyncio
async def test_invalid_async_command_is_not_falsely_accepted(rig3):
    seed(rig3)
    async with httpx.AsyncClient(transport=transport(rig3),base_url="http://test") as c:
        r=await c.post("/api/v1/accounts/qd01/commands",headers=HEADERS,json={"op":"send_text","args":{},"async":True,"idempotency_key":"invalid-audit"})
        assert r.status_code==400
        assert r.json()["code"]=="INVALID_ARGS"

@pytest.mark.asyncio
async def test_finished_queued_command_does_not_execute(rig3,monkeypatch):
    seed(rig3); bus=rig3.agent.bus; adapter=bus.adapters["qidian"]
    execute=AsyncMock(); monkeypatch.setattr(adapter,"execute",execute)
    done=asyncio.get_running_loop().create_future(); done.set_result(None)
    job=_Job(Command("qd01","get_state",{},trace_id="already-finished"),Account("qd01","qidian","running"),done)
    await bus._execute(job)
    execute.assert_not_awaited()

@pytest.mark.asyncio
async def test_confirm_queue_pressure_is_not_an_unhandled_exception(rig3,monkeypatch):
    seed(rig3); bus=rig3.agent.bus
    bus.cfg=replace(bus.cfg,bus=replace(bus.cfg.bus,confirm_timeout_qidian_ms=5),qidian=replace(bus.cfg.qidian,confirm_poll_interval_ms=1))
    ticks=iter(range(1,100)); bus.clock=lambda:next(ticks)
    q=asyncio.Queue(maxsize=1); q.put_nowait((None,[])); bus._queues["qd01"]=q
    done=asyncio.get_running_loop().create_future()
    job=_Job(Command("qd01","send_text",{"session":"123456","text":"synthetic"},trace_id="pressure"),Account("qd01","qidian","running"),done)
    monkeypatch.setattr(bus,"_finalize",lambda job,res,**kw:job.done.set_result(res))
    await bus._confirm(job,"123456",1,"qidian_db")
    assert done.done()

def test_qidian_database_read_is_bounded(tmp_path):
    from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
    path=tmp_path/"synthetic.db"
    c=sqlite3.connect(path)
    c.execute("CREATE TABLE messages(_id INTEGER,issend INTEGER,time INTEGER,msgtype INTEGER,uniseq INTEGER,senderuin BLOB,msgData BLOB)")
    c.executemany("INSERT INTO messages VALUES (?,0,1,-1000,?,x'00',x'00')",[(i,i) for i in range(1,1201)])
    c.commit(); c.close()
    rows=LocalSqliteMainDb(str(path)).rows_after("messages",0)
    assert 0<len(rows)<=500
    assert rows[-1].id<1200
