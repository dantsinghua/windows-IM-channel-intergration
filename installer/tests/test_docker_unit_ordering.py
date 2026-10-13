"""Verify offline boot transactions, including systemd's implicit dependencies.

No service, container, or WSL instance is started. With QTRADE_ROOTFS_ARCHIVE,
the actual archive's unit graph is checked without replacing its QTrade units.
Otherwise the host's vendor units and the current shipped QTrade units are used.
Executable placeholders only satisfy verify's existence checks; generators and
man-page subprocesses are disabled. This does not prove runtime daemon health.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile

import pytest


UNITS = Path(__file__).resolve().parents[1] / "rootfs/files/systemd"
CONFIG = "qtrade-docker-config.service"
BAD_TRANSACTION = re.compile(r"ordering cycle|(?:job .*deleted|deleting job)", re.I)


def _archive_units(archive_path, root):
    """Copy only unit files; never extract rootfs executables or runtime data."""
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if not name.startswith(("etc/systemd/system/", "usr/lib/systemd/system/",
                                    "lib/systemd/system/")):
                continue
            assert ".." not in PurePosixPath(name).parts
            if name.startswith("lib/"):
                name = "usr/" + name
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            assert not path.parent.is_symlink()
            if member.isdir():
                path.mkdir(exist_ok=True)
            elif member.isfile():
                assert not path.is_symlink()
                path.write_bytes(archive.extractfile(member).read())
            elif member.issym():
                path.symlink_to(member.linkname)
            else:
                pytest.fail(f"unsupported archived unit type: {name}")


def _placeholder_executables(root):
    for folder in (root / "etc/systemd/system", root / "usr/lib/systemd/system"):
        for unit in folder.rglob("*"):
            if unit.is_symlink() or not unit.is_file():
                continue
            for executable in re.findall(r"^Exec\w+=[-+!:@]*(/\S+)",
                                         unit.read_text(), re.MULTILINE):
                if "%" in executable:
                    continue
                path = root / executable.lstrip("/")
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("#!/bin/sh\nexit 99\n")
                    path.chmod(0o755)


@pytest.fixture(scope="module")
def unit_graph(tmp_path_factory):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is required for offline boot transaction tests")
    root = tmp_path_factory.mktemp("docker-unit-graph")
    archive_path = os.environ.get("QTRADE_ROOTFS_ARCHIVE")
    if archive_path:
        assert Path(archive_path).is_file(), "explicit rootfs artifact does not exist"
        _archive_units(archive_path, root)
    else:
        vendor = Path("/usr/lib/systemd/system")
        if not (vendor / "docker.service").is_file():
            pytest.skip("Docker vendor units absent; supply QTRADE_ROOTFS_ARCHIVE")
        shutil.copytree(vendor, root / "usr/lib/systemd/system", symlinks=True)
        shutil.copytree(UNITS, root / "etc/systemd/system", symlinks=True)
        # Reproduce the image's enabled services plus docker.socket activation.
        for name in (CONFIG, "qtrade-firstboot.service", "qtrade-container-janitor.service",
                     "docker.service", "docker.socket"):
            base = "/etc/systemd/system" if name.startswith("qtrade-") else "/lib/systemd/system"
            target = "sockets.target" if name.endswith(".socket") else "multi-user.target"
            link = root / f"etc/systemd/system/{target}.wants/{name}"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(f"{base}/{name}")
    (root / "lib").symlink_to("usr/lib")
    _placeholder_executables(root)
    return root


@pytest.fixture
def graph(unit_graph, tmp_path):
    root = tmp_path / "root"
    shutil.copytree(unit_graph, root, symlinks=True)
    return root


def _verify(root, target, request):
    env = dict(os.environ, LC_ALL="C", SYSTEMD_LOG_LEVEL="debug", SYSTEMD_COLORS="0")
    # --root must own all unit discovery, independent of a developer's shell.
    env.pop("SYSTEMD_UNIT_PATH", None)
    result = subprocess.run(
        ["systemd-analyze", f"--root={root}", "--generators=no", "--man=no", "verify", target],
        capture_output=True, text=True, env=env, timeout=30,
    )
    evidence = os.environ.get("QTRADE_SYSTEMD_EVIDENCE_DIR")
    if evidence:
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.node.name)
        (directory / f"{name}.log").write_text(result.stdout + result.stderr)
        (directory / f"{name}.json").write_text(json.dumps({
            "command": result.args, "returncode": result.returncode,
            "transaction_errors": BAD_TRANSACTION.findall(result.stderr),
        }, indent=2) + "\n")
    return result


def _assert_intact_transaction(result, jobs):
    # verify may return 0 after deleting an optional job to break a boot cycle.
    assert not BAD_TRANSACTION.search(result.stderr), result.stderr
    assert result.returncode == 0, result.stderr
    for job in jobs:
        assert f"Installed new job {job}/start" in result.stderr, (
            f"required boot job absent: {job}\n{result.stderr}"
        )


def _dropin(root, unit, setting):
    path = root / f"etc/systemd/system/{unit}.d/ordering-test.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[Unit]\n{setting}\n")


@pytest.mark.parametrize("target,jobs", [
    ("multi-user.target", ("basic.target", "sockets.target", CONFIG, "docker.socket",
                           "docker.service", "qtrade-firstboot.service",
                           "qtrade-container-janitor.service")),
    ("docker.service", (CONFIG, "docker.socket", "docker.service")),
    ("docker.socket", (CONFIG, "docker.socket")),
])
def test_boot_transaction_keeps_docker_configuration_and_activation_jobs(graph, request, target, jobs):
    _assert_intact_transaction(_verify(graph, target, request), jobs)


@pytest.mark.parametrize("entrypoint", ["docker.service", "docker.socket"])
def test_docker_cannot_start_without_network_configuration_unit(graph, request, entrypoint):
    config = graph / f"etc/systemd/system/{CONFIG}"
    config.unlink()
    config.symlink_to("/dev/null")
    result = _verify(graph, entrypoint, request)
    assert result.returncode != 0, "Docker accepted a missing required configuration unit"
    assert f"Unit {CONFIG} is masked" in result.stderr


@pytest.mark.parametrize("entrypoint", ["docker.service", "docker.socket"])
def test_network_configuration_must_complete_before_docker_activation(graph, request, entrypoint):
    # An opposite ordering edge must conflict with the real startup graph.
    _dropin(graph, CONFIG, f"After={entrypoint}")
    result = _verify(graph, entrypoint, request)
    assert "ordering cycle" in result.stderr, "Docker has no effective configuration-before-start order"


@pytest.mark.parametrize("prerequisite", ["sysinit.target", "local-fs.target"])
def test_configuration_waits_for_system_initialization(graph, request, prerequisite):
    _dropin(graph, CONFIG, f"Before={prerequisite}")
    result = _verify(graph, "multi-user.target", request)
    assert "ordering cycle" in result.stderr, f"configuration no longer waits for {prerequisite}"


def test_cycle_guard_rejects_legacy_implicit_basic_target_dependency(graph, request):
    # Restore the old implicit edge in a private copy, leaving source untouched.
    _dropin(graph, CONFIG, "After=basic.target")
    result = _verify(graph, "multi-user.target", request)
    assert "ordering cycle" in result.stderr
    with pytest.raises(AssertionError, match="ordering cycle"):
        _assert_intact_transaction(result, (CONFIG, "docker.socket", "docker.service"))
