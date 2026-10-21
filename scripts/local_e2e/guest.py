"""Linux build/start operations with explicit, validated deployment inputs."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from common import digest, fingerprint, save, verify
from context import LocalContext, load_context
from runtime_guard import owned_guest

def http(url: str, token: str | None = None) -> tuple[int, dict]:
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("HEALTH_RESPONSE_TOO_LARGE")
            return response.status, json.loads(raw)
    except HTTPError as error:
        status = error.code
        error.close()
        return status, {}

def bind(context: LocalContext) -> None:
    data = Path(context.linux_data)
    token = sys.stdin.buffer.read(4097).decode("ascii").strip()
    if not 32 <= len(token) <= 4096 or not all(c.isalnum() or c in "_-" for c in token):
        raise ValueError("BOOTSTRAP_CREDENTIAL_INVALID")
    cfg = tomllib.loads((data / "agent.toml").read_text(encoding="utf-8-sig"))
    target = Path(cfg["winagent"]["token_file"])
    if target != data / "winagent.token" or target.is_symlink():
        raise ValueError("CREDENTIAL_DESTINATION_UNAPPROVED")
    status, _ = http(cfg["winagent"]["url"] + "/wa/v1/time", token)
    if status != 200:
        raise ValueError("AGENT_ROLE_CREDENTIAL_REJECTED: http=" + str(status))
    if target.exists() and target.read_text().strip() != token:
        backup = target.with_name("winagent.token.before-" + str(time.time_ns()))
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(target.read_bytes())
    fd, name = tempfile.mkstemp(prefix=".winagent-token-", dir=data)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(token.encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    status, _ = http(cfg["winagent"]["url"] + "/wa/v1/time", token)
    if status != 200:
        raise ValueError("AGENT_ROLE_BIND_VERIFY_FAILED: http=" + str(status))
    print(json.dumps({"bound": True, "agent_role_time": status, "mode": oct(target.stat().st_mode & 0o777)}))

def run(cmd: list[str], cwd: Path, log: Path, timeout: int) -> None:
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("OSS_", "OSSBOX_", "XUNJIA_", "QT_DEV_", "VITE_DEV_", "PW_", "AGENT_BASE_URL"))}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    try:
        with log.open("wb") as output:
            result = subprocess.run(cmd, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, timeout=timeout, env=env)
    except FileNotFoundError:
        raise RuntimeError("BUILD_TOOL_MISSING: " + cmd[0]) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("BUILD_STEP_TIMEOUT: " + str(log)) from None
    if result.returncode:
        raise RuntimeError(f"BUILD_STEP_FAILED: exit={result.returncode}; log={log}")

def build(release: Path, context: LocalContext) -> None:
    source = release / "source"
    expected = json.loads((release / "source-manifest.json").read_text())
    verify(source, expected)
    console = source / "console"
    deps = Path(context.linux_dependencies)
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("BUILD_TOOL_MISSING: npm")
    if not (console / "node_modules").exists():
        if (deps / "node_modules").is_dir() and (deps / "package-lock.json").is_file() and (console / "package-lock.json").read_bytes() == (deps / "package-lock.json").read_bytes():
            (console / "node_modules").symlink_to(deps / "node_modules", target_is_directory=True)
        else:
            run([npm, "ci", "--offline"], console, release / "npm-ci.log", 600)
    run([context.linux_python, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_local_e2e_launcher.py", "tests/test_local_e2e_launcher_lock.py",
         "tests/test_auth_health_closeout.py", "tests/test_ossbox_sts.py",
         "tests/acceptance/test_spec_sts.py", "tests/acceptance/test_sts_mq_contract.py",
         "tests/acceptance/test_sts_settings_closure.py"], source, release / "backend-gates.log", 600)
    for task in ("typecheck", "test", "build"):
        run([npm, "run", task], console, release / (task + ".log"), 600)
    server = {"host": "127.0.0.1", "port": urlsplit(context.vite_origin).port, "strictPort": True,
              "proxy": {"/api/v1": {"target": context.agent_origin, "ws": True, "changeOrigin": True}}}
    config = ("import base from " + json.dumps(str(console / "vite.config.ts")) + ";\n"
              + "import {mergeConfig} from \"vite\";\nexport default mergeConfig(base,"
              + json.dumps({"server": server}) + ");\n")
    (console / "vite.local-e2e.config.mjs").write_text(config)
    verify(source, expected)
    assets = {str(path.relative_to(release)): digest(path) for path in (console / "dist").rglob("*") if path.is_file()}
    assets[str((console / "vite.local-e2e.config.mjs").relative_to(release))] = digest(console / "vite.local-e2e.config.mjs")
    assets["context.json"] = digest(release / "context.json")
    save(release / "runtime-artifacts.json", assets)
    print("FRONTEND_BUILD_OK")

def running_owned(context: LocalContext) -> list[tuple[int, str, bytes]]:
    result = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        pid = int(path.parent.name)
        if pid in (os.getpid(), os.getppid()):
            continue
        try:
            raw = path.read_bytes()
            args = raw.decode().split("\0")
            cwd = (path.parent / "cwd").resolve()
        except (OSError, UnicodeError):
            continue
        kind = owned_guest(args, cwd, Path(context.linux_builds), Path(context.linux_data))
        if kind:
            result.append((pid, kind, raw))
    return result

def restart(release: Path, context: LocalContext) -> None:
    expected = json.loads((release / "source-manifest.json").read_text())
    verify(release / "source", expected)
    verify(release, json.loads((release / "runtime-artifacts.json").read_text()))
    for pid, kind, before in running_owned(context):
        path = Path("/proc") / str(pid) / "cmdline"
        if not path.exists():
            continue
        if path.read_bytes() != before:
            raise RuntimeError("PROCESS_OWNERSHIP_CHANGED")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 45
        while path.exists() and path.read_bytes() and time.monotonic() < deadline:
            time.sleep(.2)
        if path.exists() and path.read_bytes():
            raise RuntimeError("PROCESS_SHUTDOWN_TIMEOUT: " + kind)
    for origin in (context.agent_origin, context.vite_origin):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", urlsplit(origin).port)) == 0:
                raise RuntimeError("UNOWNED_PORT_LISTENER: " + origin)
    node = shutil.which("node")
    if not node:
        raise RuntimeError("BUILD_TOOL_MISSING: node")
    source = release / "source"
    data = Path(context.linux_data)
    logs = release / "runtime"
    logs.mkdir(exist_ok=True)
    cmds = {"agent": [context.linux_python, "-B", str(source / "scripts/local_e2e/agent_entry.py"),
                      "--data", str(data), "--release-id", fingerprint(expected),
                      "--api-port", str(urlsplit(context.agent_origin).port)],
            "vite": [node, str(source / "console/node_modules/vite/bin/vite.js"),
                     "--config", str(source / "console/vite.local-e2e.config.mjs")]}
    pids = {}
    for kind, cmd in cmds.items():
        with (logs / (kind + ".log")).open("ab") as output:
            process = subprocess.Popen(cmd, cwd=data if kind == "agent" else source / "console",
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        pids[kind] = process.pid
    save(logs / "pids.json", pids)
    print(json.dumps(pids))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["build", "bind", "restart"])
    parser.add_argument("--release")
    parser.add_argument("--context", required=True)
    options = parser.parse_args()
    context = load_context(options.context)
    if options.action == "bind":
        bind(context)
    else:
        release = Path(options.release).resolve()
        if release.parent != Path(context.linux_builds).resolve():
            raise ValueError("RELEASE_ROOT_UNAPPROVED")
        {"build": build, "restart": restart}[options.action](release, context)
