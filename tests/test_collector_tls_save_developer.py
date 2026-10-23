"""TLS credential replacement on old current-settings rows; no network or native send."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import socket
from urllib.request import OpenerDirector

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
import pytest

from qtrade_agent.ossbox.collector_settings import CollectorReportingSettings, SETTINGS_KEY
from qtrade_agent.ossbox.errors import OssboxSettingsError
from qtrade_agent.store import Store
from qtrade_agent.vault_client import FakeVault


@pytest.fixture
def replacement(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('settings save must not open a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(OpenerDirector, 'open', forbidden)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-tls-save-only')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
            .not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path/'client.crt', tmp_path/'client.key'
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())); kp.chmod(0o600)
    store = Store(str(tmp_path/'collector.db')).open()
    vault = FakeVault()
    settings = CollectorReportingSettings(store, vault)
    revision = 'a'*32
    legacy = {'workbench_name': 'Synthetic current desk', 'base_url': 'https://its-relay.yingmi.cn',
              'workbench_id': settings.workbench_id, 'revision': revision,
              'credentials_ref': f'vault://settings/collector-reporting/{revision}/credentials'}
    store.settings_set(SETTINGS_KEY, legacy)
    body = {'workbench_name': legacy['workbench_name'], 'base_url': legacy['base_url'],
            'client_cert_file': str(cp), 'client_key_file': str(kp), 'ca_file': str(cp),
            'key': 'synthetic-complete-replacement-key', 'sm4_key': '0123456789abcdef'}
    try:
        yield settings, vault, legacy, body
    finally:
        store.close()


@pytest.mark.parametrize('prior_credentials_available', [True, False])
def test_full_tls_replacement_of_saved_row_without_tls_fields_is_atomic_and_idempotent(replacement, prior_credentials_available):
    settings, vault, legacy, body = replacement
    async def run():
        if prior_credentials_available:
            await vault.put(legacy['credentials_ref'], json.dumps({'key': 'synthetic-prior-key',
                                                               'sm4_key': 'fedcba9876543210'}))
        before_identity = settings.workbench_id
        await settings.save(body, actor='synthetic-replacement')
        saved = settings.saved()
        assert saved['revision'] != legacy['revision'] and saved['workbench_id'] == before_identity
        for field in ('client_cert_file', 'client_key_file', 'ca_file'):
            assert saved[field] == body[field] and settings.view()[field] == body[field]
        assert await settings._credentials() == {'key': body['key'], 'sm4_key': body['sm4_key']}
        assert 'key' not in saved and 'sm4_key' not in saved
        assert body['key'] not in json.dumps(settings.view())
        count = len(vault.entries)
        await settings.save(body, actor='synthetic-identical-save')
        assert settings.saved() == saved and len(vault.entries) == count
    asyncio.run(run())


def test_missing_tls_replacement_still_rejected_before_persistence(replacement):
    settings, vault, legacy, body = replacement
    async def run():
        incomplete = {key: value for key, value in body.items() if key not in ('client_cert_file', 'client_key_file')}
        with pytest.raises(OssboxSettingsError) as caught:
            await settings.save(incomplete, actor='synthetic-missing-tls')
        assert caught.value.reason == 'file_tls_client_cert_required'
        assert settings.saved() == legacy and not vault.entries
    asyncio.run(run())
