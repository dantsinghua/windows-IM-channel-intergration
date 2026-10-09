"""``WinWsl`` —— ``wsl.exe`` 全部调用 + ``.wslconfig`` 读写(**会话代理**执行,C-02 / R3-6)。

🔴 ``.wslconfig`` 必须解析**安装用户**的 ``%USERPROFILE%\\.wslconfig``:
LocalSystem 在 Session 0 解析到的是 ``systemprofile\\.wslconfig``,是错的那份 —— 所以本类只在会话代理里实例化。

🔴 ``shutdown()`` 是**唯一**能发 ``wsl --shutdown`` 的地方(00 §11.6 [NOSHUTDOWN]);
调用方(``wslctl``)只在 ``confirm``/``confirm_shutdown`` 为真时才会走到它,本类不自行判断,但会记一行 WARN 便于事后审计。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from ..logfmt import get_logger
from . import require_windows

log = get_logger("wslctl")
WSL = "wsl.exe"


class WinWsl:
    def __init__(self, *, distro: str = "qtrade", wslconfig_path: str | None = None):
        self._distro = distro
        self._path = wslconfig_path or os.path.join(os.path.expanduser("~"), ".wslconfig")

    async def _run(self, args: list[str], timeout_s: float = 30.0) -> tuple[int, str]:
        require_windows("wsl.exe")
        p = await asyncio.create_subprocess_exec(WSL, *args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            p.kill()
            raise
        # WSL commands can return UTF-8 or UTF-16. A successful UTF-16
        # decode alone proves nothing: any even-length ASCII buffer decodes.
        if out.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = out.decode("utf-16", "replace")
        elif b"\x00" in out:
            text = out.decode("utf-16-le", "replace")
        else:
            text = out.decode("utf-8-sig", "replace")
        return int(p.returncode or 0), text.strip()

    async def list_distros(self) -> list[dict[str, Any]]:
        rc, out = await self._run(["--list", "--verbose"])
        rows: list[dict[str, Any]] = []
        for line in out.splitlines()[1:]:
            s = line.strip()
            if not s:
                continue
            default = s.startswith("*")
            parts = s.lstrip("* ").split()
            if len(parts) < 3:
                continue
            rows.append({"name": parts[0], "running": parts[1].lower() == "running",
                         "version": int(parts[2]) if parts[2].isdigit() else None, "default": default})
        return rows

    async def start(self, distro: str) -> None:
        rc, out = await self._run(["-d", distro, "--exec", "/bin/true"])
        if rc != 0:
            raise RuntimeError(f"wsl -d {distro} 启动失败:{out}")

    async def terminate(self, distro: str) -> None:
        await self._run(["--terminate", distro])

    async def shutdown(self) -> None:
        """🔴 会中断用户**全部**发行版 —— 只有用户确认过时机时调用方才会走到这里(00 §11.6)。"""
        log.warning("执行 wsl --shutdown(调用方已带用户确认)", extra={"op": "wsl.shutdown", "code": "CONFIRMED"})
        await self._run(["--shutdown"], timeout_s=60.0)

    async def exec(self, distro: str, argv: list[str]) -> tuple[int, str]:
        if argv and argv[0].startswith("--"):            # 发行版级命令(--import/--unregister),不进 --exec
            return await self._run(argv, timeout_s=600.0)
        return await self._run(["-d", distro, "-u", "root", "--exec"] + argv)

    async def version(self) -> str:
        _rc, out = await self._run(["--version"])
        return out

    def wslconfig_path(self) -> str:
        return self._path

    def read_wslconfig(self) -> str:
        try:
            with open(self._path, encoding="utf-8-sig") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def write_wslconfig(self, text: str) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(text)
