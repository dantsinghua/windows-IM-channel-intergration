"""OSS admission, bounded state and dependency-ready recovery: synthetic only."""
import asyncio
from unittest.mock import AsyncMock
import pytest
from qtrade_agent.ossbox.inbox import XunjiaInbox
from qtrade_agent.ossbox.bind import Dedup
from qtrade_agent.store import Store, StoreCorrupt
from test_ossbox_xunjia_send import FakeBox, _command, _put_command, _open_ack, _XUNJIA, _WB

@pytest.mark.asyncio
async def test_oss_uses_shared_bus_gate_before_external_sender(rig3):
    rig3.store.ensure_account("qd01","qidian",state="running",self_uid="12345678")
    rig3.store.settings_set("gate.blocklist",["synthetic-blocked"])
    box=FakeBox(); send=AsyncMock(return_value=True)
    _put_command(box,_command(account_uid="12345678",text="synthetic-blocked",idempotency_key="audit-oss-gate"))
    inbox=XunjiaInbox(box,_XUNJIA,_WB,send,rig3.store,clock=rig3.clock)
    inbox.bus=rig3.agent.bus
    await inbox.poll()
    send.assert_not_awaited()
    assert _open_ack(box,"cmd_new01")["ok"] is False
    rows=rig3.store.list_commands("qd01")
    assert len(rows)==1 and rows[0]["code"]=="GATE_BLOCKED"

def test_dedup_recent_cache_has_a_hard_capacity():
    cache=Dedup()
    for i in range(10000):
        cache.seen_command(str(i)); cache.seen_event(str(i))
    assert len(cache._commands)<=4096 and len(cache._events)<=4096
    assert cache.seen_command("9999") is False

def test_oss_outcome_recent_cache_has_a_hard_capacity():
    inbox=XunjiaInbox(FakeBox(),b"x"*32,"synthetic",lambda *_:False,object())
    for i in range(10000): inbox._remember(str(i),ok=False,code="failed")
    assert len(inbox._outcome)<=4096
    assert "9999" in inbox._outcome

@pytest.mark.asyncio
async def test_docker_late_ready_recovers_once(rig3,monkeypatch):
    app=rig3.agent; recover=AsyncMock(return_value=[])
    monkeypatch.setattr(app.accounts,"recover",recover)
    monkeypatch.setattr(app.scheduler,"start",AsyncMock())
    monkeypatch.setattr(app.scheduler,"stop",AsyncMock())
    probe=AsyncMock(return_value=False); monkeypatch.setattr(app.runtime,"dockerd_ok",probe)
    await app.start(recover=True); await asyncio.sleep(0); await asyncio.sleep(0)
    assert recover.await_count==0
    probe.return_value=True
    await app.dockerd_probe(); await asyncio.sleep(0); await asyncio.sleep(0)
    assert recover.await_count==1
    await app.dockerd_probe(); await asyncio.sleep(0)
    assert recover.await_count==1
    await app.stop()

@pytest.mark.asyncio
async def test_explicit_recover_false_survives_dependency_ready(rig3,monkeypatch):
    app=rig3.agent; recover=AsyncMock(return_value=[])
    monkeypatch.setattr(app.accounts,"recover",recover)
    monkeypatch.setattr(app.scheduler,"start",AsyncMock())
    monkeypatch.setattr(app.scheduler,"stop",AsyncMock())
    monkeypatch.setattr(app.runtime,"dockerd_ok",AsyncMock(return_value=True))
    await app.start(recover=False); await app.dockerd_probe(); await asyncio.sleep(0)
    recover.assert_not_awaited()
    await app.stop()

def test_same_version_legacy_schema_is_not_silently_accepted(tmp_path):
    path=str(tmp_path/"synthetic-legacy.db")
    store=Store(path).open()
    store.con.execute("ALTER TABLE mail_outbox DROP COLUMN render_notes")
    store.close()
    with pytest.raises(StoreCorrupt): Store(path).open()
