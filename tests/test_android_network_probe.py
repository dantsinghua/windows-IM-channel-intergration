"""Android 自身 DNS/TCP 探测：全假 ADB，不访问宿主或真实设备网络。"""
from __future__ import annotations

import asyncio
import json
import shlex
import socket

import pytest

from qtrade_agent.runtime.backends import AdbCliBackend, AdbShellResult, FakeAdb
from tests.test_qidian_apk_preparation import SERIAL, ScriptedProcess


HOST = "msfxg.3g.qq.com"
TARGET = f"{HOST}:8080"
RAW_SECRET = "test-only-secret-never-in-evidence"


@pytest.fixture
def probe(monkeypatch):
    from qtrade_agent.runtime.network import probe_qidian_network

    def no_host_network(*args, **kwargs):
        pytest.fail("企点初始化不得借用宿主 DNS/TCP 的成功结论")

    monkeypatch.setattr(socket, "getaddrinfo", no_host_network)
    monkeypatch.setattr(socket, "create_connection", no_host_network)
    return probe_qidian_network


async def test_android_dns_success_with_all_icmp_packets_lost_can_still_connect(probe):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "dns")] = [AdbShellResult(
        1, f"PING {HOST} (192.0.2.10) 56(84) bytes of data.\n100% packet loss\n")]

    report = await probe(adb, SERIAL, [TARGET])

    assert report["ok"] is True
    assert (report["result"], report["side"]) == ("OK", "android")
    assert report["targets"][0]["stage"] == "tcp"
    assert report["targets"][0]["address"] == "192.0.2.10"
    assert all(serial == SERIAL for serial, _, _ in adb.shell_result_calls)
    assert not any("google" in cmd or "dumpsys connectivity" in cmd for _, cmd, _ in adb.shell_result_calls)


async def test_explicit_android_dns_failure_stops_before_tcp(probe):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "dns")] = [AdbShellResult(1, f"ping: unknown host {HOST}")]

    report = await probe(adb, SERIAL, [TARGET])

    assert report["ok"] is False
    assert (report["targets"][0]["stage"], report["targets"][0]["result"]) == ("dns", "DNS_FAIL")
    assert len(adb.shell_result_calls) == 1


@pytest.mark.parametrize("reply", [AdbShellResult(0, ""), AdbShellResult(1, "100% packet loss")])
async def test_unrecognized_dns_output_is_skipped_without_guessing_a_network_failure(probe, reply):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "dns")] = [reply]

    report = await probe(adb, SERIAL, [TARGET])

    assert report["ok"] is False
    assert (report["targets"][0]["stage"], report["targets"][0]["result"]) == ("dns", "SKIPPED")
    assert len(adb.shell_result_calls) == 1


@pytest.mark.parametrize("reply, expected", [
    (AdbShellResult(1, "nc: connect: Connection refused"), "TCP_REFUSED"),
    (AdbShellResult(1, "nc: connect: Connection timed out"), "TCP_TIMEOUT"),
    (TimeoutError("bounded connect timed out"), "TCP_TIMEOUT"),
], ids=["refused", "timed-out-exit", "timed-out-exception"])
async def test_tcp_failures_keep_the_observed_failure_kind(probe, reply, expected):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "tcp")] = [reply]

    report = await probe(adb, SERIAL, [TARGET])

    assert report["ok"] is False
    assert (report["targets"][0]["stage"], report["targets"][0]["result"]) == ("tcp", expected)


async def test_any_one_configured_msf_port_is_sufficient(probe):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "tcp")] = [
        AdbShellResult(1, "Connection refused"),
        AdbShellResult(0, ""),
        AdbShellResult(1, "Connection timed out"),
    ]

    report = await probe(adb, SERIAL, [f"{HOST}:{port}" for port in (8080, 14000, 443)])

    assert report["ok"] is True
    assert report["result"] == "OK"
    assert {target["result"] for target in report["targets"]} == {"TCP_REFUSED", "OK", "TCP_TIMEOUT"}


async def test_all_targets_begin_without_waiting_for_the_first_dns_probe(probe):
    class ParallelAdb(FakeAdb):
        def __init__(self):
            super().__init__()
            self.count = 0
            self.all_started = asyncio.Event()
            self.release = asyncio.Event()

        async def shell_result(self, serial, cmd, *, timeout_s):
            if "ping" in shlex.split(cmd):
                self.count += 1
                if self.count == 3:
                    self.all_started.set()
                await self.release.wait()
            return await super().shell_result(serial, cmd, timeout_s=timeout_s)

    adb = ParallelAdb()
    task = asyncio.create_task(probe(adb, SERIAL, [f"{HOST}:{port}" for port in (8080, 14000, 443)]))
    try:
        await asyncio.wait_for(adb.all_started.wait(), timeout=0.5)
        adb.release.set()
        assert (await task)["ok"] is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("code", [2, 127])
async def test_ping_execution_failure_is_not_success_even_with_a_header(probe, code):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "dns")] = [AdbShellResult(code, f"PING {HOST} (192.0.2.10)\n")]

    report = await probe(adb, SERIAL, [TARGET])

    assert report["ok"] is False
    assert report["targets"][0]["result"] == "SKIPPED"
    assert len(adb.shell_result_calls) == 1


async def test_default_stage_budgets_match_the_documented_three_and_five_seconds(probe):
    adb = FakeAdb()
    await probe(adb, SERIAL, [TARGET])
    assert [limit for _, _, limit in adb.shell_result_calls] == [3, 5]


async def test_bad_target_does_not_suppress_an_independent_good_target(probe):
    adb = FakeAdb()
    report = await probe(adb, SERIAL, ["invalid target", TARGET])
    assert report["ok"] is True
    assert {r["result"] for r in report["targets"]} == {"OK", "SKIPPED"}


async def test_empty_target_configuration_is_never_reported_as_online(probe):
    adb = FakeAdb()
    report = await probe(adb, SERIAL, [])
    assert report["ok"] is False and report["result"] == "SKIPPED"
    assert not adb.shell_result_calls


def test_config_declares_default_targets_and_loads_custom_probe_hosts():
    from qtrade_agent.config import AgentConfig
    assert AgentConfig().probe.qidian_hosts == tuple(f"{HOST}:{port}" for port in (8080, 14000, 443))
    custom = AgentConfig.from_toml_dict({"probe": {"qidian_hosts": ["192.0.2.20:443"]}})
    assert custom.probe.qidian_hosts == ("192.0.2.20:443",)
    assert custom.probe.agent_probe_enabled is False


async def test_toybox_nc_does_not_use_unsupported_zero_timeout_or_z(probe):
    adb = FakeAdb()
    await probe(adb, SERIAL, [TARGET])

    command = next(cmd for _, cmd, _ in adb.shell_result_calls if "nc" in shlex.split(cmd))
    args = shlex.split(command)
    assert "-z" not in args
    assert "-q" in args and float(args[args.index("-q") + 1]) >= 1
    assert "192.0.2.10" in args, "TCP 必须使用刚从 Android 解析出的地址"


@pytest.mark.parametrize("stage", ["dns", "tcp"])
async def test_stage_timeout_cancels_the_pending_adb_operation(probe, stage):
    adb = FakeAdb()
    adb.shell_result_gates[(SERIAL, stage)] = asyncio.Event()

    report = await asyncio.wait_for(probe(
        adb, SERIAL, [TARGET], dns_timeout_s=0.02, tcp_timeout_s=0.02,
        target_timeout_s=0.15, total_timeout_s=0.2), timeout=1)

    assert report["ok"] is False
    assert report["targets"][0]["result"] == ("DNS_FAIL" if stage == "dns" else "TCP_TIMEOUT")
    assert (SERIAL, stage) in adb.cancelled_shell_results
    assert not adb.active_shell_results
    assert all(0 < limit <= 0.02 for _, _, limit in adb.shell_result_calls)


@pytest.mark.parametrize("target_limit,total_limit", [(0.02, 0.2), (0.2, 0.02)], ids=["per-target", "whole-round"])
async def test_total_budgets_bound_work_even_when_backend_does_not_self_timeout(probe, target_limit, total_limit):
    adb = FakeAdb()
    adb.shell_result_gates[(SERIAL, "dns")] = asyncio.Event()

    report = await asyncio.wait_for(probe(
        adb, SERIAL, [TARGET], dns_timeout_s=5, tcp_timeout_s=5,
        target_timeout_s=target_limit, total_timeout_s=total_limit), timeout=1)

    assert report["ok"] is False
    assert not adb.active_shell_results
    assert (SERIAL, "dns") in adb.cancelled_shell_results


async def test_cancelling_the_probe_propagates_and_releases_all_pending_targets(probe):
    adb = FakeAdb()
    adb.shell_result_gates[(SERIAL, "dns")] = asyncio.Event()
    task = asyncio.create_task(probe(adb, SERIAL, [TARGET, f"{HOST}:443"]))
    try:
        await asyncio.wait_for(adb.shell_result_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not adb.active_shell_results
        assert adb.cancelled_shell_results
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("reply", [
    AdbShellResult(1, f"unknown host {RAW_SECRET}; adb shell ping -c 1"),
    RuntimeError(f"adb transport error {RAW_SECRET}"),
])
async def test_raw_shell_output_and_exception_text_are_not_returned(probe, reply):
    adb = FakeAdb()
    adb.shell_result_results[(SERIAL, "dns")] = [reply]

    report = await probe(adb, SERIAL, [TARGET])

    payload = json.dumps(report)
    assert RAW_SECRET not in payload
    assert "adb shell" not in payload
    assert "output" not in report["targets"][0]
    if isinstance(reply, RuntimeError):
        assert report["targets"][0]["result"] == "SKIPPED", "探测器异常不能冒充网络超时"


@pytest.mark.parametrize("host", ["bad target:8080", "example.test;echo:443", "example.test:70000"])
async def test_invalid_targets_are_skipped_without_running_shell(probe, host):
    adb = FakeAdb()

    report = await probe(adb, SERIAL, [host])

    assert report["ok"] is False
    assert report["targets"][0]["result"] == "SKIPPED"
    assert not adb.shell_result_calls


async def test_cli_shell_result_preserves_remote_exit_status_without_real_process(monkeypatch):
    from qtrade_agent.runtime import backends
    calls = []
    process = ScriptedProcess((7, "test-output"))

    async def fake_process(*args, **kwargs):
        calls.append(args)
        return process

    monkeypatch.setattr(backends.asyncio, "create_subprocess_exec", fake_process)
    result = await AdbCliBackend(adb_bin="test-only-adb").shell_result(SERIAL, "toybox nc", timeout_s=0.5)

    assert (result.returncode, result.output) == (7, "test-output")
    assert calls == [("test-only-adb", "-P", "16000", "-s", SERIAL, "shell", "toybox nc")]
    assert process.waited


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_cli_shell_cancellation_and_timeout_reap_only_its_process(monkeypatch, stop):
    from qtrade_agent.runtime import backends
    process = ScriptedProcess((0, ""), blocked=True)
    started = asyncio.Event()

    async def fake_process(*args, **kwargs):
        started.set()
        return process

    monkeypatch.setattr(backends.asyncio, "create_subprocess_exec", fake_process)
    task = asyncio.create_task(AdbCliBackend(adb_bin="test-only-adb").shell_result(
        SERIAL, "toybox nc", timeout_s=0.02 if stop == "timeout" else 5))
    await asyncio.wait_for(started.wait(), timeout=1)
    if stop == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if stop == "cancel" else TimeoutError):
        await asyncio.wait_for(task, timeout=1)
    assert process.terminated or process.killed
    assert process.waited and process.returncode is not None
