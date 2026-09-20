"""wslctl:``.wslconfig`` 写入规则 R1/R2/R3、十一键集、内核三判据、🔴 NOSHUTDOWN 红线(04 §2.7 / 03 §2.6 / 00 §11.6)。"""
from __future__ import annotations

import os

import pytest

from qtrade_winagent.config import WslConfig
from qtrade_winagent.errors import WaError
from qtrade_winagent.fakes import FakeWsl
from qtrade_winagent.wslctl import (BACKUP_SUFFIX_FMT, NEVER_TOUCH, WSLCONFIG_KEYS, WslConfigFile, WslCtl, esc_path)


def mk(tmp_path, text="[wsl2]\nmemory=6GB\n"):
    w = FakeWsl(wslconfig_text=text)
    return w, WslCtl(w, WslConfig(), backup_dir=str(tmp_path / "wslbak"))


def test_key_set_is_the_eleven_keys_from_04():
    assert len(WSLCONFIG_KEYS) == 11
    assert set(WSLCONFIG_KEYS) == {"kernel", "memory", "processors", "swap", "localhostForwarding",
                                   "autoMemoryReclaim", "sparseVhd", "vmIdleTimeout", "guiApplications",
                                   "maxCrashDumpCount", "crashDumpFolder"}
    assert WSLCONFIG_KEYS["memory"][1] == "ours"                     # R3:唯一「改用户已有值」的键
    assert WSLCONFIG_KEYS["maxCrashDumpCount"][1] == "fill_missing"  # N-7:缺失才写,已有保留
    assert set(NEVER_TOUCH) >= {"networkingMode", "dnsTunneling", "autoProxy", "firewall"}


def test_parser_keeps_comments_and_unknown_keys(tmp_path):
    text = "# 顶部注释\n[wsl2]\nmemory=6GB\nnetworkingMode=mirrored\n\n[experimental]\nfoo=bar\n"
    f = WslConfigFile.parse(text)
    f.set("memory", "11GB", "wsl2")
    out = f.text()
    assert "# 顶部注释" in out and "networkingMode=mirrored" in out and "foo=bar" in out
    assert "memory=11GB" in out and "memory=6GB" not in out


def test_r3_only_memory_overwrites_existing_user_value(tmp_path):
    w, c = mk(tmp_path, "[wsl2]\nmemory=6GB\nswap=8GB\nguiApplications=true\n")
    out = c.config_put({"memory": "11GB", "swap": "2GB", "guiApplications": "false"})
    assert out["changed"] == {"memory": {"from": "6GB", "to": "11GB"}}
    assert out["kept"] == {"swap": "8GB", "guiApplications": "true"}   # 其余键用户已有值保留、只提示
    assert "swap=8GB" in w.wslconfig_text and "guiApplications=true" in w.wslconfig_text


def test_r2_fill_missing_writes_defaults(tmp_path):
    w, c = mk(tmp_path, "[wsl2]\nmemory=11GB\n")
    out = c.config_put({"swap": "2GB", "localhostForwarding": "true", "autoMemoryReclaim": "gradual"})
    assert set(out["changed"]) == {"swap", "localhostForwarding", "autoMemoryReclaim"}
    assert "[experimental]" in w.wslconfig_text and "autoMemoryReclaim=gradual" in w.wslconfig_text


def test_crash_dump_keys_follow_n7(tmp_path):
    w, c = mk(tmp_path, "[wsl2]\nmemory=11GB\nmaxCrashDumpCount=10\ncrashDumpFolder=C:\\\\Temp\\\\d\n")
    out = c.config_put({"maxCrashDumpCount": "2", "crashDumpFolder": "D:\\\\QTrade\\\\wsl-crashes"})
    assert out["changed"] == {}                                        # 已有用户值一律保留不改
    assert out["kept"]["maxCrashDumpCount"] == "10"
    assert any("建议值 2" in wmsg for wmsg in out["warnings"])
    assert any("系统盘" in wmsg for wmsg in out["warnings"])


def test_never_key_is_ignored(tmp_path):
    _w, c = mk(tmp_path)
    out = c.config_put({"vmIdleTimeout": "60"})
    assert out["changed"] == {} and any("不写不动" in x for x in out["warnings"])


def test_unknown_key_is_rejected(tmp_path):
    _w, c = mk(tmp_path)
    with pytest.raises(WaError) as e:
        c.config_put({"networkingMode": "mirrored"})
    assert e.value.reason == "unknown_key"


def test_allow_keys_whitelist_for_endpoint_25(tmp_path):
    _w, c = mk(tmp_path)
    with pytest.raises(WaError) as e:
        c.config_put({"kernel": "x"}, allow_keys=("memory", "processors", "autoMemoryReclaim", "swap"))
    assert e.value.reason == "key_not_allowed"


def test_r1_backup_created_with_bak_timestamp_suffix(tmp_path):
    _w, c = mk(tmp_path, "[wsl2]\nmemory=6GB\n")
    out = c.config_put({"memory": "11GB"})
    assert out["backup"] and os.path.basename(out["backup"]).startswith(".wslconfig.bak-")   # R6-14 后缀
    assert len(os.path.basename(out["backup"])) == len(".wslconfig.bak-20260920-184000")


def test_backup_keep_limit(tmp_path):
    import time
    w = FakeWsl(wslconfig_text="[wsl2]\nmemory=1GB\n")
    stamps = iter(range(1_700_000_000_000, 1_700_000_000_000 + 20_000_000, 1_000_000))
    c = WslCtl(w, WslConfig(backup_keep=3), backup_dir=str(tmp_path / "b"), clock=lambda: next(stamps))
    for i in range(6):
        c.config_put({"memory": f"{i + 2}GB"})
    assert len(c.list_backups()) == 3


def test_idempotent_no_write_no_backup(tmp_path):
    _w, c = mk(tmp_path, "[wsl2]\nmemory=11GB\n")
    out = c.config_put({"memory": "11GB"})
    assert out["changed"] == {} and out["backup"] is None and c.list_backups() == []


def test_pending_restart_set_and_never_auto_shutdown(tmp_path):
    w, c = mk(tmp_path, "[wsl2]\nmemory=6GB\n")
    out = c.config_put({"memory": "11GB"})
    assert out["pending_restart"] is True
    assert w.shutdown_calls == 0 and w.terminate_calls == []          # 🔴 改完绝不自动 shutdown(00 §11.6)


async def test_stop_only_terminates_our_distro(tmp_path):
    w, c = mk(tmp_path)
    await c.stop()
    assert w.terminate_calls == ["qtrade"] and w.shutdown_calls == 0


async def test_restart_shutdown_requires_confirm(tmp_path):
    w, c = mk(tmp_path)
    with pytest.raises(WaError) as e:
        await c.restart(mode="shutdown")
    assert e.value.reason == "shutdown_not_confirmed" and e.value.needs_human is True
    assert w.shutdown_calls == 0
    await c.restart(mode="shutdown", confirm=True)
    assert w.shutdown_calls == 1


async def test_restart_terminate_does_not_shutdown(tmp_path):
    w, c = mk(tmp_path)
    await c.restart(mode="terminate")
    assert w.terminate_calls == ["qtrade"] and w.shutdown_calls == 0


async def test_restart_bad_mode(tmp_path):
    _w, c = mk(tmp_path)
    with pytest.raises(WaError) as e:
        await c.restart(mode="reboot")
    assert e.value.reason == "bad_mode"


async def test_kernel_verify_needs_all_three_criteria(tmp_path):
    w, c = mk(tmp_path)
    assert (await c.kernel_verify()).ok is True
    w.uname = "6.6.87.2-generic"                                       # ① 不含 binder 标识
    v = await c.kernel_verify()
    assert v.ok is False and v.binder_fs is True and v.binderfs_mount is True
    w.uname = "6.6.87.2-binder+"
    w.filesystems = "nodev\ttmpfs\n"                                   # ② /proc/filesystems 没有 binder
    assert (await c.kernel_verify()).binder_fs is False
    w.filesystems = "nodev\tbinder\n"
    w.binderfs_mountable = False                                       # ③ 试挂失败
    v3 = await c.kernel_verify()
    assert v3.ok is False and v3.binderfs_mount is False


async def test_kernel_apply_half_requires_confirm_shutdown(tmp_path):
    w, c = mk(tmp_path)
    with pytest.raises(WaError) as e:
        await c.kernel_write_and_restart(kernel_path="C:\\k\\bzImage", confirm_shutdown=False)
    assert e.value.reason == "shutdown_not_confirmed" and e.value.stage == "user"
    assert w.shutdown_calls == 0


async def test_kernel_apply_writes_escaped_path_and_verifies(tmp_path):
    w, c = mk(tmp_path)
    out = await c.kernel_write_and_restart(kernel_path="C:\\ProgramData\\QTrade\\kernel\\bzImage",
                                           confirm_shutdown=True)
    assert out["verify"]["ok"] is True and "wsl.shutdown" in out["partial"]
    assert "kernel=C:\\\\ProgramData\\\\QTrade\\\\kernel\\\\bzImage" in w.wslconfig_text    # 反斜杠转义
    assert esc_path("C:\\a\\b") == "C:\\\\a\\\\b" and esc_path("C:\\\\a") == "C:\\\\a"


async def test_kernel_rollback_clears_kernel_line(tmp_path):
    w, c = mk(tmp_path, "[wsl2]\nmemory=11GB\nkernel=C:\\\\x\\\\bzImage\n")
    out = await c.kernel_write_and_restart(kernel_path=None, confirm_shutdown=True)
    assert "kernel=" not in w.wslconfig_text and "wslconfig.kernel.cleared" in out["partial"]


async def test_distro_repair_requires_confirm_and_keeps_data(tmp_path):
    w, c = mk(tmp_path)
    with pytest.raises(WaError) as e:
        await c.distro_repair(confirm=False, rootfs="r.tar", backup_tar="b.tar")
    assert e.value.reason == "not_confirmed"
    out = await c.distro_repair(confirm=True, rootfs="r.tar", backup_tar="b.tar")
    assert out["partial"] == ["terminate", "export:b.tar", "unregister", "import", "start", "restore"]
    assert w.shutdown_calls == 0                                       # 修复发行版也不 --shutdown


async def test_status_reports_kernel_state(tmp_path):
    _w, c = mk(tmp_path, "[wsl2]\nmemory=11GB\nkernel=C:\\\\ProgramData\\\\QTrade\\\\kernel\\\\bzImage\n")
    st = await c.status()
    assert st["kernel_state"] == "OURS" and st["wslconfig"]["memory"] == "11GB"
    _w2, c2 = mk(tmp_path, "[wsl2]\nkernel=D:\\\\mine\\\\bzImage\n")
    assert (await c2.status())["kernel_state"] == "OTHER_CUSTOM"
    _w3, c3 = mk(tmp_path, "[wsl2]\n")
    assert (await c3.status())["kernel_state"] == "DEFAULT"
