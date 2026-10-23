"""对象体密封 —— AES-GCM（NIST SP 800-38D）。

布局：``nonce(12) || ciphertext || tag(16)``，关联数据为空。
密钥不得出现在异常消息或日志里；认证失败不返回明文。
"""
from __future__ import annotations

import hmac
import secrets

# FIPS-197 S-box。长度在首次展开密钥时核对，避免表被截断后静默算出错密文。
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
_SEALED_FAIL = "密封包无法打开"


def _fail() -> None:
    """认证失败或密文损坏。消息固定，不拼接密钥、明文或密文。"""
    raise ValueError(_SEALED_FAIL)


def _xtime(value: int) -> int:
    shifted = (value << 1) & 0xFF
    return shifted ^ 0x1B if value & 0x80 else shifted


def _mix_column(col: list[int]) -> list[int]:
    a0, a1, a2, a3 = col
    return [
        _xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3,
        a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3,
        a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3,
        _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3),
    ]


def _shift_rows(state: bytearray) -> None:
    state[1], state[5], state[9], state[13] = state[5], state[9], state[13], state[1]
    state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
    state[3], state[7], state[11], state[15] = state[15], state[3], state[7], state[11]


def _round_keys(key: bytes) -> list[bytes]:
    if len(_SBOX) != 256:
        raise ValueError("AES 表损坏")
    if len(key) not in (16, 24, 32):
        raise ValueError("密钥长度无效")
    nk = len(key) // 4
    nr = {4: 10, 6: 12, 8: 14}[nk]
    words = [bytearray(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon_i = 1
    while len(words) < 4 * (nr + 1):
        temp = bytearray(words[-1])
        if len(words) % nk == 0:
            temp = bytearray((
                _SBOX[temp[1]] ^ _RCON[rcon_i],
                _SBOX[temp[2]],
                _SBOX[temp[3]],
                _SBOX[temp[0]],
            ))
            rcon_i += 1
        elif nk > 6 and len(words) % nk == 4:
            temp = bytearray(_SBOX[b] for b in temp)
        prev = words[-nk]
        words.append(bytearray(a ^ b for a, b in zip(prev, temp)))
    return [bytes(b for word in words[4 * r:4 * r + 4] for b in word) for r in range(nr + 1)]


def _encrypt_block(block: bytes, rounds: list[bytes]) -> bytes:
    state = bytearray(a ^ b for a, b in zip(block, rounds[0]))
    last = len(rounds) - 1
    for rnd in range(1, last):
        for i in range(16):
            state[i] = _SBOX[state[i]]
        _shift_rows(state)
        for col in range(4):
            start = 4 * col
            mixed = _mix_column(list(state[start:start + 4]))
            state[start:start + 4] = mixed
        rk = rounds[rnd]
        for i in range(16):
            state[i] ^= rk[i]
    for i in range(16):
        state[i] = _SBOX[state[i]]
    _shift_rows(state)
    rk = rounds[last]
    for i in range(16):
        state[i] ^= rk[i]
    return bytes(state)


def _gmul(left: int, right: int) -> int:
    """GF(2^128) 乘法。约简多项式为 x^128 + x^7 + x^2 + x + 1。"""
    acc = 0
    value = left
    for bit in range(128):
        if (right >> (127 - bit)) & 1:
            acc ^= value
        if value & 1:
            value = (value >> 1) ^ 0xE1000000000000000000000000000000
        else:
            value >>= 1
    return acc


def _ghash(h: bytes, ciphertext: bytes) -> bytes:
    """无关联数据的 GHASH。长度按真实比特数，填充只用于分块。"""
    h_int = int.from_bytes(h, "big")
    state = 0
    padded = ciphertext + b"\x00" * ((-len(ciphertext)) % 16)
    for offset in range(0, len(padded), 16):
        block = int.from_bytes(padded[offset:offset + 16], "big")
        state = _gmul(state ^ block, h_int)
    length = len(ciphertext) * 8
    state = _gmul(state ^ length, h_int)
    return state.to_bytes(16, "big")


def _inc32(block: bytes) -> bytes:
    count = (int.from_bytes(block[12:], "big") + 1) & 0xFFFFFFFF
    return block[:12] + count.to_bytes(4, "big")


def _gctr(rounds: list[bytes], counter: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    out = bytearray()
    cb = counter
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        stream = _encrypt_block(cb, rounds)
        out.extend(b ^ stream[i] for i, b in enumerate(block))
        cb = _inc32(cb)
    return bytes(out)


def _gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    rounds = _round_keys(key)
    h = _encrypt_block(b"\x00" * 16, rounds)
    j0 = nonce + b"\x00\x00\x00\x01"
    ciphertext = _gctr(rounds, _inc32(j0), plaintext)
    tag = _gctr(rounds, j0, _ghash(h, ciphertext))
    return ciphertext, tag


def _gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    rounds = _round_keys(key)
    h = _encrypt_block(b"\x00" * 16, rounds)
    j0 = nonce + b"\x00\x00\x00\x01"
    expect = _gctr(rounds, j0, _ghash(h, ciphertext))
    if len(tag) != len(expect) or not hmac.compare_digest(expect, tag):
        _fail()
    return _gctr(rounds, _inc32(j0), ciphertext)


def seal(key: bytes, plaintext: bytes) -> bytes:
    """密封。同一明文每次 nonce 不同，因此密文不同。"""
    key_b = bytes(key)
    body = bytes(plaintext)
    if len(key_b) not in (16, 24, 32):
        raise ValueError("密钥长度无效")
    nonce = secrets.token_bytes(12)
    ciphertext, tag = _gcm_encrypt(key_b, nonce, body)
    return nonce + ciphertext + tag


def open_sealed(key: bytes, blob: bytes) -> bytes:
    """解开密封包。密钥错误或密文损坏时失败，且不返回明文。"""
    try:
        key_b = bytes(key)
        data = bytes(blob)
        if len(key_b) not in (16, 24, 32) or len(data) < 28:
            _fail()
        nonce, tag = data[:12], data[-16:]
        ciphertext = data[12:-16]
        return _gcm_decrypt(key_b, nonce, ciphertext, tag)
    except Exception:
        # 丢掉内部异常链，避免消息、参数或 cause 带出密钥材料。
        raise ValueError(_SEALED_FAIL) from None
