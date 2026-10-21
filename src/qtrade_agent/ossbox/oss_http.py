"""阿里云 OSS V1 对象箱；支持原有静态凭据与按需刷新的 STS 凭据。

密钥留在实例里，不写进异常消息。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
from email.utils import formatdate
from html import unescape
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .sts import NoRedirect, StsCredentialProvider

_KEY_RE = re.compile(r"<Key>([^<]*)</Key>")


class OssV1Box:
    def __init__(self, *, endpoint: str, bucket: str = "", key_id: str = "", secret: str = "",
                 security_token: str = "", credentials_provider: StsCredentialProvider | None = None) -> None:
        try:
            parsed = urlsplit(endpoint if "://" in endpoint else "https://" + endpoint)
            if (parsed.scheme not in ("https", "http") or not parsed.hostname or
                    parsed.username is not None or parsed.password is not None or
                    parsed.path not in ("", "/") or parsed.query or parsed.fragment or
                    any(ord(c) < 33 for c in endpoint)):
                raise ValueError
            _ = parsed.port
        except ValueError:
            raise ValueError("oss_endpoint_invalid") from None
        if credentials_provider is None and not all((bucket, key_id, secret)):
            raise ValueError("oss_credentials_missing")
        if credentials_provider is not None and (key_id or secret or security_token):
            raise ValueError("oss_credentials_conflict")
        if credentials_provider is not None and not bucket:
            raise ValueError("oss_bucket_missing")
        if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise ValueError("oss_bucket_invalid")
        if any(any(ord(c) < 33 or ord(c) == 127 for c in value) for value in (key_id, secret, security_token)):
            raise ValueError("oss_credentials_invalid")
        self._host = parsed.netloc
        self._bucket = bucket
        self._key_id = key_id
        self._secret = secret
        self._security_token = security_token
        self._credentials_provider = credentials_provider
        self._base = f"https://{bucket}.{self._host}"
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def from_env(cls) -> "OssV1Box | None":
        """只装配，不出网。显式 STS 配错时拒绝，绝不降级到旧 AK/SK。"""
        endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
        mode = os.environ.get("OSS_AUTH_MODE", "").strip().lower()
        sts_configured = any(name.startswith("OSS_STS_") for name in os.environ)
        mode = mode or ("sts" if sts_configured else "static")
        if mode not in ("static", "sts"):
            raise ValueError("oss_auth_mode_invalid")
        if mode == "static" and sts_configured:
            raise ValueError("oss_auth_mode_conflict")
        bucket = os.environ.get("OSS_BUCKET", "").strip()
        if mode == "sts":
            if not endpoint:
                raise ValueError("oss_endpoint_missing")
            auth_key = _env_secret("OSS_STS_AUTH_KEY")
            sm4_key = _env_secret("OSS_STS_SM4_KEY")
            url = os.environ.get("OSS_STS_URL", "").strip()
            http_option = os.environ.get("OSS_STS_ALLOW_HTTP", "false").strip().lower()
            if http_option not in ("true", "false"):
                raise ValueError("sts_http_option_invalid")
            provider = StsCredentialProvider(url, auth_key, sm4_key.encode("utf-8"),
                                             allow_http=http_option == "true")
            return cls(endpoint=endpoint, bucket=bucket, credentials_provider=provider)
        key_id = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
        secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
        if not all((endpoint, bucket, key_id, secret)):
            return None
        return cls(endpoint=endpoint, bucket=bucket, key_id=key_id, secret=secret,
                   security_token=os.environ.get("OSS_SECURITY_TOKEN", "").strip())

    def list_prefix(self, prefix: str) -> list[str]:
        names: list[str] = []
        marker = ""
        markers = {marker}
        deadline = time.monotonic() + 30
        for _page in range(100):
            if time.monotonic() >= deadline:
                raise RuntimeError("oss_list_budget_exhausted")
            params = {"max-keys": "100", "prefix": prefix}
            if marker:
                params["marker"] = marker
            status, body = self._call("GET", "", params, None, "", deadline=deadline)
            if status != 200:
                raise RuntimeError(f"oss_list_{status}")
            text = body.decode("utf-8", "replace")
            page_names = [unescape(name) for name in _KEY_RE.findall(text)]
            names.extend(page_names)
            if "<IsTruncated>true</IsTruncated>" not in text:
                return names
            found = re.search(r"<NextMarker>([^<]*)</NextMarker>", text)
            marker = unescape(found.group(1)) if found else (page_names[-1] if page_names else "")
            if not marker or marker in markers:
                raise RuntimeError("oss_list_no_progress")
            markers.add(marker)
        raise RuntimeError("oss_list_page_limit")

    def get(self, name: str) -> bytes | None:
        status, body = self._call("GET", name, None, None, "")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"oss_get_{status}")
        return body

    def put(self, name: str, body: bytes) -> None:
        status, _body = self._call("PUT", name, None, body, "application/octet-stream")
        if status not in (200, 201):
            raise RuntimeError(f"oss_put_{status}")

    def string_to_sign(self, method: str, content_type: str, date: str, name: str,
                       params: dict[str, str] | None, *, bucket: str | None = None,
                       security_token: str | None = None) -> str:
        # prefix、max-keys、marker 只放在 URL 上。写进签名串会得到 403。
        del params
        resource = f"/{self._bucket if bucket is None else bucket}/{name}"
        token = self._security_token if security_token is None else security_token
        headers = f"x-oss-security-token:{token}\n" if token else ""
        return f"{method}\n\n{content_type}\n{date}\n{headers}{resource}"

    def _call(
        self,
        method: str,
        name: str,
        params: dict[str, str] | None,
        body: bytes | None,
        content_type: str,
        *,
        deadline: float | None = None,
    ) -> tuple[int, bytes]:
        # 每个请求只取得一次不可变凭据快照，避免并发刷新混用新 ID 与旧 secret/token。
        credentials = self._credentials_provider.get_credentials() if self._credentials_provider is not None else None
        # Destination must only come from the explicit OSS business profile.
        bucket = self._bucket
        key_id = credentials.access_key_id if credentials is not None else self._key_id
        secret = credentials.access_key_secret if credentials is not None else self._secret
        security_token = credentials.security_token if credentials is not None else self._security_token
        date = formatdate(usegmt=True)
        signed = self.string_to_sign(method, content_type, date, name, params,
                                     bucket=bucket, security_token=security_token)
        digest = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
        token = base64.b64encode(digest).decode("ascii")
        path = "/" + quote(name, safe="/") if name else "/"
        if params:
            path += "?" + urlencode(sorted(params.items()))
        request = Request(f"https://{bucket}.{self._host}" + path, data=body, method=method)
        request.add_header("Date", date)
        if content_type:
            request.add_header("Content-Type", content_type)
        request.add_header("Authorization", f"OSS {key_id}:{token}")
        if security_token:
            request.add_header("x-oss-security-token", security_token)
        timeout_s = 30.0 if deadline is None else min(30.0, deadline - time.monotonic())
        if timeout_s <= 0:
            raise RuntimeError("oss_list_budget_exhausted")
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                status, payload = response.status, response.read()
        except HTTPError as exc:
            try:
                status, payload = exc.code, exc.read(65536)
            except (OSError, URLError, TimeoutError, HTTPException):
                raise RuntimeError("oss_unavailable") from None
            finally:
                exc.close()
        except (OSError, URLError, TimeoutError, HTTPException):
            raise RuntimeError("oss_unavailable") from None
        if (status == 403 and credentials is not None and
                re.search(rb"<Code>(?:SecurityTokenExpired|InvalidSecurityToken)</Code>", payload)):
            self._credentials_provider.invalidate(credentials)
        # 此层不重做请求；尤其不能把 PUT 的未知结果变成重新执行业务发送。
        return status, payload


def _env_secret(name: str) -> str:
    """支持受限私有文件，避免把长期鉴权材料放进仓库或命令行参数。"""
    value = os.environ.get(name)
    filename = os.environ.get(name + "_FILE")
    if value is not None and filename is not None:
        raise ValueError("sts_secret_source_conflict")
    if filename is not None:
        try:
            with Path(filename).open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError
            value = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
        except (OSError, UnicodeError, ValueError):
            raise ValueError("sts_secret_file_invalid") from None
    if not value:
        raise ValueError("sts_secret_missing")
    return value
