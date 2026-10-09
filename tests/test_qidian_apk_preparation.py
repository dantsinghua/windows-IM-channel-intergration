"""05 §2.1.1 ⑥:全新 Android 必须先校验、补齐 APK,再进入登录。

所有账号、容器、ADB 与 Vault 都是假件;APK 是临时目录中的虚构字节。
本文件不启动 Agent 调度器,不连接真实 Docker/ADB/WinAgent。
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, RuntimeConfig
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs
from qtrade_agent.vault_client import FakeVault
from qtrade_agent.winagent_client import FakeWinAgent
from tests.conftest import Clock


SERIAL = "127.0.0.1:16001"
QIDIAN = "com.tencent.qidian"
KEYBOARD = "com.android.adbkeyboard"
QIDIAN_BYTES = b"test-only-qidian-apk-trusted-v1"
KEYBOARD_BYTES = b"test-only-adbkeyboard-rootfs-v1"
QIDIAN_SHA = hashlib.sha256(QIDIAN_BYTES).hexdigest()


class RecordingAdb(FakeAdb):
    def __init__(self):
        super().__init__()
        self.installed_payloads: list[tuple[str, bytes]] = []

    async def install(self, serial, apk_path, *, expected_package):
        self.installed_payloads.append((expected_package, Path(apk_path).read_bytes()))
        await super().install(serial, apk_path, expected_package=expected_package)


@dataclass
class ApkRig:
    app: AgentApp
    adb: RecordingAdb
    cache: Path
    source: Path
    keyboard: Path
    logins: list[set[str]] = field(default_factory=list)

    async def create(self, *, remember=True):
        row = await self.app.accounts.create({
            "channel": "qidian", "label": "isolated-apk-test",
            "login": {"mode": "password", "account": "test-account",
                      "secret": "test-only-secret", "remember": remember},
        }, actor="test:apk")
        return row["id"]

    async def start(self, account_id):
        await self.app.accounts.start(account_id, actor="test:apk")
        await asyncio.wait_for(self.app.accounts.wait_idle(account_id), timeout=5)
        return self.app.accounts.get(account_id)


@pytest.fixture
async def apk_rig(tmp_path):
    rigs = []

    def build(*, installed=(), apk_url=None, apk_sha256=QIDIAN_SHA, prepare_login_fn=None):
        root = tmp_path / f"rig-{len(rigs)}"
        root.mkdir()
        source = root / "source.apk"
        source.write_bytes(QIDIAN_BYTES)
        keyboard = root / "ADBKeyboard.apk"
        keyboard.write_bytes(KEYBOARD_BYTES)
        cache = root / "apk-cache"
        cfg = AgentConfig(runtime=RuntimeConfig(
            accounts_dir=str(root / "accounts"), apk_cache_dir=str(cache),
            apk_url=source.as_uri() if apk_url is None else apk_url,
            apk_sha256=apk_sha256,
        ))
        adb = RecordingAdb()
        adb.installed_packages_by_serial[SERIAL] = set(installed)
        logins = []

        async def login(row, account, secret):
            logins.append(set(adb.installed_packages_by_serial[SERIAL]))
            return "running"

        wa = FakeWinAgent()
        app = AgentApp(
            cfg, db_path=str(root / "agent.db"), data_dir=str(root),
            clock=Clock(auto_step_ms=50), containers=FakeContainers(), adb=adb,
            vault=FakeVault(), fs=FakeFs(), login_fn=login, boot_poll_s=0,
            wsl_total_mb=11264, winagent_transport=wa,
            winagent_base_url="http://winagent.fake:17610", winagent_token=wa.token,
            adbkeyboard_apk=str(keyboard),
            prepare_login_fn=prepare_login_fn,
        ).open()
        rig = ApkRig(app, adb, cache, source, keyboard, logins)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        for task in rig.app.accounts.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*rig.app.accounts.tasks.values(), return_exceptions=True)
        rig.app.store.close()


async def test_fresh_android_installs_both_trusted_packages_before_login(apk_rig):
    rig = apk_rig()
    account_id = await rig.create()
    assert rig.adb.install_calls == []  # #2 仅建档。
    row = await rig.start(account_id)

    assert rig.logins == [{QIDIAN, KEYBOARD}], "未补齐两包就调用登录执行体"
    assert row["state"] == "running"
    assert {package for _, _, package in rig.adb.install_calls} == {QIDIAN, KEYBOARD}
    assert dict(rig.adb.installed_payloads) == {QIDIAN: QIDIAN_BYTES, KEYBOARD: KEYBOARD_BYTES}
    assert all(serial == SERIAL for serial, _, _ in rig.adb.install_calls)


@pytest.mark.parametrize("overrides", [
    {"apk_url": ""},
    {"apk_sha256": ""},
    {"apk_sha256": "0" * 64},
    {"apk_sha256": "not-a-sha256"},
], ids=["missing-url", "missing-sha", "sha-mismatch", "malformed-sha"])
async def test_untrusted_or_unavailable_apk_blocks_all_installation_and_login(apk_rig, overrides):
    rig = apk_rig(**overrides)
    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "APK_UNAVAILABLE")
    assert rig.adb.install_calls == []
    assert rig.logins == []
    assert not list(rig.cache.glob("*.part"))


async def test_package_query_failure_is_install_failed_without_login(apk_rig):
    rig = apk_rig()
    rig.adb.package_query_errors[(SERIAL, QIDIAN)] = RuntimeError("device offline")
    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "INSTALL_FAILED")
    assert rig.adb.install_calls == []
    assert rig.logins == []


@pytest.mark.parametrize("failure", ["install-error", "missing-after-install", "query-after-install"])
async def test_failed_installation_never_reaches_login(apk_rig, failure):
    rig = apk_rig(installed={KEYBOARD})
    key = (SERIAL, QIDIAN)
    if failure == "install-error":
        rig.adb.install_errors[key] = RuntimeError("Failure [INSTALL_FAILED_INVALID_APK]")
    elif failure == "missing-after-install":
        rig.adb.install_missing_packages.add(key)
    else:
        rig.adb.package_query_results[key] = [False, RuntimeError("device offline after install")]
    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "INSTALL_FAILED")
    assert len(rig.adb.install_calls) == 1
    assert rig.logins == []


async def test_installed_packages_need_no_source_or_upgrade(apk_rig):
    rig = apk_rig(installed={QIDIAN, KEYBOARD}, apk_url="", apk_sha256="")
    rig.source.unlink()
    row = await rig.start(await rig.create())

    assert row["state"] == "running"
    assert rig.logins == [{QIDIAN, KEYBOARD}]
    assert rig.adb.install_calls == []
    assert not rig.cache.exists()
    assert not any("pm clear" in cmd or "uninstall" in cmd for cmd in rig.adb.shell_cmds(SERIAL))


async def test_without_saved_password_prepares_packages_before_wait_password(apk_rig):
    rig = apk_rig()
    row = await rig.start(await rig.create(remember=False))

    assert (row["state"], row["state_code"]) == ("login_required", "WAIT_PASSWORD")
    assert rig.adb.installed_packages_by_serial[SERIAL] == {QIDIAN, KEYBOARD}
    assert rig.logins == []


@pytest.mark.parametrize("installed, expected", [
    ({KEYBOARD}, QIDIAN), ({QIDIAN}, KEYBOARD),
], ids=["only-qidian-missing", "only-keyboard-missing"])
async def test_installs_only_the_missing_package(apk_rig, installed, expected):
    rig = apk_rig(installed=installed)
    row = await rig.start(await rig.create())

    assert row["state"] == "running"
    assert [package for _, _, package in rig.adb.install_calls] == [expected]
    assert rig.logins == [{QIDIAN, KEYBOARD}]


async def test_missing_builtin_keyboard_is_apk_unavailable_before_any_install(apk_rig):
    rig = apk_rig()
    rig.keyboard.unlink()
    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "APK_UNAVAILABLE")
    assert rig.adb.install_calls == []
    assert rig.logins == []


async def test_download_error_is_apk_unavailable_not_container_exit(apk_rig):
    rig = apk_rig()
    rig.source.unlink()
    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "APK_UNAVAILABLE")
    assert rig.adb.install_calls == []
    assert rig.logins == []
    assert not list(rig.cache.glob("*.part"))


async def _wait_for_stage_or_completed(stage, task):
    """由事件或业务任务完成唤醒;不以盲睡碰运气。"""
    waiter = asyncio.create_task(stage.wait())
    try:
        await asyncio.wait_for(asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED), 5)
        assert stage.is_set(), "启动流程未到达预期安装/下载阶段"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_stop_during_install_cancels_work_and_cannot_login_later(apk_rig):
    rig = apk_rig(installed={KEYBOARD})
    gate = asyncio.Event()
    rig.adb.install_gates[(SERIAL, QIDIAN)] = gate
    account_id = await rig.create()
    await rig.app.accounts.start(account_id, actor="test:apk")
    await _wait_for_stage_or_completed(rig.adb.install_started, rig.app.accounts.tasks[account_id])

    await rig.app.accounts.stop(account_id, actor="test:apk")
    await rig.app.accounts.wait_idle(account_id)
    gate.set()

    assert rig.app.accounts.get(account_id)["state"] == "stopped"
    assert rig.logins == []
    assert rig.adb.active_installs == set()
    assert rig.adb.cancelled_installs == [(SERIAL, QIDIAN)]
    assert QIDIAN not in rig.adb.installed_packages_by_serial[SERIAL]
    assert not list(rig.cache.glob("*.part"))


async def test_stop_during_download_removes_only_own_partial_file(apk_rig, monkeypatch):
    from qtrade_agent.runtime import apk

    entered = asyncio.Event()
    release = asyncio.Event()

    async def download(url, destination, *, timeout_s=300, honor_env_proxy=True):
        Path(destination).write_bytes(b"incomplete-untrusted-apk")
        entered.set()
        await release.wait()

    monkeypatch.setattr(apk, "download_apk", download)
    rig = apk_rig()
    rig.cache.mkdir()
    unrelated = rig.cache / "other-task.part"
    unrelated.write_bytes(b"other-task-still-running")
    account_id = await rig.create()
    await rig.app.accounts.start(account_id, actor="test:apk")
    await _wait_for_stage_or_completed(entered, rig.app.accounts.tasks[account_id])
    assert len(list(rig.cache.glob("*.part"))) == 2

    await rig.app.accounts.stop(account_id, actor="test:apk")
    await rig.app.accounts.wait_idle(account_id)
    release.set()

    assert rig.app.accounts.get(account_id)["state"] == "stopped"
    assert rig.adb.install_calls == []
    assert rig.logins == []
    assert list(rig.cache.glob("*.part")) == [unrelated]
    assert unrelated.read_bytes() == b"other-task-still-running"


async def test_complete_cache_is_reused_without_fetching_source(apk_rig, monkeypatch):
    from qtrade_agent.runtime import apk

    rig = apk_rig(installed={KEYBOARD})
    account_id = await rig.create()
    assert (await rig.start(account_id))["state"] == "running"
    await rig.app.accounts.stop(account_id, actor="test:apk")
    await rig.app.accounts.wait_idle(account_id)
    rig.adb.installed_packages_by_serial[SERIAL] = {KEYBOARD}
    rig.source.unlink()

    async def no_download(*args, **kwargs):
        pytest.fail("完整且校验正确的缓存不应再次下载")

    monkeypatch.setattr(apk, "download_apk", no_download)
    assert (await rig.start(account_id))["state"] == "running"
    assert rig.logins == [{QIDIAN, KEYBOARD}, {QIDIAN, KEYBOARD}]


async def test_tampered_cache_is_not_installed_when_source_is_unavailable(apk_rig):
    rig = apk_rig(installed={KEYBOARD})
    account_id = await rig.create()
    assert (await rig.start(account_id))["state"] == "running"
    cached_path = Path(rig.adb.install_calls[0][1])
    assert cached_path.is_relative_to(rig.cache)
    cached_path.write_bytes(b"tampered-cached-apk")
    rig.source.unlink()
    await rig.app.accounts.stop(account_id, actor="test:apk")
    await rig.app.accounts.wait_idle(account_id)
    rig.adb.installed_packages_by_serial[SERIAL] = {KEYBOARD}
    rig.adb.install_calls.clear()
    rig.logins.clear()

    row = await rig.start(account_id)
    assert (row["state"], row["state_code"]) == ("error", "APK_UNAVAILABLE")
    assert rig.adb.install_calls == []
    assert rig.logins == []


class ScriptedProcess:
    """仅测试进程句柄;不会创建 OS 子进程。"""
    def __init__(self, result, *, blocked=False):
        self.result = result
        self.blocked = blocked
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False
        self.released = asyncio.Event()

    async def communicate(self):
        if self.blocked:
            await self.released.wait()
        if self.returncode is None:
            self.returncode = self.result[0]
        self.released.set()
        await self.wait()  # asyncio.Process.communicate() 在收完 stdout/stderr 后等待退出。
        return self.result[1].encode(), b""

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.released.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.released.set()

    async def wait(self):
        await self.released.wait()
        self.waited = True
        return self.returncode


@pytest.fixture
def cli_rig(monkeypatch):
    from qtrade_agent.runtime import backends

    def build(*, install=(0, "Success\n"), query=(0, "package:com.tencent.qidian\n"), blocked=False):
        calls = []
        processes = []
        started = asyncio.Event()

        def result_for(argv):
            assert argv[:5] == ["adb-test-only", "-P", "16000", "-s", SERIAL]
            calls.append(argv)
            if argv[5] == "install":
                assert "-r" not in argv and "-d" not in argv
                return install
            assert argv[5:] == ["shell", "pm", "list", "packages", QIDIAN]
            return query

        async def subprocess_exec(*argv, **kwargs):
            result = result_for(list(argv))
            process = ScriptedProcess(result, blocked=blocked and argv[5] == "install")
            processes.append(process)
            if argv[5] == "install":
                started.set()
            return process

        async def run(argv, **kwargs):
            return result_for(argv)

        monkeypatch.setattr(backends.asyncio, "create_subprocess_exec", subprocess_exec)
        monkeypatch.setattr(backends, "_run", run)
        return backends.AdbCliBackend(adb_bin="adb-test-only"), calls, processes, started

    return build


@pytest.mark.parametrize("result, expected", [
    ((0, "package:com.tencent.qidian\n"), True),
    ((0, ""), False),
    ((0, "package:com.tencent.qidian.fake\n"), False),
])
async def test_cli_package_query_matches_exact_package(cli_rig, result, expected):
    adb, calls, _, _ = cli_rig(query=result)
    assert await adb.package_installed(SERIAL, QIDIAN) is expected
    assert len(calls) == 1


@pytest.mark.parametrize("result", [
    (1, "error: device offline"), (0, "error: device offline"),
    (0, "package:com.tencent.qidian\nError: package manager not ready\n"),
])
async def test_cli_query_error_is_not_treated_as_absent(cli_rig, result):
    adb, _, _, _ = cli_rig(query=result)
    with pytest.raises(RuntimeError):
        await adb.package_installed(SERIAL, QIDIAN)


@pytest.mark.parametrize("install", [
    (1, "Success\n"),
    (0, "Failure [INSTALL_FAILED_INVALID_APK]\n"),
    (0, "Performing Streamed Install\n"),
    (0, "NotSuccess\n"),
    (0, "Success\nFailure [INSTALL_FAILED_INVALID_APK]\n"),
])
async def test_cli_install_rejects_exit_or_result_failure(cli_rig, install, tmp_path):
    adb, _, _, _ = cli_rig(install=install)
    apk_path = tmp_path / "test.apk"
    apk_path.write_bytes(QIDIAN_BYTES)
    with pytest.raises(RuntimeError):
        await adb.install(SERIAL, str(apk_path), expected_package=QIDIAN)


@pytest.mark.parametrize("query", [(0, ""), (1, "device offline"), (0, "package:unrelated.app\n")])
async def test_cli_install_success_requires_post_install_package_presence(cli_rig, query, tmp_path):
    adb, _, _, _ = cli_rig(query=query)
    apk_path = tmp_path / "test.apk"
    apk_path.write_bytes(QIDIAN_BYTES)
    with pytest.raises(RuntimeError):
        await adb.install(SERIAL, str(apk_path), expected_package=QIDIAN)


async def test_cli_install_success_checks_expected_package(cli_rig, tmp_path):
    adb, calls, _, _ = cli_rig()
    apk_path = tmp_path / "test.apk"
    apk_path.write_bytes(QIDIAN_BYTES)
    await adb.install(SERIAL, str(apk_path), expected_package=QIDIAN)

    assert calls == [
        ["adb-test-only", "-P", "16000", "-s", SERIAL, "install", str(apk_path)],
        ["adb-test-only", "-P", "16000", "-s", SERIAL, "shell", "pm", "list", "packages", QIDIAN],
    ]


async def test_stop_reaps_only_the_current_install_subprocess(apk_rig, cli_rig):
    adb, calls, processes, started = cli_rig(blocked=True)
    rig = apk_rig(installed={KEYBOARD})
    rig.adb.install = adb.install
    account_id = await rig.create()
    await rig.app.accounts.start(account_id, actor="test:apk")
    await _wait_for_stage_or_completed(started, rig.app.accounts.tasks[account_id])

    await rig.app.accounts.stop(account_id, actor="test:apk")
    await rig.app.accounts.wait_idle(account_id)

    assert rig.app.accounts.get(account_id)["state"] == "stopped"
    assert rig.logins == []
    assert len(processes) == 1
    assert processes[0].terminated or processes[0].killed
    assert processes[0].waited
    assert processes[0].returncode is not None
    assert len(calls) == 1  # 取消后不能补查包或继续安装。
    assert not list(rig.cache.glob("*.part"))


async def test_wait_password_is_published_only_after_packages_and_ui_are_ready(apk_rig):
    entered = asyncio.Event()
    release = asyncio.Event()
    packages_at_prepare = []

    async def prepare(row):
        packages_at_prepare.append(set(rig.adb.installed_packages_by_serial[SERIAL]))
        entered.set()
        await release.wait()

    rig = apk_rig(prepare_login_fn=prepare)
    account_id = await rig.create(remember=False)
    await rig.app.accounts.start(account_id, actor="test:apk")
    await _wait_for_stage_or_completed(entered, rig.app.accounts.tasks[account_id])

    assert packages_at_prepare == [{QIDIAN, KEYBOARD}]
    assert rig.app.accounts.get(account_id)["state"] == "starting"
    assert not any(event["payload"]["state"] == "login_required" for event in
                   rig.app.store.list_events(event="account_state", account_id=account_id))
    assert rig.logins == []
    release.set()
    await rig.app.accounts.wait_idle(account_id)
    row = rig.app.accounts.get(account_id)
    assert (row["state"], row["state_code"]) == ("login_required", "WAIT_PASSWORD")


async def test_ui_preparation_failure_cannot_enter_login(apk_rig):
    async def prepare(row):
        raise RuntimeError("input method readback mismatch")

    rig = apk_rig(prepare_login_fn=prepare)
    row = await rig.start(await rig.create())

    assert (row["state"], row["state_code"]) == ("error", "UI_UNEXPECTED")
    assert rig.logins == []
    assert not any(event["payload"]["state"] == "login_required" for event in
                   rig.app.store.list_events(event="account_state", account_id=row["id"]))


@pytest.fixture
def ui_rig():
    from qtrade_agent.adapters.qidian.ui import QidianUi
    from tests.test_qidian_ui import UiFakeAdb, Clk

    adb, clock = UiFakeAdb(), Clk()

    async def sleep(seconds):
        clock.ms += int(seconds * 1000)

    return QidianUi(adb=adb, clock=clock, sleep=sleep), adb


async def test_ui_preparation_enables_ime_and_restarts_without_credentials(ui_rig):
    from tests.test_qidian_ui import LOGIN_TREE, acct, node, tree

    ui, adb = ui_rig
    adb.ime_list = "com.android.inputmethod/.Ime\n"
    adb.dumps = [tree(node(text="同意", bounds="[100,500][300,600]")), LOGIN_TREE]
    assert await ui.prepare_login(acct()) is True

    assert "ime enable com.android.adbkeyboard/.AdbIME" in adb.cmds
    assert "ime set com.android.adbkeyboard/.AdbIME" in adb.cmds
    assert "settings get secure default_input_method" in adb.cmds
    assert adb.cmds.count(f"am force-stop {QIDIAN}") == 1
    assert len([cmd for cmd in adb.cmds if cmd.startswith("am start -n ")]) == 2
    assert "input tap 200 550" in adb.taps
    assert adb.typed == []


async def test_ui_preparation_rejects_wrong_ime_readback(ui_rig):
    from tests.test_qidian_ui import LOGIN_TREE

    ui, adb = ui_rig
    adb.ime_default = "com.android.inputmethod/.Ime\n"
    adb.dumps = [LOGIN_TREE]
    with pytest.raises(RuntimeError):
        await ui.prepare_login_fn()({"id": "qd01", "channel": "qidian", "state": "starting"})
    assert adb.typed == []


async def test_login_after_preparation_does_not_force_stop_a_second_time(ui_rig):
    from tests.test_qidian_ui import LOGIN_TREE, MAIN_TREE, acct

    ui, adb = ui_rig
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, MAIN_TREE]
    assert await ui.prepare_login(acct()) is True
    outcome = await ui.login(acct(), "test-account", "test-only-secret")

    assert outcome.result == "running"
    assert adb.typed == ["test-account", "test-only-secret"]
    assert adb.cmds.count(f"am force-stop {QIDIAN}") == 1


async def test_login_rechecks_ime_after_waiting_for_password(ui_rig):
    from tests.test_qidian_ui import LOGIN_TREE, MAIN_TREE, acct

    ui, adb = ui_rig
    adb.dumps = [LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, LOGIN_TREE, MAIN_TREE]
    assert await ui.prepare_login(acct()) is True
    adb.ime_default = "com.android.inputmethod/.Ime\n"

    outcome = await ui.login(acct(), "test-account", "test-only-secret")

    assert adb.typed == [], "等待密码期间 IME 已变化,不得使用旧准备结果填入凭据"
    assert outcome.result is None
    assert adb.cmds.count(f"am force-stop {QIDIAN}") == 1
