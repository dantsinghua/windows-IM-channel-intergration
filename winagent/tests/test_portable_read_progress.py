"""Slow history scans keep a private checkpoint; no real Chatlog calls."""
import pytest
from qtrade_winagent import wechat_read_page as page
from qtrade_winagent.errors import WaError

class Clock:
    value = 0.0
    def __call__(self):
        return self.value

def fixture(clock, names=8):
    calls = []
    def fetch(path, query, **kwargs):
        clock.value += .4
        calls.append((path, dict(query)))
        if path.endswith("session"):
            return [{"userName": str(i)} for i in range(names)]
        return [{"talker": query["talker"], "seq": int(query["talker"]) + 1, "content": "synthetic"}]
    return fetch, calls

def test_slow_full_scan_resumes_without_repeating_finished_requests(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(page.time, "monotonic", clock)
    fetch, calls = fixture(clock)
    reader = page.ResumableReads()
    result = None
    for _ in range(20):
        try:
            result = reader.read(fetch, time_range="2026-10-01~2026-10-02", talker=None, since_seq=0, limit=20, seconds=.5)
            break
        except WaError as error:
            assert error.reason == "wechat_read_catching_up"
            assert error.retryable
    assert result is not None and [r["seq"] for r in result] == list(range(1, 9))
    assert len(calls) == 9, "completed pages must not restart on every time budget"

def test_transient_fetch_failure_retries_same_checkpoint(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(page.time, "monotonic", clock)
    fetch, calls = fixture(clock, 3)
    reader = page.ResumableReads()
    failures = [True]
    def flaky(path, query, **kw):
        if query.get("talker") == "1" and failures.pop() if failures else False:
            raise WaError("NOT_READY", "synthetic timeout", reason="wechat_read_timeout", retryable=True)
        return fetch(path, query, **kw)
    for _ in range(10):
        try:
            rows = reader.read(flaky, time_range="2026-10-01~2026-10-02", talker=None, since_seq=0, limit=20, seconds=.5)
            break
        except WaError:
            continue
    else:
        pytest.fail("scan never completed")
    assert [r["seq"] for r in rows] == [1, 2, 3]

def test_reset_invalidates_partial_account_scan(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(page.time, "monotonic", clock)
    fetch, calls = fixture(clock)
    reader = page.ResumableReads()
    with pytest.raises(WaError):
        reader.read(fetch, time_range="2026-10-01~2026-10-02", talker=None, since_seq=0, limit=20, seconds=.5)
    reader.reset()
    rows = reader.read(fetch, time_range="2026-10-01~2026-10-02", talker="7", since_seq=0, limit=20, seconds=10)
    assert [r["seq"] for r in rows] == [8]

def test_interrupted_scan_cache_is_bounded(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(page.time, "monotonic", clock)
    fetch, calls = fixture(clock)
    reader = page.ResumableReads(max_scans=2)
    for cursor in range(10):
        with pytest.raises(WaError):
            reader.read(fetch, time_range="2026-10-01~2026-10-02", talker=None, since_seq=cursor, limit=20, seconds=.1)
        assert len(reader._scans) <= 2
