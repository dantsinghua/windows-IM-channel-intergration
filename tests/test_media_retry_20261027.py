"""Media retries are durable, bounded and do not turn permanent errors into loops."""
from types import SimpleNamespace
import asyncio

import pytest

from qtrade_agent.media import MediaStore, UrllibDownloader
from qtrade_agent.store import Store

PNG = b"\x89PNG\r\n\x1a\nsynthetic-media-payload"


class Downloader:
    def __init__(self, outcomes): self.outcomes=list(outcomes); self.calls=0
    async def fetch(self, ref, *, max_bytes):
        value=self.outcomes[min(self.calls,len(self.outcomes)-1)]
        self.calls+=1
        return value


def outcome(ok=False, *, retryable=False):
    return SimpleNamespace(ok=ok,data=PNG if ok else b"",reason="" if ok else "synthetic-failure",
                           declared_size=len(PNG) if ok else None,retryable=retryable)


@pytest.fixture
def rig(tmp_path):
    clock=[1793000000000]
    store=Store(":memory:",clock=lambda:clock[0]).open()
    made=[]
    def make(outcomes):
        downloader=Downloader(outcomes)
        media=MediaStore(store,media_dir=str(tmp_path/"media"),downloader=downloader,clock=lambda:clock[0])
        mid=media.note(kind="image",origin={"wa_path":"/wa/v1/wechat/media/synthetic"})
        return media,mid,downloader,clock
    yield make
    store.close()


async def test_temporary_download_failure_retries_only_when_due_and_recovers(rig):
    media,mid,downloader,clock=rig([outcome(retryable=True),outcome(True)])
    await media.fetch_eager()
    first=media.get(mid)
    assert first["status"]=="failed" and first["attempts"]==1
    assert first["next_attempt_ms"]>clock[0]
    await media.fetch_eager()
    assert downloader.calls==1
    clock[0]=first["next_attempt_ms"]
    await media.fetch_eager()
    ready=media.get(mid)
    assert downloader.calls==2 and ready["status"]=="ready"
    assert ready["attempts"]==2 and ready["next_attempt_ms"] is None and not ready["fail_reason"]
    assert media.read_file(mid)==PNG


async def test_repeated_temporary_failure_stops_at_five_automatic_attempts(rig):
    media,mid,downloader,clock=rig([outcome(retryable=True)])
    for attempt in range(1,6):
        await media.fetch_eager()
        saved=media.get(mid)
        assert saved["attempts"]==attempt
        if attempt<5:
            assert 0<saved["next_attempt_ms"]-clock[0]<=300000
            clock[0]=saved["next_attempt_ms"]
        else:
            assert saved["next_attempt_ms"] is None and saved["status"]=="failed"
    clock[0]+=86400000
    await media.fetch_eager()
    assert downloader.calls==5


async def test_permanent_error_has_no_automatic_retry_but_explicit_retry_is_available(rig):
    media,mid,downloader,clock=rig([outcome(retryable=False),outcome(True)])
    await media.fetch_eager()
    first=media.get(mid)
    assert first["status"]=="failed" and first["attempts"]==1 and first["next_attempt_ms"] is None
    clock[0]+=86400000
    await media.fetch_eager()
    assert downloader.calls==1
    await media.fetch_into(mid)
    assert downloader.calls==2 and media.get(mid)["status"]=="ready"


@pytest.mark.parametrize("retryable",[True,False])
async def test_owned_winagent_error_retains_explicit_retry_classification(retryable):
    from qtrade_agent.media import MediaFetchError
    async def fail(path): raise MediaFetchError("synthetic-reader-error",retryable=retryable)
    result=await UrllibDownloader(wa_raw=fail).fetch(
        {"wa_path":"/wa/v1/wechat/media/synthetic"},max_bytes=1024)
    assert not result.ok and result.retryable is retryable


def test_default_failed_download_is_not_implicitly_retryable():
    from qtrade_agent.media import Fetched
    assert Fetched(False,reason="no_ref").retryable is False


async def test_concurrent_automatic_fetches_claim_one_download_lease(rig):
    media,mid,downloader,clock=rig([outcome(True)])
    entered,release=asyncio.Event(),asyncio.Event()
    original=downloader.fetch
    async def slow(ref,*,max_bytes):
        entered.set()
        await release.wait()
        return await original(ref,max_bytes=max_bytes)
    downloader.fetch=slow
    first=asyncio.create_task(media.fetch_eager())
    await asyncio.wait_for(entered.wait(),timeout=2)
    try:
        await asyncio.wait_for(media.fetch_eager(),timeout=2)
        assert media.get(mid)["attempts"]==1
    finally:
        release.set()
        await first
    assert downloader.calls==1 and media.get(mid)["status"]=="ready"


async def test_interrupted_last_attempt_lease_expires_to_failure_without_sixth_download(rig):
    media,mid,downloader,clock=rig([outcome(True)])
    with media._store._tx() as conn:
        conn.execute("UPDATE media SET attempts=4 WHERE id=?",(mid,))
    entered=asyncio.Event()
    async def interrupted(ref,*,max_bytes):
        downloader.calls+=1
        entered.set()
        await asyncio.Event().wait()
    downloader.fetch=interrupted
    task=asyncio.create_task(media.fetch_eager())
    await asyncio.wait_for(entered.wait(),timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    claimed=media.get(mid)
    assert claimed["attempts"]==5 and claimed["next_attempt_ms"]>clock[0]
    clock[0]=claimed["next_attempt_ms"]+1
    await media.fetch_eager()
    final=media.get(mid)
    assert downloader.calls==1 and final["status"]=="failed" and final["next_attempt_ms"] is None


@pytest.mark.parametrize("deduplicate",[False,True])
async def test_old_download_success_cannot_overwrite_newer_identity_failure_or_rebind_message(rig,deduplicate):
    from qtrade_agent.models import Message,Session
    media,mid,downloader,clock=rig([outcome(True)])
    store=media._store
    store.ensure_account("wx51","wechat",state="running",self_uid="wxid_synthetic")
    message=Message(account_id="wx51",channel="wechat",session=Session("wx51","synthetic@chatroom","group"),
        dir="in",type="image",text="",ts_ms=clock[0],source="chatlog",ext_msg_id="synthetic@chatroom:1",
        media=[{"kind":"image","media_id":mid,"state":"pending"}])
    store.ingest(message,now_ms=clock[0])
    retained=media.put_bytes(PNG,kind="image",origin={"file":"synthetic-dedup"}) if deduplicate else None
    retained_before=dict(media.get(retained["media_id"])) if retained else None
    entered,release=asyncio.Event(),asyncio.Event()
    async def out_of_order(ref,*,max_bytes):
        downloader.calls+=1
        if downloader.calls==1:
            entered.set();await release.wait();return outcome(True)
        failed=outcome(retryable=False);failed.reason="identity_changed";return failed
    downloader.fetch=out_of_order
    old=asyncio.create_task(media.fetch_eager())
    await asyncio.wait_for(entered.wait(),timeout=2)
    clock[0]=media.get(mid)["next_attempt_ms"]+1
    await media.fetch_eager()
    after_new=dict(media.get(mid));message_after_new=dict(store.get_message(message.id))
    assert after_new["status"]=="failed" and after_new["attempts"]==2 and after_new["fail_reason"]=="identity_changed"
    release.set();await old
    assert media.get(mid)==after_new,"A stale success must not retire or revive the newer failed media row"
    assert dict(store.get_message(message.id))==message_after_new,"A stale completion must not rebind attachment IDs"
    if retained:
        assert media.get(retained["media_id"])==retained_before
