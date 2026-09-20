"""API 视图序列化 —— 00 §7.1 Account(含 02 §3.4.1「Account 序列化」的 error_since_ms 口径)、00 §7.4 Message、Session、Command/CommandResult。

时间一律 ISO 8601 带 +08:00(00 §6);``GET /messages`` 不带事件专属的 lag_s/late/origin(00 §7.4 / R6-49)。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Optional

from ..events import iso8601
from ..models import CommandResult, json_safe

PORT_BASE = {"adb": 16000, "stream": 16500, "frida": 16600, "ws": 16100, "http": 16200, "webui": 16300}   # 00 §3:段基址 + NN


def _iso(ms: Optional[int]) -> Optional[str]:
    return iso8601(ms) if ms is not None else None


def account_view(row: dict[str, Any], capabilities: list[str]) -> dict[str, Any]:
    seq = int(row["seq"])
    ch = row["channel"]
    runtime: dict[str, Any] = {"kind": row.get("runtime_kind") or {"qidian": "redroid", "qq": "napcat", "wechat": "wechat_pc"}[ch]}
    if ch == "qidian":
        runtime.update({"container": row.get("container_name") or f"qtrade-{row['id']}",
                        "adb_port": row.get("adb_port") or PORT_BASE["adb"] + seq, "stream_port": row.get("stream_port") or PORT_BASE["stream"] + seq,
                        "app_version": row.get("runtime_app_version")})
    elif ch == "qq":
        runtime.update({"container": row.get("container_name") or f"qtrade-{row['id']}",
                        "ws_port": row.get("ws_port") or PORT_BASE["ws"] + seq, "http_port": row.get("http_port") or PORT_BASE["http"] + seq})
    else:
        runtime.update({"wechat_version": row.get("wechat_version"), "wxkey_dll": row.get("wxkey_dll")})
    try:
        identity = json.loads(row.get("identity_json") or "{}")
    except ValueError:
        identity = {}
    error_since = row.get("runtime_error_since_ms")
    return {
        "id": row["id"], "channel": ch, "label": row["label"], "host": row["host"],
        "state": row["state"], "state_code": row.get("state_code") or "", "state_reason": row.get("state_reason") or "",
        "error_since_ms": error_since if row["state"] == "error" else None,     # 仅 state=error 时非空(R6-4)
        "enabled": bool(row["enabled"]), "auto_recover": bool(row["auto_recover"]), "deleted_ms": row.get("deleted_ms"),
        "runtime": runtime, "identity": identity,
        "login": {"mode": row["login_mode"], "credential_ref": row.get("credential_ref"), "remember": bool(row.get("remember"))},
        "capabilities": capabilities, "quota_mb": row["quota_mb"],
        "self_nick": row.get("self_nick"), "self_uid": row.get("self_uid"),
        "wxid": row.get("wxid"), "merged_into": row.get("merged_into"),
        "created_at": _iso(row["created_ms"]), "updated_at": _iso(row["updated_ms"]), "last_seen_at": _iso(row.get("last_seen_ms")),
    }


def message_view(row: dict[str, Any]) -> dict[str, Any]:
    try:
        media = json.loads(row.get("media_json") or "[]")
    except ValueError:
        media = []
    return {
        "id": row["id"], "ext_msg_id": row.get("ext_msg_id"), "account_id": row["account_id"], "channel": row["channel"],
        "session": {"id": row["session_id"], "name": row.get("session_name"), "kind": row.get("session_kind")},
        "dir": row["dir"], "type": row["type"], "state": row["state"],
        "text": row.get("text"), "text_len": row.get("text_len"), "fingerprint": row.get("fingerprint"),
        "media": media, "sender": {"id": row.get("sender_id"), "name": row.get("sender_name")}, "self": bool(row.get("is_self")),
        "ts": _iso(row["ts_ms"]), "received_at": _iso(row["received_ms"]), "source": row["source"],
        "revoked": bool(row.get("revoked")), "raw_ref": row.get("raw_ref"),
        "confirmed_by": row.get("confirmed_by"), "trace_id": row.get("trace_id"),
    }


def session_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "account_id": row["account_id"], "channel": row["channel"], "native_id": row["native_id"],
            "name": row["name"], "kind": row["kind"], "member_count": row.get("member_count"), "last_msg_at": _iso(row.get("last_msg_ms")),
            "msg_count": row.get("msg_count", 0), "unread": row.get("unread", 0), "muted": bool(row.get("muted")),
            "capture_text": row.get("capture_text"), "retention_days": row.get("retention_days")}


def result_view(res: CommandResult) -> dict[str, Any]:
    # `data` 里可能带裸二进制(截图的图片体):出 JSON 前换成 `{__binary__, len}` 占位(models.json_safe)
    out: dict[str, Any] = {"ok": res.ok, "code": res.code, "data": json_safe(res.data), "cost_ms": res.cost_ms, "trace_id": res.trace_id,
                           "source": res.source, "state_before": res.state_before, "state_after": res.state_after}
    if res.error is not None:
        out["error"] = {"message": res.error.message, "reason": res.error.reason, "retryable": res.error.retryable,
                        "needs_human": res.error.needs_human, "details": res.error.details}
    return out


def command_view(row: dict[str, Any]) -> dict[str, Any]:
    try:
        args = json.loads(row.get("args_json") or "{}")
    except ValueError:
        args = {}
    return {"trace_id": row["trace_id"], "account_id": row["account_id"], "op": row["op"], "args": args,
            "idempotency_key": row.get("idempotency_key"), "confirm": bool(row.get("confirm")), "timeout_ms": row.get("timeout_ms"),
            "origin": {"transport": row.get("origin_transport"), "actor": row.get("origin_actor"), "ip": row.get("origin_ip")},
            "status": row["status"], "submitted_at": _iso(row.get("submitted_ms")), "started_at": _iso(row.get("started_ms")),
            "finished_at": _iso(row.get("finished_ms"))}


def stored_result_view(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    if row.get("code") is None:
        return None
    try:
        data = json.loads(row.get("data_json") or "{}")
    except ValueError:
        data = {}
    out = {"ok": bool(row["ok"]), "code": row["code"], "data": data, "cost_ms": row.get("cost_ms"), "trace_id": row["trace_id"],
           "source": row.get("source"), "confirmed_by": row.get("confirmed_by")}
    if row.get("error_message") is not None:
        out["error"] = {"message": row["error_message"], "retryable": bool(row.get("error_retryable")), "needs_human": bool(row.get("error_needs_human"))}
    return out


def encode_cursor(ts_ms: int, id: str) -> str:
    """02 §3.4 通用 G-16:``cursor = base64url(JSON{"ts_ms":…,"id":…})``(R6-53:R6-52 曾写 "ts_ms:id",按 G-16 改回)。"""
    raw = json.dumps({"ts_ms": int(ts_ms), "id": id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cur: str) -> tuple[int, str]:
    pad = "=" * (-len(cur) % 4)
    obj = json.loads(base64.urlsafe_b64decode(cur + pad).decode())
    return int(obj["ts_ms"]), str(obj["id"])
