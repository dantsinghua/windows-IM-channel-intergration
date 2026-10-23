"""Read actual native media bytes from database references, without UI or hooks.

Typed native picture metadata and the account-scoped file index are supported.
A thumbnail or a placeholder is never a ready attachment.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import tempfile
from .msgdata import iter_fields
from ...media import sniff_mime

MAX_BYTES = 50 * 1024 * 1024
_BASES = ("/sdcard/Android/data/com.tencent.qidian/Tencent/Qidian/",
          "/storage/emulated/0/Android/data/com.tencent.qidian/Tencent/Qidian/",
          "/sdcard/Tencent/Qidian/", "/storage/emulated/0/Tencent/Qidian/",
          "/sdcard/Tencent/MobileQQ/", "/storage/emulated/0/Tencent/MobileQQ/")
_ROOTS = tuple(base + folder + "/" for base in _BASES for folder in
               ("Qidianfile_recv", "QQfile_recv", "photo", "image", "pic", "diskcache"))


def validate_native_path(path):
    if not isinstance(path, str) or len(path) > 2048 or not path.startswith("/"):
        raise ValueError("NATIVE_MEDIA_PATH_INVALID")
    if any(ord(c) < 32 for c in path) or "\\" in path or ".." in path.split("/"):
        raise ValueError("NATIVE_MEDIA_PATH_INVALID")
    if not any(path.lower().startswith(root.lower()) for root in _ROOTS):
        raise ValueError("NATIVE_MEDIA_PATH_OUTSIDE_ACCOUNT_STORAGE")
    return path


def _protobuf_strings(raw, depth=0):
    if depth > 8 or len(raw) > 2 * 1024 * 1024:
        return
    try:
        fields = list(iter_fields(raw))
    except ValueError:
        return
    if len(fields) > 2048:
        return
    for _, wt, value in fields:
        if wt != 2 or not isinstance(value, bytes):
            continue
        try:
            text = value.decode("utf8")
        except UnicodeError:
            text = ""
        if text.startswith("/"):
            yield text
        else:
            yield from _protobuf_strings(value, depth + 1)


def local_references(msgtype, raw):
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("NATIVE_MEDIA_METADATA_OVERSIZE")
    values = []
    if raw.startswith(b"\xac\xed\x00\x05"):
        # Extract only length-framed Java TC_STRING atoms. No object loading,
        # class instantiation, network URL following, or Java execution.
        if msgtype != -2017:
            return []
        for match in re.finditer(b"\x74([\x00-\x08].)", raw, re.S):
            size = int.from_bytes(match.group(1), "big")
            start = match.end()
            if not 1 <= size <= 2048 or start + size > len(raw):
                continue
            try:
                text = raw[start:start + size].decode("utf8")
            except UnicodeError:
                continue
            if text.startswith("/"):
                values.append(text)
    else:
        values = list(_protobuf_strings(raw))
    result = []
    for path in values:
        try:
            validate_native_path(path)
        except ValueError:
            continue
        # A cached thumbnail is not the original media. Do not silently substitute.
        if any(v in path.lower() for v in ("thumb", "/thumbnail", "/head/", "avatar")):
            continue
        ext = PurePosixPath(path).suffix.lower()
        if msgtype not in (-2005, -2017) and ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            continue
        if path not in result:
            result.append(path)
    if len(result) > 20:
        raise ValueError("NATIVE_MEDIA_REFERENCE_LIMIT")
    return result


class AdbMediaReader:
    def __init__(self, source):
        self.source = source

    def __call__(self, path):
        validate_native_path(path)
        if not hasattr(self.source, "serial"):
            raise ValueError("NATIVE_MEDIA_BACKEND_UNAVAILABLE")
        actual = self.source._shell("readlink -f " + shlex.quote(path)).strip()
        validate_native_path(actual)
        raw_size = self.source._shell("stat -c %s " + shlex.quote(actual)).strip()
        if not raw_size.isdecimal() or not 0 < int(raw_size) <= MAX_BYTES:
            raise ValueError("NATIVE_MEDIA_SIZE_INVALID")
        size = int(raw_size)
        # Bounded at the device even if a file changes after stat.
        cmd = "head -c " + str(size + 1) + " " + shlex.quote(actual)
        try:
            p = subprocess.run([self.source.adb, "-P", str(self.source.server_port),
                "-s", self.source.serial, "exec-out", cmd], capture_output=True,
                timeout=max(10, min(45, self.source.timeout_s * 3)), check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("NATIVE_MEDIA_READ_UNAVAILABLE") from None
        if p.returncode != 0 or len(p.stdout) != size:
            raise ValueError("NATIVE_MEDIA_READ_INCOMPLETE")
        return p.stdout


class NativeMediaResolver:
    def __init__(self, root, read_bytes, *, source=None, image_download=None):
        from .media_download import OriginalImageDownloader
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.read_bytes, self.source = read_bytes, source
        self.image_download = image_download or OriginalImageDownloader()
        self.blobs = {}

    def for_message(self, table, row, raw):
        if row.msgtype in (-2005, -2017) and self.source is not None:
            from .media_file_index import file_manager_reference
            from .media_metadata import troop_file_reference
            try:
                ref = file_manager_reference(self.source, table, row)
                if ref is not None:
                    if row.msgtype == -2017 and raw.startswith(b'\xac\xed\x00\x05\x73'):
                        original = troop_file_reference(raw)
                        if ((original.md5 and ref.md5 and original.md5 != ref.md5)
                                or (original.size and ref.size and original.size != ref.size)):
                            raise ValueError('NATIVE_FILE_MANAGER_CONTENT_MISMATCH')
                    return self._resolve([ref], image=False)
            except ValueError as exc:
                return [], str(exc)
        return self(row.msgtype, raw)

    def __call__(self, msgtype, raw):
        from .media_metadata import picture_references, troop_file_reference, MediaReference
        if msgtype not in (-2000, -1035, -2005, -2017):
            return [], 'NATIVE_MEDIA_MESSAGE_TYPE_UNSUPPORTED'
        try:
            refs = picture_references(msgtype, raw)
            if refs: return self._resolve(refs, image=True)
            if msgtype == -2017 and raw.startswith(b'\xac\xed\x00\x05\x73'):
                return self._resolve([troop_file_reference(raw)], image=False)
            # Retain legacy structured local-path support, without treating arbitrary URLs as originals.
            refs = [MediaReference(name=PurePosixPath(p).name[:200], paths=(p,)) for p in local_references(msgtype, raw)]
            if not refs: return [], 'NATIVE_MEDIA_METADATA_UNSUPPORTED'
            return self._resolve(refs, image=msgtype not in (-2005,-2017))
        except ValueError as exc:
            return [], str(exc) if str(exc).startswith('NATIVE_') else 'NATIVE_MEDIA_METADATA_INVALID'

    @staticmethod
    def _check(data, ref, image):
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
            raise ValueError('NATIVE_MEDIA_SIZE_INVALID')
        if ref.size and len(data) != ref.size: raise ValueError('NATIVE_MEDIA_SIZE_MISMATCH')
        if ref.md5 and hashlib.md5(data).hexdigest() != ref.md5:
            raise ValueError('NATIVE_MEDIA_MD5_MISMATCH')
        if ref.sha1 and hashlib.sha1(data).hexdigest() != ref.sha1:
            raise ValueError('NATIVE_MEDIA_SHA1_MISMATCH')
        mime = sniff_mime(data[:32])
        if image and not mime.startswith('image/'): raise ValueError('NATIVE_IMAGE_CONTENT_INVALID')
        return mime

    def _store(self, data):
        h = hashlib.sha256(data).hexdigest(); target = self.root / h
        if not target.is_file() or target.stat().st_size != len(data) or hashlib.sha256(target.read_bytes()).hexdigest() != h:
            fd, temp = tempfile.mkstemp(prefix='.native-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(data); f.flush(); os.fsync(f.fileno())
                os.replace(temp, target); target.chmod(0o600)
            finally:
                if os.path.exists(temp): os.unlink(temp)
        self.blobs[h] = target
        return h

    def _resolve(self, refs, *, image):
        items, issue = [], None
        for ref in refs:
            name = (PurePosixPath(ref.name).name if ref.name else 'attachment')[:200]
            error, data, mime = 'NATIVE_MEDIA_UNAVAILABLE', None, 'application/octet-stream'
            if ref.encrypted:
                error = 'NATIVE_MEDIA_ENCRYPTION_UNSUPPORTED'
            elif ref.size > MAX_BYTES:
                error = 'NATIVE_MEDIA_SIZE_INVALID'
            else:
                paths = list(ref.paths)
                cache_paths = getattr(self.read_bytes, 'cache_paths', None)
                if image and ref.md5 and callable(cache_paths):
                    try: paths.extend(cache_paths(ref))
                    except (OSError, ValueError): pass
                for path in dict.fromkeys(paths):
                    try:
                        validate_native_path(path)
                        candidate = self.read_bytes(path)
                        mime = self._check(candidate, ref, image); data = candidate; break
                    except (OSError, ValueError) as exc:
                        error = str(exc) if isinstance(exc,ValueError) and str(exc).startswith('NATIVE_') else 'NATIVE_MEDIA_UNAVAILABLE'
                if data is None and image and ref.raw_url and ref.md5:
                    try:
                        candidate = self.image_download(ref)
                        mime = self._check(candidate, ref, True); data = candidate
                    except (OSError, ValueError) as exc:
                        error = str(exc) if isinstance(exc,ValueError) and str(exc).startswith('NATIVE_') else 'NATIVE_MEDIA_DOWNLOAD_UNAVAILABLE'
            if data is None:
                issue = error
                items.append({'name':name,'size':0,'mime':'application/octet-stream','state':'unavailable'})
            else:
                h = self._store(data)
                items.append({'sha256':h,'name':name,'size':len(data),'mime':mime,'state':'ready'})
        return items, issue