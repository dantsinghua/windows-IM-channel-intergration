"""Explicit local development inputs. This file contains no workstation identity."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

class ContextError(ValueError):
    def __init__(self, reason: str, field: str):
        super().__init__(f"{reason}: {field}")
        self.reason = reason
        self.field = field

def _windows(value: str, field: str, *, allow_wsl: bool = False) -> str:
    path = PureWindowsPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ContextError("CONTEXT_PATH_INVALID", field)
    if str(path).startswith("\\\\") and not (allow_wsl and str(path).lower().startswith("\\\\wsl.localhost\\")):
        raise ContextError("CONTEXT_NETWORK_PATH_FORBIDDEN", field)
    if not str(path).startswith("\\\\") and not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise ContextError("CONTEXT_DEVICE_PATH_FORBIDDEN", field)
    return str(path)

def _linux(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if not value.startswith("/") or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ContextError("CONTEXT_PATH_INVALID", field)
    return str(path)

def _origin(value: str, field: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port is not None and 1 <= parts.port <= 65535
                 and parts.path in {"", "/"} and not parts.query and not parts.fragment
                 and parts.username is None and parts.password is None)
    except ValueError:
        valid = False
    if not valid:
        raise ContextError("CONTEXT_ORIGIN_INVALID", field)
    return value.rstrip("/")

@dataclass(frozen=True)
class LocalContext:
    build_root: str
    dev_root: str
    service_root: str
    distro: str
    linux_user: str
    linux_python: str
    linux_data: str
    linux_builds: str
    linux_dependencies: str
    windows_python: str
    electron_archive: str
    agent_origin: str
    winagent_origin: str
    vite_origin: str

    @classmethod
    def from_dict(cls, raw: dict) -> "LocalContext":
        if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw.get("schema") != 1:
            raise ContextError("CONTEXT_SCHEMA_INVALID", "schema")
        values = {}
        for field in cls.__dataclass_fields__:
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ContextError("CONTEXT_FIELD_MISSING", field)
            if any(character in value for character in ("\x00", "\r", "\n")):
                raise ContextError("CONTEXT_FIELD_INVALID", field)
            values[field] = value
        for field in ("build_root", "dev_root", "service_root", "windows_python"):
            values[field] = _windows(values[field], field)
        values["electron_archive"] = _windows(values["electron_archive"], "electron_archive", allow_wsl=True)
        for field in ("linux_python", "linux_data", "linux_builds", "linux_dependencies"):
            values[field] = _linux(values[field], field)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", values["distro"]):
            raise ContextError("CONTEXT_DISTRO_INVALID", "distro")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", values["linux_user"]):
            raise ContextError("CONTEXT_USER_INVALID", "linux_user")
        for field in ("agent_origin", "winagent_origin", "vite_origin"):
            values[field] = _origin(values[field], field)
        if len({values[field] for field in ("agent_origin", "winagent_origin", "vite_origin")}) != 3:
            raise ContextError("CONTEXT_PORT_CONFLICT", "origins")
        paths = [PureWindowsPath(values[field]) for field in ("build_root", "dev_root", "service_root")]
        if any(a == b or a in b.parents or b in a.parents for index, a in enumerate(paths) for b in paths[index+1:]):
            raise ContextError("CONTEXT_ROOT_OVERLAP", "roots")
        data, builds = PurePosixPath(values["linux_data"]), PurePosixPath(values["linux_builds"])
        if data == builds or data in builds.parents or builds in data.parents:
            raise ContextError("CONTEXT_ROOT_OVERLAP", "linux_data")
        return cls(**values)

    @property
    def unc(self) -> Path:
        return Path("\\\\wsl.localhost\\" + self.distro)

    def guest_path(self, value: str) -> Path:
        return self.unc.joinpath(*PurePosixPath(_linux(value, "guest_path")).parts[1:])

    def wsl_command(self, *arguments: str) -> list[str]:
        windows = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if not windows:
            raise ContextError("WINDOWS_SYSTEM_ROOT_MISSING", "SystemRoot")
        executable = str(Path(windows) / "System32/wsl.exe")
        return [executable, "-d", self.distro, "--user", self.linux_user, "--exec", self.linux_python, "-B", *arguments]

    def to_dict(self) -> dict:
        return {"schema": 1, **asdict(self)}

    @property
    def identity(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

def load_context(path: str | Path | None = None) -> LocalContext:
    value = path or os.environ.get("QTRADE_LOCAL_CONTEXT")
    if not value:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise ContextError("CONTEXT_MISSING", "--context")
        value = Path(base) / "QTrade/development.json"
    target = Path(value)
    try:
        if target.stat().st_size > 32768:
            raise ContextError("CONTEXT_TOO_LARGE", "configuration")
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ContextError("CONTEXT_MISSING", str(target)) from None
    except PermissionError:
        raise ContextError("CONTEXT_ACCESS_DENIED", str(target)) from None
    except (UnicodeError, json.JSONDecodeError):
        raise ContextError("CONTEXT_FORMAT_INVALID", str(target)) from None
    return LocalContext.from_dict(raw)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--context")
    arguments = parser.parse_args()
    try:
        configuration = load_context(arguments.context)
        print(json.dumps({"valid": True, "schema": 1, "context_id": configuration.identity}))
    except ContextError as error:
        print(json.dumps({"valid": False, "reason": error.reason, "field": error.field}))
        raise SystemExit(2)
