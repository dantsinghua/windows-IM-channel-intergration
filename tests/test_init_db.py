"""``python -m qtrade_agent.main --init-db``(03 §2.7.3 ②(e) 首启建库)—— 对照 02 §2.1 步 4、§3.3 迁移、§3.8 版本关系、§2.6 损坏判据。

覆盖:全新建库 / 已最新幂等 / 版本落后跑迁移 / 库损坏拒绝且不动原文件 / 版本比代码新拒绝 /
建库后紧接正常启动不再建表 / 真子进程冒烟(退出码 + 表数)。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

import qtrade_agent
from qtrade_agent.config import AgentConfig
from qtrade_agent.main import EXIT_DB_CORRUPT, EXIT_INIT_FAILED, EXIT_OK, EXIT_SCHEMA_TOO_NEW, init_db, main
from qtrade_agent.store import Store, store as store_mod

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(qtrade_agent.__file__)))


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def table_count(path: str) -> int:
    s = Store(path).open()
    try:
        return s.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        s.close()


# ────────────────────────────────────────────── ① 全新
def test_init_db_fresh_creates_parent_dir_and_schema(tmp_path):
    db = tmp_path / "var" / "lib" / "qtrade" / "agent.db"
    assert init_db(AgentConfig(), str(db)) == EXIT_OK
    assert db.exists()
    # 父目录不存在时建出来;02/03 无显式 mode 约定 ⇒ 取最小权限 0750(main.DB_DIR_MODE)
    assert os.stat(db.parent).st_mode & 0o777 == 0o750
    s = Store(str(db)).open()
    try:
        assert s.schema_version() == 1
        rows = s.con.execute("SELECT version, name, checksum FROM schema_version").fetchall()
        assert [r["version"] for r in rows] == [1] and rows[0]["name"] == "0001_baseline_docs02_v0.4.6"
        assert rows[0]["checksum"] == hashlib.sha256(open(store_mod.SCHEMA_PATH, encoding="utf-8").read().encode()).hexdigest()
        # 02 §2.8.4:auto_vacuum=INCREMENTAL 只在建库时能设 —— 与正常启动路径同一组 PRAGMA
        assert s.con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert s.con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert s.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] >= 27
    finally:
        s.close()


def test_init_db_does_not_start_http_or_scheduler(tmp_path, monkeypatch):
    """``--init-db`` 只建库:不进 ``build()``、不 ``uvicorn.run``(不起 HTTP/调度器/外部依赖)。"""
    import qtrade_agent.main as main_mod
    monkeypatch.setattr(main_mod, "build", lambda *a, **k: pytest.fail("--init-db 不该构建 AgentApp"))
    assert main(["--init-db", "--db", str(tmp_path / "agent.db"), "--config", str(tmp_path / "nope.toml")]) == EXIT_OK


# ────────────────────────────────────────────── ② 已最新 ⇒ 幂等
def test_init_db_idempotent_when_current(tmp_path):
    db = str(tmp_path / "agent.db")
    assert init_db(AgentConfig(), db) == EXIT_OK
    s = Store(db).open()
    before = s.con.execute("SELECT version, name, applied_ms, checksum FROM schema_version").fetchall()
    n_tables = s.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    s.settings_set("seq.qq", 7)                       # 已有业务数据不得被重建冲掉
    s.close()

    assert init_db(AgentConfig(), db) == EXIT_OK

    s = Store(db).open()
    try:
        after = s.con.execute("SELECT version, name, applied_ms, checksum FROM schema_version").fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]     # 一行不增、applied_ms 不变
        assert s.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == n_tables
        assert s.settings_get("seq.qq") == 7
    finally:
        s.close()


# ────────────────────────────────────────────── ③ 版本落后 ⇒ 跑迁移
def test_init_db_applies_pending_migrations(tmp_path, monkeypatch):
    db = str(tmp_path / "agent.db")
    assert init_db(AgentConfig(), db) == EXIT_OK                            # 先建到基线 v1

    mig = tmp_path / "migrations"
    mig.mkdir()
    sql = "CREATE TABLE demo_0002 (id INTEGER PRIMARY KEY) STRICT;\nCREATE INDEX ix_demo_0002 ON demo_0002(id);"
    (mig / "0002_demo.sql").write_text(sql, encoding="utf-8")
    (mig / "0001_baseline_docs02_v0.4.6.sql").write_text("SELECT 1;", encoding="utf-8")   # 基线编号不当迁移重跑
    monkeypatch.setattr(store_mod, "MIGRATIONS_DIR", str(mig))
    assert store_mod.code_schema_version() == 2

    assert init_db(AgentConfig(), db) == EXIT_OK

    s = Store(db).open()
    try:
        assert s.schema_version() == 2
        row = s.con.execute("SELECT name, checksum FROM schema_version WHERE version=2").fetchone()
        assert row["name"] == "0002_demo" and row["checksum"] == hashlib.sha256(sql.encode()).hexdigest()
        assert s.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='demo_0002'").fetchone()[0] == 1
    finally:
        s.close()
    assert init_db(AgentConfig(), db) == EXIT_OK                            # 迁完再跑仍幂等
    s = Store(db).open()
    try:
        assert s.con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 2
    finally:
        s.close()


# ────────────────────────────────────────────── ④ 损坏 / 不是 SQLite ⇒ 拒绝且不动原文件
@pytest.mark.parametrize("payload", [b"not a sqlite file, just junk\n", b"SQLite format 3\x00" + b"\x00" * 200])
def test_init_db_rejects_corrupt_db_without_touching_it(tmp_path, payload, caplog):
    db = tmp_path / "agent.db"
    db.write_bytes(payload)
    before = sha256(str(db))
    assert init_db(AgentConfig(), str(db)) == EXIT_DB_CORRUPT
    assert db.exists() and sha256(str(db)) == before                        # 既不覆盖也不删除
    assert not (tmp_path / "agent.db-wal").exists()
    assert "备份" in caplog.text                                            # 中文错误信息指向 §3.3 备份


# ────────────────────────────────────────────── ⑤ 版本比代码新 ⇒ 拒绝
def test_init_db_rejects_schema_newer_than_code(tmp_path):
    db = str(tmp_path / "agent.db")
    assert init_db(AgentConfig(), db) == EXIT_OK
    s = Store(db).open()
    s.con.execute("INSERT INTO schema_version(version, name, applied_ms, checksum) VALUES (99, '0099_future', 0, 'x')")
    s.close()
    before = sha256(db)

    assert init_db(AgentConfig(), db) == EXIT_SCHEMA_TOO_NEW
    assert sha256(db) == before                                            # 降级不支持:库一字不改(02 §3.8)
    with pytest.raises(store_mod.SchemaTooNew, match="99"):
        Store(db).open()                                                   # 正常启动路径同样拒绝


def test_init_db_parent_dir_not_creatable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("我是个文件,不是目录", encoding="utf-8")
    assert init_db(AgentConfig(), str(blocker / "agent.db")) == EXIT_INIT_FAILED


# ────────────────────────────────────────────── ⑥ 建库后紧接正常启动
def test_normal_open_after_init_db_creates_nothing(tmp_path):
    """firstboot 的真实次序:先 ``--init-db``,再 ``systemctl enable --now qtrade-agent``(同一套 ``Store.open()``)。"""
    db = str(tmp_path / "agent.db")
    assert init_db(AgentConfig(), db) == EXIT_OK
    n = table_count(db)
    s = Store(db).open()                                                   # = AgentApp.open() 的第一步
    try:
        assert s.schema_version() == 1
        assert s.con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
        assert s.con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == n
        assert s.con.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'seq.%'").fetchone()[0] == 3
    finally:
        s.close()


def test_init_db_on_db_created_by_normal_startup(tmp_path):
    """反向:Agent 自己建的库,再跑 ``--init-db`` 也幂等(firstboot 可重跑)。"""
    db = str(tmp_path / "agent.db")
    Store(db).open().close()
    assert init_db(AgentConfig(), db) == EXIT_OK
    s = Store(db).open()
    try:
        assert s.con.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    finally:
        s.close()


# ────────────────────────────────────────────── ⑦ 真子进程冒烟(firstboot 逐字那一行)
def test_subprocess_init_db_smoke(tmp_path):
    db = tmp_path / "var" / "lib" / "qtrade" / "agent.db"
    env = {**os.environ, "PYTHONPATH": SRC_DIR}
    cmd = [sys.executable, "-m", "qtrade_agent.main", "--init-db", "--db", str(db),
           "--config", str(tmp_path / "absent.toml")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert db.exists()
    assert table_count(str(db)) >= 27
    r2 = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)   # 幂等重跑
    assert r2.returncode == 0, r2.stderr


def test_subprocess_help_mentions_init_db():
    r = subprocess.run([sys.executable, "-m", "qtrade_agent.main", "--help"],
                       env={**os.environ, "PYTHONPATH": SRC_DIR}, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "--init-db" in r.stdout
