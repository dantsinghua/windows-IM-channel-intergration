"""账号生命周期服务(02 §3.4.1 #2/#4/#5/#6/#7/#9/#10/#11/#19、§2.6 恢复;05 §2.1.1 冷启动全序、§2.5.2 操作集合;00 §8.1 状态机)。

- 状态迁移**只经** ``transition()``:``store.transition``(同事务改 ``accounts.state`` + ``account_runtime.error_since_ms`` 两个动作)+ 发 ``account_state`` 事件
  (payload = Account 序列化 + ``state_before/login_session_id/prompt/error_since_ms``,与 #1/#3 同一份,02 §3.4.1「三处必须同值」)。
- 序列不跳段(00 §8.1):``created → provisioning → starting → login_required → logging_in → running``;免验证时中间态停留 0 秒仍发事件(05-P5)。
- 企点冷启动(05 §2.1.1):④ ``provisioning`` 起容器 → ``starting`` → ⑤ 等 ``boot_completed``(超时 ``error(BOOT_TIMEOUT)``)→ ⑤b ``ensure_root``(失败不阻断、只 warn)
  → ⑥⑦⑧ 装/拉起(UI 执行层未接:本期以可注入的 ``login_fn`` 代替,缺省未接)→ ``login_required(WAIT_PASSWORD)`` → ⑨ 取凭据(Vault 离线 → ``error(VAULT_UNAVAILABLE)``,
  不回退成让人输入)→ ⑩ ``logging_in`` → ⑪ 由 ``login_fn`` 判定;未接 ⇒ 回 ``login_required`` 等人。
- ``stop``:``stopping``(容器 stop)→ ``stopped`` **之后**、释放额度**之前** ``runtime._purge_ephemeral``(05 §2.5.7 / 02 §2.2.4)。
- ``start`` 前过 ``pool.can_add``(``stopped`` 不占额度但 start 要再过一次);全局启动串行在 ``runtime.start_lock``。
- ``DELETE`` 恒软删(P-09):``deleted_ms``、``desired_state=stopped``、容器停并保留卷;``confirm`` 须等于当前 ``label``;id 不复用(``settings.seq.*`` 只增)。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from .adapters.base import Account
from .api.auth import ApiError
from .api.serialize import account_view
from .config import AgentConfig
from .events import iso8601
from .ids import login_session_id as new_login_session_id
from .vault_client import VaultUnavailable, credential_ref, vault_name

log = logging.getLogger("qtrade.accounts")

LoginFn = Callable[[dict[str, Any], Optional[str], Optional[str]], Awaitable[Optional[str]]]   # (row, account, secret) -> 'running'|'login_required'|None(未接)

STOPPABLE = ("provisioning", "starting", "login_required", "logging_in", "running", "degraded", "error")
STARTABLE = ("created", "stopped", "error")
LOGIN_MODES = ("password", "qrcode", "manual")
STATE_CACHE_MS = 2000


async def _login_not_wired(row: dict[str, Any], account: Optional[str], secret: Optional[str]) -> Optional[str]:
    return None


class AccountService:
    def __init__(self, *, store, events, pool, runtime, vault, cfg: AgentConfig, adapters: dict[str, Any], health,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000), login_fn: Optional[LoginFn] = None):
        self._store = store
        self._events = events
        self._pool = pool
        self._runtime = runtime
        self._vault = vault
        self.cfg = cfg
        self._adapters = adapters
        self._health = health
        self._clock = clock
        self._login_fn: LoginFn = login_fn or _login_not_wired
        self.tasks: dict[str, asyncio.Task] = {}
        self._state_cache: dict[str, tuple[int, str]] = {}
        self.recovering: Optional[tuple[int, int]] = None      # (done, total) 供 P-DASH「正在恢复 2/3」

    # ------------------------------------------------------------------ 基础
    def caps(self, row: dict[str, Any]) -> list[str]:
        ad = self._adapters.get(row["channel"])
        return sorted(ad.capabilities) if ad else []

    def view(self, row: dict[str, Any]) -> dict[str, Any]:
        return account_view(row, self.caps(row))

    def get(self, id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        row = self._store.get_account_full(id)
        if row is None or (row.get("deleted_ms") and not include_deleted):
            raise ApiError(404, "TARGET_NOT_FOUND", f"账号不存在:{id}")
        return row

    def transition(self, id: str, state: str, *, state_code: Optional[str] = None, state_reason: str = "", login_session_id: str = "",
                   prompt: Optional[dict[str, Any]] = None, **kw: Any) -> dict[str, Any]:
        before, row = self._store.transition(id, state, state_code=state_code, state_reason=state_reason, now_ms=self._clock(), **kw)
        payload = self.view(row) | {"state_before": before, "login_session_id": login_session_id, "prompt": prompt}
        self._events.emit("account_state", payload=payload, account_id=id, channel=row["channel"], now_ms=self._clock())
        return row

    def _spawn(self, id: str, coro) -> asyncio.Task:
        t = asyncio.create_task(coro, name=f"account:{id}")
        self.tasks[id] = t
        return t

    async def wait_idle(self, id: str) -> None:
        """等该账号在途的后台序列结束(测试 / recover 用)。"""
        t = self.tasks.get(id)
        if t is not None:
            try:
                await t
            except Exception:
                pass

    def busy(self, id: str) -> bool:
        t = self.tasks.get(id)
        return t is not None and not t.done()

    # ------------------------------------------------------------------ #2 新增
    async def create(self, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        channel = body.get("channel")
        if channel not in ("qidian", "qq", "wechat"):
            raise ApiError(400, "INVALID_ARGS", "channel 须为 qidian|qq|wechat", reason="bad_channel", extra={"details": [{"pointer": "/channel"}]})
        label = body.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ApiError(400, "INVALID_ARGS", "label 不能为空", reason="bad_label", extra={"details": [{"pointer": "/label"}]})
        login = body.get("login") or {}
        mode = login.get("mode") or ("qrcode" if channel != "qidian" else "password")
        if mode not in LOGIN_MODES:
            raise ApiError(400, "INVALID_ARGS", "login.mode 须为 password|qrcode|manual", reason="bad_login_mode", extra={"details": [{"pointer": "/login/mode"}]})
        quota_mb = body.get("quota_mb")
        if quota_mb is None:
            quota_mb = self._pool.quota(channel)              # 创建时从 resource_pools.quota_json 拷贝
        if not isinstance(quota_mb, int) or quota_mb <= 0:
            raise ApiError(400, "INVALID_ARGS", "quota_mb 须为正整数", reason="bad_quota", extra={"details": [{"pointer": "/quota_mb"}]})
        # 资源预检(05 §2.1.1 ①):不足 409 + alternatives;微信槽位被占 409 + hint_actions
        ok, reason, alts = self._pool.can_add(channel)
        if not ok:
            self._raise_exhausted(channel, reason, alts)
        remember = bool(login.get("remember", False))       # R4-6:默认不存
        secret = login.get("secret")
        capture_text = body.get("capture_text")
        identity = dict(body.get("identity") or {})
        if login.get("account"):
            identity["login_account"] = str(login["account"])   # 登录账号名不是秘密,随 identity 落库;密码只进 Vault
        row = self._store.create_account(channel=channel, label=label.strip(), login_mode=mode, quota_mb=quota_mb, remember=remember and bool(secret),
                                         capture_text=capture_text if isinstance(capture_text, bool) else None, identity=identity, now_ms=self._clock())
        aid, seq = row["id"], int(row["seq"])
        ports = self._runtime.port_plan(channel, seq)
        rt_kind = self._store.RUNTIME_KIND[channel]
        cols: dict[str, Any] = {"container_name": f"qtrade-{aid}" if channel != "wechat" else None,
                                "data_dir": self._runtime.data_dir(aid) if channel != "wechat" else None}
        if channel == "qidian":
            cols.update({"adb_port": ports["adb"], "stream_port": ports["stream"], "frida_port": ports["frida"], "adb_serial": ports["adb_serial"],
                         "mem_limit_mb": self.cfg.runtime.qidian_mem_limit_mb})
        elif channel == "qq":
            cols.update({"ws_port": ports["ws"], "http_port": ports["http"], "webui_port": ports["webui"], "mem_limit_mb": self.cfg.runtime.qq_mem_limit_mb})
        self._store.upsert_runtime(aid, kind=rt_kind, now_ms=self._clock(), **cols)
        # 凭据:secret 只在此一次传输,写 Vault 后丢弃(#2);remember=false 不落 Vault
        if secret and remember:
            ref = credential_ref(aid)
            try:
                await self._vault.put(vault_name(ref), str(secret), scope="account")
                self._store.patch_account(aid, credential_ref=ref, remember=True, now_ms=self._clock())
            except VaultUnavailable as e:
                log.warning("Vault 不可用(%s),账号 %s 已建但凭据未保存(remember 置 0)", e.reason, aid)
                self._store.patch_account(aid, remember=False, now_ms=self._clock())
        secret = None
        row = self._store.get_account_full(aid)
        payload = self.view(row) | {"state_before": None, "login_session_id": "", "prompt": None}
        self._events.emit("account_state", payload=payload, account_id=aid, channel=channel, now_ms=self._clock())
        self._store.insert_audit(kind="system", transport="system", actor=actor, action="account.create", account_id=aid, result_code="OK",
                                 detail={"channel": channel, "seq": seq, "quota_mb": quota_mb, "remember": bool(row["remember"])}, now_ms=self._clock())
        return row

    def _raise_exhausted(self, channel: str, reason: str, alts: list[dict[str, Any]]) -> None:
        if channel == "wechat":
            if reason == "slot_held":
                holder = alts[0]["holder"] if alts else ""
                raise ApiError(409, "RESOURCE_EXHAUSTED", f"微信槽位被 {holder} 占用,请用切换", reason=reason,
                               extra={"hint_actions": ["wechat_switch"], "alternatives": alts})
            if reason == "winagent_offline":
                raise ApiError(503, "NOT_READY", "WinAgent 离线,微信不可用", reason=reason, retryable=True)
            raise ApiError(409, "RESOURCE_EXHAUSTED", f"微信不可新增:{reason}", reason=reason, extra={"alternatives": alts})
        raise ApiError(409, "RESOURCE_EXHAUSTED", f"资源不足,无法新增 {channel}(剩余 {self._pool.free_mb()} MB,需 {self._pool.quota(channel)} MB)",
                       reason=reason, extra={"alternatives": alts, "free_mb": self._pool.free_mb(), "need_mb": self._pool.quota(channel)})

    # ------------------------------------------------------------------ #9 start / #11 restart
    async def start(self, id: str, *, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if not row["enabled"]:
            raise ApiError(409, "NOT_APPLICABLE", "账号已停用,先 enable", reason="account_disabled")
        if self.busy(id):
            raise ApiError(409, "NOT_APPLICABLE", "账号正在切换状态", reason="busy")
        if row["state"] not in STARTABLE:
            if row["state"] in ("login_required", "logging_in", "running", "degraded", "starting", "provisioning"):
                return {"state": row["state"], "already": True}
            raise ApiError(409, "NOT_APPLICABLE", f"当前状态 {row['state']} 不可 start", reason="bad_state")
        ok, reason, alts = self._pool.can_add(row["channel"])
        if not ok:
            self._raise_exhausted(row["channel"], reason, alts)
        if row["channel"] == "wechat":
            if not self._health.winagent_online:
                raise ApiError(503, "NOT_READY", "WinAgent 离线", reason="winagent_offline", retryable=True)
            if not self._health.user_agent_online:
                raise ApiError(503, "NOT_READY", "WinAgent 会话代理不在线", reason="winagent_user_offline", retryable=True)
        self.transition(id, "provisioning", desired_state="running")
        self._spawn(id, self._start_sequence(id))
        return {"state": "starting"}

    async def _start_sequence(self, id: str) -> None:
        row = self._store.get_account_full(id)
        try:
            if row["channel"] == "wechat":
                self.transition(id, "starting")
                self.transition(id, "login_required", state_code="WAIT_QRCODE", state_reason="微信登录由 WinAgent 会话代理承接,本期未接")
                return
            await self._runtime.start(row)                                       # ④ docker run/start(全局串行)
            row = self.transition(id, "starting")
            if row["channel"] == "qidian":
                if not await self._runtime.wait_boot(row):                        # ⑤ 等 boot_completed
                    self.transition(id, "error", state_code="BOOT_TIMEOUT", state_reason=f"boot_completed 超时 {self.cfg.runtime.boot_timeout_s}s")
                    return
                await self._runtime.ensure_root(row)                             # ⑤b 提权;失败只 warn,不阻断
                await self._login_phase(id, row)
            else:
                self.transition(id, "login_required", state_code="WAIT_QRCODE", state_reason="QQ 扫码登录执行层未接入")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("start 序列失败 account=%s: %s", id, e)
            self.transition(id, "error", state_code="CONTAINER_EXIT", state_reason=f"启动失败:{e}")

    async def _login_phase(self, id: str, row: dict[str, Any]) -> None:
        """05 §2.1.1 ⑨~⑪:序列不跳段(00 §8.1),先 login_required 再 logging_in。"""
        ls = new_login_session_id()
        self.transition(id, "login_required", state_code="WAIT_PASSWORD", login_session_id=ls,
                        prompt={"kind": "WAIT_PASSWORD", "text": "请在画面完成登录"})
        if not (row["remember"] and row.get("credential_ref")):
            return                                                               # 不保存凭据:等人
        try:
            secret = await self._vault.read(vault_name(row["credential_ref"]), trace_id=ls)
        except VaultUnavailable as e:
            self.transition(id, "error", state_code="VAULT_UNAVAILABLE", state_reason=f"凭据保险库不可用({e.reason}),登录不进行", login_session_id=ls)
            return
        if secret is None:
            self.transition(id, "login_required", state_code="WAIT_PASSWORD", state_reason="Vault 无该账号凭据", login_session_id=ls,
                            prompt={"kind": "WAIT_PASSWORD", "text": "凭据缺失,请在画面输入"})
            return
        self.transition(id, "logging_in", login_session_id=ls)
        try:
            import json as _json
            try:
                login_account = (_json.loads(row.get("identity_json") or "{}") or {}).get("login_account")
            except ValueError:
                login_account = None
            result = await self._login_fn(row, login_account, secret)
        finally:
            secret = None                                                       # 密码用完置零
        if result == "running":
            self.transition(id, "running", state_code=None, login_session_id=ls)
        elif result == "bad_credential":
            try:
                await self._vault.flag(vault_name(row["credential_ref"]), suspect=True)
            except VaultUnavailable:
                pass
            self.transition(id, "error", state_code="BAD_CREDENTIAL", state_reason="账号或密码错误", login_session_id=ls)
        elif isinstance(result, str) and result.startswith("WAIT_"):
            self.transition(id, "login_required", state_code=result, login_session_id=ls, prompt={"kind": result, "text": "等待人在画面完成验证"})
        else:
            self.transition(id, "login_required", state_code="WAIT_PASSWORD", state_reason="登录执行层未接入", login_session_id=ls,
                            prompt={"kind": "WAIT_PASSWORD", "text": "请在画面完成登录"})

    async def restart(self, id: str, *, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if not row["enabled"]:
            raise ApiError(409, "NOT_APPLICABLE", "账号已停用", reason="account_disabled")
        if self.busy(id):
            raise ApiError(409, "NOT_APPLICABLE", "账号正在切换状态", reason="busy")
        ok, reason, alts = self._pool.can_add(row["channel"]) if row["state"] in ("stopped", "created", "error") else (True, "", [])
        if not ok:
            self._raise_exhausted(row["channel"], reason, alts)

        async def seq() -> None:
            if row["state"] in STOPPABLE:
                self.transition(id, "stopping", desired_state="stopped")
                await self._stop_sequence(id, graceful=True)
            self.transition(id, "provisioning", desired_state="running")
            await self._start_sequence(id)

        self._spawn(id, seq())
        return {"state": "stopping" if row["state"] in STOPPABLE else "starting"}

    # ------------------------------------------------------------------ #10 stop
    async def stop(self, id: str, *, graceful: bool = True, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if row["state"] in ("stopped", "disabled", "created"):
            self._store.set_desired_state(id, "stopped", now_ms=self._clock())
            return {"state": row["state"], "already": True}
        if self.busy(id):
            t = self.tasks[id]
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.transition(id, "stopping", desired_state="stopped")
        self._spawn(id, self._stop_sequence(id, graceful=graceful))
        return {"state": "stopping"}

    async def _stop_sequence(self, id: str, *, graceful: bool) -> None:
        row = self._store.get_account_full(id)
        try:
            if row["channel"] != "wechat":
                await self._runtime.stop(row, graceful=graceful)
            row = self.transition(id, "stopped", desired_state="stopped")          # 账号已置 stopped 之后……
            if row["channel"] != "wechat":
                await self._runtime._purge_ephemeral(row)                            # ……同步清可再生临时数据(05 §2.5.7)……
            else:
                self._store.wechat_slot_release_holder(id, now_ms=self._clock())    # 微信释放槽位
            self._pool.release(id)                                                  # ……再释放额度
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("stop 序列失败 account=%s: %s", id, e)
            self.transition(id, "error", state_code="CONTAINER_EXIT", state_reason=f"停止失败:{e}")

    # ------------------------------------------------------------------ #5 enable / #6 disable
    async def enable(self, id: str, *, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if row["enabled"] and row["state"] != "disabled":
            return row
        return self.transition(id, "stopped", enabled=True, desired_state="stopped")        # disabled → stopped;不自动 start

    async def disable(self, id: str, *, graceful: bool = True, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if row["state"] == "disabled":
            return row
        if row["state"] in STOPPABLE or self.busy(id):
            await self.stop(id, graceful=graceful, actor=actor)                 # 先 stop(有副作用,故不是 PATCH)
            await self.wait_idle(id)
        return self.transition(id, "disabled", enabled=False, desired_state="stopped")

    # ------------------------------------------------------------------ #7 软删
    async def delete(self, id: str, *, confirm: Optional[str], actor: str) -> dict[str, Any]:
        row = self.get(id)
        if confirm != row["label"]:
            raise ApiError(400, "INVALID_ARGS", "confirm 须等于当前 label(二次确认)", reason="confirm_mismatch", extra={"details": [{"pointer": "/confirm"}]})
        if row["state"] in STOPPABLE or self.busy(id):
            await self.stop(id, graceful=True, actor=actor)
            await self.wait_idle(id)
        self.transition(id, "stopped", deleted_ms=self._clock(), desired_state="stopped")
        if row["channel"] == "wechat":
            self._store.wechat_slot_release_holder(id, now_ms=self._clock())
        return {"deleted": True, "data_kept": True, "id": id}

    # ------------------------------------------------------------------ #4 PATCH
    PATCH_KEYS = ("label", "quota_mb", "capture_text", "retention_days", "media_policy", "login")

    async def patch(self, id: str, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        row = self.get(id)
        bad = [k for k in body if k not in self.PATCH_KEYS]
        if bad or "enabled" in body:
            raise ApiError(400, "INVALID_ARGS", f"不可 PATCH 的字段:{bad or ['enabled']}(enabled 走 #5/#6,C-40)", reason="bad_field",
                           extra={"details": [{"pointer": f"/{k}"} for k in (bad or ["enabled"])]})
        cols: dict[str, Any] = {}
        if "label" in body:
            if not isinstance(body["label"], str) or not body["label"].strip():
                raise ApiError(400, "INVALID_ARGS", "label 不能为空", reason="bad_label", extra={"details": [{"pointer": "/label"}]})
            cols["label"] = body["label"].strip()
        if "quota_mb" in body:
            q = body["quota_mb"]
            if not isinstance(q, int) or q <= 0:
                raise ApiError(400, "INVALID_ARGS", "quota_mb 须为正整数", reason="bad_quota", extra={"details": [{"pointer": "/quota_mb"}]})
            delta = q - int(row["quota_mb"])
            counted = row["host"] == "wsl" and row["enabled"] and row["state"] not in ("stopped", "disabled", "error")
            if delta > 0 and counted and self._pool.free_mb() < delta:
                raise ApiError(409, "RESOURCE_EXHAUSTED", f"配额上调 {delta} MB 超出剩余 {self._pool.free_mb()} MB", reason="wsl_budget",
                               extra={"alternatives": [], "free_mb": self._pool.free_mb(), "need_mb": delta})
            if row["host"] == "wsl" and q > self._pool.wsl_budget():
                raise ApiError(409, "RESOURCE_EXHAUSTED", f"quota_mb 超过 WSL 预算 {self._pool.wsl_budget()} MB", reason="wsl_budget",
                               extra={"alternatives": [], "free_mb": self._pool.free_mb(), "need_mb": q})
            cols["quota_mb"] = q
        if "capture_text" in body:
            v = body["capture_text"]
            if v is not None and not isinstance(v, bool):
                raise ApiError(400, "INVALID_ARGS", "capture_text 须为 bool 或 null", reason="bad_capture_text", extra={"details": [{"pointer": "/capture_text"}]})
            cols["capture_text"] = None if v is None else int(v)
        if "retention_days" in body:
            v = body["retention_days"]
            if v is not None and (not isinstance(v, int) or v <= 0 or v > 30):
                raise ApiError(400, "INVALID_ARGS", "retention_days 须在 1..30(E-18)", reason="retention_too_long", extra={"details": [{"pointer": "/retention_days"}]})
            cols["retention_days"] = v
        if "media_policy" in body:
            import json as _json
            v = body["media_policy"]
            if v is not None and not isinstance(v, dict):
                raise ApiError(400, "INVALID_ARGS", "media_policy 须为对象", reason="bad_media_policy", extra={"details": [{"pointer": "/media_policy"}]})
            cols["media_policy_json"] = None if v is None else _json.dumps(v, ensure_ascii=False)
        if "login" in body:
            lg = body["login"] or {}
            if "remember" in lg:
                cols["remember"] = bool(lg["remember"])
                if not lg["remember"] and row.get("credential_ref"):
                    try:
                        await self._vault.delete(vault_name(row["credential_ref"]))
                    except VaultUnavailable:
                        pass
                    cols["credential_ref"] = None
        if cols:
            row = self._store.patch_account(id, now_ms=self._clock(), **cols)
        return row

    # ------------------------------------------------------------------ #19 实时状态
    async def state_of(self, id: str) -> dict[str, Any]:
        row = self.get(id)
        now = self._clock()
        hit = self._state_cache.get(id)
        if hit and now - hit[0] <= STATE_CACHE_MS:
            adapter_state = hit[1]
        else:
            ad = self._adapters.get(row["channel"])
            adapter_state = row["state"]
            if ad is not None:
                try:
                    adapter_state = await ad.get_state(Account(id=id, channel=row["channel"], state=row["state"], self_uid=row.get("self_uid"),
                                                               self_nick=row.get("self_nick"), state_code=row.get("state_code")))
                except Exception:
                    adapter_state = "unknown"
            self._state_cache[id] = (now, adapter_state)
        return {"state": row["state"], "state_code": row.get("state_code") or "", "state_reason": row.get("state_reason") or "",
                "adapter_state": adapter_state, "last_seen_at": iso8601(row["last_seen_ms"]) if row.get("last_seen_ms") else None,
                "error_since_ms": row.get("runtime_error_since_ms") if row["state"] == "error" else None}

    # ------------------------------------------------------------------ 02 §2.6 启动恢复
    async def recover(self) -> list[dict[str, Any]]:
        rows = self._store.list_recover_candidates()
        results: list[dict[str, Any]] = []
        self.recovering = (0, len(rows))
        for i, r in enumerate(rows):
            id = r["id"]
            try:
                if r["channel"] == "wechat":
                    win = self._store.pool_get("windows")
                    if win and win["slot_holder"] and win["slot_holder"] != id:
                        self.transition(id, "stopped", state_reason=f"微信槽位被 {win['slot_holder']} 占用")
                        results.append({"id": id, "ok": False, "reason": "slot_held"})
                        continue
                if r["state"] in ("running", "degraded", "login_required", "logging_in"):
                    info = await self._runtime.inspect(r, fresh=True)
                    if info.running:
                        results.append({"id": id, "ok": True, "reason": "already_running"})
                        continue
                    self.transition(id, "stopped")           # 容器不在跑:按 stopped 重走 start
                elif r["state"] not in STARTABLE:
                    self.transition(id, "stopped")
                res = await self.start(id, actor="system:recover")
                await self.wait_idle(id)
                results.append({"id": id, "ok": True, "reason": res.get("state")})
            except ApiError as e:
                log.warning("恢复 %s 跳过:%s %s", id, e.code, e.message)
                results.append({"id": id, "ok": False, "reason": e.reason or e.code})
            except Exception as e:                            # 任一步失败不阻塞下一账号
                log.exception("恢复 %s 失败: %s", id, e)
                self.transition(id, "error", state_code="CONTAINER_EXIT", state_reason=f"恢复失败:{e}")
                results.append({"id": id, "ok": False, "reason": str(e)})
            finally:
                self.recovering = (i + 1, len(rows))
        self._store.insert_audit(kind="system", transport="system", actor="system:recover", action="account.recover", result_code="OK",
                                 detail={"results": results}, now_ms=self._clock())
        self.recovering = None
        return results
