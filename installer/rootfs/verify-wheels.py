#!/usr/bin/env python3
"""构建期断言:发行版 venv 里每个运行期依赖装上的,必须是 wheels.expected 里登记的那一个 wheel 文件(E4-1)。

为什么需要(--require-hashes + --only-binary 还不够):
  锁(uv pip compile --generate-hashes)为每个包登记了该版本在 PyPI 上**全部**文件的 hash。
  同一包在目标平台若有 ≥2 个可用 wheel(websockets 17.1:cp312 manylinux 带 C 加速 /
  py3-none-any 纯 Python),pip 先按「hash 在锁里」过滤候选、再挑最优 ——
  被选中那个的 hash 对不上时,它被**静默滤掉**,pip 改装另一个同样在锁里的 wheel,
  rc=0、pip freeze 与锁逐行一致,谁也看不出。所以「hash 对不上即构建失败」必须由
  「实际装的是预期的那一个文件」来兜底 —— 这就是本脚本。它按「类」设防,不针对某一个包。

子命令:
  check  构建期用(Dockerfile 装完依赖后紧接着跑),任一不符 ⇒ exit 1 让 docker build 失败:
           ① 清单自洽:清单包集合 = 锁包集合,每行的版本 = 锁版本、sha256 ∈ 锁里该包的 hash;
           ② pip --report 逐项:实际装的包集合 = 清单;下载文件名 = 清单文件名;文件 sha256 = 清单 sha256;
           ③ site-packages 逐包(不依赖 pip 的报告,独立一路):dist-info 集合 = 清单 ∪ {pip};
              版本 = 清单;`WHEEL` 的 Tag 集合 = 清单文件名展开的 tag 集合;RECORD 存在;
           ④ pip 自身版本 = pip-bootstrap.lock 钉的版本(E3-O1)。
  emit   再生成清单用(见 README §8「wheels.expected」):读一次 `pip install --dry-run --report`
         的输出,核对每项 sha256 ∈ 锁后,按清单格式打印到 stdout。

只用标准库 + pip 自带的 vendored packaging(构建时 venv 里一定有 pip)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename

REQ_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def die(msg: str) -> None:
    print(f"verify-wheels: FAIL: {msg}", file=sys.stderr)


def parse_lock(path: str) -> dict[str, tuple[str, set[str]]]:
    """requirements 格式(带 --hash 续行)⇒ {规范名: (版本, {sha256…})}。"""
    out: dict[str, tuple[str, set[str]]] = {}
    cur = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = REQ_RE.match(line)
        if m:
            cur = canonicalize_name(m.group(1))
            if cur in out:
                raise SystemExit(f"verify-wheels: FAIL: {path} 里 {cur} 出现两次")
            out[cur] = (m.group(2), set())
        for h in HASH_RE.findall(line):
            if cur is None:
                raise SystemExit(f"verify-wheels: FAIL: {path} 有不属于任何包的 hash 行")
            out[cur][1].add(h)
    return out


def parse_manifest(path: str) -> dict[str, tuple[str, str, str]]:
    """wheels.expected ⇒ {规范名: (版本, 文件名, sha256)}。格式:`名 版本 文件名 sha256`,# 开头为注释。"""
    out: dict[str, tuple[str, str, str]] = {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 4:
            raise SystemExit(f"verify-wheels: FAIL: {path}:{n} 须为 4 列(名 版本 文件名 sha256):{line!r}")
        name, ver, fname, sha = parts
        key = canonicalize_name(name)
        if key in out:
            raise SystemExit(f"verify-wheels: FAIL: {path}:{n} {key} 重复")
        out[key] = (ver, fname, sha)
    return out


def wheel_tags(fname: str) -> tuple[str, str, set[str]]:
    """文件名 ⇒ (规范名, 版本, 展开后的 tag 集合)。压缩 tag(a.b.c)按 PEP 425 展开。"""
    name, ver, _build, tags = parse_wheel_filename(fname)
    return canonicalize_name(name), str(ver), {str(t) for t in tags}


def cmd_check(a: argparse.Namespace) -> int:
    bad = 0
    lock = parse_lock(a.lock)
    man = parse_manifest(a.manifest)

    # ── ① 清单自洽(防「空规则」:清单为空、少包、多包都算失败)──
    if not man:
        die("清单为空"); bad += 1
    if set(man) != set(lock):
        die(f"清单包集合 ≠ 锁包集合:仅清单 {sorted(set(man) - set(lock))} 仅锁 {sorted(set(lock) - set(man))}")
        bad += 1
    for k, (ver, fname, sha) in sorted(man.items()):
        if not fname.endswith(".whl"):
            die(f"{k}: 清单文件名不是 wheel:{fname}"); bad += 1; continue
        fn_name, fn_ver, _ = wheel_tags(fname)
        if fn_name != k or fn_ver != ver:
            die(f"{k}: 清单文件名 {fname} 与名/版本 {k}=={ver} 不符"); bad += 1
        if k in lock:
            lver, lhashes = lock[k]
            if lver != ver:
                die(f"{k}: 清单版本 {ver} ≠ 锁版本 {lver}"); bad += 1
            if sha not in lhashes:
                die(f"{k}: 清单 sha256 {sha[:12]}… 不在锁里该包的 {len(lhashes)} 条 hash 中"); bad += 1

    # ── ② pip --report:实际下载/安装的是哪个文件 ──
    rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
    got: dict[str, tuple[str, str, str]] = {}
    for item in rep.get("install", []):
        md = item.get("metadata", {})
        k = canonicalize_name(md.get("name", "?"))
        di = item.get("download_info", {})
        fname = unquote(Path(urlparse(di.get("url", "")).path).name)
        hashes = di.get("archive_info", {}).get("hashes", {}) or {}
        sha = hashes.get("sha256", "")
        if not sha:
            h = di.get("archive_info", {}).get("hash", "")
            sha = h.split("=", 1)[1] if h.startswith("sha256=") else ""
        got[k] = (str(md.get("version", "?")), fname, sha)
    if set(got) != set(man):
        die(f"pip 实装包集合 ≠ 清单:仅实装 {sorted(set(got) - set(man))} 仅清单 {sorted(set(man) - set(got))}")
        bad += 1
    for k in sorted(set(got) & set(man)):
        ver, fname, sha = man[k]
        gver, gfname, gsha = got[k]
        if (gver, gfname) != (ver, fname):
            die(f"{k}: pip 实装 {gfname}({gver}),清单要的是 {fname}({ver})"); bad += 1
        if gsha != sha:
            die(f"{k}: pip 实装文件 sha256 {gsha[:12] or '(无)'}… ≠ 清单 {sha[:12]}…"); bad += 1

    # ── ③ site-packages 逐包:dist-info 的 WHEEL Tag / 版本 / RECORD(独立于 pip 报告)──
    sp = a.site_packages or sysconfig.get_paths()["purelib"]
    dists = {canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions(path=[sp])}
    allowed = set(man) | {"pip"}
    if set(dists) != allowed:
        die(f"site-packages 的 dist-info 集合 ≠ 清单 ∪ {{pip}}:多出 {sorted(set(dists) - allowed)} 缺 {sorted(allowed - set(dists))}")
        bad += 1
    for k in sorted(set(dists) & set(man)):
        ver, fname, _sha = man[k]
        d = dists[k]
        if d.version != ver:
            die(f"{k}: 已装版本 {d.version} ≠ 清单 {ver}"); bad += 1
        wheel_txt = d.read_text("WHEEL") or ""
        inst = {ln.split(":", 1)[1].strip() for ln in wheel_txt.splitlines() if ln.startswith("Tag:")}
        _, _, want = wheel_tags(fname)
        if inst != want:
            die(f"{k}: 已装 WHEEL Tag {sorted(inst)} ≠ 清单文件名的 tag {sorted(want)}"); bad += 1
        if not d.read_text("RECORD"):
            die(f"{k}: dist-info 缺 RECORD"); bad += 1

    # ── ④ pip 自身版本 = pip-bootstrap.lock 钉的版本(E3-O1)──
    if a.pip_lock:
        pl = parse_lock(a.pip_lock)
        want_pip = pl.get("pip", ("(未钉)", set()))[0]
        have_pip = dists["pip"].version if "pip" in dists else "(未装)"
        if have_pip != want_pip:
            die(f"pip 版本 {have_pip} ≠ pip-bootstrap.lock 钉的 {want_pip}"); bad += 1

    if bad:
        print(f"verify-wheels: {bad} 处不符 ⇒ 构建失败", file=sys.stderr)
        return 1
    print(f"verify-wheels: OK —— {len(man)} 个运行期包实装 wheel 与清单逐包一致(文件名/sha256/WHEEL Tag/版本),"
          f"pip={dists['pip'].version if 'pip' in dists else '?'}")
    for k in sorted(man):
        print(f"    {man[k][1]}")
    return 0


def cmd_emit(a: argparse.Namespace) -> int:
    lock = parse_lock(a.lock)
    rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
    rows = []
    for item in rep.get("install", []):
        md = item.get("metadata", {})
        k = canonicalize_name(md["name"])
        di = item.get("download_info", {})
        fname = unquote(Path(urlparse(di.get("url", "")).path).name)
        sha = (di.get("archive_info", {}).get("hashes", {}) or {}).get("sha256", "")
        if k not in lock or sha not in lock[k][1] or lock[k][0] != str(md["version"]):
            raise SystemExit(f"verify-wheels: FAIL: emit:{k} {fname} 的版本/sha256 与锁对不上")
        if not fname.endswith(".whl"):
            raise SystemExit(f"verify-wheels: FAIL: emit:{k} 选中的不是 wheel:{fname}")
        rows.append((k, str(md["version"]), fname, sha))
    if {r[0] for r in rows} != set(lock):
        raise SystemExit("verify-wheels: FAIL: emit:报告的包集合 ≠ 锁包集合")
    print("# 发行版 venv 每个运行期依赖「应装上的那一个 wheel 文件」—— 由 verify-wheels.py emit 生成,勿手改")
    print("# 列:规范名 版本 wheel 文件名 sha256。再生成与何时要更新:见 README.md §8「wheels.expected」。")
    for r in sorted(rows):
        print(" ".join(r))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--report", required=True)
    c.add_argument("--manifest", required=True)
    c.add_argument("--lock", required=True)
    c.add_argument("--pip-lock")
    c.add_argument("--site-packages")
    e = sub.add_parser("emit")
    e.add_argument("--report", required=True)
    e.add_argument("--lock", required=True)
    a = ap.parse_args()
    return cmd_check(a) if a.cmd == "check" else cmd_emit(a)


if __name__ == "__main__":
    sys.exit(main())
