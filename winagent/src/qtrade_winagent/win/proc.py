"""``WinProc`` —— 进程起停与窗口关闭(05 §2.4.6:先 ``WM_CLOSE``,``process_close_grace_s`` 未退再 ``taskkill /F``)。"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Optional

from ..backends import ProcInfo
from . import require_windows


class WinProc:
    async def start(self, argv: list[str], *, cwd: Optional[str] = None) -> int:
        p = await asyncio.create_subprocess_exec(*argv, cwd=cwd, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        return p.pid

    async def stop(self, pid: int, *, force: bool = False) -> None:
        args = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
        await asyncio.to_thread(subprocess.run, args, capture_output=True)

    def find(self, name: str) -> list[ProcInfo]:
        """按映像名找进程。🔴 只按 ``name`` 扫全表,``memory_info``/``create_time`` 只对命中的几条取:
        Windows 上对几百个进程逐个开句柄取内存要 1~2 s,#28 `status` 一次要扫两遍,原写法直接把它推到 10 s 超时边缘。"""
        import psutil
        want = name.lower()
        out: list[ProcInfo] = []
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info.get("name") or "").lower() != want:
                continue
            try:
                mi = p.memory_info()
                created = int(p.create_time() * 1000)
            except (psutil.Error, OSError):
                mi, created = None, 0
            out.append(ProcInfo(p.info["pid"], p.info["name"], (mi.rss / 1e6) if mi else 0.0, 0.0, created))
        return out

    async def close_window(self, pid: int) -> bool:
        """给该 pid 的**顶层窗口**发 ``WM_CLOSE``;找不到窗口返回 False(调用方随后 ``taskkill /F``)。"""
        require_windows("WM_CLOSE")
        import win32con
        import win32gui
        import win32process
        hits: list[int] = []

        def cb(hwnd: int, _: object) -> bool:
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                hits.append(hwnd)
            return True
        win32gui.EnumWindows(cb, None)
        for hwnd in hits:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return bool(hits)
