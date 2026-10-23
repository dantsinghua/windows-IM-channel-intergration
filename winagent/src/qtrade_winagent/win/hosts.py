"""``WinHosts`` —— 整机 ``hosts`` 读写与可写性(04 §2.5.4 B-2;执行体 = **服务**,LocalSystem 才写得动)。

- 路径 ``%SystemRoot%\\System32\\drivers\\etc\\hosts``。
- 可写性用 ``GENERIC_WRITE`` 打开试探(**不改内容**);只读 / 被 EDR 或组策略保护 ⇒ ``writable()=False``、``write()`` 抛
  ``PermissionError`` —— 上层降级为告警 ``H21_WECHAT_HOSTS_BLOCK_FAILED``,**不绕 EDR/策略**。
- 写前备份到 ``[wechat] hosts_backup_dir``,保留 ``hosts_backup_keep`` 份。
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Optional

from . import require_windows, run_text

DEFAULT_HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")


class WinHosts:
    def __init__(self, *, path: Optional[str] = None, backup_dir: Optional[str] = None, backup_keep: int = 5):
        self._path = path or DEFAULT_HOSTS
        self._backup_dir = backup_dir
        self._backup_keep = backup_keep

    def read(self) -> str:
        with open(self._path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def writable(self) -> bool:
        try:
            with open(self._path, "a", encoding="utf-8"):
                return True
        except OSError:
            return False

    def write(self, text: str) -> None:
        if self._backup_dir:
            os.makedirs(self._backup_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copyfile(self._path, os.path.join(self._backup_dir, f"hosts.bak-{stamp}"))
            olds = sorted(n for n in os.listdir(self._backup_dir) if n.startswith("hosts.bak-"))
            for n in olds[:-self._backup_keep] if len(olds) > self._backup_keep else []:
                os.remove(os.path.join(self._backup_dir, n))
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            raise PermissionError(f"hosts 写入被拒(只读 / EDR / 组策略):{e}") from e

    def resolve(self, domain: str) -> Optional[str]:
        """写后复核:``Resolve-DnsName <域名>`` 应回 ``0.0.0.0``(04 H21 判据)。"""
        require_windows("Resolve-DnsName")
        out = run_text(["powershell", "-NoProfile", "-Command",
                        f"(Resolve-DnsName -Name {domain} -Type A -ErrorAction SilentlyContinue "
                        f"| Select-Object -First 1).IPAddress"], timeout=10).stdout.strip()
        return out or None
