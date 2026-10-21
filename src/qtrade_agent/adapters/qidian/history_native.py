"""Bounded seven-day reads from the Qidian main database; never live cursors."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import re
from .maindb import MainDbError
from .msgdata import MainDbRow, TEXT_FAMILY, MIXED_TEXT_IMAGE, decode
from .xor import xor_hex

TABLE = re.compile(r"mr_(friend|troop)_([A-F0-9]{32})_New")
HISTORY_SECONDS = 7 * 86400

@dataclass(frozen=True)
class HistoryTable:
    name: str
    peer_id: str
    kind: str

class NativeHistoryReader:
    def __init__(self, source):
        self.source = source

    def tables(self):
        if not self.source.exists():
            raise MainDbError("open_failed", "history source unavailable")
        result = []
        for name in sorted(self.source.list_message_tables()):
            match = TABLE.fullmatch(name)
            if match is None:
                continue
            last = self.source.last_row_peer(name)
            if last is None:
                continue
            try:
                peer = xor_hex(last[0]).decode("ascii")
            except (ValueError, UnicodeError):
                raise MainDbError("schema_mismatch", "history peer decode failed") from None
            if not peer.isdecimal() or hashlib.md5(peer.encode()).hexdigest().upper() != match[2]:
                raise MainDbError("schema_mismatch", "history table identity mismatch")
            group = int(last[1]) == 1
            if int(last[1]) not in (0, 1) or group != (match[1] == "troop"):
                raise MainDbError("schema_mismatch", "history session kind mismatch")
            result.append(HistoryTable(name, "g_" + peer if group else peer, "group" if group else "private"))
        return result

    def page(self, table, *, after=0, now, limit=100):
        if not isinstance(table, HistoryTable) or TABLE.fullmatch(table.name) is None:
            raise ValueError("HISTORY_TABLE_INVALID")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("HISTORY_PAGE_INVALID")
        since = max(0, int(now) - HISTORY_SECONDS)
        sql = (f'SELECT _id,issend,time,msgtype,uniseq,hex(senderuin),hex(msgData) FROM "{table.name}" '
               f'WHERE _id>{after} AND time>={since} AND time<={int(now)+60} ORDER BY _id LIMIT {limit}')
        query = getattr(self.source, "_run", None) or getattr(self.source, "_sql", None)
        if not callable(query):
            raise MainDbError("open_failed", "history read backend unavailable")
        rows = query(sql)
        result = []
        for r in rows:
            if len(r) != 7:
                raise MainDbError("schema_mismatch", "history row shape invalid")
            result.append(MainDbRow(*(int(r[i]) for i in range(5)), r[5] or "", r[6] or ""))
        return result

    def anchor(self, table, row_id):
        return self.source.uniseq_at(table.name, row_id) if row_id else None

    def message(self, table, row, media_resolver=None):
        raw = xor_hex(row.msgdata_hex)
        def resolve():
            scoped = getattr(media_resolver, "for_message", None)
            return scoped(table, row, raw) if callable(scoped) else media_resolver(row.msgtype, raw)
        text = ""
        kind = "text"
        media = []
        issue = None
        if row.msgtype in TEXT_FAMILY or row.msgtype == MIXED_TEXT_IMAGE:
            value = decode(row.msgtype, raw)
            text = value.text or ""
            if row.msgtype == MIXED_TEXT_IMAGE and media_resolver is not None:
                media, issue = resolve()
                if not value.emit and not media:
                    return None, issue or "HISTORY_MIXED_MEDIA_UNAVAILABLE"
                if not value.emit:
                    kind = "image"
        elif row.msgtype in (-2000, -2005, -2017):
            if media_resolver is None:
                return None, "HISTORY_MEDIA_RESOLVER_UNAVAILABLE"
            media, issue = resolve()
            if not media:
                return None, issue or "HISTORY_NATIVE_MEDIA_METADATA_UNSUPPORTED"
            kind = "file" if row.msgtype in (-2005, -2017) else "image"
        else:
            return None, "HISTORY_UNSUPPORTED_MESSAGE_TYPE"
        if not row.uniseq or row.issend not in (0, 1):
            raise MainDbError("schema_mismatch", "history native identity invalid")
        return {"native_id": "qd:" + str(row.uniseq), "peer_id": table.peer_id,
                "direction": "out" if row.issend else "in", "occurred_at": row.time,
                "text": text, "type": kind, "session_kind": table.kind,
                "media": media, "historical": True}, issue