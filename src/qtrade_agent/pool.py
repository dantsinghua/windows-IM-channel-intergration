"""pool(02 §2.2.5 资源管理器)—— 最小版:``can_add`` / ``reserve`` / ``release`` / ``snapshot``;``calibrate`` 本期不做。

算法(02 §2.2.5 伪代码逐字落地):
    wsl_budget = wsl.total_mb - wsl.reserved_mb
    used = Σ quota_mb of accounts where host='wsl' and enabled and state ∉ {stopped, disabled, error}
    can_add(qidian) = wsl_budget - used >= quota.qidian
    can_add(qq)     = wsl_budget - used >= quota.qq
    can_add(wechat) = windows.wechat_enabled AND slot.holder == '' AND slot.pending == '' and windows.total_mb - windows.reserved_mb >= quota.wechat
``resource_pools`` 两行是真值(``agent.toml [pool] quota_*`` 只是首次建表初始值,C-43);``stopped`` 不占额度但 ``start`` 要再过 ``can_add``。
WinAgent 服务不在线 ⇒ ``windows`` 池 ``unknown``、``can_add(wechat)=false``(02 §2.5 降级 ①)。
``reserve`` 的「检查+写库」用数据库行级 claim(R-08:条件 UPDATE + rowcount),不用内存锁。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from .config import AgentConfig
from .events import iso8601

CHANNELS = ("qidian", "qq", "wechat")


def _read_meminfo_total_mb(path: str = "/proc/meminfo") -> Optional[int]:
    """WSL 内 MemTotal 就是 ``.wslconfig memory=`` 落地后的 VM 内存(经 WinAgent 读不到时的本地兜底)。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


class Pool:
    def __init__(self, store, cfg: AgentConfig, *, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 wsl_total_mb: Optional[int] = None, windows_total_mb: int = 0):
        self._store = store
        self.cfg = cfg
        self._clock = clock
        self.windows_known = False                      # WinAgent health 快照到手前 = unknown
        self.realtime: dict[str, Optional[int]] = {"wsl_anon_mb": None, "win_available_mb": None}
        total = wsl_total_mb if wsl_total_mb is not None else (_read_meminfo_total_mb() or 0)
        store.ensure_pools(quota={"qidian": cfg.pool.quota_qidian_mb, "qq": cfg.pool.quota_qq_mb, "wechat": cfg.pool.quota_wechat_mb},
                           wsl_total_mb=total, wsl_reserved_mb=cfg.pool.wsl_reserved_mb, windows_total_mb=windows_total_mb,
                           windows_reserved_mb=cfg.pool.windows_reserved_mb, now_ms=clock())

    # ---- 输入(WinAgent 快照 / 手动)
    def set_wsl_total(self, total_mb: int, *, source: str = "winagent") -> None:
        self._store.pool_set("wsl", total_mb=int(total_mb), source=source, now_ms=self._clock())

    def set_windows(self, *, total_mb: Optional[int] = None, wechat_enabled: Optional[bool] = None, available_mb: Optional[int] = None,
                    known: bool = True) -> None:
        cols: dict[str, Any] = {}
        if total_mb is not None:
            cols["total_mb"] = int(total_mb)
            cols["source"] = "winagent"
        if wechat_enabled is not None:
            cols["wechat_enabled"] = bool(wechat_enabled)
        if cols:
            self._store.pool_set("windows", now_ms=self._clock(), **cols)
        if available_mb is not None:
            self.realtime["win_available_mb"] = int(available_mb)
        self.windows_known = known

    # ---- 算法
    def quota(self, channel: str) -> int:
        return int(self._store.pool_get("wsl")["quota"][channel])

    def wsl_budget(self) -> int:
        w = self._store.pool_get("wsl")
        return int(w["total_mb"]) - int(w["reserved_mb"])

    def used_mb(self, *, exclude_account_id: Optional[str] = None) -> int:
        return self._store.pool_used_mb(exclude_id=exclude_account_id)

    def free_mb(self, *, exclude_account_id: Optional[str] = None) -> int:
        return self.wsl_budget() - self.used_mb(exclude_account_id=exclude_account_id)

    def can_add(self, channel: str, *, extra_mb: int = 0, exclude_account_id: Optional[str] = None) -> tuple[bool, str, list[dict[str, Any]]]:
        """返回 ``(ok, reason, alternatives)``;``extra_mb`` 供 PATCH quota_mb 增量校验;``exclude_account_id`` = start/restart 排除自身(R6-55)。"""
        if channel not in CHANNELS:
            return False, "unknown_channel", []
        if channel == "wechat":
            win = self._store.pool_get("windows")
            if not self.windows_known:
                return False, "winagent_offline", []
            if not win["wechat_enabled"]:
                return False, "wechat_disabled", []
            if win["slot_holder"] != "":
                return False, "slot_held", [{"kind": "wechat_switch", "holder": win["slot_holder"]}]
            if win["slot_pending"] != "":
                # R6-54 钉死 alternatives[].kind ∈ {add_other_channel, stop_one, wechat_switch};
                # 原实现给的 `wait_or_cancel` 在枚举外(见 `.omc/handoffs/wechat-channel.md` 既有缺陷 2),
                # 保守收进 `wechat_switch`(#17/#18 正是「等/取消 pending 或接管」的入口),不新增枚举值。
                return False, "slot_pending", [{"kind": "wechat_switch", "holder": "", "pending": win["slot_pending"]}]
            if int(win["total_mb"]) - int(win["reserved_mb"]) < int(win["quota"]["wechat"]):
                return False, "windows_budget", []
            return True, "", []
        need = self.quota(channel) if not extra_mb else extra_mb
        free = self.free_mb(exclude_account_id=exclude_account_id)
        if free >= need:
            return True, "", []
        return False, "wsl_budget", self._alternatives(channel, need, free, exclude_account_id)

    def _alternatives(self, channel: str, need: int, free: int, exclude_account_id: Optional[str] = None) -> list[dict[str, Any]]:
        """C.3.3「可改开 QQ / 停用一个企点」:每项 ``{kind, channel?, account_ids?, need_mb, free_mb}``。"""
        alts: list[dict[str, Any]] = []
        for other in ("qq", "qidian"):
            if other != channel and free >= self.quota(other):
                alts.append({"kind": "add_other_channel", "channel": other, "need_mb": self.quota(other), "free_mb": free})
        rows = self._store.list_accounts()
        running = [r["id"] for r in rows if r["host"] == "wsl" and r["enabled"] and r["state"] not in ("stopped", "disabled", "error")
                   and r["id"] != exclude_account_id]
        if running:
            alts.append({"kind": "stop_one", "account_ids": running, "need_mb": need, "free_mb": free})
        return alts

    def reserve(self, account_id: str, channel: str) -> bool:
        """行级 claim(R-08):``UPDATE resource_pools … WHERE 剩余 ≥ quota``,rowcount==1 判抢到;wechat 走槽位 claim(§2.2.5),本期不在此。"""
        if channel == "wechat":
            ok, _r, _a = self.can_add("wechat")
            return ok
        return self._store.pool_claim_wsl(self.quota(channel), now_ms=self._clock())

    def release(self, account_id: str) -> None:
        """额度由 accounts.state 推导(stopped/disabled/error 自动不计),这里只刷新时间戳供 P-RES「上次释放」。"""
        self._store.pool_set("wsl", now_ms=self._clock())

    # ---- 出数(00 §7.6 ResourcePool)
    def snapshot(self) -> dict[str, Any]:
        wsl = self._store.pool_get("wsl")
        win = self._store.pool_get("windows")
        used = self.used_mb()
        budget = int(wsl["total_mb"]) - int(wsl["reserved_mb"])
        free = max(0, budget - used)
        q = wsl["quota"]
        can = {"qidian": free // int(q["qidian"]) if q["qidian"] else 0, "qq": free // int(q["qq"]) if q["qq"] else 0,
               "wechat": 1 if self.can_add("wechat")[0] else 0}
        exp = win["slot_pending_expires_ms"]
        windows: dict[str, Any] = {
            "total_mb": int(win["total_mb"]), "reserved_mb": int(win["reserved_mb"]), "wechat_mb": int(q["wechat"]),
            "wechat_enabled": bool(win["wechat_enabled"]), "status": "ok" if self.windows_known else "unknown",
            "wechat_slots": {"used": 1 if win["slot_holder"] else 0, "max": 1, "holder": win["slot_holder"] or "", "pending": win["slot_pending"] or "",
                             "pending_expires_at": iso8601(exp) if exp else None,
                             "pending_login_session_id": win["slot_pending_login_session_id"] or ""},      # R6-6 空值口径:三个串出 "",只有时刻出 null
        }
        return {"pools": {"wsl": {"total_mb": int(wsl["total_mb"]), "reserved_mb": int(wsl["reserved_mb"]), "used_mb": used, "free_mb": free},
                          "windows": windows},
                "realtime": dict(self.realtime), "quota_mb": {k: int(v) for k, v in q.items()}, "can_add": can}

    def calibrate(self, account_id: str) -> None:
        raise NotImplementedError("资源池自校准(M2.9)本期未接")
