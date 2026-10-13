"""Build-host COM wrappers must not become frozen runtime configuration.

The spec runs with a realistic Analysis TOC, without importing a Windows SDK.
The runtime hook runs against injected modules, without COM or filesystem I/O.
Native artifact checks are recorded separately in the package evidence directory.
"""
from __future__ import annotations

import builtins
import os
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest


BUILD = Path(os.environ.get("QTRADE_COMTYPES_BUILD_DIR", Path(__file__).resolve().parents[1] / "build"))
USER_SPEC = BUILD / "qtrade-winagent-user.spec"


def _run_spec(monkeypatch, pure, datas=()):
    """Execute the real spec with deterministic SDK discovery and build objects."""
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda _package: ([], [], [])
    hooks.collect_submodules = lambda _package: []
    monkeypatch.setitem(sys.modules, "PyInstaller", ModuleType("PyInstaller"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", ModuleType("PyInstaller.utils"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    seen = {}

    def analysis(scripts, **kwargs):
        seen["analysis"] = kwargs
        obj = SimpleNamespace(pure=pure, scripts=scripts, zipped_data=[], binaries=[], zipfiles=[], datas=list(datas))
        seen["result"] = obj
        return obj

    def pyz(modules, *_args, **_kwargs):
        seen["pyz"] = modules

    runpy.run_path(str(USER_SPEC), init_globals={
        "SPEC": str(USER_SPEC), "Analysis": analysis, "PYZ": pyz,
        "EXE": lambda *_args, **_kwargs: None,
        "COLLECT": lambda *_args, **_kwargs: None,
    })
    return seen


@pytest.mark.parametrize("generated", [
    "comtypes.gen.UIAutomationClient",
    "comtypes.gen.stdole",
    "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0",
    "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
    "comtypes.gen.FutureTargetLibrary",
])
def test_generated_com_wrapper_never_enters_runtime_archive(monkeypatch, generated):
    base = ("comtypes.gen", "sdk/comtypes/gen/__init__.py", "PYMODULE")
    wrapper = (generated, "sdk/comtypes/gen/generated.py", "PYMODULE")
    assert _run_spec(monkeypatch, [base, wrapper])["pyz"] == [base]


def test_package_base_sdk_modules_and_similar_names_survive_unchanged(monkeypatch):
    names = ["comtypes", "comtypes.gen", "comtypes.client", "comtypes.tools.codegenerator",
             "comtypes.general", "another.comtypes.gen.Custom", "pywinauto", "qtrade_winagent.main_user"]
    modules = [(name, f"sdk/{index}.py", "PYMODULE") for index, name in enumerate(names)]
    assert _run_spec(monkeypatch, modules)["pyz"] == modules


def test_clean_analysis_without_generated_wrappers_still_builds(monkeypatch):
    assert _run_spec(monkeypatch, [])["pyz"] == []


@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize("filename", ["UIAutomationClient.py", "stdole.py", "_future.py"])
def test_generated_sources_are_also_removed_from_collected_files(monkeypatch, separator, filename):
    generated = (separator.join(["comtypes", "gen", filename]), "build-sdk/generated.py", "DATA")
    kept = [(separator.join(["comtypes", "gen", "__init__.py"]), "build-sdk/base.py", "DATA"),
            (separator.join(["comtypes", "client", "_generate.py"]), "build-sdk/generator.py", "DATA"),
            (separator.join(["another", "comtypes", "gen", filename]), "other-package/file.py", "DATA"),
            ("qtrade_winagent/schema_winagent.sql", "src/schema.sql", "DATA")]
    assert _run_spec(monkeypatch, [], [generated, *kept])["result"].datas == kept


def _runtime_hook(monkeypatch):
    hooks = _run_spec(monkeypatch, [])["analysis"]["runtime_hooks"]
    matching = [Path(path) for path in hooks if Path(path).name == "rthook_comtypes.py"]
    assert len(matching) == 1, "The user bundle must execute its COM portability hook exactly once"
    assert matching[0].is_file(), "The registered COM runtime hook must be shipped with the build source"
    return matching[0]


@pytest.mark.parametrize("initial", [None, "read-only-install/comtypes/gen", "user-cache/comtypes"])
def test_hook_forces_memory_generation_without_mutating_sdk_or_logging_paths(monkeypatch, capsys, caplog, initial):
    hook = _runtime_hook(monkeypatch)
    comtypes = ModuleType("comtypes")
    client = ModuleType("comtypes.client")
    gen = ModuleType("comtypes.gen")
    gen.__path__ = ["read-only-install/comtypes/gen"]
    marker = object()
    client.GetModule = marker
    client.gen_dir = initial
    comtypes.client, comtypes.gen = client, gen
    for name, module in [("comtypes", comtypes), ("comtypes.client", client), ("comtypes.gen", gen)]:
        monkeypatch.setitem(sys.modules, name, module)
    before = dict(gen.__dict__)
    caplog.clear()
    runpy.run_path(str(hook))
    runpy.run_path(str(hook))
    assert client.gen_dir is None
    assert client.GetModule is marker
    assert dict(gen.__dict__) == before
    assert capsys.readouterr() == ("", "")
    assert not caplog.records


@pytest.mark.parametrize("present,initial,expected", [(False, None, 0), (True, 0, 0), (True, 2, 2), (True, 6, 6)])
def test_hook_preserves_default_mta_and_caller_com_apartment_policy(monkeypatch, present, initial, expected):
    hook = _runtime_hook(monkeypatch)
    if present:
        monkeypatch.setattr(sys, "coinit_flags", initial, raising=False)
    else:
        monkeypatch.delattr(sys, "coinit_flags", raising=False)
    comtypes, client = ModuleType("comtypes"), ModuleType("comtypes.client")
    comtypes.client = client
    client.gen_dir = "cache"
    monkeypatch.setitem(sys.modules, "comtypes", comtypes)
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    importer, seen = builtins.__import__, []

    def inspect_import(name, *args, **kwargs):
        if name == "comtypes.client":
            seen.append(getattr(sys, "coinit_flags", None))
        return importer(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", inspect_import)
    runpy.run_path(str(hook))
    assert seen == [expected]
    assert hasattr(sys, "coinit_flags") is present
    if present:
        assert sys.coinit_flags == initial
    assert client.gen_dir is None


@pytest.mark.parametrize("present,initial", [(False, None), (True, 0), (True, 2)])
def test_failed_sdk_import_restores_caller_com_policy_and_propagates_error(monkeypatch, present, initial):
    hook = _runtime_hook(monkeypatch)
    if present:
        monkeypatch.setattr(sys, "coinit_flags", initial, raising=False)
    else:
        monkeypatch.delattr(sys, "coinit_flags", raising=False)
    importer = builtins.__import__

    def fail_sdk(name, *args, **kwargs):
        if name == "comtypes.client":
            raise ImportError("synthetic COM dependency failure")
        return importer(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_sdk)
    with pytest.raises(ImportError, match="synthetic COM dependency failure"):
        runpy.run_path(str(hook))
    assert hasattr(sys, "coinit_flags") is present
    if present:
        assert sys.coinit_flags == initial
