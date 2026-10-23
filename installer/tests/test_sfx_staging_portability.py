"""Compile the actual patched C++ staging class with fake Win32 APIs, never an EXE."""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def stage_header():
    patch = (ROOT / "sfx-stub/qtrade-sfx.patch").read_text(encoding="utf-8-sig")
    marker = "+++ b/CPP/7zip/Bundles/SFXSetup/QTradeStage.h"
    assert marker in patch, "secure random stage implementation is absent from the distributable patch"
    added = patch.split(marker, 1)[1].split("\n--- ", 1)[0]
    return "\n".join(line[1:] for line in added.splitlines() if line.startswith("+") and not line.startswith("+++")) + "\n"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("g++ unavailable; fake-Win32 dynamic staging validation was not run")
    root = tmp_path_factory.mktemp("sfx-stage")
    (root / "QTradeStage.h").write_text(stage_header())
    for header in ("aclapi.h", "sddl.h", "shlobj.h"):
        (root / header).write_text("#pragma once\n")
    binary = root / "staging-fixture"
    result = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-I", str(root),
                             str(ROOT / "tests/fixtures/sfx_stage_fake.cpp"), "-o", str(binary)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.parametrize("scenario", ["unique", "collision", "junction-base", "junction-parent", "unsafe-base",
                                       "unprotected-base", "unsafe-ancestor", "guid-failure", "live-path",
                                       "all-collisions", "handle-failure", "acl-query-failure"])
def test_secure_random_stage_with_fake_win32(harness, scenario):
    result = subprocess.run([str(harness), scenario], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, f"{scenario}: {result.stderr}"


def test_sfx_configuration_targets_only_the_known_staging_base():
    text = (ROOT / "build/sfx-config-qtrade.txt").read_text(encoding="utf-8-sig")
    install = [line for line in text.splitlines() if line.startswith("InstallPath=")]
    assert install == ['InstallPath="%ProgramData%\\\\QTrade-Staging"']


def test_release_stub_contains_the_new_staging_behavior_marker():
    path = os.environ.get("QTRADE_SFX_STUB")
    if not path:
        pytest.skip("QTRADE_SFX_STUB not supplied; compiled release stub has not been inspected")
    data = Path(path).read_bytes()
    assert data[:2] == b"MZ"
    assert "QTRADE_UNIQUE_STAGE_V1".encode("utf-16-le") in data
