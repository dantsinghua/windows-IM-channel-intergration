"""``wechat`` 模块(M3.5 骨架;02 §2.4 / §3.6 #28~#43 / 05 §2.4 / 04 §2.5.2、§2.5.4)。

三块,按**执行体**分开:

- ``WeChatStore``     —— **服务**:``wechat_profiles`` / ``wechat_install`` / ``wechat_version_matrix`` 三张表
  (#30 profiles、#29 version-match 的事实来源)。
- ``WeChatHostsBlock`` —— **服务**:#33b ``POST /wa/v1/wechat/update-block``,B-2 hosts 屏蔽。
  🔴 **R6-15 写法**:**每域名一行** ``0.0.0.0 <域名>  # QTrade-wechat-update-block``,
  **行尾标记是增删/复核的唯一识别依据(按标记逐行匹配,不按域名匹配)**;**不是 BEGIN/END 围栏块**。
  域名只有两个下载 CDN(``dldir1.qq.com``/``dldir1v6.qq.com``);MMTLS 长短连接域名**绝不可进表**——
  写前与 ``[probe] wechat_hosts`` 做**交集检查,相交拒写**。
- ``WeChatSession``   —— **会话代理**:登录会话状态机、讲述人仪式、chatlog 托管与试钥、读写、截图。

🔴 取钥时序(05 §2.4.4a,2026-09-18 实测):两把钥来源不同、且 **chatlog 要求同一次运行内都拿到才落盘**——
只拿到 ``data_key`` 会整轮作废。正确顺序 = **a) 先起 hook(chatlog key)→ b) 引导打开任意图片取 ``img_key``
→ c) 引导退出微信重新登录取 ``data_key``(该轮前 30 秒内)**;超时本轮作废,**循环重装 hook 再来**,
不能一次失败就判 ``KEY_FAIL``。落盘判据 = ``data_key`` 与 ``img_key`` **同时非空**。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .backends import HostsBackend, WeChatBackend
from .config import ProbeConfig, WechatConfig
from .db import Db
from .errors import INVALID_ARGS, NOT_READY, TARGET_NOT_FOUND, WaError
from .ids import login_session_id as new_login_session_id
from .logfmt import get_logger

log = get_logger("wechat")

# 05 §2.4.4a + R3-3:``GET /wa/v1/wechat/login/status`` 的 phase 枚举(**以 05 为准**;旧枚举作废)
PHASES = ("idle", "narrator", "qrcode", "identified", "keytry", "ready", "key_failed")
# 00 §8.1 登录阶段 state_code(05 §2.4.4a 补登两码;🔴 两个独立码、不许合成)
WAIT_NARRATOR = "WAIT_NARRATOR"
WAIT_QRCODE = "WAIT_QRCODE"
WAIT_KEY_IMG = "WAIT_KEY_IMG"              # 等用户打开任意图片取 img_key,窗口约 60s
WAIT_KEY_RELOGIN = "WAIT_KEY_RELOGIN"      # 等用户退出微信重新登录取 data_key,窗口约 30s
KEY_FAIL = "KEY_FAIL"
IMG_KEY_WINDOW_S = 60                      # 05 §2.4.4a 实测:img_key 内存扫描约 60 秒
DATA_KEY_WINDOW_S = 30                     # 🔴 data_key 只在该轮**前 30 秒**内、且必须发生「登录」那一刻
WECHAT_READY_POLLS = 40                    # 拉起微信后等窗口出现的轮数(×0.5 s ≈ 20 s)
WINDOW_SHOW_POLLS = 10                     # 从托盘唤醒主窗口后等它可见的轮数(×0.5 s ≈ 5 s)

# 基线 §8.6 版本匹配
MATCH_SUPPORTED = "SUPPORTED"
MATCH_NEWER = "UNSUPPORTED_NEWER"
MATCH_OLDER = "UNSUPPORTED_OLDER"
MATCH_NOT_INSTALLED = "NOT_INSTALLED"
MATCH_MULTIPLE = "MULTIPLE_INSTALLS"
MATRIX_STATUS = ("verified", "failed", "unknown")
MATRIX_SOURCE = ("bundled", "runtime", "manual")

HOSTS_BLOCK_IP = "0.0.0.0"
H21 = "H21_WECHAT_HOSTS_BLOCK_FAILED"


def _sha256_file(path: str) -> str:
    """随包安装包 250 MB 级,分块算;调用方放线程里。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------- 服务:三张表


class WeChatStore:
    """``wechat_profiles`` / ``wechat_install`` / ``wechat_version_matrix``(02 §3.2;R6-13 安装级版本在 ``wechat_install``)。"""

    def __init__(self, db: Db, *, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._clock = clock

    # ---- #30 GET /wa/v1/wechat/profiles(A;svc)
    def profiles(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wechat_profiles" + ("" if include_deleted else " WHERE deleted_ms IS NULL")
        return self._db.query(sql + " ORDER BY account_id")

    def bind(self, *, wxid: str, account_id: str) -> dict[str, Any]:
        """#33c ``POST /wa/v1/wechat/bind``(R3-2):把取钥读到的 ``wxid`` 绑到 Agent 已建的 ``wxNN``;同 ``wxid`` 重绑幂等。"""
        if not (len(account_id) == 4 and account_id.startswith("wx") and account_id[2:].isdigit()):
            raise WaError(INVALID_ARGS, "account_id 必须形如 wxNN(02 §3.2 CHECK account_id GLOB 'wx[0-9][0-9]')",
                          reason="bad_account_id")
        now = self._clock()
        exist = self._db.one("SELECT * FROM wechat_profiles WHERE wxid=?", (wxid,))
        other = self._db.one("SELECT * FROM wechat_profiles WHERE account_id=? AND wxid<>?", (account_id, wxid))
        if other is not None:
            raise WaError("RESOURCE_EXHAUSTED", f"{account_id} 已绑定另一个 wxid", reason="account_id_taken")
        with self._db.tx() as con:
            if exist is None:
                con.execute("INSERT INTO wechat_profiles(wxid, account_id, login_count, created_ms, updated_ms) "
                            "VALUES (?,?,0,?,?)", (wxid, account_id, now, now))
            else:
                con.execute("UPDATE wechat_profiles SET account_id=?, updated_ms=?, deleted_ms=NULL WHERE wxid=?",
                            (account_id, now, wxid))
        return {"ok": True, "wxid": wxid, "account_id": account_id}

    def record_login(self, *, wxid: str, account_id: str, nickname: Optional[str] = None,
                     wechat_version: Optional[str] = None, wxkey_dll: Optional[str] = None,
                     data_dir: Optional[str] = None) -> None:
        now = self._clock()
        self.bind(wxid=wxid, account_id=account_id)
        with self._db.tx() as con:
            con.execute("UPDATE wechat_profiles SET nickname=COALESCE(?, nickname), last_login_ms=?, "
                        "first_login_ms=COALESCE(first_login_ms, ?), login_count=login_count+1, "
                        "last_wechat_version=COALESCE(?, last_wechat_version), last_wxkey_dll=COALESCE(?, last_wxkey_dll), "
                        "data_dir=COALESCE(?, data_dir), updated_ms=? WHERE wxid=?",
                        (nickname, now, now, wechat_version, wxkey_dll, data_dir, now, wxid))

    def mark_ritual_done(self, wxid: str) -> None:
        with self._db.tx() as con:
            con.execute("UPDATE wechat_profiles SET ritual_done_ms=?, updated_ms=? WHERE wxid=?",
                        (self._clock(), self._clock(), wxid))

    # ---- main_wnd_class(R6-58 (at):02 §3.2 新增列 + 02 §7.2/05 §7 [wechat] main_wnd_class 配置默认)
    def effective_main_wnd_class(self, wxid: Optional[str], *, default: str) -> str:
        """取用顺序:``wechat_profiles.main_wnd_class`` 该 wxid 非 NULL 用本列(实测值)优先,
        否则回落 ``default``(winagent.toml ``[wechat] main_wnd_class``,配置侧默认/兜底)。

        ``wxid`` 为 None(仪式/扫码阶段尚未识别出账号)时直接用 ``default``。
        """
        if wxid:
            row = self._db.one("SELECT main_wnd_class FROM wechat_profiles WHERE wxid=?", (wxid,))
            if row and row.get("main_wnd_class"):
                return row["main_wnd_class"]
        return default

    def wxid_for_account(self, account_id: str) -> Optional[str]:
        row = self._db.one("SELECT wxid FROM wechat_profiles WHERE account_id=? AND deleted_ms IS NULL", (account_id,))
        return row["wxid"] if row else None

    def effective_main_wnd_class_for_account(self, account_id: Optional[str], *, default: str) -> str:
        """②取用顺序的 ``account_id`` 版(``POST /wa/v1/wechat/login/start`` 只有 ``account_id``,还没有 ``wxid``):
        先按 ``account_id`` 找到已绑定的 ``wxid`` 再走 ``effective_main_wnd_class``;全新登录(``account_id`` 为
        None)或查无此账号(尚未 bind 过)时直接用 ``default``。
        """
        return self.effective_main_wnd_class(self.wxid_for_account(account_id) if account_id else None, default=default)

    def record_main_wnd_class(self, wxid: str, class_name: Optional[str]) -> None:
        """把该 wxid 实测到的微信主窗口类名(经 ``WeChatBackend.main_window()`` 探测)写回本列。

        ``class_name`` 落空(未测到/窗口不存在)时不写 —— 不能用「没测到」覆盖已有实测值,也不能把列打回 NULL;
        wxid 尚未经 ``bind()`` 建行时同样静默无操作(``UPDATE`` 零行,与 ``mark_ritual_done`` 同一套写法)。
        """
        if not class_name:
            return
        with self._db.tx() as con:
            con.execute("UPDATE wechat_profiles SET main_wnd_class=?, updated_ms=? WHERE wxid=?",
                        (class_name, self._clock(), wxid))

    # ---- wechat_install(单行)
    def install(self) -> Optional[dict[str, Any]]:
        return self._db.one("SELECT * FROM wechat_install WHERE key='current'")

    def put_install(self, **fields: Any) -> None:
        cols = ("path", "version", "exe_version", "version_subdir", "data_root_ini", "data_root", "data_dir",
                "appdata_dir", "backup_dir", "reinstalled_ms", "auto_update_json", "selected_ms")
        cur = self.install() or {}
        vals = {c: fields.get(c, cur.get(c)) for c in cols}
        vals["auto_update_json"] = vals["auto_update_json"] or "{}"
        with self._db.tx() as con:
            con.execute(
                "INSERT INTO wechat_install(key, path, version, exe_version, version_subdir, data_root_ini, data_root, "
                "data_dir, appdata_dir, backup_dir, reinstalled_ms, auto_update_json, selected_ms, updated_ms) "
                "VALUES ('current',?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "path=excluded.path, version=excluded.version, exe_version=excluded.exe_version, "
                "version_subdir=excluded.version_subdir, data_root_ini=excluded.data_root_ini, "
                "data_root=excluded.data_root, data_dir=excluded.data_dir, appdata_dir=excluded.appdata_dir, "
                "backup_dir=excluded.backup_dir, reinstalled_ms=excluded.reinstalled_ms, "
                "auto_update_json=excluded.auto_update_json, selected_ms=excluded.selected_ms, updated_ms=excluded.updated_ms",
                (*[vals[c] for c in cols], self._clock()))

    # ---- wechat_version_matrix
    def matrix(self, version: Optional[str] = None) -> list[dict[str, Any]]:
        if version:
            row = self._db.one("SELECT * FROM wechat_version_matrix WHERE version=?", (version,))
            return [row] if row else []
        return self._db.query("SELECT * FROM wechat_version_matrix ORDER BY version")

    def put_matrix(self, *, version: str, dll: Optional[str], status: str, source: str, note: Optional[str] = None) -> None:
        if status not in MATRIX_STATUS or source not in MATRIX_SOURCE:
            raise WaError(INVALID_ARGS, "status/source 不在 02 §3.2 的 CHECK 枚举内", reason="bad_enum")
        with self._db.tx() as con:
            con.execute("INSERT INTO wechat_version_matrix(version, dll, status, source, verified_ms, note) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET dll=excluded.dll, status=excluded.status, "
                        "source=excluded.source, verified_ms=excluded.verified_ms, note=excluded.note",
                        (version, dll, status, source, self._clock() if status == "verified" else None, note))

    # ---- #29 GET /wa/v1/wechat/version-match(运行时按 03 §2.9.2 规则算,C-37)
    def version_match(self, *, bundled_version: str, installed: Optional[dict[str, Any]] = None,
                      multiple_installs: bool = False, wxkey_dlls: tuple[str, ...] = ()) -> dict[str, Any]:
        inst = installed if installed is not None else self.install()
        if multiple_installs:
            return {"match": MATCH_MULTIPLE, "action": "choose_path", "current_version": None,
                    "bundled_version": bundled_version, "dll": None, "status": None}
        current = (inst or {}).get("version")
        if not inst or not current:
            return {"match": MATCH_NOT_INSTALLED, "action": "install_bundled", "current_version": None,
                    "bundled_version": bundled_version, "dll": None, "status": None}
        row = (self.matrix(current) or [None])[0]
        dll, status = (row or {}).get("dll"), (row or {}).get("status", "unknown")
        if row and status == "verified":                       # 矩阵有 verified 记录时优先该 DLL(02 §7.2 wxkey_dlls 注)
            return {"match": MATCH_SUPPORTED, "action": "continue", "current_version": current,
                    "bundled_version": bundled_version, "dll": dll, "status": status}
        cmp_ = _cmp_version(current, bundled_version)          # 与随包版本**逐段比较**(03 §2.9.2)
        if cmp_ == 0:
            return {"match": MATCH_SUPPORTED, "action": "continue", "current_version": current,
                    "bundled_version": bundled_version, "dll": dll or (wxkey_dlls[0] if wxkey_dlls else None),
                    "status": status}
        return {"match": MATCH_NEWER if cmp_ > 0 else MATCH_OLDER, "action": "reinstall_bundled",
                "current_version": current, "bundled_version": bundled_version, "dll": dll, "status": status}


def _cmp_version(a: str, b: str) -> int:
    """四段版本逐段比较(不认外层 VersionInfo,只认这里传进来的实值;分发侧以 sha256 为准,05 §2.4.1)。"""
    pa = [int(x) for x in a.split(".") if x.isdigit()]
    pb = [int(x) for x in b.split(".") if x.isdigit()]
    pa += [0] * (4 - len(pa))
    pb += [0] * (4 - len(pb))
    return (pa > pb) - (pa < pb)


# ---------------------------------------------------------------------- 服务:#33b hosts 屏蔽


class WeChatHostsBlock:
    """B-2 hosts 屏蔽(04 §2.5.4 是唯一出处;本类只实现)。"""

    def __init__(self, hosts: HostsBackend, cfg: WechatConfig, probe_cfg: ProbeConfig, *,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._hosts = hosts
        self._cfg = cfg
        self._probe_cfg = probe_cfg
        self._clock = clock

    def _guard_domains(self) -> tuple[str, ...]:
        """🔴 写前与 ``[probe] wechat_hosts`` 做交集检查,**相交拒写**——MMTLS 长短连接域名与收发消息同通道,绝不可拦。"""
        probe_domains = {hp.split(":", 1)[0] for hp in self._probe_cfg.wechat_hosts}
        bad = sorted(set(self._cfg.update_block_domains) & probe_domains)
        if bad:
            raise WaError(INVALID_ARGS,
                          f"拒绝屏蔽 {bad}:它们同时是 [probe] wechat_hosts 的探测目标(MMTLS 长短连接,拦了就收不到消息)",
                          reason="domain_is_mmtls")
        return tuple(self._cfg.update_block_domains)

    def _lines_without_marker(self, text: str) -> list[str]:
        marker = self._cfg.update_block_marker
        return [l for l in text.splitlines() if marker not in l]

    def state(self) -> dict[str, Any]:
        """#28 ``status.hosts_block`` 的内容:``{enabled, domains, applied_at, last_result}``。"""
        text = self._hosts.read()
        marker = self._cfg.update_block_marker
        present = [l.split()[1] for l in text.splitlines() if marker in l and len(l.split()) >= 2]
        return {"enabled": bool(present), "domains": present, "applied_at": None,
                "last_result": "applied" if present else "removed"}

    def apply(self, enable: bool) -> dict[str, Any]:
        """返回 ``{result:'applied'|'removed'|'blocked_by_policy', domains, applied_at}``(02 §3.6 #33b 逐字)。

        hosts 只读 / 被 EDR 或组策略保护 ⇒ ``blocked_by_policy`` + 告警 ``H21_WECHAT_HOSTS_BLOCK_FAILED``
        (**不是失败,降级为告警**,C 层版本守卫兜底)。
        """
        domains = self._guard_domains()
        marker = self._cfg.update_block_marker
        try:
            text = self._hosts.read()
            lines = self._lines_without_marker(text)           # 增删一律**按标记逐行匹配**,不按域名匹配
            if enable:
                lines += [f"{HOSTS_BLOCK_IP} {d}  {marker}" for d in domains]
            new_text = "\n".join(lines) + "\n"
            if new_text != text:
                self._hosts.write(new_text)
        except (PermissionError, OSError) as e:
            log.warning("hosts 写入被拒", extra={"op": "wechat.update_block", "code": "BLOCKED_BY_POLICY"})
            return {"result": "blocked_by_policy", "domains": list(domains), "applied_at": self._clock(),
                    "alert": H21, "reason": repr(e)}
        if enable:                                             # 写后 Resolve-DnsName 应回 0.0.0.0(04 H21 判据)
            unresolved = [d for d in domains if self._hosts.resolve(d) != HOSTS_BLOCK_IP]
            if unresolved:
                return {"result": "blocked_by_policy", "domains": list(domains), "applied_at": self._clock(),
                        "alert": H21, "reason": f"写入后解析仍非 0.0.0.0:{unresolved}"}
        return {"result": "applied" if enable else "removed", "domains": list(domains), "applied_at": self._clock()}


# ---------------------------------------------------------------------- 会话代理:登录会话状态机


@dataclass
class LoginSession:
    """05 §2.4.2 ``login_session_id`` + §2.4.4a 的取钥轮次。"""
    login_session_id: str
    account_id: Optional[str]
    phase: str = "idle"
    state_code: Optional[str] = None
    started_ms: int = 0
    phase_started_ms: int = 0
    narrator_started_ms: Optional[int] = None
    narrator_rounds: int = 0
    narrator_stop_pending: bool = False   # 会话代理停不掉讲述人(高完整性),等服务侧提权结束(#33 响应随带)
    narrator_early_attempt: bool = False  # R6-90:本次关讲述人是「未满 min 的提前探测」(失败不扣轮次)
    narrator_early_tried: bool = False    # R6-90:提前探测已试过一次,之后必须满 min 才关
    key_rounds: int = 0
    wxid: Optional[str] = None
    nickname: Optional[str] = None
    dll: Optional[str] = None
    key_error: Optional[str] = None
    cancelled: bool = False
    events: list[str] = field(default_factory=list)


class WeChatSession:
    """**会话代理**侧的微信控制面(#28/#31~#42)。状态机只推进相位,真正的 UI/进程动作全在 ``WeChatBackend``。"""

    def __init__(self, backend: WeChatBackend, cfg: WechatConfig, *,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._wx = backend
        self._cfg = cfg
        self._clock = clock
        self.session: Optional[LoginSession] = None

    # ---------------------------------------------------------------- 本机安装定位(服务侧 #29 的事实来源)
    def locate(self) -> dict[str, Any]:
        """``{installed, path, version, data_root, …}``(03 §2.9.2 三来源检测由后端做);模块关闭也可查,纯只读。"""
        return dict(self._wx.locate() or {})

    # ---------------------------------------------------------------- #28 status
    def status(self, *, hosts_block: Optional[dict[str, Any]] = None, screen_locked: bool = False) -> dict[str, Any]:
        if not self._cfg.enabled:                              # 模块关闭时 enabled:false 其余 null(#28 逐字)
            return {"enabled": False, "wechat": None, "chatlog": None, "ritual_done": None,
                    "screen_locked": screen_locked, "login_session": None, "hosts_block": hosts_block}
        loc = self._wx.locate()
        win = self._wx.main_window()
        keys = self._wx.key_state()
        cl = self._wx.chatlog_status()
        s = self.session
        wxid = self._wx.current_wxid()                         # 只算一次(内部还要再 locate 一遍)
        # 2026-10-10 真机:微信没在跑时 pywinauto 两次 exists(timeout=2) + comtypes 首次生成 + 两趟 process_iter
        # 把本方法拖过 10s ⇒ 服务侧 #28 恒 TIMEOUT。主窗口都不在,仪式自然没做完,不必再去问 UIA。
        ritual_done = bool(win.get("exists")) and not self._wx.narrator_running() and self._wx.ui_tree_visible()
        return {"enabled": True,
                "wechat": {"installed": loc.get("installed"), "version": loc.get("version"),
                           "running": win.get("exists"), "logged_in": bool(wxid),
                           "wxid": wxid, "nickname": s.nickname if s else None, "pid": win.get("pid"),
                           # R6-58 (at):经 main_window() 探测到的主窗口类名(未识别/窗口不存在为 None);
                           # 落库走 WeChatStore.record_main_wnd_class(该 wxid, 本值)——服务侧持有 DB,本类只探测
                           "main_wnd_class": win.get("class_name")},
                "chatlog": {"running": cl.get("running"), "port": self._cfg.chatlog_port,
                            "key_ok": bool(keys.get("ok")), "dll": cl.get("dll")},
                "ritual_done": ritual_done,
                "screen_locked": screen_locked,
                "login_session": self.login_status() if s else None,
                "hosts_block": hosts_block}

    # ---------------------------------------------------------------- #36 ui-visible
    def ui_visible(self) -> dict[str, Any]:
        w = self._wx.main_window()
        return {"visible": bool(w.get("exists") and w.get("visible")), "minimized": bool(w.get("minimized")),
                "locked": False}

    # ---------------------------------------------------------------- R6-58 (at):经现有协议探测到的主窗口类名
    def detected_main_wnd_class(self) -> Optional[str]:
        """给服务侧调度用的只读探测口(经现有 ``main_window()`` 协议;本类不碰 DB,落库由服务侧
        ``WeChatStore.record_main_wnd_class`` 做)。窗口不存在/未识别时为 None。"""
        return self._wx.main_window().get("class_name")

    # ---------------------------------------------------------------- #31 login/start
    async def login_start(self, *, account_id: Optional[str] = None, login_session_id: Optional[str] = None,
                          main_wnd_class: Optional[str] = None) -> dict[str, Any]:
        """→ ``202 {login_session_id}``。按 05 §2.4.4 ①~⑥ 推进;**取钥按 §2.4.4a 的 a)→b)→c) 顺序**。

        ``main_wnd_class``:R6-58 (at) ②取用顺序落地处 —— 服务侧按 ``account_id`` 算好的「该 wxid 行值 / 配置
        默认」,经管道参数传入;非空时先切到该类名再进入状态机(不传 = 沿用会话代理启动时的配置默认,不切)。
        """
        if not self._cfg.enabled:
            raise WaError(NOT_READY, "微信模块未启用(winagent.toml [wechat] enabled=false)", reason="wechat_disabled")
        if main_wnd_class:
            self._wx.set_main_wnd_class(main_wnd_class)
        now = self._clock()
        s = LoginSession(login_session_id=login_session_id or new_login_session_id(now), account_id=account_id,
                         started_ms=now, phase_started_ms=now)
        self.session = s
        # ② R6-93(2026-10-10 安琳 + 真机):**取钥 / 读消息不依赖讲述人与 UI 树** —— chatlog 取钥是内存 hook + 内存扫描,
        #    读走 chatlog server,都不碰 UI 自动化;UI 树只有**发送**(pyweixin 走界面)才需要。此前把取钥挡在讲述人仪式
        #    之后:真机 4.1.12.26 上讲述人已无法让 UI 树可见(上游 Weixin4.0.md 实录,本机实测 descendants=1),
        #    于是死等 2×5 分钟再判 WAIT_UI_TREE,永远到不了取钥。现在默认(auto)直接起 hook 取钥;
        #    只有显式 narrator_ritual="always"(老版本 / 兜底)才走阻塞仪式。发送前另行检查 UI 树(见 send)。
        if self._cfg.narrator_ritual == "always":
            await self._wx.narrator_start()
            s.narrator_started_ms = self._clock()
            s.narrator_rounds += 1
            self._set_phase(s, "narrator", WAIT_NARRATOR)
        else:
            await self._start_hook_then_qrcode(s)
        return {"login_session_id": s.login_session_id, "phase": s.phase}

    async def _start_hook_then_qrcode(self, s: LoginSession) -> None:
        """🔴 a) **hook 必须先于登录动作装上** —— 被 hook 的函数只在「登录」那一刻被调用(05 §2.4.4a)。

        R6-96(安琳 2026-10-10):覆盖「微信没打开」与「微信在后台托盘」两种情形 —— 微信没进程时**先拉起**
        (停在扫码页,不是登录动作),这样 ``chatlog key`` 能用 ``--pid`` 挂到真正的登录主进程;已在跑(含托盘已登录)
        则不重复拉起,直接挂 hook。拉起微信 ≠ 登录动作,hook 仍先于用户的「扫码 / 退出重登」。"""
        await self._ensure_wechat_running()
        dll = self._next_dll(s)
        await self._wx.chatlog_start(dll)
        s.dll = dll
        s.key_rounds += 1
        await self._wx.launch()
        self._set_phase(s, "qrcode", WAIT_QRCODE)

    async def _ensure_wechat_running(self) -> None:
        """微信没进程(没打开过 / 被关)时先拉起,给取钥提供可挂的登录主进程;已在跑(前台或托盘)直接返回。"""
        if self._wx.wechat_running():
            return
        await self._wx.launch()
        # 刚起的进程还没加载 Weixin.dll,这时挂 DLL 一样「模式匹配失败」⇒ 等窗口(扫码 / 主窗)出来再挂;等不到也继续,
        # 本轮失败会由 keytry 的「key 进程已退出」分支自动重开一轮
        for _ in range(WECHAT_READY_POLLS):
            if self._wx.main_window().get("exists"):
                return
            await asyncio.sleep(0.5)

    async def _serve(self, s: LoginSession) -> None:
        """R6-91:两钥落盘 ⇒ 停 ``key`` 进程、起 ``chatlog server``(:5030)再进 ready。此前取钥成了也没人起 server,
        读消息 / H09 / 发送读回全部失败,账号恒 KEY_FAIL。起不来不假装 ready,判 key_failed 并给出原因。"""
        await self._wx.chatlog_stop()
        try:
            await self._wx.chatlog_serve()
        except Exception as e:                                          # noqa: BLE001 —— 原因透给向导,不吞
            s.key_error = f"已取到密钥,但 chatlog 读服务没起来:{e}"
            self._set_phase(s, "key_failed", KEY_FAIL)
            return
        self._serve_tried_ms = self._clock()
        self._set_phase(s, "ready", None)

    async def ensure_server(self) -> bool:
        """自愈:模块启用 + 两钥在 + 微信已登录 + chatlog 没在跑 ⇒ 拉起 server(30 s 节流)。覆盖开机 / 会话代理重启 /
        chatlog 崩溃三种情形 —— 否则全新机器每次重启后账号都卡在 KEY_FAIL,只能人工重取钥。登录流进行中不插手。"""
        if not self._cfg.enabled:
            return False
        s = self.session
        if s is not None and not s.cancelled and s.phase not in ("ready", "idle", "key_failed"):
            return False
        if self._wx.chatlog_status().get("running") or not self._wx.key_state().get("ok"):
            return False
        if not self._wx.current_wxid():
            return False
        now = self._clock()
        if now - getattr(self, "_serve_tried_ms", -10**12) < 30_000:
            return False
        self._serve_tried_ms = now
        try:
            await self._wx.chatlog_serve()
        except Exception as e:                                          # noqa: BLE001
            log.warning("自愈拉起 chatlog server 失败", extra={"op": "wechat.serve", "code": "SERVE_FAILED", "error": repr(e)})
            return False
        return True

    async def _after_narrator_stopped(self, s: LoginSession) -> None:
        """05 §2.4.3 ④:讲述人已关 ⇒ 重新探可见性;可见进取钥,不可见按轮次上限再做一次或判 WAIT_UI_TREE。

        R6-90:提前探测(未满 min 就关)失败**不扣轮次**——重开讲述人、标记已试过提前,下次必须满 min 才关,
        免得「开着可见 → 关了不可见」在几秒内把 narrator_max_rounds 耗光。"""
        if self._wx.ui_tree_visible():
            await self._start_hook_then_qrcode(s)
        elif s.narrator_early_attempt:
            s.narrator_early_tried, s.narrator_early_attempt = True, False
            await self._wx.narrator_start()
            s.narrator_started_ms = self._clock()                  # 重开后按满 min 重新计时(轮次不变)
        elif s.narrator_rounds >= self._cfg.narrator_max_rounds:
            self._set_phase(s, "key_failed", "WAIT_UI_TREE")
        else:
            await self._wx.narrator_start()
            s.narrator_started_ms = self._clock()
            s.narrator_rounds += 1

    async def _rearm_data_key_hook(self, s: LoginSession, reason: Optional[str]) -> None:
        """重开一轮只为等「退出并重新登录」。同一把 DLL;轮次用尽才 KEY_FAIL,并写明要立刻重登。"""
        if reason and s.key_rounds >= self._cfg.key_retry_per_hour:
            s.key_error = reason + "。请点重新取钥,看到倒计时 30 秒时立刻退出微信并重新登录"
            self._set_phase(s, "key_failed", KEY_FAIL)
            return
        await self._wx.chatlog_stop()
        await self._ensure_wechat_running()
        dll = s.dll or self._next_dll(s)
        await self._wx.chatlog_start(dll)
        s.dll = dll
        if reason:
            s.key_rounds += 1
            s.key_error = reason
        self._set_phase(s, "keytry", WAIT_KEY_RELOGIN)

    async def _next_key_round(self, s: LoginSession, reason: str) -> None:
        """本轮作废:未到 ``key_retry_per_hour`` 就重装 hook(换下一把 DLL)重开一轮,到了判 KEY_FAIL 并留原因。"""
        if s.key_rounds >= self._cfg.key_retry_per_hour:
            s.key_error = reason
            self._set_phase(s, "key_failed", KEY_FAIL)
            return
        await self._wx.chatlog_stop()
        await self._ensure_wechat_running()
        dll = self._next_dll(s)
        await self._wx.chatlog_start(dll)
        s.dll = dll
        s.key_rounds += 1
        s.key_error = reason                           # 上一轮为什么没成,随状态带给向导
        self._set_phase(s, "keytry", WAIT_KEY_IMG)

    async def _ensure_main_window(self) -> None:
        """pyweixin 要操作可见的主窗口:缩在托盘时窗口是隐藏的,UIA 找不到,会被误判成「UI 树不可见」。
        没开就拉起;开着但隐藏 / 最小化就再运行一次 ``Weixin.exe``(单实例,只唤醒已有窗口,同用户双击图标)。"""
        if not self._wx.wechat_running():
            await self._ensure_wechat_running()
            return
        win = self._wx.main_window()
        if win.get("exists") and win.get("visible") and not win.get("minimized"):
            return
        await self._wx.launch()
        for _ in range(WINDOW_SHOW_POLLS):
            win = self._wx.main_window()
            if win.get("exists") and win.get("visible") and not win.get("minimized"):
                return
            await asyncio.sleep(0.5)

    def _next_dll(self, s: LoginSession) -> str:
        dlls = self._cfg.wxkey_dlls or ("wx_key2.dll",)
        return dlls[(s.key_rounds) % len(dlls)]

    def _set_phase(self, s: LoginSession, phase: str, state_code: Optional[str]) -> None:
        assert phase in PHASES, phase
        s.phase, s.state_code, s.phase_started_ms = phase, state_code, self._clock()
        s.events.append(f"{phase}:{state_code or ''}")

    # ---------------------------------------------------------------- 状态机推进(由会话代理的轮询驱动)
    async def poll(self) -> dict[str, Any]:
        """把相位往前推一格。每一步的判据都取自后端的**客观事实**,不靠计时器猜。"""
        s = self.session
        if s is None or s.cancelled:
            return {"phase": "idle"}
        was_ready = s.phase == "ready"
        now = self._clock()
        if s.phase == "narrator":
            # 05 §2.4.3:**目标是「UI 树可见」,讲述人只是手段** —— R6-90(安琳 2026-10-10):每轮都探可见性,
            # 一旦可见立即关讲述人进下一步,不必死等 narrator_min_seconds;该值退化为「可见性迟迟不出现」的保底上限。
            elapsed = (now - (s.narrator_started_ms or now)) / 1000
            if s.narrator_stop_pending:
                # 上一轮会话代理停不掉(高完整性进程),已把 stop_pending 带给服务去提权结束;这里只看它停了没
                if not self._wx.narrator_running():
                    s.narrator_stop_pending = False
                    await self._after_narrator_stopped(s)
            elif self._wx.current_wxid() and (
                    (not s.narrator_early_tried and elapsed >= self._cfg.narrator_probe_min_seconds
                     and self._wx.ui_tree_visible())
                    or elapsed >= self._cfg.narrator_min_seconds):
                # 🔴 讲述人开着时 UI 树本就可见(它正是解屏蔽手段),真判据是「**关掉之后**仍可见」——只能关了再探。
                # 已登录 + 已可见 + 满短驻留 ⇒ 提前试一次关(成了就省掉剩下的等待);没成则重开、本次按满 min 兜底。
                s.narrator_early_attempt = elapsed < self._cfg.narrator_min_seconds
                if await self._wx.narrator_stop() or not self._wx.narrator_running():
                    await self._after_narrator_stopped(s)
                else:
                    s.narrator_stop_pending = True
            elif not self._wx.main_window().get("exists"):
                await self._wx.launch()                        # 仪式期间把登录窗摆出来,扫码由人做(D-2)
        elif s.phase == "qrcode":
            wxid = self._wx.current_wxid()
            if wxid:
                s.wxid = wxid
                # 🔴 identified 是两钥取钥流程的**枢纽相位**:Agent 见此调 #33c bind 把 wxid 绑到 wxNN
                self._set_phase(s, "identified", None)
        elif s.phase == "identified":
            self._set_phase(s, "keytry", WAIT_KEY_IMG)         # b) 先引导打开任意图片取 img_key
        elif s.phase == "keytry":
            keys = self._wx.key_state()
            running = self._wx.chatlog_status().get("running")
            if keys.get("ok"):
                await self._serve(s)
            elif s.state_code == WAIT_KEY_RELOGIN and (not running or (now - s.phase_started_ms) / 1000 > DATA_KEY_WINDOW_S):
                err = keys.get("error") or ""
                if "模式匹配" in err or "未登录的微信" in err:
                    await self._next_key_round(s, err)
                else:
                    # 30 秒到了还没重登:同一把 DLL 再开一轮。数据库钥只在这 30 秒里的登录动作上产生。
                    await self._rearm_data_key_hook(s, err or "这 30 秒里没有重新登录,数据库密钥没取到")
            elif not running:
                # key 进程已退出(DLL 没挂上 / 本轮扫描到点)而两钥没落盘。停在「请打开图片」会让用户白开。
                await self._next_key_round(s, keys.get("error") or "本轮取钥已结束,两把钥未在同一轮内同时取到")
            elif keys.get("img_key") and s.state_code == WAIT_KEY_IMG:
                # 图片钥已在手。前面的扫描已经把 30 秒花掉了,重新装 hook,倒计时从「请立刻退出重登」出现时再算。
                await self._rearm_data_key_hook(s, None)
        out = self.login_status()
        # R6-58 (au) 跟进:record_login 只应在「本轮取钥刚成功、相位刚进 ready」那一次触发,不能每次轮询都记一遍
        # login_count(轮询是高频的);服务侧看这个一次性标记决定要不要调 WeChatStore.record_login(05 §2.4.4 ⑤⑥)
        out["just_became_ready"] = bool(s.phase == "ready" and not was_ready)
        if out["just_became_ready"]:
            out["wechat_version"] = self._wx.locate().get("version")
        return out

    # ---------------------------------------------------------------- #33 login/status
    def login_status(self) -> dict[str, Any]:
        s = self.session
        if s is None:
            return {"phase": "idle"}
        keys = self._wx.key_state()
        countdown = None
        if s.state_code == WAIT_KEY_IMG:
            countdown = max(0, IMG_KEY_WINDOW_S - int((self._clock() - s.phase_started_ms) / 1000))
        elif s.state_code == WAIT_KEY_RELOGIN:
            countdown = max(0, DATA_KEY_WINDOW_S - int((self._clock() - s.phase_started_ms) / 1000))
        narrator_remaining = None
        if s.narrator_started_ms is not None:
            narrator_remaining = max(0, self._cfg.narrator_min_seconds - int((self._clock() - s.narrator_started_ms) / 1000))
        return {"login_session_id": s.login_session_id, "phase": s.phase, "state_code": s.state_code,
                "wxid": s.wxid, "account_id": s.account_id, "nickname": s.nickname, "countdown_s": countdown,
                "narrator": {"state": "running" if self._wx.narrator_running() else "stopped",
                             "remaining_s": narrator_remaining, "rounds": s.narrator_rounds,
                             "stop_pending": s.narrator_stop_pending},       # 服务侧见此即提权结束讲述人
                "key": {"ok": bool(keys.get("ok")), "dll": s.dll, "error": s.key_error,
                        "data_key": bool(keys.get("data_key")), "img_key": bool(keys.get("img_key")),
                        "rounds": s.key_rounds,
                        # 02 #33 R6-58 (ak):两个取钥阶段的**唯一辨别手段**;此前缺这个键,Agent 恒判 img,永远不提示「退出重登」
                        "stage": "relogin" if s.state_code == WAIT_KEY_RELOGIN else "img"}}

    # ---------------------------------------------------------------- #32 login/cancel
    async def login_cancel(self, *, login_session_id: Optional[str] = None) -> dict[str, Any]:
        """取消登录会话(扫码超时/用户放弃);**微信已登进去的不登出**(#32 逐字)。

        ``login_session_id`` 可选:不带 = 取消「当前这次」;带 = **只取消指定的那一次**——
        用户点取消与后台自动重开新尝试可能撞车,不带 id 会误杀刚重开的那次(00 §10 N-14)。
        """
        s = self.session
        if s is None:
            return {"cancelled": False, "reason": "no_active_session"}
        if login_session_id and login_session_id != s.login_session_id:
            return {"cancelled": False, "reason": "stale_login_session_id", "current": s.login_session_id}
        s.cancelled = True
        stop_pending = False
        if self._wx.narrator_running():
            stop_pending = not await self._wx.narrator_stop() and self._wx.narrator_running()
        self._set_phase(s, "idle", None)
        return {"cancelled": True, "login_session_id": s.login_session_id, "narrator_stop_pending": stop_pending}

    # ---------------------------------------------------------------- #34 logout
    async def logout(self) -> dict[str, Any]:
        """D-1:微信登出 = **结束进程**(先 ``WM_CLOSE``,``process_close_grace_s`` 未退再 ``taskkill /F``);``ui`` 模式失败自动回落。

        切换时 chatlog 必须**先停**(它的 DLL 注入在 Weixin.exe 里,05 §2.4.5 第 2 步)。
        """
        await self._wx.chatlog_stop()
        mode = await self._wx.logout(mode=self._cfg.logout_mode, grace_s=self._cfg.process_close_grace_s)
        self.session = None
        return {"logged_out": True, "mode": mode}

    # ---------------------------------------------------------------- #35 key/retry
    async def key_retry(self) -> dict[str, Any]:
        """手动重试取钥 → ``{ok, dll, error}``。已登录场景直接 a)→b)→c),c) 用「退出重登」触发(05 §2.4.4a)。"""
        s = self.session or LoginSession(login_session_id=new_login_session_id(self._clock()), account_id=None,
                                         started_ms=self._clock(), phase_started_ms=self._clock())
        # R6-91 幂等:本轮取钥正在进行(keytry + key 进程在跑)⇒ 不拆掉重来。2026-10-10 真机:Agent 巡检每 10 s 调一次
        # key/retry,每次都 stop+start,把用户正在「打开图片 / 重登」的那一轮直接杀掉。
        if s.phase == "keytry" and not s.cancelled and self._wx.chatlog_status().get("running"):
            keys = self._wx.key_state()
            return {"ok": bool(keys.get("ok")), "dll": s.dll, "error": keys.get("error"), "in_progress": True}
        self.session = s
        await self._wx.chatlog_stop()
        await self._ensure_wechat_running()          # 微信没开(托盘被关 / 从没登过)时先拉起,否则 hook 没进程可挂
        dll = self._next_dll(s)
        await self._wx.chatlog_start(dll)
        s.dll = dll
        s.key_rounds += 1
        s.key_error = None
        await self._ensure_main_window()             # 用户要在微信里开图片 / 重登:缩在托盘时把主窗口唤出来
        self._set_phase(s, "keytry", WAIT_KEY_IMG)
        keys = self._wx.key_state()
        return {"ok": bool(keys.get("ok")), "dll": dll, "error": keys.get("error")}

    # ---------------------------------------------------------------- #38 send / #39 read / #40 media / #41 sessions / #42 screenshot
    def _require_key(self) -> None:
        """05 §2.4.7:``degraded(KEY_FAIL)`` 下**读与写都不开放** —— 写类直接 ``NOT_READY``,逐字的中文理由。"""
        if not self._wx.key_state().get("ok"):
            raise WaError(NOT_READY, "微信解密不可用,无法确认送达,已拒绝发送", reason="key_fail")

    async def send(self, *, session_name: str, text: Optional[str] = None, image_path: Optional[str] = None,
                   idempotency_key: Optional[str] = None, confirm_timeout_ms: Optional[int] = None) -> dict[str, Any]:
        """#38:pyweixin 写 + 临时加速 chatlog 轮询读回;``{ok, code:'DELIVERED|SEND_FAILED', ext_msg_id, confirm_ms}``;**不重试**。"""
        if not text and not image_path:
            raise WaError(INVALID_ARGS, "text 与 image_path 至少给一个", reason="empty_payload")
        self._require_key()
        await self._ensure_main_window()
        # 图片/文件仍要控件树。纯文本在树不可见时改走快捷键(Ctrl+F 搜索、Alt+S 发送),不依赖讲述人。
        if image_path and not await asyncio.to_thread(self._wx.ui_tree_visible):
            raise WaError(NOT_READY,
                          "微信界面控件树不可见,图片发送不可用;文字发送改走快捷键,读消息与取钥不受影响",
                          reason="ui_tree_invisible")
        out = await self._wx.send(session_name=session_name, text=text, image_path=image_path,
                                  confirm_timeout_ms=confirm_timeout_ms or self._cfg.confirm_timeout_ms)
        return {"ok": bool(out.get("ok")), "code": out.get("code"), "ext_msg_id": out.get("ext_msg_id"),
                "confirm_ms": out.get("confirm_ms"), "idempotency_key": idempotency_key}

    async def read(self, *, talker: Optional[str] = None, since_seq: Optional[int] = None,
                   limit: Optional[int] = None) -> dict[str, Any]:
        """#39:取窗 ``cursor − lookback_overlap_s`` + ``normalize`` → 增量 ``Message[]``(``ext_msg_id="{talker}:{seq}"``)。"""
        self._require_key()
        rows = await self._wx.read_messages(talker=talker, since_seq=since_seq, limit=limit or self._cfg.page_limit)
        msgs = [{**r, "ext_msg_id": f"{r.get('talker')}:{r.get('seq')}"} for r in rows]
        return {"messages": msgs, "next_seq": msgs[-1]["seq"] if msgs else since_seq,
                "lookback_overlap_s": self._cfg.lookback_overlap_s}

    async def media(self, key: str) -> bytes:
        """#40:代理 chatlog ``/image/<key>``(同源限制与 20MB 上限由后端实现负责)。"""
        try:
            return await self._wx.media(key)
        except FileNotFoundError as e:
            raise WaError(TARGET_NOT_FOUND, f"媒体 {key} 不存在", reason="media_not_found") from e

    async def sessions(self, *, keyword: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        return await self._wx.list_sessions(keyword=keyword, limit=limit)

    async def screenshot(self, *, screen_locked: bool = False) -> bytes:
        """#42:微信主窗口截图 PNG;**锁屏时 ``NOT_READY``**(锁屏后是安全桌面,UI 自动化触达不到)。"""
        if screen_locked:
            raise WaError(NOT_READY, "Windows 已锁屏,无法截取微信窗口", reason="screen_locked")
        return await self._wx.screenshot()

    # ---------------------------------------------------------------- #37 reinstall(管道半)
    async def reinstall(self, *, installer: Optional[str] = None) -> dict[str, Any]:
        """#37 的 ``user`` 半 = UI 引导;备份/安装的提权半在服务。**每步用户确认**(00 §11.8 [WXVER]),
        卸载**不走静默**(B-3:NSIS ``/S`` = 连聊天数据一起删)。

        拉起前两道门(R6-87),任何一道不过都是**能照着做的 404**,不拉起任何进程:
        ① 文件在位 —— 缺 = 安装器没落包 / 被清理,提示重跑安装程序修复或在 toml 指路径;
        ② sha256 = 钉死值(R2-6:另一候选包外层版本号完全相同、装出来却是 4.1.12.55,只能靠 sha256 分辨)。
        调用方显式传 ``installer`` 时同样校验 —— 传进来的也必须是那一个包。"""
        path = os.path.expandvars(installer or self._cfg.bundled_installer or "")
        if not path or not os.path.isfile(path):
            raise WaError(TARGET_NOT_FOUND,
                          f"随包微信安装包不在位:{path or '(未配置)'};请重新运行 QTrade 安装程序修复,"
                          f"或在 winagent.toml [wechat] bundled_installer 指定 weixin_{self._cfg.bundled_version}.exe 的路径",
                          reason="bundled_installer_missing")
        want = (self._cfg.bundled_sha256 or "").strip().lower()
        if want:
            got = await asyncio.to_thread(_sha256_file, path)
            if got != want:
                raise WaError(TARGET_NOT_FOUND,
                              f"随包微信安装包校验不符:{path} sha256={got[:12]}…,应为 {want[:12]}…(钉死的 "
                              f"weixin_{self._cfg.bundled_version}.exe);文件可能被替换或损坏,请重新运行 QTrade 安装程序修复",
                              reason="bundled_installer_sha_mismatch")
        return await self._wx.reinstall(path)

    def warmup(self) -> None:
        """会话代理上线/模块启用时在线程里预热后端(pywinauto/comtypes 首次导入在全新机器上可达十几秒,
        不预热就会让第一次 #28 `status` 撞 10s 超时)。失败只记日志,不影响任何功能。"""
        fn = getattr(self._wx, "warmup", None)
        if callable(fn):
            try:
                fn()
            except Exception as e:                                   # noqa: BLE001
                log.warning("微信后端预热失败(忽略)", extra={"op": "wechat.warmup", "code": "WARMUP_FAILED", "error": repr(e)})
