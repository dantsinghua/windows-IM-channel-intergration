"""询价工作台通讯录:UTF-16 名称解码 + GET /accounts/{id}/contacts。"""
from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from qtrade_agent.adapters.qidian.contacts import list_private_contacts
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.xor import cxor, xor
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig
from tests.conftest import Clock


def _blob_name(text: str) -> bytes:
    return cxor(text).encode("utf-8")


def _blob_uin(uin: str) -> bytes:
    return xor(uin.encode("ascii"))


def test_cxor_roundtrip_chinese():
    for s in ("李静", "国泰君安", ""):
        assert cxor(cxor(s)) == s


def test_contacts_prefer_remark_and_keep_external_nick(tmp_path):
    path = tmp_path / "3007373675.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Friends (uin BLOB, remark BLOB, name BLOB)")
    con.execute("CREATE TABLE QidianExternalInfo (uin BLOB, nickname BLOB)")
    con.execute("INSERT INTO Friends VALUES (?,?,?)", (_blob_uin("415011447"), _blob_name("国泰君安"), _blob_name("李静")))
    con.execute("INSERT INTO QidianExternalInfo VALUES (?,?)", (_blob_uin("10001"), _blob_name("外部")))
    con.commit()
    con.close()
    rows = LocalSqliteMainDb(str(path)).list_contact_source_rows()
    data = list_private_contacts(rows)
    by_uin = {row["uin"]: row for row in data}
    assert by_uin["415011447"] == {"uin": "415011447", "name": "国泰君安", "remark": "国泰君安", "kind": "private"}
    assert by_uin["10001"] == {"uin": "10001", "name": "外部", "remark": "", "kind": "private"}
    assert all(not row["uin"].startswith("qd") for row in data)


def test_contacts_api_503_when_not_running_and_200_when_db_has_friends(tmp_path):
    clock = Clock()
    db_path = tmp_path / "3007373675.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE Friends (uin BLOB, remark BLOB, name BLOB)")
    con.execute("CREATE TABLE QidianExternalInfo (uin BLOB, nickname BLOB)")
    con.execute("INSERT INTO Friends VALUES (?,?,?)", (_blob_uin("415011447"), b"", _blob_name("李静")))
    con.commit()
    con.close()
    agent = AgentApp(AgentConfig(), db_path=str(tmp_path / "agent.db"), clock=clock,
                     maindb_factory=lambda uid, acct: LocalSqliteMainDb(str(db_path))).open()
    agent.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675", label="现券A台")
    agent.store.ensure_account("qd02", "qidian", state="login_required", self_uid="222", label="掉线")
    agent.store.upsert_api_client(app_id="panel", name="询价", level="read", token="panel-token")
    api = agent.create_api()
    try:
        with TestClient(api, client=("127.0.0.1", 40000)) as client:
            headers = {"Authorization": "Bearer panel-token", "X-QT-Api-Min": "1.0"}
            off = client.get("/api/v1/accounts/qd02/contacts", headers=headers)
            assert off.status_code == 503
            assert off.json()["ok"] is False
            ok = client.get("/api/v1/accounts/qd01/contacts", headers=headers)
            assert ok.status_code == 200, ok.text
            body = ok.json()
            assert body["ok"] is True
            assert body["data"] == [{"uin": "415011447", "name": "李静", "remark": "", "kind": "private"}]
            assert "trace_id" in body
    finally:
        agent.store.close()
