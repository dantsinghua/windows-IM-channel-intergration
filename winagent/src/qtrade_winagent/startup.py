"""Bootstrap diagnostics safe before logging, configuration, or Vault is ready."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

def startup_diagnostic(error: BaseException, stage: str) -> dict:
    code = getattr(error, "winerror", None) or getattr(error, "errno", 0) or 0
    reason = {5: "ACCESS_DENIED", 13: "ACCESS_DENIED", 126: "NATIVE_DEPENDENCY_MISSING",
              193: "EXECUTABLE_ARCHITECTURE_INVALID", 577: "EXECUTABLE_SIGNATURE_REJECTED",
              10048: "PORT_IN_USE", 98: "PORT_IN_USE", 1260: "EXECUTION_POLICY_BLOCKED"}.get(code, "STARTUP_EXCEPTION")
    if isinstance(error, (tomllib.TOMLDecodeError, UnicodeError)):
        reason = "CONFIG_INVALID"
    elif isinstance(error, FileNotFoundError):
        reason = "CONFIG_MISSING" if stage == "configuration" else "DEPENDENCY_FILE_MISSING"
    elif isinstance(error, PermissionError):
        reason = "ACCESS_DENIED"
    elif isinstance(error, TimeoutError):
        reason = "STARTUP_TIMEOUT"
    elif isinstance(error, sqlite3.Error):
        reason = getattr(error, "sqlite_errorname", None) or "DATABASE_STARTUP_FAILED"
    return {"success": False, "time": time.time(), "pid": os.getpid(), "stage": stage,
            "reason": reason, "native_code": code, "error_type": type(error).__name__}

def record_startup_failure(root: str, report: dict) -> None:
    text = json.dumps(report, ensure_ascii=True)
    if sys.stderr is not None:
        try:
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
        except OSError:
            pass
    try:
        base = Path(root)
        if not base.is_dir() or base.is_symlink():
            return
        directory = base / "logs"
        if directory.is_symlink():
            return
        directory.mkdir(exist_ok=True)
        temporary = directory / ("startup-failure." + str(os.getpid()) + ".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        temporary.replace(directory / "startup-failure.json")
    except OSError:
        # The service manager still gets a nonzero initialization result.
        pass
