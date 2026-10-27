"""Independent current-account organization gates. No real ADB, keys or devices."""
from copy import deepcopy
import asyncio
import base64
import hashlib
import hmac
import io
import json
import shlex
import sqlite3
import struct
from types import SimpleNamespace
import zipfile

import pytest

from qtrade_agent.app import AgentApp
from qtrade_agent.adapters.qidian import org, org_native
from qtrade_agent.ossbox.institution import InstitutionCollectorHost


UID_A, UID_B = '9100000001', '9200000001'


def account(slot='qd01', uid=UID_A, serial='synthetic-device-a', container='synthetic-container-a'):
    return {'id': slot, 'seq': 1 if slot == 'qd01' else 2, 'channel': 'qidian', 'self_uid': uid,
            'state': 'running', 'enabled': True, 'deleted_ms': None, 'adb_serial': serial,
            'container_id': container, 'container_name': container, 'created_ms': 100, 'last_started_ms': 200}


def application(rows):
    class Store:
        def list_accounts(self, channel):
            return [deepcopy(row) for row in rows if row['channel'] == channel]
        def get_account_full(self, aid):
            return next((deepcopy(row) for row in rows if row['id'] == aid), None)
    app = object.__new__(AgentApp)
    app.store = Store()
    return app


def test_distinct_accounts_in_distinct_current_sources_do_not_conflict():
    rows = [account(), account('qd02', UID_B, 'synthetic-device-b', 'synthetic-container-b')]
    app = application(rows)
    a, b = [app._xunjia_org_source_identity(row) for row in rows]
    assert a != b and a[0:2] == ('qd01', UID_A) and b[0:2] == ('qd02', UID_B)
    assert app._xunjia_org_source_identity(rows[0]) == a


@pytest.mark.parametrize('collision', ['same-uid', 'same-serial', 'same-container-id', 'same-container-name',
                                      'same-container-name-mixed-id'])
def test_active_account_or_redroid_source_collision_fails_before_native_read(collision):
    first = account()
    second = account('qd02', UID_B, 'synthetic-device-b', 'synthetic-container-b')
    if collision == 'same-uid': second['self_uid'] = UID_A
    elif collision == 'same-serial': second['adb_serial'] = first['adb_serial']
    elif collision == 'same-container-id': second['container_id'] = first['container_id']
    elif collision == 'same-container-name-mixed-id':
        first['container_id'] = 'independent-container-id-distinct-from-name'
        second['container_id'] = None
        second['container_name'] = first['container_name']
    else:
        first['container_id'] = second['container_id'] = None
        second['container_name'] = first['container_name']
    app = application([first, second])
    app._maindb_for_uid = lambda *args: pytest.fail('Conflicting account reached native source')
    with pytest.raises(org.OrgReadError):
        app._xunjia_v22_contacts(first)


@pytest.mark.parametrize('inactive', ['stopped', 'disabled', 'deleted'])
def test_inactive_old_row_does_not_block_the_unique_current_source(inactive):
    first, old = account(), account('qd02', UID_A, 'synthetic-old-device', 'synthetic-old-container')
    if inactive == 'stopped': old['state'] = 'stopped'
    elif inactive == 'disabled': old['enabled'] = False
    else: old['deleted_ms'] = 300
    identity = application([first, old])._xunjia_org_source_identity(first)
    assert identity[0:2] == ('qd01', UID_A)


def test_uid_moved_since_caller_row_snapshot_cannot_return_old_cache():
    stale = account()
    current = {**stale, 'self_uid': UID_B}
    app = application([current])
    app._xunjia_org_cache = {UID_A: {'result': {'org_state': 'ready', 'org': {'departments': [], 'members': []}}}}
    app._maindb_for_uid = lambda *args: pytest.fail('Stale caller identity reached native source')
    with pytest.raises(org.OrgReadError, match='ORG_ACCOUNT_SOURCE_CHANGED'):
        app._xunjia_v22_contacts(stale)


def collector_directory(rows):
    host = object.__new__(InstitutionCollectorHost)
    host.store = application(rows).store
    host.workbench = 'independent-source-routing'
    host.active = True
    host.clock = lambda: 1000
    host.presence_provider = lambda row: None
    host.presence = {(row['id'], row['self_uid']): {'state': 'online', 'confirmed_at': 1000} for row in rows}
    host.hosts = {(row['id'], row['self_uid']): SimpleNamespace(_active=True, _intake_ok=True) for row in rows}
    return host


def test_directory_migration_prefers_unique_running_source_without_deleting_old_row():
    old = {**account(), 'state': 'stopped'}
    active = account('qd02', UID_A, 'synthetic-new-device', 'synthetic-new-container')
    rows = [old, active]
    before = deepcopy(rows)
    host = collector_directory(rows)
    assert host.online('qd01', UID_A) is False
    assert host.online('qd02', UID_A) is True
    [entry] = host.directory_rows()
    assert entry['account_id'] == 'qd02' and entry['online'] is True and entry['capabilities']
    assert rows == before


def test_same_uid_multiple_active_sources_are_offline_but_other_uid_stays_usable():
    rows = [account(), account('qd02', UID_A, 'synthetic-device-b', 'synthetic-container-b'),
            account('qd03', UID_B, 'synthetic-device-c', 'synthetic-container-c')]
    host = collector_directory(rows)
    assert not host.online('qd01', UID_A) and not host.online('qd02', UID_A)
    assert host.online('qd03', UID_B)
    entries = {item['account_uid']: item for item in host.directory_rows()}
    assert entries[UID_A]['online'] is False and entries[UID_A]['capabilities'] == []
    assert entries[UID_B]['online'] is True and entries[UID_B]['account_id'] == 'qd03'


def test_migrated_source_still_requires_fresh_native_presence_and_live_host():
    rows = [{**account(), 'state': 'stopped'}, account('qd02', UID_A, 'synthetic-new-device', 'synthetic-new-container')]
    host = collector_directory(rows)
    host.presence[('qd02', UID_A)]['confirmed_at'] = 939
    assert not host.online('qd02', UID_A) and host.directory_rows()[0]['online'] is False
    host.presence[('qd02', UID_A)]['confirmed_at'] = 1000
    host.hosts[('qd02', UID_A)]._intake_ok = False
    assert host.directory_rows()[0]['online'] is False


def test_no_active_source_remains_offline_without_deleting_directory_rows():
    rows = [{**account(), 'state': 'stopped'}]
    before = deepcopy(rows)
    host = collector_directory(rows)
    assert host.online('qd01', UID_A) is False
    [entry] = host.directory_rows()
    assert entry['online'] is False and entry['capabilities'] == [] and rows == before


def test_migration_retires_old_consumer_before_starting_new_same_uid_host():
    rows = [{**account(), 'state': 'stopped'}, account('qd02', UID_A, 'synthetic-new-device', 'synthetic-new-container')]
    before = deepcopy(rows)
    host = collector_directory(rows)
    events = []
    class AccountHost:
        _active = True
        _intake_ok = True
        def __init__(self, aid): self.aid = aid
        async def stop(self):
            self._active = False
            events.append(('stop', self.aid))
        async def start(self):
            self._active = True
            events.append(('start', self.aid))
    host.hosts = {('qd01', UID_A): AccountHost('qd01')}
    host.histories = {('qd01', UID_A): object()}
    host.errors = {}
    host.publisher = SimpleNamespace(request_sync=lambda *args: None)
    host._make_host = lambda row: AccountHost(row['id'])
    asyncio.run(host.reconcile_accounts())
    assert events == [('stop', 'qd01'), ('start', 'qd02')]
    assert ('qd01', UID_A) not in host.hosts and ('qd01', UID_A) not in host.histories
    assert ('qd02', UID_A) in host.hosts and rows == before


def test_failed_old_host_stop_remains_tracked_blocks_same_uid_but_allows_other_uid():
    rows = [{**account(), 'state': 'stopped'}, account('qd02', UID_A, 'synthetic-new-device', 'synthetic-new-container'),
            account('qd03', UID_B, 'synthetic-other-device', 'synthetic-other-container')]
    before = deepcopy(rows)
    host = collector_directory(rows)
    events = []
    class Consumer:
        _active = True
        _intake_ok = True
        def __init__(self, aid, fail=False): self.aid, self.fail = aid, fail
        async def stop(self):
            events.append(('stop', self.aid))
            if self.fail: raise TimeoutError('synthetic stop still draining')
            self._active = False
        async def start(self): events.append(('start', self.aid))
    old = Consumer('qd01', fail=True)
    history = object()
    host.hosts = {('qd01', UID_A): old}
    host.histories = {('qd01', UID_A): history}
    host.errors = {}
    host.publisher = SimpleNamespace(request_sync=lambda *args: None)
    host._make_host = lambda row: Consumer(row['id'])
    try:
        asyncio.run(host.reconcile_accounts())
    except TimeoutError:
        pass
    assert host.hosts.get(('qd01', UID_A)) is old
    assert host.histories.get(('qd01', UID_A)) is history
    assert ('qd02', UID_A) not in host.hosts and ('start', 'qd02') not in events
    assert ('qd03', UID_B) in host.hosts and ('start', 'qd03') in events
    assert rows == before


def test_conflicting_active_hosts_retire_without_deleting_unknown_ledger_or_other_uid(tmp_path):
    rows = [account(), account('qd02', UID_A, 'synthetic-second-device', 'synthetic-second-container'),
            account('qd03', UID_B, 'synthetic-other-device', 'synthetic-other-container')]
    before = deepcopy(rows)
    host = collector_directory(rows)
    events = []
    class Consumer:
        _active = True
        _intake_ok = True
        def __init__(self, aid):
            self.aid = aid
            self.state_path = tmp_path / (aid + '-ledger.db')
            with sqlite3.connect(self.state_path) as db:
                db.execute('CREATE TABLE command(command_id TEXT,state TEXT)')
                db.execute("INSERT INTO command VALUES('synthetic-unknown','UNKNOWN')")
        async def stop(self):
            events.append(('stop', self.aid)); self._active = False
        async def start(self): pytest.fail('Ambiguous source started a consumer')
    consumers = {row['id']: Consumer(row['id']) for row in rows}
    host.hosts = {(row['id'], row['self_uid']): consumers[row['id']] for row in rows}
    histories = {(row['id'], row['self_uid']): object() for row in rows}
    host.histories = dict(histories)
    host.errors = {}
    host.publisher = SimpleNamespace(request_sync=lambda *args: None)
    host._make_host = lambda row: pytest.fail('Conflict created a new account host')
    ledger_hashes = {aid: hashlib.sha256(consumer.state_path.read_bytes()).hexdigest() for aid, consumer in consumers.items()}
    asyncio.run(host.reconcile_accounts())
    assert set(events) == {('stop', 'qd01'), ('stop', 'qd02')}
    assert set(host.hosts) == {('qd03', UID_B)} and host.hosts[('qd03', UID_B)] is consumers['qd03']
    assert host.histories == {('qd03', UID_B): histories[('qd03', UID_B)]}
    assert rows == before
    for aid, consumer in consumers.items():
        assert hashlib.sha256(consumer.state_path.read_bytes()).hexdigest() == ledger_hashes[aid]
        with sqlite3.connect(consumer.state_path) as db:
            assert db.execute('SELECT state FROM command').fetchone() == ('UNKNOWN',)


def test_remote_reader_does_not_read_previous_local_password_configuration(tmp_path, monkeypatch):
    source = SimpleNamespace(path='/data/data/com.tencent.qidian/databases/' + UID_A + '.db',
        adb='synthetic-never-executed-adb', serial='synthetic-current-device', server_port=16000,
        _shell=lambda _: pytest.fail('Delegation fixture must not execute ADB'))
    old = tmp_path / 'old-instance.json'
    old.write_text(json.dumps({'account_uid': UID_B, 'org_password_file': 'must-not-read.bin'}))
    answer = {'org_state': 'ready', 'org': {'departments': [], 'members': []}, 'org_updated_at': 42, 'org_error': None}
    calls = []
    def native(src, uid, **kwargs):
        calls.append((src, uid))
        return answer
    monkeypatch.setattr(org_native, 'read_native_org', native)
    assert org.read_account_org(source, UID_A, old) == answer
    assert calls == [(source, UID_A)] and not (tmp_path / 'must-not-read.bin').exists()


@pytest.mark.parametrize('protected', [True, False])
def test_local_file_backend_keeps_original_protected_key_gate(tmp_path, monkeypatch, protected):
    source = SimpleNamespace(path=tmp_path / (UID_A + '.db'))
    with sqlite3.connect(tmp_path / (UID_A + '-org.db')) as db:
        db.executescript('CREATE TABLE OrgDepartment(uid TEXT,pid TEXT,name TEXT);CREATE TABLE OrgD2M(did TEXT,mid TEXT,ord INT);CREATE TABLE OrgD2D(did TEXT,ord INT);')
        db.execute("INSERT INTO OrgDepartment VALUES('d','0','Synthetic local department')")
    with sqlite3.connect(tmp_path / (UID_A + '-org-member.db')) as db:
        db.execute('CREATE TABLE OrgMember(uin TEXT,name TEXT,account TEXT)')
    key = tmp_path / 'synthetic-local-key.bin'
    key.write_bytes(b'synthetic-local-only'); key.chmod(0o600 if protected else 0o644)
    config = tmp_path / 'local.json'
    config.write_text(json.dumps({'account_uid': UID_A, 'org_password_file': key.name, 'org_password_format': 'raw'}))
    monkeypatch.setattr(org_native, 'read_native_org', lambda *args, **kwargs: pytest.fail('Local source switched to remote key derivation'))
    if protected:
        assert org.read_account_org(source, UID_A, config)['org_state'] == 'ready'
    else:
        with pytest.raises(org.OrgReadError, match='ORG_CREDENTIALS_PERMISSIONS'):
            org.read_account_org(source, UID_A, config)


@pytest.fixture
def native_snapshot(monkeypatch):
    # This fixture isolates source/cache integrity from BKS cryptography. The
    # separate known-BKS gates below exercise the real PyJKS path.
    constants = {'store_password': 'independent-test-store', 'alias_prefix_confidentiality': 'conf-',
                 'alias_prefix_integrity': 'mac-', 'alias_application': 'independent',
                 'alias_sdk_suffix': '-fixture', 'key_separator': ':'}
    values = list(constants.values())
    dex = bytearray(112 + len(values) * 4)
    dex[:8] = b'dex\n035\0'
    struct.pack_into('<II', dex, 36, 112, 0x12345678)
    struct.pack_into('<II', dex, 56, len(values), 112)
    for index, text in enumerate(values):
        struct.pack_into('<I', dex, 112 + index * 4, len(dex))
        dex.extend(bytes([len(text)]) + text.encode('ascii') + b'\0')
    struct.pack_into('<I', dex, 32, len(dex))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('classes5.dex', dex)
    apk = archive.getvalue()
    profile = {'dex': 'classes5.dex', 'sdk_min': 26, 'string_indices': dict(zip(constants, range(len(values))))}
    monkeypatch.setattr(org_native, 'PROFILES', {hashlib.sha256(apk).hexdigest(): profile})
    databases = []
    for schema in ("CREATE TABLE OrgDepartment(uid,pid,name);CREATE TABLE OrgD2M(did,mid,ord);INSERT INTO OrgDepartment VALUES('d','0','Independent');",
                   "CREATE TABLE OrgMember(uin,name,account);INSERT INTO OrgMember VALUES('p','Independent','account');"):
        with sqlite3.connect(':memory:') as db:
            db.executescript(schema)
            databases.append(db.serialize())
    base = '/data/data/com.tencent.qidian'
    apk_path = '/data/app/independent-fixture/base.apk'
    files = {apk_path: apk, base + '/ks.jks': b'synthetic-isolated-keystore',
             base + '/databases/' + UID_A + '-org.db': databases[0],
             base + '/databases/' + UID_A + '-org-member.db': databases[1]}
    state = {'files': files, 'stamp': 1, 'identity': ('qd01', UID_A, 'generation-one'),
             'boot': '00000000-0000-0000-0000-000000000001', 'reads': [], 'shell_calls': [], 'read_hook': None}
    state.update(constants=constants, apk_path=apk_path, ks_path=base + '/ks.jks',
                 real_password_decoder=org_native.password_from_keystore)
    def shell(command):
        args = shlex.split(command)
        state['shell_calls'].append(args)
        if args == ['cat', '/proc/sys/kernel/random/boot_id']: return state['boot']
        if args == ['pm', 'path', 'com.tencent.qidian']: return 'package:' + apk_path
        if args == ['getprop', 'ro.build.version.sdk']: return '30'
        if args[:3] == ['stat', '-c', '%s:%Y']:
            return '\n'.join(f"{len(files[path])}:{state['stamp']}" for path in args[3:])
        if args[0] == 'sha256sum':
            return '\n'.join(hashlib.sha256(files[path]).hexdigest() + '  ' + path for path in args[1:])
        pytest.fail('Unapproved fake ADB read command')
    source = SimpleNamespace(path=base + '/databases/' + UID_A + '.db', adb='INDEPENDENT_FAKE_ADB',
                             serial='synthetic-device-a', server_port=16000, timeout_s=5, _shell=shell,
                             org_identity_check=lambda: state['identity'])
    def run(command, **kwargs):
        assert command[:6] == [source.adb, '-P', str(source.server_port), '-s', source.serial, 'exec-out']
        assert command[6:8] == ['head', '-c'] and 5 <= kwargs['timeout'] <= 60
        name = command[-1]
        state['reads'].append(name)
        content = files[name][:int(command[8])]
        if state['read_hook']: state['read_hook'](name)
        return SimpleNamespace(returncode=0, stdout=content, stderr=b'')
    monkeypatch.setattr(org_native.subprocess, 'run', run)
    monkeypatch.setattr(org_native, 'password_from_keystore', lambda *args: b'synthetic-isolated-password')
    return source, state


def test_same_size_same_mtime_database_corruption_cannot_return_old_ready_cache(native_snapshot):
    source, state = native_snapshot
    cache = {}
    assert org.read_account_org(source, UID_A, 'unused-old-config', cache=cache)['org_state'] == 'ready'
    path = str(source.path)[:-3] + '-org.db'
    previous = state['files'][path]
    state['files'][path] = bytes([previous[0] ^ 1]) + previous[1:]
    with pytest.raises(org.OrgReadError):
        org.read_account_org(source, UID_A, 'unused-old-config', cache=cache)
    assert UID_A not in cache


def known_bks(strings, *, keys=(bytes(range(16)), bytes(range(32, 64))), duplicate_alias=False):
    """Deterministic standard BKS v2 vector, sealed RAW entries and SHA1 MAC.

    The fixture contains only synthetic constants. Expected output is computed
    directly from the two known clear keys, independently of the reader.
    """
    from jks.rfc7292 import derive_key
    from Cryptodome.Cipher import DES3
    def utf(text):
        encoded = text.encode('utf8')
        return struct.pack('>H', len(encoded)) + encoded
    def block(value): return struct.pack('>I', len(value)) + value
    password = strings['store_password']
    suffix = strings['alias_application'] + strings['alias_sdk_suffix']
    aliases = [strings['alias_prefix_confidentiality'] + suffix, strings['alias_prefix_integrity'] + suffix]
    body = bytearray()
    for index, clear_key in enumerate(keys):
        salt = b'entryfixture' + bytes([index])
        iterations = 13 + index
        raw = b'\x02' + utf('RAW') + utf('AES' if index == 0 else 'HmacSHA256') + block(clear_key)
        pad = 8 - len(raw) % 8
        key = derive_key(hashlib.sha1, 1, password, salt, iterations, 24)
        iv = derive_key(hashlib.sha1, 2, password, salt, iterations, 8)
        encrypted = DES3.new(key, DES3.MODE_CBC, iv).encrypt(raw + bytes([pad]) * pad)
        sealed = block(salt) + struct.pack('>I', iterations) + encrypted
        alias = aliases[0] if duplicate_alias else aliases[index]
        body.extend(b'\x04' + utf(alias) + struct.pack('>qI', 123456, 0) + block(sealed))
    body.append(0)
    salt, iterations = b'independent-mac', 17
    mac_key = derive_key(hashlib.sha1, 3, password, salt, iterations, 20)
    return struct.pack('>I', 2) + block(salt) + struct.pack('>I', iterations) + bytes(body) + hmac.new(mac_key, body, hashlib.sha1).digest()


@pytest.fixture
def native_with_bks(native_snapshot, monkeypatch):
    source, state = native_snapshot
    monkeypatch.setattr(org_native, 'password_from_keystore', state['real_password_decoder'])
    state['files'][state['ks_path']] = known_bks(state['constants'])
    return source, state


def test_standard_bks_known_clear_keys_are_authenticated_and_reconstructed(native_with_bks):
    _, state = native_with_bks
    expected = base64.b64encode(bytes(range(16))) + b':' + base64.b64encode(bytes(range(32, 64)))
    assert org_native.password_from_keystore(state['files'][state['ks_path']], state['constants']) == expected


@pytest.mark.parametrize('fault', ['bad-mac', 'wrong-store-password', 'duplicate-alias'])
def test_bad_known_bks_cannot_supply_a_password_or_expose_material(native_with_bks, fault):
    _, state = native_with_bks
    strings = dict(state['constants'])
    blob = known_bks(strings, duplicate_alias=fault == 'duplicate-alias')
    if fault == 'bad-mac': blob = blob[:-1] + bytes([blob[-1] ^ 1])
    elif fault == 'wrong-store-password': strings['store_password'] = 'independent-wrong-password'
    with pytest.raises(org.OrgReadError) as failure:
        org_native.password_from_keystore(blob, strings)
    assert str(failure.value).startswith('ORG_')
    assert strings['store_password'] not in str(failure.value)


def test_remote_uses_standard_bks_and_reuses_only_stable_snapshot(native_with_bks):
    source, state = native_with_bks
    cache = {}
    first = org.read_account_org(source, UID_A, 'old-instance-config-must-not-be-read', cache=cache, clock=lambda: 10)
    assert first['org_state'] == 'ready' and first['org']['departments'][0]['name'] == 'Independent'
    read_count = len(state['reads'])
    second = org.read_account_org(source, UID_A, 'old-instance-config-must-not-be-read', cache=cache, clock=lambda: 20)
    assert second == first
    assert state['reads'][read_count:] == [state['ks_path']]
    assert any(call[0] == 'sha256sum' and call[-1].endswith('-org.db') for call in state['shell_calls'])


def test_uid_migration_uses_new_source_identity_and_never_previous_result_stamp(native_with_bks):
    source, state = native_with_bks
    cache = {}
    first = org.read_account_org(source, UID_A, 'unused', cache=cache, clock=lambda: 10)
    reads = len(state['reads'])
    source.serial = 'synthetic-device-after-migration'
    source.server_port = 16001
    state['identity'] = ('qd02', UID_A, 'generation-two')
    state['boot'] = '00000000-0000-0000-0000-000000000002'
    second = org.read_account_org(source, UID_A, 'unused', cache=cache, clock=lambda: 20)
    assert first['org_updated_at'] == 10 and second['org_updated_at'] == 20
    assert any(path.endswith('-org.db') for path in state['reads'][reads:])


@pytest.mark.parametrize('target', ['apk', 'keystore', 'database'])
def test_material_change_during_read_with_unchanged_stat_refuses_snapshot(native_with_bks, target):
    source, state = native_with_bks
    changed = state['apk_path'] if target == 'apk' else state['ks_path'] if target == 'keystore' else str(source.path)[:-3] + '-org.db'
    trigger = str(source.path)[:-3] + '-org-member.db'
    def mutate(path):
        if path == trigger:
            original = state['files'][changed]
            state['files'][changed] = original[:-1] + bytes([original[-1] ^ 1])
    state['read_hook'] = mutate
    cache = {}
    with pytest.raises(org.OrgReadError, match='ORG_SOURCE_CHANGED'):
        org.read_account_org(source, UID_A, 'unused', cache=cache)
    assert UID_A not in cache


def test_mapping_change_after_decode_is_rejected_without_stale_ready(native_with_bks, monkeypatch):
    source, state = native_with_bks
    original = org.load_org_plain_bytes
    def parse(*args, **kwargs):
        result = original(*args, **kwargs)
        state['identity'] = ('different-slot', UID_A, 'changed-during-parse')
        return result
    monkeypatch.setattr(org, 'load_org_plain_bytes', parse)
    cache = {}
    with pytest.raises(org.OrgReadError, match='ORG_SOURCE_CHANGED'):
        org.read_account_org(source, UID_A, 'unused', cache=cache)
    assert UID_A not in cache


def test_changed_apk_or_corrupt_keystore_does_not_return_prior_success(native_with_bks):
    source, state = native_with_bks
    cache = {}
    org.read_account_org(source, UID_A, 'unused', cache=cache)
    blob = state['files'][state['ks_path']]
    state['files'][state['ks_path']] = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(org.OrgReadError, match='ORG_KEYSTORE_AUTHENTICATION_FAILED'):
        org.read_account_org(source, UID_A, 'unused', cache=cache)
    assert UID_A not in cache
    state['files'][state['apk_path']] += b'unknown-build'
    reads = len(state['reads'])
    with pytest.raises(org.OrgReadError, match='ORG_APK_PROFILE_UNSUPPORTED'):
        org.read_account_org(source, UID_A, 'unused', cache=cache)
    assert len(state['reads']) == reads


def test_app_resolves_current_serial_instead_of_stale_caller_snapshot(native_with_bks, tmp_path):
    source, state = native_with_bks
    caller = account()
    current = {**caller, 'adb_serial': 'synthetic-new-current-device', 'container_id': 'new-container', 'last_started_ms': 300}
    app = application([current]); app._data_dir = str(tmp_path)
    selected = []
    def choose(uid, acct):
        selected.append((uid, acct.extra['adb_serial']))
        source.serial = acct.extra['adb_serial']
        return source
    source.exists = lambda: True
    source.list_contact_source_rows = lambda: []
    app._maindb_for_uid = choose
    result = app._xunjia_v22_contacts(caller)
    assert selected == [(UID_A, 'synthetic-new-current-device')]
    assert result['org_state'] == 'ready' and result['org']['members']


def test_app_mapping_changed_during_native_read_returns_error_not_cached_ready(native_with_bks, tmp_path):
    source, state = native_with_bks
    rows = [account()]
    app = application(rows); app._data_dir = str(tmp_path)
    source.exists = lambda: True
    source.list_contact_source_rows = lambda: []
    app._maindb_for_uid = lambda *args: source
    def move(path):
        if path.endswith('-org-member.db'):
            rows[0]['container_id'] = 'moved-during-read'
            rows[0]['last_started_ms'] += 1
    state['read_hook'] = move
    result = app._xunjia_v22_contacts(dict(rows[0]))
    assert result['org_state'] == 'error' and result['org_error'] == 'ORG_ACCOUNT_SOURCE_CHANGED'
    assert 'org' not in result and UID_A not in app._xunjia_org_cache
