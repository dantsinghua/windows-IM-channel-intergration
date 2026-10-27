"""Authorized WeChat fact capture, independent from native sending controls."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from relay.gateway_paths import CHUNK_BYTES
from relay.protocol import ProtocolError, canonical, digest
from relay.wechat_capture import (LEASE_SECONDS, capture_id, capture_paths,
                                 load_grants_file, make_payload, peer_for, validate_payload)


class WechatCaptureProducer:
    """Consume the durable native outbox without executing or classifying sends."""
    def __init__(self, *, store, grant, adapter, root, media_provider=None,
                 clock=time.time, online_guard=lambda: True, refresher=None,
                 authorized_guard=lambda: True):
        self.store, self.grant, self.adapter = store, grant, adapter
        self.media_provider, self.clock = media_provider, clock
        self.online_guard, self.refresher = online_guard, refresher
        self.authorized_guard = authorized_guard
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root/"capture.sqlite3"
        self.lock = threading.RLock()
        self.nonce = uuid.uuid4().hex
        self.ready_until = 0.0
        self.active_native_ids = frozenset()
        self.last_hello = float("-inf")
        self.error = None
        self.adapter.manifest_handler = self.accept_manifest
        # CaptureTransport consults this for both initial writes and retries.
        self.adapter.delivery_guard = self.delivery_permitted
        bounds = store.capture_source_bounds()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS capture_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capture_message(
                    id TEXT PRIMARY KEY,message_id TEXT NOT NULL,native_id TEXT NOT NULL,ext_msg_id TEXT NOT NULL,
                    source_seq INTEGER NOT NULL,historical INTEGER NOT NULL,content_revision INTEGER NOT NULL,
                    media_revision INTEGER NOT NULL,content_hash TEXT NOT NULL,media_hash TEXT NOT NULL,
                    event TEXT NOT NULL,checked_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS capture_outbox(id TEXT PRIMARY KEY,capture_id TEXT NOT NULL,payload TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS capture_backfill(native_id TEXT,ext_msg_id TEXT,state TEXT NOT NULL,error TEXT,
                    PRIMARY KEY(native_id,ext_msg_id));
                CREATE TABLE IF NOT EXISTS capture_rejected(source_seq INTEGER PRIMARY KEY,code TEXT NOT NULL,
                    message_id TEXT,native_id TEXT,ext_msg_id TEXT,snapshot TEXT,state TEXT NOT NULL DEFAULT 'pending',
                    checked_at REAL NOT NULL DEFAULT 0,recovered_seq INTEGER);
            """)
            rejected_columns = {row[1] for row in db.execute("PRAGMA table_info(capture_rejected)")}
            for name, declaration in (("message_id", "TEXT"), ("native_id", "TEXT"), ("ext_msg_id", "TEXT"),
                                      ("snapshot", "TEXT"), ("state", "TEXT NOT NULL DEFAULT 'pending'"),
                                      ("checked_at", "REAL NOT NULL DEFAULT 0"), ("recovered_seq", "INTEGER")):
                if name not in rejected_columns:
                    db.execute("ALTER TABLE capture_rejected ADD COLUMN " + name + " " + declaration)
            old = db.execute("SELECT value FROM capture_meta WHERE key='grant'").fetchone()
            if old and old[0] != grant.grant_id:
                raise ProtocolError("CAPTURE_STATE_SCOPE_CHANGED")
            source_kind = db.execute("SELECT value FROM capture_meta WHERE key='source_kind'").fetchone()
            if old and (not source_kind or source_kind[0] != "native-capture-source-v1"):
                raise ProtocolError("CAPTURE_SOURCE_NAMESPACE_CHANGED")
            db.execute("INSERT OR IGNORE INTO capture_meta VALUES('grant',?)", (grant.grant_id,))
            db.execute("INSERT OR IGNORE INTO capture_meta VALUES('source_kind','native-capture-source-v1')")
            db.execute("INSERT OR IGNORE INTO capture_meta VALUES('cursor',?)", (str(bounds[1] or 0),))
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _account(self):
        matches = [row for row in self.store.list_accounts(channel="wechat")
                   if str(row.get("self_uid") or "") == self.grant.account_uid
                   and row.get("enabled", True) and not row.get("deleted_ms")
                   and row.get("state") in ("running", "degraded")]
        return matches[0] if len(matches) == 1 else None

    def accept_manifest(self, envelope):
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        validate_payload(payload, self.grant)
        if payload["op"] != "capture_ready":
            raise ProtocolError("CAPTURE_PRODUCER_INPUT_INVALID")
        if payload["nonce"] != self.nonce:
            return  # A delayed response cannot extend a newer handshake.
        now = self.clock()
        if not 0 < payload["lease_until"] - now <= LEASE_SECONDS:
            raise ProtocolError("CAPTURE_LEASE_INVALID")
        self.ready_until = payload["lease_until"]
        self.active_native_ids = frozenset(payload["active_native_ids"])

    def delivery_permitted(self, envelope):
        if self.authorized_guard() is not True:
            return False
        payload = envelope.get("payload", {})
        if payload.get("op") == "data_ack":
            return True
        try:
            validate_payload(payload, self.grant)
        except (ProtocolError, TypeError, ValueError):
            return False
        if payload["op"] == "capture_hello":
            return True
        if not self.clock() < self.ready_until or self.online_guard() is not True or self._account() is None:
            return False
        event = payload.get("event")
        if event:
            return event["session"]["native_id"] in self.active_native_ids
        with self.db() as db:
            row = db.execute("SELECT native_id FROM capture_message WHERE id=?", (payload.get("capture_id"),)).fetchone()
        return bool(row and row[0] in self.active_native_ids)

    def request_backfill(self, native_id, ext_msg_id):
        if (native_id not in self.grant.selected_native_ids or not isinstance(ext_msg_id, str)
                or not ext_msg_id or len(ext_msg_id) > 512 or not ext_msg_id.isprintable()):
            raise ProtocolError("CAPTURE_BACKFILL_SCOPE_INVALID")
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO capture_backfill VALUES(?,?,'pending',NULL)", (native_id, ext_msg_id))

    def _snapshot(self, message_id, source_seq, historical):
        account = self._account()
        message = self.store.get_message(message_id)
        if not account or not message or message["account_id"] != account["id"]:
            raise ProtocolError("CAPTURE_NATIVE_ACCOUNT_CHANGED")
        identity = self.store.native_identity(account["id"])
        if (message.get("identity_epoch", 0) != identity.get("epoch", 0)
                or type(source_seq) is not int or source_seq < 1
                or message.get("source") != "chatlog" or not message.get("ext_msg_id")):
            raise ProtocolError("CAPTURE_NATIVE_IDENTITY_UNVERIFIED")
        session = self.store.con.execute("SELECT native_id,kind,name FROM sessions WHERE id=?", (message["session_id"],)).fetchone()
        if (not session or session["kind"] != "group" or session["native_id"] not in self.grant.selected_native_ids
                or message["session_id"] != account["id"] + ":" + session["native_id"]):
            raise ProtocolError("CAPTURE_NATIVE_GROUP_NOT_SELECTED")
        native = session["native_id"]
        peer = peer_for(native)
        cid = capture_id(self.grant, peer, message["ext_msg_id"])
        recalled = bool(message.get("revoked"))
        event = {"event_id": "capture-"+cid, "peer_id": peer, "ext_msg_id": message["ext_msg_id"],
                 "kind": "retract" if recalled else "message", "type": message["type"],
                 "direction": message["dir"], "text": message.get("text") or "", "source_seq": source_seq,
                 "occurred_at": message["ts_ms"] / 1000, "channel": "wechat", "native_account_uid": self.grant.account_uid,
                 "native_message_id": message["id"], "identity_epoch": identity.get("epoch", 0),
                 "session": {"native_id": native, "kind": "group", "name": session["name"] or native},
                 "sender": {"id": message.get("sender_id"), "name": message.get("sender_name")},
                 "historical": bool(historical), "is_recalled": recalled, "attribution": {"status": "unresolved"},
                 "media_state": "ready", "media": [], "attachments": []}
        blobs = {}
        if message["type"] == "image" and not recalled:
            event["media_state"] = "pending"
            if self.media_provider:
                try:
                    facts = self.media_provider({**event, "media": json.loads(message.get("media_json") or "[]")})
                except Exception:
                    self.error = "CAPTURE_MEDIA_UNAVAILABLE"
                    facts = {"media_state": "pending", "media": [], "attachments": []}
                if not isinstance(facts, dict):
                    raise ProtocolError("CAPTURE_MEDIA_PROVIDER_INVALID")
                attachments = []
                for item in facts.get("attachments", []):
                    attachment = {key: item[key] for key in ("name", "sha256", "size", "content_base64", "mime", "quality") if key in item}
                    mime = attachment.get("mime") or mimetypes.guess_type(attachment.get("name", ""))[0]
                    if mime:
                        attachment["mime"] = mime
                    attachments.append(attachment)
                event["attachments"] = attachments
                event["media"] = [{key: item[key] for key in ("kind", "state", "sha256", "size", "mime", "quality") if key in item}
                                  for item in facts.get("media", [])]
                state = facts.get("media_state", "pending")
                event["media_state"] = state if attachments else "pending"
                blobs = dict(facts.get("_media_blobs", {}))
        try:
            make_payload(self.grant, "capture_fact", capture_id=cid, content_revision=1, media_revision=0, event=event)
        except ProtocolError as exc:
            if event["type"] != "image" or recalled:
                raise
            # Invalid attachment bytes must not poison capture of later facts.
            # Keep the verified native metadata waiting for a valid media retry.
            pending = {**event, "media_state": "pending", "media": [], "attachments": []}
            make_payload(self.grant, "capture_fact", capture_id=cid, content_revision=1, media_revision=0, event=pending)
            self.error = str(exc)
            event, blobs = pending, {}
        return cid, event, blobs

    def _stage(self, message_id, source_seq, historical):
        cid, event, blobs = self._snapshot(message_id, source_seq, historical)
        content_hash = digest({key: event[key] for key in ("kind", "type", "direction", "text", "is_recalled")})
        media_hash = digest({key: event[key] for key in ("media_state", "media", "attachments")})
        with self.db() as db:
            old = db.execute("SELECT * FROM capture_message WHERE id=?", (cid,)).fetchone()
            if old and old["content_hash"] == content_hash and old["media_hash"] == media_hash:
                db.execute("UPDATE capture_message SET message_id=?,source_seq=MAX(source_seq,?),checked_at=? WHERE id=?",
                           (message_id, source_seq, self.clock(), cid))
                return
            content_revision = old["content_revision"] + int(old["content_hash"] != content_hash) if old else 1
            media_revision = old["media_revision"] + int(old["media_hash"] != media_hash) if old else int(bool(event["attachments"]))
            op = "capture_media" if old and old["content_hash"] == content_hash else "capture_fact"
            payload = make_payload(self.grant, op, capture_id=cid, content_revision=content_revision,
                                   media_revision=media_revision, event=event)
            packets = [payload]
            for attachment in event["attachments"]:
                if "content_base64" in attachment:
                    continue
                raw = blobs.get(attachment["sha256"])
                if not isinstance(raw, bytes) or len(raw) != attachment["size"] or hashlib.sha256(raw).hexdigest() != attachment["sha256"]:
                    raise ProtocolError("CAPTURE_MEDIA_SPOOL_UNVERIFIED")
                total = (len(raw)+CHUNK_BYTES-1)//CHUNK_BYTES
                for index in range(total):
                    packets.append(make_payload(self.grant, "capture_media_chunk", capture_id=cid,
                        content_revision=content_revision, media_revision=media_revision, sha256=attachment["sha256"],
                        size=len(raw), total=total, index=index,
                        data=base64.b64encode(raw[index*CHUNK_BYTES:(index+1)*CHUNK_BYTES]).decode("ascii")))
            for packet in packets:
                db.execute("INSERT OR IGNORE INTO capture_outbox(id,capture_id,payload) VALUES(?,?,?)",
                           ("capture-"+digest(packet), cid, canonical(packet).decode()))
            db.execute("""INSERT INTO capture_message VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET message_id=excluded.message_id,source_seq=MAX(source_seq,excluded.source_seq),
                    content_revision=excluded.content_revision,media_revision=excluded.media_revision,
                    content_hash=excluded.content_hash,media_hash=excluded.media_hash,event=excluded.event,checked_at=excluded.checked_at""",
                (cid, message_id, event["session"]["native_id"], event["ext_msg_id"], source_seq, int(historical),
                 content_revision, media_revision, content_hash, media_hash, json.dumps(event), self.clock()))

    def _scan(self):
        account = self._account()
        if not account or self.online_guard() is not True:
            return
        with self.db() as db:
            cursor = int(db.execute("SELECT value FROM capture_meta WHERE key='cursor'").fetchone()[0])
        rows = self.store.capture_sources(cursor, 200)
        for row in rows:
            # Do not consume the source while its active identity is changing.
            current_account = self._account()
            if self.online_guard() is not True or not current_account or current_account["id"] != account["id"]:
                return
            rejected = False
            if row["native_uid"] == self.grant.account_uid:
                value = json.loads(row["payload_json"])
                native = (value.get("session") or {}).get("native_id")
                selected = native in self.grant.selected_native_ids and value.get("type") in ("image", "text")
                if selected and (row["account_id"] != account["id"]
                                 or row["identity_epoch"] != self.store.native_identity(account["id"]).get("epoch", 0)):
                    self._quarantine(row, value, "CAPTURE_NATIVE_SOURCE_RELOCATED")
                    rejected = True
                message = self.store.get_message(row["message_id"]) if selected and not rejected else None
                if selected and not rejected and message is None and value.get("ext_msg_id"):
                    # A confirmed send can have merged the transient native row
                    # into its canonical message after the event was committed.
                    matches = self.store.con.execute("SELECT id FROM messages WHERE account_id=? AND ext_msg_id=?",
                                                     (account["id"], value["ext_msg_id"])).fetchall()
                    current = [self.store.get_message(item["id"]) for item in matches]
                    current = [item for item in current if item is not None]
                    message = current[0] if len(current) == 1 else None
                if selected and not rejected and message is None:
                    self._quarantine(row, value, "CAPTURE_NATIVE_SOURCE_UNAVAILABLE")
                    rejected = True
                if message and message.get("type") in ("text", "image") and message.get("ext_msg_id"):
                    native = message["session_id"].removeprefix(account["id"]+":")
                    if native in self.grant.selected_native_ids:
                        try:
                            self._stage(message["id"], row["seq"], bool(row["historical"]))
                        except ProtocolError as exc:
                            self.error = str(exc)
                            self._quarantine(row, value, str(exc))
                            rejected = True
            # A crash before this checkpoint simply replays the idempotent fact.
            if not rejected:
                with self.db() as db:
                    db.execute("UPDATE capture_meta SET value=? WHERE key='cursor'", (str(row["seq"]),))

    def _quarantine(self, row, message, code):
        source = dict(row)
        value = json.loads(source["payload_json"])
        message_id = source.get("message_id") or (message["id"] if message else value.get("id"))
        session_id = message.get("session_id", "") if message else (value.get("session") or {}).get("id", "")
        native = session_id.removeprefix(str(source["account_id"])+":")
        ext = message.get("ext_msg_id") if message else value.get("ext_msg_id")
        snapshot = {"source_kind": "native-capture-source-v1", "outbox": source,
                    "message": {key: item for key, item in value.items() if key != "raw_ref"}}
        with self.db() as db:
            db.execute("""INSERT INTO capture_rejected(source_seq,code,message_id,native_id,ext_msg_id,snapshot,state,checked_at)
                VALUES(?,?,?,?,?,?,'pending',?) ON CONFLICT(source_seq) DO UPDATE SET code=excluded.code,
                message_id=excluded.message_id,native_id=excluded.native_id,ext_msg_id=excluded.ext_msg_id,
                snapshot=excluded.snapshot,state='pending',checked_at=excluded.checked_at""",
                (source["seq"], code, message_id, native, ext, json.dumps(snapshot, ensure_ascii=False), self.clock()))
            # Quarantine and its consumed source watermark are one durable write.
            db.execute("UPDATE capture_meta SET value=? WHERE key='cursor'", (str(source["seq"]),))

    def rejected_sources(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("CAPTURE_DIAGNOSTIC_LIMIT_INVALID")
        with self.db() as db:
            rows = db.execute("SELECT source_seq,code,message_id,native_id,ext_msg_id,snapshot,state FROM capture_rejected ORDER BY source_seq LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None} for row in rows]

    def _retry_rejected(self):
        with self.db() as db:
            rows = db.execute("SELECT source_seq,message_id,native_id,ext_msg_id,snapshot FROM capture_rejected WHERE state='pending' ORDER BY checked_at,source_seq LIMIT 4").fetchall()
        for row in rows:
            if not row["message_id"]:
                continue
            try:
                # The protected snapshot is diagnostic, not authority to bypass
                # current account/epoch checks or fabricate a new native record.
                account = self._account()
                if not account:
                    return
                identity = self.store.native_identity(account["id"])
                matches = self.store.con.execute("SELECT id FROM messages WHERE account_id=? AND session_id=? AND ext_msg_id=? AND identity_epoch=?",
                    (account["id"], account["id"]+":"+row["native_id"], row["ext_msg_id"], identity.get("epoch", 0))).fetchall()
                if len(matches) != 1:
                    raise ProtocolError("CAPTURE_RECOVERY_NATIVE_UNAVAILABLE")
                message_id = matches[0]["id"]
                proof = self.store.con.execute("SELECT MAX(seq) FROM native_capture_source WHERE message_id=? AND native_uid=? AND identity_epoch=?",
                    (message_id, self.grant.account_uid, identity.get("epoch", 0))).fetchone()[0]
                if not proof:
                    raise ProtocolError("CAPTURE_RECOVERY_NATIVE_UNVERIFIED")
                self._stage(message_id, proof, True)
            except (ProtocolError, OSError, ValueError):
                with self.db() as db:
                    db.execute("UPDATE capture_rejected SET checked_at=? WHERE source_seq=?", (self.clock(), row["source_seq"]))
            else:
                with self.db() as db:
                    db.execute("UPDATE capture_rejected SET state='recovered',checked_at=?,recovered_seq=? WHERE source_seq=?", (self.clock(), proof, row["source_seq"]))

    def _backfill(self):
        with self.db() as db:
            pending = db.execute("SELECT native_id,ext_msg_id FROM capture_backfill WHERE state='pending' LIMIT 1").fetchall()
        for item in pending:
            try:
                if self.refresher:
                    self.refresher(self.grant, item["native_id"], item["ext_msg_id"])
                account = self._account()
                if not account:
                    return
                rows = self.store.con.execute("SELECT id FROM messages WHERE account_id=? AND session_id=? AND ext_msg_id=?",
                    (account["id"], account["id"]+":"+item["native_id"], item["ext_msg_id"])).fetchall()
                if len(rows) != 1:
                    raise ProtocolError("CAPTURE_BACKFILL_NATIVE_AMBIGUOUS")
                message_id = rows[0]["id"]
                source = self.store.con.execute("SELECT MAX(seq) FROM native_capture_source WHERE account_id=? AND message_id=? AND native_uid=? AND identity_epoch=?",
                    (account["id"], message_id, self.grant.account_uid, self.store.native_identity(account["id"]).get("epoch", 0))).fetchone()[0]
                if not source:
                    raise ProtocolError("CAPTURE_BACKFILL_SOURCE_MISSING")
                self._stage(message_id, source, True)
                with self.db() as db:
                    db.execute("UPDATE capture_backfill SET state='staged',error=NULL WHERE native_id=? AND ext_msg_id=?", tuple(item))
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
                with self.db() as db:
                    db.execute("UPDATE capture_backfill SET error=? WHERE native_id=? AND ext_msg_id=?", (self.error, *tuple(item)))

    def _refresh(self):
        with self.db() as db:
            rows = db.execute("SELECT message_id,source_seq,historical FROM capture_message ORDER BY checked_at,rowid LIMIT 20").fetchall()
        for row in rows:
            try:
                self._stage(row["message_id"], row["source_seq"], bool(row["historical"]))
            except (ProtocolError, OSError, ValueError) as exc:
                self.error = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
                with self.db() as db:
                    db.execute("UPDATE capture_message SET checked_at=? WHERE message_id=?", (self.clock(), row["message_id"]))

    def tick(self):
        with self.lock:
            if self.authorized_guard() is not True:
                self.ready_until = 0
                self.active_native_ids = frozenset()
                return
            self.error = None
            if self.clock() - self.last_hello >= 20:
                self.nonce = uuid.uuid4().hex
                payload = make_payload(self.grant, "capture_hello", nonce=self.nonce)
                self.adapter.publish_manifest(payload, message_id="capture-"+digest(payload))
                self.last_hello = self.clock()
            if self.adapter.poll(limit=4):
                raise ProtocolError("CAPTURE_NON_MANIFEST_REJECTED")
            if self.online_guard() is not True or self._account() is None:
                return
            self._scan()
            self._retry_rejected()
            self._backfill()
            self._refresh()
            if not self.clock() < self.ready_until or not self.active_native_ids:
                return
            with self.db() as db:
                placeholders = ",".join("?" for _ in self.active_native_ids)
                rows = db.execute("SELECT o.id,o.payload FROM capture_outbox o JOIN capture_message m ON m.id=o.capture_id "
                    "WHERE o.published=0 AND m.native_id IN ("+placeholders+") ORDER BY o.rowid LIMIT 4",
                    tuple(self.active_native_ids)).fetchall()
            sent = 0
            for row in rows:
                payload = json.loads(row["payload"])
                if not self.delivery_permitted({"payload": payload}):
                    continue
                self.adapter.publish_manifest(payload, message_id=row["id"])
                with self.db() as db:
                    db.execute("UPDATE capture_outbox SET published=1 WHERE id=?", (row["id"],))
                sent += 1
                if sent >= 4:
                    break

    def status(self):
        with self.db() as db:
            pending = db.execute("SELECT COUNT(*) FROM capture_outbox WHERE published=0").fetchone()[0]
            facts = db.execute("SELECT COUNT(*) FROM capture_message").fetchone()[0]
            rejected = db.execute("SELECT COUNT(*) FROM capture_rejected").fetchone()[0]
        return {"grant_id": self.grant.grant_id, "ready": self.clock() < self.ready_until,
                "lease_until": self.ready_until, "pending_packets": pending, "facts": facts,
                "rejected_sources": rejected, "error": self.error}


class WechatCaptureCoordinator:
    """Load explicit local authorizations and share only the existing file client."""
    def __init__(self, host, *, grant_path=None, refresher=None):
        self.host = host
        self.grant_path = grant_path if grant_path is not None else os.environ.get("QTRADE_WECHAT_CAPTURE_GRANTS", "")
        self.refresher = refresher
        self.producers = {}
        self.error = None

    def authorized(self, grant):
        try:
            grants, _ = load_grants_file(self.grant_path, self.host.keys.owner_id)
            return any(g.grant_id == grant.grant_id and g.workbench_id == self.host.workbench for g in grants)
        except (OSError, ValueError, TypeError):
            return False

    def _online(self, grant):
        rows = [r for r in self.host.rows() if r["channel"] == "wechat" and str(r["self_uid"]) == grant.account_uid
                and r["state"] in ("running", "degraded")]
        return len(rows) == 1 and self.host.online(rows[0]["id"], grant.account_uid)

    def tick(self):
        from server.wechat_capture_transport import CaptureTransport
        try:
            grants, backfill = load_grants_file(self.grant_path, self.host.keys.owner_id)
            if any(g.workbench_id != self.host.workbench for g in grants):
                raise ProtocolError("CAPTURE_WORKBENCH_NOT_LOCAL")
            wanted = {g.grant_id: g for g in grants}
            self.producers = {key: value for key, value in self.producers.items() if key in wanted}
            for identity, grant in wanted.items():
                if identity not in self.producers:
                    root = self.host.root/"wechat-capture"/identity
                    paths = capture_paths(grant)
                    binding = self.host.keys.binding(grant.workbench_id, grant.account_uid, channel="wechat")
                    adapter = CaptureTransport(binding, self.host.client(paths["capture_down"]),
                        self.host.client(paths["capture_up"]), root/"publication.sqlite3", role="collector",
                        institution_owner=self.host.keys.owner_id, clock=self.host.clock)
                    self.producers[identity] = WechatCaptureProducer(store=self.host.store, grant=grant, adapter=adapter,
                        root=root, media_provider=self.host.media_provider, clock=self.host.clock,
                        online_guard=lambda g=grant: self._online(g), refresher=self.refresher,
                        authorized_guard=lambda g=grant: self.authorized(g))
                producer = self.producers[identity]
                for request in backfill:
                    if request["grant_id"] == identity:
                        producer.request_backfill(request["native_id"], request["ext_msg_id"])
                producer.tick()
            self.error = None
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
            # Do not retain a cached grant as authority after a bad file update.
            for producer in self.producers.values():
                producer.ready_until = 0
                producer.active_native_ids = frozenset()

    def status(self):
        return {"configured": bool(self.grant_path), "error": self.error,
                "workers": [producer.status() for producer in list(self.producers.values())]}
