"""企点控件树来源合同：真实本机 shell 仿真，不连接任何 ADB 设备。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sys

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi, load_profiles
from qtrade_agent.runtime.backends import AdbShellResult
from tests.test_qidian_ui import PKG, UiFakeAdb, acct, node, tree


class LocalDumpAdb(UiFakeAdb):
    """仅将 Android 临时文件落点映射到 tmp_path，其余 dump shell 原样执行。

    fake uiautomator 复现 exit 0 不产文件及并发交错；并不为产品补充 freshness 判断。
    """

    def __init__(self, root: Path, *, mode="write", old_xml=None):
        super().__init__()
        self.root = root
        self.mode = mode
        self.fg = f"{PKG}/com.tencent.mobileqq.activity.SplashActivity"
        self.databases_ls = "-rw- 1 u0 u0 4096 1758240000 9000000123.db\n"
        self.remote = root / "remote"
        self.remote.mkdir()
        self.old = self.remote / "qtrade-ui.xml"
        self.old.write_text(old_xml or tree(node(text="OLD_TREE")))
        bindir = root / "bin"
        bindir.mkdir()
        executable = bindir / "uiautomator"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, sys, time\n"
            "mode = os.environ['FAKE_DUMP_MODE']\n"
            "if mode == 'no_output':\n"
            "    print('ERROR: could not get idle state.', file=sys.stderr)\n"
            "    sys.exit(0)\n"
            "label = os.environ['FAKE_DUMP_LABEL']\n"
            "pathlib.Path(sys.argv[-1]).write_text('<hierarchy><node bounds=\"[0,0][100,100]\" text=\"' + label + '\" /></hierarchy>')\n"
            "if mode == 'concurrent':\n"
            "    root = pathlib.Path(os.environ['FAKE_DUMP_ROOT'])\n"
            "    (root / (label + '.ready')).touch()\n"
            "    deadline = time.monotonic() + 2\n"
            "    while len(list(root.glob('*.ready'))) < 2:\n"
            "        if time.monotonic() > deadline: sys.exit(4)\n"
            "        time.sleep(0.005)\n"
        )
        executable.chmod(0o700)
        self.env = {**os.environ, "PATH": str(bindir) + ":" + os.environ["PATH"],
                    "FAKE_DUMP_MODE": mode, "FAKE_DUMP_ROOT": str(root)}

    async def _execute_dump(self, cmd):
        self.cmds.append(cmd)
        env = {**self.env, "FAKE_DUMP_LABEL": asyncio.current_task().get_name()}
        mapped = cmd.replace("/data/local/tmp/", str(self.remote) + "/")
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", mapped, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        return AdbShellResult(proc.returncode, stdout.decode())

    async def shell(self, serial, cmd):
        if "uiautomator dump" in cmd:
            return (await self._execute_dump(cmd)).output
        return await super().shell(serial, cmd)

    async def shell_result(self, serial, cmd, *, timeout_s):
        if "uiautomator dump" in cmd:
            return await self._execute_dump(cmd)
        return await super().shell_result(serial, cmd, timeout_s=timeout_s)


def make_dump_ui(adb):
    p = load_profiles()["default"]
    p = replace(p, timeouts={**p.timeouts, "dump_retries": 1, "ui_settle_s": 0})
    return QidianUi(adb=adb, profiles={"default": p}), p


async def test_dump_exit_zero_without_new_output_cannot_return_preexisting_xml(tmp_path):
    adb = LocalDumpAdb(tmp_path, mode="no_output")
    ui, p = make_dump_ui(adb)

    with pytest.raises(ValueError):
        await ui._dump(acct(), p)

    assert "OLD_TREE" in adb.old.read_text()
    assert adb.taps == []
    assert adb.typed == []


async def test_overlapping_dump_calls_each_return_their_own_tree(tmp_path):
    adb = LocalDumpAdb(tmp_path, mode="concurrent")
    ui, p = make_dump_ui(adb)

    first = asyncio.create_task(ui._dump(acct(), p), name="CALL_A")
    second = asyncio.create_task(ui._dump(acct(), p), name="CALL_B")
    a, b = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert [n.text for n in a] == ["CALL_A"]
    assert [n.text for n in b] == ["CALL_B"]
    assert "OLD_TREE" in adb.old.read_text()


async def test_fresh_tree_still_available_to_existing_shell_only_consumer(tmp_path):
    adb = LocalDumpAdb(tmp_path)
    ui, p = make_dump_ui(adb)

    result = await asyncio.create_task(ui._dump(acct(), p), name="FRESH_TREE")

    assert [n.text for n in result] == ["FRESH_TREE"]
    assert adb.taps == []


async def test_readonly_login_probe_cannot_accept_old_main_tree_when_dump_produces_nothing(tmp_path):
    old_main = tree(node(rid=f"{PKG}:id/recent_chat_list"))
    adb = LocalDumpAdb(tmp_path, mode="no_output", old_xml=old_main)
    ui, _p = make_dump_ui(adb)

    outcome = await ui.probe_login(acct())

    assert outcome.result != "running"
    assert adb.taps == []
    assert adb.typed == []
    assert not any(c.startswith(("am start", "am force-stop", "ime set")) for c in adb.cmds)
    assert adb.old.read_text() == old_main
