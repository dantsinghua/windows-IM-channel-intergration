"""Offline installer operations. These paths never start the Agent or adapters."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import tomllib


def _checked_asset(node: dict, key: str) -> Path:
    path = Path(node[key])
    expected = node.get("sha256", "")
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("invalid bundled asset metadata")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("bundled asset hash mismatch")
    return path


def configure_runtime(contents_path: str, config_path: str) -> None:
    """Validate the offline payload and update only its four managed TOML keys."""
    contents = json.loads(Path(contents_path).read_text(encoding="utf-8"))
    values: dict[str, str] = {}
    for key in ("redroid_image", "napcat_image"):
        node = contents[key]
        _checked_asset(node, "tar")
        ref = node["ref"]
        if not isinstance(ref, str) or not ref or any(c.isspace() for c in ref):
            raise ValueError("invalid bundled image reference")
        values[key] = ref
    apk = contents["qidian_apk"]
    values["apk_url"] = _checked_asset(apk, "path").as_uri()
    values["apk_sha256"] = apk["sha256"]
    path = Path(config_path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    current = tomllib.loads(text)
    runtime = current.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("invalid runtime configuration")
    # Preserve unrelated text, including tables not understood by this version.
    # Only the standard [runtime] table is writable; unusual dotted/inline forms
    # fail closed instead of silently dropping a user's configuration.
    section = re.search(r"(?m)^\[runtime\][ \t]*(?:#.*)?$", text)
    if "runtime" in current and section is None:
        raise ValueError("runtime must use a standard TOML table")
    if section:
        following = re.search(r"(?m)^\[", text[section.end():])
        end = section.end() + following.start() if following else len(text)
        body = text[section.end():end]
        for key, value in values.items():
            expression = rf"(?m)^{key}[ \t]*=[^\n]*(?:\n|$)"
            if key in runtime and re.search(expression, body) is None:
                raise ValueError("unsupported managed runtime key syntax")
            body = re.sub(expression, "", body)
        text = text[:section.end()] + "\n" + "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items()
        ) + body + text[end:]
    else:
        text += "\n[runtime]\n" + "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items()
        )
    parsed = tomllib.loads(text)
    expected = dict(current)
    expected["runtime"] = {**runtime, **values}
    if parsed != expected:
        raise ValueError("runtime update would alter unrelated configuration")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def bind_console_token(cfg, db_path: str | None, token: str) -> None:
    """Seed the one built-in console identity; never replace an existing secret."""
    from .store import Store

    token = token.rstrip("\r\n")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,4096}", token):
        raise ValueError("invalid console token")
    path = db_path or cfg.db.path
    Path(path).parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    store = Store(path, out_merge_window_s=cfg.bus.out_merge_window_s, capture_text=cfg.messages.capture_text)
    secret_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    try:
        store.open()
        with store._tx() as connection:
            existing = connection.execute("SELECT * FROM api_clients WHERE app_id='console'").fetchone()
            if existing:
                if (existing["secret_hash"] != secret_hash or existing["auth_kind"] != "bearer"
                        or existing["level"] != "admin" or not existing["enabled"]
                        or existing["revoked_ms"] is not None):
                    raise ValueError("console identity already exists with different credentials")
                return
            now = int(time.time() * 1000)
            connection.execute(
                "INSERT INTO api_clients(app_id,name,auth_kind,secret_hash,secret_ref,level,"
                "allow_accounts_json,created_ms,updated_ms) VALUES('console','QTrade Console',"
                "'bearer',?,'vault://winagent/console_token','admin','[\"*\"]',?,?)",
                (secret_hash, now, now),
            )
    finally:
        store.close()
