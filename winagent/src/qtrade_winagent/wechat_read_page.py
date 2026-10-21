"""Bounded, resumable Chatlog scans; only a complete global prefix is returned."""
from __future__ import annotations

import itertools
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generator, Protocol

from .errors import BUSY, INVALID_ARGS, NOT_READY, RESOURCE_EXHAUSTED, TIMEOUT, WaError

MAX_ROWS = 500
PAGE_BYTES = 768 * 1024
FETCH_ROWS = 200
MAX_SESSIONS = 10000
MAX_SCANNED_ROWS = 200000
Row = dict[str, Any]
Query = tuple[str, dict[str, Any]]
Scan = Generator[Query, list[Row], list[Row]]

class FetchPage(Protocol):
    def __call__(self, path: str, query: dict[str, Any], *, timeout_s: float) -> list[Row]: ...

def chatlog_timestamp(value: object) -> float | None:
    """Normalize observed seconds, milliseconds, or ISO timestamps without guessing."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        else:
            return None
        if not math.isfinite(result) or result <= 0:
            return None
        return result / 1000 if result > 100_000_000_000 else result
    except (ValueError, OverflowError, OSError):
        return None


def checked_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise WaError(INVALID_ARGS, "Message page limit must be 1..500", reason="bad_read_limit")
    return value

def sequence(row: Row) -> int:
    value = row.get("seq")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WaError(NOT_READY, "Invalid Chatlog sequence", reason="wechat_read_invalid_response")
    try:
        number = int(value)
    except ValueError:
        raise WaError(NOT_READY, "Invalid Chatlog sequence", reason="wechat_read_invalid_response") from None
    if number < 0:
        raise WaError(NOT_READY, "Invalid Chatlog sequence", reason="wechat_read_invalid_response")
    return number

def wire_row(row: Row) -> Row:
    seq = sequence(row)
    talker = row.get("talker")
    if not isinstance(talker, str) or not talker:
        raise WaError(NOT_READY, "Missing Chatlog talker", reason="wechat_read_invalid_response")
    return {**row, "seq": seq, "ext_msg_id": f"{talker}:{seq}"}

def row_bytes(row: Row) -> int:
    return len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 2

def frontier(rows: list[Row], limit: int) -> list[Row]:
    """Retain a bounded prefix plus its first deferred boundary witness."""
    unique = {(r["talker"], sequence(r)): r for r in rows}
    ordered = sorted(unique.values(), key=lambda r: (sequence(r), r["talker"]))
    out: list[Row] = []
    used = 512
    for row in ordered:
        out.append(row)
        used += row_bytes(row)
        if len(out) > limit or used > PAGE_BYTES:
            break
    return out

def message_page(rows: list[Row], limit: int, *, budget: int = PAGE_BYTES) -> list[Row]:
    checked_limit(limit)
    ordered = sorted((wire_row(row) for row in rows), key=lambda r: (r["seq"], r["talker"]))
    out: list[Row] = []
    used = 512
    for _, group in itertools.groupby(ordered, key=lambda row: row["seq"]):
        same = list(group)
        size = sum(row_bytes(row) for row in same)
        if len(out) + len(same) > limit or used + size > budget:
            if not out:
                reason = "wechat_message_too_large" if len(same) == 1 else "wechat_sequence_group_too_large"
                raise WaError(RESOURCE_EXHAUSTED, "Message exceeds response budget; cursor unchanged", reason=reason)
            break
        out.extend(same)
        used += size
    return out

def _scan(*, time_range: str, talker: str | None, since: int, limit: int, not_before: float | None = None) -> Scan:
    if talker:
        names = [talker]
    else:
        names = []
        seen: set[str] = set()
        offset = 0
        last = None
        start = datetime.fromisoformat(time_range.split("~", 1)[0]).timestamp()
        if not_before is not None:
            start = max(start, not_before)
        while True:
            batch = yield "/api/v1/session", {"limit": FETCH_ROWS, "offset": offset}
            identity = tuple(r.get("userName") or r.get("UserName") or r.get("talker") for r in batch)
            if batch and identity == last:
                raise WaError(NOT_READY, "Session pagination did not advance", reason="wechat_read_pagination_unavailable")
            last = identity
            for row, name in zip(batch, identity):
                if not name:
                    continue
                if not isinstance(name, str):
                    raise WaError(NOT_READY, "Invalid session identity", reason="wechat_read_invalid_response")
                if name in seen:
                    continue
                seen.add(name)
                stamp = chatlog_timestamp(row.get("nOrder"))
                if stamp is None:
                    stamp = chatlog_timestamp(row.get("nTime"))
                if stamp is not None and 0 < stamp < start:
                    continue
                names.append(name)
            offset += len(batch)
            if len(batch) < FETCH_ROWS:
                break
            if offset >= MAX_SESSIONS:
                raise WaError(RESOURCE_EXHAUSTED, "Session scan budget exceeded", reason="wechat_read_session_limit")
    best: list[Row] = []
    for name in names:
        offset = 0
        previous = -1
        collected: list[Row] = []
        while True:
            batch = yield "/api/v1/chatlog", {"time": time_range, "talker": name, "limit": FETCH_ROWS, "offset": offset}
            seqs = [sequence(row) for row in batch]
            if seqs and (seqs != sorted(seqs) or seqs[0] <= previous):
                raise WaError(NOT_READY, "Message order is not monotonic", reason="wechat_read_pagination_unavailable")
            if seqs:
                previous = seqs[-1]
            for raw, seq in zip(batch, seqs):
                stamp = chatlog_timestamp(raw.get("time"))
                if stamp is None:
                    stamp = chatlog_timestamp(raw.get("createTime"))
                if seq > since and (not_before is None or stamp is None or stamp >= not_before):
                    collected.append(wire_row({**raw, "talker": raw.get("talker") or name}))
                if len(collected) > limit:
                    break
            offset += len(batch)
            collected = frontier(collected, limit)
            if len(batch) < FETCH_ROWS or len(collected) > limit:
                break
            if sum(row_bytes(row) for row in collected) + 512 > PAGE_BYTES:
                break
            if offset >= MAX_SCANNED_ROWS:
                raise WaError(RESOURCE_EXHAUSTED, "History scan budget exceeded", reason="wechat_read_history_limit")
        best = frontier(best + collected, limit)
    return message_page(best, limit)

@dataclass
class _Checkpoint:
    scan: Scan
    query: Query
    touched: float
    completed_requests: int = 0

class ResumableReads:
    """Private, bounded, in-memory progress; never advance the public cursor early.

    At most four interrupted scans are retained for two minutes. A process restart
    or account/key context reset discards them safely: the durable cursor is unchanged.
    """
    def __init__(self, max_scans: int = 4, ttl_s: float = 120.0):
        if max_scans < 1 or ttl_s <= 0:
            raise ValueError("Invalid read checkpoint limits")
        self._max_scans = max_scans
        self._ttl_s = ttl_s
        self._scans: OrderedDict[tuple, _Checkpoint] = OrderedDict()
        self._guard = threading.Lock()
        self._epoch = 0

    def reset(self) -> None:
        # Nonblocking invalidation, including an already-running worker thread.
        self._epoch += 1
        if self._guard.acquire(blocking=False):
            try:
                for checkpoint in self._scans.values():
                    checkpoint.scan.close()
                self._scans.clear()
            finally:
                self._guard.release()

    def read(self, fetch: FetchPage, *, time_range: str, talker: str | None,
             since_seq: int | None, limit: int, seconds: float = 10.0, not_before: float | None = None) -> list[Row]:
        checked_limit(limit)
        if not_before is not None and (type(not_before) not in (int, float) or not math.isfinite(not_before) or not_before >= 100_000_000_000 or not_before < 0):
            raise WaError(INVALID_ARGS, "Invalid read time floor", reason="bad_read_window")
        if since_seq is not None and (type(since_seq) is not int or since_seq < 0):
            raise WaError(INVALID_ARGS, "Invalid read cursor", reason="bad_read_cursor")
        if not self._guard.acquire(blocking=False):
            raise WaError(BUSY, "Another read is in progress", reason="wechat_read_busy", retryable=True)
        epoch = self._epoch
        key = (epoch, time_range, talker, since_seq, limit, not_before)
        try:
            now = time.monotonic()
            for old in list(self._scans):
                if old[0] != epoch or now - self._scans[old].touched > self._ttl_s:
                    self._scans.pop(old).scan.close()
            checkpoint = self._scans.get(key)
            if checkpoint is None:
                while len(self._scans) >= self._max_scans:
                    self._scans.popitem(last=False)[1].scan.close()
                scan = _scan(time_range=time_range, talker=talker, since=since_seq if since_seq is not None else -1, limit=limit, not_before=not_before)
                checkpoint = _Checkpoint(scan, next(scan), now)
                self._scans[key] = checkpoint
            self._scans.move_to_end(key)
            checkpoint.touched = now
            deadline = now + seconds
            while True:
                if epoch != self._epoch:
                    raise WaError(NOT_READY, "Read context changed", reason="wechat_read_context_changed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WaError(TIMEOUT, "History scan continuing; cursor unchanged",
                                  reason="wechat_read_catching_up", retryable=True,
                                  extra={"completed_requests": checkpoint.completed_requests, "cursor_unchanged": True})
                path, query = checkpoint.query
                batch = fetch(path, query, timeout_s=min(2.0, remaining))
                if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                    raise WaError(NOT_READY, "Invalid Chatlog page", reason="wechat_read_invalid_response")
                checkpoint.completed_requests += 1
                try:
                    checkpoint.query = checkpoint.scan.send(batch)
                except StopIteration as done:
                    self._scans.pop(key, None)
                    if epoch != self._epoch:
                        raise WaError(NOT_READY, "Read context changed", reason="wechat_read_context_changed")
                    return done.value
        except WaError as error:
            if not error.retryable:
                checkpoint = self._scans.pop(key, None)
                if checkpoint is not None:
                    checkpoint.scan.close()
            raise
        except Exception:
            checkpoint = self._scans.pop(key, None)
            if checkpoint is not None:
                checkpoint.scan.close()
            raise
        finally:
            self._guard.release()

def collect_messages(fetch: FetchPage, *, time_range: str, talker: str | None,
                     since_seq: int | None, limit: int, seconds: float = 10.0, not_before: float | None = None) -> list[Row]:
    """One-shot compatibility entry; production owns a ResumableReads instance."""
    return ResumableReads(max_scans=1).read(fetch, time_range=time_range, talker=talker,
                                          since_seq=since_seq, limit=limit, seconds=seconds, not_before=not_before)
