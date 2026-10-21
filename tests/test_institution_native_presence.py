"""Passive native session observations never perform login or native sends."""
import asyncio,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from qtrade_agent.adapters.qidian.presence import NativePresenceProbe,classify_nodes
from qtrade_agent.adapters.qidian.ui import Node,load_profiles
from qtrade_agent.ossbox.institution import InstitutionCollectorHost
from relay.gateway_paths import InstitutionKeys
from relay.file_gateway import GatewayAuth

P=load_profiles()['default']
def node(rid='',text='',cls='android.widget.TextView',clickable=False):return Node(rid,text,'',cls,(0,0,100,100),clickable,0)

@pytest.mark.parametrize('text',['重新登录','重新登陆'])
def test_relogin_native_button_is_explicit_offline(text):
 assert classify_nodes(P,[node(text=text,cls='android.widget.Button',clickable=True)])[0]=='offline'

def test_chat_text_cannot_spoof_kick_or_relogin():
 n=[node('com.tencent.qidian:id/qdui_aio_inputbar_edittext'),node('com.tencent.qidian:id/qdui_aio_inputbar_send_btn'),node(text='重新登录')]
 assert classify_nodes(P,n)[0]=='online'

def test_native_password_and_login_controls_are_offline():
 assert classify_nodes(P,[node('com.tencent.qidian:id/password'),node('com.tencent.qidian:id/login')])==('offline','NATIVE_LOGIN_REQUIRED')

def test_unrecognized_ui_is_unknown_not_logged_out():
 assert classify_nodes(P,[])[0]=='unknown'

class FakeUI:
 def __init__(self,activity='SplashActivity',uid='10001',failure=False):self.profiles={'default':P};self.activity=activity;self.uid=uid;self.failure=failure;self.calls=[]
 async def _read_shell(self,a,c):
  self.calls.append(c)
  if self.failure:raise OSError('unavailable')
  assert c=='dumpsys window'
  return 'mCurrentFocus=Window{abc u0 com.tencent.qidian/com.tencent.mobileqq.activity.'+self.activity+'}'
 async def _dump_xml(self,a,read_only=False):
  assert read_only
  return '<hierarchy><node resource-id="com.tencent.qidian:id/recent_chat_list" bounds="[0,0][100,100]"/></hierarchy>'
 async def discover_self_uid(self,a,p):return self.uid

def row():return {'id':'qd01','self_uid':'10001','state':'running','channel':'qidian','enabled':True}
@pytest.mark.parametrize('activity,state',[('SplashActivity','online'),('LoginActivity','offline')])
def test_passive_probe_accepts_main_and_rejects_login(activity,state):
 ui=FakeUI(activity);r=asyncio.run(NativePresenceProbe(ui)(row()));assert r['state']==state
 assert all(c=='dumpsys window' for c in ui.calls)

def test_wrong_native_identity_is_not_current_account_online():
 r=asyncio.run(NativePresenceProbe(FakeUI(uid='10002'))(row()));assert r['state']=='offline' and r['code']=='NATIVE_ACCOUNT_CHANGED'

def test_read_failure_not_classified_as_kick():
 r=asyncio.run(NativePresenceProbe(FakeUI(failure=True))(row()));assert r['state']=='unknown' and 'KICK' not in r['code']

def test_timeout_does_not_renew_last_confirmed_online_lease(tmp_path):
 async def case():
  clock=[1800000000];answer={'state':'online','self_uid':'10001','code':'NATIVE_MAIN_VISIBLE'}
  async def probe(_):return dict(answer)
  class Store:
   def list_accounts(self,channel):return [row()]
  settings=SimpleNamespace(workbench_id='wb-a',view=lambda:{'workbench_name':'desk'},saved=lambda:None)
  m=InstitutionCollectorHost(store=Store(),bus=None,loop=asyncio.get_running_loop(),keys=InstitutionKeys.from_auth(GatewayAuth('synthetic',b'0123456789abcdef')),config={'workbench_name':'desk','contract':'its-relay-path-v1'},settings=settings,data_dir=tmp_path,legacy_inbox_disabled=lambda:True,friends_provider=lambda _:[],organization_provider=lambda _:None,clock=lambda:clock[0],presence_provider=probe)
  m.active=True
  assert not m.online('qd01','10001')
  await m.probe_presence_once();assert m.online('qd01','10001')
  answer.update(state='unknown',code='NATIVE_PRESENCE_UNAVAILABLE');clock[0]+=59
  await m.probe_presence_once();assert m.online('qd01','10001')
  clock[0]+=1;assert not m.online('qd01','10001')
  answer.update(state='online');await m.probe_presence_once();assert m.online('qd01','10001')
  answer.update(state='offline',code='NATIVE_RELOGIN_REQUIRED');await m.probe_presence_once();assert not m.online('qd01','10001')
  assert m.presence[('qd01','10001')]['code']=='NATIVE_RELOGIN_REQUIRED'
 asyncio.run(case())