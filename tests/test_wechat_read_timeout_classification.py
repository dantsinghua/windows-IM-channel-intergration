"""微信读超时分类(R6-111):504(管道 deadline/服务侧超时,游标不变)应视为可续读,不计 read_fail_streak。

根因:旧 poll.py 只认 reason=="wechat_read_catching_up";管道 deadline 超时分支 reason 为空/为
user_agent_timeout,被当硬失败,连续 3 次误判 KEY_FAIL 致降级停读、回填中断。
"""
from __future__ import annotations

from qtrade_agent.adapters.wechat import WechatAccountView
from qtrade_agent.adapters.wechat.client import WeChatCallFailed, WeChatNotReady
from tests.test_wechat_rig import make_wechat_rig

T0 = 1_758_240_000_000


def view(rig, id="wx01") -> WechatAccountView:
    row = rig.store.get_account_full(id)
    return WechatAccountView(row["id"], row["state"], row["self_uid"], row["self_nick"])


async def test_504_timeout_treated_as_resumable_not_hard_failure(tmp_path):
    """504(无 reason,模拟管道 deadline 超时)连续 5 次也不触发 degraded_key_fail。"""
    rig = make_wechat_rig(tmp_path)
    async def _504(*a, **kw):
        raise WeChatCallFailed(504, {"error": {"code": "TIMEOUT", "message": "deadline"}}, "/wa/v1/wechat/read")
    rig.client.read = _504
    for _ in range(5):
        assert await rig.poller.poll(view(rig)) == 0
    assert rig.poller.degraded_key_fail("wx01") is False
    rig.store.close()


async def test_504_with_user_agent_timeout_reason_is_resumable(tmp_path):
    """504 + reason=user_agent_timeout(服务侧重试分支)也视为可续读。"""
    rig = make_wechat_rig(tmp_path)
    async def _504ua(*a, **kw):
        raise WeChatCallFailed(504, {"error": {"code": "TIMEOUT", "reason": "user_agent_timeout",
                                               "message": "svc timeout"}}, "/wa/v1/wechat/read")
    rig.client.read = _504ua
    for _ in range(5):
        await rig.poller.poll(view(rig))
    assert rig.poller.degraded_key_fail("wx01") is False
    rig.store.close()


async def test_catching_up_reason_still_resumable(tmp_path):
    """明确的 wechat_read_catching_up(可续读)保持不计(回归保护)。"""
    rig = make_wechat_rig(tmp_path)
    async def _catch(*a, **kw):
        raise WeChatCallFailed(504, {"error": {"code": "TIMEOUT", "reason": "wechat_read_catching_up",
                                               "message": "continuing"}}, "/wa/v1/wechat/read")
    rig.client.read = _catch
    for _ in range(5):
        await rig.poller.poll(view(rig))
    assert rig.poller.degraded_key_fail("wx01") is False
    rig.store.close()


async def test_non_timeout_error_still_counts_as_failure(tmp_path):
    """非超时类(500 INTERNAL)仍计硬失败,确保只放宽超时。"""
    rig = make_wechat_rig(tmp_path)
    async def _500(*a, **kw):
        raise WeChatCallFailed(500, {"error": {"code": "INTERNAL", "message": "boom"}}, "/wa/v1/wechat/read")
    rig.client.read = _500
    from qtrade_agent.adapters.wechat.poll import READ_FAIL_STREAK
    for i in range(READ_FAIL_STREAK):
        await rig.poller.poll(view(rig))
        rig.clock.advance(max(1, rig.poller.st("wx01").next_retry_ms - rig.clock()))
    assert rig.poller.degraded_key_fail("wx01") is True
    rig.store.close()


async def test_not_ready_still_counts_as_failure(tmp_path):
    """503 NOT_READY(会话代理离线)仍计硬失败(与超时区分)。"""
    rig = make_wechat_rig(tmp_path)
    async def _503(*a, **kw):
        raise WeChatNotReady("user_agent_offline", "offline")
    rig.client.read = _503
    from qtrade_agent.adapters.wechat.poll import READ_FAIL_STREAK
    for i in range(READ_FAIL_STREAK):
        await rig.poller.poll(view(rig))
        rig.clock.advance(max(1, rig.poller.st("wx01").next_retry_ms - rig.clock()))
    assert rig.poller.degraded_key_fail("wx01") is True
    rig.store.close()
