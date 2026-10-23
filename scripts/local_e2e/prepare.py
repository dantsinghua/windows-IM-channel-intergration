"""Build an immutable release; all workstation locations come from LocalContext."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from common import digest, fingerprint, manifest, save, verify
from context import LocalContext, load_context

REPO = Path(__file__).resolve().parents[2]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def command(cmd, log: Path, cwd: Path | None = None, timeout: int = 900) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("wb") as output:
            result = subprocess.run([str(arg) for arg in cmd], cwd=cwd, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError("BUILD_TOOL_MISSING: " + str(cmd[0])) from None
    except PermissionError:
        raise RuntimeError("BUILD_TOOL_ACCESS_DENIED: " + str(cmd[0])) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("BUILD_STEP_TIMEOUT: " + str(log)) from None
    if result.returncode:
        raise RuntimeError(f"BUILD_STEP_FAILED: exit={result.returncode}; log={log}")

def merge_native(sources: list[Path], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for source in sources:
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(path, destination)
                continue
            if digest(path) == digest(destination):
                continue
            if path.suffix != ".zip":
                raise RuntimeError("NATIVE_PAYLOAD_CONFLICT: " + str(relative))
            with zipfile.ZipFile(path) as left_zip, zipfile.ZipFile(destination) as right_zip:
                left = {name: left_zip.read(name) for name in left_zip.namelist()}
                right = {name: right_zip.read(name) for name in right_zip.namelist()}
            if any(left[name] != right[name] for name in left.keys() & right.keys()):
                raise RuntimeError("NATIVE_LIBRARY_CONFLICT: " + str(relative))
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
                for name, data in sorted((right | left).items()):
                    output.writestr(name, data)

def stock_electron(version: str, context: LocalContext) -> Path:
    if version != "33.4.11":
        raise RuntimeError("ELECTRON_VERSION_UNREVIEWED: update the pinned archive contract")
    build = Path(context.build_root)
    root = build / ("electron-" + version)
    record = build / ("electron-" + version + ".json")
    if record.exists():
        verify(root, json.loads(record.read_text(encoding="utf-8")))
        return root / "electron.exe"
    archive = Path(context.electron_archive)
    if not archive.is_file():
        raise RuntimeError("ELECTRON_ARCHIVE_MISSING: provide electron_archive in the local context")
    if digest(archive) != "f64c8a5a81d9b420b636fdba13e180f49d69f2198e1d86a8b01f858b17a9483c":
        raise RuntimeError("ELECTRON_ARCHIVE_HASH_MISMATCH")
    if root.exists():
        raise RuntimeError("ELECTRON_EXTRACTION_INCOMPLETE: inspect " + str(root))
    root.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        for name in zipped.namelist():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("ELECTRON_ARCHIVE_UNSAFE_PATH")
        zipped.extractall(root)
    save(record, {str(path.relative_to(root)): digest(path) for path in root.rglob("*") if path.is_file()})
    return root / "electron.exe"

def prepare(progress=None, context: LocalContext | None = None) -> dict:
    context = context or load_context()
    notify = progress or (lambda phase, **fields: None)
    notify("source_fingerprint")
    program_data = os.environ.get("ProgramData")
    if program_data and (Path(program_data) / "QTrade").exists():
        raise RuntimeError("FORMAL_INSTALLATION_CONFLICT: use the installed launcher, not the development deployer")
    before = manifest(REPO)
    revision = fingerprint(before)
    build = Path(context.build_root)
    candidate_id = revision + "-" + context.identity[:12]
    base = build / candidate_id
    linux_release = context.linux_builds + "/" + candidate_id
    release = context.guest_path(linux_release)
    print("SOURCE_VERSION=" + revision, flush=True)
    if (base / "ready.json").exists():
        notify("verify_cached_artifacts", revision=revision)
        plan = json.loads((base / "ready.json").read_text(encoding="utf-8"))
        if plan.get("context") != context.to_dict():
            raise RuntimeError("BUILD_CONTEXT_MISMATCH")
        verify(base, json.loads((base / "artifacts.json").read_text(encoding="utf-8")))
        verify(release / "source", before)
        verify(release, json.loads((release / "runtime-artifacts.json").read_text(encoding="utf-8")))
        stock_electron("33.4.11", context)
        if manifest(REPO) != before:
            raise RuntimeError("SOURCE_CHANGED_DURING_VERIFICATION")
        save(build / "prepared.json", plan)
        return plan
    if base.exists() or release.exists():
        raise RuntimeError("BUILD_CANDIDATE_INCOMPLETE: inspect " + candidate_id)
    notify("snapshot_current_source", revision=revision)
    base.mkdir(parents=True)
    release.mkdir(parents=True)
    save(release / "source-manifest.json", before)
    save(release / "context.json", context.to_dict())
    save(base / "context.json", context.to_dict())
    for name in before:
        for root in (base / "source", release / "source"):
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, destination)
    verify(base / "source", before)
    verify(release / "source", before)
    notify("frontend_and_sts_regression", build_log=str(release / "backend-gates.log"))
    guest = linux_release + "/source/scripts/local_e2e/guest.py"
    command(context.wsl_command(guest, "build", "--release", linux_release,
                                "--context", linux_release + "/context.json"), base / "frontend.log", timeout=1000)
    lock = json.loads((base / "source/console/package-lock.json").read_text(encoding="utf-8"))
    electron = stock_electron(lock["packages"]["node_modules/electron"]["version"], context)
    native = base / "native"
    native_source = base / "source/winagent"
    inputs = {str(path.relative_to(native_source)): digest(path)
              for folder in (native_source / "src", native_source / "build")
              for path in folder.rglob("*") if path.is_file() and path.suffix in (".py", ".spec", ".sql", ".txt")}
    save(base / "native-inputs.json", inputs)
    dist = base / "native-build/dist"
    for kind in ("svc", "user"):
        notify("build_native_" + kind, build_log=str(base / ("build-" + kind + ".log")))
        command([context.windows_python, "-B", "-m", "PyInstaller", "--noconfirm", "--distpath", dist,
                 "--workpath", base / "native-build/work" / kind, native_source / "build" / ("qtrade-winagent-" + kind + ".spec")],
                base / ("build-" + kind + ".log"), cwd=native_source)
    notify("native_selfcheck")
    merge_native([dist / "qtrade-winagent-svc", dist / "qtrade-winagent-user"], native)
    for kind in ("svc", "user"):
        command([native / ("qtrade-winagent-" + kind + ".exe"), "--selfcheck"],
                base / ("selfcheck-" + kind + ".log"), timeout=30)
    app = base / "desktop"
    app.mkdir()
    shutil.copy2(base / "source/console/package.json", app / "package.json")
    shutil.copytree(release / "source/console/dist", app / "dist")
    assets = {str(path.relative_to(base)): digest(path) for folder in (native, app, base / "source")
              for path in folder.rglob("*") if path.is_file()}
    assets["context.json"] = digest(base / "context.json")
    verify(REPO, before)
    if manifest(REPO) != before:
        raise RuntimeError("SOURCE_CHANGED_DURING_BUILD")
    plan = {"revision": revision, "repo": str(REPO), "base": str(base), "linux_release": linux_release,
            "native": str(native), "desktop": str(app), "electron": str(electron),
            "source_count": len(before), "source": str(base / "source"), "context": context.to_dict()}
    save(base / "artifacts.json", assets)
    save(base / "source-manifest.json", before)
    save(base / "ready.json", plan)
    save(build / "prepared.json", plan)
    print("BUILD_VERIFIED", flush=True)
    return plan

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--context")
    options = parser.parse_args()
    try:
        print(json.dumps(prepare(context=load_context(options.context))))
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        sys.exit(1)
