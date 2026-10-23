"""Read-only media contracts reconstructed from the installed Qidian 6.9.7 APK.
PicRec fields are named in localpb.richMsg.RichMsg$PicRec.__fieldMap__.
No Java objects are deserialized or executed; only bounded primitive/String data.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
import struct
from .msgdata import iter_fields

MAX_METADATA = 2 * 1024 * 1024

@dataclass(frozen=True)
class MediaReference:
    name: str = ''
    size: int = 0
    md5: str = ''
    sha1: str = ''
    paths: tuple[str, ...] = ()
    raw_url: str = ''
    encrypted: bool = False

def _text(value):
    if not isinstance(value, bytes): return ''
    try: value = value.decode('utf8')
    except UnicodeError: return ''
    return value if len(value) <= 4096 and not any(ord(c) < 32 for c in value) else ''

def _hash(value, length):
    value = str(value or '')
    return value.lower() if re.fullmatch('[a-fA-F0-9]{'+str(length)+'}', value) else ''

def picture_references(msgtype, raw):
    if msgtype not in (-2000, -1035) or not isinstance(raw, bytes) or len(raw) > MAX_METADATA:
        return []
    found = []
    budget = [0]
    def walk(blob, depth):
        if depth > 4 or len(found) >= 20: return
        budget[0] += len(blob)
        if budget[0] > MAX_METADATA * 5: raise ValueError('NATIVE_MEDIA_METADATA_LIMIT')
        try: fs = list(iter_fields(blob))
        except ValueError: return
        if not fs or len(fs) > 256 or any(k < 1 for k, _, _ in fs): return
        fields = {}
        for k, wt, value in fs:
            if k in fields: break
            fields[k] = (wt, value)
        else:
            text = lambda k: _text(fields.get(k, (None, None))[1]) if fields.get(k, (None,))[0] == 2 else ''
            number = lambda k: fields.get(k, (None, 0))[1] if fields.get(k, (None,))[0] == 0 else 0
            md5 = _hash(text(6), 32); path = text(1); size = number(2)
            # A recognized PicRec must have typed identity, not arbitrary protobuf strings.
            if md5 and (size > 0 or path or text(10)):
                found.append(MediaReference(name=md5, size=size, md5=md5,
                    paths=(path,) if path.startswith('/') else (), raw_url=text(10), encrypted=bool(number(31))))
                return
        for _, wt, value in fs:
            if wt == 2 and isinstance(value, bytes): walk(value, depth + 1)
    walk(raw, 0)
    return found

class _JavaStrings:
    """Restricted Java serialization reader: a single TroopFileData and strings only."""
    def __init__(self, raw):
        if not isinstance(raw, bytes) or len(raw) > MAX_METADATA or raw[:4] != b'\xac\xed\x00\x05':
            raise ValueError('NATIVE_FILE_METADATA_INVALID')
        self.raw, self.pos, self.refs = raw, 4, []
    def take(self, n):
        if not 0 <= n <= MAX_METADATA or self.pos + n > len(self.raw):
            raise ValueError('NATIVE_FILE_METADATA_TRUNCATED')
        out = self.raw[self.pos:self.pos+n]; self.pos += n; return out
    def integer(self, n): return int.from_bytes(self.take(n), 'big')
    def utf(self):
        raw = self.take(self.integer(2))
        if len(raw) > 8192: raise ValueError('NATIVE_FILE_STRING_LIMIT')
        # Modified UTF-8 permits encoded NUL and UTF-16 surrogate pairs.
        try:
            text = raw.replace(b'\xc0\x80', b'\0').decode('utf8', 'surrogatepass')
            return text.encode('utf16', 'surrogatepass').decode('utf16')
        except UnicodeError: raise ValueError('NATIVE_FILE_STRING_ENCODING') from None
    def string(self):
        tag = self.integer(1)
        if tag == 0x70: return ''
        if tag == 0x71:
            i = self.integer(4) - 0x7e0000
            if not 0 <= i < len(self.refs) or not isinstance(self.refs[i], str):
                raise ValueError('NATIVE_FILE_REFERENCE_INVALID')
            return self.refs[i]
        if tag != 0x74: raise ValueError('NATIVE_FILE_OBJECT_UNSUPPORTED')
        s = self.utf(); self.refs.append(s); return s
    def read(self):
        if self.integer(1) != 0x73 or self.integer(1) != 0x72:
            raise ValueError('NATIVE_FILE_CLASS_REQUIRED')
        name = self.utf(); self.take(8)
        if name != 'com.tencent.mobileqq.data.TroopFileData':
            raise ValueError('NATIVE_FILE_CLASS_UNSUPPORTED')
        self.refs.append(None)
        if self.integer(1) != 2: raise ValueError('NATIVE_FILE_SERIAL_FLAGS_UNSUPPORTED')
        count = self.integer(2)
        if count > 64: raise ValueError('NATIVE_FILE_FIELD_LIMIT')
        fields = []
        for _ in range(count):
            kind = chr(self.integer(1)); key = self.utf()
            if kind == 'L' and self.string() != 'Ljava/lang/String;':
                raise ValueError('NATIVE_FILE_FIELD_UNSUPPORTED')
            if kind not in 'ZBCSIJFDL': raise ValueError('NATIVE_FILE_FIELD_UNSUPPORTED')
            fields.append((kind, key))
        if self.take(2) != b'\x78\x70': raise ValueError('NATIVE_FILE_SUPERCLASS_UNSUPPORTED')
        self.refs.append(None)
        values = {}
        fmts = {'Z':'>?','B':'>b','C':'>H','S':'>h','I':'>i','J':'>q','F':'>f','D':'>d'}
        for kind, key in fields:
            if key in values: raise ValueError('NATIVE_FILE_DUPLICATE_FIELD')
            values[key] = self.string() if kind == 'L' else struct.unpack(fmts[kind], self.take(struct.calcsize(fmts[kind])))[0]
        if self.pos != len(self.raw): raise ValueError('NATIVE_FILE_TRAILING_DATA')
        return values

def troop_file_reference(raw):
    values = _JavaStrings(raw).read()
    name = values.get('fileName') or values.get('dspFileName') or 'attachment'
    size = values.get('lfileSize', 0)
    if type(size) is not int or size < 0: raise ValueError('NATIVE_FILE_SIZE_INVALID')
    path = values.get('fileUrl', '')
    return MediaReference(name=str(name)[:200], size=size, md5=_hash(values.get('md5'), 32),
        sha1=_hash(values.get('sha1'), 40), paths=(path,) if isinstance(path,str) and path.startswith('/') else ())