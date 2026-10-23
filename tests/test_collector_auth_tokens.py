"""Current gateway token contract, independently opened through OpenSSL C ABI."""
import base64
import ctypes
import json

import pytest

from relay.file_gateway import FileGatewayError, GatewayAuth


def openssl_plain(token, key):
    raw = base64.b64decode(token, validate=True)
    assert len(raw) >= 32 and len(raw) % 16 == 0
    assert base64.b64encode(raw).decode("ascii") == token
    lib = ctypes.CDLL("libcrypto.so.3")
    ptr = ctypes.c_void_p
    size = ctypes.POINTER(ctypes.c_int)
    for name, args, result in (
        ("EVP_CIPHER_CTX_new", [], ptr), ("EVP_CIPHER_CTX_free", [ptr], None),
        ("EVP_sm4_cbc", [], ptr), ("EVP_DecryptInit_ex", [ptr, ptr, ptr, ptr, ptr], ctypes.c_int),
        ("EVP_DecryptUpdate", [ptr, ptr, size, ptr, ctypes.c_int], ctypes.c_int),
        ("EVP_DecryptFinal_ex", [ptr, ptr, size], ctypes.c_int),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = args, result
    ctx = lib.EVP_CIPHER_CTX_new()
    assert ctx
    try:
        out = ctypes.create_string_buffer(len(raw) + 16)
        n, tail = ctypes.c_int(), ctypes.c_int()
        assert lib.EVP_DecryptInit_ex(ctx, lib.EVP_sm4_cbc(), None, key, raw[:16]) == 1
        assert lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(n), raw[16:], len(raw) - 16) == 1
        assert lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(out, n.value), ctypes.byref(tail)) == 1
        return json.loads(out.raw[:n.value + tail.value].decode("utf-8"))
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


@pytest.mark.parametrize("business_key", ["synthetic-auth", "合成业务口令", "  synthetic-whitespace-key  "])
@pytest.mark.parametrize("sm4", [b"0123456789abcdef", ("中" * 5 + "a").encode()])
def test_current_token_is_base64_iv_sm4cbc_with_exact_unicode_key_and_string_seconds(business_key, sm4):
    auth = GatewayAuth(business_key, sm4)
    tokens = [auth.token(now=1_700_000_000.9) for _ in range(3)]
    assert len(set(tokens)) == 3, "Each authentication attempt needs a fresh random IV"
    assert len({base64.b64decode(value)[:16] for value in tokens}) == 3
    for token in tokens:
        assert openssl_plain(token, sm4) == {"key": business_key, "ts": "1700000000"}
        assert business_key not in token and sm4.decode() not in token
    assert business_key not in repr(auth) and sm4.decode() not in repr(auth)


@pytest.mark.parametrize("sm4", [b"short", b"x" * 15, b"x" * 17, ("中" * 16).encode(), "0123456789abcdef"])
def test_current_auth_rejects_every_non_16_byte_sm4_key(sm4):
    with pytest.raises(FileGatewayError, match="FILE_AUTH_SM4_LENGTH_INVALID"):
        GatewayAuth("synthetic-key", sm4)


@pytest.mark.parametrize("now", [-1, float("nan"), float("inf"), True, "1700000000"])
def test_current_auth_rejects_invalid_token_timestamps(now):
    with pytest.raises(FileGatewayError, match="FILE_AUTH_TIME_INVALID"):
        GatewayAuth("synthetic-key", b"0123456789abcdef").token(now=now)
