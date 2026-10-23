"""``audit`` 模块(02 §2.4:所有 ``/wa/v1`` 调用与管道调度都记 ``wa_audit_log``,含 ``target``、``trace_id``,**不记值**)。

- ``actor`` 取值(02 §3.2 列注释):``'agent' | 'console' | 'installer' | 'system' | 'svc→user'``(管道调度)。
- ``result``:``'OK'`` 或错误码。
- ``detail_json``:**不含值**;``.wslconfig`` 改动记前后 diff 与备份路径;告警缓冲记 payload。
  写入前统一过 ``logfmt.redact``(02 §2.9「日志 Filter 与审计序列化器同一张表」)。
- 05 §2.2.6 补强:Vault 相关审计**不记值、不记值的长度、不记摘要**——``vault.*`` 动作的 detail 只允许白名单键。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from .db import Db
from .logfmt import get_logger, redact

log = get_logger("audit")

ACTOR_AGENT = "agent"
ACTOR_CONSOLE = "console"
ACTOR_INSTALLER = "installer"
ACTOR_SYSTEM = "system"
ACTOR_SVC_TO_USER = "svc→user"                  # 02 §2.4.1「审计」行逐字
ACTORS = (ACTOR_AGENT, ACTOR_CONSOLE, ACTOR_INSTALLER, ACTOR_SYSTEM, ACTOR_SVC_TO_USER)

# 05 §2.2.6:vault 动作的 detail 只允许这些键(``version``/``scope`` 是元数据,不泄露值)
VAULT_DETAIL_ALLOW = frozenset({"scope", "version", "op", "found", "suspect", "blob_sha256"})


class Audit:
    def __init__(self, db: Db, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._clock = clock

    def record(self, *, actor: str, action: str, result: str = "OK", target: Optional[str] = None,
               ip: Optional[str] = None, trace_id: Optional[str] = None,
               detail: Optional[dict[str, Any]] = None) -> int:
        d = dict(detail or {})
        if action.startswith("vault."):                     # 05 §2.2.6:不记值、不记长度、不记摘要
            d = {k: v for k, v in d.items() if k in VAULT_DETAIL_ALLOW}
        safe = redact(d)
        with self._db.tx() as con:
            cur = con.execute(
                "INSERT INTO wa_audit_log(ts_ms, actor, ip, action, target, result, trace_id, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                (self._clock(), actor, ip, action, target, result, trace_id, json.dumps(safe, ensure_ascii=False)))
        log.info("audit", extra={"op": action, "code": result, "kv": {"actor": actor, "target": target or ""}})
        return int(cur.lastrowid or 0)

    def page(self, *, since: Optional[int] = None, until: Optional[int] = None, limit: int = 100,
             cursor: Optional[int] = None) -> dict[str, Any]:
        """#44 ``GET /wa/v1/audit``:分页(``since/until/limit/cursor``,C-42 统一参数名)。``cursor`` = 上一页最后一行 id。"""
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM wa_audit_log WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            sql, _ = sql + " AND ts_ms >= ?", params.append(since)
        if until is not None:
            sql, _ = sql + " AND ts_ms <= ?", params.append(until)
        if cursor is not None:
            sql, _ = sql + " AND id < ?", params.append(cursor)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit + 1)
        rows = self._db.query(sql, tuple(params))
        next_cursor = rows[limit - 1]["id"] if len(rows) > limit else None
        return {"items": [_row_view(r) for r in rows[:limit]], "next_cursor": next_cursor}


def _row_view(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["detail"] = json.loads(out.pop("detail_json") or "{}")
    return out
