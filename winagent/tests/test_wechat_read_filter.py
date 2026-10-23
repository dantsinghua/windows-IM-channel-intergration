"""会话级时间过滤必须用 nOrder(epoch 秒),不是 nTime 字符串(2026-10-15 实测根因)。

背景:chatlog `session?format=json` 返回 `{userName, nOrder(epoch秒), nTime(ISO字符串), ...}`。
旧实现用 `nTime` 做 `isinstance(stamp,(int,float))` 判断恒为 False,导致「窗口内无消息的会话」
也被逐个查询;账号会话 ≥500 时一次全量扫描上千次 HTTP 往返,超时误判 KEY_FAIL。

历史窗口:chatlog `time` 参数只支持日期粒度(小时粒度 400),故「追历史」实现为**仅最近一天**
(time="今天~今天"),会话过滤用 nOrder 精确到秒。
"""
from __future__ import annotations

import datetime

from qtrade_winagent import wechat_read_page as page


class Clock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


def _day_start(iso: str) -> float:
    return datetime.datetime.fromisoformat(iso).timestamp()


def test_session_scan_skips_sessions_older_than_window(monkeypatch):
    start = _day_start("2026-10-15")
    sessions = [
        {"userName": "recent", "nOrder": start + 8 * 3600, "nTime": "recent-iso"},
        {"userName": "old", "nOrder": start - 2 * 86400, "nTime": "old-iso"},
    ]
    clock = Clock()
    monkeypatch.setattr(page.time, "monotonic", clock)
    calls: list = []

    def fetch(path, query, **kw):
        clock.value += 0.01
        calls.append((path, dict(query)))
        if path.endswith("session"):
            return sessions
        return [{"talker": query["talker"], "seq": 1, "content": "x"}]

    reader = page.ResumableReads()
    rows = reader.read(fetch, time_range="2026-10-15~2026-10-15", talker=None,
                       since_seq=0, limit=20, seconds=10)
    queried = [c[1]["talker"] for c in calls if c[0].endswith("chatlog")]
    assert queried == ["recent"], f"应只查窗口内有消息的会话,实际查了 {queried}"
    assert [r["talker"] for r in rows] == ["recent"]


def test_session_filter_falls_back_to_ntime_iso_string(monkeypatch):
    start = _day_start("2026-10-15")
    recent_ts = start + 8 * 3600
    old_ts = start - 2 * 86400
    sessions = [
        {"userName": "recent", "nTime": datetime.datetime.fromtimestamp(recent_ts, tz=datetime.timezone.utc).isoformat()},
        {"userName": "old", "nTime": datetime.datetime.fromtimestamp(old_ts, tz=datetime.timezone.utc).isoformat()},
    ]
    clock = Clock()
    monkeypatch.setattr(page.time, "monotonic", clock)
    calls: list = []

    def fetch(path, query, **kw):
        clock.value += 0.01
        calls.append((path, dict(query)))
        if path.endswith("session"):
            return sessions
        return [{"talker": query["talker"], "seq": 1, "content": "x"}]

    reader = page.ResumableReads()
    reader.read(fetch, time_range="2026-10-15~2026-10-15", talker=None,
                since_seq=0, limit=20, seconds=10)
    queried = [c[1]["talker"] for c in calls if c[0].endswith("chatlog")]
    assert queried == ["recent"], f"nTime 字符串回退应正确过滤,实际查了 {queried}"
