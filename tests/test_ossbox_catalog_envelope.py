"""目录双密封封套。只用假钥，不连 OSS，不读环境变量。"""
from __future__ import annotations

import base64
import json

import pytest

from qtrade_agent.ossbox.crypto import open_sealed
from qtrade_agent.ossbox.envelope import pack_piece_envelope
from qtrade_agent.ossbox.schema import validate_catalog

_DEPOSIT = bytes(range(32))
_XUNJIA = bytes(range(32, 64))
_STANDARD = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


def _catalog() -> dict:
    return {
        "workbench_id": "anlin-dev",
        "label": "不该出现",
        "accounts": [
            {
                "channel": "wechat",
                "account_uid": "wxid_example",
                "account_nick": "德儿塔外",
                "label": "备注",
                "groups": [
                    {"native_id": "100@chatroom", "name": "固收群", "label": "群备注"},
                ],
            },
            {
                "channel": "qidian",
                "account_uid": "3000000000",
                "account_nick": "",
                "groups": [],
            },
        ],
    }


def test_两把假钥都能解开同一份目录且备注被去掉():
    body = pack_piece_envelope(validate_catalog(_catalog()), _DEPOSIT, _XUNJIA)
    assert isinstance(body, bytes)
    assert b"\n" not in body and b"\r" not in body
    assert _DEPOSIT not in body and _XUNJIA not in body
    envelope = json.loads(body.decode("utf-8"))
    assert list(envelope) == ["v", "seals"]
    assert envelope["v"] == 1
    assert list(envelope["seals"]) == ["deposit", "xunjia"]

    opened = []
    for slot, key in (("deposit", _DEPOSIT), ("xunjia", _XUNJIA)):
        text = envelope["seals"][slot]
        assert set(text) <= _STANDARD
        assert "-" not in text and "_" not in text
        blob = base64.b64decode(text, validate=True)
        assert base64.b64encode(blob).decode("ascii") == text
        opened.append(open_sealed(key, blob))
    assert opened[0] == opened[1]
    plain = json.loads(opened[0].decode("utf-8"))
    assert plain["workbench_id"] == "anlin-dev"
    assert plain["accounts"][0]["account_nick"] == "德儿塔外"
    assert plain["accounts"][0]["groups"][0]["native_id"] == "100@chatroom"
    assert "label" not in json.dumps(plain, ensure_ascii=False)


def test_存款钥解不开询价密封():
    body = pack_piece_envelope(validate_catalog(_catalog()), _DEPOSIT, _XUNJIA)
    seals = json.loads(body.decode("utf-8"))["seals"]
    deposit_blob = base64.b64decode(seals["deposit"])
    xunjia_blob = base64.b64decode(seals["xunjia"])
    assert deposit_blob != xunjia_blob
    with pytest.raises(ValueError):
        open_sealed(_DEPOSIT, xunjia_blob)
    with pytest.raises(ValueError):
        open_sealed(_XUNJIA, deposit_blob)


def test_非法通道拒绝且异常不含假钥():
    catalog = _catalog()
    catalog["accounts"][0]["channel"] = "mail"
    with pytest.raises(ValueError) as exc:
        pack_piece_envelope(validate_catalog(catalog), _DEPOSIT, _XUNJIA)
    text = str(exc.value)
    assert _DEPOSIT.hex() not in text
    assert _XUNJIA.hex() not in text
