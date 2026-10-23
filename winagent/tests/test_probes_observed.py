"""实测采样候选的回写 / 读取 / 采纳(整合裁决 (ci) 补的两个 WinAgent 端点;02 #76b + §3.2 DDL + 04 §2.8.4/§3.4)。

三段职责各验一遍:
① ``POST /probe {mode:'sample'}`` 只返回不落库(04 §3.4);
② ``PUT /probes {kind:'observed'}`` 是**唯一写入口**(04 §3.4);
③ ``PUT /probes/adopt {observed_ids}`` 写 ``adopted_ms`` + ``settings['probe.targets']``(02 #76b)。
"""
from __future__ import annotations

import pytest

from qtrade_winagent.config import AlertConfig, NetConfig, ProbeConfig
from qtrade_winagent.alerts import AlertBuffer
from qtrade_winagent.db import Db
from qtrade_winagent.errors import WaError
from qtrade_winagent.fakes import FakeFirewall, FakeNet, FakeProbe
from qtrade_winagent.netprobe import (HOSTS_KEY_BY_CHANNEL, PROBE_KINDS, SETTING_PROBE_TARGETS, NetProbe)

from tests.conftest import AGENT_TOKEN, CONSOLE_TOKEN, INSTALLER_TOKEN, SVC_EXE, Clock, client

WECHAT_ROW = {"channel": "wechat", "account_id": "wx01", "hostname": "long.weixin.qq.com",
              "ip": "203.205.254.1", "port": 443, "proto": "tcp", "side": "windows", "samples": 3}
QIDIAN_ROW = {"channel": "qidian", "account_id": "qd01", "hostname": "msfxg.3g.qq.com",
              "ip": "180.163.1.1", "port": 8080, "proto": "tcp", "side": "container", "samples": 5}


def mk():
    clk = Clock()
    db = Db(":memory:", clock=clk).open()
    np_ = NetProbe(db, FakeNet(), FakeFirewall(), FakeProbe(), NetConfig(), ProbeConfig(),
                   AlertBuffer(AlertConfig(), clock=clk), svc_exe=SVC_EXE, clock=clk)
    return db, np_, clk


# ---------------------------------------------------------------- ② 写入口
def test_write_observed_upserts_by_the_ddl_unique_index():
    """02 §3.2 ``ux_probe_obs (COALESCE(account_id,''), remote_ip, remote_port, proto)``:同键累加 ``hits``、推 ``last_seen_ms``。"""
    db, np_, clk = mk()
    assert np_.write_observed([WECHAT_ROW, QIDIAN_ROW]) == 2
    rows = db.query("SELECT * FROM probe_targets_observed ORDER BY id")
    assert [r["hits"] for r in rows] == [3, 5]
    assert rows[0]["first_seen_ms"] == rows[0]["last_seen_ms"]
    clk.advance(60_000)
    np_.write_observed([{**WECHAT_ROW, "samples": 2}])
    r = db.one("SELECT * FROM probe_targets_observed WHERE remote_ip=?", (WECHAT_ROW["ip"],))
    assert r["hits"] == 5 and r["last_seen_ms"] > r["first_seen_ms"]        # 累加而不是新增一行
    assert len(db.query("SELECT * FROM probe_targets_observed")) == 2
    db.close()


def test_write_observed_keeps_existing_hostname_when_new_is_null():
    """域名会变解析,采不到时不要把已有的抹掉(``remote_host`` 只在新值非空时覆盖)。"""
    db, np_, _clk = mk()
    np_.write_observed([WECHAT_ROW])
    np_.write_observed([{**WECHAT_ROW, "hostname": None}])
    assert db.one("SELECT remote_host FROM probe_targets_observed")["remote_host"] == "long.weixin.qq.com"
    db.close()


def test_write_observed_accepts_ddl_column_names_too():
    """Agent 侧既可能按 04 的 ``ip/port/hostname/samples`` 发,也可能按 02 DDL 的列名发,两套都收。"""
    db, np_, _clk = mk()
    np_.write_observed([{"channel": "qq", "remote_host": "msfwifi.3g.qq.com", "remote_ip": "1.2.3.4",
                         "remote_port": 14000, "proto": "tcp", "side": "container", "hits": 7}])
    r = db.one("SELECT * FROM probe_targets_observed")
    assert r["remote_port"] == 14000 and r["hits"] == 7 and r["channel"] == "qq"
    db.close()


def test_write_observed_rejects_bad_enums_and_missing_fields():
    _db, np_, _clk = mk()
    with pytest.raises(WaError) as e1:
        np_.write_observed([{**WECHAT_ROW, "channel": "telegram"}])
    assert e1.value.reason == "bad_enum"
    with pytest.raises(WaError) as e2:
        np_.write_observed([{**WECHAT_ROW, "proto": "sctp"}])
    assert e2.value.reason == "bad_enum"
    with pytest.raises(WaError) as e3:
        np_.write_observed([{**WECHAT_ROW, "side": "moon"}])
    assert e3.value.reason == "bad_enum"
    with pytest.raises(WaError) as e4:
        np_.write_observed([{"channel": "qq", "port": 443}])
    assert e4.value.reason == "missing_field"


def test_write_observed_allows_null_channel_for_host_processes():
    """02 §3.2:``account_id`` 为 NULL = 宿主机进程(如微信 PC),``channel`` 也可为 NULL。"""
    db, np_, _clk = mk()
    np_.write_observed([{"ip": "1.1.1.1", "port": 443, "proto": "tcp", "side": "windows"}])
    r = db.one("SELECT * FROM probe_targets_observed")
    assert r["account_id"] is None and r["channel"] is None
    db.close()


# ---------------------------------------------------------------- 读
def test_read_observed_carries_stable_id_and_in_config():
    """裁决 (ch):行的稳定 id = ``probe_targets_observed.id``,``observed_ids`` 用的就是它。"""
    db, np_, _clk = mk()
    np_.write_observed([WECHAT_ROW, QIDIAN_ROW])
    rows = np_.read_observed()
    assert all(isinstance(r["id"], int) for r in rows)
    assert all(r["in_config"] is False for r in rows)
    np_.adopt([rows[0]["id"]])
    assert [r["in_config"] for r in np_.read_observed() if r["id"] == rows[0]["id"]] == [True]
    db.close()


def test_read_observed_filters():
    db, np_, clk = mk()
    np_.write_observed([WECHAT_ROW])
    t0 = clk.now_ms
    clk.advance(120_000)
    np_.write_observed([QIDIAN_ROW])
    assert [r["channel"] for r in np_.read_observed(channel="qidian")] == ["qidian"]
    assert len(np_.read_observed(since=t0 + 1)) == 1
    assert len(np_.read_observed(limit=1)) == 1
    ids = [r["id"] for r in np_.read_observed(channel="wechat")]
    np_.adopt(ids)
    assert [r["channel"] for r in np_.read_observed(adopted=True)] == ["wechat"]
    assert [r["channel"] for r in np_.read_observed(adopted=False)] == ["qidian"]
    db.close()


# ---------------------------------------------------------------- ③ 采纳
def test_adopt_writes_adopted_ms_and_probe_targets_setting():
    db, np_, clk = mk()
    np_.write_observed([WECHAT_ROW, QIDIAN_ROW])
    ids = [r["id"] for r in np_.read_observed()]
    out = np_.adopt(ids)
    assert out["adopted"] == sorted(ids)                                    # 回**行 id 数组**(#76b 的 adopted)
    assert all(r["adopted_ms"] == clk.now_ms for r in out["adopted_rows"])
    assert db.get_setting(SETTING_PROBE_TARGETS) == out["targets"]
    assert set(out["targets"]) == {"long.weixin.qq.com:443", "msfxg.3g.qq.com:8080"}   # 元素 = "host:port"(04 §2.8.4)
    db.close()


def test_adopt_groups_hosts_by_channel_for_star_hosts_config():
    """04 §2.8.4「写入配置」= 按通道分成 ``qidian_hosts``/``qq_hosts``/``wechat_hosts`` 三组。"""
    db, np_, _clk = mk()
    np_.write_observed([WECHAT_ROW, QIDIAN_ROW])
    out = np_.adopt([r["id"] for r in np_.read_observed()])
    assert set(out["hosts_by_channel"]) == set(HOSTS_KEY_BY_CHANNEL.values())
    assert out["hosts_by_channel"]["wechat_hosts"] == ["long.weixin.qq.com:443"]
    assert out["hosts_by_channel"]["qidian_hosts"] == ["msfxg.3g.qq.com:8080"]
    assert out["hosts_by_channel"]["qq_hosts"] == []
    db.close()


def test_adopt_replaces_whole_table_not_append():
    """🔴 04 §2.8.4 逐字「**替换整表不追加**,让用户能删旧项」:``observed_ids`` 是采纳后的**全集**。"""
    db, np_, _clk = mk()
    np_.write_observed([WECHAT_ROW, QIDIAN_ROW])
    rows = np_.read_observed()
    wx = [r["id"] for r in rows if r["channel"] == "wechat"]
    qd = [r["id"] for r in rows if r["channel"] == "qidian"]
    np_.adopt(wx + qd)
    out = np_.adopt(qd)                                                  # 只留企点 ⇒ 微信那条应被取消采纳
    assert out["adopted"] == qd and [r["channel"] for r in out["adopted_rows"]] == ["qidian"]
    assert db.one("SELECT adopted_ms FROM probe_targets_observed WHERE id=?", (wx[0],))["adopted_ms"] is None
    assert db.get_setting(SETTING_PROBE_TARGETS) == ["msfxg.3g.qq.com:8080"]
    db.close()


def test_adopt_empty_list_clears_targets():
    db, np_, _clk = mk()
    np_.write_observed([WECHAT_ROW])
    np_.adopt([r["id"] for r in np_.read_observed()])
    out = np_.adopt([])
    assert out["adopted"] == [] and out["targets"] == [] and db.get_setting(SETTING_PROBE_TARGETS) == []
    assert db.one("SELECT adopted_ms FROM probe_targets_observed")["adopted_ms"] is None
    db.close()


def test_adopt_does_not_refresh_already_adopted_timestamp():
    """幂等护栏(与 ``error_since_ms`` 同款):重复采纳不刷新起算时刻。"""
    db, np_, clk = mk()
    np_.write_observed([WECHAT_ROW])
    ids = [r["id"] for r in np_.read_observed()]
    first = np_.adopt(ids)["adopted_rows"][0]["adopted_ms"]
    clk.advance(60_000)
    assert np_.adopt(ids)["adopted_rows"][0]["adopted_ms"] == first
    db.close()


def test_adopt_rejects_unknown_and_duplicate_ids():
    db, np_, _clk = mk()
    np_.write_observed([WECHAT_ROW])
    ids = [r["id"] for r in np_.read_observed()]
    with pytest.raises(WaError) as e:
        np_.adopt(ids + [9999])
    assert e.value.reason == "observed_id_not_found" and e.value.http_status == 404
    with pytest.raises(WaError) as e2:
        np_.adopt(ids + ids)
    assert e2.value.reason == "duplicate_ids"
    assert db.one("SELECT adopted_ms FROM probe_targets_observed")["adopted_ms"] is None    # 失败不留半截状态
    db.close()


# ================================================================== HTTP 契约
async def test_http_put_observed_then_get_then_adopt(rig):
    """一整圈:Agent 回写候选 → 控制台读候选 → 用户勾选后采纳。"""
    async with client(rig) as c:
        w = await c.put("/wa/v1/probes", json={"kind": "observed", "rows": [WECHAT_ROW, QIDIAN_ROW]})
        assert w.status_code == 200 and w.json() == {"written": 2, "kind": "observed"}
        got = (await c.get("/wa/v1/probes?kind=observed")).json()
        assert len(got["observed"]) == 2 and got["targets"] == []
        ids = [r["id"] for r in got["observed"]]
        a = await c.put("/wa/v1/probes/adopt", json={"observed_ids": ids})
        assert a.status_code == 200
        body = a.json()
        assert body["adopted"] == sorted(ids) and len(body["targets"]) == 2
        assert all(isinstance(t, str) and ":" in t for t in body["targets"])
        assert body["hosts_by_channel"]["wechat_hosts"] == ["long.weixin.qq.com:443"]
        again = (await c.get("/wa/v1/probes?kind=observed")).json()
        assert all(r["in_config"] for r in again["observed"]) and len(again["targets"]) == 2


async def test_http_get_observed_query_params(rig):
    async with client(rig) as c:
        await c.put("/wa/v1/probes", json={"kind": "observed", "rows": [WECHAT_ROW, QIDIAN_ROW]})
        assert len((await c.get("/wa/v1/probes?kind=observed&channel=qq")).json()["observed"]) == 0
        assert len((await c.get("/wa/v1/probes?kind=observed&channel=wechat")).json()["observed"]) == 1
        assert len((await c.get("/wa/v1/probes?kind=observed&limit=1")).json()["observed"]) == 1
        assert len((await c.get("/wa/v1/probes?kind=observed&adopted=false")).json()["observed"]) == 2


async def test_http_kind_result_is_unchanged(rig):
    """#15 原语义一个字不动:不带 ``kind`` 仍读 ``probe_results``、仍回 ``results`` 键。"""
    async with client(rig) as c:
        await c.put("/wa/v1/probes", json={"run_id": "r1", "trigger": "boot", "results": [
            {"target": "apk_url", "side": "wsl", "host": "apk.corp", "port": 443,
             "level_reached": "http", "result": "OK"}]})
        b = (await c.get("/wa/v1/probes?run_id=r1")).json()
        assert list(b) == ["results"] and b["results"][0]["target"] == "apk_url"
        assert (await c.get("/wa/v1/probes?kind=result&latest=1")).json()["results"]


async def test_http_bad_kind_is_400(rig):
    async with client(rig) as c:
        r = await c.get("/wa/v1/probes?kind=候选")
        assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_kind"
        w = await c.put("/wa/v1/probes", json={"kind": "候选", "rows": []})
        assert w.status_code == 400 and w.json()["error"]["reason"] == "bad_kind"
    assert PROBE_KINDS == ("result", "observed")


async def test_http_adopt_token_matrix_agent_only(rig):
    """令牌沿用同类写端点 #16 的 **A**;控制台经 Agent 调,不直连 17610(C-32/C-03/R-07)。"""
    for tok in (CONSOLE_TOKEN, INSTALLER_TOKEN):
        async with client(rig, token=tok) as c:
            assert (await c.put("/wa/v1/probes/adopt", json={"observed_ids": []})).status_code == 403
    async with client(rig, token=AGENT_TOKEN) as c:
        assert (await c.put("/wa/v1/probes/adopt", json={"observed_ids": []})).status_code == 200


async def test_http_get_observed_token_matrix_follows_15(rig):
    """读端点沿用 #15 的 **A/C/I**。"""
    for tok in (AGENT_TOKEN, CONSOLE_TOKEN, INSTALLER_TOKEN):
        async with client(rig, token=tok) as c:
            assert (await c.get("/wa/v1/probes?kind=observed")).status_code == 200


async def test_http_adopt_rejects_targets_key_per_ruling_cg(rig):
    """裁决 (cg):入参只收 ``observed_ids``;传 ``targets`` 要给出明确指引,而不是语焉不详的 400。"""
    async with client(rig) as c:
        r = await c.put("/wa/v1/probes/adopt", json={"targets": ["a:1"]})
    b = r.json()
    assert r.status_code == 400 and b["error"]["reason"] == "missing_observed_ids"
    assert "observed_ids" in b["error"]["message"]


async def test_http_adopt_validates_id_types(rig):
    async with client(rig) as c:
        for bad in (["1"], [True], "1", {"a": 1}):
            r = await c.put("/wa/v1/probes/adopt", json={"observed_ids": bad})
            assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_observed_ids"


async def test_http_adopt_unknown_id_is_404(rig):
    async with client(rig) as c:
        r = await c.put("/wa/v1/probes/adopt", json={"observed_ids": [4242]})
    assert r.status_code == 404 and r.json()["error"]["reason"] == "observed_id_not_found"


async def test_http_both_endpoints_are_audited(rig):
    async with client(rig) as c:
        await c.get("/wa/v1/probes?kind=observed")
        await c.put("/wa/v1/probes/adopt", json={"observed_ids": []})
    actions = {r["action"] for r in rig.db.query("SELECT action FROM wa_audit_log")}
    assert "probes.read.observed" in actions and "probes.adopt" in actions


async def test_http_sample_then_writeback_is_the_only_write_path(rig):
    """端到端:``mode:'sample'`` 不落库 → Agent 拿 ``rows`` 回写 → 只此一次计入 ``hits``。"""
    rig.probe.conns = [{"ip": "203.205.254.1", "port": 443, "hostname": "long.weixin.qq.com",
                        "channel": "wechat", "account_id": "wx01", "samples": 4}]
    async with client(rig, token=CONSOLE_TOKEN) as c:
        s = (await c.post("/wa/v1/probe", json={"mode": "sample", "pid_names": ["Weixin.exe"]})).json()
    assert rig.db.query("SELECT * FROM probe_targets_observed") == []
    rows = [{**r, "side": "windows"} for r in s["rows"]]
    async with client(rig) as c:
        await c.put("/wa/v1/probes", json={"kind": "observed", "rows": rows})
    assert rig.db.one("SELECT hits FROM probe_targets_observed")["hits"] == 4          # 不是 8
