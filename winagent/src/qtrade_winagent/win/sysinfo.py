"""``WinSys`` —— 04 §2.2 采样项 S1/S2/S3/S4/S5/S6/S13/S14/S15 与 §2.9 校时的真实现。

关键取值口径(照 04 §2.2 那张表):
- S1 用 PDH ``\\Processor Information(_Total)\\% Processor Utility``(``psutil.cpu_percent`` 亦读它);
  **不用 ``% Processor Time``** —— 睿频机器上后者会超 100%、与任务管理器不一致。
- S5 ``vmmem``(Win10)/``vmmemWSL``(Win11)**两名都找**。
- S6 微信 4.x ``Weixin.exe`` / 3.x ``WeChat.exe`` **两名都找**。
- S13 ``WTSQuerySessionInformation(WTSSessionInfoEx)`` 取活动会话 ``SessionFlags``;备用 ``LogonUI.exe`` 是否存在。
- S15 三处注册表任一存在即待重启。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from ..backends import ProcInfo
from . import require_windows, run_text

REBOOT_KEYS = (
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending", None),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired", None),
    (r"SYSTEM\CurrentControlSet\Control\Session Manager", "PendingFileRenameOperations"),
)
WTS_SESSIONSTATE_LOCK = 0


class WinSys:
    def __init__(self) -> None:
        self._last_resume_ms: Optional[int] = None

    def cpu_percent(self) -> float:
        import psutil
        return float(psutil.cpu_percent(interval=None))

    def memory_mb(self) -> dict[str, float]:
        import psutil
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        return {"total_mb": vm.total / 1e6, "available_mb": vm.available / 1e6,
                "committed_mb": (vm.total - vm.available + sm.used) / 1e6}

    def disk_free_mb(self, path: str) -> tuple[float, float]:
        import psutil
        u = psutil.disk_usage(path)
        return u.free / 1e6, u.total / 1e6

    def path_exists(self, path: str) -> bool:
        import os
        return os.path.exists(path)

    def net_throughput_kbps(self) -> dict[str, tuple[float, float]]:
        import psutil
        now = time.time()
        cur = psutil.net_io_counters(pernic=True)
        prev = getattr(self, "_prev_net", None)
        self._prev_net = (now, cur)                                   # type: ignore[attr-defined]
        if prev is None:
            return {k: (0.0, 0.0) for k in cur}
        t0, c0 = prev
        dt = max(1e-6, now - t0)
        return {k: (((v.bytes_recv - c0[k].bytes_recv) * 8 / 1000) / dt,
                    ((v.bytes_sent - c0[k].bytes_sent) * 8 / 1000) / dt) for k, v in cur.items() if k in c0}

    def processes(self, names: tuple[str, ...]) -> list[ProcInfo]:
        import psutil
        wanted = {n.lower() for n in names}
        out: list[ProcInfo] = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "create_time"]):
            try:
                nm = (p.info.get("name") or "")
                if nm.lower() not in wanted:
                    continue
                mi = p.info.get("memory_info")
                out.append(ProcInfo(pid=p.info["pid"], name=nm, rss_mb=(mi.rss / 1e6) if mi else 0.0,
                                    cpu_pct=float(p.info.get("cpu_percent") or 0.0),
                                    started_ms=int((p.info.get("create_time") or 0) * 1000)))
            except Exception:                                          # 进程可能在遍历期间消失
                continue
        return out

    def session_locked(self) -> bool:
        require_windows("WTS 会话状态")
        try:
            import win32ts
            sid = win32ts.WTSGetActiveConsoleSessionId()
            info = win32ts.WTSQuerySessionInformation(None, sid, win32ts.WTSSessionInfoEx)
            return bool(getattr(info, "SessionFlags", 1) == WTS_SESSIONSTATE_LOCK)
        except Exception:
            return bool(self.processes(("LogonUI.exe",)))              # 备用判据(04 §2.2 S13)

    def active_session(self) -> Optional[dict[str, Any]]:
        require_windows("WTS 会话枚举")
        import win32ts
        sid = win32ts.WTSGetActiveConsoleSessionId()
        if sid in (0xFFFFFFFF, None):
            return None
        user = win32ts.WTSQuerySessionInformation(None, sid, win32ts.WTSUserName)
        return {"session_id": int(sid), "user": user, "sid": _user_sid(user)}

    def pending_reboot(self) -> bool:
        require_windows("注册表读取")
        import winreg
        for path, name in REBOOT_KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                    if name is None:
                        return True
                    winreg.QueryValueEx(k, name)
                    return True
            except OSError:
                continue
        return False

    def last_resume_ms(self) -> Optional[int]:
        """R3-5:主机上次从睡眠/休眠唤醒的时刻。取系统日志 Kernel-Power 事件 ID 107(恢复)。"""
        require_windows("事件日志查询")
        try:
            out = run_text(
                ["wevtutil", "qe", "System", "/q:*[System[(EventID=107)]]", "/c:1", "/rd:true", "/f:text"],
                timeout=5).stdout
            for line in out.splitlines():
                if "Date:" in line:
                    import datetime
                    ts = line.split("Date:", 1)[1].strip()
                    return int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return self._last_resume_ms
        return self._last_resume_ms

    def w32time(self) -> dict[str, Any]:
        require_windows("w32tm 查询")
        out = run_text(["w32tm", "/query", "/status"], timeout=5).stdout
        source, last = None, None
        for line in out.splitlines():
            low = line.lower()
            if low.startswith("source:") or "源:" in line:
                source = line.split(":", 1)[1].strip()
            if "last successful sync time" in low or "上次成功同步时间" in line:
                import datetime
                raw = line.split(":", 1)[1].strip()
                try:
                    last = int(datetime.datetime.strptime(raw, "%Y/%m/%d %H:%M:%S").timestamp() * 1000)
                except ValueError:
                    last = None
        return {"source": source, "last_sync_ms": last}

    def tz_offset_min(self) -> int:
        return -int(time.timezone / 60) if not time.daylight else -int(time.altzone / 60)

    def file_version(self, path: str) -> Optional[str]:
        """``GetFileVersionInfo`` 取 ``Weixin.exe`` 的文件版本(04 §2.5.2:**不读注册表 DisplayVersion**,实测陈旧)。"""
        require_windows("文件版本资源")
        try:
            import win32api
            info = win32api.GetFileVersionInfo(path, "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:
            return None

    def policy_lockscreen(self) -> Optional[int]:
        """组策略锁屏(04 §2.5.1 最后一行):``InactivityTimeoutSecs``;**只检测告知,不绕过**。"""
        require_windows("注册表读取")
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as k:
                return int(winreg.QueryValueEx(k, "InactivityTimeoutSecs")[0])
        except OSError:
            return None


def _user_sid(user: Optional[str]) -> Optional[str]:
    if not user:
        return None
    try:
        import win32security
        return win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, user)[0])
    except Exception:
        return None
