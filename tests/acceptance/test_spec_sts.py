"""STS HTML 协议独立验收：真实回环 HTTP、OpenSSL 参照、纯捕获 OSS。

依据根目录 STS接口调用说明.html 和总控给定公开接口合同编写；不读取产品
实现或开发者测试。所有口令、临时凭据均为合成值，不接触云端或工作账号。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import io
import json
import os
import socket
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest

from qtrade_agent.ossbox.oss_http import OssV1Box
from qtrade_agent.ossbox.sts import StsCredentialProvider, build_access_token


AUTH_KEY = "acceptance-only-业务口令"
SM4_KEY = b"0123456789abcdef"
BUCKET = "acceptance-only-bucket"
NOW = 1_760_400_000.0


class Seconds:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def _decrypt_reference(token):
    """通过 OpenSSL C ABI 独立解密；不启动宿主命令，不使用产品加密库。"""
    raw = base64.b64decode(token, validate=True)
    assert len(raw) >= 32 and len(raw) % 16 == 0
    assert base64.b64encode(raw).decode("ascii") == token
    lib = ctypes.CDLL("libcrypto.so.3")
    ptr = ctypes.c_void_p
    size = ctypes.POINTER(ctypes.c_int)
    for name, args, result in (
        ("EVP_CIPHER_CTX_new", [], ptr),
        ("EVP_CIPHER_CTX_free", [ptr], None),
        ("EVP_sm4_cbc", [], ptr),
        ("EVP_DecryptInit_ex", [ptr, ptr, ptr, ptr, ptr], ctypes.c_int),
        ("EVP_DecryptUpdate", [ptr, ptr, size, ptr, ctypes.c_int], ctypes.c_int),
        ("EVP_DecryptFinal_ex", [ptr, ptr, size], ctypes.c_int),
    ):
        func = getattr(lib, name)
        func.argtypes, func.restype = args, result
    ctx = lib.EVP_CIPHER_CTX_new()
    assert ctx
    try:
        out = ctypes.create_string_buffer(len(raw) + 16)
        n, tail = ctypes.c_int(), ctypes.c_int()
        assert lib.EVP_DecryptInit_ex(ctx, lib.EVP_sm4_cbc(), None, SM4_KEY, raw[:16]) == 1
        assert lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(n), raw[16:], len(raw) - 16) == 1
        assert lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(out, n.value), ctypes.byref(tail)) == 1
        return json.loads(out.raw[:n.value + tail.value].decode("utf-8"))
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def _credentials(label="one", *, expiration=NOW + 3600, bucket=BUCKET):
    return {
        "access_key_id": f"STS.acceptance-{label}",
        "access_key_secret": f"synthetic-secret-{label}",
        "security_token": f"synthetic-token-{label}",
        "expiration": datetime.fromtimestamp(expiration, timezone.utc).isoformat().replace("+00:00", "Z"),
        "bucket": bucket,
    }


@pytest.fixture(autouse=True)
def _network_boundary(monkeypatch):
    """仅放行本用例创建的回环服务；即使产品吞异常也会在 teardown 变红。"""
    allowed, violations = set(), []
    connect = socket.socket.connect
    getaddrinfo = socket.getaddrinfo
    for name in list(os.environ):
        if name.startswith("OSS_") or name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name, raising=False)

    def resolve(host, port, *args, **kwargs):
        if (host, int(port)) not in allowed:
            violations.append((host, port))
            raise AssertionError("STS acceptance attempted unowned network destination")
        return getaddrinfo(host, port, *args, **kwargs)

    def guarded_connect(sock, address):
        if address not in allowed:
            violations.append(address)
            raise AssertionError("STS acceptance attempted unowned network destination")
        return connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield allowed
    assert not violations, f"Unexpected network attempts: {violations}"


@pytest.fixture
def sts_server(_network_boundary):
    active = []

    @contextmanager
    def start(clock=None):
        clock = clock or Seconds()
        replies, requests, errors = deque(), [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                token = self.headers.get("access_token", "")
                requests.append((self.path, token))
                try:
                    plain = _decrypt_reference(token)
                    assert plain["key"] == AUTH_KEY
                    assert isinstance(plain["ts"], str)
                    assert abs(int(plain["ts"]) - clock()) <= 60
                    assert self.path == "/api/sts"
                    status, body, headers = replies.popleft()
                except Exception as exc:
                    errors.append(repr(exc))
                    status, body, headers = 401, {"message": "synthetic-validation-error"}, {}
                if status == 0:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        _network_boundary.add(httpd.server_address)
        thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()

        class Service:
            url = f"http://127.0.0.1:{httpd.server_port}/api/sts"

            def reply(self, body=None, status=200, **headers):
                replies.append((status, body if body is not None else _credentials(), headers))

            def provider(self, **kwargs):
                return StsCredentialProvider(self.url, AUTH_KEY, SM4_KEY, clock=clock, **kwargs)

        service = Service()
        service.requests, service.clock = requests, clock
        active.append((httpd, thread))
        try:
            yield service
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            active.remove((httpd, thread))
            assert not thread.is_alive()
            assert not errors, f"HTML protocol mismatch at independent server: {errors}"

    yield start
    for httpd, thread in active:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class CapturedResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class CaptureOss:
    """urllib 公开运输接缝，仅记录请求，无 DNS/socket。"""
    def __init__(self, *replies):
        self.requests = []
        self.replies = deque(replies)

    def open(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.popleft() if self.replies else (200, b"accepted")
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if status >= 400:
            raise HTTPError(request.full_url, status, "synthetic-error", {}, io.BytesIO(body))
        return CapturedResponse(body, status)


def _assert_signed(request, expected, *, bucket=BUCKET):
    headers = {key.lower(): value for key, value in request.header_items()}
    token = expected.get("security_token")
    if token:
        assert headers["x-oss-security-token"] == token
    else:
        assert "x-oss-security-token" not in headers
    path = urlsplit(request.full_url).path
    canonical_headers = "".join(f"{key}:{value.strip()}\n" for key, value in sorted(headers.items()) if key.startswith("x-oss-"))
    canonical = "\n".join((request.get_method(), headers.get("content-md5", ""), headers.get("content-type", ""), headers["date"]))
    canonical += "\n" + canonical_headers + f"/{bucket}{path}"
    signature = base64.b64encode(hmac.new(expected["access_key_secret"].encode(), canonical.encode(), hashlib.sha1).digest()).decode()
    assert headers["authorization"] == f"OSS {expected['access_key_id']}:{signature}"
    assert urlsplit(request.full_url).hostname == f"{bucket}.oss.example.invalid"


def test_html_token_openssl_reference_and_fresh_iv():
    tokens = [build_access_token(AUTH_KEY, SM4_KEY, now=NOW + 0.75) for _ in range(3)]
    assert len(set(tokens)) == 3
    for token in tokens:
        assert _decrypt_reference(token) == {"key": AUTH_KEY, "ts": str(int(NOW))}


def test_real_http_cache_refresh_boundary_and_frozen_credentials(sts_server):
    with sts_server() as server:
        first, second = _credentials(), _credentials("two", expiration=NOW + 7200)
        server.reply(first)
        server.reply(second)
        provider = server.provider(expected_bucket=BUCKET)
        credentials = provider.get_credentials()
        assert (credentials.access_key_id, credentials.access_key_secret, credentials.security_token, credentials.expiration, credentials.bucket) == (
            first["access_key_id"], first["access_key_secret"], first["security_token"], NOW + 3600, BUCKET)
        with pytest.raises((AttributeError, TypeError)):
            credentials.bucket = "changed"
        server.clock.now = NOW + 3299
        assert provider.get_credentials() is credentials
        assert len(server.requests) == 1
        server.clock.now = NOW + 3300
        assert provider.get_credentials().access_key_id == second["access_key_id"]
        assert len(server.requests) == 2
        assert server.requests[0][1] != server.requests[1][1]


@pytest.mark.parametrize("status", [502, 0], ids=["http-502", "peer-disconnect"])
def test_transient_failure_reuses_still_valid_cache_for_only_15_seconds(sts_server, status):
    with sts_server() as server:
        server.reply()
        server.reply({"message": "synthetic-upstream-failure"}, status)
        server.reply(_credentials("two", expiration=NOW + 7200))
        provider = server.provider()
        first = provider.get_credentials()
        server.clock.now = NOW + 3300
        assert provider.get_credentials() is first
        server.clock.now += 14
        assert provider.get_credentials() is first
        assert len(server.requests) == 2
        server.clock.now += 1
        assert provider.get_credentials().access_key_id == "STS.acceptance-two"
        assert len(server.requests) == 3
        assert len({token for _, token in server.requests}) == 3


def test_real_502_cannot_reuse_cache_with_only_30_seconds_left(sts_server):
    with sts_server() as server:
        server.reply()
        server.reply({"message": "synthetic-upstream-failure"}, 502)
        provider = server.provider()
        provider.get_credentials()
        server.clock.now = NOW + 3570
        with pytest.raises(Exception):
            provider.get_credentials()
        assert len(server.requests) == 2


def test_real_401_revokes_usable_old_cache(sts_server):
    with sts_server() as server:
        server.reply()
        server.reply({"message": "key mismatch"}, 401)
        server.reply({"message": "synthetic-upstream-failure"}, 502)
        provider = server.provider()
        provider.get_credentials()
        server.clock.now = NOW + 3300
        with pytest.raises(Exception):
            provider.get_credentials()
        with pytest.raises(Exception):
            provider.get_credentials()
        assert len(server.requests) == 3


@pytest.mark.parametrize("bad", [
    _credentials(expiration=NOW),
    _credentials(bucket="another-bucket"),
    {**_credentials(), "security_token": ""},
    {**_credentials(), "expiration": "not-a-date"},
    b"x" * (65536 + 1),
], ids=["expired", "wrong-bucket", "empty-token", "invalid-expiration", "response-over-64k"])
def test_bad_200_response_fails_closed(sts_server, bad):
    with sts_server() as server:
        server.reply(bad)
        with pytest.raises(Exception):
            server.provider(expected_bucket=BUCKET).get_credentials()
        assert len(server.requests) == 1


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirect_never_sends_access_token_to_target(sts_server, code):
    with sts_server() as origin, sts_server() as target:
        target.reply()
        origin.reply({}, code, Location=target.url)
        with pytest.raises(Exception):
            origin.provider().get_credentials()
        assert len(origin.requests) == 1
        assert target.requests == []


def test_non_loopback_plain_http_rejected_before_any_network():
    with pytest.raises(Exception):
        StsCredentialProvider("http://192.0.2.1/api/sts", AUTH_KEY, SM4_KEY).get_credentials()


def test_default_http_transport_sets_ten_second_socket_timeout(sts_server, monkeypatch):
    timeouts = []
    original = socket.socket.settimeout

    def record(sock, value):
        timeouts.append(value)
        return original(sock, value)

    monkeypatch.setattr(socket.socket, "settimeout", record)
    with sts_server() as server:
        server.reply()
        server.provider().get_credentials()
    assert 10 in timeouts


def test_real_sts_to_fake_oss_all_operations_use_rotated_triplet(sts_server):
    with sts_server() as server:
        first, second = _credentials(), _credentials("two", expiration=NOW + 7200)
        server.reply(first)
        server.reply(second)
        box = OssV1Box(endpoint="https://oss.example.invalid", credentials_provider=server.provider())
        transport = CaptureOss((200, b"body"), (200, b""), (200, b"<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>sample.bin</Key></Contents></ListBucketResult>"))
        box._opener = transport
        assert box.get("sample.bin") == b"body"
        _assert_signed(transport.requests[0], first)
        server.clock.now = NOW + 3300
        assert box.put("sample.bin", b"new-body") is None
        assert transport.requests[1].data == b"new-body"
        assert box.list_prefix("sample") == ["sample.bin"]
        for request in transport.requests[1:]:
            _assert_signed(request, second)
        assert len(server.requests) == 2


@pytest.mark.parametrize("code", ["InvalidSecurityToken", "SecurityTokenExpired"])
def test_explicit_oss_token_rejection_invalidates_without_same_request_retry(sts_server, code):
    with sts_server() as server:
        first, second = _credentials(), _credentials("two")
        server.reply(first)
        server.reply(second)
        box = OssV1Box(endpoint="https://oss.example.invalid", credentials_provider=server.provider())
        transport = CaptureOss((403, f"<Error><Code>{code}</Code></Error>".encode()), (200, b"next"))
        box._opener = transport
        with pytest.raises(Exception):
            box.get("sample.bin")
        assert len(transport.requests) == len(server.requests) == 1
        assert box.get("sample.bin") == b"next"
        assert len(server.requests) == 2
        _assert_signed(transport.requests[0], first)
        _assert_signed(transport.requests[1], second)


def test_unknown_oss_network_failure_does_not_retry_or_discard_cache(sts_server):
    with sts_server() as server:
        server.reply()
        box = OssV1Box(endpoint="https://oss.example.invalid", credentials_provider=server.provider())
        transport = CaptureOss(URLError("synthetic-disconnect"), (200, b"next"))
        box._opener = transport
        with pytest.raises(Exception):
            box.put("sample.bin", b"one-write-only")
        assert len(transport.requests) == 1
        assert box.get("sample.bin") == b"next"
        assert len(server.requests) == 1


def test_late_old_token_rejection_does_not_invalidate_new_cached_credentials(sts_server):
    with sts_server() as server:
        first, second = _credentials(), _credentials("two", expiration=NOW + 7200)
        server.reply(first)
        server.reply(second)
        provider = server.provider()

        class LateOldTokenRejection(CaptureOss):
            def open(self, request, timeout):
                if not self.requests:
                    server.clock.now = NOW + 3300
                    assert provider.get_credentials().access_key_id == second["access_key_id"]
                return super().open(request, timeout)

        transport = LateOldTokenRejection((403, b"<Error><Code>SecurityTokenExpired</Code></Error>"), (200, b"new"))
        box = OssV1Box(endpoint="https://oss.example.invalid", credentials_provider=provider)
        box._opener = transport
        with pytest.raises(Exception):
            box.get("sample.bin")
        assert len(server.requests) == 2
        assert box.get("sample.bin") == b"new"
        assert len(server.requests) == 2
        _assert_signed(transport.requests[0], first)
        _assert_signed(transport.requests[1], second)


def test_env_sts_factory_is_lazy_and_reads_secret_files(sts_server, monkeypatch, tmp_path):
    with sts_server() as server:
        key_file, sm4_file = tmp_path / "auth.txt", tmp_path / "sm4.txt"
        key_file.write_text(AUTH_KEY, encoding="utf-8")
        sm4_file.write_text(SM4_KEY.decode("ascii"), encoding="utf-8")
        for key, value in {
            "OSS_ENDPOINT": "https://oss.example.invalid",
            "OSS_STS_URL": server.url,
            "OSS_STS_AUTH_KEY_FILE": str(key_file),
            "OSS_STS_SM4_KEY_FILE": str(sm4_file),
        }.items():
            monkeypatch.setenv(key, value)
        # Factory uses wall clock; serve a synthetic credential in that time domain.
        import time
        server.clock.now = time.time()
        credentials = _credentials(expiration=server.clock() + 3600)
        server.reply(credentials)
        box = OssV1Box.from_env()
        assert server.requests == []
        box._opener = CaptureOss()
        assert box.get("sample.bin") == b"accepted"
        _assert_signed(box._opener.requests[0], credentials)
        assert len(server.requests) == 1


def test_missing_sts_configuration_never_falls_back_to_static(monkeypatch):
    for key, value in {"OSS_AUTH_MODE": "sts", "OSS_ENDPOINT": "https://oss.example.invalid", "OSS_BUCKET": BUCKET,
                       "OSS_ACCESS_KEY_ID": "legacy-key", "OSS_ACCESS_KEY_SECRET": "legacy-secret"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(Exception):
        box = OssV1Box.from_env()
        box._opener = CaptureOss()
        box.get("sample.bin")


def test_static_constructor_remains_compatible():
    box = OssV1Box(endpoint="https://oss.example.invalid", bucket=BUCKET, key_id="static-key", secret="static-secret")
    box._opener = CaptureOss()
    assert box.get("sample.bin") == b"accepted"
    _assert_signed(box._opener.requests[0], {"access_key_id": "static-key", "access_key_secret": "static-secret"})


def test_env_factory_defaults_to_legacy_static_mode(monkeypatch):
    for key, value in {"OSS_ENDPOINT": "https://oss.example.invalid", "OSS_BUCKET": BUCKET,
                       "OSS_ACCESS_KEY_ID": "legacy-key", "OSS_ACCESS_KEY_SECRET": "legacy-secret"}.items():
        monkeypatch.setenv(key, value)
    box = OssV1Box.from_env()
    box._opener = CaptureOss()
    assert box.get("sample.bin") == b"accepted"
    _assert_signed(box._opener.requests[0], {"access_key_id": "legacy-key", "access_key_secret": "legacy-secret"})
