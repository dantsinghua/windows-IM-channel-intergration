"""Media scanning preserves concurrent reference updates and does not starve eager images."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3

import pytest

from qtrade_agent.media import FakeDownloader,MediaStore
from qtrade_agent.models import Message,Session
from qtrade_agent.store import Store

PNG=bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c6360f8cf000002020100bdc0c4d30000000049454e44ae426082")


@pytest.fixture
def rig(tmp_path):
    store=Store(":memory:",clock=lambda:1793000000000).open()
    store.ensure_account("wx94","wechat",state="running",self_uid="wxid_synthetic")
    downloader=FakeDownloader()
    media=MediaStore(store,media_dir=str(tmp_path/"media"),downloader=downloader,clock=lambda:1793000000000)
    yield store,media,downloader
    store.close()


async def test_many_lazy_files_do_not_hide_a_later_eager_image(rig):
    store,media,downloader=rig
    lazy=[media.note(kind="file",origin={"file":"lazy-"+str(index)}) for index in range(80)]
    image=media.note(kind="image",origin={"file":"eager-image"})
    downloader.blobs["eager-image"]=PNG
    await media.fetch_eager(limit=20)
    assert media.get(image)["status"]=="ready", "A page of lazy files cannot permanently starve an eligible image"
    assert all(media.get(mid)["status"]=="pending" and media.get(mid)["attempts"]==0 for mid in lazy)
    assert [ref["file"] for ref in downloader.calls]==["eager-image"]


def test_scan_does_not_restore_a_media_id_retired_by_concurrent_deduplication(rig,monkeypatch):
    store,media,downloader=rig
    retained=media.put_bytes(PNG,kind="image",origin={"file":"retained"})["media_id"]
    retired=media.note(kind="image",origin={"file":"same-image"})
    downloader.blobs["same-image"]=PNG
    message=Message(account_id="wx94",channel="wechat",session=Session("wx94","room@chatroom","group"),
        dir="in",type="image",text="",ts_ms=1793000000000,source="chatlog",ext_msg_id="room@chatroom:1",
        media=[{"kind":"image","media_id":retired,"state":"pending"},{"kind":"image","file":"second-image"}])
    store.ingest(message,now_ms=1793000000000)
    original_tx=store._tx
    interleaved=[False]
    @contextmanager
    def transaction_with_racing_completion():
        # Complete another pending download immediately before the scanner's
        # first write transaction. The assertion is about persisted references,
        # not whether any particular helper or transaction shape was used.
        if not interleaved[0]:
            interleaved[0]=True
            asyncio.run(media.fetch_into(retired))
        with original_tx() as connection:yield connection
    monkeypatch.setattr(store,"_tx",transaction_with_racing_completion)
    created=media.scan_messages()
    if not interleaved[0]:
        asyncio.run(media.fetch_into(retired))
    attachments=json.loads(store.get_message(message.id)["media_json"])
    assert media.get(retired) is None
    assert attachments[0]["media_id"]==retained
    assert len(created)==1 and attachments[1]["media_id"]==created[0]
    assert all(media.get(item["media_id"]) is not None for item in attachments)
    assert media.read_file(retained)==PNG


def unlinked_message(store):
    message=Message(account_id="wx94",channel="wechat",session=Session("wx94","room@chatroom","group"),
        dir="in",type="image",text="",ts_ms=1793000000000,source="chatlog",ext_msg_id="room@chatroom:2",
        media=[{"kind":"image","file":"first"},{"kind":"image","file":"second"}])
    store.ingest(message,now_ms=1793000000000)
    return message


def test_media_creation_rolls_back_when_message_reference_update_fails(rig):
    store,media,downloader=rig
    message=unlinked_message(store)
    before=dict(store.get_message(message.id))
    with store._tx() as db:
        db.execute("CREATE TRIGGER refuse_media_link BEFORE UPDATE OF media_json ON messages BEGIN SELECT RAISE(ABORT,'synthetic link failure'); END")
    with pytest.raises(sqlite3.DatabaseError):media.scan_messages()
    assert store.get_message(message.id)==before
    assert store.con.execute("SELECT COUNT(*) FROM media").fetchone()[0]==0


def test_critical_disk_gate_mid_message_commits_only_the_already_linked_item(rig):
    store,media,downloader=rig
    message=unlinked_message(store)
    decisions=iter([True,False])
    media._ingest_allows=lambda:next(decisions)
    created=media.scan_messages()
    assert len(created)==1
    values=json.loads(store.get_message(message.id)["media_json"])
    assert values[0]["media_id"]==created[0] and media.get(created[0]) is not None
    assert "media_id" not in values[1]
    assert store.con.execute("SELECT COUNT(*) FROM media").fetchone()[0]==1
