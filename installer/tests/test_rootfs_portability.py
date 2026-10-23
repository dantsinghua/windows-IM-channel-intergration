"""Execute shipped shell logic with every machine path rebased to a fixture root."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

FILES = Path(__file__).resolve().parents[1] / "rootfs/files"


def run_docker_config(tmp_path, payload, *, script_text=None, with_pinned_python=True):
    """Only literal filesystem roots are substituted; shell logic is unchanged."""
    env_dir = tmp_path / "etc/qtrade"
    env_dir.mkdir(parents=True)
    if payload is not None:
        (env_dir / "install.env").write_text(payload)
    source = Path(os.environ.get("QTRADE_DOCKER_CONFIG_SCRIPT", FILES / "opt/qtrade/bin/qtrade-docker-config.sh"))
    text = source.read_text() if script_text is None else script_text
    for old in ("/mnt/c/ProgramData/", "/etc/", "/var/", "/opt/"):
        text = text.replace(old, str(tmp_path) + old)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The cold image has no system python3. A poisoned PATH catches accidental
    # host-interpreter fallback even when this test host happens to have Python.
    (bin_dir / "python3").write_text(
        "#!/bin/sh\nprintf '%s\\n' forbidden-system-python3 >&2\nexit 93\n",
    )
    (bin_dir / "python3").chmod(0o755)
    if with_pinned_python:
        pinned = tmp_path / "opt/qtrade/agent/venv/bin/python"
        pinned.parent.mkdir(parents=True)
        invocation = tmp_path / "pinned-python.invoked"
        pinned.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' invoked >> {shlex.quote(str(invocation))}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        )
        pinned.chmod(0o755)
    (bin_dir / "logger").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "logger").chmod(0o755)
    # Keep the existing shell-injection check meaningful with the restricted PATH.
    (bin_dir / "touch").symlink_to("/usr/bin/touch")
    script = tmp_path / "docker-config.sh"
    script.write_text(text)
    env = {"PATH": str(bin_dir), "LANG": "C"}
    result = subprocess.run(["/bin/bash", str(script)], text=True, capture_output=True, env=env, timeout=10)
    return result, tmp_path / "etc/docker/daemon.json", env_dir / ".docker-configured"


def test_docker_pool_uses_target_machine_selected_canonical_keys(tmp_path):
    r, daemon, marker = run_docker_config(
        tmp_path, "QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\nQTRADE_PACKAGE_VERSION=1.0.2\n",
    )
    assert r.returncode == 0, r.stderr
    cfg = json.loads(daemon.read_text())
    assert cfg["default-address-pools"] == [{"base": "10.244.0.0/16", "size": 24}]
    assert cfg["bip"] == "10.244.0.1/24"
    assert marker.exists()
    assert (tmp_path / "pinned-python.invoked").read_text().splitlines() == ["invoked"]


def test_missing_bundled_python_does_not_fall_back_to_path_or_mark_success(tmp_path):
    r, daemon, marker = run_docker_config(
        tmp_path, "QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\n",
        with_pinned_python=False,
    )
    assert r.returncode != 0
    assert "forbidden-system-python3" not in r.stderr
    assert not daemon.exists()
    assert not marker.exists()
    assert not (tmp_path / "pinned-python.invoked").exists()


@pytest.mark.parametrize("payload", [
    None,
    "",
    "docker_cidr=10.244.0.0/16\ndocker_bip=10.244.0.1/24\n",
    "QTRADE_DOCKER_POOL_BASE=999.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\n",
    "QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.243.0.1/24\n",
])
def test_missing_or_invalid_network_selection_never_falls_back(tmp_path, payload):
    r, daemon, marker = run_docker_config(tmp_path, payload)
    assert r.returncode != 0, "must not invent a network pool or report successful first boot"
    assert not daemon.exists()
    assert not marker.exists()


def test_install_env_is_data_not_a_root_shell_program(tmp_path):
    injected = tmp_path / "injected"
    payload = ("QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\n"
               f"UNRELATED=$(touch {injected})\n")
    r, daemon, _ = run_docker_config(tmp_path, payload)
    assert r.returncode == 0, r.stderr
    assert not injected.exists()
    assert json.loads(daemon.read_text())["bip"] == "10.244.0.1/24"


def test_existing_docker_pool_is_not_silently_replaced(tmp_path):
    daemon = tmp_path / "etc/docker/daemon.json"
    daemon.parent.mkdir(parents=True)
    original = '{"default-address-pools":[{"base":"10.99.0.0/16","size":24}],"bip":"10.99.0.1/24"}\n'
    daemon.write_text(original)
    r, _, marker = run_docker_config(tmp_path,
        "QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\n")
    assert r.returncode != 0
    assert daemon.read_text() == original
    assert not marker.exists()


def test_duplicate_network_keys_are_rejected_instead_of_last_value_wins(tmp_path):
    r, daemon, marker = run_docker_config(tmp_path,
        "QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_POOL_BASE=10.245.0.0/16\n"
        "QTRADE_DOCKER_BIP=10.245.0.1/24\n")
    assert r.returncode != 0
    assert not daemon.exists()
    assert not marker.exists()
