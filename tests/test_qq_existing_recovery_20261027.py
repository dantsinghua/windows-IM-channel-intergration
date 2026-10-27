from types import SimpleNamespace
import pytest
from qtrade_agent.adapters.qq.recovery import ExistingQQRecovery


def setup_recovery():
    row = {"id": "qq95", "channel": "qq", "state": "running", "desired_state": "running",
           "self_uid": "700001", "enabled": True}
    session = SimpleNamespace(generation=object(), client=SimpleNamespace(connected=False))
    calls = []
    async def action(name):
        return {"online": True} if name == "get_status" else {"user_id": "700001"}
    session.client.call_action = action
    class Adapter:
        current = None
        def session_of(self, aid): return self.current
        async def start(self, account):
            calls.append("start"); self.current = session
            account.extra["_qq_session_generation"] = session.generation
        async def stop(self, account, graceful):
            calls.append("stop"); self.current = None
    adapter = Adapter()
    store = SimpleNamespace(list_accounts=lambda **kw: [row], get_account_full=lambda aid: row,
                            native_identity=lambda aid: {"epoch": 1})
    now = [0.0]
    recovery = ExistingQQRecovery(store, adapter, clock=lambda: now[0])
    return row, session, calls, adapter, now, recovery


async def test_backend_starting_late_preserves_connection_loop_and_recovers():
    row, session, calls, adapter, now, recovery = setup_recovery()
    await recovery.tick()
    assert calls == ["start"]
    assert not recovery.verified
    session.client.connected = True
    now[0] = 3
    await recovery.tick()
    assert recovery.verified == {"qq95"}
    assert calls == ["start"]


@pytest.mark.parametrize("field,value", [("enabled", False), ("state", "stopped"),
    ("desired_state", "stopped"), ("deleted_ms", 1)])
async def test_disabled_or_retired_account_never_starts_transport(field, value):
    row, session, calls, adapter, now, recovery = setup_recovery()
    row[field] = value
    await recovery.tick()
    assert calls == []


async def test_wrong_native_identity_is_stopped_and_not_automatically_relogged():
    row, session, calls, adapter, now, recovery = setup_recovery()
    session.client.connected = True
    async def action(name):
        return {"online": True} if name == "get_status" else {"user_id": "other"}
    session.client.call_action = action
    await recovery.tick()
    now[0] = 100
    await recovery.tick()
    assert calls == ["start", "stop"]
    assert recovery.errors["qq95"] == "NATIVE_ACCOUNT_CHANGED"
    assert not recovery.verified


async def test_identity_change_during_probe_cannot_mark_account_ready():
    row, session, calls, adapter, now, recovery = setup_recovery()
    session.client.connected = True
    async def action(name):
        row["self_uid"] = "changed"
        return {"online": True} if name == "get_status" else {"user_id": "700001"}
    session.client.call_action = action
    await recovery.tick()
    assert calls == ["start", "stop"]
    assert not recovery.verified


async def test_manual_lifecycle_busy_never_creates_connection():
    row, session, calls, adapter, now, recovery = setup_recovery()
    recovery.busy = lambda aid: True
    await recovery.tick()
    assert calls == []
