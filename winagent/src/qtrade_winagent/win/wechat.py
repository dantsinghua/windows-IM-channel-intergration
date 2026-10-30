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
import base64
import filecmp
import glob
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Optional
from dataclasses import dataclass

from .. import tempvol
from ..errors import BUSY, NOT_READY, TARGET_NOT_FOUND, TIMEOUT, WaError
from ..logfmt import get_logger
from . import require_windows, run_text

log = get_logger("wechat")
CHATLOG_JSON = os.path.join(os.path.expanduser("~"), ".chatlog", "chatlog.json")
NARRATOR = "narrator.exe"
# 🔴 R6-90:chatlog 按**固定相对路径**加载取钥 DLL —— 实测 `chatlog.exe key` **没有 `--dll` 参数**
# (传了直接 `unknown flag` 秒退、hook 根本没装上);DLL 只认 `<cwd 或 exe 旁>/lib/windows_x64/wx_key.dll`。
WX_KEY_REL = os.path.join("lib", "windows_x64", "wx_key.dll")


CHATLOG_READ_DAYS = 1                     # 增量读的日期窗:仅最近一天(time 参数只支持日期粒度);会话级用 nOrder 精确过滤降请求量(R6-111)
CHATLOG_STOP_POLLS = 20                   # chatlog 优雅退出(卸载 hook)的等待轮数(×0.3 s ≈ 6 s),超时才兜底强杀
CHATLOG_SERVER_START_TIMEOUT_S = 5.0      # Port check, launch, and health confirmation share this budget.


async def _blocking(fn, *args):
    """取消只中断调用方；持有资源的线程退场前，调用任务仍保留所有权。"""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


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
    from ..wechat_read_page import chatlog_timestamp
    value = row.get("time") if row.get("time") is not None else row.get("createTime")
    return chatlog_timestamp(value)


@dataclass(frozen=True)
class ReadbackBaseline:
    account_id: str
    talker: str
    max_seq: int
    seen_ids: frozenset[str]
    captured_at_s: float


def pick_readback(rows: list[dict[str, Any]], *, text: Optional[str], image: bool, since_s: float,
                  baseline: Optional[ReadbackBaseline] = None, account_id: Optional[str] = None,
                  used_ids: frozenset[str] = frozenset()) -> Optional[dict[str, Any]]:
    """Only a unique, fresh, exact native observation can confirm this attempt."""
    from ..wechat_read_page import sequence
    if (baseline is None or not baseline.account_id or account_id != baseline.account_id
            or not baseline.talker or type(baseline.max_seq) is not int or baseline.max_seq < 0
            or (not baseline.seen_ids and baseline.max_seq != 0) or not math.isfinite(baseline.captured_at_s)
            or baseline.captured_at_s <= 0 or not math.isfinite(since_s)
            or since_s < int(baseline.captured_at_s) or image or not isinstance(text, str) or not text):
        return None
    candidates: dict[str, dict[str, Any]] = {}
    observed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("talker") != baseline.talker:
            continue
        try:
            seq = sequence(row)
        except WaError:
            return None
        ext = f"{baseline.talker}:{seq}"
        if row.get("ext_msg_id") not in (None, ext):
            return None
        if ext in observed and observed[ext] != row:
            return None
        observed[ext] = row
        stamp = _row_epoch_s(row)
        if seq <= baseline.max_seq:
            if ext not in baseline.seen_ids:
                return None  # The sequence space changed or was reset.
            continue
        if ext in baseline.seen_ids or ext in used_ids:
            continue
        own = row.get("isSelf") if "isSelf" in row else row.get("is_self")
        if own is not True or stamp is None or stamp < since_s or stamp > time.time() + 1:
            continue
        if type(row.get("type")) is not int or row["type"] != 1:
            continue
        body = row.get("content") if row.get("content") is not None else row.get("text")
        if body == text and isinstance(body, str):
            candidates[ext] = row
    return next(iter(candidates.values())) if len(candidates) == 1 else None


class WeChatWindowError(ValueError):
    """桌面上的微信主窗口不是恰好一个，不能发信。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def plan_wechat_foreground(active: Optional[int], hwnds: list[int]) -> tuple[str, int]:
    """决定发信前要不要把微信调到前台。

    桌面上必须恰好有一个微信主窗口。它已经是当前活跃窗口时直接发快捷键；
    不是时先把它变成唯一活跃窗口，再发快捷键。零个或多个都拒绝，避免发错窗口。
    """
    unique = list(dict.fromkeys(hwnds))
    if not unique:
        raise WeChatWindowError("wechat_window_missing", "桌面上没有已登录的微信窗口，无法发送")
    if len(unique) != 1:
        raise WeChatWindowError("wechat_window_ambiguous", "桌面上有多个微信窗口，无法确定发到哪一个")
    hwnd = unique[0]
    if active == hwnd:
        return "ready", hwnd
    return "activate", hwnd


def begin_wechat_input(active: Optional[int], hwnds: list[int], claim, foreground_now) -> int:
    """发任何按键或点击之前调用。微信不是前台就先切换；切完仍不是前台就拒绝，调用方不得再输入。"""
    action, hwnd = plan_wechat_foreground(active, hwnds)
    if action == "activate" and not claim(hwnd):
        raise WeChatWindowError("wechat_not_foreground", "微信窗口没有成为前台，快捷键未发出")
    if foreground_now() != hwnd:
        raise WeChatWindowError("wechat_not_foreground", "微信窗口没有成为前台，快捷键未发出")
    return hwnd


def chatlog_time_range(days: int = CHATLOG_READ_DAYS, *, today: Optional["object"] = None) -> str:
    """chatlog ``time`` 参数:``YYYY-MM-DD~YYYY-MM-DD``(实测说明页口径)。``today`` 仅测试注入。"""
    import datetime as _dt
    end = today or _dt.date.today()
    start = end - _dt.timedelta(days=max(0, days - 1))
    return f"{start.isoformat()}~{end.isoformat()}"


def chatlog_read_window(anchor: float, *, now: float | None = None) -> tuple[str, float]:
    """A fixed twelve-hour history floor, with a growing date-granularity envelope."""
    from datetime import datetime
    cutoff = anchor - 12 * 3600
    end = max(anchor, time.time() if now is None else now)
    period = datetime.fromtimestamp(cutoff).date().isoformat() + "~" + datetime.fromtimestamp(end).date().isoformat()
    return period, cutoff


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


LOG_REOPEN_RETRY_S = 5.0


def _shift_backups(path: str, backups: int) -> None:
    for index in range(backups, 1, -1):
        source = f"{path}.{index - 1}"
        if os.path.exists(source):
            os.replace(source, f"{path}.{index}")


def rotate_log(path: str, backups: int) -> None:
    """``path`` → ``path.1``,依次后移,超出 ``backups`` 的最旧一份被覆盖删除;``backups<=0`` 直接删掉。"""
    if backups <= 0:
        if os.path.exists(path):
            os.remove(path)
        return
    _shift_backups(path, backups)
    if os.path.exists(path):
        os.replace(path, f"{path}.1")


def rotate_oversized_log(path: str, max_bytes: int, backups: int) -> bool:
    """R6-118 ④:既有超限日志在拉起前按同一上限先行轮转 —— 只把末尾不超过 ``max_bytes`` 的整行留进 ``.1``。"""
    if max_bytes <= 0:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= max_bytes:
        return False
    tail = f"{path}.tail.tmp"
    try:
        if backups > 0:
            with open(path, "rb") as source, open(tail, "wb") as target:
                source.seek(size - max_bytes)
                first = source.read(1 << 20)
                cut = first.find(b"\n")
                target.write(first[cut + 1:] if cut >= 0 else first)
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    target.write(chunk)
            _shift_backups(path, backups)
            os.replace(tail, f"{path}.1")
        os.remove(path)
    except OSError as error:
        log.warning("chatlog 日志先行轮转失败,按原文件继续", extra={
            "op": "chatlog.log_rotate", "code": "ROTATE_FAILED", "kv": {"err": type(error).__name__}})
        try:
            os.remove(tail)
        except OSError:
            pass
        return False
    return True


class _LogSink:
    """按大小轮转的日志文件;任何文件错误都只丢本行,不抛给泵 —— 泵必须一直读,否则 chatlog 的输出管道会写满卡死。"""

    def __init__(self, path: str, max_bytes: int = 0, backups: int = 0):
        self._path, self._max, self._backups = path, max_bytes, backups
        self._file = None
        self._size = 0
        self._retry_at = 0.0
        self._open()

    def _open(self) -> None:
        try:
            self._file = open(self._path, "ab")
            self._size = self._file.tell()
        except OSError:
            self._file = None
            self._retry_at = time.monotonic() + LOG_REOPEN_RETRY_S

    def _rotate(self) -> None:
        self.close()
        try:
            rotate_log(self._path, self._backups)
        except OSError:
            try:
                with open(self._path, "wb"):
                    pass
            except OSError:
                pass
        self._open()

    def write(self, data: bytes) -> None:
        if self._max > 0 and len(data) > self._max:
            data = data[:self._max]
        if self._file is None:
            if time.monotonic() < self._retry_at:
                return
            self._open()
            if self._file is None:
                return
        if self._max > 0 and self._size > 0 and self._size + len(data) > self._max:
            self._rotate()
            if self._file is None:
                return
        try:
            self._file.write(data)
            self._file.flush()
            self._size += len(data)
        except OSError:
            self.close()
            self._retry_at = time.monotonic() + LOG_REOPEN_RETRY_S

    def close(self) -> None:
        f, self._file = self._file, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass


async def _pump_redacted(stream: "asyncio.StreamReader", log_path: str, *, max_bytes: int = 0,
                         backups: int = 0) -> None:
    """把子进程输出逐行脱敏后追加到日志;子进程退出(EOF)即结束。``max_bytes>0`` 时按大小轮转(R6-118 ④)。"""
    sink = _LogSink(log_path, max_bytes, backups)
    try:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:                                         # 超长行:缓冲已丢弃,继续读
                continue
            if not raw:
                break
            sink.write(redact_keys(decode_line(raw)).encode("utf-8", errors="replace"))
    finally:
        sink.close()


# ---------------------------------------------------------------- R6-118 filecopy 副本归属与清扫
INSTANCE_RECORD = "chatlog-instances.json"
INSTANCE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
INSTANCE_NAMES_KEEP = 16
INSTANCE_SEPARATOR = "_+"


def instance_of_copy(name: str, instances: "list[str] | tuple[str, ...]") -> Optional[str]:
    """副本名 = ``<实例名>_+<库名>_+…``;只认 ``<实例名>_+`` 整体前缀(``wxid_a`` 不命中 ``wxid_ab_+…``)。"""
    for instance in instances:
        if name.startswith(instance + INSTANCE_SEPARATOR):
            return instance
    return None


def scan_instance_copies(directory: str, instances: "list[str] | tuple[str, ...]") -> list[tuple[str, int, str]]:
    """只看 ``directory`` 这一层的普通文件(不递归、不跟链接),返回 ``[(路径, 字节数, 实例名)]``。"""
    if not instances:
        return []
    found: list[tuple[str, int, str]] = []
    try:
        entries = os.scandir(directory)
    except OSError:
        return []
    with entries:
        for entry in entries:
            instance = instance_of_copy(entry.name, instances)
            if instance is None:
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            found.append((entry.path, int(size), instance))
    return found


def sweep_instance_copies(directory: str, instances: "list[str] | tuple[str, ...]", *,
                          budget_s: Optional[float] = None) -> dict[str, Any]:
    """删除可证明归属的副本(含 ``-wal``/``-shm``);仍被占用或删不掉的只计数、跳过。超出 ``budget_s`` 停在原处。"""
    files = scan_instance_copies(directory, instances)
    deadline = None if budget_s is None else time.monotonic() + budget_s
    result = {"before_bytes": sum(size for _, size, _ in files), "freed_bytes": 0, "deleted": 0, "failed": 0,
              "complete": True}
    for path, size, _ in files:
        if deadline is not None and time.monotonic() >= deadline:
            result["complete"] = False
            break
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError:
            result["failed"] += 1
            continue
        result["deleted"] += 1
        result["freed_bytes"] += size
    return result


def resource_namespace(account_root: str) -> str:
    """chatlog work_dir 末级名 = filecopy 实例名(``resource-<sha256(账号根)[:24]>``)。"""
    identity = os.path.normcase(os.path.realpath(account_root))
    return "resource-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def pick_wechat_root_pid(procs: list[dict[str, Any]], window_pid: Optional[int] = None) -> Optional[int]:
    """取钥要挂的 ``Weixin.exe``:**登录主进程**(进程树的根)。``procs`` 只含 ``Weixin.exe``,每项 ``{pid, ppid, cmdline, rss}``。

    2026-10-10 真机:微信 4.x 是一个根进程 + 一串 ``--type=...`` 渲染 / 插件子进程;DLL 挂到子进程上特征扫描必然
    「模式匹配失败,找到 0 个结果」。主窗口最小化到托盘时 ``FindWindow`` 拿不到 pid。这是**兜底**选择(没有句柄判据时):
    有窗口 pid 就沿父链走到根；没有窗口证据时只接受唯一主进程，不以内存大小猜账号归属。
    登录主进程的另一项判据见 ``login_pid``(消息库文件句柄)。"""
    by = {int(p["pid"]): p for p in procs}
    if window_pid is not None and int(window_pid) in by:
        pid, seen = int(window_pid), set()
        while int(by[pid].get("ppid") or 0) in by and pid not in seen:
            seen.add(pid)
            pid = int(by[pid]["ppid"])
        return pid if pid not in seen else None
    roots = [p for p in procs if int(p.get("ppid") or 0) not in by]
    mains = [p for p in roots if "--type=" not in str(p.get("cmdline") or "")] or roots
    # 内存大小不能证明登录归属；没有窗口/账号句柄证据时，只接受唯一主进程。
    return int(mains[0]["pid"]) if len(mains) == 1 else None


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


async def spawn_redacted(args: list[str], *, cwd: str, log_path: str, max_bytes: int = 0,
                         backups: int = 0) -> "asyncio.subprocess.Process":
    """起子进程,stdout+stderr 经 ``redact_keys`` 落 ``log_path``(不再丢进 DEVNULL,也不让密钥明文落盘)。

    R6-96:Windows 上用 ``CREATE_NEW_PROCESS_GROUP`` 起 —— 这样 ``chatlog_stop`` 能对它发 ``CTRL_BREAK`` 让它**优雅退出、
    卸载 DLL hook**(否则强杀会把 inline hook 残留在微信进程里,后续取钥全"模式匹配失败")。"""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    startup = {}
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        startup["startupinfo"] = info
    p = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT, creationflags=flags, **startup)
    assert p.stdout is not None
    task = asyncio.get_running_loop().create_task(_pump_redacted(p.stdout, log_path, max_bytes=max_bytes,
                                                                 backups=backups))
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
    # 🔴 R6-100(2026-10-11 真机):取钥成功后,被注入的 Weixin.exe 会**持续 load** 这个 wx_key.dll;再次 login/start
    # 时旧实现无条件 shutil.copyfile 覆盖同一路径 ⇒ `PermissionError: [Errno 13]`(文件被另一进程占用)⇒ login/start
    # 恒 500、向导卡第一步。幂等化:目标已是同一字节(含被 load 的情形)直接复用,绝不去覆盖被占用的 DLL。
    if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src) and filecmp.cmp(src, dst, shallow=False):
        return dst
    # 不同候选不能冒充已经替换；调用方将权限错误转成可操作终态。
    shutil.copyfile(src, dst)
    return dst


class WinWeChat:
    def __init__(self, *, exe_path: str = "", chatlog_dir: str = "", chatlog_port: int = 5030,
                 chatlog_work_dir: str = "", main_wnd_class: str = "Qt51514QWindowIcon", process_close_grace_s: int = 10,
                 chatlog_log_max_bytes: int = 67108864, chatlog_log_backups: int = 5, temp_dir: str = ""):
        self._exe = exe_path
        self._chatlog_dir = chatlog_dir
        self._work_dir = chatlog_work_dir or os.path.join(tempfile.gettempdir(), "qtrade-chatlog")
        self._port = chatlog_port
        self._log_max_bytes = chatlog_log_max_bytes
        self._log_backups = chatlog_log_backups
        self._temp_dir = temp_dir                       # 空 = chatlog 继承的用户 %TEMP%(R6-118)
        self._namespace: Optional[str] = None           # 本次 server 的 work_dir 末级名 = filecopy 实例名
        from ..wechat_read_page import ResumableReads
        self._read_progress = ResumableReads()
        self._read_anchor: Optional[float] = None
        self._send_lock = asyncio.Lock()
        self._send_used_ids: dict[tuple, set[str]] = {}
        self._server_account: Optional[tuple[str, str]] = None
        self._cls = main_wnd_class
        self._grace = process_close_grace_s
        self._wechat_pid: Optional[int] = None
        self._chatlog_pid: Optional[int] = None
        self._chatlog_process: Optional[asyncio.subprocess.Process] = None
        self._chatlog_mode: Optional[str] = None
        self._chatlog_created: Optional[float] = None
        self._chatlog_record_id: Optional[str] = None
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
        require_windows("Narrator")
        from . import narrator
        # A pre-existing accessibility process belongs to the user, not this ritual.
        existing = await asyncio.to_thread(narrator.scoped_processes)
        if existing:
            raise WaError(NOT_READY, "Narrator is already running in this session", reason="narrator_not_owned")
        self._narrator_target = await asyncio.to_thread(narrator.start_owned)
        self._narrator_pid = self._narrator_target["pid"]
        return self._narrator_pid

    def narrator_stop_target(self) -> Optional[dict[str, Any]]:
        target = getattr(self, "_narrator_target", None)
        return dict(target) if target else None

    async def narrator_stop(self) -> bool:
        from . import narrator
        target = self.narrator_stop_target()
        if target is None:
            return not await asyncio.to_thread(self.narrator_running)
        owner = await asyncio.to_thread(narrator.current_owner)
        if not owner:
            return False
        stopped = await asyncio.to_thread(narrator.stop_owned, target, owner_sid=owner["sid"],
                                          owner_session_id=owner["session_id"])
        if stopped:
            self._narrator_pid, self._narrator_target = None, None
        return stopped

    @staticmethod
    def kill_narrator_elevated(target: Optional[dict[str, Any]] = None, *, owner_sid: str = "",
                               owner_session_id: int = 0) -> bool:
        from . import narrator
        return narrator.stop_owned(target, owner_sid=owner_sid, owner_session_id=owner_session_id)

    def narrator_running(self) -> bool:
        from . import narrator
        target = self.narrator_stop_target()
        try:
            if target:
                current = narrator.process_identity(target["pid"])
                if current is not None:
                    return current == target
                from .proc import WinProc
                return any(proc.pid == target["pid"] for proc in WinProc().find("Narrator.exe"))
            return bool(narrator.scoped_processes())
        except Exception:
            # Unknown state cannot prove successful cleanup.
            return True

    # ---------------------------------------------------------------- chatlog 与取钥
    def _key_preflight(self, dll: str) -> str:
        """检查当前动作的依赖，不把 ffmpeg 缺失混同为取钥失败。"""
        if os.path.basename(dll) != dll or not dll.lower().endswith(".dll"):
            raise WaError(NOT_READY, "取钥 DLL 配置不受支持", reason="wechat_dll_invalid")
        exe = os.path.join(self._chatlog_dir, "chatlog.exe")
        for path in (exe, os.path.join(self._chatlog_dir, dll)):
            try:
                with open(path, "rb") as f:
                    header = f.read(64)
                    pe_at = int.from_bytes(header[60:64], "little") if len(header) == 64 else 0
                    f.seek(pe_at)
                    pe = f.read(24)
                    if (header[:2] != b"MZ" or pe_at < 64 or len(pe) != 24 or pe[:4] != b"PE\0\0"
                            or int.from_bytes(pe[4:6], "little") != 0x8664):
                        raise WaError(NOT_READY, f"取钥文件格式损坏:{os.path.basename(path)}，请修复安装",
                                      reason="wechat_tool_corrupt")
            except FileNotFoundError as e:
                raise WaError(TARGET_NOT_FOUND, f"取钥文件不在位:{path}，请修复安装",
                              reason="wechat_tool_missing") from e
            except PermissionError as e:
                raise WaError(NOT_READY, f"无法读取取钥文件:{path}，请检查文件权限或安全软件隔离记录",
                              reason="wechat_tool_access_denied") from e
        try:
            os.makedirs(self._work_dir, exist_ok=True)
            with tempfile.TemporaryFile(dir=self._work_dir):
                pass
            with open(os.path.join(self._work_dir, "chatlog-key.log"), "ab"):
                pass
        except OSError as e:
            raise WaError(NOT_READY, "取钥工作目录不可写，请检查磁盘空间与目录权限",
                          reason="wechat_workdir_unwritable") from e
        return exe

    def _key_target(self) -> tuple[int, float]:
        import psutil
        pid = self.login_pid()
        if pid is None:
            raise WaError(NOT_READY, "无法确定唯一的微信主进程；请保留目标微信窗口后重试",
                          reason="wechat_pid_ambiguous")
        try:
            p = psutil.Process(pid)
            if p.name().lower() != "weixin.exe":
                raise psutil.NoSuchProcess(pid)
            selected = self._exe or (self.locate() or {}).get("path")
            if not selected or os.path.normcase(p.exe()) != os.path.normcase(selected):
                raise WaError(NOT_READY, "微信进程与已选择的安装目录不一致，请重新核对安装位置",
                              reason="wechat_pid_path_mismatch")
            return pid, p.create_time()
        except psutil.AccessDenied as e:
            raise WaError(NOT_READY, "无法访问微信进程，请检查微信与用户代理的权限",
                          reason="wechat_pid_access_denied") from e
        except psutil.Error as e:
            raise WaError(NOT_READY, "微信目标进程已退出，请重试", reason="wechat_pid_gone") from e

    @staticmethod
    def _target_alive(target: tuple[int, float]) -> bool:
        import psutil
        try:
            p = psutil.Process(target[0])
            return p.name().lower() == "weixin.exe" and p.create_time() == target[1]
        except psutil.Error:
            return False

    async def _spawn_owned(self, args: list[str], log_path: str) -> asyncio.subprocess.Process:
        if self._chatlog_process is not None and self._chatlog_process.returncode is None:
            raise WaError(BUSY, "前次取钥或读取服务仍在运行", reason="wechat_operation_busy")
        limits = ((self._log_max_bytes, self._log_backups) if len(args) > 1 and args[1] == "server" else (0, 0))
        task = asyncio.create_task(spawn_redacted(args, cwd=self._work_dir, log_path=log_path,
                                                  max_bytes=limits[0], backups=limits[1]))
        try:
            p = await asyncio.shield(task)
        except asyncio.CancelledError:
            # spawn 本身也可能已创建子进程；先收回句柄，随后仅清理这个子进程。
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled() and task.exception() is None:
                self._chatlog_process = task.result()
                self._chatlog_pid = task.result().pid
                await self.chatlog_stop()
            raise
        except OSError as e:
            reason = ("wechat_tool_missing" if isinstance(e, FileNotFoundError) else
                      "wechat_tool_access_denied" if isinstance(e, PermissionError) else
                      "wechat_tool_corrupt" if getattr(e, "winerror", None) in (193, 216) else
                      "wechat_tool_start_failed")
            raise WaError(NOT_READY, "取钥辅助程序无法启动，请检查安装完整性、架构与文件权限", reason=reason) from e
        self._chatlog_process, self._chatlog_pid = p, p.pid
        self._chatlog_mode = args[1]
        self._chatlog_created = await _blocking(self._process_created, p.pid)
        await _blocking(self._record_process, args[0], args[1], p.pid, self._chatlog_created)
        await asyncio.sleep(0.2)
        if p.returncode is not None:
            raise WaError(NOT_READY, f"取钥辅助程序启动后退出（exit={p.returncode}），请检查安装文件与日志",
                          reason="wechat_tool_exited")
        return p

    @property
    def _process_record_path(self) -> str:
        return os.path.join(self._work_dir, "chatlog-process.json")

    def _previous_process(self) -> Optional[dict[str, Any]]:
        """只核已记录 PID 的身份；不扫描名字、不自动接管或结束旧进程。"""
        import psutil
        try:
            with open(self._process_record_path, encoding="utf-8") as f:
                row = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise WaError(BUSY, "前次辅助程序归属记录不可验证，已阻止重复启动",
                          reason="wechat_previous_process_unverified") from e
        if (not isinstance(row, dict) or row.get("state") != "active" or type(row.get("pid")) is not int
                or row["pid"] <= 0 or not isinstance(row.get("operation_id"), str)
                or type(row.get("created")) not in (int, float) or row["created"] <= 0
                or row.get("mode") not in ("key", "server")
                or row.get("work_dir") != os.path.abspath(self._work_dir)
                or row.get("exe") != os.path.abspath(os.path.join(self._chatlog_dir, "chatlog.exe"))):
            raise WaError(BUSY, "前次辅助程序启动未完成或归属不明，请先核实遗留进程；未启动第二个程序",
                          reason="wechat_previous_process_unverified")
        try:
            proc = psutil.Process(row["pid"])
            if proc.create_time() != row["created"]:
                return row  # PID 已重用：只允许清除旧记录，绝不触碰新进程。
            if (os.path.normcase(proc.exe()) != os.path.normcase(row["exe"])
                    or len(proc.cmdline()) < 2 or proc.cmdline()[1] != row["mode"]):
                raise WaError(BUSY, "前次辅助程序身份不一致，已阻止自动接管或启动",
                              reason="wechat_previous_process_unverified")
        except psutil.NoSuchProcess:
            return row
        except psutil.Error as e:
            raise WaError(BUSY, "无法核实前次辅助程序的访问权限，已阻止重复启动",
                          reason="wechat_previous_process_unverified") from e
        raise WaError(BUSY, "前次用户代理退出后辅助程序仍在运行，请先核实并结束该受管进程；未启动第二个程序",
                      reason="wechat_previous_process_running")

    def _server_listener_state(self) -> tuple[str, Optional[int]]:
        """Enumerate only listeners which can conflict with the configured IPv4 loopback endpoint."""
        import psutil

        try:
            rows: list[Any] = []
            for kind in ("tcp", "tcp6"):
                part = psutil.net_connections(kind=kind)
                if not isinstance(part, list):
                    return "unknown", None
                rows.extend(part)
            listeners: set[Optional[int]] = set()
            for row in rows:
                if row.status != psutil.CONN_LISTEN or not row.laddr or row.laddr.port != self._port:
                    continue
                address = ipaddress.ip_address(row.laddr.ip)
                if address.version == 4:
                    conflict = address.is_unspecified or address == ipaddress.IPv4Address("127.0.0.1")
                elif address.ipv4_mapped is not None:
                    mapped = address.ipv4_mapped
                    conflict = mapped.is_unspecified or mapped == ipaddress.IPv4Address("127.0.0.1")
                elif address.is_unspecified:
                    # A wildcard IPv6 listener may or may not also own the IPv4 loopback endpoint.
                    return "unknown", None
                else:
                    conflict = False
                if conflict:
                    listeners.add(row.pid if isinstance(row.pid, int) and row.pid > 0 else None)
            if None in listeners or len(listeners) > 1:
                return "unknown", None
            if listeners:
                return "occupied", next(iter(listeners))
            return "free", None
        except Exception:  # noqa: BLE001 - an incomplete OS query is not evidence that the port is free
            return "unknown", None

    def _server_process_matches(self, pid: int) -> bool:
        """Verify the listening PID against the process object this instance launched."""
        import psutil

        process = self._chatlog_process
        if (process is None or process.returncode is not None or process.pid != pid
                or self._chatlog_mode != "server" or self._chatlog_created is None):
            return False
        try:
            current = psutil.Process(pid)
            expected = os.path.normcase(os.path.abspath(os.path.join(self._chatlog_dir, "chatlog.exe")))
            return (current.create_time() == self._chatlog_created
                    and os.path.normcase(os.path.abspath(current.exe())) == expected)
        except psutil.AccessDenied as exc:
            raise WaError(BUSY, "无法核实本次微信读取服务进程身份",
                          reason="wechat_server_owner_unverified") from exc
        except psutil.NoSuchProcess:
            return False

    def _chatlog_server_healthy(self, timeout_s: float) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=timeout_s) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read(4096).decode("utf-8") or "{}")
                return isinstance(payload, dict) and payload.get("status") == "ok"
        except Exception:  # noqa: BLE001 - readiness is retried within the shared start deadline
            return False

    def _reserve_start(self, mode: str) -> None:
        if self._chatlog_process is not None and self._chatlog_process.returncode is None:
            raise WaError(BUSY, "前次辅助程序仍在运行", reason="wechat_operation_busy")
        stale = self._previous_process()
        if stale is not None:
            self._remove_process_record(stale.get("operation_id"))
        if mode == "server":
            state, _ = self._server_listener_state()
            if state == "unknown":
                raise WaError(BUSY, "无法完整核实微信读取服务端口监听，已阻止重复启动",
                              reason="wechat_chatlog_port_unverified")
            if state == "occupied":
                raise WaError(BUSY, "微信读取服务端口已有监听，已阻止重复启动",
                              reason="wechat_chatlog_port_in_use")
        operation_id = uuid.uuid4().hex
        row = {"state": "pending", "operation_id": operation_id, "mode": mode,
               "exe": os.path.abspath(os.path.join(self._chatlog_dir, "chatlog.exe")),
               "work_dir": os.path.abspath(self._work_dir)}
        os.makedirs(self._work_dir, exist_ok=True)
        try:
            with open(self._process_record_path, "x", encoding="utf-8") as f:
                json.dump(row, f)
        except FileExistsError as e:
            raise WaError(BUSY, "另一次辅助程序启动已占用工作目录", reason="wechat_operation_busy") from e
        self._chatlog_record_id = operation_id

    def _record_process(self, exe: str, mode: str, pid: int, created: Optional[float]) -> None:
        if created is None or self._chatlog_record_id is None:
            raise WaError(NOT_READY, "辅助程序创建身份无法记录，已停止本次启动",
                          reason="wechat_process_identity_unavailable")
        with open(self._process_record_path, encoding="utf-8") as f:
            previous = json.load(f)
        if not isinstance(previous, dict) or previous.get("operation_id") != self._chatlog_record_id:
            raise WaError(BUSY, "辅助程序启动归属已变化，已停止本次启动", reason="wechat_previous_process_unverified")
        row = {"state": "active", "operation_id": self._chatlog_record_id, "pid": pid, "created": created,
               "exe": os.path.abspath(exe), "work_dir": os.path.abspath(self._work_dir), "mode": mode}
        temp = self._process_record_path + "." + self._chatlog_record_id + ".tmp"
        try:
            with open(temp, "x", encoding="utf-8") as f:
                json.dump(row, f)
            os.replace(temp, self._process_record_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _remove_process_record(self, operation_id: Optional[str]) -> None:
        if not operation_id:
            return
        try:
            with open(self._process_record_path, encoding="utf-8") as f:
                row = json.load(f)
            if row.get("operation_id") == operation_id:
                os.unlink(self._process_record_path)
        except FileNotFoundError:
            pass

    @staticmethod
    def _process_created(pid: int) -> Optional[float]:
        import psutil
        try:
            return psutil.Process(pid).create_time()
        except psutil.Error:
            return None

    async def preflight_key(self, dll: str) -> None:
        await _blocking(self._key_preflight, dll)

    async def chatlog_start(self, dll: str) -> int:
        """a) 起 hook:``chatlog.exe key``。⚠️ 日志里「Hook安装成功」**只证明 hook 装上了,不证明取到密钥**(05 §2.4.4a)。

        🔴 R6-90(2026-10-10 真机):旧实现 ``chatlog.exe key --dll <dll>`` —— ``key`` 子命令**没有 ``--dll``**,
        进程一启动就 ``Error: unknown flag: --dll`` 秒退,hook 从未装上,打开图片 / 重登都取不到密钥。正确做法:
        先 ``stage_wx_key_dll`` 把选中的 DLL 放成 ``<work>/lib/windows_x64/wx_key.dll``,再以 ``cwd=<work>`` 跑
        ``chatlog.exe key``(不带 DLL 参数)。stdout/stderr 落 ``<work>/chatlog-key.log`` 便于排障,不再丢进 DEVNULL。"""
        require_windows("chatlog")
        exe = await _blocking(self._key_preflight, dll)
        target = await _blocking(self._key_target)
        await _blocking(self._reserve_start, "key")
        try:
            await _blocking(stage_wx_key_dll, self._chatlog_dir, self._work_dir, dll)
        except PermissionError as e:
            raise WaError(NOT_READY, "取钥 DLL 无法替换，文件可能仍被微信占用；未使用旧 DLL 冒充本次候选",
                          reason="wechat_dll_in_use") from e
        if not await _blocking(self._target_alive, target):
            raise WaError(NOT_READY, "微信目标进程已变化，请重新开始取钥", reason="wechat_pid_gone")
        args = [exe, "key", "--debug", "--pid", str(target[0])]
        # 🔴 R6-93(真机日志):`chatlog key` 不带 `--pid` 会掉进交互式进程选择器(`Select a process:`),
        #    非 tty 下永久挂住 —— 全新机器常有「未登录微信」辅助进程 + 登录进程两个 Weixin.exe。显式指定登录主进程的 pid。
        log_path = os.path.join(self._work_dir, "chatlog-key.log")
        self._key_log_offset = await _blocking(lambda: os.path.getsize(log_path) if os.path.isfile(log_path) else 0)
        p = await self._spawn_owned(args, log_path)
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
        (窗口 pid 沿父链到根 / 唯一主进程)。"""
        import psutil
        try:
            window_pid = self.main_window().get("pid")
        except Exception:                                              # noqa: BLE001 —— FindWindow 失败不挡取钥
            window_pid = None
        procs: list[dict[str, Any]] = []
        objs: dict[int, Any] = {}
        incomplete = False
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() != "weixin.exe":
                    continue
                procs.append({"pid": p.info["pid"], "ppid": p.ppid(), "cmdline": " ".join(p.cmdline())})
                objs[p.info["pid"]] = p
            except Exception:                                          # noqa: BLE001 —— 已识别候选不能丢掉后再假报唯一
                incomplete = True
                continue
        if incomplete:
            raise WaError(NOT_READY, "微信进程列表不完整，无法验证唯一目标；请检查访问权限后重试",
                          reason="wechat_pid_access_denied")
        acc = self._account_dir()
        if acc:
            matched: set[int] = set()
            for pid, p in objs.items():
                try:
                    if any((f.path or "").lower().startswith(acc + os.sep) for f in p.open_files()):
                        root = pick_wechat_root_pid(procs, pid)
                        if root is not None:
                            matched.add(root)
                except Exception:                                      # noqa: BLE001 —— 句柄权限 / 进程消失:换下一个
                    continue
            if matched:
                return next(iter(matched)) if len(matched) == 1 else None
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
        self._read_progress.reset()
        self._read_anchor = None
        from .proc import WinProc
        p = self._chatlog_process
        if p is None:
            await _blocking(self._remove_process_record, self._chatlog_record_id)
            self._chatlog_record_id = None
            return
        created = self._chatlog_created
        try:
            if p.returncode is None:
                try:
                    # PID 复用不能把信号发给别人的进程；强制兜底只用创建时保留的 Process 句柄。
                    same = created is not None and await _blocking(self._process_created, p.pid) == created
                    if same and not await WinProc().interrupt(p.pid):
                        log.warning("辅助程序未接收 CTRL_BREAK", extra={"op": "chatlog.stop", "code": "SIGNAL_FAILED"})
                except (OSError, ValueError, AttributeError):
                    pass
                try:
                    await asyncio.wait_for(p.wait(), timeout=CHATLOG_STOP_POLLS * 0.3)
                except asyncio.TimeoutError:
                    # Process 对象持有创建时的 Windows 句柄，不根据重用的 PID 另找进程。
                    p.kill()
                    await p.wait()
        finally:
            if self._chatlog_process is p and p.returncode is not None:
                await _blocking(self._remove_process_record, self._chatlog_record_id)
                self._chatlog_record_id = None
                self._chatlog_process = None
                self._chatlog_pid = self._key_log_offset = None
                self._chatlog_mode = None
                self._chatlog_created = None

    async def chatlog_serve(self) -> int:
        """R6-91:取钥成功后切到 ``chatlog server``(:5030)供读消息 / H09 健康 / 发送读回确认。

        2026-10-10 真机:``server`` 空配置报 ``dataDir or workDir is required``,**不会**自己读 key 模式存的钥 ——
        必须显式给 data_dir/work_dir/两把钥。密钥写进 chatlog 自己会搜的 ``~/.chatlog/chatlog-server.json``
        (与它存 chatlog.json 同目录、同一份明文风险),**不放命令行**。数据目录按真实 DB 探测(兼容 v3/v4 布局)。"""
        require_windows("chatlog server")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHATLOG_SERVER_START_TIMEOUT_S
        last_state = "free"
        try:
            async with asyncio.timeout(CHATLOG_SERVER_START_TIMEOUT_S):
                await _blocking(self._reserve_start, "server")
                exe = await _blocking(self._prepare_server)
                p = await self._spawn_owned([exe, "server"], os.path.join(self._work_dir, "chatlog-server.log"))
                self._chatlog_pid = p.pid
                while True:
                    if p.returncode is not None:
                        raise WaError(NOT_READY, "微信读取服务启动后退出，请查看 chatlog-server.log",
                                      reason="wechat_server_process_exited")
                    state, listener_pid = await _blocking(self._server_listener_state)
                    last_state = state
                    if state == "occupied":
                        if listener_pid != p.pid or not self._server_process_matches(listener_pid):
                            raise WaError(BUSY, "端口监听者与本次微信读取服务进程不一致",
                                          reason="wechat_server_owner_unverified")
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        if await _blocking(self._chatlog_server_healthy, min(0.5, remaining)):
                            self._read_anchor = time.time()
                            return p.pid
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.1, remaining))
                if last_state == "unknown":
                    raise WaError(BUSY, "无法核实本次微信读取服务的监听状态",
                                  reason="wechat_server_owner_unverified")
                raise WaError(TIMEOUT, "微信读取服务未能在 5 秒内确认属于本次进程并就绪",
                              reason="wechat_server_start_timeout")
        except TimeoutError as exc:
            if last_state == "unknown":
                raise WaError(BUSY, "无法核实本次微信读取服务的监听状态",
                              reason="wechat_server_owner_unverified") from exc
            raise WaError(TIMEOUT, "微信读取服务启动与健康确认超过 5 秒",
                          reason="wechat_server_start_timeout") from exc

    def _prepare_server(self) -> str:
        os.makedirs(self._work_dir, exist_ok=True)
        server_log = os.path.join(self._work_dir, "chatlog-server.log")
        rotate_oversized_log(server_log, self._log_max_bytes, self._log_backups)
        with open(server_log, "ab"):
            pass
        keys = read_chatlog_keys()
        if not keys:
            raise RuntimeError("chatlog.json 里两把钥不全,不能起 server(先完成取钥)")
        loc = self.locate() or {}
        wxid = self.current_wxid()
        if not isinstance(wxid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", wxid) is None:
            raise RuntimeError("Current WeChat resource account is not verified")
        data_dir = discover_chatlog_data_dir(loc.get("data_root"), wxid)
        if not data_dir:
            raise RuntimeError("找不到当前微信消息库目录，请核对微信数据位置")
        # DB discovery verifies the account's data; resource paths start at its parent account root.
        account_root = os.path.realpath(os.path.join(loc["data_root"], "xwechat_files", wxid))
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", wxid or "")
                or os.path.commonpath([account_root, os.path.realpath(data_dir)]) != account_root):
            raise RuntimeError("Current WeChat resource root is not verified")
        namespace = resource_namespace(account_root)
        # 10-27 之前的旧布局 work_dir = decrypted\<wxid>;现役 data_dir 末级名同样是 <wxid>。两者都是本代理的实例名。
        self._remember_instance_names([namespace, wxid])
        self._namespace = namespace
        work = os.path.join(self._work_dir, "decrypted", wxid, namespace)
        os.makedirs(work, exist_ok=True)
        cfg = build_server_config(data_dir=account_root, work_dir=work, keys=keys, addr=f"127.0.0.1:{self._port}")
        cfg_dir = os.path.join(os.path.expanduser("~"), ".chatlog")
        os.makedirs(cfg_dir, exist_ok=True)
        tmp = os.path.join(cfg_dir, "chatlog-server.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(tmp, os.path.join(cfg_dir, "chatlog-server.json"))
        self._server_account = (wxid, os.path.normcase(account_root)) if wxid else None
        return os.path.join(self._chatlog_dir, "chatlog.exe")

    def chatlog_status(self) -> dict[str, Any]:
        """只报告本实例持有的进程。遗留记录留给下一次启动去拒绝，状态查询本身不因此失败。"""
        running = self._chatlog_process is not None and self._chatlog_process.returncode is None
        http_ok = False
        if running and self._chatlog_mode == "server":
            try:
                import urllib.request
                # R6-91:H09 改探 ``/health``(实测回 ``{"status":"ok"}``)。原 ``/api/v1/session?limit=1`` 在会话为空时
                # chatlog 回 404,会把「空号 / 刚登录还没消息」误判成 chatlog 挂了。
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=1) as r:
                    http_ok = r.status == 200
            except Exception:
                http_ok = False
        return {"running": running, "http_ok": http_ok, "dll": self._dll, "port": self._port}

    # ---------------------------------------------------------------- R6-118 回收事实与副本清扫(05 §2.4.10)
    def _filecopy_dir(self) -> str:
        return os.path.join(self._temp_dir or tempvol.user_temp_dir(), tempvol.FILECOPY_DIRNAME)

    @property
    def _instance_record_path(self) -> str:
        return os.path.join(self._work_dir, INSTANCE_RECORD)

    def _read_instance_names(self) -> list[str]:
        try:
            with open(self._instance_record_path, encoding="utf-8") as f:
                row = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("chatlog 实例名记录不可读,本次只用当前实例名", extra={"op": "chatlog.sweep", "code": "RECORD_UNREADABLE"})
            return []
        names = row.get("names") if isinstance(row, dict) else None
        if not isinstance(names, list):
            return []
        return [n for n in names if isinstance(n, str) and INSTANCE_NAME_RE.fullmatch(n)]

    def _remember_instance_names(self, names: list[str]) -> None:
        """持久化本代理用过的实例名(T7),换实例名后旧前缀的残留仍可证明归属。写失败只记日志,不挡拉起。"""
        fresh = [n for n in names if isinstance(n, str) and INSTANCE_NAME_RE.fullmatch(n)]
        known = self._read_instance_names()
        merged = [n for n in known if n not in fresh] + list(dict.fromkeys(fresh))
        merged = merged[-INSTANCE_NAMES_KEEP:]
        if merged == known:
            return
        temp = self._instance_record_path + "." + uuid.uuid4().hex + ".tmp"
        try:
            os.makedirs(self._work_dir, exist_ok=True)
            with open(temp, "x", encoding="utf-8") as f:
                json.dump({"version": 1, "names": merged}, f)
            os.replace(temp, self._instance_record_path)
        except OSError as error:
            log.warning("chatlog 实例名记录写入失败", extra={"op": "chatlog.sweep", "code": "RECORD_WRITE_FAILED",
                                                          "kv": {"err": type(error).__name__}})
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _current_instance_names(self) -> list[str]:
        """当前账号对应的实例名:``resource-<hash>`` 与 ``<wxid>``;定位不到账号时退回本进程上次拉起用的,都没有为空,不猜。"""
        cached = [self._namespace, self._server_account[0]] if self._namespace and self._server_account else []
        try:
            loc = self.locate() or {}
            wxid = self.current_wxid()
            if (not isinstance(wxid, str) or INSTANCE_NAME_RE.fullmatch(wxid) is None
                    or not discover_chatlog_data_dir(loc.get("data_root"), wxid)):
                return cached
            account_root = os.path.realpath(os.path.join(loc["data_root"], "xwechat_files", wxid))
            return list(dict.fromkeys([resource_namespace(account_root), wxid] + cached))
        except Exception:                                              # noqa: BLE001 —— 定位失败只是少清一个前缀
            return cached

    def _counted_instance_names(self) -> list[str]:
        """判据 (a)/(c) 的统计口径(DECISION-3 C1):已记录的上一实例名(含旧 ``<wxid>``)+ 本进程当前实例名;不定位账号。"""
        current = [self._namespace] if self._namespace else []
        if self._server_account:
            current.append(self._server_account[0])
        return list(dict.fromkeys(self._read_instance_names() + current))

    def chatlog_recycle_facts(self) -> dict[str, Any]:
        """回收判据的事实:本代理 chatlog 是否在跑、模式、归属(创建时间 + 可执行路径一致)、本实例副本量、临时卷空闲。"""
        process = self._chatlog_process
        running = process is not None and process.returncode is None
        mode = self._chatlog_mode if running else None
        owned = False
        if running and mode == "server":
            try:
                owned = self._server_process_matches(process.pid)
            except WaError:
                owned = False
        copies = scan_instance_copies(self._filecopy_dir(), self._counted_instance_names())
        try:
            volume = tempvol.sample(self._temp_dir or None)
        except OSError:
            volume = {"volume": None, "free_bytes": None}
        return {"running": running, "mode": mode, "owned": owned,
                "copy_bytes": sum(size for _, size, _ in copies), "copy_files": len(copies),
                "temp_volume": volume["volume"], "temp_free_bytes": volume["free_bytes"]}

    def chatlog_sweep_copies(self, *, budget_s: float = 15.0) -> dict[str, Any]:
        """在 ``%TEMP%\\filecopy_chatlog`` 这一层删除本实例与已知上一实例前缀的副本;本代理的 chatlog 在跑时不动。"""
        process = self._chatlog_process
        if process is not None and process.returncode is None:
            return {"before_bytes": 0, "freed_bytes": 0, "deleted": 0, "failed": 0, "complete": False,
                    "skipped": "chatlog_running", "instances": 0}
        current = self._current_instance_names()
        if current:
            self._remember_instance_names(current)
        names = list(dict.fromkeys(self._read_instance_names() + current))
        result = sweep_instance_copies(self._filecopy_dir(), names, budget_s=budget_s)
        result["instances"] = len(names)
        return result

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
    def _metadata_body(self, url: str, *, deadline: float) -> bytes:
        """Interrupt this request's socket at the absolute metadata deadline."""
        import http.client
        import socket
        import threading
        import urllib.error
        import urllib.parse

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Metadata deadline expired")
        connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=remaining)
        response = None
        timer = None
        try:
            connection.connect()
            owned_socket = connection.sock

            def interrupt() -> None:
                try:
                    owned_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                finally:
                    owned_socket.close()

            timer = threading.Timer(max(0.0, deadline - time.monotonic()), interrupt)
            timer.start()
            parsed = urllib.parse.urlsplit(url)
            connection.request("GET", parsed.path + "?" + parsed.query)
            response = connection.getresponse()
            if response.status >= 400:
                raise urllib.error.HTTPError(url, response.status, "Chatlog HTTP error", response.headers, None)
            raw = response.read(4 * 1024 * 1024 + 1)
            if time.monotonic() >= deadline:
                raise TimeoutError("Metadata deadline expired")
            return raw
        except (OSError, http.client.HTTPException) as error:
            if time.monotonic() >= deadline:
                raise TimeoutError("Metadata deadline expired") from None
            if isinstance(error, http.client.HTTPException):
                raise WaError(NOT_READY, "wechat_read_transport_error; stored keys unchanged",
                              reason="wechat_read_transport_error", retryable=True) from None
            raise
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()

    def _get_items(self, path: str, query: dict[str, Any], *, timeout_s: float = 10.0,
                   deadline: Optional[float] = None) -> list[dict[str, Any]]:
        import socket
        import urllib.error
        import urllib.parse
        import urllib.request
        from ..errors import RESOURCE_EXHAUSTED
        url = f"http://127.0.0.1:{self._port}{path}?" + urllib.parse.urlencode({**query, "format": "json"})
        try:
            if deadline is None:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url, timeout=timeout_s) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise WaError(RESOURCE_EXHAUSTED, "Chatlog HTTP page too large", reason="wechat_read_response_too_large")
            else:
                raw = self._metadata_body(url, deadline=deadline)
                if len(raw) > 4 * 1024 * 1024:
                    raise WaError(RESOURCE_EXHAUSTED, "Chatlog HTTP page too large", reason="wechat_read_response_too_large")
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            # The pinned Chatlog API uses 404 for an empty collection.
            if status == 404 and path in {"/api/v1/session", "/api/v1/chatlog", "/api/v1/contact", "/api/v1/chatroom"}:
                return []
            raise WaError(NOT_READY, f"Chatlog returned HTTP {status}; stored keys unchanged",
                          reason=f"wechat_read_http_{status}", retryable=status >= 500) from None
        except (TimeoutError, socket.timeout):
            raise WaError(TIMEOUT, "Chatlog request timed out", reason="wechat_read_timeout", retryable=True) from None
        except (urllib.error.URLError, OSError) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            number = getattr(cause, "winerror", None) or getattr(cause, "errno", None)
            if isinstance(cause, (TimeoutError, socket.timeout)) or number in (60, 110, 10060):
                reason = "wechat_read_timeout"
            elif isinstance(cause, ConnectionRefusedError) or number in (61, 111, 10061):
                reason = "wechat_read_connection_refused"
            elif isinstance(cause, PermissionError) or number in (5, 13, 10013):
                reason = "wechat_read_access_denied"
            else:
                reason = "wechat_read_transport_error"
            raise WaError(NOT_READY, reason + "; stored keys unchanged", reason=reason, retryable=True) from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise WaError(NOT_READY, "Invalid Chatlog JSON", reason="wechat_read_invalid_json") from None
        rows = body if isinstance(body, list) else body.get("items") if isinstance(body, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise WaError(NOT_READY, "Invalid Chatlog page shape", reason="wechat_read_invalid_response")
        return rows

    async def read_messages(self, *, talker: Optional[str], since_seq: Optional[int], limit: int) -> list[dict[str, Any]]:
        # Both "time" and "talker" are mandatory in each backend query.
        if self._read_anchor is None:
            self._read_anchor = time.time()
        period, cutoff = chatlog_read_window(self._read_anchor)
        return await _blocking(lambda: self._read_progress.read(
            self._get_items, time_range=period, talker=talker,
            since_seq=since_seq, limit=limit, not_before=cutoff))

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

    def prepare_send(self) -> None:
        """Prepare only the verified existing main window, without starting WeChat."""
        self._require_foreground()

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
        if self._send_lock.locked():
            raise WaError(BUSY, "Another native send is active", reason="wechat_send_busy")
        async with self._send_lock:
            return await self._send_with_baseline(session_name=session_name, text=text, image_path=image_path,
                                                  confirm_timeout_ms=confirm_timeout_ms)

    def _send_identity(self) -> tuple[str, int, float, int, float, str]:
        """Bind the managed reader to the existing process holding this account's DB."""
        import psutil
        binding = self._server_account
        state, server_pid = self._server_listener_state()
        if (binding is None or state != "occupied" or server_pid is None
                or not self._server_process_matches(server_pid)):
            raise WaError(NOT_READY, "Native account reader identity is unavailable",
                          reason="wechat_send_identity_unverified")
        wxid, data_dir = binding
        pid, created = self._key_target()
        try:
            files = psutil.Process(pid).open_files()
            matched = any(os.path.normcase(os.path.realpath(item.path)).startswith(data_dir + os.sep)
                          and item.path.lower().endswith((".db", ".db-wal", ".db-shm")) for item in files)
        except psutil.Error:
            matched = False
        if not matched or not self._target_alive((pid, created)):
            raise WaError(NOT_READY, "Current WeChat process does not prove the reader account",
                          reason="wechat_send_identity_unverified")
        return wxid, pid, created, server_pid, self._chatlog_created, data_dir

    async def _send_with_baseline(self, *, session_name: str, text: Optional[str],
                                  image_path: Optional[str], confirm_timeout_ms: int) -> dict[str, Any]:
        from ..wechat_read_page import sequence
        name = await asyncio.to_thread(self._resolve_send_target, session_name)
        identity = await asyncio.to_thread(self._send_identity)
        before = await asyncio.to_thread(self._readback_rows, session_name, None)
        if any(row.get("talker") != session_name or _row_epoch_s(row) is None for row in before):
            raise WaError(NOT_READY, "A complete native conversation baseline is unavailable",
                          reason="wechat_send_baseline_unavailable")
        seqs = [sequence(row) for row in before]
        if len(set(seqs)) != len(seqs):
            raise WaError(NOT_READY, "Native baseline identities are ambiguous",
                          reason="wechat_send_baseline_unavailable")
        baseline = ReadbackBaseline(identity[0], session_name, max(seqs, default=0),
                                    frozenset(f"{session_name}:{seq}" for seq in seqs), time.time())
        anchor_row = next((row for row in before if sequence(row) == baseline.max_seq), None)
        anchor = None
        if anchor_row is not None:
            body = anchor_row.get("content") if anchor_row.get("content") is not None else anchor_row.get("text")
            anchor = {"seq": sequence(anchor_row), "time_s": _row_epoch_s(anchor_row),
                      "text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()
                      if isinstance(body, str) else None}
        await asyncio.to_thread(self._require_foreground)
        if await asyncio.to_thread(self._send_identity) != identity:
            raise WaError(NOT_READY, "Native account changed before input",
                          reason="wechat_send_identity_changed")
        started = time.time()
        if image_path:
            await self._pyweixin_send(name, text, image_path)
        else:
            await _blocking(self._keyboard_send_text, name, text or "", session_name.endswith("@chatroom"))
        completed = time.time()
        deadline = asyncio.get_running_loop().time() + confirm_timeout_ms / 1000
        used = self._send_used_ids.setdefault(identity, set())
        reason, trusted_snapshot = "confirm_timeout", False
        while asyncio.get_running_loop().time() < deadline:
            snapshot_timeout = None
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                snapshot_timeout = asyncio.timeout(remaining)
                async with snapshot_timeout:
                    current, rows = await self._read_send_snapshot(session_name)
                if current != identity:
                    reason = "identity_changed"
                    break
                if anchor is not None:
                    current_anchor = next((row for row in rows
                                           if row.get("talker") == session_name and sequence(row) == anchor["seq"]), None)
                    body = (current_anchor.get("content") if current_anchor.get("content") is not None
                            else current_anchor.get("text")) if current_anchor else None
                    if (current_anchor is None or _row_epoch_s(current_anchor) != anchor["time_s"]
                            or (anchor["text_sha256"] is not None
                                and (not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest()
                                     != anchor["text_sha256"]))):
                        reason = "anchor_changed"
                        break
                previous, candidates = -1, 0
                for row in rows:
                    seq, stamp = sequence(row), _row_epoch_s(row)
                    ext = f"{session_name}:{seq}"
                    if (row.get("talker") != session_name or seq <= previous
                            or row.get("ext_msg_id") not in (None, ext) or stamp is None
                            or stamp > time.time() + 1
                            or (seq <= baseline.max_seq and ext not in baseline.seen_ids)):
                        reason = "invalid_snapshot"
                        break
                    previous = seq
                    own = row.get("isSelf") if "isSelf" in row else row.get("is_self")
                    body = row.get("content") if row.get("content") is not None else row.get("text")
                    if (seq > baseline.max_seq and own is True and type(row.get("type")) is int
                            and row["type"] == 1 and body == text and stamp >= int(started)):
                        candidates += 1
                if reason == "invalid_snapshot":
                    break
                if candidates > 1:
                    reason = "ambiguous_candidate"
                    break
                trusted_snapshot = True
                hit = pick_readback(rows, text=text, image=bool(image_path), since_s=int(started),
                                    baseline=baseline, account_id=current[0], used_ids=frozenset(used))
            except asyncio.TimeoutError:
                reason = ("confirm_timeout" if trusted_snapshot and snapshot_timeout is not None
                          and snapshot_timeout.expired() else "read_failed")
                break
            except WaError as error:
                reason = "identity_changed" if error.reason == "wechat_send_identity_changed" else "read_failed"
                break
            except Exception:  # Input completed; a read failure never proves it did not send.
                reason = "read_failed"
                break
            if hit is not None:
                ext = f"{session_name}:{sequence(hit)}"
                used.add(ext)
                log.info("wechat confirmation exit=confirmed")
                return {"ok": True, "code": "DELIVERED", "ext_msg_id": ext,
                        "confirm_ms": int((time.time() - started) * 1000), "confirmation_reason": "confirmed"}
            await asyncio.sleep(min(1, max(0, deadline - asyncio.get_running_loop().time())))
        result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                  "confirm_ms": confirm_timeout_ms, "confirmation_reason": reason}
        if (reason == "confirm_timeout" and trusted_snapshot and not image_path
                and isinstance(text, str) and text and (anchor is None or anchor["text_sha256"] is not None)):
            result["confirmation_proof"] = {
                "version": 1, "self_uid": baseline.account_id, "native_pid": identity[1],
                "talker": baseline.talker, "max_seq": baseline.max_seq, "anchor": anchor,
                "captured_at_s": baseline.captured_at_s, "send_started_at_s": started,
                "action_completed_at_s": completed, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        log.info("wechat confirmation exit=%s proof=%s baseline_at=%s max_seq=%s started_at=%s completed_at=%s text_sha256=%s",
                 reason, "confirmation_proof" in result, baseline.captured_at_s, baseline.max_seq, started, completed,
                 hashlib.sha256(text.encode("utf-8")).hexdigest() if isinstance(text, str) else None)
        return result

    async def _read_send_snapshot(self, talker: str) -> tuple[tuple, list[dict[str, Any]]]:
        identity = await asyncio.to_thread(self._send_identity)
        rows = await asyncio.to_thread(self._readback_rows, talker, None)
        if await asyncio.to_thread(self._send_identity) != identity:
            raise WaError(NOT_READY, "Native account changed during readback",
                          reason="wechat_send_identity_changed")
        return identity, rows

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

    def _keyboard_send_text(self, name: str, text: str, expected_group: bool = False) -> None:
        """先把唯一微信主窗口调到前台，再通过唯一 UIA 控件搜索目标并发送。

        微信 4 的 Ctrl+F 打开「搜一搜」，不会切到目标会话，所以不走那个快捷键。
        对象唯一性在 ``_resolve_send_target`` 已拒绝重名。是否写进该会话，由随后的 chatlog 读回判定。"""
        hwnd = self._require_foreground()
        _click_send_in_open_wechat(hwnd, name, text, expected_group=expected_group)

    def _require_foreground(self) -> int:
        """当前活跃窗口不是微信时，先把唯一的微信主窗口调成前台。切不成就不返回。"""
        import win32gui
        from ..errors import NOT_READY, WaError

        try:
            return begin_wechat_input(
                win32gui.GetForegroundWindow(),
                wechat_main_hwnds(_visible_window_rows()),
                _claim_foreground,
                win32gui.GetForegroundWindow,
            )
        except WeChatWindowError as exc:
            raise WaError(NOT_READY, str(exc), reason=exc.reason) from exc

    def _readback_rows(self, talker: str, text: Optional[str]) -> list[dict[str, Any]]:
        """Read a complete bounded conversation; a partial page is never a watermark."""
        from datetime import date
        from ..wechat_read_page import FETCH_ROWS, MAX_SCANNED_ROWS, sequence
        rows: list[dict[str, Any]] = []
        previous = -1
        deadline = time.monotonic() + 10
        while len(rows) < MAX_SCANNED_ROWS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaError(NOT_READY, "Native baseline scan exceeded its budget",
                              reason="wechat_send_baseline_unavailable")
            query = {"time": f"1970-01-01~{date.today().isoformat()}", "talker": talker,
                     "limit": FETCH_ROWS, "offset": len(rows)}
            batch = self._get_items("/api/v1/chatlog", query, timeout_s=remaining)
            for row in batch:
                seq = sequence(row)
                if (row.get("talker") != talker or seq <= previous or _row_epoch_s(row) is None
                        or _row_epoch_s(row) > time.time() + 1):
                    raise WaError(NOT_READY, "Native conversation scan is not a trustworthy sequence",
                                  reason="wechat_send_baseline_unavailable")
                previous = seq
            rows.extend(batch)
            if len(batch) < FETCH_ROWS:
                return rows
        raise WaError(NOT_READY, "Native conversation exceeds the baseline scan limit",
                      reason="wechat_send_baseline_unavailable")

    async def list_sessions(self, *, keyword: Optional[str], limit: int) -> list[dict[str, Any]]:
        q = {k: v for k, v in (("keyword", keyword), ("limit", limit)) if v}
        sessions = await asyncio.to_thread(self._get_items, "/api/v1/session", q)
        if not sessions:
            return sessions
        # Metadata is optional enrichment within the existing directory timeout.
        deadline = time.monotonic() + 2.0

        def metadata(path: str) -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            page_size = 1000
            for page in range(20):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch = self._get_items(path, {"limit": page_size, "offset": page * page_size},
                                            timeout_s=remaining, deadline=deadline)
                except WaError as error:
                    log.warning("WeChat directory metadata unavailable", extra={"code": error.code})
                    break
                ids = {_f(row, "UserName", "Name") for row in batch} - {""}
                if not ids - seen:
                    break
                rows.extend(batch)
                seen.update(ids)
                if len(batch) < page_size:
                    break
            return rows

        sources = await asyncio.gather(
            _blocking(metadata, "/api/v1/contact"),
            _blocking(metadata, "/api/v1/chatroom"),
            return_exceptions=True,
        )
        for source in sources:
            if isinstance(source, BaseException):
                raise source
        contacts, rooms = sources
        names: dict[str, str] = {}
        # Remarks take precedence across both sources; blank contact rows cannot hide rooms.
        for field in ("Remark", "NickName"):
            for rows, keys in ((contacts, ("UserName",)), (rooms, ("Name", "UserName"))):
                for row in rows:
                    native = _f(row, *keys)
                    name = _f(row, field).strip()
                    if native and name:
                        names.setdefault(native, name)
        result = []
        for row in sessions:
            native = _f(row, "UserName", "Talker", "Native_id", "Id")
            fallback = native
            if not native.endswith("@chatroom"):
                fallback = _f(row, "Remark", "NickName", "Name").strip() or native
            result.append({**row, "display_name": names.get(native, fallback)})
        return result

    async def media(self, key: str) -> bytes:
        import io
        import ntpath
        import urllib.error
        import urllib.parse
        import urllib.request
        import warnings
        from PIL import Image

        if not isinstance(key, str) or not key or len(key) > 2048:
            raise ValueError("Invalid native image reference")
        expected_uid, relative = None, None
        if key.startswith("wximage-v1."):
            encoded = key.split(".", 1)[1]
            if re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
                raise ValueError("Invalid native image reference")
            try:
                raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
                    raise ValueError()
                def unique_fields(pairs):
                    values = dict(pairs)
                    if len(values) != len(pairs):
                        raise ValueError()
                    return values
                descriptor = json.loads(raw, object_pairs_hook=unique_fields)
                if not isinstance(descriptor, dict) or set(descriptor) != {"uid", "md5", "path"}:
                    raise ValueError()
                expected_uid, key, relative = descriptor["uid"], descriptor["md5"], descriptor["path"]
                if not isinstance(expected_uid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", expected_uid) is None:
                    raise ValueError()
            except (ValueError, UnicodeError, TypeError) as error:
                raise ValueError("Invalid native image reference") from error
        elif "," in key:
            key, relative = key.split(",", 1)
        if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) is None:
            raise ValueError("Invalid native image key")
        if relative is not None:
            if not isinstance(relative, str) or not relative or len(relative) > 1024:
                raise ValueError("Invalid native image path")
            relative = relative.replace("/", "\\")
            parts = relative.split("\\")
            if (ntpath.isabs(relative) or ntpath.splitdrive(relative)[0]
                    or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts)
                    or any(ord(ch) < 32 or ch in ':<>"|?*%,' for ch in relative)):
                raise ValueError("Invalid native image path")
        identity = await asyncio.to_thread(self._send_identity)
        if expected_uid is not None and expected_uid != identity[0]:
            raise WaError(NOT_READY, "Image reference belongs to another account", reason="wechat_media_identity_changed")
        if relative is not None:
            root = os.path.normcase(os.path.realpath(identity[5]))
            candidate = os.path.join(root, *relative.split("\\"))
            for suffix in ("", ".dat", "_h.dat", "_t.dat"):
                resolved = os.path.normcase(os.path.realpath(candidate + suffix))
                if os.path.commonpath([root, resolved]) != root:
                    raise ValueError("Native image path escapes its account")
            key += "," + relative

        def go() -> bytes:
            address = f"http://127.0.0.1:{self._port}/image/" + urllib.parse.quote(key, safe="")
            try:
                with urllib.request.urlopen(address, timeout=10) as response:
                    data = response.read(20 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise WaError(TARGET_NOT_FOUND, "Native image resource was not found", reason="wechat_media_not_found") from error
                raise WaError(NOT_READY, "Native image read failed", reason="wechat_media_read_failed", retryable=True) from error
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("Native image exceeds the media limit")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as picture:
                        picture.verify()
                    with Image.open(io.BytesIO(data)) as picture:
                        picture.load()
                        if picture.width < 1 or picture.height < 1:
                            raise ValueError()
            except Exception as error:
                raise WaError(NOT_READY, "Native media is not a complete decoded image", reason="wechat_media_invalid_image") from error
            return data

        data = await asyncio.to_thread(go)
        if await asyncio.to_thread(self._send_identity) != identity:
            raise WaError(NOT_READY, "Image account changed during read", reason="wechat_media_identity_changed")
        return data

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


_SEARCH_TARGET_SECTIONS = frozenset({
    "功能", "联系人", "群聊", "最近使用", "最常使用",
    "Features", "Contacts", "Groups", "Recent used", "Most used",
    "聯絡人", "群組",
})
_SEARCH_REJECT_SECTIONS = frozenset({
    "聊天记录", "搜索网络结果", "网络结果",
    "Chat History", "聊天記錄",
})


def pick_wechat_search_hit(labels: list[str], name: str) -> int | None:
    """在搜索下拉里只点「功能 / 联系人 / 群聊」的同名项。聊天记录和搜索网络结果一律不点。"""
    wanted = name.strip()
    if not wanted:
        return None
    section = ""
    found: list[int] = []
    for index, raw in enumerate(labels):
        text = " ".join(raw.split())
        if not text:
            continue
        if text in _SEARCH_TARGET_SECTIONS or text in _SEARCH_REJECT_SECTIONS:
            section = text
            continue
        row_section = ""
        title = text
        for candidate in _SEARCH_TARGET_SECTIONS | _SEARCH_REJECT_SECTIONS:
            suffix = " - " + candidate
            if text.endswith(suffix):
                row_section = candidate
                title = text[: -len(suffix)].strip()
                break
        if row_section:
            section = row_section
        if section in _SEARCH_REJECT_SECTIONS or section not in _SEARCH_TARGET_SECTIONS:
            continue
        if title == wanted:
            found.append(index)
    if len(found) != 1:
        return None
    return found[0]


def wechat_click_points(width: int, height: int) -> dict[str, tuple[int, int]]:
    """主窗口里三个点击位置。比例来自已经把文字写进文件传输助手的那次窗口。"""
    return {
        "search": (int(width * 0.18), int(height * 0.09)),
        "composer": (int(width * 0.46), int(height * 0.88)),
        "send": (int(width * 0.63), int(height * 0.945)),
    }


def _wechat_ui_call(stage: str, action, *, retry_read: bool = False):
    """只对失效元素的完整只读操作重取一次；原生输入、点击绝不重放。"""
    for attempt in range(2 if retry_read else 1):
        try:
            return action()
        except Exception as exc:
            hresult = getattr(exc, "hresult", None)
            if hresult is None and exc.args and isinstance(exc.args[0], int):
                hresult = exc.args[0]
            hresult = hresult & 0xFFFFFFFF if isinstance(hresult, int) else None
            # 不记录异常正文、源码行或局部变量：COM 消息和输入参数可能含联系人与正文。
            stack = []
            tb = exc.__traceback__
            while tb is not None:
                frame = tb.tb_frame
                stack.append(f"{frame.f_code.co_filename}:{tb.tb_lineno}:{frame.f_code.co_name}")
                tb = tb.tb_next
            safe = RuntimeError(f"WeChat UI {type(exc).__name__}; HRESULT={hresult}")
            log.warning("微信 UI 操作失败", exc_info=(RuntimeError, safe, None),
                        extra={"op": "wechat.send", "kv": {"stage": stage,
                               "hresult": f"0x{hresult:08X}" if hresult is not None else "none",
                               "stack": " | ".join(stack)}})
            if not retry_read or hresult != 0x80040201:
                raise
            if attempt == 1:
                raise WaError(NOT_READY, "微信界面元素持续失效，已停止发送",
                              reason="wechat_uia_element_unavailable") from exc


def _ensure_wechat_foreground(hwnd: int) -> bool:
    """每个原生步骤前核同一个唯一主窗；需要时复用正常前台切换并二次确认。"""
    import win32gui

    active = win32gui.GetForegroundWindow()
    try:
        hwnds = wechat_main_hwnds(_visible_window_rows())
        _action, current = plan_wechat_foreground(active, hwnds)
        if current != hwnd:
            raise WeChatWindowError("wechat_window_changed", "微信主窗口已变化，已停止发送")
        begin_wechat_input(active, hwnds, _claim_foreground, win32gui.GetForegroundWindow)
    except WeChatWindowError as exc:
        raise WaError(NOT_READY, str(exc), reason=exc.reason) from exc
    return active != hwnd


def _search_popup_entries(win) -> list[tuple[object, str]]:
    """完整重取文字行，不能略过失效分组或候选造成假唯一。"""
    def read():
        entries: list[tuple[object, str]] = []
        for ctrl in win.descendants():
            kind = str(ctrl.element_info.control_type or "")
            if kind not in ("ListItem", "Text"):
                continue
            text = " ".join((ctrl.window_text() or "").split())
            if text:
                entries.append((ctrl, text))
        return entries
    return _wechat_ui_call("search_snapshot", read, retry_read=True)


def _verify_wechat_chat_header(win, composer, target_control, expected_name: str, *,
                               expected_group: bool = False) -> None:
    """Verify the recipient in the composer's own message page, never in its draft."""
    def unverified():
        raise WaError(NOT_READY, "WeChat conversation identity could not be verified",
                      reason="wechat_session_unverified")

    def identity(ctrl):
        info = ctrl.element_info
        runtime_id = tuple(info.runtime_id or ())
        if (not runtime_id or any(not isinstance(part, int) for part in runtime_id)
                or not isinstance(info.process_id, int) or info.process_id <= 0):
            unverified()
        return runtime_id, info

    root_id, root_info = identity(win)
    if (root_info.class_name != "mmui::MainWindow"
            or not isinstance(root_info.handle, int) or root_info.handle <= 0
            or not expected_name or type(expected_group) is not bool):
        unverified()
    root_pid, root_handle = root_info.process_id, root_info.handle

    def chain(ctrl):
        result, seen = [], set()
        for _ in range(64):
            key, info = identity(ctrl)
            if key in seen or info.process_id != root_pid:
                unverified()
            seen.add(key)
            result.append(ctrl)
            if key == root_id:
                if info.handle != root_handle:
                    unverified()
                return result
            ctrl = ctrl.parent()
            if ctrl is None:
                unverified()
        unverified()

    def inside(ctrl, parent):
        box, outer = ctrl.rectangle(), parent.rectangle()
        if (box.right <= box.left or box.bottom <= box.top
                or outer.right <= outer.left or outer.bottom <= outer.top
                or box.left < outer.left or box.top < outer.top
                or box.right > outer.right or box.bottom > outer.bottom):
            unverified()

    def page_for(ctrl):
        ancestors = chain(ctrl)
        pages = [node for node in ancestors
                 if node.element_info.class_name == "mmui::ChatMessagePage"]
        if len(pages) != 1 or not pages[0].is_visible():
            unverified()
        page = pages[0]
        inside(page, win)
        inside(ctrl, page)
        return page, ancestors

    page, _ = page_for(composer)
    page_id = identity(page)[0]
    masters = [ctrl for ctrl in page.descendants()
               if ctrl.element_info.class_name == "mmui::ChatTitleBarMasterView"
               and ctrl.is_visible()]
    if len(masters) != 1:
        unverified()
    master = masters[0]
    if identity(page_for(master)[0])[0] != page_id:
        unverified()
    master_id = identity(master)[0]
    titles = [ctrl for ctrl in master.descendants()
              if ctrl.element_info.control_type == "Text"
              and ctrl.element_info.class_name == "mmui::XTextView"
              and ctrl.is_visible() and not ctrl.children()]
    if len(titles) != 1 and not (expected_group and len(titles) == 2):
        unverified()
    title_ids, labels = set(), []
    for title in titles:
        title_page, title_chain = page_for(title)
        title_id = identity(title)[0]
        if (identity(title_page)[0] != page_id or title_id in title_ids
                or sum(identity(node)[0] == master_id for node in title_chain) != 1):
            unverified()
        title_ids.add(title_id)
        inside(title, master)
        labels.append(title.window_text())
    if identity(page_for(target_control)[0])[0] != page_id:
        unverified()
    if identity(win)[0] != root_id or win.element_info.process_id != root_pid:
        unverified()
    if expected_name not in labels:
        raise WaError(NOT_READY, "Current WeChat conversation does not match the recipient",
                      reason="wechat_session_mismatch")
    if labels.count(expected_name) != 1:
        unverified()
    for label in labels:
        if label != expected_name and re.fullmatch(r"(?:\([1-9][0-9]*\)|（[1-9][0-9]*）)", label) is None:
            unverified()


def _unique_wechat_control(win, role: str, *, require_enabled: bool = True,
                           expected_name: str | None = None, expected_group: bool = False):
    """只接受主窗口内唯一可见的实际控件；不能定位时不向猜测的位置输入。"""
    def read():
        controls = win.descendants()

        def unique(wanted: str, enabled: bool):
            kind, class_name = {
                "search": ("Edit", "mmui::XValidatorTextEdit"),
                "composer": ("Edit", "mmui::ChatInputField"),
                "send": ("Button", "mmui::XOutlineButton"),
            }[wanted]
            matches = []
            for ctrl in controls:
                info = ctrl.element_info
                if info.control_type != kind or info.class_name != class_name:
                    continue
                if wanted == "send" and ctrl.window_text().strip() not in {"发送", "Send", "傳送"}:
                    continue
                if ctrl.is_visible():
                    matches.append(ctrl)
            if len(matches) != 1:
                raise WaError(NOT_READY, "微信发送控件不存在或不唯一", reason=f"wechat_{wanted}_control_not_unique")
            ctrl = matches[0]
            box, outer = ctrl.rectangle(), win.rectangle()
            if (box.right <= box.left or box.bottom <= box.top
                    or box.left < outer.left or box.top < outer.top
                    or box.right > outer.right or box.bottom > outer.bottom):
                raise WaError(NOT_READY, "微信发送控件位置无效", reason=f"wechat_{wanted}_control_bounds_invalid")
            if enabled and not ctrl.is_enabled():
                raise WaError(NOT_READY, "微信发送控件不可用", reason=f"wechat_{wanted}_control_disabled")
            return ctrl

        composer = unique("composer", True) if expected_name is not None else None
        ctrl = composer if role == "composer" and composer is not None else unique(role, require_enabled)
        if expected_name is not None:
            _verify_wechat_chat_header(win, composer, ctrl, expected_name, expected_group=expected_group)
        return ctrl
    return _wechat_ui_call(f"{role}_control", read, retry_read=True)


def _click_send_in_open_wechat(hwnd: int, name: str, text: str, *, expected_group: bool = False) -> None:
    """点左侧搜索框，只打开「功能 / 联系人 / 群聊」里的同名项，再把正文贴进输入区并点绿色发送按钮。"""
    import ctypes
    import time
    import win32clipboard
    import win32con
    import win32gui
    from pywinauto import Desktop

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    win = Desktop(backend="uia").window(handle=hwnd)

    def control(role: str, *, enabled: bool = True):
        return _unique_wechat_control(win, role, require_enabled=enabled,
                                      expected_name=name if role != "search" else None,
                                      expected_group=expected_group)

    def foreground():
        return _wechat_ui_call("foreground", lambda: _ensure_wechat_foreground(hwnd))

    def check_foreground():
        if win32gui.GetForegroundWindow() != hwnd:
            raise WaError(NOT_READY, "微信未保持前台，已停止发送", reason="wechat_not_foreground")

    def click(stage: str, locate):
        foreground()
        ctrl = locate()
        if foreground():
            ctrl = locate()
        check_foreground()
        _wechat_ui_call(stage, ctrl.click_input)

    def prepare_input(role: str) -> bool:
        restored = foreground()
        ctrl = control(role)
        restored = foreground() or restored
        if restored:
            # 调回前台后，旧焦点可能已经移到浏览器；只重聚焦当前已验证的输入控件。
            ctrl = control(role)
            check_foreground()
            _wechat_ui_call(f"{role}_refocus", ctrl.click_input)
        check_foreground()
        return restored

    def paste(role: str, value: str) -> None:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(value, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        prepare_input(role)
        user32 = ctypes.windll.user32
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x56, 0, 0, 0)
        user32.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
        user32.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)

    def select_all_and_delete(role: str) -> None:
        user32 = ctypes.windll.user32
        for attempt in range(2):
            prepare_input(role)
            user32.keybd_event(0x11, 0, 0, 0)
            user32.keybd_event(0x41, 0, 0, 0)
            user32.keybd_event(0x41, 0, win32con.KEYEVENTF_KEYUP, 0)
            user32.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.05)
            if not prepare_input(role):
                break
            # 恢复后的聚焦会丢失 Ctrl+A 选择；最多重选一次，绝不重复粘贴。
            if attempt == 1:
                raise WaError(NOT_READY, "微信前台持续变化，已停止发送", reason="wechat_not_foreground")
        user32.keybd_event(0x2E, 0, 0, 0)
        user32.keybd_event(0x2E, 0, win32con.KEYEVENTF_KEYUP, 0)

    click("search_click", lambda: control("search"))
    time.sleep(0.2)
    _wechat_ui_call("search_clear", lambda: select_all_and_delete("search"))
    _wechat_ui_call("search_paste", lambda: paste("search", name))
    time.sleep(0.45)
    entries = _search_popup_entries(win)
    hit = pick_wechat_search_hit([label for _ctrl, label in entries], name)
    if hit is None:
        raise WaError(NOT_READY, "搜索结果里没有唯一的功能、联系人或群，已拒绝点网络结果", reason="wechat_search_not_function")
    click("search_result_click", lambda: entries[hit][0])
    time.sleep(0.45)
    control("composer")
    # 空输入区的发送按钮可以禁用；粘贴后必须重新确认已经启用。
    control("send", enabled=False)
    click("composer_click", lambda: control("composer"))
    time.sleep(0.2)
    _wechat_ui_call("composer_clear", lambda: select_all_and_delete("composer"))
    _wechat_ui_call("composer_paste", lambda: paste("composer", text))
    time.sleep(0.25)
    click("send_click", lambda: control("send"))


def foreground_threads_to_attach(current: int, foreground: int, target: int) -> tuple[int, ...]:
    """SetForegroundWindow 跑在调用线程上。只把前台线程和目标线程互相挂上，系统仍拒绝调用线程。"""
    chosen: list[int] = []
    for thread_id in (foreground, target):
        if thread_id and thread_id != current and thread_id not in chosen:
            chosen.append(thread_id)
    return tuple(chosen)


def _claim_foreground(hwnd: int) -> bool:
    """把窗口调到前台。先让调用线程挂上当前前台线程，系统才允许切换。"""
    import ctypes
    import win32con
    import win32gui
    import win32process

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    fg = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
    current = int(kernel32.GetCurrentThreadId())
    attached = foreground_threads_to_attach(current, fg_thread, target_thread)
    for thread_id in attached:
        user32.AttachThreadInput(current, thread_id, True)
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        for thread_id in reversed(attached):
            user32.AttachThreadInput(current, thread_id, False)
    return win32gui.GetForegroundWindow() == hwnd


def _verified_wechat_window_class(hwnd: int, pid: int, native_class: str, *, defer_hidden_generic: bool = False) -> str:
    """Verify UIA identity; a deferred generic class never proves a main window."""
    try:
        from pywinauto import Desktop

        info = Desktop(backend="uia").window(handle=hwnd).element_info
        if int(info.handle) != hwnd or int(info.process_id) != pid:
            raise ValueError("UIA window identity mismatch")
        kind = info.class_name
        deferred_generic = defer_hidden_generic and native_class == kind == "Qt51514QWindowIcon"
        if kind not in {"mmui::MainWindow", "mmui::LoginWindow", "mmui::XHBoxView"} and not deferred_generic:
            raise ValueError("UIA window class is unknown")
        if native_class != "Qt51514QWindowIcon" and native_class != kind:
            raise ValueError("Win32 and UIA window classes conflict")
        return kind
    except Exception as exc:
        # Identity/read failures and unknown classes cannot be deferred.
        raise WeChatWindowError("wechat_window_unverified", "WeChat window could not be safely identified") from exc


def _visible_window_rows() -> list[tuple[int, str, str]]:
    import ctypes
    from ctypes import wintypes
    import win32gui
    import win32process

    kernel32 = ctypes.windll.kernel32
    rows: list[tuple[int, str, str]] = []

    def exe_of(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
        return ""

    # Classify hidden WeChat candidates as well, so restore never launches a process.
    main_pids: set[int] = set()
    hidden_auxiliary_pids: set[int] = set()
    verified_main_hwnds: set[int] = set()
    visible_main_rows: list[tuple[int, int, str, str]] = []
    deferred_generic_rows: list[tuple[int, int, str, str]] = []

    def unchanged(row: tuple[int, int, str, str], visible: bool) -> bool:
        hwnd, pid, native_class, exe = row
        return (bool(win32gui.IsWindowVisible(hwnd)) == visible
                and win32process.GetWindowThreadProcessId(hwnd)[1] == pid
                and win32gui.GetClassName(hwnd) == native_class
                and exe_of(pid).lower() == exe.lower())

    def visit(hwnd: int, _: object) -> bool:
        visible = bool(win32gui.IsWindowVisible(hwnd))
        kind = win32gui.GetClassName(hwnd) or ""
        candidates = {"mmui::MainWindow", "mmui::LoginWindow", "Qt51514QWindowIcon"}
        if not visible and kind not in candidates:
            return True
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = exe_of(pid)
        wechat = exe.lower() in {"weixin.exe", "wechat.exe"}
        if not visible and not wechat:
            return True
        if wechat and kind in candidates:
            native_row = (int(hwnd), pid, kind, exe)
            verified_kind = _verified_wechat_window_class(int(hwnd), pid, kind,
                                                        defer_hidden_generic=not visible)
            if verified_kind == "mmui::XHBoxView" and visible:
                raise WeChatWindowError("wechat_window_unverified", "Visible WeChat auxiliary window is not verified")
            # A recycled or changed candidate invalidates the entire uniqueness proof.
            if not unchanged(native_row, visible):
                raise WeChatWindowError("wechat_window_unverified", "WeChat window identity changed during verification")
            if verified_kind == "Qt51514QWindowIcon":
                # Not a verified auxiliary: decide only after all main proofs exist.
                deferred_generic_rows.append(native_row)
                return True
            kind = verified_kind
            if kind == "mmui::MainWindow":
                main_pids.add(pid)
                verified_main_hwnds.add(int(hwnd))
                if visible:
                    visible_main_rows.append(native_row)
            elif kind == "mmui::XHBoxView":
                hidden_auxiliary_pids.add(pid)
        rows.append((int(hwnd), kind, exe))
        return True

    win32gui.EnumWindows(visit, None)
    # The one observed auxiliary class is safe only beside a proven main of that PID.
    if hidden_auxiliary_pids - main_pids:
        raise WeChatWindowError("wechat_window_unverified", "Hidden WeChat auxiliary window has no verified main")
    if deferred_generic_rows:
        if len(visible_main_rows) != 1 or len(verified_main_hwnds) != 1:
            raise WeChatWindowError("wechat_window_unverified", "Hidden generic Qt window has no unique visible verified main")
        main = visible_main_rows[0]
        if (not unchanged(main, True)
                or any(row[1] != main[1] or not unchanged(row, False) for row in deferred_generic_rows)):
            raise WeChatWindowError("wechat_window_unverified", "Hidden generic Qt window proof changed or belongs to another process")
    return rows


def wechat_main_hwnds(rows: list[tuple[int, str, str]]) -> list[int]:
    """只留微信进程的主窗口。同名 Qt 窗口若不属于 Weixin.exe / WeChat.exe，不算。登录窗排除。"""
    allowed_exe = {"weixin.exe", "wechat.exe"}
    allowed_class = {"mmui::MainWindow", "Qt51514QWindowIcon"}
    found: list[int] = []
    for hwnd, kind, exe in rows:
        if kind == "mmui::LoginWindow" or kind not in allowed_class:
            continue
        if exe.lower() not in allowed_exe:
            continue
        found.append(int(hwnd))
    return list(dict.fromkeys(found))


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
