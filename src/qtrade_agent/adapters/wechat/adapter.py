"""微信适配器(02 §2.2.3 wechat 条)。

- **Agent 侧不直接碰 chatlog/pyweixin**,全部经 WinAgent ``/wa/v1/wechat/*``(§3.6);``ChatlogClient`` 与
  ``normalize.filter_and_normalize`` 住在 WinAgent 用户会话代理的 wechat 模块里,Agent 只消费 #39 的归一化结果。
- ``state_store.PersistentPollState`` 的 ``(talker, seq)`` 去重语义搬到 Agent 的 ``cursors`` + ``messages`` 唯一索引
  (06 §2.8/§2.9),JSON 文件形态废弃。
- **取钥失败 `degraded(KEY_FAIL)` 下读写都拒**(C-19 / 05 §2.4.7):写回 ``NOT_READY``
  ``"微信解密不可用,无法确认送达,已拒绝发送"``(chatlog 是微信**唯一**的送达确认手段,没有它的写 =
  每条都判失败却可能已经发出去,是最危险的形态)。
- **锁屏 `degraded(SCREEN_LOCKED)`(C-20 / 05 §2.5.4)**:**读取正常**(chatlog 不依赖桌面),
  **写类拒 `NOT_READY`**(pywinauto 拿不到前台窗口)——与 ``KEY_FAIL`` 的「读写都拒」不同。
- **不做自动重登(D-2)**:没有 ``relogin()``;``start()`` 只建连,发现未登录即回 ``login_required``;
  运行期检测到主窗口消失/进程退出只置 ``login_required(LOGGED_OUT)`` + 发事件,**不自动拉起微信、不自动登回**。
- ``send``:走 #38，返回可信原生 ID 后持久保存精确读回证据；再经 #39 取真实原生行，按 trace 绑定
  ``bus`` 先落的 ``SENDING`` 行。可信超时仅保留发送前证明，后续完整原生读取核验后才绑定。
  ``[bus] confirm_timeout_wechat_ms=10000`` 内没等到 ⇒ ``SEND_FAILED``(C.4.2 微信口径)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import math
from datetime import datetime
from typing import Any, Optional

from ...models import Command, CommandError, CommandResult
from ...events import message_payload
from ...store import Store
from ..base import Account
from .client import WeChatCallFailed, WeChatNotReady, WeChatWinAgent
from .normalize import to_message
from .poll import WechatAccountView, WechatPoller

log = logging.getLogger("qtrade.adapters.wechat")

KEY_FAIL_MESSAGE = "微信解密不可用,无法确认送达,已拒绝发送"      # 05 §2.4.7 逐字
SCREEN_LOCKED_MESSAGE = "Windows 已锁屏,发送不可用"                # 05 §2.5.4 state_reason 逐字
WECHAT_NOT_RUNNING_CODE = "WECHAT_NOT_RUNNING"
WECHAT_AMBIGUOUS_CODE = "WECHAT_WINDOW_AMBIGUOUS"
WECHAT_NOT_RUNNING_MESSAGE = "采集端显示该微信号在运行，但当前系统没有微信窗口，已停止"
WECHAT_AMBIGUOUS_MESSAGE = "桌面上有多个微信窗口，无法确定发到哪一个，已停止"
_DESKTOP_FAULTS = {
    "wechat_window_missing": (WECHAT_NOT_RUNNING_CODE, WECHAT_NOT_RUNNING_MESSAGE),
    "wechat_window_ambiguous": (WECHAT_AMBIGUOUS_CODE, WECHAT_AMBIGUOUS_MESSAGE),
}


class _ReadbackInvalidated(ValueError):
    """An observed contradiction permanently fences this original proof."""


class WechatAdapter:
    channel = "wechat"
    # 目录里 wechat='supported' 的五个 op(02 §3.10 / capabilities/*.json);degraded 下的实际可用由 #20 矩阵与本文件的闸共同决定
    capabilities = frozenset({"send_text", "read_messages", "get_state", "screenshot", "list_sessions"})

    def __init__(self, poller: WechatPoller, *, client: WeChatWinAgent, store: Store, media_put=None):
        """``media_put(acct, png) -> {"media_id":…, "sha256":…}``:装配方注入的落盘器(02 §2.8.2);
        不注入(或落盘失败回空)时截图的图片体以 ``png_b64`` 回,**任何情况下都不把裸 ``bytes`` 塞进 ``data``**
        —— 见 ``_screenshot_data``。"""
        self._poller = poller
        self._client = client
        self._store = store
        self._media_put = media_put
        self._stop_account = None
        self._key_retry_needed: dict[str, bool] = {}

    @staticmethod
    def _view(acct: Account) -> WechatAccountView:
        return WechatAccountView(acct.id, acct.state, acct.self_uid, acct.self_nick)

    @staticmethod
    def _native(acct: Account, session: str) -> str:
        return session.split(":", 1)[1] if session.startswith(acct.id + ":") else session

    # ------------------------------------------------------------------ 生命周期
    async def start(self, acct: Account) -> None:
        """建连(不阻塞到登录,02 §2.2.3):微信的「拉起 + 挂 hook」由 WinAgent 会话代理在 #31 里做,
        Agent 侧 ``start`` 只清一次该账号的读循环内存态,登录由 ``WechatLoginFlow`` 驱动。"""
        self._poller.state.pop(acct.id, None)

    async def retry_read(self, acct: Account) -> None:
        """An explicit read retry never restarts WeChat, Chatlog, or the key hook."""
        state = self._poller.st(acct.id)
        async with state.lock:
            state.retry_attempts = 0
            state.retry_started_ms = None
            state.retry_exhausted = False
            state.next_retry_ms = 0
            # Preserve the safety guard until a read has actually succeeded.
            state.read_fail_streak = max(state.read_fail_streak, 3)
        await self._poller.poll(self._view(acct))


    async def stop(self, acct: Account, *, graceful: bool) -> None:
        """停止对该账号的 ``wechat/read`` 拉取(05 §2.4.5 第 2 步);**登出微信是 #34**,由槽位切换/`#16 logout` 发起,
        不在这里顺手做——``stop`` 也会被健康循环调用,顺手登出会误踢在用的号。"""
        self._poller.state.pop(acct.id, None)

    async def get_state(self, acct: Account) -> str:
        """05 §2.5.4 微信三行的落地(检测手段 → 状态),返回 00 §8.1 的 ``state``;``state_code`` 由调用方按 ``last_state_code`` 取。"""
        state, code, _reason = await self.probe_state(acct)
        self.last_state_code = code
        return state

    def key_retry_required(self, account_id: str) -> bool:
        return self._key_retry_needed.get(account_id, False)

    async def probe_state(self, acct: Account, *, require_identity: bool = False) -> tuple[str, Optional[str], str]:
        """返回 ``(state, state_code, state_reason)``。判定顺序(实现口径,已登记 handoff「建议裁决」):
        会话代理/模块不可用 → 微信未登录 → **取钥失败优先于锁屏**(前者读写都拒、比后者严格)→ 锁屏 → running。"""
        self._key_retry_needed[acct.id] = False
        try:
            st = await self._client.status()
        except WeChatNotReady as e:
            return "degraded", "WINAGENT_USER_OFFLINE" if e.reason == "user_agent_offline" else "WINAGENT_OFFLINE", "WinAgent 微信模块不可用"
        except WeChatCallFailed as e:
            return "degraded", "WINAGENT_OFFLINE", f"WinAgent 微信端点异常 status={e.status}"
        if not st.get("enabled"):
            return "degraded", "WINAGENT_USER_OFFLINE", "winagent.toml [wechat] enabled=false(未启用微信模块)"
        wx = st.get("wechat") or {}
        chatlog = st.get("chatlog") or {}
        count = wx.get("window_count")
        # 只有明确报了 0 个窗口才是「号在跑、桌面没窗」。没报 window_count 且进程不在，仍是登出。
        if count == 0:
            return "stopped", WECHAT_NOT_RUNNING_CODE, WECHAT_NOT_RUNNING_MESSAGE
        if isinstance(count, int) and count > 1:
            return "stopped", WECHAT_AMBIGUOUS_CODE, WECHAT_AMBIGUOUS_MESSAGE
        if type(wx.get("running")) is not bool or type(wx.get("logged_in")) is not bool:
            return "degraded", "WECHAT_READ_FAILED", "微信登录探测信息不完整，暂不能确认收发状态；不自动重新登录或取钥"
        if not wx["running"] or not wx["logged_in"]:
            # Only an explicit native negative observation is a logout.
            return "login_required", "LOGGED_OUT", "微信已退出登录,请点「登录」重新扫码"
        if require_identity and (not acct.self_uid or wx.get("wxid") != acct.self_uid):
            return "degraded", "WECHAT_READ_FAILED", "无法确认当前微信与原绑定账号一致，保留原状态，不自动重新绑定"
        self._key_retry_needed[acct.id] = chatlog.get("key_ok") is False
        if chatlog.get("key_ok") is False:
            return "degraded", "KEY_FAIL", KEY_FAIL_MESSAGE
        if chatlog.get("key_ok") is not True:
            return "degraded", "WECHAT_READ_FAILED", "无法确认密钥与读取服务状态，不自动重新取钥"
        if not chatlog.get("running") or self._poller.degraded_read_failed(acct.id):
            return "degraded", "WECHAT_READ_FAILED", "密钥已就绪，但微信消息读取异常，无法确认送达，暂停发送；无需重新取钥或重装微信"
        if st.get("screen_locked"):
            return "degraded", "SCREEN_LOCKED", SCREEN_LOCKED_MESSAGE
        return "running", None, ""

    last_state_code: Optional[str] = None

    # ------------------------------------------------------------------ 写闸(05 §2.4.7 / §2.5.4 锁屏行)
    def write_guard(self, acct: Account) -> Optional[CommandResult]:
        """写类指令的前置:``degraded(KEY_FAIL)`` / ``degraded(SCREEN_LOCKED)`` 一律 ``NOT_READY``;可写回 ``None``。"""
        code = acct.state_code
        if code == "WECHAT_READ_FAILED" or self._poller.degraded_read_failed(acct.id):
            return _not_ready("微信消息读取未恢复，无法确认送达，已拒绝发送", "wechat_read_failed")
        if acct.state == "degraded" and code == "KEY_FAIL":
            return _not_ready(KEY_FAIL_MESSAGE, "key_fail")
        if acct.state == "degraded" and code == "SCREEN_LOCKED":
            return _not_ready(SCREEN_LOCKED_MESSAGE, "screen_locked")
        return None

    def report_desktop_fault(self, account_id: str, code: str, reason: str) -> None:
        """没有微信或多开微信：记错误日志，并把账号停到「已停止」。界面提示由状态事件带出。"""
        log.error("微信账号已停止 account=%s code=%s: %s", account_id, code, reason)
        if self._stop_account is not None:
            self._stop_account(account_id, code, reason)

    def read_guard(self, acct: Account) -> Optional[CommandResult]:
        """``KEY_FAIL`` 下 chatlog 拿不到 Data Key、库读不了 ⇒ ``read_messages`` 也不可用(05 §2.4.7 读那一条);
        锁屏**不**挡读(chatlog 不依赖桌面)。"""
        if acct.state == "degraded" and acct.state_code == "KEY_FAIL":
            return _not_ready(KEY_FAIL_MESSAGE, "key_fail")
        return None

    # ------------------------------------------------------------------ 指令
    async def execute(self, acct: Account, cmd: Command) -> CommandResult:
        trace = cmd.trace_id or ""
        if cmd.op == "get_state":
            state, code, reason = await self.probe_state(acct)
            return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog",
                                 data={"state": state, "state_code": code, "state_reason": reason, "self_uid": acct.self_uid})
        if cmd.op == "read_messages":
            blocked = self.read_guard(acct)
            if blocked is not None:
                return _with_trace(blocked, trace)
            session = cmd.args.get("session")
            session_id = f"{acct.id}:{self._native(acct, session)}" if session else None
            rows = self._store.list_messages(acct.id, session_id=session_id, limit=int(cmd.args.get("limit") or 100))
            return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"items": rows})
        if cmd.op == "list_sessions":
            try:
                data = await self._client.sessions(keyword=cmd.args.get("keyword"), limit=cmd.args.get("limit"))
            except WeChatNotReady as e:
                return _with_trace(_not_ready(f"微信会话代理不可用({e.reason})", e.reason), trace)
            return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"items": data})
        if cmd.op == "screenshot":
            try:
                png = await self._client.screenshot()
            except WeChatNotReady as e:
                return _with_trace(_not_ready(f"微信窗口截图不可用({e.reason})", e.reason), trace)
            return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data=self._screenshot_data(acct, png))
        return CommandResult(ok=False, code="UNSUPPORTED", trace_id=trace, source="chatlog")

    def _screenshot_data(self, acct: Account, png: bytes) -> dict[str, Any]:
        """``screenshot`` 的 ``CommandResult.data`` —— 形态以**能力目录**为准:
        ``capabilities/screenshot.json`` 的 ``result_schema = {png_b64, width, height}``
        (02 §3.10:``result_schema`` 就是 ``CommandResult.data``,``send_text`` 那行写得最明白)。

        🔴 **D-1**:``bus._finalize → store.finish_command`` 要把 ``data`` 整体 ``json.dumps`` 落
        ``command_results.data_json``,裸字节会 ``TypeError`` ⇒ 整条能力经总线恒 ``INTERNAL``。
        修法是**图片体一律另给 JSON 形态**(``png_b64`` 或 ``media/`` 引用),落库/出 JSON 两处再经
        ``models.json_safe()`` 把 ``data['png']`` 这类裸字节换成 ``{__binary__, len}`` 占位。
        ``data['png']`` 只给**进程内**消费方(#33 的二进制出口、直调 ``execute`` 的调用方),
        不是序列化路径上的键 —— 它冗余于 ``png_b64``,等 #33 端点接上就该删(见 handoff)。

        ``media_put`` 注入时按 02 §2.8.2 落 ``media/`` 并只给引用(``media_id``/``sha256``),
        此时**不再重复塞 ``png_b64``** —— 图片体已经在 ``media/`` 里,再塞一份会让 ``data_json`` 平白大一倍;
        没落盘(未注入 / 落盘失败)才带 ``png_b64``,保证 ``result_schema`` 里的图片体始终取得到。
        """
        import base64
        import hashlib
        data: dict[str, Any] = {"mime": "image/png", "png_len": len(png), "sha256": hashlib.sha256(png).hexdigest()}
        size = _png_size(png)
        if size is not None:
            data["width"], data["height"] = size
        ref = self._media_put(acct, png) if self._media_put is not None else {}
        if ref.get("media_id") is not None:
            data.update({k: v for k, v in ref.items() if k in ("media_id", "sha256")})
        else:
            data["png_b64"] = base64.b64encode(png).decode("ascii")
        data["png"] = png                              # 进程内出口;序列化侧由 models.json_safe() 兜住
        return data

    async def send(self, acct: Account, cmd: Command) -> CommandResult:
        """#38 写后，仅以 #39 真实行和持久精确 trace/原生 ID 证据绑定发送占位。"""
        trace = cmd.trace_id or ""
        blocked = self.write_guard(acct)
        if blocked is not None:
            return _with_trace(blocked, trace)
        native = self._native(acct, cmd.args["session"])
        self._poller.note_send(acct.id)                    # 05 §2.4.4 ⑦:发送确认期把拉取周期加密到 1 s
        request_key = cmd.idempotency_key or trace
        confirm_timeout_ms = self._poller.cfg.bus.confirm_timeout_wechat_ms
        send_started = self._poller.clock()
        identity_epoch = self._store.native_identity(acct.id).get("epoch", 0)
        try:
            identity = await self._readback_identity(acct)
            if identity is None:
                return _with_trace(_not_ready("无法验证当前微信账号，已拒绝发送", "wechat_send_identity_unverified"), trace)
            try:
                res = await self._client.send(session_name=native, text=cmd.args.get("text"),
                                              idempotency_key=request_key,
                                              confirm_timeout_ms=confirm_timeout_ms)
            except (WeChatNotReady, WeChatCallFailed):
                raise
            except Exception:
                return CommandResult(ok=False, code="SEND_FAILED", trace_id=trace, source="chatlog",
                                     error=CommandError("微信发送调用结果未知，请核对原生会话；禁止自动补发",
                                                        reason="send_outcome_unknown", retryable=False, needs_human=True))
        except WeChatNotReady as e:
            fault = _DESKTOP_FAULTS.get(e.reason)
            if fault is not None:
                code, reason = fault
                self.report_desktop_fault(acct.id, code, e.detail or reason)
                return _with_trace(_not_ready(e.detail or reason, e.reason), trace)
            return _with_trace(_not_ready(e.detail or f"微信发送不可用({e.reason})", e.reason), trace)
        except WeChatCallFailed as e:
            err = (e.body.get("error") or {}) if isinstance(e.body, dict) else {}
            if e.status == 404:
                # R6-92:找不到会话 / 显示名重名 ⇒ WinAgent **没有发**(重名拒发是防发错对象);不是 SEND_FAILED,不可盲重试
                return CommandResult(ok=False, code="TARGET_NOT_FOUND", trace_id=trace, source="chatlog",
                                     error=CommandError(str(err.get("message") or "微信会话不存在或无法唯一定位"),
                                                        reason=str(err.get("reason") or "target_not_found"), retryable=False))
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=trace, source="chatlog",
                                 error=CommandError(str(err.get("message") or f"WinAgent 发送失败 status={e.status}"),
                                                    reason="send_outcome_unknown", retryable=False, needs_human=True))
        if not isinstance(res, dict):
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=trace, source="chatlog",
                                 error=CommandError("微信发送响应无法验证，请核对原生会话；禁止自动补发",
                                                    reason="send_outcome_unknown", retryable=False, needs_human=True))
        reason = res.get("confirmation_reason")
        diagnostic = ({"native_confirmation_reason": reason}
                      if isinstance(reason, str) and reason in {
                          "confirmed", "confirm_timeout", "identity_changed", "anchor_changed",
                          "ambiguous_candidate", "invalid_snapshot", "read_failed"} else {})
        if res.get("ok") is True and res.get("code") == "DELIVERED" and res.get("ext_msg_id"):
            try:
                current = await self._readback_identity(acct)
            except (WeChatNotReady, WeChatCallFailed):
                current = None
            ext = res["ext_msg_id"]
            if current == identity and isinstance(ext, str) and trace:
                record = {"version": 1, "account_id": acct.id, "self_uid": identity[0],
                          "identity_epoch": self._store.native_identity(acct.id).get("epoch", 0),
                          "native": native, "ext": ext, "started_ms": send_started,
                          "text_sha256": hashlib.sha256(cmd.args["text"].encode("utf-8")).hexdigest(),
                          "status": "pending"}
                key = "wechat.readback." + trace
                old = self._store.settings_get(key)
                if old is None:
                    self._store.settings_set(key, record, actor="system:wechat_adapter", now_ms=self._poller.clock())
                elif not isinstance(old, dict) or any(old.get(k) != record[k] for k in record if k != "status"):
                    return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"native_id": native, **diagnostic})
                if await self._ingest_readback(acct, trace, old or record):
                    return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog",
                                         data={"ext_msg_id": ext, "confirm_ms": res.get("confirm_ms"), **diagnostic})
            return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"native_id": native, **diagnostic})
        if res.get("code") == "SEND_FAILED":
            try:
                now = self._poller.clock()
            except Exception:
                now = None
            submitted = cmd.submitted_at_ms
            if (res.get("ok") is False and res.get("ext_msg_id") is None
                    and type(confirm_timeout_ms) is int and confirm_timeout_ms > 0
                    and type(res.get("confirm_ms")) is int and res["confirm_ms"] == confirm_timeout_ms
                    and request_key and res.get("idempotency_key") == request_key
                    and type(submitted) is int and type(now) is int and 0 < submitted <= now
                    and type(cmd.timeout_ms) is int and cmd.timeout_ms > 0
                    and now + confirm_timeout_ms < submitted + cmd.timeout_ms + confirm_timeout_ms):
                proof = res.get("confirmation_proof")
                if trace and _valid_confirmation_proof(proof, native=native, self_uid=identity[0],
                        text_sha256=hashlib.sha256(cmd.args["text"].encode("utf-8")).hexdigest(),
                        request_key=request_key, started_ms=send_started, now_ms=now):
                    try:
                        current = await self._readback_identity(acct)
                    except (WeChatNotReady, WeChatCallFailed):
                        current = None
                    if (current == identity and proof["native_pid"] == identity[1]
                            and identity_epoch == self._store.native_identity(acct.id).get("epoch", 0)):
                        record = {"version": 2, "account_id": acct.id, "self_uid": identity[0],
                                  "identity_epoch": identity_epoch, "native": native, "started_ms": send_started,
                                  "text_sha256": proof["text_sha256"], "proof": proof, "status": "pending"}
                        key = "wechat.readback." + trace
                        if self._store.settings_get(key) is None:
                            self._store.settings_set(key, record, actor="system:wechat_adapter",
                                                     now_ms=self._poller.clock())
                # Preserve the existing extra window; only an original proof enables later attribution.
                return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"native_id": native, **diagnostic})
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=trace, source="chatlog", data=diagnostic,
                                 error=CommandError("微信 10 秒内未在聊天记录读到(C.4.2)",
                                                    reason="send_outcome_unknown", retryable=False, needs_human=True))
        # WinAgent 只回了「点完了」:读回确认交给 poll 的 ingest 合并,bus 在队列外等那一行翻 DELIVERED
        return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"native_id": native, **diagnostic})

    async def _readback_identity(self, acct: Account) -> Optional[tuple[str, int]]:
        status = await self._client.status()
        native = status.get("wechat") if isinstance(status, dict) else None
        if not isinstance(native, dict):
            return None
        wxid, pid = native.get("wxid"), native.get("pid")
        if (not acct.self_uid or wxid != acct.self_uid or native.get("logged_in") is not True
                or native.get("running") is not True or type(pid) is not int or pid <= 0):
            return None
        return wxid, pid

    def _readback_record_current(self, acct: Account, trace: str, record: dict[str, Any]) -> bool:
        if record.get("status") != "pending" or self._store.settings_get("wechat.readback." + trace) != record:
            return False
        if (type(record.get("version")) is not int or record["version"] not in (1, 2) or record.get("account_id") != acct.id
                or not acct.self_uid or record.get("self_uid") != acct.self_uid
                or record.get("identity_epoch") != self._store.native_identity(acct.id).get("epoch", 0)):
            return False
        command = self._store.get_command(trace)
        if (command is None or command["account_id"] != acct.id or command["op"] != "send_text"
                or command.get("identity_epoch") != record["identity_epoch"]):
            return False
        key = command.get("idempotency_key")
        if key:
            idem = self._store.idem_get(acct.id, key)
            if not idem or idem.get("trace_id") != trace or idem.get("status") == "IDENTITY_FENCED":
                return False
        if record["version"] == 2:
            try:
                args = json.loads(command["args_json"])
                native = self._native(acct, args["session"])
                digest = record.get("text_sha256")
                # Commands deliberately retain only sha8; Store.bind checks the placeholder's full hash.
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(char not in "0123456789abcdef" for char in digest)
                        or args.get("text_sha8") != digest[:8]):
                    return False
            except (KeyError, TypeError, ValueError, AttributeError):
                return False
            if (record.get("native") != native
                    or not _valid_confirmation_proof(record.get("proof"), native=native, self_uid=acct.self_uid,
                        text_sha256=digest, request_key=key or trace, started_ms=record.get("started_ms"),
                        now_ms=self._poller.clock())):
                return False
        return True

    async def _proof_readback(self, native: str, proof: dict[str, Any]) -> list[dict[str, Any]]:
        """A short page may be byte-truncated: require the final empty page."""
        watermark, anchor = proof["max_seq"], proof["anchor"]
        since = watermark - 1 if watermark > 0 else None
        previous = since if since is not None else -1
        anchor_seen, hits = anchor is None, []
        for _ in range(4):
            rows = await self._client.read(talker=native, since_seq=since, limit=500)
            if not isinstance(rows, list) or len(rows) > 500:
                return []
            if not rows:
                return hits if anchor_seen and len(hits) == 1 else []
            for row in rows:
                if not isinstance(row, dict):
                    return []
                value = row.get("seq")
                if (type(value) not in (int, str) or not str(value).isascii()
                        or not str(value).isdecimal() or str(int(value)) != str(value)):
                    return []
                seq = int(value)
                stamp = _readback_seconds(row)
                body = row.get("content") if row.get("content") is not None else row.get("text")
                digest = hashlib.sha256(body.encode("utf-8")).hexdigest() if isinstance(body, str) else None
                if (seq <= previous or row.get("talker") != native
                        or row.get("ext_msg_id") not in (None, f"{native}:{seq}")
                        or stamp is None or stamp > self._poller.clock() / 1000 + 1):
                    return []
                previous = seq
                if anchor is not None and seq == watermark:
                    if stamp != anchor["time_s"] or digest != anchor["text_sha256"]:
                        raise _ReadbackInvalidated("anchor_changed")
                    anchor_seen = True
                own = row.get("isSelf") if "isSelf" in row else row.get("is_self")
                if (seq > watermark and own is True and type(row.get("type")) is int and row["type"] == 1
                        and digest == proof["text_sha256"] and stamp >= math.floor(proof["send_started_at_s"])
                        and stamp <= proof["action_completed_at_s"] + 1):
                    hits.append(row)
                    if len(hits) > 1:
                        raise _ReadbackInvalidated("ambiguous_candidate")
            since = previous
        return []  # A bounded prefix is not proof of a unique candidate.

    async def _ingest_readback(self, acct: Account, trace: str, record: dict[str, Any]) -> bool:
        """Fetch the exact native ID; request bodies and current time are never evidence."""
        if not self._readback_record_current(acct, trace, record):
            return False
        native, ext = record.get("native"), record.get("ext")
        started_ms = record.get("started_ms")
        if not isinstance(native, str) or type(started_ms) is not int:
            return False
        try:
            identity = await self._readback_identity(acct)
            if identity is None:
                return False
            if record["version"] == 2:
                proof = record["proof"]
                if identity != (proof["self_uid"], proof["native_pid"]):
                    return False
                rows = await asyncio.wait_for(self._proof_readback(native, proof),
                    timeout=self._poller.cfg.bus.confirm_timeout_wechat_ms / 1000)
                if len(rows) != 1:
                    return False
                raw_seq = str(rows[0]["seq"])
                ext = f"{native}:{raw_seq}"
            else:
                if not isinstance(ext, str):
                    return False
                prefix, _, raw_seq = ext.rpartition(":")
                if prefix != native or not raw_seq.isdigit() or str(int(raw_seq)) != raw_seq or int(raw_seq) <= 0:
                    return False
                rows = await self._client.read(talker=native, since_seq=int(raw_seq) - 1, limit=2)
            if await self._readback_identity(acct) != identity:
                return False
            hits = []
            for row in rows:
                own = row.get("isSelf") if "isSelf" in row else row.get("is_self")
                native_seq = row.get("seq")
                if (row.get("talker") != native or type(native_seq) is bool
                        or not isinstance(native_seq, (int, str)) or str(native_seq) != raw_seq
                        or row.get("ext_msg_id") not in (None, ext) or own is not True
                        or type(row.get("type")) is not int or row["type"] != 1):
                    continue
                body = row.get("content") if row.get("content") is not None else row.get("text")
                if (not isinstance(body, str)
                        or hashlib.sha256(body.encode("utf-8")).hexdigest() != record.get("text_sha256")):
                    continue
                stamp = row.get("time") if row.get("time") is not None else row.get("createTime")
                if isinstance(stamp, bool) or stamp is None:
                    continue
                if isinstance(stamp, str):
                    seconds = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                elif isinstance(stamp, (int, float)):
                    seconds = float(stamp) / (1000 if stamp > 1e11 else 1)
                else:
                    continue
                if (not math.isfinite(seconds) or seconds <= 0 or seconds < started_ms // 1000
                        or seconds > self._poller.clock() / 1000 + 1):
                    continue
                hits.append(row)
            if len(hits) != 1 or not self._readback_record_current(acct, trace, record):
                return False
            msg = to_message(acct.id, hits[0], self_uid=acct.self_uid, self_nick=acct.self_nick)
            msg._native_account_uid = identity[0]
            already = self._store.confirmed_outbound_for_trace(trace)
            bound = self._store.bind_wechat_readback(trace, msg, now_ms=self._poller.clock(),
                                                     readback_record=record)
            if bound is None:
                return False
            if not already:
                persisted = self._store.get_message(bound)
                msg.text = persisted["text"]
                msg.fingerprint = persisted["fingerprint"]
                payload = message_payload(msg, late_after_s=self._poller.cfg.messages.late_after_s, origin="rpa")
                payload.update(trace_id=trace, confirmed_by=persisted["confirmed_by"],
                               confirmed_ms=persisted["confirmed_ms"], text_len=persisted["text_len"])
                self._poller.events.emit("message", payload=payload, account_id=acct.id, channel="wechat",
                                         trace_id=trace, now_ms=self._poller.clock())
            return True
        except _ReadbackInvalidated as error:
            key = "wechat.readback." + trace
            if record.get("status") == "pending":
                self._store.settings_swap(key, json.dumps(record, ensure_ascii=False),
                                          {**record, "status": "invalidated", "reason": str(error)},
                                          actor="system:wechat_adapter", now_ms=self._poller.clock())
            log.info("wechat late confirmation exit=%s", error)
            return False
        except (WeChatNotReady, WeChatCallFailed, ValueError, TypeError, OverflowError, OSError, asyncio.TimeoutError):
            return False

    async def confirm_probe(self, acct: Account, cmd: Command) -> bool:
        """Only an immutable trace already bound to native evidence can confirm replay."""
        trace = cmd.trace_id or ""
        if cmd.idempotency_key:
            idem = self._store.idem_get(acct.id, cmd.idempotency_key)
            if idem is None or idem.get("status") == "IDENTITY_FENCED":
                return False
            trace = idem.get("trace_id") or ""
        row = self._store.confirmed_outbound_for_trace(trace) if trace else None
        return bool(row and row["account_id"] == acct.id and row["source"] == "chatlog"
                    and row["confirmed_by"] == "chatlog"
                    and row["session_id"] == f"{acct.id}:{self._native(acct, cmd.args['session'])}"
                    and row["text"] == cmd.args.get("text"))

    async def poll(self, acct: Account, *, only_sessions: Optional[list[str]] = None) -> None:
        await self._poller.poll(self._view(acct), only_sessions)
        pending = []
        for entry in self._store.settings_list_prefix("wechat.readback."):
            record = entry["value"]
            if (not isinstance(record, dict) or record.get("status") != "pending"
                    or record.get("account_id") != acct.id):
                continue
            trace = entry["key"][len("wechat.readback."):]
            if self._readback_record_current(acct, trace, record):
                pending.append(entry)
        if not pending:
            return
        cursor_key = "wechat.readback_cursor." + acct.id
        epoch = self._store.native_identity(acct.id).get("epoch", 0)
        cursor = self._store.settings_get(cursor_key)
        last_key, end_key = "", pending[-1]["key"]
        if (isinstance(cursor, dict) and cursor.get("self_uid") == acct.self_uid
                and cursor.get("identity_epoch") == epoch
                and isinstance(cursor.get("last_key"), str) and isinstance(cursor.get("end_key"), str)):
            last_key, end_key = cursor["last_key"], cursor["end_key"]
        # A fixed round boundary prevents new arrivals from postponing the wrap forever.
        remaining = [entry for entry in pending if last_key < entry["key"] <= end_key]
        if not remaining:
            remaining = pending
            end_key = pending[-1]["key"]
        for entry in remaining[:4]:
            # Persist scheduling before I/O so a restart resumes after this attempt.
            self._store.settings_set(cursor_key, {"self_uid": acct.self_uid, "identity_epoch": epoch,
                                                  "last_key": entry["key"], "end_key": end_key},
                                     actor="system:wechat_adapter", now_ms=self._poller.clock())
            await self._ingest_readback(acct, entry["key"][len("wechat.readback."):], entry["value"])



def _readback_seconds(row: dict[str, Any]) -> Optional[float]:
    value = row.get("time") if row.get("time") is not None else row.get("createTime")
    try:
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        elif type(value) in (int, float):
            stamp = float(value) / (1000 if value > 1e11 else 1)
        else:
            return None
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def _valid_confirmation_proof(proof: Any, *, native: str, self_uid: str, text_sha256: str,
                              request_key: str, started_ms: int, now_ms: int) -> bool:
    fields = {"version", "self_uid", "native_pid", "talker", "max_seq", "anchor",
              "captured_at_s", "send_started_at_s", "action_completed_at_s", "text_sha256", "idempotency_key"}
    if (not isinstance(proof, dict) or set(proof) != fields or type(proof["version"]) is not int
            or proof["version"] != 1 or proof["self_uid"] != self_uid or proof["talker"] != native
            or proof["text_sha256"] != text_sha256 or not request_key or proof["idempotency_key"] != request_key
            or type(proof["native_pid"]) is not int or proof["native_pid"] <= 0
            or type(proof["max_seq"]) is not int or proof["max_seq"] < 0
            or type(started_ms) is not int or type(now_ms) is not int or started_ms <= 0):
        return False
    captured, started, completed = (proof[key] for key in
        ("captured_at_s", "send_started_at_s", "action_completed_at_s"))
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in (captured, started, completed))
            or not started_ms / 1000 - 1 <= captured <= started <= completed <= now_ms / 1000 + 1
            or now_ms / 1000 - captured > 12 * 3600):
        return False
    anchor = proof["anchor"]
    if anchor is None:
        return proof["max_seq"] == 0
    return (isinstance(anchor, dict) and set(anchor) == {"seq", "time_s", "text_sha256"}
            and type(anchor["seq"]) is int and anchor["seq"] == proof["max_seq"]
            and type(anchor["time_s"]) in (int, float) and math.isfinite(anchor["time_s"])
            and 0 < anchor["time_s"] <= captured + 1
            and isinstance(anchor["text_sha256"], str) and len(anchor["text_sha256"]) == 64
            and all(char in "0123456789abcdef" for char in anchor["text_sha256"]))

def _png_size(png: bytes) -> Optional[tuple[int, int]]:
    """PNG 的 IHDR 头给 ``width``/``height``(``result_schema`` 的两个整数键);不是合法 PNG 头就不给这两键。"""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _not_ready(message: str, reason: str) -> CommandResult:
    return CommandResult(ok=False, code="NOT_READY", trace_id="", source="chatlog",
                         error=CommandError(message, reason=reason, retryable=True))


def _with_trace(res: CommandResult, trace: str) -> CommandResult:
    res.trace_id = trace
    return res
