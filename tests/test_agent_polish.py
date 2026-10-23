"""开发者测试:Agent 侧收尾批(self_uid 回填 / default profile 降级 / 告警码登记 / 三执行体 / 档案库单一来源)。

全假后端:一条 adb / docker / 网络请求都不发(四级探测的四个级全部注入替身)。
"""
from __future__ import annotations

import asyncio
import json
import socket

import dataclasses

import pytest

from qtrade_agent import device_profiles as dp
from qtrade_agent import netprobe, sysenv
from qtrade_agent.alerts import QIDIAN_PROFILE_FALLBACK, REGISTERED, Alerts
from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qidian.ui import QidianUi, load_profiles, pick_profile
from qtrade_agent.config import AgentConfig, DeviceProfilesConfig

from tests.conftest import make_rig


# ══════════════════════════════════════════════════ 1. 告警码登记(02 §3.7)
def test_profile_fallback_码已登记为info():
    assert REGISTERED[QIDIAN_PROFILE_FALLBACK] == "info"


def test_profile_fallback_不显式传severity也是info():
    events = []

    class E:
        def emit(self, kind, **kw):
            events.append((kind, kw))

    a = Alerts(E(), clock=lambda: 1)
    assert a.firing(QIDIAN_PROFILE_FALLBACK, subject="account:qd01") is True
    assert a.active[(QIDIAN_PROFILE_FALLBACK, "account:qd01")].severity == "info"


# ══════════════════════════════════════════════════ 2. profile 落 default ⇒ 通知装配方
def _profile(version: str):
    """真的 ``Profile`` 对象:取包内 ``default.yaml`` 再换版本号(不自造替身,免得形状漂移测不出来)。"""
    return dataclasses.replace(load_profiles()["default"], version=version)


def test_落default时回调被调用_精确命中时不调用():
    seen = []
    profiles = {"default": _profile("default"), "9.9.9.9": _profile("9.9.9.9")}
    ui = QidianUi(adb=object(), profiles=profiles, on_default_profile=lambda aid, ver: seen.append((aid, ver)))
    ui.profile_for(Account(id="qd01", channel="qidian", state="running", app_version="9.9.9.9"))
    assert seen == []                                     # exact ⇒ 不降级
    ui.profile_for(Account(id="qd02", channel="qidian", state="running", app_version="1.2.3.4"))
    assert seen == [("qd02", "1.2.3.4")]                  # default ⇒ 报事实


def test_回落低版本时发告警而不降级():
    events = []

    class E:
        def emit(self, kind, **kw):
            events.append(kw.get("payload", {}).get("code"))

    seen = []
    profiles = {"default": _profile("default"), "9.1.0.0": _profile("9.1.0.0")}
    ui = QidianUi(adb=object(), profiles=profiles, alerts=Alerts(E(), clock=lambda: 1),
                  on_default_profile=lambda aid, ver: seen.append(aid))
    p, how = pick_profile(profiles, "9.1.5.0")
    assert how == "fallback" and p.version == "9.1.0.0"
    ui.profile_for(Account(id="qd01", channel="qidian", state="running", app_version="9.1.5.0"))
    assert QIDIAN_PROFILE_FALLBACK in events               # 告警发了
    assert seen == []                                      # 但不置 degraded(02 §2.2.3 两个分支各一件事)


def test_回调抛异常不影响取profile():
    def boom(aid, ver):
        raise RuntimeError("通知炸了")

    ui = QidianUi(adb=object(), profiles={"default": _profile("default")}, on_default_profile=boom)
    assert ui.profile_for(Account(id="qd01", channel="qidian", state="running", app_version="1.0.0.0")).version == "default"


# ══════════════════════════════════════════════════ 3. AccountService 的两条回填通道
@pytest.fixture()
def agent_app(tmp_path):
    r = make_rig(tmp_path)
    yield r.agent
    r.store.close()


def _mk_account(app, channel="qidian", state="logging_in"):
    row = app.store.create_account(channel=channel, label="测试号", login_mode="password",
                                   quota_mb=app.cfg.quota_mb(channel), now_ms=1000)
    app.store.transition(row["id"], state, now_ms=1000)
    return row["id"]


def test_note_self_uid_随running一并落库(agent_app):
    aid = _mk_account(agent_app)
    agent_app.accounts.note_self_uid(aid, "12345678")
    assert agent_app.store.get_account_full(aid)["self_uid"] is None      # 还没落库
    asyncio.run(_run_login(agent_app, aid, "running"))
    assert agent_app.store.get_account_full(aid)["self_uid"] == "12345678"
    assert agent_app.store.get_account_full(aid)["state"] == "running"


def test_note_self_uid_空值不记_换号只回填不删水位(agent_app, caplog):
    aid = _mk_account(agent_app)
    agent_app.accounts.note_self_uid(aid, "")
    assert agent_app.accounts._pending_self_uid == {}
    agent_app.accounts.note_self_uid(aid, "111")
    asyncio.run(_run_login(agent_app, aid, "running"))
    # 第二轮登录换了 uin:只回填新值,水位/审计一概不动(那是 06 §2.9.5 ③ 的活)
    agent_app.store.transition(aid, "logging_in", now_ms=2000)
    agent_app.accounts.note_self_uid(aid, "222")
    asyncio.run(_run_login(agent_app, aid, "running"))
    assert agent_app.store.get_account_full(aid)["self_uid"] == "222"
    assert agent_app.store.list_audit(action="qidian.rebootstrap") == []


def test_登录没进running时回填值不留到下一轮(agent_app):
    aid = _mk_account(agent_app)
    agent_app.accounts.note_self_uid(aid, "999")
    asyncio.run(_run_login(agent_app, aid, "WAIT_SMS"))
    assert agent_app.accounts._pending_self_uid == {}
    assert agent_app.store.get_account_full(aid)["self_uid"] is None


def test_落default在running时立刻置degraded(agent_app):
    aid = _mk_account(agent_app, state="running")
    agent_app.accounts.note_default_profile(aid, "1.2.3.4")
    row = agent_app.store.get_account_full(aid)
    assert row["state"] == "degraded" and row["state_code"] == "UI_UNEXPECTED"
    assert "default.yaml" in row["state_reason"]
    agent_app.accounts.note_default_profile(aid, "1.2.3.4")            # 已 degraded ⇒ 幂等,不再迁
    assert agent_app.store.get_account_full(aid)["state"] == "degraded"


def test_落default在logging_in时不跳段_等running后再置(agent_app):
    aid = _mk_account(agent_app, state="logging_in")
    agent_app.accounts.note_default_profile(aid, "1.2.3.4")
    assert agent_app.store.get_account_full(aid)["state"] == "logging_in"   # 00 §8.1:不许 logging_in → degraded
    asyncio.run(_run_login(agent_app, aid, "running"))
    row = agent_app.store.get_account_full(aid)
    assert row["state"] == "degraded" and row["state_code"] == "UI_UNEXPECTED"


def test_degraded事件序列不跳段(agent_app):
    aid = _mk_account(agent_app, state="logging_in")
    agent_app.accounts.note_default_profile(aid, "1.0.0.0")
    agent_app.accounts.note_self_uid(aid, "777")
    asyncio.run(_run_login(agent_app, aid, "running"))
    states = [e["payload"]["state"] for e in agent_app.store.list_events(event="account_state", account_id=aid)]
    assert states[-2:] == ["running", "degraded"]
    assert agent_app.store.get_account_full(aid)["self_uid"] == "777"


async def _run_login(app, aid, result):
    row = app.store.get_account_full(aid)

    async def fn(r, account, secret):
        return result

    app.accounts._login_fn = fn
    await app.accounts._run_login(aid, row, None, "ls_test")


# ══════════════════════════════════════════════════ 4a. 四级探测(一条真请求都不发)
def _probe(**kw):
    base = {"resolve": lambda h, p, t: ["1.2.3.4"], "connect": lambda h, p, t: None,
            "handshake": lambda h, p, t: {"tls_version": "TLSv1.3", "issuer": ""},
            "http_get": lambda h, p, path, *, tls, timeout_s: 200}
    base.update(kw)
    return netprobe.SocketLevelProbe(**base)


def test_parse_target_三种形态():
    assert netprobe.parse_target("a.b:443") == ("a.b", 443, None)
    assert netprobe.parse_target("https://a.b/v2/") == ("a.b", 443, "/v2/")
    assert netprobe.parse_target("http://a.b:8080/x") == ("a.b", 8080, "/x")
    assert netprobe.parse_target("a.b") == ("a.b", None, None)
    with pytest.raises(ValueError):
        netprobe.parse_target("  ")


def test_逐级到http_全通OK():
    out = asyncio.run(_probe().probe("a.b:443"))
    assert out["status"] == "OK" and out["level_reached"] == "http" and out["http_status"] == 200


def test_dns失败就停在none():
    def boom(h, p, t):
        raise socket.gaierror("no such host")

    out = asyncio.run(_probe(resolve=boom).probe("a.b:443"))
    assert out["status"] == "DNS_FAIL" and out["level_reached"] == "none"


def test_解析无结果也算DNS_FAIL():
    out = asyncio.run(_probe(resolve=lambda h, p, t: []).probe("a.b:443"))
    assert out["status"] == "DNS_FAIL"


def test_tcp超时停在dns():
    def boom(h, p, t):
        raise socket.timeout("timed out")

    out = asyncio.run(_probe(connect=boom).probe("a.b:443"))
    assert out["status"] == "TCP_TIMEOUT" and out["level_reached"] == "dns"


def test_快RST判BLOCKED_BY_POLICY_慢RST判TCP_REFUSED():
    def refuse(h, p, t):
        raise ConnectionRefusedError("refused")

    ticks = iter([0.0, 0.000_1])                      # 0.1 ms ⇒ 本机策略
    fast = netprobe.SocketLevelProbe(resolve=lambda h, p, t: ["1.2.3.4"], connect=refuse, clock=lambda: next(ticks))
    assert asyncio.run(fast.probe("a.b:443"))["status"] == "BLOCKED_BY_POLICY"
    ticks2 = iter([0.0, 0.05])                        # 50 ms ⇒ 远端拒
    slow = netprobe.SocketLevelProbe(resolve=lambda h, p, t: ["1.2.3.4"], connect=refuse, clock=lambda: next(ticks2))
    assert asyncio.run(slow.probe("a.b:443"))["status"] == "TCP_REFUSED"


def test_私有协议端口只探到tcp():
    out = asyncio.run(_probe().probe("msfxg.3g.qq.com:14000"))
    assert out["status"] == "OK" and out["level_reached"] == "tcp"        # 04 §2.8.2:企点 dns→tcp,不做更深


def test_tls失败停在tcp():
    import ssl

    def boom(h, p, t):
        raise ssl.SSLError("handshake failure")

    out = asyncio.run(_probe(handshake=boom).probe("a.b:443"))
    assert out["status"] == "TLS_FAIL" and out["level_reached"] == "tcp"


def test_公司CA的签发者原样记下():
    out = asyncio.run(_probe(handshake=lambda h, p, t: {"tls_version": "TLSv1.2", "issuer": "ACME Corp CA"}).probe("a.b:993"))
    assert out["mitm_ca"] == "ACME Corp CA" and out["level_reached"] == "tls"   # 993 不在 HTTP_PORTS ⇒ 停 tls


def test_http状态码映射():
    assert netprobe.http_status_to_result(407) == "PROXY_REQUIRED"
    assert netprobe.http_status_to_result(403) == "HTTP_4XX"
    assert netprobe.http_status_to_result(503) == "HTTP_5XX"
    assert netprobe.http_status_to_result(204) == "OK"
    out = asyncio.run(_probe(http_get=lambda h, p, path, *, tls, timeout_s: 407).probe("https://a.b/"))
    assert out["status"] == "PROXY_REQUIRED" and out["level_reached"] == "http"


def test_坏目标记SKIPPED_不出网():
    out = asyncio.run(_probe().probe(""))
    assert out["status"] == "SKIPPED" and out["detail"].startswith("bad_target")


def test_总上限到点即止():
    def slow(h, p, t):
        import time as _t
        _t.sleep(0.3)
        return ["1.2.3.4"]

    p = netprobe.SocketLevelProbe(resolve=slow, total_timeout_s=0.05)
    out = asyncio.run(p.probe("a.b:443"))
    assert out["status"] == "TCP_TIMEOUT" and "总上限" in out["detail"]


def test_没给端口只探dns():
    out = asyncio.run(_probe().probe("a.b"))
    assert out["status"] == "OK" and out["level_reached"] == "dns"


def test_缺省不出网_app没注入就没有net_probe(agent_app):
    assert getattr(agent_app, "net_probe", None) is None


# ══════════════════════════════════════════════════ 4b. WSL 侧只读采集
class _FakeBase:
    def snapshot(self):
        return {"iface": "eth0", "mtu": 1400}


def test_wsl_env_reader_合并三项():
    files = {"/etc/os-release": 'ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04.3 LTS"\n# 注释\n'}

    class U:
        sysname, release, version, machine = "Linux", "6.6.1-qtrade", "#1 SMP", "x86_64"

    class DU:
        total, used, free = 100 * 1048576, 40 * 1048576, 60 * 1048576

    sysr = sysenv.LinuxSysReader(read_text=files.get, uname=lambda: U(), disk_usage=lambda p: DU(),
                                df_paths=("/", "/var/lib/qtrade"))
    snap = sysenv.WslEnvReader(base=_FakeBase(), sys_reader=sysr).snapshot()
    assert snap["mtu"] == 1400                                  # 基础项原样带上
    assert snap["os_release"] == {"id": "ubuntu", "version_id": "22.04", "pretty_name": "Ubuntu 22.04.3 LTS"}
    assert snap["uname"]["release"] == "6.6.1-qtrade"
    assert snap["disks"] == [{"path": "/", "total_mb": 100, "used_mb": 40, "free_mb": 60},
                             {"path": "/var/lib/qtrade", "total_mb": 100, "used_mb": 40, "free_mb": 60}]


def test_读不到的项一律None_不编造():
    sysr = sysenv.LinuxSysReader(read_text=lambda p: None, uname=_raise_os, disk_usage=_raise_os,
                                 df_paths=("/nope",))
    snap = sysenv.WslEnvReader(base=_FakeBase(), sys_reader=sysr).snapshot()
    assert snap["os_release"] == {"id": None, "version_id": None, "pretty_name": None}
    assert snap["uname"] == {"sysname": None, "release": None, "version": None, "machine": None}
    assert snap["disks"] == []                                  # 读不到的挂载点整条不出现(不放 0 冒充盘满)


def _raise_os(*a, **kw):
    raise OSError("nope")


def test_os_release解析剥引号与注释():
    assert sysenv.parse_os_release('A="x"\nB=\'y\'\n#C=z\nD\n') == {"A": "x", "B": "y"}
    assert sysenv.parse_os_release(None) == {}


# ══════════════════════════════════════════════════ 4c. docker 代理执行体(只写不重启)
class MemIo:
    def __init__(self):
        self.files: dict[str, str] = {}

    def read(self, path):
        return self.files.get(path)

    def write(self, path, content):
        self.files[path] = content

    def remove(self, path):
        return self.files.pop(path, None) is not None


def test_apply写dropin并要求重启_不自己重启():
    io = MemIo()
    ap = sysenv.DockerProxyApplier(io=io)
    assert asyncio.run(ap.apply({"http": "http://p:7890", "no_proxy": "corp.local"})) is True
    body = io.files[ap.path]
    assert 'Environment="HTTP_PROXY=http://p:7890"' in body
    assert 'Environment="HTTPS_PROXY=http://p:7890"' in body          # https 缺省沿用 http
    assert "corp.local" in body and "127.0.0.1" in body and "localhost" in body
    assert ap.last_result == {"action": "apply", "path": ap.path, "changed": True,
                              "restart_required": True, "content": body}


def test_apply幂等_内容不变就不重写也不要求重启():
    io = MemIo()
    ap = sysenv.DockerProxyApplier(io=io)
    asyncio.run(ap.apply({"http": "http://p:7890"}))
    asyncio.run(ap.apply({"http": "http://p:7890"}))
    assert ap.last_result["changed"] is False and ap.last_result["restart_required"] is False


def test_disable删文件_没有文件时不要求重启():
    io = MemIo()
    ap = sysenv.DockerProxyApplier(io=io)
    asyncio.run(ap.apply({"http": "http://p:7890"}))
    assert asyncio.run(ap.disable()) is True
    assert ap.path not in io.files and ap.last_result["restart_required"] is True
    assert asyncio.run(ap.disable()) is True
    assert ap.last_result["restart_required"] is False


def test_apply空代理等于disable():
    io = MemIo()
    ap = sysenv.DockerProxyApplier(io=io)
    asyncio.run(ap.apply({"http": "http://p:7890"}))
    asyncio.run(ap.apply(None))
    assert ap.path not in io.files and ap.last_result["action"] == "disable"


def test_代理里没有http也没有https就拒绝渲染():
    with pytest.raises(ValueError):
        sysenv.render_dropin({"no_proxy": "corp.local"})


def test_current给出当前落盘内容():
    io = MemIo()
    ap = sysenv.DockerProxyApplier(io=io)
    assert ap.current() is None
    asyncio.run(ap.apply({"https": "http://p:7890"}))
    assert ap.current() == io.files[ap.path]


# ══════════════════════════════════════════════════ 5. 机型档案库单一来源
def _lib(payload, path="/x/device_profiles.json"):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return dp.Library(path, read_text=lambda p: raw)


def test_从库文件加载_两种顶层形状():
    rows = [{"profile_key": "a", "brand": "B", "model": "M", "release": "13"}]
    for payload in ({"templates": rows}, rows):
        lib = _lib(payload)
        assert lib.source == dp.SOURCE_LIBRARY and len(lib) == 1
        assert lib.list_view() == [{"profile_key": "a", "brand": "B", "model": "M", "release": "13", "weight": 1}]


def test_缺weight补默认_有weight原样():
    lib = _lib({"templates": [{"profile_key": "a", "weight": 7}, {"profile_key": "b"}]})
    assert [r["weight"] for r in lib.list_view()] == [7, dp.DEFAULT_WEIGHT]


def test_字段名用作废的template_key直接拒收():
    with pytest.raises(ValueError, match="template_key"):
        dp.parse_library(json.dumps({"templates": [{"template_key": "a"}]}))


def test_坏文件回落内置清单并记error(caplog):
    for payload in ("{不是json", json.dumps({"templates": []}), json.dumps({"templates": [{"brand": "B"}]}),
                    json.dumps({"templates": "x"}), json.dumps({"templates": [{"profile_key": "a"}, {"profile_key": "a"}]})):
        lib = _lib(payload)
        assert lib.source == dp.SOURCE_BUILTIN and len(lib) == len(dp.BUILTIN_FALLBACK)
        assert lib.error


def test_文件缺失回落内置清单():
    lib = dp.Library("/nope/device_profiles.json", read_text=lambda p: None)
    assert lib.source == dp.SOURCE_BUILTIN and lib.error == "库文件读不到"
    assert all(set(r) == set(dp.LIST_KEYS) for r in lib.list_view())


def test_没给路径时只用内置_不读盘():
    called = []
    lib = dp.load(AgentConfig(device_profiles=DeviceProfilesConfig(library="")),
                  read_text=lambda p: called.append(p))
    assert lib.source == dp.SOURCE_BUILTIN and called == [] and lib.error is None


def test_load走配置键():
    cfg = AgentConfig(device_profiles=DeviceProfilesConfig(library="/custom/lib.json"))
    seen = []

    def rt(p):
        seen.append(p)
        return json.dumps([{"profile_key": "z"}])

    lib = dp.load(cfg, read_text=rt)
    assert seen == ["/custom/lib.json"] and lib.by_key("z")["profile_key"] == "z"


def test_by_key取完整模板_不存在回None():
    lib = _lib({"templates": [{"profile_key": "a", "sdk": 33, "serialno_style": "hex8"}]})
    assert lib.by_key("a")["serialno_style"] == "hex8"
    assert lib.by_key("nope") is None


def test_内置回落清单的形状与库一致():
    keys = set(dp.LIST_KEYS)
    assert all(keys <= set(t) for t in dp.BUILTIN_FALLBACK)
    assert len({t["profile_key"] for t in dp.BUILTIN_FALLBACK}) == len(dp.BUILTIN_FALLBACK)


def test_随包真库能被本解析器读出32条():
    """`installer/rootfs/profiles/device_profiles.json` 是随 rootfs 落盘的那一份(只读,不改)。"""
    import pathlib

    f = pathlib.Path(__file__).resolve().parents[1] / "installer" / "rootfs" / "profiles" / "device_profiles.json"
    if not f.exists():                                  # 只跑在有 installer 目录的树上
        pytest.skip("installer/rootfs/profiles 不在本树")
    rows = dp.parse_library(f.read_text(encoding="utf-8"))
    assert len(rows) == 32 and all(r.get("profile_key") for r in rows)


def test_app装配了档案库(agent_app):
    assert isinstance(agent_app.device_profiles, dp.Library)


def test_24端点的数据源就是装配好的库(agent_app):
    """#24 端点里那一行 ``device_profiles.templates(getattr(agent,'device_profiles',None))`` 的等价断言:
    换掉 ``agent.device_profiles`` 之后出参随之变(证明它读的是装配好的库,不是模块常量)。"""
    agent_app.device_profiles = _lib({"templates": [{"profile_key": "only_one", "brand": "B", "model": "M", "release": "13"}]})
    rows = dp.templates(getattr(agent_app, "device_profiles", None))
    assert [r["profile_key"] for r in rows] == ["only_one"]
    assert all(set(x) == set(dp.LIST_KEYS) for x in rows)


# ══════════════════════════════════════════════════ 6. 装配:假后端不装真执行体、显式注入优先
def test_假后端下三个执行体都不自动装(agent_app):
    """`make_rig` 注了 FakeAdb + FakeContainers ⇒ 不是真机:一个读 /proc、一个写 /etc、一个出网,都不许自动装上。"""
    assert agent_app.net_probe is None                 # 🔴 缺省不出网
    assert agent_app.wsl_env_reader is None
    assert agent_app.docker_proxy is None


def test_显式注入优先(tmp_path):
    from qtrade_agent.app import AgentApp
    from qtrade_agent.config import AgentConfig
    from qtrade_agent.runtime import FakeAdb, FakeContainers
    from qtrade_agent.runtime.runtime import FakeFs
    from qtrade_agent.vault_client import FakeVault
    from qtrade_agent.winagent_client import FakeWinAgent

    probe, applier, reader = _probe(), sysenv.DockerProxyApplier(io=MemIo()), sysenv.WslEnvReader(base=_FakeBase())
    wa = FakeWinAgent()
    app = AgentApp(AgentConfig(), db_path=str(tmp_path / "a.db"), containers=FakeContainers(), adb=FakeAdb(),
                   vault=FakeVault(), winagent_transport=wa, winagent_base_url="http://x:17610", winagent_token=wa.token,
                   fs=FakeFs(), wsl_total_mb=11264, boot_poll_s=0,
                   net_probe=probe, docker_proxy=applier, wsl_env_reader=reader).open()
    try:
        assert app.net_probe is probe and app.docker_proxy is applier and app.wsl_env_reader is reader
    finally:
        app.store.close()


# ── #75 WSL 侧四级探测的总开关 `[probe] agent_probe_enabled`(04 §7 owner;裁决 00 §15g R6-62 Ⅶ①)
def _mk_app(tmp_path, cfg, **kw):
    from qtrade_agent.app import AgentApp
    from qtrade_agent.runtime import FakeAdb, FakeContainers
    from qtrade_agent.runtime.runtime import FakeFs
    from qtrade_agent.vault_client import FakeVault
    from qtrade_agent.winagent_client import FakeWinAgent

    wa = FakeWinAgent()
    kw.setdefault("containers", FakeContainers())
    kw.setdefault("adb", FakeAdb())
    return AgentApp(cfg, db_path=str(tmp_path / "probe.db"), vault=FakeVault(), winagent_transport=wa,
                    winagent_base_url="http://x:17610", winagent_token=wa.token, fs=FakeFs(),
                    wsl_total_mb=11264, boot_poll_s=0, **kw).open()


def _cfg_probe(enabled: bool):
    from qtrade_agent.config import ProbeConfig
    return dataclasses.replace(AgentConfig(), probe=ProbeConfig(agent_probe_enabled=enabled))


def test_probe_启用键缺省false_缺省不出网():
    """04 §7 `[probe] agent_probe_enabled` 缺省 false ⇒ 不装探测器 ⇒ #75 `mode:"full"` 的 WSL 侧
    逐目标记 `SKIPPED(agent_probe_disabled)`(那一步的断言在 tests/test_api_ext.py,判据就是 `net_probe is None`)。"""
    assert AgentConfig().probe.agent_probe_enabled is False


def test_probe_配置开着但不是真机后端仍不装(tmp_path):
    """两个条件是 **且**:假 adb/假容器的开发容器与测试,配置开了也不许出网。"""
    app = _mk_app(tmp_path, _cfg_probe(True))
    try:
        assert app.net_probe is None
    finally:
        app.store.close()


def test_probe_真机后端下配置开才装_关则不装(tmp_path):
    """真后端(不注 adb/容器)+ `agent_probe_enabled=true` ⇒ 自动装 `SocketLevelProbe`;为 false 时仍不装。

    只让 `net_probe` 这一条走自动装配:另两个真机执行体(读 /proc、写 /etc)照旧注假件。
    `SocketLevelProbe()` 的构造不出网,出网只发生在 `probe()`,本用例不调用它。
    """
    from qtrade_agent import netprobe as np

    fakes = dict(docker_proxy=sysenv.DockerProxyApplier(io=MemIo()), wsl_env_reader=sysenv.WslEnvReader(base=_FakeBase()))
    on = _mk_app(tmp_path, _cfg_probe(True), containers=None, adb=None, **fakes)
    try:
        assert isinstance(on.net_probe, np.SocketLevelProbe)
    finally:
        on.store.close()
    off = _mk_app(tmp_path, _cfg_probe(False), containers=None, adb=None, **fakes)
    try:
        assert off.net_probe is None
    finally:
        off.store.close()


def test_probe_显式注入优先于配置(tmp_path):
    """注入的探测器 > 配置:配置关着也用注入的那个(反过来也不会被自动装的顶掉)。"""
    probe = _probe()
    app = _mk_app(tmp_path, _cfg_probe(False), net_probe=probe)
    try:
        assert app.net_probe is probe
    finally:
        app.store.close()


def test_企点UI装上了两条回填回调(agent_app):
    """假后端下 sender/login_fn 保持未接,但 `QidianUi` 对象本身已带 on_default_profile 通道。"""
    assert agent_app.qidian_ui._on_default_profile is not None
    aid = _mk_account(agent_app, state="running")
    agent_app.qidian_ui._on_default_profile(aid, "1.2.3.4")          # 走一遍真回调,不经 adb
    assert agent_app.store.get_account_full(aid)["state_code"] == "UI_UNEXPECTED"
