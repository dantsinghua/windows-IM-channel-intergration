"""Nested open-chat diagnostics use synthetic ADB only and cannot affect actions."""
import asyncio
import json
import logging
import re

import pytest

from qtrade_agent.timing import timing_context
from tests.test_qidian_ui import CHAT_ACT, acct
from tests.test_qidian_verified_search_title import BODY, NAME, UID, make_ui


PREFIX = "qidian.open_chat."


def completed(caplog):
    records = []
    for row in caplog.records:
        if row.name != "qtrade.timing" or not row.getMessage().startswith("chain_timing "):
            continue
        event = json.loads(row.getMessage().split(" ", 1)[1])
        if event["stage"].startswith(PREFIX) and event["phase"] == "end":
            records.append(event)
    return records


async def test_strong_preflight_hit_logs_reuse_without_navigation(caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb = make_ui(store_name=NAME)
    adb.stage, adb.fg = "chat", CHAT_ACT
    with timing_context("synthetic-trace", "qd01"):
        outcome = await ui._open_chat(acct(), ui.profiles["default"], UID, NAME)
    assert outcome.ok and outcome.evidence["reused"] is True
    rows = completed(caplog)
    assert [row["stage"] for row in rows] == [PREFIX + "preflight"]
    assert rows[0]["outcome"] == "matched"
    assert rows[0]["trace_id"] == "synthetic-trace"
    assert rows[0]["account_id"] == "qd01"
    assert not any(cmd.startswith(("am start", "input tap", "am broadcast")) for cmd in adb.cmds)


async def test_navigation_steps_keep_private_inputs_out_of_diagnostics(caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb = make_ui()
    outcome = await ui.send(acct(), UID, BODY)
    assert outcome.ok and adb.sent == [BODY]
    rows = completed(caplog)
    assert [row["stage"] for row in rows] == [
        PREFIX + name for name in ("preflight", "launch", "prepare_home", "search_entry",
                                  "search_clear", "search_input", "search_results",
                                  "card_identity", "enter_chat")
    ]
    assert rows[0]["outcome"] == "navigate"
    assert all(row["status"] == "returned" and row["elapsed_ms"] >= 0 for row in rows)
    assert set().union(*(row.keys() for row in rows)) <= {
        "stage", "phase", "trace_id", "account_id", "elapsed_ms", "status", "error_type", "outcome"}
    encoded = json.dumps(rows)
    assert UID not in encoded and NAME not in encoded and BODY not in encoded


async def test_diagnostic_sink_failure_preserves_all_native_calls(monkeypatch):
    first, normal_adb = make_ui()
    expected = await first.send(acct(), UID, BODY)

    def broken_sink(*args, **kwargs):
        raise RuntimeError("synthetic diagnostic failure")

    monkeypatch.setattr(logging.getLogger("qtrade.timing"), "info", broken_sink)
    second, failing_adb = make_ui()
    actual = await second.send(acct(), UID, BODY)
    assert actual == expected
    # Unique UI-dump filenames intentionally differ between independent runs.
    def commands(rows):
        return [re.sub(r"qtrade-ui-[0-9a-f]{32}", "qtrade-ui-NONCE", command) for command in rows]
    assert commands(failing_adb.cmds) == commands(normal_adb.cmds)
    assert failing_adb.sent == normal_adb.sent == [BODY]


@pytest.mark.parametrize("cancelled", [False, True])
async def test_native_exception_is_never_swallowed_by_nested_diagnostics(monkeypatch, caplog, cancelled):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb = make_ui()
    failure = asyncio.CancelledError() if cancelled else OSError("synthetic private native failure")
    calls = []

    async def failed_launch(*args):
        calls.append("launch")
        raise failure

    monkeypatch.setattr(ui, "_launch", failed_launch)
    with pytest.raises(type(failure)) as caught:
        await ui._open_chat(acct(), ui.profiles["default"], UID, NAME)
    assert caught.value is failure and calls == ["launch"]
    assert adb.sent == []
    rows = completed(caplog)
    assert [row["stage"] for row in rows] == [PREFIX + "preflight", PREFIX + "launch"]
    assert rows[-1]["status"] == "error" and rows[-1]["error_type"] == type(failure).__name__
    assert "synthetic private native failure" not in json.dumps(rows)


async def test_card_identity_rejection_stays_before_composer_input(caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb = make_ui(card_uid="9000000999")
    outcome = await ui.send(acct(), UID, BODY)
    assert not outcome.ok and outcome.reason == "card_identity_mismatch"
    assert adb.sent == [] and adb.typed == [UID]
    rows = completed(caplog)
    assert rows[-1]["stage"] == PREFIX + "card_identity"
    assert rows[-1]["outcome"] == "rejected"
    assert not any(row["stage"] == PREFIX + "enter_chat" for row in rows)


async def test_logging_failure_cannot_replace_the_original_native_exception(monkeypatch):
    ui, adb = make_ui()
    failure = OSError("synthetic original native failure")
    calls = []

    async def failed_launch(*args):
        calls.append("launch")
        raise failure

    def broken_sink(*args, **kwargs):
        raise RuntimeError("synthetic diagnostic failure")

    monkeypatch.setattr(ui, "_launch", failed_launch)
    monkeypatch.setattr(logging.getLogger("qtrade.timing"), "info", broken_sink)
    with pytest.raises(OSError) as caught:
        await ui._open_chat(acct(), ui.profiles["default"], UID, NAME)
    assert caught.value is failure and calls == ["launch"]
    assert adb.sent == []
