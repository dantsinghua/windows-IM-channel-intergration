"""服务 ⇄ 会话代理 IPC(02 §2.4.1):帧协议、HELLO/WELCOME、心跳、超时、SID 校验与多用户仲裁。"""
from __future__ import annotations

import asyncio

import pytest

from qtrade_winagent import __version__
from qtrade_winagent.audit import ACTOR_SVC_TO_USER, Audit
from qtrade_winagent.backends import PipeFrame
from qtrade_winagent.config import IpcConfig
from qtrade_winagent.db import Db
from qtrade_winagent.errors import BUSY, FORBIDDEN, NOT_READY, TIMEOUT, VERSION_MISMATCH, WaError
from qtrade_winagent.fakes import FakePipeBackend
from qtrade_winagent.pipe import FORBIDDEN_REASON, IPC_VERSION, PipeHub, UserAgentLink, method_for_path

from tests.conftest import INSTALL_SID, OTHER_SID, Clock


def mk_hub(clock=None, cfg=None):
    db = Db(":memory:").open()
    be = FakePipeBackend()
    hub = PipeHub(be, cfg or IpcConfig(), pipe_name=r"\\.\pipe\qtrade-winagent-user",
                  install_user_sid=INSTALL_SID, version=__version__, audit=Audit(db),
                  clock=clock or (lambda: 1_758_240_000_000))
    return db, be, hub


def mk_link(be, *, session_id="Console", sid=INSTALL_SID, version=None, cfg=None, clock=None):
    return UserAgentLink(be, cfg or IpcConfig(), pipe_name=r"\\.\pipe\qtrade-winagent-user", session_id=session_id,
                         user_sid=sid, version=version or __version__, pid=1234, modules=("wslctl", "wechat"),
                         clock=clock or (lambda: 1_758_240_000_000))


# ---------------------------------------------------------------- 帧协议
def test_frame_wire_shapes():
    req = PipeFrame(id=7, method="wsl.start", params={"a": 1}, trace_id="T", deadline_ms=29000)
    assert req.to_wire() == {"id": 7, "method": "wsl.start", "params": {"a": 1}, "trace_id": "T", "deadline_ms": 29000}
    assert PipeFrame(id=7, ok=True, result={"x": 1}).to_wire() == {"id": 7, "ok": True, "result": {"x": 1}}
    assert PipeFrame(id=7, ok=False, error={"code": "TIMEOUT"}).to_wire() == {"id": 7, "ok": False,
                                                                              "error": {"code": "TIMEOUT"}}


def test_method_maps_one_to_one_with_wa_v1_path():
    assert method_for_path("/wa/v1/wsl/start") == "wsl.start"
    assert method_for_path("/wa/v1/wsl/config") == "wsl.config"
    assert method_for_path("/wa/v1/wechat/login/start") == "wechat.login.start"


async def test_frame_larger_than_max_is_rejected():
    be = FakePipeBackend(max_frame_kb=1)
    conn = await be.connect("p")
    with pytest.raises(ValueError):
        await conn.send(PipeFrame(id=1, method="x", params={"blob": "a" * 4000}))


# ---------------------------------------------------------------- HELLO / WELCOME / 心跳
async def test_hello_welcome_and_heartbeat_drives_user_agent_flag():
    clk = Clock()
    db, be, hub = mk_hub(clock=clk)
    await hub.start()
    assert hub.user_agent_online is False
    link = mk_link(be, clock=clk)
    welcome = await link.connect()
    assert welcome["type"] == "WELCOME" and welcome["ipc_version"] == IPC_VERSION
    await asyncio.sleep(0)
    assert hub.user_agent_online is True
    assert hub.user_session_view()["sid"] == INSTALL_SID
    clk.advance(IpcConfig().offline_after_s * 1000 + 1)          # 15s 无心跳 ⇒ 判离线
    assert hub.user_agent_online is False
    await hub.stop()
    db.close()


async def test_probe_never_goes_through_pipe():
    """🔴 R3-1:``user_agent`` 只读服务维护的心跳状态,**不穿管道** —— 会话代理挂死也不该让探活卡住。"""
    clk = Clock()
    db, be, hub = mk_hub(clock=clk)
    await hub.start()
    link = mk_link(be, clock=clk)
    link.on("wsl.status", lambda p: asyncio.sleep(999))          # 会话代理完全挂死
    await link.connect()
    t = asyncio.create_task(link.run())
    await asyncio.sleep(0)
    assert hub.user_agent_online is True                          # 探活不受挂死影响
    t.cancel()
    await hub.stop()
    db.close()


# ---------------------------------------------------------------- 仲裁:先校验 SID,再谈先到先得
async def test_non_install_user_is_rejected_at_hello():
    db, be, hub = mk_hub()
    await hub.start()
    with pytest.raises(WaError) as e:
        await mk_link(be, session_id="RDP", sid=OTHER_SID).connect()
    assert e.value.code == FORBIDDEN and e.value.message == FORBIDDEN_REASON
    assert hub.holder is None and ("FORBIDDEN", OTHER_SID) in hub.rejected
    await hub.stop()
    db.close()


async def test_sid_check_happens_before_arbitration():
    """R3-12 的关键点:非安装用户**即使先到**也拿不到持有者,后到的安装用户仍能成为 holder。"""
    db, be, hub = mk_hub()
    await hub.start()
    with pytest.raises(WaError):
        await mk_link(be, session_id="RDP", sid=OTHER_SID).connect()
    await asyncio.sleep(0)
    await mk_link(be, session_id="Console", sid=INSTALL_SID).connect()
    await asyncio.sleep(0)
    assert hub.holder is not None and hub.holder.session_id == "Console"
    await hub.stop()
    db.close()


async def test_second_install_user_session_gets_busy_and_takes_over_after_holder_drops():
    db, be, hub = mk_hub()
    await hub.start()
    first = mk_link(be, session_id="Console")
    await first.connect()
    t = asyncio.create_task(first.run())
    await asyncio.sleep(0)
    with pytest.raises(WaError) as e:
        await mk_link(be, session_id="RDP2").connect()
    assert e.value.code == BUSY and e.value.extra["holder_user"] == "Console" and e.value.extra["retry_after_s"] == 30
    # 持有者断开 ⇒ 下一位可接管
    t.cancel()
    await first.stop()
    await asyncio.sleep(0.01)
    assert hub.holder is None
    await mk_link(be, session_id="RDP2").connect()
    await asyncio.sleep(0)
    assert hub.holder.session_id == "RDP2"
    await hub.stop()
    db.close()


async def test_version_mismatch_is_refused_and_alerts():
    from qtrade_winagent.alerts import WA_USER_VERSION_MISMATCH, AlertBuffer
    from qtrade_winagent.config import AlertConfig
    db = Db(":memory:").open()
    be = FakePipeBackend()
    alerts = AlertBuffer(AlertConfig())
    hub = PipeHub(be, IpcConfig(), pipe_name="p", install_user_sid=INSTALL_SID, version="1.0.0",
                  alerts=alerts, audit=Audit(db))
    await hub.start()
    link = UserAgentLink(be, IpcConfig(), pipe_name="p", session_id="C", user_sid=INSTALL_SID, version="2.0.0",
                         pid=1, modules=())
    with pytest.raises(WaError) as e:
        await link.connect()
    assert e.value.code == VERSION_MISMATCH
    assert alerts.is_firing(WA_USER_VERSION_MISMATCH, "host")
    await hub.stop()
    db.close()


# ---------------------------------------------------------------- 调用:deadline / 超时 / 离线 / 审计
async def test_call_passes_deadline_minus_one_second():
    db, be, hub = mk_hub()
    await hub.start()
    seen: dict = {}

    async def handler(p):
        return {"ok": True}
    link = mk_link(be)
    link.on("wsl.start", handler)
    await link.connect()
    t = asyncio.create_task(link.run())
    await asyncio.sleep(0)
    await hub.call("wsl.start", {}, timeout_s=30.0)
    sent = [f for f in link.conn.sent]                              # type: ignore[union-attr]
    # 服务发出的请求帧在会话代理侧被收到;这里用服务侧持有者连接的 sent 检查
    frames = hub.holder.conn.sent                                   # type: ignore[union-attr]
    req = [f for f in frames if f.method == "wsl.start"][0]
    assert req.deadline_ms == 29000                                 # §2.5 超时 30s − 1s
    t.cancel()
    await hub.stop()
    db.close()


async def test_user_agent_timeout_returns_TIMEOUT_frame():
    db, be, hub = mk_hub()
    await hub.start()
    link = mk_link(be)
    link.on("wechat.send", lambda p: asyncio.sleep(5))
    await link.connect()
    t = asyncio.create_task(link.run())
    await asyncio.sleep(0)
    with pytest.raises(WaError) as e:
        await hub.call("wechat.send", {}, timeout_s=1.2)            # deadline 200ms → 会话代理自己回 TIMEOUT
    assert e.value.code == TIMEOUT
    t.cancel()
    await hub.stop()
    db.close()


async def test_call_without_user_agent_is_503_not_ready():
    db, be, hub = mk_hub()
    await hub.start()
    with pytest.raises(WaError) as e:
        await hub.call("wsl.start", {}, timeout_s=30.0)
    assert e.value.code == NOT_READY and e.value.http_status == 503
    assert e.value.message == "用户会话代理未运行(用户未登录或代理被结束)"
    rows = db.query("SELECT * FROM wa_audit_log WHERE actor=?", (ACTOR_SVC_TO_USER,))
    assert rows and rows[0]["result"] == NOT_READY
    await hub.stop()
    db.close()


async def test_each_dispatch_is_audited_as_svc_to_user():
    db, be, hub = mk_hub()
    await hub.start()
    link = mk_link(be)
    link.on("wsl.stop", lambda p: _ok())
    await link.connect()
    t = asyncio.create_task(link.run())
    await asyncio.sleep(0)
    await hub.call("wsl.stop", {}, timeout_s=30.0, trace_id="TR-9", target="wsl.stop")
    row = db.one("SELECT * FROM wa_audit_log WHERE actor=? ORDER BY id DESC", (ACTOR_SVC_TO_USER,))
    assert row["action"] == "wsl.stop" and row["result"] == "OK" and row["trace_id"] == "TR-9"
    t.cancel()
    await hub.stop()
    db.close()


async def test_handler_error_becomes_error_frame_with_stage_user():
    db, be, hub = mk_hub()
    await hub.start()
    link = mk_link(be)

    async def boom(p):
        raise WaError("INVALID_ARGS", "参数不对", reason="bad")
    link.on("wsl.restart", boom)
    await link.connect()
    t = asyncio.create_task(link.run())
    await asyncio.sleep(0)
    with pytest.raises(WaError) as e:
        await hub.call("wsl.restart", {}, timeout_s=30.0)
    assert e.value.code == "INVALID_ARGS" and e.value.stage == "user"
    t.cancel()
    await hub.stop()
    db.close()


async def _ok():
    return {"ok": True}
