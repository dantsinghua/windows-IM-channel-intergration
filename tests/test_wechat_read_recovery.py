"""Valid stored keys must not be replaced because a read response failed."""
from tests.test_wechat_rig import make_wechat_rig
from tests.test_integration_wiring_common import make_rig,close_rig

async def test_known_valid_keys_do_not_request_key_retry_on_read_failure(tmp_path):
    rig=make_wechat_rig(tmp_path)
    try:
        rig.poller.st("wx01").read_fail_streak=3
        state,code,reason=await rig.adapter.probe_state(rig.account())
        assert state=="degraded"
        assert rig.adapter.key_retry_required("wx01") is False
        assert "\u8bfb\u53d6" in reason
        rig.fake.chatlog["key_ok"]=False
        await rig.adapter.probe_state(rig.account())
        assert rig.adapter.key_retry_required("wx01") is True
    finally:rig.store.close()

async def test_health_loop_never_rearms_hook_for_valid_keys(tmp_path):
    rig=make_rig(tmp_path)
    try:
        rig.store.ensure_account("wx01","wechat",state="running",self_uid="wxid_synthetic")
        ad=rig.agent.adapters["wechat"]
        ad._poller.st("wx01").read_fail_streak=3
        for _ in range(4):await rig.agent.healthloop.check_wechat()
        assert rig.wechat.key_retry_calls==0
    finally:close_rig(rig)
