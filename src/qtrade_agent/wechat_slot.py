"""微信单槽位状态机(02 §2.2.5「微信槽位」+ 05 §2.4.2 槽位视图 / §2.4.5 切换;基线 §11.18 [SLOT])。

槽位落在 ``resource_pools(pool='windows')`` 的四列 ``slot_holder / slot_pending / slot_pending_expires_ms /
slot_pending_login_session_id``(02 §3.1 DDL)。逐条红线:

- **`holder` = 真实在线登录态,绝不自动释放**;`pending` = 正在登录/切换中的临时占位,**失败即释放 + reaper 兜底**。
  两套语义不可混用(05 §2.4.2 R-04)。
- **置 `pending` 用数据库行级 claim**(R-08 §2.3.1 ③):``WHERE slot_holder='' AND slot_pending=''`` 的条件 UPDATE,
  ``rowcount==1`` 判抢到;三列在**同一条 UPDATE** 里一起写(R6-6)。
- **三条释放路径**(02 §2.2.5):①失败即释放(不等 TTL)②TTL 到期由 ``wechat_slot_reaper`` 每 60 s 扫
  ③用户手动 ``#16b login/cancel``(必带 ``login_session_id``,不等 ⇒ 幂等 no-op,**绝不误杀新尝试**)。
  释放时**同步回收中间产物**(临时 hook/chatlog 进程、半截密钥、``accounts/<id>/tmp``;复用 ``runtime._purge_ephemeral``)。
- **`pending → holder` 只在成功时转换**;转换时尝试 id 随 `pending` 一起清(`holder` 不记 ``login_session_id``)。
- **故障 holder 的接管(R4-8 + R5-4)**:判据 = ``account_runtime.error_since_ms`` 非空且
  ``now − error_since_ms ≥ [wechat] slot_error_takeover_s×1000``,**且** #17/#18 带 ``confirm:true``;
  两者缺一律回 ``409 RESOURCE_EXHAUSTED / reason='slot_held_by_error'``。**绝不自动接管**;
  claim 的 ``WHERE slot_holder=''`` **不放宽**——接管靠「先把故障 holder 停成 stopped 腾空」再抢。
- **切换(05 §2.4.5)**:排空 holder(``switch_drain_timeout_s``)→ 停 chatlog → 登出(#34)→ holder `stopped`
  → `pending=目标` → 目标走 §2.4.4 ③~⑦;任一步失败 ⇒ 槽位空、目标回 `stopped`,**不会自动把原号登回去**。

本模块只做**槽位与切换编排**;账号状态迁移、容器/临时产物回收、登录流分别经注入的
``transition`` / ``purge`` / ``stop_account`` / ``begin_login`` 回调完成(文件所有权:accounts.py / runtime / api 由总控接线)。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .adapters.wechat.client import WeChatNotReady, WeChatWinAgent
from .adapters.wechat.settings import WechatAdapterConfig
from .api.auth import ApiError
from .config import AgentConfig
from .ids import login_session_id as new_login_session_id

log = logging.getLogger("qtrade.wechat_slot")

REAPER_AUDIT = "slot_pending_expired"          # 02 §2.2.5 ②
CANCEL_AUDIT = "slot_pending_cancelled"        # 02 §3.4.1 #16b 微信分支(与 accounts.login_cancel 同名)
SWITCH_AUDIT = "account.switch"
TAKEOVER_AUDIT = "account.switch_takeover"


@dataclass
class SlotState:
    """``ResourcePool.windows.wechat_slots``(00 §7.6 / 05 §2.4.2 字段表)。

    🔴 空值口径(R6-6):``holder``/``pending``/``pending_login_session_id`` 三个字符串字段无值时一律 ``""``;
    **只有 `pending_expires_ms` 是 `None`**。消费方一律按「非空串」判,勿写 ``!= null``。
    """
    holder: str = ""
    pending: str = ""
    pending_expires_ms: Optional[int] = None
    pending_login_session_id: str = ""

    @property
    def used(self) -> int:
        return 1 if self.holder else 0


class WechatSlot:
    def __init__(self, *, store, events, cfg: AgentConfig, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 client: Optional[WeChatWinAgent] = None, wechat_cfg: Optional[WechatAdapterConfig] = None,
                 purge: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
                 transition: Optional[Callable[..., dict[str, Any]]] = None):
        self._store = store
        self._events = events
        self.cfg = cfg
        self.wechat_cfg = wechat_cfg or WechatAdapterConfig()
        self._clock = clock
        self._client = client
        self._purge = purge                     # runtime._purge_ephemeral(acct_row) —— 回收中间产物
        self._transition = transition           # accounts.transition(id, state, ...)

    # ------------------------------------------------------------------ 读
    def view(self) -> SlotState:
        win = self._store.pool_get("windows") or {}
        return SlotState(holder=win.get("slot_holder") or "", pending=win.get("slot_pending") or "",
                         pending_expires_ms=win.get("slot_pending_expires_ms"),
                         pending_login_session_id=win.get("slot_pending_login_session_id") or "")

    def pending_remaining_s(self, now_ms: Optional[int] = None) -> Optional[int]:
        """``P-ACCT`` 微信组显示的 pending 剩余秒数(05 §2.4.2 R-04);无 pending 回 ``None``。"""
        s = self.view()
        if not s.pending or s.pending_expires_ms is None:
            return None
        return max(0, (s.pending_expires_ms - (now_ms or self._clock())) // 1000)

    # ------------------------------------------------------------------ 置 pending / 释放 / 转 holder
    def claim(self, target: str, login_session_id: str, *, now_ms: Optional[int] = None) -> bool:
        """02 §2.2.5 行级 claim:三列同一条 UPDATE,``rowcount==1`` 判抢到;
        ``pending_expires_at = now + [wechat] slot_pending_ttl_s×1000``(⚠️ **agent.toml** 的 ``[wechat]``,§15b N-2)。"""
        now = now_ms or self._clock()
        expires = now + self.cfg.wechat.slot_pending_ttl_s * 1000
        return bool(self._store.wechat_slot_claim(target, login_session_id, expires, now_ms=now))

    async def release_pending(self, target: Optional[str] = None, *, reason: str, actor: str = "system",
                              audit: Optional[str] = None, now_ms: Optional[int] = None) -> int:
        """①失败即释放 / ③用户手动:三列一起清 + **同步回收该次绑定的中间产物**(否则下次绑定读到脏状态)。"""
        now = now_ms or self._clock()
        ls = self.view().pending_login_session_id
        n = self._store.wechat_slot_release_pending(target, now_ms=now)
        if n and target:
            await self._purge_for(target)
            self._store.insert_audit(kind="system", transport="system", actor=actor, action=audit or CANCEL_AUDIT,
                                     account_id=target, result_code="OK", detail={"reason": reason, "login_session_id": ls}, now_ms=now)
        return n

    def promote(self, target: str, *, now_ms: Optional[int] = None) -> bool:
        """**成功**才把 `pending` 转 `holder`:``slot_holder=slot_pending`` 且三列一起清(02 §2.2.5)。

        ⚠️ 列的写入顺序不可调:``resource_pools`` 的第二条 CHECK 是「没有 pending 就不该有过期时刻/残留尝试 id」,
        故必须先清 ``expires``/``login_session_id``、最后清 ``slot_pending``。
        (handoff 已建议给 ``store`` 补一个与规格同形的单条 ``wechat_slot_promote()`` UPDATE 取代本处。)"""
        now = now_ms or self._clock()
        s = self.view()
        if not s.pending or s.pending != target or s.holder:
            return False
        self._store.pool_set("windows", now_ms=now, slot_holder=target, slot_pending_expires_ms=None,
                             slot_pending_login_session_id="", slot_pending="")
        return True

    def release_holder(self, holder: str, *, now_ms: Optional[int] = None) -> int:
        """`holder` 的释放**只走**正常 ``stopping → stopped``(#6/#10);本方法是那条路径的落点,不受 TTL 约束。"""
        return int(self._store.wechat_slot_release_holder(holder, now_ms=now_ms or self._clock()))

    async def _purge_for(self, account_id: str) -> None:
        if self._purge is None:
            return
        row = self._store.get_account_full(account_id)
        if row is None:
            return
        try:
            await self._purge(row)
        except Exception as e:                                   # 回收失败不该把释放路径带塌(下次 start 前还会补做一次)
            log.warning("释放槽位时回收中间产物失败 account=%s: %s", account_id, e)

    # ------------------------------------------------------------------ ② reaper(每 60 s)
    async def reap(self, *, now_ms: Optional[int] = None) -> list[str]:
        """``scheduler`` 注册的 ``wechat_slot_reaper``:``slot_pending<>'' AND slot_pending_expires_ms < now`` ⇒ 三列一起清,
        对被释放的目标发 ``account_state``(payload 里带该次尝试的 ``login_session_id`` 标明「是这一次过期了」;
        **槽位侧三列此刻已清空**,对外 ``pending_login_session_id`` 即 ``""``)+ 记审计 ``slot_pending_expired`` + ``_purge_ephemeral``。

        **幂等**:条件一旦命中即置 NULL,多实例/重入不会重复释放。"""
        now = now_ms or self._clock()
        s = self.view()
        if not s.pending or s.pending_expires_ms is None or s.pending_expires_ms >= now:
            return []
        target, ls = s.pending, s.pending_login_session_id
        if not self._store.wechat_slot_release_pending(target, now_ms=now):
            return []
        await self._purge_for(target)
        self._store.insert_audit(kind="system", transport="system", actor="system:wechat_slot_reaper", action=REAPER_AUDIT,
                                 account_id=target, result_code="OK", detail={"login_session_id": ls, "expired_ms": s.pending_expires_ms}, now_ms=now)
        self._to_stopped(target, ls, "登录尝试已超时(pending TTL 到期)")
        return [target]

    def _to_stopped(self, account_id: str, ls: str, reason: str) -> None:
        if self._transition is not None:
            self._transition(account_id, "stopped", state_reason=reason, login_session_id=ls, desired_state="stopped")
            return
        row = self._store.get_account_full(account_id)           # 未注入状态机时只发事件,不改状态(测试/只读场景)
        if row is not None:
            self._events.emit("account_state", payload={"id": account_id, "state": row["state"], "state_reason": reason,
                                                        "login_session_id": ls},
                              account_id=account_id, channel="wechat", now_ms=self._clock())

    # ------------------------------------------------------------------ 故障 holder 接管判定(R5-4)
    def takeover_check(self, *, now_ms: Optional[int] = None) -> tuple[bool, Optional[int]]:
        """返回 ``(可接管, 已故障秒数)``。唯一判据 = ``account_runtime.error_since_ms``:
        ``error_since_ms IS NOT NULL AND (now − error_since_ms) >= slot_error_takeover_s*1000``;``NULL`` ⇒ 恒不可接管。"""
        now = now_ms or self._clock()
        holder = self.view().holder
        if not holder:
            return False, None
        rt = self._store.get_runtime(holder) or {}
        since = rt.get("error_since_ms")
        if since is None:
            return False, None
        seconds = (now - int(since)) // 1000
        return seconds >= self.cfg.wechat.slot_error_takeover_s, seconds

    def _raise_slot_held_by_error(self, holder: str, seconds: Optional[int]) -> None:
        n = seconds if seconds is not None else 0
        raise ApiError(409, "RESOURCE_EXHAUSTED", f"微信槽位被故障号 {holder} 占用(已故障 {n} 秒),确认后可接管",
                       reason="slot_held_by_error", extra={"hint_actions": ["wechat_switch"], "holder": holder, "error_seconds": n})

    # ------------------------------------------------------------------ #17/#18 切换
    async def switch(self, target: str, *, confirm: bool = False, actor: str = "token:console",
                     stop_account: Optional[Callable[[str], Awaitable[None]]] = None,
                     begin_login: Optional[Callable[[str, str], Awaitable[None]]] = None,
                     drain: Optional[Callable[[str, float], Awaitable[bool]]] = None) -> dict[str, Any]:
        """05 §2.4.5 逐步落地 → ``202 {holder_before, target, login_session_id}``。

        ``stop_account(id)``/``begin_login(id, ls)``/``drain(id, timeout_s)`` 由装配方注入(分别是
        ``accounts.stop`` 收口、登录流入口、总线排空);缺省时该步跳过(单测按步验证)。
        """
        now = self._clock()
        row = self._store.get_account_full(target)
        if row is None or row.get("deleted_ms"):
            raise ApiError(404, "TARGET_NOT_FOUND", f"账号不存在:{target}")
        if row["channel"] != "wechat":
            raise ApiError(409, "NOT_APPLICABLE", "switch 只对微信通道", reason="not_wechat")
        s = self.view()
        if s.pending:
            raise ApiError(409, "RESOURCE_EXHAUSTED", f"微信槽位正在切换中(pending={s.pending})", reason="slot_pending",
                           extra={"pending": s.pending, "pending_login_session_id": s.pending_login_session_id})
        holder_before = s.holder
        if holder_before and holder_before != target:
            hrow = self._store.get_account_full(holder_before) or {}
            if hrow.get("state") == "error":
                ok, seconds = self.takeover_check(now_ms=now)
                if not (ok and confirm):
                    self._raise_slot_held_by_error(holder_before, seconds)
                # 接管:先把故障 holder 停成 stopped(令 slot_holder='')再让目标 claim;claim 的 WHERE 不放宽
                self._store.insert_audit(kind="system", transport="system", actor=actor, action=TAKEOVER_AUDIT, account_id=target,
                                         result_code="OK", detail={"holder_before": holder_before, "error_seconds": seconds}, now_ms=now)
                await self._stop_holder(holder_before, stop_account)
            else:
                await self._drain_and_logout(holder_before, target, drain, stop_account)
        elif holder_before and holder_before == target:
            # 05 §2.5.4 微信掉线重登:「目标就是自己,排空步骤天然为空」—— 仍要先腾空 holder 才能 claim
            await self._stop_holder(holder_before, stop_account)

        ls = new_login_session_id(now)
        if not self.claim(target, ls, now_ms=now):
            s2 = self.view()
            raise ApiError(409, "RESOURCE_EXHAUSTED", "微信槽位已被占用", reason="slot_held" if s2.holder else "slot_pending",
                           extra={"hint_actions": ["wechat_switch"], "holder": s2.holder, "pending": s2.pending})
        self._store.insert_audit(kind="system", transport="system", actor=actor, action=SWITCH_AUDIT, account_id=target,
                                 result_code="OK", detail={"holder_before": holder_before, "target": target, "login_session_id": ls}, now_ms=now)
        if begin_login is not None:
            try:
                await begin_login(target, ls)
            except Exception as e:
                # 切换期间任意一步失败 ⇒ 槽位空、目标回 stopped、alert;**不会自动把原号登回去**(05 §2.4.5 末句)
                log.warning("微信切换的登录流启动失败 target=%s: %s", target, e)
                await self.release_pending(target, reason=f"login_start_failed:{e}", actor=actor)
                self._to_stopped(target, ls, "切换失败:登录流未起来")
                raise
        return {"holder_before": holder_before, "target": target, "login_session_id": ls}

    async def _stop_holder(self, holder: str, stop_account: Optional[Callable[[str], Awaitable[None]]]) -> None:
        if stop_account is not None:
            await stop_account(holder)
        self.release_holder(holder)

    async def _drain_and_logout(self, holder: str, target: str, drain, stop_account) -> None:
        """05 §2.4.5 ①~④:排空(上限 ``switch_drain_timeout_s``)→ 停 chatlog + 登出微信 → holder ``stopped``、槽位腾空。

        「停 Agent 侧对该账号的 ``wechat/read`` 拉取 + 会话代理停 chatlog 进程」在 WinAgent #34 ``logout`` 里一并完成
        (它的 DLL 注入在 ``Weixin.exe`` 里,先停 chatlog 再动微信,02 §3.6 #34 / 05 §2.4.6)。"""
        if drain is not None:
            drained = await drain(holder, float(self.wechat_cfg.switch_drain_timeout_s))
            if not drained:
                log.warning("微信切换排空超时(%ss),队列里未跑完的按 TIMEOUT 回执 holder=%s", self.wechat_cfg.switch_drain_timeout_s, holder)
        if self._client is not None:
            try:
                await self._client.logout()
            except WeChatNotReady as e:
                log.warning("微信登出时会话代理不在线(%s),继续按结束进程口径收尾 holder=%s", e.reason, holder)
        await self._stop_holder(holder, stop_account)
