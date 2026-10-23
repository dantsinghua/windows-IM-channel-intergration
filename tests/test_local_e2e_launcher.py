"""Launcher guards only: no process termination, UAC, network, or real services."""
from pathlib import Path
import importlib.util
import tomllib
import pytest

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location("local_e2e_test_"+name, ROOT/"scripts/local_e2e"/(name+".py"))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
common=load("common")
guard=load("runtime_guard")

def test_revision_includes_unsaved_git_changes_once_written_to_disk():
    assert common.fingerprint({"a":"1","b":"2"}) == common.fingerprint({"b":"2","a":"1"})
    assert common.fingerprint({"a":"1"}) != common.fingerprint({"a":"2"})

@pytest.mark.parametrize("name", ["src/qtrade_agent/ossbox/sts.py", "console/src/pages/mail/OssboxConfig.vue",
    "console/electron/main/winagent-pipe.ts", "scripts/local_e2e/launch.py", "docs/02.md", "tests/test_local_e2e_launcher.py"])
def test_required_sources_participate_in_version_identity(name):
    assert common.selected(name)

@pytest.mark.parametrize("name", ["console/node_modules/x.ts", "src/.env", "winagent/.venv-build/x.py",
    "scripts/local_e2e/private.token", "console/dist/main.js", "installer/out/x.txt"])
def test_credentials_and_build_outputs_never_enter_source_snapshot(name):
    assert not common.selected(name)

def test_artifact_tamper_fails_closed(tmp_path):
    file=tmp_path/"code.py";file.write_text("before")
    expected={"code.py":common.digest(file)}
    common.verify(tmp_path,expected)
    file.write_text("after")
    with pytest.raises(ValueError,match="Version gate failed"): common.verify(tmp_path,expected)

@pytest.mark.parametrize("text", [
    "[wsl]\nautostart = true\n[wechat]\nenabled = true\n",
    "[wsl]\r\nautostart = true\r\n[wechat]\r\nenabled = true\r\n",
    "[wechat]\nenabled = true",
    "[wsl]",
    "[wsl]\nautostart = false\n[wechat]\nenabled = true\n"])
def test_user_override_is_valid_and_changes_only_autostart(text):
    before=tomllib.loads(text)
    before.setdefault("wsl",{})["autostart"]=False
    assert tomllib.loads(guard.user_config(text))==before

def test_joined_toml_line_is_never_used():
    with pytest.raises(tomllib.TOMLDecodeError): guard.user_config("[wsl]\nautostart = false[wechat]\n")

def test_new_vite_process_is_found_on_second_click_without_port_in_arguments():
    builds=Path("/owned/releases"); data=Path("/owned/data")
    config=builds/"version/source/console/vite.local-e2e.config.mjs"
    assert guard.owned_guest(["/usr/bin/node","vite.js","--config",str(config)],config.parent,builds,data)=="vite"

def test_known_legacy_processes_can_be_replaced():
    builds=Path("/owned/releases"); data=Path("/owned/data")
    assert guard.owned_guest(["python","run_agent.py"],data,builds,data)=="agent"
    assert guard.owned_guest(["node","vite","--config",str(data/"vite.real.config.mjs"),"--port","5313"],Path("/elsewhere"),builds,data)=="vite"

@pytest.mark.parametrize("arguments", [
    ["python","-c","print(\"run_agent.py\")"],
    ["node","vite","--port","5313"],
    ["python","/other/source/scripts/local_e2e/agent_entry.py"],
    ["bash","/owned/releases/v/source/scripts/local_e2e/agent_entry.py"]])
def test_unowned_processes_are_never_selected(arguments):
    assert guard.owned_guest(arguments,Path("/owned/data"),Path("/owned/releases"),Path("/owned/data")) is None

@pytest.mark.parametrize("user", [False,None,"true",1])
def test_ping_and_partial_health_never_pass_ready_gate(user):
    assert not guard.health_ready({"ok":True,"dockerd":True,"winagent":{"online":True,"user_agent":user}})

def test_full_authenticated_health_passes():
    assert guard.health_ready({"ok":True,"dockerd":True,"winagent":{"online":True,"user_agent":True}})
    assert not guard.health_ready({"ok":True,"dockerd":True,"winagent":True,"user_agent":True})
