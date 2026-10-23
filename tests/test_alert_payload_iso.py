"""2026-10-10 真机:告警类事件 payload 的 ``first_seen_at`` / ``last_seen_at`` 发的是毫秒整数,
控制台按字符串排序(``localeCompare``)直接抛 TypeError,告警铃整体不渲染。规格(00 §6 时间规则、04 §2.4.1 示例)= ISO 8601。"""
from __future__ import annotations

import re

from qtrade_agent.alerts import Alerts
from qtrade_agent.events import Events
from qtrade_agent.pool_calibrate import PoolCalibrator

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


class _Clock:
    def __init__(self) -> None:
        self.now = 1_791_640_000_000

    def __call__(self) -> int:
        return self.now


def test_alerts_payload_times_are_iso8601(store):
    clock = _Clock()
    alerts = Alerts(Events(store), clock=clock)
    alerts.firing("H12_DISK_LOW", subject="host", severity="warn")
    p = store.list_events(event="alert")[-1]["payload"]
    assert ISO.match(p["first_seen_at"]) and ISO.match(p["last_seen_at"])
    assert p["first_seen_at"] <= p["last_seen_at"]


def test_pool_drift_payload_times_are_iso8601(store):
    clock = _Clock()
    cal = PoolCalibrator(store, events=Events(store), clock=clock)
    cal._drift_since_ms = clock.now - 3_700_000                       # noqa: SLF001
    p = cal._emit_drift("firing", 79.0, 2560, 548.0, clock.now)        # noqa: SLF001
    assert ISO.match(p["first_seen_at"]) and ISO.match(p["last_seen_at"])
