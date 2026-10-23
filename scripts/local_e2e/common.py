"""Version gates for this workstation launcher; no OS or account side effects."""
from __future__ import annotations
import hashlib,json,subprocess
from pathlib import Path
PREFIXES=("src/","tests/","winagent/","console/","scripts/local_e2e/","installer/","docs/","data/")
ROOT_FILES={"pyproject.toml","winagent/pyproject.toml","README.md","AGENTS.md"}
SKIP_PARTS={"node_modules","dist","release","__pycache__",".pytest_cache",".venv",".venv-build",".pw-results","test-results","out"}
def digest(path: Path) -> str:
 with path.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()
def selected(name: str) -> bool:
 p=Path(name)
 return (name in ROOT_FILES or name.startswith(PREFIXES)) and not any(x in SKIP_PARTS for x in p.parts) and not p.name.startswith(".env") and p.suffix not in {".token",".pem",".key",".pyc",".exe",".log",".zip",".dll",".sfx",".tar",".7z"}
def manifest(repo: Path) -> dict[str,str]:
 raw=subprocess.check_output(["git","-C",str(repo),"ls-files","-z","--cached","--others","--exclude-standard"],timeout=30)
 names=sorted({n for n in raw.decode("utf-8").split("\0") if n and selected(n)})
 result={}
 for name in names:
  p=repo/name
  if p.is_symlink():raise ValueError("Source symlink requires explicit review: "+name)
  if p.is_file():result[name]=digest(p)
 required={"src/qtrade_agent/ossbox/sts.py","console/src/pages/mail/OssboxConfig.vue","console/electron/main/winagent-pipe.ts"}
 if not required.issubset(result):raise ValueError("Required STS/authentication sources missing")
 return result
def fingerprint(value: dict[str,str]) -> str:
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def verify(root: Path, expected: dict[str,str]) -> None:
 bad=[name for name,sha in expected.items() if not (root/name).is_file() or digest(root/name)!=sha]
 if bad:raise ValueError("Version gate failed: "+", ".join(bad[:10]))
def save(path: Path, value) -> None:
 path.parent.mkdir(parents=True,exist_ok=True)
 temp=path.with_suffix(path.suffix+".new")
 temp.write_text(json.dumps(value,ensure_ascii=True,indent=2)+"\n",encoding="utf-8")
 temp.replace(path)
