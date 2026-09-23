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


def cxor(text: str) -> str:
    """名称类 TEXT 列:按 UTF-16 码元逐个 XOR 同一把 17 字节密钥(与 ``qidian_monitor_all.cxor`` 一致)。

    自反:``cxor(cxor(s)) == s``。不要用 ``xor()`` 解中文名,那是逐字节,解出来是乱码。
    """
    if not text:
        return text
    raw = text.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(raw[i:i + 2], "little") ^ KEY[(i // 2) % len(KEY)] for i in range(0, len(raw), 2)]
    return b"".join(x.to_bytes(2, "little") for x in units).decode("utf-16-le", "replace")


def decode_name_hex(hex_str: str) -> str:
    """``hex(remark|name|nickname)`` → 显示字符串。空串、非法 hex 回 ``""``。"""
    if not hex_str:
        return ""
    try:
        blob = bytes.fromhex(hex_str)
    except ValueError:
        return ""
    return cxor(blob.decode("utf-8", "replace")).replace("\x00", "").strip()
