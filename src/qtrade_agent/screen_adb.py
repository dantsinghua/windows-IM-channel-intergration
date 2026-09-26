"""企点画面的两条 adb 旁路:#33 截图(``exec-out screencap -p``)与 #35 REST 注入兜底(``input``)。

画面流本体(#34)在 ``screen_scrcpy.py``,走 scrcpy 控制 socket,不经任何 shell。
这里的两条都经 ``adb shell``:命令串一律用 ``shlex.join`` 拼,文本参数由 ``shlex.quote`` 包住,
``; $() ` | & >`` 之类到了设备 shell 里只是字面字符(评审 B1)。
全部异步(``asyncio.create_subprocess_exec``),不在事件循环里同步跑 subprocess。
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Optional, Protocol

log = logging.getLogger("qtrade.screen.adb")

#: 键名 → Android KEYCODE_*(REST 与 WS 共用;数字 keycode 直接用)
KEYCODES: dict[str, int] = {
    "HOME": 3, "BACK": 4, "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22,
    "TAB": 61, "SPACE": 62, "ENTER": 66, "DEL": 67, "MENU": 82, "PAGE_UP": 92, "PAGE_DOWN": 93,
    "ESCAPE": 111, "FORWARD_DEL": 112, "MOVE_HOME": 122, "MOVE_END": 123, "APP_SWITCH": 187,
}
KEYCODE_MAX = 400                       # Android 11 的 KEYCODE_* 最大值在 300 出头;越界一律拒绝
#: #35 ``swipe.duration_ms``(控制台静态预览档靠「同点 swipe + 时长」做长按;R6-75,02 #35 已登记)
SWIPE_DURATION_DEFAULT_MS = 120
SWIPE_DURATION_MAX_MS = 5000


class AdbRunner(Protocol):
    async def run(self, serial: str, *args: str, timeout: float = 15.0) -> tuple[int, bytes]: ...


class AsyncAdb:
    """``adb -P <server_port> -s <serial> …`` 的异步封装(04 §2.7.4:adb server 固定 16000,永不裸 adb)。"""

    def __init__(self, adb_bin: str = "adb", server_port: int = 16000):
        self._base = [adb_bin, "-P", str(server_port)]

    def argv(self, serial: str, *args: str) -> list[str]:
        return [*self._base, "-s", serial, *args]

    async def run(self, serial: str, *args: str, timeout: float = 15.0) -> tuple[int, bytes]:
        """跑一条 adb 命令并收全部输出;超时杀掉子进程并回 ``(-1, b"")``。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(serial, *args), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as e:
            log.warning("adb 起不来: %s", e)
            return -1, b""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, b""
        return proc.returncode if proc.returncode is not None else -1, out or b""

    async def spawn(self, serial: str, *args: str) -> Any:
        """起一个常驻子进程(scrcpy-server 的 ``adb shell``),调用方负责收尾。"""
        return await asyncio.create_subprocess_exec(
            *self.argv(serial, *args), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)


async def shell(adb: AdbRunner, serial: str, argv: list[str], *, timeout: float = 10.0) -> tuple[int, bytes]:
    """``adb shell <命令串>``:命令串由 ``shlex.join`` 拼,每个参数各自引号包住。"""
    return await adb.run(serial, "shell", shlex.join(argv), timeout=timeout)


async def screencap_png(adb: AdbRunner, serial: str) -> bytes:
    """一帧真机 PNG(``exec-out`` 不经 pty,字节不被改写);不是 PNG 一律回空。"""
    rc, out = await adb.run(serial, "exec-out", "screencap", "-p", timeout=8.0)
    return out if rc == 0 and out.startswith(b"\x89PNG") else b""


async def device_size(adb: AdbRunner, serial: str) -> Optional[tuple[int, int]]:
    """``wm size`` 的物理分辨率;读不出回 ``None``(调用方据此拒绝归一化坐标,不瞎猜)。"""
    _rc, out = await shell(adb, serial, ["wm", "size"], timeout=5.0)
    text = out.decode("utf-8", "replace")
    found: Optional[tuple[int, int]] = None
    for line in text.splitlines():                       # 有 Override size 时以它为准(那才是当前显示尺寸)
        _, _, val = line.partition(":")
        a, _, b = val.strip().partition("x")
        if a.isdigit() and b.isdigit():
            found = (int(a), int(b))
    return found


def keycode_of(raw: Any) -> Optional[int]:
    """``4`` / ``"4"`` / ``"BACK"`` / ``"KEYCODE_BACK"`` → 4;认不出回 ``None``(不再落成默认返回键)。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if 0 < raw <= KEYCODE_MAX else None
    s = str(raw or "").strip().upper()
    if s.isdigit():
        return keycode_of(int(s))
    if s.startswith("KEYCODE_"):
        s = s[len("KEYCODE_"):]
    return KEYCODES.get(s)


def swipe_duration_ms(raw: Any) -> int:
    """缺省 120 ms,钳到 1~5000;不是数字抛 ``ValueError``。"""
    if raw is None:
        return SWIPE_DURATION_DEFAULT_MS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"duration_ms 须为数字:{raw!r}")
    return max(1, min(SWIPE_DURATION_MAX_MS, int(raw)))


async def adb_input(adb: AdbRunner, serial: str, msg: dict[str, Any]) -> dict[str, Any]:
    """#35 REST 兜底:``tap|swipe|key|text`` → ``adb shell input …``(02 #35「与 RPA 同一条 adb input 路径」)。

    坐标:``0~1`` 视为归一化(按 ``wm size`` 换算),大于 1 视为设备像素。
    """
    kind = msg.get("type")
    if kind in ("tap", "swipe"):
        pts = [float(msg["x"]), float(msg["y"])]
        if kind == "swipe":
            pts += [float(msg["x2"]), float(msg["y2"])]
        if all(0 <= v <= 1 for v in pts):
            size = await device_size(adb, serial)
            if size is None:
                raise RuntimeError("读不出设备分辨率(wm size),归一化坐标无法换算")
            w, h = size
            pts = [pts[i] * (w if i % 2 == 0 else h) for i in range(len(pts))]
        coords = [str(max(0, int(v))) for v in pts]
        if kind == "tap":
            argv = ["input", "tap", *coords]
        else:
            argv = ["input", "swipe", *coords, str(swipe_duration_ms(msg.get("duration_ms")))]
    elif kind == "key":
        code = keycode_of(msg.get("keycode"))
        if code is None:
            raise ValueError(f"不认识的 keycode:{msg.get('keycode')!r}")
        argv = ["input", "keyevent", str(code)]
    elif kind == "text":
        text = str(msg.get("text") or "")
        if not text:
            return {"ok": True, "skipped": "empty_text"}
        argv = ["input", "text", text]                   # shlex.join 里逐参数 shlex.quote
    else:
        raise ValueError(f"不支持的注入类型:{kind!r}")
    rc, out = await shell(adb, serial, argv)
    if rc != 0:
        raise RuntimeError(f"adb input 失败 rc={rc}: {out.decode('utf-8', 'replace')[:200]}")
    return {"ok": True}
