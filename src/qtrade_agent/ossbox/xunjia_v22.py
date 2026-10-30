"""Current V2.2 collector core. It never starts an IM account or a native sender.

The existing Agent owns this worker and its event hook. The institution collector
supplies the transport; this core owns guarded receiver execution and durable state.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

log = logging.getLogger('qtrade.ossbox.xunjia_v22')

REPLY_WIRE_KEYS = ('state', 'text', 'source_text', 'source_direction', 'sender_id', 'ts', 'source_ext_msg_id')
REPLY_SOURCE_TEXT_MAX = 4000
_reply_warned = set()


def _reply_warn_once(reason):
    # Reason codes only: quote text and source text must never reach the log.
    if reason not in _reply_warned:
        _reply_warned.add(reason)
        log.warning('Qidian reply omitted from V2.2 event: %s', reason)


def _optional_str(value):
    return value if isinstance(value, str) else None


class XunjiaV22Host:
    def __init__(self, *, store, bus, loop, binding, bridge, port, state_path,
                 legacy_inbox_disabled, media_provider=None):
        if not callable(legacy_inbox_disabled):
            raise ValueError('invalid_collector_execution_policy')
        if not legacy_inbox_disabled():
            raise RuntimeError('legacy_xunjia_inbox_must_be_disabled')
        if getattr(bus, 'supports_xunjia_v22_guard', False) is not True:
            raise RuntimeError('qtrade_v22_native_guard_not_installed')
        if bridge.binding != binding or port.binding != binding:
            raise ValueError('collector_binding_mismatch')
        self.store, self.bus, self.loop = store, bus, loop
        self.binding, self.bridge, self.port = binding, bridge, port
        self.ledger = bridge.ledger
        self.legacy_inbox_disabled = legacy_inbox_disabled
        self.media_provider = media_provider
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active = False
        self._intake_ok = True
        self._worker = None
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self.port.execution_permission = self.permission
        quiescent = getattr(self.port, 'quiescent', None)
        self.bridge.native_quiescent = quiescent if callable(quiescent) else None
        self.bridge.native_send = self._send
        self.bridge.native_reconcile = self.port.native_reconcile
        identity = json.dumps([binding.owner_user_id, binding.account_uid, binding.workbench_id] + ([self.port.channel] if self.port.channel != 'qidian' else []))
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS host_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS host_event (event_id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS host_media (event_id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '', checked_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS host_media_chunk (sha256 TEXT NOT NULL, route TEXT NOT NULL,
                    position INTEGER NOT NULL, PRIMARY KEY(sha256,route,position));
                CREATE TABLE IF NOT EXISTS host_native (native_key TEXT PRIMARY KEY, original_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL, revoked INTEGER NOT NULL, outbox_seq INTEGER NOT NULL DEFAULT 0);
            ''')
            old = db.execute("SELECT value FROM host_meta WHERE key='binding'").fetchone()
            if old is not None and old['value'] != identity:
                raise ValueError('collector_state_belongs_to_another_binding')
            db.execute("INSERT OR IGNORE INTO host_meta VALUES('binding',?)", (identity,))
            if db.execute("SELECT 1 FROM host_meta WHERE key='installation_seq'").fetchone() is None:
                bounds = self.store.outbox_seq_bounds() if hasattr(self.store, 'outbox_seq_bounds') else (None, None)
                db.execute("INSERT INTO host_meta VALUES('installation_seq',?)", (str(bounds[1] or 0),))
            db.execute("INSERT OR IGNORE INTO host_meta VALUES('replayed_seq','0')")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.state_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def permission(self, command):
        return bool(self._active and self._intake_ok
                    and self.legacy_inbox_disabled() and self.ledger.permission(command) is True
                    and all(getattr(adapter, 'receive_ready', lambda: True)() for adapter in getattr(self.bridge, 'adapters', {}).values()))

    def _send(self, command):
        if command.get('business_version') != '2.2':
            return {'status': 'failed', 'evidence': 'collector-requires-business-version-2.2'}
        if not self._active or not self._intake_ok or not self.legacy_inbox_disabled():
            return {'status': 'failed', 'evidence': 'qtrade-v2-native-disabled'}
        return self.port.native_send(command)

    def get_session_reset_status(self, account_id):
        """Read the shared ledger only for this port's unique bound account."""
        account = self.port._account()
        if (not account or account.get('id') != account_id
                or account.get('channel') != self.port.channel
                or str(account.get('self_uid')) != self.binding.account_uid):
            return None
        current = self.ledger.reset_status()
        epoch = self.ledger.account_epoch()
        if type(epoch) is not int or epoch < 0:
            raise ValueError('session_reset_status_unavailable')
        reset = None
        if current is None:
            if epoch != 0:
                raise ValueError('session_reset_status_unavailable')
        else:
            phases = {'waiting_native', 'barrier_ready', 'complete', 'blocked'}
            if (not isinstance(current, dict) or current.get('phase') not in phases
                    or type(current.get('account_epoch')) is not int
                    or current['account_epoch'] != epoch):
                raise ValueError('session_reset_status_unavailable')
            if current['phase'] == 'complete':
                fingerprint = current.get('snapshot_hash')
                if (not isinstance(current.get('reset_id'), str) or not current['reset_id']
                        or not isinstance(fingerprint, str) or len(fingerprint) != 64
                        or set(fingerprint) - set('0123456789abcdef')
                        or not isinstance(current.get('snapshot'), dict)
                        or current['snapshot'].get('account_epoch') != epoch):
                    raise ValueError('session_reset_status_unavailable')
            reset = {key: current.get(key) for key in
                     ('reset_id', 'phase', 'account_epoch', 'snapshot_hash', 'snapshot', 'code')}
        return {'available': True, 'account_id': account_id,
                'workbench_id': self.binding.workbench_id, 'account_uid': self.binding.account_uid,
                'account_epoch': epoch, 'reset': reset}

    def observe(self, *, event, payload, account_id=None, event_id='', channel=None,
                trace_id=None, now_ms=None, historical=False, source_seq=0):
        if event != 'message' or channel != self.port.channel:
            return
        try:
            # Event payload is already the committed QTrade message snapshot.
            row = dict(payload)
            row['account_id'] = account_id or row.get('account_id')
            row['session_id'] = (row.get('session') or {}).get('id', row.get('session_id'))
            if 'ts_ms' not in row:
                row['ts_ms'] = datetime.fromisoformat(row['ts'].replace('Z', '+00:00')).timestamp() * 1000
            bound = self.port._account()
            if not bound or row['account_id'] != bound['id']:
                return
            if not source_seq and event_id and hasattr(self.store, 'con'):
                position = self.store.con.execute("SELECT seq FROM events_outbox WHERE target='ws' AND event_id=?", (event_id,)).fetchone()
                source_seq = position['seq'] if position else 0
            identity = getattr(self.store, 'native_identity', lambda _: {})(bound['id'])
            if identity and (not source_seq or source_seq <= identity.get('outbox_seq', 0)):
                return
            row['source_seq'] = source_seq
            row['native_account_uid'] = payload.get('native_account_uid', str(bound['self_uid']))
            row['identity_epoch'] = payload.get('identity_epoch', identity.get('epoch', 0))
            lookup = getattr(self.store, 'get_message', None)
            if callable(lookup):
                message = lookup(row.get('id')) if row.get('id') else None
                if (message is None or message.get('identity_epoch', 0) != identity.get('epoch', 0)
                        or message.get('ext_msg_id') != row.get('ext_msg_id')
                        or message.get('dir') != row.get('dir') or message.get('session_id') != row.get('session_id')):
                    return
            data = self.port.normalize_native_message(row, live=not historical)
            if self.port.channel == 'qidian':
                data['reply'] = self._wire_reply(payload, row, bound)
            native_key = json.dumps([data['peer_id'], data['direction'], data['ext_msg_id']])
            text_hash = hashlib.sha256(data['text'].encode()).hexdigest()
            revoked = int(bool(row.get('revoked')))
            if not source_seq and event_id and hasattr(self.store, 'con'):
                position = self.store.con.execute("SELECT seq FROM events_outbox WHERE target='ws' AND event_id=?", (event_id,)).fetchone()
                source_seq = position['seq'] if position else 0
            with self._lock, self._db() as db:
                previous = db.execute('SELECT * FROM host_native WHERE native_key=?', (native_key,)).fetchone()
                if previous and source_seq and source_seq <= previous['outbox_seq']:
                    return
                if previous and previous['text_hash'] == text_hash and previous['revoked'] == revoked:
                    db.execute('UPDATE host_native SET outbox_seq=MAX(outbox_seq,?) WHERE native_key=?', (source_seq, native_key))
                    return
                original = previous['original_id'] if previous else data['event_id']
                if revoked or previous:
                    kind = 'retract' if revoked else 'edit'
                    data.update(kind=kind, target_event_id=original)
                    data['event_id'] = 'qtrade-' + hashlib.sha256(json.dumps(
                        [original, kind, event_id or text_hash], separators=(',', ':')).encode()).hexdigest()
                # This barrier is synchronous and local: never queue network work first.
                if data['direction'] == 'in' and not data['historical']:
                    self.ledger.begin_inbound(data['event_id'], data['peer_id'])
                elif data['direction'] == 'out':
                    self.ledger.stage_out(data, self.bridge.echo_wait_seconds)
                db.execute('INSERT OR IGNORE INTO host_event(event_id,body) VALUES(?,?)',
                    (data['event_id'], json.dumps(data, ensure_ascii=False)))
                db.execute('INSERT INTO host_native VALUES(?,?,?,?,?) ON CONFLICT(native_key) DO UPDATE SET text_hash=excluded.text_hash,revoked=excluded.revoked,outbox_seq=excluded.outbox_seq',
                    (native_key, original, text_hash, revoked, source_seq))
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self._wake.set)
        except Exception:
            self._intake_ok = False
            log.error('V2.2 native intake failed; automatic native sending blocked')
            raise

    def _wire_reply(self, payload, row, bound):
        """Wire form of a Qidian event's quote (06 2.9.5, R6-117); None on any anomaly, never raises.

        The result is not part of text_hash, native_key or event_id.
        """
        try:
            reply = payload.get('reply')
            if reply is None:
                return None
            if not isinstance(reply, dict):
                _reply_warn_once('not_object')
                return None
            state = reply.get('state')
            if state not in ('ok', 'unparsed'):
                _reply_warn_once('bad_state')
                return None
            wire = dict.fromkeys(REPLY_WIRE_KEYS)
            wire['state'] = state
            if state == 'unparsed':
                return wire
            sender = reply.get('sender')
            wire.update(text=_optional_str(reply.get('text')),
                        sender_id=_optional_str(sender.get('id')) if isinstance(sender, dict) else None,
                        ts=_optional_str(reply.get('ts')),
                        source_ext_msg_id=_optional_str(reply.get('source_ext_msg_id')))
            source = self._reply_source(reply.get('resolved_message_id'), row)
            if source is not None:
                text = source.get('text')
                wire['source_text'] = text[:REPLY_SOURCE_TEXT_MAX] if isinstance(text, str) and text else None
                wire['source_direction'] = source['dir']
            elif wire['sender_id'] is not None:
                wire['source_direction'] = 'out' if wire['sender_id'] == str(bound['self_uid']) else 'in'
            return wire
        except Exception as exc:
            _reply_warn_once('error:' + type(exc).__name__)
            return None

    def _reply_source(self, resolved_message_id, row):
        """The quoted message row, only via resolved_message_id within the event's account/session/epoch."""
        if not isinstance(resolved_message_id, str) or not resolved_message_id:
            return None
        lookup = getattr(self.store, 'get_message', None)
        if not callable(lookup):
            return None
        source = lookup(resolved_message_id)
        if (not isinstance(source, dict) or source.get('account_id') != row['account_id']
                or source.get('session_id') != row['session_id']
                or source.get('identity_epoch', 0) != row['identity_epoch']
                or source.get('dir') not in ('in', 'out')):
            return None
        return source

    def _replay(self):
        if not hasattr(self.store, 'con'):
            return
        with self._db() as db:
            cursor = int(db.execute("SELECT value FROM host_meta WHERE key='replayed_seq'").fetchone()['value'])
            installed = int(db.execute("SELECT value FROM host_meta WHERE key='installation_seq'").fetchone()['value'])
        rows = self.store.con.execute("SELECT seq,event_id,event,account_id,channel,payload_json,ts_ms FROM events_outbox WHERE target='ws' AND seq>? ORDER BY seq LIMIT 200", (cursor,)).fetchall()
        for row in rows:
            self.observe(event=row['event'], payload=json.loads(row['payload_json']), account_id=row['account_id'],
                channel=row['channel'], event_id=row['event_id'], now_ms=row['ts_ms'], historical=row['seq'] <= installed, source_seq=row['seq'])
            with self._db() as db:
                db.execute("UPDATE host_meta SET value=? WHERE key='replayed_seq'", (str(row['seq']),))

    def _flush_native(self):
        self._replay()
        with self._db() as db:
            rows = db.execute('SELECT event_id,body FROM host_event WHERE published=0 ORDER BY rowid LIMIT 200').fetchall()
        for row in rows:
            data = json.loads(row['body'])
            account = self.port._account()
            identity = getattr(self.store, 'native_identity', lambda _: {})(account['id']) if account else {}
            if (data.get('native_account_uid') != self.binding.account_uid
                    or data.get('identity_epoch') != identity.get('epoch', 0)
                    or data.get('channel') != self.port.channel):
                # Preserve unverifiable pre-upgrade work without replaying it as a new login.
                self._intake_ok = False
                log.error('Native pending identity is unverified; automatic sending blocked')
                continue
            if data.get('media') and callable(self.media_provider):
                from relay.protocol import digest
                data.update(self._prepare_media(data))
                data['media_manifest_id'] = 'native-media-' + digest([self.binding.account_uid, self.port.channel, data['event_id']])
                with self._db() as db:
                    db.execute('INSERT OR IGNORE INTO host_media(event_id,body) VALUES(?,?)',
                               (data['event_id'], json.dumps(data, ensure_ascii=False)))
                    db.execute('UPDATE host_event SET body=? WHERE event_id=?',
                               (json.dumps(data, ensure_ascii=False), data['event_id']))
                self._publish_media_chunks(data)
            self.bridge.publish_native(data, historical=data.get('historical', False))
            with self._db() as db:
                db.execute('UPDATE host_event SET published=1 WHERE event_id=?', (row['event_id'],))
        self._flush_media()

    def _prepare_media(self, event):
        facts = self.media_provider(event)
        blobs = facts.pop('_media_blobs', {})
        root = self.state_path.parent / 'native-media'
        for sha, raw in blobs.items():
            if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError('NATIVE_MEDIA_HASH_MISMATCH')
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / sha
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    raise ValueError('NATIVE_MEDIA_SPOOL_CORRUPT')
            else:
                temporary = root / (sha + '.pending')
                with temporary.open('wb') as out:
                    out.write(raw); out.flush()
                    import os
                    os.fsync(out.fileno())
                temporary.replace(path)
                path.chmod(0o600)
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        # A restart or MediaStore expiration cannot erase already spooled bytes.
        previous = event.get('attachments', [])
        if facts.get('media_state') == 'pending' and previous and len(previous) == len(event.get('media', [])):
            valid = True
            for item in previous:
                if item.get('content_base64'):
                    continue
                sha = item.get('sha256', '')
                path = root / sha if len(sha) == 64 and not set(sha) - set('0123456789abcdef') else None
                if path is None or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                    valid = False; break
            if valid:
                facts = {'attachments': previous, 'media': event['media'],
                         'media_state': 'chunked' if any('content_base64' not in a for a in previous) else 'ready'}
        return facts

    def _publish_media_chunks(self, event):
        import base64
        from relay.gateway_paths import CONTRACT, CHUNK_BYTES, MAX_FILE_BYTES
        for item in event.get('attachments', []):
            if 'content_base64' in item:
                continue
            sha, size = item.get('sha256'), item.get('size')
            if (not isinstance(sha, str) or len(sha) != 64 or set(sha) - set('0123456789abcdef')
                    or type(size) is not int or not 0 < size <= MAX_FILE_BYTES):
                raise ValueError('NATIVE_MEDIA_SOURCE_INVALID')
            path = self.state_path.parent / 'native-media' / sha
            if not path.is_file() or path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError('NATIVE_MEDIA_SOURCE_LOST')
            count = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
            for route, adapter in self.bridge.adapters.items():
                with path.open('rb') as source:
                    for position in range(count):
                        raw = source.read(CHUNK_BYTES)
                        with self._db() as db:
                            sent = db.execute('SELECT 1 FROM host_media_chunk WHERE sha256=? AND route=? AND position=?',
                                              (sha, route, position)).fetchone()
                        if sent:
                            continue
                        adapter.publish_manifest({'contract': CONTRACT, 'op': 'media_chunk', 'sha256': sha,
                            'size': size, 'position': position, 'chunks': count, 'data': base64.b64encode(raw).decode('ascii')},
                            message_id='media-' + sha + '-' + str(position))
                        with self._db() as db:
                            db.execute('INSERT OR IGNORE INTO host_media_chunk VALUES(?,?,?)', (sha, route, position))

    def _flush_media(self):
        if not callable(self.media_provider):
            return
        from relay.protocol import digest
        from relay.gateway_paths import CONTRACT
        with self._db() as db:
            pending = db.execute('SELECT * FROM host_media ORDER BY checked_at,rowid LIMIT 20').fetchall()
        for row in pending:
            event = json.loads(row['body'])
            facts = self._prepare_media(event)
            self._publish_media_chunks({**event, **facts})
            payload = {"contract": CONTRACT, "op": "native_media", "event_id": event['event_id'],
                       "ext_msg_id": event['ext_msg_id'], "peer_id": event['peer_id'],
                       "channel": self.port.channel, "media_manifest_id": event['media_manifest_id'], **facts}
            fingerprint = digest(payload)
            if fingerprint != row['fingerprint']:
                for adapter in self.bridge.adapters.values():
                    adapter.publish_manifest(payload, message_id=event['media_manifest_id'] + '-' + fingerprint)
            with self._db() as db:
                if facts.get('media_state') in ('ready', 'chunked'):
                    db.execute('DELETE FROM host_media WHERE event_id=?', (row['event_id'],))
                else:
                    db.execute('UPDATE host_media SET fingerprint=?,checked_at=?,body=? WHERE event_id=?',
                               (fingerprint, time.time(), json.dumps({**event, **facts}, ensure_ascii=False), row['event_id']))

    async def run_once(self):
        async with self._tick_lock:
            await asyncio.to_thread(self._flush_native)
            result = await asyncio.to_thread(self.bridge.run_once)
            return result

    async def _run(self):
        while self._active:
            try:
                await self.run_once()
            except Exception:
                log.warning('V2.2 collector work remains durable; retry on next tick')
            if not self._active:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def start(self):
        if self._worker is not None and not self._worker.done():
            return
        if not self.legacy_inbox_disabled():
            raise RuntimeError('legacy_xunjia_inbox_must_be_disabled')
        self._active = True
        self._worker = asyncio.create_task(self._run(), name='xunjia-v22-collector')

    async def stop(self):
        self._active = False
        self._wake.set()
        if self._worker is not None:
            # Do not cancel a to_thread native invocation and pretend it never ran.
            await asyncio.wait_for(asyncio.shield(self._worker), timeout=120)
            self._worker = None
