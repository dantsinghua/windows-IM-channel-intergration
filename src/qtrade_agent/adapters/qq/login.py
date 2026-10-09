"""NapCat 4.18.28 登录期接口；只转发上游已经生成的 PNG。

上游 QQLogin API 返回的是扫码 URL，不是图片。真实图片由
napcat-shell/base.ts 写入 /app/napcat/cache/qrcode.png；不下载扫码 URL。
WebUI 的 token 只从 NapCat 自己的配置读取，临时 Credential 只在内存中使用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import urllib.request
import zlib
from typing import Any, Awaitable, Callable

MAX_QR_BYTES = 1024 * 1024
QR_REFRESH_MS = 30_000
QR_LIFETIME_MS = 120_000


class NapCatLoginError(RuntimeError):
    """只包含固定故障类别，不携带 HTTP 正文、二维码或认证信息。"""


def valid_png(data: bytes) -> bool:
    if not isinstance(data, bytes) or not 45 <= len(data) <= MAX_QR_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos, seen_header, seen_data = 8, False, False
    while pos + 12 <= len(data):
        size = struct.unpack_from(">I", data, pos)[0]
        end = pos + 12 + size
        if end > len(data):
            return False
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:end - 4]
        if zlib.crc32(kind + body) & 0xffffffff != struct.unpack_from(">I", data, end - 4)[0]:
            return False
        if not seen_header:
            if kind != b"IHDR" or size != 13:
                return False
            width, height = struct.unpack_from(">II", body)
            if not 0 < width <= 4096 or not 0 < height <= 4096:
                return False
            seen_header = True
        if kind == b"IDAT":
            seen_data = True
        if kind == b"IEND":
            return size == 0 and seen_data and end == len(data)
        pos = end
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def _post(port: int, path: str, body: dict[str, Any], credential: str = "") -> dict[str, Any]:
    """固定 loopback、禁代理/重定向，响应和每次请求均有上限。"""
    def request() -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = "Bearer " + credential
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}",
                                     data=json.dumps(body).encode(), headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=12) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise NapCatLoginError("response_too_large")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("code") != 0 or not isinstance(result.get("data"), dict):
                raise NapCatLoginError("napcat_not_ready")
            return result["data"]
        except NapCatLoginError:
            raise
        except Exception:
            raise NapCatLoginError("webui_unavailable") from None
    return await asyncio.wait_for(asyncio.to_thread(request), 13)


class NapCatLoginBackend:
    def __init__(self, *, token_for: Callable[[dict[str, Any]], str],
                 read_png: Callable[[dict[str, Any]], Awaitable[bytes]],
                 request: Callable[..., Awaitable[dict[str, Any]]] = _post):
        self._token_for = token_for
        self._read_png = read_png
        self._request = request

    async def qrcode(self, row: dict[str, Any], *, refresh: bool) -> bytes:
        try:
            return await asyncio.wait_for(self._qrcode(row, refresh=refresh), 25)
        except asyncio.CancelledError:
            raise
        except NapCatLoginError:
            raise
        except Exception:
            raise NapCatLoginError("qrcode_unavailable") from None

    async def _qrcode(self, row: dict[str, Any], *, refresh: bool) -> bytes:
        seq = int(row["seq"])
        if row.get("channel") != "qq" or not 1 <= seq <= 98 or row["id"] != f"qq{seq:02d}":
            raise NapCatLoginError("invalid_account")
        token = self._token_for(row)
        if not isinstance(token, str) or not token:
            raise NapCatLoginError("webui_not_ready")
        auth = await self._request(16300 + seq, "auth/login", {"hash": hashlib.sha256((token + ".napcat").encode()).hexdigest()})
        token = ""
        credential = auth.get("Credential")
        if not isinstance(credential, str) or not credential or auth.get("require2FA"):
            raise NapCatLoginError("webui_auth_unavailable")
        before = b""
        if refresh:
            try:
                before = await self._read_png(row)
            except Exception:
                pass
        path = "RefreshQRcode" if refresh else "GetQQLoginQrcode"
        result = await self._request(16300 + seq, "QQLogin/" + path, {}, credential)
        credential = ""
        url = result.get("qrcodeurl" if refresh else "qrcode")
        if not isinstance(url, str) or not url or result.get("restarting"):
            raise NapCatLoginError("qrcode_not_ready")
        # NapCat 先发布 URL，再异步写 PNG；刷新不得抢读上一次文件。
        for _ in range(15):
            try:
                png = await self._read_png(row)
            except Exception:
                png = b""
            if valid_png(png) and (not refresh or png != before):
                return png
            await asyncio.sleep(0.2)
        raise NapCatLoginError("qrcode_not_ready")
