"""Installer-only CLI paths use fixture files and must not start the Agent runtime."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tomllib
from unittest.mock import Mock

import pytest

from qtrade_agent import main


@pytest.fixture(autouse=True)
def no_agent_runtime(monkeypatch):
    build = Mock(side_effect=AssertionError("bootstrap must not create AgentApp or start its backends"))
    monkeypatch.setattr(main, "build", build)
    yield
    build.assert_not_called()


def fixture_contents(tmp_path):
    content = {}
    for key, data in [("redroid_image", b"redroid-fixture"), ("napcat_image", b"napcat-fixture"), ("qidian_apk", b"apk-fixture")]:
        path = tmp_path / (key + ".bin")
        path.write_bytes(data)
        entry = {"sha256": hashlib.sha256(data).hexdigest()}
        if key == "qidian_apk":
            entry.update(path=str(path), version="6.9.7")
        else:
            entry.update(tar=str(path), ref=f"fixture/{key}:portable")
        content[key] = entry
    path = tmp_path / "contents.json"
    path.write_text(json.dumps(content))
    return path, content


def test_runtime_bootstrap_uses_verified_bundle_and_preserves_unrelated_config(tmp_path):
    path, contents = fixture_contents(tmp_path)
    config = tmp_path / "agent.toml"
    config.write_text('[messages]\ncapture_text = false\n\n[runtime]\naccounts_dir = "/fixture/accounts"\n')
    assert main.main(["--bootstrap-runtime-contents", str(path), "--config", str(config)]) == 0
    value = tomllib.loads(config.read_text())
    assert value["messages"]["capture_text"] is False
    assert value["runtime"]["accounts_dir"] == "/fixture/accounts"
    assert value["runtime"]["redroid_image"] == contents["redroid_image"]["ref"]
    assert value["runtime"]["napcat_image"] == contents["napcat_image"]["ref"]
    assert value["runtime"]["apk_url"] == (tmp_path / "qidian_apk.bin").as_uri()
    assert value["runtime"]["apk_sha256"] == contents["qidian_apk"]["sha256"]


@pytest.mark.parametrize("key", ["redroid_image", "napcat_image", "qidian_apk"])
@pytest.mark.parametrize("failure", ["missing-hash", "mismatch", "missing-file"])
def test_invalid_bundle_leaves_previous_config_unchanged(tmp_path, key, failure):
    path, contents = fixture_contents(tmp_path)
    if failure == "missing-hash": contents[key].pop("sha256")
    elif failure == "mismatch": contents[key]["sha256"] = "0" * 64
    else: (tmp_path / (key + ".bin")).unlink()
    path.write_text(json.dumps(contents))
    config = tmp_path / "agent.toml"
    original = '[messages]\ncapture_text = false\n'
    config.write_text(original)
    assert main.main(["--bootstrap-runtime-contents", str(path), "--config", str(config)]) != 0
    assert config.read_text() == original


def seed_token(monkeypatch, path, token):
    monkeypatch.setattr(main.sys, "stdin", io.StringIO(token))
    return main.main(["--bootstrap-console-token-stdin", "--db", str(path)])


def test_console_token_is_hashed_and_same_token_bootstrap_is_idempotent(monkeypatch, tmp_path, capsys):
    db = tmp_path / "agent.db"
    token = "A" * 64
    assert seed_token(monkeypatch, db, token) == 0
    assert seed_token(monkeypatch, db, token) == 0
    with sqlite3.connect(db) as connection:
        row = connection.execute("SELECT secret_hash FROM api_clients WHERE app_id='console'").fetchall()
    assert row == [(hashlib.sha256(token.encode()).hexdigest(),)]
    assert token not in capsys.readouterr().out
    assert token.encode() not in db.read_bytes()


def test_existing_different_console_token_is_not_silently_rotated(monkeypatch, tmp_path):
    db = tmp_path / "agent.db"
    assert seed_token(monkeypatch, db, "A" * 64) == 0
    assert seed_token(monkeypatch, db, "B" * 64) != 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT secret_hash FROM api_clients WHERE app_id='console'").fetchone()[0] == hashlib.sha256(b"A" * 64).hexdigest()


@pytest.mark.parametrize("token", ["", "short", "x" * 4097, "A" * 64 + "\n" + "B" * 64])
def test_missing_malformed_or_oversized_console_token_is_rejected(monkeypatch, tmp_path, token, capsys):
    db = tmp_path / "agent.db"
    assert seed_token(monkeypatch, db, token) != 0
    assert not db.exists()
    captured = capsys.readouterr()
    if token:
        assert token not in captured.out + captured.err
