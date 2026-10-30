"""R6-118 附录 E:chatlog 回收的最小回收间隔与效果退避(05 §2.4.10 ⑥;02 §7.2;07;DECISION-3 附录 E 含 E7/E8)。

全部用 ``FakeWeChat`` + 可拨时钟;``serve_copy_bytes`` 模拟真机「每次拉起都重新复制约 1.7 GB 副本」。
不碰真 chatlog 进程、真 ``%TEMP%\\filecopy_chatlog`` 与真微信账号。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import pytest

from qtrade_winagent import __version__
from qtrade_winagent import user as user_mod
from qtrade_winagent import wechat as wechat_mod
from qtrade_winagent.config import WechatConfig, load
from qtrade_winagent.errors import BUSY, WaError
from qtrade_winagent.fakes import FakeWeChat
from qtrade_winagent.main_svc import sync_temp_volume
from qtrade_winagent.user import UserAgent
from qtrade_winagent.wechat import WeChatSession

from tests.conftest import AGENT_TOKEN, INSTALL_SID, Clock, attach_user_agent, build_rig, client

GiB = 1 << 30
MiB = 1 << 20
COPY_LIMIT = 4 * GiB              # chatlog_recycle_copy_bytes
FREE_LIMIT = 20 * GiB             # chatlog_recycle_min_free_bytes
LOW_FREE = 15 * GiB               # C 盘因副本以外的原因长期紧张
HIGH_FREE = 30 * GiB
BASE_S = 600                      # chatlog_recycle_min_interval_s
CAP_S = 3600                      # 退避封顶
MIN_FREED = GiB                   # chatlog_recycle_min_freed_bytes
REBUILD = 1_700_000_000           # 真机每次拉起重新复制约 1.7 GB
UPTIME_S = 60                     # 判据 (b) 用例把运行时长阈值调小,冷却期内 (b) 持续成立
REASON = {"a": "copy_bytes", "b": "max_uptime", "c": "min_free"}
CFG = {"a": {}, "b": {"chatlog_recycle_max_uptime_s": UPTIME_S}, "c": {}}
CHECK_MS = 30_000                 # 维护循环周期(user.WECHAT_MAINTAIN_S)


@dataclass
class StickyWeChat(FakeWeChat):
    """D2 场景:``locked_bytes`` 那部分旧副本被别的句柄占用,清扫删不掉。"""
    locked_bytes: int = 0

    def chatlog_sweep_copies(self, *, budget_s: float = 15.0):
        if self.chatlog_pid is not None or not self.locked_bytes:
            return super().chatlog_sweep_copies(budget_s=budget_s)
        self.calls.append("chatlog_sweep")
        before = self.copy_bytes
        keep = min(self.locked_bytes, before)
        self.copy_bytes, self.copy_files = keep, 1 if keep else 0
        return {"before_bytes": before, "freed_bytes": before - keep, "deleted": 1, "failed": 1 if keep else 0,
                "complete": True, "instances": 2}


async def serving(clk=None, *, wx=None, **cfgkw):
    """两钥在 + 已登录 ⇒ ensure_server 拉起本代理的 server 模式 chatlog。"""
    clk = clk or Clock()
    wx = wx if wx is not None else FakeWeChat()
    s = WeChatSession(wx, WechatConfig(enabled=True, **cfgkw), clock=clk)
    wx.data_key = wx.img_key = True
    wx.wxid = "wxid_abc"
    assert await s.ensure_server() is True
    assert wx.chatlog_mode == "server"
    return wx, s, clk


def arm(wx, crit):
    """让判据 crit 成立((b) 靠时钟走过 UPTIME_S)。"""
    if crit == "a":
        wx.copy_bytes, wx.copy_files = max(wx.copy_bytes, COPY_LIMIT), max(wx.copy_files, 5)
    elif crit == "c":
        wx.temp_free_bytes = LOW_FREE
        wx.copy_bytes, wx.copy_files = max(wx.copy_bytes, 1), max(wx.copy_files, 1)


def at(clk, base_ms, offset_ms):
    clk.now_ms = base_ms + offset_ms


def stops(wx):
    return wx.calls.count("chatlog_stop")


async def no_recycle(wx, s):
    n = stops(wx)
    assert await s.maintain() is None
    assert stops(wx) == n, "冷却期内不得回收"


async def recycle(wx, s, crit):
    out = await s.maintain()
    assert out and out["recycled"] is True and out["criterion"] == crit, out
    return out


def holds(s, wx, crit):
    """判据确实成立(否则「不回收」的断言是空转)。"""
    return s._recycle_reason(wx.chatlog_recycle_facts()) == REASON[crit]           # noqa: SLF001


async def first_effective_recycle(crit, clk=None, wx=None):
    """拉起后做一次「有效」回收,返回 (wx, s, clk, 完成时刻);(c) 以下一次检查卷空闲恢复判有效。"""
    wx, s, clk = await serving(clk, wx=wx, **CFG[crit])
    if crit == "b":
        wx.copy_bytes, wx.copy_files = 2 * GiB, 3
        clk.advance(UPTIME_S * 1000)
    arm(wx, crit)
    await recycle(wx, s, crit)
    done = clk.now_ms
    if crit == "c":
        at(clk, done, CHECK_MS)
        wx.temp_free_bytes = HIGH_FREE
        await no_recycle(wx, s)
    return wx, s, clk, done


async def woken_within(s, timeout_s):
    t0 = time.monotonic()
    await s.wait_maintain(timeout_s)
    return time.monotonic() - t0


# ---------------------------------------------------------------- 新配置键
def test_new_cooldown_keys_default_values():
    c = WechatConfig()
    assert c.chatlog_recycle_min_interval_s == 600
    assert c.chatlog_recycle_min_freed_bytes == 1073741824


def test_new_cooldown_keys_load_from_wechat_section():
    w = load({"wechat": {"chatlog_recycle_min_interval_s": 123, "chatlog_recycle_min_freed_bytes": 456}}).wechat
    assert (w.chatlog_recycle_min_interval_s, w.chatlog_recycle_min_freed_bytes) == (123, 456)


# ---------------------------------------------------------------- ⑥ 最小回收间隔:三判据 × 两条触发路径
@pytest.mark.parametrize("crit", ["a", "b", "c"])
async def test_min_interval_applies_to_every_criterion_on_maintenance_path(crit):
    wx, s, clk, done = await first_effective_recycle(crit)
    for off in (UPTIME_S * 1000 + 1, 300_000, BASE_S * 1000 - 1):
        at(clk, done, off)
        arm(wx, crit)
        assert holds(s, wx, crit)
        await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    arm(wx, crit)
    await recycle(wx, s, crit)


@pytest.mark.parametrize("crit", ["a", "b", "c"])
async def test_min_interval_applies_on_ensure_server_path_without_early_wakeup(crit):
    wx, s, clk, done = await first_effective_recycle(crit)
    at(clk, done, 300_000)
    arm(wx, crit)
    assert holds(s, wx, crit)
    n = stops(wx)
    for _ in range(5):
        assert await s.ensure_server() is False
    assert stops(wx) == n
    assert await woken_within(s, 0.3) >= 0.25, "冷却期内 ensure_server 不得提前唤醒维护循环"
    # 对照:冷却期满后同一路径会唤醒
    at(clk, done, BASE_S * 1000)
    arm(wx, crit)
    await s.ensure_server()
    assert await woken_within(s, 2.0) < 1.0
    await recycle(wx, s, crit)


async def test_criterion_c_is_no_longer_immediate_in_user_agent_loop(tmp_path, monkeypatch):
    """(c) 持续成立 + 每次拉起重新复制 1.7 GB:维护循环高频运转、ensure_server 反复调用,冷却期内只回收一次;读取正常。"""
    monkeypatch.setattr(user_mod, "WECHAT_MAINTAIN_S", 0.05)
    rig = build_rig(tmp_path)
    try:
        ua = await attach_user_agent(rig)
        wx = rig.wechat
        wx.serve_copy_bytes, wx.serve_copy_files = REBUILD, 12
        wx.data_key = wx.img_key = True
        wx.wxid = "wxid_abc"
        wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
        assert await ua.wechat.ensure_server() is True
        wx.temp_free_bytes = LOW_FREE
        deadline = time.monotonic() + 3
        while stops(wx) == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert stops(wx) == 1, "维护循环应先回收一次"
        while "chatlog_serve" not in wx.calls[wx.calls.index("chatlog_stop"):] and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)                                     # 等回收收尾(置回「非回收中」)
        assert wx.copy_bytes == REBUILD                               # 重拉后副本又回来了,(c) 仍成立
        async with client(rig, token=AGENT_TOKEN) as c:
            t_end = time.monotonic() + 1.5                            # ≈ 30 个维护周期
            while time.monotonic() < t_end:
                await ua.wechat.ensure_server()
                r = await c.get("/wa/v1/wechat/read?talker=群A")
                assert r.status_code == 200 and r.json()["messages"][0]["seq"] == 1
                await asyncio.sleep(0.05)
        assert stops(wx) == 1 and wx.calls.count("chatlog_serve") == 2
    finally:
        for t in rig._tasks:                                         # noqa: SLF001
            t.cancel()
        if rig.user_agent is not None:
            await rig.user_agent.stop()
        await rig.deps.hub.stop()
        rig.db.close()


# ---------------------------------------------------------------- ⑥ 效果退避:(a)/(b) 按释放量
def _sticky_setup(crit):
    wx = StickyWeChat()
    if crit == "a":
        wx.serve_copy_bytes = 200 * MiB
        return wx, 200 * MiB
    wx.serve_copy_bytes = 100 * MiB
    return wx, 100 * MiB


@pytest.mark.parametrize("crit", ["a", "b"])
async def test_low_yield_doubles_to_cap_then_effective_yield_resets(crit):
    wx, per = _sticky_setup(crit)
    wx, s, clk = await serving(wx=wx, **CFG[crit])
    locked = 4 * GiB + 512 * MiB
    if crit == "a":                                                 # 4.5 GiB 旧副本被占用(D2)
        wx.locked_bytes = locked
        wx.copy_bytes += locked
    else:
        clk.advance(UPTIME_S * 1000)
    out = await recycle(wx, s, crit)
    expected = [1200, 2400, 3600, 3600]
    for i, cd in enumerate(expected):
        assert out["freed_bytes"] == per and out["cooldown_s"] == cd, (i, out)
        done = clk.now_ms
        at(clk, done, cd * 1000 - 1)
        assert holds(s, wx, crit)
        await no_recycle(wx, s)
        at(clk, done, cd * 1000)
        if i < len(expected) - 1:
            out = await recycle(wx, s, crit)
    # 有效一次 ⇒ 复位为基础值
    if crit == "a":
        wx.locked_bytes = 0
    else:
        wx.copy_bytes = 2 * GiB
    out = await recycle(wx, s, crit)
    assert out["freed_bytes"] >= MIN_FREED and out["cooldown_s"] == BASE_S
    done = clk.now_ms
    if crit == "a":
        wx.locked_bytes = locked
        wx.copy_bytes += locked
    at(clk, done, BASE_S * 1000 - 1)
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    out = await recycle(wx, s, crit)
    assert out["freed_bytes"] == per and out["cooldown_s"] == 2 * BASE_S   # 复位后从基础值重新翻倍


@pytest.mark.parametrize("cfgkw,freed,cooldown", [
    ({}, MIN_FREED - 1, 2 * BASE_S),
    ({}, MIN_FREED, BASE_S),
    ({"chatlog_recycle_min_interval_s": 120, "chatlog_recycle_min_freed_bytes": 1000}, 999, 240),
    ({"chatlog_recycle_min_interval_s": 120, "chatlog_recycle_min_freed_bytes": 1000}, 1000, 120),
])
async def test_freed_threshold_inclusive_and_config_honoured(cfgkw, freed, cooldown):
    wx, s, clk = await serving(**CFG["b"], **cfgkw)
    clk.advance(UPTIME_S * 1000)
    wx.copy_bytes, wx.copy_files = freed, 3
    out = await recycle(wx, s, "b")
    assert out["freed_bytes"] == freed and out["cooldown_s"] == cooldown
    done = clk.now_ms
    at(clk, done, cooldown * 1000 - 1)
    await no_recycle(wx, s)
    at(clk, done, cooldown * 1000)
    await recycle(wx, s, "b")


async def test_criterion_a_b_effect_not_deferred_to_next_check():
    """(a)/(b) 不走 E7 的「下一次检查看卷空闲」:回收结果当场给出最终间隔,之后卷空闲变化不改它。"""
    wx, per = _sticky_setup("b")
    wx, s, clk = await serving(wx=wx, **CFG["b"])
    clk.advance(UPTIME_S * 1000)
    wx.temp_free_bytes = HIGH_FREE
    out = await recycle(wx, s, "b")
    assert out["cooldown_s"] == 2 * BASE_S
    done = clk.now_ms
    at(clk, done, CHECK_MS)
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    assert holds(s, wx, "b")
    await no_recycle(wx, s)


# ---------------------------------------------------------------- E7:(c) 按下一次检查时的卷空闲判效
async def test_criterion_c_judged_by_next_check_free_space_backs_off_to_cap_and_resets():
    wx = FakeWeChat()
    wx.serve_copy_bytes, wx.serve_copy_files = REBUILD, 12
    wx, s, clk = await serving(wx=wx)
    wx.temp_free_bytes = LOW_FREE
    out = await recycle(wx, s, "c")
    assert out["freed_bytes"] == REBUILD and REBUILD >= MIN_FREED      # 释放量「够」,但 (c) 不看它
    assert wx.copy_bytes == REBUILD                                  # 拉起即重新复制
    for cd in (1200, 2400, 3600, 3600):
        done = clk.now_ms
        at(clk, done, CHECK_MS)                                       # 下一次检查:卷仍不足 ⇒ 低效
        await no_recycle(wx, s)
        at(clk, done, cd * 1000 - 1)
        assert holds(s, wx, "c")
        await no_recycle(wx, s)
        at(clk, done, cd * 1000)
        out = await recycle(wx, s, "c")
        assert out["freed_bytes"] == REBUILD
    # 卷空闲在下一次检查时恢复 ⇒ 有效 ⇒ 复位
    done = clk.now_ms
    at(clk, done, CHECK_MS)
    wx.temp_free_bytes = HIGH_FREE
    await no_recycle(wx, s)
    wx.temp_free_bytes = LOW_FREE
    at(clk, done, BASE_S * 1000 - 1)
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    await recycle(wx, s, "c")
    # 复位后再次低效 ⇒ 从基础值重新翻倍
    done = clk.now_ms
    at(clk, done, CHECK_MS)
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    await no_recycle(wx, s)
    at(clk, done, 2 * BASE_S * 1000)
    await recycle(wx, s, "c")


@pytest.mark.parametrize("free_at_check,cooldown", [(FREE_LIMIT - 1, 2 * BASE_S), (FREE_LIMIT, BASE_S)])
async def test_criterion_c_effect_threshold_is_inclusive(free_at_check, cooldown):
    wx = FakeWeChat()
    wx.serve_copy_bytes = REBUILD
    wx, s, clk = await serving(wx=wx)
    wx.temp_free_bytes = LOW_FREE
    await recycle(wx, s, "c")
    done = clk.now_ms
    at(clk, done, CHECK_MS)
    wx.temp_free_bytes = free_at_check
    await no_recycle(wx, s)
    wx.temp_free_bytes = LOW_FREE
    at(clk, done, cooldown * 1000 - 1)
    await no_recycle(wx, s)
    at(clk, done, cooldown * 1000)
    await recycle(wx, s, "c")


async def test_criterion_c_after_cooldown_still_skips_idle_wait():
    wx, s, clk, done = await first_effective_recycle("c")
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    original = wx.read_messages
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*a, **k):
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), 8)
        except asyncio.TimeoutError:
            pass
        return await original(*a, **k)

    wx.read_messages = blocked
    reading = asyncio.create_task(s.read(talker="群A"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        at(clk, done, BASE_S * 1000)
        arm(wx, "c")
        t0 = time.monotonic()
        await asyncio.wait_for(recycle(wx, s, "c"), 5)
        assert time.monotonic() - t0 < 2
    finally:
        release.set()
        await asyncio.gather(reading, return_exceptions=True)


# ---------------------------------------------------------------- E8:进入停进程后中途失败 ⇒ 释放 0、低效、当场翻倍
def _inject_failure(wx, fail_at):
    """一次性失败:stop = 停进程那步抛错;sweep = 清扫抛错;restart = 重拉前探测抛错;serve = 重拉 chatlog_serve 失败。"""
    name = {"stop": "chatlog_stop", "sweep": "chatlog_sweep_copies", "restart": "key_state",
            "serve": "chatlog_serve"}[fail_at]
    original = getattr(wx, name)
    state = {"armed": True}

    def fire():
        if state["armed"] and (fail_at == "stop" or wx.chatlog_pid is None):   # 只在进入停进程之后失败一次
            state["armed"] = False
            raise RuntimeError("合成:回收中途失败")

    if asyncio.iscoroutinefunction(original):
        async def broken(*a, **k):
            fire()
            return await original(*a, **k)
    else:
        def broken(*a, **k):
            fire()
            return original(*a, **k)

    setattr(wx, name, broken)
    return state


async def _fail_one_recycle(crit, fail_at):
    """判据 crit 成立、若成功按释放量本应判有效(释放 ≥ 2 GiB)时,让回收在 fail_at 那一步失败一次。"""
    wx, s, clk = await serving(**CFG[crit])
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    if crit == "b":
        clk.advance(UPTIME_S * 1000)
    arm(wx, crit)
    wx.copy_bytes = max(wx.copy_bytes, 2 * GiB)
    state = _inject_failure(wx, fail_at)
    result = (await asyncio.gather(s.maintain(), return_exceptions=True))[0]
    assert state["armed"] is False                                   # 确在该步失败(stop 之后的步骤以进程已停为前提)
    return wx, s, clk, clk.now_ms, result


async def _relaunch_if_stopped(wx, s, clk, done):
    at(clk, done, 31_000)
    if wx.chatlog_pid is None:
        assert await s.ensure_server() is True                       # 既有自愈把 server 拉回来
    else:
        await s.ensure_server()


@pytest.mark.parametrize("fail_at", ["stop", "sweep"])
@pytest.mark.parametrize("crit", ["a", "b", "c"])
async def test_mid_recycle_failure_is_low_yield_and_doubles_regardless_of_criterion(crit, fail_at):
    """E8:已进入停进程步骤后,停进程或清扫失败 ⇒ 释放 0、低效、当场翻倍(600 → 1200),不等下一次检查。"""
    wx, s, clk, done, _ = await _fail_one_recycle(crit, fail_at)
    await _relaunch_if_stopped(wx, s, clk, done)
    # 下一次检查卷空闲已恢复:(c) 的失败也不得据此判为有效
    at(clk, done, 2 * CHECK_MS + 1000)
    wx.temp_free_bytes = HIGH_FREE
    await no_recycle(wx, s)
    for off in (BASE_S * 1000, 2 * BASE_S * 1000 - 1):
        at(clk, done, off)
        arm(wx, crit)
        assert holds(s, wx, crit)
        await no_recycle(wx, s)
    at(clk, done, 2 * BASE_S * 1000)
    arm(wx, crit)
    await recycle(wx, s, crit)


@pytest.mark.parametrize("crit", ["a", "b", "c"])
async def test_stop_failure_does_not_wedge_session_agent(crit):
    """E9:停进程失败 ⇒ maintain 不抛、不持续 BUSY;chatlog 仍在跑则读取照常;冷却期满允许再试。"""
    wx, s, clk, done, result = await _fail_one_recycle(crit, "stop")
    assert not isinstance(result, BaseException), result
    assert wx.chatlog_pid is not None                                # 假后端:停进程那次抛错,进程仍在
    assert await s.ensure_server() is False                          # 不抛 wechat_cleanup_pending
    page = await s.read(talker="群A")
    assert [m["seq"] for m in page["messages"]] == [1]
    at(clk, done, 2 * BASE_S * 1000)
    arm(wx, crit)
    await recycle(wx, s, crit)


@pytest.mark.parametrize("fail_at", ["serve", "restart"])
@pytest.mark.parametrize("crit", ["a", "b", "c"])
async def test_relaunch_only_failure_is_judged_by_criterion_not_e8(crit, fail_at):
    """E8 边界(第 9 条):停进程、清扫都成功,只有重拉失败(serve 起不来 / 重拉前探测抛错)⇒ 不算 E8;
    (a)/(b) 按释放量(≥ 2 GiB ⇒ 有效 600),(c) 按下一次检查卷空闲(已恢复 ⇒ 有效 600)。"""
    wx, s, clk, done, _ = await _fail_one_recycle(crit, fail_at)
    assert wx.calls.count("chatlog_sweep") >= 2                      # 启动清扫 + 本次回收清扫都已执行
    assert wx.chatlog_pid is None
    await _relaunch_if_stopped(wx, s, clk, done)
    at(clk, done, 2 * CHECK_MS + 1000)
    wx.temp_free_bytes = HIGH_FREE
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000 - 1)
    arm(wx, crit)
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    arm(wx, crit)
    await recycle(wx, s, crit)


# ---------------------------------------------------------------- E9 残留路径(05 §2.4.10 第 10 条):收尾停进程失败
def _arm_relaunch_then_stop_failure(wx, *, half=False, hang=False):
    """下一次 chatlog_serve:进程未起即失败 / 拉起一半(pid 已在)后失败 / 挂起等被取消;随后收尾的那次停进程抛错一次。"""
    state = {"serve": True, "stop": False, "stop_failed": 0}
    orig_serve, orig_stop = wx.chatlog_serve, wx.chatlog_stop

    async def serve():
        if not state["serve"]:
            return await orig_serve()
        state["serve"], state["stop"] = False, True
        wx.calls.append("chatlog_serve")
        if hang:
            await asyncio.sleep(30)
        if half:
            wx.chatlog_pid, wx.chatlog_mode = 5305, "server"
        raise RuntimeError("合成:重拉失败")

    async def stop():
        if state["stop"]:
            state["stop"] = False
            state["stop_failed"] += 1
            raise OSError("合成:收尾停进程失败")
        return await orig_stop()

    wx.chatlog_serve, wx.chatlog_stop = serve, stop
    return state


async def _assert_not_cleanup_pending(wx, s):
    """ensure_server 与登录入口都不被 ``wechat_cleanup_pending`` 挡住(登录用未安装探测,越过入口即止,不真取钥)。"""
    try:
        await s.ensure_server()
    except WaError as e:
        assert e.reason != "wechat_cleanup_pending", e.reason
    wx.installed = False
    try:
        with pytest.raises(WaError) as e:
            await s.login_start(account_id="wx01")
        assert e.value.reason == "wechat_not_installed", e.value.reason
    finally:
        wx.installed = True
        s.session = None


async def _settle(s, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        task = s._operation_task                                     # noqa: SLF001
        if task is None or task.done():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("回收任务未在时限内收尾")


@pytest.mark.parametrize("half", [False, True], ids=["not_started", "half_started"])
@pytest.mark.parametrize("crit", ["a", "c"])
async def test_relaunch_failure_then_cleanup_stop_failure_does_not_wedge(crit, half):
    """重拉失败、收尾停进程又失败:maintain 不外抛;ensure_server/登录不 cleanup_pending;读取照常;
    判效仍按第 9 条判据口径((a) 释放 ≥ 1 GiB、(c) 下一次检查卷空闲恢复 ⇒ 有效 600);冷却期满可再回收。"""
    wx, s, clk = await serving(**CFG[crit])
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    arm(wx, crit)
    wx.copy_bytes = max(wx.copy_bytes, 5 * GiB)
    state = _arm_relaunch_then_stop_failure(wx, half=half)
    result = (await asyncio.gather(s.maintain(), return_exceptions=True))[0]
    assert not isinstance(result, BaseException), result
    assert state["stop_failed"] == 1 and state["serve"] is False
    assert result.get("restarted") is not True
    done = clk.now_ms
    assert (wx.chatlog_pid is not None) is half
    await _assert_not_cleanup_pending(wx, s)
    if half:
        page = await s.read(talker="群A")                            # 残留进程在跑:读取照常
        assert [m["seq"] for m in page["messages"]] == [1]
    await _relaunch_if_stopped(wx, s, clk, done)
    await _assert_not_cleanup_pending(wx, s)
    page = await s.read(talker="群A")
    assert [m["seq"] for m in page["messages"]] == [1]
    at(clk, done, 2 * CHECK_MS + 1000)
    wx.temp_free_bytes = HIGH_FREE
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000 - 1)
    arm(wx, crit)
    await no_recycle(wx, s)
    at(clk, done, BASE_S * 1000)
    arm(wx, crit)
    out = await recycle(wx, s, crit)
    assert out["restarted"] is True


@pytest.mark.parametrize("crit", ["a", "c"])
async def test_budget_timeout_with_cleanup_stop_failure_is_low_yield_and_not_busy(crit, monkeypatch):
    """回收预算超时(重拉挂起被取消)且取消收尾停进程失败:maintain 不外抛;不卡 BUSY;按 E8 释放 0 低效翻倍
    (600 不回收、1200 回收;(c) 下一次检查卷空闲恢复也不复位)。"""
    monkeypatch.setattr(wechat_mod, "RECYCLE_BUDGET_S", 0.5)
    wx, s, clk = await serving(**CFG[crit])
    wx.messages = [{"talker": "群A", "seq": 1, "text": "x"}]
    arm(wx, crit)
    wx.copy_bytes = max(wx.copy_bytes, 5 * GiB)
    state = _arm_relaunch_then_stop_failure(wx, hang=True)
    t0 = time.monotonic()
    result = (await asyncio.gather(s.maintain(), return_exceptions=True))[0]
    assert time.monotonic() - t0 < 5
    assert not isinstance(result, BaseException), result
    assert result.get("recycled") is not True
    await _settle(s)
    assert state["stop_failed"] == 1
    done = clk.now_ms
    await _assert_not_cleanup_pending(wx, s)
    await _relaunch_if_stopped(wx, s, clk, done)
    await _assert_not_cleanup_pending(wx, s)
    page = await s.read(talker="群A")
    assert [m["seq"] for m in page["messages"]] == [1]
    at(clk, done, 2 * CHECK_MS + 1000)
    wx.temp_free_bytes = HIGH_FREE
    await no_recycle(wx, s)
    for off in (BASE_S * 1000, 2 * BASE_S * 1000 - 1):
        at(clk, done, off)
        arm(wx, crit)
        assert holds(s, wx, crit)
        await no_recycle(wx, s)
    at(clk, done, 2 * BASE_S * 1000)
    arm(wx, crit)
    out = await recycle(wx, s, crit)
    assert out["restarted"] is True


async def test_non_recycle_cleanup_failure_keeps_sticky_semantics_r6_106():
    """R6-106 / 05 §2.4.4b 未被 E9 改变:取钥操作收尾停不掉 key 进程 ⇒ 之后取钥/登录/自愈/登出都明确返回
    「前次操作正在收尾」(BUSY wechat_cleanup_pending),重复调用也一样;进程能停之后由取消收尾,之后恢复。"""
    wx = FakeWeChat()
    wx.ui_visible = True
    s = WeChatSession(wx, WechatConfig(enabled=True), clock=Clock())
    await s.login_start(account_id="wx01")
    assert wx.chatlog_mode == "key"
    orig_stop = wx.chatlog_stop
    broken = {"on": True}

    async def stop():
        if broken["on"]:
            raise OSError("合成:key 进程停不掉")
        return await orig_stop()

    wx.chatlog_stop = stop
    with pytest.raises(Exception):
        await s.key_retry()                                          # 重开一轮取钥要先停旧 key 进程
    for call in (s.key_retry, lambda: s.login_start(account_id="wx01"), s.ensure_server, s.logout):
        for _ in range(2):
            with pytest.raises(WaError) as e:
                await call()
            assert e.value.code == BUSY and e.value.reason == "wechat_cleanup_pending", (e.value.code, e.value.reason)
    assert wx.chatlog_pid is not None and wx.chatlog_mode == "key"   # 停不掉的进程仍在,没有被当成已清理
    broken["on"] = False
    await s.login_cancel()
    out = await s.login_start(account_id="wx01")
    assert out["login_session_id"] and wx.chatlog_mode == "key"


# ---------------------------------------------------------------- 冷却期:不回收、读取正常、每周期至多一行 cooldown 日志
def _cooldown_lines(records):
    return [r for r in records if getattr(r, "op", "") == "wechat.recycle"
            and "cooldown" in str(getattr(r, "code", "")).lower()]


def _remaining(record):
    kv = getattr(record, "kv", {}) or {}
    keys = [k for k in kv if "remain" in k]
    assert keys, kv
    return kv[keys[0]]


async def test_cooldown_reads_normal_one_log_line_per_cycle_no_secrets(caplog):
    wx = FakeWeChat()
    wx.serve_copy_bytes, wx.serve_copy_files = REBUILD, 12
    wx, s, clk = await serving(wx=wx)
    secret = "cd" * 32
    wx.data_key = secret
    wx.messages = [{"talker": "群A", "seq": 3, "text": "x"}]
    wx.sessions = [{"name": "群A"}]
    wx.temp_free_bytes = LOW_FREE
    await recycle(wx, s, "c")
    done = clk.now_ms
    n = stops(wx)
    seen = {}
    with caplog.at_level(logging.INFO):
        for cycle in range(1, 9):
            at(clk, done, cycle * CHECK_MS)
            mark = len(caplog.records)
            for _ in range(4):                                       # Agent 侧状态轮询走 ensure_server 路径
                assert await s.ensure_server() is False
                page = await s.read(talker="群A")
                assert [m["seq"] for m in page["messages"]] == [3]
            assert await s.sessions(keyword="群") == [{"name": "群A"}]
            assert await s.maintain() is None
            lines = _cooldown_lines(caplog.records[mark:])
            assert len(lines) <= 1, [r.getMessage() for r in lines]
            if lines:
                seen[cycle] = _remaining(lines[0])
            if cycle <= 2:
                assert await woken_within(s, 0.3) >= 0.25
    assert stops(wx) == n
    assert seen, "冷却期内判据成立,应记 cooldown 行"
    if 2 in seen:                                                   # 第 2 个周期:(c) 已判低效 ⇒ 间隔 1200
        assert int(seen[2]) == 1200 - 2 * CHECK_MS // 1000
    assert secret not in caplog.text
    assert all(secret not in str(getattr(r, "kv", "")) for r in caplog.records)


# ---------------------------------------------------------------- 状态范围:会话代理重启复位
@pytest.mark.parametrize("effective,cooldown", [(False, 2 * BASE_S), (True, BASE_S)])
async def test_session_agent_restart_resets_cooldown_and_interval(effective, cooldown):
    wx, per = _sticky_setup("a")
    wx, s, clk = await serving(wx=wx)
    locked = 4 * GiB + 512 * MiB
    wx.locked_bytes = locked
    wx.copy_bytes += locked
    for cd in (1200, 2400, 3600):
        out = await recycle(wx, s, "a")
        assert out["cooldown_s"] == cd
        clk.advance(cd * 1000)
    clk.advance(-1000)                                               # 旧代理仍在 3600 s 冷却期内
    await no_recycle(wx, s)
    s2 = WeChatSession(wx, WechatConfig(enabled=True), clock=clk)     # 会话代理重启:进程内状态不落盘
    if effective:
        wx.locked_bytes = 0
    out = await recycle(wx, s2, "a")                                 # 无冷却
    assert out["cooldown_s"] == cooldown                             # 从基础值起算,而不是从 3600 继续翻倍


# ---------------------------------------------------------------- 回收审计/日志字段 cooldown_s / freed_bytes
@pytest.mark.parametrize("crit", ["a", "b", "c"])
async def test_recycle_result_and_log_carry_cooldown_and_freed(crit, caplog):
    wx, s, clk = await serving(**CFG[crit])
    if crit == "b":
        wx.copy_bytes, wx.copy_files = 2 * GiB, 3
        clk.advance(UPTIME_S * 1000)
    arm(wx, crit)
    before = wx.copy_bytes
    with caplog.at_level(logging.INFO):
        out = await recycle(wx, s, crit)
    assert out["freed_bytes"] == before
    assert isinstance(out["cooldown_s"], int) and out["cooldown_s"] > 0
    rows = [r for r in caplog.records if getattr(r, "op", "") == "wechat.recycle"
            and "freed_bytes" in (getattr(r, "kv", {}) or {})]
    assert rows, "回收必须记一行结构化日志"
    kv = rows[-1].kv
    assert kv["freed_bytes"] == before and "cooldown_s" in kv and isinstance(kv["cooldown_s"], int)
    if crit != "c":                                                  # (a)/(b) 当场定出的下一次最小间隔
        assert kv["cooldown_s"] == out["cooldown_s"] == BASE_S


# ---------------------------------------------------------------- WINAGENT_TEMP_DISK_LOW 不受冷却影响
class _Probe:
    def __init__(self, free):
        self.free = free

    def __call__(self):
        return {"volume": "C:", "free_bytes": self.free, "total_bytes": 10 * FREE_LIMIT}


def _temp_alerts(rig):
    return [a["payload"]["state"] for a in rig.deps.alerts.pull()["alerts"]
            if a["payload"]["code"] == "WINAGENT_TEMP_DISK_LOW"]


async def test_temp_volume_alert_fires_and_resolves_during_recycle_cooldown(tmp_path):
    rig = build_rig(tmp_path)
    probe = _Probe(LOW_FREE)
    try:
        await rig.deps.hub.start()
        ua = UserAgent(rig.deps.cfg, pipe=rig.pipe, wsl=rig.wsl, wechat=rig.wechat, power=rig.power_backend,
                       session_id="Console", user_sid=INSTALL_SID, version=__version__,
                       backup_dir=rig.deps.installer._wsl_backup_dir, clock=rig.clock,   # noqa: SLF001
                       temp_probe=probe)
        await ua.start()
        rig._tasks.append(asyncio.create_task(ua.run()))             # noqa: SLF001
        await asyncio.sleep(0)
        rig.user_agent = ua
        wx, s = rig.wechat, ua.wechat
        wx.serve_copy_bytes = REBUILD
        wx.data_key = wx.img_key = True
        wx.wxid = "wxid_abc"
        assert await s.ensure_server() is True
        wx.temp_free_bytes = LOW_FREE
        await recycle(wx, s, "c")
        await no_recycle(wx, s)                                      # 冷却中
        await sync_temp_volume(rig.deps)
        assert rig.deps.alerts.is_firing("WINAGENT_TEMP_DISK_LOW", "volume:C:")
        probe.free = HIGH_FREE
        await sync_temp_volume(rig.deps)
        assert not rig.deps.alerts.is_firing("WINAGENT_TEMP_DISK_LOW", "volume:C:")
        probe.free = LOW_FREE
        await sync_temp_volume(rig.deps)
        assert _temp_alerts(rig) == ["firing", "resolved", "firing"]
        await no_recycle(wx, s)                                      # 告警照常,不靠反复强停
        assert stops(wx) == 1
    finally:
        for t in rig._tasks:                                         # noqa: SLF001
            t.cancel()
        if rig.user_agent is not None:
            await rig.user_agent.stop()
        await rig.deps.hub.stop()
        rig.db.close()
