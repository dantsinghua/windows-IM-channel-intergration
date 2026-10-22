"""Institutional Qidian host; existing QQ/WeChat and native send guards stay intact."""
from __future__ import annotations
import uuid
import asyncio
from contextlib import contextmanager
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import threading
import time
from .xunjia_v22 import XunjiaV22Host

def publication_error(exc):
    """Expose fixed error codes/status, never upstream bodies or credentials."""
    code=getattr(exc,"code",None)
    if not isinstance(code,str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}",code) is None:
        code=type(exc).__name__
    status=getattr(exc,"status",None)
    return code+(f" (HTTP {status})" if type(status) is int and 100<=status<=599 else "")


class SnapshotPublisher:
    def __init__(self, root, keys, workbench, name, client, *, clock=time.time):
        from relay.protocol import token
        token(workbench)
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/"snapshots.sqlite3"; self.keys=keys; self.workbench=workbench
        self.name=name; self.client=client; self.clock=clock; self.lock=threading.RLock()
        with self.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS revision(k TEXT PRIMARY KEY,n INTEGER); CREATE TABLE IF NOT EXISTS publication(k TEXT PRIMARY KEY,stamp REAL,fingerprint TEXT); CREATE TABLE IF NOT EXISTS sync_request(uid TEXT PRIMARY KEY,organization INTEGER,requested REAL,generation INTEGER NOT NULL DEFAULT 0);")
            columns={r[1] for r in db.execute("PRAGMA table_info(sync_request)")}
            if "generation" not in columns:
                db.execute("ALTER TABLE sync_request ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        self.path.chmod(0o600)
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10); db.row_factory=sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db: yield db
        finally: db.close()
    def publish(self, kind, uid, operation, path, payload):
        from relay.gateway_paths import CONTRACT,HISTORY_SECONDS
        from relay.protocol import Codec,envelope,canonical,digest
        key=operation+":"+uid
        with self.lock:
            with self.db() as db:
                db.execute("INSERT INTO revision VALUES(?,1) ON CONFLICT(k) DO UPDATE SET n=n+1",(key,))
                revision=db.execute("SELECT n FROM revision WHERE k=?",(key,)).fetchone()[0]
        now=self.clock(); body={**payload,"contract":CONTRACT,"op":operation,"revision":revision,"updated_at":now}
        binding=self.keys.binding(self.workbench,uid)
        env=envelope(binding,kind,body,message_id="snapshot-"+digest([self.workbench,key,revision]),created_at=now,expires_at=now+HISTORY_SECONDS)
        blob=Codec(binding,clock=self.clock).pack(env)
        self.client(path).upload("xj-"+digest([self.workbench,key,revision])+".bin",blob)
        with self.db() as db:
            db.execute("INSERT INTO publication VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET stamp=excluded.stamp,fingerprint=excluded.fingerprint",(key,now,digest(payload)))
        return env
    def directory(self, accounts):
        from relay.gateway_paths import DIRECTORY_PATH
        return self.publish("manifest","directory","directory",DIRECTORY_PATH,{"workbench_name":self.name,"accounts":accounts})
    def friends(self, uid, contacts):
        from relay.gateway_paths import account_paths
        if not isinstance(contacts,list): raise ValueError("FRIENDS_PROVIDER_INVALID")
        return self.publish("contacts",uid,"friends",account_paths(self.workbench,uid)["friends_path"],{"contacts":contacts})
    def organization(self, book):
        from relay.gateway_paths import ORGANIZATION_PATH
        if book.get("org_state") not in ("ready","empty"): raise ValueError("ORGANIZATION_SOURCE_NOT_READY")
        org=book.get("org")
        if not isinstance(org,dict) or not isinstance(org.get("departments"),list) or not isinstance(org.get("members"),list): raise ValueError("ORGANIZATION_SOURCE_INVALID")
        return self.publish("manifest","organization","organization",ORGANIZATION_PATH,{"state":book["org_state"],"org":org})
    def request_sync(self, uid, organization=False):
        with self.db() as db:
            db.execute("INSERT INTO sync_request(uid,organization,requested,generation) VALUES(?,?,?,1) ON CONFLICT(uid) DO UPDATE SET organization=MAX(organization,excluded.organization),requested=excluded.requested,generation=sync_request.generation+1",(uid,int(organization),self.clock()))
    def pending_sync(self):
        with self.db() as db:return [dict(r) for r in db.execute("SELECT * FROM sync_request ORDER BY requested")]
    def complete_sync(self, uid, requested, generation=None):
        # A timestamp alone cannot acknowledge an overlapping refresh.
        if generation is None:return
        with self.db() as db:db.execute("DELETE FROM sync_request WHERE uid=? AND requested=? AND generation=?",(uid,requested,generation))
    def last(self, key):
        with self.db() as db:
            row=db.execute("SELECT stamp FROM publication WHERE k=?",(key,)).fetchone()
        return row[0] if row else 0

class InstitutionAccountHost(XunjiaV22Host):
    def _publish_directory(self):
        # Institutional snapshots use their own fixed paths, not the account stream.
        return None
    def permission(self, command):
        return self.online_guard() and super().permission(command)
    def _send(self, command):
        if str(command.get("peer_id","")).startswith("g_"):
            return {"status":"failed","evidence":"group-auto-reply-not-supported"}
        if not self.online_guard():return {"status":"failed","evidence":"collector-account-offline"}
        return super()._send(command)
    def observe(self, **event):
        p=event.get("payload") or {}
        session=p.get("session") or {}
        native=session.get("native_id") or session.get("id") or p.get("session_id","")
        if (p.get("type","text")!="text" or p.get("revoked") or session.get("kind")=="group"
                or str(native).startswith("g_") or ":g_" in str(native)):return
        return super().observe(**event)

class InstitutionCollectorHost:
    def __init__(self, *, store,bus,loop,keys,config,settings,data_dir,legacy_inbox_disabled,
                 friends_provider,organization_provider,clock=time.time,client_factory=None,host_factory=None,history_provider=None,presence_provider=None):
        from relay.file_gateway_connection import connection_client
        from relay.file_gateway import GatewayAuth
        self.store,self.bus,self.loop=store,bus,loop; self.keys=keys; self.config=config
        self.settings=settings; self.workbench=settings.workbench_id; self.clock=clock
        self.legacy_inbox_disabled=legacy_inbox_disabled
        self.friends_provider=friends_provider; self.organization_provider=organization_provider
        self.client=client_factory or (lambda path:connection_client(config,config["auth"],path=path))
        self.root=Path(data_dir)/"institution-path-v1"/keys.institution_id/self.workbench
        self.publisher=SnapshotPublisher(self.root,keys,self.workbench,config["workbench_name"],self.client,clock=clock)
        self._factory=host_factory; self.hosts={}; self.active=False; self._tasks=[]; self._lease=None
        self._wake=asyncio.Event(); self._contacts_wake=asyncio.Event(); self.errors={}
        self.last_directory_at=0; self.applied_revision=None
        self.history_provider=history_provider; self.histories={}; self._history_wake=asyncio.Event(); self._history_next=0
        self.presence_provider=presence_provider;self.presence={};self._presence_wake=asyncio.Event();self._identity_candidates={}
    @classmethod
    async def from_settings(cls,settings,**kwargs):
        from relay.file_gateway import GatewayAuth
        from relay.gateway_paths import InstitutionKeys,CONTRACT
        from urllib.parse import urlsplit
        public,secrets=await settings._prepare({})
        auth=GatewayAuth(secrets["key"],secrets["sm4_key"].encode("utf8"))
        config={**public,"auth":auth,"contract":CONTRACT}
        if urlsplit(public["base_url"]).hostname==settings._seed.get("hostname"):
            for key in ("hostname","addresses"):
                if key in settings._seed:config[key]=settings._seed[key]
        return cls(settings=settings,keys=InstitutionKeys.from_auth(auth),config=config,**kwargs)
    def rows(self):
        from relay.protocol import token,ProtocolError
        result=[]
        for row in self.store.list_accounts(channel="qidian"):
            if row.get("deleted_ms") or not row.get("enabled",True) or not row.get("self_uid"):continue
            try:token(str(row["self_uid"]));token(row["id"])
            except ProtocolError:continue
            result.append(row)
        return result
    def online(self, aid, uid):
        rows=[r for r in self.rows() if str(r["self_uid"])==uid]
        native=self.presence.get((aid,uid),{})
        confirmed=(self.presence_provider is None or (native.get("state")=="online" and 0<=self.clock()-native.get("confirmed_at",0)<60))
        return bool(confirmed and self.active and len(rows)==1 and rows[0]["id"]==aid and rows[0]["state"] in ("running","degraded"))
    def _make_host(self,row):
        if self._factory:return self._factory(row)
        from relay.gateway_paths import account_paths
        from relay.collector_bridge import CollectorBridge
        from relay.qtrade_native_port import QTradeNativePort
        from server.receiver_ledger import ReceiverLedger
        from server.institution_transport import InstitutionTransport
        uid=str(row["self_uid"]);binding=self.keys.binding(self.workbench,uid)
        root=self.root/"accounts"/hashlib.sha256(uid.encode()).hexdigest(); paths=account_paths(self.workbench,uid)
        ledger=ReceiverLedger(root/"receiver.sqlite3",binding,clock=self.clock)
        adapter=InstitutionTransport(binding,self.client(paths["downlink_path"]),self.client(paths["uplink_path"]),root/"publication.sqlite3",role="collector",institution_owner=self.keys.owner_id,clock=self.clock)
        adapter.manifest_handler=lambda data:self._sync_request(uid,data)
        port=QTradeNativePort(binding=binding,store=self.store,bus=self.bus,loop=self.loop,legacy_inbox_disabled=self.legacy_inbox_disabled,execution_permission=ledger.permission)
        bridge=CollectorBridge(ledger=ledger,adapters={"oss":adapter},native_send=port.native_send,native_reconcile=port.native_reconcile)
        host=InstitutionAccountHost(store=self.store,bus=self.bus,loop=self.loop,binding=binding,bridge=bridge,port=port,state_path=root/"host.sqlite3",legacy_inbox_disabled=self.legacy_inbox_disabled)
        host.online_guard=lambda:self.online(row["id"],uid)
        if self.history_provider is not None:
            from .history_publish import NativeHistoryPublisher
            self.histories[(row["id"],uid)]=NativeHistoryPublisher(root/"native-history",self.history_provider(row),adapter,clock=self.clock)
        return host
    def _sync_request(self,uid,data):
        from relay.gateway_paths import CONTRACT
        from relay.protocol import ProtocolError
        p=data["payload"]
        if p.get("contract")==CONTRACT and p.get("op")=="activate_context":
            host=next((h for pair,h in self.hosts.items() if pair[1]==uid),None)
            if host is None:raise ProtocolError("CONTEXT_ACCOUNT_NOT_READY")
            channel=host.bridge.adapters["oss"]
            state=host.ledger.activate_context(p.get("context_id"),p.get("context_version"),
                published_source_highwater=channel.store.source_highwater)
            channel.publish_manifest({"contract":CONTRACT,"op":"context_ready",**state})
            self.publisher.request_sync(uid,False)
            self.loop.call_soon_threadsafe(self._contacts_wake.set)
            return
        if p.get("contract")!=CONTRACT or p.get("op")!="sync_contacts" or type(p.get("organization",False)) is not bool:
            raise ProtocolError("COLLECTOR_SYNC_REQUEST_UNSUPPORTED")
        self.publisher.request_sync(uid,p.get("organization",False))
        self.loop.call_soon_threadsafe(self._contacts_wake.set)
    async def reconcile_accounts(self):
        rows=self.rows(); wanted={(r["id"],str(r["self_uid"])):r for r in rows}
        for pair in list(self.hosts):
            if pair not in wanted:
                host=self.hosts.pop(pair); await host.stop()
                self.histories.pop(pair,None)
        for pair,row in wanted.items():
            if pair in self.hosts or not self.online(*pair):continue
            try:
                host=self._make_host(row); await host.start(); self.hosts[pair]=host
                self.publisher.request_sync(pair[1],False); self.errors.pop(pair[0],None)
            except Exception as exc:self.errors[pair[0]]=type(exc).__name__
    def directory_rows(self):
        from relay.gateway_paths import account_paths
        groups={}
        for row in self.rows():groups.setdefault(str(row["self_uid"]),[]).append(row)
        result=[]
        for uid,rows in sorted(groups.items()):
            row=sorted(rows,key=lambda r:r["id"])[0];host=self.hosts.get((row["id"],uid))
            online=bool(len(rows)==1 and self.online(row["id"],uid) and host and host._active and host._intake_ok)
            result.append({"account_id":row["id"],"account_uid":uid,"channel":"qidian",
                "account_nick":row.get("self_nick") or row.get("label") or uid,"online":online,
                "capabilities":["negotiation-v22-watermark","negotiation-v22-session-reset","negotiation-v22-fresh-context"] if online else [],
                "paths":account_paths(self.workbench,uid)})
        return result
    def observe(self, **event):
        if event.get("channel")=="qidian" and event.get("event")=="message":
            for pair,host in list(self.hosts.items()):
                if pair[0]==event.get("account_id"):host.observe(**event)
        if event.get("event") in ("account_state","account_created","account_deleted","account_updated"):
            self.loop.call_soon_threadsafe(self._wake.set)
            self.loop.call_soon_threadsafe(self._contacts_wake.set)
    def get_session_reset_status(self,account_id):
        from relay.gateway_paths import OFFLINE_SECONDS
        for pair,host in list(self.hosts.items()):
            if pair[0]!=account_id:continue
            data=host.get_session_reset_status(account_id)
            if data is None:return None
            now=self.clock();native=self.presence.get(pair,{})
            state=native.get("state","unknown")
            if state not in ("online","offline") or not 0<=now-native.get("checked_at",0)<OFFLINE_SECONDS:
                state="unknown"
            if state=="online" and not 0<=now-native.get("confirmed_at",0)<OFFLINE_SECONDS:
                state="unknown"
            relevant={"directory","directory_shutdown","contacts:"+pair[1],"history:"+pair[1],"presence:"+pair[1]}
            channel={"native_state":state,"native_checked_at":native.get("checked_at"),
                "heartbeat_published_at":self.last_directory_at or None,
                "heartbeat_fresh":bool(self.last_directory_at and 0<=now-self.last_directory_at<OFFLINE_SECONDS),
                "intake_ready":bool(host._active and host._intake_ok),
                "errors":{k:v for k,v in self.errors.items() if k in relevant}}
            return {**data,"channel_status":channel}
        return None
    async def _presence_loop(self):
        from relay.gateway_paths import HEARTBEAT_SECONDS
        while self.active:
            changed=self._wake.is_set();self._wake.clear()
            try:
                await self.reconcile_accounts()
                if changed or self.clock()-self.last_directory_at>=HEARTBEAT_SECONDS:
                    self.publisher.name=self.settings.view()["workbench_name"]
                    await asyncio.to_thread(self.publisher.directory,self.directory_rows())
                    self.last_directory_at=self.clock();self.errors.pop("directory",None)
            except Exception as exc:self.errors["directory"]=publication_error(exc)
            try:await asyncio.wait_for(self._wake.wait(),timeout=1)
            except asyncio.TimeoutError:pass
    def metadata_once(self):
        pending={r["uid"]:r for r in self.publisher.pending_sync()}
        rows=[r for r in self.rows() if self.online(r["id"],str(r["self_uid"]))]
        org_due=self.clock()-self.publisher.last("organization:organization")>=86400
        for row in rows:
            uid=str(row["self_uid"]);request=pending.get(uid);need_org=bool(request and request["organization"])
            try:
                if request or self.clock()-self.publisher.last("friends:"+uid)>=10:
                    self.publisher.friends(uid,self.friends_provider(row))
                if org_due or need_org:
                    self.publisher.organization(self.organization_provider(row));org_due=False
                if request:self.publisher.complete_sync(uid,request["requested"],request["generation"])
                self.errors.pop("contacts:"+uid,None)
            except Exception as exc:self.errors["contacts:"+uid]=publication_error(exc)
    async def probe_presence_once(self):
        if self.presence_provider is None:return
        semaphore=asyncio.Semaphore(3)
        async def probe(row):
            pair=(row["id"],str(row["self_uid"]))
            if row["state"] not in ("running","degraded"):
                self.presence[pair]={"state":"offline","code":"COLLECTOR_ACCOUNT_OFFLINE","checked_at":self.clock()};return
            async with semaphore:
                try:report=await asyncio.wait_for(self.presence_provider(row),18)
                except Exception:report={"state":"unknown","code":"NATIVE_PRESENCE_UNAVAILABLE"}
            old=self.presence.get(pair,{})
            if not isinstance(report,dict):report={"state":"unknown","code":"NATIVE_PRESENCE_INVALID"}
            state=report.get("state")
            observed=report.get("self_uid")
            if report.get("code")=="NATIVE_ACCOUNT_CHANGED":
                # Invalidate the old lease immediately, including before the second
                # observation. No queued command may execute using the new login.
                self.presence[pair]={"state":"offline","code":"NATIVE_ACCOUNT_CHANGED","checked_at":self.clock()}
                self.errors["presence:"+pair[1]]="NATIVE_ACCOUNT_CHANGED"
                candidate=(observed,self.clock())
                previous=self._identity_candidates.get(pair)
                self._identity_candidates[pair]=candidate
                if (isinstance(observed,str) and observed.isascii() and observed.isdigit()
                        and 5<=len(observed)<=20 and observed!=pair[1]
                        and previous and previous[0]==observed and 0<self.clock()-previous[1]<=30):
                    old_host=self.hosts.get(pair)
                    if old_host is not None:
                        await old_host.stop()
                        port=getattr(old_host,"port",None)
                        if port is not None and not port.quiescent():
                            self.errors["presence:"+pair[1]]="NATIVE_OLD_TASK_FINISHING"
                            return
                        if self.bus is not None and not self.bus.account_quiescent(pair[0]):
                            self.errors["presence:"+pair[1]]="NATIVE_OLD_TASK_FINISHING"
                            return
                        ledger=getattr(old_host,"ledger",None)
                        if ledger is not None:
                            ledger.activate_context(uuid.uuid4().hex,int(self.clock()*1000000))
                        self.hosts.pop(pair,None)
                    self.histories.pop(pair,None)
                    updated=self.store.observe_qidian_identity(pair[0],pair[1],observed)
                    if updated:
                        self._identity_candidates.pop(pair,None)
                        self.errors.pop("presence:"+pair[1],None)
                        new_pair=(pair[0],observed)
                        self.presence[new_pair]={"state":"online","code":"NATIVE_ACCOUNT_CHANGED","confirmed_at":self.clock(),"checked_at":self.clock()}
                        self.publisher.request_sync(observed,False)
                        if self.bus is not None:
                            self.bus.events.emit("account_updated",payload={"id":pair[0],"self_uid":observed,"reason":"native_account_changed"},account_id=pair[0],channel="qidian")
                        self._contacts_wake.set();self._history_wake.set()
                self._wake.set()
                return
            self._identity_candidates.pop(pair,None)
            if state=="online" and report.get("self_uid")!=pair[1]:state="unknown"
            if state=="online":self.presence[pair]={"state":"online","code":report.get("code"),"confirmed_at":self.clock(),"checked_at":self.clock()}
            elif state=="offline":self.presence[pair]={"state":"offline","code":report.get("code"),"checked_at":self.clock()}
            else:self.presence[pair]={**old,"code":report.get("code","NATIVE_PRESENCE_UNVERIFIED"),"checked_at":self.clock()}
            if self.online(*pair):self.errors.pop("presence:"+pair[1],None)
            else:self.errors["presence:"+pair[1]]=self.presence[pair].get("code","NATIVE_PRESENCE_UNVERIFIED")
        await asyncio.gather(*(probe(row) for row in self.rows()))
        self._wake.set()

    async def _native_presence_loop(self):
        while self.active:
            try:await self.probe_presence_once()
            except Exception as exc:self.errors["presence"]=type(exc).__name__
            try:await asyncio.wait_for(self._presence_wake.wait(),timeout=10)
            except asyncio.TimeoutError:pass
            self._presence_wake.clear()

    async def _history_loop(self):
        while self.active:
            pairs=[p for p in self.histories if self.online(*p)]
            if pairs:
                pair=pairs[self._history_next % len(pairs)];self._history_next+=1
                try:
                    await asyncio.to_thread(self.histories[pair].tick)
                    self.errors.pop("history:"+pair[1],None)
                except Exception as exc:self.errors["history:"+pair[1]]=type(exc).__name__
            try:await asyncio.wait_for(self._history_wake.wait(),timeout=2)
            except asyncio.TimeoutError:pass
            self._history_wake.clear()

    async def _contacts_loop(self):
        while self.active:
            self._contacts_wake.clear()
            try:await asyncio.to_thread(self.metadata_once)
            except Exception as exc:self.errors["metadata"]=type(exc).__name__
            try:await asyncio.wait_for(self._contacts_wake.wait(),timeout=10)
            except asyncio.TimeoutError:pass
    async def start(self):
        if self.active:return
        if not self.legacy_inbox_disabled():raise RuntimeError("legacy_xunjia_inbox_must_be_disabled")
        from server.receiver_ledger import account_lock
        lease=account_lock(self.root/"collector-worker",self.keys.binding(self.workbench,"directory"),blocking=False)
        lease.__enter__();self._lease=lease;self.active=True
        try:
            await self.reconcile_accounts()
            self._wake.set();self._contacts_wake.set()
            self._tasks=[asyncio.create_task(self._presence_loop(),name="institution-presence"),asyncio.create_task(self._contacts_loop(),name="institution-contacts")]
            if self.history_provider is not None:self._tasks.append(asyncio.create_task(self._history_loop(),name="institution-native-history"))
            if self.presence_provider is not None:self._tasks.append(asyncio.create_task(self._native_presence_loop(),name="institution-native-presence"))
            saved=self.settings.saved();self.applied_revision=saved["revision"] if saved else None
            self.settings.state="running";self.settings.applied_revision=self.applied_revision
        except BaseException:
            await self.stop();raise
    async def stop(self):
        self.active=False;self._wake.set();self._contacts_wake.set();self._history_wake.set();self._presence_wake.set()
        if self._tasks:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(*self._tasks)),timeout=125)
            self._tasks=[]
        for host in list(self.hosts.values()):await host.stop()
        self.hosts.clear()
        try:await asyncio.to_thread(self.publisher.directory,self.directory_rows())
        except Exception:self.errors["directory_shutdown"]="OFFLINE_SNAPSHOT_NOT_UPLOADED"
        if self._lease:self._lease.__exit__(None,None,None);self._lease=None
        self.settings.state="stopped"
    def status(self):
        return {"contract":self.config["contract"],"active":self.active,"workbench_id":self.workbench,
                "applied_revision":self.applied_revision,"last_directory_at":self.last_directory_at,
                "account_workers":len(self.hosts),"errors":dict(self.errors),
                "history":{uid:h.status() for (_,uid),h in self.histories.items()},
                "native_presence":{uid:dict(value) for (_,uid),value in self.presence.items()}}
