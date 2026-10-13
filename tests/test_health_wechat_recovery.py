"""重启恢复与 WS 回放边界，全部使用假后端和临时 SQLite。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from qtrade_agent.alerts import Alerts
from tests.test_integration_wiring_common import TOKEN_ADMIN, close_rig, make_rig


@pytest.fixture
def rig(tmp_path):
    value = make_rig(tmp_path)
    yield value
    close_rig(value)


CASES = [("winagent_probe", "H02_WINAGENT_API_DOWN", "host"),
         ("dockerd_probe", "H03_DOCKERD_DOWN", "wsl")]


def states(rig, code):
    return [event["payload"]["state"] for event in rig.events_of("alert")
            if event["payload"]["code"] == code]


def previous_process_failure(rig, code, subject):
    old = Alerts(rig.agent.events, clock=rig.clock)
    old.firing(code, subject=subject)
    assert not rig.agent.alerts.is_firing(code, subject)


@pytest.mark.parametrize("probe,code,subject", CASES)
async def test_restart_healthy_probe_closes_persisted_failure_once(rig, probe, code, subject):
    previous_process_failure(rig, code, subject)
    for _ in range(4):
        await getattr(rig.agent, probe)()
    assert states(rig, code) == ["firing", "resolved"]


@pytest.mark.parametrize("probe,code,subject", CASES)
async def test_restart_unknown_probe_does_not_resolve_previous_failure(rig, monkeypatch, probe, code, subject):
    previous_process_failure(rig, code, subject)
    if probe == "winagent_probe":
        monkeypatch.setattr(rig.agent.winagent, "ping", AsyncMock(return_value=None))
        monkeypatch.setattr(rig.agent.winagent, "discover", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr(rig.agent.runtime, "dockerd_ok", AsyncMock(return_value=None))
    for _ in range(4):
        await getattr(rig.agent, probe)()
    assert "resolved" not in states(rig, code)


@pytest.mark.parametrize("probe,code,subject", CASES)
async def test_recovery_record_failure_is_retried_and_not_marked_reconciled(rig, monkeypatch, probe, code, subject):
    previous_process_failure(rig, code, subject)
    real_emit = rig.agent.events.emit
    failures = []

    def fail_first_recovery(event, **kwargs):
        payload = kwargs.get("payload", {})
        if payload.get("code") == code and payload.get("state") == "resolved" and not failures:
            failures.append("disk unavailable")
            raise OSError("test outbox unavailable")
        return real_emit(event, **kwargs)

    monkeypatch.setattr(rig.agent.events, "emit", fail_first_recovery)
    try:
        await getattr(rig.agent, probe)()
    except OSError:
        pass
    assert failures == ["disk unavailable"]
    assert states(rig, code) == ["firing"]
    await getattr(rig.agent, probe)()
    await getattr(rig.agent, probe)()
    assert states(rig, code) == ["firing", "resolved"]


def test_resolve_write_failure_keeps_active_alert_for_next_recovery(rig, monkeypatch):
    code, subject = "H02_WINAGENT_API_DOWN", "host"
    rig.agent.alerts.firing(code, subject=subject)
    real_emit = rig.agent.events.emit
    monkeypatch.setattr(rig.agent.events, "emit", lambda *a, **k: (_ for _ in ()).throw(OSError("outbox")))
    with pytest.raises(OSError):
        rig.agent.alerts.resolve(code, subject=subject)
    assert rig.agent.alerts.is_firing(code, subject), "Failed persistence must not consume the recovery transition"
    monkeypatch.setattr(rig.agent.events, "emit", real_emit)
    assert rig.agent.alerts.resolve(code, subject=subject)
    assert states(rig, code) == ["firing", "resolved"]


def test_ws_replayed_is_transport_only_and_new_event_is_live_before_old_queue_drains(rig):
    for i in range(3):
        rig.agent.events.emit("alert", payload={"code": f"OLD{i}", "state": "firing", "subject": "host"})
    with rig.client.websocket_connect(f"/api/v1/events?token={TOKEN_ADMIN}") as ws:
        ws.send_json({"subscribe": {"events": ["alert"], "since_seq": 0}})
        first = ws.receive_json()
        assert first.get("replayed") is True
        rig.agent.events.emit("alert", payload={"code": "LIVE", "state": "firing", "subject": "host"})
        rest = [ws.receive_json() for _ in range(3)]
    assert [f["payload"]["code"] for f in rest] == ["OLD1", "OLD2", "LIVE"]
    assert [f.get("replayed") for f in rest] == [True, True, False]
    for event in rig.store.replay_outbox(0, 20):
        assert "replayed" not in event
        assert "replayed" not in event["payload"]


def test_ws_truncated_first_available_event_is_replayed(rig):
    rig.agent.events.emit("alert", payload={"code": "EXPIRED"})
    rig.store.purge_outbox_ws(older_than_ms=rig.clock() + 1)
    rig.agent.events.emit("alert", payload={"code": "FIRST"})
    minimum, _ = rig.store.outbox_seq_bounds()
    with rig.client.websocket_connect(f"/api/v1/events?token={TOKEN_ADMIN}") as ws:
        ws.send_json({"subscribe": {"events": ["alert"], "since_seq": 0}})
        assert ws.receive_json() == {"replay": "truncated", "from_seq": minimum}
        frame = ws.receive_json()
    assert frame["seq"] == minimum
    assert frame["payload"]["code"] == "FIRST"
    assert frame.get("replayed") is True


@pytest.mark.parametrize("method,path,budget,retry", [
    ("status", "/wa/v1/wechat/status", 15.0, False),
    ("login_status", "/wa/v1/wechat/login/status", 15.0, False),
    ("login_start", "/wa/v1/wechat/login/start", 65.0, False),
    ("key_retry", "/wa/v1/wechat/key/retry", 65.0, False),
])
async def test_state_advancing_http_endpoints_keep_nested_budget_without_transport_retry(method, path, budget, retry):
    from qtrade_agent.adapters.wechat.client import WeChatWinAgent
    request = AsyncMock(return_value=(200, {}))
    from types import SimpleNamespace
    client = WeChatWinAgent(SimpleNamespace(request=request))
    await getattr(client, method)()
    assert request.call_args.args[1] == path
    assert request.call_args.kwargs["timeout_s"] == budget
    assert request.call_args.kwargs["retry"] is retry
