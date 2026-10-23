"""Vault:DPAPI 机器级 + 附加熵、版本、0 填充、审计不记值、熵自检(02 §3.6 #7~#12 / 05 §2.2)。"""
from __future__ import annotations

import os

import pytest

from qtrade_winagent.audit import Audit
from qtrade_winagent.config import VaultConfig
from qtrade_winagent.db import Db
from qtrade_winagent.errors import WaError
from qtrade_winagent.fakes import FakeCrypto
from qtrade_winagent.vault import ENTROPY_BYTES, VAULT_ENTROPY_MISSING, Vault, blob_relpath, entry_name


def mk(tmp_path):
    db = Db(":memory:").open()
    crypto = FakeCrypto()
    v = Vault(db, crypto, VaultConfig(), root=str(tmp_path / "vault"), audit=Audit(db))
    return db, crypto, v


def test_entry_name_strips_scheme():
    assert entry_name("vault://account/qd01") == "account/qd01"
    assert entry_name("mail/hmac/cmd/ops") == "mail/hmac/cmd/ops"      # R6-10:cmd/ 这一段不得省


def test_blob_path_is_sha256_of_name(tmp_path):
    import hashlib
    n = "account/qd01"
    assert blob_relpath(n) == "blobs\\" + hashlib.sha256(n.encode()).hexdigest() + ".bin"


async def test_entropy_is_32_bytes_and_acl_tightened(tmp_path):
    _db, crypto, v = mk(tmp_path)
    e = v.ensure_entropy()
    assert len(e) == ENTROPY_BYTES
    assert ("tighten_acl", v.entropy_path) in crypto.calls
    assert v.startup_selfcheck() is None


async def test_selfcheck_reports_missing_or_loosened_entropy(tmp_path):
    _db, crypto, v = mk(tmp_path)
    assert v.startup_selfcheck()["evidence"]["reason"] == "missing"     # 还没生成
    v.ensure_entropy()
    crypto.loosen_acl(v.entropy_path)
    a = v.startup_selfcheck()
    assert a["code"] == VAULT_ENTROPY_MISSING and a["severity"] == "crit" and a["evidence"]["reason"] == "acl_loosened"
    os.remove(v.entropy_path)
    assert v.startup_selfcheck()["evidence"]["reason"] == "missing"


async def test_put_read_version_and_old_blob_zero_filled(tmp_path):
    db, _c, v = mk(tmp_path)
    assert await v.put("account/qd01", '{"account":"a","secret":"s"}', scope="account") == 1
    old_path = os.path.join(v._root, *blob_relpath("account/qd01").split("\\"))   # noqa: SLF001
    old_bytes = open(old_path, "rb").read()
    assert await v.put("account/qd01", "v2", scope="account") == 2                # 覆盖 = version+1
    assert open(old_path, "rb").read() != old_bytes                               # 旧密文已被换掉
    assert await v.read("account/qd01", trace_id="T") == "v2"
    row = db.one("SELECT * FROM vault_index WHERE name='account/qd01'")
    assert row["version"] == 2 and row["read_count"] == 1 and row["last_read_ms"] is not None


async def test_read_records_audit_without_value_or_length(tmp_path):
    db, _c, v = mk(tmp_path)
    await v.put("account/qd01", "super-secret-value", scope="account")
    await v.read("account/qd01", trace_id="TRACE-1")
    rows = db.query("SELECT * FROM wa_audit_log WHERE action='vault.read'")
    assert rows and rows[0]["trace_id"] == "TRACE-1"
    blob = rows[0]["detail_json"]
    assert "super-secret" not in blob and "len" not in blob and "18" not in blob   # 不记值、不记长度、不记摘要


async def test_delete_keeps_metadata_row_and_zero_fills(tmp_path):
    db, _c, v = mk(tmp_path)
    await v.put("mail/smtp", "pw", scope="mail")
    path = os.path.join(v._root, *blob_relpath("mail/smtp").split("\\"))           # noqa: SLF001
    assert v.delete("mail/smtp") is True
    assert not os.path.exists(path)                                                # blob 0 填充后删
    row = db.one("SELECT * FROM vault_index WHERE name='mail/smtp'")
    assert row is not None and row["deleted_ms"] is not None                       # 元数据留一行
    assert v.exists("mail/smtp") is False
    assert await v.read("mail/smtp", trace_id="T") is None


async def test_list_returns_metadata_only(tmp_path):
    _db, _c, v = mk(tmp_path)
    await v.put("account/qd01", "x", scope="account")
    await v.put("mail/pop3", "y", scope="mail")
    items = v.list(scope="account")
    assert [i["name"] for i in items] == ["account/qd01"]
    assert "value" not in items[0] and set(items[0]) == {"name", "scope", "version", "updated_ms", "last_read_ms",
                                                         "read_count", "suspect", "deleted_ms"}


async def test_flag_marks_suspect(tmp_path):
    db, _c, v = mk(tmp_path)
    await v.put("account/qd01", "x", scope="account")
    assert v.flag("vault://account/qd01", suspect=True) is True
    assert db.one("SELECT suspect FROM vault_index WHERE name='account/qd01'")["suspect"] == 1
    assert v.flag("account/none") is False


async def test_value_size_limit_and_bad_scope(tmp_path):
    _db, _c, v = mk(tmp_path)
    with pytest.raises(WaError) as e:
        await v.put("api/x", "a" * 5000, scope="api")
    assert e.value.reason == "value_too_large"
    with pytest.raises(WaError) as e2:
        await v.put("api/x", "a", scope="不存在的类别")
    assert e2.value.reason == "bad_scope"


async def test_blob_tamper_is_detected(tmp_path):
    _db, _c, v = mk(tmp_path)
    await v.put("api/ops", "k", scope="api")
    path = os.path.join(v._root, *blob_relpath("api/ops").split("\\"))             # noqa: SLF001
    with open(path, "wb") as f:
        f.write(b"FAKEDPAPI1" + b"\x00" * 8 + b"zzz")
    with pytest.raises(WaError) as e:
        await v.read("api/ops", trace_id="T")
    assert e.value.reason == "blob_tampered"


async def test_wrong_entropy_cannot_decrypt(tmp_path):
    """05 §2.2.5:熵丢失 = 全库不可解(等同机器迁移),**不是缺陷,是 DPAPI 性质**。"""
    _db, crypto, v = mk(tmp_path)
    await v.put("api/ops", "k", scope="api")
    with open(v.entropy_path, "wb") as f:
        f.write(crypto.random_bytes(32))
    v._entropy = None                                                              # noqa: SLF001 —— 强制重读熵
    with pytest.raises(ValueError):
        await v.read("api/ops", trace_id="T")
