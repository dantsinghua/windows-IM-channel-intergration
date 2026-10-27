"""Known synthetic BKS/DEX/native files; no network, credentials or real ADB."""
import base64
import hashlib
import hmac
import io
import shlex
import sqlite3
import struct
from types import SimpleNamespace
import zipfile

import pytest
from qtrade_agent.adapters.qidian import org_native as native
from qtrade_agent.adapters.qidian.org import OrgReadError, read_account_org

STRINGS={'store_password':'synthetic-store','alias_prefix_confidentiality':'c-',
         'alias_prefix_integrity':'i-','alias_application':'synthetic','alias_sdk_suffix':'-sdk',
         'key_separator':':'}

def bks(keys=(b'A'*16,b'B'*32),*,duplicate=False):
    from jks.rfc7292 import derive_key
    from Cryptodome.Cipher import DES3
    utf=lambda s:struct.pack('>H',len(s.encode()))+s.encode()
    data=lambda b:struct.pack('>I',len(b))+b
    salt=b'synth123';iterations=10;password=STRINGS['store_password'];body=b''
    for index,key in enumerate(keys):
        alias=('c-' if index==0 or duplicate else 'i-')+'synthetic-sdk'
        plain=b'\x02'+utf('RAW')+utf('AES')+data(key)
        padding=8-len(plain)%8;plain+=bytes([padding])*padding
        dk=derive_key(hashlib.sha1,1,password,salt,iterations,24)
        iv=derive_key(hashlib.sha1,2,password,salt,iterations,8)
        sealed=data(salt)+struct.pack('>I',iterations)+DES3.new(dk,DES3.MODE_CBC,iv).encrypt(plain)
        body+=b'\x04'+utf(alias)+struct.pack('>QI',123,0)+data(sealed)
    body+=b'\0'
    mac=hmac.new(derive_key(hashlib.sha1,3,password,salt,iterations,20),body,hashlib.sha1).digest()
    return struct.pack('>I',2)+data(salt)+struct.pack('>I',iterations)+body+mac


def apk():
    values=list(STRINGS.values());count=len(values);head=bytearray(112+4*count)
    head[:8]=b'dex\n035\0';struct.pack_into('<II',head,36,112,0x12345678)
    struct.pack_into('<II',head,56,count,112)
    for i,value in enumerate(values):
        struct.pack_into('<I',head,112+4*i,len(head));head+=bytes([len(value)])+value.encode()+b'\0'
    struct.pack_into('<I',head,32,len(head))
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w')as z:z.writestr('classes5.dex',head)
    return stream.getvalue(),dict(zip(STRINGS,range(count)))


def databases():
    org=sqlite3.connect(':memory:');people=sqlite3.connect(':memory:')
    org.executescript("CREATE TABLE OrgDepartment(uid,pid,name);CREATE TABLE OrgD2M(did,mid,ord);INSERT INTO OrgDepartment VALUES('d','0','Synthetic');")
    people.executescript("CREATE TABLE OrgMember(uin,name,account);INSERT INTO OrgMember VALUES('9001','Synthetic','test');")
    result=(org.serialize(),people.serialize());org.close();people.close();return result


@pytest.fixture
def current(monkeypatch):
    uid='3000000001';apk_blob,indices=apk();apk_path='/data/app/synthetic/base.apk';digest=hashlib.sha256(apk_blob).hexdigest()
    monkeypatch.setattr(native,'PROFILES',{digest:{'dex':'classes5.dex','sdk_min':26,'string_indices':indices}})
    root='/data/data/com.tencent.qidian';org,people=databases()
    files={apk_path:apk_blob,root+'/ks.jks':bks(),root+'/databases/'+uid+'-org.db':org,root+'/databases/'+uid+'-org-member.db':people}
    state={'boot':'00000000-0000-0000-0000-000000000001','identity':('slot-a','generation-a'),'stamp':1,'reads':[],'files':files}
    def shell(command):
        a=shlex.split(command)
        if a[:2]==['cat','/proc/sys/kernel/random/boot_id']:return state['boot']
        if a[:2]==['pm','path']:return 'package:'+apk_path
        if a[0]=='getprop':return '30'
        if a[0]=='sha256sum':return hashlib.sha256(files[a[1]]).hexdigest()+'  '+a[1]
        if a[0]=='stat':return '\n'.join(str(len(files[p]))+':'+str(state['stamp'])for p in a[3:])
        pytest.fail('unexpected native command')
    source=SimpleNamespace(path=root+'/databases/'+uid+'.db',serial='synthetic-device',server_port=16000,adb='synthetic-adb',timeout_s=5,
                           _shell=shell,org_identity_check=lambda:state['identity'])
    def run(args,**kwargs):
        assert args[:6]==['synthetic-adb','-P','16000','-s',source.serial,'exec-out']
        assert args[6:8]==['head','-c'];state['reads'].append(args[-1])
        return SimpleNamespace(returncode=0,stdout=files[args[-1]][:int(args[8])])
    monkeypatch.setattr(native.subprocess,'run',run)
    return source,state,uid


def test_standard_bks_mac_and_sealed_raw_keys_are_verified():
    assert native.password_from_keystore(bks(),STRINGS)==base64.b64encode(b'A'*16)+b':'+base64.b64encode(b'B'*32)


@pytest.mark.parametrize('kind',['mac','duplicate','workfactor'])
def test_bad_keystore_is_safe_failure(kind):
    data=bytearray(bks(duplicate=kind=='duplicate'))
    if kind=='mac':data[-1]^=1
    if kind=='workfactor':struct.pack_into('>I',data,8+8,0xffffffff)
    with pytest.raises(OrgReadError)as got:native.password_from_keystore(bytes(data),STRINGS)
    assert str(got.value).startswith('ORG_') and 'synthetic-store' not in str(got.value)


def test_remote_reads_current_bks_without_account_config_and_reuses_only_stable_cache(current):
    source,state,uid=current;cache={}
    r=read_account_org(source,uid,'absent-old-config',cache=cache,clock=lambda:10)
    assert r['org_state']=='ready' and len(r['org']['members'])==1
    count=len(state['reads'])
    assert read_account_org(source,uid,'absent-old-config',cache=cache,clock=lambda:20)==r
    assert len(state['reads'])==count+1  # Revalidate current keystore, never recopy APK/databases.
    state['identity']=('slot-b','generation-b');state['boot']='00000000-0000-0000-0000-000000000002'
    changed=read_account_org(source,uid,'absent-old-config',cache=cache,clock=lambda:30)
    assert changed['org_updated_at']==30 and len(state['reads'])>count+1


def test_changed_keystore_cannot_reuse_old_success(current):
    source,state,uid=current;cache={};read_account_org(source,uid,'unused',cache=cache)
    p='/data/data/com.tencent.qidian/ks.jks';data=bytearray(state['files'][p]);data[-1]^=1;state['files'][p]=bytes(data)
    with pytest.raises(OrgReadError,match='ORG_KEYSTORE_AUTHENTICATION_FAILED'):read_account_org(source,uid,'unused',cache=cache)
    assert uid not in cache


def test_source_mapping_change_during_read_refuses_result(current,monkeypatch):
    source,state,uid=current;cache={};original=native._read
    def read(*args):
        result=original(*args)
        if args[1].endswith('-org-member.db'):state['identity']=('other-slot','other-generation')
        return result
    monkeypatch.setattr(native,'_read',read)
    with pytest.raises(OrgReadError,match='ORG_SOURCE_CHANGED'):read_account_org(source,uid,'unused',cache=cache)
    assert uid not in cache


def test_unknown_apk_refuses_before_keystore(current):
    source,state,uid=current;state['files']['/data/app/synthetic/base.apk']=b'unsupported'
    with pytest.raises(OrgReadError,match='ORG_APK_PROFILE_UNSUPPORTED'):read_account_org(source,uid,'unused')
    assert not state['reads']


def test_same_size_same_second_database_change_never_returns_old_ready_cache(current):
    source,state,uid=current;cache={}
    read_account_org(source,uid,'unused',cache=cache)
    p='/data/data/com.tencent.qidian/databases/'+uid+'-org.db'
    state['files'][p]=b'X'*len(state['files'][p])
    with pytest.raises(OrgReadError):read_account_org(source,uid,'unused',cache=cache)
    assert uid not in cache
