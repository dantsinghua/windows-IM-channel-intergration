"""邮件路由 ``mail_routes`` —— 规格:docs/06 §2.15(E-5「按通道配多邮箱;预留按登录账号配邮箱」)。

查找顺序(§2.15.1)= 账号级 ``(channel, account_id)`` → 通道级 ``(channel, NULL)`` → 全局默认。
⚠️ 列形态以 **02 §3.1 DDL 为准**(``id`` INTEGER AUTOINCREMENT、全局行 ``channel IS NULL AND account_id IS NULL``);
06 §3.1 写的 ``id TEXT PK``/``'default'`` 保留行与 ``mailbox_key``/``level``/``name``/``inbound_enabled`` 独立列在 02 DDL 里不存在
——见 handoff「建议裁决 ②」;本模块按 02 DDL 落地,``mailbox_key`` 程序算、``level``/``enabled`` 放 ``inbound_json``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import MailConfig, MailFallbackConfig, MailHmacKey, MailInboundConfig, MailOutboundConfig

GLOBAL_ROUTE_NAME = "default"       # §2.15.1 ③ 全局默认路由的展示名(库里是 channel/account_id 皆 NULL 的那一行)


def mailbox_key(host: str, user: str) -> str:
    """§2.15.2:``mailbox_key = lower(host)+"/"+lower(user)``,程序算;读线程去重与 ``cursors.owner=mail:<mailbox_key>`` 取值。"""
    return f"{(host or '').lower()}/{(user or '').lower()}"


def _inbound_from_json(d: dict[str, Any]) -> MailInboundConfig:
    d = dict(d or {})
    hmac_raw = d.pop("hmac", {}) or {}
    fb_raw = d.pop("fallback", {}) or {}
    names = MailInboundConfig.__dataclass_fields__.keys()
    cfg = MailInboundConfig(**{k: d[k] for k in names if k in d and k not in ("hmac", "fallback")})
    fb_names = MailFallbackConfig.__dataclass_fields__.keys()
    cfg.fallback = MailFallbackConfig(**{k: fb_raw[k] for k in fb_names if k in fb_raw})
    cfg.hmac = {
        str(s): MailHmacKey(short_name=str(s), from_addr=str((v or {}).get("from", "")),
                            secret_ref=str((v or {}).get("secret_ref", "")))
        for s, v in hmac_raw.items() if isinstance(v, dict)
    }
    return cfg


def _outbound_from_json(d: dict[str, Any]) -> MailOutboundConfig:
    d = dict(d or {})
    if "from" in d:
        d["from_addr"] = d.pop("from")
    if "to" in d:                                   # §2.15.2 outbound_json 用 to/cc;配置段用 recipients/cc
        d["recipients"] = d.pop("to")
    names = MailOutboundConfig.__dataclass_fields__.keys()
    return MailOutboundConfig(**{k: d[k] for k in names if k in d})


@dataclass
class MailRoute:
    """一条路由(§2.15.2)。``channel is None and account_id is None`` = 全局默认路由。"""
    id: Optional[int]
    channel: Optional[str]
    account_id: Optional[str]
    inbound: MailInboundConfig = field(default_factory=MailInboundConfig)
    outbound: MailOutboundConfig = field(default_factory=MailOutboundConfig)
    enabled: bool = True
    inbound_template_id: Optional[int] = None
    outbound_template_id: Optional[int] = None

    # ---- 展示与键
    @property
    def name(self) -> str:
        if self.account_id:
            return f"{self.channel}/{self.account_id}"
        return self.channel or GLOBAL_ROUTE_NAME

    @property
    def is_global(self) -> bool:
        return self.channel is None and self.account_id is None

    @property
    def mailbox_key(self) -> str:
        return mailbox_key(self.inbound.host, self.inbound.user)

    @property
    def cursor_owner(self) -> str:
        """§2.15.3:``cursors.owner = mail:<mailbox_key>``。"""
        return f"mail:{self.mailbox_key}"

    @property
    def level(self) -> str:
        """§2.3.6「权限级别」:邮件入口经验签的发件人视同路由 ``level``(默认 admin,可降 write/read)。"""
        return self.inbound.level

    # ---- headers.build_headers / recipients_of 需要的三个面
    @property
    def outbound_from(self) -> str:
        return self.outbound.effective_from()

    @property
    def outbound_to(self) -> list[str]:
        return list(self.outbound.recipients)

    @property
    def outbound_cc(self) -> list[str]:
        return list(self.outbound.cc)

    @property
    def session_overrides(self) -> list[dict[str, Any]]:
        return list(self.outbound.session_overrides)

    def covers(self, account_id: str, channel: Optional[str] = None) -> bool:
        """§2.15.1:指令邮件里的 ``账号`` 必须属于**该路由覆盖的账号集合**,否则 ``ROUTE_MISMATCH``。

        账号级 = 那一个账号;通道级 = 该通道全部账号;全局 = 全部。
        """
        if self.is_global:
            return True
        if self.account_id:
            return account_id == self.account_id
        return channel is not None and channel == self.channel

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MailRoute":
        """从 ``mail_routes`` 一行构造(02 §3.1 DDL 的列)。"""
        def _json(v: Any) -> dict[str, Any]:
            if isinstance(v, dict):
                return v
            try:
                d = json.loads(v or "{}")
            except ValueError:
                return {}
            return d if isinstance(d, dict) else {}
        return cls(
            id=row.get("id"),
            channel=row.get("channel"),
            account_id=row.get("account_id"),
            inbound=_inbound_from_json(_json(row.get("inbound_json"))),
            outbound=_outbound_from_json(_json(row.get("outbound_json"))),
            enabled=bool(row.get("enabled", 1)),
            inbound_template_id=row.get("inbound_template_id"),
            outbound_template_id=row.get("outbound_template_id"),
        )

    @classmethod
    def materialize_global(cls, cfg: MailConfig, *, row_id: Optional[int] = None) -> "MailRoute":
        """§3.1 末:全局默认行由启动时从 ``agent.toml [mail.inbound]/[mail.outbound]`` **物化**(单一来源仍是 TOML)。"""
        return cls(id=row_id, channel=None, account_id=None, inbound=cfg.inbound, outbound=cfg.outbound,
                   enabled=cfg.enabled)


class RouteTable:
    """路由表(§2.15.1 查找顺序 + §2.15.3 读线程按 ``mailbox_key`` 去重)。"""

    def __init__(self, routes: list[MailRoute]):
        self.routes = list(routes)

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "RouteTable":
        return cls([MailRoute.from_row(r) for r in rows])

    def _find(self, channel: Optional[str], account_id: Optional[str]) -> Optional[MailRoute]:
        for r in self.routes:
            if r.enabled and r.channel == channel and r.account_id == account_id:
                return r
        return None

    def lookup(self, channel: Optional[str], account_id: Optional[str] = None) -> Optional[MailRoute]:
        """§2.15.1:① 账号级 → ② 通道级 → ③ 全局默认;``enabled=false`` 的路由跳过查找。"""
        if channel and account_id:
            hit = self._find(channel, account_id)
            if hit is not None:
                return hit
        if channel:
            hit = self._find(channel, None)
            if hit is not None:
                return hit
        return self._find(None, None)

    def inbound_routes(self) -> list[MailRoute]:
        """开了 inbound 的路由(每条各自收信)。"""
        return [r for r in self.routes if r.enabled and r.inbound.enabled and r.inbound.host]

    def mailboxes(self) -> dict[str, list[MailRoute]]:
        """§2.15.3:**按 ``mailbox_key`` 去重,一个物理邮箱一条读线程**;多条路由共用同一邮箱时共用那条线程。"""
        out: dict[str, list[MailRoute]] = {}
        for r in self.inbound_routes():
            out.setdefault(r.mailbox_key, []).append(r)
        return out

    def route_for_inbox_account(self, mailbox: str, account_id: str, channel: Optional[str]) -> Optional[MailRoute]:
        """§2.15.3:一封邮件落在哪条路由——同邮箱的路由里,按 ``账号`` 所属通道/账号归属。"""
        candidates = [r for r in self.inbound_routes() if r.mailbox_key == mailbox]
        for r in candidates:
            if r.account_id == account_id:
                return r
        for r in candidates:
            if r.account_id is None and channel is not None and r.channel == channel:
                return r
        for r in candidates:
            if r.is_global:
                return r
        return candidates[0] if candidates else None

    def scope_subject_prefixes(self, mailbox: str, prefixes_of: Any) -> list[str]:
        """§2.15.3:清理范围仍按邮箱圈定,``scope_subject_prefix`` 取该邮箱上**所有路由**模板前缀的并集。"""
        out: list[str] = []
        for r in self.inbound_routes():
            if r.mailbox_key != mailbox:
                continue
            for p in prefixes_of(r):
                if p and p not in out:
                    out.append(p)
        return out
