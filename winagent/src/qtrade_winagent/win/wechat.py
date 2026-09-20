"""``WinWeChat`` —— 05 §2.4 的微信 / chatlog / 讲述人 / 取钥 / 读写(**会话代理**执行)。

M3.5 骨架:本类把每个动作落到具体的外部程序与文件事实上,**仍待真机验证**(见 README「须在 Windows 真机验证清单」):
- 微信定位:注册表路径 → ``Weixin.exe`` 的 ``FileVersion``(**不读 DisplayVersion**,实测陈旧);
  数据根来自 ``%APPDATA%\\Tencent\\xwechat\\config\\<32hex>.ini`` 取 LastWrite 最新的那个的第一行(05 §2.4.9)。
- chatlog:``chatlog.exe key`` 起 hook(DLL 由 ``wxkey_dlls`` 顺序或矩阵 ``verified`` 决定);
  落盘判据 = ``~/.chatlog/chatlog.json`` 的 ``history[0].data_key`` 与 ``img_key`` **同时非空**(05 §2.4.4a)。
- UI 树可见性 / 发送:pywinauto(UIA)+ pyweixin;**锁屏期间不可用**(安全桌面,不模拟输入绕过)。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import subprocess
from typing import Any, Optional

from ..logfmt import get_logger
from . import require_windows

log = get_logger("wechat")
CHATLOG_JSON = os.path.join(os.path.expanduser("~"), ".chatlog", "chatlog.json")
NARRATOR = "narrator.exe"


class WinWeChat:
    def __init__(self, *, exe_path: str = "", chatlog_dir: str = "", chatlog_port: int = 5030,
                 main_wnd_class: str = "Qt51514QWindowIcon", process_close_grace_s: int = 10):
        self._exe = exe_path
        self._chatlog_dir = chatlog_dir
        self._port = chatlog_port
        self._cls = main_wnd_class
        self._grace = process_close_grace_s
        self._wechat_pid: Optional[int] = None
        self._chatlog_pid: Optional[int] = None
        self._narrator_pid: Optional[int] = None
        self._dll: Optional[str] = None

    # ---------------------------------------------------------------- 定位
    def locate(self) -> dict[str, Any]:
        require_windows("微信定位")
        path = self._exe or _registry_weixin_path()
        version = None
        if path and os.path.exists(path):
            from .sysinfo import WinSys
            version = WinSys().file_version(path)
        data_root = _data_root_from_ini()
        return {"installed": bool(path and os.path.exists(path)), "path": path, "version": version,
                "data_root": data_root,
                "data_dir": os.path.join(data_root, "xwechat_files") if data_root else None,
                "appdata_dir": os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat")}

    # ---------------------------------------------------------------- 进程
    async def launch(self) -> int:
        require_windows("拉起微信")
        path = self._exe or _registry_weixin_path()
        p = await asyncio.create_subprocess_exec(path)
        self._wechat_pid = p.pid
        return p.pid

    async def logout(self, *, mode: str, grace_s: int) -> str:
        """D-1:默认 ``process`` —— 先 ``WM_CLOSE``,``grace_s`` 未退再 ``taskkill /F``;``ui`` 模式失败自动回落 ``process``。"""
        from .proc import WinProc
        proc = WinProc()
        if mode == "ui":
            try:
                if await self._ui_logout():
                    return "ui"
            except Exception:
                log.warning("UI 退出登录失败,回落 process 模式", extra={"op": "wechat.logout", "code": "FALLBACK"})
        pids = [p.pid for p in proc.find("Weixin.exe")] + [p.pid for p in proc.find("WeChat.exe")]
        for pid in pids:
            await proc.close_window(pid)
        await asyncio.sleep(grace_s or self._grace)
        for pid in pids:
            if any(p.pid == pid for p in proc.find("Weixin.exe") + proc.find("WeChat.exe")):
                await proc.stop(pid, force=True)
        self._wechat_pid = None
        return "process"

    async def _ui_logout(self) -> bool:
        from pyweixin import Uielements                                        # type: ignore[import-not-found]
        return bool(Uielements().LogoutButton().click())

    def main_window(self) -> dict[str, Any]:
        require_windows("FindWindow")
        import win32gui
        hwnd = win32gui.FindWindow(self._cls, None)
        if not hwnd:
            return {"exists": False, "visible": False, "minimized": False, "pid": None}
        import win32process
        return {"exists": True, "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "minimized": bool(win32gui.IsIconic(hwnd)), "pid": win32process.GetWindowThreadProcessId(hwnd)[1]}

    # ---------------------------------------------------------------- 讲述人仪式
    def ui_tree_visible(self) -> bool:
        """05 §2.4.3:pywinauto(UIA)能否找到会话列表 ``List`` 与「搜索」编辑框 —— 找到 = 可见,**跳过仪式**。"""
        try:
            from pywinauto import Desktop                                      # type: ignore[import-not-found]
            w = Desktop(backend="uia").window(class_name=self._cls)
            return bool(w.child_window(control_type="List").exists(timeout=2)
                        and w.child_window(control_type="Edit").exists(timeout=2))
        except Exception:
            return False

    async def narrator_start(self) -> int:
        require_windows("讲述人")
        p = await asyncio.create_subprocess_exec(NARRATOR)
        self._narrator_pid = p.pid
        return p.pid

    async def narrator_stop(self) -> None:
        await asyncio.to_thread(subprocess.run, ["taskkill", "/IM", "Narrator.exe", "/F"], capture_output=True)
        self._narrator_pid = None

    def narrator_running(self) -> bool:
        from .proc import WinProc
        return bool(WinProc().find("Narrator.exe"))

    # ---------------------------------------------------------------- chatlog 与取钥
    async def chatlog_start(self, dll: str) -> int:
        """a) 起 hook:``chatlog.exe key``。⚠️ 日志里「Hook安装成功」**只证明 hook 装上了,不证明取到密钥**(05 §2.4.4a)。"""
        require_windows("chatlog")
        exe = os.path.join(self._chatlog_dir, "chatlog.exe")
        p = await asyncio.create_subprocess_exec(exe, "key", "--dll", dll, cwd=self._chatlog_dir,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self._chatlog_pid, self._dll = p.pid, dll
        return p.pid

    async def chatlog_stop(self) -> None:
        from .proc import WinProc
        for p in WinProc().find("chatlog.exe"):
            await WinProc().stop(p.pid, force=True)
        self._chatlog_pid = None

    def chatlog_status(self) -> dict[str, Any]:
        from .proc import WinProc
        running = bool(WinProc().find("chatlog.exe"))
        http_ok = False
        if running:
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/api/v1/session?limit=1", timeout=3) as r:
                    http_ok = r.status == 200                    # 04 H09 判据
            except Exception:
                http_ok = False
        return {"running": running, "http_ok": http_ok, "dll": self._dll, "port": self._port}

    def key_state(self) -> dict[str, Any]:
        """🔴 落盘判据:``history[0].data_key`` 与 ``img_key`` **同时非空**;只看 stdout 的「获取到数据库密钥」不够。"""
        try:
            with open(CHATLOG_JSON, encoding="utf-8") as f:
                hist = (json.load(f) or {}).get("history") or []
        except (OSError, ValueError):
            return {"data_key": False, "img_key": False, "ok": False, "dll": self._dll, "error": "chatlog.json 不可读"}
        h = hist[0] if hist else {}
        dk, ik = bool(h.get("data_key")), bool(h.get("img_key"))
        return {"data_key": dk, "img_key": ik, "ok": dk and ik, "dll": self._dll,
                "error": None if (dk and ik) else ("仅取到 data_key,img_key 落空,本轮作废" if dk and not ik else None)}

    def current_wxid(self) -> Optional[str]:
        """05 §2.4.4 ⑤:从 ``xwechat_files\\wxid_xxx`` **目录名**取(不走 UI,稳)。"""
        root = (self.locate() or {}).get("data_dir")
        if not root or not os.path.isdir(root):
            return None
        dirs = [os.path.basename(p) for p in glob.glob(os.path.join(root, "wxid_*")) if os.path.isdir(p)]
        dirs.sort(key=lambda n: os.path.getmtime(os.path.join(root, n)), reverse=True)
        return dirs[0] if dirs else None

    # ---------------------------------------------------------------- 读写
    async def read_messages(self, *, talker: Optional[str], since_seq: Optional[int], limit: int) -> list[dict[str, Any]]:
        import urllib.parse
        import urllib.request
        q = {"limit": limit}
        if talker:
            q["talker"] = talker
        url = f"http://127.0.0.1:{self._port}/api/v1/chatlog?" + urllib.parse.urlencode(q)

        def go() -> list[dict[str, Any]]:
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.loads(r.read().decode("utf-8")).get("items") or []
        rows = await asyncio.to_thread(go)
        return [r for r in rows if since_seq is None or int(r.get("seq", 0)) > since_seq]

    async def send(self, *, session_name: str, text: Optional[str], image_path: Optional[str],
                   confirm_timeout_ms: int) -> dict[str, Any]:
        """pyweixin 写 + 临时加速 chatlog 轮询读回;微信没有 ``confirm=false``,10s 读不到即 ``SEND_FAILED``。"""
        require_windows("pyweixin 发送")
        from pyweixin import WeixinClient                                       # type: ignore[import-not-found]
        cli = WeixinClient()
        await asyncio.to_thread(cli.send, session_name, text or "", image_path)
        deadline = asyncio.get_running_loop().time() + confirm_timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            rows = await self.read_messages(talker=session_name, since_seq=None, limit=5)
            for r in rows:
                if r.get("is_self") and (text or "") and text in str(r.get("text") or ""):
                    return {"ok": True, "code": "DELIVERED", "ext_msg_id": f"{session_name}:{r.get('seq')}",
                            "confirm_ms": confirm_timeout_ms}
            await asyncio.sleep(1)
        return {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": confirm_timeout_ms}

    async def list_sessions(self, *, keyword: Optional[str], limit: int) -> list[dict[str, Any]]:
        import urllib.parse
        import urllib.request
        url = f"http://127.0.0.1:{self._port}/api/v1/session?" + urllib.parse.urlencode(
            {k: v for k, v in (("keyword", keyword), ("limit", limit)) if v})

        def go() -> list[dict[str, Any]]:
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.loads(r.read().decode("utf-8")).get("items") or []
        return await asyncio.to_thread(go)

    async def media(self, key: str) -> bytes:
        import urllib.request

        def go() -> bytes:
            with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/image/{key}", timeout=10) as r:
                data = r.read(20 * 1024 * 1024 + 1)             # 20MB 上限(与 ChatlogClient.download_image 同)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("媒体超过 20MB 上限")
                return data
        return await asyncio.to_thread(go)

    async def screenshot(self) -> bytes:
        require_windows("窗口截图")
        import io
        from PIL import ImageGrab                                               # type: ignore[import-not-found]
        import win32gui
        hwnd = win32gui.FindWindow(self._cls, None)
        box = win32gui.GetWindowRect(hwnd) if hwnd else None
        img = ImageGrab.grab(bbox=box)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def reinstall(self, installer: str) -> dict[str, Any]:
        """🔴 B-3:**卸载不走静默** —— NSIS 的 ``/S`` = 连 ``xwechat_files`` 与登录态一起删。这里只拉起交互式卸载/安装,
        每步由用户确认(00 §11.8 [WXVER]);进度经 #33 ``login/status`` 回报。"""
        require_windows("微信重装")
        p = await asyncio.create_subprocess_exec(installer)
        return {"started": True, "installer": installer, "pid": p.pid, "silent": False}


def _registry_weixin_path() -> str:
    import winreg
    for hive, path, name in ((winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin", "InstallPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin", "InstallPath")):
        try:
            with winreg.OpenKey(hive, path) as k:
                base = winreg.QueryValueEx(k, name)[0]
                for exe in ("Weixin.exe", "WeChat.exe"):
                    p = os.path.join(base, exe)
                    if os.path.exists(p):
                        return p
        except OSError:
            continue
    return ""


def _data_root_from_ini() -> Optional[str]:
    """05 §2.4.9:``%APPDATA%\\Tencent\\xwechat\\config\\<32hex>.ini``,取 **LastWrite 最新**的那个,值 = 第一行。"""
    cfg = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat", "config")
    inis = sorted(glob.glob(os.path.join(cfg, "*.ini")), key=lambda p: os.path.getmtime(p), reverse=True)
    for p in inis:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                first = f.readline().strip()
            if first:
                return first
        except OSError:
            continue
    return None
