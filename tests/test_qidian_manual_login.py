"""人完成企点登录后的只读回填；全假件，不连接真实账号。"""
from __future__ import annotations

import asyncio
import json

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi
from qtrade_agent.runtime.backends import AdbShellResult
from tests.conftest import make_rig
from tests.test_poll_maindb import env as poll_env  # noqa: F401
from tests.test_qidian_ui import LOGIN_TREE, MAIN_TREE, PKG, UiFakeAdb, acct, node, tree


UID = "3000008246"


@pytest.fixture
async def manual_rig(tmp_path):
    rig = make_rig(tmp_path)
    rig.store.ensure_account("qd01", "qidian", state="login_required")
    rig.store.con.execute("UPDATE accounts SET state_code='WAIT_PASSWORD', identity_json=? WHERE id='qd01'",
                          (json.dumps({"login_account": UID}),))
    yield rig
    for task in rig.agent.accounts.tasks.values():
        if not task.done():
            task.cancel()
    await asyncio.gather(*rig.agent.accounts.tasks.values(), return_exceptions=True)
    rig.store.close()


async def probe_tick(rig):
    """旧版没有观察任务时仍跑真实旧轮询，让RED落在缺失的业务状态而非缺符号。"""
    job = "qidian_login_probe" if "qidian_login_probe" in rig.agent.scheduler.jobs else "qidian_poll_all"
    await rig.agent.scheduler.run_once(job)


@pytest.mark.parametrize("state_code", ["WAIT_PASSWORD", "WAIT_SMS", "WAIT_CAPTCHA"])
async def test_confirmed_manual_login_is_reflected_in_account_state(manual_rig, monkeypatch, state_code):
    rig = manual_rig
    rig.store.con.execute("UPDATE accounts SET state_code=? WHERE id='qd01'", (state_code,))

    async def observed(row):
        return {"ready": True, "self_uid": UID, "reason": "confirmed"}

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    await probe_tick(rig)

    row = rig.agent.accounts.get("qd01")
    assert (row["state"], row["self_uid"]) == ("running", UID), "人已完成正确身份登录后，后台不得永久停在WAIT"
    events = rig.store.list_events(event="account_state", account_id="qd01")
    assert [e["payload"]["state"] for e in events] == ["logging_in", "running"]


@pytest.mark.parametrize("observation", [
    {"ready": True, "self_uid": "9000000001", "reason": "other-account"},
    {"ready": True, "self_uid": None, "reason": "no-uid"},
    {"ready": False, "self_uid": UID, "reason": "not-logged-in"},
    RuntimeError("test-only-private-diagnostic"),
], ids=["wrong-uid", "no-uid", "not-ready", "read-error"])
async def test_uncertain_manual_login_never_becomes_online(manual_rig, monkeypatch, observation):
    rig = manual_rig
    seen = []

    async def observed(row):
        seen.append(row["id"])
        if isinstance(observation, Exception):
            raise observation
        return observation

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    await probe_tick(rig)

    assert seen == ["qd01"]
    row = rig.agent.accounts.get("qd01")
    assert (row["state"], row["state_code"], row["self_uid"]) == ("login_required", "WAIT_PASSWORD", None)
    assert not rig.store.list_events(event="account_state", account_id="qd01")


@pytest.mark.parametrize("login_account", ["", "test@example.test", "13800000000"])
async def test_uid_is_not_inferred_from_a_missing_email_or_different_numeric_login(manual_rig, monkeypatch, login_account):
    rig = manual_rig
    rig.store.con.execute("UPDATE accounts SET identity_json=? WHERE id='qd01'", (json.dumps({"login_account": login_account}),))

    async def observed(row):
        return {"ready": True, "self_uid": UID, "reason": "confirmed"}

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    await probe_tick(rig)
    assert rig.agent.accounts.get("qd01")["state"] == "login_required"
    assert rig.agent.accounts.get("qd01")["self_uid"] is None


async def test_old_persisted_uid_cannot_override_the_current_expected_account(manual_rig, monkeypatch):
    rig = manual_rig
    old = "9000000001"
    rig.store.con.execute("UPDATE accounts SET self_uid=? WHERE id='qd01'", (old,))

    async def observed(row):
        return {"ready": True, "self_uid": old, "reason": "old-database"}

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    await probe_tick(rig)
    assert rig.agent.accounts.get("qd01")["state"] == "login_required"


@pytest.mark.parametrize("state,code", [
    ("running", None), ("logging_in", None), ("error", "NETWORK_UNAVAILABLE"),
    ("login_required", "KICKED"), ("login_required", "WAIT_DEVICE_CONFIRM"),
])
async def test_only_supported_wait_states_are_observed(manual_rig, monkeypatch, state, code):
    rig = manual_rig
    rig.store.con.execute("UPDATE accounts SET state=?, state_code=? WHERE id='qd01'", (state, code))
    seen = []

    async def observed(row):
        seen.append(row["id"])
        return {"ready": True, "self_uid": UID, "reason": "confirmed"}

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    await probe_tick(rig)
    assert seen == []
    assert rig.agent.accounts.get("qd01")["state"] == state


async def test_busy_account_is_not_probed(manual_rig, monkeypatch):
    rig = manual_rig
    seen = []
    busy_task = asyncio.create_task(asyncio.Event().wait())
    rig.agent.accounts.tasks["qd01"] = busy_task

    async def observed(row):
        seen.append(row["id"])
        return {"ready": True, "self_uid": UID, "reason": "confirmed"}

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    await probe_tick(rig)
    assert seen == []
    assert rig.agent.accounts.get("qd01")["state"] == "login_required"


@pytest.mark.parametrize("changed", ["state", "identity", "login-session", "busy"])
async def test_slow_observation_cannot_overwrite_a_newer_account_action(manual_rig, monkeypatch, changed):
    rig = manual_rig
    entered, release = asyncio.Event(), asyncio.Event()
    rig.agent.accounts._current_ls["qd01"] = "test-original-session"

    async def observed(row):
        entered.set()
        await release.wait()
        return {"ready": True, "self_uid": UID, "reason": "confirmed"}

    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", observed, raising=False)
    task = asyncio.create_task(probe_tick(rig))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if changed == "state":
            rig.store.con.execute("UPDATE accounts SET state='stopped' WHERE id='qd01'")
        elif changed == "identity":
            rig.store.con.execute("UPDATE accounts SET identity_json=? WHERE id='qd01'", (json.dumps({"login_account": "9000000001"}),))
        elif changed == "login-session":
            rig.agent.accounts._current_ls["qd01"] = "test-new-session"
        else:
            rig.agent.accounts.tasks["qd01"] = asyncio.create_task(asyncio.Event().wait())
        release.set()
        await task
        assert rig.agent.accounts.get("qd01")["state"] == ("stopped" if changed == "state" else "login_required")
        assert rig.agent.accounts.get("qd01")["self_uid"] is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class AndroidIdentityAdb(UiFakeAdb):
    """真实Toybox拒绝GNU ls选项；仅列文件元数据，不读取数据库内容。"""

    def __init__(self):
        super().__init__()
        self.fg = f"{PKG}/com.tencent.mobileqq.activity.SplashActivity"
        self.dumps = [MAIN_TREE]
        self.metadata = f"1758240000 /data/data/{PKG}/databases/{UID}.db\n"

    async def shell(self, serial, cmd):
        if "--time-style" in cmd:
            self.cmds.append(cmd)
            return "ls: Unknown option 'time-style=+%s' (see 'ls --help')\n"
        if "stat " in cmd:
            self.cmds.append(cmd)
            return self.metadata
        return await super().shell(serial, cmd)

    async def shell_result(self, serial, cmd, *, timeout_s):
        output = await self.shell(serial, cmd)
        return AdbShellResult(2 if "--time-style" in cmd else 0, output)


async def test_uid_discovery_uses_android_supported_file_metadata():
    adb = AndroidIdentityAdb()
    ui = QidianUi(adb=adb)

    assert await ui.discover_self_uid(acct()) == UID, "Android拒绝ls --time-style，不能因此永远发现不了已登录UID"
    assert not any("--time-style" in cmd for cmd in adb.cmds)
    assert not any("sqlite3" in cmd or "cat " in cmd for cmd in adb.cmds)


async def test_profile_degraded_account_with_uid_still_participates_in_read_poll(manual_rig, monkeypatch):
    rig = manual_rig
    rig.store.con.execute("UPDATE accounts SET state='degraded', state_code='UI_UNEXPECTED', self_uid=? WHERE id='qd01'", (UID,))
    polled = []

    async def read_only_poll(account):
        polled.append((account.id, account.state, account.self_uid))

    monkeypatch.setattr(rig.agent.adapters["qidian"], "poll", read_only_poll)
    await rig.agent.qidian_poll_all()

    assert polled == [("qd01", "degraded", UID)], "UI定位降级不应关闭既有主库只读收信路径"
    row = rig.agent.accounts.get("qd01")
    assert (row["state"], row["state_code"]) == ("degraded", "UI_UNEXPECTED")


async def test_probe_confirms_main_page_and_uid_using_only_reads():
    adb = AndroidIdentityAdb()
    ui = QidianUi(adb=adb)

    result = await ui.probe_login(acct())

    assert (result.result, result.self_uid) == ("running", UID)
    assert not any(cmd.startswith(("input ", "ime ", "am ", "settings put", "pm ", "stop ")) for cmd in adb.cmds)
    assert not any("ADB_INPUT" in cmd or "sqlite3" in cmd for cmd in adb.cmds)


@pytest.mark.parametrize("scenario", [
    "other-app", "login-activity", "only-message-description", "form-overlay", "agreement-overlay", "sms-overlay", "missing-uid", "ambiguous-uid",
])
async def test_unproven_ui_or_identity_is_never_reported_running(scenario):
    adb = AndroidIdentityAdb()
    if scenario == "other-app":
        adb.fg = "other.app/.SplashActivity"
    elif scenario == "login-activity":
        adb.fg = f"{PKG}/com.tencent.mobileqq.activity.LoginActivity"
    elif scenario == "only-message-description":
        adb.dumps = [tree(node(desc="消息"))]
    elif scenario == "form-overlay":
        adb.dumps = [tree(node(rid=f"{PKG}:id/recent_chat_list"), node(rid=f"{PKG}:id/password"))]
    elif scenario == "agreement-overlay":
        adb.dumps = [tree(node(rid=f"{PKG}:id/recent_chat_list"), node(rid=f"{PKG}:id/dialogRightBtn", text="同意"))]
    elif scenario == "sms-overlay":
        adb.dumps = [tree(node(rid=f"{PKG}:id/recent_chat_list"), node(text="短信验证"))]
    elif scenario == "missing-uid":
        adb.metadata = ""
    else:
        adb.metadata += f"1758240000 /data/data/{PKG}/databases/9000000001.db\n"

    result = await QidianUi(adb=adb).probe_login(acct())

    assert result.result is None
    assert not adb.typed and not adb.taps
    assert not any(cmd.startswith(("ime ", "am ")) for cmd in adb.cmds)


async def test_uid_prefers_latest_wal_and_refuses_ties():
    adb = AndroidIdentityAdb()
    adb.metadata = (f"1758240000 /data/data/{PKG}/databases/{UID}.db\n"
                    f"1758240100 /data/data/{PKG}/databases/{UID}.db-wal\n"
                    f"1758240090 /data/data/{PKG}/databases/9000000001.db-wal\n"
                    f"1758240999 /data/data/{PKG}/databases/9000000001.db\n")
    ui = QidianUi(adb=adb)
    assert await ui.discover_self_uid(acct()) == UID
    adb.metadata = adb.metadata.replace("1758240090", "1758240100")
    assert await ui.discover_self_uid(acct()) is None


def test_degraded_account_reads_new_messages_through_the_real_poller(store, clock, maindb):
    from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
    from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
    from qtrade_agent.alerts import Alerts
    from qtrade_agent.config import AgentConfig
    from qtrade_agent.events import Events

    events = Events(store)
    poller = QidianPoller(store=store, events=events, alerts=Alerts(events, clock=clock), cfg=AgentConfig(),
                          h13_firing=lambda: False, clock=clock,
                          maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path))
    account = QidianAccountView("qd01", "degraded", maindb.self_uin)
    poller.poll_maindb(account)  # 初次只建立历史水位。
    clock.advance(1000)
    maindb.insert_text("4000000001", "test-only-incoming-message", time_s=clock.now_s)

    poller.poll_maindb(account)

    assert store.count_messages("qd01") == 1, "UI降级但身份已确认，真实poller必须继续把新消息入库"
    assert store.list_events("message", "qd01")[0]["payload"]["text"] == "test-only-incoming-message"


@pytest.mark.parametrize("state", ["created", "starting", "login_required", "logging_in", "error", "stopped"])
def test_non_online_states_still_do_not_open_any_main_database(store, clock, state):
    from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
    from qtrade_agent.alerts import Alerts
    from qtrade_agent.config import AgentConfig
    from qtrade_agent.events import Events

    def never_read(uid):
        pytest.fail("未在线的账号不允许读取遗留主库")

    events = Events(store)
    poller = QidianPoller(store=store, events=events, alerts=Alerts(events, clock=clock), cfg=AgentConfig(),
                          h13_firing=lambda: False, clock=clock, maindb_factory=never_read)
    poller.poll_maindb(QidianAccountView("qd01", state, UID))
    assert store.count_messages("qd01") == 0


def test_degraded_account_keeps_group_gap_detection(poll_env):
    from qtrade_agent.adapters.qidian.poll import QidianAccountView
    from qtrade_agent.alerts import QIDIAN_MSG_GAP

    db, clock, poller = poll_env["maindb"], poll_env["clock"], poll_env["poller"]
    db.insert_text("4000000001", "test-only-group-first", time_s=clock.now_s, group=True, shmsgseq=1)
    db.insert_text("4000000001", "test-only-group-last", time_s=clock.now_s, group=True, shmsgseq=22)
    poller.poll_maindb(QidianAccountView("qd01", "running", db.self_uin))

    poller.check_group_gaps(QidianAccountView("qd01", "degraded", db.self_uin))

    assert poll_env["alerts"].is_firing(QIDIAN_MSG_GAP, "account:qd01")


@pytest.mark.parametrize("stage", ["dumpsys window", "uiautomator dump", "stat "])
async def test_read_errors_cannot_confirm_login_or_expose_raw_output(monkeypatch, stage):
    adb = AndroidIdentityAdb()
    original = adb.shell_result
    marker = "test-only-secret-read-error"

    async def fail_read(serial, cmd, *, timeout_s):
        if stage in cmd:
            return AdbShellResult(1, marker)
        return await original(serial, cmd, timeout_s=timeout_s)

    monkeypatch.setattr(adb, "shell_result", fail_read)
    result = await QidianUi(adb=adb).probe_login(acct())

    assert result.result is None
    assert marker not in repr(result)
    assert not adb.typed


async def test_cancelled_ui_read_propagates_without_any_login_action(monkeypatch):
    adb = AndroidIdentityAdb()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_read(serial, cmd, *, timeout_s):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(adb, "shell_result", blocked_read)
    task = asyncio.create_task(QidianUi(adb=adb).probe_login(acct()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not adb.typed and not adb.taps


async def test_explicit_probe_callback_is_wired_to_the_periodic_job(tmp_path):
    from qtrade_agent.app import AgentApp
    from qtrade_agent.config import AgentConfig
    from qtrade_agent.runtime import FakeAdb, FakeContainers
    from qtrade_agent.runtime.runtime import FakeFs
    from qtrade_agent.vault_client import FakeVault
    from qtrade_agent.winagent_client import FakeWinAgent
    from tests.conftest import Clock

    async def observed(row):
        return {"ready": True, "self_uid": UID, "reason": "confirmed"}

    wa = FakeWinAgent()
    app = AgentApp(AgentConfig(), db_path=str(tmp_path / "wire.db"), clock=Clock(auto_step_ms=50),
                   adb=FakeAdb(), containers=FakeContainers(), fs=FakeFs(), vault=FakeVault(),
                   winagent_transport=wa, winagent_base_url="http://winagent.fake:17610", winagent_token=wa.token,
                   login_probe_fn=observed).open()
    try:
        app.store.ensure_account("qd01", "qidian", state="login_required")
        app.store.con.execute("UPDATE accounts SET state_code='WAIT_PASSWORD', identity_json=? WHERE id='qd01'",
                              (json.dumps({"login_account": UID}),))
        assert 0 < app.scheduler.jobs["qidian_login_probe"].interval_s <= 5
        await app.scheduler.run_once("qidian_login_probe")
        assert app.accounts.get("qd01")["state"] == "running"
    finally:
        app.store.close()


async def test_manual_login_readback_enables_actual_incoming_ingestion_while_preserving_profile_warning(manual_rig, tmp_path, monkeypatch):
    from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
    from tests.conftest import FakeMainDb

    rig = manual_rig
    adb = AndroidIdentityAdb()
    ui = QidianUi(adb=adb)
    database = FakeMainDb(str(tmp_path / "test-only-main.db"), self_uin=UID)
    monkeypatch.setattr(rig.agent.accounts, "_login_probe_fn", ui.login_probe_fn())
    monkeypatch.setattr(rig.agent.poller, "maindb_factory", lambda uid: LocalSqliteMainDb(database.path))
    rig.agent.accounts.note_default_profile("qd01", "test-only-unknown-version")

    await rig.agent.scheduler.run_once("qidian_login_probe")
    row = rig.agent.accounts.get("qd01")
    assert (row["state"], row["state_code"], row["self_uid"]) == ("degraded", "UI_UNEXPECTED", UID)
    await rig.agent.qidian_poll_all()
    rig.clock.advance(1000)
    database.insert_text("4000000001", "test-only-confirmed-incoming", time_s=rig.clock.now_s)
    await rig.agent.qidian_poll_all()

    assert rig.store.count_messages("qd01") == 1
    assert rig.store.list_events("message", "qd01")[0]["payload"]["text"] == "test-only-confirmed-incoming"
    assert not adb.typed and not adb.taps
    assert not any(cmd.startswith(("ime ", "am ", "input ")) for cmd in adb.cmds)
