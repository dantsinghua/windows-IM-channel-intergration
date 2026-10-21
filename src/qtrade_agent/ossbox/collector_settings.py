"""Collector-owned configuration and unpublished directory preview.

Saving here does not edit a trader's connection, change a binding, publish a
directory, or enable native sending. The peer discovery contract is pending.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urlsplit
from uuid import uuid4

from ..vault_client import VaultUnavailable
from .keys import book_object_name, index_object_name
from .object_io import ObjectIO
from .settings import OssboxSettingsError

SETTINGS_KEY = "collector.reporting"
IDENTITY_KEY = "collector.identity"
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
log = logging.getLogger(__name__)


def public_values(values):
    name, base = values.get("workbench_name"), values.get("base_url")
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise OssboxSettingsError("collector_workbench_name_invalid")
    if not isinstance(base, str) or len(base) > 2048:
        raise OssboxSettingsError("collector_url_invalid")
    try:
        base = base.strip().rstrip("/")
        url = urlsplit(base)
        if (url.scheme not in ("https", "http") or not url.hostname
                or url.username is not None or url.password is not None
                or "?" in base or "#" in base or "%" in url.path
                or ".." in url.path.split("/") or any(c.isspace() for c in base)
                or url.path.rstrip("/").endswith("/api/files")):
            raise ValueError()
        _ = url.port
    except ValueError:
        raise OssboxSettingsError("collector_url_invalid") from None
    return {"workbench_name": name.strip(), "base_url": base}


class CollectorReportingSettings:
    state = "pending_alignment"
    applied_revision = None
    baseline_revision = None

    def __init__(self, store, vault, seed=None):
        self.store, self.vault = store, vault
        self._seed = dict(seed or {})
        self._lock = asyncio.Lock()
        self._io = ObjectIO()
        self._credentials_unavailable = False
        identity = store.settings_get(IDENTITY_KEY)
        if identity is None:
            proposed = self._seed.get("workbench_id", "")
            value = proposed if isinstance(proposed, str) and _IDENT.fullmatch(proposed) else uuid4().hex
            identity = {"workbench_id": value}
            store.settings_set(IDENTITY_KEY, identity, actor="collector-identity")
        if (not isinstance(identity, dict) or not isinstance(identity.get("workbench_id"), str)
                or not _IDENT.fullmatch(identity["workbench_id"])):
            raise OssboxSettingsError("collector_configuration_invalid", unavailable=True)
        self.workbench_id = identity["workbench_id"]

    def saved(self):
        try:
            value = self.store.settings_get(SETTINGS_KEY)
            if value is None:
                return None
            revision = value["revision"]
            if (not re.fullmatch(r"[0-9a-f]{32}", revision)
                    or value["workbench_id"] != self.workbench_id
                    or value["credentials_ref"] != f"vault://settings/collector-reporting/{revision}/credentials"):
                raise ValueError()
            public_values(value)
            return value
        except (KeyError, TypeError, ValueError):
            raise OssboxSettingsError("collector_configuration_invalid", unavailable=True) from None

    def view(self):
        saved = self.saved()
        source = saved or self._seed
        has_keys = bool(saved) or bool(source.get("key") and source.get("sm4_key"))
        has_keys = has_keys and not self._credentials_unavailable
        name, base = source.get("workbench_name", ""), source.get("base_url", "")
        configured = False
        try:
            public_values({"workbench_name": name, "base_url": base})
            configured = has_keys
        except OssboxSettingsError:
            pass
        return {"workbench_id": self.workbench_id, "workbench_name": name, "base_url": base,
                "saved": saved is not None, "configured": configured,
                "key_configured": has_keys, "sm4_key_configured": has_keys}

    async def view_async(self):
        if self.saved():
            try:
                await self._credentials()
            except OssboxSettingsError as exc:
                return {**self.view(), "configured": False, "credential_error": exc.reason}
        return self.view()

    async def _credentials(self):
        saved = self.saved()
        if saved is None:
            return {key: self._seed.get(key) for key in ("key", "sm4_key")}
        try:
            raw = await self.vault.read(saved["credentials_ref"])
            values = json.loads(raw) if raw is not None else None
            if not isinstance(values, dict) or set(values) != {"key", "sm4_key"}:
                raise ValueError()
            if (not isinstance(values["key"], str) or not values["key"]
                    or not isinstance(values["sm4_key"], str) or len(values["sm4_key"].encode("utf-8")) != 16):
                raise ValueError()
            self._credentials_unavailable = False
            return values
        except VaultUnavailable:
            raise OssboxSettingsError("vault_unavailable", unavailable=True) from None
        except (ValueError, TypeError, UnicodeError):
            self._credentials_unavailable = True
            raise OssboxSettingsError("collector_credentials_missing", unavailable=True) from None

    async def _prepare(self, body):
        if not isinstance(body, dict) or set(body) - {"workbench_name", "base_url", "key", "sm4_key"}:
            raise OssboxSettingsError("collector_unknown_field")
        source = self.saved() or self._seed
        public = public_values({key: body.get(key, source.get(key, ""))
                                for key in ("workbench_name", "base_url")})
        if {"key", "sm4_key"} <= body.keys():
            secret = {key: body[key] for key in ("key", "sm4_key")}
        else:
            secret = await self._credentials()
            secret.update({key: body[key] for key in ("key", "sm4_key") if key in body})
        key, sm4 = secret.get("key"), secret.get("sm4_key")
        try:
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 4096:
                raise OssboxSettingsError("file_auth_key_invalid")
            if not isinstance(sm4, str) or len(sm4.encode("utf-8")) != 16:
                raise OssboxSettingsError("file_auth_sm4_length_invalid")
        except UnicodeError:
            raise OssboxSettingsError("collector_credentials_missing") from None
        return public, secret

    async def save(self, body, *, actor):
        async with self._lock:
            public, secret = await self._prepare(body)
            previous = self.saved()
            if previous and public == {key: previous[key] for key in public}:
                try:
                    unchanged = secret == await self._credentials()
                except OssboxSettingsError as exc:
                    if exc.reason != "collector_credentials_missing" or not {"key", "sm4_key"} <= body.keys():
                        raise
                    unchanged = False
                if unchanged:
                    return
            revision = uuid4().hex
            name = f"settings/collector-reporting/{revision}/credentials"
            try:
                await self.vault.put(name, json.dumps(secret, ensure_ascii=False), scope="other")
            except VaultUnavailable:
                raise OssboxSettingsError("vault_unavailable", unavailable=True) from None
            try:
                self.store.settings_set(SETTINGS_KEY, {**public, "workbench_id": self.workbench_id,
                    "revision": revision, "credentials_ref": "vault://" + name}, actor=actor)
            except Exception:
                try:
                    await self.vault.delete(name)
                except Exception:
                    log.warning("Unused collector credential cleanup deferred")
                raise OssboxSettingsError("collector_save_failed", unavailable=True) from None
            self._credentials_unavailable = False

    async def test(self, body):
        async with self._lock:
            public, secret = await self._prepare(body)
        try:
            from relay.file_gateway import FileGatewayClient, FileGatewayError, GatewayAuth
            auth = GatewayAuth(secret["key"], secret["sm4_key"].encode("utf-8"))
            options = {}
            # A deployment DNS pin is reused only for the same hostname. It is
            # never applied to a different address entered in the draft.
            if urlsplit(public["base_url"]).hostname == self._seed.get("hostname"):
                from relay.file_gateway_connection import pinned_opener
                options["opener"] = pinned_opener({**self._seed, **public})
            from relay.gateway_paths import DIRECTORY_PATH
            client = FileGatewayClient(public["base_url"], auth, path=DIRECTORY_PATH,
                allow_http=urlsplit(public["base_url"]).scheme == "http", timeout=10, **options)
            pending = await asyncio.wait_for(self._io.run(client.pending), timeout=15)
        except TimeoutError:
            raise OssboxSettingsError("collector_test_timeout", unavailable=True) from None
        except Exception as exc:
            reason = "file_auth_rejected" if getattr(exc, "code", "") == "FILE_AUTH_REJECTED" else "collector_test_unavailable"
            raise OssboxSettingsError(reason, unavailable=True) from None
        return {"authenticated": True, "carrier": "its-relay", "pending": pending, "token_generated": True}

    def catalog(self):
        accounts = []
        for row in self.store.list_accounts(channel="qidian"):
            uid, slot = str(row.get("self_uid") or ""), str(row["id"])
            if row.get("state") == "deleted" or not _IDENT.fullmatch(uid) or not _IDENT.fullmatch(slot):
                continue
            accounts.append({"account_id": slot, "account_uid": uid, "channel": "qidian",
                "account_nick": row.get("self_nick") or row.get("label") or "",
                "collector_id": slot, "book_path": book_object_name(self.workbench_id, "qidian", uid, slot)})
        return {"workbench_id": self.workbench_id, "workbench_name": self.view()["workbench_name"],
                "index_path": index_object_name(self.workbench_id),
                "accounts": sorted(accounts, key=lambda row: (row["account_uid"], row["account_id"])),
                "published": False, "alignment_required": True}
