"""安全闸(02 §2.2.2 流水第四段 / §6 四件套 / 00 §11.3 [GATE] / 05 §2.5.5 sessions.allowlist、gates.custom):白名单、出口词表热更、自定义闸、GATE_BLOCKED 留痕。"""
from __future__ import annotations

import json

from qtrade_agent.gate import Gate
from qtrade_agent.models import Command, CommandOrigin
from tests.conftest import make_rig


def _cmd(text="早上好", session="qd01:415011447", key="k1", op="send_text"):
    return Command(account_id="qd01", op=op, args={"session": session, "text": text}, idempotency_key=key, origin=CommandOrigin())


async def _submit(rig, **kw):
    return await rig.agent.bus.submit(_cmd(**kw))


def _settings(rig, **s):
    rig.store.patch_account("qd01", settings_json=json.dumps(s, ensure_ascii=False))


def test_gate_unit_allowlist_blocklist_custom(rig3):
    st = rig3.store
    st.ensure_account("qd01", "qidian", state="running")
    g = Gate(st)
    row = st.get_account_full("qd01")
    assert g.check(row, "send_text", {"session": "qd01:1", "text": "hi"}) is None          # 默认 ["*"]
    assert g.check(row, "read_messages", {"session": "qd01:1"}) is None                    # 读不过闸
    _settings(rig3, sessions={"allowlist": ["415011447"]})
    row = st.get_account_full("qd01")
    assert g.check(row, "send_text", {"session": "qd01:415011447", "text": "hi"}) is None
    assert g.check(row, "send_text", {"session": "415011447", "text": "hi"}) is None
    e = g.check(row, "send_text", {"session": "qd01:999", "text": "hi"})
    assert e is not None and e.reason == "session_not_allowed" and e.details == [{"pointer": "/session"}]
    st.settings_set("gate.blocklist", ["成交", "报价"])                                         # 热更:无需重启
    e = g.check(row, "send_text", {"session": "415011447", "text": "今天成交了吗"})
    assert e is not None and e.reason == "blocklist_hit" and e.details[0]["pointer"] == "/text" and "成交" not in json.dumps(e.details)
    st.settings_set("gate.blocklist", [])
    assert g.check(row, "send_text", {"session": "415011447", "text": "今天成交了吗"}) is None
    _settings(rig3, gates={"custom": ["risk_a"]})
    row = st.get_account_full("qd01")
    assert g.check(row, "send_text", {"session": "x", "text": "hi"}).reason == "gate_not_registered"
    g.register("risk_a", lambda r, op, a: "买" not in a["text"])
    assert g.check(row, "send_text", {"session": "x", "text": "hi"}) is None
    assert g.check(row, "send_text", {"session": "x", "text": "买100"}).reason == "custom:risk_a"


async def test_bus_gate_blocked_leaves_trace_and_frees_idempotency(rig3):
    st = rig3.store
    st.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    _settings(rig3, sessions={"allowlist": ["111"]})
    res = await _submit(rig3, session="qd01:222", key="k1")
    assert res.ok is False and res.code == "GATE_BLOCKED" and res.error.reason == "session_not_allowed" and res.error.needs_human is True
    c = st.get_command(res.trace_id)
    assert c["status"] == "failed" and c["started_ms"] is None                          # 留痕:没进队列
    assert st.get_command_result(res.trace_id)["code"] == "GATE_BLOCKED"
    assert st.idem_get("qd01", "k1") is None                                            # 闸可配置:幂等行删掉,人改白名单后原键可重发
    assert st.con.execute("SELECT COUNT(*) FROM messages WHERE dir='out'").fetchone()[0] == 0   # 不写 SENDING 行
    ev = [e for e in st.list_events(event="command_done", account_id="qd01")]
    assert ev and ev[-1]["payload"]["code"] == "GATE_BLOCKED" and ev[-1]["payload"]["reason"] == "session_not_allowed"
    _settings(rig3, sessions={"allowlist": ["*"]})
    res2 = await _submit(rig3, session="qd01:222", key="k1")
    assert res2.code != "GATE_BLOCKED"                                                  # 同键重发能过闸(执行层未接 ⇒ SEND_FAILED)


async def test_bus_blocklist_hot_reload_and_order_after_idempotency(rig3):
    st = rig3.store
    st.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    st.settings_set("gate.blocklist", ["机密"])
    res = await _submit(rig3, text="这是机密文件", key="k2")
    assert res.code == "GATE_BLOCKED" and res.error.reason == "blocklist_hit"
    args = json.loads(st.get_command(res.trace_id)["args_json"])
    assert "text" not in args and args["text_len"] == 6                                   # 留痕不含正文(P-11)
    st.settings_set("gate.blocklist", [])
    res = await _submit(rig3, text="这是机密文件", key="k2")
    assert res.code != "GATE_BLOCKED"


async def test_login_gate_precedes_security_gate(rig3):
    st = rig3.store
    st.ensure_account("qd01", "qidian", state="login_required")
    _settings(rig3, sessions={"allowlist": ["111"]})
    res = await _submit(rig3, session="qd01:222", key="k3")
    assert res.code == "LOGIN_REQUIRED"                                                  # 登录门在前(02 §2.2.2 流水顺序)
