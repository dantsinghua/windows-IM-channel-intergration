"""企点 UI 执行层(登录 05 §2.1.1 ⑥~⑪ / 发送 06 §2.9.5 + 02 §2.2.3 qidian 行)。

`QidianAdapter` 的 `sender` 与 `AccountService` 的 `login_fn` 在本模块落地。**全部动作经注入的
``AdbBackend`` 执行**(不直接起 subprocess、不碰 docker),开发容器里注 ``runtime.FakeAdb``。

🔴 三条不可让步的口径:
1. **发送前对象校验**(00 §11.3 [GATE] / 02 §2.2.3 「发后对象校验」的前半):进了聊天页还要核对
   「这确实是目标会话」才允许打字与点发送 —— 参考实现 ``echo_loop_maindb.py::send()`` 只验「前台是企点」
   就点固定坐标,那是**会发错会话**的写法,本模块不照抄。校验不过 ⇒ 不发、回失败原因。
2. **点完即返回**(R6-38):点了发送键就返回并让出账号串行队列,**不在这里等读回** ——
   确认是 ``poll`` 的 ingest 合并,由 ``bus`` 在队列外等。
3. **不盲点**:坐标一律由控件树(``uiautomator dump`` 的 ``bounds``)算出;控件找不到就失败,
   **不退回魔法坐标**(profile 可显式开 ``coord_fallback.enabled``,缺省关,见 ``profiles/default.yaml``)。

定位表按 APK 版本成 profile(02 §2.2.3 G-15/P-36):``profiles/<apk_version>.yaml`` + ``default.yaml``,
选择 = 精确命中 → 同主次版本里最高的低版本(``QIDIAN_PROFILE_FALLBACK`` info)→ ``default.yaml``
(账号应置 ``degraded(UI_UNEXPECTED)``)。坏文件**拒绝加载但不拒绝启动**。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ...alerts import QIDIAN_PROFILE_FALLBACK
from ...text import has_control_chars
from ...workflow.yaml_min import YamlError, safe_load
from ..base import Account

log = logging.getLogger("qtrade.qidian.ui")

def profiles_root():
    """profile 数据文件的根,按**包内资源**定位(``importlib.resources``)。

    🔴 不用源码树相对路径:装成 wheel 后 ``__file__`` 旁边未必有 ``profiles/`` —— 数据文件要靠
    ``pyproject.toml`` 的 ``package-data`` 打进去,定位也必须走包资源(``tests/test_packaging.py`` 守这条)。
    """
    return resources.files(__package__).joinpath("profiles")

#: 02 §2.2.3 的 profile 回退告警。**码已登记**(02 §3.7:`info`、subject `account:<id>`),码常量与
#: severity 的唯一出处 = ``alerts.REGISTERED``;本模块只引用(再导出是为了不改既有 import 点)。

ADB_PORT_BASE = 16000                       # 00 §3 段基址(runtime.PORT_BASE['adb'] 同值,此处只读不改)
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)")
_TOP_TEXT_RE = re.compile(r'\btext=(?:"([^"]*)"|(\S+))')
_SEQ_RE = re.compile(r"(\d+)$")


# ---------------------------------------------------------------------------- 控件树
@dataclass(frozen=True)
class Node:
    """``uiautomator dump`` 的一个节点;``center`` 即本模块唯一的坐标来源。"""
    rid: str
    text: str
    desc: str
    cls: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    order: int

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


def parse_ui_xml(raw: str) -> list[Node]:
    """从 ``uiautomator dump`` 的原始输出里切出 ``<hierarchy>`` 并解析成节点表。

    设备把「UI hierchary dumped to: …」之类的尾巴混在同一股 stdout 里,故先按标签切。
    解析不出 / 没有 ``<hierarchy>`` ⇒ ``ValueError``(调用方按「控件树不可得」处理,不盲点)。
    """
    start = raw.find("<hierarchy")
    end = raw.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise ValueError("输出里没有 <hierarchy>(uiautomator dump 失败或被动画卡住)")
    root = ET.fromstring(raw[start:end + len("</hierarchy>")])
    out: list[Node] = []
    for i, el in enumerate(root.iter("node")):
        m = _BOUNDS_RE.match(el.get("bounds", ""))
        if not m:
            continue
        out.append(Node(rid=el.get("resource-id", ""), text=el.get("text", ""), desc=el.get("content-desc", ""),
                        cls=el.get("class", ""), bounds=(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))),
                        clickable=el.get("clickable") == "true", order=i))
    return out


# ---------------------------------------------------------------------------- profile
class ProfileError(ValueError):
    """profile 文件不合 schema(坏文件拒绝加载,不拒绝启动)。"""


@dataclass(frozen=True)
class Profile:
    """一份定位表。``nodes`` 的每个锚点 = ``{id?, text_any?, desc_any?}``,三者取或。"""
    version: str
    package: str
    splash_activity: str
    ime: str
    activities: dict[str, str]
    nodes: dict[str, dict[str, Any]]
    markers: dict[str, list[str]]
    timeouts: dict[str, float]
    coord_fallback: dict[str, Any] = field(default_factory=dict)
    verify_input: bool = True
    title_sources: tuple[str, ...] = ("uiautomator", "dumpsys_top")

    @property
    def version_tuple(self) -> tuple[int, ...]:
        return version_tuple(self.version)

    def anchor(self, name: str) -> dict[str, Any]:
        a = self.nodes.get(name)
        if not a:
            raise ProfileError(f"profile {self.version} 缺锚点 {name}")
        return a

    def timeout(self, name: str, default: float) -> float:
        v = self.timeouts.get(name)
        return float(v) if isinstance(v, (int, float)) else default

    def coord(self, name: str) -> Optional[tuple[int, int]]:
        """坐标兜底:只有 profile 显式 ``coord_fallback.enabled: true`` 才给值,否则恒 ``None``。"""
        if not self.coord_fallback.get("enabled"):
            return None
        v = (self.coord_fallback.get("taps") or {}).get(name)
        if isinstance(v, str):
            parts = v.split()
            if len(parts) == 2 and all(p.lstrip("-").isdigit() for p in parts):
                return int(parts[0]), int(parts[1])
        return None


_REQUIRED_NODES = ("agree_button", "relogin_button", "login_account", "login_password", "login_submit",
                   "main_marker", "search_entry", "search_result_first", "card_send_msg", "card_identity",
                   "chat_title", "chat_input", "chat_send")


def version_tuple(v: str) -> tuple[int, ...]:
    """``"1.2.3.4"`` → ``(1,2,3,4)``;非数字段按 0(比较只用于挑 profile,不参与别处)。"""
    out = []
    for part in (v or "").split("."):
        m = _SEQ_RE.search(part)
        out.append(int(m.group(1)) if m else 0)
    return tuple(out)


def parse_profile(text: str, *, version: str) -> Profile:
    d = safe_load(text)
    if not isinstance(d, dict):
        raise ProfileError("profile 顶层必须是映射")
    if int(d.get("schema") or 0) != 1:
        raise ProfileError(f"profile schema 版本不支持:{d.get('schema')!r}(本版只认 1)")
    nodes = d.get("nodes")
    if not isinstance(nodes, dict):
        raise ProfileError("profile 缺 nodes 段")
    missing = [k for k in _REQUIRED_NODES if not isinstance(nodes.get(k), dict)]
    if missing:
        raise ProfileError(f"profile 缺锚点 {missing}")
    for k, v in nodes.items():
        if not ({"id", "text_any", "desc_any"} & set(v)):
            raise ProfileError(f"锚点 {k} 必须至少给 id / text_any / desc_any 之一")
    acts = d.get("activities") or {}
    if not isinstance(acts, dict) or not acts.get("chat"):
        raise ProfileError("profile 的 activities 段缺 chat(聊天页 Activity 判据)")
    return Profile(version=str(d.get("apk_version") or version), package=str(d.get("package") or "com.tencent.qidian"),
                   splash_activity=str(d.get("splash_activity") or ""), ime=str(d.get("ime") or ""),
                   activities={str(k): str(v) for k, v in acts.items()}, nodes=nodes,
                   markers={str(k): [str(x) for x in (v or [])] for k, v in (d.get("markers") or {}).items()},
                   timeouts={str(k): float(v) for k, v in (d.get("timeouts") or {}).items() if isinstance(v, (int, float))},
                   coord_fallback=d.get("coord_fallback") or {}, verify_input=bool(d.get("verify_input", True)),
                   title_sources=tuple(str(x) for x in (d.get("title_sources") or ("uiautomator", "dumpsys_top"))))


def load_profiles(directory: Any = None) -> dict[str, Profile]:
    """加载目录下全部 ``*.yaml``;**坏文件只记 error 并跳过**(02 §2.2.3:不拒绝启动)。

    ``directory`` 缺省 = 包内资源目录;给字符串/``Path``(测试用)时按目录读。两种入参都只用
    ``iterdir`` / ``name`` / ``read_text`` 这三件 ``Traversable`` 与 ``Path`` 共有的能力。
    """
    out: dict[str, Profile] = {}
    root = profiles_root() if directory is None else (Path(directory) if isinstance(directory, str) else directory)
    try:
        entries = sorted(root.iterdir(), key=lambda e: e.name)
    except (OSError, ModuleNotFoundError) as e:
        log.error("企点 profile 目录不可读 %s:%s", root, e)
        return out
    for entry in entries:
        if not entry.name.endswith(".yaml"):
            continue
        version = entry.name[:-len(".yaml")]
        try:
            out[version] = parse_profile(entry.read_text(encoding="utf-8"), version=version)
        except (OSError, YamlError, ProfileError, ValueError) as e:
            log.error("企点 profile %s 加载失败,已跳过:%s", entry.name, e)
    return out


def pick_profile(profiles: dict[str, Profile], app_version: Optional[str]) -> tuple[Optional[Profile], str]:
    """02 §2.2.3 选择规则。返回 ``(profile, how)``,``how ∈ {exact, fallback, default, none}``。"""
    if app_version and app_version in profiles:
        return profiles[app_version], "exact"
    if app_version:
        want = version_tuple(app_version)
        cands = [p for k, p in profiles.items()
                 if k != "default" and p.version_tuple[:2] == want[:2] and p.version_tuple <= want]
        if cands:
            return max(cands, key=lambda p: p.version_tuple), "fallback"
    d = profiles.get("default")
    return (d, "default") if d is not None else (None, "none")


# ---------------------------------------------------------------------------- 结果
@dataclass(frozen=True)
class LoginOutcome:
    """``result`` 逐字对齐 ``accounts.LoginFn`` 的判定分支(05 §2.1.1 ⑪)。

    ``running`` / ``bad_credential`` / ``WAIT_SMS|WAIT_CAPTCHA|WAIT_DEVICE_CONFIRM|WAIT_PASSWORD`` /
    ``None``(未判定 ⇒ ``AccountService`` 回 ``login_required(WAIT_PASSWORD)`` 等人)。
    """
    result: Optional[str]
    reason: str = ""
    self_uid: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendOutcome:
    ok: bool
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


def serial_of(account_id: str) -> str:
    """``qdNN`` → ``127.0.0.1:160NN``(02 §2.2.4 端口按序号推导,不查表)。"""
    m = _SEQ_RE.search(account_id or "")
    if not m:
        raise ValueError(f"账号 id {account_id!r} 里没有序号,推不出 adb serial")
    return f"127.0.0.1:{ADB_PORT_BASE + int(m.group(1))}"


def _postlogin_activity_visible(raw: str, component: str, p: Profile) -> bool:
    """真机结构判据：前台 Activity 内，同一可见 QQTabHost 下的会话列表、标题和 tabs。

    只解析类名、资源 ID、可见 flags/层级，不从“消息”等文本推断登录，也不跨 Activity 拼节点。
    """
    headers = list(re.finditer(r"(?m)^[ \t]*ACTIVITY[ \t]+(\S+)[^\n]*$", raw))
    required = {
        ("android.widget.RelativeLayout", "app:id/conversation_activity_title"): "title",
        ("com.tencent.widget.OlympicListView", "app:id/recent_chat_list"): "list",
        ("com.tencent.mobileqq.widget.QQTabWidget", "android:id/tabs"): "tabs",
    }
    blocked_ids = {str(p.anchor(key).get("id", "")).replace(p.package + ":id/", "app:id/")
                   for key in ("agree_button", "login_account", "login_password", "relogin_button")}
    blocked_ids.discard("")
    for index, header in enumerate(headers):
        if header[1] != component:
            continue
        section = raw[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(raw)]
        if re.search(r"\bmResumed=false\b|\bmStopped=true\b|\bmFinished=true\b", section):
            continue
        hierarchy_indent: Optional[int] = None
        ancestors: list[tuple[int, bool, Optional[set[str]]]] = []
        hosts: list[set[str]] = []
        login_visible = False
        for line in section.splitlines():
            indent = len(line) - len(line.lstrip())
            if line.strip() == "View Hierarchy:":
                hierarchy_indent = indent
                ancestors = []
                continue
            if hierarchy_indent is None:
                continue
            if line.strip() and indent <= hierarchy_indent:
                hierarchy_indent = None
                ancestors = []
                continue
            node = re.match(r"\s*([A-Za-z0-9_.$]+)\{\S+\s+([VIG][A-Za-z.]{8})\s", line)
            if node is None:
                continue
            while ancestors and ancestors[-1][0] >= indent:
                ancestors.pop()
            visible = node[2][0] == "V" and (not ancestors or ancestors[-1][1])
            host = ancestors[-1][2] if ancestors and visible else None
            ids = set(re.findall(r"\b(?:app|android|" + re.escape(p.package) + r"):id/[A-Za-z0-9_]+", line))
            ids = {rid.replace(p.package + ":id/", "app:id/") for rid in ids}
            if visible:
                login_visible |= bool(ids & blocked_ids)
                if node[1] == "com.tencent.mobileqq.widget.QQTabHost" and "android:id/tabhost" in ids:
                    host = set()
                    hosts.append(host)
                if host is not None:
                    for (cls, rid), key in required.items():
                        if node[1] == cls and rid in ids:
                            host.add(key)
            ancestors.append((indent, visible, host))
        if not login_visible and any(host == {"title", "list", "tabs"} for host in hosts):
            return True
    return False


class QidianUi:
    """企点控件层。构造只建对象,**不连任何设备**;每个方法自己带超时与可观察的失败原因。"""

    def __init__(self, *, adb, profiles: Optional[dict[str, Profile]] = None, profile_dir: Any = None,
                 store=None, alerts=None, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                 sleep: Optional[Callable[[float], Awaitable[None]]] = None,
                 on_default_profile: Optional[Callable[[str, Optional[str]], None]] = None):
        self._adb = adb
        self.profiles = profiles if profiles is not None else load_profiles(profile_dir)
        self._store = store
        self._alerts = alerts
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        #: 落到 ``default.yaml`` 时通知装配方(02 §2.2.3 要求账号 ``degraded(UI_UNEXPECTED)``;
        #: 状态迁移是 ``AccountService.transition`` 的职责,本层只报事实、不自己迁移)。
        self._on_default_profile = on_default_profile
        self._prepared_login: dict[str, bool] = {}

    # ------------------------------------------------------------------ profile
    def profile_for(self, acct: Account) -> Profile:
        p, how = pick_profile(self.profiles, acct.app_version)
        if p is None:
            raise ProfileError("没有任何可用的企点 profile(含 default.yaml)")
        if how == "fallback" and self._alerts is not None:
            # 码已登记(02 §3.7 = info,见 alerts.REGISTERED);severity 仍显式传同值 —— 既有开发者测试
            # 断言的是「本调用给出 info」这件事,行为等价,不动它的断言
            self._alerts.firing(QIDIAN_PROFILE_FALLBACK, subject=f"account:{acct.id}", severity="info", account_id=acct.id,
                                evidence={"app_version": acct.app_version, "profile": p.version})
        elif how == "default" and acct.app_version:
            # 02 §2.2.3:落到 default 时账号应 degraded(UI_UNEXPECTED)。状态迁移归 accounts —— 本层只报事实。
            log.warning("企点 profile 落到 default(account=%s app_version=%s):定位表未必匹配,账号应置 degraded(UI_UNEXPECTED)",
                        acct.id, acct.app_version)
            if self._on_default_profile is not None:
                try:
                    self._on_default_profile(acct.id, acct.app_version)
                except Exception:                        # 通知失败不许影响登录/发送本身
                    log.exception("profile 落 default 的降级通知失败(account=%s)", acct.id)
        return p

    # ------------------------------------------------------------------ adb 原语
    async def _shell(self, acct: Account, cmd: str) -> str:
        return await self._adb.shell(serial_of(acct.id), cmd)

    async def _dump(self, acct: Account, p: Profile) -> list[Node]:
        """控件树。空读/解析失败按 ``timeouts.dump_retries`` 重试;仍不行 ⇒ ``ValueError``(调用方不盲点)。"""
        retries = max(1, int(p.timeout("dump_retries", 3)))
        last = "未执行"
        for i in range(retries):
            try:
                nodes = parse_ui_xml(await self._shell(acct, "uiautomator dump /dev/tty 2>/dev/null"))
                if nodes:
                    return nodes
                last = "控件树为空"
            except (ValueError, ET.ParseError) as e:
                last = str(e)
            await self._sleep(p.timeout("ui_settle_s", 0.5) * (i + 1))
        raise ValueError(f"uiautomator dump 不可用:{last}")

    async def _foreground(self, acct: Account) -> tuple[str, str]:
        """前台 ``(package, activity)``;取不到回 ``("","")``。"""
        m = _FOCUS_RE.search(await self._shell(acct, "dumpsys window"))
        return (m.group(1), m.group(2)) if m else ("", "")

    async def _top_texts(self, acct: Account) -> list[str]:
        """``dumpsys activity top`` 里的 ``text=…``。控件树被常驻动画卡住时的第二个标题来源(只读文本,不产坐标)。"""
        raw = await self._shell(acct, "dumpsys activity top")
        return [a or b for a, b in _TOP_TEXT_RE.findall(raw) if (a or b)]

    async def _tap(self, acct: Account, x: int, y: int, p: Profile) -> None:
        await self._shell(acct, f"input tap {x} {y}")
        await self._sleep(p.timeout("ui_settle_s", 0.5))

    async def _tap_anchor(self, acct: Account, p: Profile, name: str, nodes: Optional[list[Node]] = None) -> bool:
        """按锚点点击:坐标只从控件树 ``bounds`` 来;找不到且没开坐标兜底 ⇒ 返回 False(不盲点)。"""
        try:
            nodes = nodes if nodes is not None else await self._dump(acct, p)
        except ValueError:
            nodes = []
        n = find_node(nodes, p.anchor(name))
        if n is not None:
            x, y = n.center
            await self._tap(acct, x, y, p)
            return True
        xy = p.coord(name)
        if xy is not None:
            log.warning("锚点 %s 控件未命中,按 profile 显式开启的坐标兜底点击 %s", name, xy)
            await self._tap(acct, xy[0], xy[1], p)
            return True
        return False

    async def ensure_ime(self, acct: Account, p: Profile) -> bool:
        """05 §2.1.1 ⑥ 的「ADBKeyboard 设为默认输入法」。中文/换行只能走它(参考实现坑3)。"""
        if not p.ime:
            return False
        if p.ime not in await self._shell(acct, "ime list -s"):
            await self._shell(acct, f"ime enable {p.ime}")
        await self._shell(acct, f"ime set {p.ime}")
        await self._sleep(p.timeout("ui_settle_s", 0.5))
        return p.ime in await self._shell(acct, "settings get secure default_input_method")

    async def _input_text(self, acct: Account, p: Profile, text: str) -> None:
        """ADBKeyboard ``ADB_INPUT_B64``:文本走 base64,**不经 shell 转义** ⇒ 中文/换行/引号/表情占位都原样送达。"""
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        await self._shell(acct, f"am broadcast -a ADB_INPUT_B64 --es msg {b64}")
        await self._sleep(p.timeout("ui_settle_s", 0.5))

    async def _clear_text(self, acct: Account, p: Profile) -> None:
        await self._shell(acct, "am broadcast -a ADB_CLEAR_TEXT")
        await self._sleep(p.timeout("ui_settle_s", 0.5))

    # ------------------------------------------------------------------ 登录(05 §2.1.1 ⑥~⑪)
    async def prepare_login(self, acct: Account) -> bool:
        """无凭据执行 ⑥~⑧；协议处理和重启后的界面读回都通过才缓存就绪。"""
        self._prepared_login.pop(acct.id, None)
        p = self.profile_for(acct)
        timeout_s = max(0.1, p.timeout("login_s", 90.0))
        deadline_ms = self._clock() + int(timeout_s * 1000)
        try:
            async with asyncio.timeout(timeout_s):
                if not await self.ensure_ime(acct, p):
                    return False
                await self._launch(acct, p)
                if not await self._wait_login_ready(acct, p, deadline_ms=deadline_ms, allow_consent=True):
                    return False
                # 首启 MSF 必须在协议已消失之后重新初始化(05 §2.1.1 ⑧)。
                await self._shell(acct, f"am force-stop {p.package}")
                await self._launch(acct, p)
                if not await self._wait_login_ready(acct, p, deadline_ms=deadline_ms, allow_consent=False):
                    return False
        except asyncio.TimeoutError:
            return False
        self._prepared_login[acct.id] = True
        return True

    async def _wait_login_ready(self, acct: Account, p: Profile, *, deadline_ms: int, allow_consent: bool) -> bool:
        """只从控件树确认协议消失及真实表单/主界面；最多点击一次协议和重新登录入口。"""
        consent_clicked = relogin_clicked = False
        while self._clock() < deadline_ms:
            try:
                nodes = await self._dump(acct, p)
            except ValueError:
                nodes = []
            if find_node(nodes, p.anchor("agree_button")) is not None:
                if not allow_consent:
                    return False                         # 重启后仍有协议，不能继续点或假报就绪。
                if not consent_clicked:
                    if not await self._tap_anchor(acct, p, "agree_button", nodes):
                        return False
                    consent_clicked = True
            elif (find_node(nodes, p.anchor("main_marker")) is not None
                  or all(find_node(nodes, p.anchor(key)) is not None
                         for key in ("login_account", "login_password", "login_submit"))):
                return True
            elif not relogin_clicked and find_node(nodes, p.anchor("relogin_button")) is not None:
                if not await self._tap_anchor(acct, p, "relogin_button", nodes):
                    return False
                relogin_clicked = True
            await self._sleep(max(0.1, p.timeout("poll_interval_s", 1.0)))
        return False

    async def login(self, acct: Account, login_account: Optional[str], secret: Optional[str]) -> LoginOutcome:
        """⑥ 设 IME → ⑦ 首拉起 + 同意协议 → ⑧ force-stop + 再拉起 → ⑩ 填账密点登录 → ⑪ 轮询判定。

        免登命中(⑬ 登录态从 /data 卷恢复)⇒ 跳过 ⑩ 直接判定。被踢后画面停在「重新登录」⇒ 先点它再填。
        需要短信/滑块/设备确认 ⇒ 回对应 ``WAIT_*`` **交人处理,不硬闯**。
        """
        try:
            p = self.profile_for(acct)
        except ProfileError as e:
            return LoginOutcome(None, reason=f"profile_unavailable:{e}")
        if acct.id not in self._prepared_login:
            if not await self.prepare_login(acct):
                return LoginOutcome(None, reason="login_form_not_found")
            ime_ok = self._prepared_login.pop(acct.id)
        else:
            self._prepared_login.pop(acct.id)
            # WAIT_PASSWORD 可能停留很久；填凭据前重新读回 IME，不重复拉起/强停。
            ime_ok = await self.ensure_ime(acct, p)
        verdict = await self._login_verdict(acct, p, once=True)
        if verdict is not None and verdict.result == "running":
            return verdict                                                        # 免登命中
        if not await self._fill_login(acct, p, login_account, secret, ime_ok):     # ⑩
            return LoginOutcome(None, reason="login_form_not_found")
        out = await self._login_verdict(acct, p)                                   # ⑪
        return out if out is not None else LoginOutcome(None, reason="login_timeout")

    async def _launch(self, acct: Account, p: Profile) -> None:
        await self._shell(acct, f"am start -n {p.package}/{p.splash_activity}")
        await self._sleep(p.timeout("launch_s", 3.0))

    async def _fill_login(self, acct: Account, p: Profile, login_account: Optional[str], secret: Optional[str],
                          ime_ok: bool) -> bool:
        """⑩ 定位账号框/密码框 → ADBKeyboard 输入 → 点登录。控件缺失 ⇒ False(不盲点、不乱打字)。"""
        if not ime_ok:
            log.warning("ADBKeyboard 未成为默认输入法(account=%s),账密无法可靠输入", acct.id)
            return False
        try:
            nodes = await self._dump(acct, p)
        except ValueError as e:
            log.warning("登录页控件树不可得(account=%s):%s", acct.id, e)
            return False
        if find_node(nodes, p.anchor("relogin_button")) is not None:               # 被踢后的「重新登录」路径
            await self._tap_anchor(acct, p, "relogin_button", nodes)
            try:
                nodes = await self._dump(acct, p)
            except ValueError:
                return False
        if find_node(nodes, p.anchor("agree_button")) is not None:
            return False                                   # 等密码期间协议重新覆盖表单，也不能向底层控件填密。
        if find_node(nodes, p.anchor("login_account")) is None or find_node(nodes, p.anchor("login_password")) is None:
            return False
        if login_account:
            await self._tap_anchor(acct, p, "login_account", nodes)
            await self._clear_text(acct, p)
            await self._input_text(acct, p, login_account)
        await self._tap_anchor(acct, p, "login_password", nodes)
        await self._clear_text(acct, p)
        await self._input_text(acct, p, secret or "")
        return await self._tap_anchor(acct, p, "login_submit", nodes)   # 同一屏的控件用同一棵树,不重复 dump

    async def _login_verdict(self, acct: Account, p: Profile, *, once: bool = False) -> Optional[LoginOutcome]:
        """⑪ 每 ``poll_interval_s`` 一轮、上限 ``login_s``:主界面 / 验证页 / 错误 toast 三类。超时回 ``None``。"""
        deadline = self._clock() + int(p.timeout("login_s", 90.0) * 1000)
        interval = p.timeout("poll_interval_s", 1.0)
        while True:
            try:
                nodes = await self._dump(acct, p)
            except ValueError:
                nodes = []
            blob = "\n".join(n.text + "\u0000" + n.desc for n in nodes)
            for code, key in (("WAIT_SMS", "sms"), ("WAIT_CAPTCHA", "captcha"), ("WAIT_DEVICE_CONFIRM", "device_confirm")):
                if any(w in blob for w in p.markers.get(key, [])):                 # b) 交人处理,不硬闯
                    return LoginOutcome(code, reason=f"marker:{key}")
            if any(w in blob for w in p.markers.get("bad_credential", [])):         # c) 错误 toast
                return LoginOutcome("bad_credential", reason="marker:bad_credential")
            if find_node(nodes, p.anchor("main_marker")) is not None:               # a) 会话列表主界面
                uid = await self.discover_self_uid(acct, p)
                return LoginOutcome("running", self_uid=uid, evidence={"self_uid_found": uid is not None})
            if once or self._clock() >= deadline:
                return None
            await self._sleep(interval)

    async def discover_self_uid(self, acct: Account, p: Optional[Profile] = None) -> Optional[str]:
        """05 §2.1.1 ⑪a(R6-39/R6-40):``self_uid`` = 登录 uin,取 databases 下 ``^[0-9]+\\.db$`` 的那个。

        多个时取 ``-wal`` 修改时间最新的;``-wal`` 都不存在时取 ``.db`` 本身最新的;**仍并列则留空、不得猜**
        (取错一次的代价是 06 §2.9.5 ③ 把该账号全部水位删掉重建)。
        """
        pkg = (p or self.profile_for(acct)).package
        directory = f"/data/data/{pkg}/databases"
        # Android Toybox 没有 GNU ls --time-style；只取文件名及 Unix mtime，不读库内容。
        cmd = f"find {shlex.quote(directory)} -maxdepth 1 -type f -name '*.db*' -exec stat -c '%Y %n' {{}} +"
        try:
            out = await self._read_shell(acct, cmd)
        except Exception:
            return None
        db: dict[str, int] = {}
        wal: dict[str, int] = {}
        for line in out.replace("\r", "").splitlines():
            m = re.fullmatch(rf"([0-9]+) {re.escape(directory)}/([0-9]+)\.db(-wal)?", line)
            if not m:
                continue
            (wal if m.group(3) else db)[m.group(2)] = int(m.group(1))
        pool = {uid: stamp for uid, stamp in wal.items() if uid in db} or db
        if not pool:
            return None
        best = max(pool.values())
        winners = [uin for uin, ts in pool.items() if ts == best]
        if len(winners) != 1:
            log.warning("self_uid 并列(account=%s 候选数=%s),按 R6-40 留空、不猜", acct.id, len(winners))
            return None
        return winners[0]

    async def _read_shell(self, acct: Account, cmd: str) -> str:
        """只读观察使用可取消 CLI，并检查 Android 命令退出码。"""
        result = await asyncio.wait_for(self._adb.shell_result(serial_of(acct.id), cmd, timeout_s=5), 5)
        if result.returncode != 0:
            raise RuntimeError("Android 只读命令未完成")
        return result.output

    async def probe_login(self, acct: Account) -> LoginOutcome:
        """只读观察手工登录；不启动应用、切 IME、填框或调用密码登录。"""
        p, selection = pick_profile(self.profiles, acct.app_version)
        if p is None:
            return LoginOutcome(None, reason="profile_unavailable", evidence={"stage": "profile"})
        stage = "foreground"
        try:
            async with asyncio.timeout(15):
                focus = _FOCUS_RE.search(await self._read_shell(acct, "dumpsys window"))
                if not focus or focus[1] != p.package or not re.search(p.activities.get("main", r"(?!)"), focus[2]):
                    return LoginOutcome(None, reason="foreground_not_main")
                stage = "ui"
                try:
                    nodes = parse_ui_xml(await self._read_shell(acct, "uiautomator dump /dev/tty 2>/dev/null"))
                except Exception:
                    nodes = None
                if nodes is None:
                    stage = "activity_structure"
                    top = await self._read_shell(acct, "dumpsys activity top")
                    if not _postlogin_activity_visible(top, f"{focus[1]}/{focus[2]}", p):
                        return LoginOutcome(None, reason="main_ui_unverified")
                else:
                    marker_id = p.anchor("main_marker").get("id")
                    # 已取得 XML 时，以它为准，绝不拿 fallback 覆盖明确的登录/验证页。
                    if not marker_id or not marker_id.startswith(p.package + ":id/") or not any(n.rid == marker_id for n in nodes):
                        return LoginOutcome(None, reason="main_ui_unverified")
                    if any(find_node(nodes, p.anchor(key)) is not None
                           for key in ("agree_button", "login_account", "login_password", "relogin_button")):
                        return LoginOutcome(None, reason="login_ui_visible")
                    blob = "\n".join(n.text + "\u0000" + n.desc for n in nodes)
                    if any(word in blob for key in ("sms", "captcha", "device_confirm", "bad_credential")
                           for word in p.markers.get(key, [])):
                        return LoginOutcome(None, reason="verification_ui_visible")
                stage = "identity"
                uid = await self.discover_self_uid(acct, p)
                if uid is None:
                    return LoginOutcome(None, reason="self_uid_unavailable", evidence={"stage": stage})
                # 读取元数据期间若切到其它窗口，旧画面不能用于宣布当前已登录。
                stage = "foreground_recheck"
                final_focus = _FOCUS_RE.search(await self._read_shell(acct, "dumpsys window"))
                if not final_focus or final_focus.groups() != focus.groups():
                    return LoginOutcome(None, reason="foreground_changed")
                return LoginOutcome("running", self_uid=uid, evidence={
                    "source": "readonly_ui",
                    "profile": {"selection": selection, "version": p.version, "app_version": acct.app_version},
                })
        except Exception:
            return LoginOutcome(None, reason="login_probe_unavailable", evidence={"stage": stage})

    # ------------------------------------------------------------------ 发送(06 §2.9.5 / 02 §2.2.3)
    async def send(self, acct: Account, native_id: str, text: str) -> SendOutcome:
        """搜索并打开目标会话 → 🔴 发送前强校验会话 → 输入 → 点发送 → **点完即返回**(确认走读库)。"""
        if has_control_chars(text):
            return SendOutcome(False, reason="text_has_control_chars")             # R6-48 的入口判据,UI 层再守一次
        if not text:
            return SendOutcome(False, reason="text_empty")
        try:
            p = self.profile_for(acct)
        except ProfileError as e:
            return SendOutcome(False, reason=f"profile_unavailable:{e}")
        if not await self.ensure_ime(acct, p):
            return SendOutcome(False, reason="adbkeyboard_not_default")            # 输入法没就位就不许打字
        expect = self._expected_title(acct, native_id)
        opened = await self._open_chat(acct, p, native_id, expect)
        if not opened.ok:
            return opened
        expect = opened.evidence.get("expect_title") or expect
        gate1 = await self._assert_session(acct, p, native_id, expect, where="before_input")
        if not gate1.ok:
            return gate1                                                           # 闸门1:不是目标会话,连字都不打
        if not await self._tap_anchor(acct, p, "chat_input"):
            return SendOutcome(False, reason="anchor_not_found:chat_input")
        await self._clear_text(acct, p)
        await self._input_text(acct, p, text)
        if p.verify_input:
            back = await self._read_input(acct, p)
            if back != attr_normalize(text):
                return SendOutcome(False, reason="input_readback_mismatch", evidence={"readback": back})
        gate2 = await self._assert_session(acct, p, native_id, expect, where="before_send")
        if not gate2.ok:
            return gate2                                                           # 闸门2:打完字到点发送之间会话被切走
        if not await self._tap_anchor(acct, p, "chat_send"):
            return SendOutcome(False, reason="anchor_not_found:chat_send")
        # 🔴 点完即返回并让出队列(R6-38):确认 = poll 的 ingest 合并,bus 在队列外等,这里**不** scrape。
        return SendOutcome(True, evidence={"native_id": native_id, "title": expect})

    async def send_text(self, acct: Account, native_id: str, text: str) -> bool:
        """``QidianAdapter`` 的 ``SendFn``。失败原因只落日志 —— 契约的返回值就是 bool。"""
        out = await self.send(acct, native_id, text)
        if not out.ok:
            log.error("企点发送未执行(account=%s native_id=%s):%s %s", acct.id, native_id, out.reason, out.evidence or "")
        return out.ok

    def _expected_title(self, acct: Account, native_id: str) -> Optional[str]:
        """会话显示名(库里已知则用它做标题期望值);查不到回 ``None``,改由名片页现读。"""
        if self._store is None:
            return None
        try:
            rows = self._store.list_sessions(account_id=acct.id)
        except Exception as e:                                                     # store 不可用不该反噬发送前校验
            log.warning("查会话名失败(account=%s):%s", acct.id, e)
            return None
        want = f"{acct.id}:{native_id}"
        for r in rows:
            if r.get("native_id") == native_id or r.get("id") == want:
                return r.get("name") or None
        return None

    async def _open_chat(self, acct: Account, p: Profile, native_id: str, expect: Optional[str]) -> SendOutcome:
        """已在目标会话 ⇒ 原地返回;否则 主界面 → 搜索 → 首条结果 → 名片页(校 ``native_id``)→ 发消息。"""
        if (await self._assert_session(acct, p, native_id, expect, where="preflight")).ok:
            return SendOutcome(True, evidence={"expect_title": expect, "reused": True})
        await self._launch(acct, p)                                                # 回主界面(比连点 BACK 确定)
        if not await self._tap_anchor(acct, p, "search_entry"):
            return SendOutcome(False, reason="anchor_not_found:search_entry")
        await self._input_text(acct, p, search_key(native_id))
        await self._sleep(p.timeout("search_s", 2.0))
        if not await self._tap_anchor(acct, p, "search_result_first"):
            return SendOutcome(False, reason="anchor_not_found:search_result_first")
        try:
            nodes = await self._dump(acct, p)
        except ValueError as e:
            return SendOutcome(False, reason=f"card_tree_unavailable:{e}")
        ident = find_node(nodes, p.anchor("card_identity"))
        blob = "\n".join(n.text + "\u0000" + n.desc for n in nodes)
        key = search_key(native_id)
        if not ((ident is not None and key in (ident.text + ident.desc)) or key in blob):
            return SendOutcome(False, reason="card_identity_mismatch", evidence={"want": key})
        title = expect or (find_node(nodes, p.anchor("chat_title")).text if find_node(nodes, p.anchor("chat_title")) else None)
        if not await self._tap_anchor(acct, p, "card_send_msg", nodes):
            return SendOutcome(False, reason="anchor_not_found:card_send_msg")
        return SendOutcome(True, evidence={"expect_title": title})

    async def _assert_session(self, acct: Account, p: Profile, native_id: str, expect: Optional[str], *,
                              where: str) -> SendOutcome:
        """🔴 发送前会话强校验(00 §11.3 [GATE]):前台必须是聊天页 **且** 标题/会话标识 == 目标。

        标题拿不到(企点聊天页常驻动画会把 ``uiautomator dump`` 卡住,参考实现记过这个坑)**就是校验不过** ——
        宁可不发,绝不发错。``dumpsys activity top`` 作为第二来源,由 profile 的 ``title_sources`` 开关。
        """
        pkg, act = await self._foreground(acct)
        if pkg != p.package or not re.search(p.activities["chat"], act):
            return SendOutcome(False, reason="not_in_chat_activity", evidence={"where": where, "fg": f"{pkg}/{act}"})
        want = {search_key(native_id), native_id} | ({expect} if expect else set())
        seen: list[str] = []
        if "uiautomator" in p.title_sources:
            try:
                nodes = await self._dump(acct, p)
            except ValueError:
                nodes = []
            t = find_node(nodes, p.anchor("chat_title"))
            if t is not None:
                seen.append(t.text)
        if not any(s and s in want for s in seen) and "dumpsys_top" in p.title_sources:
            seen += await self._top_texts(acct)
        if any(s and s in want for s in seen):
            return SendOutcome(True, evidence={"where": where})
        return SendOutcome(False, reason="session_mismatch" if seen else "title_unavailable",
                           evidence={"where": where, "want": sorted(x for x in want if x), "seen": seen[:8]})

    async def _read_input(self, acct: Account, p: Profile) -> Optional[str]:
        try:
            nodes = await self._dump(acct, p)
        except ValueError:
            return None
        n = find_node(nodes, p.anchor("chat_input"))
        return n.text if n is not None else None

    # ------------------------------------------------------------------ 装配适配
    def login_probe_fn(self):
        """只读观察回调，与会输入凭据的 login_fn 分开装配。"""
        async def fn(row: dict[str, Any]) -> dict[str, Any]:
            acct = Account(id=row["id"], channel="qidian", state=row["state"], self_uid=row.get("self_uid"),
                           app_version=row.get("app_version") or row.get("runtime_app_version"))
            out = await self.probe_login(acct)
            return {"ready": out.result == "running", "self_uid": out.self_uid, "reason": out.reason,
                    "profile": out.evidence.get("profile"), "stage": out.evidence.get("stage")}
        return fn

    def prepare_login_fn(self):
        """包成 AccountService 的无凭据准备回调；不填账号密码、不点登录。"""
        async def fn(row: dict[str, Any]) -> None:
            acct = Account(id=row["id"], channel="qidian", state=row.get("state") or "starting",
                           self_uid=row.get("self_uid"), app_version=row.get("app_version"))
            if not await self.prepare_login(acct):
                raise RuntimeError("企点协议、登录界面或输入法尚未就绪")
        return fn

    def login_fn(self, on_self_uid: Optional[Callable[[str, str], None]] = None):
        """包成 ``accounts.LoginFn``:``(row, account, secret) -> 'running'|'bad_credential'|'WAIT_*'|None``。"""
        async def fn(row: dict[str, Any], account: Optional[str], secret: Optional[str]) -> Optional[str]:
            channel = row.get("channel") or "qidian"
            if channel != "qidian":                       # 同一个 login_fn 挂在 AccountService 上,只认企点
                return None
            acct = Account(id=row["id"], channel=channel, state=row.get("state") or "logging_in",
                           self_uid=row.get("self_uid"), app_version=row.get("app_version"))
            out = await self.login(acct, account, secret)
            if out.result != "running" or not out.reason:
                log.info("企点登录判定 account=%s result=%s reason=%s", acct.id, out.result, out.reason)
            if out.self_uid and on_self_uid is not None:
                on_self_uid(acct.id, out.self_uid)
            return out.result
        return fn


# ---------------------------------------------------------------------------- 节点查询
def find_node(nodes: list[Node], anchor: dict[str, Any]) -> Optional[Node]:
    """锚点命中:``id`` 全等 或 ``text_any`` 子串命中 或 ``desc_any`` 子串命中(取或);按文档序取第一个。"""
    rid = anchor.get("id")
    texts = [str(t) for t in (anchor.get("text_any") or [])]
    descs = [str(t) for t in (anchor.get("desc_any") or [])]
    for n in nodes:
        if rid and n.rid == rid:
            return n
        if texts and any(t in n.text for t in texts):
            return n
        if descs and any(t in n.desc for t in descs):
            return n
    return None


def attr_normalize(s: str) -> str:
    """XML 属性值规范化(XML 1.0 §3.3.3):``\r\n`` / ``\r`` / ``\n`` / ``\t`` 一律成一个空格。

    输入框回读走的是 ``uiautomator dump`` 的 ``text=`` **属性**,解析器必然对它做这一步 ⇒ 含换行的文本
    回读出来永远是空格。比对时对期望值做同样的规范化,**其余字符仍逐字比**(这是对齐 XML 规范,不是放松判据)。
    """
    return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")


def search_key(native_id: str) -> str:
    """搜索/校验用的原生标识:群 ``g_<群号>`` 取群号,单聊即对端 uin(06 §2.9.5 R6-21)。"""
    return native_id[2:] if native_id.startswith("g_") else native_id
