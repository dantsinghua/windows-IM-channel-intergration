"""Portability input contracts. Values are synthetic and no process is launched."""
import ast
import importlib.util
import json
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portable_context_contract", ROOT / "scripts/local_e2e/context.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def settings():
    return {"schema": 1, "build_root": "E:/Build Cache/portable", "dev_root": "D:/Users/Synthetic/dev",
            "service_root": "F:/Products/Test Product/winagent", "distro": "Portable-Test",
            "linux_user": "tester", "linux_python": "/opt/testing/bin/python",
            "linux_data": "/srv/runtime", "linux_builds": "/srv/releases",
            "linux_dependencies": "/srv/dependencies", "windows_python": "E:/Python/python.exe",
            "electron_archive": "E:/Assets/electron.zip", "agent_origin": "http://127.0.0.1:17690",
            "winagent_origin": "http://127.0.0.1:17691", "vite_origin": "http://127.0.0.1:5399"}

def test_non_default_drives_users_and_distro_are_preserved(monkeypatch):
    monkeypatch.setenv("SystemRoot", "D:/Windows")
    context = module.LocalContext.from_dict(settings())
    args = context.wsl_command("/srv/test.py")
    assert args[2] == "Portable-Test"
    assert args[4] == "tester"
    assert args[6] == "/opt/testing/bin/python"
    assert context.service_root.startswith("F:")
    assert module.LocalContext.from_dict(context.to_dict()).identity == context.identity

@pytest.mark.parametrize("field,value,reason", [
    ("service_root", "relative/path", "CONTEXT_PATH_INVALID"),
    ("linux_data", "/srv/../elsewhere", "CONTEXT_PATH_INVALID"),
    ("distro", "x --shutdown", "CONTEXT_DISTRO_INVALID"),
    ("linux_user", "root;shutdown", "CONTEXT_USER_INVALID"),
    ("agent_origin", "http://example.invalid:80", "CONTEXT_ORIGIN_INVALID"),
    ("agent_origin", "http://127.0.0.1:5399", "CONTEXT_PORT_CONFLICT"),
    ("service_root", "E:/Build Cache/portable/production", "CONTEXT_ROOT_OVERLAP"),
    ("linux_builds", "/srv/runtime/releases", "CONTEXT_ROOT_OVERLAP"),
    ("windows_python", "", "CONTEXT_FIELD_MISSING"),
    ("agent_origin", "http://user:secret@127.0.0.1:80", "CONTEXT_ORIGIN_INVALID"),
])
def test_invalid_context_has_exact_reason(field, value, reason):
    raw = settings()
    raw[field] = value
    with pytest.raises(module.ContextError) as error:
        module.LocalContext.from_dict(raw)
    assert error.value.reason == reason
    assert "secret" not in str(error.value)

def test_context_missing_is_not_reported_as_service_authentication(tmp_path):
    with pytest.raises(module.ContextError) as error:
        module.load_context(tmp_path / "missing.json")
    assert error.value.reason == "CONTEXT_MISSING"

def test_context_json_error_is_not_reported_as_port_conflict(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{bad")
    with pytest.raises(module.ContextError) as error:
        module.load_context(path)
    assert error.value.reason == "CONTEXT_FORMAT_INVALID"

def test_launch_scripts_have_no_workstation_identity_literals():
    forbidden = ("/home/" + "anlin", "users/" + "anlin", "ubuntu-" + "24.04",
                 "real-chain-2026", "final-regression-2026", "c:/programdata/qtrade-e2e")
    for path in (ROOT / "scripts/local_e2e").glob("*"):
        if path.suffix not in {".py", ".ps1"}:
            continue
        text = path.read_text(encoding="utf-8-sig").replace("\\", "/").lower()
        assert not any(value in text for value in forbidden), path.name

def test_packaged_sqlite_adapters_are_identical_contracts():
    left = ROOT / "src/qtrade_agent/store/locking.py"
    right = ROOT / "winagent/src/qtrade_winagent/sqlite_locking.py"
    assert ast.dump(ast.parse(left.read_text(encoding="utf-8-sig"))) == ast.dump(ast.parse(right.read_text(encoding="utf-8-sig")))
