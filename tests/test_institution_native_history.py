import hashlib,sqlite3,struct,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb, MainDbError
from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.adapters.qidian.history_native import NativeHistoryReader,HistoryTable
from qtrade_agent.adapters.qidian.history_media import NativeMediaResolver,local_references,validate_native_path
from qtrade_agent.ossbox.history_publish import NativeHistoryPublisher
NOW=1800000000
MEDIA='/sdcard/Android/data/com.tencent.qidian/Tencent/Qidian/Qidianfile_recv/'

def varint(n):
 out=bytearray()
 while n>127:out.append((n&127)|128);n>>=7
 out.append(n);return bytes(out)
def field(n,data):return varint(n*8+2)+varint(len(data))+data

def native(tmp_path,records,group=False):
 path=tmp_path/'native.db';peer='415011447';kind='troop' if group else 'friend'
 table='mr_'+kind+'_'+hashlib.md5(peer.encode()).hexdigest().upper()+'_New'
 with sqlite3.connect(path) as db:
  db.execute(f'CREATE TABLE "{table}"(_id INTEGER PRIMARY KEY,issend INTEGER,time INTEGER,msgtype INTEGER,uniseq INTEGER,senderuin BLOB,msgData BLOB,frienduin BLOB,istroop INTEGER)')
  for n,stamp,typ,raw in records:
   db.execute(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?,?)',(n,0,stamp,typ,100+n,xor(peer.encode()),xor(raw),xor(peer.encode()),int(group)))
 return path,table,LocalSqliteMainDb(str(path))
class Adapter:
 def __init__(self):self.calls=[];self.fail=False
 def publish_manifest(self,payload,*,message_id):
  if self.fail:raise OSError('synthetic upload failure')
  self.calls.append((message_id,payload))

def test_native_seven_day_window_and_readonly(tmp_path):
 p,t,source=native(tmp_path,[(1,NOW-604801,-1000,b'old'),(2,NOW-604800,-1000,b'boundary'),(3,NOW,-1051,b'new'),(4,NOW+61,-1000,b'future')])
 before=p.read_bytes();r=NativeHistoryReader(source);tables=r.tables();rows=r.page(tables[0],now=NOW)
 assert [x.id for x in rows]==[2,3]
 assert [r.message(tables[0],x)[0]['native_id'] for x in rows]==['qd:102','qd:103']
 assert p.read_bytes()==before

def test_native_group_is_archive_only(tmp_path):
 _,_,source=native(tmp_path,[(1,NOW,-1000,b'group')],group=True);r=NativeHistoryReader(source);t=r.tables()[0]
 msg,issue=r.message(t,r.page(t,now=NOW)[0]);assert msg['peer_id']=='g_415011447' and msg['historical'] and not issue

@pytest.mark.parametrize('name',["mr_friend_bad_New",'";drop table x;--'])
def test_invalid_table_rejected_before_sql(tmp_path,name):
 source=SimpleNamespace(_run=lambda _:pytest.fail('SQL must not execute'))
 with pytest.raises(ValueError):NativeHistoryReader(source).page(HistoryTable(name,'1','private'),now=NOW)

def test_native_peer_identity_must_match(tmp_path):
 p,t,source=native(tmp_path,[(1,NOW,-1000,b'x')])
 with sqlite3.connect(p) as db:db.execute(f'UPDATE "{t}" SET frienduin=?',(xor(b'999'),))
 with pytest.raises(MainDbError):NativeHistoryReader(source).tables()

def test_backfill_manifest_dedup_does_not_advance_live_cursor(tmp_path):
 p,t,source=native(tmp_path,[(1,NOW,-1000,b'one'),(2,NOW,-1000,b'two')]);a=Adapter()
 before=p.read_bytes();h=NativeHistoryPublisher(tmp_path/'pub',source,a,clock=lambda:NOW)
 h.tick();assert len(a.calls)==1 and len(a.calls[0][1]['items'])==2
 assert a.calls[0][1]['op']=='history' and p.read_bytes()==before
 h.tick();h=NativeHistoryPublisher(tmp_path/'pub',source,a,clock=lambda:NOW);h.tick();assert len(a.calls)==1

def test_failed_upload_keeps_history_cursor_for_retry(tmp_path):
 _,_,source=native(tmp_path,[(1,NOW,-1000,b'one')]);a=Adapter();h=NativeHistoryPublisher(tmp_path/'pub',source,a,clock=lambda:NOW)
 a.fail=True
 with pytest.raises(OSError):h.tick()
 with h.db() as db:assert db.execute('SELECT count(*) FROM history_cursor').fetchone()[0]==0
 a.fail=False;h.tick();assert len(a.calls)==1

@pytest.mark.parametrize('path',['/etc/passwd',MEDIA+'../wtlogin/token','/data/data/com.tencent.qidian/files/mmkv/config',MEDIA+'x\nsecret'])
def test_media_paths_reject_non_media_roots_and_traversal(path):
 with pytest.raises(ValueError):validate_native_path(path)

def test_real_bytes_not_placeholder_and_thumbnail_not_substituted(tmp_path):
 data=b'\x89PNG\r\n\x1a\n'+b'\0'*40;path=MEDIA+'photo.png';raw=field(1,field(2,path.encode()))
 r=NativeMediaResolver(tmp_path,lambda p:data if p==path else pytest.fail('unexpected path'))
 items,issue=r(-2000,raw);assert not issue and items[0]['state']=='ready'
 assert r.blobs[items[0]['sha256']].read_bytes()==data
 assert local_references(-2000,field(1,(MEDIA+'thumbnail.png').encode()))==[]

def test_missing_binary_never_reports_ready(tmp_path):
 def missing(_):raise OSError('missing')
 r=NativeMediaResolver(tmp_path,missing);items,issue=r(-2000,field(1,(MEDIA+'missing.png').encode()))
 assert issue=='NATIVE_MEDIA_UNAVAILABLE' and items[0]['state']=='unavailable' and 'sha256' not in items[0]

def test_java_file_local_bytes_transmitted_in_bounded_chunks(tmp_path):
 path=MEDIA+'document.txt';raw=b'\xac\xed\x00\x05\x74'+len(path.encode()).to_bytes(2,'big')+path.encode()
 _,_,source=native(tmp_path,[(1,NOW,-2017,raw)]);a=Adapter();data=b'abcd'*(180000)
 h=NativeHistoryPublisher(tmp_path/'pub',source,a,clock=lambda:NOW,media_reader=lambda _:data)
 h.tick();chunks=[p for _,p in a.calls if p['op']=='media_chunk'];history=a.calls[-1][1]
 import base64
 assert len(chunks)==2 and b''.join(base64.b64decode(c['data']) for c in chunks)==data
 assert history['op']=='history' and history['items'][0]['media'][0]['sha256']==hashlib.sha256(data).hexdigest()
 assert history['items'][0]['historical']

def test_unsupported_native_media_is_explicit_not_text_success(tmp_path):
 _,_,source=native(tmp_path,[(1,NOW,-2000,b'not a media reference')]);a=Adapter()
 h=NativeHistoryPublisher(tmp_path/'pub',source,a,clock=lambda:NOW);s=h.tick()
 assert not a.calls and s['error']=='NATIVE_MEDIA_METADATA_UNSUPPORTED'
 assert h.status()['unresolved_media'][0]['n']==1
def test_unavailable_old_attachment_never_starves_new_history_pages(tmp_path):
    records=[(1,NOW-10,-2000,field(1,(MEDIA+'unavailable.png').encode()))]
    records += [(n,NOW,-1000,('text'+str(n)).encode()) for n in range(2,75)]
    _,_,source=native(tmp_path,records);a=Adapter()
    def unavailable(_):raise OSError('synthetic missing attachment')
    h=NativeHistoryPublisher(tmp_path/'pub',source,a,clock=lambda:NOW,media_reader=unavailable)
    h.tick(limit=50);h.tick(limit=50)
    items=[row for _,packet in a.calls if packet['op']=='history' for row in packet['items']]
    assert {row['native_id'] for row in items}=={'qd:'+str(100+n) for n in range(1,75)}
    assert h.status()['unresolved_media'][0]['n']==1