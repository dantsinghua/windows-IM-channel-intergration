"""runtime(02 §2.2.4 / 06 §2.9.5 ensure_root / 05 §2.5.7 清临时):端口推导、docker 参数、ensure_root 三步逐字与失败语义、_purge_ephemeral、启动串行、boot 超时。"""
from __future__ import annotations

import asyncio

import pytest

from qtrade_agent.alerts import QIDIAN_NOT_ROOT
from qtrade_agent.config import AgentConfig, RuntimeConfig
from qtrade_agent.runtime import ContainerSpec, docker_run_argv, port_plan
from tests.conftest import Clock, make_rig


def _acct(rig, id="qd01", channel="qidian", state="running"):
    rig.store.ensure_account(id, channel, state=state, self_uid="3007373675" if channel == "qidian" else None,
                             login_mode="password" if channel == "qidian" else "qrcode")
    return rig.store.get_account_full(id)


# ---------------------------------------------------------------- 端口推导(C-07)
def test_port_plan_is_derived_from_seq():
    assert port_plan("qidian", 7) == {"adb": 16007, "stream": 16507, "frida": 16607, "adb_serial": "127.0.0.1:16007"}
    assert port_plan("qq", 3) == {"ws": 16103, "http": 16203, "webui": 16303}
    assert port_plan("wechat", 1) == {}
    for bad in (0, 99, 100):
        with pytest.raises(ValueError):
            port_plan("qidian", bad)


def test_docker_run_argv_has_ulimit_core0_and_loopback_ports():
    spec = ContainerSpec(name="qtrade-qd01", image="redroid/redroid:11.0.0-latest", ports={16001: 5555}, volumes={"/var/lib/qtrade/accounts/qd01/data": "/data"},
                         mem_limit_mb=3584, props={"androidboot.redroid_dpi": "320"}, mac="02:4e:a1:7b:c3:19")
    argv = docker_run_argv(spec)
    s = " ".join(argv)
    assert "--ulimit core=0" in s and "--memory 3584m" in s and "--restart no" in s
    assert "-p 127.0.0.1:16001:5555" in s and "-v /var/lib/qtrade/accounts/qd01/data:/data" in s and "--mac-address 02:4e:a1:7b:c3:19" in s
    assert argv[-1] == "androidboot.redroid_dpi=320" and "redroid/redroid:11.0.0-latest" in argv


def test_spec_for_qidian_and_qq(rig3):
    rt = rig3.agent.runtime
    qd = rt.spec_for(_acct(rig3))
    assert qd.name == "qtrade-qd01" and qd.ports == {16001: 5555} and qd.volumes == {"/var/lib/qtrade/accounts/qd01/data": "/data"} and qd.mem_limit_mb == 3584
    assert qd.props["androidboot.redroid_width"] == "720" and qd.props["androidboot.redroid_height"] == "1280"
    qq = rt.spec_for(_acct(rig3, "qq03", "qq"))
    assert qq.ports == {16103: 3001, 16203: 3000, 16303: 6099} and qq.mem_limit_mb == 1024      # WebUI 端口始终映射(C-35)
    with pytest.raises(ValueError):
        rt.spec_for({"id": "wx01", "channel": "wechat", "seq": 1})


# ---------------------------------------------------------------- ensure_root(06 §2.9.5 唯一出处)
async def test_ensure_root_three_steps_verbatim_and_no_kill_server(rig3):
    rt, adb, health = rig3.agent.runtime, rig3.adb, rig3.agent.health
    row = _acct(rig3)
    ok = await rt.ensure_root(row)
    assert ok is True
    s = "127.0.0.1:16001"
    assert adb.calls == [("root", s), ("shell", f"{s}: stop adbd; start adbd"), ("disconnect", s), ("connect", s), ("shell", f"{s}: whoami")]
    assert health.is_rooting("qd01") and health.rooting_until("qd01") is not None      # 窗口起点 = ensure_root 开始
    assert not any("kill" in k for k, _ in adb.calls)
    assert not hasattr(adb, "kill_server")                                              # 协议上就没有
    assert rt.qidian_root_fail_streak["qd01"] == 0
    assert not rig3.agent.alerts.is_firing(QIDIAN_NOT_ROOT, "account:qd01")


async def test_ensure_root_failure_only_warn_never_changes_state(rig3):
    rt, adb, store = rig3.agent.runtime, rig3.adb, rig3.store
    row = _acct(rig3)
    adb.whoami_after_root["127.0.0.1:16001"] = "shell"
    assert await rt.ensure_root(row) is False
    a = rig3.agent.alerts.active[(QIDIAN_NOT_ROOT, "account:qd01")]
    assert a.severity == "warn" and a.evidence == {"whoami": "shell", "ensure_root_attempts": 1, "db_visible": False} and a.hint_actions == ["open_env"]
    assert store.get_account("qd01")["state"] == "running"
    for _ in range(3):
        assert await rt.ensure_root(row) is False
    a = rig3.agent.alerts.active[(QIDIAN_NOT_ROOT, "account:qd01")]
    assert a.severity == "warn" and a.count == 4 and a.evidence["ensure_root_attempts"] == 4        # 连续 4 次仍 warn、永不 crit
    assert store.get_account("qd01")["state"] == "running"
    ev = [e for e in store.list_events(event="alert") if e["payload"]["code"] == QIDIAN_NOT_ROOT]
    assert len(ev) == 1 and ev[0]["payload"]["state"] == "firing"                                     # 重复 firing 只累加不重发
    adb.whoami_after_root["127.0.0.1:16001"] = "root"
    assert await rt.ensure_root(row) is True
    assert rt.qidian_root_fail_streak["qd01"] == 0 and not rig3.agent.alerts.is_firing(QIDIAN_NOT_ROOT, "account:qd01")
    ev = [e for e in store.list_events(event="alert") if e["payload"]["code"] == QIDIAN_NOT_ROOT]
    assert [e["payload"]["state"] for e in ev] == ["firing", "resolved"]


# ---------------------------------------------------------------- _purge_ephemeral(E-19)
async def test_purge_ephemeral_clears_only_regenerable_and_audits(rig3):
    rt, fs, adb, store, containers = rig3.agent.runtime, rig3.fs, rig3.adb, rig3.store, rig3.containers
    row = _acct(rig3)
    await rt.provision(row)
    await containers.start("qtrade-qd01")
    fs.put("/var/lib/qtrade/accounts/qd01/tmp/shot.png", 2 * 1048576)
    fs.put("/var/lib/qtrade/accounts/qd01/tmp/dump/ui.xml", 1048576)
    fs.put("/var/lib/qtrade/accounts/qd01/data/wtlogin_guid", 10)
    fs.put("/var/lib/qtrade/media/tmp/qd01-abc.part", 1048576)
    fs.put("/var/lib/qtrade/media/tmp/qd02-xyz.part", 1048576)
    fs.put("/var/lib/qtrade/media/202609/deadbeef", 1048576)
    res = await rt._purge_ephemeral(row)
    assert res["freed_mb"] == 4.0
    assert "/var/lib/qtrade/accounts/qd01/data/wtlogin_guid" in fs.files and "/var/lib/qtrade/media/tmp/qd02-xyz.part" in fs.files \
        and "/var/lib/qtrade/media/202609/deadbeef" in fs.files
    assert not any(p.startswith("/var/lib/qtrade/accounts/qd01/tmp/") for p in fs.files) and "/var/lib/qtrade/media/tmp/qd01-abc.part" not in fs.files
    assert any(c.startswith("rm -rf /data/local/tmp/*") for c in adb.shell_cmds("127.0.0.1:16001"))
    assert ("forward_remove", "127.0.0.1:16001: tcp:16601") in adb.calls
    rows = store.list_audit(action="runtime.purge_ephemeral")
    assert len(rows) == 1 and rows[0]["kind"] == "system"
    import json
    d = json.loads(rows[0]["detail_json"])
    assert d["account_id"] == "qd01" and d["freed_mb"] == 4.0 and "container:/data/local/tmp" in d["targets"]
    # 幂等:再来一次不报错、freed 0
    res2 = await rt._purge_ephemeral(row)
    assert res2["freed_mb"] == 0 and len(store.list_audit(action="runtime.purge_ephemeral")) == 2


async def test_purge_ephemeral_skips_container_step_when_not_running(rig3):
    rt, adb = rig3.agent.runtime, rig3.adb
    row = _acct(rig3)
    res = await rt._purge_ephemeral(row)
    assert "container:skipped(not running)" in res["targets"] and not adb.calls


# ---------------------------------------------------------------- 启动串行 / boot / inspect 缓存
async def test_start_is_globally_serial(tmp_path):
    rig = make_rig(tmp_path)
    rt, containers = rig.agent.runtime, rig.containers
    orig = containers.start

    async def slow_start(name):
        await asyncio.sleep(0.02)
        await orig(name)
    containers.start = slow_start
    rows = [_acct(rig, "qd01"), _acct(rig, "qd02")]
    await asyncio.gather(rt.start(rows[0]), rt.start(rows[1]))
    assert rt.max_concurrent_starts == 1 and rt.start_count == 2
    starts = [n for k, n in containers.calls if k == "start"]
    assert starts == ["qtrade-qd01", "qtrade-qd02"] or starts == ["qtrade-qd02", "qtrade-qd01"]
    assert ("connect", "127.0.0.1:16001") in rig.adb.calls and ("connect", "127.0.0.1:16002") in rig.adb.calls
    rig.store.close()


async def test_start_runs_purge_before_provision(rig3):
    rt, containers, store = rig3.agent.runtime, rig3.containers, rig3.store
    row = _acct(rig3)
    await rt.start(row)
    audit = store.list_audit(action="runtime.purge_ephemeral")
    assert len(audit) == 1
    create_idx = [i for i, (k, _) in enumerate(containers.calls) if k == "create"][0]
    assert containers.calls[0][0] == "inspect" and create_idx > 0            # purge 先 inspect(容器不在)再 create
    rt_row = store.get_runtime("qd01")
    assert rt_row["container_name"] == "qtrade-qd01" and rt_row["container_id"] and rt_row["data_dir"] == "/var/lib/qtrade/accounts/qd01/data"


async def test_wait_boot_timeout_uses_boot_timeout_s(tmp_path):
    cfg = AgentConfig(runtime=RuntimeConfig(boot_timeout_s=2))
    rig = make_rig(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=300))
    rig.adb.boot_completed["127.0.0.1:16001"] = "0"
    row = _acct(rig)
    assert await rig.agent.runtime.wait_boot(row) is False
    rig.adb.boot_completed["127.0.0.1:16001"] = "1"
    assert await rig.agent.runtime.wait_boot(row) is True
    assert rig.store.get_runtime("qd01")["last_boot_completed_ms"]
    rig.store.close()


async def test_inspect_cache_5s(rig3):
    rt, containers, clock = rig3.agent.runtime, rig3.containers, rig3.clock
    row = _acct(rig3)
    await rt.inspect(row)
    await rt.inspect(row)
    assert sum(1 for k, _ in containers.calls if k == "inspect") == 1
    clock.advance(6000)
    await rt.inspect(row)
    assert sum(1 for k, _ in containers.calls if k == "inspect") == 2


async def test_stop_and_destroy_keep_volume(rig3):
    rt, containers = rig3.agent.runtime, rig3.containers
    row = _acct(rig3)
    await rt.start(row)
    info = await rt.stop(row)
    assert info.exists and not info.running and ("stop", "qtrade-qd01") in containers.calls
    await rt.destroy(row, keep_data=True)
    assert ("remove", "qtrade-qd01") in containers.calls and "qtrade-qd01" not in containers.containers
