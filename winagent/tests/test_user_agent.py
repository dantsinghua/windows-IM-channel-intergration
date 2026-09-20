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
    from tests.conftest import CONSOLE_TOKEN
    rig = build_rig(tmp_path)
    await attach_user_agent(rig)
    async with client(rig, token=CONSOLE_TOKEN) as c:
        r = await c.post("/wa/v1/wechat/reinstall", json={"installer": "C:\\pkg\\WeChatSetup.exe"})
    assert r.status_code == 202 and "reinstall:C:\\pkg\\WeChatSetup.exe" in rig.wechat.calls
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
