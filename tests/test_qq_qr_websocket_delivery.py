"""R6-83: real FastAPI WebSocket delivery, replay privacy and subscription ACL."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json

import pytest
from starlette.testclient import TestClient

from qtrade_agent.webhook import FakeHttp, WebhookDispatcher
from tests.conftest import make_rig
from tests.test_qq_first_login_common import png

ADMIN = "qr-ws-fixture-admin"
LIMITED = "qr-ws-fixture-limited"


@pytest.fixture
def websocket_rig(tmp_path):
    rig = make_rig(tmp_path)
    rig.store.ensure_account("qq01", "qq", state="login_required")
    rig.store.ensure_account("qq02", "qq", state="login_required")
    rig.store.ensure_account("qd01", "qidian", state="login_required")
    rig.store.upsert_api_client(app_id="console", name="fixture", level="admin", token=ADMIN)
    rig.store.upsert_api_client(
        app_id="limited", name="fixture", level="read", token=LIMITED, allow_accounts=["qq02"]
    )
    with TestClient(rig.agent.create_api(), client=("127.0.0.1", 40001)) as client:
        yield rig, client
    rig.store.close()


def emit(rig, client, *, account="qq02", channel="qq", event="account_state", secret=None):
    payload = {"state": "login_required", "login_session_id": "fixture-session"}
    if secret is not None:
        payload["prompt"] = {"kind": "WAIT_QRCODE", "qrcode_png_b64": secret}
    return client.portal.call(
        lambda: rig.agent.events.emit(event, account_id=account, channel=channel, payload=payload)
    )


@contextmanager
def subscribed(rig, client, *, token=ADMIN, accounts=None, channels=None, events=None):
    # Replay a harmless marker as a handshake barrier; do not infer readiness from sleep.
    marker = emit(rig, client)
    with client.websocket_connect(f"/api/v1/events?token={token}") as websocket:
        websocket.send_json({"subscribe": {
            "events": events or ["account_state"], "accounts": accounts or ["*"],
            "channels": channels or [], "since_seq": marker - 1,
        }})
        ready = websocket.receive_json()
        assert ready["seq"] == marker
        yield websocket


def test_current_qr_reaches_two_actual_websocket_subscribers(websocket_rig):
    rig, client = websocket_rig
    secret = base64.b64encode(png(81)).decode()
    with subscribed(rig, client) as first:
        with subscribed(rig, client) as second:
            # The first subscriber also receives the second subscriber's marker.
            first.receive_json()
            seq = emit(rig, client, secret=secret)
            frames = [first.receive_json(), second.receive_json()]
    assert [frame["seq"] for frame in frames] == [seq, seq]
    assert all(frame["account_id"] == "qq02" for frame in frames)
    assert [frame["payload"]["prompt"].get("qrcode_png_b64") for frame in frames] == [secret, secret]


def test_reconnecting_websocket_never_replays_a_cached_qr(websocket_rig):
    rig, client = websocket_rig
    secret = base64.b64encode(png(82)).decode()
    seq = emit(rig, client, secret=secret)
    for _ in range(2):
        with client.websocket_connect(f"/api/v1/events?token={ADMIN}") as websocket:
            websocket.send_json({"subscribe": {"events": ["account_state"], "since_seq": seq - 1}})
            frame = websocket.receive_json()
        assert frame["seq"] == seq
        assert frame["payload"]["prompt"]["kind"] == "WAIT_QRCODE"
        assert "qrcode_png_b64" not in frame["payload"]["prompt"]
        assert secret not in json.dumps(frame)


def test_actual_websocket_delivery_keeps_sqlite_and_webhooks_redacted(websocket_rig, caplog):
    rig, client = websocket_rig
    rig.store.con.execute(
        "INSERT INTO webhooks(id,name,url,secret_ref,created_ms,updated_ms) VALUES(?,?,?,?,?,?)",
        ("fixture", "fixture", "https://fake.invalid/webhook", "vault://fixture", rig.clock(), rig.clock()),
    )
    http = FakeHttp()
    dispatcher = WebhookDispatcher(
        rig.store, http=http, secret_provider=lambda row: "fixture-secret", clock=rig.clock
    )
    rig.agent.events.on_emit = dispatcher.fanout
    secret = base64.b64encode(png(83)).decode()
    with subscribed(rig, client) as websocket:
        seq = emit(rig, client, secret=secret)
        frame = websocket.receive_json()
        assert frame["seq"] == seq
    rows = [dict(row) for row in rig.store.con.execute("SELECT target,payload_json FROM events_outbox")]
    assert {row["target"] for row in rows} == {"ws", "webhook:fixture"}
    assert "qrcode_png_b64" not in json.dumps(rows)
    assert secret not in "\n".join(rig.store.con.iterdump())
    client.portal.call(dispatcher.tick)
    assert http.requests
    assert all(secret.encode() not in request["body"] for request in http.requests)
    assert all(b"qrcode_png_b64" not in request["body"] for request in http.requests)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "subscription,denied",
    [
        ({"token": LIMITED}, {"account": "qq01"}),
        ({"accounts": ["qq02"]}, {"account": "qq01"}),
        ({"channels": ["qq"]}, {"account": "qd01", "channel": "qidian"}),
        ({"events": ["account_state"]}, {"event": "message"}),
    ],
    ids=["token-account-acl", "account-filter", "channel-filter", "event-filter"],
)
def test_live_qr_preserves_acl_and_subscription_filters(websocket_rig, subscription, denied):
    rig, client = websocket_rig
    denied_secret = base64.b64encode(png(84)).decode()
    allowed_secret = base64.b64encode(png(85)).decode()
    with subscribed(rig, client, **subscription) as websocket:
        emit(rig, client, secret=denied_secret, **denied)
        allowed_seq = emit(rig, client, secret=allowed_secret)
        frame = websocket.receive_json()
    assert frame["seq"] == allowed_seq
    assert frame["account_id"] == "qq02"
    assert frame["channel"] == "qq"
    assert frame["event"] == "account_state"
    assert denied_secret not in json.dumps(frame)
    assert frame["payload"]["prompt"].get("qrcode_png_b64") == allowed_secret
