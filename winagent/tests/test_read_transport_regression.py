"""Synthetic regression cases: no real chatlog, pipes, credentials or IM actions."""
import asyncio, json
from unittest.mock import AsyncMock
import pytest
from qtrade_winagent.win.wechat import WinWeChat
from qtrade_winagent.backends import PipeFrame
from qtrade_winagent.config import IpcConfig
from qtrade_winagent.fakes import FakePipeBackend
from qtrade_winagent.pipe import UserAgentLink
from qtrade_winagent.errors import WaError
from tests.test_wechat import mk_session

def row(seq,talker="a",size=12):
 return {"seq":seq,"talker":talker,"content":"x"*size,"type":1,"time":1791900000,"isSelf":False}

def fake_reader(monkeypatch,rows):
 wx=WinWeChat();wx._read_anchor=1791900000;calls=[]
 async def sessions(**kwargs):return [{"userName":name} for name in rows]
 monkeypatch.setattr(wx,"list_sessions",sessions)
 def fetch(path,query,**kwargs):
  calls.append((path,dict(query)))
  if path=="/api/v1/session":
   a=[{"userName":name} for name in rows];off=query.get("offset",0);return a[off:off+query["limit"]]
  a=rows[query["talker"]];off=query.get("offset",0);return a[off:off+query["limit"]]
 monkeypatch.setattr(wx,"_get_items",fetch)
 return wx,calls

async def test_full_read_limit_is_global_and_sorted_not_squared(monkeypatch):
 wx,calls=fake_reader(monkeypatch,{"a":[row(2),row(4),row(6)],"b":[row(1,"b"),row(3,"b"),row(5,"b")]})
 out=await wx.read_messages(talker=None,since_seq=0,limit=2)
 assert [r["seq"] for r in out]==[1,2]

async def test_since_cursor_pages_past_old_first_page(monkeypatch):
 wx,calls=fake_reader(monkeypatch,{"a":[row(i) for i in range(1,230)]})
 out=await wx.read_messages(talker="a",since_seq=220,limit=3)
 assert [r["seq"] for r in out]==[221,222,223]
 assert any(q.get("offset",0)>0 for _,q in calls)

async def test_repeated_pages_deliver_all_rows_once(monkeypatch):
 rows={"a":[row(i) for i in range(2,31,2)],"b":[row(i,"b") for i in range(1,31,2)]}
 wx,_=fake_reader(monkeypatch,rows);cursor=0;seen=[]
 for _ in range(15):
  batch=await wx.read_messages(talker=None,since_seq=cursor,limit=3)
  if not batch:break
  seen.extend(r["seq"] for r in batch);cursor=max(r["seq"] for r in batch)
 assert seen==list(range(1,31))

async def test_equal_sequence_group_is_not_split_across_watermark(monkeypatch):
 wx,_=fake_reader(monkeypatch,{"a":[row(1),row(3)],"b":[row(3,"b"),row(4,"b")]})
 out=await wx.read_messages(talker=None,since_seq=0,limit=2)
 assert [r["seq"] for r in out]==[1]

async def test_session_response_fits_frame_without_truncating_content():
 wx,session,_=mk_session();wx.data_key=wx.img_key=True
 rows=[row(i,size=16000) for i in range(1,151)]
 wx.read_messages=AsyncMock(return_value=rows)
 result=await session.read(limit=150,since_seq=0)
 wire=PipeFrame(id=99,ok=True,result=result).to_wire()
 assert len(json.dumps(wire,ensure_ascii=False).encode("utf-8"))+1<=1024*1024
 assert 0<len(result["messages"])<len(rows)
 assert all(r["content"]=="x"*16000 for r in result["messages"])
 assert result["next_seq"]==result["messages"][-1]["seq"]

async def test_one_oversized_message_fails_without_advancing_cursor():
 wx,session,_=mk_session();wx.data_key=wx.img_key=True
 wx.read_messages=AsyncMock(return_value=[row(11,size=2*1024*1024)])
 with pytest.raises(WaError) as err:await session.read(limit=1,since_seq=10)
 assert err.value.reason=="wechat_message_too_large"

async def test_oversized_dispatch_returns_error_and_connection_remains_usable():
 be=FakePipeBackend(max_frame_kb=1)
 conn=await be.connect("synthetic")
 link=UserAgentLink(be,IpcConfig(max_frame_kb=1),pipe_name="synthetic",session_id="C",user_sid="test",version="1.0.0",pid=7,modules=())
 link.on("large",AsyncMock(return_value={"text":"x"*4000}))
 await link._dispatch(conn,PipeFrame(id=1,method="large"))
 assert conn.sent[-1].ok is False
 assert conn.sent[-1].error["reason"]=="ipc_response_too_large"
 link.on("small",AsyncMock(return_value={"value":1}))
 await link._dispatch(conn,PipeFrame(id=2,method="small"))
 assert conn.sent[-1].ok is True and conn.sent[-1].result=={"value":1}

@pytest.mark.parametrize("value",[0,-1,True,20000])
async def test_invalid_page_limit_is_rejected(value,monkeypatch):
 wx,_=fake_reader(monkeypatch,{"a":[row(1)]})
 with pytest.raises((WaError,ValueError)):await wx.read_messages(talker="a",since_seq=0,limit=value)
