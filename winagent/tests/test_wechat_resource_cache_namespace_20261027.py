"""Changing resource roots must never mix decrypted database layouts."""
import json
import os
from pathlib import Path

import pytest

from qtrade_winagent.win import wechat as native


def setup_backend(tmp_path,monkeypatch):
    current={"root":tmp_path/"saved-a","uid":"wxid_synthetic"}
    work=tmp_path/"reader-work";home=tmp_path/"isolated-home";home.mkdir()
    def database(root,uid):
        path=root/"xwechat_files"/uid/"db_storage"/"message_0.db"
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b"synthetic-db")
    database(current["root"],current["uid"])
    backend=native.WinWeChat(chatlog_dir=str(tmp_path/"package"),chatlog_work_dir=str(work))
    monkeypatch.setattr(backend,"locate",lambda:{"data_root":str(current["root"])})
    monkeypatch.setattr(backend,"current_wxid",lambda:current["uid"])
    monkeypatch.setattr(native,"read_chatlog_keys",lambda:{"data_key":"a"*64,"img_key":"b"*32})
    original=os.path.expanduser
    monkeypatch.setattr(native.os.path,"expanduser",lambda p:str(home) if p=="~" else original(p))
    def prepare():
        backend._prepare_server()
        return json.loads((home/".chatlog"/"chatlog-server.json").read_text())
    return current,work,database,prepare


def test_account_resource_root_does_not_reuse_or_delete_legacy_db_only_cache(tmp_path,monkeypatch):
    current,work,database,prepare=setup_backend(tmp_path,monkeypatch)
    old=work/"decrypted"/current["uid"];old.mkdir(parents=True)
    sentinel=old/"legacy-layout.db";sentinel.write_bytes(b"old-cache-must-survive")
    config=prepare();fresh=Path(config["work_dir"])
    assert fresh.resolve()!=old.resolve(),"A root change cannot rescan the old decrypted database layout"
    assert fresh.resolve().is_relative_to(work.resolve()) and fresh.is_dir()
    assert sentinel.read_bytes()==b"old-cache-must-survive"
    assert not list(fresh.rglob("legacy-layout.db")),"Old cache data must not be copied into the new namespace"


def test_same_uid_at_different_resource_root_gets_an_independent_cache(tmp_path,monkeypatch):
    current,work,database,prepare=setup_backend(tmp_path,monkeypatch)
    first=prepare();cache=Path(first["work_dir"])
    marker=cache/"first-root.db";marker.write_bytes(b"first-root-cache")
    current["root"]=tmp_path/"saved-b";database(current["root"],current["uid"])
    second=prepare()
    assert Path(second["work_dir"]).resolve()!=cache.resolve()
    assert marker.read_bytes()==b"first-root-cache"
    assert not (Path(second["work_dir"])/marker.name).exists()


def test_repeated_preparation_of_same_normalized_root_keeps_cache_identity(tmp_path,monkeypatch):
    current,work,database,prepare=setup_backend(tmp_path,monkeypatch)
    first=prepare();marker=Path(first["work_dir"])/"current.db";marker.write_bytes(b"keep-current-cache")
    current["root"]=str(current["root"])+os.sep+"."
    second=prepare()
    assert Path(first["work_dir"]).resolve()==Path(second["work_dir"]).resolve()
    assert marker.read_bytes()==b"keep-current-cache"
    assert Path(second["data_dir"]).name==current["uid"]


def test_unavailable_current_uid_cannot_reuse_previous_accounts_cache(tmp_path,monkeypatch):
    current,work,database,prepare=setup_backend(tmp_path,monkeypatch)
    first=prepare();marker=Path(first["work_dir"])/"current.db";marker.write_bytes(b"preserved")
    current["uid"]="wxid_absent"
    with pytest.raises((ValueError,RuntimeError)):
        prepare()
    assert marker.read_bytes()==b"preserved"
