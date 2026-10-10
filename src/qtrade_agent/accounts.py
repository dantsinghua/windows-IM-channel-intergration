"""账号生命周期服务(02 §3.4.1 #2/#4/#5/#6/#7/#9/#10/#11/#19、§2.6 恢复;05 §2.1.1 冷启动全序、§2.5.2 操作集合;00 §8.1 状态机)。

- 状态迁移**只经** ``transition()``:``store.transition``(同事务改 ``accounts.state`` + ``account_runtime.error_since_ms`` 两个动作)+ 发 ``account_state`` 事件
  (payload = Account 序列化 + ``state_before/login_session_id/prompt/error_since_ms``,与 #1/#3 同一份,02 §3.4.1「三处必须同值」)。
- 序列不跳段(00 §8.1):``created → provisioning → starting → login_required → logging_in → running``;免验证时中间态停留 0 秒仍发事件(05-P5)。
- 企点冷启动(05 §2.1.1):④ ``provisioning`` 起容器 → ``starting`` → ⑤ 等 ``boot_completed``(超时 ``error(BOOT_TIMEOUT)``)→ ⑤b ``ensure_root``(失败不阻断、只 warn)
  → ⑥ 安装缺失包 → ⑦⑧ 无凭据准备登录界面 → ``login_required(WAIT_PASSWORD)`` → ⑨ 取凭据(Vault 离线 → ``error(VAULT_UNAVAILABLE)``,
  不回退成让人输入)→ ⑩ ``logging_in`` → ⑪ 由 ``login_fn`` 判定;未接 ⇒ 回 ``login_required`` 等人。
- ``stop``:``stopping``(容器 stop)→ ``stopped`` **之后**、释放额度**之前** ``runtime._purge_ephemeral``(05 §2.5.7 / 02 §2.2.4)。
- ``start`` 前过 ``pool.can_add``(``stopped`` 不占额度但 start 要再过一次);全局启动串行在 ``runtime.start_lock``。
- ``DELETE`` 恒软删(P-09):``deleted_ms``、``desired_state=stopped``、容器停并保留卷;``confirm`` 须等于当前 ``label``;id 不复用(``settings.seq.*`` 只增)。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from .adapters.base import Account
from .adapters.qq.login import QR_LIFETIME_MS, QR_REFRESH_MS, valid_png
from .alerts import ACCOUNT_OFFLINE, QIDIAN_PROFILE_FALLBACK
from .api.auth import ApiError
from .api.serialize import account_view
from .config import AgentConfig
from .events import iso8601
from .ids import login_session_id as new_login_session_id, ulid
from .runtime.apk import ApkPreparationError
from .vault_client import VaultUnavailable, credential_ref, vault_name

log = logging.getLogger("qtrade.accounts")

LoginFn = Callable[[dict[str, Any], Optional[str], Optional[str]], Awaitable[Optional[str]]]   # (row, account, secret) -> 'running'|'login_required'|None(未接)
PrepareLoginFn = Callable[[dict[str, Any]], Awaitable[Optional[bool]]]
LoginProbeFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

STOPPABLE = ("provisioning", "starting", "login_required", "logging_in", "running", "degraded", "error")
STARTABLE = ("created", "stopped", "error")
LOGIN_PHASE = ("login_required", "logging_in")
LOGIN_MODES = ("password", "qrcode", "manual")
OFFLINE_CODES = ("KICKED", "LOGGED_OUT", "TOKEN_EXPIRED", "LOGIN_TIMEOUT")        # 05 §2.5.4 掉线原因组(伴随 login_required)
STATE_CACHE_MS = 2000
BATCH_ACTIONS = ("start", "stop", "restart", "enable", "disable")
SETTINGS_KEYS = ("send", "sessions", "gates", "auto_recover", "capture_text", "retention_days", "mail_route_id", "auto_stop_on_pressure")   # #22 / 05 §2.5.5


async def _login_not_wired(row: dict[str, Any], account: Optional[str], secret: Optional[str]) -> Optional[str]:
    return None


class AccountService:
    def __init__(self, *, store, events, pool, runtime, vault, cfg: AgentConfig, adapters: dict[str, Any], health,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000), login_fn: Optional[LoginFn] = None,
                 caps_by_op: Optional[dict[str, dict[str, Any]]] = None, pressure=None,
                 prepare_login_fn: Optional[PrepareLoginFn] = None, login_probe_fn: Optional[LoginProbeFn] = None,
                 qq_login_backend=None):
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
        self._prepare_login_fn = prepare_login_fn
        self._login_probe_fn = login_probe_fn
        self._qq_login_backend = qq_login_backend
        self._qq_attempts: dict[str, dict[str, Any]] = {}
        self._manual_login_probes: set[str] = set()
        self._manual_login_probe_failures: dict[str, tuple[str, str]] = {}
        self._caps_by_op = caps_by_op or {}
        self.pressure = pressure                                # pressure.MemoryWatermark(装配后注入);None = 不判水位
        self._wechat_slot = None                                # wechat_slot.WechatSlot(装配后注入);None = 微信分支退回「本期未接」
        self._wechat_login = None                               # adapters.wechat.WechatLoginFlow(装配后注入)
        self._bus = None                                        # bus.Bus(装配后注入):#17 切换排空 holder 队列用
        self.tasks: dict[str, asyncio.Task] = {}
        self._state_cache: dict[str, tuple[int, str]] = {}
        self.recovering: Optional[tuple[int, int]] = None      # (done, total) 供 P-DASH「正在恢复 2/3」
        self._prompts: dict[str, dict[str, Any]] = {}          # #15:当前等人的提示(内存态;与 account_state.payload.prompt 同一对象)
        self._current_ls: dict[str, str] = {}                  # 进行中的登录尝试 login_session_id(终态即失效)
        self._remind: dict[str, dict[str, Any]] = {}           # login_required 提醒:同 trace_id 每 login_remind_interval_s 重发
        self._pending_self_uid: dict[str, str] = {}            # 执行层读到的 self_uid,等本次登录判 running 时一并落库(05 §2.1.1 ⑪a)
        self._pending_ui_degraded: dict[str, Optional[str]] = {}   # 执行层落 default profile:等 running 后置 degraded(UI_UNEXPECTED)

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
                   prompt: Optional[dict[str, Any]] = None, trace_id: Optional[str] = None, **kw: Any) -> dict[str, Any]:
        now = self._clock()
        before, row = self._store.transition(id, state, state_code=state_code, state_reason=state_reason, now_ms=now, **kw)
        if state in LOGIN_PHASE:
            if login_session_id:
                self._current_ls[id] = login_session_id
            if prompt is not None:
                self._prompts[id] = {"login_session_id": login_session_id or self._current_ls.get(id, ""), "prompt": dict(prompt), "since_ms": now}
            if state == "login_required":                        # 05 §2.5.4:提醒事件与首条同 trace_id
                rem = self._remind.setdefault(id, {"trace_id": trace_id or ulid(now), "last_ms": now})
                rem["last_ms"] = now
                trace_id = trace_id or rem["trace_id"]
        else:                                                    # 登录尝试到达终态即失效(05 §2.0)
            self._prompts.pop(id, None)
            self._current_ls.pop(id, None)
            self._remind.pop(id, None)
        payload = self.view(row) | {"state_before": before, "login_session_id": login_session_id, "prompt": prompt}
        self._events.emit("account_state", payload=payload, account_id=id, channel=row["channel"], trace_id=trace_id, now_ms=now)
        return row

    def _spawn(self, id: str, coro) -> asyncio.Task:
        t = asyncio.create_task(coro, name=f"account:{id}")
        self.tasks[id] = t
        return t

    async def wait_idle(self, id: str) -> None:
        """等该账号在途的后台序列结束(测试 / recover 用);被取消的序列(login_cancel)不把 CancelledError 冒给等待者(R6-57 ⑩)。"""
        t = self.tasks.get(id)
        if t is not None:
            try:
                await t
            except asyncio.CancelledError:
                if not t.cancelled():
                    raise                                       # 等待者自己被取消
            except Exception:
                pass

    def busy(self, id: str) -> bool:
        t = self.tasks.get(id)
        return t is not None and not t.done()

    # ------------------------------------------------------------------ #2 新增
    async def create(self, body: dict[str, Any], *, actor: str, pool_check: bool = True) -> dict[str, Any]:
        """``pool_check=False`` 只给 #18 ``switch_new`` 用:切换的语义就是「槽位被占着也要新建一个号去顶」,
        ``can_add('wechat')`` 在 holder/pending 非空时必然拒绝,先建行再走 switch 才是 05 §2.4.5 的顺序。
        内存水位 critical 的阻断(E-19)**不跳过**。"""
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
        # 内存水位 critical(E-19):新增一律 409 mem_pressure + LRU 建议停用名单
        self._raise_if_mem_pressure()
        # 资源预检(05 §2.1.1 ①):不足 409 + alternatives;微信槽位被占 409 + hint_actions
        if pool_check:
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

    def _raise_if_mem_pressure(self) -> None:
        if self.pressure is not None and self.pressure.blocked():
            lru = self.pressure.lru_suggest()
            raise ApiError(409, "RESOURCE_EXHAUSTED", f"整机可用内存 {self.pressure.avail_mb} MB 低于 critical 水位,已暂停新增与自动恢复",
                           reason="mem_pressure", extra={"alternatives": lru, "avail_mb": self.pressure.avail_mb, "hint_actions": ["open_res"]})

    def _raise_exhausted(self, channel: str, reason: str, alts: list[dict[str, Any]], *, exclude_account_id: Optional[str] = None) -> None:
        free = self._pool.free_mb(exclude_account_id=exclude_account_id)
        if channel == "wechat":
            if reason == "slot_held":
                holder = alts[0]["holder"] if alts else ""
                raise ApiError(409, "RESOURCE_EXHAUSTED", f"微信槽位被 {holder} 占用,请用切换", reason=reason,
                               extra={"hint_actions": ["wechat_switch"], "alternatives": alts})
            if reason == "winagent_offline":
                raise ApiError(503, "NOT_READY", "WinAgent 离线,微信不可用", reason=reason, retryable=True)
            raise ApiError(409, "RESOURCE_EXHAUSTED", f"微信不可新增:{reason}", reason=reason, extra={"alternatives": alts})
        raise ApiError(409, "RESOURCE_EXHAUSTED", f"资源不足,无法新增 {channel}(剩余 {free} MB,需 {self._pool.quota(channel)} MB)",
                       reason=reason, extra={"alternatives": alts, "free_mb": free, "need_mb": self._pool.quota(channel)})

    # ------------------------------------------------------------------ #9 start / #11 restart
    async def start(self, id: str, *, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if not row["enabled"]:
            raise ApiError(409, "NOT_APPLICABLE", "账号已停用,先 enable", reason="account_disabled")
        if self.busy(id):
            raise ApiError(409, "NOT_APPLICABLE", "账号正在切换状态", reason="busy")
        # R6-91:微信首登卡在取钥 / UI 树(degraded + KEY_FAIL|WAIT_UI_TREE)⇒ 允许 start 重跑登录流(**不登出微信**,
        # WinAgent 按「已登录」路径走:仪式可见即跳 → identified → 回填/绑定 → 取钥)。否则这类号只能删了重建。
        wechat_relogin = (row["channel"] == "wechat" and row["state"] == "degraded"
                          and (row.get("state_code") or "") in ("KEY_FAIL", "WAIT_UI_TREE", "WINAGENT_OFFLINE", "WINAGENT_USER_OFFLINE"))
        # 微信已开着、登录会话却丢了(会话代理重启 / 停在讲述人):允许重跑取钥,不要求先停号
        wechat_resume = row["channel"] == "wechat" and row["state"] in ("login_required", "logging_in", "degraded")
        if row["state"] not in STARTABLE and not wechat_relogin and not wechat_resume:
            if row["state"] in ("login_required", "logging_in", "running", "degraded", "starting", "provisioning"):
                return {"state": row["state"], "already": True}
            raise ApiError(409, "NOT_APPLICABLE", f"当前状态 {row['state']} 不可 start", reason="bad_state")
        ok, reason, alts = self._pool.can_add(row["channel"], exclude_account_id=id)      # R6-55:排除自身(created 已计入 used)
        if not ok:
            self._raise_exhausted(row["channel"], reason, alts, exclude_account_id=id)
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
                await self._wechat_start(id)
                return
            await self._runtime.start(row)                                       # ④ docker run/start(全局串行)
            row = self.transition(id, "starting", state_reason=(
                "正在启动 Android，随后检查 DNS/TCP 并完成企点协议与登录界面初始化"
                if row["channel"] == "qidian" else ""))
            if row["channel"] == "qidian":
                if not await self._runtime.wait_boot(row):                        # ⑤ 等 boot_completed
                    self.transition(id, "error", state_code="BOOT_TIMEOUT", state_reason=f"boot_completed 超时 {self.cfg.runtime.boot_timeout_s}s")
                    return
                await self._runtime.ensure_root(row)                             # ⑤b 提权;失败只 warn,不阻断
                report = await self._runtime.check_qidian_network(row)
                if not report["ok"]:
                    prefix = "Android 网络探测未完成" if report["result"] == "SKIPPED" else "Android 网络预检失败"
                    details = "; ".join(f"{item.get('host') or 'target'}:{item.get('port') or '-'} "
                                        f"{item['stage']}/{item['result']}({item['detail']})"
                                        for item in report["targets"][:3])
                    self.transition(id, "error", state_code="NETWORK_UNAVAILABLE",
                                    state_reason=f"{prefix}: {report['result']} {details}；检查后重试启动")
                    return
                await self._runtime.prepare_qidian(row)                          # ⑥ 缺包安装并复查
                if self._prepare_login_fn is not None:
                    try:
                        if await self._prepare_login_fn(row) is False:          # ⑦⑧ 无凭据准备，完成后才发布 WAIT_PASSWORD
                            raise RuntimeError("企点登录界面尚未就绪")
                    except Exception:
                        log.warning("企点登录界面准备失败 account=%s", id)
                        self.transition(id, "error", state_code="UI_UNEXPECTED", state_reason="企点登录界面准备失败")
                        return
                await self._login_phase(id, row)
            else:
                await self._qq_start(id, row)
        except asyncio.CancelledError:
            raise
        except ApkPreparationError as e:
            self.transition(id, "error", state_code=e.code, state_reason=str(e))
        except Exception as e:
            log.exception("start 序列失败 account=%s: %s", id, e)
            self.transition(id, "error", state_code="CONTAINER_EXIT", state_reason=f"启动失败:{e}")

    # ------------------------------------------------------------------ 05 §2.3 QQ 首登序列 ⑤/⑦
    async def _qq_start(self, id: str, row: dict[str, Any], ls: Optional[str] = None) -> None:
        """人发起的启动/登录；免扫失败后继续同一次扫码会话，不靠运行期自恢复触发。"""
        ad = self._adapters.get("qq")
        if ad is None:
            self.transition(id, "error", state_code="ONEBOT_UNREACHABLE", state_reason="QQ 适配器未装配")
            return
        acct = Account(id=id, channel="qq", state=row["state"], self_uid=row.get("self_uid"), self_nick=row.get("self_nick"),
                       state_code=row.get("state_code"), extra={"login_pending": True})
        ls = ls or new_login_session_id()
        self.transition(id, "login_required", state_code="WAIT_QRCODE", login_session_id=ls,
                        prompt={"kind": "WAIT_QRCODE", "text": "请扫码登录 QQ"})
        attempt = {"session": ls, "deadline": self._clock() + self.cfg.accounts.qr_max_wait_s * 1000,
                   "refresh_due": self._clock(), "lock": asyncio.Lock()}
        self._qq_attempts[id] = attempt
        if ad.session_of(id) is not None:
            await ad.stop(acct, graceful=True)
        await ad.start(acct)
        deadline = time.monotonic() + self.cfg.accounts.qq_quick_login_wait_s
        while True:
            info = await self._qq_login_info(ad, id)
            if not self._qq_attempt_current(id, attempt):
                return
            if info:
                await self._qq_complete(id, attempt, info)
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        # 同一卷可能保留上次 PNG，首张也要求 NapCat 新生成，避免旧文件抢读。
        await self._qq_refresh(id, attempt, refresh=True)

    def qq_login_pending(self, id: str) -> bool:
        attempt = self._qq_attempts.get(id)
        return attempt is not None and self._qq_attempt_current(id, attempt)

    def _qq_attempt_current(self, id: str, attempt: dict[str, Any]) -> bool:
        row = self._store.get_account_full(id)
        return bool(self._qq_attempts.get(id) is attempt and self._current_ls.get(id) == attempt["session"]
                    and row and row.get("enabled") and not row.get("deleted_ms") and row["state"] in LOGIN_PHASE
                    and self._clock() < attempt["deadline"])

    async def _qq_refresh(self, id: str, attempt: dict[str, Any], *, refresh: bool) -> None:
        async with attempt["lock"]:
            if not self._qq_attempt_current(id, attempt) or self.get(id)["state"] != "login_required":
                return
            prompt = {"kind": "WAIT_QRCODE", "text": "请用手机 QQ 扫码并确认登录"}
            reason = ""
            try:
                if self._qq_login_backend is None:
                    raise RuntimeError("not_wired")
                png = await asyncio.wait_for(self._qq_login_backend.qrcode(self.get(id), refresh=refresh), 26)
                if not valid_png(png):
                    raise ValueError("invalid_png")
                encoded = base64.b64encode(png).decode("ascii")
                previous = (self._prompts.get(id) or {}).get("prompt", {}).get("qrcode_png_b64")
                if refresh and encoded == previous:
                    raise ValueError("stale_qrcode")
                prompt.update(qrcode_png_b64=encoded, expires_at=iso8601(self._clock() + QR_LIFETIME_MS))
            except Exception:
                # 上游错误正文可能含二维码/凭据，只向人显示固定可重试说明。
                reason = "二维码获取暂不可用，请重试；若持续失败请检查 NapCat 登录初始化"
                prompt["text"] = reason
            if not self._qq_attempt_current(id, attempt) or self.get(id)["state"] != "login_required":
                return
            attempt["refresh_due"] = self._clock() + QR_REFRESH_MS
            self.transition(id, "login_required", state_code="WAIT_QRCODE", state_reason=reason,
                            login_session_id=attempt["session"], prompt=prompt)

    async def poll_qq_logins(self) -> None:
        """仅观察明确由人开始且仍有效的 QQ 登录尝试；运行期掉线不创建尝试。"""
        for id, attempt in list(self._qq_attempts.items()):
            if self._clock() >= attempt["deadline"] and self._current_ls.get(id) == attempt["session"]:
                self._qq_attempts.pop(id, None)
                await self.stop(id, graceful=True, actor="system:qq_login_timeout")
                await self.wait_idle(id)
                if self.get(id)["state"] == "stopped":
                    self.transition(id, "stopped", state_reason="扫码超时,已停止", desired_state="stopped")
                continue
            if not self._qq_attempt_current(id, attempt):
                if self._qq_attempts.get(id) is attempt:
                    self._qq_attempts.pop(id, None)
                continue
            if self.busy(id) or attempt["lock"].locked():
                continue
            async with attempt["lock"]:
                info = await self._qq_login_info(self._adapters["qq"], id)
                if self._qq_attempt_current(id, attempt) and info:
                    await self._qq_complete(id, attempt, info)
            if self._qq_attempt_current(id, attempt) and self._clock() >= attempt["refresh_due"]:
                await self._qq_refresh(id, attempt, refresh=True)

    async def _qq_complete(self, id: str, attempt: dict[str, Any], info: dict[str, Any]) -> None:
        if not self._qq_attempt_current(id, attempt):
            return
        row = self.get(id)
        expected = str(json.loads(row.get("identity_json") or "{}").get("qq_uin") or "")
        uid = str(info["user_id"])
        if expected and expected != uid:
            self._qq_attempts.pop(id, None)
            self.transition(id, "error", state_code="BAD_CREDENTIAL", state_reason="扫码登录的 QQ 号与指定账号不一致")
            return
        ls = attempt["session"]
        self._prompts.pop(id, None)
        self.transition(id, "logging_in", login_session_id=ls)
        try:
            restarted = await self._runtime.set_napcat_webui(
                row | {"qq_confirmed_uid": uid, "qq_login_current": lambda: self._qq_attempt_current(id, attempt)}, False)
            if not self._qq_attempt_current(id, attempt):
                return
            if restarted:
                ad = self._adapters["qq"]
                acct = Account(id=id, channel="qq", state="logging_in", self_uid=uid, extra={"login_pending": True})
                await ad.stop(acct, graceful=True)
                await ad.start(acct)
                deadline = time.monotonic() + self.cfg.accounts.qq_quick_login_wait_s
                while True:
                    verified = await self._qq_login_info(ad, id)
                    if not self._qq_attempt_current(id, attempt):
                        return
                    if verified and str(verified["user_id"]) == uid:
                        info = verified
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("onebot_unavailable")
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
            if not self._qq_attempt_current(id, attempt):
                return
            self._store.con.execute("UPDATE accounts SET self_nick=?, updated_ms=? WHERE id=?",
                                    (str(info.get("nickname") or ""), self._clock(), id))
            self.transition(id, "running", state_code=None, login_session_id=ls, self_uid=uid)
            session = self._adapters["qq"].session_of(id)
            if session is not None:
                session.acct.self_uid = uid
                session.acct.self_nick = str(info.get("nickname") or "")
                session.acct.state = "running"
                session.acct.extra.pop("login_pending", None)
            self._store.upsert_runtime(id, kind="napcat", webui_published_until_ms=None, now_ms=self._clock())
            self._qq_attempts.pop(id, None)
        except Exception:
            if self._qq_attempt_current(id, attempt):
                self._qq_attempts.pop(id, None)
                self.transition(id, "error", state_code="ONEBOT_UNREACHABLE", state_reason="QQ 登录后关闭管理界面或连接验证失败，请重试启动")

    @staticmethod
    async def _qq_login_info(ad: Any, account_id: str) -> Optional[dict[str, Any]]:
        """只接受 OneBot 确认的数字 UID；缺身份时不能进入 running。"""
        sess = ad.session_of(account_id)
        if sess is None:
            return None
        try:
            status = await asyncio.wait_for(sess.client.call_action("get_status"), 5)
            if not isinstance(status, dict) or status.get("online") is not True:
                return None
            info = await asyncio.wait_for(sess.client.call_action("get_login_info"), 5)
        except Exception:
            return None
        uid = info.get("user_id") if isinstance(info, dict) else None
        return info if (not isinstance(uid, bool) and isinstance(uid, (int, str))
                        and str(uid).isascii() and str(uid).isdigit() and int(uid) > 0) else None

    # ------------------------------------------------------------------ 05 §2.4 微信登录流
    async def _wechat_start(self, id: str) -> None:
        """05 §2.4.2:行级 claim 槽位(02 §2.2.5)→ ``WechatLoginFlow`` 驱动相位与状态。

        ``WechatLoginFlow.run`` 自己做 ``provisioning → starting``(``_enter_starting`` 幂等),故此处不再 transition。"""
        if self._wechat_slot is None or self._wechat_login is None:
            self.transition(id, "starting")
            self.transition(id, "login_required", state_code="WAIT_QRCODE", state_reason="微信登录流未装配")
            return
        ls = new_login_session_id()
        if not self._wechat_slot.claim(id, ls):
            slot = self._wechat_slot.view()
            if slot.pending != id and slot.holder != id:
                self.transition(id, "stopped", state_reason="微信槽位被占用", desired_state="stopped")
                return
        await self._wechat_login.run(id, ls)

    # ------------------------------------------------------------------ #97 / #98 QQ WebUI 临时开关(C-35)
    async def set_webui(self, id: str, enable: bool, *, until_ms: Optional[int] = None, actor: str) -> dict[str, Any]:
        """配置和执行结果为准；到期登记不能被误当作管理面已关闭。"""
        row = self.get(id)
        if row["channel"] != "qq":
            raise ApiError(400, "NOT_APPLICABLE", "只有 QQ 支持 NapCat WebUI", reason="channel_not_qq")
        rt = self._store.get_runtime(id) or {}
        now = self._clock()
        registered_until = rt.get("webui_published_until_ms")
        try:
            currently_on = self._runtime.napcat_webui_enabled(row)
            target_until = until_ms if enable else None
            if ((enable and currently_on and registered_until is not None and registered_until > now)
                    or (not enable and not currently_on and registered_until is None)):
                return {"changed": False, "restart": False, "until_ms": registered_until}
            # 失败重试时配置可能已写成关闭，但上一进程尚未退出；登记保留即仍要核实重启。
            force_restart = not enable and registered_until is not None
            restarted = await self._runtime.set_napcat_webui(row | {"napcat_restart_required": force_restart}, enable)
        except Exception:
            raise ApiError(503, "NOT_READY", "NapCat 管理界面配置或重启失败，请重试", reason="napcat_webui_unavailable", retryable=True) from None
        self._store.upsert_runtime(id, kind=self._store.RUNTIME_KIND[row["channel"]],
                                   webui_published_until_ms=target_until, now_ms=now)
        self._store.insert_audit(kind="system", transport="system", actor=actor, action="settings.update", account_id=id,
                                 result_code="OK", detail={"webui": enable, "until_ms": until_ms}, now_ms=now)
        return {"changed": True, "restart": restarted, "until_ms": target_until}

    async def expire_qq_webui(self) -> None:
        """只处理我方明确登记的临时窗口；失败保留登记，下轮重试，不发起登录。"""
        now = self._clock()
        for row in self._store.list_accounts(channel="qq"):
            runtime = self._store.get_runtime(row["id"]) or {}
            until = runtime.get("webui_published_until_ms")
            if until is None or until > now or self.busy(row["id"]) or self.qq_login_pending(row["id"]):
                continue
            try:
                await self.set_webui(row["id"], False, actor="system:qq_webui_expiry")
            except ApiError:
                log.warning("QQ 临时管理界面到期关闭失败 account=%s", row["id"])

    async def _drain_account(self, account_id: str, timeout_s: float) -> bool:
        """#17 切换第 ① 步:等该账号的总线队列跑完(上限 ``[adapters.wechat] switch_drain_timeout_s``)。

        返回 True = 排空;False = 超时(队列里未跑完的由总线自己按 TIMEOUT 收尾)。总线未装配 ⇒ 视作已排空。"""
        if self._bus is None:
            return True
        q = self._bus._queues.get(account_id)
        if q is None:
            return True
        try:
            await asyncio.wait_for(q.join(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------ #17 / #18 微信切换
    async def switch(self, id: str, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """#17 ``POST /accounts/{id}/switch``:出参字面键集顶层平铺(R6-55)``{holder_before, target, login_session_id}``。"""
        if self._wechat_slot is None:
            raise ApiError(503, "NOT_READY", "微信槽位模块未装配", reason="wechat_not_wired", retryable=True)
        return await self._wechat_slot.switch(
            id, confirm=bool(body.get("confirm", False)), actor=actor,
            stop_account=self._switch_stop_account,
            begin_login=self._switch_begin_login,
            drain=self._drain_account)

    async def switch_new(self, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """#18 ``POST /accounts/switch``(``body.target='new'``):先建 ``wxNN`` 行(``created``/``wxid=NULL``)再走同一条 switch。"""
        row = await self.create({"channel": "wechat", "label": body.get("label") or "新微信",
                                 "login": {"mode": "qrcode", "remember": False}}, actor=actor, pool_check=False)
        return await self.switch(row["id"], body, actor=actor)

    async def _switch_stop_account(self, holder: str) -> None:
        await self.stop(holder, graceful=True, actor="system:wechat_switch")
        await self.wait_idle(holder)

    async def _switch_begin_login(self, target: str, ls: str) -> None:
        if self._wechat_login is None:
            raise RuntimeError("微信登录流未装配")
        self._spawn(target, self._wechat_login.run(target, ls))

    async def _login_phase(self, id: str, row: dict[str, Any]) -> None:
        """05 §2.1.1 ⑨~⑪:序列不跳段(00 §8.1),先 login_required 再 logging_in;保存了凭据(人预先授权的启动,05 §2.0)才自动取用。"""
        ls = new_login_session_id()
        self.transition(id, "login_required", state_code="WAIT_PASSWORD", login_session_id=ls,
                        prompt={"kind": "WAIT_PASSWORD", "text": "请在画面完成登录"})
        if not (row["remember"] and row.get("credential_ref")):
            return                                                               # 不保存凭据:等人(05 §2.2.7)
        try:
            secret = await self._vault.read(vault_name(row["credential_ref"]), trace_id=ls)
        except VaultUnavailable as e:
            self.transition(id, "error", state_code="VAULT_UNAVAILABLE", state_reason=f"凭据保险库不可用({e.reason}),登录不进行", login_session_id=ls)
            return
        if secret is None:
            self.transition(id, "login_required", state_code="WAIT_PASSWORD", state_reason="Vault 无该账号凭据", login_session_id=ls,
                            prompt={"kind": "WAIT_PASSWORD", "text": "凭据缺失,请在画面输入"})
            return
        await self._run_login(id, row, secret, ls)

    # ------------------------------------------------------------------ 执行层 → 本服务的回填通道(05 §2.1.1 ⑪a / 02 §2.2.3)
    def note_self_uid(self, account_id: str, self_uid: str) -> None:
        """登录执行层读到 ``self_uid`` 时调这里(``QidianUi.login_fn(on_self_uid=…)``)。

        05 §2.1.1 ⑪a:企点 ``self_uid`` = 登录 uin 纯数字。**本方法不自己写库** —— 值先记在内存,
        由 ``_run_login`` 判定 ``running`` 时随同一次 ``transition`` 落库(状态与身份列同一个事务,
        避免「回填成功但状态没进 running」的半截态)。

        🔴 **换号(同一 ``qdNN`` 先后登录了不同 uin)**:本步**只回填新值**,不做任何破坏性动作 ——
        识别换号与「删该账号全部 ``qidian_rowid:*`` 水位 + 重做首次 bootstrap + 记 ``qidian.rebootstrap``
        审计」由 06 §2.9.5 ③ 在下一个全量轮做(R6-50/R6-40,已实现于 ``adapters/qidian/poll.py``)。
        """
        if not self_uid:
            return
        row = self._store.get_account_full(account_id)
        old = (row or {}).get("self_uid")
        if old and old != self_uid:
            log.warning("账号 %s 的 self_uid 变了(%s → %s):本步只回填,换号处置由 06 §2.9.5 ③ 的全量轮做",
                        account_id, old, self_uid)
        self._pending_self_uid[account_id] = self_uid

    def note_default_profile(self, account_id: str, app_version: Optional[str] = None) -> None:
        """企点定位 profile 落到 ``default.yaml`` 时调这里(``QidianUi(on_default_profile=…)``)。

        02 §2.2.3:没有对应企点版本的专用 profile ⇒ 用 ``default.yaml`` 且账号 ``degraded(state_code=UI_UNEXPECTED)``。
        🔴 **只能从 ``running``/``degraded`` 迁入**(00 §8.1 只允许 ``running → degraded``)——发送路径调到这里时
        账号已 ``running``,直接迁;登录路径调到这里时还在 ``logging_in``,**不许跳段**,记下来等 ``running`` 之后再迁。
        """
        reason = f"企点定位表无 {app_version or '本'} 版本的 profile,已退到 default.yaml"
        row = self._store.get_account_full(account_id)
        state = (row or {}).get("state")
        if state == "degraded":
            return
        if state == "running":
            self.transition(account_id, "degraded", state_code="UI_UNEXPECTED", state_reason=reason)
            return
        self._pending_ui_degraded[account_id] = app_version

    @staticmethod
    def _login_account(row: dict[str, Any]) -> Optional[str]:
        try:
            return (json.loads(row.get("identity_json") or "{}") or {}).get("login_account")
        except ValueError:
            return None

    async def _run_login(self, id: str, row: dict[str, Any], secret: Optional[str], ls: str) -> None:
        """05 §2.1.1 ⑩⑪:``logging_in`` → 登录执行层判定(可注入 ``login_fn``;未接 ⇒ 回 ``login_required`` 等人)。密码用完置零、不落库、不进事件。"""
        if self._store.get_account_full(id)["state"] != "logging_in":
            self.transition(id, "logging_in", login_session_id=ls)
        try:
            result = await self._login_fn(row, self._login_account(row), secret)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("登录执行层异常 account=%s: %s", id, e)
            result = None
        finally:
            secret = None                                                       # 密码用完置零
        if result == "running":
            uid = self._pending_self_uid.pop(id, None)
            self._login_succeeded(id, ls, uid)
        elif result == "bad_credential":
            if row.get("credential_ref"):
                try:
                    await self._vault.flag(vault_name(row["credential_ref"]), suspect=True)      # 05 §2.1.1 ⑪c:凭据来自 Vault 则标 suspect
                except VaultUnavailable:
                    pass
                self._store.upsert_runtime(id, kind=self._store.RUNTIME_KIND[row["channel"]], suspect_credential=1, now_ms=self._clock())
            self.transition(id, "error", state_code="BAD_CREDENTIAL", state_reason="账号或密码错误", login_session_id=ls)
        elif isinstance(result, str) and result.startswith("WAIT_"):
            self.transition(id, "login_required", state_code=result, login_session_id=ls, prompt={"kind": result, "text": "等待人在画面完成验证"})
        elif row["channel"] == "wechat":
            # 🔴 微信没有账密登录(00 §354):走到 else 是执行层/环境异常,判 degraded(KEY_FAIL) 让用户走取钥重试,绝不设 WAIT_PASSWORD
            self.transition(id, "degraded", state_code="KEY_FAIL", state_reason="微信登录执行层未就绪,请重试取钥", login_session_id=ls)
        else:
            self.transition(id, "login_required", state_code="WAIT_PASSWORD", state_reason="登录执行层未接入", login_session_id=ls,
                            prompt={"kind": "WAIT_PASSWORD", "text": "请在画面完成登录"})
        if result != "running":                 # 没进 running 的这一轮,回填值不许留到下一次尝试(下轮重新读)
            self._pending_self_uid.pop(id, None)
            self._pending_ui_degraded.pop(id, None)

    def _login_succeeded(self, id: str, ls: str, uid: Optional[str]) -> None:
        self.transition(id, "running", state_code=None, login_session_id=ls, **({"self_uid": uid} if uid else {}))
        if id in self._pending_ui_degraded:                                  # 02 §2.2.3:落 default profile ⇒ degraded(UI_UNEXPECTED)
            ver = self._pending_ui_degraded.pop(id)
            self.transition(id, "degraded", state_code="UI_UNEXPECTED",
                            state_reason=f"企点定位表无 {ver or '本'} 版本的 profile,已退到 default.yaml")

    def _note_login_probe_health(self, id: str, reason: str, stage: str, *, recovered: bool = False) -> None:
        """只记录固定失败类别/阶段；相同故障去重，完整身份观察成功才记录恢复。"""
        if reason in ("login_probe_unavailable", "profile_unavailable", "self_uid_unavailable"):
            if stage not in ("profile", "foreground", "ui", "activity_structure", "identity", "foreground_recheck"):
                stage = "observation"
            failure = (reason, stage)
            if self._manual_login_probe_failures.get(id) != failure:
                self._manual_login_probe_failures[id] = failure
                log.warning("企点手工登录只读观察失败 account=%s reason=%s stage=%s", id, reason, stage)
        elif recovered and self._manual_login_probe_failures.pop(id, None) is not None:
            log.info("企点手工登录只读观察恢复 account=%s status=recovered", id)

    async def probe_manual_logins(self) -> None:
        """观察手工完成登录/验证的企点；不取凭据、不重登，旧观察不能覆盖新登录会话。"""
        if self._login_probe_fn is None:
            return
        waiting = ("WAIT_PASSWORD", "WAIT_SMS", "WAIT_CAPTCHA")
        for row in self._store.list_accounts(channel="qidian", state="login_required"):
            aid = row["id"]
            expected_uid = self._login_account(row)
            if (not row.get("enabled") or row.get("state_code") not in waiting or self.busy(aid)
                    or aid in self._manual_login_probes or not isinstance(expected_uid, str)
                    or not expected_uid.isascii() or not expected_uid.isdigit()):
                continue
            session = self._current_ls.get(aid)
            updated_ms = row["updated_ms"]
            self._manual_login_probes.add(aid)
            try:
                report = await asyncio.wait_for(self._login_probe_fn(row), 15)
                fresh = self._store.get_account_full(aid)
                if (not fresh or fresh.get("deleted_ms") or not fresh.get("enabled")
                        or fresh["state"] != "login_required" or fresh.get("state_code") not in waiting
                        or self.busy(aid) or self._current_ls.get(aid) != session
                        or fresh["updated_ms"] != updated_ms or self._login_account(fresh) != expected_uid):
                    continue
                matched = report.get("ready") is True and report.get("self_uid") == expected_uid
                self._note_login_probe_health(aid, report.get("reason"), report.get("stage"), recovered=matched)
                if not matched:
                    continue
                # self_uid 来自实际观察，不用登录名、手机号、邮箱作缺省 UID。
                profile = report.get("profile") or {}
                if profile.get("selection") == "default" and profile.get("app_version"):
                    self.note_default_profile(aid, profile["app_version"])
                ls = session or new_login_session_id()
                self.transition(aid, "logging_in", login_session_id=ls)
                self._pending_self_uid.pop(aid, None)
                self._login_succeeded(aid, ls, expected_uid)
                if profile.get("selection") == "fallback":
                    self._alerts_firing(QIDIAN_PROFILE_FALLBACK, aid, severity="info",
                                        evidence={"app_version": profile.get("app_version"), "profile": profile.get("version")})
                self._store.insert_audit(
                    kind="system", transport="system", actor="system:accounts", action="account.manual_login_observed",
                    account_id=aid, result_code="OK", detail={"source": "readonly_ui", "identity_match": True}, now_ms=self._clock(),
                )
            except Exception:
                self._note_login_probe_health(aid, "login_probe_unavailable", "observation")
            finally:
                self._manual_login_probes.discard(aid)

    # ------------------------------------------------------------------ #12 人发起登录 / #13 #14 凭据 / #15 prompt / #16b 取消
    async def login(self, id: str, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """#12:人发起登录(D-2:唯一的重登入口;05 §2.2.7 「不保存」路径提交密码);``202 {state:'logging_in', login_session_id}``。"""
        row = self.get(id)
        if row["state"] == "logging_in" or self.busy(id):
            raise ApiError(409, "NOT_APPLICABLE", "已有登录尝试在进行", reason="busy", extra={"current_login_session_id": self._current_ls.get(id, "")})
        if row["state"] in ("running", "degraded"):
            raise ApiError(409, "NOT_APPLICABLE", "账号已登录", reason="already_running")
        if row["state"] != "login_required":
            raise ApiError(409, "NOT_APPLICABLE", f"当前状态 {row['state']} 不可登录,请先 start", reason="bad_state")
        if row["channel"] == "qq":
            if body.get("mode", "qrcode") != "qrcode":
                raise ApiError(400, "INVALID_ARGS", "QQ 仅支持扫码登录", reason="bad_login_mode")
            ls = new_login_session_id()
            self._qq_attempts.pop(id, None)
            self._prompts.pop(id, None)
            self.transition(id, "logging_in", login_session_id=ls)
            self._spawn(id, self._qq_relogin(id, ls))
            self._store.insert_audit(kind="system", transport="system", actor=actor, action="account.login", account_id=id,
                                     result_code="OK", detail={"mode": "qrcode", "login_session_id": ls}, now_ms=self._clock())
            return {"state": "logging_in", "login_session_id": ls}
        if row["channel"] == "wechat":
            # 🔴 微信 PC 没有账密登录(00 §354):「人发起登录」= 重跑取钥流(WechatLoginFlow),绝不进 _run_login/WAIT_PASSWORD。
            # 此前缺这个分支,微信号落进企点通用账密流、login_fn 返回 None ⇒ 被错判成 WAIT_PASSWORD「请在画面完成登录」。
            ls = new_login_session_id()
            self._spawn(id, self._wechat_start(id))
            self._store.insert_audit(kind="system", transport="system", actor=actor, action="account.login", account_id=id,
                                     result_code="OK", detail={"mode": "wechat_keytry", "login_session_id": ls}, now_ms=self._clock())
            return {"state": "starting", "login_session_id": ls}
        mode = body.get("mode") or row["login_mode"]
        if mode not in LOGIN_MODES:
            raise ApiError(400, "INVALID_ARGS", "mode 须为 password|qrcode|manual", reason="bad_login_mode", extra={"details": [{"pointer": "/mode"}]})
        secret = body.get("secret")
        remember = bool(body.get("remember", False))                            # #12:remember 默认 false
        if secret is not None and not isinstance(secret, str):
            raise ApiError(400, "INVALID_ARGS", "secret 须为字符串", reason="bad_secret", extra={"details": [{"pointer": "/secret"}]})
        cols: dict[str, Any] = {}
        if body.get("account"):
            ident = dict(json.loads(row.get("identity_json") or "{}") or {})
            ident["login_account"] = str(body["account"])
            cols["settings_json"] = row.get("settings_json") or "{}"            # 占位,下面用 patch 单独写 identity
            self._store.con.execute("UPDATE accounts SET identity_json=?, updated_ms=? WHERE id=?", (json.dumps(ident, ensure_ascii=False), self._clock(), id))
            cols.pop("settings_json")
        ls = new_login_session_id()
        if secret and remember:                                                 # 再次勾「保存到保险库」
            ref = credential_ref(id)
            try:
                await self._vault.put(vault_name(ref), secret, scope="account")
                self._store.patch_account(id, credential_ref=ref, remember=True, now_ms=self._clock())
            except VaultUnavailable as e:
                log.warning("Vault 不可用(%s),本次登录不保存凭据", e.reason)
        if mode == "password" and not secret:
            if row["remember"] and row.get("credential_ref"):
                try:
                    secret = await self._vault.read(vault_name(row["credential_ref"]), trace_id=ls)
                except VaultUnavailable as e:
                    self.transition(id, "error", state_code="VAULT_UNAVAILABLE", state_reason=f"凭据保险库不可用({e.reason}),登录不进行", login_session_id=ls)
                    raise ApiError(503, "NOT_READY", "凭据保险库不可用,登录不进行", reason="vault_unavailable", retryable=True)
            if not secret:
                # 05 §2.5.4 / §2.2.7(R6-57 取代 R6-56 ① 的 400):Vault 无条目 ⇒ login_required(WAIT_PASSWORD),等人在卡片输入,不调执行层
                self.transition(id, "login_required", state_code="WAIT_PASSWORD", state_reason="无保存的凭据,请输入密码登录", login_session_id=ls,
                                prompt={"kind": "WAIT_PASSWORD", "text": "请输入密码登录(可勾选保存到保险库)"})
                self._store.insert_audit(kind="system", transport="system", actor=actor, action="account.login", account_id=id, result_code="OK",
                                         detail={"mode": mode, "remember": remember, "login_session_id": ls, "outcome": "WAIT_PASSWORD"}, now_ms=self._clock())
                return {"state": "login_required", "state_code": "WAIT_PASSWORD", "login_session_id": ls}
        row = self._store.get_account_full(id)
        self.transition(id, "logging_in", login_session_id=ls)
        self._spawn(id, self._run_login(id, row, secret, ls))
        secret = None
        self._store.insert_audit(kind="system", transport="system", actor=actor, action="account.login", account_id=id, result_code="OK",
                                 detail={"mode": mode, "remember": remember, "login_session_id": ls}, now_ms=self._clock())
        return {"state": "logging_in", "login_session_id": ls}

    async def _qq_relogin(self, id: str, ls: str) -> None:
        try:
            row = self.get(id)
            await self._runtime.set_napcat_webui(row, True)
            if self._current_ls.get(id) != ls or self.get(id)["state"] != "logging_in":
                return
            ad = self._adapters.get("qq")
            if ad is not None:
                await ad.stop(Account(id=id, channel="qq", state=row["state"]), graceful=True)
            await self._qq_start(id, self.get(id), ls)
        except Exception:
            if self._current_ls.get(id) == ls:
                self.transition(id, "error", state_code="ONEBOT_UNREACHABLE", state_reason="QQ 登录准备失败，请重试启动")

    async def set_credential(self, id: str, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """#13:只写 Vault 与 credential_ref,不登录;响应不回显。"""
        row = self.get(id)
        secret = body.get("secret")
        if not isinstance(secret, str) or not secret:
            raise ApiError(400, "INVALID_ARGS", "secret 必填", reason="secret_required", extra={"details": [{"pointer": "/secret"}]})
        ref = credential_ref(id)
        try:
            await self._vault.put(vault_name(ref), secret, scope="account")
        except VaultUnavailable as e:
            raise ApiError(503, "NOT_READY", f"凭据保险库不可用({e.reason})", reason="vault_unavailable", retryable=True)
        finally:
            secret = None
        if body.get("account"):
            ident = dict(json.loads(row.get("identity_json") or "{}") or {})
            ident["login_account"] = str(body["account"])
            self._store.con.execute("UPDATE accounts SET identity_json=?, updated_ms=? WHERE id=?", (json.dumps(ident, ensure_ascii=False), self._clock(), id))
        self._store.upsert_runtime(id, kind=self._store.RUNTIME_KIND[row["channel"]], suspect_credential=0, now_ms=self._clock())
        return self._store.patch_account(id, credential_ref=ref, remember=True, now_ms=self._clock())

    async def delete_credential(self, id: str, *, actor: str) -> dict[str, Any]:
        """#14:删 Vault 条目,remember=0、credential_ref=NULL。"""
        row = self.get(id)
        if row.get("credential_ref"):
            try:
                await self._vault.delete(vault_name(row["credential_ref"]))
            except VaultUnavailable as e:
                raise ApiError(503, "NOT_READY", f"凭据保险库不可用({e.reason})", reason="vault_unavailable", retryable=True)
        return self._store.patch_account(id, credential_ref=None, remember=False, now_ms=self._clock())

    def prompt(self, id: str, login_session_id: Optional[str] = None) -> dict[str, Any]:
        """#15:当前等人的提示,与 ``account_state.payload.prompt`` 同一对象;无等待 ``{kind:null}``。"""
        row = self.get(id)
        p = self._prompts.get(id)
        if row["state"] not in LOGIN_PHASE or p is None:
            return {"kind": None}
        if login_session_id and login_session_id != p["login_session_id"]:
            return {"kind": None}                                               # 那一次尝试已结束
        out = dict(p["prompt"])
        out.setdefault("kind", row.get("state_code"))
        out["login_session_id"] = p["login_session_id"]
        return out

    async def refresh_prompt(self, id: str, login_session_id: Optional[str] = None) -> dict[str, Any]:
        row = self.get(id)
        if row["channel"] != "qq":
            return self.prompt(id, login_session_id)
        attempt = self._qq_attempts.get(id)
        if (attempt is None or (login_session_id and login_session_id != attempt["session"])
                or not self._qq_attempt_current(id, attempt) or row["state"] != "login_required"):
            return {"kind": None}
        await self._qq_refresh(id, attempt, refresh=True)
        if not self._qq_attempt_current(id, attempt):
            return {"kind": None}
        return self.prompt(id, attempt["session"])

    async def login_cancel(self, id: str, login_session_id: Optional[str] = None, *, actor: str) -> dict[str, Any]:
        """#16b:取消一次登录尝试 / 释放微信槽位 pending;带 id 只取消指定那次,不等 ⇒ 幂等 no-op ``{cancelled:false, stale:true, current_login_session_id}``。"""
        row = self.get(id)
        now = self._clock()
        if row["channel"] == "wechat":
            win = self._store.pool_get("windows")
            cur = win["slot_pending_login_session_id"] if win and win["slot_pending"] == id else ""
            if not cur or (login_session_id is not None and login_session_id != cur):
                return {"cancelled": False, "stale": True, "current_login_session_id": cur}
            t = self.tasks.get(id)
            if t is not None and not t.done():                                    # 取消在跑的登录流,免得它继续续期 pending
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            if self._wechat_slot is not None:
                # 三列 + **无条件**回收中间产物 + 审计,全在 WechatSlot.release_pending 里(02 §2.2.5 ①)。
                # 既有实现的 `if row["host"] == "wsl"` 对微信恒假(微信账号 host='windows')⇒ 中间产物永不回收,是实现缺陷。
                await self._wechat_slot.release_pending(id, reason="user_cancel", actor=actor)
            else:
                self._store.wechat_slot_release_pending(id, now_ms=now)
                self._store.insert_audit(kind="system", transport="system", actor=actor, action="slot_pending_cancelled", account_id=id,
                                         result_code="OK", detail={"login_session_id": cur}, now_ms=now)
            self.transition(id, "stopped", login_session_id=cur, desired_state="stopped")
            return {"cancelled": True, "stale": False}
        cur = self._current_ls.get(id, "") if row["state"] in LOGIN_PHASE else ""
        if not cur:
            raise ApiError(409, "NOT_APPLICABLE", "当前没有进行中的登录尝试", reason="no_login_in_progress")
        if login_session_id is not None and login_session_id != cur:
            return {"cancelled": False, "stale": True, "current_login_session_id": cur}
        if row["channel"] == "qq":
            self._qq_attempts.pop(id, None)                            # 先使所有在途 QR/身份观察失效
            self._current_ls.pop(id, None)
        t = self.tasks.get(id)
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        code = "WAIT_QRCODE" if row["channel"] == "qq" else "WAIT_PASSWORD"
        self.transition(id, "login_required", state_code=code, state_reason="登录尝试已取消", login_session_id=cur,
                        prompt={"kind": code, "text": "请点「登录」重新登录"})
        self._current_ls.pop(id, None)                                          # 那一次尝试结束;下次 login 是新的 id
        self._prompts[id]["login_session_id"] = ""
        self._store.insert_audit(kind="system", transport="system", actor=actor, action="account.login_cancel", account_id=id, result_code="OK",
                                 detail={"login_session_id": cur}, now_ms=now)
        return {"cancelled": True, "stale": False}

    # ------------------------------------------------------------------ 05 §2.5.4 掉线登记 + 提醒
    def mark_offline(self, id: str, code: str, *, reason: str = "") -> dict[str, Any]:
        """掉线 ⇒ 只做三件事:置 ``login_required(code)``、推 ``account_state``(prompt 引导点「登录」)、``alert(warn)`` 一条;1 小时内第 3 次升 crit;**不自动重登**。"""
        if code not in OFFLINE_CODES:
            raise ValueError(f"掉线原因码须为 {OFFLINE_CODES},得到 {code!r}")
        row = self.get(id)
        if row["state"] not in ("running", "degraded", "logging_in", "login_required"):
            return row
        now = self._clock()
        rt = self._store.get_runtime(id) or {}
        last = rt.get("last_offline_ms")
        cnt = int(rt.get("offline_count_1h") or 0) + 1 if last and now - int(last) < 3600_000 else 1
        self._store.upsert_runtime(id, kind=self._store.RUNTIME_KIND[row["channel"]], last_offline_ms=now, last_offline_code=code, offline_count_1h=cnt, now_ms=now)
        self._current_ls.pop(id, None)
        row = self.transition(id, "login_required", state_code=code, state_reason=reason or f"掉线({code}),请重新登录", login_session_id="",
                              prompt={"kind": code, "text": "请点「登录」重新登录"}, trace_id=ulid(now))
        self._alerts_firing(ACCOUNT_OFFLINE, id, severity="crit" if cnt >= 3 else "warn",
                            evidence={"code": code, "offline_count_1h": cnt, "last_offline_at": iso8601(now)})
        return row

    _alerts = None                                                              # 装配后注入(alerts.Alerts)

    def _alerts_firing(self, code: str, id: str, **kw: Any) -> None:
        if self._alerts is not None:
            self._alerts.firing(code, subject=f"account:{id}", account_id=id, **kw)

    async def login_remind(self) -> int:
        """``login_required`` 期间每 ``[accounts] login_remind_interval_s`` 重发一条同 ``trace_id`` 的提醒事件(控制台去重);不计入告警。"""
        now = self._clock()
        n = 0
        for row in self._store.list_accounts(state="login_required"):
            aid = row["id"]
            rem = self._remind.setdefault(aid, {"trace_id": ulid(now), "last_ms": now})
            if now - rem["last_ms"] < self.cfg.accounts.login_remind_interval_s * 1000:
                continue
            p = self._prompts.get(aid)
            payload = self.view(row) | {"state_before": "login_required", "login_session_id": (p or {}).get("login_session_id", ""),
                                        "prompt": (p or {}).get("prompt"), "remind": True}
            self._events.emit("account_state", payload=payload, account_id=aid, channel=row["channel"], trace_id=rem["trace_id"], now_ms=now)
            rem["last_ms"] = now
            n += 1
        return n

    # ------------------------------------------------------------------ #20 能力矩阵 / #22 账号级设置 / #23 批量
    def capabilities_of(self, id: str) -> dict[str, Any]:
        """#20:``{capabilities:[…], matrix:{op:'supported|unsupported|not_applicable'}}``(该账号当前实际可用):目录里通道不支持 ⇒ unsupported;
        写类在非 running/degraded ⇒ not_applicable;读类恒 supported(登录阶段 get_state/screenshot 照常,00 §8.1)。"""
        row = self.get(id)
        ch, state = row["channel"], row["state"]
        matrix: dict[str, str] = {}
        for op, cap in self._caps_by_op.items():
            sup = (cap.get("channels") or {}).get(ch, "unsupported")
            if sup != "supported":
                matrix[op] = sup if sup in ("unsupported", "not_applicable") else "unsupported"
            elif cap.get("kind") == "read" or state in ("running", "degraded"):
                matrix[op] = "supported"
            else:
                matrix[op] = "not_applicable"
        return {"capabilities": sorted(op for op, v in matrix.items() if v == "supported"), "matrix": matrix}

    async def patch_settings(self, id: str, body: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """#22:05 §2.5.5 键子集;``auto_recover/capture_text/retention_days`` 落各自列,其余留 ``settings_json``;``retention_days > 30 → 400``(E-18);
        ``mail_route_id`` 须指向 ``mail_routes`` 中同账号或同通道的行否则 400;立即生效。"""
        row = self.get(id)
        bad = [k for k in body if k not in SETTINGS_KEYS]
        if bad:
            raise ApiError(400, "INVALID_ARGS", f"不可设置的键:{bad}", reason="bad_field", extra={"details": [{"pointer": f"/{k}"} for k in bad]})
        cols: dict[str, Any] = {}
        try:
            settings = json.loads(row.get("settings_json") or "{}") or {}
        except ValueError:
            settings = {}
        if "auto_recover" in body:
            if not isinstance(body["auto_recover"], bool):
                raise ApiError(400, "INVALID_ARGS", "auto_recover 须为 bool", reason="bad_auto_recover", extra={"details": [{"pointer": "/auto_recover"}]})
            cols["auto_recover"] = body["auto_recover"]
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
        if "mail_route_id" in body:
            v = body["mail_route_id"]
            if v is not None:
                if not isinstance(v, int):
                    raise ApiError(400, "INVALID_ARGS", "mail_route_id 须为整数或 null", reason="bad_mail_route", extra={"details": [{"pointer": "/mail_route_id"}]})
                r = self._store.con.execute("SELECT channel, account_id FROM mail_routes WHERE id=?", (v,)).fetchone()
                if r is None or r["channel"] != row["channel"] or (r["account_id"] is not None and r["account_id"] != id):
                    raise ApiError(400, "INVALID_ARGS", "mail_route_id 须指向 mail_routes 中同账号或同通道的路由行(E-5)", reason="mail_route_mismatch",
                                   extra={"details": [{"pointer": "/mail_route_id"}]})
            settings["mail_route_id"] = v
        if "auto_stop_on_pressure" in body:
            if not isinstance(body["auto_stop_on_pressure"], bool):
                raise ApiError(400, "INVALID_ARGS", "auto_stop_on_pressure 须为 bool", reason="bad_auto_stop", extra={"details": [{"pointer": "/auto_stop_on_pressure"}]})
            settings["auto_stop_on_pressure"] = body["auto_stop_on_pressure"]
        if "send" in body:
            s = body["send"]
            if not isinstance(s, dict) or any(k not in ("min_interval_ms", "jitter_ms", "max_per_minute") or not isinstance(x, int) or x < 0 for k, x in s.items()):
                raise ApiError(400, "INVALID_ARGS", "send 须为 {min_interval_ms?, jitter_ms?, max_per_minute?} 非负整数", reason="bad_send", extra={"details": [{"pointer": "/send"}]})
            settings["send"] = {**(settings.get("send") or {}), **s}
        if "sessions" in body:
            s = body["sessions"]
            if not isinstance(s, dict) or not isinstance(s.get("allowlist"), list) or not all(isinstance(x, str) and x for x in s["allowlist"]):
                raise ApiError(400, "INVALID_ARGS", "sessions.allowlist 须为非空字符串数组(可含 \"*\")", reason="bad_allowlist", extra={"details": [{"pointer": "/sessions/allowlist"}]})
            settings["sessions"] = {"allowlist": list(s["allowlist"])}
        if "gates" in body:
            g = body["gates"]
            if not isinstance(g, dict) or not isinstance(g.get("custom"), list) or not all(isinstance(x, str) for x in g["custom"]):
                raise ApiError(400, "INVALID_ARGS", "gates.custom 须为字符串数组", reason="bad_gates", extra={"details": [{"pointer": "/gates/custom"}]})
            settings["gates"] = {"custom": list(g["custom"])}
        cols["settings_json"] = json.dumps(settings, ensure_ascii=False)
        row = self._store.patch_account(id, now_ms=self._clock(), **cols)
        self._store.insert_audit(kind="system", transport="system", actor=actor, action="settings.update", account_id=id, result_code="OK",
                                 detail={"keys": sorted(body.keys())}, now_ms=self._clock())
        return row

    async def batch(self, body: dict[str, Any], *, actor: str) -> dict[str, dict[str, Any]]:
        """#23:显式列 id(不接受 ``*``),内部串行,任一失败不回滚其它;``{results:{id:{ok, code}}}``。"""
        ids, action = body.get("ids"), body.get("action")
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids) or "*" in ids:
            raise ApiError(400, "INVALID_ARGS", "ids 须为显式账号 id 数组,不接受 \"*\"", reason="bad_ids", extra={"details": [{"pointer": "/ids"}]})
        if action not in BATCH_ACTIONS:
            raise ApiError(400, "INVALID_ARGS", f"action 须为 {'|'.join(BATCH_ACTIONS)}", reason="bad_action", extra={"details": [{"pointer": "/action"}]})
        results: dict[str, dict[str, Any]] = {}
        for aid in ids:
            try:
                fn = getattr(self, action)
                if action in ("start", "stop", "restart"):
                    await fn(aid, actor=actor)
                    await self.wait_idle(aid)                                   # 批量内部仍串行(05 §2.5.2)
                elif action == "disable":
                    await fn(aid, graceful=True, actor=actor)
                else:
                    await fn(aid, actor=actor)
                results[aid] = {"ok": True, "code": "OK"}
            except ApiError as e:
                results[aid] = {"ok": False, "code": e.code, "reason": e.reason}
            except Exception as e:                                              # 单个失败不影响其它
                results[aid] = {"ok": False, "code": "INTERNAL", "reason": str(e)}
        return {"results": results}

    async def restart(self, id: str, *, actor: str) -> dict[str, Any]:
        row = self.get(id)
        if not row["enabled"]:
            raise ApiError(409, "NOT_APPLICABLE", "账号已停用", reason="account_disabled")
        if self.busy(id):
            raise ApiError(409, "NOT_APPLICABLE", "账号正在切换状态", reason="busy")
        ok, reason, alts = self._pool.can_add(row["channel"], exclude_account_id=id) if row["state"] in ("stopped", "created", "error") else (True, "", [])
        if not ok:
            self._raise_exhausted(row["channel"], reason, alts, exclude_account_id=id)

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
            if row["channel"] == "qq":
                self._qq_attempts.pop(id, None)
                ad = self._adapters.get("qq")
                if ad is not None:
                    await ad.stop(Account(id=id, channel="qq", state=row["state"]), graceful=graceful)
            if row["channel"] != "wechat":
                await self._runtime.stop(row, graceful=graceful)
            row = self.transition(id, "stopped", desired_state="stopped")          # 账号已置 stopped 之后……
            if row["channel"] != "wechat":
                await self._runtime._purge_ephemeral(row)                            # ……同步清可再生临时数据(05 §2.5.7)……
            else:
                self._release_holder(id)                                             # 微信释放槽位(收口到 WechatSlot)
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
            self._release_holder(id)
        return {"deleted": True, "data_kept": True, "id": id}

    def _release_holder(self, id: str) -> int:
        """微信 holder 释放的唯一落点:装配后走 ``WechatSlot.release_holder``(槽位语义收在一个模块),否则退回 store。"""
        if self._wechat_slot is not None:
            return int(self._wechat_slot.release_holder(id, now_ms=self._clock()))
        return int(self._store.wechat_slot_release_holder(id, now_ms=self._clock()))

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
            v = body["media_policy"]
            if v is not None and not isinstance(v, dict):
                raise ApiError(400, "INVALID_ARGS", "media_policy 须为对象", reason="bad_media_policy", extra={"details": [{"pointer": "/media_policy"}]})
            cols["media_policy_json"] = None if v is None else json.dumps(v, ensure_ascii=False)
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
                if self.pressure is not None and self.pressure.blocked():        # E-19:critical 水位下尚未拉起的一律跳过,已在跑的不动
                    results.append({"id": id, "ok": False, "reason": "mem_pressure"})
                    continue
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
