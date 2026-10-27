"""企点组织架构。

层级来自已解开的 ``{uin}-org.db``：``OrgDepartment`` 是部门，``OrgD2D.ord`` 是部门排序，
``OrgD2M`` 是成员到部门的归属。显示名和账号来自 ``{uin}-org-member.db`` 的 ``OrgMember``。
一个人挂在多个部门时按归属各留一行。没有任何部门的人 ``department_id`` 为空字符串，不丢。
私人好友不进这里。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parent(value: Any) -> str:
    text = _text(value)
    if text in ("", "0"):
        return ""
    return text


def _order(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _row(item: Any, *indexes: int) -> tuple[Any, ...]:
    return tuple(item[i] if i < len(item) else None for i in indexes)


def build_org(
    departments: Iterable[Any],
    people: Iterable[Any],
    memberships: Iterable[Any],
    edges: Iterable[Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """把已读出的行收成目录里的 ``org``。

    ``departments`` 每行是 ``(uid, pid, name)``。
    ``people`` 每行是 ``(uin, name, account)``。
    ``memberships`` 每行是 ``(did, mid, ord)``。
    ``edges`` 每行是 ``(did, ord)``，只提供部门排序；上级以部门行的 ``pid`` 为准。
    """
    order_by_dept: dict[str, int] = {}
    for edge in edges or ():
        did, ord_value = _row(edge, 0, 1)
        dept_id = _text(did)
        if dept_id and dept_id not in order_by_dept:
            order_by_dept[dept_id] = _order(ord_value)

    dept_rows: list[dict[str, Any]] = []
    for item in departments:
        uid, pid, name = _row(item, 0, 1, 2)
        dept_id = _text(uid)
        if not dept_id or dept_id == "0":
            continue
        dept_rows.append({
            "id": dept_id,
            "parent_id": _parent(pid),
            "name": _text(name),
            "order": order_by_dept.get(dept_id, 0),
        })

    directory: dict[str, tuple[str, str]] = {}
    people_order: list[str] = []
    for item in people:
        uin, name, account = _row(item, 0, 1, 2)
        key = _text(uin)
        if not key:
            continue
        if key not in directory:
            people_order.append(key)
        directory[key] = (_text(name), _text(account))

    member_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in memberships:
        did, mid, ord_value = _row(item, 0, 1, 2)
        uin = _text(mid)
        dept_id = _text(did)
        if not uin:
            continue
        name, account = directory.get(uin, ("", ""))
        member_rows.append({
            "uin": uin,
            "name": name,
            "account": account,
            "department_id": "" if dept_id in ("", "0") else dept_id,
            "order": _order(ord_value),
        })
        seen.add(uin)
    for uin in people_order:
        if uin in seen:
            continue
        name, account = directory[uin]
        member_rows.append({
            "uin": uin,
            "name": name,
            "account": account,
            "department_id": "",
            "order": 0,
        })
    return {"departments": dept_rows, "members": member_rows}


def assert_org_only_on_qidian(accounts: Iterable[Mapping[str, Any]]) -> None:
    """组织只挂在企点账号上。群列表里不能出现部门或成员，也不写存款侧的群过滤。"""
    for account in accounts:
        channel = _text(account.get("channel"))
        if account.get("org") not in (None, {}) and channel != "qidian":
            raise ValueError("组织只能挂在企点账号上")
        if "group_native_ids" in account:
            raise ValueError("目录不写存款群过滤")
        groups = account.get("groups") or []
        if not isinstance(groups, list):
            raise ValueError("groups 必须是列表")
        for group in groups:
            if not isinstance(group, dict):
                continue
            if any(key in group for key in ("uin", "department_id", "departments", "members")):
                raise ValueError("群配置不能写入组织")


def attach_org(accounts: list[dict[str, Any]], account_uid: str, org: Mapping[str, Any]) -> list[dict[str, Any]]:
    """只给账号号相等的企点账号放上这份 ``org``。其它账号的群列表保持原样。"""
    uid = _text(account_uid)
    if not uid:
        raise ValueError("企点号为空")
    matched = False
    out: list[dict[str, Any]] = []
    for account in accounts:
        item = dict(account)
        if _text(item.get("channel")) == "qidian" and _text(item.get("account_uid")) == uid:
            item["org"] = {
                "departments": list(org.get("departments") or []),
                "members": list(org.get("members") or []),
            }
            matched = True
        out.append(item)
    if not matched:
        raise ValueError("目录里没有这个企点号")
    assert_org_only_on_qidian(out)
    return out


def load_org_sqlite(org_db: str | Path, member_db: str | Path) -> dict[str, list[dict[str, Any]]]:
    """读两份已经解开的 SQLite。打不开或缺少表时直接失败，不返回空组织。"""
    org_con = sqlite3.connect(f"file:{Path(org_db).as_posix()}?mode=ro", uri=True)
    mem_con = sqlite3.connect(f"file:{Path(member_db).as_posix()}?mode=ro", uri=True)
    try:
        return _from_connections(org_con, mem_con)
    finally:
        org_con.close()
        mem_con.close()


def load_org_plain_bytes(org_plain: bytes, member_plain: bytes, *, allow_empty=False) -> dict[str, list[dict[str, Any]]]:
    """从内存中的明文库字节读组织。"""
    org_con = _memory_db(org_plain)
    mem_con = _memory_db(member_plain)
    try:
        return _from_connections(org_con, mem_con, allow_empty=allow_empty)
    finally:
        org_con.close()
        mem_con.close()


def decrypt_sqlcipher3(blob: bytes, password: bytes, *, kdf_iter: int = 64000, page_size: int = 1024) -> bytes:
    """解开 SQLCipher 3 默认参数的库：AES-256-CBC、PBKDF2-HMAC-SHA1、页 1024、保留 48 字节。"""
    if not password:
        raise ValueError("组织库口令为空")
    if page_size < 64 or len(blob) % page_size != 0 or len(blob) < page_size:
        raise ValueError("组织库长度对不齐页")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise ValueError("解开组织库需要 cryptography") from exc

    salt = blob[:16]
    key = hashlib.pbkdf2_hmac("sha1", password, salt, kdf_iter, 32)
    reserve = 48
    out = bytearray()
    pages = len(blob) // page_size
    for index in range(pages):
        page = blob[index * page_size : (index + 1) * page_size]
        iv = page[page_size - reserve : page_size - reserve + 16]
        if index == 0:
            body = page[16 : page_size - reserve]
            decrypted = _aes_cbc(Cipher, algorithms, modes, key, iv, body)
            page_field = int.from_bytes(decrypted[:2], "big")
            if page_field != page_size or decrypted[2] not in (1, 2) or decrypted[3] not in (1, 2):
                raise ValueError("组织库口令或页参数对不上")
            raw = b"SQLite format 3\x00" + decrypted
            raw = raw + b"\x00" * (page_size - len(raw))
        else:
            body = page[: page_size - reserve]
            decrypted = _aes_cbc(Cipher, algorithms, modes, key, iv, body)
            raw = decrypted + b"\x00" * reserve
        out += raw
    return bytes(out)


def read_encrypted_org(org_blob: bytes, member_blob: bytes, password: bytes, *, allow_empty=False) -> dict[str, list[dict[str, Any]]]:
    """解开两份加密库并解析。口令不对时抛错，不返回空组织。"""
    org = load_org_plain_bytes(
        decrypt_sqlcipher3(org_blob, password),
        decrypt_sqlcipher3(member_blob, password),
        allow_empty=allow_empty,
    )
    if not allow_empty and not org["departments"] and not org["members"]:
        raise ValueError("组织库解开后是空的")
    return org


def read_password_blob(blob: bytes) -> bytes:
    """从一次性落盘的口令记录里取出 ``getPassword`` 那段。记录本身不进仓库。"""
    for part in blob.split(b"\n---\n"):
        if not part.startswith(b"getPassword\n"):
            continue
        raw = part[len(b"getPassword\n") :]
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if raw:
            return raw
    raise ValueError("没有组织库口令")


def _aes_cbc(cipher_cls: Any, algorithms: Any, modes: Any, key: bytes, iv: bytes, data: bytes) -> bytes:
    decryptor = cipher_cls(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def _memory_db(blob: bytes) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(blob)
    except Exception:
        con.close()
        raise ValueError("明文组织库打不开") from None
    return con


def _from_connections(org_con: sqlite3.Connection, mem_con: sqlite3.Connection, *, allow_empty=False) -> dict[str, list[dict[str, Any]]]:
    _require_tables(org_con, ("OrgDepartment", "OrgD2M"))
    _require_tables(mem_con, ("OrgMember",))
    departments = org_con.execute("SELECT uid, pid, name FROM OrgDepartment").fetchall()
    edges = _optional_edges(org_con)
    memberships = org_con.execute("SELECT did, mid, ord FROM OrgD2M").fetchall()
    people = mem_con.execute("SELECT uin, name, account FROM OrgMember").fetchall()
    org = build_org(departments, people, memberships, edges)
    if not allow_empty and not org["departments"] and not org["members"]:
        raise ValueError("组织库解开后是空的")
    return org


def _optional_edges(con: sqlite3.Connection) -> list[tuple[Any, Any]]:
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "OrgD2D" not in names:
        return []
    return list(con.execute("SELECT did, ord FROM OrgD2D"))


def _require_tables(con: sqlite3.Connection, needed: tuple[str, ...]) -> None:
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in needed if name not in names]
    if missing:
        raise ValueError("组织库缺少表 " + ",".join(missing))


class OrgReadError(RuntimeError):
    def __init__(self, code, *, state='error'):
        self.code, self.state = code, state
        super().__init__(code)


def read_account_org(source, account_uid, config_file, *, cache=None, clock=None):
    """Read this account's current organization without changing native data.

    Native sources derive material from their current verified APK/keystore;
    local file sources use their explicit protected-key reference.
    """
    import json
    import os
    import re
    import time
    clock = time.time if clock is None else clock
    uid = str(account_uid)
    if not re.fullmatch(r'[0-9]+', uid) or Path(str(source.path)).name != uid + '.db':
        raise OrgReadError('ORG_ACCOUNT_MISMATCH')
    if all(hasattr(source, key) for key in ('adb', 'serial', 'server_port', '_shell')):
        from .org_native import read_native_org
        return read_native_org(source, uid, cache=cache, clock=clock)
    try:
        cfg_path = Path(config_file)
        if cfg_path.stat().st_size > 16384:
            raise OrgReadError('ORG_CONFIG_INVALID')
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        if cfg.get('account_uid') != uid:
            raise OrgReadError('ORG_ACCOUNT_MISMATCH')
        reference = cfg.get('org_password_file')
        if not reference:
            raise OrgReadError('ORG_CREDENTIALS_NOT_CONFIGURED', state='unreported')
        key_path = Path(reference)
        if not key_path.is_absolute():
            key_path = cfg_path.parent / key_path
        if os.name != 'nt' and key_path.stat().st_mode & 0o077:
            raise OrgReadError('ORG_CREDENTIALS_PERMISSIONS')
        if not 0 < key_path.stat().st_size <= 4096:
            raise OrgReadError('ORG_CREDENTIALS_INVALID')
        password = key_path.read_bytes()
        form = cfg.get('org_password_format', 'getPassword')
        if form == 'getPassword':
            password = read_password_blob(password)
        elif form != 'raw':
            raise OrgReadError('ORG_CREDENTIALS_INVALID')
    except OrgReadError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise OrgReadError('ORG_CREDENTIALS_UNAVAILABLE') from None
    paths = [str(source.path)[:-3] + suffix for suffix in ('-org.db', '-org-member.db')]
    def metadata():
        return tuple((Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p in paths)

    try:
        before = metadata()
        if any(not 0 < size <= 64 * 1024 * 1024 for size, _ in before):
            raise OrgReadError('ORG_SOURCE_SIZE_INVALID')
        fingerprint = (uid, tuple(paths), before, hashlib.sha256(password).digest())
        previous = (cache or {}).get(uid)
        if previous and previous['fingerprint'] == fingerprint:
            return dict(previous['result'])
        blobs = [Path(path).read_bytes() for path in paths]
        if tuple(len(blob) for blob in blobs) != tuple(size for size, _ in before) or metadata() != before:
            raise OrgReadError('ORG_SOURCE_CHANGED')
        if all(blob.startswith(b'SQLite format 3\x00') for blob in blobs):
            org = load_org_plain_bytes(*blobs, allow_empty=True)
        else:
            org = read_encrypted_org(*blobs, password, allow_empty=True)
        observed = clock()
        result = {'org': org, 'org_state': 'ready' if org['departments'] or org['members'] else 'empty',
                  'org_updated_at': observed, 'org_error': None}
        if cache is not None:
            cache[uid] = {'fingerprint': fingerprint, 'result': result}
        return result
    except OrgReadError:
        raise
    except Exception:
        raise OrgReadError('ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR') from None
