"""Current collector fresh-context controls; local ledgers, no network/native send."""
import asyncio
from types import SimpleNamespace
import pytest
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from server.receiver_ledger import ReceiverLedger
from relay.protocol import Binding,ProtocolError
from relay.gateway_paths import CONTRACT


def test_signed_account_context_activates_receiver_and_publishes_ack(tmp_path):
    binding=Binding('synthetic-owner','10001','synthetic-wb',b'a'*16,b'b'*32)
    ledger=ReceiverLedger(tmp_path/'receiver.db',binding,clock=lambda:1800000000)
    messages=[];sync=[];loop=asyncio.new_event_loop()
    host=InstitutionCollectorHost.__new__(InstitutionCollectorHost)
    host.loop=loop;host._contacts_wake=asyncio.Event()
    channel=SimpleNamespace(store=SimpleNamespace(source_highwater=12),publish_manifest=messages.append)
    account=SimpleNamespace(ledger=ledger,bridge=SimpleNamespace(adapters={'oss':channel}))
    host.hosts={('qd1','10001'):account};host.publisher=SimpleNamespace(request_sync=lambda uid,org:sync.append((uid,org)))
    data={'payload':{'contract':CONTRACT,'op':'activate_context','context_id':'fresh-c1','context_version':1800000000000000}}
    try:
        host._sync_request('10001',data)
        with ledger._db() as db:
            assert db.execute('SELECT context_id FROM rx_context').fetchone()[0]=='fresh-c1'
        assert messages[0]['op']=='context_ready' and messages[0]['context_id']=='fresh-c1'
        assert messages[0]['source_highwater']==12 and sync==[('10001',False)]
        host._sync_request('10001',data)
        assert len(messages)==2 and messages[1]==messages[0]
        with pytest.raises(ProtocolError,match='NOT_READY'):host._sync_request('other-account',data)
        assert len(messages)==2
    finally:loop.close()
