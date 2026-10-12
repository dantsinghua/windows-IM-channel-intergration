"""阿里云 OSS V1 签名的对象箱。只在环境变量齐了之后由询价轮询使用。

密钥留在实例里，不写进异常消息。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from email.utils import formatdate
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

_KEY_RE = re.compile(r"<Key>([^<]*)</Key>")


class OssV1Box:
    def __init__(self, *, endpoint: str, bucket: str, key_id: str, secret: str) -> None:
        host = endpoint.removeprefix("https://").removeprefix("http://").strip("/")
        self._bucket = bucket
        self._key_id = key_id
        self._secret = secret
        self._base = f"https://{bucket}.{host}"
        self._opener = build_opener(ProxyHandler({}))

    def list_prefix(self, prefix: str) -> list[str]:
        names: list[str] = []
        marker = ""
        while True:
            params = {"max-keys": "100", "prefix": prefix}
            if marker:
                params["marker"] = marker
            status, body = self._call("GET", "", params, None, "")
            if status != 200:
                raise RuntimeError(f"oss_list_{status}")
            text = body.decode("utf-8", "replace")
            names.extend(_KEY_RE.findall(text))
            if "<IsTruncated>true</IsTruncated>" not in text:
                return names
            found = re.search(r"<NextMarker>([^<]*)</NextMarker>", text)
            marker = found.group(1) if found else (names[-1] if names else "")
            if not marker:
                return names

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

    def string_to_sign(self, method: str, content_type: str, date: str, name: str, params: dict[str, str] | None) -> str:
        # prefix、max-keys、marker 只放在 URL 上。写进签名串会得到 403。
        del params
        resource = f"/{self._bucket}/{name}"
        return f"{method}\n\n{content_type}\n{date}\n{resource}"

    def _call(
        self,
        method: str,
        name: str,
        params: dict[str, str] | None,
        body: bytes | None,
        content_type: str,
    ) -> tuple[int, bytes]:
        date = formatdate(usegmt=True)
        signed = self.string_to_sign(method, content_type, date, name, params)
        digest = hmac.new(self._secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
        token = base64.b64encode(digest).decode("ascii")
        path = "/" + quote(name, safe="/") if name else "/"
        if params:
            path += "?" + urlencode(sorted(params.items()))
        request = Request(self._base + path, data=body, method=method)
        request.add_header("Date", date)
        if content_type:
            request.add_header("Content-Type", content_type)
        request.add_header("Authorization", f"OSS {self._key_id}:{token}")
        try:
            with self._opener.open(request, timeout=30) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()
