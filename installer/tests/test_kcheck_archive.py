"""Verify the actual micro-rootfs artifact without importing or launching WSL.

Set QTRADE_KCHECK_ARCHIVE to a built tar. This explicitly artifact-bound suite
skips when the caller has no archive, rather than silently testing an old one.
"""
from __future__ import annotations

import configparser
import hashlib
import os
from pathlib import Path, PurePosixPath
import struct
import tarfile

import pytest

PINNED_BUSYBOX_SHA256 = "6e123e7f3202a8c1e9b1f94d8941580a25135382b99e8d3e34fb858bba311348"


@pytest.fixture(scope="module")
def archive():
    configured = os.environ.get("QTRADE_KCHECK_ARCHIVE")
    if not configured:
        pytest.skip("QTRADE_KCHECK_ARCHIVE must name the built artifact")
    path = Path(configured)
    assert path.is_file(), f"archive missing: {path}"
    with tarfile.open(path, "r:") as opened:
        yield opened


def members(archive):
    return {str(PurePosixPath(member.name)): member for member in archive.getmembers()}


def contents(archive, name):
    member = members(archive)[name]
    assert member.isfile(), f"{name} must be a regular file"
    return archive.extractfile(member).read()


def test_wsl_owns_root_init_and_archive_cannot_redirect_its_creation(archive):
    # WSL creates /init before chroot. A supplied symlink makes O_TRUNC follow
    # its target outside the rootfs, so even a valid static BusyBox is unsafe.
    assert "init" not in members(archive), "do not ship /init in a WSL rootfs"


def test_busybox_is_the_pinned_executable_not_an_unverified_replacement(archive):
    data = contents(archive, "bin/busybox")
    assert hashlib.sha256(data).hexdigest() == PINNED_BUSYBOX_SHA256
    assert members(archive)["bin/busybox"].mode & 0o111 == 0o111


def test_busybox_is_static_x86_64_elf_without_a_dynamic_interpreter(archive):
    data = contents(archive, "bin/busybox")
    assert data[:6] == b"\x7fELF\x02\x01", "require 64-bit little-endian ELF"
    assert struct.unpack_from("<H", data, 18)[0] == 62, "require x86_64"
    offset = struct.unpack_from("<Q", data, 32)[0]
    stride, count = struct.unpack_from("<HH", data, 54)
    types = {struct.unpack_from("<I", data, offset + i * stride)[0] for i in range(count)}
    assert 3 not in types, "PT_INTERP would require an absent dynamic loader"
    assert 2 not in types, "PT_DYNAMIC is not allowed in this static micro-rootfs"


@pytest.mark.parametrize("applet", ["sh", "uname", "cat", "mount", "dmesg", "mkdir", "sleep"])
def test_kernel_probe_applets_resolve_to_the_pinned_busybox(archive, applet):
    member = members(archive)[f"bin/{applet}"]
    assert member.issym()
    assert member.linkname == "/bin/busybox"


def test_wsl_configuration_disables_external_integration_and_uses_root(archive):
    config = configparser.ConfigParser()
    config.read_string(contents(archive, "etc/wsl.conf").decode())
    for section, key in [("boot", "systemd"), ("interop", "enabled"), ("interop", "appendWindowsPath"), ("automount", "enabled")]:
        assert config.getboolean(section, key) is False
    assert config["user"]["default"] == "root"
    assert contents(archive, "etc/passwd").decode().splitlines() == ["root:x:0:0:root:/root:/bin/sh"]
    assert contents(archive, "etc/group").decode().splitlines() == ["root:x:0:"]


def test_archive_paths_ownership_and_temporary_directory_are_safe(archive):
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert member.uid == member.gid == 0
        assert not (member.ischr() or member.isblk() or member.isfifo())
    assert members(archive)["tmp"].mode == 0o1777
    assert members(archive)["etc/shadow"].mode & 0o007 == 0
