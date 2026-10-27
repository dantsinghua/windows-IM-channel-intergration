"""Passive same-account recovery, never login/start/rekey or cross-account reuse."""
import pytest
from tests.test_wechat_rig import make_wechat_rig
from tests.test_integration_wiring_common import make_rig,close_rig

@pytest.mark.parametrize('field',['running','logged_in'])
@pytest.mark.parametrize('value',[None,'true',1])
async def test_missing_or_invalid_login_proof_is_unknown_not_logged_out(tmp_path,field,value):
    rig=make_wechat_rig(tmp_path)
    try:
        rig.fake.wechat[field]=value
        state,code,_=await rig.adapter.probe_state(rig.account())
        assert (state,code)==('degraded','WECHAT_READ_FAILED')
        assert not rig.adapter.key_retry_required('wx01')
    finally:rig.store.close()

@pytest.mark.parametrize('wxid',[None,'wxid_other'])
async def test_passive_recovery_requires_exact_native_identity(tmp_path,wxid):
    rig=make_wechat_rig(tmp_path)
    try:
        rig.fake.wechat['wxid']=wxid
        state,code,_=await rig.adapter.probe_state(rig.account(),require_identity=True)
        assert (state,code)==('degraded','WECHAT_READ_FAILED')
    finally:rig.store.close()

def offline(rig,*,code='LOGGED_OUT'):
    rig.store.ensure_account('wx01','wechat',state='running',self_uid=rig.wechat.wechat['wxid'])
    rig.agent.accounts.transition('wx01','login_required',state_code=code,desired_state='running')

async def test_existing_same_account_login_recovers_without_start_login_or_key_retry(tmp_path,monkeypatch):
    rig=make_rig(tmp_path)
    try:
        offline(rig)
        adapter=rig.agent.adapters['wechat'];calls=[]
        original=adapter.probe_state
        async def probe(*args,**kwargs):
            calls.append(kwargs);return await original(*args,**kwargs)
        monkeypatch.setattr(adapter,'probe_state',probe)
        original_message_count=rig.store.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        await rig.agent.healthloop.check_wechat()
        assert calls==[{'require_identity':True}]
        assert rig.store.get_account_full('wx01')['state']=='running'
        states=[e['payload']['state'] for e in rig.events_of('account_state')]
        assert states[-2:]==['logging_in','running']
        assert rig.wechat.key_retry_calls==0
        assert rig.store.con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==original_message_count
    finally:close_rig(rig)

@pytest.mark.parametrize('case',['other_identity','missing_identity','missing_probe','offline','bad_key','read_stopped','locked'])
async def test_unproven_recovery_keeps_login_required_and_never_rekeys(tmp_path,case):
    rig=make_rig(tmp_path)
    try:
        offline(rig)
        if case=='other_identity':rig.wechat.wechat['wxid']='wxid_other'
        elif case=='missing_identity':rig.wechat.wechat['wxid']=None
        elif case=='missing_probe':rig.wechat.wechat.pop('logged_in')
        elif case=='offline':rig.wechat.wechat['logged_in']=False
        elif case=='bad_key':rig.wechat.chatlog['key_ok']=False
        elif case=='read_stopped':rig.wechat.chatlog['running']=False
        elif case=='locked':rig.wechat.screen_locked=True
        await rig.agent.healthloop.check_wechat()
        row=rig.store.get_account_full('wx01')
        assert (row['state'],row['state_code'])==('login_required','LOGGED_OUT')
        assert rig.wechat.key_retry_calls==0
    finally:close_rig(rig)

@pytest.mark.parametrize('case',['stopped','disabled','deleted','auto_recover_disabled','first_login','key_ritual'])
async def test_passive_probe_does_not_resurrect_retired_or_unfinished_login(tmp_path,monkeypatch,case):
    rig=make_rig(tmp_path)
    try:
        offline(rig)
        if case=='stopped':rig.agent.accounts.transition('wx01','stopped',desired_state='stopped')
        elif case=='disabled':rig.agent.accounts.transition('wx01','login_required',state_code='LOGGED_OUT',enabled=False)
        elif case=='deleted':rig.agent.accounts.transition('wx01','stopped',deleted_ms=rig.clock(),desired_state='stopped')
        elif case=='auto_recover_disabled':rig.store.con.execute("UPDATE accounts SET auto_recover=0 WHERE id='wx01'")
        elif case=='first_login':rig.agent.accounts.transition('wx01','login_required',state_code='WAIT_QRCODE')
        elif case=='key_ritual':rig.agent.accounts.transition('wx01','login_required',state_code='WAIT_KEY_IMG')
        calls=[]
        async def forbidden(*args,**kwargs):calls.append(1);raise AssertionError('unexpected recovery probe')
        monkeypatch.setattr(rig.agent.adapters['wechat'],'probe_state',forbidden)
        await rig.agent.healthloop.check_wechat()
        assert calls==[]
        assert rig.store.get_account_full('wx01')['state']!='running'
    finally:close_rig(rig)

async def test_manual_stop_during_probe_wins_over_late_recovery(tmp_path,monkeypatch):
    rig=make_rig(tmp_path)
    try:
        offline(rig)
        async def raced(*args,**kwargs):
            rig.agent.accounts.transition('wx01','stopped',desired_state='stopped')
            return 'running',None,''
        monkeypatch.setattr(rig.agent.adapters['wechat'],'probe_state',raced)
        await rig.agent.healthloop.check_wechat()
        assert rig.store.get_account_full('wx01')['state']=='stopped'
    finally:close_rig(rig)


async def test_disabling_auto_recovery_during_probe_wins(tmp_path,monkeypatch):
    rig=make_rig(tmp_path)
    try:
        offline(rig)
        async def raced(*args,**kwargs):
            rig.store.con.execute("UPDATE accounts SET auto_recover=0 WHERE id='wx01'")
            return 'running',None,''
        monkeypatch.setattr(rig.agent.adapters['wechat'],'probe_state',raced)
        await rig.agent.healthloop.check_wechat()
        assert rig.store.get_account_full('wx01')['state']=='login_required'
    finally:close_rig(rig)
