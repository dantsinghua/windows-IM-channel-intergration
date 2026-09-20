"""微信 M3.5 骨架:登录状态机与取钥时序(05 §2.4.4a)、hosts 屏蔽(04 §2.5.4 B-2)、版本匹配、三张表。"""
from __future__ import annotations

import pytest

from qtrade_winagent.config import ProbeConfig, WechatConfig
from qtrade_winagent.db import Db
from qtrade_winagent.errors import WaError
from qtrade_winagent.fakes import FakeHosts, FakeWeChat
from qtrade_winagent.wechat import (DATA_KEY_WINDOW_S, HOSTS_BLOCK_IP, KEY_FAIL, MATCH_NEWER, MATCH_NOT_INSTALLED,
                                    MATCH_OLDER, MATCH_SUPPORTED, PHASES, WAIT_KEY_IMG, WAIT_KEY_RELOGIN,
                                    WAIT_NARRATOR, WAIT_QRCODE, WeChatHostsBlock, WeChatSession, WeChatStore)

from tests.conftest import Clock


def mk_session(clock=None, **cfgkw):
    clk = clock or Clock()
    wx = FakeWeChat()
    cfg = WechatConfig(enabled=True, **cfgkw)
    return wx, WeChatSession(wx, cfg, clock=clk), clk


# ---------------------------------------------------------------- 相位枚举(R3-3:以 05 为准)
def test_phase_enum_is_the_05_one():
    assert PHASES == ("idle", "narrator", "qrcode", "identified", "keytry", "ready", "key_failed")
    assert "identified" in PHASES                      # 🔴 旧枚举没有这个枢纽相位,轮询方与状态机会对不上


def test_two_key_state_codes_are_separate():
    assert WAIT_KEY_IMG != WAIT_KEY_RELOGIN            # 🔴 两个独立码、不许合成(指示完全不同)
    assert (DATA_KEY_WINDOW_S, ) == (30,)              # data_key 只在该轮**前 30 秒**


# ---------------------------------------------------------------- 登录流程
async def test_login_start_runs_ritual_when_ui_tree_invisible():
    wx, s, _clk = mk_session()
    wx.ui_visible = False
    out = await s.login_start(account_id="wx01")
    assert out["phase"] == "narrator" and s.session.state_code == WAIT_NARRATOR
    assert "narrator_start" in wx.calls and "chatlog_start:wx_key2.dll" not in wx.calls


async def test_login_start_skips_ritual_when_ui_tree_visible():
    """05 §2.4.3:目标是「UI 树可见」,讲述人只是手段 —— 可见就跳过仪式。"""
    wx, s, _clk = mk_session()
    wx.ui_visible = True
    out = await s.login_start(account_id="wx01")
    assert out["phase"] == "qrcode" and s.session.state_code == WAIT_QRCODE
    # 🔴 hook 必须先于登录动作装上
    assert wx.calls.index("chatlog_start:wx_key2.dll") < wx.calls.index("launch")


async def test_ritual_completes_then_hook_then_qrcode():
    clk = Clock()
    wx, s, _ = mk_session(clock=clk)
    wx.ui_visible = False
    await s.login_start()
    wx.wxid = "wxid_abc"                                # 用户扫码登进去了
    clk.advance(300_000)                                # 满 narrator_min_seconds
    wx.ui_visible = True
    await s.poll()
    assert "narrator_stop" in wx.calls and s.session.phase == "qrcode"


async def test_ritual_max_rounds_then_wait_ui_tree():
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, narrator_max_rounds=2)
    wx.ui_visible = False
    await s.login_start()
    wx.wxid = "wxid_abc"
    for _ in range(3):
        clk.advance(300_001)
        await s.poll()
    assert s.session.phase == "key_failed" and s.session.state_code == "WAIT_UI_TREE"


async def test_identified_is_the_pivot_phase_for_bind():
    wx, s, _clk = mk_session()
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    wx.wxid = "wxid_abc"
    st = await s.poll()
    assert st["phase"] == "identified" and st["wxid"] == "wxid_abc"     # Agent 见此调 #33c bind


async def test_key_order_is_img_then_relogin_then_ready():
    """🔴 05 §2.4.4a 顺序:a) 起 hook → b) 打开图片取 img_key → c) 退出重登取 data_key。"""
    clk = Clock()
    wx, s, _ = mk_session(clock=clk)
    wx.ui_visible = True
    await s.login_start()
    wx.wxid = "wxid_abc"
    await s.poll()                                       # → identified
    st = await s.poll()                                  # → keytry / WAIT_KEY_IMG
    assert st["state_code"] == WAIT_KEY_IMG and st["countdown_s"] == 60
    wx.img_key = True
    st = await s.poll()
    assert st["state_code"] == WAIT_KEY_RELOGIN and st["countdown_s"] == 30
    wx.data_key = True
    st = await s.poll()
    assert st["phase"] == "ready" and st["key"]["ok"] is True


async def test_only_data_key_does_not_count_as_success():
    """实测:只拿到 ``data_key`` 会打 WRN 并**整轮作废**;落盘判据是两把**同时**非空。"""
    wx, s, _clk = mk_session()
    wx.data_key, wx.img_key = True, False
    assert wx.key_state()["ok"] is False


async def test_key_round_loops_reinstalling_hook_before_giving_up():
    """超时本轮作废 ⇒ **循环重装 hook 再来**,不能一次失败就判 KEY_FAIL。"""
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, key_retry_per_hour=3)
    wx.ui_visible = True
    await s.login_start()
    wx.wxid = "wxid_abc"
    await s.poll()
    await s.poll()
    wx.img_key = True
    await s.poll()                                       # WAIT_KEY_RELOGIN
    clk.advance(31_000)
    st = await s.poll()
    assert st["state_code"] == WAIT_KEY_IMG and st["key"]["rounds"] == 2       # 重装 hook,换下一把 DLL
    assert "chatlog_start:wx_key1.dll" in wx.calls
    for _ in range(4):                                   # 轮次耗尽后才 KEY_FAIL
        clk.advance(31_000)
        st = await s.poll()
        if st["phase"] == "key_failed":
            break
    assert st["phase"] == "key_failed" and st["state_code"] == KEY_FAIL


async def test_login_cancel_only_cancels_named_session():
    wx, s, _clk = mk_session()
    wx.ui_visible = True
    out = await s.login_start()
    lsid = out["login_session_id"]
    assert (await s.login_cancel(login_session_id="ls_OTHER"))["reason"] == "stale_login_session_id"
    assert (await s.login_cancel(login_session_id=lsid))["cancelled"] is True
    assert wx.logged_in is False                         # 已登进去的不登出(#32)


async def test_logout_stops_chatlog_first():
    """05 §2.4.5 第 2 步:chatlog 的 DLL 注入在 Weixin.exe 里,**先停它再动微信**。"""
    wx, s, _clk = mk_session()
    wx.ui_visible = True
    await s.login_start()
    await s.logout()
    assert wx.calls.index("chatlog_stop") < wx.calls.index("logout:process")


async def test_send_and_read_rejected_without_key():
    """05 §2.4.7:``degraded(KEY_FAIL)`` 下读与写都不开放;写类的中文理由逐字。"""
    wx, s, _clk = mk_session()
    with pytest.raises(WaError) as e:
        await s.send(session_name="文件传输助手", text="hi")
    assert e.value.message == "微信解密不可用,无法确认送达,已拒绝发送"
    with pytest.raises(WaError):
        await s.read()


async def test_send_and_read_with_key_ok():
    wx, s, _clk = mk_session()
    wx.data_key = wx.img_key = True
    out = await s.send(session_name="群A", text="hello", idempotency_key="k1")
    assert out["code"] == "DELIVERED" and out["ext_msg_id"] == "群A:1" and out["idempotency_key"] == "k1"
    r = await s.read(talker="群A")
    assert r["messages"][0]["ext_msg_id"] == "群A:1" and r["next_seq"] == 1


async def test_send_requires_text_or_image():
    wx, s, _clk = mk_session()
    wx.data_key = wx.img_key = True
    with pytest.raises(WaError) as e:
        await s.send(session_name="x")
    assert e.value.reason == "empty_payload"


async def test_screenshot_not_ready_when_locked():
    wx, s, _clk = mk_session()
    with pytest.raises(WaError) as e:
        await s.screenshot(screen_locked=True)
    assert e.value.reason == "screen_locked"
    assert (await s.screenshot()).startswith(b"\x89PNG")


async def test_status_when_module_disabled():
    wx = FakeWeChat()
    s = WeChatSession(wx, WechatConfig(enabled=False))
    st = s.status()
    assert st["enabled"] is False and st["wechat"] is None and st["chatlog"] is None
    with pytest.raises(WaError) as e:
        await s.login_start()
    assert e.value.reason == "wechat_disabled"


# ---------------------------------------------------------------- hosts 屏蔽(B-2 / R6-15)
def mk_hosts(**cfgkw):
    h = FakeHosts()
    return h, WeChatHostsBlock(h, WechatConfig(**cfgkw), ProbeConfig())


def test_hosts_block_writes_one_line_per_domain_with_trailing_marker():
    h, hb = mk_hosts()
    out = hb.apply(True)
    assert out["result"] == "applied"
    lines = [l for l in h.text.splitlines() if "QTrade-wechat-update-block" in l]
    assert len(lines) == 2
    assert lines[0] == "0.0.0.0 dldir1.qq.com  # QTrade-wechat-update-block"
    assert "BEGIN" not in h.text and "END" not in h.text              # 🔴 R6-15:不是 BEGIN/END 围栏块


def test_hosts_block_removal_matches_by_marker_not_domain():
    h, hb = mk_hosts()
    h.text += "0.0.0.0 dldir1.qq.com\n"                                # 用户自己写的同域名行(无标记)
    hb.apply(True)
    hb.apply(False)
    assert "QTrade-wechat-update-block" not in h.text
    assert "0.0.0.0 dldir1.qq.com" in h.text                           # 按标记匹配 ⇒ 用户那行原样保留


def test_hosts_block_is_idempotent():
    h, hb = mk_hosts()
    hb.apply(True)
    first = h.text
    hb.apply(True)
    assert h.text == first


def test_hosts_block_refuses_mmtls_domains():
    """🔴 写前与 ``[probe] wechat_hosts`` 做交集检查,相交拒写 —— 拦了长短连接就收不到消息。"""
    h = FakeHosts()
    hb = WeChatHostsBlock(h, WechatConfig(update_block_domains=("long.weixin.qq.com",)), ProbeConfig())
    with pytest.raises(WaError) as e:
        hb.apply(True)
    assert e.value.reason == "domain_is_mmtls"


def test_hosts_block_blocked_by_policy_when_readonly():
    h, hb = mk_hosts()
    h.can_write = False
    out = hb.apply(True)
    assert out["result"] == "blocked_by_policy" and out["alert"] == "H21_WECHAT_HOSTS_BLOCK_FAILED"


def test_hosts_block_detects_resolution_still_real():
    h, hb = mk_hosts()
    h.resolved = {"dldir1.qq.com": "1.2.3.4"}

    def resolve(domain):                                               # 写进去了但解析仍返回真实 IP
        return "1.2.3.4"
    h.resolve = resolve                                                # type: ignore[method-assign]
    out = hb.apply(True)
    assert out["result"] == "blocked_by_policy"


def test_hosts_block_state_feeds_status():
    h, hb = mk_hosts()
    assert hb.state()["enabled"] is False
    hb.apply(True)
    st = hb.state()
    assert st["enabled"] is True and set(st["domains"]) == {"dldir1.qq.com", "dldir1v6.qq.com"}
    assert HOSTS_BLOCK_IP == "0.0.0.0"


# ---------------------------------------------------------------- 三张表与版本匹配
def test_bind_is_idempotent_and_validates_wxNN():
    db = Db(":memory:").open()
    st = WeChatStore(db)
    assert st.bind(wxid="wxid_a", account_id="wx01")["ok"] is True
    assert st.bind(wxid="wxid_a", account_id="wx01")["ok"] is True      # 同 wxid 重绑幂等
    with pytest.raises(WaError) as e:
        st.bind(wxid="wxid_b", account_id="wx1")
    assert e.value.reason == "bad_account_id"
    with pytest.raises(WaError) as e2:
        st.bind(wxid="wxid_b", account_id="wx01")
    assert e2.value.reason == "account_id_taken"
    db.close()


def test_record_login_updates_profile_counters():
    db = Db(":memory:").open()
    st = WeChatStore(db)
    st.record_login(wxid="wxid_a", account_id="wx01", nickname="老王", wechat_version="4.1.12.26",
                    wxkey_dll="wx_key2.dll")
    st.record_login(wxid="wxid_a", account_id="wx01")
    p = st.profiles()[0]
    assert p["login_count"] == 2 and p["nickname"] == "老王" and p["last_wxkey_dll"] == "wx_key2.dll"
    assert p["first_login_ms"] is not None
    db.close()


# ---------------------------------------------------------------- main_wnd_class(R6-58 (at))
def test_main_wnd_class_falls_back_to_config_when_row_is_null():
    """②取用顺序:该列(实测值)为 NULL 时回落 ``[wechat] main_wnd_class`` 配置默认;wxid 未知时同样回落。"""
    db = Db(":memory:").open()
    st = WeChatStore(db)
    st.bind(wxid="wxid_a", account_id="wx01")
    assert st.profiles()[0]["main_wnd_class"] is None
    assert st.effective_main_wnd_class("wxid_a", default="Qt51514QWindowIcon") == "Qt51514QWindowIcon"
    assert st.effective_main_wnd_class(None, default="Qt51514QWindowIcon") == "Qt51514QWindowIcon"
    assert st.effective_main_wnd_class("wxid_never_bound", default="Qt51514QWindowIcon") == "Qt51514QWindowIcon"
    db.close()


def test_main_wnd_class_row_value_overrides_config_once_measured():
    """②取用顺序:该 wxid 实测值(本列非 NULL)优先于配置默认。"""
    db = Db(":memory:").open()
    st = WeChatStore(db)
    st.bind(wxid="wxid_a", account_id="wx01")
    st.record_main_wnd_class("wxid_a", "WeChatMainWndForPC")           # 该 wxid 实测到(如 3.x)的类名
    assert st.effective_main_wnd_class("wxid_a", default="Qt51514QWindowIcon") == "WeChatMainWndForPC"
    assert st.profiles()[0]["main_wnd_class"] == "WeChatMainWndForPC"
    db.close()


def test_record_main_wnd_class_ignores_empty_value_and_unbound_wxid():
    db = Db(":memory:").open()
    st = WeChatStore(db)
    st.bind(wxid="wxid_a", account_id="wx01")
    st.record_main_wnd_class("wxid_a", None)                          # 没测到不写,不把列打回 NULL / 不覆盖已有实测值
    assert st.profiles()[0]["main_wnd_class"] is None
    st.record_main_wnd_class("wxid_never_bound", "Qt51514QWindowIcon")  # 未 bind 过的 wxid:静默无操作,不凭空建行
    assert len(st.profiles()) == 1
    db.close()


def test_main_window_class_name_detected_via_existing_backend_protocol_writes_through():
    """①经现有 ``WeChatBackend.main_window()`` 协议探测(Fake 可编程),把该 wxid 实测到的类名写回 ``wechat_profiles``。"""
    wx, s, _clk = mk_session()
    wx.running_pid = 5101
    wx.window_class = "Qt51514QWindowIcon"                            # 测试可编程的「实测值」
    win = wx.main_window()
    assert win["class_name"] == "Qt51514QWindowIcon"
    st_status = s.status()
    assert st_status["wechat"]["main_wnd_class"] == "Qt51514QWindowIcon"   # status() 顺带探测出来

    db = Db(":memory:").open()
    st = WeChatStore(db)
    st.bind(wxid="wxid_a", account_id="wx01")
    st.record_main_wnd_class("wxid_a", st_status["wechat"]["main_wnd_class"])
    assert st.profiles()[0]["main_wnd_class"] == "Qt51514QWindowIcon"
    db.close()


def test_main_window_class_name_is_none_when_window_absent():
    wx, s, _clk = mk_session()
    assert wx.main_window()["class_name"] is None
    assert s.status()["wechat"]["main_wnd_class"] is None


def test_version_match_all_branches():
    db = Db(":memory:").open()
    st = WeChatStore(db)
    assert st.version_match(bundled_version="4.1.12.26")["match"] == MATCH_NOT_INSTALLED
    st.put_install(version="4.1.12.26", path="D:\\Weixin.exe")
    assert st.version_match(bundled_version="4.1.12.26")["match"] == MATCH_SUPPORTED
    st.put_install(version="4.1.13.12")
    m = st.version_match(bundled_version="4.1.12.26")
    assert m["match"] == MATCH_NEWER and m["action"] == "reinstall_bundled"
    st.put_install(version="4.1.11.52")
    assert st.version_match(bundled_version="4.1.12.26")["match"] == MATCH_OLDER
    assert st.version_match(bundled_version="4.1.12.26", multiple_installs=True)["match"] == "MULTIPLE_INSTALLS"
    db.close()


def test_matrix_verified_row_wins_over_version_compare():
    """矩阵里有 ``verified`` 记录时优先该 DLL(02 §7.2 ``wxkey_dlls`` 注)。"""
    db = Db(":memory:").open()
    st = WeChatStore(db)
    st.put_install(version="4.1.13.12")
    st.put_matrix(version="4.1.13.12", dll="wx_key2.dll", status="verified", source="runtime")
    m = st.version_match(bundled_version="4.1.12.26")
    assert m["match"] == MATCH_SUPPORTED and m["dll"] == "wx_key2.dll" and m["status"] == "verified"
    with pytest.raises(WaError):
        st.put_matrix(version="x", dll=None, status="不存在", source="bundled")
    db.close()
