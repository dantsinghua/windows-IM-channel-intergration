"""账号级健康循环(04 §2.3 H04 / H05 / H06;02 §5「容器意外退出」自愈;告警码 02 §3.7)。

- **H04 容器存活**(每 ``[health] container_check_s``=10 s):``desired_state='running'`` 的 wsl 账号,容器退出(或不存在)⇒ ``state=error(CONTAINER_EXIT)``、
  alert ``H04_CONTAINER_EXITED``(crit);``OOMKilled=true`` 先推 ``CONTAINER_OOM_KILLED``(02 §3.7 登记 warn)再走同一退避重拉。
  自愈:退避 ``container_restart_backoff_s=[60,120,300,600]``,``restart_count < container_restart_max=5``(每小时清零)→ 自动 ``start``;超限**停止自愈**并保持告警。
- **H05 boot_completed 稳态**(每 60 s,仅企点 running/degraded):``getprop sys.boot_completed`` 从 1 变非 1 = Android 重启 ⇒ ``H05_BOOT_INCOMPLETE``(crit),等它回来。
- **H06 adb 连接与 root 态**(每 ``adb_check_s``=30 s,仅企点;R6-32 两个子判据互不影响):
  (a) 连接态:``adb devices`` 里该 serial 非 ``device`` ⇒ 先 ``disconnect`` + ``connect``,成功后追加 ``ensure_root``;``h06_fail_streak`` +1(内存态、按账号一个整数、不落库;成功一次清零),
      达 3 ⇒ 该账号 crit；共享恢复仅允许 R6-112 持久化预算与全部参与者同意的专用协调器执行;**宽限窗内(``health.is_rooting``)不判定 (a)**:不递增、不清零、不发连接态告警。
  (b) root 态:``whoami != root`` ⇒ 触发一次 ``ensure_root``(它自己发 warn ``QIDIAN_NOT_ROOT``、单独计数),永不升 crit、不计入 (a)。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .adapters.base import Account
from .alerts import (ACCOUNT_OFFLINE, AUTO_RESTART_EXHAUSTED, CONTAINER_OOM_KILLED, H04_CONTAINER_EXITED,
                     H05_BOOT_INCOMPLETE, H06_ADB_OFFLINE, QIDIAN_NOT_ROOT)
from .ids import ulid
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
        self._adb_probe_lock = asyncio.Lock()
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
            if self._accounts.busy(aid) or self._accounts.qq_login_pending(aid):
                continue                                             # 启动/停止/扫码收尾进行中不判，避免拆容器的空档被写成「容器不存在」
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
        self._clear_false_wechat_offline()
        for row in self._store.list_accounts(channel="wechat", include_deleted=True):
            aid = row["id"]
            recovering = (row["state"] == "login_required"
                          and row.get("state_code") in ("LOGGED_OUT", "KICKED", "TOKEN_EXPIRED", "LOGIN_TIMEOUT")
                          and bool(row.get("self_uid")) and row.get("desired_state") == "running"
                          and bool(row.get("auto_recover")))
            if (row.get("deleted_ms") or not row.get("enabled")
                    or (row["state"] not in ("running", "degraded") and not recovering)
                    or self._accounts.busy(aid)):
                continue
            acct = Account(id=aid, channel="wechat", state=row["state"], self_uid=row.get("self_uid"),
                           self_nick=row.get("self_nick"), state_code=row.get("state_code"))
            try:
                # Observe an already restored same-account session; never launch,
                # log in, bind, or retry keys on this passive recovery path.
                state, code, reason = (await ad.probe_state(acct, require_identity=True)
                                       if recovering else await ad.probe_state(acct))
            except Exception as e:                       # 会话代理抖动:本轮不判,下轮再看
                log.info("微信健康探测未取到 account=%s: %s", aid, e)
                continue
            current = self._store.get_account_full(aid)
            if (not current or current.get("deleted_ms") or not current.get("enabled")
                    or current["state"] != row["state"] or self._accounts.busy(aid)
                    or current.get("state_code") != row.get("state_code")
                    or current.get("self_uid") != row.get("self_uid")
                    or (recovering and not current.get("auto_recover"))
                    or current.get("desired_state") != row.get("desired_state")):
                continue
            self.last["WECHAT"][aid] = {"state": state, "state_code": code, "checked_ms": now}
            if recovering:
                if state != "running":
                    continue
                self._accounts.transition(aid, "logging_in")
            if state != row["state"] or (code or "") != (row.get("state_code") or ""):
                self._accounts.transition(aid, state, state_code=code, state_reason=reason)
            # R6-91:只对「曾经取钥成功、跑起来过」的号自动试钥(self_uid 已回填)。首登没走完的号自动 key/retry 毫无意义
            # (取钥要人重登),反而反复拆掉用户正在做的那一轮;它们由控制台「重新取钥」重跑登录流(含回填/绑定)。
            if (code == "KEY_FAIL" and row.get("self_uid") and row.get("auto_recover")
                    and getattr(ad, "key_retry_required", lambda _aid: False)(aid)):
                await self._wechat_key_retry(aid, now)
            elif state == "running":
                self._alerts.resolve(ACCOUNT_OFFLINE, subject=f"account:{aid}", account_id=aid)

    def _clear_false_wechat_offline(self) -> None:
        """``ACCOUNT_OFFLINE`` 只属于 05 §2.5.4 的掉线(``login_required`` + ``KICKED|LOGGED_OUT|TOKEN_EXPIRED|LOGIN_TIMEOUT``)。

        取钥耗尽、已停止、已软删都不是掉线。旧进程把 ``KEY_FAIL`` 耗尽错记成这条告警，
        outbox 重放后抽屉会一直挂着；每个账号只补一次 ``resolved``。
        """
        offline = {"KICKED", "LOGGED_OUT", "TOKEN_EXPIRED", "LOGIN_TIMEOUT"}
        accounts = {r["id"]: r for r in self._store.list_accounts(include_deleted=True)}

        def real(aid: str) -> bool:
            row = accounts.get(aid)
            return bool(row and not row.get("deleted_ms") and row.get("state") == "login_required"
                        and row.get("state_code") in offline)

        for (code, subject) in list(self._alerts.active):
            if code != ACCOUNT_OFFLINE or not subject.startswith("account:"):
                continue
            aid = subject.split(":", 1)[1]
            if not real(aid):
                self._alerts.resolve(ACCOUNT_OFFLINE, subject=subject, account_id=aid)
        if getattr(self, "_outbox_offline_swept", False):
            return
        self._outbox_offline_swept = True
        latest: dict[str, str] = {}
        try:
            rows = self._store.list_events(event="alert")
        except Exception:
            rows = []
        for row in rows:
            payload = row.get("payload") or {}
            if payload.get("code") != ACCOUNT_OFFLINE:
                continue
            subject = str(payload.get("subject") or "")
            state = str(payload.get("state") or "")
            if subject.startswith("account:") and state in ("firing", "resolved"):
                latest[subject] = state
        for subject, state in latest.items():
            aid = subject.split(":", 1)[1]
            if state == "firing" and not real(aid):
                self._alerts.settle(ACCOUNT_OFFLINE, subject=subject, account_id=aid)

    async def _wechat_key_retry(self, account_id: str, now: int) -> None:
        """``degraded(KEY_FAIL)`` 下自动重试取钥,每小时 ``KEY_RETRY_PER_HOUR`` 次;超限停在 ``degraded``,不再试,也**不是掉线**。"""
        hist = [t for t in self.key_retry.get(account_id, []) if now - t < 3600_000]
        if len(hist) >= self.KEY_RETRY_PER_HOUR:
            self.key_retry[account_id] = hist
            log.warning("微信取钥重试已达每小时上限 account=%s retries=%s,保持 degraded(KEY_FAIL)", account_id, len(hist))
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
        """Observe once, then at most one shared recovery per sweep."""
        if self._adb_probe_lock.locked():
            return
        async with self._adb_probe_lock:
            await self._check_adb_once()

    async def _check_adb_once(self) -> None:
        now = self._clock()
        rows = [r for r in self._watched(H06_STATES) if r["channel"] == "qidian" and not self._accounts.busy(r["id"])]
        if not rows:
            return
        devices = await self._runtime._adb.devices()
        failed = []
        for row in rows:
            aid, serial, subject = row["id"], self._serial(row), "account:" + row["id"]
            if self._health.is_rooting(aid):
                self.last["H06"][aid] = {"skipped": "rooting_grace", "checked_ms": now}
                continue
            state = devices.get(serial)
            self.last["H06"][aid] = {"adb_state": state or "missing", "checked_ms": now}
            if state != "device":
                streak = self.h06_fail_streak[aid] = self.h06_fail_streak.get(aid, 0) + 1
                self._alerts.firing(H06_ADB_OFFLINE, subject=subject, account_id=aid, severity="crit" if streak >= 3 else "warn",
                                    evidence={"adb_state": state or "missing", "h06_fail_streak": streak})
                failed.append(row)
                continue
            self.h06_fail_streak[aid] = 0
            self._alerts.resolve(H06_ADB_OFFLINE, subject=subject, account_id=aid)
            whoami = (await self._runtime._adb.shell(serial, "whoami")).strip()
            if whoami != "root":
                await self._runtime.ensure_root(row)
            else:
                self._runtime.qidian_root_fail_streak[aid] = 0
                self._alerts.resolve(QIDIAN_NOT_ROOT, subject=subject, account_id=aid)
        if failed and any(self.h06_fail_streak[r["id"]] >= 3 for r in failed):
            if await self._recover_adb_shared(devices):
                return
        for row in failed:
            current = self._store.get_account_full(row["id"])
            if not self._adb_row_active(current) or self._accounts.busy(row["id"]):
                continue
            serial = self._serial(current)
            await self._runtime._adb.disconnect(serial)
            if await self._runtime._adb.connect(serial):
                await self._runtime.ensure_root(current)

    @staticmethod
    def _adb_row_active(row) -> bool:
        return bool(row and row.get("channel") == "qidian" and row.get("host") == "wsl"
                    and row.get("enabled") and not row.get("deleted_ms")
                    and row.get("desired_state") == "running" and row.get("state") in H06_STATES)

    async def _recover_adb_shared(self, devices: dict[str, str]) -> bool:
        """A daemon reset affects every serial, so every participant must consent."""
        if not self.cfg.health.adb_selfheal_enabled:
            return False
        participants = [r for r in self._store.list_accounts(channel="qidian")
                        if r.get("enabled") and r.get("desired_state") == "running"]
        if not participants or any(not self._adb_row_active(r) or not r.get("auto_recover")
                                   or self._accounts.busy(r["id"]) or self._health.is_rooting(r["id"])
                                   for r in participants):
            return False
        serials = {self._serial(r) for r in participants}
        expected = {"127.0.0.1:" + str(16000 + int(r["seq"])) for r in participants}
        if serials != expected or set(devices) - serials:
            log.warning("ADB shared recovery deferred: unowned or unexpected serial")
            return False
        budget = self._store.reserve_recovery_attempt(
            "recovery.adb.16000", now_ms=self._clock(), maximum=self.cfg.health.adb_selfheal_max_per_hour)
        if not budget["allowed"]:
            if budget["reason"] in ("recovery_budget_exhausted", "recovery_budget_invalid"):
                self._alerts.firing("ADB_SELFHEAL_EXHAUSTED", subject="adb:16000", severity="warn",
                                    evidence=budget, hint_actions=["open_logs"])
            return False
        self._alerts.resolve("ADB_SELFHEAL_EXHAUSTED", subject="adb:16000")
        trace = ulid(self._clock())
        log.warning("ADB shared recovery attempt=%d trace_id=%s participants=%d", budget["attempts"], trace, len(participants))
        reason = "OK"
        try:
            await self._runtime._adb.kill_server()
            # Re-read desired state after the await; never reconnect an account just stopped.
            current = [self._store.get_account_full(r["id"]) for r in participants]
            current = [r for r in current if self._adb_row_active(r) and r.get("auto_recover") and not self._accounts.busy(r["id"])]
            await self._runtime._adb.reconnect_all([self._serial(r) for r in current])
            for row in current:
                latest = self._store.get_account_full(row["id"])
                if self._adb_row_active(latest) and not self._accounts.busy(row["id"]):
                    if not await self._runtime.ensure_root(latest):
                        reason = "ADB_ROOT_NOT_READY"
        except asyncio.CancelledError:
            reason = "ADB_RECOVERY_CANCELLED"
            raise
        except Exception as error:
            reason = getattr(error, "reason", "ADB_RECOVERY_FAILED")
            log.warning("ADB recovery incomplete trace_id=%s cause=%s type=%s", trace, reason, type(error).__name__)
        finally:
            self._store.insert_audit(kind="system", transport="system", actor="system:health",
                                     action="adb.selfheal", result_code=reason, trace_id=trace,
                                     detail={"attempt": budget["attempts"], "participants": [r["id"] for r in participants]},
                                     now_ms=self._clock())
        # Even a failed attempt owns this sweep; no duplicate reset or fallthrough.
        return True
