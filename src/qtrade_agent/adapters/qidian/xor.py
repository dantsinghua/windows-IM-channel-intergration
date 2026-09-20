"""企点主库列编码 —— 规格唯一出处:docs/06 §2.9.5「列编码 = 逐字节 XOR」(R6-36,R6-39 勘误)。

密钥 = ASCII 字符串 ``"02:00:00:00:00:00"`` 的 **17 个字节**循环(含冒号),
``out[i] = in[i] ^ KEY[i % 17]``。🔴 不是安卓默认 MAC 的 6 个原始字节——按 6 字节解全是乱码。
非加密、无盐、恒定;``msgData``/``senderuin``/``frienduin`` 等文本/blob 列都用它;
``_id``/``issend``/``istroop``/``time``/``uniseq``/``msgseq``/``shmsgseq`` 是明文整数,不解码。
"""
from __future__ import annotations

KEY = b"02:00:00:00:00:00"
assert len(KEY) == 17


def xor(data: bytes | bytearray | memoryview) -> bytes:
    """逐字节异或;对合法输入是自反的(``xor(xor(b)) == b``)。"""
    b = bytes(data)
    k = KEY
    return bytes(b[i] ^ k[i % 17] for i in range(len(b)))


def xor_hex(hex_str: str) -> bytes:
    """设备上用 ``hex(col)`` 取回的十六进制串 → 解码后的原始字节。"""
    return xor(bytes.fromhex(hex_str))


def decode_uin(data: bytes | str) -> str:
    """``senderuin``/``frienduin`` 列(XOR 后是 ASCII 数字串)→ 纯数字 uin 字符串。"""
    raw = xor_hex(data) if isinstance(data, str) else xor(data)
    return raw.decode("ascii", errors="replace").strip("\x00")
