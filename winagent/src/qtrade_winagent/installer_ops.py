"""``installer-ops`` 模块(**服务**;02 §2.4 / 03 §2.6、§2.8、§3.3)。

职责 = 内核验证 / 回滚 / 重应用里**需要提权的那一半**:写 ``%ProgramData%\\QTrade\\kernel``、收紧 ACL、
写 ``install_history`` 与 ``install_state``;**判据由会话代理算、服务落盘**。

🔴 **混合执行端点的分解(02 §2.4.1 R3-15)** —— #26 verify / #27 rollback / #46 apply 都是 ``svc+user``:

- **服务半(不下发管道)**:内核文件落盘/ACL、``.wslconfig`` 备份目录与 ACL、``install_history``、firewall/powercfg 原值备份。
- **管道半(下发会话代理)**:``.wslconfig`` 文件读写(在**安装用户** ``%USERPROFILE%`` 下)、``wsl --shutdown``/``--terminate``、
  ``uname -r``/``/proc/filesystems``/试挂等内核判据。
- **两半的时序与失败合并**:服务按固定顺序串起两半,任一半失败即**整端点失败**,
  响应 ``{ok:false, error:{code, message, stage, partial:[…已完成的半]}}``;成功才回 ``202``/``200``。
  每半各记一行 ``wa_audit_log``(``actor='svc'`` 与 ``actor='svc→user'``),``trace_id`` 贯穿。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Callable, Optional

from .audit import Audit
from .backends import CryptoBackend
from .db import Db
from .errors import INTERNAL, WaError
from .logfmt import get_logger
from .pipe import PipeHub

log = get_logger("installer")

# 02 §3.2 install_history.actor CHECK
ACTORS = ("installer", "winagent", "console", "user")
# 02 §3.2 install_history.to_state:R6-58 (au)「无 CHECK 是有意的」—— 取值 = 00 §8.2 安装状态机键
# **外加**下列内核流程的七个非安装态迁移名(逐字抄自 schema_winagent.sql 的 DDL 注释,登记在此,不得另起同义名)
KERNEL_TO_STATES = ("KERNEL_APPLYING", "KERNEL_APPLIED", "KERNEL_APPLY_FAILED",
                    "KERNEL_VERIFIED", "KERNEL_VERIFY_FAILED", "KERNEL_ROLLING_BACK", "KERNEL_ROLLED_BACK")
KERNEL_FILE = "bzImage"
# 02 §2.5「超时」:wsl 30s(经会话代理再减 1s,由 PipeHub.call 统一做)
WSL_TIMEOUT_S = 30.0


class InstallerOps:
    def __init__(self, db: Db, *, kernel_dir: str, wsl_backup_dir: str, package_version: str,
                 crypto: Optional[CryptoBackend] = None, audit: Optional[Audit] = None, hub: Optional[PipeHub] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._kernel_dir = kernel_dir
        self._wsl_backup_dir = wsl_backup_dir
        self._pkg = package_version
        self._crypto = crypto
        self._audit = audit
        self._hub = hub
        self._clock = clock

    # ---------------------------------------------------------------- install_state / install_history
    def state(self) -> Optional[dict[str, Any]]:
        row = self._db.one("SELECT * FROM install_state WHERE key='current'")
        if row is None:
            return None
        for col in ("parked_json", "env_json", "wslconfig_json", "distro_json", "clients_json", "resume_json"):
            row[col[:-5]] = json.loads(row.pop(col) or "{}")
        return row

    def put_state(self, *, state: str, substate: Optional[str] = None, **sections: Any) -> None:
        """03 的 ``install_state.json`` 与本表双写;**以 json 为准、表落后时按 json 回填**(C-36)。这里只管表这一份。"""
        cur = self.state() or {}
        merged = {k: sections.get(k, cur.get(k, {})) for k in ("parked", "env", "wslconfig", "distro", "clients", "resume")}
        with self._db.tx() as con:
            con.execute(
                "INSERT INTO install_state(key, state, substate, parked_json, package_version, env_json, wslconfig_json, "
                "distro_json, clients_json, resume_json, updated_ms) VALUES ('current',?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state, substate=excluded.substate, "
                "parked_json=excluded.parked_json, package_version=excluded.package_version, env_json=excluded.env_json, "
                "wslconfig_json=excluded.wslconfig_json, distro_json=excluded.distro_json, "
                "clients_json=excluded.clients_json, resume_json=excluded.resume_json, updated_ms=excluded.updated_ms",
                (state, substate, _j(merged["parked"]), self._pkg, _j(merged["env"]), _j(merged["wslconfig"]),
                 _j(merged["distro"]), _j(merged["clients"]), _j(merged["resume"]), self._clock()))

    def history(self, *, to_state: str, from_state: Optional[str] = None, actor: str = "winagent",
                note: Optional[str] = None, detail: Optional[dict[str, Any]] = None) -> int:
        if actor not in ACTORS:
            raise WaError(INTERNAL, f"install_history.actor 必须是 {ACTORS} 之一", reason="bad_actor")
        if to_state.startswith("KERNEL_") and to_state not in KERNEL_TO_STATES:
            raise WaError(INTERNAL, f"install_history.to_state 的 KERNEL_ 前缀名必须是 {KERNEL_TO_STATES} 之一"
                          "(R6-58 (au):登记在 02 §3.2 DDL 注释,不得另起同义名)", reason="bad_kernel_to_state")
        with self._db.tx() as con:
            cur = con.execute(
                "INSERT INTO install_history(at_ms, from_state, to_state, package_version, actor, note, detail_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (self._clock(), from_state, to_state, self._pkg, actor, note, _j(detail or {})))
        return int(cur.lastrowid or 0)

    def history_page(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM install_history ORDER BY at_ms DESC, id DESC LIMIT ?", (max(1, min(limit, 500)),))
        for r in rows:
            r["detail"] = json.loads(r.pop("detail_json") or "{}")
        return rows

    # ---------------------------------------------------------------- 服务半:内核文件落盘 / ACL / 备份目录
    def stage_kernel(self, src: str) -> str:
        """把随包内核复制进 ``%ProgramData%\\QTrade\\kernel`` 并收紧 ACL(**LocalSystem 才做得了**)。"""
        os.makedirs(self._kernel_dir, exist_ok=True)
        dst = os.path.join(self._kernel_dir, KERNEL_FILE)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
        if self._crypto is not None:
            self._crypto.tighten_acl(dst)
        return dst

    def current_kernel(self) -> Optional[str]:
        p = os.path.join(self._kernel_dir, KERNEL_FILE)
        return p if os.path.exists(p) else None

    def ensure_backup_dir(self) -> str:
        os.makedirs(self._wsl_backup_dir, exist_ok=True)
        if self._crypto is not None:
            self._crypto.tighten_acl(self._wsl_backup_dir)
        return self._wsl_backup_dir

    # ---------------------------------------------------------------- 混合端点:两半编排 + 失败合并
    async def _mixed(self, *, action: str, svc_steps: list[tuple[str, Callable[[], Any]]],
                     user_calls: list[tuple[str, str, dict[str, Any], float]], trace_id: Optional[str]) -> dict[str, Any]:
        """按固定顺序串起服务半与管道半;任一半失败即整端点失败,带 ``stage``/``partial`` 抛出(R3-15)。"""
        partial: list[str] = []
        for name, fn in svc_steps:
            try:
                fn()
            except WaError:
                raise
            except Exception as e:
                if self._audit:
                    self._audit.record(actor="system", action=f"{action}.svc", result=INTERNAL, trace_id=trace_id,
                                       detail={"step": name})
                raise WaError(INTERNAL, f"{action} 的服务半在 {name} 失败:{e}", reason="svc_step_failed",
                              stage="svc", partial=partial) from e
            partial.append(f"svc:{name}")
        if self._audit:
            self._audit.record(actor="system", action=f"{action}.svc", trace_id=trace_id, detail={"partial": partial})
        results: dict[str, Any] = {}
        if user_calls and self._hub is None:
            raise WaError("NOT_READY", "服务未接管道,无法下发会话代理", reason="no_pipe", stage="svc", partial=partial)
        for name, method, params, timeout_s in user_calls:
            try:
                results[name] = await self._hub.call(method, params, timeout_s=timeout_s, trace_id=trace_id, target=action)  # type: ignore[union-attr]
            except WaError as e:
                e.stage = e.stage or "user"
                e.partial = partial
                raise
            partial.append(f"user:{name}")
        return {"partial": partial, "results": results}

    async def kernel_verify(self, *, trace_id: Optional[str] = None) -> dict[str, Any]:
        """#26:``user``(判据)+ ``svc``(记录 ``install_history``)。"""
        out = await self._mixed(action="wsl.kernel.verify", svc_steps=[], trace_id=trace_id,
                                user_calls=[("verify", "wsl.kernel.verify", {}, WSL_TIMEOUT_S)])
        verdict = out["results"]["verify"]
        self.history(to_state="KERNEL_VERIFIED" if verdict.get("ok") else "KERNEL_VERIFY_FAILED",
                     actor="winagent", detail=verdict)
        return verdict

    async def kernel_apply(self, *, confirm_shutdown: bool, kernel_src: Optional[str] = None,
                           trace_id: Optional[str] = None) -> dict[str, Any]:
        """#46 ``POST /wa/v1/wsl/kernel/apply``:**必须带 ``confirm_shutdown=true``**,否则 400(00 §11.6)。

        流程(02 §3.6 #46 逐字):服务备份/落盘 → 管道写 ``.wslconfig kernel=`` → 管道 shutdown → 拉起 → #26 校验
        → 失败自动回滚(03 §2.6.6) → ``202``,进度经 #20。
        """
        if not confirm_shutdown:
            raise WaError("INVALID_ARGS", "重新应用内核需要 wsl --shutdown,必须带 confirm_shutdown=true",
                          reason="shutdown_not_confirmed", needs_human=True, stage="svc", partial=[])
        staged: dict[str, Any] = {}

        def _stage() -> None:
            self.ensure_backup_dir()
            staged["path"] = self.stage_kernel(kernel_src) if kernel_src else self.current_kernel()
            if not staged["path"]:
                raise WaError(INTERNAL, "随包内核文件不存在,无法应用", reason="kernel_missing", stage="svc")

        try:
            out = await self._mixed(
                action="wsl.kernel.apply",
                svc_steps=[("stage_kernel", _stage), ("history_begin", lambda: self.history(to_state="KERNEL_APPLYING", actor="console"))],
                user_calls=[("apply", "wsl.kernel.apply", {"kernel_path": staged.get("path"), "confirm_shutdown": True},
                             WSL_TIMEOUT_S * 3)],
                trace_id=trace_id)
        except WaError:
            self.history(to_state="KERNEL_APPLY_FAILED", actor="console")
            raise
        verdict = (out["results"]["apply"] or {}).get("verify") or {}
        if not verdict.get("ok"):
            # 03 §2.6.6:校验不过 ⇒ 自动回滚(服务 + 管道协同)
            rollback = await self.kernel_rollback(confirm_shutdown=True, trace_id=trace_id, auto=True)
            self.history(to_state="KERNEL_ROLLED_BACK", actor="console", detail={"verify": verdict})
            raise WaError(INTERNAL, "内核应用后三判据未全过,已自动回滚", reason="kernel_verify_failed",
                          stage="user", partial=out["partial"] + ["svc:auto_rollback"], extra={"rollback": rollback})
        self.history(to_state="KERNEL_APPLIED", actor="console", detail=verdict)
        return {"accepted": True, "kernel": staged.get("path"), "verify": verdict, "partial": out["partial"]}

    async def kernel_rollback(self, *, confirm_shutdown: bool, trace_id: Optional[str] = None,
                              auto: bool = False) -> dict[str, Any]:
        """#27:改 ``.wslconfig kernel=`` 回备份值 → 需用户确认的 shutdown 时机 → 校验 → ``install_history``。"""
        if not confirm_shutdown:
            raise WaError("INVALID_ARGS", "回滚内核需要 wsl --shutdown,必须带 confirm_shutdown=true",
                          reason="shutdown_not_confirmed", needs_human=True, stage="svc", partial=[])
        out = await self._mixed(action="wsl.kernel.rollback",
                                svc_steps=[("history_begin", lambda: self.history(to_state="KERNEL_ROLLING_BACK",
                                                                                  actor="winagent" if auto else "console"))],
                                user_calls=[("rollback", "wsl.kernel.rollback", {"confirm_shutdown": True}, WSL_TIMEOUT_S * 3)],
                                trace_id=trace_id)
        verdict = (out["results"]["rollback"] or {}).get("verify") or {}
        self.history(to_state="KERNEL_ROLLED_BACK", actor="winagent" if auto else "console", detail=verdict)
        return {"accepted": True, "verify": verdict, "partial": out["partial"]}


def _j(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)
