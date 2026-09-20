"""bus(指令总线,02 §2.2.2)—— 本期骨架:登录门 → 参数校验(R6-48)→ 幂等三态 → 每账号串行队列 → 执行 → 队列外等确认(R6-38)→ 审计。

未落地(留给后续里程碑):安全闸词表(§6 双闸门)、限流的随机抖动细节、broadcast、工作流触发、邮件入口的键改写。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..adapters.base import Account, Adapter
from ..config import AgentConfig
from ..events import Events
from ..ids import ulid
from ..models import Command, CommandError, CommandResult, RESULT_CODES, Message, Session
from ..store import Store
from .validate import validate_args

log = logging.getLogger("qtrade.bus")

LOGIN_PHASE_STATES = frozenset({"login_required", "logging_in"})
# 02 §2.2.2 登录门:登录阶段允许的 op(操作对象是屏幕不是业务)
ALLOWED_IN_LOGIN_PHASE = frozenset({"screenshot", "get_state", "stream_touch", "stream_key", "stream_text", "stream_scroll", "login", "prompt", "login_cancel"})


def canonical_args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """P-11:args.text 入库前替换为 {text_sha8, text_len};secret 擦成 ***。"""
    out = dict(args)
    if isinstance(out.get("text"), str):
        t = out.pop("text")
        out["text_sha8"] = hashlib.sha256(t.encode("utf-8")).hexdigest()[:8]
        out["text_len"] = len(t)
    if "secret" in out:
        out["secret"] = "***"
    return out


def _err_result(code: str, trace_id: str, error: CommandError, **kw) -> CommandResult:
    retryable, needs_human = RESULT_CODES.get(code, (False, False))
    error.retryable = error.retryable or retryable
    error.needs_human = error.needs_human or needs_human
    return CommandResult(ok=False, code=code, trace_id=trace_id, error=error, **kw)


@dataclass
class _Job:
    cmd: Command
    acct: Account
    done: "asyncio.Future[CommandResult]"
    msg_id: Optional[str] = None      # send_* 的 SENDING 行


class Bus:
    def __init__(self, *, store: Store, events: Events, adapters: dict[str, Adapter], cfg: AgentConfig, clock=None):
        self.store = store
        self.events = events
        self.adapters = adapters
        self.cfg = cfg
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._queues: dict[str, asyncio.Queue] = {}
        self._consumers: dict[str, asyncio.Task] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future] = {}
        self._last_send_ms: dict[str, int] = {}
        self.random = random.Random()

    # ------------------------------------------------------------------ 队列
    def _queue(self, account_id: str) -> asyncio.Queue:
        q = self._queues.get(account_id)
        if q is None:
            q = self._queues[account_id] = asyncio.Queue(maxsize=self.cfg.bus.queue_max_per_account)
            self._consumers[account_id] = asyncio.create_task(self._consume(account_id, q), name=f"bus:{account_id}")
        return q

    async def close(self) -> None:
        for t in self._consumers.values():
            t.cancel()
        for t in self._consumers.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._consumers.clear()

    async def _consume(self, account_id: str, q: asyncio.Queue) -> None:
        while True:
            item = await q.get()
            try:
                if isinstance(item, _Job):
                    await self._execute(item)
                else:                                   # 内部指令:确认窗内的加速轮 poll(only_sessions)
                    acct, only = item
                    await self.adapters[acct.channel].poll(acct, only_sessions=only)
            except Exception as e:                      # 单条指令的异常不杀消费者
                log.exception("bus consumer error account=%s: %s", account_id, e)
                if isinstance(item, _Job) and not item.done.done():
                    item.done.set_result(_err_result("INTERNAL", item.cmd.trace_id or "", CommandError(str(e))))
            finally:
                q.task_done()

    # ------------------------------------------------------------------ 入口
    def _load_account(self, account_id: str) -> Optional[Account]:
        row = self.store.get_account(account_id)
        if row is None:
            return None
        return Account(id=row["id"], channel=row["channel"], state=row["state"], self_uid=row.get("self_uid"),
                       self_nick=row.get("self_nick"), state_code=row.get("state_code"), app_version=row.get("runtime_app_version"))

    async def submit(self, cmd: Command) -> CommandResult:
        """同步语义:等到确认结束(或超时)才返回;等待期间不占账号队列(R6-38)。"""
        cmd.trace_id = cmd.trace_id or ulid()
        cmd.submitted_at_ms = cmd.submitted_at_ms or self.clock()
        tid = cmd.trace_id
        acct = self._load_account(cmd.account_id)
        if acct is None:
            return _err_result("TARGET_NOT_FOUND", tid, CommandError(f"账号不存在:{cmd.account_id}"))
        adapter = self.adapters.get(acct.channel)
        if adapter is None:
            return _err_result("NOT_READY", tid, CommandError(f"通道 {acct.channel} 适配器未加载"))
        if cmd.op not in adapter.capabilities:
            return _err_result("UNSUPPORTED", tid, CommandError(f"{acct.channel} 不支持 {cmd.op}"))

        # 登录门(D-2):登录阶段只放屏幕类;IM 写类直接 LOGIN_REQUIRED,不排队、不写 idempotency、不写 SENDING 行;
        # B-30(R6-51 收口):留痕 —— commands 行 status='failed'、started_ms IS NULL(没进队列)+ command_results
        if acct.state in LOGIN_PHASE_STATES and cmd.op not in ALLOWED_IN_LOGIN_PHASE:
            res = _err_result("LOGIN_REQUIRED", tid, CommandError("账号处于登录阶段,需人工完成登录", reason=acct.state_code or acct.state),
                              state_before=acct.state, state_after=acct.state)
            now = self.clock()
            self.store.insert_command(trace_id=tid, account_id=acct.id, op=cmd.op, args_json=json.dumps(redact_args(cmd.args), ensure_ascii=False),
                                      idempotency_key=cmd.idempotency_key, confirm=cmd.confirm, timeout_ms=cmd.timeout_ms,
                                      transport=cmd.origin.transport, actor=cmd.origin.actor, ip=cmd.origin.ip, now_ms=now)
            self.store.finish_command(trace_id=tid, ok=False, code="LOGIN_REQUIRED", data={}, cost_ms=0, source=None,
                                      error_message=res.error.message, retryable=False, needs_human=True, confirmed_by=None, confirm_ms=None, now_ms=now)
            self.events.emit("command_done", payload={"trace_id": tid, "code": "LOGIN_REQUIRED", "cost_ms": 0, "ok": False},
                             account_id=acct.id, channel=acct.channel, trace_id=tid, now_ms=now)
            return res

        # 参数校验(02 §3.10 + R6-48):失败不进 commands、不占幂等键
        err = validate_args(cmd.op, cmd.args)
        if err is not None:
            return _err_result("INVALID_ARGS", tid, err)

        # 幂等三态(C.4.3)
        key = cmd.idempotency_key
        now = self.clock()
        if key:
            args_hash = canonical_args_hash(cmd.args)
            row = self.store.idem_get(acct.id, key)
            if row is not None:
                if row["args_hash"] != args_hash:
                    return _err_result("INVALID_ARGS", tid, CommandError("幂等键已被使用且参数不同", reason="idempotency_args_mismatch"))
                if row["status"] == "DONE":
                    prev = self.store.get_command_result(row["trace_id"]) or {}
                    return CommandResult(ok=True, code="IDEMPOTENT_REPLAY", trace_id=row["trace_id"], data=prev.get("data", {}),
                                         cost_ms=0, source=prev.get("source"))
                if row["status"] == "SENDING":
                    fut = self._inflight.get((acct.id, key))
                    if fut is not None:
                        try:
                            res = await asyncio.wait_for(asyncio.shield(fut), timeout=cmd.timeout_ms / 1000)
                        except asyncio.TimeoutError:
                            return _err_result("TIMEOUT", tid, CommandError("同键指令仍在执行", reason="inflight"))
                        return CommandResult(ok=res.ok, code="IDEMPOTENT_REPLAY", trace_id=res.trace_id, data=res.data, source=res.source)
                    row = dict(row, status="ABANDONED")
                if row["status"] == "ABANDONED":
                    if await adapter.confirm_probe(acct, cmd):        # 上次其实已发(只查本库:出向行是否已被 ingest 合并成 DELIVERED)
                        self.store.idem_finish(account_id=acct.id, key=key, status="DONE", result_code="DELIVERED", now_ms=now)
                        # B-08:command_results.confirmed_by 由 probe 回填——回填到**首次**那条 trace(从 messages.confirmed_by 派生,code 不改写)
                        m = self.store.backfill_confirm_from_message(row["trace_id"])
                        data = {"message_id": m["id"], "ext_msg_id": m["ext_msg_id"], "confirmed_by": m["confirmed_by"]} if m else {}
                        return CommandResult(ok=True, code="IDEMPOTENT_REPLAY", trace_id=row["trace_id"], data=data, source=m.get("source") if m else None)
                    self.store.idem_delete(acct.id, key)
            if not self.store.idem_claim(account_id=acct.id, key=key, op=cmd.op, args_hash=args_hash, trace_id=tid, now_ms=now,
                                         ttl_days=self.cfg.bus.idempotency_ttl_days):
                return _err_result("INVALID_ARGS", tid, CommandError("幂等键并发冲突", reason="idempotency_race"))

        self.store.insert_command(trace_id=tid, account_id=acct.id, op=cmd.op, args_json=json.dumps(redact_args(cmd.args), ensure_ascii=False),
                                  idempotency_key=key, confirm=cmd.confirm, timeout_ms=cmd.timeout_ms,
                                  transport=cmd.origin.transport, actor=cmd.origin.actor, ip=cmd.origin.ip, now_ms=now)
        loop = asyncio.get_running_loop()
        job = _Job(cmd=cmd, acct=acct, done=loop.create_future())
        if key:
            self._inflight[(acct.id, key)] = job.done
        q = self._queue(acct.id)
        if q.full():
            self._finalize(job, _err_result("RATE_LIMITED", tid, CommandError("账号队列已满", reason="queue_full")))
            return job.done.result()
        q.put_nowait(job)
        try:
            return await asyncio.wait_for(asyncio.shield(job.done), timeout=cmd.timeout_ms / 1000 + self.cfg.confirm_timeout_ms(acct.channel) / 1000)
        except asyncio.TimeoutError:
            res = _err_result("TIMEOUT", tid, CommandError("超过 timeout_ms"))
            self._finalize(job, res)
            return res

    # ------------------------------------------------------------------ 执行(在账号队列里)
    async def _execute(self, job: _Job) -> None:
        cmd, acct, adapter = job.cmd, job.acct, self.adapters[job.acct.channel]
        started = self.clock()
        self.store.command_started(cmd.trace_id, started)
        if cmd.op.startswith("send_"):
            await self._rate_limit(acct.id)
            session = cmd.args["session"]
            native = session.split(":", 1)[1] if session.startswith(acct.id + ":") else session
            kind = "group" if native.startswith("g_") or native.endswith("@chatroom") else "private"
            out = Message(account_id=acct.id, channel=acct.channel, session=Session(acct.id, native, kind, name=native), dir="out",
                          type="text", text=cmd.args["text"], ts_ms=started, source="ui" if acct.channel == "qidian" else ("onebot" if acct.channel == "qq" else "chatlog"),
                          sender_id=acct.self_uid, sender_name=acct.self_nick, self=True, state="SENDING",
                          trace_id=cmd.trace_id, idempotency_key=cmd.idempotency_key)
            r = self.store.ingest(out, now_ms=started)          # 出向先落库(经 store.ingest,先 upsert sessions)
            job.msg_id = r.id
            res = await adapter.send(acct, cmd)                 # 点完即返回、让出队列
            if not res.ok:
                if job.msg_id:
                    self.store.mark_out_state(job.msg_id, "FAILED")
                self._finalize(job, CommandResult(ok=False, code="SEND_FAILED", trace_id=cmd.trace_id, source=res.source,
                                                  error=CommandError("发送动作失败", retryable=True), cost_ms=self.clock() - started))
                return
            self._last_send_ms[acct.id] = self.clock()
            # 队列外等确认:不 await,让消费者立刻取下一条
            asyncio.create_task(self._confirm(job, native, started, res.source or "qidian_db"), name=f"confirm:{cmd.trace_id}")
            return
        res = await adapter.execute(acct, cmd)
        res.cost_ms = self.clock() - started
        self._finalize(job, res)

    async def _rate_limit(self, account_id: str) -> None:
        last = self._last_send_ms.get(account_id)
        if last is None:
            return
        gap = self.cfg.bus.send_min_interval_ms + self.random.randint(0, self.cfg.bus.send_rand_extra_ms)
        wait = last + gap - self.clock()
        if wait > 0:
            await asyncio.sleep(wait / 1000)

    async def _confirm(self, job: _Job, native_id: str, started: int, source: str) -> None:
        cmd, acct = job.cmd, job.acct
        timeout = self.cfg.confirm_timeout_ms(acct.channel)
        interval = (self.cfg.qidian.confirm_poll_interval_ms if acct.channel == "qidian" else 1000) / 1000
        deadline = started + timeout
        while True:
            st = self.store.message_state(job.msg_id) if job.msg_id else None
            if st and st["state"] == "DELIVERED":
                self._finalize(job, CommandResult(ok=True, code="DELIVERED", trace_id=cmd.trace_id, source=source,
                                                  data={"message_id": job.msg_id, "ext_msg_id": st["ext_msg_id"], "confirmed_by": st["confirmed_by"]},
                                                  cost_ms=self.clock() - started), confirmed_by=st["confirmed_by"], confirm_ms=st["confirmed_ms"])
                return
            if self.clock() >= deadline:
                break
            if acct.channel == "qidian":
                self._queue(acct.id).put_nowait((acct, [native_id]))    # 窗内每 confirm_poll_interval_ms 投一轮只查目标会话表的 poll
            await asyncio.sleep(interval)
        if acct.channel == "wechat":
            if job.msg_id:
                self.store.mark_out_state(job.msg_id, "FAILED")
            self._finalize(job, CommandResult(ok=False, code="SEND_FAILED", trace_id=cmd.trace_id, source=source,
                                              error=CommandError("10 秒内未在聊天记录读到", retryable=True), cost_ms=self.clock() - started))
        else:
            if job.msg_id:
                self.store.mark_out_state(job.msg_id, "UNCONFIRMED")
            self._finalize(job, CommandResult(ok=False, code="SEND_CALLED_BUT_UNCONFIRMED", trace_id=cmd.trace_id, source=source,
                                              data={"message_id": job.msg_id}, error=CommandError("已发出,期限内未读回确认(待核)"),
                                              cost_ms=self.clock() - started))

    # ------------------------------------------------------------------ 收尾:结果落库 + 幂等终态 + 事件
    def _finalize(self, job: _Job, res: CommandResult, *, confirmed_by: Optional[str] = None, confirm_ms: Optional[int] = None) -> None:
        if job.done.done():
            return
        cmd, acct = job.cmd, job.acct
        now = self.clock()
        err = res.error
        self.store.finish_command(trace_id=cmd.trace_id, ok=res.ok, code=res.code, data=res.data, cost_ms=res.cost_ms, source=res.source,
                                  error_message=err.message if err else None, retryable=err.retryable if err else None,
                                  needs_human=err.needs_human if err else None, confirmed_by=confirmed_by, confirm_ms=confirm_ms, now_ms=now)
        if cmd.idempotency_key:
            self._inflight.pop((acct.id, cmd.idempotency_key), None)
            if err is not None and err.needs_human:
                self.store.idem_delete(acct.id, cmd.idempotency_key)   # needs_human 不落 DONE(06 §9)
            elif res.code == "SEND_CALLED_BUT_UNCONFIRMED":
                pass                                                    # B-08:留 SENDING,下次同 key 先 confirm_probe
            else:
                self.store.idem_finish(account_id=acct.id, key=cmd.idempotency_key, status="DONE", result_code=res.code, now_ms=now)
        self.events.emit("command_done", payload={"trace_id": cmd.trace_id, "code": res.code, "cost_ms": res.cost_ms, "ok": res.ok},
                         account_id=acct.id, channel=acct.channel, trace_id=cmd.trace_id, now_ms=now)
        job.done.set_result(res)
