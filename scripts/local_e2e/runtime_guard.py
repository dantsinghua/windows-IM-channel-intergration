"""Pure ownership, readiness, and configuration guards for local E2E."""
from __future__ import annotations
import copy
import re
import tomllib
from pathlib import Path

def user_config(text: str) -> str:
    original = tomllib.loads(text.lstrip("\ufeff"))
    expected = copy.deepcopy(original)
    expected.setdefault("wsl", {})["autostart"] = False
    lines = text.lstrip("\ufeff").splitlines(keepends=True)
    inside = False
    found = False
    for index, line in enumerate(lines):
        if re.match(r"\s*\[", line):
            inside = line.strip() == "[wsl]"
        if inside and re.match(r"\s*autostart\s*=", line):
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = "autostart = false" + ending
            found = True
    if not found:
        for index, line in enumerate(lines):
            if line.strip() == "[wsl]":
                if not line.endswith("\n"): lines[index] += "\n"
                lines.insert(index + 1, "autostart = false\n")
                found = True
                break
    if not found:
        if lines and not lines[-1].endswith("\n"): lines[-1] += "\n"
        lines.extend(["[wsl]\n", "autostart = false\n"])
    result = "".join(lines)
    if tomllib.loads(result) != expected:
        raise ValueError("User configuration changed beyond autostart")
    return result

def owned_guest(args: list[str], cwd: Path, builds: Path, data: Path) -> str | None:
    if not args: return None
    executable = Path(args[0]).name
    if executable.startswith("python"):
        if "run_agent.py" in args and cwd == data: return "agent"
        for arg in args[1:]:
            if arg.startswith(str(builds) + "/") and arg.endswith("/source/scripts/local_e2e/agent_entry.py"):
                if cwd == data: return "agent"
    if executable in {"node", "nodejs"}:
        if "5313" in args and str(data / "vite.real.config.mjs") in args: return "vite"
        for arg in args[1:]:
            if arg.startswith(str(builds) + "/") and arg.endswith("/source/console/vite.local-e2e.config.mjs"):
                if cwd == Path(arg).parent: return "vite"
    return None

def health_ready(body: dict) -> bool:
    winagent = body.get("winagent")
    return (body.get("ok") is True and body.get("dockerd") is True
            and isinstance(winagent, dict) and winagent.get("online") is True
            and winagent.get("user_agent") is True)
