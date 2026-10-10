"""会话代理装配(``user.py``)与 ``win/`` 真实现的可导入性 / 平台守卫。"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from qtrade_winagent import __version__
from qtrade_winagent.config import WinAgentConfig
from qtrade_winagent.errors import NOT_READY, WaError
from qtrade_winagent.fakes import FakePipeBackend, FakePower, FakeWeChat, FakeWsl
from qtrade_winagent.user import UserAgent

from tests.conftest import INSTALL_SID, attach_user_agent, build_rig, client


# ---------------------------------------------------------------- 会话代理
async def test_autostart_pulls_distro_up_on_login(tmp_path):
    """02 §2.1 步 2′:``[wsl] autostart=true`` ⇒ 会话代理上线即拉起发行版(**随用户登录,不随开机**)。"""
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    assert rig.wsl.distros[0]["running"] is True
    await rig.deps.hub.stop()
    rig.db.close()


async def test_autostart_failure_does_not_block_handshake(tmp_path):
    rig = build_rig(tmp_path)
    rig.wsl.fail_start = True
    ua = await attach_user_agent(rig)
    assert rig.deps.hub.user_agent_online is True                # 拉不起来也算上线(失败只记日志,由服务侧重试)
    await rig.deps.hub.stop()
    rig.db.close()


async def test_modules_advertised_follow_wechat_enabled(tmp_path):
    rig = build_rig(tmp_path, wechat_enabled=False)
    await attach_user_agent(rig)
    assert rig.deps.hub.user_session_view()["modules"] == ["wslctl"]
    await rig.deps.hub.stop()
    rig.db.close()


async def test_user_agent_without_wechat_module_reports_not_ready(tmp_path):
    rig = build_rig(tmp_path)
    await rig.deps.hub.start()
    ua = UserAgent(rig.deps.cfg, pipe=rig.pipe, wsl=rig.wsl, wechat=None, power=rig.power_backend,
                   session_id="Console", user_sid=INSTALL_SID, version=__version__,
                   backup_dir=str(rig.deps.installer._wsl_backup_dir))       # noqa: SLF001
    await ua.start()
    t = asyncio.create_task(ua.run())
    await asyncio.sleep(0)
    async with client(rig) as c:
        r = await c.get("/wa/v1/wechat/status")
    assert r.status_code == 503 and r.json()["error"]["reason"] == "wechat_module_absent"
    t.cancel()
    await rig.deps.hub.stop()
    rig.db.close()


async def test_wechat_module_toggle_propagates_to_user_agent(tmp_path):
    from tests.conftest import CONSOLE_TOKEN
    rig = build_rig(tmp_path)
    ua = await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        await c.put("/wa/v1/settings/wechat", json={"enabled": False})
    assert ua.cfg.wechat.enabled is False
    await rig.deps.hub.stop()
    rig.db.close()


async def test_login_flow_end_to_end_over_pipe(tmp_path):
    """#31 → #33 的完整一圈:服务收 HTTP → 管道下发 → 会话代理推状态机 → 相位回到 HTTP。"""
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    rig.wechat.ui_visible = True
    async with client(rig) as c:
        start = await c.post("/wa/v1/wechat/login/start", json={"account_id": "wx01"})
        assert start.status_code == 202
        lsid = start.json()["login_session_id"]
        rig.wechat.wxid = "wxid_abc"
        assert (await c.get("/wa/v1/wechat/login/status")).json()["phase"] == "identified"
        assert (await c.get("/wa/v1/wechat/login/status")).json()["state_code"] == "WAIT_KEY_IMG"
        rig.wechat.img_key = True
        assert (await c.get("/wa/v1/wechat/login/status")).json()["state_code"] == "WAIT_KEY_RELOGIN"
        rig.wechat.data_key = True
        st = (await c.get("/wa/v1/wechat/login/status")).json()
        assert st["phase"] == "ready" and st["key"]["ok"] is True
        cancel = await c.post("/wa/v1/wechat/login/cancel", json={"login_session_id": lsid})
        assert cancel.json()["cancelled"] is True
    await rig.deps.hub.stop()
    rig.db.close()


async def test_key_retry_endpoint_reinstalls_hook(tmp_path):
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    async with client(rig) as c:
        b = (await c.post("/wa/v1/wechat/key/retry", json={})).json()
    assert b["dll"] in ("wx_key2.dll", "wx_key1.dll") and "chatlog_stop" in rig.wechat.calls
    await rig.deps.hub.stop()
    rig.db.close()


async def test_logout_and_ui_visible_and_sessions(tmp_path):
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    rig.wechat.sessions = [{"name": "群A"}, {"name": "文件传输助手"}]
    rig.wechat.ui_visible = True                                                      # UI 树可见 ⇒ 跳过仪式直接拉起
    async with client(rig) as c:
        assert (await c.get("/wa/v1/wechat/ui-visible")).json()["visible"] is False   # 微信还没起
        await c.post("/wa/v1/wechat/login/start", json={})
        assert (await c.get("/wa/v1/wechat/ui-visible")).json()["visible"] is True
        s = (await c.get("/wa/v1/wechat/sessions?keyword=群")).json()["sessions"]
        assert [x["name"] for x in s] == ["群A"]
        assert (await c.post("/wa/v1/wechat/logout", json={})).json()["mode"] == "process"
    await rig.deps.hub.stop()
    rig.db.close()


async def test_reinstall_is_not_silent(tmp_path):
    """B-3:卸载器 NSIS 的 ``/S`` = 连聊天数据一起删 ⇒ **重装引导不走静默**。"""
    import hashlib
    from tests.conftest import CONSOLE_TOKEN
    installer = tmp_path / "weixin_4.1.12.26.exe"
    installer.write_bytes(b"MZ-fake-bundled-installer")
    cfg = WinAgentConfig().with_wechat(bundled_sha256=hashlib.sha256(installer.read_bytes()).hexdigest())
    rig = build_rig(tmp_path, cfg=cfg)
    await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wechat/reinstall", json={"installer": str(installer)})
    assert r.status_code == 202 and f"reinstall:{installer}" in rig.wechat.calls
    await rig.deps.hub.stop()
    rig.db.close()


async def test_reinstall_uses_installer_layout_by_default(tmp_path):
    """R6-87:默认路径 = 安装器实际落包处 `pkg\\wechat\\weixin_4.1.12.26.exe`(此前写 `pkg\\WeChatSetup.exe`,
    安装器又从不写本键 ⇒ 全新机器装完「重装」必缺文件)。"""
    from qtrade_winagent.config import WechatConfig
    assert WechatConfig().bundled_installer.lower().endswith("\\pkg\\wechat\\weixin_4.1.12.26.exe")
    assert WechatConfig().bundled_version == "4.1.12.26"
    assert WechatConfig().bundled_sha256 == "58997cfe4513ab71f107c2137bb570ade030f228115c14688544cec80e604053"


async def test_reinstall_refuses_installer_with_wrong_sha256(tmp_path):
    """R2-6:WeChatWin_4.1.12.exe 与随包 weixin_4.1.12.26.exe 外层 VersionInfo 完全相同、装出来却是 4.1.12.55 ——
    文件在位但 sha256 不是钉死值 ⇒ 404,不拉起。"""
    from tests.conftest import CONSOLE_TOKEN
    installer = tmp_path / "weixin_4.1.12.26.exe"
    installer.write_bytes(b"MZ-wrong-package")
    rig = build_rig(tmp_path)                                        # 默认配置 = 钉死 sha256
    await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wechat/reinstall", json={"installer": str(installer)})
    assert r.status_code == 404 and r.json()["error"]["reason"] == "bundled_installer_sha_mismatch"
    assert not any(c.startswith("reinstall:") for c in rig.wechat.calls)
    await rig.deps.hub.stop()
    rig.db.close()


async def test_reinstall_with_missing_installer_is_a_clear_404_not_internal(tmp_path):
    """2026-10-10 真机:随包安装包不在位时 ``create_subprocess_exec`` 的 ``FileNotFoundError`` 原样穿到控制台弹窗
    (「Error invoking remote method … 系统找不到指定的文件」)。现在先核文件:404 TARGET_NOT_FOUND + 能照着做的话,且不拉起任何进程。"""
    from tests.conftest import CONSOLE_TOKEN
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wechat/reinstall", json={})          # 走配置缺省 bundled_installer(测试环境不在位)
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["code"] == "TARGET_NOT_FOUND" and body["error"]["reason"] == "bundled_installer_missing"
    assert "bundled_installer" in body["error"]["message"] and "FileNotFoundError" not in body["error"]["message"]
    assert not any(c.startswith("reinstall:") for c in rig.wechat.calls)
    await rig.deps.hub.stop()
    rig.db.close()


async def test_version_match_uses_live_locate_when_install_row_is_empty(tmp_path):
    """R6-86 承接:机器上本来就装着随包同版本时,``wechat_install`` 没行不等于「未安装」—— 问会话代理本机实际安装并回填。"""
    from tests.conftest import CONSOLE_TOKEN
    rig = build_rig(tmp_path)
    rig.wechat.installed, rig.wechat.version, rig.wechat.path = True, "4.1.12.26", "D:\\Program Files\\Tencent\\Weixin\\Weixin.exe"
    await attach_user_agent(rig)
    assert rig.deps.wechat_store.install() is None
    async with client(rig, token=CONSOLE_TOKEN) as c:
        b = (await c.get("/wa/v1/wechat/version-match")).json()
    assert b["match"] == "SUPPORTED" and b["current_version"] == "4.1.12.26" and b["action"] == "continue"
    row = rig.deps.wechat_store.install()
    assert row and row["version"] == "4.1.12.26" and row["path"].endswith("Weixin.exe")
    await rig.deps.hub.stop()
    rig.db.close()


async def test_version_match_prefers_live_locate_over_stale_row(tmp_path):
    """用户事后自己升级/卸载了微信:`wechat_install` 旧行不作数,以本机实际安装为准并回填。"""
    from tests.conftest import CONSOLE_TOKEN
    rig = build_rig(tmp_path)
    rig.deps.wechat_store.put_install(version="4.1.12.26", path="D:\\old\\Weixin.exe")
    rig.wechat.installed, rig.wechat.version = True, "4.1.13.12"      # 自动更新到了更新版
    await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        b = (await c.get("/wa/v1/wechat/version-match")).json()
        assert b["match"] == "UNSUPPORTED_NEWER" and b["current_version"] == "4.1.13.12"
        assert rig.deps.wechat_store.install()["version"] == "4.1.13.12"
        rig.wechat.installed, rig.wechat.version = False, None        # 又卸掉了
        b2 = (await c.get("/wa/v1/wechat/version-match")).json()
        assert b2["match"] == "NOT_INSTALLED"
    await rig.deps.hub.stop()
    rig.db.close()


async def test_version_match_without_user_agent_still_reports_not_installed(tmp_path):
    """会话代理不在线时没有事实来源,维持 NOT_INSTALLED(不猜),且不报错。"""
    from tests.conftest import CONSOLE_TOKEN
    rig = build_rig(tmp_path)
    await rig.deps.hub.start()
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.get("/wa/v1/wechat/version-match")
    assert r.status_code == 200 and r.json()["match"] == "NOT_INSTALLED"
    await rig.deps.hub.stop()
    rig.db.close()


async def test_user_agent_aligns_wechat_module_with_service_on_handshake(tmp_path):
    """R6-88:会话代理启动时读到的 toml 说 enabled=false,服务侧(真值持有者)说 true ⇒ 握手 WELCOME 即对齐,
    之后 #28 立刻是 enabled:true(此前要等下一次 PUT 才推,控制台卡在「尚未确认微信模块已启用」)。"""
    rig = build_rig(tmp_path, wechat_enabled=True)                   # 服务侧 enabled=true
    await rig.deps.hub.start()
    ua = UserAgent(WinAgentConfig(), pipe=rig.pipe, wsl=rig.wsl, wechat=rig.wechat, power=rig.power_backend,
                   session_id="Console", user_sid=INSTALL_SID, version=__version__,
                   backup_dir=str(rig.deps.installer._wsl_backup_dir))       # noqa: SLF001 —— 本地 toml:enabled=false
    assert ua.cfg.wechat.enabled is False
    await ua.start()
    t = asyncio.create_task(ua.run())
    await asyncio.sleep(0)
    assert ua.cfg.wechat.enabled is True
    async with client(rig) as c:
        b = (await c.get("/wa/v1/wechat/status")).json()
    assert b["enabled"] is True
    assert not any(c.startswith("logout") for c in rig.wechat.calls), "对齐不得触发登出(会杀用户正在用的微信)"
    t.cancel()
    await rig.deps.hub.stop()
    rig.db.close()


async def test_user_agent_handshake_with_service_disabled_does_not_logout(tmp_path):
    """两边都是 false 时对齐是空操作:绝不能因为「再推一次 false」就去结束用户自己开着的微信进程。"""
    rig = build_rig(tmp_path, wechat_enabled=False)
    rig.wechat.running_pid = 4242                                     # 用户自己开着微信
    await attach_user_agent(rig)
    async with client(rig, token=__import__("tests.conftest", fromlist=["CONSOLE_TOKEN"]).CONSOLE_TOKEN) as c:
        await c.put("/wa/v1/settings/wechat", json={"enabled": False})   # 同值再 PUT 一次
    assert not any(c.startswith("logout") for c in rig.wechat.calls)
    assert rig.wechat.running_pid == 4242
    await rig.deps.hub.stop()
    rig.db.close()


async def test_settings_wechat_put_persists_to_toml(tmp_path):
    """R6-88:#43 改动写回 winagent.toml(C-43 真值),服务重启后 load 得到同样的值;文件其余内容逐字不动。"""
    from qtrade_winagent.config import load as load_cfg
    from qtrade_winagent.main_svc import read_toml
    from tests.conftest import CONSOLE_TOKEN
    toml = tmp_path / "winagent.toml"
    toml.write_bytes("\ufeff# dev instance\r\n[api]\r\nport = 17610\r\n\r\n[wechat]\r\nenabled = false\r\nkeep_awake_mode = \"off\"\r\n\r\n[log]\r\nlevel = \"INFO\"\r\n".encode("utf-8"))
    rig = build_rig(tmp_path, wechat_enabled=False)
    rig.deps.config_path = str(toml)
    await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.put("/wa/v1/settings/wechat", json={"enabled": True, "poll_interval_s": 7})
        assert r.status_code == 200 and r.json()["enabled"] is True
    raw = toml.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and b"# dev instance" in raw and b'level = "INFO"' in raw
    reloaded = load_cfg(read_toml(str(toml)))
    assert reloaded.wechat.enabled is True and reloaded.wechat.poll_interval_s == 7
    assert reloaded.wechat.keep_awake_mode == "off" and reloaded.api.port == 17610
    await rig.deps.hub.stop()
    rig.db.close()


async def test_service_kills_narrator_when_user_agent_cannot(tmp_path):
    """R6-89 服务半:#33 响应带 `narrator.stop_pending` ⇒ 服务调提权 killer、记审计,并把本次响应改为已停。"""
    from tests.conftest import AGENT_TOKEN
    rig = build_rig(tmp_path, cfg=WinAgentConfig().with_wechat(narrator_ritual="always"))   # R6-93:仪式只在 always 下
    rig.wechat.ui_visible = False
    rig.wechat.narrator_unkillable = True
    rig.deps.narrator_killer = rig.wechat.kill_narrator_elevated
    await attach_user_agent(rig)
    async with client(rig, token=AGENT_TOKEN) as c:
        r = await c.post("/wa/v1/wechat/login/start", json={"account_id": "wx01"})
        assert r.status_code == 202, r.text
        rig.wechat.wxid = "wxid_abc"
        rig.clock.advance(300_000)
        await asyncio.sleep(0.15)                                    # 让会话代理按新时钟补一次心跳,别被判离线
        b = (await c.get("/wa/v1/wechat/login/status")).json()
    assert b.get("narrator"), b
    assert b["narrator"]["stop_pending"] is False and b["narrator"]["state"] == "stopped"
    assert "narrator_kill_elevated" in rig.wechat.calls
    assert any(a["action"] == "wechat.narrator_stop" and a["result"] == "OK" for a in rig.deps.audit.page(limit=100)["items"])
    await rig.deps.hub.stop()
    rig.db.close()


async def test_status_skips_ui_probe_when_wechat_not_running(tmp_path):
    """2026-10-10 真机:微信没在跑时 pywinauto 探窗把 #28 拖过 10s ⇒ 恒 TIMEOUT。主窗口不在 ⇒ ritual_done=False,不碰 UIA。"""
    rig = build_rig(tmp_path)
    rig.wechat.running_pid = None
    rig.wechat.ui_visible = True                                     # 若仍去问 UIA,这里会被误判为 True
    await attach_user_agent(rig)
    async with client(rig) as c:
        b = (await c.get("/wa/v1/wechat/status")).json()
    assert b["wechat"]["running"] is False and b["ritual_done"] is False
    await rig.deps.hub.stop()
    rig.db.close()


# ---------------------------------------------------------------- win/ 真实现:Linux 上可导入、调用有守卫
@pytest.mark.parametrize("mod", ["dpapi", "sysinfo", "netinfo", "firewall", "power", "probe", "proc", "pipes",
                                 "wsl", "hosts", "wechat"])
def test_win_modules_import_on_linux(mod):
    """所有 Windows 专有导入都在函数体内 ⇒ 在 Linux/CI 上导入本包不报错(测试一律注入 Fake*)。"""
    m = importlib.import_module(f"qtrade_winagent.win.{mod}")
    assert m is not None


def test_require_windows_guard_matches_platform():
    """非 Windows 上 ``require_windows`` 必须挡住(提示去注入 fakes);Windows 上必须放行。两条都断言,不 skip。"""
    from qtrade_winagent.win import is_windows, require_windows
    if is_windows():
        require_windows("DPAPI")                       # 真机上不得抛
        return
    with pytest.raises(RuntimeError) as e:
        require_windows("DPAPI")
    assert "只能在 Windows 上运行" in str(e.value) and "fakes.py" in str(e.value)


def test_win_probe_works_cross_platform():
    """``WinProbe`` 只用标准库,真机与开发机行为一致(故意没有 require_windows)。"""
    from qtrade_winagent.win.probe import WinProbe

    async def go():
        step = await WinProbe().tcp("127.0.0.1", 1, 0.2)
        assert step.ok is False and step.result in ("TCP_REFUSED", "TCP_TIMEOUT", "BLOCKED_BY_POLICY")
    asyncio.run(go())
