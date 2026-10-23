"""首次协议与网络初始化的独立开发者回归；只使用隔离假后端。"""
from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import replace

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi
from qtrade_agent.runtime.backends import AdbShellResult
from tests.test_qidian_apk_preparation import KEYBOARD, QIDIAN, SERIAL, _wait_for_stage_or_completed, apk_rig  # noqa: F401
from tests.test_qidian_ui import LOGIN_TREE, PKG, Clk, UiFakeAdb, acct, node, tree


AGREEMENT = tree(node(rid=f"{PKG}:id/dialogRightBtn", text="同意", bounds="[200,700][520,760]"))


class InitializationAdb(UiFakeAdb):
    """页面随操作变化，避免 dump 次数刚好匹配却没有完成协议的假绿。"""

    def __init__(self, scenario):
        super().__init__()
        self.scenario = scenario
        self.agreed = False
        self.restarted = False
        self.pages = []

    async def shell(self, serial, cmd):
        if cmd.startswith("input tap 360 730"):
            self.agreed = True
        if cmd.startswith("am force-stop"):
            self.restarted = True
        if cmd.startswith("uiautomator dump"):
            if self.scenario == "missing-agreement-and-form":
                page = tree(node(text="用户协议", clickable="false"))
            elif self.scenario == "agreement-never-disappears":
                page = AGREEMENT
            elif self.scenario == "agreement-returns-after-restart":
                page = AGREEMENT if not self.agreed or self.restarted else LOGIN_TREE
            else:
                page = AGREEMENT if not self.agreed else LOGIN_TREE
            self.dumps = [page]
            self.pages.append(page)
        return await super().shell(serial, cmd)


def initialization_ui(scenario):
    adb = InitializationAdb(scenario)
    clock = Clk()

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)

    return QidianUi(adb=adb, clock=clock, sleep=sleep), adb


@pytest.mark.parametrize("scenario", [
    "missing-agreement-and-form",
    "agreement-never-disappears",
    "agreement-returns-after-restart",
])
async def test_protocol_must_finish_before_prepared_is_true(scenario):
    ui, adb = initialization_ui(scenario)

    assert await ui.prepare_login(acct()) is False, "协议未完成或重启后仍停协议，不能只凭 IME 就宣称 ready"
    assert adb.typed == [], "初始化不得提前输入任何凭据"
    if scenario != "agreement-returns-after-restart":
        assert not adb.restarted, "协议真实消失之前不能提前强停并把首次初始化算完成"


async def test_agreement_disappears_then_restart_reads_login_form_before_ready():
    ui, adb = initialization_ui("success")

    assert await ui.prepare_login(acct()) is True
    restart = adb.cmds.index(f"am force-stop {PKG}")
    assert "input tap 360 730" in adb.cmds[:restart]
    assert any(c.startswith("uiautomator dump") for c in adb.cmds[restart + 1:]), "重启后必须读回可登录界面"
    assert adb.pages[-1] == LOGIN_TREE
    assert adb.typed == []


async def test_protocol_overlay_after_prepared_cannot_receive_credentials():
    adb = UiFakeAdb()
    clock = Clk()

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)

    ui = QidianUi(adb=adb, clock=clock, sleep=sleep)
    adb.dumps = [LOGIN_TREE]
    assert await ui.prepare_login(acct()) is True
    adb.dumps = [tree(node(rid=f"{PKG}:id/dialogRightBtn", text="同意", bounds="[200,700][520,760]"),
                      node(rid=f"{PKG}:id/account"), node(rid=f"{PKG}:id/password"),
                      node(rid=f"{PKG}:id/login", text="登录"))]

    outcome = await ui.login(acct(), "test-account", "test-only-secret")

    assert adb.typed == [], "WAIT_PASSWORD 期间又被协议覆盖，旧prepared不能授权填入凭据"
    assert outcome.result is None


@pytest.mark.parametrize("remember", [False, True], ids=["wait-password", "saved-password"])
async def test_android_network_failure_blocks_wait_password_and_login(apk_rig, monkeypatch, remember):
    rig = apk_rig(installed={QIDIAN, KEYBOARD})

    async def network_failure(row):
        return {"ok": False, "result": "DNS_FAIL", "side": "android", "targets": [], "elapsed_ms": 1}

    monkeypatch.setattr(rig.app.runtime, "check_qidian_network", network_failure, raising=False)
    account_id = await rig.create(remember=remember)
    row = await rig.start(account_id)

    assert (row["state"], row["state_code"]) == ("error", "NETWORK_UNAVAILABLE")
    assert rig.logins == []
    events = rig.app.store.list_events(event="account_state", account_id=account_id)
    assert all(e["payload"].get("state_code") != "WAIT_PASSWORD" for e in events)


async def test_android_failure_report_and_audit_never_contain_raw_diagnostics(apk_rig):
    rig = apk_rig(installed={QIDIAN, KEYBOARD})
    marker = "test-only-private-diagnostic-never-persist"
    rig.adb.shell_result_results[(SERIAL, "dns")] = [
        AdbShellResult(1, f"unknown host {marker}; password=test-only-secret; adb shell ping") for _ in range(3)
    ]

    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "NETWORK_UNAVAILABLE")
    audits = rig.app.store.con.execute("SELECT action, detail_json FROM audit_log").fetchall()
    network_audits = [r for r in audits if r[0] == "runtime.network_probe"]
    assert len(network_audits) == 1
    report = json.loads(network_audits[0][1])
    assert report["side"] == "android" and report["result"] == "DNS_FAIL"
    evidence = json.dumps([list(r) for r in audits])
    assert marker not in evidence and "test-only-secret" not in evidence and "adb shell" not in evidence


@pytest.mark.parametrize("stage", ["dns", "tcp"])
async def test_stop_during_android_probe_prevents_late_ready_or_login(apk_rig, monkeypatch, stage):
    rig = apk_rig(installed={QIDIAN, KEYBOARD})
    gate = asyncio.Event()
    entered = asyncio.Event()
    rig.adb.shell_result_gates[(SERIAL, stage)] = gate
    original = rig.adb.shell_result

    async def observe(serial, cmd, *, timeout_s):
        if ("dns" if "ping" in shlex.split(cmd) else "tcp") == stage:
            entered.set()
        return await original(serial, cmd, timeout_s=timeout_s)

    monkeypatch.setattr(rig.adb, "shell_result", observe)
    account_id = await rig.create()
    await rig.app.accounts.start(account_id, actor="test:network")
    await _wait_for_stage_or_completed(entered, rig.app.accounts.tasks[account_id])

    await rig.app.accounts.stop(account_id, actor="test:network")
    gate.set()
    await rig.app.accounts.wait_idle(account_id)

    assert rig.app.accounts.get(account_id)["state"] == "stopped"
    assert rig.logins == [] and not rig.adb.active_shell_results
    assert (SERIAL, stage) in rig.adb.cancelled_shell_results
    assert not any(e["payload"].get("state_code") == "WAIT_PASSWORD" for e in
                   rig.app.store.list_events(event="account_state", account_id=account_id))


async def test_ui_backend_hang_is_bounded_before_ready(monkeypatch):
    ui, adb = initialization_ui("success")
    ui.profiles = {name: replace(profile, timeouts={**profile.timeouts, "login_s": 0.02})
                   for name, profile in ui.profiles.items()}
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def hung_shell(serial, cmd):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(adb, "shell", hung_shell)
    assert await asyncio.wait_for(ui.prepare_login(acct()), timeout=0.5) is False
    assert entered.is_set() and cancelled.is_set()


async def test_cancelling_ui_preparation_propagates_without_typing(monkeypatch):
    ui, adb = initialization_ui("success")
    entered = asyncio.Event()

    async def hung_shell(serial, cmd):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adb, "shell", hung_shell)
    task = asyncio.create_task(ui.prepare_login(acct()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adb.typed == []


async def test_network_and_ui_complete_in_starting_before_wait_password(apk_rig, monkeypatch):
    stages = []

    async def prepare(row):
        stages.append(("ui", row["state"]))

    rig = apk_rig(installed={QIDIAN, KEYBOARD}, prepare_login_fn=prepare)

    async def network_ok(row):
        stages.append(("network", row["state"]))
        return {"ok": True, "result": "OK", "side": "android", "targets": [], "elapsed_ms": 1}

    monkeypatch.setattr(rig.app.runtime, "check_qidian_network", network_ok, raising=False)
    row = await rig.start(await rig.create(remember=False))

    assert (row["state"], row["state_code"]) == ("login_required", "WAIT_PASSWORD")
    assert stages == [("network", "starting"), ("ui", "starting")]
    assert rig.logins == []


@pytest.mark.parametrize("scenario", [
    "missing-agreement-and-form", "agreement-never-disappears", "agreement-returns-after-restart",
])
async def test_real_ui_preparation_failure_blocks_the_account_start_sequence(apk_rig, scenario):
    ui, adb = initialization_ui(scenario)
    rig = apk_rig(installed={QIDIAN, KEYBOARD}, prepare_login_fn=ui.prepare_login_fn())

    row = await rig.start(await rig.create(remember=False))

    assert (row["state"], row["state_code"]) == ("error", "UI_UNEXPECTED")
    assert rig.logins == [] and adb.typed == []
    assert not any(e["payload"].get("state_code") == "WAIT_PASSWORD" for e in
                   rig.app.store.list_events(event="account_state", account_id=row["id"]))
