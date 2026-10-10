"""2026-10-08真机脱敏结构的回归，fixture无账号/消息/description文本。

结构证据：qidian-view-hierarchy.json；句柄、PID、坐标和UID均为合成值。
资源组合来自当前企点SplashActivity，绝不从其它Activity/隐藏树凑证据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi
from tests.test_qidian_manual_login import AndroidIdentityAdb, UID, manual_rig, probe_tick  # noqa: F401
from tests.test_qidian_ui import LOGIN_TREE, PKG, acct, node, tree


MAIN_ACTIVITY = Path(__file__).with_name("fixtures").joinpath("qidian_postlogin_activity.txt").read_text()


class StructureAdb(AndroidIdentityAdb):
    def __init__(self, activity=MAIN_ACTIVITY):
        super().__init__()
        self.activity = activity
        self.dumps = ["ERROR: could not get idle state"]

    async def shell(self, serial, cmd):
        if cmd.startswith("dumpsys activity top"):
            self.cmds.append(cmd)
            return self.activity
        return await super().shell(serial, cmd)


@pytest.mark.parametrize("lifecycle_fields", [True, False], ids=["explicit-resumed", "fields-absent"])
async def test_real_visible_postlogin_structure_is_detected_when_xml_is_unavailable(lifecycle_fields):
    activity = MAIN_ACTIVITY if lifecycle_fields else MAIN_ACTIVITY.replace("      mResumed=true mStopped=false mFinished=false\n", "")
    adb = StructureAdb(activity)

    result = await QidianUi(adb=adb).probe_login(acct())

    assert (result.result, result.self_uid) == ("running", UID), "真实主界面有完整可见结构，XML不可用时仍应只读确认登录"
    assert not adb.typed and not adb.taps
    assert not any(cmd.startswith(("am ", "ime ", "input ")) for cmd in adb.cmds)


@pytest.mark.parametrize("changed", [
    "hidden-host", "invisible-host", "hidden-list", "missing-title", "wrong-list-class",
    "other-package", "other-activity", "background-activity", "cross-activity", "cross-tree", "cross-tabhost", "message-text-only",
])
async def test_partial_hidden_or_other_activity_structure_cannot_confirm_login(changed):
    activity = MAIN_ACTIVITY
    if changed == "hidden-host":
        activity = activity.replace("QQTabHost{102 V", "QQTabHost{102 G")
    elif changed == "invisible-host":
        activity = activity.replace("QQTabHost{102 V", "QQTabHost{102 I")
    elif changed == "hidden-list":
        activity = activity.replace("OlympicListView{109 V", "OlympicListView{109 G")
    elif changed == "missing-title":
        activity = activity.replace("app:id/conversation_activity_title", "app:id/unrelated_title")
    elif changed == "wrong-list-class":
        activity = activity.replace("com.tencent.widget.OlympicListView", "android.widget.TextView")
    elif changed == "other-package":
        activity = activity.replace("ACTIVITY com.tencent.qidian/", "ACTIVITY other.app/")
    elif changed == "other-activity":
        activity = activity.replace("activity.SplashActivity", "activity.LoginActivity")
    elif changed == "background-activity":
        activity = activity.replace("mResumed=true mStopped=false", "mResumed=false mStopped=true")
    elif changed == "cross-activity":
        title = next(line for line in activity.splitlines() if "conversation_activity_title" in line)
        activity = activity.replace(title, "") + (
            "\n  ACTIVITY other.app/.OtherActivity abc pid=987\n"
            "    View Hierarchy:\n" + title + "\n")
    elif changed == "cross-tree":
        title = next(line for line in activity.splitlines() if "conversation_activity_title" in line)
        activity = activity.replace(title, "") + "\n    View Hierarchy:\n" + title + "\n"
    elif changed == "cross-tabhost":
        title = next(line for line in activity.splitlines() if "conversation_activity_title" in line)
        activity = activity.replace(title, "") + (
            "\n          com.tencent.mobileqq.widget.QQTabHost{202 VFED..... 0,0-720,1280 android:id/tabhost}\n"
            + title + "\n")
    else:
        activity = "ACTIVITY com.tencent.qidian/com.tencent.mobileqq.activity.SplashActivity\n  text=消息\n"
    adb = StructureAdb(activity)

    result = await QidianUi(adb=adb).probe_login(acct())

    assert result.result is None
    assert not adb.typed and not adb.taps


@pytest.mark.parametrize("xml", [
    LOGIN_TREE,
    tree(node(rid=f"{PKG}:id/dialogRightBtn", text="同意")),
    tree(node(text="短信验证")),
])
async def test_a_valid_xml_login_or_verification_page_is_not_overruled_by_fallback(xml):
    adb = StructureAdb()
    adb.dumps = [xml]

    result = await QidianUi(adb=adb).probe_login(acct())

    assert result.result is None
    assert not any(cmd.startswith("dumpsys activity top") for cmd in adb.cmds)


async def test_fallback_does_not_bind_a_different_uid(manual_rig, monkeypatch):
    adb = StructureAdb()
    adb.metadata = f"1758240000 /data/data/{PKG}/databases/9000000001.db\n"
    monkeypatch.setattr(manual_rig.agent.accounts, "_login_probe_fn", QidianUi(adb=adb).login_probe_fn())

    await probe_tick(manual_rig)

    row = manual_rig.agent.accounts.get("qd01")
    assert (row["state"], row["self_uid"]) == ("login_required", None)
