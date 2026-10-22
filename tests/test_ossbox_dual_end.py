"""工作台与存款、询价两侧的 AES-GCM 密封包必须能互解。

不连接 nyoss.yingmi.com，不读取环境变量里的真实密钥。
存款侧用它自己的解释器调用 cryptography；询价侧验证当前共享协议 Codec。
"""
from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from qtrade_agent.ossbox.bridge import plan_uploads
from qtrade_agent.ossbox.crypto import open_sealed, seal

_KEY = b"0123456789abcdef0123456789abcdef"
_PLAIN = "固收群报价 3M 1.52 可谈".encode()
_DEPOSIT = Path("/home/anlin/work/qtrade/ibquote/src/njcb_deposit_agent/wechat_collect/crypto.py")
_DEPOSIT_PY = Path("/home/anlin/work/qtrade/ibquote/.venv/bin/python")
_PEER = """
import base64, importlib.util, sys
path, op = sys.argv[1], sys.argv[2]
key = base64.b64decode(sys.argv[3])
data = base64.b64decode(sys.argv[4]) if sys.argv[4] else b""
spec = importlib.util.spec_from_file_location("peer_crypto", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
out = module.open_sealed(key, data) if op == "open" else module.seal(key, data)
sys.stdout.buffer.write(base64.b64encode(out))
"""


def _deposit_call(op: str, data: bytes) -> bytes:
    if not _DEPOSIT.is_file() or not _DEPOSIT_PY.is_file():
        pytest.skip("存款侧解释器或密封模块不在本机")
    result = subprocess.run(
        [_DEPOSIT_PY, "-c", _PEER, str(_DEPOSIT), op, base64.b64encode(_KEY).decode(), base64.b64encode(data).decode()],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return base64.b64decode(result.stdout)


def test_workbench_and_deposit_open_each_other() -> None:
    outbound = seal(_KEY, _PLAIN)
    assert _deposit_call("open", outbound) == _PLAIN
    inbound = _deposit_call("seal", _PLAIN)
    assert open_sealed(_KEY, inbound) == _PLAIN
    assert _PLAIN not in outbound
    assert _KEY not in outbound


def test_workbench_and_xunjia_open_each_other() -> None:
    from relay.protocol import Binding, Codec, canonical, envelope
    binding=Binding('synthetic-owner','synthetic-account','synthetic-workbench',_KEY,b's'*32)
    codec=Codec(binding,clock=lambda:100)
    value=envelope(binding,'event',{'event_id':'synthetic-native','peer_id':'synthetic-peer',
        'direction':'in','text':_PLAIN.decode(),'source_seq':1,'occurred_at':100},
        message_id='synthetic-native',peer_id='synthetic-peer',created_at=100,expires_at=200)
    inbound=codec.pack(value)
    signed_plain=open_sealed(_KEY,inbound)
    signed=json.loads(signed_plain)
    assert signed.pop('signature').startswith('hmac-sha256=')
    assert signed==value
    outbound=seal(_KEY,signed_plain)
    assert codec.unpack(outbound)==value
    assert _PLAIN not in inbound and _KEY not in inbound


def test_planned_upload_opens_on_deposit() -> None:
    catalog = {
        "workbench_id": "wb-nanjing-a",
        "accounts": [{"channel": "wechat", "account_uid": "wxid_a", "nick": "安琳"}],
    }
    binding = {
        "workbench_id": "wb-nanjing-a",
        "transport": "oss",
        "wxid": "wxid_a",
        "group_native_ids": ["grp-1"],
    }
    message = {
        "event_id": "e1",
        "workbench_id": "wb-nanjing-a",
        "channel": "wechat",
        "account_uid": "wxid_a",
        "dir": "in",
        "text": "3M 1.52 可谈",
        "ts": "2026-10-12T02:00:00+08:00",
        "ext_msg_id": "m1",
        "session": {"kind": "group", "native_id": "grp-1"},
    }
    uploads = plan_uploads(catalog, binding, [message], key=_KEY, consumer="deposit")
    assert uploads
    for item in uploads:
        opened = _deposit_call("open", item.ciphertext)
        assert b"3M 1.52" in opened or b"wb-nanjing-a" in opened
        assert _KEY not in item.ciphertext
