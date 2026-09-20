"""安全闸(02 §2.2.2 七段流水的「安全闸」段 / §6「发送类的四件套」/ 00 §11.3 [GATE];只在 ``bus`` 一处实现,适配器不可绕)。

三道闸,按序:
① **对象校验(发前)**:目标会话必须在账号级 ``settings_json.sessions.allowlist``(05 §2.5.5,默认 ``["*"]``;匹配 ``session_id`` 或原生 id)——
   非白名单一律 ``GATE_BLOCKED``(``reason='session_not_allowed'``);读不受限。发后对象校验由适配器 ``send`` 的返回承担。
② **出口词表**:``settings`` 表键 ``gate.blocklist``(``[bus] gate_blocklist_ref = "settings:gate.blocklist"``,**热更**:每次校验现读),
   JSON 字符串数组,子串、大小写不敏感命中即 ``GATE_BLOCKED``(``reason='blocklist_hit'``;审计只记命中词的 ``sha8``,不记词与正文)。
③ **自定义闸**:``settings_json.gates.custom`` 里的闸名须已在进程内注册(``Gate.register(name, fn)``);未注册的名字按拒绝处理
   (``reason='gate_not_registered'``,安全默认),注册的闸返回 False ⇒ ``reason='custom:<name>'``。
只对 ``send_*`` 生效(画面注入类不过 GATE,02 §2.2.2)。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Optional

from .models import CommandError

GateFn = Callable[[dict[str, Any], str, dict[str, Any]], bool]     # (account_row, op, args) -> True 放行
BLOCKLIST_KEY = "gate.blocklist"


def _sha8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


class Gate:
    def __init__(self, store, *, custom: Optional[dict[str, GateFn]] = None):
        self._store = store
        self._custom: dict[str, GateFn] = dict(custom or {})

    def register(self, name: str, fn: GateFn) -> None:
        self._custom[name] = fn

    def blocklist(self) -> list[str]:
        """热更:每次现读 settings(02 §2.2.2「安全闸词表从 settings 加载,热更新」)。"""
        v = self._store.settings_get(BLOCKLIST_KEY)
        if isinstance(v, list):
            return [str(w) for w in v if str(w)]
        if isinstance(v, str) and v:
            try:
                arr = json.loads(v)
                return [str(w) for w in arr if str(w)] if isinstance(arr, list) else [v]
            except ValueError:
                return [v]
        return []

    @staticmethod
    def _settings(row: dict[str, Any]) -> dict[str, Any]:
        try:
            s = json.loads(row.get("settings_json") or "{}")
        except ValueError:
            s = {}
        return s if isinstance(s, dict) else {}

    def check(self, row: dict[str, Any], op: str, args: dict[str, Any]) -> Optional[CommandError]:
        """返回 None = 放行;否则返回可直接放进 ``CommandResult.error`` 的 GATE_BLOCKED 错误。"""
        if not op.startswith("send_"):
            return None
        settings = self._settings(row)
        aid = row["id"]
        # ① 会话白名单
        allow = (settings.get("sessions") or {}).get("allowlist", ["*"])
        if not isinstance(allow, list):
            allow = ["*"]
        session = str(args.get("session") or "")
        native = session.split(":", 1)[1] if session.startswith(aid + ":") else session
        if "*" not in allow and session not in allow and native not in allow and f"{aid}:{native}" not in allow:
            return CommandError("目标会话不在该账号的发送白名单(sessions.allowlist)", reason="session_not_allowed", details=[{"pointer": "/session"}])
        # ② 出口词表
        text = args.get("text")
        if isinstance(text, str) and text:
            low = text.lower()
            for w in self.blocklist():
                if w.lower() in low:
                    return CommandError("正文命中出口词表,已拦截", reason="blocklist_hit", details=[{"pointer": "/text", "word_sha8": _sha8(w)}])
        # ③ 自定义闸
        for name in (settings.get("gates") or {}).get("custom", []) or []:
            fn = self._custom.get(str(name))
            if fn is None:
                return CommandError(f"自定义闸 {name} 未注册,按拒绝处理", reason="gate_not_registered", details=[{"gate": str(name)}])
            try:
                ok = bool(fn(row, op, args))
            except Exception:
                ok = False
            if not ok:
                return CommandError(f"自定义闸 {name} 拒绝", reason=f"custom:{name}", details=[{"gate": str(name)}])
        return None
