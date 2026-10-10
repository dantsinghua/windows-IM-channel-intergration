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
import shutil
import subprocess
import tempfile
import time
from typing import Any, Optional

from ..logfmt import get_logger
from . import require_windows

log = get_logger("wechat")
CHATLOG_JSON = os.path.join(os.path.expanduser("~"), ".chatlog", "chatlog.json")
NARRATOR = "narrator.exe"
# 🔴 R6-90:chatlog 按**固定相对路径**加载取钥 DLL —— 实测 `chatlog.exe key` **没有 `--dll` 参数**
# (传了直接 `unknown flag` 秒退、hook 根本没装上);DLL 只认 `<cwd 或 exe 旁>/lib/windows_x64/wx_key.dll`。
WX_KEY_REL = os.path.join("lib", "windows_x64", "wx_key.dll")


CHATLOG_READ_DAYS = 2                     # 增量读的日期窗(跨零点也不丢);真正去重靠 (talker, seq)
CHATLOG_STOP_POLLS = 20                   # chatlog 优雅退出(卸载 hook)的等待轮数(×0.3 s ≈ 6 s),超时才兜底强杀


def _f(row: dict[str, Any], *keys: str) -> str:
    """chatlog 的 JSON 键名大小写不固定(contact 实测 ``UserName/Alias/Remark/NickName``),按候选名取第一个非空。"""
    for k in keys:
        for kk in (k, k[:1].lower() + k[1:], k.lower()):
            v = row.get(kk)
            if v not in (None, ""):
                return str(v)
    return ""


def resolve_send_target(talker: str, contacts: list[dict[str, Any]], rooms: list[dict[str, Any]]) -> tuple[str, str]:
    """R6-92:talker → pyweixin 用的显示名(备注优先,否则昵称 / 群名)。返回 ``(name, why_not)``。"""
    for r in contacts:
        if _f(r, "UserName") == talker:
            name = _f(r, "Remark", "NickName")
            return (name, "") if name else ("", f"联系人 {talker} 没有可搜索的备注或昵称")
    for r in rooms:
        if _f(r, "Name", "UserName") == talker:
            name = _f(r, "Remark", "NickName")
            return (name, "") if name else ("", f"群 {talker} 没有群名或备注,微信里无法按名字搜到")
    return "", f"未在微信通讯录里找到会话 {talker}"


def display_name_collisions(talker: str, name: str, rows: list[dict[str, Any]]) -> list[str]:
    """同一显示名(备注 / 昵称 / 微信号 任一相等)还对应别的联系人或群 ⇒ 返回那些对象的 id;空 = 唯一。"""
    out: list[str] = []
    for r in rows:
        rid = _f(r, "UserName", "Name")
        if not rid or rid == talker or rid in out:
            continue
        if name in (_f(r, "Remark"), _f(r, "NickName"), _f(r, "Alias")):
            out.append(rid)
    return out


def _row_epoch_s(row: dict[str, Any]) -> Optional[float]:
    v = row.get("time") if row.get("time") is not None else row.get("createTime")
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e11 else 1)
    if isinstance(v, str) and v:
        import datetime as _dt
        try:
            return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def pick_readback(rows: list[dict[str, Any]], *, text: Optional[str], image: bool, since_s: float) -> Optional[dict[str, Any]]:
    """发送读回:自己发的(``isSelf``)、发送时刻之后、文本含原文 / 图片 type=3;取 seq 最大的那条。"""
    best: Optional[dict[str, Any]] = None
    for r in rows:
        is_self = r.get("isSelf") if "isSelf" in r else r.get("is_self")
        if not is_self:
            continue
        ts = _row_epoch_s(r)
        if ts is not None and ts < since_s:
            continue
        body = str(r.get("content") if r.get("content") is not None else r.get("text") or "")
        if text and not image and text not in body:
            continue
        if image and str(r.get("type")) not in ("3", "49") and not (text and text in body):
            continue
        if best is None or int(r.get("seq") or 0) > int(best.get("seq") or 0):
            best = r
    return best


def chatlog_time_range(days: int = CHATLOG_READ_DAYS, *, today: Optional["object"] = None) -> str:
    """chatlog ``time`` 参数:``YYYY-MM-DD~YYYY-MM-DD``(实测说明页口径)。``today`` 仅测试注入。"""
    import datetime as _dt
    end = today or _dt.date.today()
    start = end - _dt.timedelta(days=max(0, days - 1))
    return f"{start.isoformat()}~{end.isoformat()}"


_KEY_HEX_RE = None


def redact_keys(line: str) -> str:
    """chatlog 会把两把钥**明文**打进日志(实测 ``server config: &{... DataKey:…}``、key 模式收尾 ``Data Key : …``)。
    落盘前把 ≥32 位连续十六进制(data_key 64 / img_key 32)打码;函数地址等短串(如 ``0x7ffa94e4bf4c``)保留以便排障。"""
    global _KEY_HEX_RE
    if _KEY_HEX_RE is None:
        import re
        _KEY_HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")
    return _KEY_HEX_RE.sub("<redacted>", line)


def decode_line(raw: bytes) -> str:
    """chatlog 在 Windows 管道上的中文输出是 GBK(真机日志按 UTF-8 解码全成了 ``���``,进度标记全丢);先严格 UTF-8,不成再 GBK。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


async def _pump_redacted(stream: "asyncio.StreamReader", log_path: str) -> None:
    """把子进程输出逐行脱敏后追加到日志;子进程退出(EOF)即结束。"""
    with open(log_path, "a", encoding="utf-8", errors="replace") as f:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            f.write(redact_keys(decode_line(raw)))
            f.flush()


def pick_wechat_root_pid(procs: list[dict[str, Any]], window_pid: Optional[int] = None) -> Optional[int]:
    """取钥要挂的 ``Weixin.exe``:**登录主进程**(进程树的根)。``procs`` 只含 ``Weixin.exe``,每项 ``{pid, ppid, cmdline, rss}``。

    2026-10-10 真机:微信 4.x 是一个根进程 + 一串 ``--type=...`` 渲染 / 插件子进程;DLL 挂到子进程上特征扫描必然
    「模式匹配失败,找到 0 个结果」。主窗口最小化到托盘时 ``FindWindow`` 拿不到 pid。这是**兜底**选择(没有句柄判据时):
    有窗口 pid 就沿父链走到根;没有就在根进程(父不是 Weixin.exe、不带 ``--type=``)里取**内存最大**的那个 —— 登录主进程
    内存压倒性大于未登录 / 辅助实例(真机:登录 626MB vs 子进程 18~99MB)。登录主进程的决定性判据见 ``login_pid``(句柄)。"""
    by = {int(p["pid"]): p for p in procs}
    if window_pid is not None and int(window_pid) in by:
        pid, seen = int(window_pid), set()
        while int(by[pid].get("ppid") or 0) in by and pid not in seen:
            seen.add(pid)
            pid = int(by[pid]["ppid"])
        return pid
    roots = [p for p in procs if int(p.get("ppid") or 0) not in by]
    mains = [p for p in roots if "--type=" not in str(p.get("cmdline") or "")] or roots
    if not mains:
        return window_pid
    kids = {pid: sum(1 for q in procs if int(q.get("ppid") or 0) == pid) for pid in by}
    return int(max(mains, key=lambda p: (float(p.get("rss") or 0), kids[int(p["pid"])]))["pid"])


def parse_key_progress(text: str) -> dict[str, Any]:
    """读 ``chatlog key`` **本轮**日志的进度标记。chatlog 只在两把钥同轮都拿到时才写 ``chatlog.json``,
    所以「图片钥已到、该退出重登了」与「DLL 没挂上」只能从日志看。返回 ``{hook, img_key, data_key, error}``。"""
    out: dict[str, Any] = {"hook": False, "img_key": False, "data_key": False, "not_logged_in": False, "error": None}
    for line in text.splitlines():
        if "Hook安装成功" in line:
            out["hook"] = True
        if ("获取到图片密钥" in line or "找到图片密钥" in line) and "未获取到图片密钥" not in line:
            out["img_key"] = True
        if ("获取到数据库密钥" in line or "已获取数据库密钥" in line) and "未获取到数据库密钥" not in line:
            out["data_key"] = True
        if "微信进程存在但未登录" in line or "数据目录未就绪" in line:
            out["not_logged_in"] = True
        elif "获取密钥超时" in line:
            out["error"] = "本轮取钥超时:hook 已装上但没等到「打开图片」+「退出重新登录」这两步"
        elif "模式匹配失败" in line or "初始化DLL失败" in line:
            out["error"] = "取钥 DLL 没能挂上微信(模式匹配失败):挂错了进程,或该微信版本与 DLL 不配"
        elif "未获取到图片密钥" in line:
            out["error"] = "本轮没取到图片密钥:要在这一轮里打开一张图片"
        elif "未获取到数据库密钥" in line:
            out["error"] = "本轮没等到登录动作:打开图片后要立刻退出微信并重新登录"
        elif "failed to get key" in line and not out["error"]:
            msg = line.split("error=", 1)[-1].strip().strip('"')
            out["error"] = f"chatlog 取钥报错:{msg}"
    if out["not_logged_in"]:                                            # 挂到未登录实例是根因(超时只是其后果),文案固定、优先
        out["error"] = "取钥挂到了未登录的微信实例(该进程没有登录账号);系统会自动改挂已登录的那个再试"
    return out


async def spawn_redacted(args: list[str], *, cwd: str, log_path: str) -> "asyncio.subprocess.Process":
    """起子进程,stdout+stderr 经 ``redact_keys`` 落 ``log_path``(不再丢进 DEVNULL,也不让密钥明文落盘)。

    R6-96:Windows 上用 ``CREATE_NEW_PROCESS_GROUP`` 起 —— 这样 ``chatlog_stop`` 能对它发 ``CTRL_BREAK`` 让它**优雅退出、
    卸载 DLL hook**(否则强杀会把 inline hook 残留在微信进程里,后续取钥全"模式匹配失败")。"""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    p = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT, creationflags=flags)
    assert p.stdout is not None
    task = asyncio.get_running_loop().create_task(_pump_redacted(p.stdout, log_path))
    _PUMPS.add(task)
    task.add_done_callback(_PUMPS.discard)
    return p


_PUMPS: set = set()                       # 留住日志泵任务引用,防被 GC


def read_chatlog_keys(path: str = CHATLOG_JSON) -> Optional[dict[str, str]]:
    """从 ``key`` 模式写的 ``~/.chatlog/chatlog.json`` 读两把钥的**明文值**(``history[0]``);任一为空返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            hist = (json.load(f) or {}).get("history") or []
    except (OSError, ValueError):
        return None
    h = hist[0] if hist else {}
    dk, ik = str(h.get("data_key") or ""), str(h.get("img_key") or "")
    return {"data_key": dk, "img_key": ik} if dk and ik else None


# chatlog server 的消息库位置随微信版本不同:v4 在 ``<wxid>/db_storage``,v3 在 ``<wxid>/Msg``;真机布局以探测为准。
_DATA_DIR_PROBES = ("db_storage", "Msg", "message", os.path.join("db_storage", "message"))


def discover_chatlog_data_dir(data_root: Optional[str], wxid: Optional[str]) -> Optional[str]:
    """定位某 wxid 的加密消息库目录。按已知子布局探测**真实存在且含 .db 的目录**,都不中则回退到账号目录
    (让 ``chatlog -v 4`` 自己递归找);``data_root``/``wxid`` 缺失返回 None。兼容「版本升级改了子目录名」的情况。"""
    if not data_root or not wxid:
        return None
    account_dir = os.path.join(data_root, "xwechat_files", wxid)
    if not os.path.isdir(account_dir):
        return None
    for sub in _DATA_DIR_PROBES:
        cand = os.path.join(account_dir, sub)
        if os.path.isdir(cand) and (glob.glob(os.path.join(cand, "**", "*.db"), recursive=True)):
            return cand
    return account_dir


def build_server_config(*, data_dir: str, work_dir: str, keys: dict[str, str], addr: str, version: int = 4,
                        platform: str = "windows") -> dict[str, Any]:
    """chatlog ``server`` 读的 ``~/.chatlog/chatlog-server.json``;**密钥只进此文件、不进命令行**(命令行参数对其它进程可见)。"""
    return {"http_addr": addr, "data_dir": data_dir, "work_dir": work_dir, "platform": platform,
            "version": version, "data_key": keys["data_key"], "img_key": keys["img_key"], "auto_decrypt": True}


def stage_wx_key_dll(chatlog_dir: str, work_dir: str, dll: str) -> str:
    """把选中的 ``wx_keyN.dll`` 放到 chatlog 能找到的位置:``<work_dir>/lib/windows_x64/wx_key.dll``。

    会话代理是**普通用户**,安装后 chatlog 在 ``%ProgramData%\\QTrade\\pkg\\chatlog``(只读)——所以 DLL 落到**用户可写**的
    ``work_dir``(cwd),chatlog.exe 仍用 payload 里的绝对路径(实测 cwd 下的 ``lib\\windows_x64\\wx_key.dll`` 能加载)。
    返回落好的 DLL 绝对路径。源 DLL 不存在则抛 ``FileNotFoundError``。
    """
    src = os.path.join(chatlog_dir, dll)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"取钥 DLL 不在位:{src}")
    dst = os.path.join(work_dir, WX_KEY_REL)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


class WinWeChat:
    def __init__(self, *, exe_path: str = "", chatlog_dir: str = "", chatlog_port: int = 5030,
                 chatlog_work_dir: str = "", main_wnd_class: str = "Qt51514QWindowIcon", process_close_grace_s: int = 10):
        self._exe = exe_path
        self._chatlog_dir = chatlog_dir
        self._work_dir = chatlog_work_dir or os.path.join(tempfile.gettempdir(), "qtrade-chatlog")
        self._port = chatlog_port
        self._cls = main_wnd_class
        self._grace = process_close_grace_s
        self._wechat_pid: Optional[int] = None
        self._chatlog_pid: Optional[int] = None
        self._narrator_pid: Optional[int] = None
        self._dll: Optional[str] = None
        self._key_log_offset: Optional[int] = None     # 本轮 `chatlog key` 日志起点;None = 没有进行中的取钥轮

    def set_main_wnd_class(self, class_name: str) -> None:
        """R6-58 (at) ②:服务侧算好的「该 wxid 行值 / 配置默认」经管道传入,切换本次 FindWindow 要用的类名。"""
        self._cls = class_name

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
            return {"exists": False, "visible": False, "minimized": False, "pid": None, "class_name": None}
        import win32process
        return {"exists": True, "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "minimized": bool(win32gui.IsIconic(hwnd)), "pid": win32process.GetWindowThreadProcessId(hwnd)[1],
                "class_name": self._cls}      # R6-58 (at):FindWindow 命中即证明该类名对这次探测成立

    def warmup(self) -> None:
        """预热 pywinauto/comtypes(首次导入会生成 UIA 类型库包装,全新机器上可达十几秒)与 psutil;放线程里调。"""
        require_windows("预热")
        import psutil                                                          # noqa: F401
        from pywinauto import Desktop                                          # type: ignore[import-not-found]
        Desktop(backend="uia")                                                 # 触发 UIA COM 初始化

    # ---------------------------------------------------------------- 讲述人仪式
    def ui_tree_visible(self) -> bool:
        """05 §2.4.3:pywinauto(UIA)能否找到会话列表 ``List`` 与「搜索」编辑框 —— 找到 = 可见,**跳过仪式**。"""
        try:
            from pywinauto import Desktop                                      # type: ignore[import-not-found]
            w = Desktop(backend="uia").window(class_name=self._cls)
            # 只读快照,不等:`exists(timeout=2)` 在控件不存在时各白等 2 s,#28 `status` 每次都要为此付 4 s(真机实测)
            return bool(w.child_window(control_type="List").exists(timeout=0.3)
                        and w.child_window(control_type="Edit").exists(timeout=0.3))
        except Exception:
            return False

    async def narrator_start(self) -> int:
        """2026-10-10 真机:``Narrator.exe`` 带 ``uiAccess`` 清单,普通用户进程 ``CreateProcess`` 它必报
        ``OSError(740, 请求的操作需要提升)``;**只能走 ShellExecute**(``os.startfile``,系统替它完成 UIAccess 启动)。
        已在跑就不再起第二个。返回 pid(ShellExecute 不给句柄,起完按映像名找;找不到回 0)。"""
        require_windows("讲述人")
        from .proc import WinProc
        found = WinProc().find("Narrator.exe")
        if not found:
            await asyncio.to_thread(os.startfile, NARRATOR)                 # noqa: S606 —— 系统自带程序,固定映像名
            for _ in range(20):                                             # 最多等 3 s 让进程出现
                await asyncio.sleep(0.15)
                found = WinProc().find("Narrator.exe")
                if found:
                    break
        self._narrator_pid = found[0].pid if found else 0
        return self._narrator_pid

    async def narrator_stop(self) -> bool:
        """返回**是否已停**。讲述人跑在高完整性级别,普通用户进程 ``taskkill`` 它是「拒绝访问」(真机实测);
        这里尽力一试,停不掉回 ``False``,由会话状态机把 ``stop_pending`` 带给服务,**服务(提权)去结束**。"""
        await asyncio.to_thread(subprocess.run, ["taskkill", "/IM", "Narrator.exe", "/F"], capture_output=True)
        await asyncio.sleep(0.3)
        still = self.narrator_running()
        if not still:
            self._narrator_pid = None
        return not still

    @staticmethod
    def kill_narrator_elevated() -> bool:
        """**服务侧**(提权 / SYSTEM)结束讲述人;会话代理停不掉时由 #33 轮询路径调用。返回是否已停。

        `taskkill /F` 发出后进程不会立刻从进程表消失,单次 `find` 会误报 False —— 短轮询到真的没了(最多 ~1.5 s)。"""
        import time as _time
        from .proc import WinProc
        subprocess.run(["taskkill", "/IM", "Narrator.exe", "/F"], capture_output=True)
        for _ in range(10):
            if not WinProc().find("Narrator.exe"):
                return True
            _time.sleep(0.15)
        return False

    def narrator_running(self) -> bool:
        from .proc import WinProc
        return bool(WinProc().find("Narrator.exe"))

    # ---------------------------------------------------------------- chatlog 与取钥
    async def chatlog_start(self, dll: str) -> int:
        """a) 起 hook:``chatlog.exe key``。⚠️ 日志里「Hook安装成功」**只证明 hook 装上了,不证明取到密钥**(05 §2.4.4a)。

        🔴 R6-90(2026-10-10 真机):旧实现 ``chatlog.exe key --dll <dll>`` —— ``key`` 子命令**没有 ``--dll``**,
        进程一启动就 ``Error: unknown flag: --dll`` 秒退,hook 从未装上,打开图片 / 重登都取不到密钥。正确做法:
        先 ``stage_wx_key_dll`` 把选中的 DLL 放成 ``<work>/lib/windows_x64/wx_key.dll``,再以 ``cwd=<work>`` 跑
        ``chatlog.exe key``(不带 DLL 参数)。stdout/stderr 落 ``<work>/chatlog-key.log`` 便于排障,不再丢进 DEVNULL。"""
        require_windows("chatlog")
        exe = os.path.join(self._chatlog_dir, "chatlog.exe")
        if not os.path.isfile(exe):
            raise FileNotFoundError(f"chatlog.exe 不在位:{exe}")
        await asyncio.to_thread(stage_wx_key_dll, self._chatlog_dir, self._work_dir, dll)
        args = [exe, "key", "--debug"]
        # 🔴 R6-93(真机日志):`chatlog key` 不带 `--pid` 会掉进交互式进程选择器(`Select a process:`),
        #    非 tty 下永久挂住 —— 全新机器常有「未登录微信」辅助进程 + 登录进程两个 Weixin.exe。显式指定登录主进程的 pid。
        pid = self.login_pid()
        if pid:
            args += ["--pid", str(pid)]
        log_path = os.path.join(self._work_dir, "chatlog-key.log")
        self._key_log_offset = os.path.getsize(log_path) if os.path.isfile(log_path) else 0
        p = await spawn_redacted(args, cwd=self._work_dir, log_path=log_path)
        self._chatlog_pid, self._dll = p.pid, dll
        return p.pid

    def wechat_running(self) -> bool:
        from .proc import WinProc
        return bool(WinProc().find("Weixin.exe"))

    def login_pid(self) -> Optional[int]:
        """取钥目标 = **已登录**微信主进程。

        🔴 R6-96(2026-10-10 真机):「退出重登」会制造多个 ``Weixin.exe``(已登录主进程 + 未登录实例),挂到未登录的那个
        → DLL hook 虽装上但「微信进程存在但未登录 / 数据目录未就绪」→ 永远等不到密钥。决定性判据:**谁打开了该账号的
        消息库目录,谁就是已登录进程**(``open_files`` 命中,不受重登瞬态影响)。拿不到句柄时退回 ``pick_wechat_root_pid``
        (窗口 pid 沿父链到根 / 内存最大)。"""
        import psutil
        try:
            window_pid = self.main_window().get("pid")
        except Exception:                                              # noqa: BLE001 —— FindWindow 失败不挡取钥
            window_pid = None
        procs: list[dict[str, Any]] = []
        objs: dict[int, Any] = {}
        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "memory_info"]):
            try:
                if (p.info.get("name") or "").lower() != "weixin.exe":
                    continue
                mi = p.info.get("memory_info")
                procs.append({"pid": p.info["pid"], "ppid": p.info.get("ppid") or 0,
                              "cmdline": " ".join(p.info.get("cmdline") or []), "rss": mi.rss if mi else 0})
                objs[p.info["pid"]] = p
            except Exception:                                          # noqa: BLE001 —— 进程可能在遍历期间消失
                continue
        acc = self._account_dir()
        if acc:
            for pid, p in objs.items():
                try:
                    if any((f.path or "").lower().startswith(acc) for f in p.open_files()):
                        return pick_wechat_root_pid(procs, pid)        # 命中进程沿父链到根(通常它本身就是根)
                except Exception:                                      # noqa: BLE001 —— 句柄权限 / 进程消失:换下一个
                    continue
        return pick_wechat_root_pid(procs, window_pid)

    def _account_dir(self) -> Optional[str]:
        """登录账号的消息库目录(小写,供句柄前缀匹配);定位不到返回 None。"""
        data_dir = (self.locate() or {}).get("data_dir")
        wxid = self.current_wxid()
        return os.path.join(data_dir, wxid).lower() if data_dir and wxid else None

    def key_progress(self) -> dict[str, Any]:
        """本轮 ``chatlog key`` 日志里的进度(``parse_key_progress``);没有进行中的取钥轮返回全 False。"""
        if self._key_log_offset is None:
            return parse_key_progress("")
        try:
            with open(os.path.join(self._work_dir, "chatlog-key.log"), "rb") as f:
                f.seek(self._key_log_offset)
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            text = ""
        return parse_key_progress(text)

    async def chatlog_stop(self) -> None:
        """🔴 R6-96(2026-10-10 真机实锤):绝不能直接 ``taskkill /F`` 正在 hook 的 chatlog —— 它来不及执行「DLL资源已清理」
        卸载 inline hook,被改写的目标函数字节会**永久残留在微信进程里**,导致之后对同一进程取钥全部「模式匹配失败,
        找到 0 个结果」(我 22:10 强杀 chatlog 把 23424 弄脏、后续全败就是这么来的)。先发 ``CTRL_BREAK`` 让它优雅卸载,
        等它退出;只有赖着不走的才兜底强杀。"""
        import signal
        from .proc import WinProc
        proc = WinProc()
        for p in proc.find("chatlog.exe"):
            try:
                os.kill(p.pid, signal.CTRL_BREAK_EVENT)        # NEW_PROCESS_GROUP 起的,CTRL_BREAK 能送进去触发清理
            except (OSError, ValueError, AttributeError):      # 非 Windows / 进程已退 / 无此信号:退回等待 + 兜底强杀
                pass
        for _ in range(CHATLOG_STOP_POLLS):
            if not proc.find("chatlog.exe"):
                break
            await asyncio.sleep(0.3)
        for p in proc.find("chatlog.exe"):
            await proc.stop(p.pid, force=True)
        self._chatlog_pid = self._key_log_offset = None

    async def chatlog_serve(self) -> int:
        """R6-91:取钥成功后切到 ``chatlog server``(:5030)供读消息 / H09 健康 / 发送读回确认。

        2026-10-10 真机:``server`` 空配置报 ``dataDir or workDir is required``,**不会**自己读 key 模式存的钥 ——
        必须显式给 data_dir/work_dir/两把钥。密钥写进 chatlog 自己会搜的 ``~/.chatlog/chatlog-server.json``
        (与它存 chatlog.json 同目录、同一份明文风险),**不放命令行**。数据目录按真实 DB 探测(兼容 v3/v4 布局)。"""
        require_windows("chatlog server")
        keys = read_chatlog_keys()
        if not keys:
            raise RuntimeError("chatlog.json 里两把钥不全,不能起 server(先完成取钥)")
        loc = self.locate() or {}
        wxid = self.current_wxid()
        data_dir = discover_chatlog_data_dir(loc.get("data_root"), wxid)
        if not data_dir:
            raise RuntimeError(f"找不到微信消息库目录(data_root={loc.get('data_root')!r} wxid={wxid!r})")
        work = os.path.join(self._work_dir, "decrypted", wxid or "default")
        os.makedirs(work, exist_ok=True)
        cfg = build_server_config(data_dir=data_dir, work_dir=work, keys=keys, addr=f"127.0.0.1:{self._port}")
        cfg_dir = os.path.join(os.path.expanduser("~"), ".chatlog")
        os.makedirs(cfg_dir, exist_ok=True)
        tmp = os.path.join(cfg_dir, "chatlog-server.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(tmp, os.path.join(cfg_dir, "chatlog-server.json"))
        exe = os.path.join(self._chatlog_dir, "chatlog.exe")
        p = await spawn_redacted([exe, "server"], cwd=self._work_dir,
                                 log_path=os.path.join(self._work_dir, "chatlog-server.log"))
        self._chatlog_pid = p.pid
        return p.pid

    def chatlog_status(self) -> dict[str, Any]:
        from .proc import WinProc
        running = bool(WinProc().find("chatlog.exe"))
        http_ok = False
        if running:
            try:
                import urllib.request
                # R6-91:H09 改探 ``/health``(实测回 ``{"status":"ok"}``)。原 ``/api/v1/session?limit=1`` 在会话为空时
                # chatlog 回 404,会把「空号 / 刚登录还没消息」误判成 chatlog 挂了。
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=3) as r:
                    http_ok = r.status == 200
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
        if dk and ik:
            return {"data_key": True, "img_key": True, "ok": True, "dll": self._dll, "error": None}
        # 两钥不全时 chatlog.json 不会更新 ⇒ 本轮进度(图片钥已到 / DLL 没挂上)以日志为准
        prog = self.key_progress()
        dk, ik = dk or prog["data_key"], ik or prog["img_key"]
        return {"data_key": dk, "img_key": ik, "ok": False, "dll": self._dll,
                "error": prog["error"] or ("仅取到 data_key,img_key 落空,本轮作废" if dk and not ik else None)}

    def current_wxid(self) -> Optional[str]:
        """05 §2.4.4 ⑤:从 ``xwechat_files\\wxid_xxx`` **目录名**取(不走 UI,稳)。"""
        root = (self.locate() or {}).get("data_dir")
        if not root or not os.path.isdir(root):
            return None
        dirs = [os.path.basename(p) for p in glob.glob(os.path.join(root, "wxid_*")) if os.path.isdir(p)]
        dirs.sort(key=lambda n: os.path.getmtime(os.path.join(root, n)), reverse=True)
        return dirs[0] if dirs else None

    # ---------------------------------------------------------------- 读写
    def _get_items(self, path: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        """chatlog HTTP 取 ``items``。R6-91(2026-10-10 实测):① 不带 ``format=json`` 回的是 **CSV**(json.loads 必崩);
        ② **查无 = 404**(不是故障,当空);其它非 200 照常抛,交上层计 H09 / KEY_FAIL。"""
        import urllib.error
        import urllib.parse
        import urllib.request
        url = f"http://127.0.0.1:{self._port}{path}?" + urllib.parse.urlencode({**query, "format": "json"})
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        if isinstance(body, list):
            return body
        return (body or {}).get("items") or []

    async def read_messages(self, *, talker: Optional[str], since_seq: Optional[int], limit: int) -> list[dict[str, Any]]:
        """R6-91:``/api/v1/chatlog`` **必须同时带 ``time`` 与 ``talker``**(实测缺任一 400)。不给 talker(全量轮)
        ⇒ 先列会话,逐会话取;``time`` 取近 ``CHATLOG_READ_DAYS`` 天的日期区间,再按 ``seq`` 过滤增量。"""
        rng = chatlog_time_range()
        talkers = [talker] if talker else [s.get("userName") or s.get("talker") or s.get("UserName")
                                           for s in await self.list_sessions(keyword=None, limit=limit)]

        def go() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for t in talkers:
                if not t:
                    continue
                out.extend(self._get_items("/api/v1/chatlog", {"time": rng, "talker": t, "limit": limit}))
            return out
        rows = await asyncio.to_thread(go)
        return [r for r in rows if since_seq is None or int(r.get("seq", 0) or 0) > since_seq]

    def _resolve_send_target(self, talker: str) -> str:
        """talker(wxid / ``xxx@chatroom``)→ pyweixin 搜索用的显示名;找不到或**重名**一律抛 ``WaError``,不发。"""
        from ..errors import TARGET_NOT_FOUND, WaError
        contacts = self._get_items("/api/v1/contact", {"keyword": talker})
        rooms = self._get_items("/api/v1/chatroom", {"keyword": talker})
        name, why = resolve_send_target(talker, contacts, rooms)
        if not name:
            raise WaError(TARGET_NOT_FOUND, why or "未在微信通讯录里找到该会话", reason="target_not_found")
        others = self._get_items("/api/v1/contact", {"keyword": name}) + self._get_items("/api/v1/chatroom", {"keyword": name})
        clash = display_name_collisions(talker, name, others)
        if clash:
            raise WaError(TARGET_NOT_FOUND,
                          f"会话「{name}」在通讯录里不唯一(另有 {len(clash)} 个同名联系人/群),为防发错对象已拒绝发送;"
                          "请在微信里给目标改一个唯一备注后重试", reason="display_name_ambiguous")
        return name

    async def send(self, *, session_name: str, text: Optional[str], image_path: Optional[str],
                   confirm_timeout_ms: int) -> dict[str, Any]:
        """pyweixin 写 + chatlog 读回确认;微信没有 ``confirm=false``,期限内读不到即 ``SEND_FAILED``。

        R6-92(真实 API,取自上游源码):
        - 上游**没有** ``WeixinClient``(旧代码装上了也必 ImportError);文本 = ``Messages.send_messages_to_friend``,
          图片/文件 = ``Files.send_files_to_friend``;
        - 两者的 ``friend`` 是**界面搜索用的显示名**(备注 / 群名),不是 wxid ⇒ 先按 talker 在 chatlog 通讯录解析,
          **重名直接拒发**(真实工作账号,发错对象不可撤回);
        - ``close_weixin`` 上游默认「任务结束关闭微信」⇒ 必须显式 False,否则每发一条关一次主窗口;
          ``search_pages=0`` 走顶部搜索栏,不靠会话列表可见范围。"""
        require_windows("pyweixin 发送")
        name = await asyncio.to_thread(self._resolve_send_target, session_name)
        started = time.time()
        if not image_path and not self.ui_tree_visible():
            await asyncio.to_thread(self._keyboard_send_text, name, text or "")
        else:
            await self._pyweixin_send(name, text, image_path)
        deadline = asyncio.get_running_loop().time() + confirm_timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            rows = await asyncio.to_thread(self._readback_rows, session_name, text)
            hit = pick_readback(rows, text=text, image=bool(image_path), since_s=started - 2)
            if hit is not None:
                return {"ok": True, "code": "DELIVERED", "ext_msg_id": f"{session_name}:{hit.get('seq')}",
                        "confirm_ms": int((time.time() - started) * 1000)}
            await asyncio.sleep(1)
        return {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": confirm_timeout_ms}

    async def _pyweixin_send(self, name: str, text: Optional[str], image_path: Optional[str]) -> None:
        import pyweixin                                                          # type: ignore[import-not-found]
        from pyweixin import Files, Messages                                     # type: ignore[import-not-found]
        try:
            pyweixin.GlobalConfig.close_weixin = False
            pyweixin.GlobalConfig.search_pages = 0
        except Exception:                                                       # noqa: BLE001
            pass
        if image_path:
            await asyncio.to_thread(Files.send_files_to_friend, friend=name, files=[image_path],
                                    with_messages=bool(text), messages=[text] if text else [], close_weixin=False)
        else:
            await asyncio.to_thread(Messages.send_messages_to_friend, friend=name, messages=[text or ""],
                                    search_pages=0, close_weixin=False)

    def _keyboard_send_text(self, name: str, text: str) -> None:
        """控件树为空时的发送(微信 4.1 主窗口常见)。只依赖窗口前置和官方快捷键,不依赖讲述人。

        Ctrl+F 聚焦搜索,回车打开唯一结果,Alt+S 发送。对象唯一性在 ``_resolve_send_target`` 已拒绝重名。
        是否发到目标会话,由随后的 chatlog 读回判定。"""
        import win32clipboard
        import win32con
        import win32gui
        import pyautogui
        from ..errors import NOT_READY, WaError

        hwnd = win32gui.FindWindow(self._cls, None) or win32gui.FindWindow("mmui::MainWindow", None)
        if not hwnd or win32gui.GetClassName(hwnd) == "mmui::LoginWindow":
            raise WaError(NOT_READY, "微信不在已登录的主窗口,无法发送", reason="wechat_not_in_main")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        pyautogui.PAUSE = 0.15

        def paste(value: str) -> None:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(value, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            pyautogui.hotkey("ctrl", "v")

        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.4)
        pyautogui.hotkey("ctrl", "a")
        paste(name)
        time.sleep(0.5)
        pyautogui.press("enter")
        time.sleep(0.6)
        paste(text)
        time.sleep(0.2)
        pyautogui.hotkey("alt", "s")

    def _readback_rows(self, talker: str, text: Optional[str]) -> list[dict[str, Any]]:
        """读回只看「今天 + 该会话 + 正文正则命中」:此前 ``limit=5`` 取到的是当天**最早** 5 条,忙会话永远读不到。"""
        import re as _re
        q: dict[str, Any] = {"time": chatlog_time_range(1), "talker": talker, "limit": 50}
        if text:
            q["keyword"] = _re.escape(text[:60])
        return self._get_items("/api/v1/chatlog", q)

    async def list_sessions(self, *, keyword: Optional[str], limit: int) -> list[dict[str, Any]]:
        q = {k: v for k, v in (("keyword", keyword), ("limit", limit)) if v}
        return await asyncio.to_thread(self._get_items, "/api/v1/session", q)

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
