"""``WinWsl`` —— ``wsl.exe`` 全部调用 + ``.wslconfig`` 读写(**会话代理**执行,C-02 / R3-6)。

🔴 ``.wslconfig`` 必须解析**安装用户**的 ``%USERPROFILE%\\.wslconfig``:
LocalSystem 在 Session 0 解析到的是 ``systemprofile\\.wslconfig``,是错的那份 —— 所以本类只在会话代理里实例化。

🔴 ``shutdown()`` 是**唯一**能发 ``wsl --shutdown`` 的地方(00 §11.6 [NOSHUTDOWN]);
调用方(``wslctl``)只在 ``confirm``/``confirm_shutdown`` 为真时才会走到它,本类不自行判断,但会记一行 WARN 便于事后审计。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from ..logfmt import get_logger
from . import decode_console_output, hidden_process_kwargs, require_windows

log = get_logger("wslctl")
WSL = "wsl.exe"
HOLDER_READY = b"QTRADE_WSL_READY\n"
HOLDER_START_TIMEOUT_S = 30.0
HOLDER_CLOSE_TIMEOUT_S = 5.0
HOLDER_STDERR_LIMIT_BYTES = 4096


class WinWsl:
    def __init__(self, *, distro: str = "qtrade", wslconfig_path: str | None = None):
        self._distro = distro
        self._path = wslconfig_path or os.path.join(os.path.expanduser("~"), ".wslconfig")
        self._holder: asyncio.subprocess.Process | None = None
        self._holder_stderr = bytearray()
        self._holder_stderr_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def _run(self, args: list[str], timeout_s: float = 30.0,
                   input_data: Optional[bytes] = None) -> tuple[int, str]:
        require_windows("wsl.exe")
        p = await asyncio.create_subprocess_exec(WSL, *args, stdout=asyncio.subprocess.PIPE,
                                                 stdin=asyncio.subprocess.PIPE if input_data is not None else None,
                                                 stderr=asyncio.subprocess.STDOUT,
                                                 **hidden_process_kwargs())
        try:
            out, _ = await asyncio.wait_for(p.communicate(input_data), timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass
            await p.communicate()
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
        running_rc, running_out = await self._run(["--list", "--running", "--quiet"])
        running = {line.strip() for line in running_out.splitlines()} if running_rc == 0 else set()
        rows: list[dict[str, Any]] = []
        for line in out.splitlines()[1:]:
            s = line.strip()
            if not s:
                continue
            default = s.startswith("*")
            parts = s.lstrip("* ").split()
            if len(parts) < 3:
                continue
            rows.append({"name": parts[0], "running": parts[0] in running,
                         "version": int(parts[2]) if parts[2].isdigit() else None, "default": default})
        return rows

    async def start(self, distro: str) -> None:
        """持有一个前台 guest 进程,避免 systemd 服务被 WSL 空闲回收。"""
        require_windows("wsl.exe")
        if distro != self._distro:
            raise ValueError("Only the configured QTrade distro can be held")
        async with self._lifecycle_lock:
            if self._holder is not None and self._holder.returncode is None:
                return
            await self._release_holder()
            self._holder_stderr.clear()
            script = "import sys; print('QTRADE_WSL_READY', flush=True); sys.stdin.buffer.read()"
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                WSL, "-d", distro, "-u", "root", "--exec", "/opt/qtrade/agent/venv/bin/python", "-c", script,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **hidden_process_kwargs()))
            try:
                # 子进程创建也受取消保护:取得进程句柄后才能精确回收。
                self._holder = await asyncio.shield(spawn)
                self._holder_stderr_task = asyncio.create_task(
                    self._capture_holder_stderr(self._holder.stderr), name="wsl-holder-stderr")
                async with asyncio.timeout(HOLDER_START_TIMEOUT_S):
                    while True:
                        line = await self._holder.stdout.readline()
                        if line == HOLDER_READY:
                            if self._holder.returncode is not None:
                                raise RuntimeError("WSL holder exited before becoming ready")
                            return
                        if not line:
                            raise RuntimeError("WSL holder exited before becoming ready")
            except BaseException as e:
                if self._holder is None and not spawn.cancelled():
                    try:
                        self._holder = await asyncio.shield(spawn)
                        self._holder_stderr_task = asyncio.create_task(
                            self._capture_holder_stderr(self._holder.stderr), name="wsl-holder-stderr")
                    except Exception:
                        pass
                p = self._holder
                await self._release_holder()
                if isinstance(e, Exception) and p is not None:
                    diagnostic = decode_console_output(bytes(self._holder_stderr)).strip()
                    detail = f"{e} (returncode={p.returncode}, stderr={diagnostic!r})"
                    if isinstance(e, TimeoutError):
                        raise TimeoutError(detail) from e
                    raise RuntimeError(detail) from e
                raise

    async def _capture_holder_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(HOLDER_STDERR_LIMIT_BYTES):
            self._holder_stderr.extend(chunk)
            del self._holder_stderr[:-HOLDER_STDERR_LIMIT_BYTES]

    async def _release_holder(self) -> None:
        p = self._holder
        if p is None:
            return

        async def finish() -> None:
            try:
                if p.stdin is not None:
                    p.stdin.close()
                try:
                    await asyncio.wait_for(p.wait(), timeout=HOLDER_CLOSE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    if p.returncode is None:
                        try:
                            p.kill()
                        except ProcessLookupError:
                            pass
                    await asyncio.wait_for(p.wait(), timeout=HOLDER_CLOSE_TIMEOUT_S)
            finally:
                pump, self._holder_stderr_task = self._holder_stderr_task, None
                if pump is not None:
                    try:
                        await asyncio.wait_for(pump, timeout=HOLDER_CLOSE_TIMEOUT_S)
                    except (Exception, asyncio.CancelledError):
                        # 诊断流失败或迟迟不 EOF 不影响自有进程的回收。
                        pass
                    finally:
                        if not pump.done():
                            pump.cancel()
                        await asyncio.gather(pump, return_exceptions=True)

        cleanup = asyncio.create_task(finish())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise
        finally:
            if p.returncode is not None:
                self._holder = None

    async def close(self) -> None:
        """只释放本实例持有的子进程,不终止发行版或其它进程。"""
        async with self._lifecycle_lock:
            await self._release_holder()

    async def terminate(self, distro: str) -> None:
        async with self._lifecycle_lock:
            if distro == self._distro:
                await self._release_holder()
            rc, out = await self._run(["--terminate", distro])
            if rc != 0:
                raise RuntimeError(f"wsl --terminate {distro} failed:{out}")

    async def shutdown(self) -> None:
        """🔴 会中断用户**全部**发行版 —— 只有用户确认过时机时调用方才会走到这里(00 §11.6)。"""
        log.warning("执行 wsl --shutdown(调用方已带用户确认)", extra={"op": "wsl.shutdown", "code": "CONFIRMED"})
        async with self._lifecycle_lock:
            await self._release_holder()
            rc, out = await self._run(["--shutdown"], timeout_s=60.0)
            if rc != 0:
                raise RuntimeError(f"wsl --shutdown failed:{out}")

    async def exec(self, distro: str, argv: list[str]) -> tuple[int, str]:
        if argv and argv[0].startswith("--"):            # 发行版级命令(--import/--unregister),不进 --exec
            return await self._run(argv, timeout_s=600.0)
        return await self._run(["-d", distro, "-u", "root", "--exec"] + argv)

    async def exec_stdin(self, distro: str, argv: list[str], data: bytes) -> tuple[int, str]:
        return await self._run(["-d", distro, "-u", "root", "--exec", *argv], input_data=data)

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
