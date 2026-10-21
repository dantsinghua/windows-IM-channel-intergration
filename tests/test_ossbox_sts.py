"""STS 接入开发者回归：纯合成凭据、可拨时钟与假 HTTP，禁止真实外网。"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import io
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from email.message import Message as EmailHeaders
from http.client import BadStatusLine
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, OpenerDirector
from urllib.response import addinfourl

import pytest

from qtrade_agent.ossbox.catalog_publish import CatalogPublisher
from qtrade_agent.ossbox.inbox import XunjiaInbox
from qtrade_agent.ossbox.oss_http import OssV1Box

_AUTH_KEY = "fixture-business-secret"
_SM4_KEY = b"0123456789abcdef"
_NOW = 1_760_260_320.75
_STS_URL = "https://nytoken.yingmi.cn/api/sts"
_URLLIB_OPEN = OpenerDirector.open


class FakeClock:
    def __init__(self, now=_NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, body, *, status=200):
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.stream = io.BytesIO(self.body)
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.stream.read(size)


class FakeOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        assert 0 < timeout <= 30
        assert self.results, "发生未预期的额外 HTTP 请求"
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _credentials(*, expiration=_NOW + 3600, **overrides):
    body = {
        "access_key_id": "STS.fixture-id",
        "access_key_secret": "fixture-oss-secret",
        "security_token": "fixture-security-token",
        "expiration": datetime.fromtimestamp(expiration, timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(expiration, (int, float)) else expiration,
        "bucket": "fixture-bucket",
    }
    body.update(overrides)
    return body


def _provider(opener, clock=None, **kwargs):
    return _sts().StsCredentialProvider(
        _STS_URL, _AUTH_KEY, _SM4_KEY, opener=opener,
        clock=clock or FakeClock(), **kwargs,
    )


def _headers(request):
    return {key.lower(): value for key, value in request.header_items()}


def _decrypt_token(token):
    # 独立使用底层密码库解密，不调用产品解码函数作为期望值。
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    raw = base64.b64decode(token, validate=True)
    assert len(raw) > 16 and (len(raw) - 16) % 16 == 0
    cipher = Cipher(algorithms.SM4(_SM4_KEY), modes.CBC(raw[:16])).decryptor()
    padded = cipher.update(raw[16:]) + cipher.finalize()
    unpadder = PKCS7(128).unpadder()
    return json.loads(unpadder.update(padded) + unpadder.finalize())


@pytest.fixture(autouse=True)
def isolated_oss(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("OSS_", "OSSBOX_")):
            monkeypatch.delenv(key)

    def deny_network(*args, **kwargs):
        pytest.fail("STS 回归必须注入假 HTTP，不得访问真实网络")

    monkeypatch.setattr(OpenerDirector, "open", deny_network)


def _sts():
    name = "qtrade_agent.ossbox.sts"
    assert importlib.util.find_spec(name) is not None, "缺少 STS 协议适配模块"
    return importlib.import_module(name)


def _configure_sts(monkeypatch):
    for name, value in {
        "OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
        "OSS_BUCKET": "qtrade-nyoss-lab",
        "OSS_AUTH_MODE": "sts",
        "OSS_STS_URL": _STS_URL,
        "OSS_STS_AUTH_KEY": "fixture-business-secret",
        "OSS_STS_SM4_KEY": "0123456789abcdef",
        "OSS_WORKBENCH_ID": "fixture-workbench",
        "OSS_SEAL_KEY_DEPOSIT": "11" * 32,
        "OSS_SEAL_KEY_XUNJIA": "22" * 32,
    }.items():
        monkeypatch.setenv(name, value)


def test_sts_provider_and_token_builder_are_available():
    module = _sts()
    assert callable(getattr(module, "build_access_token", None))
    assert callable(getattr(module, "StsCredentialProvider", None))


def test_static_temporary_credentials_sign_security_token_header(monkeypatch):
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"fixture-object"

    class Opener:
        def open(self, request, *, timeout):
            captured.append(request)
            return Response()

    box = OssV1Box(
        endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="fixture-bucket",
        key_id="STS.fixture", secret="fixture-oss-secret", security_token="fixture-security-token",
    )
    monkeypatch.setattr(box, "_opener", Opener())
    assert box.get("wb/test/in/xunjia/cmd.bin") == b"fixture-object"
    headers = {name.lower(): value for name, value in captured[0].header_items()}
    assert headers["x-oss-security-token"] == "fixture-security-token"
    canonical = (
        f"GET\n\n\n{headers['date']}\n"
        "x-oss-security-token:fixture-security-token\n"
        "/fixture-bucket/wb/test/in/xunjia/cmd.bin"
    )
    expected = base64.b64encode(hmac.new(b"fixture-oss-secret", canonical.encode(), hashlib.sha1).digest()).decode()
    assert headers["authorization"] == f"OSS STS.fixture:{expected}"


@pytest.mark.parametrize("consumer", ["inbox", "catalog"])
def test_sts_consumers_assemble_without_static_keys_or_network(monkeypatch, tmp_path, consumer):
    _configure_sts(monkeypatch)
    if consumer == "inbox":
        result = XunjiaInbox.from_env(object(), lambda *_: False)
    else:
        result = CatalogPublisher.from_env(object(), str(tmp_path / "catalog-state.json"))
    assert result is not None, "STS 配置完整时只需要独立OSS_BUCKET，不需要静态AK/SK"
    assert isinstance(result.box, OssV1Box)


def test_access_token_matches_documented_sm4_cbc_and_string_seconds():
    build = _sts().build_access_token
    first = build(_AUTH_KEY, _SM4_KEY, now=_NOW)
    second = build(_AUTH_KEY, _SM4_KEY, now=_NOW)
    assert first != second, "同一明文必须使用不同的随机 IV"
    assert _decrypt_token(first) == {"key": _AUTH_KEY, "ts": "1760260320"}
    assert _decrypt_token(second) == {"key": _AUTH_KEY, "ts": "1760260320"}
    assert _AUTH_KEY not in first


def test_access_token_preserves_unicode_business_key():
    token = _sts().build_access_token("仅测试的业务口令", _SM4_KEY, now=_NOW)
    assert _decrypt_token(token) == {"key": "仅测试的业务口令", "ts": "1760260320"}


@pytest.mark.parametrize("bad_key", [b"", b"short-key", b"x" * 15, b"x" * 17, b"x" * 32])
def test_access_token_rejects_non_16_byte_sm4_key(bad_key):
    with pytest.raises((ValueError, RuntimeError)):
        _sts().build_access_token(_AUTH_KEY, bad_key, now=_NOW)


def test_provider_makes_get_with_fresh_token_and_parses_credentials():
    clock = FakeClock()
    opener = FakeOpener(FakeResponse(_credentials()))
    provider = _provider(opener, clock)
    credential = provider.get_credentials()
    request = opener.requests[0]
    assert request.full_url == _STS_URL
    assert request.get_method() == "GET" and request.data is None
    assert _decrypt_token(_headers(request)["access_token"]) == {"key": _AUTH_KEY, "ts": str(int(clock.now))}
    assert credential.access_key_id == "STS.fixture-id"
    assert credential.access_key_secret == "fixture-oss-secret"
    assert credential.security_token == "fixture-security-token"
    assert credential.expiration == _NOW + 3600
    assert credential.bucket == ""
    for secret in (_AUTH_KEY, _SM4_KEY.decode(), "fixture-oss-secret", "fixture-security-token"):
        assert secret not in repr(provider)
        assert secret not in repr(credential)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        credential.security_token = "changed"


def test_provider_caches_until_exact_five_minute_refresh_boundary():
    clock = FakeClock()
    opener = FakeOpener(
        FakeResponse(_credentials(expiration=_NOW + 600)),
        FakeResponse(_credentials(expiration=_NOW + 4000, access_key_id="STS.refreshed")),
    )
    provider = _provider(opener, clock)
    first = provider.get_credentials()
    clock.now = _NOW + 299.999
    assert provider.get_credentials() == first
    assert len(opener.requests) == 1
    clock.now = _NOW + 300
    assert provider.get_credentials().access_key_id == "STS.refreshed"
    assert len(opener.requests) == 2
    tokens = [_headers(request)["access_token"] for request in opener.requests]
    assert tokens[0] != tokens[1]
    assert _decrypt_token(tokens[1])["ts"] == str(int(clock.now))


@pytest.mark.parametrize("refresh", [False, True])
def test_concurrent_callers_share_one_credential_fetch(refresh):
    clock = FakeClock()
    opener = FakeOpener(FakeResponse(_credentials()))
    provider = _provider(opener, clock)
    if refresh:
        provider.get_credentials()
        clock.now += 3300
        opener.results.append(FakeResponse(_credentials(expiration=clock.now + 3600)))
    barrier = threading.Barrier(8)

    def fetch():
        barrier.wait(timeout=5)
        return provider.get_credentials()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch) for _ in range(8)]
        result = [future.result(timeout=10) for future in futures]
    assert len(opener.requests) == 1 + int(refresh)
    assert all(item == result[0] for item in result)


@pytest.mark.parametrize("failure", ["502", "network"])
def test_temporary_refresh_failure_reuses_still_safe_cache_with_backoff(failure):
    clock = FakeClock()
    error = HTTPError(_STS_URL, 502, "upstream failure", {}, io.BytesIO(b"upstream-secret")) if failure == "502" else URLError("fixture-network-error")
    opener = FakeOpener(FakeResponse(_credentials(expiration=_NOW + 600)), error)
    provider = _provider(opener, clock)
    first = provider.get_credentials()
    clock.now += 300
    assert provider.get_credentials() == first
    assert provider.get_credentials() == first
    assert len(opener.requests) == 2, "刷新失败短退避期间不得每次都请求 STS"


def test_refresh_failure_does_not_return_cache_with_only_thirty_seconds_left():
    clock = FakeClock()
    opener = FakeOpener(FakeResponse(_credentials(expiration=_NOW + 600)), URLError("fixture-network-error"))
    provider = _provider(opener, clock)
    provider.get_credentials()
    clock.now += 570
    with pytest.raises((RuntimeError, ValueError)):
        provider.get_credentials()


@pytest.mark.parametrize("failure", ["401", "malformed"])
def test_authorization_or_schema_failure_never_falls_back_to_cached_credentials(failure):
    clock = FakeClock()
    bad = {
        "401": HTTPError(_STS_URL, 401, "unauthorized", {}, io.BytesIO(b"fixture-server-secret")),
        "malformed": FakeResponse(b"not-json"),
        "bucket": FakeResponse(_credentials(bucket="other-bucket")),
    }[failure]
    opener = FakeOpener(FakeResponse(_credentials(expiration=_NOW + 600)), bad)
    provider = _provider(opener, clock, expected_bucket="fixture-bucket")
    provider.get_credentials()
    clock.now += 300
    with pytest.raises((RuntimeError, ValueError)):
        provider.get_credentials()


@pytest.mark.parametrize("field", ["access_key_id", "access_key_secret", "security_token", "expiration"])
@pytest.mark.parametrize("value", [None, "", 123])
def test_response_rejects_missing_empty_or_non_string_required_fields(field, value):
    body = _credentials(**{field: value})
    if value is None:
        del body[field]
    with pytest.raises((RuntimeError, ValueError)):
        _provider(FakeOpener(FakeResponse(body))).get_credentials()


@pytest.mark.parametrize("expiration", ["2026-10-13T12:00:00", "not-a-timestamp", "2020-01-01T00:00:00Z"])
def test_response_rejects_naive_malformed_or_expired_time(expiration):
    with pytest.raises((RuntimeError, ValueError)):
        _provider(FakeOpener(FakeResponse(_credentials(expiration=expiration)))).get_credentials()


def test_response_size_is_bounded_before_json_parse():
    response = FakeResponse(b" " * 65537)
    with pytest.raises((RuntimeError, ValueError)):
        _provider(FakeOpener(response)).get_credentials()
    assert response.read_sizes and all(0 < size <= 65537 for size in response.read_sizes)


@pytest.mark.parametrize("error_kind", ["http", "network", "malformed"])
def test_server_and_transport_errors_never_leak_secret_text(error_kind, caplog):
    secret = "fixture-private-upstream-message"
    error = {
        "http": HTTPError(_STS_URL, 401, secret, {}, io.BytesIO(json.dumps({"message": secret}).encode())),
        "network": URLError(secret),
        "malformed": FakeResponse(secret.encode()),
    }[error_kind]
    with pytest.raises((RuntimeError, ValueError)) as caught:
        _provider(FakeOpener(error)).get_credentials()
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    for forbidden in (secret, _AUTH_KEY, _SM4_KEY.decode()):
        assert forbidden not in rendered


@pytest.mark.parametrize("url", ["http://nytoken.yingmi.cn/api/sts", "ftp://127.0.0.1/api/sts", "https://user:password@example.test/api/sts"])
def test_provider_rejects_insecure_or_userinfo_urls_before_io(url):
    opener = FakeOpener()
    with pytest.raises((RuntimeError, ValueError)):
        _sts().StsCredentialProvider(url, _AUTH_KEY, _SM4_KEY, opener=opener)
    assert not opener.requests


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/api/sts", "http://[::1]:8080/api/sts"])
def test_provider_allows_explicit_loopback_http_for_fake_service(url):
    opener = FakeOpener(FakeResponse(_credentials()))
    result = _sts().StsCredentialProvider(url, _AUTH_KEY, _SM4_KEY, opener=opener, clock=FakeClock()).get_credentials()
    assert result.bucket == ""
    assert opener.requests[0].full_url == url


@pytest.mark.parametrize("consumer", ["inbox", "catalog"])
def test_consumers_reuse_supplied_box_without_fetching_credentials(monkeypatch, tmp_path, consumer):
    _configure_sts(monkeypatch)
    shared_box = object()
    if consumer == "inbox":
        result = XunjiaInbox.from_env(object(), lambda *_: False, box=shared_box)
    else:
        result = CatalogPublisher.from_env(object(), str(tmp_path / "state.json"), box=shared_box)
    assert result is not None and result.box is shared_box


@pytest.mark.parametrize("status", [302, 303, 307, 308])
def test_default_transport_never_forwards_access_token_to_redirect_target(monkeypatch, status):
    requests = []

    class FakeHttps(HTTPSHandler):
        handler_order = 100

        def https_open(self, request):
            requests.append(request)
            headers = EmailHeaders()
            headers["Location"] = "https://other-host.invalid/steal-token"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, status)
            response.msg = "Found"
            return response

    provider = _sts().StsCredentialProvider(_STS_URL, _AUTH_KEY, _SM4_KEY, clock=FakeClock())
    provider._opener.add_handler(FakeHttps())
    monkeypatch.setattr(provider._opener, "open", _URLLIB_OPEN.__get__(provider._opener))
    with pytest.raises((RuntimeError, ValueError)):
        provider.get_credentials()
    assert len(requests) == 1
    assert requests[0].full_url == _STS_URL


def test_response_that_expires_during_request_is_rejected():
    clock = FakeClock()

    class SlowOpener:
        def open(self, request, *, timeout):
            clock.now += 700
            return FakeResponse(_credentials(expiration=_NOW + 600))

    with pytest.raises((RuntimeError, ValueError)):
        _provider(SlowOpener(), clock).get_credentials()


def _factory_transports(monkeypatch):
    sts_module = _sts()
    oss_module = importlib.import_module("qtrade_agent.ossbox.oss_http")
    sts = FakeOpener(FakeResponse(_credentials(expiration=time.time() + 3600)))
    oss = FakeOpener(FakeResponse(b"object"))
    monkeypatch.setattr(sts_module, "build_opener", lambda *_: sts)
    monkeypatch.setattr(oss_module, "build_opener", lambda *_: oss)
    return sts, oss


@pytest.mark.parametrize("explicit_mode", [True, False])
def test_sts_factory_uses_explicit_url_and_server_bucket_lazily(monkeypatch, explicit_mode):
    _configure_sts(monkeypatch)
    if not explicit_mode:
        monkeypatch.delenv("OSS_AUTH_MODE")
    sts, oss = _factory_transports(monkeypatch)
    box = OssV1Box.from_env()
    assert box is not None
    assert not sts.requests and not oss.requests, "只装配不能探测尚未上线的服务"
    assert box.get("fixture.bin") == b"object"
    assert sts.requests[0].full_url == _STS_URL
    assert oss.requests[0].full_url == "https://qtrade-nyoss-lab.oss-cn-hangzhou.aliyuncs.com/fixture.bin"


@pytest.mark.parametrize("url", [None, "", "   "])
def test_sts_factory_without_explicit_url_fails_closed_without_network_or_static_fallback(monkeypatch, url):
    _configure_sts(monkeypatch)
    if url is None:
        monkeypatch.delenv("OSS_STS_URL")
    else:
        monkeypatch.setenv("OSS_STS_URL", url)
    monkeypatch.setenv("OSS_BUCKET", "legacy-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "legacy-key-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "legacy-secret")
    sts, oss = _factory_transports(monkeypatch)
    with pytest.raises(ValueError, match="sts_url"):
        OssV1Box.from_env()
    assert not sts.requests and not oss.requests


def test_sts_factory_reads_secret_files_and_preserves_business_key_whitespace(monkeypatch, tmp_path):
    _configure_sts(monkeypatch)
    auth = tmp_path / "auth.txt"
    sm4 = tmp_path / "sm4.txt"
    auth.write_bytes(b"  fixture-business-secret  \r\n")
    sm4.write_bytes(_SM4_KEY + b"\n")
    monkeypatch.delenv("OSS_STS_AUTH_KEY")
    monkeypatch.delenv("OSS_STS_SM4_KEY")
    monkeypatch.setenv("OSS_STS_AUTH_KEY_FILE", str(auth))
    monkeypatch.setenv("OSS_STS_SM4_KEY_FILE", str(sm4))
    monkeypatch.setenv("OSS_STS_URL", "http://127.0.0.1:8080/api/sts")
    sts, _ = _factory_transports(monkeypatch)
    box = OssV1Box.from_env()
    assert box is not None and box.get("fixture.bin") == b"object"
    assert sts.requests[0].full_url == "http://127.0.0.1:8080/api/sts"
    assert _decrypt_token(_headers(sts.requests[0])["access_token"])["key"] == "  fixture-business-secret  "


@pytest.mark.parametrize("field", ["OSS_STS_AUTH_KEY", "OSS_STS_SM4_KEY"])
def test_sts_factory_rejects_ambiguous_inline_and_file_secret(monkeypatch, tmp_path, field):
    _configure_sts(monkeypatch)
    source = tmp_path / "fixture-secret.txt"
    source.write_text("0123456789abcdef")
    monkeypatch.setenv(field + "_FILE", str(source))
    with pytest.raises((RuntimeError, ValueError)):
        OssV1Box.from_env()


@pytest.mark.parametrize("field", ["OSS_STS_AUTH_KEY", "OSS_STS_SM4_KEY"])
def test_broken_sts_configuration_never_falls_back_to_legacy_static_keys(monkeypatch, field):
    _configure_sts(monkeypatch)
    monkeypatch.delenv(field)
    monkeypatch.setenv("OSS_BUCKET", "legacy-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "legacy-key-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "legacy-secret")
    with pytest.raises((RuntimeError, ValueError)):
        OssV1Box.from_env()


def test_factory_without_any_oss_configuration_remains_disabled():
    assert OssV1Box.from_env() is None


def test_legacy_static_factory_keeps_original_signing_contract(monkeypatch):
    for name, value in {
        "OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com",
        "OSS_BUCKET": "fixture-bucket",
        "OSS_ACCESS_KEY_ID": "legacy-key-id", "OSS_ACCESS_KEY_SECRET": "legacy-secret",
    }.items():
        monkeypatch.setenv(name, value)
    oss_module = importlib.import_module("qtrade_agent.ossbox.oss_http")
    opener = FakeOpener(FakeResponse(b"legacy-object"))
    monkeypatch.setattr(oss_module, "build_opener", lambda *_: opener)
    box = OssV1Box.from_env()
    assert box is not None and box.get("fixture.bin") == b"legacy-object"
    headers = _headers(opener.requests[0])
    assert "x-oss-security-token" not in headers
    canonical = f"GET\n\n\n{headers['date']}\n/fixture-bucket/fixture.bin"
    signature = base64.b64encode(hmac.new(b"legacy-secret", canonical.encode(), hashlib.sha1).digest()).decode()
    assert headers["authorization"] == f"OSS legacy-key-id:{signature}"


def test_oss_requests_take_key_secret_and_security_token_from_same_refresh(monkeypatch):
    clock = FakeClock()
    sts = FakeOpener(
        FakeResponse(_credentials(expiration=_NOW + 600)),
        FakeResponse(_credentials(expiration=_NOW + 4000, access_key_id="STS.new", access_key_secret="new-secret", security_token="new-token")),
    )
    provider = _provider(sts, clock)
    box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="qtrade-nyoss-lab", credentials_provider=provider)
    oss = FakeOpener(FakeResponse(b"first"), FakeResponse(b"second"))
    monkeypatch.setattr(box, "_opener", oss)
    assert box.get("fixture.bin") == b"first"
    clock.now += 300
    assert box.get("fixture.bin") == b"second"
    for request, key_id, secret, token in zip(oss.requests, ["STS.fixture-id", "STS.new"], ["fixture-oss-secret", "new-secret"], ["fixture-security-token", "new-token"]):
        headers = _headers(request)
        assert headers["x-oss-security-token"] == token
        canonical = f"GET\n\n\n{headers['date']}\nx-oss-security-token:{token}\n/qtrade-nyoss-lab/fixture.bin"
        signature = base64.b64encode(hmac.new(secret.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
        assert headers["authorization"] == f"OSS {key_id}:{signature}"


def test_oss_credential_rejection_does_not_retry_write_but_next_call_refreshes(monkeypatch):
    sts = FakeOpener(
        FakeResponse(_credentials()),
        FakeResponse(_credentials(access_key_id="STS.refreshed")),
    )
    provider = _provider(sts)
    box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="qtrade-nyoss-lab", credentials_provider=provider)
    error = HTTPError("https://fixture.invalid/object", 403, "Forbidden", {}, io.BytesIO(b"<Error><Code>SecurityTokenExpired</Code></Error>"))
    oss = FakeOpener(error, FakeResponse(b""))
    monkeypatch.setattr(box, "_opener", oss)
    with pytest.raises(RuntimeError):
        box.put("fixture.bin", b"fixture-payload")
    assert len(oss.requests) == 1 and len(sts.requests) == 1
    box.put("fixture.bin", b"fixture-payload")
    assert len(oss.requests) == 2 and len(sts.requests) == 2
    assert _headers(oss.requests[-1])["authorization"].startswith("OSS STS.refreshed:")


def test_oss_network_write_failure_is_not_retried_or_leaked(monkeypatch):
    secret = "fixture-private-signed-request"
    box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="fixture-bucket", key_id="fixture-id", secret="fixture-secret")
    opener = FakeOpener(URLError(secret))
    monkeypatch.setattr(box, "_opener", opener)
    with pytest.raises(RuntimeError) as caught:
        box.put("fixture.bin", b"fixture-payload")
    assert len(opener.requests) == 1
    assert secret not in "".join(traceback.format_exception(caught.value))


async def test_inbox_slow_object_listing_keeps_event_loop_responsive():
    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    worker_threads = []

    class SlowBox:
        def list_prefix(self, prefix):
            worker_threads.append(threading.get_ident())
            started.set()
            assert release.wait(timeout=3), "事件循环未能在 OSS 等待期间释放假请求"
            return []

    inbox = XunjiaInbox(SlowBox(), b"x" * 32, "fixture-workbench", lambda *_: False, object())
    pending = asyncio.create_task(inbox.poll())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
        assert not pending.done(), "请求未释放前轮询必须仍在等待"
        assert len(worker_threads) == 1 and worker_threads[0] != loop_thread
    finally:
        release.set()
        await asyncio.wait_for(pending, timeout=3)


def test_old_request_invalidation_does_not_discard_newly_refreshed_credentials():
    clock = FakeClock()
    opener = FakeOpener(
        FakeResponse(_credentials(expiration=_NOW + 600)),
        FakeResponse(_credentials(expiration=_NOW + 4000, access_key_id="STS.refreshed")),
    )
    provider = _provider(opener, clock)
    old = provider.get_credentials()
    clock.now += 300
    new = provider.get_credentials()
    provider.invalidate(old)
    assert provider.get_credentials() == new
    assert len(opener.requests) == 2, "旧请求的迟到 403 不得废弃已刷新的凭据"


def test_generic_oss_access_denied_does_not_force_credential_refresh(monkeypatch):
    sts = FakeOpener(FakeResponse(_credentials()))
    provider = _provider(sts)
    box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="qtrade-nyoss-lab", credentials_provider=provider)
    error = HTTPError("https://fixture.invalid/object", 403, "Forbidden", {}, io.BytesIO(b"<Error><Code>AccessDenied</Code></Error>"))
    oss = FakeOpener(error, FakeResponse(b"object"))
    monkeypatch.setattr(box, "_opener", oss)
    with pytest.raises(RuntimeError):
        box.get("not-allowed.bin")
    assert box.get("allowed.bin") == b"object"
    assert len(sts.requests) == 1


def test_provider_pins_first_server_bucket_across_refreshes():
    clock = FakeClock()
    opener = FakeOpener(
        FakeResponse(_credentials(expiration=_NOW + 600)),
        FakeResponse(_credentials(bucket="other-bucket")),
    )
    provider = _provider(opener, clock)
    provider.get_credentials()
    clock.now += 300
    assert provider.get_credentials().bucket == ""


def test_transient_backoff_can_refresh_after_short_recovery_window():
    clock = FakeClock()
    opener = FakeOpener(
        FakeResponse(_credentials(expiration=_NOW + 600)),
        URLError("fixture-network-error"),
        FakeResponse(_credentials(expiration=_NOW + 4000, access_key_id="STS.recovered")),
    )
    provider = _provider(opener, clock)
    first = provider.get_credentials()
    clock.now += 300
    assert provider.get_credentials() == first
    clock.now += 30
    assert provider.get_credentials().access_key_id == "STS.recovered"
    assert len(opener.requests) == 3


@pytest.mark.parametrize("field", ["access_key_id", "access_key_secret", "security_token"])
def test_response_rejects_header_injection_values(field):
    body = _credentials(**{field: "fixture\r\nX-Injected: unsafe"})
    with pytest.raises((RuntimeError, ValueError)):
        _provider(FakeOpener(FakeResponse(body))).get_credentials()


@pytest.mark.parametrize("kind", ["missing", "invalid-utf8", "oversized"])
def test_secret_file_errors_are_redacted_without_static_fallback(monkeypatch, tmp_path, kind):
    _configure_sts(monkeypatch)
    monkeypatch.delenv("OSS_STS_AUTH_KEY")
    path = tmp_path / "fixture-auth.txt"
    if kind == "invalid-utf8":
        path.write_bytes(b"\xff\xfe")
    elif kind == "oversized":
        path.write_bytes(b"fixture-private-file-content" * 200)
    monkeypatch.setenv("OSS_STS_AUTH_KEY_FILE", str(path))
    monkeypatch.setenv("OSS_BUCKET", "legacy-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "legacy-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "legacy-secret")
    with pytest.raises((ValueError, RuntimeError)) as caught:
        OssV1Box.from_env()
    rendered = "".join(traceback.format_exception(caught.value))
    assert "fixture-private-file-content" not in rendered
    assert "legacy-secret" not in rendered


async def test_catalog_snapshot_stays_on_store_thread_while_upload_yields(tmp_path):
    loop_thread = threading.get_ident()
    started, release = threading.Event(), threading.Event()
    store_calls = []

    class ThreadBoundStore:
        def list_accounts(self):
            assert threading.get_ident() == loop_thread
            store_calls.append("accounts")
            return [{"id": "wx-fixture", "channel": "wechat", "self_uid": "wxid_fixture"}]

        def list_sessions(self, **kwargs):
            assert threading.get_ident() == loop_thread
            store_calls.append("sessions")
            return []

    class SlowBox:
        def put(self, name, body):
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(timeout=3), "目录上传阻塞了释放请求的事件循环"

    publisher = CatalogPublisher(SlowBox(), ThreadBoundStore(), "fixture-workbench", b"d" * 32, b"x" * 32, str(tmp_path / "catalog.json"))
    publish = getattr(publisher, "publish", None)
    assert callable(publish), "Agent 定时发布需要可 await 的非阻塞入口"
    pending = asyncio.create_task(publish())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 2), timeout=3)
        assert not pending.done()
        assert store_calls == ["accounts", "sessions"]
    finally:
        release.set()
        await asyncio.wait_for(pending, timeout=3)


@pytest.mark.parametrize("target", ["sts", "oss"])
@pytest.mark.parametrize("phase", ["open", "read"])
def test_protocol_errors_are_fixed_and_redacted(monkeypatch, caplog, target, phase):
    secret = "fixture-secret-echo"

    class BrokenResponse(FakeResponse):
        def read(self, size=-1):
            raise BadStatusLine(secret)

    result = BadStatusLine(secret) if phase == "open" else BrokenResponse(b"")
    opener = FakeOpener(result)
    if target == "sts":
        operation = _provider(opener).get_credentials
    else:
        box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="fixture-bucket", key_id="fixture-id", secret="fixture-secret")
        monkeypatch.setattr(box, "_opener", opener)
        operation = lambda: box.get("fixture.bin")
    with pytest.raises(RuntimeError) as caught:
        operation()
    assert secret not in "".join(traceback.format_exception(caught.value)) + caplog.text
    assert len(opener.requests) == 1


class BlockingObjectBox:
    """控制真实工作线程的退出，不用睡眠猜测 HTTP 是否已结束。"""

    def __init__(self, block_on):
        self.block_on = block_on
        self.started = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.calls = []
        self.active = 0
        self.lock = threading.Lock()

    def _enter(self, operation):
        self.calls.append(operation)
        if operation != self.block_on:
            return
        with self.lock:
            self.active += 1
        self.started.set()
        try:
            assert self.release.wait(timeout=5), "测试必须释放本次创建的假 HTTP 线程"
        finally:
            with self.lock:
                self.active -= 1
                if self.active == 0:
                    self.exited.set()

    def list_prefix(self, prefix):
        self._enter("list")
        return [] if self.block_on == "list" else [prefix + "cmd_fixture.bin"]

    def get(self, name):
        self._enter("get")
        return None if "/ack/" in name else b"fixture-invalid-envelope"

    def put(self, name, body):
        self._enter("put")


async def _assert_cancelled_io_stays_owned(operation, box, *, before_recovery=None):
    first = asyncio.create_task(operation())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(box.started.wait, 2), timeout=3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(first, timeout=0.02)
        assert not box.exited.is_set(), "协程超时不应冒充线程已退出"
        initial_calls = list(box.calls)
        try:
            await asyncio.wait_for(operation(), timeout=0.2)
        except RuntimeError as error:
            assert str(error) == "oss_io_in_progress"
        except TimeoutError:
            pytest.fail("上一轮线程未退出，下一轮又派发了阻塞 IO")
        assert box.calls == initial_calls, "上一轮取消后仍在执行时，不得再派发任何对象 IO"
    finally:
        box.release.set()
        await asyncio.wait_for(asyncio.to_thread(box.exited.wait, 2), timeout=3)
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    if before_recovery is not None:
        before_recovery()
    completed_calls = len(box.calls)

    async def wait_for_recovery():
        # 等待线程完成回调被事件循环消费；仅允许明确定义的 busy，不重试其它错误。
        while len(box.calls) == completed_calls:
            try:
                await operation()
            except RuntimeError as error:
                assert str(error) == "oss_io_in_progress"
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_recovery(), timeout=2)
    assert len(box.calls) > completed_calls, "底层线程退出后必须可以继续下一轮"


@pytest.mark.parametrize("block_on", ["list", "get", "put"])
async def test_cancelled_inbox_keeps_running_thread_owned_until_it_exits(block_on):
    box = BlockingObjectBox(block_on)
    inbox = XunjiaInbox(box, b"x" * 32, "fixture-workbench", lambda *_: False, object())
    await _assert_cancelled_io_stays_owned(inbox.poll, box)


async def test_cancelled_catalog_keeps_running_thread_owned_until_it_exits(tmp_path):
    box = BlockingObjectBox("put")

    class Store:
        nickname = "first"

        def list_accounts(self):
            return [{"id": "qd-fixture", "channel": "qidian", "self_uid": "qd_fixture", "self_nick": self.nickname}]

    store = Store()
    publisher = CatalogPublisher(box, store, "fixture-workbench", b"d" * 32, b"x" * 32, str(tmp_path / "catalog.json"))
    await _assert_cancelled_io_stays_owned(publisher.publish, box, before_recovery=lambda: setattr(store, "nickname", "second"))


def _oss_page(marker, *, truncated=True):
    tail = f"<NextMarker>{marker}</NextMarker>" if marker else ""
    flag = "true" if truncated else "false"
    return FakeResponse(f"<ListBucketResult><IsTruncated>{flag}</IsTruncated>{tail}</ListBucketResult>".encode())


def _static_box_with_opener(monkeypatch, opener):
    box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="fixture-bucket", key_id="fixture-id", secret="fixture-secret")
    monkeypatch.setattr(box, "_opener", opener)
    return box


def test_oss_listing_rejects_repeated_marker_without_following_forever(monkeypatch):
    opener = FakeOpener(_oss_page("same-marker"), _oss_page("same-marker"), _oss_page("", truncated=False))
    box = _static_box_with_opener(monkeypatch, opener)
    with pytest.raises(RuntimeError):
        box.list_prefix("wb/fixture/")
    assert len(opener.requests) == 2


def test_oss_listing_rejects_truncated_page_without_progress_marker(monkeypatch):
    opener = FakeOpener(_oss_page(""))
    box = _static_box_with_opener(monkeypatch, opener)
    with pytest.raises(RuntimeError):
        box.list_prefix("wb/fixture/")
    assert len(opener.requests) == 1


def test_oss_listing_stops_at_one_hundred_pages(monkeypatch):
    pages = [_oss_page(f"marker-{number}") for number in range(100)] + [_oss_page("", truncated=False)]
    opener = FakeOpener(*pages)
    box = _static_box_with_opener(monkeypatch, opener)
    with pytest.raises(RuntimeError):
        box.list_prefix("wb/fixture/")
    assert len(opener.requests) == 100


def test_oss_listing_enforces_one_thirty_second_budget_across_pages(monkeypatch):
    clock = FakeClock(1000)
    calls = []

    class SlowPages:
        def open(self, request, *, timeout):
            calls.append(timeout)
            clock.now += 16
            return _oss_page(f"marker-{len(calls)}", truncated=len(calls) < 3)

    monkeypatch.setattr(time, "monotonic", clock)
    box = _static_box_with_opener(monkeypatch, SlowPages())
    with pytest.raises(RuntimeError):
        box.list_prefix("wb/fixture/")
    assert len(calls) == 2
    assert 0 < calls[0] <= 30
    assert 0 < calls[1] <= 14, "第二页超时必须扣除第一页已用预算"


def test_oss_listing_budget_includes_time_spent_fetching_sts(monkeypatch):
    clock = FakeClock(1000)
    timeouts = []

    class SlowCredentials:
        def get_credentials(self):
            clock.now += 10
            return _sts().StsCredentials("STS.fixture", "fixture-secret", "fixture-token", _NOW + 3600, "fixture-bucket")

    class Opener:
        def open(self, request, *, timeout):
            timeouts.append(timeout)
            return _oss_page("", truncated=False)

    monkeypatch.setattr(time, "monotonic", clock)
    box = OssV1Box(endpoint="oss-cn-hangzhou.aliyuncs.com", bucket="qtrade-nyoss-lab", credentials_provider=SlowCredentials())
    monkeypatch.setattr(box, "_opener", Opener())
    assert box.list_prefix("wb/fixture/") == []
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 20, "申请 STS 的十秒必须计入本轮总预算"
