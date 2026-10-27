"""Synthetic organization publication failures; no native account or gateway I/O."""
import json
from types import SimpleNamespace

import pytest

from qtrade_agent.ossbox.institution import InstitutionCollectorHost, SnapshotPublisher
from relay.file_gateway import GatewayAuth
from relay.gateway_paths import InstitutionKeys, ORGANIZATION_PATH
from relay.protocol import Codec


@pytest.fixture
def setup(tmp_path):
    now = [1800000000.0]
    uploads = []
    flags = {'upload_fails': False}
    keys = InstitutionKeys.from_auth(GatewayAuth('synthetic-org-publisher', b'0123456789abcdef'))
    class Client:
        def __init__(self, path): self.path = path
        def upload(self, name, blob):
            if flags['upload_fails']:
                raise RuntimeError('synthetic-private-upload-details')
            uploads.append((self.path, name, blob))
    publisher = SnapshotPublisher(tmp_path, keys, 'synthetic-wb', 'Synthetic', Client, clock=lambda: now[0])
    def organization_packets():
        codec = Codec(keys.binding('synthetic-wb', 'organization'), clock=lambda: now[0])
        return [codec.unpack(blob) for path, name, blob in uploads if path == ORGANIZATION_PATH]
    return SimpleNamespace(now=now, uploads=uploads, flags=flags, publisher=publisher, packets=organization_packets)


READY = {'org_state': 'ready', 'org': {'departments': [{'name': 'Synthetic desk'}], 'members': []}}


@pytest.mark.parametrize('state', ['error', 'unreported'])
def test_failure_is_signed_metadata_without_org_and_retains_exact_safe_code(setup, state):
    s = setup
    s.publisher.organization(READY)
    success = s.publisher.last('organization:organization')
    s.now[0] += 5
    with pytest.raises(ValueError) as raised:
        s.publisher.organization({'org_state': state, 'org_error': 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR',
                                  'org': {'secret': 'synthetic-private-content'}})
    assert raised.value.code == 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR'
    packet = s.packets()[-1]['payload']
    assert packet['state'] == state and packet['error_code'] == raised.value.code
    assert 'org' not in packet and 'synthetic-private' not in json.dumps(packet)
    assert s.publisher.last('organization:organization') == success
    assert s.publisher.last('organization-error:organization') == s.now[0]


@pytest.mark.parametrize('code', [None, 'private detail', 'ORG_bad', 'ORG_', 'ORG_' + 'A' * 76, {'private': 'value'}])
def test_untrusted_error_code_is_replaced_with_safe_fallback(setup, code):
    with pytest.raises(ValueError) as raised:
        setup.publisher.organization({'org_state': 'error', 'org_error': code})
    assert raised.value.code == 'ORGANIZATION_REFRESH_FAILED'
    assert setup.packets()[-1]['payload']['error_code'] == 'ORGANIZATION_REFRESH_FAILED'


def test_repeated_error_is_persistently_throttled_without_hiding_failure(setup):
    s = setup
    failure = {'org_state': 'error', 'org_error': 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR'}
    for advance in (0, 10, 49):
        s.now[0] += advance
        with pytest.raises(ValueError): s.publisher.organization(failure)
    assert len(s.packets()) == 1
    s.now[0] += 1
    with pytest.raises(ValueError): s.publisher.organization(failure)
    assert len(s.packets()) == 2
    s.now[0] += 1
    with pytest.raises(ValueError):
        s.publisher.organization({'org_state': 'error', 'org_error': 'ORG_SOURCE_UNAVAILABLE'})
    assert len(s.packets()) == 3


def test_failed_upload_is_not_stamped_as_published(setup):
    s = setup
    s.flags['upload_fails'] = True
    with pytest.raises(ValueError) as raised:
        s.publisher.organization({'org_state': 'error', 'org_error': 'ORG_SOURCE_UNAVAILABLE'})
    assert raised.value.code == 'ORG_SOURCE_UNAVAILABLE'
    assert s.publisher.last('organization:organization') == 0
    assert s.publisher.last('organization-error:organization') == 0
    assert s.packets() == []


def test_metadata_keeps_request_pending_until_real_success_then_clears_error(setup):
    s = setup
    host = object.__new__(InstitutionCollectorHost)
    host.publisher, host.clock, host.errors = s.publisher, lambda: s.now[0], {}
    host.rows = lambda: [{'id': 'synthetic-slot', 'self_uid': '123456789', 'channel': 'qidian'}]
    host.online = lambda *args: True
    host.friends_provider = lambda row: []
    host.organization_provider = lambda row: {'org_state': 'error', 'org_error': 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR'}
    s.publisher.request_sync('123456789', True)
    request = s.publisher.pending_sync()
    host.metadata_once()
    assert s.publisher.pending_sync() == request
    assert host.errors['contacts:123456789'] == 'ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR'
    assert s.publisher.last('organization:organization') == 0
    s.now[0] += 1
    host.organization_provider = lambda row: READY
    host.metadata_once()
    assert s.publisher.pending_sync() == [] and host.errors == {}
    assert s.publisher.last('organization:organization') == s.now[0]
    assert s.publisher.last('organization-error:organization') == 0
    assert s.packets()[-1]['payload']['state'] == 'ready'


def test_provider_exception_publishes_generic_error_without_private_text(setup):
    s = setup
    host = object.__new__(InstitutionCollectorHost)
    host.publisher, host.clock, host.errors = s.publisher, lambda: s.now[0], {}
    host.rows = lambda: [{'id': 'synthetic-slot', 'self_uid': '123456789', 'channel': 'qidian'}]
    host.online = lambda *args: True
    host.friends_provider = lambda row: []
    def broken(row): raise RuntimeError('synthetic-private-provider-details')
    host.organization_provider = broken
    s.publisher.request_sync('123456789', True)
    host.metadata_once()
    assert host.errors['contacts:123456789'] == 'ORGANIZATION_REFRESH_FAILED'
    assert s.packets()[-1]['payload']['state'] == 'error'
    assert 'synthetic-private' not in json.dumps(s.packets())
    assert s.publisher.pending_sync()
