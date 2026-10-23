"""QQ 适配器测试的共用夹具(本文件不含用例,只被 ``test_qq_*.py`` import)。

开发容器里**只用 ``FakeOneBot``**,绝不连真实 QQ / NapCat(SKILL §4 禁区)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from conftest import Clock

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qq import FakeOneBot, QQAdapter, QQAdapterConfig
from qtrade_agent.alerts import Alerts
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.store import Store

ACCOUNT_ID = "qq03"
SELF_UID = "415011447"
PEER = "3007373675"
GROUP = "123456"


@dataclass
class QQRig:
    store: Store
    events: Events
    alerts: Alerts
    cfg: AgentConfig
    adapter: QQAdapter
    bot: FakeOneBot
    acct: Account
    clock: Clock

    def close(self) -> None:
        self.store.close()

    def messages(self, **kw) -> list[dict[str, Any]]:
        return self.store.list_messages(self.acct.id, **kw)

    def emitted(self, event: str = "message") -> list[dict[str, Any]]:
        return self.store.list_events(event=event)


def make_qq_rig(tmp_path, *, clock: Optional[Clock] = None, cfg: Optional[AgentConfig] = None,
                qq_cfg: Optional[QQAdapterConfig] = None, capture_text: bool = True,
                account_id: str = ACCOUNT_ID, self_uid: str = SELF_UID, state: str = "running",
                online: bool = True) -> QQRig:
    clock = clock or Clock()
    cfg = cfg or AgentConfig()
    store = Store(":memory:", clock=clock, out_merge_window_s=cfg.bus.out_merge_window_s, capture_text=capture_text).open()
    store.ensure_account(account_id, "qq", state=state, login_mode="qrcode", self_uid=self_uid)
    store.upsert_runtime(account_id, kind="napcat", desired_state="running")
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    bot = FakeOneBot(self_id=int(self_uid), clock=clock, online=online)
    adapter = QQAdapter(store=store, events=events, cfg=cfg, qq_cfg=qq_cfg,
                        transport_factory=lambda acct: bot, clock=clock)
    acct = Account(id=account_id, channel="qq", state=state, self_uid=self_uid, self_nick="我")
    return QQRig(store=store, events=events, alerts=alerts, cfg=cfg, adapter=adapter, bot=bot, acct=acct, clock=clock)


def private_event(*, message_id: int, text: str = "你好", time_s: int, user_id: str = PEER,
                  self_id: str = SELF_UID, nickname: str = "对端", message_seq: Optional[int] = None) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "message_id": message_id, "user_id": int(user_id), "self_id": int(self_id), "time": time_s,
        "sender": {"user_id": int(user_id), "nickname": nickname},
        "message": [{"type": "text", "data": {"text": text}}], "raw_message": text,
    }
    if message_seq is not None:
        ev["message_seq"] = message_seq
    return ev


def group_event(*, message_id: int, text: str = "群里的话", time_s: int, group_id: str = GROUP,
                user_id: str = PEER, self_id: str = SELF_UID, message_seq: Optional[int] = None) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "message_id": message_id, "group_id": int(group_id), "user_id": int(user_id), "self_id": int(self_id), "time": time_s,
        "sender": {"user_id": int(user_id), "nickname": "群友", "card": "群名片"},
        "message": [{"type": "text", "data": {"text": text}}], "raw_message": text,
    }
    if message_seq is not None:
        ev["message_seq"] = message_seq
    return ev
