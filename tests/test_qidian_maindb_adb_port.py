"""主库旁路读取必须连接项目ADB server，所有subprocess均由本文件假件拦截。"""
from __future__ import annotations

import subprocess

import pytest

from qtrade_agent.adapters.qidian.maindb import AdbMainDb


SERIAL = "127.0.0.1:16001"
UID = "3000008246"
TABLE = "mr_friend_00000000000000000000000000000001_New"


@pytest.fixture
def isolated_adb(monkeypatch):
    from qtrade_agent.adapters.qidian import maindb
    calls = []

    def build(*, port=16000, serial=SERIAL, executable="test-only-adb", stdout="1\n"):
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            assert isinstance(argv, list), "必须以argv执行单个adb程序，不能交给宿主shell解释"
            assert not kwargs.get("shell", False)
            if argv[:6] != [executable, "-P", str(port), "-s", serial, "shell"]:
                return subprocess.CompletedProcess(argv, 1, "", "isolated test server is unavailable on the implicit port")
            assert len(argv) == 7, "Android shell命令必须是单个argv项"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        monkeypatch.setattr(maindb.subprocess, "run", run)
        return calls

    return build


def test_default_main_database_read_reaches_the_owned_16000_server(isolated_adb):
    calls = isolated_adb()

    assert AdbMainDb(SERIAL, UID, adb="test-only-adb").exists() is True

    assert calls[0][0][:6] == ["test-only-adb", "-P", "16000", "-s", SERIAL, "shell"]
    assert f"/databases/{UID}.db" in calls[0][0][6]


def test_injected_server_port_is_used_for_the_same_account(isolated_adb):
    calls = isolated_adb(port=16022)

    assert AdbMainDb(SERIAL, UID, adb="test-only-adb", server_port=16022).exists() is True

    assert calls[0][0][1:5] == ["-P", "16022", "-s", SERIAL]


def test_adb_executable_and_serial_are_not_interpreted_by_a_host_shell(isolated_adb):
    executable = "/test-only/path with spaces/adb$(must-not-run)"
    serial = "127.0.0.1:16001;must-not-run"
    calls = isolated_adb(executable=executable, serial=serial)

    assert AdbMainDb(serial, UID, adb=executable).exists() is True

    assert calls[0][0][0] == executable and calls[0][0][4] == serial
    assert calls[0][1]["timeout"] == 10.0


def test_message_table_query_keeps_select_readonly_uri_and_result_parsing(isolated_adb):
    calls = isolated_adb(stdout=TABLE + "\n")

    assert AdbMainDb(SERIAL, UID, adb="test-only-adb").list_message_tables() == [TABLE]

    command = calls[0][0][6]
    assert f"file:/data/data/com.tencent.qidian/databases/{UID}.db?mode=ro" in command
    assert "SELECT name FROM sqlite_master" in command
    assert "INSERT" not in command and "UPDATE" not in command and "DELETE" not in command


def test_message_row_parsing_is_unchanged_when_using_the_owned_server(isolated_adb):
    calls = isolated_adb(stdout="7|0|1758240000|-1000|12|313233|74657374\n")

    rows = AdbMainDb(SERIAL, UID, adb="test-only-adb").rows_after(TABLE, 6)

    assert len(rows) == 1
    assert (rows[0].id, rows[0].issend, rows[0].time, rows[0].msgtype, rows[0].uniseq) == (7, 0, 1758240000, -1000, 12)
    assert "?mode=ro" in calls[0][0][6]
