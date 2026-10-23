"""Host/guest discovery remains scoped to the managed running distro."""
import json
from unittest.mock import AsyncMock

import pytest

from qtrade_winagent import __version__
from qtrade_winagent.netprobe import NetProbe
from qtrade_winagent.user import UserAgent


def user_agent(rig, monkeypatch, *, running=True, routes=None, addresses=None):
    monkeypatch.setattr(rig.wsl, "list_distros", AsyncMock(return_value=[
        {"name": "unrelated-distro", "running": True},
        {"name": rig.deps.cfg.wsl.distro, "running": running},
    ]))
    outputs = [(0, json.dumps(routes if routes is not None else [{"dev": "eth0", "gateway": "172.20.0.1"}]))]
    outputs += [(0, json.dumps(addresses if addresses is not None else [
        {"addr_info": [{"family": "inet", "local": "172.20.16.4"}]}]))]
    monkeypatch.setattr(rig.wsl, "exec", AsyncMock(side_effect=outputs))
    monkeypatch.setattr(rig.wsl, "exec_stdin", AsyncMock(return_value=(0, "")))
    ua = UserAgent(rig.deps.cfg, pipe=rig.pipe, wsl=rig.wsl, wechat=None, power=rig.power_backend,
                   session_id="Console", user_sid="S-1-5-21-1111-2222-3333-1001", version=__version__,
                   backup_dir="fixture", clock=rig.clock)
    return ua


async def test_guest_address_is_read_from_default_interface_not_windows_gateway(rig, monkeypatch):
    ua = user_agent(rig, monkeypatch)
    assert await ua._wsl_network({}) == {"guest_ip": "172.20.16.4"}
    calls = rig.wsl.exec.call_args_list
    assert all(call.args[0] == rig.deps.cfg.wsl.distro for call in calls)
    assert calls[1].args[1] == ["ip", "-j", "-4", "address", "show", "dev", "eth0", "scope", "global"]


async def test_stopped_managed_distro_is_not_started_for_discovery(rig, monkeypatch):
    ua = user_agent(rig, monkeypatch, running=False)
    assert await ua._wsl_network({}) == {"guest_ip": None}
    rig.wsl.exec.assert_not_awaited()


@pytest.mark.parametrize("routes,addresses", [
    ([], []),
    ([{"dev": "eth0"}, {"dev": "vpn0"}], []),
    ([{"dev": "eth0"}], [{"addr_info": [{"family": "inet", "local": "8.8.8.8"}]}]),
    ([{"dev": "eth0"}], [{"addr_info": [{"family": "inet", "local": "127.0.0.1"}]}]),
    ([{"dev": "eth0"}], [{"addr_info": [{"family": "inet", "local": "172.20.0.2"},
                                        {"family": "inet", "local": "172.20.0.3"}]}]),
])
async def test_ambiguous_missing_or_invalid_guest_data_has_no_guessed_address(rig, monkeypatch, routes, addresses):
    ua = user_agent(rig, monkeypatch, routes=routes, addresses=addresses)
    assert await ua._wsl_network({}) == {"guest_ip": None}


async def test_host_file_has_fixed_path_and_public_data_only_via_stdin(rig, monkeypatch):
    ua = user_agent(rig, monkeypatch)
    out = await ua._wsl_host_update({"host_ip": "172.20.0.1", "seq": 7, "wsl_subnet": "172.20.0.0/20",
                                     "path": "/etc/forbidden", "token": "DO_NOT_PERSIST", "command": "touch forbidden"})
    assert out == {"written": True}
    distro, argv, data = rig.wsl.exec_stdin.call_args.args
    assert distro == rig.deps.cfg.wsl.distro
    assert argv[:2] == ["/opt/qtrade/agent/venv/bin/python", "-c"]
    assert "/run/qtrade" in argv[2] and "host.json" in argv[2]
    assert "os.replace" in argv[2]
    body = json.loads(data)
    assert body["winagent_base_url"] == "http://172.20.0.1:17610"
    assert body["seq"] == 7
    assert set(body) == {"host_ip", "winagent_base_url", "wsl_subnet", "seq", "written_ms"}
    assert "DO_NOT_PERSIST" not in str((argv, data))
    assert "/etc/forbidden" not in str((argv, data))


async def test_host_file_write_works_when_guest_has_only_bundled_python(rig, monkeypatch):
    """Cold rootfs intentionally has no python3 executable on PATH."""
    ua = user_agent(rig, monkeypatch)
    monkeypatch.setattr(ua, "_clock", lambda: 123456789)
    written = []

    async def only_bundled_python(distro, argv, data):
        if argv[0] != "/opt/qtrade/agent/venv/bin/python":
            return 127, "executable not found"
        written.append((distro, json.loads(data)))
        return 0, ""

    rig.wsl.exec_stdin.side_effect = only_bundled_python
    assert await ua._wsl_host_update({"host_ip": "172.20.0.1", "seq": 9}) == {"written": True}
    rig.wsl.exec_stdin.assert_awaited_once()
    assert written == [(rig.deps.cfg.wsl.distro, {
        "host_ip": "172.20.0.1", "winagent_base_url": "http://172.20.0.1:17610",
        "seq": 9, "wsl_subnet": None, "written_ms": 123456789,
    })]


@pytest.mark.parametrize("exit_code", [1, 127])
async def test_failed_host_write_reports_not_written_without_fallback(rig, monkeypatch, exit_code):
    ua = user_agent(rig, monkeypatch)
    rig.wsl.exec_stdin.return_value = (exit_code, "guest write failed")
    assert await ua._wsl_host_update({"host_ip": "172.20.0.1"}) == {"written": False}
    rig.wsl.exec_stdin.assert_awaited_once()
    assert rig.wsl.exec_stdin.call_args.args[1][0] == "/opt/qtrade/agent/venv/bin/python"
    rig.wsl.exec.assert_not_awaited()


async def test_host_write_never_starts_a_stopped_managed_distro(rig, monkeypatch):
    ua = user_agent(rig, monkeypatch, running=False)
    assert await ua._wsl_host_update({"host_ip": "172.20.0.1"}) == {"written": False}
    rig.wsl.exec_stdin.assert_not_awaited()
    rig.wsl.exec.assert_not_awaited()


@pytest.mark.parametrize("host", [None, "", "8.8.8.8", "127.0.0.1", "$(touch forbidden)"])
async def test_invalid_host_data_does_not_invoke_guest_python(rig, monkeypatch, host):
    ua = user_agent(rig, monkeypatch)
    assert await ua._wsl_host_update({"host_ip": host}) == {"written": False}
    rig.wsl.exec_stdin.assert_not_awaited()


@pytest.mark.parametrize("guest", [None, "", "8.8.8.8", "127.0.0.1", "bad", "172.20.16.2"])
async def test_network_snapshot_separates_host_listener_from_guest_agent(rig, guest):
    d = rig.deps
    provider = AsyncMock(return_value=guest)
    np = NetProbe(rig.db, rig.net, rig.firewall, rig.probe, d.cfg.net, d.cfg.probe, d.alerts,
                  svc_exe="fixture", guest_ip_provider=provider, clock=rig.clock)
    state = await np.refresh()
    assert state.host_ip == rig.net.wsl_adapter().ipv4
    assert state.agent_base_url == ("http://172.20.16.2:17600" if guest == "172.20.16.2" else None)
    assert np.host_json()["winagent_base_url"] == f"http://{state.host_ip}:17610"
