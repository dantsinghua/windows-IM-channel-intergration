"""Windows process mutex: real contention is distinct from a Win32 failure."""
from __future__ import annotations
import ctypes
import time

class NativeMutex:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k.WaitForSingleObject.restype = ctypes.c_ulong
        k.ReleaseMutex.argtypes = [ctypes.c_void_p]
        k.ReleaseMutex.restype = ctypes.c_int
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = ctypes.c_int
    def create(self, name):
        handle = self.kernel.CreateMutexW(None, False, name)
        if not handle: raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        return handle
    def wait(self, handle, timeout_ms):
        value = self.kernel.WaitForSingleObject(handle, timeout_ms)
        error = ctypes.get_last_error()
        return value, error
    def release(self, handle):
        if not self.kernel.ReleaseMutex(handle):
            raise OSError(ctypes.get_last_error(), "ReleaseMutex failed")
    def close(self, handle):
        self.kernel.CloseHandle(handle)

class LauncherLock:
    def __init__(self, name, api=None):
        self.api = api if api is not None else NativeMutex()
        self.handle = self.api.create(name)
        self.owned = False
    def acquire(self, timeout_ms=0):
        if self.handle is None: raise RuntimeError("Mutex already closed")
        if self.owned: return True
        value, error = self.api.wait(self.handle, timeout_ms)
        value &= 0xFFFFFFFF
        if value in (0, 0x80):
            self.owned = True
            return True
        if value == 258: return False
        if value == 0xFFFFFFFF: raise OSError(error, "Launcher mutex wait failed, not another running build")
        raise RuntimeError("Unexpected mutex result: " + str(value))
    def wait_until_free(self, timeout_s=900, progress=lambda: None, clock=time.monotonic):
        deadline = clock() + timeout_s
        while clock() < deadline:
            progress()
            if self.acquire(min(1000, max(1, int((deadline-clock())*1000)))): return True
        return False
    def close(self):
        if self.handle is None: return
        try:
            if self.owned: self.api.release(self.handle)
        finally:
            self.api.close(self.handle)
            self.handle = None
            self.owned = False
