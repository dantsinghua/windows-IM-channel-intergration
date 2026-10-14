"""Linux half of the local E2E launcher. No global WSL/Docker operations."""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,time,socket,tomllib,tempfile,shutil
from pathlib import Path
from urllib.request import build_opener,ProxyHandler,Request
from urllib.error import HTTPError
from common import verify,save,digest
from runtime_guard import owned_guest
DATA=Path("/home/anlin/work/qtrade-build/real-chain-20261010")
BUILDS=Path("/home/anlin/work/qtrade-build/local-e2e-releases")
PY="/home/anlin/.venvs/qtrade/bin/python"
def http(url,token=None):
 h={"Authorization":"Bearer "+token} if token else {}
 try:
  with build_opener(ProxyHandler({})).open(Request(url,headers=h),timeout=15) as r:return r.status,json.load(r)
 except HTTPError as e:
  try:return e.code,{}
  finally:e.close()
def bind():
 token=sys.stdin.buffer.read(4097).decode("ascii").strip()
 if not (32<=len(token)<=4096) or not all(c.isalnum() or c in "_-" for c in token):raise ValueError("Invalid bootstrap credential")
 cfg=tomllib.loads((DATA/"agent.toml").read_text())
 target=Path(cfg["winagent"]["token_file"])
 if target!=DATA/"winagent.token" or target.is_symlink():raise ValueError("Unapproved credential destination")
 status,_=http(cfg["winagent"]["url"]+"/wa/v1/time",token)
 if status!=200:raise ValueError("Agent-role credential rejected before any file replacement: "+str(status))
 if target.exists() and target.read_text().strip()!=token:
  backup=target.with_name("winagent.token.before-"+str(time.time_ns()))
  fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,"wb") as f:f.write(target.read_bytes())
 fd,name=tempfile.mkstemp(prefix=".winagent-token-",dir=DATA)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,"wb") as f:f.write(token.encode());f.flush();os.fsync(f.fileno())
  os.replace(name,target)
 finally:
  if os.path.exists(name):os.unlink(name)
 status,_=http(cfg["winagent"]["url"]+"/wa/v1/time",token)
 if status!=200:raise ValueError("Agent-role credential verification failed: "+str(status))
 print(json.dumps({"bound":True,"agent_role_time":status,"mode":oct(target.stat().st_mode&0o777)}))
def build(release):
 source=release/"source";verify(source,json.loads((release/"source-manifest.json").read_text()))
 console=source/"console";deps=Path("/home/anlin/work/qtrade-build/final-regression-20261010/console")
 if not (console/"node_modules").exists():
  if (console/"package-lock.json").read_bytes()==(deps/"package-lock.json").read_bytes():(console/"node_modules").symlink_to(deps/"node_modules",target_is_directory=True)
  else:run(["npm","ci"],console,release/"npm-ci.log",600)
 run([PY,"-B","-m","pytest","-q","-p","no:cacheprovider","tests/test_local_e2e_launcher.py","tests/test_local_e2e_launcher_lock.py","tests/test_auth_health_closeout.py","tests/test_ossbox_sts.py","tests/acceptance/test_spec_sts.py","tests/acceptance/test_sts_mq_contract.py","tests/acceptance/test_sts_settings_closure.py"],source,release/"backend-gates.log",600)
 for task in ("typecheck","test","build"):run(["npm","run",task],console,release/(task+".log"),600)
 config="import base from "+json.dumps(str(console/"vite.config.ts"))+";\nimport {mergeConfig} from \"vite\";\nexport default mergeConfig(base,{server:{host:\"127.0.0.1\",port:5313,strictPort:true,proxy:{\"/api/v1\":{target:\"http://127.0.0.1:17650\",ws:true,changeOrigin:true}}}});\n"
 (console/"vite.local-e2e.config.mjs").write_text(config)
 verify(source,json.loads((release/"source-manifest.json").read_text()))
 assets={str(p.relative_to(release)):digest(p) for p in (console/"dist").rglob("*") if p.is_file()}
 assets[str((console/"vite.local-e2e.config.mjs").relative_to(release))]=digest(console/"vite.local-e2e.config.mjs")
 save(release/"runtime-artifacts.json",assets)
 print("FRONTEND_BUILD_OK")
def run(cmd,cwd,log,timeout):
 env={k:v for k,v in os.environ.items() if not k.upper().startswith(("OSS_","OSSBOX_","XUNJIA_","QT_DEV_","VITE_DEV_","PW_","AGENT_BASE_URL"))}
 env.update(PYTHONDONTWRITEBYTECODE="1",PYTHONIOENCODING="utf-8")
 with log.open("wb") as f:r=subprocess.run(cmd,cwd=cwd,stdout=f,stderr=subprocess.STDOUT,timeout=timeout,env=env)
 if r.returncode:raise RuntimeError("Command failed; see "+str(log))
def running_owned():
 result=[]
 for path in Path("/proc").glob("[0-9]*/cmdline"):
  pid=int(path.parent.name)
  if pid in (os.getpid(),os.getppid()):continue
  try:
   args=path.read_bytes().decode().split("\0");cwd=(path.parent/"cwd").resolve();joined=" ".join(args)
  except (OSError,UnicodeError):continue
  kind=owned_guest(args,cwd,BUILDS,DATA)
  if kind:result.append((pid,kind,path.read_bytes()))
 return result
def restart(release):
 verify(release/"source",json.loads((release/"source-manifest.json").read_text()))
 verify(release,json.loads((release/"runtime-artifacts.json").read_text()))
 for pid,kind,before in running_owned():
  path=Path("/proc")/str(pid)/"cmdline"
  if not path.exists():continue
  if path.read_bytes()!=before:raise RuntimeError("Process ownership changed")
  os.kill(pid,signal.SIGTERM)
  deadline=time.monotonic()+45
  while path.exists() and path.read_bytes() and time.monotonic()<deadline:time.sleep(.2)
  if path.exists() and path.read_bytes():raise RuntimeError("Graceful shutdown timed out: "+kind)
 for port in (17650,5313):
  with socket.socket() as s:
   if s.connect_ex(("127.0.0.1",port))==0:raise RuntimeError("Unowned listener on "+str(port))
 source=release/"source";logs=release/"runtime";logs.mkdir(exist_ok=True)
 cmds={"agent":[PY,"-B",str(source/"scripts/local_e2e/agent_entry.py"),"--data",str(DATA),"--release-id",release.name],"vite":["/usr/bin/node",str(source/"console/node_modules/vite/bin/vite.js"),"--config",str(source/"console/vite.local-e2e.config.mjs")]}
 pids={}
 for kind,cmd in cmds.items():
  with (logs/(kind+".log")).open("ab") as f:p=subprocess.Popen(cmd,cwd=DATA if kind=="agent" else source/"console",stdout=f,stderr=subprocess.STDOUT,start_new_session=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1"))
  pids[kind]=p.pid
 save(logs/"pids.json",pids)
 print(json.dumps(pids))
if __name__=="__main__":
 a=argparse.ArgumentParser();a.add_argument("action",choices=["build","bind","restart"]);a.add_argument("--release");o=a.parse_args()
 if o.action=="bind":bind()
 else:
  r=Path(o.release).resolve()
  if r.parent!=BUILDS:raise ValueError("Unapproved release root")
  globals()[o.action](r)
