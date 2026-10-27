"""Dynamic native sources and QQ connection generations; synthetic I/O only."""
from types import SimpleNamespace
import asyncio

import pytest

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qq import QQAdapter, QQAdapterConfig
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Command
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.store import Store
from test_qq_common import private_event


NOW = 1_800_000_000_000
UID = "70000001"
PEER = "70000002"


@pytest.fixture
def registry():
    value = Store(":memory:", clock=lambda: NOW).open()
    yield value
    value.close()


def account(registry, aid, channel, *, uid=UID, state="running"):
    registry.ensure_account(aid, channel, state=state, self_uid=uid)
    return Account(id=aid, channel=channel, state=state, self_uid=uid)


def inactive(registry, aid, reason):
    if reason == "deleted":
        registry.con.execute("UPDATE accounts SET deleted_ms=? WHERE id=?", (NOW, aid))
    elif reason == "disabled":
        registry.con.execute("UPDATE accounts SET enabled=0 WHERE id=?", (aid,))
    else:
        registry.set_account_state(aid, "stopped")


def institution(registry, tmp_path):
    from relay.file_gateway import GatewayAuth
    from relay.gateway_paths import InstitutionKeys

    return InstitutionCollectorHost(
        store=registry, bus=SimpleNamespace(supports_xunjia_v22_guard=True),
        loop=asyncio.get_running_loop(),
        keys=InstitutionKeys.from_auth(GatewayAuth("synthetic-slot", b"0123456789abcdef")),
        config={"workbench_name": "Synthetic slots"},
        settings=SimpleNamespace(workbench_id="synthetic-slot-workbench"),
        data_dir=tmp_path, legacy_inbox_disabled=lambda: True,
        friends_provider=lambda _: [], organization_provider=lambda _: {},
        client_factory=lambda path: SimpleNamespace(queue_identity=("synthetic", path)),
        clock=lambda: NOW / 1000,
    )


@pytest.mark.parametrize("channel,prefix", [("qidian", "qd"), ("wechat", "wx")])
@pytest.mark.parametrize("retired", ["stopped", "disabled", "deleted"])
@pytest.mark.parametrize("state", ["running", "degraded"])
async def test_native_port_uses_unique_current_source_without_altering_retired_rows(
    registry, tmp_path, channel, prefix, retired, state
):
    old = account(registry, prefix + "03", channel)
    current = account(registry, prefix + "94", channel, state=state)
    inactive(registry, old.id, retired)
    before = [tuple(r) for r in registry.con.execute("SELECT * FROM accounts ORDER BY id")]
    host = institution(registry, tmp_path)
    row = registry.get_account_full(current.id)
    native = host._make_host(row)
    selected = native.port._account()
    assert selected is not None and selected["id"] == current.id
    assert native.port.ready()["single_bound_account"] is True
    assert native.port.ready()["account_online"] is True
    assert [tuple(r) for r in registry.con.execute("SELECT * FROM accounts ORDER BY id")] == before


@pytest.mark.parametrize("channel,prefix", [("qidian", "qd"), ("wechat", "wx")])
async def test_native_port_re_resolves_after_slot_change_and_refuses_conflicting_active_sources(
    registry, tmp_path, channel, prefix
):
    first = account(registry, prefix + "03", channel)
    host = institution(registry, tmp_path)
    native = host._make_host(registry.get_account_full(first.id))
    assert native.port._account()["id"] == first.id
    inactive(registry, first.id, "stopped")
    second = account(registry, prefix + "94", channel)
    assert native.port._account()["id"] == second.id
    account(registry, prefix + "95", channel, state="degraded")
    assert native.port._account() is None
    assert native.port.ready()["single_bound_account"] is False


async def test_same_uid_in_different_channels_keeps_independent_current_native_sources(registry, tmp_path):
    host = institution(registry, tmp_path)
    selected = {}
    for channel, prefix in [("qidian", "qd"), ("wechat", "wx")]:
        old = account(registry, prefix + "03", channel)
        inactive(registry, old.id, "stopped")
        current = account(registry, prefix + "94", channel)
        selected[channel] = host._make_host(registry.get_account_full(current.id))
    for channel, prefix in [("qidian", "qd"), ("wechat", "wx")]:
        assert selected[channel].port._account()["id"] == prefix + "94"


@pytest.mark.parametrize("channel,prefix", [("qidian", "qd"), ("wechat", "wx")])
async def test_no_active_source_never_falls_back_to_retained_slot(registry, tmp_path, channel, prefix):
    old = account(registry, prefix + "03", channel, state="stopped")
    disabled = account(registry, prefix + "94", channel)
    inactive(registry, disabled.id, "disabled")
    host = institution(registry, tmp_path)
    native = host._make_host(registry.get_account_full(old.id))
    assert native.port._account() is None
    assert native.port.ready()["account_online"] is False


@pytest.mark.parametrize("channel,prefix", [("qidian", "qd"), ("wechat", "wx")])
async def test_dynamic_resolution_preserves_original_native_epoch_guard(registry, tmp_path, channel, prefix):
    old = account(registry, prefix + "03", channel)
    host = institution(registry, tmp_path)
    native = host._make_host(registry.get_account_full(old.id))
    inactive(registry, old.id, "stopped")
    current = account(registry, prefix + "94", channel)
    registry.settings_set("relay.identity." + current.id, {"epoch": 1, "uid": UID}, actor="synthetic-test")
    assert native.port._account() is None
    assert native.port.ready()["single_bound_account"] is False


class Consumer:
    def __init__(self, aid, channel, events, *, stop_fails=False):
        self.aid, self.events, self.stop_fails = aid, events, stop_fails
        self.ledger = SimpleNamespace(binding=SimpleNamespace(channel=channel))
        self._active = self._intake_ok = True

    async def stop(self):
        self.events.append(("stop", self.aid))
        if self.stop_fails:
            raise RuntimeError("synthetic-stop-error")

    async def start(self):
        self.events.append(("start", self.aid))


@pytest.mark.parametrize("retired", ["stopped", "disabled", "deleted"])
async def test_wechat_failed_old_consumer_shutdown_blocks_replacement_but_not_other_uid(
    registry, tmp_path, retired
):
    old = account(registry, "wx03", "wechat")
    account(registry, "wx94", "wechat")
    account(registry, "wx95", "wechat", uid="70000003")
    inactive(registry, old.id, retired)
    host = institution(registry, tmp_path)
    host.active = True
    events = []
    previous = Consumer(old.id, "wechat", events, stop_fails=True)
    host.hosts[(old.id, UID)] = previous
    host._make_host = lambda r: Consumer(r["id"], r["channel"], events)
    host.publisher.request_sync = lambda *args: None
    await host.reconcile_accounts()
    assert host.hosts[(old.id, UID)] is previous
    assert ("start", "wx94") not in events
    assert ("start", "wx95") in events
    previous.stop_fails = False
    await host.reconcile_accounts()
    assert (old.id, UID) not in host.hosts
    assert ("wx94", UID) in host.hosts


class SyntheticClient:
    """Controllable callback and action boundary; no socket or background task."""
    def __init__(self, url, *, on_event, on_reconnect, **kwargs):
        self.url, self._on_event, self._on_reconnect = url, on_event, on_reconnect
        self.connected = False
        self.actions = []
        self.login_uid = UID
        self.readback_gate = None
        self.readback_started = asyncio.Event()

    async def start(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def call_action(self, name, params=None):
        self.actions.append((name, params))
        if name == "get_recent_contact":
            return []
        if name in ("send_private_msg", "send_group_msg"):
            return {"message_id": 901}
        if name == "get_msg":
            self.readback_started.set()
            if self.readback_gate is not None:
                await self.readback_gate.wait()
            return event(901)
        if name == "get_login_info":
            return {"user_id": int(self.login_uid)}
        if name == "get_status":
            return {"online": True, "good": True}
        raise AssertionError("Unexpected synthetic action: " + name)


def event(mid=1, uid=UID):
    return private_event(message_id=mid, text="synthetic dynamic slot", time_s=NOW // 1000,
                         user_id=PEER, self_id=uid, message_seq=mid)


@pytest.fixture
async def qq(registry, monkeypatch):
    import qtrade_agent.adapters.qq.adapter as module
    monkeypatch.setattr(module, "OneBotClient", SyntheticClient)
    adapter = QQAdapter(store=registry, events=Events(registry), cfg=AgentConfig(),
                        qq_cfg=QQAdapterConfig(), transport_factory=lambda _: object(),
                        clock=lambda: NOW)
    yield adapter
    await adapter.close()


@pytest.mark.parametrize("new_uid", [UID, "70000003"])
async def test_qq_retired_event_callback_cannot_write_into_replacement_session(registry, qq, new_uid):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    callback = qq.session_of(old.id).client._on_event
    await qq.stop(old, graceful=True)
    registry.set_account_state(old.id, "running", self_uid=new_uid)
    current = Account(id=old.id, channel="qq", state="running", self_uid=new_uid)
    await qq.start(current)
    await callback(event())
    assert registry.count_messages(current.id) == 0
    assert registry.cursor_get(current.id, "onebot_seq:" + PEER) is None
    await qq.session_of(current.id).client._on_event(event(2, new_uid))
    assert registry.count_messages(current.id) == 1


async def test_qq_retired_reconnect_callback_does_not_backfill_replacement_session(registry, qq):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    callback = qq.session_of(old.id).client._on_reconnect
    await qq.stop(old, graceful=True)
    current = Account(id=old.id, channel="qq", state="running", self_uid=UID)
    await qq.start(current)
    new_client = qq.session_of(current.id).client
    before = list(new_client.actions)
    await callback()
    assert new_client.actions == before


async def test_qq_wrong_native_self_id_is_rejected_before_message_and_cursor_writes(registry, qq):
    current = account(registry, "qq94", "qq")
    await qq.start(current)
    callback = qq.session_of(current.id).client._on_event
    await callback(event(uid="70000003"))
    assert registry.count_messages(current.id) == 0
    assert registry.cursor_get(current.id, "onebot_seq:" + PEER) is None
    await callback(event(2))
    assert registry.count_messages(current.id) == 1


@pytest.mark.parametrize("retired", ["stopped", "disabled", "deleted"])
async def test_qq_retired_slot_callbacks_do_not_write_or_disrupt_new_slot(registry, qq, retired):
    old = account(registry, "qq03", "qq")
    await qq.start(old)
    callback = qq.session_of(old.id).client._on_event
    inactive(registry, old.id, retired)
    current = account(registry, "qq94", "qq")
    await qq.start(current)
    await callback(event())
    assert registry.count_messages(old.id) == 0
    await qq.session_of(current.id).client._on_event(event(2))
    assert registry.count_messages(current.id) == 1
    await qq.stop(old, graceful=True)
    assert qq.session_of(current.id).client.connected
    assert qq.session_of(current.id).client.url == "ws://127.0.0.1:16194"


async def test_qq_send_uses_requested_current_slot_transport(registry, qq):
    old = account(registry, "qq03", "qq", state="stopped")
    current = account(registry, "qq94", "qq")
    await qq.start(current)
    result = await qq.send(current, Command(current.id, "send_text", {"session": PEER, "text": "synthetic send"}))
    assert result.ok
    assert qq.session_of(old.id) is None
    sends = [call for call in qq.session_of(current.id).client.actions if call[0] == "send_private_msg"]
    assert sends == [("send_private_msg", {"user_id": int(PEER),
                                          "message": [{"type": "text", "data": {"text": "synthetic send"}}]})]
    assert registry.count_messages(old.id) == 0
    assert registry.count_messages(current.id) == 1


async def test_qq_readback_from_replaced_connection_cannot_confirm_or_ingest(registry, qq):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    previous = qq.session_of(old.id)
    previous.client.readback_gate = asyncio.Event()
    task = asyncio.create_task(qq._readback(previous, PEER, "private", 901))
    await previous.client.readback_started.wait()
    await qq.stop(old, graceful=True)
    current = Account(id=old.id, channel="qq", state="running", self_uid=UID)
    await qq.start(current)
    previous.client.readback_gate.set()
    assert await task is False
    assert registry.count_messages(current.id) == 0


async def test_qq_two_active_sources_for_same_uid_cannot_send(registry, qq):
    current = account(registry, "qq94", "qq")
    await qq.start(current)
    account(registry, "qq95", "qq", state="degraded")
    client = qq.session_of(current.id).client
    result = await qq.send(current, Command(current.id, "send_text", {"session": PEER, "text": "synthetic send"}))
    assert not result.ok
    assert not any(name.startswith("send_") for name, _ in client.actions)
    assert registry.count_messages(current.id) == 0


async def test_qq_changed_registry_uid_invalidates_existing_connection(registry, qq):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    client = qq.session_of(old.id).client
    registry.set_account_state(old.id, "running", self_uid="70000003")
    await client._on_event(event())
    result = await qq.send(old, Command(old.id, "send_text", {"session": PEER, "text": "synthetic send"}))
    assert not result.ok
    assert not any(name.startswith("send_") for name, _ in client.actions)
    assert registry.count_messages(old.id) == 0
    assert registry.cursor_get(old.id, "onebot_seq:" + PEER) is None


async def test_qq_native_login_uid_mismatch_blocks_send_before_side_effect(registry, qq):
    current = account(registry, "qq94", "qq")
    await qq.start(current)
    client = qq.session_of(current.id).client
    client.login_uid = "70000003"
    result = await qq.send(current, Command(current.id, "send_text", {"session": PEER, "text": "synthetic send"}))
    assert not result.ok
    assert not any(name.startswith("send_") for name, _ in client.actions)
    assert registry.count_messages(current.id) == 0


async def test_qq_history_returning_after_connection_replacement_cannot_write(registry, qq):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    previous = qq.session_of(old.id)
    started, release = asyncio.Event(), asyncio.Event()

    async def history_action(name, params=None):
        if name == "get_recent_contact":
            return [{"peerUin": PEER, "chatType": "1", "peerName": "Synthetic peer"}]
        assert name == "get_friend_msg_history"
        started.set()
        await release.wait()
        return {"messages": [event(10)]}

    previous.client.call_action = history_action
    task = asyncio.create_task(qq.backfill(old))
    await started.wait()
    await qq.stop(old, graceful=True)
    current = Account(id=old.id, channel="qq", state="running", self_uid=UID)
    await qq.start(current)
    release.set()
    assert await task == 0
    assert registry.count_messages(current.id) == 0
    assert registry.cursor_get(current.id, "onebot_seq:" + PEER) is None


async def test_qq_epoch_change_invalidates_late_event_from_same_uid(registry, qq):
    current = account(registry, "qq94", "qq")
    await qq.start(current)
    client = qq.session_of(current.id).client
    registry.settings_set("relay.identity." + current.id, {"epoch": 1, "uid": UID}, actor="synthetic-test")
    await client._on_event(event())
    assert registry.count_messages(current.id) == 0
    assert registry.cursor_get(current.id, "onebot_seq:" + PEER) is None


async def test_qq_start_replaces_changed_identity_on_same_slot(registry, qq):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    previous = qq.session_of(old.id)
    registry.set_account_state(old.id, "running", self_uid="70000003")
    current = Account(id=old.id, channel="qq", state="running", self_uid="70000003")
    await qq.start(current)
    replacement = qq.session_of(current.id)
    assert replacement is not previous
    assert previous.client.connected is False
    assert replacement.client.connected is True
    await previous.client._on_event(event())
    assert registry.count_messages(current.id) == 0
    await replacement.client._on_event(event(2, "70000003"))
    assert registry.count_messages(current.id) == 1


@pytest.mark.parametrize("new_uid", [UID, "70000003"])
async def test_qq_late_stop_for_retired_connection_does_not_stop_replacement(registry, qq, new_uid):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    previous = qq.session_of(old.id)
    await qq.stop(old, graceful=True)
    registry.set_account_state(old.id, "running", self_uid=new_uid)
    current = Account(id=old.id, channel="qq", state="running", self_uid=new_uid)
    await qq.start(current)
    replacement = qq.session_of(current.id)
    await qq.stop(old, graceful=True)
    assert qq.session_of(current.id) is replacement
    assert replacement.client.connected is True
    assert previous.client.connected is False
    await replacement.client._on_event(event(2, new_uid))
    assert registry.count_messages(current.id) == 1


async def test_qq_stale_start_cannot_replace_current_verified_native_identity(registry, qq):
    old = account(registry, "qq94", "qq")
    await qq.start(old)
    registry.set_account_state(old.id, "running", self_uid="70000003")
    current = Account(id=old.id, channel="qq", state="running", self_uid="70000003")
    await qq.start(current)
    replacement = qq.session_of(current.id)
    await qq.start(old)
    assert qq.session_of(current.id) is replacement
    assert replacement.client.connected is True
    await replacement.client._on_event(event(2, "70000003"))
    assert registry.count_messages(current.id) == 1

