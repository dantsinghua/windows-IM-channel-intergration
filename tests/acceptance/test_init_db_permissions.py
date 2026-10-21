"""Independent filesystem permission contract for database initialization.

Each umask probe runs in its own Python child; the parent process, service
configuration and existing application data are never changed.
"""
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "src"
PROBE = """
import os
import sys
from qtrade_agent.config import AgentConfig
from qtrade_agent.main import init_db
mask = int(sys.argv[2], 8)
original = os.umask(mask)
try:
    result = init_db(AgentConfig(), sys.argv[1])
    assert os.umask(mask) == mask, 'init_db changed the process umask'
    assert result == 0, result
    print('INIT_DB_PROBE_OK')
finally:
    os.umask(original)
"""


def initialize(path, mask):
    env = dict(os.environ, PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", PROBE, str(path), oct(mask)],
                            cwd=path.parents[1], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "INIT_DB_PROBE_OK" in result.stdout
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0


@pytest.mark.parametrize("mask", [0o077, 0o022])
def test_new_database_parent_is_0750_under_strict_or_standard_umask(tmp_path, mask):
    folder = tmp_path / "new-application-state"
    initialize(folder / "agent.db", mask)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o750


@pytest.mark.parametrize("existing_mode", [0o700, 0o755])
def test_existing_database_parent_keeps_operator_permissions(tmp_path, existing_mode):
    folder = tmp_path / "existing-application-state"
    folder.mkdir()
    folder.chmod(existing_mode)
    initialize(folder / "agent.db", 0o077)
    assert stat.S_IMODE(folder.stat().st_mode) == existing_mode
