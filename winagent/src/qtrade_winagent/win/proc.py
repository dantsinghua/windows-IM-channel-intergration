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
        import psutil
        out: list[ProcInfo] = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
            if (p.info.get("name") or "").lower() != name.lower():
                continue
            mi = p.info.get("memory_info")
            out.append(ProcInfo(p.info["pid"], p.info["name"], (mi.rss / 1e6) if mi else 0.0, 0.0,
                                int((p.info.get("create_time") or 0) * 1000)))
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
