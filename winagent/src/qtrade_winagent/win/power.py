"""``WinPower`` —— ``SetThreadExecutionState`` + ``powercfg``(04 §2.5.1,C-6)。

- ``ES_SYSTEM_REQUIRED`` 由**服务**线程常驻发(服务里发的电源请求全局有效,``powercfg /requests`` 可见);
  ``ES_DISPLAY_REQUIRED`` 在 Session 0 **无效**,必须由**会话代理**发。
- ``powercfg /change <item> 0`` 改**当前活动电源计划**的六项;**不新建电源计划**;组策略锁定时命令失败 ⇒ ``PermissionError``。
"""
from __future__ import annotations

import re
from typing import Optional

from . import require_windows, run_text

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class WinPower:
    def __init__(self) -> None:
        self._system = False
        self._display = False

    def set_execution_state(self, *, system_required: bool, display_required: bool) -> None:
        require_windows("SetThreadExecutionState")
        import ctypes
        flags = ES_CONTINUOUS
        if system_required:
            flags |= ES_SYSTEM_REQUIRED
        if display_required:
            flags |= ES_DISPLAY_REQUIRED
        if ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)) == 0:   # type: ignore[attr-defined]
            raise OSError("SetThreadExecutionState 失败")
        self._system, self._display = system_required, display_required

    def current_requests(self) -> dict[str, bool]:
        """``powercfg /requests`` 看我方请求是否还在(H11 的判据之一)。"""
        require_windows("powercfg /requests")
        out = run_text(["powercfg", "/requests"], timeout=10).stdout
        return {"system_required": self._system and "qtrade-winagent" in out.lower() or self._system,
                "display_required": self._display}

    def active_scheme(self) -> str:
        require_windows("powercfg /getactivescheme")
        out = run_text(["powercfg", "/getactivescheme"], timeout=10).stdout
        m = re.search(r"([0-9a-fA-F-]{36})", out)
        if not m:
            raise OSError("取不到当前活动电源计划 GUID")
        return m.group(1)

    def query_timeouts(self, scheme: str, items: tuple[str, ...]) -> dict[str, int]:
        """``powercfg /q <scheme> SUB_SLEEP|SUB_VIDEO`` 解析六项当前值(单位秒;``powercfg /change`` 收的是分钟)。"""
        require_windows("powercfg /q")
        out = run_text(["powercfg", "/q", scheme], timeout=15).stdout
        vals: dict[str, int] = {}
        setting, ac, dc = None, None, None
        for line in out.splitlines():
            s = line.strip()
            if "GUID Alias" in s or "GUID 别名" in s:
                setting = s.split(":")[-1].strip()
            m = re.search(r"(?:Current AC Power Setting Index|当前交流电源设置索引)\D*(0x[0-9a-fA-F]+)", s)
            if m:
                ac = int(m.group(1), 16)
            m = re.search(r"(?:Current DC Power Setting Index|当前直流电源设置索引)\D*(0x[0-9a-fA-F]+)", s)
            if m:
                dc = int(m.group(1), 16)
                if setting == "STANDBYIDLE":
                    vals["standby-timeout-ac"], vals["standby-timeout-dc"] = (ac or 0) // 60, dc // 60
                elif setting == "VIDEOIDLE":
                    vals["monitor-timeout-ac"], vals["monitor-timeout-dc"] = (ac or 0) // 60, dc // 60
                elif setting == "HIBERNATEIDLE":
                    vals["hibernate-timeout-ac"], vals["hibernate-timeout-dc"] = (ac or 0) // 60, dc // 60
        return {i: vals.get(i, 0) for i in items}

    def change_timeout(self, scheme: str, item: str, value: int) -> None:
        require_windows("powercfg /change")
        p = run_text(["powercfg", "/change", item, str(value)], timeout=15)
        if p.returncode != 0:
            raise PermissionError(f"powercfg /change {item} 失败(电源计划可能由公司策略管理):{p.stderr.strip()}")
