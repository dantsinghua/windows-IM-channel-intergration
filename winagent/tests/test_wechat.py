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
async def test_login_start_goes_straight_to_key_extraction_even_if_ui_tree_invisible():
    """R6-93(2026-10-10 安琳 + 真机):取钥是内存 hook,不依赖 UI 树;默认(auto)**不做阻塞的讲述人仪式**,
    UI 树不可见也直接起 hook 进 WAIT_QRCODE —— 此前在这里死等 2×5 分钟再判 WAIT_UI_TREE,永远到不了取钥。"""
    wx, s, _clk = mk_session()
    wx.ui_visible = False
    out = await s.login_start(account_id="wx01")
    assert out["phase"] == "qrcode" and s.session.state_code == WAIT_QRCODE
    assert "narrator_start" not in wx.calls and "chatlog_start:wx_key2.dll" in wx.calls


async def test_login_start_runs_ritual_only_when_explicitly_always():
    """仪式机制保留给显式 narrator_ritual="always"(老版本 / 兜底)。"""
    wx, s, _clk = mk_session(narrator_ritual="always")
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
    # 🔴 hook 必须先于登录动作(用户扫码 / 点「进入微信」)装上;R6-96:微信没开时先拉起(停在登录页),hook 才有进程可挂
    assert wx.calls[:2] == ["launch", "chatlog_start:wx_key2.dll"]
    assert wx.wxid is None                                # 至此用户还没有登录动作


async def test_login_start_hooks_running_wechat_without_relaunch_first():
    """R6-96:微信已在跑(前台或缩在托盘)⇒ 不先拉起,直接挂 hook。"""
    wx, s, _clk = mk_session()
    wx.running_pid = 23424
    await s.login_start(account_id="wx01")
    assert wx.calls[0] == "chatlog_start:wx_key2.dll"


async def test_keytry_restarts_round_when_key_process_exits_without_keys():
    """R6-96(2026-10-10 真机):key 进程已退出(DLL 没挂上 / 扫描到点)而两钥未落盘 ⇒ 不能停在 WAIT_KEY_IMG 干等,
    自动重开一轮并带上原因;到 key_retry_per_hour 判 KEY_FAIL。"""
    wx, s, _clk = mk_session()
    wx.running_pid, wx.wxid = 23424, "wxid_abc"
    await s.key_retry()
    assert s.session.state_code == WAIT_KEY_IMG and s.session.key_rounds == 1
    wx.chatlog_pid, wx.key_error = None, "取钥 DLL 没能挂上微信(模式匹配失败)"
    st = await s.poll()
    assert st["state_code"] == WAIT_KEY_IMG and st["key"]["rounds"] == 2
    assert "模式匹配失败" in st["key"]["error"] and wx.chatlog_pid is not None
    for _ in range(2):
        wx.chatlog_pid = None
        st = await s.poll()
    assert st["phase"] == "key_failed" and st["state_code"] == KEY_FAIL and "模式匹配失败" in st["key"]["error"]


async def test_key_retry_launches_wechat_when_not_running():
    """R6-96:微信没开(从没登过 / 被关)时重试取钥先把它拉起来,否则 hook 没进程可挂。"""
    wx, s, _clk = mk_session()
    await s.key_retry()
    assert wx.calls.index("launch") < wx.calls.index("chatlog_start:wx_key2.dll")


async def test_send_wakes_window_hidden_in_tray():
    """R6-96:微信缩在托盘时主窗口隐藏,UIA 找不到 —— 发送前先唤出窗口,不误判成「UI 树不可见」。"""
    wx, s, _clk = mk_session()
    wx.running_pid, wx.window_visible, wx.ui_visible = 23424, False, True
    wx.data_key = wx.img_key = True
    orig_launch = wx.launch

    async def show() -> int:
        wx.window_visible = True
        return await orig_launch()
    wx.launch = show                                      # type: ignore[method-assign]
    wx.sessions = [{"userName": "wxid_peer", "nickName": "peer"}]
    await s.send(session_name="wxid_peer", text="hi")
    assert wx.window_visible


async def test_ritual_completes_then_hook_then_qrcode():
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, narrator_ritual="always")
    wx.ui_visible = False
    await s.login_start()
    wx.wxid = "wxid_abc"                                # 用户扫码登进去了
    clk.advance(300_000)                                # 满 narrator_min_seconds
    wx.ui_visible = True
    await s.poll()
    assert "narrator_stop" in wx.calls and s.session.phase == "qrcode"


async def test_ritual_unkillable_narrator_hands_stop_to_service_then_continues():
    """R6-89(2026-10-10 真机):讲述人跑在高完整性级别,会话代理 taskkill 拒绝访问 ⇒ 状态机不卡死:
    打 `narrator.stop_pending` 交给服务提权结束;下一轮看到它已停,再按 ④ 探可见性继续。"""
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, narrator_ritual="always")
    wx.ui_visible = False
    wx.narrator_unkillable = True
    await s.login_start()
    wx.wxid = "wxid_abc"
    clk.advance(300_000)
    wx.ui_visible = True
    await s.poll()
    st = s.login_status()
    assert s.session.phase == "narrator" and st["narrator"]["stop_pending"] is True
    assert wx.calls.count("narrator_stop") == 1
    await s.poll()                                       # 服务还没杀 ⇒ 仍等待,不重复 taskkill、不加轮次
    assert s.session.phase == "narrator" and wx.calls.count("narrator_stop") == 1 and s.session.narrator_rounds == 1
    wx.kill_narrator_elevated()                          # 服务侧提权结束了
    await s.poll()
    assert s.session.phase == "qrcode" and s.login_status()["narrator"]["stop_pending"] is False


async def test_cancel_reports_narrator_stop_pending_when_unkillable():
    wx, s, _ = mk_session(narrator_ritual="always")
    wx.ui_visible = False
    wx.narrator_unkillable = True
    await s.login_start()
    out = await s.login_cancel()
    assert out["cancelled"] is True and out["narrator_stop_pending"] is True


async def test_ritual_early_probe_finishes_without_waiting_full_min():
    """R6-90(安琳):已登录 + UI 树可见 + 满短驻留(60s)⇒ 提前试关讲述人;关了仍可见就直接进下一步,不再死等 300s。"""
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, narrator_ritual="always")
    wx.ui_visible = False
    await s.login_start()
    wx.wxid = "wxid_abc"
    wx.ui_visible = True
    clk.advance(30_000)
    await s.poll()
    assert s.session.phase == "narrator" and "narrator_stop" not in wx.calls       # 不足短驻留,不动
    clk.advance(31_000)                                                             # 共 61s
    await s.poll()
    assert "narrator_stop" in wx.calls and s.session.phase == "qrcode" and s.session.narrator_rounds == 1


async def test_ritual_early_probe_failure_does_not_burn_rounds():
    """提前探测「开着可见、关了不可见」⇒ 重开讲述人、**不扣轮次**,之后必须满 min 才再关(防几秒耗光 max_rounds)。"""
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, narrator_max_rounds=2, narrator_ritual="always")
    real_stop = wx.narrator_stop

    async def stop_and_hide() -> bool:
        wx.ui_visible = False                                       # 关掉讲述人后 UI 树又不可见(缓存没建立)
        return await real_stop()
    wx.narrator_stop = stop_and_hide                                 # type: ignore[method-assign]
    wx.ui_visible = False
    await s.login_start()
    wx.wxid = "wxid_abc"
    wx.ui_visible = True
    clk.advance(61_000)
    await s.poll()
    assert s.session.phase == "narrator" and s.session.narrator_rounds == 1 and s.session.narrator_early_tried
    assert wx.calls.count("narrator_start") == 2                    # 已重开
    wx.ui_visible = True
    clk.advance(61_000)
    await s.poll()
    assert wx.calls.count("narrator_stop") == 1                     # 已试过提前,不足满 min 不再关
    clk.advance(240_000)                                            # 重开后满 300s
    wx.narrator_stop = real_stop                                     # type: ignore[method-assign]
    await s.poll()
    assert s.session.phase == "qrcode" and s.session.narrator_rounds == 1


async def test_login_status_reports_key_stage_for_agent():
    """02 #33 R6-58 (ak):`key.stage` 是 Agent 区分 WAIT_KEY_IMG / WAIT_KEY_RELOGIN 的唯一手段,必须随状态给出。"""
    wx, s, _ = mk_session()
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    s._set_phase(s.session, "keytry", WAIT_KEY_IMG)                 # noqa: SLF001
    assert s.login_status()["key"]["stage"] == "img"
    s._set_phase(s.session, "keytry", WAIT_KEY_RELOGIN)             # noqa: SLF001
    assert s.login_status()["key"]["stage"] == "relogin"


def test_stage_wx_key_dll_places_dll_where_chatlog_loads_it(tmp_path):
    """R6-90:chatlog 只认 `<cwd>/lib/windows_x64/wx_key.dll`(实测;`key` 没有 `--dll` 参数)。"""
    from qtrade_winagent.win.wechat import WX_KEY_REL, stage_wx_key_dll
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "wx_key2.dll").write_bytes(b"MZ-v2")
    (src / "wx_key1.dll").write_bytes(b"MZ-v1")
    work = tmp_path / "work"
    out = stage_wx_key_dll(str(src), str(work), "wx_key2.dll")
    assert out == str(work / WX_KEY_REL) and (work / "lib" / "windows_x64" / "wx_key.dll").read_bytes() == b"MZ-v2"
    stage_wx_key_dll(str(src), str(work), "wx_key1.dll")              # 换 DLL 重试 = 覆盖同一位置
    assert (work / "lib" / "windows_x64" / "wx_key.dll").read_bytes() == b"MZ-v1"
    with pytest.raises(FileNotFoundError):
        stage_wx_key_dll(str(src), str(work), "wx_key9.dll")


def test_redact_keys_masks_both_keys_but_keeps_short_hex():
    """chatlog 把两把钥明文打进日志 ⇒ 落盘前必须打码;函数地址这类短串保留以便排障。"""
    from qtrade_winagent.win.wechat import redact_keys
    line = 'server config: &{DataKey:' + "ab" * 32 + ' ImgKey:' + "cd" * 16 + '} addr=0x7ffa94e4bf4c'
    out = redact_keys(line)
    assert "ab" * 32 not in out and "cd" * 16 not in out and out.count("<redacted>") == 2
    assert "0x7ffa94e4bf4c" in out
    assert redact_keys("Data Key : " + "7" * 64) == "Data Key : <redacted>"


def test_read_chatlog_keys_requires_both(tmp_path):
    from qtrade_winagent.win.wechat import read_chatlog_keys
    p = tmp_path / "chatlog.json"
    p.write_text('{"history":[{"data_key":"' + "a" * 64 + '","img_key":""}]}', encoding="utf-8")
    assert read_chatlog_keys(str(p)) is None                         # 只有一把 = 不能起 server
    p.write_text('{"history":[{"data_key":"' + "a" * 64 + '","img_key":"' + "b" * 32 + '"}]}', encoding="utf-8")
    assert read_chatlog_keys(str(p)) == {"data_key": "a" * 64, "img_key": "b" * 32}
    assert read_chatlog_keys(str(tmp_path / "missing.json")) is None


def test_discover_chatlog_data_dir_probes_real_layout(tmp_path):
    """兼容 v4(db_storage)/ v3(Msg)/ 未知布局:按真实 .db 探测,都不中回退账号目录让 chatlog 自己找。"""
    from qtrade_winagent.win.wechat import discover_chatlog_data_dir
    root = tmp_path / "Saved Files"
    acct = root / "xwechat_files" / "wxid_abc"
    (acct / "db_storage" / "message").mkdir(parents=True)
    (acct / "db_storage" / "message" / "message_0.db").write_bytes(b"x")
    assert discover_chatlog_data_dir(str(root), "wxid_abc") == str(acct / "db_storage")
    acct2 = root / "xwechat_files" / "wxid_old"
    (acct2 / "Msg").mkdir(parents=True)
    (acct2 / "Msg" / "MSG0.db").write_bytes(b"x")
    assert discover_chatlog_data_dir(str(root), "wxid_old") == str(acct2 / "Msg")
    acct3 = root / "xwechat_files" / "wxid_new"
    acct3.mkdir(parents=True)
    assert discover_chatlog_data_dir(str(root), "wxid_new") == str(acct3)          # 未知布局:交给 chatlog
    assert discover_chatlog_data_dir(str(root), "wxid_absent") is None
    assert discover_chatlog_data_dir(None, "wxid_abc") is None


def test_build_server_config_keeps_keys_out_of_argv():
    from qtrade_winagent.win.wechat import build_server_config
    cfg = build_server_config(data_dir="D", work_dir="W", keys={"data_key": "k1", "img_key": "k2"}, addr="127.0.0.1:5030")
    assert cfg == {"http_addr": "127.0.0.1:5030", "data_dir": "D", "work_dir": "W", "platform": "windows",
                   "version": 4, "data_key": "k1", "img_key": "k2", "auto_decrypt": True}


def test_chatlog_time_range_format():
    import datetime as dt
    from qtrade_winagent.win.wechat import chatlog_time_range
    assert chatlog_time_range(2, today=dt.date(2026, 10, 10)) == "2026-10-09~2026-10-10"
    assert chatlog_time_range(1, today=dt.date(2026, 1, 1)) == "2026-01-01~2026-01-01"


def test_read_paths_always_ask_json_and_send_time_talker():
    """回归(R6-91 实测):不带 format=json 回 CSV;/chatlog 缺 time 或 talker 回 400;H09 不再探会因空号 404 的 /session。"""
    import inspect
    from qtrade_winagent.win import wechat as winwx
    get_items = inspect.getsource(winwx.WinWeChat._get_items)
    assert '"format": "json"' in get_items and "404" in get_items
    rd = inspect.getsource(winwx.WinWeChat.read_messages)
    assert '"time"' in rd and '"talker"' in rd
    st = inspect.getsource(winwx.WinWeChat.chatlog_status)
    assert "/health" in st and "/api/v1/session" not in st.split("R6-91")[-1].split("\n", 3)[-1]


async def test_keys_ok_starts_server_before_ready():
    """R6-91:两钥落盘 ⇒ 先停 key 进程、起 chatlog server,再 ready;此前直接 ready,读路径全断。"""
    wx, s, _ = mk_session()
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    s._set_phase(s.session, "keytry", WAIT_KEY_IMG)                 # noqa: SLF001
    wx.data_key = wx.img_key = True
    await s.poll()
    assert s.session.phase == "ready" and "chatlog_serve" in wx.calls
    assert wx.calls.index("chatlog_serve") > max(i for i, c in enumerate(wx.calls) if c == "chatlog_stop")


async def test_keys_ok_but_server_fails_is_not_fake_ready():
    wx, s, _ = mk_session()
    wx.ui_visible = True
    wx.serve_fails = True
    await s.login_start(account_id="wx01")
    s._set_phase(s.session, "keytry", WAIT_KEY_IMG)                 # noqa: SLF001
    wx.data_key = wx.img_key = True
    await s.poll()
    assert s.session.phase == "key_failed" and "读服务没起来" in (s.session.key_error or "")


async def test_ensure_server_self_heals_after_reboot_and_throttles():
    """开机 / 会话代理重启 / chatlog 崩溃:两钥在 + 已登录 + server 没跑 ⇒ 自动拉起;30 s 节流;登录流进行中不插手。"""
    clk = Clock()
    wx, s, _ = mk_session(clock=clk)
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_abc"
    wx.chatlog_pid = None
    assert await s.ensure_server() is True and wx.calls.count("chatlog_serve") == 1
    wx.chatlog_pid = None                                            # 又崩了
    assert await s.ensure_server() is False                          # 节流期内不连拉
    clk.advance(31_000)
    assert await s.ensure_server() is True and wx.calls.count("chatlog_serve") == 2
    wx.chatlog_pid = None
    clk.advance(31_000)
    wx.ui_visible = False
    await s.login_start(account_id="wx02")                           # 登录流在讲述人相位
    assert await s.ensure_server() is False
    wx.data_key = False                                              # 没钥不起
    s.session = None
    clk.advance(31_000)
    assert await s.ensure_server() is False


def test_resolve_send_target_uses_remark_then_nickname():
    """R6-92:pyweixin 的 friend 是显示名(备注优先,否则昵称/群名),不是 wxid。"""
    from qtrade_winagent.win.wechat import resolve_send_target
    contacts = [{"UserName": "wxid_a", "Alias": "zs", "Remark": "张三-交易", "NickName": "张三"},
                {"UserName": "wxid_b", "Alias": "", "Remark": "", "NickName": "李四"}]
    rooms = [{"Name": "123@chatroom", "Remark": "", "NickName": "债券群", "Owner": "wxid_a", "UserCount": 9}]
    assert resolve_send_target("wxid_a", contacts, rooms) == ("张三-交易", "")
    assert resolve_send_target("wxid_b", contacts, rooms) == ("李四", "")
    assert resolve_send_target("123@chatroom", contacts, rooms) == ("债券群", "")
    name, why = resolve_send_target("wxid_x", contacts, rooms)
    assert name == "" and "wxid_x" in why
    assert resolve_send_target("wxid_c", [{"userName": "wxid_c", "remark": "小写键也认"}], [])[0] == "小写键也认"


def test_display_name_collisions_blocks_ambiguous_target():
    """重名(另一个联系人的备注/昵称/微信号 或 同名群)⇒ 拒发,防止 pyweixin 按名字搜到别人。"""
    from qtrade_winagent.win.wechat import display_name_collisions
    rows = [{"UserName": "wxid_a", "Remark": "张三", "NickName": "x"},
            {"UserName": "wxid_dup", "Remark": "", "NickName": "张三"},
            {"Name": "9@chatroom", "NickName": "张三"},
            {"UserName": "wxid_other", "Remark": "张三丰", "NickName": "y"}]
    assert sorted(display_name_collisions("wxid_a", "张三", rows)) == ["9@chatroom", "wxid_dup"]
    assert display_name_collisions("wxid_a", "张三", rows[:1] + rows[3:]) == []        # 张三丰 ≠ 张三


def test_pick_readback_needs_self_after_send_and_text_match():
    from qtrade_winagent.win.wechat import pick_readback
    t0 = 1_790_000_000.0
    rows = [{"seq": 5, "isSelf": True, "content": "求个 ofr", "time": t0 - 60},      # 发送前的旧消息
            {"seq": 6, "isSelf": False, "content": "求个 ofr", "time": t0 + 1},      # 对方发的
            {"seq": 7, "isSelf": True, "content": "求个 ofr", "time": "2026-09-21T22:13:21+08:00"}]
    assert pick_readback(rows, text="求个 ofr", image=False, since_s=t0 - 2) is None or \
        pick_readback(rows, text="求个 ofr", image=False, since_s=t0 - 2)["seq"] == 7
    rows2 = [{"seq": 9, "isSelf": True, "content": "求个 ofr", "time": t0 + 3}]
    assert pick_readback(rows2, text="求个 ofr", image=False, since_s=t0 - 2)["seq"] == 9
    assert pick_readback(rows2, text="别的话", image=False, since_s=t0 - 2) is None
    img = [{"seq": 11, "isSelf": True, "type": 3, "content": "", "time": (t0 + 2) * 1000}]
    assert pick_readback(img, text=None, image=True, since_s=t0 - 2)["seq"] == 11


def test_send_no_longer_uses_nonexistent_weixinclient():
    """回归:上游 pyweixin 没有 WeixinClient;必须走 Messages/Files,显式 close_weixin=False,且先解析显示名。"""
    import inspect
    from qtrade_winagent.win import wechat as winwx
    src = inspect.getsource(winwx.WinWeChat.send)
    assert "WeixinClient" not in src.split('"""', 2)[-1]
    assert "send_messages_to_friend" in src and "send_files_to_friend" in src
    assert src.count("close_weixin=False") >= 2 and "_resolve_send_target" in src


def test_chatlog_start_passes_pid_to_avoid_interactive_picker():
    """R6-93 真机:不带 --pid 的 `chatlog key` 会掉进交互式进程选择器(多个 Weixin.exe 时)并挂住;源码层面钉死带 --pid。"""
    import inspect
    from qtrade_winagent.win import wechat as winwx
    src = inspect.getsource(winwx.WinWeChat.chatlog_start)
    assert '"--pid"' in src and "login_pid()" in src
    assert "main_window()" in inspect.getsource(winwx.WinWeChat.login_pid)


# 2026-10-10 真机进程树:根 23424 + 一串 --type= 子进程;另一个未登录辅助根 36000(无子进程)
_WX_TREE = [
    {"pid": 23424, "ppid": 15612, "cmdline": "Weixin.exe", "rss": 444e6},
    {"pid": 44680, "ppid": 23424, "cmdline": 'Weixin.exe --user-lib-dir="D:\\x" --type=w', "rss": 92e6},
    {"pid": 57668, "ppid": 23424, "cmdline": "Weixin.exe --type=w", "rss": 30e6},
    {"pid": 21328, "ppid": 23424, "cmdline": "Weixin.exe --type=xplayer", "rss": 42e6},
    {"pid": 36000, "ppid": 15612, "cmdline": "Weixin.exe", "rss": 60e6},
]


@pytest.mark.parametrize("window_pid", [None, 23424, 44680, 21328])
def test_pick_wechat_root_pid_targets_login_main_process(window_pid):
    """R6-96:托盘态(无窗口 pid)、窗口 pid 落在子进程上,都要挂到登录主进程 —— 挂子进程必「模式匹配失败,0 个结果」。"""
    from qtrade_winagent.win.wechat import pick_wechat_root_pid
    assert pick_wechat_root_pid(_WX_TREE, window_pid) == 23424


def test_pick_wechat_root_pid_edge_cases():
    from qtrade_winagent.win.wechat import pick_wechat_root_pid
    assert pick_wechat_root_pid([], None) is None                       # 微信没开
    assert pick_wechat_root_pid([], 777) == 777                         # 枚举失败时退回窗口 pid
    assert pick_wechat_root_pid(_WX_TREE[-1:], None) == 36000           # 只有一个进程


def test_parse_key_progress_reads_real_chatlog_markers():
    """R6-96:chatlog 两钥不同轮不写 chatlog.json,进度只能看日志;样本取自 9-18 成功日志与 10-10 失败日志原文。"""
    from qtrade_winagent.win.wechat import parse_key_progress
    ok = parse_key_progress("DBG [DLL SUCCESS] Hook安装成功，现在登录微信...\n"
                            "INF 通过 内存扫描 获取到图片密钥\n")
    assert ok["hook"] and ok["img_key"] and not ok["data_key"] and ok["error"] is None
    bad = parse_key_progress('ERR failed to get key error="初始化DLL失败: 模式匹配失败，找到 0 个结果"\n')
    assert not bad["hook"] and "模式匹配失败" in bad["error"]
    half = parse_key_progress("WRN 30秒轮询结束，已获取数据库密钥，但未获取到图片密钥\n")
    assert half["data_key"] and not half["img_key"] and "图片密钥" in half["error"]
    # R6-96 真机(22:36 挂到未登录实例 60220 的日志原文):明确报「未登录 / 数据目录未就绪」与「获取密钥超时」
    nolog = parse_key_progress("INF 微信进程存在但未登录，将尝试初始化DLL，请登录微信后操作\n"
                               "DBG [DLL SUCCESS] Hook安装成功\n"
                               "ERR 获取密钥超时（30秒）！\n")
    assert nolog["not_logged_in"] and nolog["hook"] and "未登录" in nolog["error"]
    timeout = parse_key_progress("DBG [DLL SUCCESS] Hook安装成功\nERR 获取密钥超时（30秒）！可能的原因\n")
    assert timeout["hook"] and not timeout["not_logged_in"] and "超时" in timeout["error"]
    noise = parse_key_progress("DBG 内存扫描结束，共检查了 903 个候选图片密钥字符串\n"
                               "INF 正在进行第 1 轮内存扫描... 请打开任意图片以触发密钥加载\n")
    assert not noise["img_key"] and noise["error"] is None


def test_chatlog_stop_unloads_hook_before_force_kill():
    """R6-96 真机实锤:``taskkill /F`` 强杀正在 hook 的 chatlog 会把 inline hook 残留在微信进程里,
    之后取钥全「模式匹配失败,0 个结果」。stop 必须先 CTRL_BREAK 优雅卸载,再兜底强杀;spawn 必须 NEW_PROCESS_GROUP。"""
    import inspect
    from qtrade_winagent.win import wechat as winwx
    stop_src = inspect.getsource(winwx.WinWeChat.chatlog_stop)
    assert "CTRL_BREAK_EVENT" in stop_src
    assert stop_src.index("CTRL_BREAK_EVENT") < stop_src.index("force=True")   # 先优雅,后兜底强杀
    assert "CREATE_NEW_PROCESS_GROUP" in inspect.getsource(winwx.spawn_redacted)


def test_decode_line_handles_gbk_console_output():
    """真机:chatlog 管道输出是 GBK,按 UTF-8 解码全成 ``���``,进度标记丢失。"""
    from qtrade_winagent.win.wechat import decode_line
    assert decode_line("Hook安装成功".encode("gbk")) == "Hook安装成功"
    assert decode_line("Hook安装成功".encode("utf-8")) == "Hook安装成功"


def test_chatlog_start_never_passes_unsupported_dll_flag():
    """回归:旧实现 `chatlog.exe key --dll <x>` 在真机秒退(unknown flag)。源码层面钉死不再出现 `--dll`。"""
    import inspect
    from qtrade_winagent.win import wechat as winwx
    src = inspect.getsource(winwx.WinWeChat.chatlog_start)
    assert '"--dll"' not in src and "stage_wx_key_dll" in src


async def test_ritual_max_rounds_then_wait_ui_tree():
    clk = Clock()
    wx, s, _ = mk_session(clock=clk, narrator_max_rounds=2, narrator_ritual="always")
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
    await s.poll()                                       # 重新装 hook,倒计时从这一下开始
    clk.advance(31_000)
    wx.chatlog_pid = None
    st = await s.poll()
    assert st["state_code"] == WAIT_KEY_RELOGIN and st["key"]["rounds"] == 2
    assert "chatlog_start:wx_key2.dll" in wx.calls
    assert "wx_key1.dll" not in wx.calls[-1]
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
    wx.ui_visible = True                                  # R6-93:发送(pyweixin 界面自动化)要求 UI 树可见
    out = await s.send(session_name="群A", text="hello", idempotency_key="k1")
    assert out["code"] == "DELIVERED" and out["ext_msg_id"] == "群A:1" and out["idempotency_key"] == "k1"
    r = await s.read(talker="群A")
    assert r["messages"][0]["ext_msg_id"] == "群A:1" and r["next_seq"] == 1


async def test_text_send_uses_keyboard_fallback_when_ui_tree_invisible():
    """微信 4.1 主窗口控件树经常是空的。纯文本仍发送(快捷键),图片才拒绝;读消息照常。"""
    wx, s, _clk = mk_session()
    wx.data_key = wx.img_key = True
    wx.ui_visible = False
    wx.sessions = [{"userName": "群A", "nickName": "群A"}]
    out = await s.send(session_name="群A", text="hello", idempotency_key="k1")
    assert out["code"] == "DELIVERED" and any(c.startswith("send:") for c in wx.calls)
    with pytest.raises(WaError) as e:
        await s.send(session_name="群A", image_path="a.png")
    assert e.value.reason == "ui_tree_invisible"
    r = await s.read(talker="群A")
    assert r["messages"]


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


def test_effective_main_wnd_class_for_account_resolves_via_wxid_lookup():
    """②取用顺序的 account_id 版(``login/start`` 只有 account_id):按 account_id 找到 wxid 再取行值。"""
    db = Db(":memory:").open()
    st = WeChatStore(db)
    assert st.wxid_for_account("wx01") is None
    assert st.effective_main_wnd_class_for_account("wx01", default="Qt51514QWindowIcon") == "Qt51514QWindowIcon"
    assert st.effective_main_wnd_class_for_account(None, default="Qt51514QWindowIcon") == "Qt51514QWindowIcon"
    st.bind(wxid="wxid_a", account_id="wx01")
    assert st.wxid_for_account("wx01") == "wxid_a"
    assert st.effective_main_wnd_class_for_account("wx01", default="Qt51514QWindowIcon") == "Qt51514QWindowIcon"
    st.record_main_wnd_class("wxid_a", "WeChatMainWndForPC")
    assert st.effective_main_wnd_class_for_account("wx01", default="Qt51514QWindowIcon") == "WeChatMainWndForPC"
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


def test_detected_main_wnd_class_helper_mirrors_main_window():
    """服务侧调度用的只读探测口:经现有 ``main_window()`` 协议,不碰 DB。"""
    wx, s, _clk = mk_session()
    assert s.detected_main_wnd_class() is None
    wx.running_pid, wx.window_class = 5101, "Qt51514QWindowIcon"
    assert s.detected_main_wnd_class() == "Qt51514QWindowIcon"


async def test_login_start_applies_main_wnd_class_override_before_ritual():
    """②取用顺序落地处:``login_start(main_wnd_class=...)`` 先把它应用到后端,再进入状态机。"""
    wx, s, _clk = mk_session()
    wx.ui_visible = True
    await s.login_start(account_id="wx01", main_wnd_class="WeChatMainWndForPC")
    assert wx.search_class == "WeChatMainWndForPC"
    assert "set_main_wnd_class:WeChatMainWndForPC" in wx.calls


async def test_login_start_without_main_wnd_class_leaves_backend_untouched():
    """不传 ``main_wnd_class``(全新登录,服务侧没有行值可推)时不调用 setter,沿用会话代理启动时的配置默认。"""
    wx, s, _clk = mk_session()
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    assert wx.search_class is None
    assert not any(c.startswith("set_main_wnd_class:") for c in wx.calls)


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
