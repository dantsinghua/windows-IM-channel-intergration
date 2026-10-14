"""Bounded chatlog reads. A returned watermark never skips a deferred sequence."""
from __future__ import annotations
import itertools, json, time
from datetime import datetime
from typing import Any
from .errors import INVALID_ARGS, NOT_READY, RESOURCE_EXHAUSTED, TIMEOUT, WaError

MAX_ROWS = 500
PAGE_BYTES = 768 * 1024
FETCH_ROWS = 200
MAX_SESSIONS = 10000
MAX_SCANNED_ROWS = 200000

def checked_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise WaError(INVALID_ARGS, "Message page limit must be 1..500", reason="bad_read_limit")
    return value

def sequence(row: dict) -> int:
    value = row.get("seq")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WaError(NOT_READY, "Invalid chatlog sequence", reason="wechat_read_invalid_response")
    try:
        number = int(value)
    except ValueError:
        raise WaError(NOT_READY, "Invalid chatlog sequence", reason="wechat_read_invalid_response") from None
    if number < 0:
        raise WaError(NOT_READY, "Invalid chatlog sequence", reason="wechat_read_invalid_response")
    return number

def wire_row(row: dict) -> dict:
    seq = sequence(row)
    talker = row.get("talker")
    if not isinstance(talker,str) or not talker:
        raise WaError(NOT_READY, "Missing chatlog talker", reason="wechat_read_invalid_response")
    return {**row, "seq":seq, "ext_msg_id":f"{talker}:{seq}"}

def row_bytes(row: dict) -> int:
    return len(json.dumps(row,ensure_ascii=False).encode("utf-8")) + 2

def frontier(rows: list[dict], limit: int) -> list[dict]:
    # Keep the first deferred row as a boundary witness; never keep an unbounded history.
    unique = {(r["talker"],sequence(r)):r for r in rows}
    ordered = sorted(unique.values(),key=lambda r:(sequence(r),r["talker"]))
    out=[]; used=512
    for r in ordered:
        out.append(r); used += row_bytes(r)
        if len(out)>limit or used>PAGE_BYTES:break
    return out

def message_page(rows: list[dict], limit: int, *, budget: int = PAGE_BYTES) -> list[dict]:
    checked_limit(limit)
    ordered=sorted((wire_row(r) for r in rows),key=lambda r:(r["seq"],r["talker"]))
    out=[];used=512
    for _,group in itertools.groupby(ordered,key=lambda r:r["seq"]):
        same=list(group);size=sum(row_bytes(r) for r in same)
        if len(out)+len(same)>limit or used+size>budget:
            if not out:
                reason="wechat_message_too_large" if len(same)==1 else "wechat_sequence_group_too_large"
                raise WaError(RESOURCE_EXHAUSTED,"Message page exceeds the safe response budget; cursor unchanged",reason=reason)
            break
        out.extend(same);used+=size
    return out

def collect_messages(fetch, *, time_range: str, talker: str | None, since_seq: int | None, limit: int, seconds: float=6.0) -> list[dict]:
    checked_limit(limit)
    if since_seq is not None and (type(since_seq) is not int or since_seq<0):
        raise WaError(INVALID_ARGS,"Invalid read cursor",reason="bad_read_cursor")
    since=since_seq if since_seq is not None else -1
    deadline=time.monotonic()+seconds
    def get(path,query):
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise WaError(TIMEOUT,"Chatlog read budget exhausted; cursor unchanged",reason="wechat_read_budget_exceeded",retryable=True)
        rows=fetch(path,query,timeout_s=min(2.0,remaining))
        if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
            raise WaError(NOT_READY,"Invalid chatlog page",reason="wechat_read_invalid_response")
        return rows
    if talker:
        names=[talker]
    else:
        names=[];seen=set();offset=0;last=None
        start=datetime.fromisoformat(time_range.split("~",1)[0]).timestamp()
        while True:
            batch=get("/api/v1/session",{"limit":FETCH_ROWS,"offset":offset})
            identity=tuple(r.get("userName") or r.get("UserName") or r.get("talker") for r in batch)
            if batch and identity==last:
                raise WaError(NOT_READY,"Chatlog session pagination did not advance",reason="wechat_read_pagination_unavailable")
            last=identity
            for r,name in zip(batch,identity):
                if not name or name in seen:continue
                seen.add(name)
                ts=r.get("nTime")
                if isinstance(ts,(int,float)) and not isinstance(ts,bool):
                    stamp=ts/1000 if ts>1e11 else ts
                    if stamp>0 and stamp<start:continue
                names.append(name)
            offset+=len(batch)
            if len(batch)<FETCH_ROWS:break
            if offset>=MAX_SESSIONS:
                raise WaError(RESOURCE_EXHAUSTED,"Chatlog session scan budget exceeded",reason="wechat_read_session_limit")
    best=[]
    for name in names:
        offset=0;previous=-1;collected=[]
        while True:
            batch=get("/api/v1/chatlog",{"time":time_range,"talker":name,"limit":FETCH_ROWS,"offset":offset})
            seqs=[sequence(r) for r in batch]
            if seqs and (seqs!=sorted(seqs) or seqs[0]<=previous):
                raise WaError(NOT_READY,"Chatlog message order or pagination is not monotonic",reason="wechat_read_pagination_unavailable")
            if seqs:previous=seqs[-1]
            for raw,seq in zip(batch,seqs):
                if seq>since:collected.append(wire_row({**raw,"talker":raw.get("talker") or name}))
                if len(collected)>limit:break
            offset+=len(batch)
            if len(batch)<FETCH_ROWS or len(collected)>limit:break
            if offset>=MAX_SCANNED_ROWS:
                raise WaError(RESOURCE_EXHAUSTED,"Chatlog history scan budget exceeded; cursor unchanged",reason="wechat_read_history_limit")
        best=frontier(best+collected,limit)
    return message_page(best,limit)
