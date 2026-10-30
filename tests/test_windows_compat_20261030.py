"""Independent developer regressions for the Windows compatibility audit."""
import asyncio
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, WinAgentConfig
from qtrade_agent.ossbox.errors import OssboxSettingsError
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.scheduler import Scheduler
from qtrade_agent import winagent_client

REPO = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("QT_COMPAT_SOURCE", REPO))


@pytest.fixture
def lifecycle(tmp_path):
    now = [1_000_000]
    app = AgentApp(AgentConfig(), db_path=":memory:", clock=lambda: now[0], data_dir=str(tmp_path))
    app.scheduler = Scheduler(clock=app.clock)
    app.xunjia_v22_host = None
    app.collector_reporting_settings = SimpleNamespace(state="pending")
    app.bus = SimpleNamespace(close=AsyncMock())
    app.webhooks = SimpleNamespace(stop=AsyncMock())
    app.workflows = SimpleNamespace(tasks={})
    return app, now


@pytest.mark.parametrize("error", [OssboxSettingsError("vault_unavailable", unavailable=True), OSError("fixture"), TimeoutError("fixture")])
async def test_boot_retry_recovers_after_transient(lifecycle, monkeypatch, error):
    app, now = lifecycle
    host = Mock(spec=InstitutionCollectorHost)
    host.start = AsyncMock()
    host.stop = AsyncMock()
    create = AsyncMock(side_effect=[error, host])
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", create)
    try:
        await app.start(recover=False)
        assert app._started and app.xunjia_v22_host is None
        assert hasattr(app, "_collector_recovery_tick"), "BOOT-RETRY: no dependency recovery callback"
        now[0] += 4999
        await app._collector_recovery_tick()
        assert create.await_count == 1
        now[0] += 1
        await app._collector_recovery_tick()
        assert app.xunjia_v22_host is host
        host.start.assert_awaited_once()
        for _ in range(3):
            now[0] += 60000
            await app._collector_recovery_tick()
            await app.start(recover=False)
        assert create.await_count == 2
        host.start.assert_awaited_once()
    finally:
        await app.stop()


async def test_boot_retry_backoff_is_bounded_and_not_busy_loop(lifecycle, monkeypatch):
    app, now = lifecycle
    create = AsyncMock(side_effect=OSError("fixture"))
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", create)
    try:
        await app.start(recover=False)
        assert hasattr(app, "_collector_recovery_tick"), "BOOT-RETRY: no recovery callback"
        count = 1
        for delay in [5, 10, 20, 40, 60, 60]:
            now[0] += delay * 1000 - 1
            await app._collector_recovery_tick()
            assert create.await_count == count
            now[0] += 1
            await app._collector_recovery_tick()
            count += 1
            assert create.await_count == count
        await app.stop()
        now[0] += 600000
        await app._collector_recovery_tick()
        assert create.await_count == count
    finally:
        await app.stop()


async def test_boot_retry_permanent_configuration_error_is_not_retried(lifecycle, monkeypatch):
    app, now = lifecycle
    create = AsyncMock(side_effect=OssboxSettingsError("invalid_gateway_url"))
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", create)
    try:
        await app.start(recover=False)
        assert hasattr(app, "_collector_recovery_tick"), "BOOT-RETRY: no recovery callback"
        for _ in range(5):
            now[0] += 600000
            await app._collector_recovery_tick()
        create.assert_awaited_once()
    finally:
        await app.stop()


async def test_boot_retry_scheduler_coalesces_concurrent_requests(lifecycle, monkeypatch):
    app, now = lifecycle
    entered, release = asyncio.Event(), asyncio.Event()
    host = Mock(spec=InstitutionCollectorHost)
    host.start, host.stop = AsyncMock(), AsyncMock()
    attempts = 0
    async def construct(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary fixture failure")
        entered.set()
        await release.wait()
        return host
    monkeypatch.setattr(InstitutionCollectorHost, "from_settings", construct)
    first = None
    try:
        await app.start(recover=False)
        assert hasattr(app, "_collector_recovery_tick")
        app.scheduler.register("fixture-recovery", 5, app._collector_recovery_tick)
        now[0] += 5000
        first = asyncio.create_task(app.scheduler.run_once("fixture-recovery"))
        await asyncio.wait_for(entered.wait(), 1)
        assert await app.scheduler.run_once("fixture-recovery") is False
        release.set()
        assert await first is True
        assert attempts == 2
        host.start.assert_awaited_once()
    finally:
        release.set()
        if first is not None:
            await first
        await app.stop()


def test_net_mirrored_uses_confirmed_loopback_not_lan_dns(tmp_path, monkeypatch):
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.255.255.254\n")
    monkeypatch.setattr(winagent_client, "wsl_networking_mode", lambda: "mirrored", raising=False)
    cfg = WinAgentConfig(host_ip_hint_file=str(tmp_path / "absent.json"))
    result = winagent_client.base_url_candidates(cfg, resolv_conf=str(resolv), gateway=lambda: "192.168.50.1")
    assert [url for _, url in result] == ["http://127.0.0.1:17610"]


def test_net_explicit_endpoint_not_overridden_by_mirrored(tmp_path, monkeypatch):
    monkeypatch.setattr(winagent_client, "wsl_networking_mode", lambda: "mirrored", raising=False)
    cfg = WinAgentConfig(url="http://172.23.1.1:17610")
    assert winagent_client.base_url_candidates(cfg) == [("url", cfg.url)]


def test_net_production_mode_probe_uses_official_wslinfo_path(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="mirrored\n"))
    monkeypatch.setattr(winagent_client.subprocess, "run", run)
    assert winagent_client.wsl_networking_mode() == "mirrored"
    assert run.call_args.args[0] == ["/usr/bin/wslinfo", "--networking-mode"]
    assert run.call_args.kwargs.get("timeout", 0) > 0
    assert run.call_args.kwargs.get("shell", False) is False


@pytest.mark.parametrize("artifact", ["requirements.in", "requirements.lock", "wheels.expected"])
def test_package_pyjks_present_in_offline_closure(artifact):
    text = (SOURCE / "installer/rootfs" / artifact).read_text()
    assert re.search(r"(?m)^pyjks(?:==20\.0\.0|\s+20\.0\.0\s)", text), f"PYJKS missing from {artifact}"


def test_package_shared_runtime_is_declared_and_vendored():
    project = tomllib.loads((SOURCE / "pyproject.toml").read_text())["project"]
    deps = {canonicalize_name(Requirement(dep).name): Requirement(dep) for dep in project["dependencies"]}
    assert "xunjia-collector-runtime" in deps, "SHARED runtime is absent from wheel dependency metadata"
    assert "2.2.12" in deps["xunjia-collector-runtime"].specifier
    assert (SOURCE / "vendor/xunjia_collector_runtime-2.2.12-py3-none-any.whl").is_file()


def test_package_clean_wheels_import_shared_runtime(tmp_path):
    """Small source-only wheel build; isolated python -S ignores all editable .pth files."""
    work = tmp_path / "source"
    work.mkdir()
    shutil.copy2(SOURCE / "pyproject.toml", work / "pyproject.toml")
    shutil.copytree(SOURCE / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    wheels = tmp_path / "wheels"
    build = subprocess.run(["/usr/bin/python3", "-m", "pip", "wheel", str(work), "--no-build-isolation", "--no-deps", "-w", str(wheels), "-q"],
                           cwd=tmp_path, capture_output=True, text=True, timeout=90,
                           env={**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    assert build.returncode == 0, build.stderr[-2000:]
    installed = tmp_path / "installed"
    for wheel in [*wheels.glob("*.whl"), *SOURCE.glob("vendor/xunjia_collector_runtime-*.whl")]:
        with zipfile.ZipFile(wheel) as z:
            z.extractall(installed)
    sites = [p for p in sys.path if p.endswith("site-packages")]
    script = "import sys; sys.path[:0] = " + repr([str(installed), *sites]) + "\n" + '''
import asyncio, importlib
from pathlib import Path
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
for name in ('relay.file_gateway', 'relay.gateway_paths', 'relay.collector_bridge',
             'relay.qtrade_native_port', 'server.receiver_ledger', 'server.institution_transport'):
    module = importlib.import_module(name)
    assert Path(module.__file__).is_relative_to(Path(sys.path[0])), 'shared runtime escaped isolated wheels: ' + name
class ExpectedBoundary(Exception): pass
class Settings:
    async def _prepare(self, unused): raise ExpectedBoundary()
try:
    asyncio.run(InstitutionCollectorHost.from_settings(Settings()))
except ExpectedBoundary:
    print('SHARED_IMPORT_AND_SETTINGS_BOUNDARY_OK')
else:
    raise AssertionError('settings boundary not reached')
'''
    result = subprocess.run([sys.executable, "-S", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "SHARED_IMPORT_AND_SETTINGS_BOUNDARY_OK" in result.stdout


def test_inst01_verify_kernel_mode_dispatched_before_baseline_static():
    """Wiring only: does not claim execution of Inno or Windows reboot recovery."""
    text = (SOURCE / "installer/engine/qtrade-setup-engine.iss").read_text(encoding="utf-8-sig")
    body = text.split("procedure CurStepChanged", 1)[1]
    before_precheck = body.split("RunStep(ST_PRECHECK)", 1)[0]
    assert re.search(r"CompareText\(OptMode,\s*'verify-kernel'\)", before_precheck, re.I), "INST-01: verify-kernel mode falls through to ordinary baseline"
    assert "resume_step" in body, "INST-01: trusted resume_step is never consumed"


def test_inst01_resume_six_skips_prior_blocks_without_skipping_distro_static():
    """Lexical block ownership catches valid Pascal with incorrect nesting; no EXE execution."""
    text = (SOURCE / "installer/engine/qtrade-setup-engine.iss").read_text(encoding="utf-8-sig")
    body = text.split("procedure CurStepChanged", 1)[1]
    body = re.sub(r"//[^\n]*", "", body)
    tokens = list(re.finditer(r"'(?:''|[^'])*'|\b[a-zA-Z_]\w*\b|<=|\d+|[^\s]", body))
    blocks = []
    for match in re.finditer(r"if\s+ResumeIndex\s*<=\s*(\d+)\s+then\s+begin\b", body, re.I):
        depth = 1
        end = None
        for token in tokens:
            if token.start() < match.end():
                continue
            word = token.group().lower()
            if word == "begin": depth += 1
            elif word == "end":
                depth -= 1
                if depth == 0:
                    end = token.end()
                    break
        assert end is not None, "unclosed resume stage block"
        blocks.append((match.start(), end, int(match[1])))
    assert blocks, "INST-01: no resume stage gates"
    expected = {"ST_WSL_FEATURE": 1, "ST_WSL_MSI": 2, "ST_KERNEL_STAGED": 3,
                "'KERNEL_SWITCH'": 4, "ST_DISTRO_IMPORTED": 6}
    for step, limit in expected.items():
        calls = list(re.finditer(r"RunStep\(" + re.escape(step) + r"\)", body))
        assert len(calls) == 1, step
        enclosing = [maximum for start, end, maximum in blocks if start < calls[0].start() < end]
        assert enclosing == [limit], f"{step} is nested under resume limits {enclosing}, expected only {limit}"
        assert all(6 <= maximum for maximum in enclosing) == (step == "ST_DISTRO_IMPORTED")
    early = body.split("RunStep(ST_PRECHECK)", 1)[0]
    assert re.search(r"CompareText\(OptMode,\s*'verify-kernel'\)\s*=\s*0", early)
    assert re.search(r"CompareText\(OptMode,\s*'resume'\)\s*=\s*0", early)
    assert "ResumeStep := ST_DISTRO_IMPORTED" in early


async def test_boot_retry_is_registered_with_scheduler_and_cancelled_on_stop(tmp_path):
    from tests.test_integration_wiring_common import make_agent
    app, _ = make_agent(tmp_path)
    try:
        job = app.scheduler.jobs.get("collector_reporting_recovery")
        assert job is not None, "BOOT-RETRY: recovery callback never registered in actual open()"
        assert job.interval_s == 5
        assert job.fn == app._collector_recovery_tick
        # Only the recovery job runs: all other app jobs remain unstarted.
        scheduler = Scheduler(clock=app.clock)
        scheduler.jobs[job.name] = job
        app.scheduler = scheduler
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def pending():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        job.fn = pending
        await scheduler.start()
        app._started = True
        scheduler.trigger(job.name)
        await asyncio.wait_for(started.wait(), 1)
        await app.stop()
        assert cancelled.is_set()
        assert job._runner is None and job._task is None
    finally:
        if app.store._con is not None:
            await app.stop()
