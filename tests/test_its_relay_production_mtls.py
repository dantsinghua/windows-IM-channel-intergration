"""Production header and mTLS over a real loopback HTTPS server, synthetic PKI only."""
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import threading
import uuid
from urllib.parse import urlsplit, parse_qs
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from relay.file_gateway import GatewayAuth, FileGatewayError
from relay.file_gateway_connection import connection_client, client_tls_context, resolve_tls_files

AUTH = GatewayAuth("synthetic-production-business-key", b"0123456789abcdef")

@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-mtls")
    now = datetime.now(timezone.utc)
    def make(name, issuer=None, server=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        b = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer[0].subject if issuer else subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True)
        if issuer:
            b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),critical=False)
        if server:
            b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),critical=False)
        cert = b.sign(issuer[1] if issuer else key, hashes.SHA256())
        cp,kp=root/(name+".crt"),root/(name+".key")
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        kp.chmod(0o600)
        return cert,key,cp,kp
    ca=make("synthetic-ca");server=make("localhost",ca,True);client=make("synthetic-client",ca)
    return root,ca,server,client

@pytest.fixture
def wire(pki):
    root,ca,server_cert,client_cert=pki
    state={"requests":[],"files":[],"redirect":False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,code,body=b"",headers=None):
            self.send_response(code)
            for key,value in (headers or {}).items():self.send_header(key,value)
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def handle_request(self):
            parsed=urlsplit(self.path);op=parsed.path.rsplit("/",1)[-1]
            state["requests"].append({"op":op,"path":parse_qs(parsed.query),"headers":dict(self.headers),"client":self.connection.getpeercert()})
            if not self.headers.get("accesstoken") or self.headers.get("access_token"):
                return self.respond(401,b"{}")
            if state["redirect"]:
                return self.respond(302,headers={"Location":"https://other.invalid/forbidden"})
            if op=="pending":return self.respond(200,json.dumps({"pending":len(state["files"])}).encode())
            if op=="upload":
                body=self.rfile.read(int(self.headers["Content-Length"]))
                raw=("Content-Type: "+self.headers["Content-Type"]+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+body
                part=next(BytesParser(policy=policy.default).parsebytes(raw).iter_parts())
                name=part.get_filename();state["files"].append((name,part.get_payload(decode=True)))
                return self.respond(200,json.dumps({"file_key":str(uuid.uuid4()),"file_name":name}).encode())
            assert op=="download"
            if not state["files"]:return self.respond(204)
            name,content=state["files"].pop(0)
            return self.respond(200,content,{"Content-Disposition":'attachment; filename="'+name+'"',"Content-Type":"application/octet-stream"})
        do_GET=handle_request
        do_POST=handle_request
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler);server.daemon_threads=True
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(server_cert[2]),str(server_cert[3]));context.load_verify_locations(str(ca[2]));context.verify_mode=ssl.CERT_REQUIRED
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    cfg={"base_url":f"https://localhost:{server.server_port}","client_cert_file":str(client_cert[2]),"client_key_file":str(client_cert[3]),"ca_file":str(ca[2])}
    try:yield cfg,state
    finally:server.shutdown();server.server_close();thread.join(5)

@pytest.mark.parametrize("pinned",[False,True])
def test_all_three_interfaces_require_mtls_and_exact_header(wire,pinned):
    cfg,state=wire
    if pinned:cfg={**cfg,"hostname":"localhost","addresses":["127.0.0.1"]}
    client=connection_client(cfg,AUTH,path="isolated_test")
    assert client.pending()==0
    content=b"synthetic mTLS file round-trip\x00\xff"
    assert client.upload("probe.txt",content)["file_name"]=="probe.txt"
    assert client.pending()==1
    item=client.download();assert item.name=="probe.txt" and item.content==content
    assert client.pending()==0 and client.download() is None
    requests=state["requests"];assert len(requests)==6
    tokens=[]
    for row in requests:
        headers={k.lower():v for k,v in row["headers"].items()}
        assert "access_token" not in headers and "accesstoken" in headers
        assert row["client"] and row["path"]=={"path":["isolated_test"]}
        tokens.append(headers["accesstoken"])
    assert len(set(tokens))==len(tokens)

def test_missing_client_cert_never_reaches_http(wire):
    cfg,state=wire;cfg={k:v for k,v in cfg.items() if not k.startswith("client_")}
    with pytest.raises(FileGatewayError):connection_client(cfg,AUTH,path="isolated_test").pending()
    assert not state["requests"]

def test_server_chain_verification_not_disabled(wire):
    cfg,state=wire;cfg={k:v for k,v in cfg.items() if k!="ca_file"}
    with pytest.raises(FileGatewayError,match="FILE_TLS_SERVER_CERT_INVALID"):
        connection_client(cfg,AUTH,path="isolated_test").pending()
    assert not state["requests"]

def test_hostname_verification_not_disabled(wire):
    cfg,state=wire;cfg={**cfg,"base_url":cfg["base_url"].replace("localhost","127.0.0.1")}
    with pytest.raises(FileGatewayError,match="FILE_TLS_SERVER_CERT_INVALID"):
        connection_client(cfg,AUTH,path="isolated_test").pending()
    assert not state["requests"]

def test_mismatched_client_key_fails_before_network(pki):
    _,ca,server,client=pki
    with pytest.raises(FileGatewayError,match="FILE_TLS_CLIENT_CERT_INVALID"):
        client_tls_context({"client_cert_file":str(client[2]),"client_key_file":str(server[3])})

@pytest.mark.parametrize("field",["client_cert_file","client_key_file"])
def test_partial_pair_rejected(field,pki):
    with pytest.raises(FileGatewayError,match="FILE_TLS_CERT_PAIR_REQUIRED"):
        client_tls_context({field:str(pki[3][2])})

def test_unreadable_file_has_safe_error(tmp_path):
    cfg={"client_cert_file":str(tmp_path/"secret-cert"),"client_key_file":str(tmp_path/"secret-key")}
    with pytest.raises(FileGatewayError,match="FILE_TLS_FILE_UNAVAILABLE") as caught:client_tls_context(cfg)
    assert "secret" not in str(caught.value)

def test_client_tls_cannot_downgrade_to_plaintext(wire):
    cfg,state=wire;cfg={**cfg,"base_url":cfg["base_url"].replace("https:","http:")}
    with pytest.raises(FileGatewayError,match="FILE_TLS_HTTPS_REQUIRED"):connection_client(cfg,AUTH)
    assert not state["requests"]

def test_redirect_never_forwards_token_or_client_certificate(wire):
    cfg,state=wire;state["redirect"]=True
    with pytest.raises(FileGatewayError) as caught:connection_client(cfg,AUTH,path="isolated_test").pending()
    assert caught.value.status==302 and len(state["requests"])==1

def test_production_origin_requires_explicit_cert_pair():
    with pytest.raises(FileGatewayError,match="FILE_TLS_CLIENT_CERT_REQUIRED"):
        connection_client({"base_url":"https://its-relay.yingmi.cn"},AUTH,path="isolated_test")

def test_certificate_paths_resolve_against_deployment_not_cwd(tmp_path):
    result=resolve_tls_files({"client_cert_file":"tls/c.crt","client_key_file":"tls/c.key","ca_file":""},tmp_path)
    assert result=={"client_cert_file":str(tmp_path/"tls/c.crt"),"client_key_file":str(tmp_path/"tls/c.key")}


def test_collector_settings_persist_and_use_same_tls_for_test_and_runtime(wire,tmp_path):
    import asyncio
    from qtrade_agent.store import Store
    from qtrade_agent.vault_client import FakeVault
    from qtrade_agent.ossbox.collector_settings import CollectorReportingSettings
    from qtrade_agent.ossbox.institution import InstitutionCollectorHost
    config,state=wire
    store=Store(str(tmp_path/"collector.db")).open()
    settings=CollectorReportingSettings(store,FakeVault())
    async def run():
        values={**config,"workbench_name":"synthetic-mtls-workbench","key":AUTH.key,"sm4_key":AUTH.sm4_key.decode()}
        await settings.save(values,actor="synthetic-mtls-test")
        public=settings.view()
        for field in ("client_cert_file","client_key_file","ca_file"):assert public[field]==config[field]
        assert AUTH.key not in json.dumps(public) and "sm4_key" not in settings.saved()
        answer=await settings.test({})
        assert answer["authenticated"] is True and answer["pending"]==0
        host=await InstitutionCollectorHost.from_settings(settings,store=store,bus=None,
            loop=asyncio.get_running_loop(),data_dir=str(tmp_path),legacy_inbox_disabled=lambda:True,
            friends_provider=lambda *_:[],organization_provider=lambda *_:{})
        for field in ("client_cert_file","client_key_file","ca_file"):assert host.config[field]==config[field]
        assert host.client("isolated_test").pending()==0
    try:asyncio.run(run())
    finally:store.close()
