from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def case(rig3):
 a=rig3.agent
 a.store.ensure_account('qd01','qidian',state='running',self_uid='10001')
 a.store.upsert_api_client(app_id='qa-reset',name='synthetic',level='admin',token='synthetic-reset-token')
 with TestClient(a.create_api()) as client:
  client.headers['Authorization']='Bearer synthetic-reset-token'
  yield a,client

def test_missing_host_is_explicit_not_connected_not_transient_error(case):
 a,c=case;a.xunjia_v22_host=None
 r=c.get('/api/v1/accounts/qd01/session-reset')
 assert r.status_code==200,r.text
 d=r.json()['data'];assert d['available'] is False and d['connection_state']=='not_connected' and d['reset'] is None

def test_connected_idle_has_no_fabricated_operation(case):
 a,c=case
 a.xunjia_v22_host=SimpleNamespace(get_session_reset_status=lambda account:{'available':True,'account_id':account,'account_epoch':0,'reset':None})
 r=c.get('/api/v1/accounts/qd01/session-reset');assert r.status_code==200
 assert r.json()['data']=={'available':True,'account_id':'qd01','account_epoch':0,'reset':None}

def test_missing_binding_is_distinct_from_ledger_read_failure(case):
 a,c=case;a.xunjia_v22_host=SimpleNamespace(get_session_reset_status=lambda _:None)
 r=c.get('/api/v1/accounts/qd01/session-reset');assert r.status_code==200
 assert r.json()['data']['connection_state']=='account_not_bound'

def test_actual_ledger_failure_is_not_hidden(case):
 a,c=case
 def fail(_):raise OSError('synthetic ledger unavailable')
 a.xunjia_v22_host=SimpleNamespace(get_session_reset_status=fail)
 r=c.get('/api/v1/accounts/qd01/session-reset');assert r.status_code==503
 assert r.json()['error']['reason']=='session_reset_status_unavailable'

def test_unauthenticated_status_does_not_disclose_account(case):
 _,c=case;c.headers.pop('Authorization')
 assert c.get('/api/v1/accounts/qd01/session-reset').status_code==401

def test_unknown_account_still_rejected(case):
 _,c=case;assert c.get('/api/v1/accounts/not-owned/session-reset').status_code==404