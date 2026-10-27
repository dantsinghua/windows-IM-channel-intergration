"""Independent capture-only scope and control-isolation contracts; no real I/O."""
from copy import deepcopy
import base64
import importlib
import json
import re
from collections import defaultdict
import sqlite3

import pytest

from qtrade_agent.events import Events, message_payload
from qtrade_agent.models import Message, Session
from qtrade_agent.store import Store

NOW = 1800000000.0
GROUP = "capture-room@chatroom"
PEER = "wxpeer-" + base64.urlsafe_b64encode(GROUP.encode()).decode().rstrip("=")
UID = "wxid_synthetic_capture"


def module():
    return importlib.import_module("relay.wechat_capture")


def grant(**changes):
    values = dict(owner_user_id="institution-test", workbench_id="capture-wb",
                  account_uid=UID, consumer_id="nanyin-deposit-parser",
                  selection_version=1, selected_native_ids=(GROUP,))
    values.update(changes)
    return module().CaptureGrant(**values)


def image_event(ext="native-1"):
    return {"event_id": "source-" + ext, "peer_id": PEER, "ext_msg_id": ext,
            "kind": "message", "type": "image", "direction": "out", "text": "",
            "source_seq": 1, "occurred_at": NOW, "channel": "wechat",
            "native_account_uid": UID, "identity_epoch": 0,
            "session": {"native_id": GROUP, "kind": "group", "name": "Synthetic"},
            "historical": False, "media_state": "pending", "media": [], "attachments": [],
            "attribution": {"status": "unresolved"}}


def fact(g=None):
    m = module(); g = g or grant(); event = image_event()
    return m.make_payload(g, "capture_fact", capture_id=m.capture_id(g, PEER, event["ext_msg_id"]),
                          content_revision=1, media_revision=0, event=event)


def test_capture_identity_matches_existing_deposit_native_identity_and_not_slot():
    from relay.protocol import digest
    m = module(); g = grant()
    expected = digest(["its-relay/wechat-message/v1", g.workbench_id, UID, PEER, "native-1"])
    assert m.capture_id(g, PEER, "native-1") == expected
    assert m.capture_id(g, PEER, "native-2") != expected
    assert m.capture_id(grant(selection_version=2), PEER, "native-1") == expected


def test_capture_paths_are_bounded_separate_from_control_and_version_scoped():
    from relay.gateway_paths import account_paths
    m = module(); g = grant(); paths = m.capture_paths(g)
    assert set(paths) == {"capture_up", "capture_down"}
    assert len(set(paths.values())) == 2
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{20}", p) for p in paths.values())
    assert not set(paths.values()) & set(account_paths(g.workbench_id, UID, channel="wechat").values())
    assert set(paths.values()).isdisjoint(m.capture_paths(grant(selection_version=2)).values())


def test_capture_valid_fact_retains_native_direction_without_control_fields():
    m = module(); g = grant(); payload = fact(g); original = deepcopy(payload)
    assert m.validate_payload(payload, g) == payload
    assert payload == original
    assert payload["event"]["direction"] == "out"
    assert payload["event"]["attribution"] == {"status": "unresolved"}


@pytest.mark.parametrize("field,value", [
    ("purpose", "ordinary-event"), ("schema_version", 2),
    ("consumer_id", "other-consumer"), ("owner_user_id", "other-owner"),
    ("workbench_id", "other-workbench"), ("account_uid", "other-account"),
    ("channel", "qidian"), ("grant_id", "0" * 64), ("selection_version", 2),
])
def test_capture_rejects_every_foreign_scope_or_schema(field, value):
    from relay.protocol import ProtocolError
    m = module(); g = grant(); payload = fact(g); payload[field] = value
    with pytest.raises((ProtocolError, ValueError)):
        m.validate_payload(payload, g)


@pytest.mark.parametrize("field,value", [
    ("source_command_id", "unproved-command"), ("manual_fence_id", "fake-fence"),
    ("receiver_fence_version", 1), ("control_version", 4),
])
def test_capture_facts_cannot_smuggle_command_attribution_or_control(field, value):
    from relay.protocol import ProtocolError
    m = module(); g = grant(); payload = fact(g); payload["event"][field] = value
    with pytest.raises((ProtocolError, ValueError)):
        m.validate_payload(payload, g)


def test_capture_rejects_group_outside_explicit_grant_even_with_valid_peer_shape():
    from relay.protocol import ProtocolError
    m = module(); g = grant(); payload = fact(g)
    payload["event"]["session"]["native_id"] = "other@chatroom"
    with pytest.raises((ProtocolError, ValueError)):
        m.validate_payload(payload, g)


@pytest.mark.parametrize("revision", [True, 0, -1, 1.5])
def test_capture_content_revision_is_positive_integer(revision):
    from relay.protocol import ProtocolError
    m = module(); g = grant(); payload = fact(g); payload["content_revision"] = revision
    with pytest.raises((ProtocolError, ValueError)):
        m.validate_payload(payload, g)


def test_ready_image_requires_real_verified_attachment_content():
    from relay.protocol import ProtocolError
    m = module(); g = grant(); payload = fact(g)
    payload["event"]["media_state"] = "ready"
    with pytest.raises((ProtocolError, ValueError)):
        m.validate_payload(payload, g)


class Adapter:
    def __init__(self, binding):
        self.binding = binding
        self.published = []
        self.incoming = []
        self.manifest_handler = None
    def publish_manifest(self, payload, *, message_id=None):
        self.published.append((deepcopy(payload), message_id))
        return message_id or "synthetic-manifest"
    def poll(self, *args, **kwargs):
        for data in self.incoming:
            self.manifest_handler(data)
        self.incoming.clear()
        return []


def producer_fixture(tmp_path, *, online=True):
    from relay.protocol import Binding
    producer_class = importlib.import_module("qtrade_agent.ossbox.capture").WechatCaptureProducer
    g = grant(); clock = [NOW]
    store = Store(":memory:", clock=lambda: int(clock[0] * 1000)).open()
    store.ensure_account("wx94", "wechat", state="running", self_uid=UID)
    adapter = Adapter(Binding(g.owner_user_id, UID, g.workbench_id, b"a" * 32, b"b" * 32, channel="wechat"))
    producer = producer_class(store=store, grant=g, adapter=adapter, root=tmp_path,
                              media_provider=lambda event: {"media_state": "pending", "media": [], "attachments": []},
                              clock=lambda: clock[0], online_guard=lambda: online)
    return producer, store, adapter, g, clock


def emit_image(store):
    msg = Message(account_id="wx94", channel="wechat", session=Session("wx94", GROUP, "group"),
                  dir="out", type="image", text="", ts_ms=int(NOW*1000), source="chatlog",
                  ext_msg_id=GROUP + ":123", self=True, state="DELIVERED")
    store.ingest(msg, now_ms=int(NOW*1000))
    Events(store).emit("message", payload=message_payload(msg, late_after_s=30),
                       account_id="wx94", channel="wechat", now_ms=int(NOW*1000))


def test_producer_requires_matching_consumer_handshake_before_any_image_fact(tmp_path):
    producer, store, adapter, g, clock = producer_fixture(tmp_path)
    try:
        emit_image(store)
        producer.tick()
        assert any(p["op"] == "capture_hello" for p, _ in adapter.published)
        assert not any(p["op"] in ("capture_fact", "capture_media") for p, _ in adapter.published)
    finally:
        store.close()


def test_producer_offline_never_publishes_native_image_fact(tmp_path):
    producer, store, adapter, g, clock = producer_fixture(tmp_path, online=False)
    try:
        emit_image(store)
        producer.tick()
        assert not any(p["op"] in ("capture_fact", "capture_media") for p, _ in adapter.published)
    finally:
        store.close()


def ready(producer, adapter, g, clock, *, nonce=None, lease=None, groups=None):
    from relay.protocol import envelope
    producer.tick()
    hello=next(p for p,_ in reversed(adapter.published) if p["op"]=="capture_hello")
    value=module().make_payload(g,"capture_ready",nonce=nonce or hello["nonce"],
        lease_until=clock[0]+60 if lease is None else lease,
        active_native_ids=list(g.selected_native_ids) if groups is None else groups)
    data=envelope(adapter.binding,"manifest",value,message_id="synthetic-ready",
                  created_at=clock[0],expires_at=clock[0]+100)
    producer.accept_manifest(data)


def test_confirmed_handshake_publishes_own_image_without_modifying_command_state(tmp_path):
    producer, store, adapter, g, clock=producer_fixture(tmp_path)
    try:
        control_before=[tuple(r) for r in store.con.execute("SELECT * FROM commands")]
        ready(producer,adapter,g,clock)
        emit_image(store);producer.tick()
        facts=[p for p,_ in adapter.published if p["op"] in ("capture_fact","capture_media")]
        assert len(facts)==1 and facts[0]["event"]["direction"]=="out"
        assert facts[0]["event"]["media_state"]=="pending"
        assert facts[0]["event"]["attribution"]=={"status":"unresolved"}
        assert [tuple(r) for r in store.con.execute("SELECT * FROM commands")]==control_before
    finally:
        store.close()


def test_expired_handshake_stops_new_facts_until_renewed(tmp_path):
    producer, store, adapter, g, clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock)
        clock[0]+=61
        emit_image(store);producer.tick()
        assert not any(p["op"] in ("capture_fact","capture_media") for p,_ in adapter.published)
    finally:
        store.close()


@pytest.mark.parametrize("bad",["nonce","excessive-lease","expired-lease","empty-selection"])
def test_invalid_or_empty_ready_never_authorizes_picture_publication(tmp_path,bad):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        kwargs={}
        if bad=="nonce":kwargs["nonce"]="unrelated-handshake"
        if bad=="excessive-lease":kwargs["lease"]=clock[0]+61
        if bad=="expired-lease":kwargs["lease"]=clock[0]-1
        if bad=="empty-selection":kwargs["groups"]=[]
        try:
            ready(producer,adapter,g,clock,**kwargs)
        except ValueError:
            pass
        emit_image(store);producer.tick()
        assert not any(p["op"] in ("capture_fact","capture_media") for p,_ in adapter.published)
    finally:
        store.close()


def test_grant_file_absence_disables_without_implicit_remote_authorization(tmp_path):
    assert module().load_grants_file(tmp_path/"absent.json","institution-test")== ((),())


def test_owner_only_grant_file_loads_and_keeps_explicit_backfill_scope(tmp_path):
    m=module();g=grant();path=tmp_path/"grant.json"
    backfill={"grant_id":g.grant_id,"native_id":GROUP,"ext_msg_id":"native-1"}
    path.write_text(json.dumps({"schema_version":1,"grants":[g.as_dict()],"backfill":[backfill]}));path.chmod(0o600)
    groups,history=m.load_grants_file(path,g.owner_user_id)
    assert groups==(g,) and history==(backfill,)


@pytest.mark.parametrize("bad",["owner","duplicate-scope","outside-backfill","permissions","symlink"])
def test_invalid_local_grant_cannot_enable_capture(tmp_path,bad):
    m=module();g=grant();path=tmp_path/"grant.json"
    data={"schema_version":1,"grants":[g.as_dict()]}
    expected=g.owner_user_id
    if bad=="owner":expected="other-owner"
    if bad=="duplicate-scope":data["grants"].append(grant(selection_version=2).as_dict())
    if bad=="outside-backfill":data["backfill"]=[{"grant_id":g.grant_id,"native_id":"foreign@chatroom","ext_msg_id":"native-1"}]
    path.write_text(json.dumps(data));path.chmod(0o644 if bad=="permissions" else 0o600)
    if bad=="symlink":
        link=tmp_path/"linked.json";link.symlink_to(path);path=link
    with pytest.raises((ValueError,OSError)):
        m.load_grants_file(path,expected)


class Queues:
    def __init__(self): self.rows=defaultdict(list);self.uploads=[]
    def client(self,path):
        owner=self
        class Client:
            queue_identity="capture-test:"+path
            def pending(self):return len(owner.rows[path])
            def upload(self,name,content):
                from relay.file_gateway import DownloadedFile
                owner.uploads.append((path,name))
                owner.rows[path].append(DownloadedFile(name,content))
                return {"file_key":"synthetic-key","file_name":name}
            def download(self):return owner.rows[path].pop(0) if owner.rows[path] else None
        return Client()


def test_persisted_capture_retry_rechecks_revoked_delivery_permission(tmp_path):
    from relay.protocol import Binding
    from server.wechat_capture_transport import CaptureTransport
    g=grant();clock=[NOW];allowed=[True];queues=Queues();paths=module().capture_paths(g)
    adapter=CaptureTransport(Binding(g.owner_user_id,UID,g.workbench_id,b'a'*32,b'b'*32,channel="wechat"),
        queues.client(paths["capture_down"]),queues.client(paths["capture_up"]),tmp_path/"transport.db",
        role="collector",institution_owner=g.owner_user_id,clock=lambda:clock[0],
        delivery_guard=lambda data:allowed[0])
    value=fact(g)
    adapter.publish_manifest(value,message_id="one-durable-fact")
    assert len(queues.uploads)==1
    queues.rows[paths["capture_up"]].clear()  # The remote consumer downloaded, but its ACK was lost.
    allowed[0]=False;clock[0]+=31
    adapter.retry_data()
    assert len(queues.uploads)==1
    with adapter.store.db() as db:
        row=db.execute("SELECT attempts,acked FROM ip_delivery WHERE id='one-durable-fact'").fetchone()
        assert tuple(row)==(1,0),"Revocation must preserve the pending record without retransmission"
    allowed[0]=True
    adapter.retry_data()
    assert len(queues.uploads)==2


def test_capture_transport_does_not_publish_when_guard_is_false(tmp_path):
    from relay.protocol import Binding
    from server.wechat_capture_transport import CaptureTransport
    g=grant();queues=Queues();paths=module().capture_paths(g)
    adapter=CaptureTransport(Binding(g.owner_user_id,UID,g.workbench_id,b'a'*32,b'b'*32,channel="wechat"),
        queues.client(paths["capture_down"]),queues.client(paths["capture_up"]),tmp_path/"transport.db",
        role="collector",institution_owner=g.owner_user_id,clock=lambda:NOW,delivery_guard=lambda data:False)
    with pytest.raises(ValueError):adapter.publish_manifest(fact(g),message_id="denied-fact")
    assert queues.uploads==[]


def test_capture_transport_retry_cannot_resurrect_a_rejected_control_event(tmp_path):
    from relay.protocol import Binding
    from server.wechat_capture_transport import CaptureTransport
    g=grant();queues=Queues();clock=[NOW];paths=module().capture_paths(g)
    adapter=CaptureTransport(Binding(g.owner_user_id,UID,g.workbench_id,b'a'*32,b'b'*32,channel="wechat"),
        queues.client(paths["capture_down"]),queues.client(paths["capture_up"]),tmp_path/"transport.db",
        role="collector",institution_owner=g.owner_user_id,clock=lambda:clock[0],delivery_guard=lambda data:True)
    with pytest.raises(ValueError):adapter.publish_event(image_event())
    assert queues.uploads==[]
    clock[0]+=31
    adapter.retry_data()
    assert queues.uploads==[],"Retries must retain the manifest-only transport boundary"


@pytest.mark.parametrize("change",["uid","disabled","grant","native-row"])
async def test_historical_image_refresh_rechecks_identity_after_native_read(tmp_path,change):
    from types import SimpleNamespace
    from qtrade_agent.app import AgentApp
    from qtrade_agent.adapters.wechat.normalize import to_message
    g=grant();store=Store(":memory:",clock=lambda:int(NOW*1000)).open()
    store.ensure_account("wx94","wechat",state="running",self_uid=UID)
    original={"talker":GROUP,"seq":123,"type":3,"time":int(NOW),"isSelf":True,"content":""}
    msg=to_message("wx94",original,self_uid=UID);store.ingest(msg,now_ms=int(NOW*1000))
    store.cursor_set("wx94","chatlog_seq",999)
    authorized=[True];app=object.__new__(AgentApp);app.store=store
    app.xunjia_v22_host=SimpleNamespace(capture=SimpleNamespace(authorized=lambda value:authorized[0]))
    app.events=Events(store);app.cfg=SimpleNamespace(messages=SimpleNamespace(late_after_s=30));app.clock=lambda:int(NOW*1000)
    async def presence(account):return {"state":"online","self_uid":UID}
    app._xunjia_institution_presence=presence
    async def native_read(**kwargs):
        assert kwargs=={"talker":GROUP,"since_seq":122,"limit":2}
        if change=="uid":store.set_account_state("wx94","running",self_uid="wxid_other")
        if change=="disabled":store.transition("wx94","disabled",enabled=False)
        if change=="grant":authorized[0]=False
        return [{**original,"seq":124 if change=="native-row" else 123,
                 "contents":{"md5":"a1"*16,"path":"msg\\attach\\abc\\2026-10\\Img\\def"}}]
    app.wechat_client=SimpleNamespace(read=native_read)
    before=[tuple(r) for r in store.con.execute("SELECT * FROM messages")]
    cursors=[tuple(r) for r in store.con.execute("SELECT * FROM cursors")]
    try:
        with pytest.raises(ValueError):await app._capture_wechat_media_refresh(g,GROUP,msg.ext_msg_id)
        assert [tuple(r) for r in store.con.execute("SELECT * FROM messages")]==before
        assert [tuple(r) for r in store.con.execute("SELECT * FROM cursors")]==cursors
    finally:
        store.close()


def checkpoint(producer):
    with producer.db() as db:
        return int(db.execute("SELECT value FROM capture_meta WHERE key='cursor'").fetchone()[0])


def test_capture_stage_commit_failure_does_not_advance_source_cursor(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock);before=checkpoint(producer)
        emit_image(store)
        with producer.db() as db:
            db.execute("CREATE TRIGGER fail_stage BEFORE INSERT ON capture_outbox BEGIN SELECT RAISE(ABORT,'synthetic durable failure'); END")
        with pytest.raises(sqlite3.DatabaseError):producer.tick()
        assert checkpoint(producer)==before
        assert not any(p["op"] in ("capture_fact","capture_media") for p,_ in adapter.published)
        with producer.db() as db:
            assert db.execute("SELECT COUNT(*) FROM capture_message").fetchone()[0]==0
            db.execute("DROP TRIGGER fail_stage")
        producer.tick()
        assert checkpoint(producer)>before
        assert len([p for p,_ in adapter.published if p["op"]=="capture_fact"])==1
    finally:
        store.close()


def test_rejection_must_be_durably_recorded_before_source_cursor_advances(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock);before=checkpoint(producer);emit_image(store)
        with store._tx() as db:
            db.execute("UPDATE messages SET source='onebot'")
        with producer.db() as db:
            db.execute("CREATE TRIGGER fail_quarantine BEFORE INSERT ON capture_rejected BEGIN SELECT RAISE(ABORT,'synthetic quarantine failure'); END")
        with pytest.raises(sqlite3.DatabaseError):producer.tick()
        assert checkpoint(producer)==before
        with producer.db() as db:
            db.execute("DROP TRIGGER fail_quarantine")
        producer.tick()
        with producer.db() as db:
            assert db.execute("SELECT COUNT(*) FROM capture_rejected").fetchone()[0]==1
        assert checkpoint(producer)>before
        assert not any(p["op"] in ("capture_fact","capture_media") for p,_ in adapter.published)
    finally:
        store.close()


def test_committed_fact_survives_checkpoint_crash_and_replay_is_idempotent(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock);before=checkpoint(producer);emit_image(store)
        with producer.db() as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON capture_meta WHEN OLD.key='cursor' BEGIN SELECT RAISE(ABORT,'synthetic checkpoint crash'); END")
        with pytest.raises(sqlite3.DatabaseError):producer.tick()
        assert checkpoint(producer)==before
        with producer.db() as db:
            assert db.execute("SELECT COUNT(*) FROM capture_message").fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM capture_outbox").fetchone()[0]==1
            db.execute("DROP TRIGGER fail_checkpoint")
        producer_class=importlib.import_module("qtrade_agent.ossbox.capture").WechatCaptureProducer
        restored=producer_class(store=store,grant=g,adapter=adapter,root=tmp_path,
            media_provider=lambda event:{"media_state":"pending","media":[],"attachments":[]},
            clock=lambda:clock[0],online_guard=lambda:True)
        ready(restored,adapter,g,clock);restored.tick()
        with restored.db() as db:
            assert db.execute("SELECT COUNT(*) FROM capture_message").fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM capture_outbox").fetchone()[0]==1
        assert len([p for p,_ in adapter.published if p["op"]=="capture_fact"])==1
    finally:
        store.close()


def test_offline_preserves_cursor_and_later_online_captures_the_pending_message(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path,online=False)
    try:
        before=checkpoint(producer);emit_image(store);producer.tick()
        assert checkpoint(producer)==before
        producer.online_guard=lambda:True
        ready(producer,adapter,g,clock);producer.tick()
        assert len([p for p,_ in adapter.published if p["op"]=="capture_fact"])==1
        assert checkpoint(producer)>before
    finally:
        store.close()


def embedded_dicts(value,depth=0):
    if depth>8:return
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from embedded_dicts(child,depth+1)
    elif isinstance(value,(list,tuple)):
        for child in value:yield from embedded_dicts(child,depth+1)
    elif isinstance(value,str) and value.startswith(("{","[")):
        try:decoded=json.loads(value)
        except ValueError:return
        yield from embedded_dicts(decoded,depth+1)


def test_rejected_source_keeps_original_event_and_message_for_controlled_recovery(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock);emit_image(store)
        with store._tx() as db:
            db.execute("UPDATE messages SET source='onebot'")
        producer.tick()
        rows=producer.rejected_sources()
        assert len(rows)==1
        snapshots=list(embedded_dicts(rows[0]))
        assert any(v.get("account_id")=="wx94" and v.get("ext_msg_id")==GROUP+":123"
                   and "media_json" in v and "identity_epoch" in v for v in snapshots), \
            "A reason and sequence alone cannot recover the rejected native message"
        assert any(v.get("native_uid")==UID and v.get("account_id")=="wx94"
                   and v.get("identity_epoch")==0 and v.get("message_id") and "payload_json" in v
                   for v in snapshots),"The original atomic native source must survive its cursor checkpoint"
    finally:
        store.close()
