"""采集端拉取询价下行指令。

只处理 ``wb/{workbench_id}/in/xunjia/{cmd_id}.bin``。用询价密钥解开。
``op=send_text`` 且 ``channel=qidian`` 时，把 ``text`` 原样交给现有企点发送，
发给 ``session.native_id``。发送成功后写入采集库，方向为发出（``dir=out``），
会话对方就是这个 ``native_id``。回执写到 ``wb/{workbench_id}/ack/{cmd_id}.bin``，
成功 ``ok=true``，失败带 ``reason``，回执里不放正文。

不连真实 OSS、不向真实企点发消息的测试，传入假对象箱和假发送函数即可。
2026-10-12 15:10 已记下的 ``cmd_31e9de7f3eba`` 见到也不发送、不补回执。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from ..adapters.base import Account
from ..models import Command, CommandOrigin, Message, Session
from collections import OrderedDict
from .bind import Dedup
from .crypto import open_sealed, seal
from .keys import ack_object_name, in_object_name
from .object_io import ObjectIO
from .schema import validate_command, validate_receipt

log = logging.getLogger("qtrade.ossbox.inbox")

# 15:10 那条正文已记下，不补发。对象名里就能认出，不必解开。
# 这两条在采集端还没轮询时就已经被页面判失败。启动后不再补发。
HELD_CMD_IDS = frozenset({"cmd_31e9de7f3eba", "cmd_bb1daaad1a38"})

SendFn = Callable[[Account, str, str], bool | Awaitable[bool]]


class ObjectBox:
    """对象箱。测试用内存实现，运行时换 OSS。"""

    def list_prefix(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def get(self, name: str) -> bytes | None:
        raise NotImplementedError

    def put(self, name: str, body: bytes) -> None:
        raise NotImplementedError


def _plaintext(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _receipt(cmd_id: str, *, ok: bool, code: str, reason: str = "") -> dict[str, Any]:
    """回执只有结果。失败才带 reason。不放 text。"""
    body: dict[str, Any] = {"cmd_id": cmd_id, "ok": bool(ok), "code": code}
    if not ok:
        body["reason"] = reason or "发送失败"
    checked = validate_receipt(body)
    if "text" in checked:
        raise ValueError("回执不得含正文")
    return checked


class XunjiaInbox:
    """一轮：列出询价指令、解开、企点发送、落库、写回执。"""

    def __init__(
        self,
        box: Any,
        key: bytes,
        workbench_id: str,
        send: SendFn,
        store: Any,
        *,
        clock: Callable[[], int] | None = None,
        confirm_wait_s: float = 0.0,
        bus: Any = None,
    ) -> None:
        self.box = box
        self.key = bytes(key)
        self.workbench_id = workbench_id
        self.send = send
        self.bus = bus
        self.store = store
        self.clock = clock or (lambda: 0)
        self.confirm_wait_s = confirm_wait_s
        self._dedup = Dedup()
        self._outcome: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._io = ObjectIO()

    @classmethod
    def from_env(cls, store: Any, send: SendFn, *, clock: Callable[[], int] | None = None,
                 box: Any = None, workbench_id: str | None = None, bus: Any = None) -> "XunjiaInbox | None":
        """环境变量齐了才装配。缺任何一项就返回 None，不拉网、不打密钥。"""
        if workbench_id is None:
            workbench_id = os.environ.get("OSS_WORKBENCH_ID", "").strip()
        seal_hex = (os.environ.get("OSS_SEAL_KEY_XUNJIA") or os.environ.get("OSSBOX_KEY_XUNJIA") or "").strip()
        missing = [
            name for name, value in (
                ("OSS_WORKBENCH_ID", workbench_id),
                ("OSS_SEAL_KEY_XUNJIA", seal_hex),
            ) if not value
        ]
        if missing:
            log.warning("询价指令轮询未装配，缺少 %s", ",".join(missing))
            return None
        try:
            key = bytes.fromhex(seal_hex)
        except ValueError:
            log.warning("询价密钥不是十六进制，不拉指令")
            return None
        if len(key) not in (16, 24, 32):
            log.warning("询价密钥长度无效，不拉指令")
            return None
        from .oss_http import OssV1Box

        try:
            box = box if box is not None else OssV1Box.from_env()
        except ValueError:
            log.warning("询价对象存储配置无效，未装配轮询")
            return None
        if box is None:
            log.warning("询价对象存储配置不完整，未装配轮询")
            return None

        log.info("询价指令轮询已装配 workbench=%s", workbench_id)
        wait_raw = os.environ.get("XUNJIA_LAND_WAIT_S", "20").strip()
        try:
            confirm_wait_s = max(0.0, float(wait_raw))
        except ValueError:
            confirm_wait_s = 20.0
        return cls(
            box,
            key,
            workbench_id,
            send,
            store,
            clock=clock,
            confirm_wait_s=confirm_wait_s,
            bus=bus,
        )

    async def poll(self) -> None:
        self._io.check_idle()
        prefix = f"wb/{self.workbench_id}/in/xunjia/"
        names = [str(item) for item in await self._io.run(self.box.list_prefix, prefix)]
        held: list[str] = []
        acked: list[str] = []
        pending: list[str] = []
        unnamed = 0
        planned: list[tuple[str, str, str]] = []
        for name in sorted(names):
            cmd_id = _cmd_id(name, self.workbench_id)
            if cmd_id is None:
                unnamed += 1
                continue
            if cmd_id in HELD_CMD_IDS:
                held.append(cmd_id)
                log.info("跳过不补发的询价指令 cmd_id=%s object=%s", cmd_id, name)
                continue
            ack_name = ack_object_name(self.workbench_id, cmd_id)
            if await self._io.run(self.box.get, ack_name) is not None:
                acked.append(cmd_id)
                continue
            pending.append(cmd_id)
            planned.append((name, cmd_id, ack_name))
        log.info(
            "询价信箱文件 files=%s held=%s acked=%s pending=%s unnamed=%s",
            len(names), held, acked, pending, unnamed,
        )
        for name, cmd_id, ack_name in planned:
            await self._one(name, cmd_id, ack_name)

    def _skip(self, cmd_id: str, name: str, branch: str) -> None:
        """断点：这个 OSS 文件没有拿去发企点。不打正文。"""
        log.info("询价文件未触发发送 cmd_id=%s object=%s branch=%s", cmd_id, name, branch)

    async def _one(self, name: str, cmd_id: str, ack_name: str) -> None:
        raw = await self._io.run(self.box.get, name)
        if not raw:
            self._skip(cmd_id, name, "empty")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="指令对象为空"))
            return
        try:
            opened = open_sealed(self.key, raw)
            command = validate_command(json.loads(opened.decode("utf-8")))
        except Exception as exc:
            log.info(
                "询价文件未触发发送 cmd_id=%s object=%s branch=unseal exc=%s nbytes=%s",
                cmd_id, name, type(exc).__name__, len(raw),
            )
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="指令无法解开"))
            return
        idem = str(command.get("idempotency_key") or "")
        if str(command.get("workbench_id") or "") != self.workbench_id:
            self._skip(cmd_id, name, "workbench")
            self._remember(idem, ok=False, code="failed", reason="工作台不一致")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="工作台不一致"))
            return
        if command.get("op") != "send_text" or command.get("channel") != "qidian":
            self._skip(cmd_id, name, "not_qidian_text")
            self._remember(idem, ok=False, code="failed", reason="这次只发企点文本")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="这次只发企点文本"))
            return
        if self.bus is not None:
            await self._submit_via_bus(command, cmd_id, ack_name)
            return
        prior = self._outcome.get(idem)
        if idem and prior is not None:
            self._skip(cmd_id, name, "replay")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=prior["ok"], code=prior["code"], reason=prior.get("reason") or ""))
            return
        session = command["session"]
        native_id = str(session["native_id"])
        text = command["text"] if isinstance(command["text"], str) else str(command["text"])
        account = self._account(str(command["account_uid"]))
        if account is None:
            self._skip(cmd_id, name, "no_account")
            self._remember(idem, ok=False, code="failed", reason="本机没有这个企点号")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="本机没有这个企点号"))
            return
        if account.state not in ("running", "degraded"):
            self._skip(cmd_id, name, "offline")
            self._remember(idem, ok=False, code="failed", reason="账号不在线")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="账号不在线"))
            return
        if idem and not self._dedup.seen_command(idem):
            self._skip(cmd_id, name, "dedup")
            prior = self._outcome.get(idem) or {"ok": True, "code": "delivered", "reason": ""}
            await self._put_ack(ack_name, _receipt(cmd_id, ok=prior["ok"], code=prior["code"], reason=prior.get("reason") or ""))
            return
        if idem and self._already_sent(account.id, idem):
            self._skip(cmd_id, name, "already_sent")
            self._remember(idem, ok=True, code="delivered")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=True, code="delivered"))
            return
        log.info(
            "询价文件触发企点发送 cmd_id=%s object=%s account=%s native_id=%s text_len=%s",
            cmd_id, name, account.id, native_id, len(text),
        )
        try:
            sent = await _call_send(self.send, account, native_id, text)
        except Exception as exc:
            log.warning(
                "企点发送未执行 cmd_id=%s object=%s account=%s branch=send_exception exc=%s",
                cmd_id, name, account.id, type(exc).__name__,
            )
            self._remember(idem, ok=False, code="failed", reason="企点发送未执行")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="企点发送未执行"))
            return
        log.info("询价文件发送结果 cmd_id=%s object=%s sent=%s", cmd_id, name, bool(sent))
        if not sent:
            self._remember(idem, ok=False, code="failed", reason="企点发送未执行")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="企点发送未执行"))
            return
        try:
            self._write_out(account, command, native_id, text, cmd_id, idem)
        except Exception as exc:
            log.warning(
                "采集库写入失败 cmd_id=%s object=%s account=%s branch=store exc=%s",
                cmd_id, name, account.id, type(exc).__name__,
            )
            self._remember(idem, ok=False, code="failed", reason="采集库写入失败")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="采集库写入失败"))
            return
        if not await self._wait_landed(account, native_id, text):
            log.info(
                "询价文件未在对话库确认 cmd_id=%s object=%s account=%s native_id=%s text_len=%s",
                cmd_id, name, account.id, native_id, len(text),
            )
            self._remember(idem, ok=False, code="failed", reason="对话里还没有这条发出")
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="对话里还没有这条发出"))
            return
        self._remember(idem, ok=True, code="delivered")
        await self._put_ack(ack_name, _receipt(cmd_id, ok=True, code="delivered"))

    async def _submit_via_bus(self, command, cmd_id, ack_name):
        account = self._account(str(command["account_uid"]))
        key = command.get("idempotency_key")
        if account is None:
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="Account not available"))
            return
        if not isinstance(key, str) or not key or len(key) > 128:
            await self._put_ack(ack_name, _receipt(cmd_id, ok=False, code="failed", reason="Invalid idempotency key"))
            return
        # The shared bus owns durable intent, account serialization, Gate and confirmation.
        cmd = Command(account.id, "send_text", {"session": command["session"]["native_id"], "text": command["text"]},
                      idempotency_key=key, origin=CommandOrigin(transport="http", actor="oss:" + self.workbench_id))
        result = await self.bus.submit(cmd)
        ok = bool(result.ok and result.code in ("DELIVERED", "IDEMPOTENT_REPLAY"))
        await self._put_ack(ack_name, _receipt(cmd_id, ok=ok, code="delivered" if ok else "failed", reason=result.code if not ok else ""))

    def _remember(self, idem: str, *, ok: bool, code: str, reason: str = "") -> None:
        if not idem:
            return
        self._outcome[idem] = {"ok": ok, "code": code, "reason": reason}
        self._outcome.move_to_end(idem)
        if len(self._outcome) > 4096:
            self._outcome.popitem(last=False)

    async def _put_ack(self, name: str, receipt: dict[str, Any]) -> None:
        try:
            await self._io.run(self.box.put, name, seal(self.key, _plaintext(receipt)))
            log.info("询价回执已写 object=%s cmd_id=%s ok=%s code=%s", name, receipt.get("cmd_id"), receipt.get("ok"), receipt.get("code"))
        except Exception as exc:
            log.warning("询价回执写入失败 object=%s exc=%s", name, type(exc).__name__)
            raise

    def _account(self, account_uid: str) -> Account | None:
        rows = self.store.list_accounts(channel="qidian")
        matched = [row for row in rows if str(row.get("self_uid") or "") == account_uid]
        if not matched:
            return None
        matched.sort(key=lambda row: 0 if row.get("state") == "running" else 1 if row.get("state") == "degraded" else 2)
        row = matched[0]
        return Account(
            id=str(row["id"]),
            channel="qidian",
            state=str(row.get("state") or ""),
            self_uid=row.get("self_uid"),
            self_nick=row.get("self_nick"),
        )

    def _already_sent(self, account_id: str, idem: str) -> bool:
        con = getattr(self.store, "con", None)
        if con is None:
            return False
        row = con.execute(
            "SELECT id FROM messages WHERE account_id=? AND idempotency_key=? AND dir='out' AND ext_msg_id IS NOT NULL LIMIT 1",
            (account_id, idem),
        ).fetchone()
        return row is not None

    def _landed(self, account: Account, native_id: str, text: str) -> bool:
        """对话库已经有带通道消息号的同文发出，才算对方会话里出现了。点发送键写的 SENDING 草稿不算。"""
        find = getattr(self.store, "find_out_by_text", None)
        if find is None:
            return False
        try:
            row = find(account.id, f"{account.id}:{native_id}", text, 120_000, int(self.clock()))
        except Exception:
            return False
        return row is not None

    async def _wait_landed(self, account: Account, native_id: str, text: str) -> bool:
        deadline = time.monotonic() + self.confirm_wait_s
        while True:
            if self._landed(account, native_id, text):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(0.4, deadline - time.monotonic()))

    def _write_out(self, account: Account, command: dict[str, Any], native_id: str, text: str, cmd_id: str, idem: str) -> None:
        """发送已经成功。采集库记一条发出，会话对方是 native_id。

        ``state=SENDING`` 且还没有通道消息号，是为了让随后的企点读库把同一条并回这一行，
        监控里仍然只有一条、方向是发出。
        """
        kind = command["session"]["kind"]
        now = int(self.clock())
        message = Message(
            account_id=account.id,
            channel="qidian",
            session=Session(account.id, native_id, kind, name=native_id),
            dir="out",
            type="text",
            text=text,
            ts_ms=now,
            source="ui",
            sender_id=account.self_uid,
            sender_name=account.self_nick,
            self=True,
            state="SENDING",
            trace_id=cmd_id,
            idempotency_key=idem or None,
        )
        self.store.ingest(message, now_ms=now)


def _cmd_id(name: str, workbench_id: str) -> str | None:
    prefix = f"wb/{workbench_id}/in/xunjia/"
    if not name.startswith(prefix) or not name.endswith(".bin"):
        return None
    cmd_id = name[len(prefix):-4]
    if not cmd_id or "/" in cmd_id:
        return None
    try:
        in_object_name(workbench_id, "xunjia", cmd_id)
    except ValueError:
        return None
    return cmd_id


async def _call_send(send: SendFn, account: Account, native_id: str, text: str) -> bool:
    result = send(account, native_id, text)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, bool):
        return result
    ok = getattr(result, "ok", None)
    if isinstance(ok, bool):
        return ok
    return bool(result)
