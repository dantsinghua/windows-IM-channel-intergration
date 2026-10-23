"""Archive-only identity check; never import, boot, or extract a rootfs."""
import os
from pathlib import Path
import posixpath
import tarfile

import pytest


@pytest.fixture(scope="module")
def archive():
    path = os.environ.get("QTRADE_ROOTFS_ARCHIVE")
    if not path:
        pytest.skip("QTRADE_ROOTFS_ARCHIVE not supplied; release archive has not been inspected")
    assert Path(path).is_file(), "explicit release artifact does not exist"
    with tarfile.open(path, "r:*") as tar:
        yield tar


@pytest.mark.parametrize("path", ["etc/machine-id", "var/lib/dbus/machine-id"])
def test_archive_has_no_build_machine_identity(archive, path):
    names = {member.name.removeprefix("./"): member for member in archive.getmembers()}
    member = names.get(path)
    if member is None:
        return
    if member.issym():
        assert path == "var/lib/dbus/machine-id"
        assert member.linkname in {"/etc/machine-id", "../../../etc/machine-id"}
        return
    assert member.isfile()
    assert archive.extractfile(member).read().strip() == b"", "build-machine identity leaked into distributable rootfs"


@pytest.mark.parametrize("path", ["etc/hostname", "etc/resolv.conf", "etc/hosts"])
def test_archive_does_not_embed_builder_hostname_or_network(archive, path):
    members = {member.name.removeprefix("./"): member for member in archive.getmembers()}
    member = members.get(path)
    if member is None:
        return
    assert member.isfile(), "WSL should generate target-machine networking on first boot"
    assert archive.extractfile(member).read().strip() == b""


@pytest.mark.parametrize("path", ["etc/qtrade/install.env", "etc/qtrade/.imported", "etc/qtrade/.docker-configured",
                                  "var/lib/qtrade/agent.db", "run/qtrade/host.json"])
def test_archive_has_no_prior_installation_state(archive, path):
    assert path not in {member.name.removeprefix("./") for member in archive.getmembers()}


def test_archive_docker_bootstrap_uses_its_shipped_python(archive, tmp_path):
    """Inspect the actual cold artifact, then execute its shell in an isolated fixture."""
    from test_rootfs_portability import run_docker_config

    members = {member.name.removeprefix("./"): member for member in archive.getmembers()}
    executable = "opt/qtrade/agent/venv/bin/python"
    seen = set()
    while True:
        assert executable not in seen, "bundled Python has a symlink cycle"
        seen.add(executable)
        member = members.get(executable)
        assert member is not None, f"bundled Python target is absent: {executable}"
        if member.issym():
            target = member.linkname
            executable = posixpath.normpath(
                target if target.startswith("/") else posixpath.join(posixpath.dirname(executable), target),
            ).lstrip("/")
        elif member.islnk():
            executable = posixpath.normpath(member.linkname).lstrip("/")
        else:
            assert member.isfile() and member.mode & 0o111, "bundled Python is not executable"
            assert archive.extractfile(member).read(4) == b"\x7fELF", "bundled Python is not a Linux executable"
            break

    script = members.get("opt/qtrade/bin/qtrade-docker-config.sh")
    assert script is not None and script.isfile() and script.mode & 0o111
    script_text = archive.extractfile(script).read().decode("utf-8")
    result, daemon, marker = run_docker_config(
        tmp_path,
        "QTRADE_DOCKER_POOL_BASE=10.244.0.0/16\nQTRADE_DOCKER_BIP=10.244.0.1/24\n",
        script_text=script_text,
    )
    assert result.returncode == 0, result.stderr
    assert daemon.is_file() and marker.is_file()
    assert (tmp_path / "pinned-python.invoked").read_text().splitlines() == ["invoked"]
