"""``power`` 模块(**服务** ``ES_SYSTEM_REQUIRED`` + **会话代理** ``ES_DISPLAY_REQUIRED``;02 §2.4 / 04 §2.5.1,C-6/C-34)。

三种模式(``winagent.toml [wechat] keep_awake_mode``,**默认 ``powercfg``**):

===========  ==========================================================================================
``powercfg`` 对**当前活动电源计划**把 04 §2.5.1 的**六项**超时改成 0;**先把六项原值读出存
             ``settings`` 键 ``power.backup_json``,且只在第一次写时存、之后不覆盖**——否则第二次启用会把
             「我们改成的 0」当原值备份掉。同时仍发 ``ES_SYSTEM_REQUIRED``/``ES_DISPLAY_REQUIRED``(零成本双保险)。
``request``  只发电源请求,不改电源计划。
``off``      什么都不做。
===========  ==========================================================================================

- **不动**:Hybrid sleep、唤醒定时器、USB 选择性暂停、硬盘关闭;**不新建电源计划**(改当前计划)。
- 组策略锁定电源计划 ⇒ ``powercfg /change`` 失败:记 ``warn`` + **退化到 ``request`` 行为**并告知,不绕策略。
- 锁屏**只检测告知**(C-34):``ES_*`` 不重置空闲计时器,对用户自设锁屏与组策略锁屏都无效,**不模拟输入**。
- 关闭微信模块 / 卸载 / ``keep_awake_mode`` 切离 ``powercfg`` ⇒ 按备份逐项还原并**删该键**。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .audit import Audit
from .backends import PowerBackend, SysBackend
from .config import WechatConfig
from .db import Db
from .errors import INVALID_ARGS, WaError
from .logfmt import get_logger

log = get_logger("power")

MODES = ("request", "powercfg", "off")
BACKUP_KEY = "power.backup_json"            # 02 §3.2 settings 键前缀约定(04 v0.3 N5~N10 / B-2)
CURRENT_KEY = "power.keepawake_current"


class Power:
    def __init__(self, db: Db, power: PowerBackend, sys: SysBackend, cfg: WechatConfig, *,
                 audit: Optional[Audit] = None, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._power = power
        self._sys = sys
        self._cfg = cfg
        self._audit = audit
        self._clock = clock
        self.display_required_pending = False       # 由**会话代理**执行的那半(Session 0 发 DISPLAY_REQUIRED 无效)
        self.policy_degraded = False                # 组策略锁定 ⇒ 退化到 request 行为

    # ---------------------------------------------------------------- 备份 / 还原
    def _backup_once(self, scheme: str) -> dict[str, Any]:
        """🔴 **只在第一次写时存,之后不覆盖**(04 §2.5.1 那句括注就是本函数存在的全部理由)。"""
        existing = self._db.get_setting(BACKUP_KEY)
        if existing:
            return existing
        vals = self._power.query_timeouts(scheme, self._cfg.powercfg_items)
        backup = {"scheme_guid": scheme, "saved_at_ms": self._clock(), **vals}
        self._db.put_setting(BACKUP_KEY, backup, updated_by="svc")
        return backup

    def restore(self) -> dict[str, Any]:
        """卸载 / 关闭微信模块 / 切离 ``powercfg`` 时按备份逐项还原,并**删该键**。"""
        backup = self._db.get_setting(BACKUP_KEY)
        if not backup:
            return {"restored": False, "reason": "no_backup"}
        scheme = backup.get("scheme_guid") or self._power.active_scheme()
        restored: dict[str, int] = {}
        for item in self._cfg.powercfg_items:
            if item in backup:
                try:
                    self._power.change_timeout(scheme, item, int(backup[item]))
                    restored[item] = int(backup[item])
                except PermissionError:
                    log.warning("powercfg restore blocked by policy", extra={"op": "power.restore", "code": "BLOCKED"})
        with self._db.tx() as con:
            con.execute("DELETE FROM settings WHERE key=?", (BACKUP_KEY,))
        self._power.set_execution_state(system_required=False, display_required=False)
        self.display_required_pending = False
        self._db.put_setting(CURRENT_KEY, {"mode": "off", "at_ms": self._clock()}, updated_by="svc")
        if self._audit:
            self._audit.record(actor="system", action="power.restore", target=scheme, detail={"restored": list(restored)})
        return {"restored": True, "items": restored, "scheme_guid": scheme}

    # ---------------------------------------------------------------- #19 POST /wa/v1/power/keepawake
    def apply(self, mode: str, *, actor: str = "console", trace_id: Optional[str] = None) -> dict[str, Any]:
        """执行「应用 / 还原」。**服务半**在这里做;``ES_DISPLAY_REQUIRED`` 那半交会话代理(见 ``display_required_pending``)。"""
        if mode not in MODES:
            raise WaError(INVALID_ARGS, f"mode 必须是 {MODES} 之一", reason="bad_mode")
        self.policy_degraded = False
        if mode == "off":
            out = self.restore()
            out["mode"] = "off"
            return out
        # request / powercfg 都发电源请求(powercfg 是「另外还改计划」,不是「取代」)
        self._power.set_execution_state(system_required=True, display_required=False)   # 服务侧只能发 SYSTEM
        self.display_required_pending = True                                            # DISPLAY 归会话代理
        changed: dict[str, int] = {}
        scheme = self._power.active_scheme()
        if mode == "powercfg":
            self._backup_once(scheme)
            for item in self._cfg.powercfg_items:
                try:
                    self._power.change_timeout(scheme, item, 0)
                    changed[item] = 0
                except PermissionError:
                    self.policy_degraded = True
                    log.warning("电源计划由公司策略管理,退化到 request 行为",
                                extra={"op": "power.keepawake", "code": "POLICY_LOCKED"})
                    break
        self._db.put_setting(CURRENT_KEY, {"mode": mode, "at_ms": self._clock(),
                                           "policy_degraded": self.policy_degraded}, updated_by="svc")
        if self._audit:
            self._audit.record(actor=actor, action="power.keepawake", target=mode, trace_id=trace_id,
                               result="OK" if not self.policy_degraded else "BLOCKED_BY_POLICY",
                               detail={"scheme_guid": scheme, "changed": list(changed)})
        return {"mode": mode, "scheme_guid": scheme, "changed": changed,
                "policy_degraded": self.policy_degraded, "display_required_by": "user_agent"}

    # ---------------------------------------------------------------- #18 GET /wa/v1/power
    def status(self) -> dict[str, Any]:
        """``{mode, system_required, display_required, session_locked}``(02 §3.6 #18 字段逐字)。"""
        cur = self._db.get_setting(CURRENT_KEY) or {}
        req = self._power.current_requests()
        return {"mode": cur.get("mode", self._cfg.keep_awake_mode), "system_required": bool(req.get("system_required")),
                "display_required": bool(req.get("display_required")), "session_locked": self._sys.session_locked()}

    def set_display_required(self, on: bool) -> None:
        """由**会话代理**回报:它已发 / 已撤 ``ES_DISPLAY_REQUIRED``(Session 0 发无效,故只能它来)。"""
        req = self._power.current_requests()
        self._power.set_execution_state(system_required=bool(req.get("system_required")), display_required=on)
        self.display_required_pending = not on

    # ---------------------------------------------------------------- H11 复核(每 10min)
    def recheck(self) -> dict[str, Any]:
        """04 H11:六项仍为 0?被外部改回则**重写并记审计**(``info``,不告警);keep-awake 请求丢失则重申请。

        🔴 **只在 keep-awake 真的应用过之后才复核**:以 ``settings`` 的 ``power.keepawake_current`` 为准,
        **不回落 ``winagent.toml [wechat] keep_awake_mode`` 的配置默认值**。理由:``apply()`` 才会写
        ``power.backup_json``;没应用过就复核 = 在**没有原值备份**的情况下把用户电源计划改成 0,关模块时还原不回去
        (04 §2.5.1「先把六项原值读出…只在第一次写时存」的前提被绕过)。
        """
        cur = self._db.get_setting(CURRENT_KEY)
        if not cur:                                     # 从未 apply 过(微信模块没启用 / 装完还没配)⇒ 什么都不做
            return {"mode": "off", "rewritten": [], "request_reapplied": False,
                    "session_locked": self._sys.session_locked(), "policy_degraded": self.policy_degraded,
                    "skipped": "never_applied"}
        mode = cur.get("mode", "off")
        fixed: list[str] = []
        if mode == "powercfg" and not self.policy_degraded:
            scheme = self._power.active_scheme()
            for item, v in self._power.query_timeouts(scheme, self._cfg.powercfg_items).items():
                if v != 0:
                    try:
                        self._power.change_timeout(scheme, item, 0)
                        fixed.append(item)
                    except PermissionError:
                        self.policy_degraded = True
            if fixed and self._audit:
                self._audit.record(actor="system", action="power.recheck", target=scheme, detail={"rewritten": fixed})
        req = self._power.current_requests()
        reapplied = False
        if mode in ("powercfg", "request") and not req.get("system_required"):
            self._power.set_execution_state(system_required=True, display_required=bool(req.get("display_required")))
            reapplied = True
        return {"mode": mode, "rewritten": fixed, "request_reapplied": reapplied,
                "session_locked": self._sys.session_locked(), "policy_degraded": self.policy_degraded}
