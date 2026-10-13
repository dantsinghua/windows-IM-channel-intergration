"""Inspect distributed permissions without extracting or booting the rootfs."""
import os
from pathlib import Path, PurePosixPath
import tarfile

import pytest


CONTROL_FILES = (
    "etc/wsl.conf",
    "etc/systemd/system/docker.service.d/qtrade.conf",
    "etc/systemd/system/docker.socket.d/qtrade.conf",
)
CONTROL_DIRS = tuple(str(PurePosixPath(path).parent) for path in CONTROL_FILES[1:])
RUNTIME_SCRIPTS = (
    "qtrade-container-janitor.sh",
    "qtrade-docker-config.sh",
    "qtrade-firstboot.sh",
    "qtrade-install-selftest.sh",
)
PLATFORM_TOOLS = (
    "adb", "fastboot", "etc1tool", "hprof-conv", "make_f2fs",
    "make_f2fs_casefold", "mke2fs", "sqlite3",
)


@pytest.fixture(scope="module")
def members():
    path = os.environ.get("QTRADE_ROOTFS_ARCHIVE")
    if not path:
        pytest.skip("QTRADE_ROOTFS_ARCHIVE not supplied; release archive has not been inspected")
    assert Path(path).is_file(), "explicit release artifact does not exist"
    with tarfile.open(path, "r:*") as archive:
        return {member.name.removeprefix("./").rstrip("/"): member
                for member in archive.getmembers()}


def controlled(path):
    # /tmp and other general runtime directories are deliberately out of scope.
    return (path == "opt/qtrade" or path.startswith("opt/qtrade/")
            or path in CONTROL_FILES or path in CONTROL_DIRS)


@pytest.mark.parametrize("kind", ["files", "directories"])
def test_distributed_control_tree_cannot_be_modified_by_group_or_other(members, kind):
    entries = [(path, entry) for path, entry in members.items()
               if controlled(path) and (entry.isdir() if kind == "directories"
                                        else entry.isfile() or entry.islnk())]
    assert entries, f"no controlled {kind} were present in the archive"
    # Symlink mode bits are ignored by Linux; their targets are checked separately.
    bad = [f"{path}: {entry.mode:#06o}" for path, entry in entries if entry.mode & 0o022]
    assert not bad, "group/other can modify distributed control entries:\n" + "\n".join(bad)


@pytest.mark.parametrize("path", CONTROL_FILES)
def test_distributed_wsl_and_docker_configuration_is_readable_nonexecutable(members, path):
    entry = members.get(path)
    assert entry is not None and entry.isfile(), f"required configuration absent: {path}"
    assert entry.mode == 0o644, f"{path}: expected 0644, got {entry.mode:#06o}"


@pytest.mark.parametrize("name", RUNTIME_SCRIPTS)
def test_distributed_control_script_remains_executable(members, name):
    path = f"opt/qtrade/bin/{name}"
    entry = members.get(path)
    assert entry is not None and entry.isfile(), f"required control script absent: {path}"
    assert entry.mode == 0o755, f"{path}: expected 0755, got {entry.mode:#06o}"


@pytest.mark.parametrize("name", PLATFORM_TOOLS)
def test_distributed_platform_tool_remains_executable(members, name):
    path = f"opt/qtrade/platform-tools/{name}"
    entry = members.get(path)
    assert entry is not None and entry.isfile(), f"required tool absent: {path}"
    assert entry.mode == 0o755, f"{path}: expected 0755, got {entry.mode:#06o}"


def test_distributed_frida_tool_remains_executable(members):
    entries = [(path, entry) for path, entry in members.items()
               if path.startswith("opt/qtrade/frida/frida-server") and entry.isfile()]
    assert entries, "required frida-server is absent"
    bad = [f"{path}: {entry.mode:#06o}" for path, entry in entries if entry.mode != 0o755]
    assert not bad, "frida-server must remain executable with 0755:\n" + "\n".join(bad)


@pytest.mark.parametrize("suffix", [".json", ".yaml", ".lock", ".expected", ".apk"])
def test_distributed_static_data_is_readable_nonexecutable(members, suffix):
    suffixes = (".yaml", ".yml") if suffix == ".yaml" else (suffix,)
    entries = [(path, entry) for path, entry in members.items()
               if path.startswith("opt/qtrade/") and path.endswith(suffixes) and entry.isfile()]
    assert entries, f"required static data category absent: {suffix}"
    bad = [f"{path}: {entry.mode:#06o}" for path, entry in entries if entry.mode != 0o644]
    assert not bad, "static data must use 0644:\n" + "\n".join(bad)


@pytest.mark.parametrize("path", [
    "opt/qtrade/scrcpy/scrcpy-server",
    "opt/qtrade/platform-tools/mke2fs.conf",
    "opt/qtrade/platform-tools/source.properties",
    "opt/qtrade/platform-tools/NOTICE.txt",
])
def test_distributed_non_native_tool_data_is_readable_nonexecutable(members, path):
    entry = members.get(path)
    assert entry is not None and entry.isfile(), f"required tool data absent: {path}"
    assert entry.mode == 0o644, f"{path}: expected 0644, got {entry.mode:#06o}"


def test_distributed_control_directories_remain_traversable(members):
    required = ("opt/qtrade", "opt/qtrade/bin", "opt/qtrade/platform-tools", *CONTROL_DIRS)
    assert all(path in members and members[path].isdir() for path in required)
    bad = [f"{path}: {entry.mode:#06o}" for path, entry in members.items()
           if controlled(path) and entry.isdir() and entry.mode & 0o111 != 0o111]
    assert not bad, "distributed control directories must remain traversable:\n" + "\n".join(bad)
