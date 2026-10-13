"""``WinPipe`` —— 命名管道(02 §2.4.1)。

- 消息模式 ``PIPE_TYPE_MESSAGE``;服务端在**服务**侧。
- **ACL**:内部管道只允许 **服务账号 + 该会话用户 SID**;控制台令牌管道的 ACL = 交互用户 SID 读写、Everyone 拒绝(C-04)。
- 🔴 **对端 SID 校验(R3-12)**:``GetNamedPipeClientProcessId`` → ``OpenProcessToken`` 取 SID,
  与安装期记录的安装用户 SID 比对 —— 仲裁之前先做这一步。
- 服务端用可取消的 OVERLAPPED I/O;用户端同步 I/O 在线程执行,不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from ..backends import PipeFrame
from . import require_windows

BUFSIZE = 1024 * 1024                       # 单帧 ≤ 1 MB(02 §2.4.1 [ipc] max_frame_kb)


def _error_code(exc: Exception) -> Any:
    return getattr(exc, "winerror", exc.args[0] if exc.args else None)


async def _overlapped(handle: object, action) -> tuple[int, Any]:
    """Keep the OVERLAPPED and its buffer alive through completion or cancel."""
    import pywintypes
    import win32event
    import win32file

    operation = pywintypes.OVERLAPPED()
    operation.hEvent = win32event.CreateEvent(None, True, False, None)
    pending = False
    try:
        try:
            result = action(operation)
            if result == 535:  # pywin32 ConnectNamedPipe returns this value.
                return 0, None
            pending = True
        except Exception as exc:
            if _error_code(exc) == 535:  # A pipe client connected before accept.
                return 0, None
            if _error_code(exc) != 997:  # ERROR_IO_PENDING
                raise
            result, pending = None, True
        while True:
            try:
                size = win32file.GetOverlappedResult(handle, operation, False)
                pending = False
                return size, result
            except Exception as exc:
                if _error_code(exc) != 996:  # ERROR_IO_INCOMPLETE
                    pending = False
                    raise
                await asyncio.sleep(0.02)
    finally:
        try:
            if pending:
                # pywin32 exposes CancelIo; submission and cancellation both
                # run on this event-loop thread. Never cancel completed I/O.
                win32file.CancelIo(handle)
                try:
                    win32file.GetOverlappedResult(handle, operation, True)
                except Exception as exc:
                    if _error_code(exc) != 995:  # ERROR_OPERATION_ABORTED
                        raise
        finally:
            operation.hEvent.Close()


class WinPipeConn:
    def __init__(self, handle: object, *, peer_sid: Optional[str], max_frame_kb: int = 1024,
                 peer_elevated: bool = False, overlapped: bool = False):
        self._h = handle
        self._peer_sid = peer_sid
        self._max = max_frame_kb * 1024
        self._buf = b""
        self._broken = False                      # Peek 探到句柄已断 ⇒ recv 直接回 None(= 断开)
        self.peer_elevated = peer_elevated
        self._overlapped = overlapped

    @property
    def peer_sid(self) -> Optional[str]:
        return self._peer_sid

    async def send(self, frame: PipeFrame) -> None:
        await self.send_json(frame.to_wire())

    async def send_json(self, value: dict[str, Any]) -> None:
        import win32file
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(raw) > self._max:
            raise ValueError(f"单帧超过 {self._max} 字节(02 §2.4.1 max_frame_kb)")
        if self._overlapped:
            written, _ = await _overlapped(self._h, lambda op: win32file.WriteFile(self._h, raw, op))
            if written != len(raw):
                raise OSError("Incomplete pipe write")
        else:
            await asyncio.to_thread(win32file.WriteFile, self._h, raw)

    async def recv(self) -> Optional[PipeFrame]:
        value = await self.recv_json()
        return PipeFrame.from_wire(value) if value is not None else None

    async def recv_json(self) -> Optional[dict[str, Any]]:
        if self._broken:
            return None
        import win32file
        while b"\n" not in self._buf:
            if self._broken:
                return None
            try:
                if self._overlapped:
                    buffer = win32file.AllocateReadBuffer(BUFSIZE)
                    size, _ = await _overlapped(self._h, lambda op: win32file.ReadFile(self._h, buffer, op))
                    data = bytes(buffer[:size])
                else:
                    rc, data = await asyncio.to_thread(win32file.ReadFile, self._h, BUFSIZE)
            except Exception:
                return None
            if not data:
                return None
            self._buf += data
            if len(self._buf) > self._max:
                raise ValueError("累计帧超过 max_frame_kb")
        line, _, self._buf = self._buf.partition(b"\n")
        if not line.strip():
            return None
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def inbound_ready(self) -> bool:
        """有没有一帧在等读。用 Peek,避免同步 ReadFile 占住句柄、把心跳 WriteFile 卡住。

        对端断开时 ``PeekNamedPipe`` 抛 ``pywintypes.error``(ERROR_BROKEN_PIPE 等):按断开处理——
        回 ``True`` 让调用方去 ``recv``,``recv`` 见 ``_broken`` 回 ``None``,``run()`` 走正常退出路径。
        """
        import win32pipe
        try:
            _data, avail, _left = win32pipe.PeekNamedPipe(self._h, 0)
        except Exception:
            self._broken = True
            return True
        return int(avail or 0) > 0

    async def close(self) -> None:
        import win32file
        try:
            await asyncio.to_thread(win32file.CloseHandle, self._h)
        except Exception:
            pass


class WinPipeBackend:
    def __init__(self, *, sddl: Optional[str] = None, max_frame_kb: int = 1024):
        # 默认 SDDL:仅 SYSTEM(SY)与本地管理员(BA)完全控制;安装用户 SID 由 ``serve`` 追加
        self._sddl = sddl or "D:(A;;GA;;;SY)(A;;GA;;;BA)"
        self._max_frame_kb = max_frame_kb
        self._ready: dict[str, asyncio.Event] = {}
        self._claimed: set[str] = set()

    async def wait_ready(self, name: str) -> None:
        await asyncio.wait_for(self._ready.setdefault(name, asyncio.Event()).wait(), 5)

    def _sa(self, extra_sid: Optional[str]) -> object:
        import win32security
        sddl = self._sddl + (f"(A;;GRGW;;;{extra_sid})" if extra_sid else "")
        sa = win32security.SECURITY_ATTRIBUTES()
        sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, win32security.SDDL_REVISION_1)
        return sa

    async def serve(self, name: str, *, allow_sid: Optional[str] = None) -> WinPipeConn:
        require_windows("命名管道服务端")
        import win32file
        import win32pipe
        # OVERLAPPED accept is cancellable. A blocked ConnectNamedPipe worker
        # would keep asyncio's executor alive after SCM requests stop.
        access = win32pipe.PIPE_ACCESS_DUPLEX | 0x40000000
        access |= 0x00080000 if name not in self._claimed else 0
        h = win32pipe.CreateNamedPipe(
            name, access,  # FILE_FLAG_FIRST_PIPE_INSTANCE guards preexisting impostors.
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT
            | 0x8,  # PIPE_REJECT_REMOTE_CLIENTS
            win32pipe.PIPE_UNLIMITED_INSTANCES, BUFSIZE, BUFSIZE, 0, self._sa(allow_sid))
        self._claimed.add(name)
        self._ready.setdefault(name, asyncio.Event()).set()
        try:
            await _overlapped(h, lambda op: win32pipe.ConnectNamedPipe(h, op))
            sid, elevated = _client_identity(h)
            return WinPipeConn(h, peer_sid=sid, peer_elevated=elevated,
                               overlapped=True, max_frame_kb=self._max_frame_kb)
        except BaseException:
            win32file.CloseHandle(h)
            raise

    async def connect(self, name: str, *, peer_sid: Optional[str] = None) -> WinPipeConn:
        require_windows("命名管道客户端")
        import win32file

        def go():
            return win32file.CreateFile(name, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                                        win32file.OPEN_EXISTING, 0, None)
        h = await asyncio.to_thread(go)
        return WinPipeConn(h, peer_sid=None, max_frame_kb=self._max_frame_kb)


def _client_sid(handle: object) -> Optional[str]:
    """R3-12:取对端进程 SID —— 仲裁之前必须先拿到它。"""
    return _client_identity(handle)[0]


def _client_identity(handle: object) -> tuple[Optional[str], bool]:
    ph = token = None
    try:
        import win32api
        import win32con
        import win32pipe
        import win32security
        pid = win32pipe.GetNamedPipeClientProcessId(handle)
        ph = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        token = win32security.OpenProcessToken(ph, win32security.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        elevated = bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
        return win32security.ConvertSidToStringSid(sid), elevated
    except Exception:
        return None, False
    finally:
        if token is not None:
            token.Close()
        if ph is not None:
            ph.Close()
