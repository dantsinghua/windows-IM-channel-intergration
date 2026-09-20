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
- ``send``:走 #38(WinAgent 内 pyweixin 写 + 临时加速 chatlog 读回),回 ``DELIVERED`` 时把读回行按
  06 §2.12 构造成 ``dir=out, self=true, source='chatlog'`` 的行喂给 ``store.ingest`` —— 合并进 ``bus`` 先落的
  ``SENDING`` 行(``confirmed_by='chatlog'``),``bus`` 在队列外等那一行翻 ``DELIVERED``;
  ``[bus] confirm_timeout_wechat_ms=10000`` 内没等到 ⇒ ``SEND_FAILED``(C.4.2 微信口径)。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ...models import Command, CommandError, CommandResult
from ...store import Store
from ...text import norm
from ..base import Account
from .client import WeChatCallFailed, WeChatNotReady, WeChatWinAgent
from .normalize import to_message
from .poll import WechatAccountView, WechatPoller

log = logging.getLogger("qtrade.adapters.wechat")

KEY_FAIL_MESSAGE = "微信解密不可用,无法确认送达,已拒绝发送"      # 05 §2.4.7 逐字
SCREEN_LOCKED_MESSAGE = "Windows 已锁屏,发送不可用"                # 05 §2.5.4 state_reason 逐字
PROBE_LOOKBACK_SEQ = 50            # confirm_probe 回看的 seq 余量(02 §2.2.3:微信查 chatlog 同分钟同文本,P-14)


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

    async def stop(self, acct: Account, *, graceful: bool) -> None:
        """停止对该账号的 ``wechat/read`` 拉取(05 §2.4.5 第 2 步);**登出微信是 #34**,由槽位切换/`#16 logout` 发起,
        不在这里顺手做——``stop`` 也会被健康循环调用,顺手登出会误踢在用的号。"""
        self._poller.state.pop(acct.id, None)

    async def get_state(self, acct: Account) -> str:
        """05 §2.5.4 微信三行的落地(检测手段 → 状态),返回 00 §8.1 的 ``state``;``state_code`` 由调用方按 ``last_state_code`` 取。"""
        state, code, _reason = await self.probe_state(acct)
        self.last_state_code = code
        return state

    async def probe_state(self, acct: Account) -> tuple[str, Optional[str], str]:
        """返回 ``(state, state_code, state_reason)``。判定顺序(实现口径,已登记 handoff「建议裁决」):
        会话代理/模块不可用 → 微信未登录 → **取钥失败优先于锁屏**(前者读写都拒、比后者严格)→ 锁屏 → running。"""
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
        if not wx.get("running") or not wx.get("logged_in"):
            # 主窗口消失 / Weixin.exe 退出 ⇒ login_required(LOGGED_OUT);槽位 holder 保持为它(它仍是「该在线的那个」)
            return "login_required", "LOGGED_OUT", "微信已退出登录,请点「登录」重新扫码"
        if not chatlog.get("running") or not chatlog.get("key_ok") or self._poller.degraded_key_fail(acct.id):
            return "degraded", "KEY_FAIL", KEY_FAIL_MESSAGE
        if st.get("screen_locked"):
            return "degraded", "SCREEN_LOCKED", SCREEN_LOCKED_MESSAGE
        return "running", None, ""

    last_state_code: Optional[str] = None

    # ------------------------------------------------------------------ 写闸(05 §2.4.7 / §2.5.4 锁屏行)
    def write_guard(self, acct: Account) -> Optional[CommandResult]:
        """写类指令的前置:``degraded(KEY_FAIL)`` / ``degraded(SCREEN_LOCKED)`` 一律 ``NOT_READY``;可写回 ``None``。"""
        code = acct.state_code
        if acct.state == "degraded" and code == "KEY_FAIL":
            return _not_ready(KEY_FAIL_MESSAGE, "key_fail")
        if acct.state == "degraded" and code == "SCREEN_LOCKED":
            return _not_ready(SCREEN_LOCKED_MESSAGE, "screen_locked")
        return None

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
        """#38 写 + WinAgent 侧读回;``DELIVERED`` 时把读回行喂 ``store.ingest`` 合并进 ``SENDING`` 行(06 §2.12)。"""
        trace = cmd.trace_id or ""
        blocked = self.write_guard(acct)
        if blocked is not None:
            return _with_trace(blocked, trace)
        native = self._native(acct, cmd.args["session"])
        self._poller.note_send(acct.id)                    # 05 §2.4.4 ⑦:发送确认期把拉取周期加密到 1 s
        try:
            res = await self._client.send(session_name=native, text=cmd.args.get("text"),
                                          idempotency_key=cmd.idempotency_key or trace,
                                          confirm_timeout_ms=self._poller.cfg.bus.confirm_timeout_wechat_ms)
        except WeChatNotReady as e:
            return _with_trace(_not_ready(f"微信发送不可用({e.reason})", e.reason), trace)
        except WeChatCallFailed as e:
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=trace, source="chatlog",
                                 error=CommandError(f"WinAgent 发送失败 status={e.status}", retryable=True))
        if res.get("code") == "DELIVERED" and res.get("ext_msg_id"):
            self._ingest_readback(acct, native, str(res["ext_msg_id"]), cmd.args.get("text"), res.get("confirm_ms"))
            return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog",
                                 data={"ext_msg_id": res["ext_msg_id"], "confirm_ms": res.get("confirm_ms")})
        if res.get("code") == "SEND_FAILED":
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=trace, source="chatlog",
                                 error=CommandError("微信 10 秒内未在聊天记录读到(C.4.2)", retryable=True))
        # WinAgent 只回了「点完了」:读回确认交给 poll 的 ingest 合并,bus 在队列外等那一行翻 DELIVERED
        return CommandResult(ok=True, code="OK", trace_id=trace, source="chatlog", data={"native_id": native})

    def _ingest_readback(self, acct: Account, native: str, ext: str, text: Optional[str], confirm_ms: Optional[Any]) -> None:
        """#38 已经替我们读回了那一行:按 06 §2.12 构造 ``dir=out, self=true, source='chatlog'`` 交给 ``ingest`` 合并。
        ``ext_msg_id`` 形态仍是 ``{talker}:{seq}``(06 §2.9.2),故下一轮 ``poll`` 再拉到同一行会按唯一键幂等跳过。"""
        seq = ext.rsplit(":", 1)[-1]
        row = {"talker": native, "seq": int(seq) if seq.isdigit() else 0, "content": text, "isSelf": True,
               "type": 1, "time": self._poller.clock()}
        try:
            self._store.ingest(to_message(acct.id, row, self_uid=acct.self_uid, self_nick=acct.self_nick), now_ms=self._poller.clock())
        except Exception as e:                              # 合并失败不该把发送判成失败(poll 下一轮还会再合并一次)
            log.warning("微信读回行合并失败 account=%s ext=%s: %s", acct.id, ext, e)

    async def confirm_probe(self, acct: Account, cmd: Command) -> bool:
        """幂等 ``SENDING`` 态复核(02 §2.2.3:微信查 chatlog 同分钟同文本,P-14)。先查本库(可能已被 poll 合并),
        再回看 chatlog 一小段 seq;两侧比较一律过 ``norm()``(06 §2.12)。"""
        native = self._native(acct, cmd.args["session"])
        session_id = f"{acct.id}:{native}"
        text = cmd.args.get("text") or ""
        window = self._poller.cfg.bus.out_merge_window_s * 1000
        ts = cmd.submitted_at_ms or self._poller.clock()
        if self._store.find_out_by_text(acct.id, session_id, text, window, ts) is not None:
            return True
        want = norm(text)
        if not want:
            return False
        since = max(0, self._poller.cursor_seq(acct.id, native) - PROBE_LOOKBACK_SEQ)
        try:
            rows = await self._client.read(talker=native, since_seq=since, limit=PROBE_LOOKBACK_SEQ)
        except (WeChatNotReady, WeChatCallFailed):
            return False                                    # 问不到就当没发出去:宁可重发也不误判已发(微信侧本就有幂等键)
        for r in rows:
            if not bool(r.get("isSelf") or r.get("is_self")):
                continue
            m = to_message(acct.id, r, self_uid=acct.self_uid, self_nick=acct.self_nick)
            if norm(m.text) == want and abs(m.ts_ms - ts) <= window:
                return True
        return False

    async def poll(self, acct: Account, *, only_sessions: Optional[list[str]] = None) -> None:
        await self._poller.poll(self._view(acct), only_sessions)


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
