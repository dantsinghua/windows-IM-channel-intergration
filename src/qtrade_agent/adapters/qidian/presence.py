"""Passive native presence observation; no launch, login, taps or identity changes."""
from __future__ import annotations
import asyncio
import re
import time
from ..base import Account
from .ui import pick_profile,parse_ui_xml,find_node,_FOCUS_RE,_postlogin_activity_visible


def classify_nodes(profile, nodes):
    """Only native controls determine logout; chat text alone is not evidence."""
    def ids(key):
        a=profile.nodes.get(key,{})
        return {v for v in [a.get('id'),*a.get('id_any',[])] if isinstance(v,str) and v.startswith(profile.package+':id/')}
    password=any(n.rid in ids('login_password') for n in nodes)
    submit=any(n.rid in ids('login_submit') for n in nodes)
    if password and submit:return 'offline','NATIVE_LOGIN_REQUIRED'
    relogin=profile.anchor('relogin_button').get('text_any',[])
    if any(n.clickable and ('Button' in n.cls or n.rid in (profile.package+':id/dialogRightBtn',profile.package+':id/dialogLeftBtn')) and any(n.text.strip()==text for text in relogin) for n in nodes):
        return 'offline','NATIVE_RELOGIN_REQUIRED'
    present={n.rid for n in nodes}
    if present & ids('main_marker'):return 'online','NATIVE_MAIN_VISIBLE'
    if present & ids('chat_input'):
        # An empty composer hides the send button. Require the complete native
        # chat layout instead of typing a message or treating a label as proof.
        chat_layout=all(present & ids(key) for key in ('chat_title','chat_message_list','chat_input_bar'))
        if present & ids('chat_send') or chat_layout:return 'online','NATIVE_CHAT_VISIBLE'
    return 'unknown','NATIVE_UI_UNVERIFIED'


class NativePresenceProbe:
    def __init__(self,ui,*,clock=time.time):self.ui=ui;self.clock=clock

    async def __call__(self,row):
        now=self.clock()
        def result(state,code,uid=None):return {'state':state,'code':code,'checked_at':now,'self_uid':uid}
        profile,_=pick_profile(self.ui.profiles,row.get('runtime_app_version') or row.get('app_version'))
        if profile is None:return result('unknown','NATIVE_PROFILE_UNAVAILABLE')
        account=Account(id=row['id'],channel='qidian',state=row['state'],self_uid=row.get('self_uid'),
                        app_version=row.get('runtime_app_version') or row.get('app_version'))
        try:
            async with asyncio.timeout(16):
                focus=_FOCUS_RE.search(await self.ui._read_shell(account,'dumpsys window'))
                if not focus or focus[1]!=profile.package:return result('unknown','NATIVE_FOREGROUND_UNAVAILABLE')
                # Known native login Activity is not an observation timeout.
                if re.search(profile.activities.get('login',r'(?!)'),focus[2]):
                    return result('offline','NATIVE_LOGIN_REQUIRED')
                try:
                    nodes=parse_ui_xml(await self.ui._dump_xml(account,read_only=True))
                except (ValueError,RuntimeError,OSError,TimeoutError):
                    nodes=None
                if nodes is not None:
                    state,code=classify_nodes(profile,nodes)
                else:
                    top=await self.ui._read_shell(account,'dumpsys activity top')
                    state,code=('online','NATIVE_MAIN_STRUCTURE') if _postlogin_activity_visible(top,focus[1]+'/'+focus[2],profile) else ('unknown','NATIVE_UI_UNVERIFIED')
                if state!='online':return result(state,code)
                uid=await self.ui.discover_self_uid(account,profile)
                last=_FOCUS_RE.search(await self.ui._read_shell(account,'dumpsys window'))
                if not last or last.groups()!=focus.groups():return result('unknown','NATIVE_FOREGROUND_CHANGED')
                if not uid:return result('unknown','NATIVE_IDENTITY_UNAVAILABLE')
                if uid!=str(row.get('self_uid') or ''):
                    return result('offline','NATIVE_ACCOUNT_CHANGED',uid)
                return result('online',code,uid)
        except asyncio.CancelledError:raise
        except Exception:return result('unknown','NATIVE_PRESENCE_UNAVAILABLE')