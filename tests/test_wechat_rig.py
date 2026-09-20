"""微信通道测试的公共夹具(全假 WinAgent —— 开发容器里绝不碰真微信/真 WinAgent,SKILL §4 禁区)。

其余 ``tests/test_wechat_*.py`` 从本模块 import ``make_wechat_rig``;本文件自己也放两条烟测(装配可用 + 假件路由对)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.wechat import FakeWeChatWinAgent, WechatAdapter, WeChatWinAgent, WechatPoller
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.pool import Pool
from qtrade_agent.store import Store
from qtrade_agent.wechat_slot import WechatSlot
from qtrade_agent.winagent_client import WinAgentClient
from tests.conftest import Clock


@dataclass
class WeRig:
    store: Store
    events: Events
    clock: Clock
    cfg: AgentConfig
    fake: FakeWeChatWinAgent
    client: WeChatWinAgent
    poller: WechatPoller
    adapter: WechatAdapter
    slot: WechatSlot
    pool: Pool
    purged: list[str]
    transitions: list[tuple[str, str, Optional[str], str]]

    def account(self, id: str = "wx01") -> Account:
        row = self.store.get_account_full(id)
        return Account(id=row["id"], channel="wechat", state=row["state"], self_uid=row["self_uid"],
                       self_nick=row["self_nick"], state_code=row["state_code"])

    def events_of(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.store.list_events(event=name)]


def make_wechat_rig(tmp_path, *, clock: Optional[Clock] = None, cfg: Optional[AgentConfig] = None,
                    account_id: str = "wx01", state: str = "running", wsl_total_mb: int = 11264,
                    windows_total_mb: int = 16384) -> WeRig:
    clock = clock or Clock()
    cfg = cfg or AgentConfig()
    store = Store(str(tmp_path / "agent.db"), clock=clock, out_merge_window_s=cfg.bus.out_merge_window_s,
                  capture_text=cfg.messages.capture_text).open()
    store.ensure_account(account_id, "wechat", state=state, login_mode="qrcode", self_uid="wxid_demo01")
    store.upsert_runtime(account_id, kind=store.RUNTIME_KIND["wechat"], now_ms=clock())
    events = Events(store, queue_max=cfg.events.ws_queue_max)
    pool = Pool(store, cfg, clock=clock, wsl_total_mb=wsl_total_mb, windows_total_mb=windows_total_mb)
    pool.set_windows(total_mb=windows_total_mb, wechat_enabled=True, known=True)

    fake = FakeWeChatWinAgent()
    wa = WinAgentClient(cfg.winagent, transport=fake, base_url="http://winagent.fake:17610", token=fake.token, clock=clock)
    client = WeChatWinAgent(wa)
    poller = WechatPoller(store=store, events=events, client=client, cfg=cfg, clock=clock)
    adapter = WechatAdapter(poller, client=client, store=store)

    purged: list[str] = []
    transitions: list[tuple[str, str, Optional[str], str]] = []

    async def purge(row):
        purged.append(row["id"])

    def transition(id: str, state: str, *, state_code: Optional[str] = None, state_reason: str = "",
                   login_session_id: str = "", prompt: Optional[dict[str, Any]] = None, **kw: Any) -> dict[str, Any]:
        """accounts.AccountService.transition 的最小替身:走 store.transition(同事务两个动作)+ 记一条流水。"""
        kw.pop("trace_id", None)
        _before, row = store.transition(id, state, state_code=state_code, state_reason=state_reason, now_ms=clock(), **kw)
        transitions.append((id, state, state_code, login_session_id))
        events.emit("account_state", payload={"id": id, "state": state, "state_code": state_code,
                                              "state_reason": state_reason, "login_session_id": login_session_id, "prompt": prompt},
                    account_id=id, channel="wechat", now_ms=clock())
        return row

    slot = WechatSlot(store=store, events=events, cfg=cfg, clock=clock, client=client, purge=purge, transition=transition)
    return WeRig(store=store, events=events, clock=clock, cfg=cfg, fake=fake, client=client, poller=poller,
                 adapter=adapter, slot=slot, pool=pool, purged=purged, transitions=transitions)


# ---------------------------------------------------------------------- 烟测
async def test_rig_builds_and_fake_routes_status(tmp_path):
    rig = make_wechat_rig(tmp_path)
    st = await rig.client.status()
    assert st["enabled"] is True and st["wechat"]["wxid"] == "wxid_demo01"
    assert st["chatlog"]["key_ok"] is True
    rig.store.close()


async def test_fake_delegates_non_wechat_paths_to_fakewinagent(tmp_path):
    """FakeWeChatWinAgent 只接管 ``/wa/v1/wechat/*``,其余路径委托给 winagent_client.FakeWinAgent(不改那份文件)。"""
    rig = make_wechat_rig(tmp_path)
    wa = WinAgentClient(rig.cfg.winagent, transport=rig.fake, base_url="http://winagent.fake:17610", token=rig.fake.token, clock=rig.clock)
    assert (await wa.ping())["version"] == rig.fake.version
    assert (await wa.health())["modules"]["wechat"] == "enabled"
    rig.store.close()
