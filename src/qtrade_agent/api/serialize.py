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


# ---------------------------------------------------------------- 邮件出参视图(02 #58/#59/#61)
#: 收件行的时间列 → 出参键(00 §6 表逐字:「时间(API/事件/**邮件**)= ISO 8601 带时区偏移」;
#: R6-62 (f):`*_ms` 的例外**只有** Account 的 `error_since_ms`/`deleted_ms`,「新增时间键一律 ISO + `*_at`」)。
INBOX_TIME_COLS = {"date_ms": "date_at", "received_ms": "received_at", "confirm_expires_ms": "confirm_expires_at",
                   "archived_ms": "archived_at", "deleted_ms": "deleted_at"}
#: 列表不下发的列:02 #58 逐字「``mail_inbox`` 行(**不含 `body_text`**,详情才给)」——正文只在 #59 给。
INBOX_BODY_COLS = ("body_text",)


def mail_inbox_row_view(row: dict[str, Any], *, route: Optional[str] = None, with_body: bool = False) -> dict[str, Any]:
    """#58 列表行 / #59 详情(``with_body=True``)的出参视图。

    口径 = 02 #58 逐字的「``mail_inbox`` 行」**减去 `body_text`**(列表),另做三件事:
    ① 时间列 `*_ms` → ISO 8601 `*_at`(00 §6);② `id`/`first_inbox_id` 转字符串(01 消费侧 `MailInboxRow.id: string`);
    ③ 补两个派生键 —— `route`(= 该行 `route_id` 落在哪个 scope,01 §2.7.8 收件时间线的「route 列」)与
    `archived`(01 同句的「是否已归档」;由 `archived_path`/`archived_ms` 判)。`sig_ok` 由 0/1 转布尔。
    """
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in INBOX_TIME_COLS:
            out[INBOX_TIME_COLS[k]] = _iso(v)
        elif k in INBOX_BODY_COLS:
            continue
        else:
            out[k] = v
    out["id"] = str(row["id"])
    out["first_inbox_id"] = None if row.get("first_inbox_id") is None else str(row["first_inbox_id"])
    out["sig_ok"] = None if row.get("sig_ok") is None else bool(row["sig_ok"])
    out["route"] = route
    out["archived"] = row.get("archived_path") is not None or row.get("archived_ms") is not None
    if with_body:
        out["body_text"] = row.get("body_text")
    return out


def mail_outbox_row_view(row: dict[str, Any], *, route: Optional[str] = None) -> dict[str, Any]:
    """#61 列表行的出参视图。

    02 #61 只定了入参,**行的键集出处 = 01 §2.7.8 发件队列逐字**「``kind/to/subject/status/attempts/next_attempt_at/
    last_error/ref``」,另加 `id`、`route_id`/`route`、分页排序列 `created_at` 与 `sent_at`。
    正文(`body_text`/`body_html`)与投递内部列(`dedup_key`/`smtp_response`/`rfc_message_id`/`template_*`)不进列表。
    `ref` = 该封邮件关联的对象引用:优先 `ref_trace_id`,其次 `ref_message_id`,再次 `ref_inbox_id`。
    `next_attempt_ms` 为 0(DDL 默认值 = 「没有下次」)时回 ``null``,不回 1970 年。
    """
    ref = row.get("ref_trace_id") or row.get("ref_message_id")
    if not ref and row.get("ref_inbox_id") is not None:
        ref = str(row["ref_inbox_id"])
    nxt = row.get("next_attempt_ms")
    return {"id": str(row["id"]), "kind": row["kind"], "route_id": row.get("route_id"), "route": route,
            "to": row.get("to_addrs") or "", "subject": row.get("subject"), "status": row["status"],
            "attempts": row.get("attempts", 0), "next_attempt_at": _iso(nxt) if nxt else None,
            "last_error": row.get("last_error"), "ref": ref,
            "created_at": _iso(row.get("created_ms")), "sent_at": _iso(row.get("sent_ms"))}


#: #65 行的出参键(06 §3.1 `mail_cleanup_log` 字段定案 ∩ 02 实际 DDL,按列序);时间列另换 `*_at`,`detail_json` 另转 `detail`。
#: ⚠️ 06 §3.1 还列了 `route_id`/`mailbox_key`(v0.3,E-5),但 02 DDL 没有这两列 ⇒ 视图不造(转文档方)。
CLEANUP_LOG_PLAIN_COLS = ("trigger", "protocol", "folder", "candidates", "archived", "deleted", "failed", "bytes_freed",
                          "quota_used_before", "quota_used_after", "quota_limit", "quota_source",
                          "archive_rotated_files", "archive_rotated_bytes", "status", "error")


def mail_cleanup_log_row_view(row: dict[str, Any]) -> dict[str, Any]:
    """#65 ``GET /mail/cleanup/log`` 的行视图(此前 ``SELECT *`` 原样透出库行)。

    与 #58/#61 同口径:① `started_ms`/`finished_ms` → ISO 8601 `started_at`/`finished_at`(00 §6「时间(API/事件/**邮件**)」,
    R6-62 (f):新增时间键一律 ISO + `*_at`);② `id` 出字符串(同 #58 D-3);③ `detail_json` 解成对象、键名 `detail`
    (与本文件 `args_json→args`、`media_json→media` 同一套路;06 §2.6.8 要求它必带 `skipped_oversize`/`skipped_out_of_scope`,
    是 R6-26 门生效的唯一可观测证据,给字符串等于让前端再 parse 一次)。键集显式列出,库里以后加列不会自动外露。
    """
    try:
        detail = json.loads(row.get("detail_json") or "{}")
    except ValueError:
        detail = {}
    out: dict[str, Any] = {"id": str(row["id"]), "started_at": _iso(row.get("started_ms")),
                           "finished_at": _iso(row.get("finished_ms"))}
    out.update({k: row.get(k) for k in CLEANUP_LOG_PLAIN_COLS})
    out["detail"] = detail
    return out


def _obj(text: Any, default: Any) -> Any:
    """``*_json`` 库列 → 对象;坏 JSON / 空 → ``default``(与 #65 ``detail`` 同套路,不让前端再 parse 一次)。"""
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


# ════════════════════════════════════════════ 第六批(S-4~S-9):其它域同型的出参视图
# 口径同 #58/#65/#68b:不把库行原样透出;`*_ms` → `*_at`(ISO 8601 +08:00,00 §6);`*_json` → 解成对象、去 `_json`
# 后缀;整数主键 `id` → 字符串。键集显式列出 ⇒ 库里以后加列不会自动外露。

def workflow_view(row: dict[str, Any], *, with_yaml: bool = True) -> dict[str, Any]:
    """#38(``with_yaml=False``,02 #38「不含 yaml 全文」)/ #39 / #40 / #41 的工作流定义。``id`` 本就是 ULID 字符串。"""
    out: dict[str, Any] = {"id": row["id"], "name": row["name"], "version": row["version"]}
    if with_yaml:
        out["yaml"] = row.get("yaml")
    out.update({"checksum": row.get("checksum"), "enabled": bool(row.get("enabled", 1)), "schedule_cron": row.get("schedule_cron"),
                "created_at": _iso(row.get("created_ms")), "updated_at": _iso(row.get("updated_ms")),
                "updated_by": row.get("updated_by")})
    return out


def workflow_run_view(row: dict[str, Any]) -> dict[str, Any]:
    """#44 的行 / #45 的 ``run``(02 §3.1 `workflow_runs`;``args_json`` → ``args`` 对象)。"""
    return {"run_id": row["run_id"], "workflow_id": row.get("workflow_id"), "workflow_version": row.get("workflow_version"),
            "trigger": row.get("trigger"), "actor": row.get("actor"), "args": _obj(row.get("args_json"), {}),
            "status": row.get("status"), "pause_reason": row.get("pause_reason"), "error": row.get("error"),
            "started_at": _iso(row.get("started_ms")), "finished_at": _iso(row.get("finished_ms"))}


def workflow_step_view(row: dict[str, Any]) -> dict[str, Any]:
    """#45 的 ``steps[]``(02 §3.1 `workflow_steps` 全列,时间换 ISO)。"""
    return {"step_id": row["step_id"], "run_id": row.get("run_id"), "idx": row.get("idx"), "step_name": row.get("step_name"),
            "op": row.get("op"), "account_id": row.get("account_id"), "trace_id": row.get("trace_id"),
            "status": row.get("status"), "attempt": row.get("attempt"), "result_code": row.get("result_code"),
            "note": row.get("note"), "started_at": _iso(row.get("started_ms")), "finished_at": _iso(row.get("finished_ms"))}


def _is_secret_key(k: str) -> bool:
    kl = k.lower()
    return kl in ("secret", "password", "token") or kl.endswith(("_secret", "_password", "_token"))


def strip_secrets(obj: Any) -> Any:
    """递归去掉「只写不读」的明文键(``secret``/``password``/``*_secret``…),``*_ref`` 保留。"""
    if isinstance(obj, dict):
        return {k: strip_secrets(v) for k, v in obj.items() if not _is_secret_key(str(k))}
    if isinstance(obj, list):
        return [strip_secrets(v) for v in obj]
    return obj


def mail_route_row_view(row: dict[str, Any], *, status: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """#105 ``GET /settings/mail/routes`` 的行(此前 ``SELECT *`` 原样,``inbound_json``/``outbound_json`` 是字符串)。

    键 = 02 #105 body 的键集(``channel/account_id/inbound/outbound/outbound_template_id/inbound_template_id/enabled``)
    + ``id`` + ``created_at/updated_at`` + ``status``(02 #105「含每条 `status` 摘要 = #56 单条」,由调用方给)。
    🔴 ``inbound``/``outbound`` 过 ``strip_secrets``:02 #105「`inbound.secret/outbound.secret` 只写不读」——
    改前实测 PUT 带进来的 ``secret`` 明文被原样存进 ``inbound_json``、GET 原样回显。
    """
    return {"id": str(row["id"]), "channel": row.get("channel"), "account_id": row.get("account_id"),
            "inbound": strip_secrets(_obj(row.get("inbound_json"), {})),
            "outbound": strip_secrets(_obj(row.get("outbound_json"), {})),
            "outbound_template_id": row.get("outbound_template_id"), "inbound_template_id": row.get("inbound_template_id"),
            "enabled": bool(row.get("enabled", 1)),
            "created_at": _iso(row.get("created_ms")), "updated_at": _iso(row.get("updated_ms")),
            "status": status}


def encode_cursor(ts_ms: int, id: str) -> str:
    """02 §3.4 通用 G-16:``cursor = base64url(JSON{"ts_ms":…,"id":…})``(R6-53:R6-52 曾写 "ts_ms:id",按 G-16 改回)。"""
    raw = json.dumps({"ts_ms": int(ts_ms), "id": id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cur: str) -> tuple[int, str]:
    pad = "=" * (-len(cur) % 4)
    obj = json.loads(base64.urlsafe_b64decode(cur + pad).decode())
    return int(obj["ts_ms"]), str(obj["id"])
