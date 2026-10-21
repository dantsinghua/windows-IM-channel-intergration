"""Synthetic bootstrap faults; never starts a real service or IM client."""
import json
import threading
import tomllib
from types import SimpleNamespace
import pytest
from qtrade_winagent import main_svc
from qtrade_winagent.startup import startup_diagnostic

@pytest.mark.parametrize("code,expected", [(5,"ACCESS_DENIED"),(126,"NATIVE_DEPENDENCY_MISSING"),(193,"EXECUTABLE_ARCHITECTURE_INVALID"),(10048,"PORT_IN_USE"),(577,"EXECUTABLE_SIGNATURE_REJECTED")])
def test_original_native_cause_is_not_rewritten_as_auth(code, expected):
    error=OSError("synthetic-sensitive-text");error.winerror=code
    result=startup_diagnostic(error,"http_bind")
    assert result["reason"]==expected
    assert result["native_code"]==code
    assert "synthetic-sensitive" not in json.dumps(result)

def test_bad_toml_is_identified_before_building_any_runtime(tmp_path,monkeypatch):
    cfg=tmp_path/"winagent.toml";cfg.write_text("[wsl]\nautostart=false[wechat]\n")
    monkeypatch.setattr(main_svc,"build_real_deps",lambda *a,**k:pytest.fail("Runtime must not be started"))
    args=SimpleNamespace(root=str(tmp_path),config=str(cfg),dev=False,install_user_sid="S-1-5-21-1-2-3-1001")
    assert main_svc.run_service(args,threading.Event(),lambda:None)==2
    report=json.loads((tmp_path/"logs/startup-failure.json").read_text())
    assert report["reason"]=="CONFIG_INVALID" and report["stage"]=="configuration"

def test_missing_explicit_config_does_not_use_defaults(tmp_path,monkeypatch):
    monkeypatch.setattr(main_svc,"build_real_deps",lambda *a,**k:pytest.fail("No runtime on missing config"))
    args=SimpleNamespace(root=str(tmp_path),config=str(tmp_path/"missing.toml"),dev=False,install_user_sid="S-1-5-21-1-2-3-1001")
    assert main_svc.run_service(args,threading.Event(),lambda:None)==2
    report=json.loads((tmp_path/"logs/startup-failure.json").read_text())
    assert report["reason"]=="CONFIG_MISSING"

def test_scope_and_exception_type_survive_unknown_startup_failure():
    report=startup_diagnostic(RuntimeError("synthetic-secret"),"ipc")
    assert report["reason"]=="STARTUP_EXCEPTION" and report["stage"]=="ipc"
    assert report["error_type"]=="RuntimeError"
    assert "synthetic-secret" not in json.dumps(report)


def test_diagnostic_never_creates_a_nonexistent_product_root(tmp_path):
    from qtrade_winagent.startup import record_startup_failure
    root = tmp_path / "must-not-be-created"
    record_startup_failure(str(root), startup_diagnostic(FileNotFoundError(), "configuration"))
    assert not root.exists()
