"""vault_client(02 §2.2.5「凭据保险库客户端」/ 00 §11.1 [CRED]):Agent 侧只是 WinAgent ``/wa/v1/vault/*`` 的薄封装。

- 条目命名 ``vault://account/<id> | mail/... | api/<app_id> | winagent/agent_token|console_token``;本模块只认 ``name``(去掉 ``vault://`` 前缀的部分)。
- 读 = ``POST /wa/v1/vault/{name}/read``(仅 Agent 令牌;须带 ``X-Trace-Id``;每次读记审计,值不进日志);写 ``PUT``、删 ``DELETE``、存在性 ``HEAD``、``POST …/flag {suspect:true}``。
- 写类(put/delete)**不重试**(02 §2.5);读类 1 次重试由 winagent_client 做。
- WinAgent 不在线 ⇒ ``VaultUnavailable``:密码型登录不进行、账号 ``error(VAULT_UNAVAILABLE)``(02 §2.5 降级 ③),**不回退成让人输入**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .ids import trace_id as new_trace_id


class VaultUnavailable(Exception):
    def __init__(self, reason: str = "winagent_offline"):
        super().__init__(reason)
        self.reason = reason


def vault_name(ref: str) -> str:
    """``vault://account/qd01`` → ``account/qd01``;已是裸名则原样。"""
    return ref[8:] if ref.startswith("vault://") else ref


def credential_ref(account_id: str) -> str:
    return f"vault://account/{account_id}"


class Vault(Protocol):
    async def read(self, name: str, *, trace_id: Optional[str] = None) -> Optional[str]: ...
    async def put(self, name: str, value: str, *, scope: str = "account") -> None: ...
    async def delete(self, name: str) -> None: ...
    async def exists(self, name: str) -> bool: ...
    async def flag(self, name: str, *, suspect: bool = True) -> None: ...


@dataclass
class _Entry:
    value: str
    scope: str
    version: int = 1
    suspect: bool = False
    read_count: int = 0


@dataclass
class FakeVault:
    """内存假保险库:``offline=True`` 时全部操作抛 ``VaultUnavailable``;记录 ``reads``(name, trace_id)以断言「读记审计、值不回显」。"""
    entries: dict[str, _Entry] = field(default_factory=dict)
    reads: list[tuple[str, Optional[str]]] = field(default_factory=list)
    offline: bool = False
    deleted: list[str] = field(default_factory=list)

    def _check(self) -> None:
        if self.offline:
            raise VaultUnavailable("winagent_offline")

    async def read(self, name: str, *, trace_id: Optional[str] = None) -> Optional[str]:
        self._check()
        self.reads.append((vault_name(name), trace_id))
        e = self.entries.get(vault_name(name))
        if e is None:
            return None
        e.read_count += 1
        return e.value

    async def put(self, name: str, value: str, *, scope: str = "account") -> None:
        self._check()
        n = vault_name(name)
        old = self.entries.get(n)
        self.entries[n] = _Entry(value, scope, version=(old.version + 1) if old else 1)

    async def delete(self, name: str) -> None:
        self._check()
        self.entries.pop(vault_name(name), None)
        self.deleted.append(vault_name(name))

    async def exists(self, name: str) -> bool:
        self._check()
        return vault_name(name) in self.entries

    async def flag(self, name: str, *, suspect: bool = True) -> None:
        self._check()
        e = self.entries.get(vault_name(name))
        if e is not None:
            e.suspect = suspect


class WinAgentVault:
    """经 winagent_client 调 ``/wa/v1/vault/*``(02 §3.6 #7~#12);WinAgent 不可达一律转 ``VaultUnavailable``。"""

    def __init__(self, client):
        self._c = client

    async def _req(self, method: str, path: str, **kw) -> tuple[int, Optional[dict[str, Any]]]:
        from .winagent_client import WinAgentUnavailable
        try:
            return await self._c.request(method, path, timeout_s=3, **kw)
        except WinAgentUnavailable as e:
            raise VaultUnavailable(e.reason) from e

    async def read(self, name: str, *, trace_id: Optional[str] = None) -> Optional[str]:
        n = vault_name(name)
        status, body = await self._req("POST", f"/wa/v1/vault/{n}/read", retry=False, headers={"X-Trace-Id": trace_id or new_trace_id()})
        if status == 404:
            return None
        if status != 200:
            raise VaultUnavailable(f"http_{status}")
        return (body or {}).get("value")

    async def put(self, name: str, value: str, *, scope: str = "account") -> None:
        status, _ = await self._req("PUT", f"/wa/v1/vault/{vault_name(name)}", json={"value": value, "scope": scope}, retry=False)
        if status not in (200, 201, 204):
            raise VaultUnavailable(f"http_{status}")

    async def delete(self, name: str) -> None:
        status, _ = await self._req("DELETE", f"/wa/v1/vault/{vault_name(name)}", retry=False)
        if status not in (200, 204, 404):
            raise VaultUnavailable(f"http_{status}")

    async def exists(self, name: str) -> bool:
        status, _ = await self._req("HEAD", f"/wa/v1/vault/{vault_name(name)}", retry=True)
        if status == 200:
            return True
        if status == 404:
            return False
        raise VaultUnavailable(f"http_{status}")

    async def flag(self, name: str, *, suspect: bool = True) -> None:
        status, _ = await self._req("POST", f"/wa/v1/vault/{vault_name(name)}/flag", json={"suspect": suspect}, retry=False)
        if status not in (200, 204):
            raise VaultUnavailable(f"http_{status}")


def redact(value: Any) -> str:
    """密钥/密码只以长度示人(00 §11.2 [NOLOG])。"""
    return f"<{len(value) if isinstance(value, str) else 0} chars>"
