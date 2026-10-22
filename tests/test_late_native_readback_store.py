"""Read-only exact-trace projection after timeout; no text/time search in reconciliation."""
import pytest
from tests.test_store_ingest import qd_msg

def test_late_readback_is_exact_and_does_not_rewrite_timeout(store,clock):
    store.ingest(qd_msg("late",ts_ms=clock(),ext=None,dir="out",self=True,state="SENDING",trace_id="late-T"))
    store.insert_command(trace_id="late-T",account_id="qd01",op="send_text",args_json='{"session":"415011447","text":"late"}',idempotency_key="late-key",confirm=True,timeout_ms=45000,transport="http",actor="synthetic-test",ip=None,now_ms=clock())
    store.finish_command(trace_id="late-T",ok=False,code="TIMEOUT",data={},cost_ms=45000,source="ui",error_message="timeout",retryable=False,needs_human=True,confirmed_by=None,confirm_ms=None,now_ms=clock())
    assert store.confirmed_outbound_for_trace("late-T") is None
    store.ingest(qd_msg("late",ts_ms=clock()+47000,ext="qd:late-proof",dir="out",self=True))
    before=store.con.total_changes
    row=store.confirmed_outbound_for_trace("late-T")
    assert row["ext_msg_id"]=="qd:late-proof" and row["confirmed_by"]=="ingest_merge"
    assert store.confirmed_outbound_for_trace("other-T") is None
    assert store.get_command_result("late-T")["code"]=="TIMEOUT"
    assert store.con.total_changes==before

def test_ambiguous_duplicate_trace_does_not_select_arbitrary_native(store,clock):
    for i in range(2):
        r=store.ingest(qd_msg("proof-"+str(i),ts_ms=clock(),ext="qd:"+str(i),dir="out",self=True,trace_id="duplicate"))
        store.con.execute("UPDATE messages SET confirmed_by='ingest_merge' WHERE id=?",(r.id,))
    assert store.confirmed_outbound_for_trace("duplicate") is None
