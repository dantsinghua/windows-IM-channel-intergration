"""Execute the installer smoke-test shell with an isolated fake Docker executable."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "rootfs/files/opt/qtrade/bin/qtrade-install-selftest.sh"
CID = "a" * 64


def run_selftest(tmp_path, mode):
    contents = tmp_path / "contents.json"
    contents.write_text(json.dumps({"redroid_image": {"ref": "offline.example/redroid:verified"}}))
    log = tmp_path / "docker.jsonl"
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''import json,os,sys
from pathlib import Path
a=sys.argv[1:]; root=Path(os.environ["FIXTURE_ROOT"]); mode=os.environ["FIXTURE_MODE"]
with (root/"docker.jsonl").open("a") as f: f.write(json.dumps(a)+"\\n")
cid="a"*64
if a[:2]==["container","inspect"]:
 if a[-1]=="qtrade-selftest": raise SystemExit(0 if mode=="existing" else 1)
 print("foreign-owner" if mode=="owner-changed" else (root/"owner").read_text()); raise SystemExit(0)
if a and a[0]=="create":
 assert "--pull=never" in a
 assert "offline.example/redroid:verified" in a
 (root/"owner").write_text(a[a.index("--label")+1].split("=",1)[1])
 print(cid); raise SystemExit(0)
if a and a[0]=="start": raise SystemExit(7 if mode=="start-failed" else 0)
if a and a[0]=="exec":
 if mode=="exec-failed": raise SystemExit(9)
 print("0" if mode=="timeout" else "1"); raise SystemExit(0)
if a and a[0]=="rm":
 assert a==["rm","-f","-v",cid]
 raise SystemExit(8 if mode=="cleanup-failed" else 0)
if a==["info"]: raise SystemExit(0)
raise SystemExit(99)
''')
    docker.chmod(0o755)
    source = SCRIPT.read_text().replace("/etc/qtrade/contents.json", str(contents)).replace(
        "/run/qtrade-install-selftest.lock", str(tmp_path / "selftest.lock"))
    script = tmp_path / "selftest.sh"
    script.write_text(source)
    env = {"PATH": str(tmp_path) + ":/usr/bin:/bin", "FIXTURE_ROOT": str(tmp_path), "FIXTURE_MODE": mode}
    result = subprocess.run(["bash", str(script), "1"], capture_output=True, text=True, env=env, timeout=10)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def test_selftest_reports_boot_only_after_its_exact_container_was_removed(tmp_path):
    result, calls = run_selftest(tmp_path, "ok")
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["ok"] is True
    assert isinstance(value["redroid_boot_ms"], int) and value["redroid_boot_ms"] >= 0
    created = next(call for call in calls if call[0] == "create")
    assert "--pull=never" in created
    assert "127.0.0.1:16099:5555" in created
    assert "--label" in created
    assert calls[-1] == ["rm", "-f", "-v", CID]
    assert all(call[0] != "pull" for call in calls)


def test_an_existing_selftest_name_is_not_adopted_or_deleted(tmp_path):
    result, calls = run_selftest(tmp_path, "existing")
    assert result.returncode != 0
    assert not any(call[0] in {"create", "start", "exec", "rm"} for call in calls)


@pytest.mark.parametrize("mode", ["timeout", "start-failed", "exec-failed", "cleanup-failed"])
def test_failure_never_becomes_a_success_receipt_and_cleanup_uses_only_the_created_id(tmp_path, mode):
    result, calls = run_selftest(tmp_path, mode)
    assert result.returncode != 0
    assert '"ok":true' not in result.stdout
    removed = [call for call in calls if call[0] == "rm"]
    assert removed
    assert all(call == ["rm", "-f", "-v", CID] for call in removed)


def test_cleanup_refuses_a_container_whose_ownership_label_changed(tmp_path):
    result, calls = run_selftest(tmp_path, "owner-changed")
    assert result.returncode != 0
    assert not any(call[0] == "rm" for call in calls)
    assert '"ok":true' not in result.stdout
