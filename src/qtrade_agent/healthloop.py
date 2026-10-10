"""账号级健康循环(04 §2.3 H04 / H05 / H06;02 §5「容器意外退出」自愈;告警码 02 §3.7)。

- **H04 容器存活**(每 ``[health] container_check_s``=10 s):``desired_state='running'`` 的 wsl 账号,容器退出(或不存在)⇒ ``state=error(CONTAINER_EXIT)``、
  alert ``H04_CONTAINER_EXITED``(crit);``OOMKilled=true`` 先推 ``CONTAINER_OOM_KILLED``(02 §3.7 登记 warn)再走同一退避重拉。
  自愈:退避 ``container_restart_backoff_s=[60,120,300,600]``,``restart_count < container_restart_max=5``(每小时清零)→ 自动 ``start``;超限**停止自愈**并保持告警。
- **H05 boot_completed 稳态**(每 60 s,仅企点 running/degraded):``getprop sys.boot_completed`` 从 1 变非 1 = Android 重启 ⇒ ``H05_BOOT_INCOMPLETE``(crit),等它回来。
- **H06 adb 连接与 root 态**(每 ``adb_check_s``=30 s,仅企点;R6-32 两个子判据互不影响):
  (a) 连接态:``adb devices`` 里该 serial 非 ``device`` ⇒ 先 ``disconnect`` + ``connect``,成功后追加 ``ensure_root``;``h06_fail_streak`` +1(内存态、按账号一个整数、不落库;成功一次清零),
      达 3 ⇒ 该账号 crit(只动该账号,**绝不 kill-server**);**宽限窗内(``health.is_rooting``)不判定 (a)**:不递增、不清零、不发连接态告警。
  (b) root 态:``whoami != root`` ⇒ 触发一次 ``ensure_root``(它自己发 warn ``QIDIAN_NOT_ROOT``、单独计数),永不升 crit、不计入 (a)。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .adapters.base import Account
from .alerts import (ACCOUNT_OFFLINE, AUTO_RESTART_EXHAUSTED, CONTAINER_OOM_KILLED, H04_CONTAINER_EXITED,
                     H05_BOOT_INCOMPLETE, H06_ADB_OFFLINE, QIDIAN_NOT_ROOT)
from .events import iso8601
from .config import AgentConfig

log = logging.getLogger("qtrade.healthloop")

H05_STEADY_INTERVAL_S = 60          # 04 H05 字面:稳态 60 s
WATCH_STATES = ("starting", "login_required", "logging_in", "running", "degraded", "error")
H06_STATES = ("login_required", "logging_in", "running", "degraded")


@dataclass
class RestartState:
    count: int = 0
    window_start_ms: int = 0
    next_ms: Optional[int] = None
    exhausted: bool = False
    history: list[int] = field(default_factory=list)


class HealthLoop:
    def __init__(self, *, store, runtime, accounts, alerts, health, cfg: AgentConfig, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._store = store
        self._runtime = runtime
        self._accounts = accounts
        self._alerts = alerts
        self._health = health
        self.cfg = cfg
        self._clock = clock
        self.restart: dict[str, RestartState] = {}
        self.h06_fail_streak: dict[str, int] = runtime.h06_fail_streak      # 04 owner 的内存态计数,与 runtime 共享同一 dict
        self.last: dict[str, dict[str, Any]] = {"H04": {}, "H05": {}, "H06": {}, "WECHAT": {}}
        self.wechat_adapter = None                                          # adapters.wechat.WechatAdapter(装配后注入)
        self.wechat_poller = None                                           # adapters.wechat.WechatPoller(同上)
        self.wechat_client = None                                           # adapters.wechat.WeChatWinAgent(同上)
        self.key_retry: dict[str, list[int]] = {}                           # 05 §2.5.4:每账号每小时 key_retry_per_hour 次

    # ------------------------------------------------------------------ 公共
    def _watched(self, states=WATCH_STATES) -> list[dict[str, Any]]:
        out = []
        for r in self._store.list_accounts():
            if r["host"] != "wsl" or not r["enabled"] or r.get("desired_state") != "running" or r["state"] not in states:
                continue
            out.append(r)
        return out

    @staticmethod
    def _serial(row: dict[str, Any]) -> str:
        return row.get("adb_serial") or f"127.0.0.1:{16000 + int(row['seq'])}"

    def checks(self) -> dict[str, str]:
        """``/system/health.checks`` 的 H04/H05/H06:任一账号 firing ⇒ firing;没跑过 ⇒ unknown。"""
        out = {}
        for code, key in ((H04_CONTAINER_EXITED, "H04"), (H05_BOOT_INCOMPLETE, "H05"), (H06_ADB_OFFLINE, "H06")):
            if not self.last[key]:
                out[key] = "unknown"
            else:
                out[key] = "firing" if any(k[0] == code for k in self._alerts.active) else "ok"
        return out

    # ------------------------------------------------------------------ H04
    async def check_containers(self) -> None:
        now = self._clock()
        backoff = list(self.cfg.health.container_restart_backoff_s)
        for row in self._watched():
            aid = row["id"]
            subject = f"account:{aid}"
            if self._accounts.busy(aid):
                continue                                             # 序列进行中(启动/停止)不判
            info = await self._runtime.inspect(row, fresh=True)
            self.last["H04"][aid] = {"running": info.running, "checked_ms": now}
            if info.exists and info.running:
                if self._alerts.is_firing(H04_CONTAINER_EXITED, subject):
                    self._alerts.resolve(H04_CONTAINER_EXITED, subject=subject, account_id=aid)
                continue
            # 本应 running 的容器退出 / 不存在
            st = self.restart.setdefault(aid, RestartState(window_start_ms=now))
            if now - st.window_start_ms >= 3600_000:                 # restart_count 每小时清零(02 §5)
                st.count, st.window_start_ms, st.exhausted, st.next_ms = 0, now, False, None
                self._alerts.resolve(AUTO_RESTART_EXHAUSTED, subject=subject, account_id=aid)
            if row["state"] != "error":
                self._accounts.transition(aid, "error", state_code="CONTAINER_EXIT",
                                          state_reason=f"容器退出 exit code {info.exit_code}" if info.exists else "容器不存在")
            if info.oom_killed:
                self._alerts.firing(CONTAINER_OOM_KILLED, subject=subject, account_id=aid,
                                    evidence={"memory_max_mb": row.get("mem_limit_mb"), "exit_code": info.exit_code}, hint_actions=["open_acct_detail"])
            self._alerts.firing(H04_CONTAINER_EXITED, subject=subject, account_id=aid,
                                evidence={"exit_code": info.exit_code, "oom_killed": info.oom_killed, "restart_count": st.count,
                                          "exhausted": st.exhausted, "next_restart_ms": st.next_ms})
            if st.count >= self.cfg.health.container_restart_max:
                if not st.exhausted:
                    st.exhausted = True
                    log.error("账号 %s 容器反复退出,已达 %d 次上限,停止自动重启", aid, st.count)
                    self._alerts.firing(H04_CONTAINER_EXITED, subject=subject, account_id=aid,
                                        evidence={"exit_code": info.exit_code, "oom_killed": info.oom_killed, "restart_count": st.count, "exhausted": True})
                # 02 §3.7 / §5:超限停止自愈并推独立码 AUTO_RESTART_EXHAUSTED(crit);每小时清零时 resolved(R6-57 ②)
                self._alerts.firing(AUTO_RESTART_EXHAUSTED, subject=subject, account_id=aid, hint_actions=["open_acct_detail"],
                                    evidence={"restart_count": st.count, "max": self.cfg.health.container_restart_max,
                                              "window_start_at": iso8601(st.window_start_ms), "exit_code": info.exit_code})
                continue                                             # 超限:停止自愈
            if st.next_ms is None:
                st.next_ms = now + backoff[min(st.count, len(backoff) - 1)] * 1000
            if now >= st.next_ms:
                st.count += 1
                st.history.append(now)
                st.next_ms = None
                try:
                    await self._accounts.start(aid, actor="system:health")
                except Exception as e:                                # 预算/状态不满足:下轮再看
                    log.warning("H04 自动重拉 %s 未受理: %s", aid, e)

    # ------------------------------------------------------------------ H05(稳态)
    async def check_boot(self) -> None:
        now = self._clock()
        for row in self._watched(("running", "degraded")):
            if row["channel"] != "qidian" or self._accounts.busy(row["id"]):
                continue
            aid, subject = row["id"], f"account:{row['id']}"
            v = (await self._runtime._adb.shell(self._serial(row), "getprop sys.boot_completed")).strip()
            self.last["H05"][aid] = {"boot_completed": v, "checked_ms": now}
            if v == "1":
                self._alerts.resolve(H05_BOOT_INCOMPLETE, subject=subject, account_id=aid)
            else:
                self._alerts.firing(H05_BOOT_INCOMPLETE, subject=subject, account_id=aid, evidence={"boot_completed": v, "phase": "steady"})

    # ------------------------------------------------------------------ 微信两条(05 §2.5.4 第 4/5 行)
    KEY_RETRY_PER_HOUR = 3          # 05 §2.5.4:chatlog 挂但微信在线 ⇒ 自动重试取钥,每小时上限

    async def check_wechat(self) -> None:
        """05 §2.5.4:
        - **chatlog 挂、微信在线**:``wechat/read`` 连续 3 次异常 ⇒ ``degraded(KEY_FAIL)``,自动重试试钥
          (``key_retry_per_hour=3``);超限停在 ``degraded`` 并 ``alert(error)``,**不是掉线、不进登录阶段**。
        - **锁屏**:周期读 #28 的 ``screen_locked`` ⇒ ``degraded(SCREEN_LOCKED)``;解锁自动回 ``running``
          (**不需要扫码,这不是登录**)。
        两条的状态判定都取自 ``WechatAdapter.probe_state()``(它已按 05 §2.4.7/§2.5.4 给出三元组)。
        """
        ad = self.wechat_adapter
        if ad is None:
            return
        now = self._clock()
        for row in self._store.list_accounts(channel="wechat"):
            aid = row["id"]
            if row["state"] not in ("running", "degraded") or self._accounts.busy(aid):
                continue
            acct = Account(id=aid, channel="wechat", state=row["state"], self_uid=row.get("self_uid"),
                           self_nick=row.get("self_nick"), state_code=row.get("state_code"))
            try:
                state, code, reason = await ad.probe_state(acct)
            except Exception as e:                       # 会话代理抖动:本轮不判,下轮再看
                log.info("微信健康探测未取到 account=%s: %s", aid, e)
                continue
            self.last["WECHAT"][aid] = {"state": state, "state_code": code, "checked_ms": now}
            if state != row["state"] or (code or "") != (row.get("state_code") or ""):
                self._accounts.transition(aid, state, state_code=code, state_reason=reason)
            # R6-91:只对「曾经取钥成功、跑起来过」的号自动试钥(self_uid 已回填)。首登没走完的号自动 key/retry 毫无意义
            # (取钥要人重登),反而反复拆掉用户正在做的那一轮;它们由控制台「重新取钥」重跑登录流(含回填/绑定)。
            if code == "KEY_FAIL" and row.get("self_uid"):
                await self._wechat_key_retry(aid, now)

    async def _wechat_key_retry(self, account_id: str, now: int) -> None:
        """``degraded(KEY_FAIL)`` 下自动重试取钥,每小时 ``KEY_RETRY_PER_HOUR`` 次;超限只 alert(error)不再试。"""
        hist = [t for t in self.key_retry.get(account_id, []) if now - t < 3600_000]
        subject = f"account:{account_id}"
        if len(hist) >= self.KEY_RETRY_PER_HOUR:
            self.key_retry[account_id] = hist
            self._alerts.firing(ACCOUNT_OFFLINE, subject=subject, severity="error", account_id=account_id,
                                evidence={"code": "KEY_FAIL", "key_retry_1h": len(hist), "exhausted": True},
                                hint_actions=["retry_key"])
            return
        hist.append(now)
        self.key_retry[account_id] = hist
        if self.wechat_client is None:
            return
        try:
            await self.wechat_client.key_retry()
        except Exception as e:
            log.warning("微信试钥(#35)失败 account=%s: %s", account_id, e)

    # ------------------------------------------------------------------ H06
    async def check_adb(self) -> None:
        now = self._clock()
        rows = [r for r in self._watched(H06_STATES) if r["channel"] == "qidian" and not self._accounts.busy(r["id"])]
        if not rows:
            return
        devices = await self._runtime._adb.devices()
        for row in rows:
            aid, serial, subject = row["id"], self._serial(row), f"account:{row['id']}"
            if self._health.is_rooting(aid):
                self.last["H06"][aid] = {"skipped": "rooting_grace", "checked_ms": now}
                continue                                             # 宽限窗内不判定 (a):不增不清不告警
            state = devices.get(serial)
            self.last["H06"][aid] = {"adb_state": state or "missing", "checked_ms": now}
            if state != "device":
                streak = self.h06_fail_streak[aid] = self.h06_fail_streak.get(aid, 0) + 1
                self._alerts.firing(H06_ADB_OFFLINE, subject=subject, account_id=aid, severity="crit" if streak >= 3 else "warn",
                                    evidence={"adb_state": state or "missing", "h06_fail_streak": streak})
                await self._runtime._adb.disconnect(serial)          # 只动该账号这一条连接(R6-12:不 kill-server)
                if await self._runtime._adb.connect(serial):
                    await self._runtime.ensure_root(row)             # reconnect 成功后复提权(R6-28)
                continue
            if self.h06_fail_streak.get(aid):
                self.h06_fail_streak[aid] = 0
            self._alerts.resolve(H06_ADB_OFFLINE, subject=subject, account_id=aid)
            # (b) root 态:独立子判据,永不 crit
            whoami = (await self._runtime._adb.shell(serial, "whoami")).strip()
            if whoami != "root":
                await self._runtime.ensure_root(row)
            else:
                self._runtime.qidian_root_fail_streak[aid] = 0
                self._alerts.resolve(QIDIAN_NOT_ROOT, subject=subject, account_id=aid)
