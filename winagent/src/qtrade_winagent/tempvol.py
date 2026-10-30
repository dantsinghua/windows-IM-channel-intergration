"""会话代理的临时目录及其所在卷(R6-118;04 §2.3「R6-118 临时目录卷空闲」段 / 05 §2.4.10)。

卷 = 会话代理进程(及其拉起、继承同一环境的 chatlog)的 ``%TEMP%`` 所在卷,不是服务(LocalSystem)的临时目录。
chatlog 的 ``filecopy`` 副本落在 ``<临时目录>\\filecopy_chatlog``。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Optional

FILECOPY_DIRNAME = "filecopy_chatlog"


def user_temp_dir() -> str:
    """与 Windows ``GetTempPath`` 同序(TMP → TEMP → USERPROFILE),chatlog 的 ``os.TempDir`` 也按此取。"""
    if os.name == "nt":
        for key in ("TMP", "TEMP", "USERPROFILE"):
            value = os.environ.get(key)
            if value and os.path.isabs(value):
                return os.path.abspath(value)
    return tempfile.gettempdir()


def volume_of(path: str) -> str:
    """Windows 返回卷根(``C:`` / UNC 共享根);其它平台返回挂载点。"""
    full = os.path.abspath(path)
    if os.name == "nt":
        drive = os.path.splitdrive(full)[0]
        if drive:
            return drive.upper() if len(drive) == 2 and drive[1] == ":" else drive
    current = full
    while not os.path.ismount(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def sample(path: Optional[str] = None) -> dict[str, Any]:
    """``{volume, free_bytes, total_bytes}``;目录不可访问时抛 ``OSError``。"""
    target = path or user_temp_dir()
    usage = shutil.disk_usage(target)
    return {"volume": volume_of(target), "free_bytes": int(usage.free), "total_bytes": int(usage.total)}
