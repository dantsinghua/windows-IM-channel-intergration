"""护栏自测:证明 `tests/acceptance/conftest.py` 的宿主命令护栏**真的会红**、且被拦的命令**从未执行**。

做法:把本目录的 conftest 原样拷进一个临时目录,再放一组内层用例,用**当前解释器**(护栏白名单里唯一的可执行文件)
起一个内层 pytest 跑它们。`PATH` 前置一个临时 bin 目录,里面的 `adb` / `docker` / `wsl.exe` / `powershell.exe`
都是**只会写标记文件**的假脚本 —— 护栏一旦失守,标记文件就会出现(且绝不会碰到宿主真实的 adb)。

出处:派工单 D-3 第 3 条(`.omc/handoffs/e2e-rootfs-2.md` D-3 的隔离隐患);判据 = 内层每条违规用例都红、
失败信息指名是哪条用例与哪条命令、吞掉异常的也红、白名单内的 `python -c` 放行、零标记文件。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent

INNER = textwrap.dedent('''
    import asyncio, os, subprocess, sys, threading

    def test_sync_adb_is_blocked():
        subprocess.run(["adb", "-s", "127.0.0.1:16001", "shell", "echo", "x"])

    def test_swallowed_docker_still_red():
        try:
            subprocess.run(["docker", "ps"], capture_output=True)
        except Exception:
            pass                                   # 产品代码吞掉异常也不能蒙混过关

    def test_asyncio_wsl_is_blocked():
        async def go():
            try:
                p = await asyncio.create_subprocess_exec("wsl.exe", "-l")
                await p.wait()
            except Exception:
                pass
        asyncio.run(go())

    def test_os_system_powershell_is_blocked():
        try:
            os.system("powershell.exe -Command Get-Date")
        except Exception:
            pass

    def test_background_thread_is_blocked():
        def worker():
            try:
                subprocess.check_output(["adb", "devices"])
            except Exception:
                pass
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    def test_any_other_host_binary_is_blocked():
        try:
            subprocess.run(["/bin/true"])          # 白名单而非黑名单:连 true 都不放
        except Exception:
            pass

    def test_current_interpreter_is_allowed():
        subprocess.run([sys.executable, "-c", "pass"], check=True)
''')

FAKE = "#!/bin/sh\necho \"$0 $*\" >> \"{marker}\"\nexit 0\n"
BLOCKED = ["test_sync_adb_is_blocked", "test_swallowed_docker_still_red", "test_asyncio_wsl_is_blocked",
           "test_os_system_powershell_is_blocked", "test_background_thread_is_blocked",
           "test_any_other_host_binary_is_blocked"]


def _run_inner(tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
    inner = tmp_path / "inner"
    inner.mkdir()
    shutil.copy(HERE / "conftest.py", inner / "conftest.py")
    (inner / "test_inner.py").write_text(INNER, encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    marker = tmp_path / "EXECUTED.txt"
    for name in ("adb", "docker", "wsl.exe", "powershell.exe"):
        f = fakebin / name
        f.write_text(FAKE.format(marker=marker), encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}", PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider", "-o", "addopts=",
                        "--rootdir", str(inner), str(inner / "test_inner.py")],
                       capture_output=True, text=True, env=env, cwd=str(inner), timeout=120)
    return p, marker


def test_guard_fails_every_host_command_and_never_executes_it(tmp_path):
    p, marker = _run_inner(tmp_path)
    out = p.stdout + p.stderr
    assert not marker.exists(), f"护栏失守:假宿主命令真的被执行了:\n{marker.read_text() if marker.exists() else ''}\n{out}"
    assert p.returncode != 0, out
    for name in BLOCKED:
        assert f"FAILED test_inner.py::{name}" in out, f"{name} 应被护栏判红\n{out}"
        assert f"[验收护栏] test_inner.py::{name}" in out, f"{name} 的失败信息须指名用例\n{out}"
    assert "PASSED test_inner.py::test_current_interpreter_is_allowed" in out, out
    assert "'adb', '-s', '127.0.0.1:16001'" in out, "失败信息须列出被拦的 argv"
