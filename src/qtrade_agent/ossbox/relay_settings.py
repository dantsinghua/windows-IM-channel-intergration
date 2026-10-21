"""Page-managed authentication for the deployed its-relay config.yaml."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from .object_io import ObjectIO
from .settings import OssboxSettingsError


class RelaySettings:
    def __init__(self, config_file, base_url):
        self.config_file = Path(config_file).resolve() if config_file else None
        self.base_url = base_url
        self._lock = asyncio.Lock()
        self._test_io = ObjectIO()
        self.baseline_revision = self._revision()
        self.applied_revision = None

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        if env.get("XUNJIA_COLLECTOR_VERSION") != "2.2":
            return None
        name = env.get("XUNJIA_COLLECTOR_CONFIG_FILE")
        if not name:
            return None
        try:
            path = Path(name)
            if path.stat().st_size > 16384:
                raise ValueError()
            config = json.loads(path.read_text(encoding="utf-8"))
            if "file_gateway" not in config:
                return None
            gateway = config["file_gateway"]
            auth_file = Path(gateway["auth_file"])
            if not auth_file.is_absolute():
                auth_file = path.parent / auth_file
            if env.get("XUNJIA_MQ_CONNECTION_CONFIG"):
                return ManagedRelaySettings(env["XUNJIA_MQ_CONNECTION_CONFIG"])
            return cls(auth_file, env.get("XUNJIA_FILE_GATEWAY_URL", ""))
        except (OSError, TypeError, ValueError, KeyError):
            # A broken new deployment must not fall back to the old STS profile.
            return cls(None, "")

    def _revision(self):
        try:
            if self.config_file is not None and self.config_file.stat().st_size <= 16384:
                return hashlib.sha256(self.config_file.read_bytes()).hexdigest()
        except OSError:
            pass
        return None

    def saved(self):
        revision = self._revision()
        return {"revision": revision} if revision else None

    def _document(self):
        if self.config_file is None:
            raise OssboxSettingsError("relay_not_configured", unavailable=True)
        try:
            if not self.config_file.exists():
                return {}
            if self.config_file.stat().st_size > 16384:
                raise ValueError()
            import yaml
            class UniqueLoader(yaml.SafeLoader):
                pass
            def mapping(loader, node, deep=False):
                result = {}
                for key, value in loader.construct_pairs(node, deep=deep):
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
            value = yaml.load(self.config_file.read_text(encoding="utf-8-sig"), Loader=UniqueLoader)
            if not isinstance(value, dict) or ("auth" in value and not isinstance(value["auth"], dict)):
                raise ValueError()
            return value
        except Exception:
            raise OssboxSettingsError("file_auth_config_invalid") from None

    def _prepare(self, body):
        if not isinstance(body, dict) or set(body) - {"key", "sm4_key"}:
            raise OssboxSettingsError("relay_unknown_field")
        document = self._document()
        values = document.get("auth", document)
        previous = {key: values.get(key) for key in ("key", "sm4_key")}
        values.update(body)
        try:
            from relay.file_gateway import GatewayAuth, FileGatewayError
        except ImportError:
            raise OssboxSettingsError("relay_not_configured", unavailable=True) from None
        try:
            key, sm4 = values.get("key"), values.get("sm4_key")
            if not isinstance(sm4, str):
                raise OssboxSettingsError("file_auth_sm4_length_invalid")
            auth = GatewayAuth(key, sm4.encode("utf-8"))
        except FileGatewayError as exc:
            raise OssboxSettingsError(exc.code.lower()) from None
        except UnicodeError:
            raise OssboxSettingsError("file_auth_config_invalid") from None
        return document, auth, previous != {key: values.get(key) for key in ("key", "sm4_key")}

    def view(self):
        configured = False
        try:
            self._prepare({})
            configured = True
        except OssboxSettingsError:
            pass
        try:
            url = urlsplit(self.base_url)
            valid_url = (url.scheme in ("https", "http") and url.hostname and url.username is None
                         and url.password is None and not url.query and not url.fragment
                         and not any(c.isspace() for c in self.base_url))
            _ = url.port
        except (TypeError, ValueError):
            valid_url = False
        return {"base_url": self.base_url if valid_url else "", "config_file": "config.yaml",
                "configured": configured, "key_configured": configured, "sm4_key_configured": configured}

    async def save(self, body, *, actor):
        async with self._lock:
            document, _auth, changed = self._prepare(body)
            if not changed:
                try:
                    self.config_file.chmod(0o600)
                except OSError:
                    raise OssboxSettingsError("relay_save_failed", unavailable=True) from None
                return
            temporary = None
            try:
                import yaml
                data = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")
                if len(data) > 16384:
                    raise ValueError()
                # This path comes only from the deployment, never from an API request.
                with tempfile.NamedTemporaryFile(dir=self.config_file.parent, prefix=".relay-auth-", delete=False) as stream:
                    temporary = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.config_file)
            except Exception:
                raise OssboxSettingsError("relay_save_failed", unavailable=True) from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    async def test(self, body):
        async with self._lock:
            _document, auth, _changed = self._prepare(body)
            try:
                from relay.file_gateway import FileGatewayClient, FileGatewayError
                client = FileGatewayClient(self.base_url, auth,
                    allow_http=urlsplit(self.base_url).scheme == "http", timeout=10)
            except FileGatewayError as exc:
                raise OssboxSettingsError(exc.code.lower()) from None
        try:
            pending = await asyncio.wait_for(self._test_io.run(client.pending), timeout=15)
        except FileGatewayError as exc:
            raise OssboxSettingsError(exc.code.lower(), unavailable=True) from None
        except TimeoutError:
            raise OssboxSettingsError("relay_test_timeout", unavailable=True) from None
        except Exception:
            raise OssboxSettingsError("relay_test_unavailable", unavailable=True) from None
        return {"authenticated": True, "carrier": "its-relay", "pending": pending, "token_generated": True}


class ManagedRelaySettings(RelaySettings):
    """Use xunjia's existing MQ editor, keeping remote and local router auth separate."""

    def __init__(self, connection_file):
        self.connection_file = Path(connection_file).resolve()
        self.host = None
        self._refresh()
        super().__init__(self.config_file, self.base_url)

    def _refresh(self):
        try:
            if self.connection_file.stat().st_size > 65536 or self.connection_file.stat().st_mode & 0o077:
                raise ValueError()
            config = json.loads(self.connection_file.read_bytes())
            auth = Path(config["auth_file"])
            self.config_file = auth if auth.is_absolute() else self.connection_file.parent / auth
            self.base_url = config["base_url"]
        except Exception:
            raise OssboxSettingsError("relay_not_configured", unavailable=True) from None

    def _revision(self):
        try:
            return hashlib.sha256(self.connection_file.read_bytes()).hexdigest()
        except OSError:
            return None

    def _document(self):
        self._refresh()
        return super()._document()

    def bind_host(self, host):
        self.host = host

    def _manager(self):
        from copy import copy
        from types import SimpleNamespace
        from server.mq_settings import MQSettings
        if self.host is None:
            raise OssboxSettingsError("relay_not_configured", unavailable=True)
        binding = self.host.binding
        row = {key: getattr(binding, key) for key in ("owner_user_id", "account_uid", "workbench_id")}
        # MQSettings validates the service's logical direction. This shallow view
        # shares only its authenticated clients; it never polls or publishes.
        adapter = copy(self.host.bridge.adapters["oss"])
        adapter.inbound, adapter.outbound = adapter.outbound, adapter.inbound
        adapter.role = "service"
        repository = SimpleNamespace(binding_for=lambda owner: row if owner == binding.owner_user_id else None)
        transport = SimpleNamespace(adapters={(binding.owner_user_id, binding.account_uid, "oss"): adapter})
        return binding.owner_user_id, MQSettings(repository, transport, self.connection_file)

    def view(self):
        self._refresh()
        if self.host is not None:
            try:
                owner, manager = self._manager()
                status = manager.status(owner)
                if not status.get("editable"):
                    raise ValueError()
                if status.get("applied"):
                    self.applied_revision = status["revision"]
                else:
                    self.applied_revision = None
            except Exception:
                raise OssboxSettingsError("relay_not_configured", unavailable=True) from None
        return super().view()

    async def view_async(self):
        try:
            return await asyncio.wait_for(self._test_io.run(self.view), timeout=15)
        except Exception:
            raise OssboxSettingsError("relay_not_configured", unavailable=True) from None

    def _operation(self, body, *, save):
        try:
            import yaml
            owner, manager = self._manager()
            _binding, _config, raw, _adapter = manager._load(owner)
            _document, auth, changed = self._prepare(body)
            if save and not changed:
                self.config_file.chmod(0o600)
                return None
            candidate = {"config_yaml": yaml.safe_dump({"key": auth.key, "sm4_key": auth.sm4_key.decode("utf-8")}),
                         "expected_revision": hashlib.sha256(raw).hexdigest()}
            if save:
                manager.save(owner, candidate)
                return None
            result = manager.test(owner, candidate)
            if result.get("passed") is not True:
                rejected = any(step.get("detail") == "FILE_AUTH_REJECTED" for step in result.get("steps", []))
                raise OssboxSettingsError("file_auth_rejected" if rejected else "relay_test_unavailable", unavailable=True)
            return {"authenticated": True, "carrier": "its-relay", "token_generated": True}
        except OssboxSettingsError:
            raise
        except Exception:
            raise OssboxSettingsError("relay_save_failed" if save else "relay_test_unavailable", unavailable=True) from None

    async def save(self, body, *, actor):
        async with self._lock:
            try:
                await asyncio.wait_for(self._test_io.run(lambda: self._operation(body, save=True)), timeout=15)
            except TimeoutError:
                raise OssboxSettingsError("relay_save_failed", unavailable=True) from None

    async def test(self, body):
        async with self._lock:
            try:
                return await asyncio.wait_for(self._test_io.run(lambda: self._operation(body, save=False)), timeout=15)
            except TimeoutError:
                raise OssboxSettingsError("relay_test_timeout", unavailable=True) from None
