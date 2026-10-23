"""Installer payloads must omit build provenance while retaining runtime metadata."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest


BUILD = Path(os.environ.get("QTRADE_METADATA_BUILD_DIR", Path(__file__).resolve().parents[1] / "build"))


def _collected(monkeypatch, name, datas):
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda _package: ([], [], [])
    hooks.collect_submodules = lambda _package: []
    for module, value in [("PyInstaller", ModuleType("PyInstaller")),
                          ("PyInstaller.utils", ModuleType("PyInstaller.utils")),
                          ("PyInstaller.utils.hooks", hooks)]:
        monkeypatch.setitem(sys.modules, module, value)
    result = SimpleNamespace(pure=[], scripts=[], zipped_data=[], binaries=[], zipfiles=[], datas=list(datas))
    seen = []

    def collect(_exe, _binaries, _zipfiles, final_datas, **_kwargs):
        seen.extend(final_datas)

    spec = BUILD / f"qtrade-winagent-{name}.spec"
    runpy.run_path(str(spec), init_globals={
        "SPEC": str(spec), "Analysis": lambda *_args, **_kwargs: result,
        "PYZ": lambda *_args, **_kwargs: None, "EXE": lambda *_args, **_kwargs: None,
        "COLLECT": collect,
    })
    return seen


@pytest.mark.parametrize("name", ["svc", "user"])
@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize("prefix", ["", "vendor/"])
def test_distribution_install_origin_is_excluded_from_final_bundle(monkeypatch, name, separator, prefix):
    destination = (prefix + "sample_sdk-1.2.dist-info/direct_url.json").replace("/", separator)
    row = (destination, "private-build-origin.json", "DATA")
    assert _collected(monkeypatch, name, [row]) == []


@pytest.mark.parametrize("name", ["svc", "user"])
def test_origin_detection_respects_windows_case_insensitive_paths(monkeypatch, name):
    row = ("Vendor\\Sample.DIST-Info\\DIRECT_url.JSON", "private-build-origin.json", "DATA")
    assert _collected(monkeypatch, name, [row]) == []


@pytest.mark.parametrize("name", ["svc", "user"])
@pytest.mark.parametrize("separator", ["/", "\\"])
def test_distribution_identity_dependencies_license_and_entrypoints_remain(monkeypatch, name, separator):
    filenames = ["METADATA", "WHEEL", "RECORD", "INSTALLER", "entry_points.txt", "licenses/LICENSE"]
    rows = [(("sample_sdk-1.2.dist-info/" + filename).replace("/", separator),
             f"build-metadata/{filename}", "DATA") for filename in filenames]
    assert _collected(monkeypatch, name, rows) == rows


@pytest.mark.parametrize("name", ["svc", "user"])
@pytest.mark.parametrize("separator", ["/", "\\"])
def test_regular_package_data_and_similar_metadata_names_are_not_removed(monkeypatch, name, separator):
    paths = ["package/direct_url.json", "direct_url.json", "package.dist-info/direct_url.json.bak",
             "package.dist-info-backup/direct_url.json", "package.dist-info/subdirectory/direct_url.json",
             "qtrade_winagent/schema_winagent.sql"]
    rows = [(path.replace("/", separator), "build-data/file", "DATA") for path in paths]
    assert _collected(monkeypatch, name, rows) == rows


@pytest.mark.parametrize("name", ["svc", "user"])
def test_filter_uses_bundle_destination_not_build_machine_source(monkeypatch, name):
    retained = ("package/direct_url.json", "build/sample.dist-info/direct_url.json", "DATA")
    removed = ("sample.dist-info/direct_url.json", "build/package/ordinary.json", "DATA")
    assert _collected(monkeypatch, name, [retained, removed]) == [retained]


@pytest.mark.parametrize("name", ["svc", "user"])
def test_bundle_without_provenance_files_builds_normally(monkeypatch, name):
    assert _collected(monkeypatch, name, []) == []
