"""适配器契约 —— 规格:docs/02 §2.2.3(所有适配器同签名;R6-38/R6-39 的企点例外写在 qidian/adapter.py)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ..models import Command, CommandResult


@dataclass
class Account:
    """总线/适配器看到的账号视图(00 §7.1 的子集;真值在 accounts 表,经 store 读)。"""
    id: str
    channel: str
    state: str
    self_uid: Optional[str] = None
    self_nick: Optional[str] = None
    state_code: Optional[str] = None
    app_version: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    channel: str
    capabilities: frozenset[str]

    async def start(self, acct: Account) -> None: ...
    async def stop(self, acct: Account, *, graceful: bool) -> None: ...
    async def get_state(self, acct: Account) -> str: ...
    async def execute(self, acct: Account, cmd: Command) -> CommandResult: ...
    async def send(self, acct: Account, cmd: Command) -> CommandResult: ...
    async def confirm_probe(self, acct: Account, cmd: Command) -> bool: ...
    async def poll(self, acct: Account, *, only_sessions: Optional[list[str]] = None) -> None: ...
