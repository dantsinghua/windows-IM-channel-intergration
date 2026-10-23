"""Observed Qidian messages page has no search; contacts tab owns the search field."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from qtrade_agent.adapters.qidian.ui import QidianUi, SendOutcome, load_profiles
from qtrade_agent.adapters.qidian.adapter import QidianAdapter
from qtrade_agent.models import Command
from tests.test_qidian_ui import PKG, Clk, acct, node, tree
from tests.test_qidian_verified_search_title import VerifiedTitleAdb, SessionNames, UID, NAME, BODY

class MessagesAdb(VerifiedTitleAdb):
    def __init__(self, *, duplicate=False, tab_text="通讯录", heading="通讯录", foreign=False, retained=False, **kwargs):
        super().__init__(**kwargs)
        self.stage="retained" if retained else "messages"
        self.fg=("other.app/Other" if foreign else f"{PKG}/com.tencent.mobileqq.activity.SplashActivity")
        self.duplicate=duplicate;self.tab_text=tab_text;self.heading=heading
    async def shell(self, serial, cmd):
        if cmd.startswith("am start"):
            self.cmds.append(cmd);return ""
        if cmd == "input keyevent 4":
            self.cmds.append(cmd);self.stage="messages";return ""
        if cmd.startswith("uiautomator dump") and self.stage in ("messages","contacts","retained"):
            self.cmds.append(cmd)
            if self.stage=="retained":
                return tree(node(rid=f"{PKG}:id/title",text="old unknown chat"),node(rid=f"{PKG}:id/input"))
            if self.stage=="contacts":
                return tree(node(rid=f"{PKG}:id/ivTitleName",text=self.heading),node(rid=f"{PKG}:id/et_search_keyword"))
            tab=node(rid=f"{PKG}:id/tab_item_title",text=self.tab_text,bounds="[200,1100][300,1180]")
            return tree(node(rid=f"{PKG}:id/ivTitleName",text="消息"),node(rid=f"{PKG}:id/recent_chat_list"),tab,*([tab] if self.duplicate else []))
        if cmd=="input tap 250 1140":
            self.cmds.append(cmd);self.stage="contacts";return ""
        return await super().shell(serial,cmd)

def setup(**kw):
    clock=Clk();adb=MessagesAdb(**kw);p=load_profiles()["default"]
    p=replace(p,timeouts={**p.timeouts,"ui_settle_s":0,"launch_s":0,"search_s":.3})
    async def sleep(seconds):clock.ms+=max(1,int(seconds*1000))
    return QidianUi(adb=adb,profiles={"default":p},store=SessionNames(UID),clock=clock,sleep=sleep),adb

@pytest.mark.parametrize("retained",[False,True])
async def test_messages_home_reaches_verified_contacts_search_then_sends_once(retained):
    ui,adb=setup(retained=retained)
    outcome=await ui.send(acct(),UID,BODY)
    assert outcome.ok,outcome
    assert adb.sent==[BODY]
    assert adb.cmds.count("input tap 250 1140")==1
    assert adb.typed==[UID,BODY]
    assert adb.cmds.count("input keyevent 4")==int(retained)

@pytest.mark.parametrize("options",[{"duplicate":True},{"tab_text":"工作台"},{"heading":"其他页面"},{"foreign":True}])
async def test_unverified_contacts_navigation_never_types_or_sends(options):
    ui,adb=setup(**options)
    outcome=await ui.send(acct(),UID,BODY)
    assert not outcome.ok
    assert adb.typed==[] and adb.sent==[]

async def test_contacts_navigation_still_verifies_target_card():
    ui,adb=setup(card_uid="9000999999")
    outcome=await ui.send(acct(),UID,BODY)
    assert not outcome.ok and outcome.reason=="card_identity_mismatch"
    assert adb.typed==[UID] and adb.sent==[]

async def test_structured_pre_send_failure_is_not_truthy_success_and_retains_reason():
    async def send(*args):return SendOutcome(False,reason="anchor_not_found:search_entry")
    adapter=QidianAdapter(SimpleNamespace(),sender=send,store=None)
    result=await adapter.send(acct(),Command(acct().id,"send_text",{"session":UID,"text":BODY}))
    assert result.ok is False
    assert result.data=={"native_action_attempted":False,"reason":"anchor_not_found:search_entry"}
    assert result.error.reason=="anchor_not_found:search_entry"


class KeyboardChatAdb(MessagesAdb):
    """Observed native BACK hides the keyboard, while the toolbar returns home."""
    def __init__(self):super().__init__(retained=True);self.keyboard=True
    async def shell(self,serial,cmd):
        if cmd=="input keyevent 4" and self.keyboard:
            self.cmds.append(cmd);self.keyboard=False;return ""
        if cmd.startswith("uiautomator dump") and self.stage=="retained":
            self.cmds.append(cmd)
            return tree(node(rid=f"{PKG}:id/title",text="unverified retained chat"),
                node(rid=f"{PKG}:id/qdui_aio_inputbar_edittext"),
                node(rid=f"{PKG}:id/ivTitleBtnLeft",desc="返回消息界面",bounds="[16,49][96,137]"))
        if cmd=="input tap 56 93":
            self.cmds.append(cmd);self.stage="messages";self.keyboard=False;return ""
        return await super().shell(serial,cmd)

@pytest.mark.parametrize("repeats",[1,2])
async def test_chat_toolbar_back_does_not_get_consumed_by_visible_keyboard(repeats):
    clock=Clk();adb=KeyboardChatAdb();p=load_profiles()["default"]
    p=replace(p,timeouts={**p.timeouts,"ui_settle_s":0,"launch_s":0,"search_s":.3})
    async def sleep(seconds):clock.ms+=max(1,int(seconds*1000))
    ui=QidianUi(adb=adb,profiles={"default":p},store=SessionNames(UID),clock=clock,sleep=sleep)
    for _ in range(repeats):
        adb.stage="retained";adb.keyboard=True;adb.fg=f"{PKG}/com.tencent.mobileqq.activity.SplashActivity"
        result=await ui.send(acct(),UID,BODY)
        assert result.ok,result
    assert adb.sent==[BODY]*repeats
    assert adb.cmds.count("input tap 56 93")==repeats
    assert "input keyevent 4" not in adb.cmds
