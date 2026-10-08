"""Local-only Linux/ADB diagnostic console; no production services required."""
from __future__ import annotations

import base64
import asyncio
from collections import deque
import contextlib
import gzip
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from video import PROTOCOL, ScrcpySession

ROOT = Path(__file__).resolve().parent
PACKAGE = "com.tencent.qidian"
PNG = b"\x89PNG\r\n\x1a\n"


class DeviceError(Exception):
    pass


class Device:
    def __init__(self):
        self.container = os.getenv("QTRADE_ADB_CONTAINER", "qtrade-linux-debug-vm")
        self.serial = os.getenv("QTRADE_ADB_SERIAL", "127.0.0.1:15555")
        self.lock = threading.Lock()

    def run(self, *args: str, selected=True, timeout=20) -> bytes:
        prefix = ["docker", "exec", self.container, "/opt/platform-tools/adb"]
        if selected:
            prefix += ["-s", self.serial]
        try:
            result = subprocess.run(prefix + list(args), capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeviceError("ADB 不可用或操作超时；请检查本机 redroid 虚拟机。") from exc
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()[:400]
            raise DeviceError(message or "ADB 操作失败")
        return result.stdout

    def shell(self, *args: str, timeout=20) -> str:
        # adb shell joins arguments on the Android side: quote each argument there too.
        return self.run("shell", shlex.join(args), timeout=timeout).decode(errors="replace").strip()

    def connect(self):
        output = self.run("connect", self.serial, selected=False).decode(errors="replace")
        if self.run("get-state").strip() != b"device":
            raise DeviceError("设备尚未进入 device 状态：" + output[:200])
        return {"connected": True, "serial": self.serial}

    def screenshot(self):
        data = self.run("exec-out", "screencap", "-p", timeout=30)
        if not data.startswith(PNG):
            raise DeviceError("设备未返回有效 PNG 画面")
        return data

    def status(self):
        result = {"serial": self.serial, "connected": False, "booted": False,
                  "qidian_installed": False, "qidian_running": False,
                  "foreground": "", "error": None}
        try:
            result["connected"] = self.run("get-state", timeout=5).strip() == b"device"
            result["booted"] = self.shell("getprop", "sys.boot_completed", timeout=5) == "1"
            result["android"] = self.shell("getprop", "ro.build.version.release", timeout=5)
            result["abi"] = self.shell("getprop", "ro.product.cpu.abilist", timeout=5)
            result["qidian_installed"] = ("package:" + PACKAGE) in self.shell("pm", "list", "packages", PACKAGE, timeout=10).splitlines()
            if result["qidian_installed"]:
                result["qidian_running"] = PACKAGE in self.shell("ps", "-A", "-o", "NAME", timeout=5).splitlines()
            activities = self.shell("dumpsys", "activity", "activities", timeout=10)
            result["foreground"] = next((x.strip() for x in activities.splitlines()
                                           if "mResumedActivity" in x or "topResumedActivity" in x), "")
        except DeviceError as exc:
            result["error"] = str(exc)
        return result

    def launch(self):
        if ("package:" + PACKAGE) not in self.shell("pm", "list", "packages", PACKAGE).splitlines():
            raise DeviceError("尚未安装企点 APK；请先执行 README 中的安装步骤。")
        resolved = self.shell("cmd", "package", "resolve-activity", "--brief", PACKAGE)
        activity = next((line.strip() for line in reversed(resolved.splitlines())
                         if re.fullmatch(r"com\.tencent\.qidian/[A-Za-z0-9_.$]+", line.strip())), None)
        if not activity:
            raise DeviceError("找不到企点启动 Activity")
        # First ARM64 launch under software emulation can take over 40 seconds.
        output = self.shell("am", "start", "-W", "-n", activity, timeout=120)
        if "Error:" in output or "Status: ok" not in output:
            raise DeviceError("企点启动失败：" + output[:300])
        return {"launched": True, "activity": activity}


def host_status():
    binder = None
    try:
        with gzip.open("/proc/config.gz", "rt") as stream:
            config = stream.read()
        binder = bool(re.search(r"^CONFIG_ANDROID_BINDER_IPC=[ym]$", config, re.M))
    except OSError:
        pass
    return {"binder": binder, "kvm": Path("/dev/kvm").exists(),
            "mode": "linux-debug", "transport": "local-qemu-adb"}


class Tap(BaseModel):
    x: int = Field(ge=0, le=8192)
    y: int = Field(ge=0, le=8192)


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=512)


def validated_origin(value):
    url = urlsplit(value)
    local = url.hostname in {"127.0.0.1", "localhost"}
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path not in {"", "/"} or (url.scheme != "https" and not (local and url.scheme == "http"))):
        raise ValueError("远程连接须配置完整 HTTPS origin；仅本机测试允许 loopback HTTP")
    return f"{url.scheme}://{url.netloc}".rstrip("/")


def create_app(device=None, *, site_origin=None, api_origin=None, token_file=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    device = device or Device()
    csrf = secrets.token_urlsafe(32)
    stream_lock = asyncio.Lock()
    site_origin = site_origin or os.getenv("QTRADE_SITE_ORIGIN")
    api_origin = api_origin or os.getenv("QTRADE_API_ORIGIN")
    token_file = token_file or os.getenv("QTRADE_REMOTE_TOKEN_FILE")
    remote_token = None
    if any((site_origin, api_origin, token_file)):
        if not all((site_origin, api_origin, token_file)):
            raise ValueError("远程模式必须同时配置 SITE_ORIGIN、API_ORIGIN 和 REMOTE_TOKEN_FILE")
        site_origin, api_origin = validated_origin(site_origin), validated_origin(api_origin)
        key_path = Path(token_file)
        if key_path.stat().st_mode & 0o077:
            raise ValueError("远程访问凭据文件必须仅所有者可读写（chmod 600）")
        remote_token = key_path.read_text().strip()
        if len(remote_token) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", remote_token):
            raise ValueError("远程访问凭据须为至少 32 字符的随机 base64url 字符串")
    # Keep file previews styled, while allowing only this exact inline CSS in CSP.
    index_html = (ROOT / "web/index.html").read_text()
    css = re.search(r"<style>(.*?)</style>", index_html, re.S).group(1)
    style_hash = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        authority = request.headers.get("host", "")
        host = authority.split(":")[0]
        request_origin = f"{request.url.scheme}://{authority}"
        if host not in {"127.0.0.1", "localhost", "testserver"} and request_origin != api_origin:
            return JSONResponse({"error": "仅允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {request_origin, site_origin}:
            return JSONResponse({"error": "拒绝跨站请求"}, status_code=403)
        if remote_token and request.url.path.startswith("/api/"):
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied.encode(), f"Bearer {remote_token}".encode()):
                return JSONResponse({"error": "访问凭据无效或缺失"}, status_code=401,
                                    headers={"Cache-Control": "no-store"})
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("x-debug-csrf", ""), csrf):
            return JSONResponse({"error": "请刷新调试页面后重试"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = f"default-src 'self'; connect-src 'self' https: wss:; img-src 'self' blob:; style-src 'self' 'sha256-{style_hash}'; frame-ancestors 'none'"
        return response

    @app.exception_handler(DeviceError)
    async def device_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.get("/")
    def index():
        return HTMLResponse(index_html)

    @app.get("/app.js")
    def javascript():
        return FileResponse(ROOT / "web/app.js", media_type="text/javascript")

    @app.get("/video.js")
    def video_javascript():
        return FileResponse(ROOT / "web/video.js", media_type="text/javascript")

    @app.websocket("/api/debug/stream")
    async def stream(ws: WebSocket):
        authority = ws.headers.get("host", "")
        scheme = "https" if ws.url.scheme == "wss" else "http"
        request_origin = f"{scheme}://{authority}"
        local_host = authority.split(":")[0] in {"127.0.0.1", "localhost", "testserver"}
        if ((not local_host and request_origin != api_origin) or not ws.headers.get("origin")
                or ws.headers.get("origin") not in {request_origin, site_origin}
                or PROTOCOL not in ws.scope.get("subprotocols", [])):
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol=PROTOCOL)
        try:
            raw = await asyncio.wait_for(ws.receive_text(), 5)
            if len(raw) > 2048:
                raise ValueError()
            import json
            auth = json.loads(raw)
            if (not isinstance(auth, dict) or auth.get("type") != "auth"
                    or not secrets.compare_digest(str(auth.get("csrf", "")).encode(), csrf.encode())
                    or (remote_token and not secrets.compare_digest(str(auth.get("token", "")).encode(), remote_token.encode()))):
                raise ValueError()
        except (ValueError, asyncio.TimeoutError, WebSocketDisconnect):
            with contextlib.suppress(Exception):
                await ws.close(code=1008, reason="视频连接鉴权失败")
            return
        if stream_lock.locked():
            await ws.close(code=1013, reason="设备已有实时连接，请先关闭原连接")
            return
        async with stream_lock:
            session = ScrcpySession(device)
            tasks = []
            try:
                metadata = await session.start()
                await ws.send_json(metadata)
                async def video_frames():
                    async for packet in session.packets():
                        await asyncio.wait_for(ws.send_bytes(packet), 10)
                async def controls():
                    recent = deque()
                    while True:
                        raw = await ws.receive_text()
                        if len(raw) > 4096:
                            raise ValueError("控制消息过长")
                        now = time.monotonic()
                        while recent and recent[0] < now - 1:
                            recent.popleft()
                        if len(recent) >= 120:
                            raise ValueError("控制输入过于频繁")
                        recent.append(now)
                        message = json.loads(raw)
                        if not isinstance(message, dict):
                            raise ValueError("无效控制消息")
                        await session.send_control(message)
                tasks = [asyncio.create_task(video_frames()), asyncio.create_task(controls())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except WebSocketDisconnect:
                pass
            except Exception as exc:
                logging.getLogger(__name__).warning("Real-time stream stopped (%s)", type(exc).__name__)
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "error", "error": "实时流中断或编码器不可用，请停止后重试。"})
                    await ws.close(code=1011)
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await session.close()

    @app.get("/api/debug/status")
    def status():
        with device.lock:
            state = device.status()
        return {"host": host_status(), "device": state, "csrf": csrf,
                "checked_at": time.time(), "login_verified": False}

    @app.post("/api/debug/connect")
    def connect():
        with device.lock:
            return device.connect()

    @app.post("/api/debug/qidian/launch")
    def launch():
        with device.lock:
            return device.launch()

    @app.get("/api/debug/screenshot")
    def screenshot():
        with device.lock:
            data = device.screenshot()
        return Response(data, media_type="image/png")

    @app.post("/api/debug/tap")
    def tap(body: Tap):
        with device.lock:
            device.shell("input", "tap", str(body.x), str(body.y))
        return {"ok": True}

    @app.post("/api/debug/key/{key}")
    def key(key: str):
        keys = {"back": "4", "home": "3", "enter": "66", "delete": "67"}
        if key not in keys:
            return JSONResponse({"error": "不支持的按键"}, status_code=400)
        with device.lock:
            device.shell("input", "keyevent", keys[key])
        return {"ok": True}

    @app.post("/api/debug/text")
    def text(body: TextInput):
        # Android input text has no reliable Unicode support; do not silently corrupt Chinese.
        if any(ord(c) < 32 or ord(c) > 126 for c in body.text) or "%" in body.text:
            return JSONResponse({"error": "此入口仅支持不含 % 的 ASCII；中文请使用设备输入法。"}, status_code=400)
        with device.lock:
            device.shell("input", "text", body.text.replace(" ", "%s"))
        return {"ok": True}

    if site_origin:
        # Include CORS headers on 401/403 as well, so the browser can explain failures.
        app.add_middleware(CORSMiddleware, allow_origins=[site_origin],
                           allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type", "X-Debug-CSRF"])
    return app


app = create_app()
