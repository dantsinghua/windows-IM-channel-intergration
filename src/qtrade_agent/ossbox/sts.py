"""按 STS 接口调用说明获取临时凭据；凭据只缓存在内存中。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_MAX_RESPONSE = 64 * 1024
_MIN_VALIDITY = 30.0
_REFRESH_BEFORE = 300.0
log = logging.getLogger("qtrade.ossbox.sts")


class NoRedirect(HTTPRedirectHandler):
    """鉴权请求不得把凭据随重定向转送到其它地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class StsError(RuntimeError):
    """只携带固定错误码，不透出服务端正文、请求或凭据。"""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.retryable = retryable


@dataclass(frozen=True)
class StsCredentials:
    access_key_id: str = field(repr=False)
    access_key_secret: str = field(repr=False)
    security_token: str = field(repr=False)
    expiration: float
    bucket: str


def build_access_token(auth_key: str, sm4_key: bytes, *, now: float | None = None) -> str:
    """标准 Base64(随机 IV16 + SM4-CBC(PKCS7(UTF-8 JSON)))。"""
    if not isinstance(auth_key, str) or not auth_key or len(auth_key.encode("utf-8")) > 4096:
        raise ValueError("sts_auth_key_invalid")
    if not isinstance(sm4_key, bytes) or len(sm4_key) != 16:
        raise ValueError("sts_sm4_key_invalid")
    stamp = time.time() if now is None else now
    if not math.isfinite(stamp) or stamp < 0:
        raise ValueError("sts_clock_invalid")
    # 延迟导入使未启用 STS 的旧静态凭据路径不触发密码操作。
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    plain = json.dumps({"key": auth_key, "ts": str(int(stamp))}, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    iv = secrets.token_bytes(16)
    encryptor = Cipher(algorithms.SM4(sm4_key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def _validate_url(url: str, *, allow_http: bool = False) -> None:
    if type(allow_http) is not bool:
        raise ValueError("sts_http_option_invalid")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
        local = host == "localhost"
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        valid = bool(host) and (parsed.scheme == "https" or (parsed.scheme == "http" and (local or allow_http)))
        if not valid or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        if any(ord(c) < 33 for c in url):
            raise ValueError
    except ValueError:
        raise ValueError("sts_url_invalid") from None


class StsCredentialProvider:
    """线程安全的按需刷新；无启动请求、无磁盘凭据、无后台刷新线程。"""

    def __init__(self, url: str, auth_key: str, sm4_key: bytes, *,
                 expected_bucket: str | None = None, clock: Callable[[], float] | None = None,
                 opener: Any = None, allow_http: bool = False) -> None:
        _validate_url(url, allow_http=allow_http)
        if not isinstance(auth_key, str) or not auth_key or len(auth_key.encode("utf-8")) > 4096:
            raise ValueError("sts_auth_key_invalid")
        if not isinstance(sm4_key, bytes) or len(sm4_key) != 16:
            raise ValueError("sts_sm4_key_invalid")
        if expected_bucket is not None and not _BUCKET.fullmatch(expected_bucket):
            raise ValueError("sts_bucket_invalid")
        self._url = url
        self._auth_key = auth_key
        self._sm4_key = sm4_key
        self._bucket = expected_bucket
        self._clock = clock or time.time
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), NoRedirect())
        self._lock = threading.Lock()
        self._cached: StsCredentials | None = None
        self._refresh_at = 0.0
        self._last_now = 0.0

    def get_credentials(self) -> StsCredentials:
        with self._lock:
            now = self._clock()
            if not math.isfinite(now) or now < self._last_now - 1:
                self._cached = None
                raise StsError("sts_clock_invalid")
            self._last_now = now
            cached = self._cached
            if cached is not None and now < self._refresh_at and cached.expiration > now + _MIN_VALIDITY:
                return cached
            try:
                fresh = self._fetch()
            except StsError as exc:
                now = self._clock()
                if exc.retryable and cached is not None and cached.expiration > now + _MIN_VALIDITY:
                    self._refresh_at = min(now + 15, cached.expiration - _MIN_VALIDITY)
                    log.warning("STS刷新暂不可用 code=sts_refresh_deferred，使用仍有效缓存，下次刷新时间=%s", self._refresh_at)
                    return cached
                self._cached = None
                raise
            now = self._clock()
            self._cached = fresh
            self._bucket = fresh.bucket  # 首次响应后固定目的；刷新不得悄悄换 bucket。
            self._refresh_at = min(max(now + 15, fresh.expiration - _REFRESH_BEFORE),
                                   fresh.expiration - _MIN_VALIDITY)
            return fresh

    def invalidate(self, credentials: StsCredentials) -> None:
        """确定的 OSS 凭据拒绝只使该版本失效，不误清除别的线程已刷新的值。"""
        with self._lock:
            if self._cached is credentials:
                self._cached = None
                self._refresh_at = 0.0

    def _fetch(self) -> StsCredentials:
        request = Request(self._url, method="GET")
        request.add_header("access_token", build_access_token(self._auth_key, self._sm4_key, now=self._clock()))
        request.add_header("Accept", "application/json")
        try:
            with self._opener.open(request, timeout=10) as response:
                status = response.status
                if status != 200:
                    raise StsError(f"sts_http_{status}", retryable=status in (429, 500, 502, 503, 504))
                raw = response.read(_MAX_RESPONSE + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise StsError(f"sts_http_{status}", retryable=status in (429, 500, 502, 503, 504)) from None
        except (OSError, URLError, TimeoutError, HTTPException):
            raise StsError("sts_unavailable", retryable=True) from None
        if len(raw) > _MAX_RESPONSE:
            raise StsError("sts_response_too_large")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            fields = [data[name] for name in ("access_key_id", "access_key_secret", "security_token", "expiration", "bucket")]
            if any(not isinstance(value, str) or not value or len(value) > 16384 or
                   any(ord(c) < 32 or ord(c) == 127 for c in value) for value in fields):
                raise ValueError
            key_id, secret, token, expiration, bucket = fields
            if not all(value.isascii() and not any(c.isspace() for c in value) for value in (key_id, secret, token)):
                raise ValueError
            if not _RFC3339.fullmatch(expiration) or not _BUCKET.fullmatch(bucket):
                raise ValueError
            expires = datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError, OverflowError):
            raise StsError("sts_response_invalid") from None
        if self._bucket is not None and bucket != self._bucket:
            raise StsError("sts_bucket_mismatch")
        if expires <= self._clock() + _MIN_VALIDITY:
            raise StsError("sts_credentials_expired")
        return StsCredentials(key_id, secret, token, expires, bucket)
