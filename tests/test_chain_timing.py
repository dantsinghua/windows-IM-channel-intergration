"""Collector chain timings are diagnostic only; all execution uses fake backends."""
import json
import logging

from qtrade_agent.models import Command
from test_bus import cfg, rig as bus_rig
from test_qidian_ui import CHAT_TREE, _FakeStore, acct, chat_tree_with_input, rig as ui_rig


def timings(caplog):
    return [json.loads(record.getMessage().split(" ", 1)[1])
            for record in caplog.records
            if record.name == "qtrade.timing" and record.getMessage().startswith("chain_timing ")]


async def test_send_and_confirmation_have_separate_trace_boundaries(bus_rig, caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    secret_text = "SYNTHETIC_PRIVATE_MESSAGE key=do-not-log"
    result = await bus_rig["bus"].submit(Command(
        "qd01", "send_text", {"session": "qd01:415011447", "text": secret_text},
        idempotency_key="timing-delivered"))
    assert result.code == "DELIVERED"
    events = timings(caplog)
    for stage in ("collector.native.send", "collector.native.confirm"):
        pair = [event for event in events if event["stage"] == stage]
        assert [event["phase"] for event in pair] == ["start", "end"]
        assert all(event["trace_id"] == result.trace_id and event["account_id"] == "qd01" for event in pair)
        assert 0 <= pair[-1]["elapsed_ms"] < 5000
    results = [event for event in events if event["stage"] == "collector.command.result"]
    assert len(results) == 1 and results[0]["status"] == "DELIVERED"
    encoded = json.dumps(events)
    assert secret_text not in encoded and "415011447" not in encoded
    assert set().union(*(event.keys() for event in events)) <= {
        "stage", "phase", "trace_id", "account_id", "elapsed_ms", "status", "error_type", "event_id"}


async def test_unknown_confirmation_is_not_reported_as_delivery(bus_rig, caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    bus_rig["sender"].deliver = False
    result = await bus_rig["bus"].submit(Command(
        "qd01", "send_text", {"session": "qd01:415011447", "text": "SYNTHETIC_UNKNOWN"},
        idempotency_key="timing-unknown", timeout_ms=30000))
    assert result.code == "SEND_CALLED_BUT_UNCONFIRMED"
    events = timings(caplog)
    results = [event for event in events if event["stage"] == "collector.command.result"]
    assert len(results) == 1 and results[0]["status"] == "SEND_CALLED_BUT_UNCONFIRMED"
    assert not any(event["status"] == "DELIVERED" for event in events)
    assert bus_rig["store"].idem_get("qd01", "timing-unknown")["status"] == "SENDING"


async def test_ui_logs_actions_but_does_not_claim_native_delivery(ui_rig, caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb, _clock = ui_rig
    body = "SYNTHETIC_UI_ONLY"
    adb.dumps = [CHAT_TREE, CHAT_TREE, CHAT_TREE, chat_tree_with_input(body), CHAT_TREE, CHAT_TREE]
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    outcome = await ui.send(acct(), "10001", body)
    assert outcome.ok
    events = timings(caplog)
    outer = [event for event in events if not event["stage"].startswith("qidian.open_chat.")]
    stages = [event["stage"] for event in outer if event["phase"] == "end"]
    assert stages == [
        "qidian.prepare_ime", "qidian.open_chat", "qidian.verify_before_input",
        "qidian.input_text", "qidian.verify_input", "qidian.verify_before_send", "qidian.tap_send"]
    assert [(event["stage"], event["phase"]) for event in outer] == [
        (stage, phase) for stage in stages for phase in ("start", "end")]
    children = [(index, event) for index, event in enumerate(events)
                if event["stage"].startswith("qidian.open_chat.")]
    assert [(event["stage"], event["phase"]) for _, event in children] == [
        ("qidian.open_chat.preflight", "start"), ("qidian.open_chat.preflight", "end")]
    assert children[-1][1]["outcome"] == "matched"
    opening = [(index, event) for index, event in enumerate(events)
               if event["stage"] == "qidian.open_chat"]
    assert [event["phase"] for _, event in opening] == ["start", "end"]
    assert opening[0][0] < children[0][0] < children[-1][0] < opening[-1][0]
    assert 0 <= children[-1][1]["elapsed_ms"] <= opening[-1][1]["elapsed_ms"]
    assert adb._dump_i == 6 and adb.typed == [body]
    assert adb.taps.count("input tap 660 1085") == 1
    assert all(event["account_id"] == "qd01" for event in events)
    assert all(event["status"] != "DELIVERED" for event in events)
    encoded = json.dumps(events)
    assert body not in encoded and "10001" not in encoded and "张三" not in encoded
    assert "张三" not in json.dumps(events, ensure_ascii=False)


async def test_message_publication_logs_only_after_ingest(bus_rig, caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    rig = bus_rig
    body = "SYNTHETIC_INBOUND_PRIVATE"
    rig["maindb"].insert_text("415011447", body, time_s=rig["clock"].now_s)
    poller = rig["poller"]
    adapter = rig["bus"].adapters["qidian"]
    poller.poll_maindb(adapter._view(rig["bus"]._load_account("qd01")))
    events = timings(caplog)
    stages = [event["stage"] for event in events]
    assert "collector.message.ingest" in stages and "collector.message.publish" in stages
    assert stages.index("collector.message.ingest") < stages.index("collector.message.publish")
    completed = [event for event in events if event["stage"] == "collector.message.publish" and event["phase"] == "end"]
    assert completed and all(isinstance(event["event_id"], int) for event in completed)
    assert body not in json.dumps(events) and "415011447" not in json.dumps(events)


async def test_diagnostic_sink_failure_cannot_fail_or_resend_command(bus_rig, monkeypatch):
    attempts = []
    def unavailable_sink(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("synthetic diagnostic sink unavailable")
    monkeypatch.setattr(logging.getLogger("qtrade.timing"), "info", unavailable_sink)
    result = await bus_rig["bus"].submit(Command(
        "qd01", "send_text", {"session": "qd01:415011447", "text": "SYNTHETIC_LOG_FAILURE"},
        idempotency_key="timing-log-failure"))
    assert result.code == "DELIVERED"
    assert attempts
    assert len(bus_rig["sender"].calls) == 1

