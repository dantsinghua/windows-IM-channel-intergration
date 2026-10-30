"""R6-118 chatlog server 回收(05 §2.4.10 ①②③;04/02 R6-118 段;DECISION-3 附录 A T5/T6/T8/T11)。

全部用 ``FakeWeChat`` + 可拨时钟,不碰真 chatlog 进程、真 ``%TEMP%`` 与真微信账号。
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from qtrade_winagent import __version__
from qtrade_winagent import user as user_mod
from qtrade_winagent.backends import PipeFrame
from qtrade_winagent.config import IpcConfig, WechatConfig
from qtrade_winagent.errors import NOT_READY, WaError
from qtrade_winagent.fakes import FakePipeBackend, FakeWeChat
from qtrade_winagent.pipe import PipeHub, UserAgentLink
from qtrade_winagent.wechat import KEY_FAIL, WAIT_KEY_IMG, WeChatSession

from tests.conftest import AGENT_TOKEN, INSTALL_SID, Clock, client

GiB = 1 << 30
COPY_LIMIT = 4 * GiB              # 02 §7.2 chatlog_recycle_copy_bytes
UPTIME_LIMIT_MS = 7200 * 1000     # 02 §7.2 chatlog_recycle_max_uptime_s
FREE_LIMIT = 20 * GiB             # 02 §7.2 chatlog_recycle_min_free_bytes


def mk(clock=None, **cfgkw):
    clk = clock or Clock()
    wx = FakeWeChat()
    return wx, WeChatSession(wx, WechatConfig(enabled=True, **cfgkw), clock=clk), clk


async def serving(clock=None, **cfgkw):
    """两钥在 + 已登录 ⇒ ensure_server 拉起本代理的 server 模式 chatlog(副本量从 0 开始)。"""
    wx, s, clk = mk(clock, **cfgkw)
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_abc"
    assert await s.ensure_server() is True
    assert wx.chatlog_mode == "server" and wx.chatlog_pid is not None
    return wx, s, clk


async def ready_via_login(clock=None, **cfgkw):
    """走一遍登录取钥 ⇒ ready(server 已起),用来核「回收不碰登录会话」。"""
    wx, s, clk = mk(clock, **cfgkw)
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    s._set_phase(s.session, "keytry", WAIT_KEY_IMG)                  # noqa: SLF001
    wx.wxid = "wxid_abc"
    wx.data_key = wx.img_key = True
    await s.poll()
    assert s.session.phase == "ready" and wx.chatlog_mode == "server"
    return wx, s, clk


GATE_MAX_S = 8.0


def gate(wx, name):
    """把后端某个异步方法挂起,直到放行(最多 GATE_MAX_S 自动放行,断言失败时不把收尾卡死);返回 (已进入, 放行)。"""
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(wx, name)

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), GATE_MAX_S)
        except asyncio.TimeoutError:
            pass
        return await original(*args, **kwargs)

    setattr(wx, name, blocked)
    return entered, release


def segment_after(calls, marker_index):
    return calls[marker_index:]


# ---------------------------------------------------------------- ① 三种判据与边界
async def test_criterion_a_copy_bytes_threshold_is_inclusive():
    wx, s, _ = await serving()
    wx.copy_bytes, wx.copy_files = COPY_LIMIT - 1, 21
    assert await s.maintain() is None
    assert "chatlog_stop" not in wx.calls
    wx.copy_bytes = COPY_LIMIT
    out = await s.maintain()
    assert out and out["recycled"] is True and out["criterion"] == "a"
    assert wx.calls.count("chatlog_stop") == 1


async def test_criterion_b_uptime_measured_from_this_server_start():
    clk = Clock()
    wx, s, _ = await serving(clk)
    clk.advance(UPTIME_LIMIT_MS - 1)
    assert await s.maintain() is None
    clk.advance(1)
    out = await s.maintain()
    assert out and out["recycled"] is True and out["criterion"] == "b"
    # 回收后重新计时:刚重拉的 server 不能马上又因为运行时长被回收
    stops = wx.calls.count("chatlog_stop")
    clk.advance(1000)
    assert await s.maintain() is None and wx.calls.count("chatlog_stop") == stops


async def test_criterion_c_needs_strictly_low_free_and_nonempty_copies():
    wx, s, _ = await serving()
    wx.copy_bytes, wx.copy_files = 1, 1
    wx.temp_free_bytes = FREE_LIMIT                                  # 等于阈值不算低
    assert await s.maintain() is None
    wx.temp_free_bytes = FREE_LIMIT - 1
    wx.copy_bytes, wx.copy_files = 0, 0                              # 卷低但本实例没有副本 ⇒ 回收无用
    assert await s.maintain() is None
    assert "chatlog_stop" not in wx.calls
    wx.copy_bytes, wx.copy_files = 1, 1
    out = await s.maintain()
    assert out and out["recycled"] is True and out["criterion"] == "c"


async def test_criteria_priority_c_over_a_over_b():
    clk = Clock()
    wx, s, _ = await serving(clk)
    clk.advance(UPTIME_LIMIT_MS)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 30
    wx.temp_free_bytes = FREE_LIMIT - 1
    out = await s.maintain()
    assert out["criterion"] == "c" and out["reason"] == "min_free"

    clk.advance(UPTIME_LIMIT_MS)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 30
    wx.temp_free_bytes = 1 << 40
    out = await s.maintain()
    assert out["criterion"] == "a" and out["reason"] == "copy_bytes"


async def test_custom_thresholds_from_config_are_honoured():
    wx, s, _ = await serving(chatlog_recycle_copy_bytes=1000)
    wx.copy_bytes, wx.copy_files = 999, 1
    assert await s.maintain() is None
    wx.copy_bytes = 1000
    assert (await s.maintain())["criterion"] == "a"


async def test_disabled_module_never_recycles():
    wx, s, _ = await serving()
    s._cfg = WechatConfig(enabled=False)                             # noqa: SLF001
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 1
    assert await s.maintain() is None and "chatlog_stop" not in wx.calls


# ---------------------------------------------------------------- 只回收本代理拉起、归属核验通过的 server 模式进程
async def test_not_owned_or_not_running_is_never_recycled():
    wx, s, _ = await serving()
    wx.copy_bytes, wx.copy_files, wx.temp_free_bytes = COPY_LIMIT * 2, 99, 0
    wx.chatlog_owned = False
    assert await s.maintain() is None
    assert "chatlog_stop" not in wx.calls and wx.calls.count("chatlog_sweep") == 1   # 只有启动清扫那一次
    wx.chatlog_owned = True
    wx.chatlog_pid = None                                            # 进程已不在 ⇒ 没东西可回收
    assert await s.maintain() is None and "chatlog_stop" not in wx.calls


async def test_no_recycle_while_login_flow_or_key_hook_in_progress():
    """T8 / R6-96:取钥(key 模式,内存 hook 中)或登录流未结束不回收 —— hook 中的 chatlog 不得被强杀。"""
    wx, s, _ = mk()
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    assert wx.chatlog_mode == "key"
    stops = wx.calls.count("chatlog_stop")
    wx.copy_bytes, wx.copy_files, wx.temp_free_bytes = COPY_LIMIT * 2, 99, 0
    assert await s.maintain() is None
    assert wx.calls.count("chatlog_stop") == stops

    # 即便后端谎报 server 模式、但登录流仍在 keytry,同样不动
    wx.chatlog_mode = "server"
    s._set_phase(s.session, "keytry", WAIT_KEY_IMG)                  # noqa: SLF001
    assert await s.maintain() is None
    assert wx.calls.count("chatlog_stop") == stops


# ---------------------------------------------------------------- ② 读取空闲等待 / 超时强制 / (c) 不等
async def test_waits_for_in_flight_read_then_recycles_unforced():
    wx, s, _ = await serving(chatlog_recycle_idle_wait_s=30)
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    entered, release = gate(wx, "read_messages")
    reading = asyncio.create_task(s.read(talker="群A"))
    await asyncio.wait_for(entered.wait(), 2)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 5
    recycle = asyncio.create_task(s.maintain())
    await asyncio.sleep(0.3)
    assert not recycle.done() and "chatlog_stop" not in wx.calls     # 读取未结束,不回收
    release.set()
    page = await asyncio.wait_for(reading, 2)
    assert page["messages"][0]["seq"] == 1                           # 进行中的读取正常完成
    out = await asyncio.wait_for(recycle, 5)
    assert out["recycled"] is True and out["forced"] is False and out["criterion"] == "a"


@pytest.mark.parametrize("busy", ["sessions", "send"])
async def test_sessions_and_send_readback_also_count_as_not_idle(busy):
    wx, s, _ = await serving(chatlog_recycle_idle_wait_s=30)
    wx.ui_visible = True
    name = "list_sessions" if busy == "sessions" else "send"
    entered, release = gate(wx, name)
    call = (s.sessions(keyword="群") if busy == "sessions" else s.send(session_name="群A", text="hi"))
    task = asyncio.create_task(call)
    await asyncio.wait_for(entered.wait(), 2)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 5
    recycle = asyncio.create_task(s.maintain())
    await asyncio.sleep(0.3)
    assert not recycle.done() and "chatlog_stop" not in wx.calls
    release.set()
    await asyncio.wait_for(task, 2)
    out = await asyncio.wait_for(recycle, 5)
    assert out["forced"] is False


async def test_idle_wait_timeout_forces_recycle():
    wx, s, _ = await serving(chatlog_recycle_idle_wait_s=1)
    entered, release = gate(wx, "read_messages")
    reading = asyncio.create_task(s.read(talker="群A"))
    await asyncio.wait_for(entered.wait(), 2)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 5
    t0 = time.monotonic()
    out = await asyncio.wait_for(s.maintain(), 10)
    elapsed = time.monotonic() - t0
    assert out["recycled"] is True and out["forced"] is True
    assert 0.8 <= elapsed < 5, elapsed
    assert not reading.done()                                        # 强制回收不等那次读取
    release.set()
    await asyncio.gather(reading, return_exceptions=True)


async def test_idle_wait_is_counted_from_first_need_detection():
    """「判为需回收后最多等 30 s」:ensure_server 路径先判到需回收,30 s 后维护循环到点就不再重新等满 30 s。"""
    clk = Clock()
    wx, s, _ = await serving(clk, chatlog_recycle_idle_wait_s=30)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 5
    assert await s.ensure_server() is False                          # 在跑:只顺带记下需回收
    clk.advance(30_000)
    entered, release = gate(wx, "read_messages")
    reading = asyncio.create_task(s.read(talker="群A"))
    await asyncio.wait_for(entered.wait(), 2)
    out = await asyncio.wait_for(s.maintain(), 3)
    assert out["recycled"] is True and out["forced"] is True
    release.set()
    await asyncio.gather(reading, return_exceptions=True)


async def test_criterion_c_recycles_immediately_without_idle_wait():
    wx, s, _ = await serving(chatlog_recycle_idle_wait_s=30)
    entered, release = gate(wx, "read_messages")
    reading = asyncio.create_task(s.read(talker="群A"))
    await asyncio.wait_for(entered.wait(), 2)
    wx.copy_bytes, wx.copy_files, wx.temp_free_bytes = 10, 1, FREE_LIMIT - 1
    t0 = time.monotonic()
    out = await asyncio.wait_for(s.maintain(), 3)
    assert time.monotonic() - t0 < 2
    assert out["criterion"] == "c" and out["recycled"] is True
    release.set()
    await asyncio.gather(reading, return_exceptions=True)


# ---------------------------------------------------------------- 回收期间读取:503 可重试、不判 KEY_FAIL、不伪造
async def test_reads_during_recycle_are_retryable_not_ready_and_never_key_fail():
    wx, s, _ = await ready_via_login()
    wx.messages = [{"talker": "群A", "seq": 7, "text": "x"}]
    wx.sessions = [{"name": "群A"}]
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 5
    entered, release = gate(wx, "chatlog_stop")
    recycle = asyncio.create_task(s.maintain())
    await asyncio.wait_for(entered.wait(), 3)
    for call in (lambda: s.read(talker="群A"), lambda: s.sessions(keyword="群")):
        with pytest.raises(WaError) as ei:
            await call()
        e = ei.value
        assert e.code == NOT_READY and e.http_status == 503 and e.retryable is True
        assert e.reason != "key_fail"
    assert s.session.phase == "ready" and s.session.state_code != KEY_FAIL
    release.set()
    out = await asyncio.wait_for(recycle, 5)
    assert out["recycled"] is True and out["restarted"] is True
    page = await s.read(talker="群A")                                # 回收完恢复正常读取
    assert [m["seq"] for m in page["messages"]] == [7]
    assert s.session.phase == "ready" and s.session.state_code != KEY_FAIL


async def _http_during_recycle(rig):
    """回收卡在停进程那一步时,经 HTTP(服务 → 管道 → 会话代理)取 #39/#41;返回 [(path, status, body)] 与回收后 #39。"""
    wx = rig.wechat
    ua = rig.user_agent
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_abc"
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    wx.sessions = [{"name": "群A"}]
    assert await ua.wechat.ensure_server() is True
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 3
    entered, release = gate(wx, "chatlog_stop")
    recycle = asyncio.create_task(ua.wechat.maintain())
    rows = []
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with client(rig, token=AGENT_TOKEN) as c:
            for path in ("/wa/v1/wechat/read?talker=群A", "/wa/v1/wechat/sessions?keyword=群"):
                r = await c.get(path)
                rows.append((path, r.status_code, r.json()))
            release.set()
            await asyncio.wait_for(recycle, 5)
            after = await c.get("/wa/v1/wechat/read?talker=群A")
    finally:
        release.set()
        await asyncio.gather(recycle, return_exceptions=True)
    return rows, after


async def test_http_read_and_sessions_return_503_during_recycle(rig_with_user):
    rows, after = await _http_during_recycle(rig_with_user)
    for path, status, body in rows:
        assert status == 503, (path, body)
        assert body["code"] == NOT_READY and body["error"]["reason"] != "key_fail"
        assert "messages" not in body and "sessions" not in body      # 不伪造成功或空页
    assert after.status_code == 200 and after.json()["messages"][0]["seq"] == 1


async def test_http_503_during_recycle_is_marked_retryable_on_the_wire(rig_with_user):
    """02 #39/#41 R6-118:「503 NOT_READY(可重试…)」;00 §8.3 NOT_READY retryable=是。对外信封须带 retryable=true。"""
    rows, _ = await _http_during_recycle(rig_with_user)
    for path, status, body in rows:
        assert status == 503
        assert body["error"]["retryable"] is True, (path, body["error"])


def _frame(code, reason=None, **flags):
    err = {"code": code, "message": "合成错误"}
    if reason is not None:
        err["reason"] = reason
    err.update(flags)
    return err


@pytest.mark.parametrize("error,retryable,needs_human,status", [
    # 帧未带标志(会话代理 _dispatch 现行帧形状):按 00 §8.3 码表补齐
    (_frame(NOT_READY, "wechat_read_connection_refused"), True, False, 503),
    (_frame("TIMEOUT", "user_handler_timeout"), True, False, 504),
    (_frame("INTERNAL"), True, False, 500),
    (_frame("DISK_FULL", "disk_full"), False, True, 507),
    (_frame("RESOURCE_EXHAUSTED", "pool_empty"), False, False, 409),
    (_frame("RESOURCE_EXHAUSTED", "seq_exhausted"), False, True, 409),      # R6-67 ① 覆盖注
    (_frame("TARGET_NOT_FOUND"), False, False, 404),
    (_frame("INVALID_ARGS"), False, False, 400),
    (_frame("UNAUTHORIZED"), False, False, 401),
    (_frame("FORBIDDEN"), False, False, 403),
    (_frame("RATE_LIMITED"), False, False, 429),
    # 帧已带标志:以帧为准(只带其一时,另一项仍按码表)
    (_frame(NOT_READY, "x", retryable=False), False, False, 503),
    (_frame("RESOURCE_EXHAUSTED", "ipc_response_too_large", retryable=False), False, False, 409),
    (_frame("DISK_FULL", "disk_full", retryable=True, needs_human=False), True, False, 507),
    (_frame("INVALID_ARGS", retryable=True, needs_human=True), True, True, 400),
    (_frame("DISK_FULL", "disk_full", retryable=True), True, True, 507),
    (_frame(NOT_READY, "x", needs_human=True), True, True, 503),
])
async def test_pipe_error_to_http_fills_flags_from_code_table_unless_frame_carries_them(error, retryable, needs_human,
                                                                                       status):
    """DECISION-3 D1:服务把会话代理错误帧转 HTTP 时按 00 §8.3 补 retryable/needs_human,帧已带则以帧为准;不扩帧格式。"""
    now = lambda: 1_758_240_000_000                                    # noqa: E731 —— 固定时钟,心跳不过期
    be = FakePipeBackend()
    hub = PipeHub(be, IpcConfig(), pipe_name="p", install_user_sid=INSTALL_SID, version=__version__, clock=now)
    await hub.start()
    link = UserAgentLink(be, IpcConfig(), pipe_name="p", session_id="Console", user_sid=INSTALL_SID,
                         version=__version__, pid=1, modules=("wechat",), clock=now)
    await link.connect()
    await asyncio.sleep(0)
    call = asyncio.create_task(hub.call("wechat.read", {"talker": "群A"}, timeout_s=5))
    try:
        req = await asyncio.wait_for(link.conn.recv(), 3)              # 直接以会话代理身份回原始错误帧
        await link.conn.send(PipeFrame(id=req.id, ok=False, error=dict(error)))
        with pytest.raises(WaError) as caught:
            await asyncio.wait_for(call, 3)
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        await link.stop()
        await hub.stop()
    e = caught.value
    assert e.code == error["code"] and e.http_status == status
    body = e.body("T")
    assert body["code"] == error["code"]
    assert body["error"]["retryable"] is retryable and body["error"]["needs_human"] is needs_human, body["error"]
    assert body["error"]["reason"] == error.get("reason", "")


# ---------------------------------------------------------------- ③ 动作顺序、T6 首次重拉不节流、失败后照常节流
async def test_recycle_order_stop_then_sweep_then_restart_without_relogin():
    wx, s, _ = await serving()
    mark = len(wx.calls)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT + 5, 8
    out = await s.maintain()
    seg = segment_after(wx.calls, mark)
    assert seg.index("chatlog_stop") < seg.index("chatlog_sweep") < seg.index("chatlog_serve")
    assert out["freed_bytes"] == COPY_LIMIT + 5 and out["before_bytes"] == COPY_LIMIT + 5
    assert not any(c.startswith("chatlog_start") or c == "launch" for c in seg)   # 不取钥、不拉起/重登微信


async def test_first_restart_after_recycle_ignores_30s_throttle():
    clk = Clock()
    wx, s, _ = await serving(clk)                                    # 刚刚拉起过(节流计时从此刻起)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 2
    out = await s.maintain()                                         # 时钟未走:仍在 30 s 节流窗内
    assert out["restarted"] is True
    assert wx.calls.count("chatlog_serve") == 2 and wx.chatlog_mode == "server"


async def test_failed_restart_falls_back_to_throttled_self_heal_and_keeps_login():
    clk = Clock()
    wx, s, _ = await ready_via_login(clk)
    mark = len(wx.calls)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 2
    wx.serve_fails = True
    out = await s.maintain()
    assert out["recycled"] is True and out["restarted"] is False
    assert s.session.phase == "ready" and s.session.state_code != KEY_FAIL   # 不升级为重新取钥
    serves = wx.calls.count("chatlog_serve")
    assert await s.ensure_server() is False                          # 首次失败后照常 30 s 节流
    assert wx.calls.count("chatlog_serve") == serves
    clk.advance(31_000)
    wx.serve_fails = False
    assert await s.ensure_server() is True
    assert wx.calls.count("chatlog_serve") == serves + 1
    assert not any(c.startswith("chatlog_start") or c == "launch" for c in wx.calls[mark:])


async def test_recycle_result_and_log_line_fields_without_secrets(caplog):
    wx, s, _ = await serving()
    secret = "ab" * 32
    wx.data_key = secret
    wx.copy_bytes, wx.copy_files, wx.sweep_failed = COPY_LIMIT, 6, 2
    with caplog.at_level(logging.INFO):
        out = await s.maintain()
    for k in ("reason", "criterion", "before_bytes", "freed_bytes", "failed", "cost_ms"):
        assert k in out, k
    assert out["failed"] == 2 and isinstance(out["cost_ms"], int) and out["cost_ms"] >= 0
    rows = [r for r in caplog.records if getattr(r, "op", "") == "wechat.recycle"]
    assert rows, "回收必须记一行结构化日志"
    kv = getattr(rows[-1], "kv", {}) or {}
    for k in ("reason", "before_bytes", "freed_bytes", "failed"):
        assert k in kv, k
    assert hasattr(rows[-1], "cost_ms") or "cost_ms" in kv
    assert secret not in caplog.text and all(secret not in str(getattr(r, "kv", "")) for r in caplog.records)


# ---------------------------------------------------------------- ③.4 启动清扫(首次拉起之前)
async def test_startup_sweep_runs_before_first_self_heal_serve():
    wx, s, _ = mk()
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_abc"
    wx.copy_bytes, wx.copy_files = 1234, 3                           # 上一实例残留
    assert await s.ensure_server() is True
    assert wx.calls.index("chatlog_sweep") < wx.calls.index("chatlog_serve")
    assert wx.copy_bytes == 0


async def test_startup_sweep_runs_before_first_serve_after_key_extraction():
    wx, s, _ = mk()
    wx.ui_visible = True
    await s.login_start(account_id="wx01")
    s._set_phase(s.session, "keytry", WAIT_KEY_IMG)                  # noqa: SLF001
    wx.wxid = "wxid_abc"
    wx.copy_bytes, wx.copy_files = 99, 1
    wx.data_key = wx.img_key = True
    await s.poll()
    assert s.session.phase == "ready"
    assert wx.calls.index("chatlog_sweep") < wx.calls.index("chatlog_serve")


# ---------------------------------------------------------------- 会话代理维护循环(≤ 60 s)与 ensure_server 路径(T5)
def test_maintenance_period_at_most_60s():
    assert 0 < user_mod.WECHAT_MAINTAIN_S <= 60


async def wait_for_call(wx, name, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in wx.calls:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_user_agent_maintenance_loop_recycles_periodically(tmp_path, monkeypatch):
    from tests.conftest import attach_user_agent, build_rig
    monkeypatch.setattr(user_mod, "WECHAT_MAINTAIN_S", 0.05)
    rig = build_rig(tmp_path)
    try:
        ua = await attach_user_agent(rig)
        wx = rig.wechat
        wx.data_key = wx.img_key = True
        wx.wxid = "wxid_abc"
        assert await ua.wechat.ensure_server() is True
        wx.copy_bytes, wx.copy_files = COPY_LIMIT, 4
        assert await wait_for_call(wx, "chatlog_stop"), "维护循环未按周期回收"
    finally:
        for t in rig._tasks:                                         # noqa: SLF001
            t.cancel()
        if rig.user_agent is not None:
            await rig.user_agent.stop()
        await rig.deps.hub.stop()
        rig.db.close()


async def test_ensure_server_path_checks_criteria_and_wakes_maintenance(rig_with_user):
    """T5:ensure_server 路径同样查判据;维护周期远未到(30 s)时也应很快回收。"""
    ua = rig_with_user.user_agent
    wx = rig_with_user.wechat
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_abc"
    assert await ua.wechat.ensure_server() is True
    await asyncio.sleep(0.1)
    wx.copy_bytes, wx.copy_files = COPY_LIMIT, 4
    assert "chatlog_stop" not in wx.calls
    await ua.wechat.ensure_server()
    assert await wait_for_call(wx, "chatlog_stop"), "ensure_server 发现需回收后未触发回收"
