"""Build an immutable, verified release from the saved working tree."""
from __future__ import annotations
import json,os,shutil,subprocess,sys,time,zipfile
from pathlib import Path
from common import manifest,fingerprint,verify,digest,save
REPO=Path(__file__).resolve().parents[2]
BUILD=Path.home()/"qtrade-build/local-e2e"
LINUX="/home/anlin/work/qtrade-build/local-e2e-releases"
UNC=Path(r"\\wsl.localhost\Ubuntu-24.04")
PY=REPO/"winagent/.venv-build/Scripts/python.exe"
NO_WINDOW=0x08000000
def command(cmd,log,cwd=None,timeout=900):
 log.parent.mkdir(parents=True,exist_ok=True)
 with log.open("wb") as out:p=subprocess.run([str(x) for x in cmd],cwd=cwd,stdout=out,stderr=subprocess.STDOUT,timeout=timeout,creationflags=NO_WINDOW)
 if p.returncode:raise RuntimeError("Build gate failed: "+str(log))
def merge_native(sources,target):
 target.mkdir(parents=True,exist_ok=False)
 for source in sources:
  for path in source.rglob("*"):
   if not path.is_file():continue
   rel=path.relative_to(source);dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True)
   if not dest.exists():shutil.copy2(path,dest);continue
   if digest(path)==digest(dest):continue
   if path.suffix!=".zip":raise RuntimeError("Native payload conflict: "+str(rel))
   with zipfile.ZipFile(path) as a,zipfile.ZipFile(dest) as b:
    left={n:a.read(n) for n in a.namelist()};right={n:b.read(n) for n in b.namelist()}
   if any(left[n]!=right[n] for n in left.keys()&right.keys()):raise RuntimeError("Native library conflict")
   with zipfile.ZipFile(dest,"w",zipfile.ZIP_DEFLATED) as out:
    for n,data in sorted((right|left).items()):out.writestr(n,data)
def stock_electron(version):
 if version!="33.4.11":raise RuntimeError("Review official Electron archive checksum before changing runtime version")
 root=BUILD/("electron-"+version);record=BUILD/("electron-"+version+".json")
 if record.exists():verify(root,json.loads(record.read_text()));return root/"electron.exe"
 archive=UNC/"home/anlin/.cache/electron/electron-v33.4.11-win32-x64.zip"
 if digest(archive)!="f64c8a5a81d9b420b636fdba13e180f49d69f2198e1d86a8b01f858b17a9483c":raise RuntimeError("Official Electron archive checksum mismatch")
 if root.exists():raise RuntimeError("Incomplete stock Electron extraction; inspect before retry")
 root.mkdir(parents=True)
 with zipfile.ZipFile(archive) as z:
  for name in z.namelist():
   p=Path(name)
   if p.is_absolute() or ".." in p.parts:raise RuntimeError("Unsafe archive path")
  z.extractall(root)
 save(record,{str(p.relative_to(root)):digest(p) for p in root.rglob("*") if p.is_file()})
 return root/"electron.exe"
def prepare(progress=None):
 notify=progress or (lambda phase,**fields: None)
 notify("source_fingerprint")
 if Path(r"C:\ProgramData\QTrade").exists():raise RuntimeError("Formal installation root unexpectedly exists")
 before=manifest(REPO);rev=fingerprint(before);base=BUILD/rev;release=UNC/(LINUX.lstrip("/")+"/"+rev)
 print("SOURCE_VERSION="+rev,flush=True)
 if (base/"ready.json").exists():
  notify("verify_cached_artifacts",revision=rev)
  plan=json.loads((base/"ready.json").read_text());verify(base,json.loads((base/"artifacts.json").read_text()));verify(release/"source",before)
  verify(release,json.loads((release/"runtime-artifacts.json").read_text()))
  stock_electron("33.4.11")
  if manifest(REPO)!=before:raise RuntimeError("Source changed during verification")
  save(BUILD/"prepared.json",plan);return plan
 if base.exists() or release.exists():raise RuntimeError("Incomplete candidate exists; inspect before rebuilding: "+rev)
 notify("snapshot_current_source",revision=rev)
 base.mkdir(parents=True);release.mkdir(parents=True);save(release/"source-manifest.json",before)
 for name in before:
  for root in (base/"source",release/"source"):
   dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(REPO/name,dest)
 verify(base/"source",before);verify(release/"source",before)
 notify("frontend_and_sts_regression",build_log=str(release/"backend-gates.log"))
 command(["wsl.exe","-d","Ubuntu-24.04","--exec","/home/anlin/.venvs/qtrade/bin/python","-B",LINUX+"/"+rev+"/source/scripts/local_e2e/guest.py","build","--release",LINUX+"/"+rev],base/"frontend.log",timeout=1000)
 lock=json.loads((base/"source/console/package-lock.json").read_text())
 electron=stock_electron(lock["packages"]["node_modules/electron"]["version"])
 native=base/"native";native_src=base/"source/winagent"
 current={str(p.relative_to(native_src)):digest(p) for folder in (native_src/"src",native_src/"build") for p in folder.rglob("*") if p.is_file() and p.suffix in (".py",".spec",".sql",".txt")}
 save(base/"native-inputs.json",current)
 dist=base/"native-build/dist"
 for kind in ("svc","user"):
  notify("build_native_"+kind,build_log=str(base/("build-"+kind+".log")))
  command([PY,"-B","-m","PyInstaller","--noconfirm","--distpath",dist,"--workpath",base/"native-build/work"/kind,native_src/"build"/("qtrade-winagent-"+kind+".spec")],base/("build-"+kind+".log"),cwd=native_src)
 notify("native_selfcheck")
 merge_native([dist/"qtrade-winagent-svc",dist/"qtrade-winagent-user"],native)
 for kind in ("svc","user"):command([native/("qtrade-winagent-"+kind+".exe"),"--selfcheck"],base/("selfcheck-"+kind+".log"),timeout=30)
 app=base/"desktop";app.mkdir();shutil.copy2(base/"source/console/package.json",app/"package.json");shutil.copytree(release/"source/console/dist",app/"dist")
 assets={str(p.relative_to(base)):digest(p) for folder in (native,app,base/"source") for p in folder.rglob("*") if p.is_file()}
 verify(REPO,before)
 if manifest(REPO)!=before:raise RuntimeError("Source changed during build; candidate must not be deployed")
 plan={"revision":rev,"repo":str(REPO),"base":str(base),"linux_release":LINUX+"/"+rev,"native":str(native),"desktop":str(app),"electron":str(electron),"source_count":len(before),"source":str(base/"source")}
 save(base/"artifacts.json",assets);save(base/"source-manifest.json",before);save(base/"ready.json",plan);save(BUILD/"prepared.json",plan)
 print("BUILD_VERIFIED",flush=True);return plan
if __name__=="__main__":
 try:print(json.dumps(prepare()))
 except Exception as exc:print(type(exc).__name__+": "+str(exc),file=sys.stderr);sys.exit(1)
