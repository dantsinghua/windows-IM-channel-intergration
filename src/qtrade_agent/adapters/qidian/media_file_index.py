"""Supplemental file metadata only; mr_fileManager never becomes a message source."""
from pathlib import Path
from .media_metadata import MediaReference, _hash

def file_manager_reference(source, table, row):
    query = getattr(source, '_run', None) or getattr(source, '_sql', None)
    if not callable(query): return None
    owner = Path(getattr(source, 'path', '')).stem
    if not owner.isascii() or not owner.isdecimal(): return None
    exists = query("SELECT name FROM sqlite_master WHERE type='table' AND name='mr_fileManager'")
    if not exists: return None
    columns = {r[1] for r in query('PRAGMA table_info(mr_fileManager)')}
    required = {'selfUin','peerUin','peerType','bSend','uniseq','msgTime','fileSize','fileName','strFilePath','strFileMd5','strFileSHA','TroopUin'}
    if not required <= columns: raise ValueError('NATIVE_FILE_MANAGER_SCHEMA_UNSUPPORTED')
    if type(row.uniseq) is not int or not -(2**63) <= row.uniseq < 2**63:
        raise ValueError('NATIVE_FILE_ID_INVALID')
    values = query('SELECT hex(selfUin),hex(peerUin),peerType,bSend,uniseq,msgTime,fileSize,'
        'hex(fileName),hex(strFilePath),hex(strFileMd5),hex(strFileSHA),TroopUin FROM mr_fileManager '
        'WHERE uniseq='+str(row.uniseq)+' LIMIT 2')
    if not values: return None
    if len(values) != 1: raise ValueError('NATIVE_FILE_ID_AMBIGUOUS')
    value = values[0]
    def text(i):
        try: return bytes.fromhex(value[i] or '').decode('utf8')
        except (UnicodeError, ValueError, TypeError):
            raise ValueError('NATIVE_FILE_MANAGER_ENCODING_UNSUPPORTED') from None
    try: kind, direction, native, stamp, size = (int(value[i]) for i in (2,3,4,5,6))
    except (TypeError, ValueError): raise ValueError('NATIVE_FILE_MANAGER_NUMERIC_INVALID') from None
    group = table.kind == 'group'; peer = table.peer_id.removeprefix('g_')
    same_peer = (str(value[11]) == peer or text(1) == peer) if group else text(1) == peer
    normalized_stamp = stamp // 1000 if stamp > 100000000000 else stamp
    if (text(0) != owner or not same_peer or kind != int(group) or direction != row.issend
            or native != row.uniseq or abs(normalized_stamp-row.time) > 1):
        raise ValueError('NATIVE_FILE_MANAGER_IDENTITY_MISMATCH')
    if size < 0 or size > 50*1024*1024: raise ValueError('NATIVE_MEDIA_SIZE_INVALID')
    path = text(8)
    return MediaReference(name=text(7)[:200], size=size, md5=_hash(text(9),32),
        sha1=_hash(text(10),40), paths=(path,) if path else ())