"""Durable native-history publication, separate from live negotiation watermarks."""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from ..adapters.qidian.history_native import NativeHistoryReader
from ..adapters.qidian.history_media import NativeMediaResolver, AdbMediaReader

class NativeHistoryPublisher:
    def __init__(self, root, source, adapter, *, clock=time.time, media_reader=None):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'native-history.sqlite3'
        self.reader = NativeHistoryReader(source); self.adapter = adapter; self.clock = clock
        self.media = NativeMediaResolver(self.root / 'media', media_reader or AdbMediaReader(source), source=source)
        self.next_table = 0
        self._retry_cursor = {}; self._retry_due = {}; self._last_prune = 0
        with self.db() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS history_cursor(name TEXT PRIMARY KEY,position INTEGER,anchor INTEGER); CREATE TABLE IF NOT EXISTS history_sent(native TEXT,direction TEXT,fingerprint TEXT,stamp REAL,PRIMARY KEY(native,direction)); CREATE TABLE IF NOT EXISTS history_retry(name TEXT,position INTEGER,reason TEXT,stamp REAL,PRIMARY KEY(name,position)); CREATE TABLE IF NOT EXISTS history_media(hash TEXT PRIMARY KEY,size INTEGER,published REAL); CREATE TABLE IF NOT EXISTS history_media_ref(native TEXT,direction TEXT,hash TEXT,stamp REAL,PRIMARY KEY(native,direction,hash));')
        self.path.chmod(0o600)
        self.stats = {'scanned': 0, 'published': 0, 'media_bytes': 0, 'unsupported': 0, 'last_read_at': None, 'last_publish_at': None, 'error': None}

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=20); db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db: yield db
        finally: db.close()

    def _publish_media(self, items):
        from relay.gateway_paths import CONTRACT, CHUNK_BYTES
        for item in items:
            if item.get('state') != 'ready': continue
            h, size = item['sha256'], item['size']
            with self.db() as db:
                old = db.execute('SELECT size FROM history_media WHERE hash=?', (h,)).fetchone()
            if old:
                if old[0] != size: raise ValueError('HISTORY_MEDIA_SIZE_CONFLICT')
                continue
            p = self.media.blobs.get(h) or self.media.root / h
            if not p.is_file() or p.stat().st_size != size: raise ValueError('HISTORY_MEDIA_SOURCE_LOST')
            hashing = hashlib.sha256()
            count = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
            with p.open('rb') as f:
                for pos in range(count):
                    raw = f.read(CHUNK_BYTES); hashing.update(raw)
                    self.adapter.publish_manifest({'contract': CONTRACT, 'op': 'media_chunk', 'sha256': h,
                        'size': size, 'position': pos, 'chunks': count, 'data': base64.b64encode(raw).decode('ascii')},
                        message_id='media-' + h + '-' + str(pos))
            if hashing.hexdigest() != h: raise ValueError('HISTORY_MEDIA_LOCAL_HASH_MISMATCH')
            with self.db() as db: db.execute('INSERT OR IGNORE INTO history_media VALUES(?,?,?)', (h, size, self.clock()))
            self.stats['media_bytes'] += size

    def _publish_rows(self, rows):
        from relay.gateway_paths import CONTRACT
        from relay.protocol import canonical, digest
        groups, batch, size = [], [], 0
        for message in rows:
            n = len(canonical(message))
            if batch and (size + n > 512 * 1024 or len(batch) >= 100):
                groups.append(batch); batch, size = [], 0
            batch.append(message); size += n
        if batch: groups.append(batch)
        for items in groups:
            for message in items: self._publish_media(message['media'])
            body = {'contract': CONTRACT, 'op': 'history', 'items': items}
            self.adapter.publish_manifest(body, message_id='history-' + digest(body))
            with self.db() as db:
                for message in items:
                    db.execute('INSERT INTO history_sent VALUES(?,?,?,?) ON CONFLICT(native,direction) DO UPDATE SET fingerprint=excluded.fingerprint,stamp=excluded.stamp',
                        (message['native_id'], message['direction'], digest(message), message['occurred_at']))
                    db.execute('DELETE FROM history_media_ref WHERE native=? AND direction=?', (message['native_id'],message['direction']))
                    for item in message['media']:
                        if item.get('state') == 'ready':
                            db.execute('INSERT INTO history_media_ref VALUES(?,?,?,?)', (message['native_id'],message['direction'],item['sha256'],message['occurred_at']))
            self.stats['published'] += len(items); self.stats['last_publish_at'] = self.clock()

    def tick(self, *, limit=50):
        from relay.protocol import digest
        if self.clock() - self._last_prune >= 3600:
            result = self.prune()
            self._last_prune = self.clock() - (3600 if result['more_pending'] else 0)
        tables = self.reader.tables()
        self.stats['last_read_at'] = self.clock()
        if not tables: return dict(self.stats)
        table = tables[self.next_table % len(tables)]
        self.next_table = (self.next_table + 1) % len(tables)
        with self.db() as db:
            cursor = db.execute('SELECT position,anchor FROM history_cursor WHERE name=?', (table.name,)).fetchone()
            next_retry = self._retry_cursor.get(table.name, 0)
            retry = db.execute('SELECT position FROM history_retry WHERE name=? AND position>? ORDER BY position LIMIT 1', (table.name, next_retry)).fetchone()
            if retry is None: retry = db.execute('SELECT position FROM history_retry WHERE name=? ORDER BY position LIMIT 1', (table.name,)).fetchone()
        after = cursor['position'] if cursor else 0
        reset = bool(cursor and self.reader.anchor(table, after) != cursor['anchor'])
        if reset:
            after = 0  # Only this backfill cursor; live capture cursors are untouched.
        raw = self.reader.page(table, after=after, now=self.clock(), limit=limit)
        # New native rows always advance first. Failed old media is a bounded,
        # separate pass and cannot keep the first page at the head forever.
        if not raw and retry and self.clock() >= self._retry_due.get(table.name, 0):
            self._retry_cursor[table.name] = retry['position']
            self._retry_due[table.name] = self.clock() + 30
            raw = self.reader.page(table, after=max(0, retry['position'] - 1), now=self.clock(), limit=1)
            if not raw or raw[0].id != retry['position']:
                with self.db() as db: db.execute('DELETE FROM history_retry WHERE name=? AND position=?', (table.name, retry['position']))
                raw = []
        if not raw: return dict(self.stats)
        pending, issues = [], []
        for row in raw:
            self.stats['scanned'] += 1
            message, issue = self.reader.message(table, row, self.media)
            if issue:
                issues.append((row.id, issue, row.time)); self.stats['unsupported'] += 1
            if message is None: continue
            with self.db() as db:
                sent = db.execute('SELECT fingerprint FROM history_sent WHERE native=? AND direction=?', (message['native_id'], message['direction'])).fetchone()
            if not sent or sent[0] != digest(message): pending.append(message)
        self._publish_rows(pending)
        # A publication failure above does not advance even this independent cursor.
        with self.db() as db:
            if reset: db.execute('DELETE FROM history_cursor WHERE name=?', (table.name,))
            db.execute('INSERT INTO history_cursor VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET position=MAX(position,excluded.position),anchor=CASE WHEN excluded.position>=position THEN excluded.anchor ELSE anchor END',
                       (table.name, raw[-1].id, raw[-1].uniseq))
            bad = {row[0] for row in issues}
            for row in raw:
                if row.id not in bad: db.execute('DELETE FROM history_retry WHERE name=? AND position=?', (table.name, row.id))
            for row_id, reason, stamp in issues:
                if reason != 'HISTORY_UNSUPPORTED_MESSAGE_TYPE':
                    db.execute('INSERT INTO history_retry VALUES(?,?,?,?) ON CONFLICT(name,position) DO UPDATE SET reason=excluded.reason', (table.name, row_id, reason, stamp))
        self.stats['error'] = issues[-1][1] if issues else None
        return dict(self.stats)

    def prune(self, *, limit=200):
        """Bound local history caches only; native/transport command journals are untouched."""
        import re
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('HISTORY_PRUNE_LIMIT_INVALID')
        cutoff = self.clock() - 7 * 86400
        result = {'history_removed': 0, 'retry_removed': 0, 'media_removed': 0, 'more_pending': False}
        with self.db() as db:
            for table, key in (('history_sent','history_removed'),('history_retry','retry_removed')):
                result[key] = db.execute('DELETE FROM '+table+' WHERE rowid IN (SELECT rowid FROM '+table+' WHERE stamp<? LIMIT ?)', (cutoff,limit)).rowcount
                result['more_pending'] |= bool(db.execute('SELECT 1 FROM '+table+' WHERE stamp<? LIMIT 1',(cutoff,)).fetchone())
            db.execute('DELETE FROM history_media_ref WHERE rowid IN (SELECT rowid FROM history_media_ref WHERE stamp<? LIMIT ?)',(cutoff,limit))
            sql = ('SELECT hash FROM history_media m WHERE published<? AND NOT EXISTS '
                   '(SELECT 1 FROM history_media_ref r WHERE r.hash=m.hash AND r.stamp>=?)')
            rows = db.execute(sql+' LIMIT ?', (cutoff,cutoff,limit)).fetchall()
            for row in rows:
                h = row['hash']
                if not isinstance(h,str) or re.fullmatch('[a-f0-9]{64}',h) is None:
                    raise ValueError('HISTORY_MEDIA_CACHE_ID_INVALID')
                path = self.media.root / h
                if path.is_symlink():
                    raise ValueError('HISTORY_MEDIA_CACHE_LINK_REJECTED')
                path.unlink(missing_ok=True)
                self.media.blobs.pop(h,None)
                db.execute('DELETE FROM history_media WHERE hash=?',(h,))
                result['media_removed'] += 1
            result['more_pending'] |= bool(db.execute(sql+' LIMIT 1',(cutoff,cutoff)).fetchone())
            result['more_pending'] |= bool(db.execute('SELECT 1 FROM history_media_ref WHERE stamp<? LIMIT 1',(cutoff,)).fetchone())
        return result

    def status(self):
        with self.db() as db:
            pending = db.execute('SELECT reason,count(*) n FROM history_retry GROUP BY reason').fetchall()
        return {**self.stats, 'retention_days': 7, 'source': 'qidian_main_database', 'unresolved_media': [dict(r) for r in pending]}