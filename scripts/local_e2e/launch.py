"""User-clicked, fail-closed launcher for the latest saved workstation source."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from common import manifest, verify, save, fingerprint
from launcher_lock import LauncherLock
from prepare import prepare, BUILD, UNC
from runtime_guard import user_config, health_ready

REPO = Path(__file__).resolve().parents[2]
DEV = Path.home() / "qtrade-dev"
ROOT = Path(r"C:\ProgramData\QTrade-E2E\winagent")
DATA = UNC / "home/anlin/work/qtrade-build/real-chain-20261010"
NO_WINDOW = 0x08000000
POWERSHELL = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")

def message(text: str, flags: int = 0x10) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, "QTrade latest local E2E", flags)

def run(command, log: Path, timeout: int = 600) -> None:
    with log.open("wb") as output:
        result = subprocess.run([str(arg) for arg in command], stdout=output, stderr=subprocess.STDOUT,
                                timeout=timeout, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Step failed; see " + str(log))

def request(url: str, token: str | None = None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=15) as response:
            payload = response.read(1048577)
            if len(payload) > 1048576: raise RuntimeError("Oversized readiness response")
            return response.status, json.loads(payload)
    except HTTPError as error:
        status = error.code
        error.close()
        return status, {}

def stop_owned_desktop() -> None:
    import psutil
    legacy = os.path.normcase(str(DEV / "electron-host"))
    parent = os.path.normcase(str(BUILD))
    owned = []
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            executable = process.info["exe"] or ""
            arguments = process.info["cmdline"] or []
            if any(arg.startswith("--type=") for arg in arguments): continue
            old = os.path.normcase(os.path.dirname(executable)) == legacy
            new = (os.path.normcase(executable).startswith(parent + os.sep)
                   and Path(executable).name.lower() == "electron.exe"
                   and any(os.path.normcase(arg).startswith(parent + os.sep)
                           and Path(arg).name == "desktop" for arg in arguments[1:]))
            if old or new: owned.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied): continue
    for process in owned:
        try:
            process.terminate()
            process.wait(timeout=15)
        except psutil.NoSuchProcess: pass

def start_user(logs: Path) -> int:
    import psutil
    executable = ROOT / "app/qtrade-winagent-user.exe"
    existing = []
    for process in psutil.process_iter(["exe"]):
        if process.info["exe"] and os.path.normcase(process.info["exe"]) == os.path.normcase(str(executable)):
            existing.append(process.pid)
    if existing: raise RuntimeError("User agent still running after controlled service restart")
    configured = user_config((ROOT / "winagent.toml").read_text(encoding="utf-8-sig"))
    target = DEV / "user-session/winagent-user.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".toml.new")
    temporary.write_text(configured, encoding="utf-8", newline="\n")
    temporary.replace(target)
    with (logs / "user.stdout.log").open("wb") as output, (logs / "user.stderr.log").open("wb") as error:
        process = subprocess.Popen([str(executable), "--root", str(ROOT), "--config", str(target)],
                                   stdout=output, stderr=error, creationflags=NO_WINDOW)
    time.sleep(2)
    if process.poll() is not None: raise RuntimeError("User agent exited before readiness")
    save(logs / "user.json", {"pid": process.pid, "config": str(target), "autostart": False})
    return process.pid

def wait_ready(plan: dict, logs: Path) -> dict:
    api_token = (DATA / "api.token").read_text(encoding="utf-8-sig").strip()
    deadline = time.monotonic() + 150
    samples = []
    while time.monotonic() < deadline:
        sample = {"time": time.time()}
        try:
            status, version = request("http://127.0.0.1:17650/__local_e2e/version")
            sample["version_matches"] = status == 200 and version.get("source_fingerprint") == plan["revision"]
            status, health = request("http://127.0.0.1:17650/api/v1/system/health", api_token)
            sample["authenticated_health"] = status
            sample["winagent"] = health.get("winagent")
            sample["dockerd"] = health.get("dockerd")
            ok = status == 200 and health_ready(health) and sample["version_matches"]
            if ok:
                status, settings = request("http://127.0.0.1:17650/api/v1/settings/ossbox", api_token)
                sample["sts_settings_status"] = status
                ok = status == 200 and "sts_url" in settings.get("data", {})
            samples.append(sample)
            save(logs / "readiness.json", samples)
            if ok: return sample
        except (OSError, ValueError, RuntimeError):
            sample["probe_error"] = "readiness_unavailable"
            samples.append(sample)
            save(logs / "readiness.json", samples)
        time.sleep(3)
    raise RuntimeError("Authenticated startup readiness failed; desktop not launched. See " + str(logs / "readiness.json"))

def start_desktop(plan: dict, logs: Path) -> int:
    environment = os.environ.copy()
    environment.pop("ELECTRON_RUN_AS_NODE", None)
    environment.update(VITE_DEV_SERVER_URL="http://127.0.0.1:5313",
                       QT_DEV_TOKEN=(DATA / "api.token").read_text(encoding="utf-8-sig").strip(),
                       PROGRAMDATA=str(DEV / "programdata"))
    import tomllib
    configuration = tomllib.loads((DEV / "programdata/QTrade/config/console.toml").read_text(encoding="utf-8-sig"))
    if configuration.get("endpoint", {}) != {"agent": "http://127.0.0.1:17650", "winagent": "http://127.0.0.1:17610"}:
        raise RuntimeError("Unexpected desktop endpoint configuration")
    with (logs / "electron.stdout.log").open("wb") as output, (logs / "electron.stderr.log").open("wb") as error:
        process = subprocess.Popen([plan["electron"], plan["desktop"]], cwd=DEV, env=environment,
                                   stdout=output, stderr=error, creationflags=NO_WINDOW)
    time.sleep(5)
    if process.poll() is not None: raise RuntimeError("Latest Electron process exited during startup")
    return process.pid

def read_state():
    try: return json.loads((BUILD / "current-run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): return {}

def activate_desktop(pid):
    import psutil
    if not isinstance(pid, int) or not psutil.pid_exists(pid): return False
    api = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def activate(window, _):
        owner = ctypes.c_ulong()
        api.GetWindowThreadProcessId(ctypes.c_void_p(window), ctypes.byref(owner))
        if owner.value == pid and api.IsWindowVisible(ctypes.c_void_p(window)):
            api.ShowWindow(ctypes.c_void_p(window), 9)
            api.SetForegroundWindow(ctypes.c_void_p(window))
        return 1
    api.EnumWindows(callback_type(activate), 0)
    return True

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--confirmed", action="store_true", help="Operator already authorized this local restart; UAC is still required")
    options = parser.parse_args()
    if os.name != "nt": raise RuntimeError("This entry is for the Windows workstation")
    lock = LauncherLock("Local\\QTradeLatestLocalE2E")
    try:
        waited = not lock.acquire()
        if waited:
            last_notice = [None]
            def waiting():
                owner = read_state()
                brief = (owner.get("pid"), owner.get("phase"), owner.get("build_log"))
                if brief != last_notice[0]:
                    print("WAITING_FOR_OWNER " + json.dumps({"pid": brief[0], "phase": brief[1], "log": owner.get("log_dir"), "build_log": brief[2]}), flush=True)
                    last_notice[0] = brief
            if not lock.wait_until_free(timeout_s=1000, progress=waiting):
                raise RuntimeError("Existing startup still owns its OS mutex; inspect its recorded PID and log. No lock was deleted.")
            print("PREVIOUS_RUN_FINISHED; continuing with the latest version check", flush=True)
        previous = read_state()
        if waited and previous.get("success") is True and previous.get("phase") == "ready":
            if previous.get("revision") == fingerprint(manifest(REPO)) and activate_desktop(previous.get("electron_pid")):
                print("LATEST_DESKTOP_ALREADY_STARTED_BY_PREVIOUS_RUN", flush=True)
                return 0
        logs = DEV / "logs" / ("latest-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()))
        logs.mkdir(parents=True, exist_ok=False)
        result = {"success": False, "phase": "prepare", "pid": os.getpid(), "started_at": time.time(),
                  "mode": "prepare_only" if options.prepare_only else "start", "log_dir": str(logs)}
        def checkpoint(phase, **fields):
            result.update(fields, phase=phase, updated_at=time.time())
            save(logs / "result.json", result)
            save(BUILD / "current-run.json", result)
            print("PHASE=" + phase + " LOG=" + str(logs), flush=True)
        try:
            checkpoint("prepare")
            plan = prepare(progress=checkpoint)
            result["revision"] = plan["revision"]
            if options.prepare_only:
                result.update(success=True, deployed=False)
                checkpoint("prepared")
                print(json.dumps(result), flush=True)
                return 0
            checkpoint("awaiting_restart_confirmation")
            if not options.confirmed:
                answer = message("Build checks passed. Restart only QTrade local E2E processes and bind the current service credential? Existing IM accounts may resume. No global WSL or Docker restart.", 0x24)
                if answer != 6:
                    checkpoint("cancelled")
                    return 2
            expected = json.loads((Path(plan["base"]) / "source-manifest.json").read_text())
            if manifest(REPO) != expected: raise RuntimeError("Source changed after build; click again to build the new revision")
            checkpoint("close_owned_desktop")
            stop_owned_desktop()
            checkpoint("awaiting_uac_and_deployment")
            script = Path(plan["source"]) / "scripts/local_e2e/Elevate.ps1"
            run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 "-PlanPath", Path(plan["base"]) / "ready.json"], logs / "administrator.log", timeout=500)
            deployment = json.loads((Path(plan["base"]) / "deployment.json").read_text(encoding="utf-8-sig"))
            if deployment.get("success") is not True or deployment.get("credential_bound") is not True:
                raise RuntimeError("Administrator deployment did not verify the Agent-role credential")
            checkpoint("start_user_session")
            result["user_pid"] = start_user(logs)
            checkpoint("restart_guest_processes")
            run(["wsl.exe", "-d", "Ubuntu-24.04", "--exec", "/home/anlin/.venvs/qtrade/bin/python", "-B",
                 plan["linux_release"] + "/source/scripts/local_e2e/guest.py", "restart",
                 "--release", plan["linux_release"]], logs / "guest-restart.log", timeout=130)
            checkpoint("authenticated_readiness")
            result["readiness"] = wait_ready(plan, logs)
            if manifest(REPO) != expected: raise RuntimeError("Source changed while restarting; this run is not the latest saved version")
            checkpoint("start_latest_desktop")
            result["electron_pid"] = start_desktop(plan, logs)
            result.update(success=True, sts_live_test_run=False, real_im_test_run=False)
            checkpoint("ready")
            save(BUILD / "active.json", {**plan, "run": result})
            print(json.dumps(result), flush=True)
            return 0
        except Exception as error:
            result.update(error=str(error), failed_phase=result["phase"])
            checkpoint("failed")
            if not options.prepare_only: message(str(error) + "\nLog: " + str(logs))
            print(json.dumps(result), file=sys.stderr)
            return 1
    finally:
        lock.close()

if __name__ == "__main__":
    raise SystemExit(main())
