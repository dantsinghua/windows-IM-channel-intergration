"""poll_maindb / check_group_gaps —— 逐分支对照 docs/06 §2.9.5 伪代码与 §8b 的 M2 验收行。"""
from __future__ import annotations

import json

import pytest

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.poll import QidianAccountView, QidianPoller
from qtrade_agent.alerts import QIDIAN_DB_UNAVAILABLE, QIDIAN_MSG_GAP, QIDIAN_TABLE_DECODE_STUCK, Alerts
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Message, Session

PEER = "415011447"
GROUP = "123456"


@pytest.fixture
def env(store, clock, maindb):
    events = Events(store)
    alerts = Alerts(events, clock=clock)
    h13 = {"firing": False}
    poller = QidianPoller(store=store, events=events, alerts=alerts, cfg=AgentConfig(), h13_firing=lambda: h13["firing"],
                          clock=clock, maindb_factory=lambda uid: LocalSqliteMainDb(maindb.path if uid == maindb.self_uin else maindb.path + ".missing"))
    return {"store": store, "clock": clock, "maindb": maindb, "events": events, "alerts": alerts, "poller": poller, "h13": h13}


def acct(state="running", uid="3007373675"):
    return QidianAccountView("qd01", state, uid)


def msg_events(store):
    return store.list_events("message", "qd01")


def test_idle_before_login_no_alert_no_cursor(env):
    p = env["poller"]
    p.poll_maindb(acct(state="login_required"))
    p.poll_maindb(acct(uid=None))
    assert env["store"].cursor_get("qd01", "qidian_bootstrap") is None
    assert not env["alerts"].active and env["store"].list_events("alert") == []


def test_first_bootstrap_history_gate_and_new_session(env):
    st, clk, db, p = env["store"], env["clock"], env["maindb"], env["poller"]
    # 首登:库里已有大量「历史」(time 早于 bootstrap 时刻 − 120 s)
    for i in range(5):
        db.insert_text(PEER, f"历史{i}", time_s=clk.now_s - 3600 - i)
    db.insert_text(GROUP, "群历史", time_s=clk.now_s - 7200, group=True)
    p.poll_maindb(acct())
    assert st.count_messages("qd01") == 0 and msg_events(st) == []
    boot = st.cursor_get("qd01", "qidian_bootstrap")
    assert boot.value == "3007373675" and boot.value_int == clk()
    assert st.cursor_get("qd01", f"qidian_rowid:{PEER}").value_int == 5              # 首次 = MAX(_id)
    assert st.cursor_get("qd01", f"qidian_rowid:g_{GROUP}").value_int == 1
    # 漫游历史后到:_id 更大但 time 更早 ⇒ 历史闸挡住,水位照常越过
    db.insert_text(PEER, "后到的历史", time_s=clk.now_s - 3000)
    p.poll_maindb(acct())
    assert st.count_messages("qd01") == 0 and st.cursor_get("qd01", f"qidian_rowid:{PEER}").value_int == 6
    # 真新消息入库 + 事件(late=false)
    clk.advance(8000)
    db.insert_text(PEER, "1Y 1.70?", time_s=clk.now_s - 6)
    p.poll_maindb(acct())
    assert st.count_messages("qd01") == 1
    ev = msg_events(st)
    assert len(ev) == 1 and ev[0]["payload"]["text"] == "1Y 1.70?" and ev[0]["payload"]["late"] is False and ev[0]["payload"]["lag_s"] == 6
    assert "origin" not in ev[0]["payload"]                                             # 入向不带 origin
    # bootstrap 之后的新会话:从 0 起、第一条不丢
    db.insert_text("999888", "第一条", time_s=clk.now_s)
    p.poll_maindb(acct())
    assert st.count_messages("qd01") == 2 and st.cursor_get("qd01", "qidian_rowid:999888").value_int == 1
    assert len(st.cursors_list("qd01", "qidian_bootstrap")) == 1


def test_rescan_after_rebuild_does_not_replay(env):
    st, clk, db, p = env["store"], env["clock"], env["maindb"], env["poller"]
    p.poll_maindb(acct())
    db.insert_text(PEER, "a", time_s=clk.now_s)
    db.insert_text(PEER, "b", time_s=clk.now_s)
    p.poll_maindb(acct())
    assert st.count_messages("qd01") == 2 and len(msg_events(st)) == 2
    # 篡改 last_uniseq → 水位自检不过 → 本表从 0 重扫:行数不变、无新事件、游标改回真实值
    cur = st.cursor_get("qd01", f"qidian_rowid:{PEER}")
    st.cursor_set("qd01", cur.kind, cur.value_int, json.dumps({"last_uniseq": 424242}))
    p.poll_maindb(acct())
    assert st.count_messages("qd01") == 2 and len(msg_events(st)) == 2
    assert st.cursor_get("qd01", cur.kind).value_json()["last_uniseq"] != 424242


def test_kicked_relogin_late_and_external_origin(env):
    st, clk, db, p = env["store"], env["clock"], env["maindb"], env["poller"]
    p.poll_maindb(acct())
    p.poll_maindb(acct(state="login_required"))          # 被踢:空转、重置内存态、不告警
    assert p.state_of("qd01").table_map == {}
    clk.advance(2 * 3600_000)
    # 离线期间对端发的私聊补同步进来(原始 time),人在别的端回了一条(issend=1)
    db.insert_text(PEER, "在吗", time_s=clk.now_s - 5400)
    db.insert_text(PEER, "在的", time_s=clk.now_s - 5000, issend=1)
    p.poll_maindb(acct())
    ev = msg_events(st)
    assert len(ev) == 2 and all(e["payload"]["late"] is True for e in ev)
    ext = [e for e in ev if e["payload"]["dir"] == "out"][0]
    assert ext["payload"]["origin"] == "external" and st.get_message(ext["payload"]["id"])["trace_id"] is None
    assert [e for e in ev if e["payload"]["dir"] == "in"][0]["payload"].get("origin") is None
    assert not env["alerts"].active


def test_out_merge_via_poll_marks_origin_rpa(env):
    st, clk, db, p = env["store"], env["clock"], env["maindb"], env["poller"]
    db.insert_text(PEER, "旧会话里的历史", time_s=clk.now_s - 3600)   # 会话表已存在,全量轮建好映射(首次向新会话发送时加速轮查不到表,属规格)
    p.poll_maindb(acct())
    out = Message(account_id="qd01", channel="qidian", session=Session("qd01", PEER, "private", PEER), dir="out", type="text", text="收到",
                  ts_ms=clk(), source="ui", sender_id="3007373675", self=True, state="SENDING", trace_id="T1", idempotency_key="k1")
    r = st.ingest(out)
    clk.advance(9000)
    db.insert_text(PEER, "收到", time_s=clk.now_s, issend=1)
    p.poll_maindb(acct(), only_sessions=[PEER])            # 加速轮:只查目标表
    row = st.get_message(r.id)
    assert row["state"] == "DELIVERED" and row["confirmed_by"] == "ingest_merge" and row["ext_msg_id"].startswith("qd:")
    ev = msg_events(st)
    assert len(ev) == 1 and ev[0]["payload"]["origin"] == "rpa" and ev[0]["payload"]["state"] == "DELIVERED"


def test_accelerated_round_does_not_touch_fail_counters(env):
    st, clk, db, p, alerts = env["store"], env["clock"], env["maindb"], env["poller"], env["alerts"]
    p.poll_maindb(acct())
    # 指向不存在的库:全量轮连续 3 轮 → QIDIAN_DB_UNAVAILABLE(not_found,已见过主库 ⇒ 3 轮)
    missing = acct(uid="0000")
    p.states["qd01"].maindb_seen = True
    for _ in range(2):
        p.poll_maindb(missing)
        p.poll_maindb(missing, only_sessions=[PEER])      # 加速轮不清零、不 resolve
    assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    p.poll_maindb(missing)
    assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    ev = [e for e in st.list_events("alert") if e["payload"]["code"] == QIDIAN_DB_UNAVAILABLE][-1]
    assert ev["payload"]["evidence"]["reason"] == "not_found" and ev["payload"]["severity"] == "warn"
    p.poll_maindb(missing, only_sessions=[PEER])
    assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    # 恢复:全量轮成功即 resolved
    p.poll_maindb(acct())
    assert not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    assert [e["payload"]["state"] for e in st.list_events("alert")][-2:] == ["firing", "resolved"]


def test_first_login_not_found_grace_is_12_rounds(env):
    p, alerts = env["poller"], env["alerts"]
    for i in range(11):
        p.poll_maindb(acct(uid="0000"))
    assert not alerts.active                                              # 建库期不误告
    p.poll_maindb(acct(uid="0000"))
    assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")


def test_h13_blocks_bootstrap_and_relogin_switch_uin_audits_once(env):
    st, clk, db, p, alerts, h13 = env["store"], env["clock"], env["maindb"], env["poller"], env["alerts"], env["h13"]
    p.poll_maindb(acct())
    st.cursor_set("qd01", f"qidian_rowid:{PEER}", 3, json.dumps({"last_uniseq": 1}))
    # 换号 + H13 firing:不删水位、不建基准、不审计;12 轮后 clock_unsynced 告警
    h13["firing"] = True
    st.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    # 让「新 uin」的库存在:把同一文件当新号的库
    p.maindb_factory = lambda uid: LocalSqliteMainDb(db.path)
    for _ in range(12):
        p.poll_maindb(acct(uid="777"))
    assert st.cursor_get("qd01", "qidian_bootstrap").value == "3007373675"
    assert st.cursor_get("qd01", f"qidian_rowid:{PEER}") is not None and st.list_audit("qidian.rebootstrap") == []
    assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    assert st.list_events("alert")[-1]["payload"]["evidence"]["reason"] == "clock_unsynced"
    # 撤掉开关:下一轮建基准、旧水位此时才删且只 audit 一次、告警 resolved
    h13["firing"] = False
    p.poll_maindb(acct(uid="777"))
    assert st.cursor_get("qd01", "qidian_bootstrap").value == "777"
    assert len(st.list_audit("qidian.rebootstrap")) == 1 and not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    p.poll_maindb(acct(uid="777"))
    assert len(st.list_audit("qidian.rebootstrap")) == 1


def test_single_table_stuck_uses_independent_code(env, maindb):
    import sqlite3
    st, clk, db, p, alerts = env["store"], env["clock"], env["maindb"], env["poller"], env["alerts"]
    db.insert_text(PEER, "ok", time_s=clk.now_s)
    # 一张表名 MD5 与内容对不上(自检不过)
    con = sqlite3.connect(db.path)
    con.execute('CREATE TABLE "mr_friend_00000000000000000000000000000000_New" AS SELECT * FROM "%s"' % __import__("tests.conftest", fromlist=["table_name"]).table_name(PEER, False))
    con.commit(); con.close()
    for _ in range(11):
        p.poll_maindb(acct())
    assert not alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
    p.poll_maindb(acct())
    assert alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01") and not alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01")
    assert st.list_events("alert")[-1]["payload"]["evidence"]["tables"] == ["mr_friend_00000000000000000000000000000000_New"]
    # 其余表照常读
    db.insert_text(PEER, "still", time_s=clk.now_s)
    p.poll_maindb(acct())
    assert st.count_messages("qd01") >= 1
    # 该表被删后下一个全量轮 resolved
    con = sqlite3.connect(db.path); con.execute('DROP TABLE "mr_friend_00000000000000000000000000000000_New"'); con.commit(); con.close()
    p.poll_maindb(acct())
    assert not alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")


def test_all_tables_undecodable_is_one_alert(env, maindb):
    import sqlite3
    st, clk, db, p, alerts = env["store"], env["clock"], env["maindb"], env["poller"], env["alerts"]
    con = sqlite3.connect(db.path)
    con.execute('CREATE TABLE "mr_friend_00000000000000000000000000000000_New" (_id INTEGER PRIMARY KEY, issend INTEGER, istroop INTEGER, time INTEGER, msgtype INTEGER, uniseq INTEGER, msgseq INTEGER, shmsgseq INTEGER, senderuin BLOB, frienduin BLOB, msgData BLOB)')
    con.execute('INSERT INTO "mr_friend_00000000000000000000000000000000_New" VALUES (1,0,0,1,-1000,1,1,1,x\'00\',x\'00\',x\'00\')')
    con.commit(); con.close()
    for _ in range(13):
        p.poll_maindb(acct())
    assert alerts.is_firing(QIDIAN_DB_UNAVAILABLE, "account:qd01") and not alerts.is_firing(QIDIAN_TABLE_DECODE_STUCK, "account:qd01")
    assert st.list_events("alert")[-1]["payload"]["evidence"]["reason"] == "decode_failed"


def test_check_group_gaps(env):
    st, clk, db, p, alerts = env["store"], env["clock"], env["maindb"], env["poller"], env["alerts"]
    # 群里 shmsgseq 1..10 再跳到 31..35(缺 20 条,正对离线时段);私聊缺口再大也不算
    for i in range(1, 11):
        db.insert_text(GROUP, f"g{i}", time_s=clk.now_s - 1000 + i, group=True, shmsgseq=i)
    for i in range(31, 36):
        db.insert_text(GROUP, f"g{i}", time_s=clk.now_s - 100 + i, group=True, shmsgseq=i)
    db.insert_text(PEER, "p", time_s=clk.now_s, shmsgseq=1)
    db.insert_text(PEER, "p2", time_s=clk.now_s, shmsgseq=99999)
    p.check_group_gaps(acct())                     # 全量轮还没重建映射:不产出
    assert not alerts.active
    p.poll_maindb(acct())
    p.check_group_gaps(acct())
    assert alerts.is_firing(QIDIAN_MSG_GAP, "account:qd01")
    ev = st.list_events("alert")[-1]["payload"]
    assert ev["evidence"]["sessions"][0]["session_id"] == f"qd01:g_{GROUP}" and ev["evidence"]["sessions"][0]["missing"] == 20
    assert all(not s["session_id"].endswith(PEER) for s in ev["evidence"]["sessions"])
    # 窗口滑过(3 天后)自动 resolved
    clk.advance(4 * 86400_000)
    p.check_group_gaps(acct())
    assert not alerts.is_firing(QIDIAN_MSG_GAP, "account:qd01")
