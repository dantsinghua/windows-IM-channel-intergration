"""Account-scoped, read-only native organization material. Never persist key bytes."""
from __future__ import annotations

import base64
import hashlib
import io
from pathlib import PurePosixPath
import re
import shlex
import struct
import subprocess
import time
import zipfile

# Public identity/DEX offsets only. Values are read from the verified installed APK.
PROFILES = {
    '2cb9564cbeaed5050b2ff03837f514e33594c76773f08fa067523adeb5641ab2': {
        'dex': 'classes5.dex', 'sdk_min': 26,
        'string_indices': {
            'store_password': 12746, 'alias_prefix_confidentiality': 45878,
            'alias_prefix_integrity': 58811, 'alias_application': 71506,
            'alias_sdk_suffix': 82184, 'key_separator': 6402,
        },
    },
}
_MAX_APK = 256 * 1024 * 1024
_MAX_DB = 64 * 1024 * 1024
_MAX_KEYSTORE = 1024 * 1024


def _fail(code):
    from .org import OrgReadError
    raise OrgReadError(code)


def dex_strings(blob, indices):
    """Read bounded ASCII profile constants, not a general DEX interpreter."""
    try:
        if len(blob) < 112 or blob[:4] != b'dex\n' or blob[7] != 0:
            _fail('ORG_APK_PROFILE_INVALID')
        size, header, endian = struct.unpack_from('<III', blob, 32)
        count, offset = struct.unpack_from('<II', blob, 56)
        if size != len(blob) or header != 112 or endian != 0x12345678 or offset < 112 or offset + count * 4 > len(blob):
            _fail('ORG_APK_PROFILE_INVALID')
        result = {}
        for name, index in indices.items():
            if type(index) is not int or not 0 <= index < count:
                _fail('ORG_APK_PROFILE_INVALID')
            position = struct.unpack_from('<I', blob, offset + index * 4)[0]
            length = 0
            for shift in range(0, 35, 7):
                byte = blob[position]; position += 1
                length |= (byte & 127) << shift
                if not byte & 128:
                    break
            else:
                _fail('ORG_APK_PROFILE_INVALID')
            if not 0 < length <= 1024:
                _fail('ORG_APK_PROFILE_INVALID')
            end = blob.find(b'\0', position, min(len(blob), position + 1025))
            if end < 0 or end - position != length:
                _fail('ORG_APK_PROFILE_INVALID')
            result[name] = blob[position:end].decode('ascii')
        return result
    except (IndexError, KeyError, struct.error, UnicodeError, TypeError):
        _fail('ORG_APK_PROFILE_INVALID')


def password_from_keystore(blob, strings):
    """Authenticate BKS before accepting exactly the reviewed sealed RAW entries."""
    try:
        import jks
    except ImportError:
        _fail('ORG_DEPENDENCY_MISSING')
    try:
        if not 4 <= len(blob) <= _MAX_KEYSTORE or struct.unpack_from('>I', blob)[0] != 2:
            _fail('ORG_KEYSTORE_FORMAT_UNSUPPORTED')
        salt_length = struct.unpack_from('>I', blob, 4)[0]
        if not 1 <= salt_length <= 64 or len(blob) < 12 + salt_length:
            _fail('ORG_KEYSTORE_FORMAT_UNSUPPORTED')
        iterations = struct.unpack_from('>I', blob, 8 + salt_length)[0]
        if not 1 <= iterations <= 1000000:
            _fail('ORG_KEYSTORE_FORMAT_UNSUPPORTED')
        password = strings['store_password']
        suffix = strings['alias_application'] + strings['alias_sdk_suffix']
        aliases = [strings['alias_prefix_confidentiality'] + suffix,
                   strings['alias_prefix_integrity'] + suffix]
        store = jks.BksKeyStore.loads(blob, password, try_decrypt_keys=False)
        if len(set(aliases)) != 2 or set(store.entries) != set(aliases) or set(store.sealed_keys) != set(aliases):
            _fail('ORG_KEYSTORE_ENTRIES_INVALID')
        keys = []
        for alias in aliases:
            entry = store.sealed_keys[alias]
            # Bound the standard PBE work factor before asking PyJKS to decrypt.
            sealed = entry._encrypted
            sealed_salt = struct.unpack_from('>I', sealed)[0]
            if not 1 <= sealed_salt <= 64 or len(sealed) < sealed_salt + 8:
                _fail('ORG_KEYSTORE_ENTRIES_INVALID')
            sealed_iterations = struct.unpack_from('>I', sealed, 4 + sealed_salt)[0]
            if not 1 <= sealed_iterations <= 1000000:
                _fail('ORG_KEYSTORE_ENTRIES_INVALID')
            entry.decrypt(password)
            if not entry.is_decrypted() or entry.type != jks.bks.KEY_TYPE_SECRET or entry.format != 'RAW':
                _fail('ORG_KEYSTORE_ENTRIES_INVALID')
            key = entry.key
            if not isinstance(key, bytes) or not 1 <= len(key) <= 4096:
                _fail('ORG_KEYSTORE_ENTRIES_INVALID')
            keys.append(key)
        return (base64.b64encode(keys[0]).decode('ascii') + strings['key_separator']
                + base64.b64encode(keys[1]).decode('ascii')).encode('ascii')
    except Exception as exc:
        from .org import OrgReadError
        if isinstance(exc, OrgReadError):
            raise
        _fail('ORG_KEYSTORE_AUTHENTICATION_FAILED')


def _shell(source, command):
    try:
        value = source._shell(command)
    except Exception:
        _fail('ORG_NATIVE_SOURCE_UNAVAILABLE')
    if not isinstance(value, str) or len(value) > 16384:
        _fail('ORG_NATIVE_SOURCE_INVALID')
    return value.strip()


def _hash(source, path):
    fields = _shell(source, 'sha256sum ' + shlex.quote(path)).split()
    if not fields or re.fullmatch('[0-9a-f]{64}', fields[0]) is None:
        _fail('ORG_SOURCE_FINGERPRINT_INVALID')
    return fields[0]


def _metadata(source, paths):
    lines = _shell(source, 'stat -c %s:%Y ' + ' '.join(shlex.quote(p) for p in paths)).splitlines()
    if len(lines) != len(paths) or any(re.fullmatch(r'[0-9]+:[0-9]+', line) is None for line in lines):
        _fail('ORG_SOURCE_UNAVAILABLE')
    return tuple(tuple(map(int, line.split(':'))) for line in lines)


def _read(source, path, maximum):
    try:
        # head bounds the remote output even if the file grows after stat.
        result = subprocess.run([source.adb, '-P', str(source.server_port), '-s', source.serial,
                                 'exec-out', 'head', '-c', str(maximum + 1), path],
                                capture_output=True, timeout=min(max(getattr(source, 'timeout_s', 30), 5), 60), check=False)
    except Exception:
        _fail('ORG_SOURCE_UNAVAILABLE')
    if result.returncode or not 0 < len(result.stdout) <= maximum:
        _fail('ORG_SOURCE_UNAVAILABLE')
    return result.stdout


def _identity(source):
    check = getattr(source, 'org_identity_check', None)
    managed = check() if check is not None else getattr(source, 'org_source_identity', ())
    boot = _shell(source, 'cat /proc/sys/kernel/random/boot_id')
    if re.fullmatch(r'[0-9a-fA-F-]{36}', boot) is None:
        _fail('ORG_NATIVE_IDENTITY_UNVERIFIED')
    return (str(source.adb), source.server_port, source.serial, str(source.path), managed, boot)


def _apk(source, identity, uid, cache):
    lines = _shell(source, 'pm path com.tencent.qidian').splitlines()
    if len(lines) != 1 or not lines[0].startswith('package:'):
        _fail('ORG_APK_SOURCE_AMBIGUOUS')
    path = lines[0][len('package:'):]
    parsed = PurePosixPath(path)
    if not path.startswith('/data/app/') or parsed.suffix != '.apk' or '..' in parsed.parts:
        _fail('ORG_APK_SOURCE_INVALID')
    meta = _metadata(source, [path])
    if not 0 < meta[0][0] <= _MAX_APK:
        _fail('ORG_APK_SOURCE_INVALID')
    digest = _hash(source, path)
    profile = PROFILES.get(digest)
    if profile is None:
        _fail('ORG_APK_PROFILE_UNSUPPORTED')
    sdk_text = _shell(source, 'getprop ro.build.version.sdk')
    if not sdk_text.isdigit() or int(sdk_text) < profile['sdk_min']:
        _fail('ORG_APK_SDK_UNSUPPORTED')
    fingerprint = (identity, path, meta, digest, sdk_text)
    profiles = cache.setdefault('_native_apk_profiles', {}) if cache is not None else {}
    saved = profiles.get(uid)
    if saved and saved['fingerprint'] == fingerprint:
        return digest, saved['strings'], path, meta
    blob = _read(source, path, _MAX_APK)
    if len(blob) != meta[0][0] or hashlib.sha256(blob).hexdigest() != digest or _metadata(source, [path]) != meta or _hash(source, path) != digest:
        _fail('ORG_SOURCE_CHANGED')
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if sum(item.filename == profile['dex'] for item in archive.infolist()) != 1:
                _fail('ORG_APK_PROFILE_INVALID')
            item = archive.getinfo(profile['dex'])
            if not 112 <= item.file_size <= _MAX_DB:
                _fail('ORG_APK_PROFILE_INVALID')
            strings = dex_strings(archive.read(item), profile['string_indices'])
    except (zipfile.BadZipFile, KeyError, OSError):
        _fail('ORG_APK_PROFILE_INVALID')
    profiles[uid] = {'fingerprint': fingerprint, 'strings': strings}
    return digest, strings, path, meta


def read_native_org(source, uid, *, cache=None, clock=None):
    from .org import OrgReadError, read_encrypted_org, load_org_plain_bytes
    uid = str(uid); clock = time.time if clock is None else clock
    path = PurePosixPath(str(source.path))
    if not re.fullmatch(r'[0-9]+', uid) or path.name != uid + '.db' or path.parent.as_posix() != '/data/data/com.tencent.qidian/databases':
        _fail('ORG_ACCOUNT_MISMATCH')
    paths = [str(path)[:-3] + suffix for suffix in ('-org.db', '-org-member.db')]
    key_path = (path.parent.parent / 'ks.jks').as_posix()
    try:
        identity = _identity(source)
        digest, strings, apk_path, apk_meta = _apk(source, identity, uid, cache)
        before = _metadata(source, [key_path] + paths)
        if not 0 < before[0][0] <= _MAX_KEYSTORE or any(not 0 < size <= _MAX_DB for size, _ in before[1:]):
            _fail('ORG_SOURCE_SIZE_INVALID')
        hashes = tuple(_hash(source, name) for name in [key_path] + paths)
        keystore = _read(source, key_path, _MAX_KEYSTORE)
        if len(keystore) != before[0][0] or hashlib.sha256(keystore).hexdigest() != hashes[0]:
            _fail('ORG_SOURCE_CHANGED')
        fingerprint = (uid, identity, digest, hashes, before)
        def verify_current():
            if (_metadata(source, [key_path] + paths) != before
                    or tuple(_hash(source, name) for name in [key_path] + paths) != hashes
                    or _metadata(source, [apk_path]) != apk_meta or _hash(source, apk_path) != digest
                    or _identity(source) != identity):
                _fail('ORG_SOURCE_CHANGED')
        previous = (cache or {}).get(uid)
        if previous and previous['fingerprint'] == fingerprint:
            verify_current()
            return dict(previous['result'])
        password = password_from_keystore(keystore, strings)
        blobs = [_read(source, name, _MAX_DB) for name in paths]
        if (tuple(len(blob) for blob in blobs) != tuple(size for size, _ in before[1:])
                or tuple(hashlib.sha256(blob).hexdigest() for blob in blobs) != hashes[1:]):
            _fail('ORG_SOURCE_CHANGED')
        try:
            org = (load_org_plain_bytes(*blobs, allow_empty=True) if all(b.startswith(b'SQLite format 3\0') for b in blobs)
                   else read_encrypted_org(*blobs, password, allow_empty=True))
        except Exception:
            _fail('ORG_SOURCE_DECRYPT_OR_SCHEMA_ERROR')
        verify_current()
        result = {'org': org, 'org_state': 'ready' if org['departments'] or org['members'] else 'empty',
                  'org_updated_at': clock(), 'org_error': None}
        if cache is not None:
            cache[uid] = {'fingerprint': fingerprint, 'result': result}
        return result
    except OrgReadError:
        if cache is not None:
            cache.pop(uid, None)
        raise
