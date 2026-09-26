"""``WinPipe`` —— 命名管道(02 §2.4.1)。

- 消息模式 ``PIPE_TYPE_MESSAGE``;服务端在**服务**侧。
- **ACL**:内部管道只允许 **服务账号 + 该会话用户 SID**;控制台令牌管道的 ACL = 交互用户 SID 读写、Everyone 拒绝(C-04)。
- 🔴 **对端 SID 校验(R3-12)**:``GetNamedPipeClientProcessId`` → ``OpenProcessToken`` 取 SID,
  与安装期记录的安装用户 SID 比对 —— 仲裁之前先做这一步。
- 读写一律 ``asyncio.to_thread``(R-09 §2.3.1 ⑤:绝不在事件循环里阻塞)。
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from ..backends import PipeFrame
from . import require_windows

BUFSIZE = 1024 * 1024                       # 单帧 ≤ 1 MB(02 §2.4.1 [ipc] max_frame_kb)


class WinPipeConn:
    def __init__(self, handle: object, *, peer_sid: Optional[str], max_frame_kb: int = 1024):
        self._h = handle
        self._peer_sid = peer_sid
        self._max = max_frame_kb * 1024
        self._buf = b""
        self._broken = False                      # Peek 探到句柄已断 ⇒ recv 直接回 None(= 断开)

    @property
    def peer_sid(self) -> Optional[str]:
        return self._peer_sid

    async def send(self, frame: PipeFrame) -> None:
        import win32file
        raw = json.dumps(frame.to_wire(), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(raw) > self._max:
            raise ValueError(f"单帧超过 {self._max} 字节(02 §2.4.1 max_frame_kb)")
        await asyncio.to_thread(win32file.WriteFile, self._h, raw)

    async def recv(self) -> Optional[PipeFrame]:
        if self._broken:
            return None
        import win32file
        while b"\n" not in self._buf:
            try:
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
        return PipeFrame.from_wire(json.loads(line.decode("utf-8")))

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

    def _sa(self, extra_sid: Optional[str]) -> object:
        import win32security
        sddl = self._sddl + (f"(A;;GRGW;;;{extra_sid})" if extra_sid else "")
        sa = win32security.SECURITY_ATTRIBUTES()
        sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, win32security.SDDL_REVISION_1)
        return sa

    async def serve(self, name: str, *, allow_sid: Optional[str] = None) -> WinPipeConn:
        require_windows("命名管道服务端")
        import win32pipe

        def go():
            h = win32pipe.CreateNamedPipe(
                name, win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES, BUFSIZE, BUFSIZE, 0, self._sa(allow_sid))
            win32pipe.ConnectNamedPipe(h, None)
            return h
        h = await asyncio.to_thread(go)
        return WinPipeConn(h, peer_sid=_client_sid(h), max_frame_kb=self._max_frame_kb)

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
    try:
        import win32api
        import win32con
        import win32pipe
        import win32security
        pid = win32pipe.GetNamedPipeClientProcessId(handle)
        ph = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        token = win32security.OpenProcessToken(ph, win32security.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    except Exception:
        return None
