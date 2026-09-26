"""企点适配器(02 §2.2.3 qidian 条)。

本期只落地能在无 redroid 环境下编码的部分:``poll``(读库正线)与 ``send`` 的阻塞语义。
真正的 UI 执行层(xunjia-agent/relay/side_a 七件套:搜索/打开会话/ADBKeyboard 输入/点发送/发后对象校验)以 ``sender`` 回调注入,
容器里用假回调;真机接入时把 ``qidian_cold_start.sh`` 的动作包成同签名的回调即可。

🔴 发送确认的阻塞语义(R6-38):``send`` 点完发送键、做完发后对象校验**即返回并让出账号串行队列**,adapter 内不同步 scrape、不在队列里等读回;
读回 = ``poll`` 的 ingest 合并,由 bus 在队列外等(bus/bus.py)。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from ...models import Command, CommandError, CommandResult
from ..base import Account
from .poll import QidianAccountView, QidianPoller

SendFn = Callable[[Account, str, str], Awaitable[bool]]     # (acct, native_id, text) -> 点了发送键且发后对象校验通过
ScreenshotFn = Callable[[str], Awaitable[bytes]]           # (account_id) -> 当前画面 PNG;取不到回 b""
#: 容器在跑、能抓到画面的状态(登录阶段也要能看画面,00 §8.1 R-06)
SCREEN_STATES = frozenset({"running", "degraded", "login_required", "logging_in"})


class QidianAdapter:
    channel = "qidian"
    capabilities = frozenset({"read_messages", "list_sessions", "get_state", "screenshot", "send_text"})

    def __init__(self, poller: QidianPoller, *, sender: SendFn, store, screenshot_fn: Optional[ScreenshotFn] = None):
        self._poller = poller
        self._sender = sender
        self._store = store
        #: #33 截图执行体(真机由 ``main`` 装配成画面流后端的 screencap);缺省未接 ⇒ UNSUPPORTED
        self.screenshot_fn = screenshot_fn

    @staticmethod
    def _view(acct: Account) -> QidianAccountView:
        return QidianAccountView(acct.id, acct.state, acct.self_uid, acct.app_version)

    async def start(self, acct: Account) -> None:      # 建连/拉起在 runtime;本期无
        return None

    async def stop(self, acct: Account, *, graceful: bool) -> None:
        return None

    async def get_state(self, acct: Account) -> str:
        return acct.state

    async def execute(self, acct: Account, cmd: Command) -> CommandResult:
        if cmd.op == "get_state":          # 只读:00 §8.1 状态 + 05 维护的 state_code;登录阶段也放行(02 §2.2.2 登录门)
            return CommandResult(ok=True, code="OK", trace_id=cmd.trace_id or "", source="ui",
                                 data={"state": acct.state, "state_code": acct.state_code, "self_uid": acct.self_uid})
        if cmd.op == "screenshot" and self.screenshot_fn is not None and not cmd.args.get("region") \
                and (cmd.args.get("format") or "png") == "png":
            if acct.state not in SCREEN_STATES:              # 容器不在跑(stopped/created/error…)就没有画面可抓
                return CommandResult(ok=False, code="NOT_READY", trace_id=cmd.trace_id or "", source="screenshot",
                                     error=CommandError(f"账号当前 {acct.state},没有画面", reason="bad_state"))
            png = await self.screenshot_fn(acct.id)          # 整屏 PNG;region / jpeg 本期不做
            if not png:
                return CommandResult(ok=False, code="NOT_READY", trace_id=cmd.trace_id or "", source="screenshot")
            return CommandResult(ok=True, code="OK", trace_id=cmd.trace_id or "", source="screenshot", data={"png": png})
        # read_messages / list_sessions 的执行层(控件树/读库查询)本期未接
        return CommandResult(ok=False, code="UNSUPPORTED", trace_id=cmd.trace_id or "", source="ui")

    async def send(self, acct: Account, cmd: Command) -> CommandResult:
        native_id = cmd.args["session"].split(":", 1)[1] if cmd.args["session"].startswith(acct.id + ":") else cmd.args["session"]
        ok = await self._sender(acct, native_id, cmd.args["text"])
        if not ok:
            return CommandResult(ok=False, code="SEND_FAILED", trace_id=cmd.trace_id or "", source="ui")
        # 点完即返回:确认由 bus 在队列外等 poll 的 ingest 合并
        return CommandResult(ok=True, code="OK", trace_id=cmd.trace_id or "", source="qidian_db", data={"native_id": native_id})

    async def confirm_probe(self, acct: Account, cmd: Command) -> bool:
        """幂等 SENDING 态复核(B-08):按同会话同 norm(text) 在窗内查已确认的出向行(被动 ingest 合并的结果),不主动 scrape。"""
        session = cmd.args["session"]
        session_id = session if session.startswith(acct.id + ":") else f"{acct.id}:{session}"
        row = await asyncio.to_thread(self._store.find_out_by_text, acct.id, session_id, cmd.args["text"],
                                      self._poller.cfg.bus.out_merge_window_s * 1000, cmd.submitted_at_ms or self._poller.clock())
        return row is not None

    async def poll(self, acct: Account, *, only_sessions: Optional[list[str]] = None) -> None:
        await asyncio.to_thread(self._poller.poll_maindb, self._view(acct), only_sessions)

    async def check_group_gaps(self, acct: Account) -> None:
        await asyncio.to_thread(self._poller.check_group_gaps, self._view(acct))
