"""终审FR-01/FR-02：真实只读UI回调的profile事实与故障可观察性。"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import pytest

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.ui import QidianUi
from qtrade_agent.runtime.backends import AdbShellResult
from tests.conftest import FakeMainDb
from tests.test_qidian_manual_login import AndroidIdentityAdb, UID, manual_rig, probe_tick  # noqa: F401


VERSION = "99.0.0.0"
PRIVATE_MARKER = "synthetic-private-adb-error-must-not-persist"


def bind_probe(rig, monkeypatch, *, adb=None, unknown_version=True, state_code="WAIT_SMS"):
    adb = adb or AndroidIdentityAdb()
    rig.store.con.execute("UPDATE accounts SET state_code=? WHERE id='qd01'", (state_code,))
    if unknown_version:
        rig.store.upsert_runtime("qd01", kind="redroid", app_version=VERSION)
    ui = QidianUi(adb=adb, on_default_profile=rig.agent.accounts.note_default_profile)
    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", ui.login_probe_fn())
    return ui, adb


def diagnostic_evidence(rig, caplog):
    audits = rig.store.con.execute("SELECT action, detail_json FROM audit_log").fetchall()
    return caplog.text + json.dumps([list(row) for row in audits])


@pytest.mark.parametrize("state_code", ["WAIT_SMS", "WAIT_PASSWORD"])
async def test_unknown_version_is_degraded_from_real_probe_fact_and_still_reads(manual_rig, monkeypatch, tmp_path, state_code):
    rig = manual_rig
    _, adb = bind_probe(rig, monkeypatch, state_code=state_code)
    db = FakeMainDb(str(tmp_path / "isolated-review-main.db"), self_uin=UID)
    monkeypatch.setattr(rig.agent.poller, "maindb_factory", lambda uid: LocalSqliteMainDb(db.path))

    await probe_tick(rig)  # 新进程内无pending标志，由真正profile选择产生事实。

    row = rig.agent.accounts.get("qd01")
    assert (row["state"], row["state_code"], row["self_uid"]) == ("degraded", "UI_UNEXPECTED", UID)
    assert [e["payload"]["state"] for e in rig.store.list_events(event="account_state", account_id="qd01")] == [
        "logging_in", "running", "degraded"]
    await rig.agent.qidian_poll_all()
    rig.clock.advance(1000)
    db.insert_text("4000000001", "test-only-review-incoming", time_s=rig.clock.now_s)
    await rig.agent.qidian_poll_all()
    assert rig.store.count_messages("qd01") == 1
    assert not adb.typed and not adb.taps


class FailingRead(AndroidIdentityAdb):
    async def shell_result(self, serial, cmd, *, timeout_s):
        self.cmds.append(cmd)
        return AdbShellResult(1, PRIVATE_MARKER)


async def test_three_failed_real_ui_observations_are_visible_without_private_output(manual_rig, monkeypatch, caplog):
    rig = manual_rig
    _, adb = bind_probe(rig, monkeypatch, adb=FailingRead(), unknown_version=False)
    caplog.set_level(logging.DEBUG, logger="qtrade")
    caplog.clear()

    for _ in range(3):
        await probe_tick(rig)

    evidence = diagnostic_evidence(rig, caplog)
    assert "login_probe_unavailable" in evidence, "调度连续读失败不能只累计runs却不给任何可观察原因"
    assert PRIVATE_MARKER not in evidence
    assert (rig.agent.accounts.get("qd01")["state"], rig.agent.accounts.get("qd01")["state_code"]) == ("login_required", "WAIT_SMS")
    assert not adb.typed and not adb.taps
    assert not any(cmd.startswith(("ime ", "am ", "input ")) for cmd in adb.cmds)


def install_exact_profile(ui):
    ui.profiles[VERSION] = replace(ui.profiles["default"], version=VERSION)


@pytest.mark.parametrize("failure", ["wrong_uid", "not_main", "read_error"])
async def test_failed_default_profile_observation_does_not_taint_a_later_exact_profile_login(manual_rig, monkeypatch, failure):
    rig = manual_rig
    ui, adb = bind_probe(rig, monkeypatch)
    original_shell, original_focus, original_metadata = adb.shell_result, adb.fg, adb.metadata
    if failure == "wrong_uid":
        adb.metadata = adb.metadata.replace(UID, "9000000001")
    elif failure == "not_main":
        adb.fg = "other.app/.SplashActivity"
    else:
        async def fail_read(serial, cmd, *, timeout_s):
            return AdbShellResult(1, PRIVATE_MARKER)
        monkeypatch.setattr(adb, "shell_result", fail_read)

    await probe_tick(rig)

    assert (rig.agent.accounts.get("qd01")["state"], rig.agent.accounts.get("qd01")["state_code"]) == ("login_required", "WAIT_SMS")
    assert rig.store.list_events(event="account_state", account_id="qd01") == []
    install_exact_profile(ui)
    adb.fg, adb.metadata = original_focus, original_metadata
    monkeypatch.setattr(adb, "shell_result", original_shell)
    await probe_tick(rig)
    assert (rig.agent.accounts.get("qd01")["state"], rig.agent.accounts.get("qd01")["state_code"]) == ("running", None)


@pytest.mark.parametrize("interruption", ["cancel", "new-login-session"])
async def test_interrupted_default_profile_observation_cannot_commit_or_taint_retry(manual_rig, monkeypatch, interruption):
    rig = manual_rig
    ui, adb = bind_probe(rig, monkeypatch)
    original = adb.shell_result
    entered, release = asyncio.Event(), asyncio.Event()
    rig.agent.accounts._current_ls["qd01"] = "test-review-original-session"

    async def blocked_read(serial, cmd, *, timeout_s):
        entered.set()
        await release.wait()
        return await original(serial, cmd, timeout_s=timeout_s)

    monkeypatch.setattr(adb, "shell_result", blocked_read)
    task = asyncio.create_task(probe_tick(rig))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert rig.store.list_events(event="account_state", account_id="qd01") == []
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            rig.agent.accounts._current_ls["qd01"] = "test-review-new-session"
            release.set()
            await task
        assert (rig.agent.accounts.get("qd01")["state"], rig.agent.accounts.get("qd01")["state_code"]) == ("login_required", "WAIT_SMS")
        assert rig.store.list_events(event="account_state", account_id="qd01") == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    install_exact_profile(ui)
    monkeypatch.setattr(adb, "shell_result", original)
    await probe_tick(rig)
    assert (rig.agent.accounts.get("qd01")["state"], rig.agent.accounts.get("qd01")["state_code"]) == ("running", None)


def warning_messages(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name.startswith("qtrade") and record.levelno >= logging.WARNING]


async def test_same_read_failure_is_deduplicated_and_only_confirmed_ready_reports_recovery(manual_rig, monkeypatch, caplog):
    rig = manual_rig
    adb = AndroidIdentityAdb()
    original = adb.shell_result
    bind_probe(rig, monkeypatch, adb=adb, unknown_version=False)
    caplog.set_level(logging.DEBUG, logger="qtrade")
    caplog.clear()

    async def fail_read(serial, cmd, *, timeout_s):
        return AdbShellResult(1, PRIVATE_MARKER)

    monkeypatch.setattr(adb, "shell_result", fail_read)
    for _ in range(3):
        await probe_tick(rig)
    failures = warning_messages(caplog)
    assert len(failures) == 1
    assert "login_probe_unavailable" in failures[0] and "foreground" in failures[0]
    monkeypatch.setattr(adb, "shell_result", original)
    original_focus = adb.fg
    adb.fg = "other.app/.SplashActivity"
    await probe_tick(rig)
    assert "recovered" not in caplog.text, "只是离开主界面不能证明原故障已恢复"
    adb.fg = original_focus
    await probe_tick(rig)
    await probe_tick(rig)
    recovered = [record.getMessage() for record in caplog.records
                 if record.levelno == logging.INFO and "recovered" in record.getMessage()]
    assert len(recovered) == 1
    assert rig.agent.accounts.get("qd01")["state"] == "running"
    assert PRIVATE_MARKER not in diagnostic_evidence(rig, caplog)


async def test_normal_foreground_waiting_is_silent(manual_rig, monkeypatch, caplog):
    rig = manual_rig
    _, adb = bind_probe(rig, monkeypatch)
    adb.fg = "other.app/.SplashActivity"
    caplog.set_level(logging.DEBUG, logger="qtrade")
    caplog.clear()

    for _ in range(3):
        await probe_tick(rig)

    assert warning_messages(caplog) == []
    assert "recovered" not in caplog.text
    assert rig.agent.accounts.get("qd01")["state"] == "login_required"


async def test_distinct_read_failure_stage_is_reported_without_echoing_exception_text(manual_rig, monkeypatch, caplog):
    rig = manual_rig
    _, adb = bind_probe(rig, monkeypatch, unknown_version=False)
    original = adb.shell_result
    failed_stage = "foreground"

    async def fail_selected_read(serial, cmd, *, timeout_s):
        if failed_stage == "foreground" or "stat " in cmd:
            raise RuntimeError(PRIVATE_MARKER)
        return await original(serial, cmd, timeout_s=timeout_s)

    monkeypatch.setattr(adb, "shell_result", fail_selected_read)
    caplog.set_level(logging.DEBUG, logger="qtrade")
    caplog.clear()
    await probe_tick(rig)
    failed_stage = "identity"
    await probe_tick(rig)
    await probe_tick(rig)

    messages = warning_messages(caplog)
    assert len(messages) == 2
    assert any("foreground" in message for message in messages)
    assert any("identity" in message for message in messages)
    assert PRIVATE_MARKER not in diagnostic_evidence(rig, caplog)
    assert rig.agent.accounts.get("qd01")["state"] == "login_required"
