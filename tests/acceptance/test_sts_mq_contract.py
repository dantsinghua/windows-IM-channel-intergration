"""MQ STS 认证边界：合成凭据、受控回环 HTTP，不访问实际服务。

服务端依据 its-accesspoint ac26e9ed72f816e2a19b9e1d01c9572bd8d214a4。
真实 Go Verify 交叉验证单列于 sts-mq-cross-language evidence，避免修改验收
宿主命令护栏；本文件只验证公开构造、请求和环境配置合同。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError

import pytest

from qtrade_agent.ossbox.oss_http import OssV1Box
from qtrade_agent.ossbox.sts import StsCredentialProvider, build_access_token


AUTH_KEY = "mq-acceptance-only-认证口令"
SM4_KEY = b"fedcba9876543210"
NOW = 1_760_400_000.25
MQ_URL = "http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts"


def credentials():
    return {
        "access_key_id": "STS.synthetic-mq",
        "access_key_secret": "synthetic-mq-access-secret",
        "security_token": "synthetic-mq-security-token",
        "expiration": datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat(),
        "bucket": "synthetic-mq-bucket",
    }


class Response(io.BytesIO):
    status = 200


class Capture:
    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return Response(json.dumps(credentials()).encode())


@pytest.fixture(autouse=True)
def network_boundary(monkeypatch):
    allowed, violations = set(), []
    connect, resolve = socket.socket.connect, socket.getaddrinfo
    for name in list(os.environ):
        if name.startswith("OSS_") or name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name, raising=False)

    def check(address):
        if address not in allowed:
            violations.append(address)
            raise AssertionError("MQ acceptance attempted unowned network access")

    def guarded_connect(sock, address):
        check(address)
        return connect(sock, address)

    def guarded_resolve(host, port, *args, **kwargs):
        check((host, int(port)))
        return resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
    yield allowed
    assert not violations, "unowned network access was attempted"


def set_env(monkeypatch, option=None):
    for name, value in {
        "OSS_AUTH_MODE": "sts",
        "OSS_ENDPOINT": "https://oss.example.invalid",
        "OSS_STS_URL": MQ_URL,
        "OSS_STS_AUTH_KEY": AUTH_KEY,
        "OSS_STS_SM4_KEY": SM4_KEY.decode(),
    }.items():
        monkeypatch.setenv(name, value)
    if option is not None:
        monkeypatch.setenv("OSS_STS_ALLOW_HTTP", option)


def test_mq_plain_http_requires_explicit_opt_in_before_transport():
    capture = Capture()
    with pytest.raises(ValueError, match="^sts_url_invalid$"):
        StsCredentialProvider(MQ_URL, AUTH_KEY, SM4_KEY, opener=capture)
    assert capture.requests == []


def test_mq_opt_in_keeps_exact_gateway_path_and_token_only_in_header():
    capture = Capture()
    provider = StsCredentialProvider(MQ_URL, AUTH_KEY, SM4_KEY, opener=capture,
                                     clock=lambda: NOW, allow_http=True)
    assert capture.requests == []
    result = provider.get_credentials()
    assert result.bucket == "synthetic-mq-bucket"
    assert len(capture.requests) == 1
    request, timeout = capture.requests[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    token = headers["access_token"]
    assert request.full_url == MQ_URL
    assert request.get_method() == "GET"
    assert request.data is None
    assert headers["accept"] == "application/json"
    assert token not in request.full_url and AUTH_KEY not in request.full_url
    assert len(base64.b64decode(token, validate=True)) % 16 == 0
    assert timeout == 10
    assert result is provider.get_credentials()
    assert len(capture.requests) == 1


@pytest.mark.parametrize("option", [None, "false", " FALSE "])
def test_env_missing_or_false_http_opt_in_rejects_mq(monkeypatch, option):
    set_env(monkeypatch, option)
    with pytest.raises(ValueError, match="^sts_url_invalid$"):
        OssV1Box.from_env()


@pytest.mark.parametrize("option", ["true", "TRUE", " true "])
def test_env_true_constructs_lazily_without_fetch(monkeypatch, option):
    set_env(monkeypatch, option)
    assert isinstance(OssV1Box.from_env(), OssV1Box)


@pytest.mark.parametrize("option", ["", "1", "0", "yes", "on", "false true", "garbage"])
def test_env_ambiguous_http_options_rejected(monkeypatch, option):
    set_env(monkeypatch, option)
    with pytest.raises(ValueError, match="^sts_http_option_invalid$"):
        OssV1Box.from_env()


@pytest.mark.parametrize("option", [None, "true", "false", 0, 1, [], {}])
def test_constructor_http_option_requires_actual_boolean(option):
    with pytest.raises(ValueError, match="^sts_http_option_invalid$"):
        StsCredentialProvider(MQ_URL, AUTH_KEY, SM4_KEY, allow_http=option)


@pytest.mark.parametrize("url", [
    "http://user:secret-canary@mq.example.invalid/api/sts",
    "http://mq.example.invalid/api/sts?access_token=secret-canary",
    "http://mq.example.invalid/api/sts#secret-canary",
    "http://mq.example.invalid:bad-port/api/sts",
    "file:///secret-canary",
    "http://mq.example.invalid/api/sts\nsecret-canary",
])
def test_http_opt_in_does_not_relax_credential_bearing_url_validation(url, caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as error:
        StsCredentialProvider(url, AUTH_KEY, SM4_KEY, allow_http=True)
    assert str(error.value) == "sts_url_invalid"
    assert "secret-canary" not in caplog.text
    assert AUTH_KEY not in caplog.text


@pytest.mark.parametrize("kind", ["401", "502", "network"])
def test_mq_transport_errors_expose_only_fixed_codes(kind, caplog):
    marker = "request-and-response-secret-canary"
    if kind == "network":
        underlying = URLError(marker)
        code = "sts_unavailable"
    else:
        underlying = HTTPError(MQ_URL + "?" + marker, int(kind), marker, {}, io.BytesIO(marker.encode()))
        code = f"sts_http_{kind}"
    provider = StsCredentialProvider(MQ_URL, AUTH_KEY, SM4_KEY, opener=Capture(underlying),
                                     clock=lambda: NOW, allow_http=True)
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as error:
        provider.get_credentials()
    assert str(error.value) == code
    assert error.value.__suppress_context__ is True
    assert marker not in caplog.text and AUTH_KEY not in caplog.text


@pytest.fixture
def redirect_server(network_boundary):
    @contextmanager
    def start(code):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("access_token")))
                self.send_response(code)
                self.send_header("Location", "http://redirect-target.example.invalid/api/sts")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        network_boundary.add(server.server_address)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/its-accesspoint/v1/api/sts", requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()
    return start


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_http_opt_in_never_forwards_auth_header_to_redirect_target(redirect_server, code):
    with redirect_server(code) as (url, requests):
        provider = StsCredentialProvider(url, AUTH_KEY, SM4_KEY, clock=lambda: NOW, allow_http=True)
        with pytest.raises(Exception, match=f"^sts_http_{code}$"):
            provider.get_credentials()
        assert len(requests) == 1
        assert requests[0][0] == "/its-accesspoint/v1/api/sts"
        assert requests[0][1]


def test_random_iv_changes_each_same_second_token():
    tokens = [build_access_token(AUTH_KEY, SM4_KEY, now=NOW) for _ in range(32)]
    assert len(set(tokens)) == 32
    ivs = {base64.b64decode(token, validate=True)[:16] for token in tokens}
    assert len(ivs) == 32

