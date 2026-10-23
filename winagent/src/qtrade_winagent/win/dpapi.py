"""``WinCrypto`` —— DPAPI 机器级 + 附加熵(05 §2.2.1 方案 B)+ 熵文件 ACL。

``CryptProtectData(pDataIn, …, pOptionalEntropy=E, …, dwFlags=CRYPTPROTECT_LOCAL_MACHINE)``;
熵文件 ACL **仅 SYSTEM + Administrators 完全控制、其余无权限、继承关闭**。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from . import require_windows

CRYPTPROTECT_LOCAL_MACHINE = 0x4
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class WinCrypto:
    def __init__(self, *, scope: str = "machine"):
        self._scope = scope

    # ---------------------------------------------------------------- DPAPI
    def _blob(self, data: bytes) -> Any:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(self, fn_name: str, data: bytes, entropy: bytes) -> bytes:
        require_windows("DPAPI")
        import ctypes
        from ctypes import wintypes
        crypt32 = ctypes.windll.crypt32                      # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32                    # type: ignore[attr-defined]

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def mk(b: bytes):
            buf = ctypes.create_string_buffer(b, len(b))
            return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        blob_in, _k1 = mk(data)
        ent, _k2 = mk(entropy)
        out = DATA_BLOB()
        flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if self._scope == "machine" else 0)
        fn = getattr(crypt32, fn_name)
        ok = fn(ctypes.byref(blob_in), None, ctypes.byref(ent), None, None, flags, ctypes.byref(out))
        if not ok:
            raise ValueError(f"{fn_name} 失败:GetLastError={ctypes.GetLastError()}")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    async def protect(self, plaintext: bytes, entropy: bytes) -> bytes:
        return await asyncio.to_thread(self._call, "CryptProtectData", plaintext, entropy)

    async def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        return await asyncio.to_thread(self._call, "CryptUnprotectData", blob, entropy)

    def random_bytes(self, n: int) -> bytes:
        return os.urandom(n)

    # ---------------------------------------------------------------- ACL
    def tighten_acl(self, path: str) -> None:
        """Use well-known SIDs and verify the protected DACL after writing."""
        require_windows("ACL 收紧")
        import win32security
        sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            "D:P(A;;FA;;;SY)(A;;FA;;;BA)", win32security.SDDL_REVISION_1)
        win32security.SetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, sd.GetSecurityDescriptorDacl(), None)
        if not self.acl_is_tight(path):
            raise PermissionError("Vault ACL verification failed")

    def acl_is_tight(self, path: str) -> bool:
        """自检:除 SYSTEM / Administrators 外还有别的主体 ⇒ 判「已放宽」(→ ``VAULT_ENTROPY_MISSING``)。"""
        require_windows("ACL 自检")
        import win32security
        try:
            sd = win32security.GetNamedSecurityInfo(path, win32security.SE_FILE_OBJECT,
                                                   win32security.DACL_SECURITY_INFORMATION)
            control, _ = sd.GetSecurityDescriptorControl()
            if not control & 0x1000:  # SE_DACL_PROTECTED
                return False
            acl = sd.GetSecurityDescriptorDacl()
            if acl is None or acl.GetAceCount() != 2:
                return False
            principals = set()
            for index in range(acl.GetAceCount()):
                (kind, flags), mask, sid = acl.GetAce(index)
                if kind != 0 or flags != 0 or mask != 0x1F01FF:  # ACCESS_ALLOWED, FILE_ALL_ACCESS
                    return False
                principals.add(win32security.ConvertSidToStringSid(sid))
            return principals == {"S-1-5-18", "S-1-5-32-544"}
        except Exception:
            return False
