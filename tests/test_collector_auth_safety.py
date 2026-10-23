"""当前文件网关认证保护：只使用合成值与内存 HTTP，不依赖退役 STS。"""
from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
from http.client import BadStatusLine
import io
import json
import logging
import socket
import ssl
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth


AUTH_KEY = "synthetic-current-only-认证口令"
SM4_KEY = b"0123456789abcdef"
NOW = 1_760_400_000.25
BASE = "https://gateway.example.invalid"
PRIVATE = "synthetic-private-upstream-canary"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """测试文件独立运行时也拒绝 DNS、HTTP 和任何 socket 连接。"""
    violations = []

    def denied(*args, **kwargs):
        violations.append(True)
        raise AssertionError("Authentication safety tests prohibit network access")

    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(OpenerDirector, "open", denied)
    yield
    assert not violations, "A network attempt occurred, even if product code caught it"


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class Capture:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        assert timeout == 15
        assert self.results, "Unexpected extra request or implicit retry"
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def client(opener):
    return FileGatewayClient(BASE, GatewayAuth(AUTH_KEY, SM4_KEY),
                             opener=opener, clock=lambda: NOW, path="synthetic")


def decrypt(token, sm4_key=SM4_KEY):
    """独立用底层密码库解密，校验 IV、SM4-CBC、PKCS7 与 UTF-8。"""
    raw = base64.b64decode(token, validate=True)
    assert len(raw) >= 32 and (len(raw) - 16) % 16 == 0
    decoder = Cipher(algorithms.SM4(sm4_key), modes.CBC(raw[:16])).decryptor()
    padded = decoder.update(raw[16:]) + decoder.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plain.decode("utf-8"))


def assert_redacted(error, logs, *extra):
    rendered = "".join(traceback.format_exception(error)) + repr(error) + logs
    for forbidden in (AUTH_KEY, SM4_KEY.decode(), PRIVATE, *extra):
        assert forbidden not in rendered


def test_random_iv_changes_all_same_second_tokens_and_preserves_plaintext():
    auth = GatewayAuth(AUTH_KEY, SM4_KEY)
    tokens = [auth.token(now=NOW) for _ in range(32)]
    assert len(set(tokens)) == 32
    assert len({base64.b64decode(token, validate=True)[:16] for token in tokens}) == 32
    for token in tokens:
        assert decrypt(token) == {"key": AUTH_KEY, "ts": str(int(NOW))}
        assert AUTH_KEY not in token


@pytest.mark.parametrize("key", ["仅测试的业务口令", "  合成-🔐-口令  ", "e\u0301-\u00e9"])
def test_unicode_business_key_roundtrips_without_normalization_or_trimming(key):
    assert decrypt(GatewayAuth(key, SM4_KEY).token(now=NOW)) == {
        "key": key, "ts": str(int(NOW))}


@pytest.mark.parametrize("key", ["x" * 4096, "密" * 1365 + "x", "🔐" * 1024],
                         ids=["ascii-4096", "cjk-4096", "emoji-4096"])
def test_business_key_accepts_exact_utf8_byte_limit(key):
    assert len(key.encode("utf-8")) == 4096
    assert decrypt(GatewayAuth(key, SM4_KEY).token(now=NOW))["key"] == key


@pytest.mark.parametrize("key", ["x" * 4097, "密" * 1366, "🔐" * 1025],
                         ids=["ascii-4097", "cjk-4098", "emoji-4100"])
def test_business_key_rejects_oversized_utf8_even_when_character_count_is_short(key, caplog):
    assert len(key.encode("utf-8")) > 4096
    with pytest.raises(FileGatewayError, match="^FILE_AUTH_KEY_INVALID$") as caught:
        GatewayAuth(key, SM4_KEY)
    assert_redacted(caught.value, caplog.text, key)


@pytest.mark.parametrize("key", [b"", b"short-key", b"x" * 15, b"x" * 17, b"x" * 32, "0123456789abcdef", None])
def test_sm4_key_requires_exactly_16_bytes(key):
    with pytest.raises(FileGatewayError, match="^FILE_AUTH_SM4_LENGTH_INVALID$"):
        GatewayAuth(AUTH_KEY, key)


@pytest.mark.parametrize("text", ["0123456789abcdef", "密" * 5 + "a", "🔐" * 4])
def test_yaml_sm4_key_uses_utf8_bytes_not_character_count(text):
    assert len(text.encode("utf-8")) == 16
    auth = GatewayAuth.from_yaml(json.dumps({"auth": {"key": AUTH_KEY, "sm4_key": text}}, ensure_ascii=False))
    assert decrypt(auth.token(now=NOW), text.encode("utf-8"))["key"] == AUTH_KEY


@pytest.mark.parametrize("text", ["密" * 16, "🔐" * 16, "密" * 5, "🔐" * 4 + "a"])
def test_yaml_sm4_key_rejects_wrong_utf8_byte_length(text, caplog):
    assert len(text.encode("utf-8")) != 16
    with pytest.raises(FileGatewayError, match="^FILE_AUTH_SM4_LENGTH_INVALID$") as caught:
        GatewayAuth.from_yaml(json.dumps({"key": AUTH_KEY, "sm4_key": text}, ensure_ascii=False))
    assert_redacted(caught.value, caplog.text, text)


def test_auth_repr_hides_both_secrets_and_auth_is_immutable():
    auth = GatewayAuth(AUTH_KEY, SM4_KEY)
    assert AUTH_KEY not in repr(auth)
    assert SM4_KEY.decode() not in repr(auth)
    with pytest.raises(FrozenInstanceError):
        auth.key = "replacement"


def test_each_request_gets_fresh_header_token_without_url_or_body_credentials():
    capture = Capture(Response(b'{"pending":0}'), Response(b'{"pending":0}'))
    gateway = client(capture)
    assert capture.requests == []
    assert gateway.pending() == 0
    assert gateway.pending() == 0
    assert len(capture.requests) == 2
    tokens = []
    for request in capture.requests:
        headers = {key.lower(): value for key, value in request.header_items()}
        token = headers["accesstoken"]
        tokens.append(token)
        assert decrypt(token) == {"key": AUTH_KEY, "ts": str(int(NOW))}
        assert request.full_url == BASE + "/api/files/pending?path=synthetic"
        assert request.data is None
        assert request.get_method() == "GET"
        for value in (token, AUTH_KEY, SM4_KEY.decode()):
            assert value not in request.full_url
    assert tokens[0] != tokens[1]


@pytest.mark.parametrize("status,code", [(401, "FILE_AUTH_REJECTED"), (403, "FILE_ACCESS_DENIED"),
    (413, "FILE_TOO_LARGE"), (429, "FILE_RATE_LIMITED"), (500, "FILE_GATEWAY_HTTP_ERROR")])
def test_http_error_status_does_not_expose_body_reason_or_token(status, code, caplog):
    capture = Capture(HTTPError(BASE, status, PRIVATE, {}, io.BytesIO(PRIVATE.encode())))
    with caplog.at_level(logging.DEBUG), pytest.raises(FileGatewayError) as caught:
        client(capture).pending()
    assert caught.value.code == code
    assert caught.value.status == status
    token = dict((key.lower(), value) for key, value in capture.requests[0].header_items())["accesstoken"]
    assert_redacted(caught.value, caplog.text, token)
    assert len(capture.requests) == 1


@pytest.mark.parametrize("operation", ["pending", "upload", "download"])
@pytest.mark.parametrize("kind", ["network", "os", "http-open", "http-read", "tls", "certificate"])
def test_transport_errors_are_redacted_and_preserve_mutation_uncertainty(operation, kind, caplog):
    class BrokenResponse(Response):
        def read(self, size=-1):
            raise BadStatusLine(PRIVATE)

    result = {
        "network": URLError(PRIVATE), "os": OSError(PRIVATE),
        "http-open": BadStatusLine(PRIVATE), "http-read": BrokenResponse(b""),
        "tls": ssl.SSLError(PRIVATE), "certificate": ssl.SSLCertVerificationError(PRIVATE),
    }[kind]
    capture = Capture(result)
    gateway = client(capture)
    with caplog.at_level(logging.DEBUG), pytest.raises(FileGatewayError) as caught:
        if operation == "upload":
            gateway.upload("synthetic.bin", b"synthetic-body")
        else:
            getattr(gateway, operation)()
    mutation = operation != "pending"
    expected = "FILE_GATEWAY_OUTCOME_UNKNOWN" if mutation else "FILE_GATEWAY_UNAVAILABLE"
    if kind == "tls":
        expected = "FILE_TLS_HANDSHAKE_FAILED"
    elif kind == "certificate":
        expected = "FILE_TLS_SERVER_CERT_INVALID"
    assert caught.value.code == expected
    assert caught.value.ambiguous is (mutation and kind != "certificate")
    token = dict((key.lower(), value) for key, value in capture.requests[0].header_items())["accesstoken"]
    assert_redacted(caught.value, caplog.text, token)
    assert len(capture.requests) == 1


def test_malformed_pending_response_hides_private_body(caplog):
    capture = Capture(Response(PRIVATE.encode()))
    with pytest.raises(FileGatewayError, match="^FILE_PENDING_RESPONSE_INVALID$") as caught:
        client(capture).pending()
    assert_redacted(caught.value, caplog.text)


@pytest.mark.parametrize("kind", ["duplicate", "malformed", "wrong-shape"])
def test_yaml_parser_errors_hide_secret_source_and_context(kind, caplog):
    text = {"duplicate": f"key: {PRIVATE}\nkey: {AUTH_KEY}\nsm4_key: '{SM4_KEY.decode()}'\n",
            "malformed": f"auth: [{PRIVATE}\n",
            "wrong-shape": json.dumps([PRIVATE, AUTH_KEY, SM4_KEY.decode()])}[kind]
    with pytest.raises(FileGatewayError, match="^FILE_AUTH_CONFIG_INVALID$") as caught:
        GatewayAuth.from_yaml(text)
    assert_redacted(caught.value, caplog.text)


@pytest.mark.parametrize("kind", ["missing", "invalid-utf8", "oversized"])
def test_synthetic_credential_file_errors_hide_contents_and_path(tmp_path, kind, caplog):
    path = tmp_path / (PRIVATE + ".yaml")
    if kind == "invalid-utf8":
        path.write_bytes(PRIVATE.encode() + b"\xff\xfe")
    elif kind == "oversized":
        path.write_bytes(PRIVATE.encode() * 1024)
    with pytest.raises(FileGatewayError, match="^FILE_AUTH_CONFIG_INVALID$") as caught:
        GatewayAuth.from_file(path)
    assert_redacted(caught.value, caplog.text, str(path))
