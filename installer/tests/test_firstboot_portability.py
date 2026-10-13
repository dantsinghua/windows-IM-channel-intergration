"""Real firstboot shell execution against a synthetic filesystem and fake commands."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "rootfs/files/opt/qtrade/bin/qtrade-firstboot.sh"


class Firstboot:
    def __init__(self, root):
        self.root = root
        self.config = root / "etc/qtrade"
        self.config.mkdir(parents=True)
        images = root / "var/lib/qtrade/images"
        images.mkdir(parents=True)
        self.calls = root / "commands.log"
        self.contents = {}
        for key, ref in [("redroid_image", "fixture.registry/redroid:portable"),
                         ("napcat_image", "fixture.registry/napcat:portable")]:
            asset = images / (key + ".tar")
            asset.write_bytes(key.encode())
            self.contents[key] = {"tar": str(asset), "ref": ref, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}
        apk = root / "opt/qtrade/apk/Qidian.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b"fixture apk, never installed")
        self.contents["qidian_apk"] = {"path": str(apk), "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(), "version": "6.9.7"}
        (self.config / "rootfs-version").write_text("rootfs-1.0.2\n")
        (self.config / "install.env").write_text("QTRADE_PACKAGE_VERSION=1.0.2\nQTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\n")
        (self.config / "winagent.token").write_text("fixture-winagent-token\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        for command in ("docker", "systemctl", "logger"):
            path = self.bin / command
            path.write_text('#!/bin/sh\nprintf "%s %s\\n" "' + command + '" "$*" >> "$TEST_COMMAND_LOG"\nexit 0\n')
            path.chmod(0o755)
        python = root / "opt/qtrade/agent/venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nprintf "agent-python %s\\n" "$*" >> "$TEST_COMMAND_LOG"\n'
                          'case " $* " in *" --bootstrap-runtime-contents "*) exec "$TEST_PYTHON" "$@";; esac\nexit 0\n')
        python.chmod(0o755)

    def run(self):
        (self.config / "contents.json").write_text(json.dumps(self.contents))
        text = SCRIPT.read_text()
        for old in ("/mnt/c/ProgramData/", "/etc/", "/var/", "/opt/"):
            text = text.replace(old, str(self.root) + old)
        script = self.root / "firstboot.sh"
        script.write_text(text)
        env = {"PATH": str(self.bin) + ":/usr/bin:/bin", "LANG": "C", "TEST_COMMAND_LOG": str(self.calls), "TEST_PYTHON": sys.executable,
               "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=10)
        return result, self.calls.read_text() if self.calls.exists() else ""


def test_firstboot_uses_bundled_refs_and_apk_then_records_real_package_version(tmp_path):
    fixture = Firstboot(tmp_path)
    result, calls = fixture.run()
    assert result.returncode == 0, result.stderr
    runtime = tomllib.loads((fixture.config / "agent.toml").read_text())["runtime"]
    assert runtime["redroid_image"] == fixture.contents["redroid_image"]["ref"]
    assert runtime["napcat_image"] == fixture.contents["napcat_image"]["ref"]
    assert runtime["apk_url"] == Path(fixture.contents["qidian_apk"]["path"]).as_uri()
    assert runtime["apk_sha256"] == fixture.contents["qidian_apk"]["sha256"]
    assert json.loads((fixture.config / ".imported").read_text())["package_version"] == "1.0.2"
    assert "docker pull" not in calls
    assert "docker image inspect fixture.registry/redroid:portable" in calls
    assert "docker image inspect fixture.registry/napcat:portable" in calls
    assert calls.index("agent-python") < calls.index("systemctl enable --now qtrade-agent")


def test_missing_install_env_cannot_mark_firstboot_done_or_start_agent(tmp_path):
    fixture = Firstboot(tmp_path)
    (fixture.config / "install.env").unlink()
    result, calls = fixture.run()
    assert result.returncode != 0
    assert not (fixture.config / ".imported").exists()
    assert "systemctl enable --now qtrade-agent" not in calls


@pytest.mark.parametrize("key", ["redroid_image", "napcat_image", "qidian_apk"])
@pytest.mark.parametrize("sha", ["", "0" * 64])
def test_missing_or_wrong_bundled_hash_refuses_completion_and_agent_start(tmp_path, key, sha):
    fixture = Firstboot(tmp_path)
    fixture.contents[key]["sha256"] = sha
    result, calls = fixture.run()
    assert result.returncode != 0
    assert not (fixture.config / ".imported").exists()
    assert "systemctl enable --now qtrade-agent" not in calls
    assert "docker pull" not in calls
