"""目录双密封封套。

同一份明文分别用存款钥和询价钥 ``seal``。单次 AES-GCM 只能一把钥解开，
所以对象体同时带两份密封，不能用一把钥覆盖另一把。
"""
from __future__ import annotations

import base64
import json

from .crypto import seal
_B64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _dual_seal(plaintext: bytes, deposit_key: bytes, xunjia_key: bytes) -> bytes:
    envelope = {
        "v": 1,
        "seals": {
            "deposit": _b64(seal(bytes(deposit_key), plaintext)),
            "xunjia": _b64(seal(bytes(xunjia_key), plaintext)),
        },
    }
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in body or b"\r" in body:
        raise ValueError("封套不得换行")
    return body


def pack_piece_envelope(piece: dict[str, Any], deposit_key: bytes, xunjia_key: bytes) -> bytes:
    """把总索引或单个账号的群/通讯录打成双封套。明文不是整包目录。"""
    if not isinstance(piece, dict):
        raise ValueError("拆分对象必须是映射")
    plaintext = json.dumps(piece, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _dual_seal(plaintext, deposit_key, xunjia_key)


def _b64(blob: bytes) -> str:
    text = base64.b64encode(blob).decode("ascii")
    if any(ch not in _B64_CHARS for ch in text):
        raise ValueError("密封段必须是标准 base64")
    return text
