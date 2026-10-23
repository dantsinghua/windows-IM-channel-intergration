"""Precise history boundary and timestamp units, without a real WeChat process."""
from datetime import datetime, timedelta
import pytest
from qtrade_winagent import wechat_read_page as page
from qtrade_winagent.win import wechat

def test_twelve_hour_window_covers_previous_day_without_reanchoring():
    anchor=datetime(2026,10,15,2,0).timestamp()
    end=anchor+24*3600
    period,floor=wechat.chatlog_read_window(anchor,now=end)
    assert period=="2026-10-14~2026-10-16"
    assert floor==anchor-12*3600

@pytest.mark.parametrize("stamp",[1791982800,1791982800000,"2026-10-14T15:40:00+00:00"])
def test_timestamp_units_and_iso_are_seconds(stamp):
    parsed=page.chatlog_timestamp(stamp)
    assert isinstance(parsed,float)
    assert parsed < 100_000_000_000

@pytest.mark.parametrize("stamp",[True,None,float("nan"),float("inf"),"invalid"])
def test_unknown_timestamp_is_not_invented(stamp):
    assert page.chatlog_timestamp(stamp) is None

def test_exact_floor_filters_messages_and_sessions_conservatively():
    floor=datetime(2026,10,15,4,0).timestamp()
    calls=[]
    def fetch(path,query,**kwargs):
        if path.endswith("session"):
            return [{"userName":"old","nOrder":(floor-1)*1000},{"userName":"recent","nOrder":floor+60}]
        calls.append(query["talker"])
        return [{"talker":"recent","seq":1,"time":floor-1,"content":"too old"},
                {"talker":"recent","seq":2,"time":floor,"content":"boundary"}]
    rows=page.ResumableReads().read(fetch,time_range="2026-10-15~2026-10-15",talker=None,since_seq=0,limit=10,not_before=floor)
    assert calls==["recent"]
    assert [r["seq"] for r in rows]==[2]
