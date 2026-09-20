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
        return {"enabled": True,
                "wechat": {"installed": loc.get("installed"), "version": loc.get("version"),
                           "running": win.get("exists"), "logged_in": bool(self._wx.current_wxid()),
                           "wxid": self._wx.current_wxid(), "nickname": s.nickname if s else None, "pid": win.get("pid")},
                "chatlog": {"running": cl.get("running"), "port": self._cfg.chatlog_port,
                            "key_ok": bool(keys.get("ok")), "dll": cl.get("dll")},
                "ritual_done": not self._wx.narrator_running() and self._wx.ui_tree_visible(),
                "screen_locked": screen_locked,
                "login_session": self.login_status() if s else None,
                "hosts_block": hosts_block}

    # ---------------------------------------------------------------- #36 ui-visible
    def ui_visible(self) -> dict[str, Any]:
        w = self._wx.main_window()
        return {"visible": bool(w.get("exists") and w.get("visible")), "minimized": bool(w.get("minimized")),
                "locked": False}

    # ---------------------------------------------------------------- #31 login/start
    async def login_start(self, *, account_id: Optional[str] = None,
                          login_session_id: Optional[str] = None) -> dict[str, Any]:
        """→ ``202 {login_session_id}``。按 05 §2.4.4 ①~⑥ 推进;**取钥按 §2.4.4a 的 a)→b)→c) 顺序**。"""
        if not self._cfg.enabled:
            raise WaError(NOT_READY, "微信模块未启用(winagent.toml [wechat] enabled=false)", reason="wechat_disabled")
        now = self._clock()
        s = LoginSession(login_session_id=login_session_id or new_login_session_id(now), account_id=account_id,
                         started_ms=now, phase_started_ms=now)
        self.session = s
        # ② 仪式判定:ui_tree_visible() 为真直接跳过(05 §2.4.3「目标是 UI 树可见,讲述人只是手段」)
        if self._cfg.narrator_ritual == "always" or (self._cfg.narrator_ritual == "auto" and not self._wx.ui_tree_visible()):
            await self._wx.narrator_start()
            s.narrator_started_ms = self._clock()
            s.narrator_rounds += 1
            self._set_phase(s, "narrator", WAIT_NARRATOR)
        else:
            await self._start_hook_then_qrcode(s)
        return {"login_session_id": s.login_session_id, "phase": s.phase}

    async def _start_hook_then_qrcode(self, s: LoginSession) -> None:
        """🔴 a) **hook 必须先于登录动作装上** —— 被 hook 的函数只在「登录」那一刻被调用(05 §2.4.4a)。"""
        dll = self._next_dll(s)
        await self._wx.chatlog_start(dll)
        s.dll = dll
        s.key_rounds += 1
        await self._wx.launch()
        self._set_phase(s, "qrcode", WAIT_QRCODE)

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
        now = self._clock()
        if s.phase == "narrator":
            # 自讲述人启动起满 narrator_min_seconds 且微信已登录 ⇒ 关讲述人 → 重新探可见性(05 §2.4.3 ④)
            elapsed = (now - (s.narrator_started_ms or now)) / 1000
            if elapsed >= self._cfg.narrator_min_seconds and self._wx.current_wxid():
                await self._wx.narrator_stop()
                if self._wx.ui_tree_visible():
                    await self._start_hook_then_qrcode(s)
                elif s.narrator_rounds >= self._cfg.narrator_max_rounds:
                    self._set_phase(s, "key_failed", "WAIT_UI_TREE")
                else:
                    await self._wx.narrator_start()
                    s.narrator_started_ms = self._clock()
                    s.narrator_rounds += 1
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
            if keys.get("ok"):
                self._set_phase(s, "ready", None)
            elif keys.get("img_key") and s.state_code == WAIT_KEY_IMG:
                self._set_phase(s, "keytry", WAIT_KEY_RELOGIN)  # c) 再引导退出重登取 data_key(该轮前 30s 内)
            elif s.state_code == WAIT_KEY_RELOGIN and (now - s.phase_started_ms) / 1000 > DATA_KEY_WINDOW_S:
                # 超时本轮作废 ⇒ **循环重装 hook 再来**,不能一次失败就判 KEY_FAIL(05 §2.4.4a)
                if s.key_rounds >= self._cfg.key_retry_per_hour:
                    s.key_error = keys.get("error") or "两把钥未在同一轮内同时取到"
                    self._set_phase(s, "key_failed", KEY_FAIL)
                else:
                    await self._wx.chatlog_stop()
                    dll = self._next_dll(s)
                    await self._wx.chatlog_start(dll)
                    s.dll = dll
                    s.key_rounds += 1
                    self._set_phase(s, "keytry", WAIT_KEY_IMG)
        return self.login_status()

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
                             "remaining_s": narrator_remaining, "rounds": s.narrator_rounds},
                "key": {"ok": bool(keys.get("ok")), "dll": s.dll, "error": s.key_error,
                        "data_key": bool(keys.get("data_key")), "img_key": bool(keys.get("img_key")),
                        "rounds": s.key_rounds}}

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
        if self._wx.narrator_running():
            await self._wx.narrator_stop()
        self._set_phase(s, "idle", None)
        return {"cancelled": True, "login_session_id": s.login_session_id}

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
        self.session = s
        await self._wx.chatlog_stop()
        dll = self._next_dll(s)
        await self._wx.chatlog_start(dll)
        s.dll = dll
        s.key_rounds += 1
        s.key_error = None
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
        卸载**不走静默**(B-3:NSIS ``/S`` = 连聊天数据一起删)。"""
        return await self._wx.reinstall(installer or self._cfg.bundled_installer)
