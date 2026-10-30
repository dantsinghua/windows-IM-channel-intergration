"""Baseline mail-column invariants remain valid after additive identity migrations."""
import hashlib
from pathlib import Path
import sqlite3

import pytest

from qtrade_agent.store import Store, StoreCorrupt, code_schema_version
from qtrade_agent.store.store import BASELINE_NAME, SCHEMA_PATH


def baseline_database(path):
    source = Path(SCHEMA_PATH).read_text()
    with sqlite3.connect(path) as db:
        db.executescript(source)
        db.execute('INSERT INTO schema_version(version,name,applied_ms,checksum) VALUES(1,?,?,?)',
                   (BASELINE_NAME, 1, hashlib.sha256(source.encode()).hexdigest()))
    db.close()


@pytest.mark.parametrize('version', [1, 2])
def test_missing_baseline_mail_column_rejected_without_mutating_existing_database(tmp_path, version):
    path = tmp_path/'schema.db'
    if version == 1:
        baseline_database(path)
    else:
        Store(str(path)).open().close()
        assert code_schema_version() == 3
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE mail_outbox DROP COLUMN render_notes')
    db.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(StoreCorrupt, match='mail_outbox lacks required columns'):
        Store(str(path)).open()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_valid_baseline_database_still_upgrades_to_identity_epoch_schema(tmp_path):
    path = tmp_path/'valid-baseline.db'
    baseline_database(path)
    store = Store(str(path)).open()
    try:
        assert store.schema_version() == code_schema_version() == 3
        assert 'render_notes' in {row[1] for row in store.con.execute('PRAGMA table_info(mail_outbox)')}
        assert {'identity_epoch', 'reply_json'} <= {row[1] for row in store.con.execute('PRAGMA table_info(messages)')}
    finally:
        store.close()
