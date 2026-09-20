"""E-19 内存水位(02 §2.2.5 / §5;05 §2.5.2;04 [monitor] mem_warn_mb/mem_critical_mb;告警 MEM_PRESSURE):三级判定、LRU 建议、只建议不自动停、#2/恢复 409。"""
from __future__ import annotations

import json

from starlette.testclient import TestClient

from qtrade_agent.alerts import MEM_PRESSURE
from qtrade_agent.models import Message, Session
from tests.conftest import make_rig


def _acct(rig, id, state="running", auto_stop=False):
    rig.store.ensure_account(id, "qidian", state=state, self_uid="3007373675")
    rig.store.upsert_runtime(id, kind="redroid", desired_state="running")
    if auto_stop:
        rig.store.patch_account(id, settings_json=json.dumps({"auto_stop_on_pressure": True}))


def _msg(rig, id, ts):
    rig.store.ingest(Message(account_id=id, channel="qidian", session=Session(id, "1", "private"), dir="in", type="text", text="x", ts_ms=ts,
                             source="qidian_db", ext_msg_id=f"qd:{ts}", sender_id="1"), now_ms=ts)


def test_levels_alert_and_lru_order(rig3):
    pr, alerts, st, clock = rig3.agent.pressure, rig3.agent.alerts, rig3.store, rig3.clock
    _acct(rig3, "qd01"); _acct(rig3, "qd02"); _acct(rig3, "qd03", state="stopped")
    _msg(rig3, "qd01", clock.now_ms - 60_000)
    assert pr.evaluate(6100) == "ok" and not alerts.active and pr.blocked() is False
    assert pr.evaluate(1900) == "warn" and alerts.active[(MEM_PRESSURE, "host")].severity == "warn" and pr.blocked() is False
    assert pr.evaluate(900) == "critical" and alerts.active[(MEM_PRESSURE, "host")].severity == "crit" and pr.blocked() is True
    ev = alerts.active[(MEM_PRESSURE, "host")].evidence
    assert ev["avail_mb"] == 900 and ev["level"] == "critical"
    assert [x["account_id"] for x in ev["lru_suggest"]] == ["qd02", "qd01"]         # 从未收发的 qd02 排最前;stopped 的不进名单
    assert ev["lru_suggest"][0]["last_active_at"] is None and ev["lru_suggest"][1]["last_active_at"].endswith("+08:00")
    assert set(ev["lru_suggest"][0]) == {"account_id", "last_active_at", "rss_mb"}
    assert pr.evaluate(2500) == "ok" and not alerts.is_firing(MEM_PRESSURE, "host")
    sev = [e["payload"]["severity"] + ":" + e["payload"]["state"] for e in st.list_events(event="resource") if e["payload"]["code"] == MEM_PRESSURE]   # 02 §3.7:MEM_PRESSURE 事件族 resource
    assert sev == ["warn:firing", "crit:firing", "crit:resolved"]
    assert pr.evaluate(None) == "unknown" and pr.blocked() is False


async def test_enforce_only_stops_opted_in_accounts_and_audits(rig3):
    pr, st = rig3.agent.pressure, rig3.store
    _acct(rig3, "qd01", auto_stop=True); _acct(rig3, "qd02"); _acct(rig3, "qd03", auto_stop=True)
    _msg(rig3, "qd03", rig3.clock.now_ms)                                              # qd03 最近有收发,qd01 更久
    pr.evaluate(900)
    avail = {"v": 900}
    stopped = await pr.enforce(lambda: avail["v"])
    assert stopped == ["qd01", "qd03"]                                                 # 按 LRU 顺序;qd02 未开开关不停
    assert st.get_account_full("qd02")["state"] == "running"
    assert [r["account_id"] for r in st.list_audit(action="pool.auto_stop_on_pressure")] == ["qd01", "qd03"]
    pr.evaluate(900)
    avail["v"] = 900
    _acct(rig3, "qd04", auto_stop=True); _acct(rig3, "qd05", auto_stop=True)
    avail_seq = iter([900, 2100])                                                      # R6-57 ④:每次 stop 前先读

    def read():
        return next(avail_seq, 2100)
    stopped = await pr.enforce(read)
    assert stopped == ["qd04"] and pr.level == "ok"                                   # 停一个后回到 ≥ mem_warn_mb 即止
    pr.evaluate(900)
    assert await pr.enforce(lambda: 2100) == []                                        # 停前已回到线上:一个都不停


def test_create_and_recover_blocked_under_critical(rig3):
    st, pr, agent = rig3.store, rig3.agent.pressure, rig3.agent
    st.upsert_api_client(app_id="w", name="w", level="write", token="tw")
    st.create_account(channel="qidian", label="一", login_mode="password", quota_mb=2560)      # 走正规分配(seq.qidian=1),不与后续 POST 撞 seq
    st.transition("qd01", "running", desired_state="running")
    pr.evaluate(900)
    with TestClient(agent.create_api(), client=("127.0.0.1", 4)) as c:
        r = c.post("/api/v1/accounts", json={"channel": "qidian", "label": "x", "idempotency_key": "k"}, headers={"Authorization": "Bearer tw"})
        assert r.status_code == 409 and r.json()["code"] == "RESOURCE_EXHAUSTED" and r.json()["error"]["reason"] == "mem_pressure"
        assert r.json()["error"]["alternatives"] == [{"account_id": "qd01", "last_active_at": None, "rss_mb": None}]
        assert st.settings_get("seq.qidian") == 1                                                   # 水位 409 不消耗序号
        st.transition("qd01", "stopped", desired_state="running")
        res = c.portal.call(agent.accounts.recover)
        assert res == [{"id": "qd01", "ok": False, "reason": "mem_pressure"}] and st.get_account_full("qd01")["state"] == "stopped"
        pr.evaluate(3000)
        assert c.post("/api/v1/accounts", json={"channel": "qidian", "label": "x", "idempotency_key": "k"}, headers={"Authorization": "Bearer tw"}).status_code == 201


async def test_winagent_probe_feeds_watermark(tmp_path):
    rig = make_rig(tmp_path)
    rig.winagent.host["available_mb"] = 800
    await rig.agent.winagent_probe()
    assert rig.agent.pressure.level == "critical" and rig.agent.alerts.is_firing(MEM_PRESSURE, "host")
    rig.winagent.host["available_mb"] = 6000
    await rig.agent.winagent_probe()
    assert rig.agent.pressure.level == "ok"
    rig.winagent.offline = True
    for _ in range(3):
        await rig.agent.winagent_probe()
    assert rig.agent.pressure.level == "unknown"
    rig.store.close()
