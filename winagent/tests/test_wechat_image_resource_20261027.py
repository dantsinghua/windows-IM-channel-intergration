"""Isolated resource-root contract; never starts Chatlog or reads real keys."""
import base64
import io
import json
import os
from pathlib import Path
from urllib.parse import unquote

import pytest

from qtrade_winagent.win import wechat as native

MD5 = "a1" * 16
RELATIVE = "msg\\attach\\" + "b2" * 16 + "\\2026-10\\Img\\" + "c3" * 16
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def resource_handle(uid="wxid_synthetic", path=RELATIVE):
    value = json.dumps({"uid":uid,"md5":MD5,"path":path}, separators=(",", ":"))
    return "wximage-v1." + base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def media_backend(tmp_path, monkeypatch, payload=PNG):
    account = tmp_path / "wxid_synthetic"
    account.mkdir()
    backend = native.WinWeChat(chatlog_port=5030)
    identity = ("wxid_synthetic", 71, 100.0, 72, 101.0, str(account))
    backend._server_account = (identity[0], str(account))
    monkeypatch.setattr(backend, "_send_identity", lambda: identity)
    monkeypatch.setattr(backend, "current_wxid", lambda: identity[0])
    monkeypatch.setattr(backend, "_account_dir", lambda: str(account))
    calls = []
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "image/png"}
    def fake_open(url, **kwargs):
        calls.append(url)
        return Response(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    return backend, identity, calls


@pytest.mark.parametrize("layout", ["db_storage", "Msg"])
def test_server_resource_root_includes_database_and_attachment_siblings(tmp_path, monkeypatch, layout):
    data_root = tmp_path / "Saved Files"
    account = data_root / "xwechat_files" / "wxid_synthetic"
    database = account / layout / "message_0.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"synthetic-db")
    picture = account / "msg" / "attach" / "synthetic" / "Img" / "picture_h.dat"
    picture.parent.mkdir(parents=True)
    picture.write_bytes(b"synthetic-encrypted-image")
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    backend = native.WinWeChat(chatlog_dir=str(tmp_path / "package"),
                               chatlog_work_dir=str(tmp_path / "work"))
    monkeypatch.setattr(backend, "locate", lambda: {"data_root": str(data_root)})
    monkeypatch.setattr(backend, "current_wxid", lambda: "wxid_synthetic")
    keys = {"data_key": "a" * 64, "img_key": "b" * 32}
    monkeypatch.setattr(native, "read_chatlog_keys", lambda: dict(keys))
    expand = os.path.expanduser
    monkeypatch.setattr(native.os.path, "expanduser", lambda path: str(isolated_home) if path == "~" else expand(path))
    assert native.discover_chatlog_data_dir(str(data_root), "wxid_synthetic") == str(database.parent)
    executable = backend._prepare_server()
    config = json.loads((isolated_home / ".chatlog" / "chatlog-server.json").read_text())
    configured_root = Path(config["data_dir"])
    assert configured_root.resolve() == account.resolve(), "Server resource paths are relative to the account, not its DB subdirectory"
    assert (configured_root / picture.relative_to(account)).read_bytes() == b"synthetic-encrypted-image"
    assert (configured_root / database.relative_to(account)).read_bytes() == b"synthetic-db"
    assert config["data_key"] == keys["data_key"] and config["img_key"] == keys["img_key"]
    assert keys["data_key"] not in executable and keys["img_key"] not in executable
    assert backend._server_account[0] == "wxid_synthetic"


def test_missing_current_account_does_not_fall_back_to_another_accounts_database(tmp_path, monkeypatch):
    data_root = tmp_path / "Saved Files"
    other = data_root / "xwechat_files" / "wxid_other" / "db_storage"
    other.mkdir(parents=True)
    (other / "message_0.db").write_bytes(b"other-db")
    home = tmp_path / "isolated-home"
    home.mkdir()
    backend = native.WinWeChat(chatlog_dir=str(tmp_path / "package"),
                               chatlog_work_dir=str(tmp_path / "work"))
    monkeypatch.setattr(backend, "locate", lambda: {"data_root": str(data_root)})
    monkeypatch.setattr(backend, "current_wxid", lambda: "wxid_missing")
    monkeypatch.setattr(native, "read_chatlog_keys", lambda: {"data_key": "a"*64,"img_key":"b"*32})
    expand = os.path.expanduser
    monkeypatch.setattr(native.os.path, "expanduser", lambda path: str(home) if path == "~" else expand(path))
    with pytest.raises(RuntimeError):
        backend._prepare_server()
    assert not (home / ".chatlog" / "chatlog-server.json").exists()


async def test_owned_resource_returns_complete_original_bytes(tmp_path, monkeypatch):
    backend, _, calls = media_backend(tmp_path, monkeypatch)
    result = await backend.media(resource_handle())
    assert result == PNG
    assert len(calls) == 1 and calls[0].startswith("http://127.0.0.1:5030/image/")
    assert MD5 in unquote(calls[0])


async def test_another_account_resource_is_rejected_before_reading_http(tmp_path, monkeypatch):
    backend, _, calls = media_backend(tmp_path, monkeypatch)
    with pytest.raises((ValueError, native.WaError)):
        await backend.media(resource_handle(uid="wxid_other"))
    assert calls == []


@pytest.mark.parametrize("relative", ["../../outside", r"C:\outside\image", r"\\server\share\image",
    "msg\\attach\\..\\outside", "msg\\attach\\%2e%2e\\image", "msg\\attach\\image\n"])
async def test_native_resource_path_cannot_escape_the_current_account(tmp_path, monkeypatch, relative):
    backend, _, calls = media_backend(tmp_path, monkeypatch)
    with pytest.raises((ValueError, native.WaError)):
        await backend.media(resource_handle(path=relative))
    assert calls == []


@pytest.mark.parametrize("payload", [b'{"error":"File not found"}', b"<html>temporary failure</html>",
    bytes.fromhex("07085632080700040000") + b"encrypted-data", PNG[:24], b""])
async def test_successful_http_without_decodable_image_is_not_a_successful_media_read(tmp_path, monkeypatch, payload):
    backend, _, calls = media_backend(tmp_path, monkeypatch, payload)
    with pytest.raises((ValueError, native.WaError)):
        await backend.media(resource_handle())
    assert len(calls) == 1


async def test_identity_change_during_image_read_does_not_return_previous_accounts_bytes(tmp_path, monkeypatch):
    backend, identity, calls = media_backend(tmp_path, monkeypatch)
    observed = iter([identity, ("wxid_other", *identity[1:])])
    monkeypatch.setattr(backend, "_send_identity", lambda: next(observed))
    with pytest.raises((ValueError, native.WaError)):
        await backend.media(resource_handle())
    assert len(calls) == 1
