"""Current organization chain on synthetic read-only SQLite and local publications."""
import asyncio
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from qtrade_agent.app import AgentApp
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.xor import xor
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from qtrade_agent.store import Store
from relay.file_gateway import GatewayAuth
from relay.gateway_paths import InstitutionKeys, ORGANIZATION_PATH, CONTRACT
from relay.protocol import Codec


@pytest.fixture
def organization_chain(tmp_path):
    uid="3000000001"
    source=tmp_path/(uid+".db")
    org=tmp_path/(uid+"-org.db");people=tmp_path/(uid+"-org-member.db")
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE Friends(uin BLOB,remark BLOB,name BLOB);CREATE TABLE QidianExternalInfo(uin BLOB,nickname BLOB);")
        db.execute("INSERT INTO Friends VALUES(?,?,?)",(xor(b"90001"),xor("Synthetic A".encode("utf-16-le")),b""))
    with sqlite3.connect(org) as db:
        db.executescript("CREATE TABLE OrgDepartment(uid TEXT,pid TEXT,name TEXT);CREATE TABLE OrgD2M(did TEXT,mid TEXT,ord INT);CREATE TABLE OrgD2D(did TEXT,ord INT);")
        db.execute("INSERT INTO OrgDepartment VALUES('desk','0','Synthetic desk')")
        db.execute("INSERT INTO OrgD2M VALUES('desk','90001',1)")
    with sqlite3.connect(people) as db:
        db.execute("CREATE TABLE OrgMember(uin TEXT,name TEXT,account TEXT)")
        db.execute("INSERT INTO OrgMember VALUES('90001','Synthetic A','a')")
    key=tmp_path/"org-key.bin";key.write_bytes(b"synthetic-only");key.chmod(0o600)
    config=tmp_path/"org.json"
    config.write_text(json.dumps({"account_uid":uid,"org_password_file":key.name,"org_password_format":"raw"}));config.chmod(0o600)
    store=Store(str(tmp_path/"agent.db")).open()
    store.ensure_account("qd01","qidian",state="running",self_uid=uid)
    app=object.__new__(AgentApp)
    app._maindb_for_uid=lambda account_uid,account:LocalSqliteMainDb(str(source))
    app._xunjia_org_config=lambda account_uid:str(config)
    now=[1800000000.0];uploaded=[]
    class Client:
        def __init__(self,path):self.path=path
        def upload(self,name,blob):uploaded.append((self.path,name,blob))
    loop=asyncio.new_event_loop()
    keys=InstitutionKeys.from_auth(GatewayAuth("synthetic",b"0123456789abcdef"))
    host=InstitutionCollectorHost(store=store,bus=None,loop=loop,keys=keys,
        config={"workbench_name":"desk","contract":CONTRACT},
        settings=SimpleNamespace(workbench_id="synthetic-org-wb"),data_dir=tmp_path,
        legacy_inbox_disabled=lambda:True,
        friends_provider=lambda row:app._xunjia_v22_contacts(row)["contacts"],
        organization_provider=app._xunjia_v22_contacts,clock=lambda:now[0],client_factory=Client)
    host.active=True
    def publications():
        binding=keys.binding("synthetic-org-wb","organization")
        return [Codec(binding,clock=lambda:now[0]).unpack(blob) for path,_,blob in uploaded if path==ORGANIZATION_PATH]
    try:
        yield SimpleNamespace(app=app,host=host,store=store,now=now,uploaded=uploaded,publications=publications,
            org=org,people=people,config=config,key=key,source=source)
    finally:
        host.active=False;loop.close();store.close()


def test_current_chain_reads_and_publishes_real_organization_without_mutating_sources(organization_chain):
    s=organization_chain
    inputs=[s.source,s.org,s.people,s.config,s.key]
    before={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    s.host.metadata_once()
    [published]=s.publications()
    assert published["payload"]["state"]=="ready"
    org=published["payload"]["org"]
    assert org["departments"][0]["name"]=="Synthetic desk"
    assert org["members"][0]["uin"]=="90001"
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==value for path,value in before.items())
    assert s.host.errors=={}


def test_current_chain_publishes_successfully_read_empty_organization(organization_chain):
    s=organization_chain
    with sqlite3.connect(s.org) as db:
        db.executescript("DELETE FROM OrgD2M;DELETE FROM OrgDepartment;")
    with sqlite3.connect(s.people) as db:db.execute("DELETE FROM OrgMember")
    s.host.metadata_once()
    [published]=s.publications()
    assert published["payload"]["state"]=="empty"
    assert published["payload"]["org"]=={"departments":[],"members":[]}
    assert s.host.errors=={}


@pytest.mark.parametrize("failure",["unreported","bad-data","unexpected-provider"])
def test_current_refresh_failure_preserves_last_successful_org_and_original_timestamp(organization_chain,failure):
    s=organization_chain;s.host.metadata_once()
    [before]=s.publications();stamp=s.host.publisher.last("organization:organization")
    previous_uploads=[entry for entry in s.uploaded if entry[0]==ORGANIZATION_PATH]
    if failure=="unreported":s.config.write_text(json.dumps({"account_uid":"3000000001"}))
    elif failure=="bad-data":s.org.write_bytes(b"synthetic-bad-encrypted-data"*64)
    else:
        def broken(row):raise RuntimeError("synthetic-private-detail-must-not-escape")
        s.host.organization_provider=broken
    s.now[0]+=60
    s.host.publisher.request_sync("3000000001",True)
    s.host.metadata_once()
    assert s.publications()==[before]
    assert [entry for entry in s.uploaded if entry[0]==ORGANIZATION_PATH]==previous_uploads
    assert s.host.publisher.last("organization:organization")==stamp
    assert s.host.errors["contacts:3000000001"] in ("ValueError","RuntimeError")
    assert "synthetic-private-detail" not in json.dumps(s.host.errors)
    assert len(s.host.publisher.pending_sync())==1
