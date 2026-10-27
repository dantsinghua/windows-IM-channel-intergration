"""Native commit and capture recovery source share one transaction, independently of WS."""
import json
import sqlite3

import pytest

from qtrade_agent.events import Events
from qtrade_agent.models import Message,Session
from qtrade_agent.store import Store
from test_wechat_capture_contract_20261027 import GROUP,NOW,UID,producer_fixture,ready,checkpoint,embedded_dicts


def message(*,aid="wx94",channel="wechat",source="chatlog",seq=1,text=""):
    return Message(account_id=aid,channel=channel,session=Session(aid,GROUP,"group"),
        dir="out",type="image",text=text,ts_ms=int(NOW*1000),source=source,
        ext_msg_id=GROUP+":"+str(seq),self=True,state="DELIVERED")


def test_native_commit_is_capturable_even_when_ws_emit_never_happened(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock)
        before_ws=[tuple(r) for r in store.con.execute("SELECT * FROM events_outbox")]
        msg=message();store.ingest(msg,now_ms=int(NOW*1000))
        # Deliberately no Events.emit: this is the old crash gap.
        producer.tick()
        facts=[p for p,_ in adapter.published if p["op"]=="capture_fact"]
        assert len(facts)==1 and facts[0]["event"]["ext_msg_id"]==msg.ext_msg_id
        assert [tuple(r) for r in store.con.execute("SELECT * FROM events_outbox")]==before_ws
    finally:store.close()


def test_native_source_write_failure_rolls_back_message_and_session_transaction(tmp_path):
    store=Store(":memory:",clock=lambda:int(NOW*1000)).open()
    store.ensure_account("wx94","wechat",state="running",self_uid=UID)
    try:
        assert store.con.execute("SELECT 1 FROM sqlite_master WHERE name='native_capture_source'").fetchone(), \
            "Capture recovery storage must be initialized by Store itself"
        with store._tx() as db:
            db.execute("CREATE TRIGGER fail_native_source BEFORE INSERT ON native_capture_source BEGIN SELECT RAISE(ABORT,'synthetic source commit failure'); END")
        before={name:[tuple(r) for r in store.con.execute("SELECT * FROM "+name)]
                for name in ("messages","sessions","events_outbox","commands")}
        with pytest.raises(sqlite3.DatabaseError):store.ingest(message(),now_ms=int(NOW*1000))
        after={name:[tuple(r) for r in store.con.execute("SELECT * FROM "+name)] for name in before}
        assert after==before
        assert store.capture_sources(0,100)==[]
    finally:store.close()


def test_duplicate_native_input_does_not_append_another_capture_source(tmp_path):
    store=Store(":memory:",clock=lambda:int(NOW*1000)).open()
    store.ensure_account("wx94","wechat",state="running",self_uid=UID)
    try:
        store.ingest(message(),now_ms=int(NOW*1000));first=store.capture_sources(0,100)
        store.ingest(message(),now_ms=int(NOW*1000)+1);second=store.capture_sources(0,100)
        assert len(first)==1 and second==first
        row=first[0]
        assert row["account_id"]=="wx94" and row["native_uid"]==UID and row["identity_epoch"]==0
        assert json.loads(row["payload_json"])
        assert store.capture_source_bounds()==(row["seq"],row["seq"])
    finally:store.close()


@pytest.mark.parametrize("channel,aid,source",[("wechat","wx94","ui"),("qq","qq94","onebot"),("qidian","qd94","qidian_db")])
def test_non_native_wechat_sources_do_not_enter_the_capture_source_table(tmp_path,channel,aid,source):
    store=Store(":memory:",clock=lambda:int(NOW*1000)).open()
    store.ensure_account(aid,channel,state="running",self_uid=UID)
    try:
        store.ingest(message(aid=aid,channel=channel,source=source),now_ms=int(NOW*1000))
        assert store.capture_sources(0,100)==[]
    finally:store.close()


def test_media_completion_appends_source_atomically_without_changing_ws_or_send_state(tmp_path):
    store=Store(":memory:",clock=lambda:int(NOW*1000)).open()
    store.ensure_account("wx94","wechat",state="running",self_uid=UID)
    try:
        msg=message();store.ingest(msg,now_ms=int(NOW*1000));initial=store.capture_sources(0,100)
        before_ws=[tuple(r) for r in store.con.execute("SELECT * FROM events_outbox")]
        before=dict(store.get_message(msg.id))
        media=[{"kind":"image","wa_path":"/wa/v1/wechat/media/"+"ab"*16}]
        assert store.complete_native_media(msg.id,account_id="wx94",expected_uid=UID,expected_epoch=0,
            expected_ext=msg.ext_msg_id,media=media,expected_media_json="[]")
        sources=store.capture_sources(0,100)
        assert len(sources)==len(initial)+1 and sources[-1]["message_id"]==msg.id
        assert sources[-1]["seq"]>initial[-1]["seq"]
        after=dict(store.get_message(msg.id))
        assert {k:v for k,v in after.items() if k!="media_json"}=={k:v for k,v in before.items() if k!="media_json"}
        assert [tuple(r) for r in store.con.execute("SELECT * FROM events_outbox")]==before_ws
        assert not store.complete_native_media(msg.id,account_id="wx94",expected_uid=UID,expected_epoch=0,
            expected_ext=msg.ext_msg_id,media=media,expected_media_json="[]")
        assert store.capture_sources(0,100)==sources
    finally:store.close()


def test_capture_source_counter_is_not_compared_with_ws_epoch_watermark(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        store.settings_set("relay.identity.wx94",{"uid":UID,"epoch":1,"outbox_seq":9999})
        ready(producer,adapter,g,clock)
        msg=message();store.ingest(msg,now_ms=int(NOW*1000));producer.tick()
        sources=store.capture_sources(0,100)
        assert len(sources)==1 and sources[0]["seq"]<9999 and sources[0]["identity_epoch"]==1
        facts=[p for p,_ in adapter.published if p["op"]=="capture_fact"]
        assert len(facts)==1 and facts[0]["event"]["source_seq"]==sources[0]["seq"]
    finally:store.close()


def test_committed_old_slot_source_is_preserved_in_quarantine_before_cursor_crosses_it(tmp_path):
    producer,store,adapter,g,clock=producer_fixture(tmp_path)
    try:
        ready(producer,adapter,g,clock)
        msg=message();store.ingest(msg,now_ms=int(NOW*1000))
        [source]=store.capture_sources(0,100)
        original=dict(store.get_message(msg.id))
        store.set_account_state("wx94","stopped")
        store.ensure_account("wx95","wechat",state="running",self_uid=UID)
        producer.tick()
        assert checkpoint(producer)>=source["seq"]
        rejected=producer.rejected_sources()
        assert any(row["source_seq"]==source["seq"] for row in rejected), \
            "A same-UID slot change must not silently discard an already committed source"
        snapshots=list(embedded_dicts(rejected))
        assert any(value.get("account_id")=="wx94" and value.get("id")==msg.id for value in snapshots)
        assert dict(store.get_message(msg.id))==original
        assert not any(p["op"] in ("capture_fact","capture_media") for p,_ in adapter.published), \
            "Recovery must not invent a verified message in the new slot"
    finally:store.close()
