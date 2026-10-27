"""Image references and persistent media links, with no native or network I/O."""
import base64
import json
import sqlite3

import pytest

from qtrade_agent.adapters.wechat.normalize import to_message
from qtrade_agent.media import FakeDownloader, MediaStore, UrllibDownloader
from qtrade_agent.store import Store

MD5 = "a1" * 16
GROUP = "123456789@chatroom"
NATIVE_PATH = "msg\\attach\\" + "b2" * 16 + "\\2026-10\\Img\\" + "c3" * 16
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def image_row(seq=1, **fields):
    return {"talker": GROUP, "seq": seq, "type": 3, "time": 1793000000 + seq,
            "isSelf": False, "content": "", **fields}


@pytest.mark.parametrize("outgoing", [False, True])
async def test_nested_native_image_can_reach_the_owned_downloader(outgoing):
    row = image_row(contents={"md5": MD5, "path": NATIVE_PATH}, isSelf=outgoing)
    message = to_message("wx07", row, self_uid="wxid_synthetic")
    assert message.type == "image" and message.dir == ("out" if outgoing else "in")
    assert len(message.media) == 1, "A native picture reference must not become an empty attachment list"
    calls = []
    async def owned(path):
        calls.append(path)
        return PNG
    fetched = await UrllibDownloader(wa_raw=owned).fetch(message.media[0], max_bytes=1024)
    assert fetched.ok and fetched.data == PNG
    assert len(calls) == 1 and calls[0].startswith("/wa/v1/wechat/media/")


@pytest.mark.parametrize("key_name", ["md5", "MD5", "key", "imgMd5"])
async def test_existing_top_level_image_reference_still_downloads(key_name):
    message = to_message("wx07", image_row(**{key_name: MD5}))
    assert len(message.media) == 1
    calls = []
    async def owned(path):
        calls.append(path)
        return PNG
    fetched = await UrllibDownloader(wa_raw=owned).fetch(message.media[0], max_bytes=1024)
    assert fetched.ok and fetched.data == PNG
    assert calls == ["/wa/v1/wechat/media/" + MD5]


async def test_persisted_legacy_ref_is_accepted_only_as_owned_wechat_media():
    calls = []
    async def owned(path):
        calls.append(path)
        return PNG
    fetched = await UrllibDownloader(wa_raw=owned).fetch(
        {"kind": "image", "ref": "/wa/v1/wechat/media/" + MD5}, max_bytes=1024)
    assert fetched.ok and fetched.data == PNG
    assert calls == ["/wa/v1/wechat/media/" + MD5]


@pytest.mark.parametrize("field", ["ref", "wa_path"])
@pytest.mark.parametrize("reference", ["https://example.invalid/picture", "file:///private/file",
    "/wa/v1/vault/read", "/wa/v1/wechat/media/../../vault", "//example.invalid/picture"])
async def test_native_ref_cannot_select_another_endpoint_or_external_source(field, reference):
    calls = []
    async def owned(path):
        calls.append(path)
        raise AssertionError("Untrusted reference must not cross the WinAgent boundary")
    fetched = await UrllibDownloader(wa_raw=owned).fetch({field: reference}, max_bytes=1024)
    assert not fetched.ok and not calls


@pytest.mark.parametrize("contents", [{"md5": True}, {"md5": []}, {"md5": {"x": 1}},
    {"md5": MD5, "path": "../../other-account/image"},
    {"md5": MD5, "path": r"C:\outside\image"},
    {"md5": MD5, "path": r"\\server\share\image"},
    {"md5": MD5, "path": "msg\\attach\\..\\..\\outside"},
    {"md5": MD5, "path": "msg\\attach\\%2e%2e\\image"},
    {"md5": MD5, "path": "msg\\attach\\image\n"},
    {"md5": MD5, "path": {"path": "msg\\attach\\image"}}])
def test_invalid_nested_resource_never_becomes_a_downloadable_attachment(contents):
    try:
        message = to_message("wx07", image_row(contents=contents))
    except ValueError:
        return
    assert message.media == []


def test_conflicting_native_image_identifiers_do_not_choose_an_arbitrary_picture():
    try:
        message = to_message("wx07", image_row(md5="d4" * 16,
            contents={"md5": MD5, "path": NATIVE_PATH}))
    except ValueError:
        return
    assert message.media == [], "Conflicting native metadata cannot be treated as one proven image"


@pytest.fixture
def media_store(tmp_path):
    store = Store(":memory:", clock=lambda: 1793000000000).open()
    store.ensure_account("wx07", "wechat", state="running", self_uid="wxid_synthetic")
    downloader = FakeDownloader()
    media = MediaStore(store, media_dir=str(tmp_path / "media"), downloader=downloader,
                       clock=lambda: 1793000000000)
    yield store, media, downloader
    store.close()


async def test_same_image_in_two_native_messages_keeps_both_media_links_readable(media_store):
    store, media, downloader = media_store
    messages = []
    for seq, key in [(1, "a1" * 16), (2, "d4" * 16)]:
        message = to_message("wx07", image_row(seq, md5=key))
        # Use the existing downloader contract to isolate content deduplication.
        message.media = [{"kind": "image", "wa_path": "/wa/v1/wechat/media/" + key}]
        store.ingest(message, now_ms=1793000000000 + seq)
        downloader.blobs[message.media[0]["wa_path"]] = PNG
        messages.append(message)
    assert len(media.scan_messages()) == 2
    result = await media.fetch_eager()
    assert result["fetched"] == 2
    retained = []
    for message in messages:
        saved = store.get_message(message.id)
        assert saved is not None
        [attachment] = json.loads(saved["media_json"])
        row = media.get(attachment["media_id"])
        assert row is not None, "Content deduplication must rebind the message before retiring its media row"
        assert row["status"] == "ready"
        assert media.read_file(row["id"]) == PNG
        retained.append(row["id"])
    assert len(store.list_messages("wx07", limit=10)) == 2
    assert len(set(retained)) == 1
    assert media.scan_messages() == []
    assert (await media.fetch_eager())["fetched"] == 0
    assert len(downloader.calls) == 2


def test_new_attachment_is_not_skipped_when_the_message_already_has_a_media_link(media_store):
    store, media, _ = media_store
    existing = media.put_bytes(PNG, kind="image", origin={"file":"synthetic-original"})
    message = to_message("wx07", image_row())
    message.media = [
        {"kind":"image", "media_id":existing["media_id"], "state":"ready"},
        {"kind":"image", "wa_path":"/wa/v1/wechat/media/" + MD5},
    ]
    store.ingest(message, now_ms=1793000000001)
    created = media.scan_messages()
    assert len(created) == 1, "A completed first attachment must not hide a newly supplied second one"
    attachments = json.loads(store.get_message(message.id)["media_json"])
    assert attachments[0]["media_id"] == existing["media_id"]
    assert attachments[1]["media_id"] == created[0]
    assert media.get(created[0])["status"] == "pending"
    assert media.scan_messages() == []


async def test_interrupted_duplicate_rebinding_rolls_back_links_and_reference_count(media_store):
    store, media, downloader = media_store
    existing = media.put_bytes(PNG, kind="image", origin={"file":"synthetic-original"})
    message = to_message("wx07", image_row(md5=MD5))
    message.media = [{"kind":"image", "wa_path":"/wa/v1/wechat/media/" + MD5}]
    store.ingest(message, now_ms=1793000000001)
    [pending_id] = media.scan_messages()
    downloader.blobs[message.media[0]["wa_path"]] = PNG
    before_message = store.get_message(message.id)["media_json"]
    before_refs = media.get(existing["media_id"])["ref_count"]
    store.con.execute("CREATE TRIGGER fail_media_retirement BEFORE DELETE ON media "
        "BEGIN SELECT RAISE(ABORT, 'synthetic transaction interruption'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await media.fetch_into(pending_id)
    assert store.get_message(message.id)["media_json"] == before_message
    assert media.get(pending_id) is not None
    assert media.get(existing["media_id"])["ref_count"] == before_refs
    store.con.execute("DROP TRIGGER fail_media_retirement")
    outcome = await media.fetch_into(pending_id)
    assert outcome["status"] == "ready"
    [attachment] = json.loads(store.get_message(message.id)["media_json"])
    assert media.read_file(attachment["media_id"]) == PNG


def empty_native_message(store, *, state="DELIVERED"):
    message = to_message("wx07", image_row(isSelf=True))
    message.state = state
    store.ingest(message, now_ms=1793000000001)
    store.cursor_set("wx07", "chatlog_seq", 123)
    return message, [{"kind":"image", "wa_path":"/wa/v1/wechat/media/" + MD5}]


def native_complete(store, message, media, **overrides):
    kwargs = {"account_id":"wx07", "expected_uid":"wxid_synthetic", "expected_epoch":0,
              "expected_ext":message.ext_msg_id, "media":media, "expected_media_json":"[]"}
    kwargs.update(overrides)
    return store.complete_native_media(message.id, **kwargs)


@pytest.mark.parametrize("state", ["DELIVERED", "UNCONFIRMED"])
def test_historical_media_completion_only_changes_attachment_and_is_idempotent(media_store, state):
    store, _, _ = media_store
    message, attachment = empty_native_message(store, state=state)
    before = dict(store.con.execute("SELECT * FROM messages WHERE id=?", (message.id,)).fetchone())
    cursors = [tuple(row) for row in store.con.execute("SELECT * FROM cursors")]
    assert native_complete(store, message, attachment) is True
    after = dict(store.con.execute("SELECT * FROM messages WHERE id=?", (message.id,)).fetchone())
    assert json.loads(after["media_json"]) == attachment
    assert {key:value for key,value in after.items() if key != "media_json"} == {
        key:value for key,value in before.items() if key != "media_json"}
    assert [tuple(row) for row in store.con.execute("SELECT * FROM cursors")] == cursors
    assert native_complete(store, message, attachment) is False


@pytest.mark.parametrize("overrides", [
    {"account_id":"wx08"}, {"expected_uid":"wxid_other"}, {"expected_epoch":1},
    {"expected_ext":GROUP + ":999"}, {"expected_media_json":'[ {"different":"revision"} ]'},
])
def test_historical_media_completion_rejects_identity_or_compare_and_swap_mismatch(media_store, overrides):
    store, _, _ = media_store
    message, attachment = empty_native_message(store)
    before = [tuple(row) for row in store.con.execute("SELECT * FROM messages")]
    cursors = [tuple(row) for row in store.con.execute("SELECT * FROM cursors")]
    with pytest.raises(ValueError):
        native_complete(store, message, attachment, **overrides)
    assert [tuple(row) for row in store.con.execute("SELECT * FROM messages")] == before
    assert [tuple(row) for row in store.con.execute("SELECT * FROM cursors")] == cursors


def test_historical_completion_does_not_overwrite_a_nonempty_media_revision(media_store):
    store, _, _ = media_store
    message, attachment = empty_native_message(store)
    existing = [{"kind":"image", "wa_path":"/wa/v1/wechat/media/" + "d4"*16}]
    previous = json.dumps(existing)
    store.con.execute("UPDATE messages SET media_json=? WHERE id=?", (previous, message.id))
    with pytest.raises(ValueError):
        native_complete(store, message, attachment, expected_media_json=previous)
    assert store.get_message(message.id)["media_json"] == previous
