"""OSS admission, bounded state and dependency-ready recovery: synthetic only."""
import asyncio
from unittest.mock import AsyncMock
import pytest
from qtrade_agent.ossbox.bind import Dedup
from qtrade_agent.store import Store, StoreCorrupt

def test_dedup_recent_cache_has_a_hard_capacity():
    cache=Dedup()
    for i in range(10000):
        cache.seen_command(str(i)); cache.seen_event(str(i))
    assert len(cache._commands)<=4096 and len(cache._events)<=4096
    assert cache.seen_command("9999") is False

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
