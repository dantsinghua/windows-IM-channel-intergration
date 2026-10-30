"""Owned Narrator process identities and handle-bound termination."""
from __future__ import annotations

import ctypes
import ntpath
import os
from ctypes import wintypes
from typing import Any, Optional


def executable() -> str:
    import win32api
    return ntpath.join(win32api.GetSystemDirectory(), "Narrator.exe")


def identity(handle: Any) -> dict[str, Any]:
    import win32process
    import win32security

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
    pid = kernel32.GetProcessId(int(handle))
    path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    if not pid or not kernel32.QueryFullProcessImageNameW(int(handle), 0, path, ctypes.byref(size)):
        raise OSError("Process identity unavailable")
    token = win32security.OpenProcessToken(handle, 0x0008)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        session = win32security.GetTokenInformation(token, win32security.TokenSessionId)
        return {"pid": pid, "sid": win32security.ConvertSidToStringSid(sid), "session_id": session,
                "created": win32process.GetProcessTimes(handle)["CreationTime"].isoformat(),
                "exe": ntpath.normcase(path.value)}
    finally:
        token.Close()


def process_identity(pid: int) -> Optional[dict[str, Any]]:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        import win32api
        handle = win32api.OpenProcess(0x1000, False, pid)
        try:
            return identity(handle)
        finally:
            handle.Close()
    except Exception:
        return None


def current_owner() -> Optional[dict[str, Any]]:
    return process_identity(os.getpid())


def scoped_processes() -> list[dict[str, Any]]:
    from .proc import WinProc
    owner = current_owner()
    if owner is None:
        raise OSError("Current session identity unavailable")
    out = []
    for proc in WinProc().find("Narrator.exe"):
        info = process_identity(proc.pid)
        if info and info["sid"] == owner["sid"] and info["session_id"] == owner["session_id"]:
            out.append(info)
    return out


def start_owned() -> dict[str, Any]:
    from win32com.shell import shell, shellcon
    owner = current_owner()
    if not owner or not owner["session_id"]:
        raise OSError("Interactive owner unavailable")
    # ShellExecute is required by Narrator's UIAccess manifest. A returned handle
    # proves which process this invocation created; never adopt a name-only match.
    launched = shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_NOCLOSEPROCESS, lpVerb="open",
                                   lpFile=executable(), nShow=1)
    handle = launched.get("hProcess")
    if not handle:
        raise OSError("Narrator launch did not return an owned process")
    try:
        target = identity(handle)
        if (target["sid"] != owner["sid"] or target["session_id"] != owner["session_id"]
                or target["exe"] != ntpath.normcase(executable())):
            raise OSError("Narrator launch identity mismatch")
        return target
    finally:
        handle.Close()


def stop_owned(target: Any, *, owner_sid: str, owner_session_id: int) -> bool:
    # The caller supplies independently verified owner facts, not HTTP input.
    if (not isinstance(target, dict) or type(target.get("pid")) is not int or target["pid"] <= 0
            or not owner_sid or type(owner_session_id) is not int or owner_session_id <= 0
            or target.get("sid") != owner_sid or type(target.get("session_id")) is not int
            or target.get("session_id") != owner_session_id
            or not isinstance(target.get("created"), str) or not target["created"]):
        return False
    try:
        import win32api
        import win32event
        if target.get("exe") != ntpath.normcase(executable()):
            return False
        # Query, terminate, and wait use one handle, so PID reuse cannot retarget it.
        handle = win32api.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, target["pid"])
        try:
            if identity(handle) != target:
                return False
            win32api.TerminateProcess(handle, 0)
            return win32event.WaitForSingleObject(handle, 1500) == win32event.WAIT_OBJECT_0
        finally:
            handle.Close()
    except Exception:
        return False
