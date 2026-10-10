"""企点 6.9.7 真机控件树回归(2026-10-10,redroid11_x86_64 720x1280@320dpi,容器 qtrade-qd90)。

下面三棵树是从真机 ``uiautomator dump`` **原样截取**的关键节点(文档序与属性未改,仅删去无关布局节点),
用来钉死真实链路联调揪出的三条缺陷,它们在旧 default.yaml + 旧 find_node 上全部为红:

1. 首拉起协议弹窗:正文 ``dialogText`` 与左键「不同意」在文档序上排在右键「同意」之前且都含「同意」,
   旧实现按文档序取第一个 ⇒ ``agree_button`` 命中正文/「不同意」,协议点不掉 ⇒ ``prepare_login`` 超时 ⇒ UI_UNEXPECTED。
2. 登录页账号框**没有 resource-id**(text="企点账号" desc="请输入企点号码或手机或邮箱"),旧锚点
   ``id/account`` + ``text_any 请输入账号`` 命不中 ⇒ 登录表单永远「未就绪」。
3. 底部「我已阅读并同意…」未勾选时点「登录」会再弹「请阅读并同意相关协议」(同一套 dialogRightBtn),
   旧 ``_fill_login`` 点完登录就不管了 ⇒ ⑪ 只会 login_timeout。
"""
from __future__ import annotations

from qtrade_agent.adapters.qidian.ui import UI_DUMP_CMD, UI_DUMP_PATH, find_node, load_profiles, parse_ui_xml
from test_qidian_ui import PKG, acct, node, rig, tree  # noqa: F401  rig 夹具重导出

# ---- 真机树 ①:首拉起「用户协议和隐私政策」
CONSENT_DIALOG_697 = tree(
    node(rid=f"{PKG}:id/dialogTitle", text="用户协议和隐私政策", bounds="[103,186][617,229]", clickable="false"),
    node(rid=f"{PKG}:id/dialogText",
         text="欢迎您使用腾讯企点！我们非常重视您的隐私保护和个人信息保护……如你同意，请勾选并点击同意开始接受我们的服务。",
         desc="欢迎您使用腾讯企点！我们非常重视您的隐私保护和个人信息保护，", bounds="[103,249][617,914]", clickable="false"),
    node(rid=f"{PKG}:id/dialogLeftBtn", text="不同意", desc="不同意按钮", bounds="[67,951][360,1034]"),
    node(rid=f"{PKG}:id/dialogRightBtn", text="同意", desc="同意按钮", bounds="[361,951][653,1034]"),
)
# ---- 真机树 ②:LoginActivity 登录页(账号框无 resource-id)
LOGIN_PAGE_697 = tree(
    node(rid=f"{PKG}:id/loginpage", desc="登录界面", bounds="[0,0][720,1184]", clickable="false"),
    node(rid="", text="企点账号", desc="请输入企点号码或手机或邮箱", bounds="[89,347][631,443]"),
    node(rid=f"{PKG}:id/password", text="密码", desc="密码 安全", bounds="[89,491][631,587]"),
    node(rid=f"{PKG}:id/findPass", text="忘记密码", desc="无法登录？", bounds="[105,611][209,647]"),
    node(rid=f"{PKG}:id/login", text="登 录", desc="登录", bounds="[89,753][631,849]"),
    node(rid=f"{PKG}:id/agreement_img", bounds="[78,1109][138,1169]"),
    node(rid="", text="我已阅读并同意", bounds="[138,1122][306,1155]", clickable="false"),
    node(rid=f"{PKG}:id/qdui_agreement_link", text="用户协议", bounds="[306,1122][402,1155]"),
)
# ---- 真机树 ③:点「登录」后的第二次协议弹窗
SUBMIT_CONSENT_697 = tree(
    node(rid=f"{PKG}:id/dialogTitle", text="请阅读并同意相关协议", bounds="[103,186][617,229]", clickable="false"),
    node(rid=f"{PKG}:id/dialogText", text="为保障你的合法权益，请阅读并同意《用户协议》、《隐私政策》、《账号规范》",
         bounds="[103,249][617,540]", clickable="false"),
    node(rid=f"{PKG}:id/dialogLeftBtn", text="取消", bounds="[67,951][360,1034]"),
    node(rid=f"{PKG}:id/dialogRightBtn", text="同意", bounds="[361,951][653,1034]"),
)
MAIN_TREE = tree(node(rid=f"{PKG}:id/recent_chat_list", text="", bounds="[0,200][720,1280]"))
PROFILE = load_profiles()["default"]


def test_控件树不走dev_tty_而是落容器临时目录再读回():
    """真机 ④:``uiautomator dump /dev/tty`` 在 redroid11 非 tty 会话只回一行提示、XML 不进 stdout;
    必须落 02 §2.2.4 规定的 ``/data/local/tmp``(停号随临时数据清理)再 ``cat``。"""
    assert UI_DUMP_PATH.startswith("/data/local/tmp/")
    assert "/dev/tty" not in UI_DUMP_CMD
    assert UI_DUMP_CMD.startswith(f"uiautomator dump {UI_DUMP_PATH}") and UI_DUMP_CMD.endswith(f"cat {UI_DUMP_PATH}")


async def test_dump与probe都用落盘读回的命令(rig):
    ui, adb, _ = rig
    adb.dumps = [LOGIN_PAGE_697]
    await ui._dump(acct(), PROFILE)
    assert UI_DUMP_CMD in adb.cmds
    assert not any("uiautomator dump /dev/tty" in c for c in adb.cmds)


def test_协议弹窗_agree_button_命中右键同意_而非正文或不同意():
    n = find_node(parse_ui_xml(CONSENT_DIALOG_697), PROFILE.anchor("agree_button"))
    assert n is not None and n.rid == f"{PKG}:id/dialogRightBtn" and n.text == "同意"
    assert n.center == (507, 992)


def test_find_node_id全等优先于文档序靠前的文本子串():
    nodes = parse_ui_xml(CONSENT_DIALOG_697)
    assert find_node(nodes, {"id": f"{PKG}:id/dialogRightBtn", "text_any": ["同意"]}).text == "同意"
    # 没给 id 时仍按文档序取第一个文本命中 —— 这正是旧锚点在真机上会点到正文的原因,行为保留供 profile 显式选择
    assert find_node(nodes, {"text_any": ["同意"]}).rid == f"{PKG}:id/dialogText"


def test_登录页三控件在真机树上全部命中_账号框靠content_desc():
    nodes = parse_ui_xml(LOGIN_PAGE_697)
    account = find_node(nodes, PROFILE.anchor("login_account"))
    assert account is not None and account.rid == "" and account.desc.startswith("请输入企点号码")
    assert find_node(nodes, PROFILE.anchor("login_password")).rid == f"{PKG}:id/password"
    assert find_node(nodes, PROFILE.anchor("login_submit")).center == (360, 801)
    assert find_node(nodes, PROFILE.anchor("agree_button")) is None      # 登录页本身没有协议弹窗,不得误判


async def test_prepare_login_真机序列_点掉协议_重启后读到登录页(rig):
    ui, adb, _ = rig
    adb.dumps = [CONSENT_DIALOG_697, LOGIN_PAGE_697, LOGIN_PAGE_697]
    assert await ui.prepare_login(acct()) is True
    assert "input tap 507 992" in adb.taps                             # 点的是右键「同意」的控件中心
    assert "input tap 213 992" not in adb.taps                         # 没点「不同意」
    assert "input tap 360 581" not in adb.taps                         # 没点正文
    assert any(c.startswith(f"am force-stop {PKG}") for c in adb.cmds)  # ⑧ 协议消失后强停再拉起


async def test_login_点登录后再弹协议_点掉一次后继续判定(rig):
    ui, adb, _ = rig
    # prepare:协议 → 登录页 → (重启后)登录页;login:once 判定 → 填表 → 提交后弹第二次协议 → 主界面
    adb.dumps = [CONSENT_DIALOG_697, LOGIN_PAGE_697, LOGIN_PAGE_697, LOGIN_PAGE_697, LOGIN_PAGE_697,
                 SUBMIT_CONSENT_697, MAIN_TREE]
    adb.databases_ls = "-rw- 1 u0 u0 4096 1758240000 3007373675.db\n"
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result == "running"
    assert adb.typed == ["13800000000", "pw"]
    assert adb.taps.count("input tap 507 992") == 2                   # 首拉起协议 + 提交后协议,各一次
    assert "input tap 360 801" in adb.taps                            # 登录键


async def test_login_提交后没有协议弹窗_不多点任何东西(rig):
    ui, adb, _ = rig
    adb.dumps = [CONSENT_DIALOG_697, LOGIN_PAGE_697, LOGIN_PAGE_697, LOGIN_PAGE_697, LOGIN_PAGE_697, MAIN_TREE]
    adb.databases_ls = "-rw- 1 u0 u0 4096 1758240000 3007373675.db\n"
    out = await ui.login(acct(), "13800000000", "pw")
    assert out.result == "running"
    assert adb.taps.count("input tap 507 992") == 1
    assert adb.taps[-1] == "input tap 360 801"
