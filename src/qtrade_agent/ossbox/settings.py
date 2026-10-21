"""Page-managed STS configuration; only its Vault reference is stored in SQLite."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from ..vault_client import VaultUnavailable
from .object_io import ObjectIO
from .oss_http import OssV1Box
from .sts import StsCredentialProvider, StsError, _validate_url

SETTINGS_KEY = "ossbox.config"
PUBLIC_DEFAULTS = {"sts_url": "", "allow_http": False, "endpoint": "", "bucket": "", "workbench_id": ""}
SECRET_FIELDS = ("auth_key", "sm4_key")
log = logging.getLogger(__name__)


class OssboxSettingsError(ValueError):
    def __init__(self, reason: str, *, unavailable: bool = False):
        super().__init__(reason)
        self.reason, self.unavailable = reason, unavailable


def validate_public(values: dict) -> dict:
    result = {key: values.get(key, default) for key, default in PUBLIC_DEFAULTS.items()}
    if type(result["allow_http"]) is not bool:
        raise OssboxSettingsError("sts_http_option_invalid")
    for key in ("sts_url", "endpoint", "bucket", "workbench_id"):
        value = result[key]
        if not isinstance(value, str) or len(value) > 2048:
            raise OssboxSettingsError("ossbox_configuration_invalid")
        result[key] = value.strip()
    try:
        _validate_url(result["sts_url"], allow_http=result["allow_http"])
    except ValueError:
        raise OssboxSettingsError("sts_url_invalid") from None
    if result["endpoint"]:
        try:
            endpoint = urlsplit(result["endpoint"] if "://" in result["endpoint"] else "https://" + result["endpoint"])
            if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
                    or endpoint.password is not None or endpoint.path not in ("", "/")
                    or endpoint.query or endpoint.fragment or any(ord(c) < 33 for c in result["endpoint"])):
                raise ValueError
            _ = endpoint.port
        except ValueError:
            raise OssboxSettingsError("oss_endpoint_invalid") from None
    if result["bucket"] and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", result["bucket"]):
        raise OssboxSettingsError("oss_bucket_invalid")
    if result["workbench_id"] and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", result["workbench_id"]):
        raise OssboxSettingsError("oss_workbench_invalid")
    return result


class OssboxSettings:
    def __init__(self, store, vault):
        self.store, self.vault = store, vault
        self.applied_revision: str | None = None
        self._lock = asyncio.Lock()
        self._test_io = ObjectIO()

    def has_saved(self) -> bool:
        return self.store.con.execute("SELECT 1 FROM settings WHERE key=?", (SETTINGS_KEY,)).fetchone() is not None

    def saved(self) -> dict | None:
        value = self.store.settings_get(SETTINGS_KEY)
        if value is None:
            if self.has_saved():
                raise OssboxSettingsError("ossbox_configuration_invalid", unavailable=True)
            return None
        if not isinstance(value, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("revision", ""))):
            raise OssboxSettingsError("ossbox_configuration_invalid", unavailable=True)
        expected = "vault://settings/ossbox/" + value["revision"] + "/credentials"
        if value.get("credentials_ref") != expected:
            raise OssboxSettingsError("ossbox_configuration_invalid", unavailable=True)
        validate_public(value)
        return value

    def view(self) -> dict:
        saved = self.saved()
        public = {key: (saved or {}).get(key, default) for key, default in PUBLIC_DEFAULTS.items()}
        return {**public, "saved": saved is not None,
                "auth_key_configured": saved is not None, "sm4_key_configured": saved is not None}

    async def _secrets(self, saved: dict | None) -> dict:
        if saved is None:
            return {}
        try:
            raw = await self.vault.read(saved["credentials_ref"])
        except VaultUnavailable:
            raise OssboxSettingsError("vault_unavailable", unavailable=True) from None
        try:
            data = json.loads(raw) if raw is not None else None
            if not isinstance(data, dict) or set(data) != set(SECRET_FIELDS):
                raise ValueError
            return data
        except (TypeError, ValueError):
            raise OssboxSettingsError("sts_credentials_missing", unavailable=True) from None

    async def _prepare(self, body: dict, saved: dict | None):
        if not isinstance(body, dict) or set(body) - set(PUBLIC_DEFAULTS) - set(SECRET_FIELDS):
            raise OssboxSettingsError("ossbox_unknown_field")
        if set(PUBLIC_DEFAULTS) - set(body):
            raise OssboxSettingsError("ossbox_fields_missing")
        public = validate_public(body)
        secrets = await self._secrets(saved) if any(key not in body for key in SECRET_FIELDS) else {}
        secrets.update({key: body[key] for key in SECRET_FIELDS if key in body})
        try:
            key, sm4_key = secrets.get("auth_key"), secrets.get("sm4_key")
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 4096:
                raise OssboxSettingsError("sts_auth_key_invalid")
            if not isinstance(sm4_key, str) or len(sm4_key.encode("utf-8")) != 16:
                raise OssboxSettingsError("sts_sm4_key_invalid")
            provider = StsCredentialProvider(public["sts_url"], key, sm4_key.encode("utf-8"),
                allow_http=public["allow_http"])
        except UnicodeError:
            raise OssboxSettingsError("sts_credentials_invalid") from None
        return public, secrets, provider

    async def save(self, body: dict, *, actor: str) -> None:
        async with self._lock:
            previous = self.saved()
            public, secrets, _provider = await self._prepare(body, previous)
            revision = uuid4().hex
            name = f"settings/ossbox/{revision}/credentials"
            try:
                # One Vault value keeps the key pair atomic. It is never sent back.
                await self.vault.put(name, json.dumps(secrets, ensure_ascii=False), scope="other")
            except VaultUnavailable:
                raise OssboxSettingsError("vault_unavailable", unavailable=True) from None
            try:
                self.store.settings_set(SETTINGS_KEY, {**public, "revision": revision,
                    "credentials_ref": "vault://" + name}, actor=actor)
            except Exception:
                await self._discard(name)
                raise OssboxSettingsError("ossbox_save_failed", unavailable=True) from None
            if previous:
                await self._discard(previous["credentials_ref"])

    async def _discard(self, name: str) -> None:
        try:
            await self.vault.delete(name)
        except Exception:
            log.warning("Unused OSS credential cleanup deferred")

    async def test(self, body: dict) -> dict:
        async with self._lock:
            # STS test is stateless. All required secrets must be in this request;
            # do not consult SQLite, Vault, or earlier successful test results.
            _public, _secrets, provider = await self._prepare(body, None)
        try:
            credentials = await asyncio.wait_for(self._test_io.run(provider.get_credentials), timeout=15)
        except StsError as exc:
            raise OssboxSettingsError(str(exc), unavailable=True) from None
        except TimeoutError:
            raise OssboxSettingsError("sts_test_timeout", unavailable=True) from None
        except Exception:
            raise OssboxSettingsError("sts_test_unavailable", unavailable=True) from None
        return {"authenticated": True,
                "expiration": datetime.fromtimestamp(credentials.expiration, timezone.utc).isoformat().replace("+00:00", "Z")}

    async def runtime(self) -> tuple[dict, OssV1Box | None] | None:
        """Called once before scheduling starts, never from a save/test request."""
        async with self._lock:
            saved = self.saved()
            if saved is None:
                return None
            public, _secrets, provider = await self._prepare({key: saved[key] for key in PUBLIC_DEFAULTS}, saved)
            box = OssV1Box(endpoint=public["endpoint"], bucket=public["bucket"], credentials_provider=provider) if public["endpoint"] else None
            return saved, box
