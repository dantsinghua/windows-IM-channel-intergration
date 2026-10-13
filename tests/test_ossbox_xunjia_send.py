"""采集端询价下行：假对象箱、假企点发送。不连 OSS，不向真实企点发消息。"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from qtrade_agent.models import Message, Session
from qtrade_agent.ossbox.crypto import open_sealed, seal
from qtrade_agent.ossbox.inbox import HELD_CMD_IDS, XunjiaInbox
from qtrade_agent.ossbox.keys import ack_object_name, in_object_name
from qtrade_agent.ossbox.oss_http import OssV1Box
from qtrade_agent.store.store import Store

_XUNJIA = b"0123456789abcdef0123456789abcdef"
_DEPOSIT = b"fedcba9876543210fedcba9876543210"
_WB = "anlin-dev"
_TEXT = "债A 1.70\n债B 2.10"
_PEER = "415011447"
_UID = "3007378246"


class FakeBox:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(name for name in self.objects if name.startswith(prefix))

    def get(self, name: str) -> bytes | None:
        return self.objects.get(name)

    def put(self, name: str, body: bytes) -> None:
        self.objects[name] = body


def _command(**overrides: object) -> dict:
    body = {
        "cmd_id": "cmd_new01",
        "workbench_id": _WB,
        "channel": "qidian",
        "account_uid": _UID,
        "op": "send_text",
        "text": _TEXT,
        "idempotency_key": "idem-new-01",
        "session": {"kind": "private", "native_id": _PEER},
    }
    body.update(overrides)
    return body


def _put_command(box: FakeBox, command: dict, *, key: bytes = _XUNJIA) -> str:
    name = in_object_name(_WB, "xunjia", str(command["cmd_id"]))
    raw = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    box.put(name, seal(key, raw))
    return name


def _open_ack(box: FakeBox, cmd_id: str) -> dict:
    blob = box.get(ack_object_name(_WB, cmd_id))
    assert blob is not None
    return json.loads(open_sealed(_XUNJIA, blob).decode("utf-8"))


@pytest.fixture
def store() -> Store:
    box = Store(":memory:", clock=lambda: 1_700_000_000_000).open()
    box.ensure_account("qd90", "qidian", state="running", self_uid=_UID)
    return box


def _inbox(box: FakeBox, store: Store, send) -> XunjiaInbox:
    return XunjiaInbox(box, _XUNJIA, _WB, send, store, clock=lambda: 1_700_000_000_000)


def test_企点文本原样发出并写入采集库和回执(store: Store) -> None:
    box = FakeBox()
    _put_command(box, _command())
    sent: list[tuple[str, str, str]] = []

    async def send(acct, native_id: str, text: str) -> bool:
        sent.append((acct.id, native_id, text))
        return True

    asyncio.run(_inbox(box, store, send).poll())

    assert sent == [("qd90", _PEER, _TEXT)]
    rows, _slow = store.query_messages(account_id="qd90", dir="out")
    assert len(rows) == 1
    assert rows[0]["dir"] == "out"
    assert rows[0]["text"] == _TEXT
    assert rows[0]["session_native_id"] == _PEER
    assert rows[0]["is_self"] == 1
    ack = _open_ack(box, "cmd_new01")
    assert ack["ok"] is False
    assert ack["reason"] == "对话里还没有这条发出"
    assert "text" not in ack
    assert _TEXT not in json.dumps(ack, ensure_ascii=False)


def test_发送失败写原因且不落发出消息(store: Store) -> None:
    box = FakeBox()
    _put_command(box, _command())

    async def send(acct, native_id: str, text: str) -> bool:
        return False

    asyncio.run(_inbox(box, store, send).poll())

    rows, _slow = store.query_messages(account_id="qd90")
    assert rows == []
    ack = _open_ack(box, "cmd_new01")
    assert ack["ok"] is False
    assert ack["reason"]
    assert "text" not in ack
    assert _TEXT not in json.dumps(ack, ensure_ascii=False)
    assert _TEXT not in ack["reason"]


def test_存款密钥打不开且不调用发送(store: Store) -> None:
    box = FakeBox()
    _put_command(box, _command(), key=_DEPOSIT)
    called = {"n": 0}

    async def send(acct, native_id: str, text: str) -> bool:
        called["n"] += 1
        return True

    asyncio.run(_inbox(box, store, send).poll())

    assert called["n"] == 0
    ack = _open_ack(box, "cmd_new01")
    assert ack["ok"] is False
    assert ack["reason"]
    assert _TEXT not in json.dumps(ack, ensure_ascii=False)


def test_非企点或不在本机不发送(store: Store) -> None:
    box = FakeBox()
    _put_command(box, _command(cmd_id="cmd_wx01", channel="wechat", idempotency_key="idem-wx"))
    _put_command(box, _command(cmd_id="cmd_miss01", account_uid="999", idempotency_key="idem-miss"))
    called = {"n": 0}

    async def send(acct, native_id: str, text: str) -> bool:
        called["n"] += 1
        return True

    asyncio.run(_inbox(box, store, send).poll())

    assert called["n"] == 0
    assert _open_ack(box, "cmd_wx01")["ok"] is False
    assert _open_ack(box, "cmd_miss01")["reason"] == "本机没有这个企点号"
    rows, _slow = store.query_messages(account_id="qd90")
    assert rows == []


def test_已有回执和同一幂等键不再发送(store: Store) -> None:
    box = FakeBox()
    _put_command(box, _command())
    _put_command(box, _command(cmd_id="cmd_new02", idempotency_key="idem-new-01"))
    called = {"n": 0}

    async def send(acct, native_id: str, text: str) -> bool:
        called["n"] += 1
        return True

    inbox = _inbox(box, store, send)
    asyncio.run(inbox.poll())
    asyncio.run(inbox.poll())

    assert called["n"] == 1
    second = _open_ack(box, "cmd_new02")
    assert second["ok"] is False
    assert second["reason"] == "对话里还没有这条发出"
    assert "text" not in second
    assert _TEXT not in json.dumps(second, ensure_ascii=False)


def test_不补发已记下的指令(store: Store) -> None:
    assert "cmd_31e9de7f3eba" in HELD_CMD_IDS
    box = FakeBox()
    held = _command(cmd_id="cmd_31e9de7f3eba", text="不要发出的旧正文", idempotency_key="idem-old")
    _put_command(box, held)
    called = {"n": 0}

    async def send(acct, native_id: str, text: str) -> bool:
        called["n"] += 1
        return True

    asyncio.run(_inbox(box, store, send).poll())

    assert called["n"] == 0
    assert box.get(ack_object_name(_WB, "cmd_31e9de7f3eba")) is None
    rows, _slow = store.query_messages(account_id="qd90")
    assert rows == []


def test_存款前缀的指令不在询价轮询里(store: Store) -> None:
    box = FakeBox()
    command = _command(cmd_id="cmd_dep01")
    raw = json.dumps(command, ensure_ascii=False).encode("utf-8")
    box.put(f"wb/{_WB}/in/deposit/cmd_dep01.bin", seal(_XUNJIA, raw))
    called = {"n": 0}

    async def send(acct, native_id: str, text: str) -> bool:
        called["n"] += 1
        return True

    asyncio.run(_inbox(box, store, send).poll())
    assert called["n"] == 0


def test_未配置环境时不装配询价轮询(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OSS_ENDPOINT",
        "OSS_BUCKET",
        "OSS_WORKBENCH_ID",
        "OSS_ACCESS_KEY_ID",
        "OSS_ACCESS_KEY_SECRET",
        "OSS_SEAL_KEY_XUNJIA",
        "OSSBOX_KEY_XUNJIA",
    ):
        monkeypatch.delenv(name, raising=False)
    assert XunjiaInbox.from_env(object(), lambda *_args: False) is None


def test_对象签名串不含密钥() -> None:
    box = OssV1Box(
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="bkt",
        key_id="kid",
        secret="super-secret-value",
    )
    signed = box.string_to_sign(
        "GET", "", "Thu, 01 Jan 2026 00:00:00 GMT", "wb/anlin-dev/in/xunjia/cmd01.bin", None,
    )
    assert signed == (
        "GET\n\n\nThu, 01 Jan 2026 00:00:00 GMT\n"
        "/bkt/wb/anlin-dev/in/xunjia/cmd01.bin"
    )
    listed = box.string_to_sign(
        "GET", "", "Thu, 01 Jan 2026 00:00:00 GMT", "",
        {"prefix": "wb/anlin-dev/in/xunjia/", "max-keys": "100"},
    )
    assert listed.endswith("/bkt/")
    assert "prefix" not in listed
    assert "max-keys" not in listed
    assert "super-secret-value" not in signed
    assert "super-secret-value" not in listed


def test_信箱文件断点记下对象并触发发送且不打正文(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    box = FakeBox()
    _put_command(box, _command())

    async def send(acct, native_id: str, text: str) -> bool:
        return False

    with caplog.at_level(logging.INFO, logger="qtrade.ossbox.inbox"):
        asyncio.run(_inbox(box, store, send).poll())

    text = caplog.text
    assert "询价信箱文件" in text
    assert "pending=['cmd_new01']" in text
    assert "询价文件触发企点发送" in text
    assert "cmd_id=cmd_new01" in text
    assert "text_len=" in text
    assert "sent=False" in text
    assert "询价回执已写" in text
    assert "object=wb/anlin-dev/in/xunjia/cmd_new01.bin" in text
    assert _TEXT not in text
    assert "债A" not in text


_PROBE = "xunjia-agent发送功能与询价功能测试，请勿回复"


def test_对话库出现发出行才回送达(store: Store) -> None:
    """点发送键不够。同文、带通道消息号的发出行出现后，回执才是送达。"""
    box = FakeBox()
    command = _command(text=_PROBE, cmd_id="cmd_probe01", idempotency_key="idem-probe")
    _put_command(box, command)

    async def send(acct, native_id: str, text: str) -> bool:
        store.ingest(Message(
            account_id=acct.id,
            channel="qidian",
            session=Session(acct.id, native_id, "private", name=native_id),
            dir="out",
            type="text",
            text=text,
            ts_ms=1_700_000_000_000,
            source="qidian_db",
            ext_msg_id="qd-probe-1",
            self=True,
            state="DELIVERED",
        ), now_ms=1_700_000_000_000)
        return True

    asyncio.run(_inbox(box, store, send).poll())
    ack = _open_ack(box, "cmd_probe01")
    assert ack["ok"] is True
    assert ack["code"] == "delivered"
    assert _PROBE not in json.dumps(ack, ensure_ascii=False)
