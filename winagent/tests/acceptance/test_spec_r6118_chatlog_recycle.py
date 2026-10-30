"""R6-118 独立验收:WinAgent 回收 chatlog、清扫 filecopy 临时副本、轮转 chatlog 日志、临时目录卷告警。

只依据规格撰写:05 §2.4.10(行为唯一出处)、04 §2.3「R6-118 临时目录卷空闲」段与 §7 [monitor]、
02 §3.6 #39/#41、§3.7 `WINAGENT_TEMP_DISK_LOW`、§7.2 [wechat]/[monitor]、07 总表,
以及 `.codex/qidian-reply-quote-20261028/DECISION-3.md` 附录 A(T1~T13)/附录 B(B1~B5);
文件末段另按 05 §2.4.10 ⑥ 与附录 E(含 E7/E8)验收最小回收间隔与效果退避。

全部数据合成:假微信后端(``FakeWeChat`` 派生)、``tmp_path`` 下的假 ``filecopy_chatlog`` 目录、本测试自起的
Python 子进程充当「会写大量输出的 chatlog」。🔴 不碰本机真实 ``%TEMP%\\filecopy_chatlog``、真实 chatlog 进程与微信账号目录:
涉及 ``WinWeChat`` 的用例把 TMP/TEMP、``~`` 全部改到 ``tmp_path``,并加守卫——任何指向 ``tmp_path`` 之外
``filecopy_chatlog`` 的删除/列目录一律拒绝并记为违规。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from qtrade_winagent.alerts import AlertBuffer
from qtrade_winagent.config import AlertConfig, MonitorConfig, WechatConfig, WinAgentConfig, load
from qtrade_winagent.db import Db
from qtrade_winagent.errors import HTTP_BY_CODE, NOT_READY, WaError
from qtrade_winagent.fakes import FakeProbe, FakeSys, FakeWeChat
from qtrade_winagent.monitor import DiskTarget, Monitor
from qtrade_winagent.wechat import KEY_FAIL, WeChatSession
from qtrade_winagent.win import wechat as native

from tests.conftest import AGENT_TOKEN, INSTALL_SID, build_rig, client

KiB, MiB, GiB = 1 << 10, 1 << 20, 1 << 30
TEMP_CODE = "WINAGENT_TEMP_DISK_LOW"
FORBIDDEN_RELOGIN_PREFIXES = ("launch", "logout", "chatlog_start", "narrator_start", "narrator_kill", "reinstall")


# ============================================================== 夹具
class RealClock:
    """真实墙钟 + 可拨偏移:空闲等待按真实时间走,运行时长/节流可用 ``advance`` 快进。"""

    def __init__(self) -> None:
        self.offset_ms = 0

    def __call__(self) -> int:
        return int(time.time() * 1000) + self.offset_ms

    def advance(self, ms: int) -> None:
        self.offset_ms += ms


class ScriptedWeChat(FakeWeChat):
    """可在读取 / 会话列表 / 发送 / chatlog 停止 / chatlog 拉起处挂闸的假微信;所有动作按序记进 ``calls``。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.read_gate: Optional[asyncio.Event] = None
        self.sessions_gate: Optional[asyncio.Event] = None
        self.send_gate: Optional[asyncio.Event] = None
        self.stop_gate: Optional[asyncio.Event] = None
        self.serve_gate: Optional[asyncio.Event] = None

    async def read_messages(self, *, talker, since_seq, limit):
        self.calls.append("read_begin")
        if self.read_gate is not None:
            await self.read_gate.wait()
        self.calls.append("read_end")
        return await super().read_messages(talker=talker, since_seq=since_seq, limit=limit)

    async def list_sessions(self, *, keyword, limit):
        self.calls.append("sessions_begin")
        if self.sessions_gate is not None:
            await self.sessions_gate.wait()
        self.calls.append("sessions_end")
        return await super().list_sessions(keyword=keyword, limit=limit)

    async def send(self, *, session_name, text, image_path, confirm_timeout_ms):
        self.calls.append("send_begin")
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.calls.append("send_end")
        return await super().send(session_name=session_name, text=text, image_path=image_path,
                                  confirm_timeout_ms=confirm_timeout_ms)

    async def chatlog_stop(self) -> None:
        self.calls.append("stop_enter")
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        await super().chatlog_stop()

    async def chatlog_serve(self) -> int:
        self.calls.append("serve_enter")
        if self.serve_gate is not None:
            await self.serve_gate.wait()
        return await super().chatlog_serve()


SECRET_TEXT = "绝密聊天内容-R6118-验收哨兵"


def make_session(**cfgkw: Any) -> tuple[ScriptedWeChat, WeChatSession, RealClock]:
    clk = RealClock()
    wx = ScriptedWeChat()
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_accept"
    wx.logged_in = True
    wx.ui_visible = True
    wx.running_pid = 5101
    wx.messages = [{"talker": "群A", "seq": 1, "text": SECRET_TEXT}]
    wx.sessions = [{"name": "群A", "userName": "群A", "nickName": "群A"}]
    return wx, WeChatSession(wx, WechatConfig(enabled=True, **cfgkw), clock=clk), clk


async def ready_session(**cfgkw: Any) -> tuple[ScriptedWeChat, WeChatSession, RealClock]:
    """两钥已落盘、已登录、经 ``ensure_server`` 拉起了本代理自己的 server 模式 chatlog。"""
    wx, s, clk = make_session(**cfgkw)
    assert await s.ensure_server() is True
    assert wx.chatlog_mode == "server" and wx.chatlog_pid is not None
    wx.calls.clear()
    return wx, s, clk


def recycled(wx: ScriptedWeChat) -> bool:
    return "chatlog_stop" in wx.calls


def assert_no_relogin(wx: ScriptedWeChat) -> None:
    bad = [c for c in wx.calls if c.startswith(FORBIDDEN_RELOGIN_PREFIXES)]
    assert bad == [], f"回收不得重登微信/重新取钥/重装,实际出现 {bad}"


def assert_not_key_fail(s: WeChatSession) -> None:
    st = s.status()
    assert st.get("state_code") != KEY_FAIL and st.get("phase") != "key_failed", st
    if s.session is not None:
        assert s.session.state_code != KEY_FAIL and s.session.phase != "key_failed"


async def drive_maintain(s: WeChatSession, *, deadline_s: float, tick_s: float = 0.05) -> Optional[dict[str, Any]]:
    """模拟会话代理维护循环:反复调 ``maintain``,直到有回收结果或到期。"""
    end = time.monotonic() + deadline_s
    while True:
        out = await s.maintain()
        if out:
            return out
        if time.monotonic() >= end:
            return None
        await asyncio.sleep(tick_s)


async def wait_for_call(wx: ScriptedWeChat, name: str, *, timeout_s: float) -> None:
    end = time.monotonic() + timeout_s
    while name not in wx.calls:
        if time.monotonic() >= end:
            raise AssertionError(f"{timeout_s}s 内未见 {name};calls={wx.calls}")
        await asyncio.sleep(0.02)


def release_all(wx: ScriptedWeChat) -> None:
    for g in (wx.read_gate, wx.sessions_gate, wx.send_gate, wx.stop_gate, wx.serve_gate):
        if g is not None:
            g.set()


_STD_ATTRS = set(vars(logging.LogRecord("x", 0, "", 0, "", (), None))) | {"message", "asctime"}


def record_blob(r: logging.LogRecord) -> str:
    extras = " ".join(f"{k}={v}" for k, v in vars(r).items() if k not in _STD_ATTRS)
    return f"{r.getMessage()} {extras}"


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def logs():
    """WinAgent 日志器可能不向 root 传播:把收集器挂到 root 与所有已建日志器上。"""
    h = _Collect()
    touched: list[tuple[logging.Logger, int]] = []
    targets = [logging.getLogger()] + [lg for lg in logging.root.manager.loggerDict.values()
                                       if isinstance(lg, logging.Logger)]
    for lg in targets:
        lg.addHandler(h)
        touched.append((lg, lg.level))
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
    yield h
    for lg, lvl in touched:
        lg.removeHandler(h)
        lg.setLevel(lvl)


REASON_TOKENS = {"a": {"a", "(a)", "copy_bytes"}, "b": {"b", "(b)", "max_uptime", "uptime"},
                 "c": {"c", "(c)", "min_free", "temp_free"}}


def reason_seen(result: Any, records: list[logging.LogRecord], crit: str) -> bool:
    toks = REASON_TOKENS[crit]
    if isinstance(result, dict) and str(result.get("reason")) in toks:
        return True
    pat = re.compile(r"reason=\(?(" + "|".join(re.escape(t) for t in toks) + r")\)?(\s|$|,)")
    for r in records:
        if str(getattr(r, "reason", None)) in toks or pat.search(record_blob(r)):
            return True
    return False


# ============================================================== 配置键与默认值(02 §7.2 / 04 §7 / 07)
def test_config_defaults_match_02_7_2_and_07():
    """02 §7.2 [wechat] 六键与 [monitor] temp_volume_warn_free_bytes 的默认值(07 总表同值)。"""
    w, m = WechatConfig(), MonitorConfig()
    assert w.chatlog_recycle_copy_bytes == 4294967296 == 4 * GiB
    assert w.chatlog_recycle_max_uptime_s == 7200
    assert w.chatlog_recycle_min_free_bytes == 21474836480 == 20 * GiB
    assert w.chatlog_recycle_idle_wait_s == 30
    assert w.chatlog_log_max_bytes == 67108864 == 64 * MiB
    assert w.chatlog_log_backups == 5
    assert m.temp_volume_warn_free_bytes == 21474836480 == 20 * GiB
    full = WinAgentConfig()
    assert full.wechat.chatlog_recycle_copy_bytes == 4 * GiB and full.monitor.temp_volume_warn_free_bytes == 20 * GiB


def test_config_keys_load_from_winagent_toml_sections():
    """七键归 winagent.toml:六个 chatlog_* 在 [wechat],temp_volume_warn_free_bytes 在 [monitor](00 R6-118 ⑦)。"""
    cfg = load({"wechat": {"chatlog_recycle_copy_bytes": 123, "chatlog_recycle_max_uptime_s": 45,
                           "chatlog_recycle_min_free_bytes": 678, "chatlog_recycle_idle_wait_s": 9,
                           "chatlog_log_max_bytes": 4096, "chatlog_log_backups": 2},
                "monitor": {"temp_volume_warn_free_bytes": 999}})
    w = cfg.wechat
    assert (w.chatlog_recycle_copy_bytes, w.chatlog_recycle_max_uptime_s, w.chatlog_recycle_min_free_bytes,
            w.chatlog_recycle_idle_wait_s, w.chatlog_log_max_bytes, w.chatlog_log_backups) == (123, 45, 678, 9, 4096, 2)
    assert cfg.monitor.temp_volume_warn_free_bytes == 999


# ============================================================== ① 回收判据
async def test_criterion_a_copy_bytes_boundary():
    """(a) 本实例前缀副本总字节数 ≥ chatlog_recycle_copy_bytes(4 GiB)⇒ 需回收;差 1 字节不回收。"""
    wx, s, _ = await ready_session()
    wx.copy_bytes, wx.copy_files = 4 * GiB - 1, 12
    assert await s.maintain() is None and not recycled(wx)
    wx.copy_bytes = 4 * GiB
    out = await drive_maintain(s, deadline_s=10)
    assert out and recycled(wx)
    assert_no_relogin(wx)


async def test_criterion_b_uptime_counts_from_this_launch():
    """(b) chatlog server **自本次拉起起**连续运行 ≥ chatlog_recycle_max_uptime_s(7200 s)⇒ 需回收;回收重拉后重新计时。"""
    wx, s, clk = await ready_session()
    clk.advance(7199_000)
    assert await s.maintain() is None and not recycled(wx)
    clk.advance(1_000)
    out = await drive_maintain(s, deadline_s=10)
    assert out and recycled(wx) and wx.chatlog_mode == "server"
    wx.calls.clear()
    clk.advance(7199_000)                                   # 距本次(回收后)拉起不足 7200 s
    assert await s.maintain() is None and not recycled(wx)


async def test_criterion_c_needs_low_free_and_nonempty_copies():
    """(c) 临时目录所在卷空闲 < chatlog_recycle_min_free_bytes(20 GiB)**且**本实例副本非空 ⇒ 需回收。"""
    wx, s, _ = await ready_session()
    wx.temp_free_bytes = 20 * GiB                           # 恰等于阈值:不算「<」
    wx.copy_bytes, wx.copy_files = 195 * MiB, 3
    assert await s.maintain() is None and not recycled(wx)
    wx.temp_free_bytes = 20 * GiB - 1
    wx.copy_bytes, wx.copy_files = 0, 0                     # 卷低但副本为空:不回收
    assert await s.maintain() is None and not recycled(wx)
    wx.copy_bytes, wx.copy_files = 195 * MiB, 3
    out = await drive_maintain(s, deadline_s=10)
    assert out and recycled(wx)


async def test_no_criterion_no_recycle():
    wx, s, _ = await ready_session()
    wx.copy_bytes, wx.copy_files = 3 * GiB, 10
    wx.temp_free_bytes = 100 * GiB
    assert await s.maintain() is None
    assert wx.calls.count("chatlog_stop") == 0 and "chatlog_sweep" not in wx.calls


# ============================================================== ② 回收时机(读取空闲)
@pytest.mark.parametrize("busy", ["read", "sessions", "send"])
async def test_waits_for_idle_window_then_recycles_after_busy_call_ends(busy):
    """进行中的 #39 read / #41 sessions / 发送读回确认 = 读取非空闲;需回收后等空闲窗口,空闲即回收。"""
    wx, s, _ = await ready_session(chatlog_recycle_idle_wait_s=8)
    gate = asyncio.Event()
    setattr(wx, f"{busy}_gate", gate)
    if busy == "read":
        call = asyncio.create_task(s.read(talker="群A"))
    elif busy == "sessions":
        call = asyncio.create_task(s.sessions())
    else:
        call = asyncio.create_task(s.send(session_name="群A", text="hi"))
    try:
        await wait_for_call(wx, f"{busy}_begin", timeout_s=5)
        wx.copy_bytes, wx.copy_files = 5 * GiB, 20          # 判据 (a):要等空闲
        loop = asyncio.create_task(drive_maintain(s, deadline_s=20))
        await asyncio.sleep(1.5)
        assert not recycled(wx), f"{busy} 仍在进行,窗口未超时就回收了:{wx.calls}"
        gate.set()
        out = await asyncio.wait_for(loop, 15)
        assert out and recycled(wx)
        assert wx.calls.index(f"{busy}_end") < wx.calls.index("chatlog_stop")
        await asyncio.wait_for(call, 10)
    finally:
        release_all(wx)


async def test_idle_wait_timeout_forces_recycle():
    """最多等 chatlog_recycle_idle_wait_s;超时仍未空闲 ⇒ 强制回收。"""
    wx, s, _ = await ready_session(chatlog_recycle_idle_wait_s=1)
    wx.read_gate = asyncio.Event()
    call = asyncio.create_task(s.read(talker="群A"))
    try:
        await wait_for_call(wx, "read_begin", timeout_s=5)
        wx.copy_bytes, wx.copy_files = 5 * GiB, 20
        t0 = time.monotonic()
        loop = asyncio.create_task(drive_maintain(s, deadline_s=20))
        await wait_for_call(wx, "chatlog_stop", timeout_s=12)
        waited = time.monotonic() - t0
        assert waited >= 0.8, f"未等空闲窗口就回收(仅 {waited:.2f}s)"
        assert "read_end" not in wx.calls, "强制回收应发生在读取仍在进行时"
        assert await asyncio.wait_for(loop, 15)
    finally:
        release_all(wx)
        await asyncio.gather(call, return_exceptions=True)


async def test_criterion_c_does_not_wait_and_outranks_a():
    """判据 (c) 触发时不等待、立即回收;(a)(c) 同时满足按 (c) 处理(优先级 (c) > (a) > (b))。"""
    wx, s, _ = await ready_session(chatlog_recycle_idle_wait_s=8)
    wx.read_gate = asyncio.Event()
    call = asyncio.create_task(s.read(talker="群A"))
    try:
        await wait_for_call(wx, "read_begin", timeout_s=5)
        wx.copy_bytes, wx.copy_files = 5 * GiB, 20
        wx.temp_free_bytes = 5 * GiB
        t0 = time.monotonic()
        out = await asyncio.wait_for(drive_maintain(s, deadline_s=10), 12)
        assert out and recycled(wx)
        assert time.monotonic() - t0 < 4.0, "判据 (c) 不应等待空闲窗口"
        assert "read_end" not in wx.calls
    finally:
        release_all(wx)
        await asyncio.gather(call, return_exceptions=True)


async def test_reason_priority_reported_c_over_a_over_b(logs):
    """回收原因(a/b/c)进日志/结果;三者同时满足报 (c),(a)(b) 同时满足报 (a)。"""
    wx, s, clk = await ready_session()
    clk.advance(7300_000)
    wx.copy_bytes, wx.copy_files = 5 * GiB, 20
    wx.temp_free_bytes = 5 * GiB
    logs.records.clear()
    out = await drive_maintain(s, deadline_s=10)
    assert out and reason_seen(out, logs.records, "c"), (out, [record_blob(r) for r in logs.records])

    wx2, s2, clk2 = await ready_session()
    clk2.advance(7300_000)
    wx2.copy_bytes, wx2.copy_files = 5 * GiB, 20
    logs.records.clear()
    out2 = await drive_maintain(s2, deadline_s=10)
    assert out2 and reason_seen(out2, logs.records, "a"), (out2, [record_blob(r) for r in logs.records])


@pytest.mark.parametrize("stage", ["stop", "serve"])
async def test_reads_during_recycle_are_503_retryable_not_key_fail(stage):
    """回收期间新到的读取:503 NOT_READY、可重试、不判 KEY_FAIL、不伪造成功或空页;回收完恢复正常读取且游标未被推进。"""
    wx, s, _ = await ready_session()
    first = await s.read(talker="群A")
    assert [m.get("ext_msg_id") for m in first["messages"]] == ["群A:1"]
    gate = asyncio.Event()
    setattr(wx, f"{stage}_gate", gate)
    wx.copy_bytes, wx.copy_files = 5 * GiB, 20
    loop = asyncio.create_task(drive_maintain(s, deadline_s=20))
    try:
        await wait_for_call(wx, f"{stage}_enter", timeout_s=10)
        for coro in (s.read(talker="群A"), s.read(talker="群A", since_seq=0), s.sessions()):
            with pytest.raises(WaError) as e:
                await asyncio.wait_for(coro, 10)
            assert e.value.code == NOT_READY and HTTP_BY_CODE[NOT_READY] == 503
            assert e.value.http_status in (None, 503)
            assert e.value.retryable is True
        assert_not_key_fail(s)
    finally:
        gate.set()
    assert await asyncio.wait_for(loop, 15)
    assert_not_key_fail(s)
    again = await s.read(talker="群A", since_seq=0)
    assert [m.get("ext_msg_id") for m in again["messages"]] == ["群A:1"]
    assert_no_relogin(wx)


# ============================================================== ①尾 只回收本代理 server 模式;登录/取钥中不回收
async def test_not_owned_chatlog_is_never_recycled():
    """只结束本代理自己拉起、归属核验通过的 chatlog;归属不符时三判据全满足也不动它。"""
    wx, s, clk = await ready_session()
    wx.chatlog_owned = False
    clk.advance(8000_000)
    wx.copy_bytes, wx.copy_files = 9 * GiB, 30
    wx.temp_free_bytes = 1 * GiB
    assert await s.maintain() is None
    assert "chatlog_stop" not in wx.calls and "stop_enter" not in wx.calls


async def test_key_mode_chatlog_is_never_recycled():
    """取钥(key 模式,内存 hook 中)不回收(R6-96:hook 中的 chatlog 不得被强杀)。"""
    wx, s, _ = make_session()
    wx.data_key = wx.img_key = False
    await s.login_start(account_id="wx01")
    assert wx.chatlog_mode == "key"
    wx.calls.clear()
    wx.copy_bytes, wx.copy_files = 9 * GiB, 30
    wx.temp_free_bytes = 1 * GiB
    assert await s.maintain() is None
    assert "chatlog_stop" not in wx.calls and "stop_enter" not in wx.calls


async def test_login_flow_in_progress_blocks_recycle_even_in_server_mode():
    """登录流未结束时不回收(即便当前进程表现为 server 模式)。"""
    wx, s, _ = make_session()
    wx.data_key = wx.img_key = False
    await s.login_start(account_id="wx01")
    wx.chatlog_mode, wx.chatlog_pid = "server", 5304
    wx.calls.clear()
    wx.copy_bytes, wx.copy_files = 9 * GiB, 30
    wx.temp_free_bytes = 1 * GiB
    assert await s.maintain() is None
    assert "chatlog_stop" not in wx.calls and "stop_enter" not in wx.calls


# ============================================================== ③ 回收动作
async def test_recycle_order_stop_then_sweep_then_serve_without_relogin_or_key():
    """顺序固定:请求 chatlog 退出 → 进程退出后清扫 → 经 ensure_server 同一路径重拉;不重登、不取钥、两钥不动。"""
    wx, s, _ = await ready_session()
    keys_before = wx.key_state()
    wx.copy_bytes, wx.copy_files = 5 * GiB, 20
    out = await drive_maintain(s, deadline_s=10)
    assert out
    calls = wx.calls
    assert calls.index("chatlog_stop") < calls.index("chatlog_sweep") < calls.index("chatlog_serve"), calls
    assert wx.copy_bytes == 0, "清扫必须在进程退出后执行(假后端在 chatlog 运行时拒绝清扫)"
    assert wx.chatlog_mode == "server"
    assert wx.key_state() == keys_before
    assert_no_relogin(wx)
    assert_not_key_fail(s)


async def test_first_relaunch_after_recycle_is_not_throttled():
    """回收后的首次重拉立即执行、不受 ensure_server 的 30 s 节流(T6)。"""
    wx, s, _ = await ready_session()                       # 刚刚 ensure_server 拉过一次
    wx.copy_bytes, wx.copy_files = 5 * GiB, 20
    out = await drive_maintain(s, deadline_s=10)
    assert out and wx.calls.count("chatlog_serve") == 1 and wx.chatlog_mode == "server"


async def test_failed_relaunch_falls_back_to_throttled_self_heal_never_key_retry():
    """重拉失败按既有 ensure_server 自愈(30 s 节流)继续,不升级为重新取钥。"""
    wx, s, clk = await ready_session()
    wx.serve_fails = True
    wx.copy_bytes, wx.copy_files = 5 * GiB, 20
    await drive_maintain(s, deadline_s=5)
    assert recycled(wx) and wx.calls.count("chatlog_serve") == 1
    wx.serve_fails = False
    assert await s.ensure_server() is False                 # 首次失败后进入既有节流
    assert wx.calls.count("chatlog_serve") == 1
    clk.advance(31_000)
    assert await s.ensure_server() is True
    assert wx.calls.count("chatlog_serve") == 2 and wx.chatlog_mode == "server"
    assert_no_relogin(wx)
    assert_not_key_fail(s)


async def test_startup_sweep_runs_before_first_server_launch():
    """④ 启动清扫:会话代理首次拉起 server 之前清扫上一实例(及本实例)前缀残留。"""
    wx, s, _ = make_session()
    wx.copy_bytes, wx.copy_files = 3 * GiB, 9               # 上次留下的残留,chatlog 未在跑
    assert await s.ensure_server() is True
    assert "chatlog_sweep" in wx.calls and "chatlog_serve" in wx.calls
    assert wx.calls.index("chatlog_sweep") < wx.calls.index("chatlog_serve"), wx.calls
    assert wx.copy_bytes == 0
    assert_no_relogin(wx)


async def test_recycle_logs_one_structured_line_without_chat_or_keys(logs):
    """⑤ 每次回收记一行:原因、回收前副本字节数、释放字节数、删除失败数、耗时;不含聊天内容与密钥。"""
    wx, s, _ = await ready_session()
    await s.read(talker="群A")                              # 让聊天哨兵真正流经会话代理
    before = 5 * GiB + 12345
    wx.copy_bytes, wx.copy_files, wx.sweep_failed = before, 20, 2
    logs.records.clear()
    out = await drive_maintain(s, deadline_s=10)
    assert out
    blobs = [record_blob(r) for r in logs.records]
    hit = [b for b in blobs if str(before) in b]
    assert hit, f"未找到含回收前副本字节数的回收日志:{blobs}"
    assert reason_seen(out, logs.records, "a")
    line = hit[0]
    assert re.search(r"(fail|失败)\w*['\"]?\s*[=:]\s*2\b", line), f"回收日志缺删除失败数:{line}"
    assert re.search(r"(cost_ms|elapsed|耗时|duration)\w*['\"]?\s*[=:]\s*\d", line), f"回收日志缺耗时:{line}"
    assert re.search(r"(freed|释放)\w*['\"]?\s*[=:]\s*\d", line), f"回收日志缺释放字节数:{line}"
    for b in blobs:
        assert SECRET_TEXT not in b
        assert not re.search(r"\b[0-9a-fA-F]{64}\b", b), f"日志疑似含密钥:{b}"


async def test_ensure_server_path_wakes_maintenance_when_recycle_needed():
    """检查挂在会话代理既有周期触发点;ensure_server 路径同样做回收判据检查(T5),发现需回收即唤醒维护循环。"""
    wx, s, _ = await ready_session()
    waiter = asyncio.create_task(s.wait_maintain(55))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    wx.copy_bytes, wx.copy_files = 5 * GiB, 20
    await s.ensure_server()
    await asyncio.wait_for(waiter, 3)


# ============================================================== ③-2 清扫:精确前缀、精确目录、占用跳过
OWN = "resource-0123456789abcdef01234567"
LEGACY = "wxid_accept"


def _copy_names(inst: str, n: int) -> list[str]:
    base = f"{inst}_+message_{n}_+db_+8412a09f_+dead4ed940c04fa{n}.db"
    return [base, base + "-wal", base + "-shm"]


def build_filecopy(root: Path) -> dict[str, list[Path]]:
    fc = root / "filecopy_chatlog"
    fc.mkdir(parents=True)
    mine = [fc / x for x in _copy_names(OWN, 0) + _copy_names(OWN, 1) + _copy_names(LEGACY, 0)]
    keep = [fc / x for x in (
        f"wxid_acceptX_+message_0_+db_+h_+v.db",            # 实例名更长:wxid_accept 不得命中
        f"wxid_accept2_+message_0_+db_+h_+v.db",
        f"wxid_accept_b_+message_0_+db_+h_+v.db",           # 前缀后不是 `_+`
        f"{OWN}extra_+message_0_+db_+h_+v.db",
        f"x{OWN}_+message_0_+db_+h_+v.db",
        f"{OWN}.db",                                        # 没有 `_+` 分隔
        "other-program.tmp",
        "resource-ffffffffffffffffffffffff_+message_0_+db_+h_+v.db",   # 别的实例
    )]
    for i, p in enumerate(mine):
        p.write_bytes(b"m" * (100 + i))
    for p in keep:
        p.write_bytes(b"k" * 777)
    nested = fc / "nested"
    nested.mkdir()
    deep = nested / _copy_names(OWN, 9)[0]
    deep.write_bytes(b"d" * 50)
    outside = root / _copy_names(OWN, 8)[0]                 # 临时目录本层,不在 filecopy_chatlog 里
    outside.write_bytes(b"o" * 60)
    return {"dir": [fc], "mine": mine, "keep": keep + [deep, outside]}


def test_prefix_match_is_whole_instance_name_plus_separator():
    assert native.instance_of_copy(f"{LEGACY}_+message_0_+db_+h_+v.db", [OWN, LEGACY]) == LEGACY
    assert native.instance_of_copy(f"{OWN}_+message_0_+db_+h_+v.db-wal", [OWN, LEGACY]) == OWN
    assert native.instance_of_copy("wxid_a_+message_0_+db_+h_+v.db", ["wxid_a"]) == "wxid_a"
    for name in ("wxid_ab_+message_0_+db_+h_+v.db", "wxid_a_b_+message_0.db", "wxid_a.db", "xwxid_a_+m.db"):
        assert native.instance_of_copy(name, ["wxid_a"]) is None, name


def test_sweep_deletes_only_owned_copies_in_exact_directory(tmp_path):
    fs = build_filecopy(tmp_path / "temp")
    fc = fs["dir"][0]
    scanned = native.scan_instance_copies(str(fc), [OWN, LEGACY])
    assert sorted(Path(p).name for p, _, _ in scanned) == sorted(p.name for p in fs["mine"])
    assert sum(size for _, size, _ in scanned) == sum(p.stat().st_size for p in fs["mine"])
    out = native.sweep_instance_copies(str(fc), [OWN, LEGACY])
    assert all(not p.exists() for p in fs["mine"]), "本实例与已知上一实例前缀副本(含 -wal/-shm)应删掉"
    assert all(p.exists() for p in fs["keep"]), "非本实例 / 子目录 / 目录外文件不得删"
    if isinstance(out, dict) and "failed" in out:
        assert out["failed"] == 0


def test_sweep_skips_and_counts_files_that_cannot_be_deleted(tmp_path, monkeypatch):
    """仍被占用或删除失败的文件只计数、跳过,不报错、不中断。"""
    fs = build_filecopy(tmp_path / "temp")
    fc = fs["dir"][0]
    locked = fs["mine"][0]
    real_remove, real_unlink = os.remove, os.unlink

    def deny(p, *a, _orig, **k):
        if os.path.abspath(os.fspath(p)) == os.path.abspath(locked):
            raise PermissionError(32, "The process cannot access the file because it is being used", str(p))
        return _orig(p, *a, **k)

    monkeypatch.setattr(os, "remove", lambda p, *a, **k: deny(p, *a, _orig=real_remove, **k))
    monkeypatch.setattr(os, "unlink", lambda p, *a, **k: deny(p, *a, _orig=real_unlink, **k))
    out = native.sweep_instance_copies(str(fc), [OWN, LEGACY])
    assert locked.exists()
    assert all(not p.exists() for p in fs["mine"][1:])
    assert all(p.exists() for p in fs["keep"])
    if isinstance(out, dict) and "failed" in out:
        assert out["failed"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="真实文件占用语义只在 Windows 上成立")
def test_sweep_skips_file_held_open_on_windows(tmp_path):
    fs = build_filecopy(tmp_path / "temp")
    fc = fs["dir"][0]
    held = fs["mine"][1]
    with open(held, "rb"):                                  # Python 打开不带 FILE_SHARE_DELETE ⇒ 删不掉
        out = native.sweep_instance_copies(str(fc), [OWN, LEGACY])
        assert held.exists()
    assert all(not p.exists() for p in fs["mine"] if p != held)
    assert all(p.exists() for p in fs["keep"])
    if isinstance(out, dict) and "failed" in out:
        assert out["failed"] == 1


# ---------------------------------------------------------------- WinWeChat 侧:%TEMP%\filecopy_chatlog 精确目录
@pytest.fixture
def isolated_native(tmp_path, monkeypatch):
    """WinWeChat 全隔离:TMP/TEMP/~/工作目录全在 tmp_path;越界访问任何 filecopy_chatlog 一律拒绝并记违规。"""
    temp, home, work = tmp_path / "temp", tmp_path / "home", tmp_path / "reader-work"
    for d in (temp, home):
        d.mkdir()
    for k in ("TMP", "TEMP"):
        monkeypatch.setenv(k, str(temp))
    original = os.path.expanduser
    monkeypatch.setattr(native.os.path, "expanduser", lambda p: str(home) if p == "~" else original(p))
    root = os.path.normcase(os.path.realpath(tmp_path))
    violations: list[str] = []

    def guard(name):
        orig = getattr(os, name)

        def wrapped(*a, **k):
            if a and not isinstance(a[0], int):
                try:
                    s = os.fspath(a[0])
                    s = s.decode(errors="ignore") if isinstance(s, bytes) else s
                except TypeError:
                    s = ""
                if "filecopy_chatlog" in s.lower() and not os.path.normcase(os.path.realpath(s)).startswith(root):
                    violations.append(f"{name}:{s}")
                    raise PermissionError(f"acceptance guard refused {s}")
            return orig(*a, **k)
        monkeypatch.setattr(os, name, wrapped)

    for n in ("remove", "unlink", "scandir", "listdir", "rmdir"):
        guard(n)

    saved = {"root": tmp_path / "saved", "uid": LEGACY}
    db = saved["root"] / "xwechat_files" / saved["uid"] / "db_storage" / "message_0.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"synthetic-db")
    backend = native.WinWeChat(chatlog_dir=str(tmp_path / "package"), chatlog_work_dir=str(work),
                               temp_dir=str(temp))
    monkeypatch.setattr(backend, "locate", lambda: {"data_root": str(saved["root"])})
    monkeypatch.setattr(backend, "current_wxid", lambda: saved["uid"])
    monkeypatch.setattr(native, "read_chatlog_keys", lambda: {"data_key": "a" * 64, "img_key": "b" * 32})
    yield backend, temp, home, violations
    assert violations == [], f"越界访问了真实 filecopy_chatlog:{violations}"


def test_native_facts_and_sweep_use_only_temp_filecopy_dir(isolated_native):
    backend, temp, home, _ = isolated_native
    backend._prepare_server()                               # 生成 chatlog-server.json(在隔离 home),取本实例 work_dir
    cfg = json.loads((home / ".chatlog" / "chatlog-server.json").read_text(encoding="utf-8"))
    own = Path(cfg["work_dir"]).name
    assert own.startswith("resource-")
    fc = temp / "filecopy_chatlog"
    fc.mkdir()
    mine = [fc / x for x in _copy_names(own, 0) + _copy_names(LEGACY, 1)]
    for p in mine:
        p.write_bytes(b"m" * 100)
    foreign = [fc / f"{own}zz_+message_0_+db_+h_+v.db", fc / "wxid_acceptX_+message_0_+db_+h_+v.db",
               fc / "someone-else.tmp"]
    for p in foreign:
        p.write_bytes(b"f" * 10_000)
    stray = temp / _copy_names(own, 5)[0]                   # %TEMP% 本层,不在 filecopy_chatlog
    stray.write_bytes(b"s" * 10_000)
    facts = backend.chatlog_recycle_facts()
    assert facts["running"] is False
    own_bytes = 300
    assert own_bytes <= facts["copy_bytes"] <= 600 and facts["copy_bytes"] < 10_000, facts
    assert facts["temp_volume"] and isinstance(facts["temp_free_bytes"], int) and facts["temp_free_bytes"] > 0
    backend.chatlog_sweep_copies()
    assert all(not p.exists() for p in mine), "本实例前缀与旧布局 <wxid> 前缀副本(B2)应删"
    assert all(p.exists() for p in foreign) and stray.exists()


# ============================================================== ④ 日志轮转
async def test_log_rotation_bounds_size_and_backups_without_blocking_chatlog(tmp_path):
    """stdout/stderr 写 chatlog-server.log 按大小轮转:单文件 ≤ max_bytes、保留 backups 份、最旧删除;不得卡住子进程。"""
    log = tmp_path / "work" / "chatlog-server.log"
    log.parent.mkdir()
    max_bytes, backups, lines = 32 * KiB, 3, 40_000
    code = ("import sys\n"
            f"for i in range({lines}):\n"
            "    sys.stdout.write('line %06d %s\\n' % (i, 'x' * 88))\n"
            "    if i % 5000 == 0: sys.stderr.write('err %06d\\n' % i)\n"
            "sys.stdout.flush()\n")
    proc = await native.spawn_redacted([sys.executable, "-c", code], cwd=str(tmp_path), log_path=str(log),
                                       max_bytes=max_bytes, backups=backups)
    rc = await asyncio.wait_for(proc.wait(), 90)
    assert rc == 0, "chatlog 输出被日志写入卡住或异常退出"
    last = f"line {lines - 1:06d}"
    end = time.monotonic() + 15
    while True:
        files = sorted(p for p in log.parent.iterdir() if p.name.startswith(log.name))
        if any(last in p.read_text(encoding="utf-8", errors="ignore") for p in files):
            break
        assert time.monotonic() < end, "日志泵未写到最后一行"
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.2)
    files = sorted(p for p in log.parent.iterdir() if p.name.startswith(log.name))
    assert log in files
    assert len(files) <= 1 + backups, [p.name for p in files]
    for p in files:
        assert p.stat().st_size <= max_bytes, (p.name, p.stat().st_size)
    allt = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)
    assert "line 000000 " not in allt, "超出保留份数的最旧日志应删除"


def _write_lines(p: Path, n: int, width: int = 100) -> None:
    with open(p, "wb") as f:
        for i in range(n):
            f.write((f"row {i:07d} ".ljust(width - 1, "y") + "\n").encode())


def test_existing_oversized_log_rotated_before_launch_to_same_limit(tmp_path):
    """既有超限日志(实测 2.8 GB)在下次拉起时按同一上限先行轮转;B3:只保留末尾不超过上限的部分。"""
    log = tmp_path / "chatlog-server.log"
    max_bytes, backups = 16 * KiB, 5
    n = (3 * max_bytes) // 100
    _write_lines(log, n)
    for i in range(1, backups + 1):
        Path(f"{log}.{i}").write_bytes(b"old backup\n")
    assert native.rotate_oversized_log(str(log), max_bytes, backups) is True
    files = sorted(p for p in tmp_path.iterdir() if p.name.startswith(log.name))
    assert len(files) <= 1 + backups, [p.name for p in files]
    for p in files:
        assert p.stat().st_size <= max_bytes, (p.name, p.stat().st_size)
    allt = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)
    assert f"row {n - 1:07d} " in allt, "末尾内容应保留"
    assert "row 0000000 " not in allt, "超限部分(开头)不应保留"


def test_log_under_limit_is_left_alone(tmp_path):
    log = tmp_path / "chatlog-server.log"
    _write_lines(log, 10)
    before = log.read_bytes()
    assert native.rotate_oversized_log(str(log), 64 * MiB, 5) is False
    assert log.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [log.name]


# ============================================================== ⑤ 磁盘监控 WINAGENT_TEMP_DISK_LOW(04 §2.3 / 02 §3.7)
def _monitor(cfg: Optional[MonitorConfig] = None):
    from tests.conftest import Clock
    clk = Clock()
    db = Db(":memory:", clock=clk).open()
    ab = AlertBuffer(AlertConfig(), clock=clk)
    m = Monitor(db, FakeSys(), ab, cfg or MonitorConfig(), probe=FakeProbe(), clock=clk,
                disks=[DiskTarget("programdata", "C:\\ProgramData\\QTrade"),
                       DiskTarget("wechat_data", "D:\\", product_level=False)])
    return db, ab, m


def _payloads(pulled: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("code") == TEMP_CODE:
                out.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(pulled)
    return out


def test_temp_volume_alert_fires_warn_and_resolves():
    db, ab, m = _monitor()
    thr = 20 * GiB
    m.check_temp_volume(volume="C:", free_bytes=thr - 1)
    assert ab.is_firing(TEMP_CODE, "volume:C:")
    fired = [p for p in _payloads(ab.pull()) if p.get("state") != "resolved"]
    assert fired, "应发出 firing"
    p = fired[-1]
    assert p["subject"] == "volume:C:" and p["severity"] == "warn"
    assert p["evidence"]["free_bytes"] == thr - 1 and p["evidence"]["threshold_bytes"] == thr
    assert not p.get("hint_actions"), "T10:无 hint_actions"
    m.check_temp_volume(volume="C:", free_bytes=1)          # 再低也只是 warn
    assert ab.active[(TEMP_CODE, "volume:C:")].severity == "warn"
    since = ab.pull()["next_since"]
    m.check_temp_volume(volume="C:", free_bytes=thr)        # 下一次采样 ≥ 阈值 ⇒ resolved
    assert not ab.is_firing(TEMP_CODE, "volume:C:")
    resolved = [p for p in _payloads(ab.pull(since=since)) if p.get("state") == "resolved"]
    assert resolved and resolved[-1]["subject"] == "volume:C:"
    db.close()


def test_temp_volume_threshold_from_monitor_config_and_subject_per_volume():
    db, ab, m = _monitor(MonitorConfig(temp_volume_warn_free_bytes=5 * GiB))
    m.check_temp_volume(volume="D:", free_bytes=6 * GiB)
    assert not ab.is_firing(TEMP_CODE, "volume:D:")
    m.check_temp_volume(volume="D:", free_bytes=5 * GiB - 1)
    assert ab.is_firing(TEMP_CODE, "volume:D:") and not ab.is_firing(TEMP_CODE, "volume:C:")
    p = [x for x in _payloads(ab.pull()) if x.get("state") != "resolved"][-1]
    assert p["evidence"]["threshold_bytes"] == 5 * GiB
    db.close()


def test_temp_volume_alert_does_not_touch_h12_levels():
    """不进 H12 产品级三级水位、不驱动 high/critical 降级:H12 判定不因本码改变。"""
    db, ab, m = _monitor()
    before = {k: v for k, v in m.health_checks().items() if "H12" in k.upper() or "disk" in k.lower()}
    m.check_temp_volume(volume="C:", free_bytes=1)
    after = {k: v for k, v in m.health_checks().items() if "H12" in k.upper() or "disk" in k.lower()}
    assert before == after
    assert not [k for k in ab.active if str(k[0]).startswith("H12")]
    db.close()


# ---------------------------------------------------------------- 端到端:会话代理采样 → 服务告警 → GET /wa/v1/alerts
async def _start_rig_with_probe(tmp_path, probe):
    from qtrade_winagent import __version__
    from qtrade_winagent.user import UserAgent
    rig = build_rig(tmp_path)
    rig.wechat = ScriptedWeChat()
    await rig.deps.hub.start()
    ua = UserAgent(rig.deps.cfg, pipe=rig.pipe, wsl=rig.wsl, wechat=rig.wechat, power=rig.power_backend,
                   session_id="Console", user_sid=INSTALL_SID, version=__version__,
                   backup_dir=rig.deps.installer._wsl_backup_dir, clock=rig.clock,   # noqa: SLF001
                   temp_probe=probe)
    await ua.start()
    rig._tasks.append(asyncio.create_task(ua.run()))
    await asyncio.sleep(0)
    rig.user_agent = ua
    return rig


async def _close_rig(rig) -> None:
    if isinstance(rig.wechat, ScriptedWeChat):
        release_all(rig.wechat)
    for t in rig._tasks:
        t.cancel()
    await asyncio.gather(*rig._tasks, return_exceptions=True)
    await rig.deps.hub.stop()
    rig.db.close()


async def test_temp_volume_alert_end_to_end_via_user_agent_sample(tmp_path):
    """T4:会话代理采其 %TEMP% 卷 → 服务产生/恢复告警 → Agent 经 GET /wa/v1/alerts 拉走;取不到的一轮不改状态。"""
    from qtrade_winagent.main_svc import sync_temp_volume
    state: dict[str, Any] = {"free": 3 * GiB, "fail": False}

    def probe() -> dict[str, Any]:
        if state["fail"]:
            raise OSError("synthetic temp dir unavailable")
        return {"volume": "C:", "free_bytes": state["free"], "total_bytes": 500 * GiB}

    rig = await _start_rig_with_probe(tmp_path, probe)
    try:
        await sync_temp_volume(rig.deps)
        assert rig.deps.alerts.is_firing(TEMP_CODE, "volume:C:")
        async with client(rig, token=AGENT_TOKEN) as c:
            r = await c.get("/wa/v1/alerts")
            assert r.status_code == 200
            got = [p for p in _payloads(r.json()) if p.get("state") != "resolved"]
            assert got and got[-1]["subject"] == "volume:C:" and got[-1]["severity"] == "warn"
            assert got[-1]["evidence"] == {"free_bytes": 3 * GiB, "threshold_bytes": 20 * GiB} or (
                got[-1]["evidence"]["free_bytes"] == 3 * GiB and got[-1]["evidence"]["threshold_bytes"] == 20 * GiB)
            state["fail"] = True
            await sync_temp_volume(rig.deps)
            assert rig.deps.alerts.is_firing(TEMP_CODE, "volume:C:"), "无效采样不得改变告警状态"
            state.update(fail=False, free=25 * GiB)
            await sync_temp_volume(rig.deps)
            assert not rig.deps.alerts.is_firing(TEMP_CODE, "volume:C:")
            r2 = await c.get("/wa/v1/alerts")
            assert [p for p in _payloads(r2.json()) if p.get("state") == "resolved"]
    finally:
        await _close_rig(rig)


async def test_http_read_and_sessions_return_503_during_recycle(tmp_path):
    """02 §3.6 #39/#41:回收期间 GET /wa/v1/wechat/read、/sessions 返回 503 NOT_READY;回收完恢复 200。"""
    rig = await _start_rig_with_probe(tmp_path, lambda: {"volume": "C:", "free_bytes": 400 * GiB,
                                                          "total_bytes": 500 * GiB})
    wx: ScriptedWeChat = rig.wechat
    wx.data_key = wx.img_key = True
    wx.wxid, wx.logged_in, wx.ui_visible, wx.running_pid = "wxid_accept", True, True, 5101
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    wx.sessions = [{"name": "群A", "userName": "群A", "nickName": "群A"}]
    try:
        async with client(rig, token=AGENT_TOKEN) as c:
            end = time.monotonic() + 10
            while wx.chatlog_mode != "server":
                await c.get("/wa/v1/wechat/status")
                assert time.monotonic() < end, f"server 未拉起:{wx.calls}"
                await asyncio.sleep(0.1)
            ok = await c.get("/wa/v1/wechat/read?talker=群A")
            assert ok.status_code == 200
            base = len(wx.calls)
            wx.serve_gate = asyncio.Event()
            wx.copy_bytes, wx.copy_files = 5 * GiB, 20
            end = time.monotonic() + 65                       # 被 ensure_server 路径唤醒应很快;最迟一个周期(≤ 60 s)
            while "serve_enter" not in wx.calls[base:]:
                await c.get("/wa/v1/wechat/status")
                assert time.monotonic() < end, f"维护循环未触发回收:{wx.calls}"
                await asyncio.sleep(0.2)
            assert "chatlog_stop" in wx.calls
            for path in ("/wa/v1/wechat/read?talker=群A", "/wa/v1/wechat/sessions"):
                r = await c.get(path)
                assert r.status_code == 503, (path, r.status_code, r.text)
                assert r.json()["code"] == "NOT_READY"
            st = (await c.get("/wa/v1/wechat/status")).json()
            assert KEY_FAIL not in json.dumps(st, ensure_ascii=False)
            wx.serve_gate.set()
            end = time.monotonic() + 15
            while True:
                r = await c.get("/wa/v1/wechat/read?talker=群A")
                if r.status_code == 200:
                    break
                assert time.monotonic() < end, f"回收后未恢复读取:{r.status_code} {r.text}"
                await asyncio.sleep(0.2)
        assert_no_relogin(wx)
    finally:
        await _close_rig(rig)


async def test_user_agent_maintenance_period_at_most_60s(tmp_path, monkeypatch):
    """检查周期 ≤ 60 s,挂在会话代理维护循环上。"""
    seen: list[float] = []
    original = WeChatSession.wait_maintain

    async def spy(self, timeout_s):
        seen.append(float(timeout_s))
        return await original(self, timeout_s)

    monkeypatch.setattr(WeChatSession, "wait_maintain", spy)
    rig = await _start_rig_with_probe(tmp_path, lambda: {"volume": "C:", "free_bytes": 400 * GiB,
                                                          "total_bytes": 500 * GiB})
    try:
        end = time.monotonic() + 5
        while not seen and time.monotonic() < end:
            async with client(rig, token=AGENT_TOKEN) as c:
                await c.get("/wa/v1/wechat/status")
            await asyncio.sleep(0.1)
        assert seen, "会话代理未运行回收维护循环"
        assert all(0 < t <= 60 for t in seen), seen
    finally:
        await _close_rig(rig)


# ============================================================== ⑥ 最小回收间隔与效果退避(05 §2.4.10 ⑥ / 附录 E)
BASE_S, CAP_S = 600, 3600
MARGIN_S = 5


class FlakyWeChat(ScriptedWeChat):
    """在已进入停进程步骤后可让 ``chatlog_stop`` 抛错(E8 中途失败);拉起失败沿用 ``serve_fails``。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.stop_error: Optional[BaseException] = None

    async def chatlog_stop(self) -> None:
        if self.stop_error is not None:
            self.calls.append("stop_enter")
            raise self.stop_error
        await super().chatlog_stop()


async def ready_flaky(**cfgkw: Any) -> tuple[FlakyWeChat, WeChatSession, RealClock]:
    clk = RealClock()
    wx = FlakyWeChat()
    wx.data_key = wx.img_key = True
    wx.wxid, wx.logged_in, wx.ui_visible, wx.running_pid = "wxid_accept", True, True, 5101
    wx.messages = [{"talker": "群A", "seq": 1, "text": SECRET_TEXT}]
    wx.sessions = [{"name": "群A", "userName": "群A", "nickName": "群A"}]
    s = WeChatSession(wx, WechatConfig(enabled=True, **cfgkw), clock=clk)
    assert await s.ensure_server() is True and wx.chatlog_mode == "server"
    wx.calls.clear()
    return wx, s, clk


def attempts(wx: ScriptedWeChat) -> int:
    """进入停进程步骤的次数(成功或中途失败都算一次回收尝试)。"""
    return wx.calls.count("stop_enter")


def jump(clk: RealClock, t0_ms: int, sec: float) -> None:
    """把会话代理时钟拨到「t0 之后 sec 秒」。"""
    clk.advance(int(t0_ms + sec * 1000) - clk())


async def recycle_now(wx: ScriptedWeChat, s: WeChatSession, clk: RealClock) -> tuple[dict[str, Any], int]:
    n = attempts(wx)
    out = await drive_maintain(s, deadline_s=10)
    assert out, f"判据成立且不在冷却期,应回收;calls={wx.calls[-12:]}"
    assert attempts(wx) == n + 1
    return out, clk()


async def assert_no_recycle(wx: ScriptedWeChat, s: WeChatSession, *, checks: int = 3, why: str = "") -> None:
    n = attempts(wx)
    for _ in range(checks):
        assert await s.maintain() is None, why
        await asyncio.sleep(0.01)
    assert attempts(wx) == n, f"冷却期内不得回收:{why};calls={wx.calls[-12:]}"


def log_int(records: list[logging.LogRecord], name: str) -> Optional[int]:
    """从回收日志记录里取字段 ``name`` 的整数值(兼容 extra 属性、k=v 文本与字典式扩展字段)。"""
    pat = re.compile(re.escape(name) + r"['\"]?\s*[=:]\s*(-?\d+)")
    for r in reversed(records):
        v = getattr(r, name, None)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        m = pat.search(record_blob(r))
        if m:
            return int(m.group(1))
    return None


def test_appendix_e_config_defaults_and_toml_keys():
    """02 §7.2 / 07:chatlog_recycle_min_interval_s=600、chatlog_recycle_min_freed_bytes=1073741824(1 GiB),归 [wechat]。"""
    w = WechatConfig()
    assert w.chatlog_recycle_min_interval_s == 600
    assert w.chatlog_recycle_min_freed_bytes == 1073741824 == GiB
    assert WinAgentConfig().wechat.chatlog_recycle_min_interval_s == 600
    cfg = load({"wechat": {"chatlog_recycle_min_interval_s": 42, "chatlog_recycle_min_freed_bytes": 7}})
    assert (cfg.wechat.chatlog_recycle_min_interval_s, cfg.wechat.chatlog_recycle_min_freed_bytes) == (42, 7)


async def test_criterion_a_low_yield_doubles_to_cap_then_effective_resets(logs):
    """(a) 按释放量:< 1 GiB ⇒ 首次低效即翻倍 600→1200→2400→3600 封顶;≥ 1 GiB ⇒ 复位 600。审计含 cooldown_s/freed_bytes。"""
    wx, s, clk = await ready_session(chatlog_recycle_copy_bytes=512 * MiB)
    wx.serve_copy_bytes, wx.serve_copy_files = 64 * MiB, 3       # 假后端:拉起后重新复制基线副本
    expected = [1200, 2400, 3600, 3600]
    t0: Optional[int] = None
    prev = BASE_S
    for k, want in enumerate(expected):
        if t0 is not None:
            wx.copy_bytes, wx.copy_files = 600 * MiB, 9
            jump(clk, t0, prev - MARGIN_S)
            await assert_no_recycle(wx, s, why=f"第 {k} 次低效后间隔应为 {prev}s")
            jump(clk, t0, prev + MARGIN_S)
        wx.copy_bytes, wx.copy_files = 600 * MiB, 9              # 释放量 600 MiB(或扣掉重拷 536 MiB)均 < 1 GiB
        logs.records.clear()
        out, t0 = await recycle_now(wx, s, clk)
        assert reason_seen(out, logs.records, "a")
        assert log_int(logs.records, "cooldown_s") == want, [record_blob(r) for r in logs.records]
        freed = log_int(logs.records, "freed_bytes")
        assert freed is not None and 0 <= freed < GiB, freed
        prev = want
    wx.copy_bytes, wx.copy_files = 2 * GiB, 30                   # 有效释放 ⇒ 复位
    jump(clk, t0, CAP_S - MARGIN_S)
    await assert_no_recycle(wx, s, why="封顶后间隔应为 3600s")
    jump(clk, t0, CAP_S + MARGIN_S)
    logs.records.clear()
    out, t0 = await recycle_now(wx, s, clk)
    assert log_int(logs.records, "cooldown_s") == BASE_S
    freed = log_int(logs.records, "freed_bytes")
    assert freed is not None and freed >= GiB, freed
    wx.copy_bytes, wx.copy_files = 600 * MiB, 9
    jump(clk, t0, BASE_S - MARGIN_S)
    await assert_no_recycle(wx, s, why="有效后应复位为 600s")
    jump(clk, t0, BASE_S + MARGIN_S)
    await recycle_now(wx, s, clk)
    assert_no_relogin(wx)


async def test_criterion_b_uses_freed_bytes_rule_and_cooldown():
    """(b) 同按释放量口径;冷却期内运行时长判据持续成立也不回收。"""
    wx, s, clk = await ready_session(chatlog_recycle_max_uptime_s=100)
    clk.advance(101_000)
    wx.copy_bytes, wx.copy_files = 300 * MiB, 6
    _, t0 = await recycle_now(wx, s, clk)                        # 释放 300 MiB < 1 GiB ⇒ 1200
    jump(clk, t0, BASE_S + MARGIN_S)                              # 运行时长早已 ≥ 100 s
    await assert_no_recycle(wx, s, why="(b) 低效后间隔应翻倍为 1200s")
    wx.copy_bytes, wx.copy_files = 2 * GiB, 30
    jump(clk, t0, 1200 + MARGIN_S)
    _, t0 = await recycle_now(wx, s, clk)                        # 释放 2 GiB ≥ 1 GiB ⇒ 复位 600
    jump(clk, t0, BASE_S - MARGIN_S)
    await assert_no_recycle(wx, s, why="(b) 有效后间隔 600s 内不回收")
    jump(clk, t0, BASE_S + MARGIN_S)
    await recycle_now(wx, s, clk)


async def test_min_interval_and_freed_threshold_come_from_config(logs):
    """基础间隔与有效阈值取配置:base=100 ⇒ 低效 200、有效复位 100;min_freed=100 MiB ⇒ 300 MiB 为有效。"""
    wx, s, clk = await ready_session(chatlog_recycle_copy_bytes=256 * MiB, chatlog_recycle_min_interval_s=100)
    wx.copy_bytes, wx.copy_files = 300 * MiB, 6
    logs.records.clear()
    _, t0 = await recycle_now(wx, s, clk)
    assert log_int(logs.records, "cooldown_s") == 200
    wx.copy_bytes, wx.copy_files = 300 * MiB, 6
    jump(clk, t0, 200 - MARGIN_S)
    await assert_no_recycle(wx, s)
    wx2, s2, clk2 = await ready_session(chatlog_recycle_copy_bytes=256 * MiB,
                                        chatlog_recycle_min_freed_bytes=100 * MiB)
    wx2.copy_bytes, wx2.copy_files = 300 * MiB, 6
    logs.records.clear()
    await recycle_now(wx2, s2, clk2)
    assert log_int(logs.records, "cooldown_s") == BASE_S


async def test_cooldown_after_any_recycle_blocks_all_criteria_including_c():
    """任一次回收完成后,间隔内 (a)(b)(c) 一律不回收(含 (c),不再「立即」);期满后 (c) 照常回收。"""
    wx, s, clk = await ready_session(chatlog_recycle_max_uptime_s=100)
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    _, t0 = await recycle_now(wx, s, clk)                        # (a) 有效 ⇒ 600
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30                   # (a) 再成立
    wx.temp_free_bytes = 5 * GiB                                 # (c) 成立
    jump(clk, t0, BASE_S - MARGIN_S)                              # (b) 也成立
    n = attempts(wx)
    for _ in range(3):
        await s.ensure_server()                                  # ensure_server 触发点同样受间隔约束
    await assert_no_recycle(wx, s, why="(a)(b)(c) 同时成立也须等最小间隔")
    assert attempts(wx) == n
    jump(clk, t0, BASE_S + MARGIN_S)
    await recycle_now(wx, s, clk)


async def test_criterion_c_persisting_does_not_recycle_every_cycle_and_backs_off_to_cap():
    """E7:卷空闲持续不足(拉起后重新复制基线副本)⇒ 间隔内不重复回收;每次下一检查仍不足 ⇒ 低效翻倍,封顶 3600。"""
    wx, s, clk = await ready_session()
    wx.serve_copy_bytes, wx.serve_copy_files = 1700 * MiB, 9     # 每次拉起重新复制约 1.7 GB
    wx.copy_bytes, wx.copy_files = 1700 * MiB, 9
    wx.temp_free_bytes = 5 * GiB                                 # 因副本以外原因长期不足
    _, t0 = await recycle_now(wx, s, clk)
    prev = BASE_S
    for want in (1200, 2400, 3600, 3600):
        for sec in range(60, want - MARGIN_S, 60):               # 每 60 s 一次维护检查
            jump(clk, t0, sec)
            await assert_no_recycle(wx, s, checks=1, why=f"(c) 持续成立,当前间隔应为 {want}s(第 {sec}s)")
        jump(clk, t0, want - MARGIN_S)
        await assert_no_recycle(wx, s, checks=1, why=f"(c) 低效后间隔应为 {want}s,不是 {prev}s")
        jump(clk, t0, want + MARGIN_S)
        _, t0 = await recycle_now(wx, s, clk)
        prev = want
    assert_no_relogin(wx)


async def test_criterion_c_effect_ignores_freed_bytes():
    """E7:(c) 不看释放量——释放 5 GiB(≥ 1 GiB)但下一次检查卷空闲仍不足 ⇒ 仍判低效、间隔翻倍。"""
    wx, s, clk = await ready_session()
    wx.serve_copy_bytes, wx.serve_copy_files = 1700 * MiB, 9
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    wx.temp_free_bytes = 5 * GiB
    _, t0 = await recycle_now(wx, s, clk)
    jump(clk, t0, 60)
    await assert_no_recycle(wx, s, checks=1)                     # 下一次检查:卷仍不足
    jump(clk, t0, BASE_S + MARGIN_S)
    await assert_no_recycle(wx, s, why="(c) 低效应翻倍为 1200s,不得按释放量判有效")
    jump(clk, t0, 1200 + MARGIN_S)
    await recycle_now(wx, s, clk)


async def test_criterion_c_effective_when_volume_recovers_resets_even_if_freed_small():
    """E7:(c) 回收并重拉后的下一次检查卷空闲 ≥ 阈值 ⇒ 有效 ⇒ 间隔复位 600(即便释放量 < 1 GiB)。"""
    wx, s, clk = await ready_session()
    wx.serve_copy_bytes, wx.serve_copy_files = 200 * MiB, 3
    wx.copy_bytes, wx.copy_files = 200 * MiB, 3
    wx.temp_free_bytes = 5 * GiB
    _, t0 = await recycle_now(wx, s, clk)
    jump(clk, t0, 60)
    await assert_no_recycle(wx, s, checks=1)                     # 卷仍不足 ⇒ 低效 ⇒ 1200
    jump(clk, t0, 1200 + MARGIN_S)
    _, t0 = await recycle_now(wx, s, clk)                        # 释放仅 200 MiB
    wx.temp_free_bytes = 30 * GiB
    jump(clk, t0, 60)
    await assert_no_recycle(wx, s, checks=1)                     # 下一次检查:卷已恢复 ⇒ 有效
    wx.temp_free_bytes = 5 * GiB
    jump(clk, t0, BASE_S - MARGIN_S)
    await assert_no_recycle(wx, s, why="有效后间隔为 600s")
    jump(clk, t0, BASE_S + MARGIN_S)
    await recycle_now(wx, s, clk)


async def test_criterion_c_after_cooldown_still_does_not_wait_for_idle():
    """冷却期满后若 (c) 仍成立,回收仍不等空闲窗口。"""
    wx, s, clk = await ready_session(chatlog_recycle_idle_wait_s=8)
    wx.serve_copy_bytes, wx.serve_copy_files = 1700 * MiB, 9
    wx.copy_bytes, wx.copy_files = 1700 * MiB, 9
    wx.temp_free_bytes = 5 * GiB
    _, t0 = await recycle_now(wx, s, clk)
    jump(clk, t0, 60)
    await assert_no_recycle(wx, s, checks=1)
    jump(clk, t0, 1200 + MARGIN_S)
    wx.read_gate = asyncio.Event()
    call = asyncio.create_task(s.read(talker="群A"))
    try:
        await wait_for_call(wx, "read_begin", timeout_s=5)
        t1 = time.monotonic()
        n = attempts(wx)
        out = await asyncio.wait_for(drive_maintain(s, deadline_s=10), 12)
        assert out and attempts(wx) == n + 1
        assert time.monotonic() - t1 < 4.0, "冷却期满后 (c) 仍不应等待空闲窗口"
        assert "read_end" not in wx.calls, "回收应发生在读取仍在进行时"
    finally:
        release_all(wx)
        await asyncio.gather(call, return_exceptions=True)


async def _fail_mid_recycle(crit: str, stage: str) -> tuple[FlakyWeChat, WeChatSession, RealClock, int]:
    """让一次回收在进入停进程步骤后失败(stage=stop:停进程抛错;stage=serve:清扫后重拉失败),返回失败时刻。"""
    wx, s, clk = await ready_flaky(chatlog_recycle_idle_wait_s=1)
    wx.serve_copy_bytes, wx.serve_copy_files = 1700 * MiB, 9
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30                   # (a) 下释放量本应 ≥ 1 GiB
    if crit == "c":
        wx.temp_free_bytes = 5 * GiB
    if stage == "stop":
        wx.stop_error = OSError(5, "synthetic: chatlog stop failed")
    else:
        wx.serve_fails = True
    end = time.monotonic() + 10
    while attempts(wx) == 0:
        try:
            await s.maintain()
        except Exception:                                        # noqa: BLE001 —— 验收只看冷却后果
            pass
        assert time.monotonic() < end, f"未进入停进程步骤:{wx.calls}"
        await asyncio.sleep(0.05)
    t0 = clk()
    wx.stop_error, wx.serve_fails = None, False
    if stage == "serve":
        clk.advance(31_000)                                      # 重拉失败后按既有 30 s 节流自愈
        assert await s.ensure_server() is True
    return wx, s, clk, t0


async def _assert_doubled_after_mid_failure(wx: FlakyWeChat, s: WeChatSession, clk: RealClock, t0: int,
                                            crit: str, stage: str) -> None:
    wx.temp_free_bytes = 30 * GiB                                # 下一次检查卷已恢复:若按 E7 会误判 (c) 有效
    jump(clk, t0, 60)
    await assert_no_recycle(wx, s, checks=1)
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    if crit == "c":
        wx.temp_free_bytes = 5 * GiB
    for sec in (BASE_S + MARGIN_S, 1200 - MARGIN_S):
        jump(clk, t0, sec)
        await assert_no_recycle(wx, s, why=f"E8:{stage} 中途失败应按释放 0 判低效、立即翻倍为 1200s(第 {sec}s)")


@pytest.mark.parametrize("crit", ["a", "c"])
async def test_stop_failure_counts_as_zero_freed_and_doubles_immediately(crit):
    """E8:进入停进程后中途失败(停进程抛错),不论判据(含 (c)),按释放 0 判低效、立即翻倍,不等下一次检查。"""
    wx, s, clk, t0 = await _fail_mid_recycle(crit, "stop")
    await _assert_doubled_after_mid_failure(wx, s, clk, t0, crit, "stop")
    assert_no_relogin(wx)
    assert_not_key_fail(s)


@pytest.mark.parametrize("crit,free_at_next_check,want", [
    ("a", None, BASE_S),                                         # (a):释放 5 GiB ≥ 1 GiB ⇒ 有效
    ("c", 30 * GiB, BASE_S),                                     # (c):下一次检查卷已恢复 ⇒ 有效
    ("c_still_low", 5 * GiB, 1200),                              # (c):下一次检查卷仍不足 ⇒ 低效
], ids=["a", "c", "c_still_low"])
async def test_relaunch_failure_after_sweep_counts_as_mid_failure(crit, free_at_next_check, want):
    """附录 E 第 9 条:停进程与清扫均成功、仅重拉失败**不算** E8 中途失败,效果按触发判据口径正常判定——
    (a)/(b) 按释放量,(c) 按下一次检查卷空闲;重拉失败交既有 ensure_server 自愈与 30 s 节流。"""
    base = "c" if crit.startswith("c") else crit
    wx, s, clk, t0 = await _fail_mid_recycle(base, "serve")
    if free_at_next_check is not None:
        wx.temp_free_bytes = free_at_next_check
    jump(clk, t0, 60)
    await assert_no_recycle(wx, s, checks=1)                     # 下一次检查((c) 在此定效)
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    if base == "c":
        wx.temp_free_bytes = 5 * GiB
    jump(clk, t0, want - MARGIN_S)
    await assert_no_recycle(wx, s, why=f"重拉失败按 {crit} 口径判效果后,间隔应为 {want}s")
    jump(clk, t0, want + MARGIN_S)
    await recycle_now(wx, s, clk)
    assert_no_relogin(wx)
    assert_not_key_fail(s)


async def test_recycle_resumes_after_stop_failure_cooldown_expires():
    """附录 E 第 10 条(E9):停进程失败时 maintain() 不得抛出;会话代理不得持续 BUSY(wechat_cleanup_pending),
    chatlog 仍运行则读取照常;按 E8 判低效翻倍冷却,冷却期满后允许再次回收。"""
    wx, s, clk = await ready_flaky(chatlog_recycle_idle_wait_s=1)
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    wx.stop_error = OSError(5, "synthetic: chatlog stop failed")
    end = time.monotonic() + 10
    while attempts(wx) == 0:
        try:
            await s.maintain()
        except Exception as e:                                   # noqa: BLE001
            pytest.fail(f"E9:停进程失败时 maintain() 不得抛出,实际 {e!r}")
        assert time.monotonic() < end, f"未进入停进程步骤:{wx.calls}"
        await asyncio.sleep(0.05)
    t0 = clk()
    wx.stop_error = None
    assert wx.chatlog_pid is not None, "假后端:停进程失败,chatlog 仍在运行"
    for sec in (5, 60, BASE_S + MARGIN_S):                       # 冷却期内:不卡死、读取照常、仍在冷却
        jump(clk, t0, sec)
        try:
            await s.ensure_server()
            got = await s.read(talker="群A", since_seq=0)
            listed = await s.sessions()
            out = await s.maintain()
        except WaError as e:
            pytest.fail(f"E9:停进程失败后不得持续返回 {e.code}/{e.reason}(第 {sec}s)")
        assert [m.get("ext_msg_id") for m in got["messages"]] == ["群A:1"]
        assert any("群A" in json.dumps(x, ensure_ascii=False) for x in listed)
        assert out is None and not {"chatlog_stop", "chatlog_sweep", "chatlog_serve"} & set(wx.calls), \
            f"E8:冷却期内不得再回收、不得停掉仍在运行的 chatlog(第 {sec}s):{wx.calls}"
        assert "wechat_cleanup_pending" not in json.dumps(s.status(), ensure_ascii=False, default=str)
    n = attempts(wx)
    jump(clk, t0, 1200 + MARGIN_S)
    last: Any = None
    end = time.monotonic() + 10
    while "chatlog_serve" not in wx.calls and time.monotonic() < end:
        try:
            last = await s.maintain()
        except Exception as e:                                   # noqa: BLE001
            pytest.fail(f"E9:maintain() 不得抛出,实际 {e!r}")
        await asyncio.sleep(0.05)
    assert attempts(wx) > n and "chatlog_serve" in wx.calls, f"冷却期满未再回收:last={last!r} calls={wx.calls}"
    assert wx.chatlog_mode == "server"
    assert_no_relogin(wx)
    assert_not_key_fail(s)


async def test_cooldown_logs_at_most_one_line_per_check_with_remaining_seconds(logs):
    """冷却期判据成立:不回收,每个检查周期至多一行 cooldown(含剩余秒数);ensure_server 多次调用不刷屏;不含聊天与密钥。"""
    wx, s, clk = await ready_session()
    await s.read(talker="群A")
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    _, t0 = await recycle_now(wx, s, clk)                        # 有效 ⇒ 600
    wx.copy_bytes, wx.copy_files = 5 * GiB, 30
    total = 0
    for sec in (60, 120, 300):
        jump(clk, t0, sec)
        logs.records.clear()
        for _ in range(5):
            await s.ensure_server()
        await assert_no_recycle(wx, s, checks=1)
        uniq = list({id(r): r for r in logs.records}.values())   # 收集器挂在多个日志器上,同一条记录会经传播重复收到
        blobs = [record_blob(r) for r in uniq]
        lines = [b for b in blobs if "cooldown" in b.lower()]
        assert len(lines) <= 1, f"一个检查周期至多一行 cooldown:{lines}"
        for line in lines:
            left = BASE_S - sec
            nums = [int(x) for x in re.findall(r"(?<![\d.])(\d+)(?![\d.])", line)]
            assert any(abs(v - left) <= 10 for v in nums) or any(abs(v - left * 1000) <= 10_000 for v in nums), \
                f"cooldown 行应含剩余秒数≈{left}:{line}"
        for b in blobs:
            assert SECRET_TEXT not in b
            assert not re.search(r"\b[0-9a-fA-F]{64}\b", b)
        total += len(lines)
    assert total >= 1, "冷却期内判据成立应在日志中记录 cooldown"


async def test_reads_and_sessions_work_normally_during_cooldown():
    """冷却期内读取照常进行,不按 ② 返回未就绪,chatlog 不被停。"""
    wx, s, clk = await ready_session()
    wx.serve_copy_bytes, wx.serve_copy_files = 1700 * MiB, 9
    wx.copy_bytes, wx.copy_files = 1700 * MiB, 9
    wx.temp_free_bytes = 5 * GiB
    _, t0 = await recycle_now(wx, s, clk)
    n = attempts(wx)
    for sec in (30, 60, 400):
        jump(clk, t0, sec)
        assert await s.maintain() is None
        got = await s.read(talker="群A", since_seq=0)
        assert [m.get("ext_msg_id") for m in got["messages"]] == ["群A:1"]
        listed = await s.sessions()
        assert any("群A" in json.dumps(x, ensure_ascii=False) for x in listed), listed
        assert wx.chatlog_mode == "server" and wx.chatlog_pid is not None
    assert attempts(wx) == n
    assert_not_key_fail(s)


async def test_cooldown_and_backoff_reset_when_session_agent_restarts(logs):
    """冷却与退避是会话代理进程内状态,不落盘:重启后无冷却、间隔回到基础值。"""
    cfg = dict(chatlog_recycle_copy_bytes=512 * MiB)
    wx, s, clk = await ready_session(**cfg)
    t0 = 0
    for _ in range(2):                                           # 两次低效 ⇒ 当前间隔 2400
        wx.copy_bytes, wx.copy_files = 600 * MiB, 9
        if attempts(wx):
            jump(clk, t0, 1200 + MARGIN_S)
        logs.records.clear()
        _, t0 = await recycle_now(wx, s, clk)
    assert log_int(logs.records, "cooldown_s") == 2400
    await wx.chatlog_stop()                                      # 旧会话代理退出,其 chatlog 随之结束
    s2 = WeChatSession(wx, WechatConfig(enabled=True, **cfg), clock=clk)
    assert await s2.ensure_server() is True and wx.chatlog_mode == "server"
    wx.copy_bytes, wx.copy_files = 600 * MiB, 9
    logs.records.clear()
    await recycle_now(wx, s2, clk)                               # 无冷却:立即回收
    assert log_int(logs.records, "cooldown_s") == 1200, "重启后间隔应从基础值 600 起翻倍,而不是 4800/3600"


async def test_temp_disk_alert_unaffected_by_recycle_cooldown(tmp_path):
    """冷却期内 WINAGENT_TEMP_DISK_LOW 照常产生/恢复;磁盘问题靠告警可见,不靠反复强停。"""
    from qtrade_winagent.main_svc import sync_temp_volume
    state: dict[str, Any] = {"free": 3 * GiB}
    rig = await _start_rig_with_probe(tmp_path, lambda: {"volume": "C:", "free_bytes": state["free"],
                                                          "total_bytes": 500 * GiB})
    wx: ScriptedWeChat = rig.wechat
    wx.data_key = wx.img_key = True
    wx.wxid, wx.logged_in, wx.ui_visible, wx.running_pid = "wxid_accept", True, True, 5101
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    wx.sessions = [{"name": "群A", "userName": "群A", "nickName": "群A"}]
    try:
        async with client(rig, token=AGENT_TOKEN) as c:
            end = time.monotonic() + 10
            while wx.chatlog_mode != "server":
                await c.get("/wa/v1/wechat/status")
                assert time.monotonic() < end, f"server 未拉起:{wx.calls}"
                await asyncio.sleep(0.1)
            wx.serve_copy_bytes, wx.serve_copy_files = 1700 * MiB, 9
            wx.copy_bytes, wx.copy_files = 1700 * MiB, 9
            wx.temp_free_bytes = 3 * GiB                         # (c) 持续成立
            base = len(wx.calls)
            end = time.monotonic() + 65
            while not ("stop_enter" in wx.calls[base:] and "serve_enter" in wx.calls[base:]
                       and wx.chatlog_mode == "server"):
                await c.get("/wa/v1/wechat/status")
                assert time.monotonic() < end, f"维护循环未触发首次回收:{wx.calls}"
                await asyncio.sleep(0.2)
            first = attempts(wx)
            await sync_temp_volume(rig.deps)
            assert rig.deps.alerts.is_firing(TEMP_CODE, "volume:C:")
            for _ in range(10):                                  # 冷却期内反复经 ensure_server 路径与读取
                assert (await c.get("/wa/v1/wechat/status")).status_code == 200
                r = await c.get("/wa/v1/wechat/read?talker=群A")
                assert r.status_code == 200, (r.status_code, r.text)
                await asyncio.sleep(0.2)
            await sync_temp_volume(rig.deps)
            assert rig.deps.alerts.is_firing(TEMP_CODE, "volume:C:")
            state["free"] = 25 * GiB
            await sync_temp_volume(rig.deps)
            assert not rig.deps.alerts.is_firing(TEMP_CODE, "volume:C:")
            got = (await c.get("/wa/v1/alerts")).json()
            assert [p for p in _payloads(got) if p.get("state") == "resolved"]
            assert attempts(wx) == first, f"冷却期内不得再次强停 chatlog:{wx.calls}"
    finally:
        await _close_rig(rig)
