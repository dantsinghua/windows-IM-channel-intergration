"""``vault`` 模块(**服务**侧;02 §2.4 / §3.6 #7~#12 / 05 §2.2)。

存放形式(05 §2.2.3,C-09)::

    %ProgramData%\\QTrade\\winagent\\vault\\
    ├─ entropy.bin                      32B 附加熵(ACL 仅 SYSTEM + Administrators)
    ├─ blobs\\<sha256(条目名)>.bin       每条一个 DPAPI blob(文件名不暴露条目名)
    └─ (元数据在 winagent.db 的 vault_index)

硬约束(逐字):
- 加密 = DPAPI **机器级 + 附加熵**(05 §2.2.1 方案 B);``dpapi_scope`` 在服务账号下必须 ``machine``。
- ``PUT`` = 新建或覆盖,``version+1``,**旧 blob 先 0 填充再删**;``DELETE`` 同样 0 填充再删、``vault_index`` 留行 ``deleted_ms``。
- ``vault_index`` **不存任何可用于还原值的东西**;``blob_sha256`` 只用于校验密文文件未被替换。
- 读 = ``POST …/read``(读有副作用:记审计 + ``read_count``/``last_read_ms``);**仅 Agent 令牌**,控制台 → 403(鉴权在 ``svc``)。
- 值上限 4 KB(05 §2.2.2);**请求体不进日志、响应不回显值**。
- 熵缺失或 ACL 放宽 ⇒ ``alert VAULT_ENTROPY_MISSING``(crit,05 §2.2.5;由 ``startup_selfcheck`` 产出)。
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .audit import Audit
from .backends import CryptoBackend
from .config import VaultConfig
from .db import Db
from .errors import INVALID_ARGS, TARGET_NOT_FOUND, WaError
from .logfmt import get_logger

log = get_logger("vault")

ENTROPY_BYTES = 32                                  # 05 §2.2.1:32 字节随机数
SCOPES = ("account", "mail", "api", "webhook", "winagent", "asr", "other")     # 02 §3.2 vault_index.scope CHECK
VAULT_ENTROPY_MISSING = "VAULT_ENTROPY_MISSING"     # 02 §3.7


def entry_name(ref: str) -> str:
    """``vault://account/qd01`` → ``account/qd01``;已是裸名则原样(与 Agent 侧 ``vault_client.vault_name`` 同语义)。"""
    return ref[8:] if ref.startswith("vault://") else ref


def blob_relpath(name: str) -> str:
    """``blobs\\<sha256(条目名)>.bin``(相对 ``[vault] dir``;02 §3.2 ``vault_index.blob_path`` 逐字)。"""
    return "blobs\\" + hashlib.sha256(name.encode("utf-8")).hexdigest() + ".bin"


@dataclass(frozen=True)
class VaultEntry:
    name: str
    scope: str
    version: int
    owner: str
    created_ms: int
    updated_ms: int
    last_read_ms: Optional[int]
    read_count: int
    suspect: bool
    deleted_ms: Optional[int]

    def view(self) -> dict[str, Any]:
        """#7 列表:**只回元数据,无值**。"""
        return {"name": self.name, "scope": self.scope, "version": self.version, "updated_ms": self.updated_ms,
                "last_read_ms": self.last_read_ms, "read_count": self.read_count, "suspect": self.suspect,
                "deleted_ms": self.deleted_ms}


class Vault:
    def __init__(self, db: Db, crypto: CryptoBackend, cfg: VaultConfig, *, root: str, audit: Optional[Audit] = None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._db = db
        self._crypto = crypto
        self._cfg = cfg
        self._root = root                          # 展开后的 [vault] dir(真机 = %ProgramData%\QTrade\winagent\vault)
        self._audit = audit
        self._clock = clock
        self._entropy: Optional[bytes] = None

    # ---------------------------------------------------------------- 熵
    @property
    def entropy_path(self) -> str:
        return os.path.join(self._root, self._cfg.entropy_file)

    def ensure_entropy(self) -> bytes:
        """首装生成 32B 熵并收紧 ACL;之后读回。**丢失 = 全库不可解**(05 §2.2.5,不是缺陷,是 DPAPI 性质)。"""
        if self._entropy is not None:
            return self._entropy
        os.makedirs(os.path.join(self._root, "blobs"), exist_ok=True)
        path = self.entropy_path
        if not os.path.exists(path):
            data = self._crypto.random_bytes(ENTROPY_BYTES)
            with open(path, "wb") as f:
                f.write(data)
            self._crypto.tighten_acl(path)
            log.info("entropy created", extra={"op": "vault.entropy", "code": "OK"})
        with open(path, "rb") as f:
            self._entropy = f.read()
        return self._entropy

    def startup_selfcheck(self) -> Optional[dict[str, Any]]:
        """启动自检:熵文件缺失 / ACL 放宽 → 返回一条 ``VAULT_ENTROPY_MISSING`` crit 告警(02 §3.7;05 §2.2.5)。"""
        path = self.entropy_path
        if not os.path.exists(path):
            return {"code": VAULT_ENTROPY_MISSING, "severity": "crit", "subject": "host",
                    "evidence": {"reason": "missing", "path": path}}
        if not self._crypto.acl_is_tight(path):
            return {"code": VAULT_ENTROPY_MISSING, "severity": "crit", "subject": "host",
                    "evidence": {"reason": "acl_loosened", "path": path}}
        return None

    # ---------------------------------------------------------------- 内部
    def _abs_blob(self, rel: str) -> str:
        return os.path.join(self._root, *rel.split("\\"))

    @staticmethod
    def _zero_fill_and_remove(path: str) -> None:
        """旧 blob **先 0 填充再删**(#9/#10 逐字)——只删文件的话密文块仍可能在磁盘上被恢复。"""
        try:
            size = os.path.getsize(path)
            with open(path, "r+b") as f:
                f.write(b"\x00" * size)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass

    def _row(self, name: str) -> Optional[dict[str, Any]]:
        return self._db.one("SELECT * FROM vault_index WHERE name=?", (name,))

    # ---------------------------------------------------------------- #7 列表
    def list(self, *, scope: Optional[str] = None, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM vault_index WHERE 1=1"
        params: list[Any] = []
        if scope:
            sql, _ = sql + " AND scope=?", params.append(scope)
        if not include_deleted:
            sql += " AND deleted_ms IS NULL"
        rows = self._db.query(sql + " ORDER BY name", tuple(params))
        return [VaultEntry(r["name"], r["scope"], r["version"], r["owner"], r["created_ms"], r["updated_ms"],
                           r["last_read_ms"], r["read_count"], bool(r["suspect"]), r["deleted_ms"]).view() for r in rows]

    # ---------------------------------------------------------------- #8 存在性
    def exists(self, name: str) -> bool:
        r = self._row(entry_name(name))
        return r is not None and r["deleted_ms"] is None

    # ---------------------------------------------------------------- #9 写
    async def put(self, name: str, value: str, *, scope: str = "other", owner: str = "agent",
                  trace_id: Optional[str] = None) -> int:
        n = entry_name(name)
        raw = value.encode("utf-8")
        if len(raw) > self._cfg.max_value_bytes:
            raise WaError(INVALID_ARGS, f"条目值超过上限 {self._cfg.max_value_bytes} 字节", reason="value_too_large")
        if scope not in SCOPES:
            raise WaError(INVALID_ARGS, f"scope 必须是 {SCOPES} 之一", reason="bad_scope")
        blob = await self._crypto.protect(raw, self.ensure_entropy())
        rel = blob_relpath(n)
        abspath = self._abs_blob(rel)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        old = self._row(n)
        if old is not None and os.path.exists(abspath):
            self._zero_fill_and_remove(abspath)                 # 旧 blob 0 填充后删(#9)
        with open(abspath, "wb") as f:
            f.write(blob)
        sha = hashlib.sha256(blob).hexdigest()
        now = self._clock()
        version = (old["version"] + 1) if old else 1            # 覆盖 = version+1(#9;删过再写也继续 +1)
        with self._db.tx() as con:
            con.execute(
                "INSERT INTO vault_index(name, scope, blob_path, blob_sha256, version, owner, created_ms, updated_ms, "
                "last_read_ms, read_count, suspect, deleted_ms) VALUES (?,?,?,?,?,?,?,?,NULL,0,0,NULL) "
                "ON CONFLICT(name) DO UPDATE SET scope=excluded.scope, blob_path=excluded.blob_path, "
                "blob_sha256=excluded.blob_sha256, version=excluded.version, owner=excluded.owner, "
                "updated_ms=excluded.updated_ms, suspect=0, deleted_ms=NULL",
                (n, scope, rel, sha, version, owner, now, now))
        if self._audit:
            self._audit.record(actor=owner, action="vault.write", target=n, trace_id=trace_id,
                               detail={"scope": scope, "version": version})
        return version

    # ---------------------------------------------------------------- #10 删
    def delete(self, name: str, *, owner: str = "agent", trace_id: Optional[str] = None) -> bool:
        n = entry_name(name)
        row = self._row(n)
        if row is None:
            return False
        self._zero_fill_and_remove(self._abs_blob(row["blob_path"]))
        now = self._clock()
        with self._db.tx() as con:
            con.execute("UPDATE vault_index SET deleted_ms=?, updated_ms=? WHERE name=?", (now, now, n))
        if self._audit:
            self._audit.record(actor=owner, action="vault.delete", target=n, trace_id=trace_id, detail={"scope": row["scope"]})
        return True

    # ---------------------------------------------------------------- #11 读(仅 Agent 令牌;须带 X-Trace-Id)
    async def read(self, name: str, *, trace_id: str, owner: str = "agent") -> Optional[str]:
        n = entry_name(name)
        row = self._row(n)
        if row is None or row["deleted_ms"] is not None:
            if self._audit:
                self._audit.record(actor=owner, action="vault.read", target=n, result=TARGET_NOT_FOUND,
                                   trace_id=trace_id, detail={"found": False})
            return None
        abspath = self._abs_blob(row["blob_path"])
        try:
            with open(abspath, "rb") as f:
                blob = f.read()
        except OSError as e:
            if self._audit:
                self._audit.record(actor=owner, action="vault.read", target=n, result="INTERNAL", trace_id=trace_id,
                                   detail={"found": False})
            raise WaError("INTERNAL", "密文文件读取失败", reason="blob_unreadable") from e
        if hashlib.sha256(blob).hexdigest() != row["blob_sha256"]:      # 只校验未被替换(02 §3.2 blob_sha256 注释)
            if self._audit:
                self._audit.record(actor=owner, action="vault.read", target=n, result="INTERNAL", trace_id=trace_id,
                                   detail={"blob_sha256": "mismatch"})
            raise WaError("INTERNAL", "密文文件摘要不匹配(可能已被替换)", reason="blob_tampered")
        value = (await self._crypto.unprotect(blob, self.ensure_entropy())).decode("utf-8")
        now = self._clock()
        with self._db.tx() as con:                                      # 读有副作用:read_count / last_read_ms(#11)
            con.execute("UPDATE vault_index SET read_count = read_count + 1, last_read_ms = ? WHERE name = ?", (now, n))
        if self._audit:
            self._audit.record(actor=owner, action="vault.read", target=n, trace_id=trace_id,
                               detail={"found": True, "version": row["version"]})   # 🔴 不记值、不记长度、不记摘要
        return value

    # ---------------------------------------------------------------- #12 flag
    def flag(self, name: str, *, suspect: bool = True, owner: str = "agent", trace_id: Optional[str] = None) -> bool:
        n = entry_name(name)
        if self._row(n) is None:
            return False
        with self._db.tx() as con:
            con.execute("UPDATE vault_index SET suspect=?, updated_ms=? WHERE name=?", (1 if suspect else 0, self._clock(), n))
        if self._audit:
            self._audit.record(actor=owner, action="vault.flag", target=n, trace_id=trace_id, detail={"suspect": suspect})
        return True
