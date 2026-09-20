"""企点 UI 执行层(`adapters/qidian/ui.py`)的开发者测试 —— **全假后端**。

🔴 本文件不碰任何真设备:没有 docker、没有 adb、没有容器名。所有动作都经 ``UiFakeAdb``(可编程地喂
控件树 XML 与命令结果),断言落在「发了哪些 shell 命令」上 —— 尤其是**不该发的那些**(不盲点、不发错会话)。
"""
from __future__ import annotations

import base64
import re
from xml.sax.saxutils import quoteattr

import pytest

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qidian.ui import (Profile, ProfileError, QidianUi, attr_normalize, find_node, load_profiles,
                                             parse_profile, parse_ui_xml, pick_profile, profiles_root, search_key,
                                             serial_of)

#: 缺省 profile 的正文 —— 按**包内资源**取(不拼源码树路径),与 ui.load_profiles 同一条定位路径。
DEFAULT_YAML_TEXT = profiles_root().joinpath("default.yaml").read_text(encoding="utf-8")

PKG = "com.tencent.qidian"
CHAT_ACT = f"{PKG}/com.tencent.mobileqq.activity.ChatActivity"


# ---------------------------------------------------------------------------- 假后端与工具
class UiFakeAdb:
    """可编程 adb:``dumps`` 依次喂控件树(耗尽后重复最后一份),其余命令按前缀路由。记录全部 shell 命令。"""

    def __init__(self):
        self.cmds: list[str] = []
        self.dumps: list[str] = []
        self.fg: str = CHAT_ACT
        self.top_texts: list[str] = []
        self.ime_list = "com.android.adbkeyboard/.AdbIME\ncom.android.inputmethod/.Ime\n"
        self.ime_default = "com.android.adbkeyboard/.AdbIME\n"
        self.databases_ls = ""
        self._dump_i = 0

    async def shell(self, serial: str, cmd: str) -> str:
        self.cmds.append(cmd)
        if cmd.startswith("uiautomator dump"):
            if not self.dumps:
                return ""
            i = min(self._dump_i, len(self.dumps) - 1)
            self._dump_i += 1
            return self.dumps[i]
        if cmd.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{d0b1 u0 {self.fg}}}\n"
        if cmd.startswith("dumpsys activity top"):
            return "\n".join(f'text="{t}"' for t in self.top_texts)
        if cmd.startswith("ime list"):
            return self.ime_list
        if cmd.startswith("settings get secure default_input_method"):
            return self.ime_default
        if cmd.startswith("ls -l"):
            return self.databases_ls
        return ""

    # 协议其余方法:本层用不到,给全以免误用真后端
    async def connect(self, serial: str) -> bool: return True
    async def disconnect(self, serial: str) -> None: return None
    async def root(self, serial: str) -> None: return None
    async def devices(self) -> dict: return {}
    async def forward_remove(self, serial: str, local: str) -> None: return None

    # ---- 断言辅助
    @property
    def taps(self) -> list[str]:
        return [c for c in self.cmds if c.startswith("input tap")]

    @property
    def typed(self) -> list[str]:
        out = []
        for c in self.cmds:
            m = re.match(r"am broadcast -a ADB_INPUT_B64 --es msg (\S+)$", c)
            if m:
                out.append(base64.b64decode(m.group(1)).decode("utf-8"))
        return out


class Clk:
    def __init__(self):
        self.ms = 1_758_240_000_000

    def __call__(self) -> int:
        return self.ms


def node(rid: str = "", text: str = "", desc: str = "", bounds: str = "[0,0][100,100]", clickable: str = "true") -> str:
    """造一个 uiautomator 节点。text/desc 按 XML 属性转义 —— 真机 dump 出来也是转义过的。"""
    return (f'<node index="0" text={quoteattr(text)} resource-id="{rid}" class="android.widget.TextView" '
            f'content-desc={quoteattr(desc)} clickable="{clickable}" bounds="{bounds}" />')


def tree(*nodes: str) -> str:
    return ('UI hierchary dumped to: /dev/tty<?xml version="1.0" encoding="UTF-8"?>'
            '<hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>\n")


@pytest.fixture
def rig():
    adb = UiFakeAdb()
    clk = Clk()

    async def sleeper(s: float) -> None:
        clk.ms += int(s * 1000)

    ui = QidianUi(adb=adb, clock=clk, sleep=sleeper)
    return ui, adb, clk


def acct(app_version: str | None = None) -> Account:
    return Account(id="qd01", channel="qidian", state="running", app_version=app_version)


CHAT_TREE = tree(node(rid=f"{PKG}:id/title", text="张三-华泰固收", bounds="[100,40][600,90]"),
                 node(rid=f"{PKG}:id/input", text="", bounds="[40,1100][600,1150]"),
                 node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[620,1060][700,1110]"))


def chat_tree_with_input(text: str) -> str:
    return tree(node(rid=f"{PKG}:id/title", text="张三-华泰固收", bounds="[100,40][600,90]"),
                node(rid=f"{PKG}:id/input", text=text, bounds="[40,1100][600,1150]"),
                node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[620,1060][700,1110]"))


# ---------------------------------------------------------------------------- profile
def test_default_profile_加载并通过_schema():
    ps = load_profiles()                       # 缺省 = 包内资源
    assert "default" in ps
    p = ps["default"]
    assert p.package == PKG and p.ime.startswith("com.android.adbkeyboard")
    assert p.timeout("login_s", 0) == 90 and p.timeout("poll_interval_s", 0) == 1      # 05 §2.1.1 ⑪
    assert p.coord("chat_send") is None                                                 # 坐标兜底缺省关


def test_坏profile只跳过不抛(tmp_path):
    (tmp_path / "default.yaml").write_text(DEFAULT_YAML_TEXT, encoding="utf-8")
    (tmp_path / "9.9.9.9.yaml").write_text("schema: 1\nnodes:\n  chat_send: {id: x}\n", encoding="utf-8")
    ps = load_profiles(str(tmp_path))
    assert sorted(ps) == ["default"]                # 缺锚点的那份被拒绝加载,但没有拒绝启动


def test_profile_选择规则_精确_回退_默认():
    def mk(v: str) -> Profile:
        return parse_profile(DEFAULT_YAML_TEXT.replace("apk_version: default", f"apk_version: {v}"), version=v)
    ps = {"default": mk("default"), "9.1.2.0": mk("9.1.2.0"), "9.1.5.0": mk("9.1.5.0"), "9.2.0.0": mk("9.2.0.0")}
    assert pick_profile(ps, "9.1.5.0")[1] == "exact"
    p, how = pick_profile(ps, "9.1.9.9")
    assert how == "fallback" and p.version == "9.1.5.0"          # 同主次版本里最高的低版本
    assert pick_profile(ps, "8.0.0.0")[1] == "default"           # 主次都不同 ⇒ default
    assert pick_profile({}, "9.1.5.0")[1] == "none"


def test_profile_回退时发_info_告警(rig):
    ui, adb, _ = rig
    seen = []

    class A:
        def firing(self, code, **kw):
            seen.append((code, kw.get("severity")))
    txt = DEFAULT_YAML_TEXT
    ui.profiles = {"default": parse_profile(txt, version="default"),
                   "9.1.2.0": parse_profile(txt.replace("apk_version: default", "apk_version: 9.1.2.0"), version="9.1.2.0")}
    ui._alerts = A()
    ui.profile_for(acct("9.1.7.0"))
    assert seen == [("QIDIAN_PROFILE_FALLBACK", "info")]


async def test_没有任何profile时_登录与发送都回可读原因(rig):
    ui, adb, _ = rig
    ui.profiles = {}
    out = await ui.send(acct(), "10001", "喂")
    assert out.ok is False and out.reason.startswith("profile_unavailable")
    assert adb.cmds == []


# ---------------------------------------------------------------------------- 工具函数
def test_控件树解析_容忍设备的尾巴文本():
    nodes = parse_ui_xml(CHAT_TREE)
    assert len(nodes) == 3
    assert find_node(nodes, {"id": f"{PKG}:id/input"}).center == (320, 1125)
    with pytest.raises(ValueError):
        parse_ui_xml("uiautomator: ERROR could not get idle state")


def test_serial_与群前缀():
    assert serial_of("qd01") == "127.0.0.1:16001" and serial_of("qd12") == "127.0.0.1:16012"
    assert search_key("g_384766") == "384766" and search_key("10001") == "10001"


# ---------------------------------------------------------------------------- 登录(05 §2.1.1 ⑥~⑪)
MAIN_TREE = tree(node(rid=f"{PKG}:id/recent_chat_list", text="", bounds="[0,200][720,1280]"))
LOGIN_TREE = tree(node(rid=f"{PKG}:id/account", text="", bounds="[100,400][620,460]"),
                  node(rid=f"{PKG}:id/password", text="", bounds="[100,500][620,560]"),
                  node(rid=f"{PKG}:id/login", text="登录", bounds="[100,620][620,680]"))


async def test_免登命中_直接running且不填账密(rig):
    ui, adb, _ = rig
    adb.dumps = [MAIN_TREE]
    adb.databases_ls = "-rw------- 1 u0 u0 40960 1758240000 3007373675.db\n"
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result == "running" and out.self_uid == "3007373675"
    assert adb.typed == []                                       # 免登就不该去填账号密码
    assert any(c.startswith(f"am force-stop {PKG}") for c in adb.cmds)      # ⑧ 仍要强制重启一次


async def test_密码登录_填账密点登录后_running(rig):
    ui, adb, _ = rig
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, MAIN_TREE]
    out = await ui.login(acct(), "13800000000", "pw123")
    assert out.result == "running"
    assert adb.typed == ["13800000000", "pw123"]
    assert adb.taps[-1] == "input tap 360 650"                   # 登录按钮的控件中心,不是魔法坐标


@pytest.mark.parametrize("marker,code", [("短信验证", "WAIT_SMS"), ("拖动滑块", "WAIT_CAPTCHA"), ("设备锁", "WAIT_DEVICE_CONFIRM")])
async def test_验证页转对应WAIT码_交人处理不硬闯(rig, marker, code):
    ui, adb, _ = rig
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, tree(node(text=marker, bounds="[0,300][720,400]"))]
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result == code and out.reason.startswith("marker:")


async def test_错误toast转bad_credential(rig):
    ui, adb, _ = rig
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, tree(node(text="账号或密码错误", bounds="[0,300][720,400]"))]
    out = await ui.login(acct(), "13800000000", "bad")
    assert out.result == "bad_credential"


async def test_登录超时回None_不冒充成功(rig):
    ui, adb, clk = rig
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, tree(node(text="正在登录", bounds="[0,300][720,400]"))]
    t0 = clk.ms
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result is None and out.reason == "login_timeout"
    assert clk.ms - t0 >= 90_000                                  # 上限 login_timeout=90s 走满


async def test_被踢后先点重新登录再填(rig):
    ui, adb, _ = rig
    kicked = tree(node(text="重新登录", bounds="[200,700][520,760]"))
    adb.dumps = [tree(node(text="您的账号在别处登录", bounds="[0,300][720,400]")),
                 tree(node(text="您的账号在别处登录", bounds="[0,300][720,400]")), kicked, LOGIN_TREE, MAIN_TREE]
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result == "running"
    assert "input tap 360 730" in adb.taps                        # 点过「重新登录」


async def test_ADBKeyboard没就位_不乱打字直接回未判定(rig):
    ui, adb, _ = rig
    adb.ime_default = "com.android.inputmethod/.Ime\n"
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE]
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result is None and out.reason == "login_form_not_found"
    assert adb.typed == []


async def test_登录页控件树拿不到_不盲点(rig):
    ui, adb, _ = rig
    adb.dumps = ["uiautomator: could not get idle state"]
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result is None and out.reason == "login_form_not_found"
    assert adb.typed == [] and adb.taps == []


# ---- ⑪a self_uid(R6-39 / R6-40)
async def test_self_uid_多个时取wal最新(rig):
    ui, adb, _ = rig
    adb.databases_ls = ("-rw- 1 u0 u0 4096 1758240000 3007373675.db\n"
                        "-rw- 1 u0 u0 4096 1758240100 3007373675.db-wal\n"
                        "-rw- 1 u0 u0 4096 1758240900 8001234567.db-wal\n"
                        "-rw- 1 u0 u0 4096 1758240001 8001234567.db\n"
                        "-rw- 1 u0 u0 4096 1758240999 contact.db\n")
    assert await ui.discover_self_uid(acct()) == "8001234567"


async def test_self_uid_无wal时取db最新_并列则留空(rig):
    ui, adb, _ = rig
    adb.databases_ls = ("-rw- 1 u0 u0 4096 1758240100 3007373675.db\n"
                        "-rw- 1 u0 u0 4096 1758240900 8001234567.db\n")
    assert await ui.discover_self_uid(acct()) == "8001234567"
    adb.databases_ls = ("-rw- 1 u0 u0 4096 1758240900 3007373675.db\n"
                        "-rw- 1 u0 u0 4096 1758240900 8001234567.db\n")
    assert await ui.discover_self_uid(acct()) is None              # 并列 ⇒ 留空,不得猜
    adb.databases_ls = ""
    assert await ui.discover_self_uid(acct()) is None


# ---------------------------------------------------------------------------- 发送(00 §11.3 [GATE] 对象校验)
async def test_发送成功路径_点完即返回(rig):
    """搜索 → 名片页校身份 → 进聊天页 → 闸门1 → 输入 → 回读 → 闸门2 → 点发送,然后立刻返回。"""
    ui, adb, _ = rig
    search = tree(node(rid=f"{PKG}:id/et_search_keyword", text="", bounds="[100,160][620,220]"))
    card = tree(node(rid=f"{PKG}:id/account_uin", text="10001", bounds="[100,200][600,250]"),
                node(rid=f"{PKG}:id/title", text="张三-华泰固收", bounds="[100,120][600,180]"),
                node(text="发消息", bounds="[480,1070][580,1120]"))
    adb.dumps = [search,                                   # preflight:主界面,没有聊天页标题 ⇒ 不是目标会话
                 search,                                   # 点搜索框
                 tree(node(rid=f"{PKG}:id/title", text="张三-华泰固收", bounds="[100,280][600,330]")),   # 搜索结果首条
                 card,                                     # 名片页:校 native_id + 取标题期望值 + 点发消息
                 CHAT_TREE,                                # 闸门1
                 CHAT_TREE,                                # 点输入框
                 chat_tree_with_input("估值+8 能出么"),      # 回读
                 CHAT_TREE,                                # 闸门2
                 CHAT_TREE]                                # 点发送
    out = await ui.send(acct(), "10001", "估值+8 能出么")
    assert out.ok is True, out
    assert adb.typed == ["10001", "估值+8 能出么"]           # 先搜索词、后正文
    assert adb.taps[-1] == "input tap 660 1085"             # 发送键的控件中心,不是魔法坐标
    assert not any("sqlite3" in c or "select" in c.lower() for c in adb.cmds)   # 点完即返回:不在这里读库确认


async def test_标题不符_不发(rig):
    ui, adb, _ = rig
    adb.dumps = [tree(node(rid=f"{PKG}:id/title", text="李四-中信固收", bounds="[100,40][600,90]"),
                      node(rid=f"{PKG}:id/input", text="", bounds="[40,1100][600,1150]"))]
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    out = await ui.send(acct(), "10001", "估值+8 能出么")
    assert out.ok is False
    assert out.reason in ("session_mismatch", "card_identity_mismatch", "anchor_not_found:search_entry")
    assert adb.typed == []                                          # 🔴 一个字都没打、更没点发送


async def test_停在别人的会话里_不在那里发_先去找目标(rig):
    """人/上一条指令把画面留在了别的会话:标题不符 ⇒ 不在这里发,转去搜索目标;正文一个字都不打。"""
    ui, adb, _ = rig
    wrong = tree(node(rid=f"{PKG}:id/title", text="李四-中信固收", bounds="[100,40][600,90]"),
                 node(rid=f"{PKG}:id/input", text="", bounds="[40,1100][600,1150]"))
    adb.dumps = [wrong]
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    out = await ui.send(acct(), "10001", "估值+8 能出么")
    assert out.ok is False and out.reason == "anchor_not_found:search_entry"
    assert "估值+8 能出么" not in adb.typed                  # 🔴 正文没打进李四的窗口
    assert adb.taps == []


async def test_前台不是聊天页也不是企点_不发(rig):
    ui, adb, _ = rig
    adb.fg = "com.android.launcher/.Launcher"
    adb.dumps = [""]
    out = await ui.send(acct(), "10001", "喂")
    assert out.ok is False and adb.typed == []


async def test_标题源都拿不到_按校验不过处理(rig):
    ui, adb, _ = rig
    adb.fg = CHAT_ACT
    adb.dumps = ["uiautomator: ERROR could not get idle state"]     # 常驻动画把 dump 卡住(参考实现记过的坑)
    adb.top_texts = []
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    out = await ui.send(acct(), "10001", "喂")
    assert out.ok is False and adb.typed == []


async def test_dumpsys_top_作第二标题来源(rig):
    ui, adb, _ = rig
    adb.fg = CHAT_ACT
    adb.dumps = ["uiautomator: ERROR could not get idle state"]
    adb.top_texts = ["张三-华泰固收", "输入消息"]
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    out = await ui.send(acct(), "10001", "喂")
    # 标题校验过了,但控件树仍拿不到 ⇒ 卡在「找不到输入框」,而不是盲点
    assert out.ok is False and out.reason == "anchor_not_found:chat_input"
    assert adb.taps == []


async def test_控件找不到_缺省不盲点_开了坐标兜底才点(rig):
    ui, adb, _ = rig
    adb.fg = CHAT_ACT
    adb.dumps = [tree(node(rid=f"{PKG}:id/title", text="10001", bounds="[100,40][600,90]"))]
    out = await ui.send(acct(), "10001", "喂")
    assert out.ok is False and out.reason == "anchor_not_found:chat_input" and adb.taps == []
    # 打开 profile 的显式坐标兜底后才允许按实测坐标点
    txt = DEFAULT_YAML_TEXT.replace("enabled: false", "enabled: true")
    ui.profiles = {"default": parse_profile(txt, version="default")}
    adb2 = UiFakeAdb()
    adb2.fg = CHAT_ACT
    adb2.dumps = [tree(node(rid=f"{PKG}:id/title", text="10001", bounds="[100,40][600,90]"))]
    ui._adb = adb2
    out = await ui.send(acct(), "10001", "喂")
    assert "input tap 320 1123" in adb2.taps


async def test_ADBKeyboard未启用_直接拒发(rig):
    ui, adb, _ = rig
    adb.ime_default = "com.android.inputmethod/.Ime\n"
    out = await ui.send(acct(), "10001", "喂")
    assert out.ok is False and out.reason == "adbkeyboard_not_default"
    assert adb.taps == [] and adb.typed == []


async def test_含控制字符的文本_不发(rig):
    ui, adb, _ = rig
    out = await ui.send(acct(), "10001", "估值\x01+8")
    assert out.ok is False and out.reason == "text_has_control_chars"
    assert adb.cmds == []
    assert (await ui.send(acct(), "10001", "")).reason == "text_empty"


async def test_中文换行表情占位原样经base64送达(rig):
    ui, adb, _ = rig
    text = "第一行\n第二行 [表情] \"引号\" 'single' $PATH 100%"
    adb.fg = CHAT_ACT
    adb.dumps = [CHAT_TREE, CHAT_TREE, CHAT_TREE, chat_tree_with_input(attr_normalize(text)), CHAT_TREE, CHAT_TREE]
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    out = await ui.send(acct(), "10001", text)
    # 送出去的是原文(base64,不经 shell 转义);回读比对按 XML 属性规范化(换行→空格)后逐字比
    assert out.ok is True and adb.typed == [text]


async def test_输入回读不符_不点发送(rig):
    ui, adb, _ = rig
    adb.fg = CHAT_ACT
    adb.dumps = [CHAT_TREE, CHAT_TREE, CHAT_TREE, chat_tree_with_input("估值+8 能出")]
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    out = await ui.send(acct(), "10001", "估值+8 能出么")
    assert out.ok is False and out.reason == "input_readback_mismatch"
    assert "input tap 660 1085" not in adb.taps                     # 没点发送键


async def test_名片页身份不符_不进聊天页(rig):
    ui, adb, _ = rig
    adb.fg = f"{PKG}/com.tencent.mobileqq.activity.SplashActivity"
    search = tree(node(rid=f"{PKG}:id/et_search_keyword", text="", bounds="[100,160][620,220]"))
    card = tree(node(rid=f"{PKG}:id/account_uin", text="99999", bounds="[100,200][600,250]"),
                node(text="发消息", bounds="[480,1070][580,1120]"))
    adb.dumps = [search, search, tree(node(rid=f"{PKG}:id/title", text="某人", bounds="[100,280][600,330]")), card]
    out = await ui.send(acct(), "10001", "喂")
    assert out.ok is False and out.reason == "card_identity_mismatch"
    assert "input tap 530 1095" not in adb.taps                     # 没点「发消息」


async def test_群会话用群号搜索与校验(rig):
    """``native_id = g_<群号>``:搜索与标题校验都用去掉前缀的群号(06 §2.9.5 R6-21)。"""
    ui, adb, _ = rig
    t = tree(node(rid=f"{PKG}:id/title", text="384766", bounds="[100,40][600,90]"),
             node(rid=f"{PKG}:id/input", text="", bounds="[40,1100][600,1150]"),
             node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[620,1060][700,1110]"))
    typed = tree(node(rid=f"{PKG}:id/title", text="384766", bounds="[100,40][600,90]"),
                 node(rid=f"{PKG}:id/input", text="报价", bounds="[40,1100][600,1150]"),
                 node(rid=f"{PKG}:id/fun_btn", text="发送", bounds="[620,1060][700,1110]"))
    adb.dumps = [t, t, t, typed, t, t]
    out = await ui.send(acct(), "g_384766", "报价")
    assert out.ok is True and adb.typed == ["报价"]


async def test_send_text_适配SendFn返回bool(rig):
    ui, adb, _ = rig
    adb.fg = "com.android.launcher/.Launcher"
    adb.dumps = [""]
    assert await ui.send_text(acct(), "10001", "喂") is False


# ---------------------------------------------------------------------------- LoginFn 适配
async def test_login_fn_签名与返回值对齐accounts契约(rig):
    ui, adb, _ = rig
    adb.dumps = [MAIN_TREE]
    adb.databases_ls = "-rw- 1 u0 u0 4096 1758240000 3007373675.db\n"
    got: list[tuple[str, str]] = []
    fn = ui.login_fn(on_self_uid=lambda aid, uid: got.append((aid, uid)))
    row = {"id": "qd01", "channel": "qidian", "state": "logging_in", "app_version": None}
    assert await fn(row, "13800000000", "pw") == "running"
    assert got == [("qd01", "3007373675")]


class _FakeStore:
    def __init__(self, names: dict[str, str]):
        self._names = names

    def list_sessions(self, *, account_id: str, **kw):
        return [{"id": f"{account_id}:{nid}", "native_id": nid, "name": name} for nid, name in self._names.items()]
