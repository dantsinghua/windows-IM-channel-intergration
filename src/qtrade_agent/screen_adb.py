"""企点画面流的真机执行体:adb screencap 取 redroid 当前帧,再编成 Annex-B H.264。

像素来自设备 framebuffer,不生成示意图。缺省不注入;生产入口 `main` 在 adb 可达时挂上。
控制台按首帧 JSON 的 width/height 铺画布,这里给出的是竖屏(高大于宽)。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from typing import AsyncIterator, Optional

_FFMPEG_CANDIDATES = (
    "ffmpeg",
    "/home/linuxbrew/.linuxbrew/bin/ffmpeg",
    "/usr/bin/ffmpeg",
)
# 540x960 仍是 9:16,宏块数落在 Baseline 3.1 以内,WebCodecs 的 avc1.42E01F 能解。
# 点击坐标按设备真实分辨率换算,不按这档缩放。
_ENC_W, _ENC_H = 540, 960
_KEYCODE = {
    "BACK": "4", "HOME": "3", "ENTER": "66", "DEL": "67", "ESCAPE": "111",
}


def ffmpeg_bin() -> Optional[str]:
    for c in _FFMPEG_CANDIDATES:
        found = shutil.which(c) if "/" not in c else (c if shutil.os.path.isfile(c) else None)
        if found:
            return found
    return None


def _run(args: list[str], *, timeout: float = 8) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout)


class _Session:
    def __init__(self, serial: str, profile: str, fps: int):
        self.serial = serial
        self.meta = {
            "codec": "avc1.42E01F",
            "width": _ENC_W,
            "height": _ENC_H,
            "profile": profile,
            "fps": fps,
            "seq0": 0,
        }
        self._fps = fps
        self._paused = False
        self._closed = False
        self._dev_w, self._dev_h = _device_size(serial)
        self._down: Optional[tuple[float, float]] = None
        self._rot = 0

    async def frames(self) -> AsyncIterator[tuple[int, bytes]]:
        ff = ffmpeg_bin()
        if not ff:
            raise RuntimeError("找不到 ffmpeg,无法把真机画面编成 H.264")
        interval = 1.0 / max(1, self._fps)
        while not self._closed:
            if self._paused:
                await asyncio.sleep(0.2)
                continue
            t0 = time.time()
            png = await asyncio.to_thread(_screencap, self.serial)
            if png:
                nal = await asyncio.to_thread(_encode_png, ff, png)
                if nal:
                    yield int(time.time() * 1000), nal
            wait = interval - (time.time() - t0)
            if wait > 0:
                await asyncio.sleep(wait)

    async def control(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "pause":
            self._paused = True
            return
        if kind == "resume":
            self._paused = False
            return
        if kind == "profile":
            self._fps = _fps_of(str(msg.get("profile") or "thumb"))
            return
        await asyncio.to_thread(self._control_sync, msg)

    def _control_sync(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "touch":
            x = float(msg.get("x") or 0)
            y = float(msg.get("y") or 0)
            action = msg.get("action") or "down"
            if action == "down":
                self._down = (x, y)
                return
            if action == "move":
                return
            x0, y0 = self._down or (x, y)
            self._down = None
            px, py = _px(x, y, self._dev_w, self._dev_h)
            if abs(x - x0) < 0.02 and abs(y - y0) < 0.02:
                _adb(self.serial, "shell", "input", "tap", str(px), str(py))
            else:
                ax, ay = _px(x0, y0, self._dev_w, self._dev_h)
                _adb(self.serial, "shell", "input", "swipe", str(ax), str(ay), str(px), str(py), "120")
        elif kind == "scroll":
            x, y = _px(float(msg.get("x") or 0.5), float(msg.get("y") or 0.5), self._dev_w, self._dev_h)
            dy = int(msg.get("dy") or 0)
            y2 = max(0, min(self._dev_h - 1, y - dy))
            _adb(self.serial, "shell", "input", "swipe", str(x), str(y), str(x), str(y2), "80")
        elif kind == "key":
            # 不认识的键名以前落成默认 4(返回)。方向键、旋转都会把应用退回桌面。
            if str(msg.get("action") or "down") != "down":
                return
            code = _KEYCODE.get(str(msg.get("keycode") or "").upper())
            if not code:
                return
            _adb(self.serial, "shell", "input", "keyevent", code)
        elif kind == "rotate":
            self._rot = (self._rot + 1) % 4
            _adb(self.serial, "shell", "settings", "put", "system", "accelerometer_rotation", "0")
            _adb(self.serial, "shell", "settings", "put", "system", "user_rotation", str(self._rot))
        elif kind == "text":
            text = str(msg.get("text") or "")
            if text:
                _adb(self.serial, "shell", "input", "text", text.replace(" ", "%s"))

    async def close(self) -> None:
        self._closed = True


class AdbScreenBackend:
    """`StreamBackend` 的真机实现。`serial_of(account_id)` 决定连哪台 redroid。"""

    def __init__(self, serial_of):
        self._serial_of = serial_of

    async def open(self, account_id: str, *, profile: str) -> _Session:
        serial = self._serial_of(account_id) or "127.0.0.1:5555"
        _ensure_qidian(serial)
        return _Session(serial, profile, _fps_of(profile))

    async def capture_png(self, account_id: str) -> bytes:
        """给没有 WebCodecs 的控制台用:直接回一帧真机 PNG,不经过 H.264。"""
        serial = self._serial_of(account_id) or "127.0.0.1:5555"
        return await asyncio.to_thread(_screencap, serial)

    async def inject(self, account_id: str, msg: dict) -> dict:
        serial = self._serial_of(account_id) or "127.0.0.1:5555"
        session = _Session(serial, "focus", 1)
        # REST 的 tap 与 WS 的 touch 字段名不同,这里统一成一次点击
        if msg.get("type") == "tap":
            w, h = session._dev_w, session._dev_h
            x, y = float(msg.get("x") or 0), float(msg.get("y") or 0)
            # REST 可能给的是像素也可能是 0~1;大于 1 当作像素
            if x <= 1 and y <= 1:
                x, y = _px(x, y, w, h)
            _adb(serial, "shell", "input", "tap", str(int(x)), str(int(y)))
            return {"ok": True}
        await session.control(msg)
        return {"ok": True}


def _fps_of(profile: str) -> int:
    return {"focus": 8, "focus15": 8, "thumb10": 5}.get(profile, 5)


def _px(x: float, y: float, w: int, h: int) -> tuple[int, int]:
    return max(0, min(w - 1, int(x * w))), max(0, min(h - 1, int(y * h)))


def _device_size(serial: str) -> tuple[int, int]:
    try:
        out = _run(["adb", "-s", serial, "shell", "wm", "size"], timeout=4).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return 720, 1280
    # Physical size: 720x1280
    for token in out.replace(":", " ").split():
        if "x" in token and token[0].isdigit():
            a, _, b = token.partition("x")
            if a.isdigit() and b.isdigit():
                return int(a), int(b)
    return 720, 1280


def _ensure_qidian(serial: str) -> None:
    """前台不是真企点时拉起 `com.tencent.qidian`,不打开 mock 包。"""
    try:
        out = _run(["adb", "-s", serial, "shell", "dumpsys", "window"], timeout=6).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return
    if "com.tencent.qidian" in out and "mCurrentFocus" in out:
        for line in out.splitlines():
            if "mCurrentFocus" in line and "com.tencent.qidian" in line:
                return
    _adb(serial, "shell", "am", "start", "-n",
         "com.tencent.qidian/com.tencent.mobileqq.activity.SplashActivity")


def _screencap(serial: str) -> bytes:
    try:
        proc = subprocess.run(["adb", "-s", serial, "exec-out", "screencap", "-p"],
                              capture_output=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    data = proc.stdout
    return data if data.startswith(b"\x89PNG") else b""


def _encode_png(ff: str, png: bytes) -> bytes:
    try:
        proc = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "image2pipe", "-vcodec", "png", "-i", "-",
             "-vf", f"scale={_ENC_W}:{_ENC_H},format=yuv420p",
             "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
             "-g", "1", "-tune", "zerolatency", "-preset", "ultrafast",
             "-f", "h264", "-frames:v", "1", "pipe:1"],
            input=png, capture_output=True, timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return b""
    data = proc.stdout
    if b"\x00\x00\x01" not in data and b"\x00\x00\x00\x01" not in data:
        return b""
    return data


def _adb(serial: str, *args: str) -> None:
    try:
        subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return
