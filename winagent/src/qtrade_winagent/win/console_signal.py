"""Send CTRL_BREAK from a short-lived helper without changing the caller's console."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if (len(args) != 1 or not isinstance(args[0], str) or not 1 <= len(args[0]) <= 10
            or not args[0].isascii() or not args[0].isdigit()):
        return 2
    pid = int(args[0])
    if not 0 < pid < 0xFFFFFFFF or sys.platform != "win32":
        return 2
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.AttachConsole.argtypes = [wintypes.DWORD]
    kernel.AttachConsole.restype = wintypes.BOOL
    kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel.FreeConsole.argtypes = []
    kernel.FreeConsole.restype = wintypes.BOOL
    if not kernel.AttachConsole(pid):
        return 1
    try:
        return 0 if kernel.GenerateConsoleCtrlEvent(1, pid) else 1
    finally:
        kernel.FreeConsole()


if __name__ == "__main__":
    raise SystemExit(main())
