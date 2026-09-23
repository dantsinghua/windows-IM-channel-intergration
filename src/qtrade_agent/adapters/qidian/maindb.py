"""企点主库 ``{uin}.db`` 的只读访问 —— 规格:docs/06 §2.9.5(两条只读铁律:``mode=ro`` 只查增量、不做 ``.backup``、不碰进程)。

两个后端同一接口:
- ``LocalSqliteMainDb``:宿主能直接打开的库文件(单测 / 将来把容器数据卷挂到宿主时用)
- ``AdbMainDb``:产品口径——设备上 ``sqlite3 'file:…?mode=ro'``,经 ``adb -s 127.0.0.1:160NN shell`` 一次调用查完一批表(不逐表起 shell)
调用方(poll.py)只依赖 ``MainDb`` 协议;任何 sqlite 报错统一抛 ``MainDbError(reason)``,``reason ∈ {open_failed, schema_mismatch}``。
"""
from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Optional, Protocol

from .contacts import CONTACT_SQL
from .msgdata import MainDbRow

MESSAGE_TABLE_GLOBS = ("mr_friend_*_New", "mr_troop_*_New")
QIDIAN_DB_DIR = "/data/data/com.tencent.qidian/databases/"


def db_path_for(self_uid: str) -> str:
    return QIDIAN_DB_DIR + self_uid + ".db"


class MainDbError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class GapStats:
    mn: int
    mx: int
    cnt: int
    first_ts: int
    last_ts: int


class MainDb(Protocol):
    def exists(self) -> bool: ...
    def list_message_tables(self) -> list[str]: ...
    def last_row_peer(self, table: str) -> Optional[tuple[str, int]]: ...          # (hex(frienduin), istroop) of ORDER BY _id DESC LIMIT 1
    def last_id_uniseq(self, table: str) -> Optional[tuple[int, int]]: ...        # (_id, uniseq) of ORDER BY _id DESC LIMIT 1
    def uniseq_at(self, table: str, row_id: int) -> Optional[int]: ...
    def rows_after(self, table: str, last: int) -> list[MainDbRow]: ...
    def group_gap_stats(self, table: str, since_s: int) -> Optional[GapStats]: ...
    def list_contact_source_rows(self) -> list[tuple[str, str, str, str]]: ...


def _q(table: str) -> str:
    if '"' in table:
        raise MainDbError("schema_mismatch", f"bad table name {table!r}")
    return f'"{table}"'


class LocalSqliteMainDb:
    """本地文件后端。每次调用都 ``mode=ro`` 打开、用完即关(WAL 读事务自带一致快照)。"""

    def __init__(self, path: str):
        self.path = path

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _conn(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            raise MainDbError("open_failed", str(e)) from e

    def _run(self, sql: str, params=()):
        con = self._conn()
        try:
            return con.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            raise MainDbError("schema_mismatch", str(e)) from e
        except sqlite3.Error as e:
            raise MainDbError("open_failed", str(e)) from e
        finally:
            con.close()

    def list_message_tables(self) -> list[str]:
        rows = self._run(
            "SELECT name FROM sqlite_master WHERE type='table' AND (name GLOB ? OR name GLOB ?)", MESSAGE_TABLE_GLOBS)
        return [r[0] for r in rows]

    def last_row_peer(self, table: str) -> Optional[tuple[str, int]]:
        rows = self._run(f"SELECT hex(frienduin), istroop FROM {_q(table)} ORDER BY _id DESC LIMIT 1")
        return (rows[0][0], int(rows[0][1])) if rows else None

    def last_id_uniseq(self, table: str) -> Optional[tuple[int, int]]:
        rows = self._run(f"SELECT _id, uniseq FROM {_q(table)} ORDER BY _id DESC LIMIT 1")
        return (int(rows[0][0]), int(rows[0][1])) if rows else None

    def uniseq_at(self, table: str, row_id: int) -> Optional[int]:
        rows = self._run(f"SELECT uniseq FROM {_q(table)} WHERE _id = ?", (row_id,))
        return int(rows[0][0]) if rows else None

    def rows_after(self, table: str, last: int) -> list[MainDbRow]:
        rows = self._run(
            f"SELECT _id, issend, time, msgtype, uniseq, hex(senderuin), hex(msgData) FROM {_q(table)} "
            f"WHERE _id > ? ORDER BY _id", (last,))
        return [MainDbRow(int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), r[5] or "", r[6] or "") for r in rows]

    def group_gap_stats(self, table: str, since_s: int) -> Optional[GapStats]:
        rows = self._run(
            f"SELECT MIN(shmsgseq), MAX(shmsgseq), COUNT(DISTINCT shmsgseq), MIN(time), MAX(time) FROM {_q(table)} "
            f"WHERE shmsgseq > 0 AND time >= ?", (since_s,))
        if not rows or rows[0][2] in (0, None):
            return None
        mn, mx, cnt, f, l = rows[0]
        return GapStats(int(mn), int(mx), int(cnt), int(f), int(l))

    def list_contact_source_rows(self) -> list[tuple[str, str, str, str]]:
        rows = self._run(CONTACT_SQL)
        return [(str(r[0]), r[1] or "", r[2] or "", r[3] or "") for r in rows if len(r) >= 4]


class AdbMainDb:
    """产品后端:``adb -s 127.0.0.1:160NN shell sqlite3 -separator '|' 'file:…?mode=ro' "<sql>"``。

    前置 = ``ensure_root`` 已通过(06 §2.9.5;非 root 读不到 /data/data)。本类不做提权。
    """

    def __init__(self, serial: str, self_uid: str, *, adb: str = "adb", timeout_s: float = 10.0):
        self.serial = serial
        self.path = db_path_for(self_uid)
        self.adb = adb
        self.timeout_s = timeout_s

    def _shell(self, cmd: str) -> str:
        try:
            p = subprocess.run([self.adb, "-s", self.serial, "shell", cmd], capture_output=True, text=True,
                               timeout=self.timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise MainDbError("open_failed", str(e)) from e
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            reason = "schema_mismatch" if ("no such" in err or "syntax error" in err) else "open_failed"
            raise MainDbError(reason, err)
        return p.stdout

    def _sql(self, sql: str) -> list[list[str]]:
        out = self._shell(f"sqlite3 -separator '|' 'file:{self.path}?mode=ro' {shlex.quote(sql)}")
        return [line.split("|") for line in out.splitlines() if line]

    def exists(self) -> bool:
        return self._shell(f"[ -f {shlex.quote(self.path)} ] && echo 1 || echo 0").strip() == "1"

    def list_message_tables(self) -> list[str]:
        rows = self._sql("SELECT name FROM sqlite_master WHERE type='table' AND "
                         "(name GLOB 'mr_friend_*_New' OR name GLOB 'mr_troop_*_New')")
        return [r[0] for r in rows]

    def last_row_peer(self, table: str) -> Optional[tuple[str, int]]:
        rows = self._sql(f"SELECT hex(frienduin), istroop FROM {_q(table)} ORDER BY _id DESC LIMIT 1")
        return (rows[0][0], int(rows[0][1])) if rows else None

    def last_id_uniseq(self, table: str) -> Optional[tuple[int, int]]:
        rows = self._sql(f"SELECT _id, uniseq FROM {_q(table)} ORDER BY _id DESC LIMIT 1")
        return (int(rows[0][0]), int(rows[0][1])) if rows else None

    def uniseq_at(self, table: str, row_id: int) -> Optional[int]:
        rows = self._sql(f"SELECT uniseq FROM {_q(table)} WHERE _id = {int(row_id)}")
        return int(rows[0][0]) if rows else None

    def rows_after(self, table: str, last: int) -> list[MainDbRow]:
        rows = self._sql(f"SELECT _id, issend, time, msgtype, uniseq, hex(senderuin), hex(msgData) FROM {_q(table)} "
                         f"WHERE _id > {int(last)} ORDER BY _id")
        return [MainDbRow(int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), r[5], r[6]) for r in rows if len(r) == 7]

    def group_gap_stats(self, table: str, since_s: int) -> Optional[GapStats]:
        rows = self._sql(f"SELECT MIN(shmsgseq), MAX(shmsgseq), COUNT(DISTINCT shmsgseq), MIN(time), MAX(time) "
                         f"FROM {_q(table)} WHERE shmsgseq > 0 AND time >= {int(since_s)}")
        if not rows or rows[0][2] in ("", "0"):
            return None
        mn, mx, cnt, f, l = rows[0]
        return GapStats(int(mn), int(mx), int(cnt), int(f), int(l))

    def list_contact_source_rows(self) -> list[tuple[str, str, str, str]]:
        rows = self._sql(CONTACT_SQL)
        return [(r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else "") for r in rows if r]
