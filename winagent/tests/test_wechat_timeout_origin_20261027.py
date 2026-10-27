"""Confirmation budget expiry and reader failure have different evidence rules."""
import asyncio
import hashlib

import pytest

from qtrade_winagent.win import wechat as native
from test_wechat_late_proof_20261026 import NativeRig, PEER, TEXT, T0


class BudgetCancellation(asyncio.CancelledError):
    """The enclosing confirmation timer cancelled an unfinished read."""


class ConfirmationClock:
    """Model both public timeout APIs without depending on OS timer rounding."""
    def __init__(self, rig, mode, *, delta=-0.015, trusted_first=True):
        self.rig, self.mode = rig, mode
        self.delta, self.trusted_first = delta, trusted_first
        self.elapsed = 0.0
        self.read_count = 0
        self.waits = []
        self.sleeps = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def get_running_loop(self):
        return self

    def time(self):
        return self.elapsed

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.elapsed += delay

    async def wait_for(self, awaitable, timeout):
        self.waits.append(timeout)
        try:
            return await awaitable
        except BudgetCancellation:
            raise asyncio.TimeoutError from None

    def timeout(self, delay):
        clock = self
        class Scope:
            expired_by_budget = False

            async def __aenter__(self):
                clock.waits.append(delay)
                return self

            async def __aexit__(self, kind, value, traceback):
                if kind is BudgetCancellation:
                    self.expired_by_budget = True
                    raise asyncio.TimeoutError from None
                return False

            def expired(self):
                return self.expired_by_budget
        return Scope()

    async def snapshot(self, talker):
        assert talker == PEER and len(self.rig.inputs) == 1
        self.read_count += 1
        if self.trusted_first and self.read_count == 1:
            return self.rig.identity, list(self.rig.before)
        self.elapsed = 1.1 + self.delta
        if self.mode == "outer-budget":
            raise BudgetCancellation
        if self.mode == "reader-timeout":
            raise TimeoutError("Synthetic native reader timeout")
        identity, rows = self.rig.identity, list(self.rig.before)
        if self.mode == "identity-changed":
            identity = ("wxid_other", *identity[1:])
        elif self.mode == "anchor-changed":
            rows = [{**rows[0], "content": "Different native history"}]
        elif self.mode == "ambiguous":
            rows += [{"talker": PEER, "seq": seq, "time": T0 + 1,
                      "type": 1, "isSelf": True, "content": TEXT} for seq in (11, 12)]
        else:
            raise AssertionError("Unknown test scenario")
        return identity, rows


def arrange(monkeypatch, mode, **options):
    rig = NativeRig(monkeypatch)
    clock = ConfirmationClock(rig, mode, **options)
    monkeypatch.setattr(native, "asyncio", clock)
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", clock.snapshot)
    return rig, clock


@pytest.mark.parametrize("delta", [-0.015, 0.0, 0.015])
async def test_confirmation_timer_expiry_preserves_original_proof_despite_clock_rounding(monkeypatch, delta):
    rig, clock = arrange(monkeypatch, "outer-budget", delta=delta)
    result = await rig.send(timeout_ms=1100)
    assert len(rig.inputs) == 1 and clock.read_count == 2
    assert clock.waits == pytest.approx([1.1, 0.1]) and clock.sleeps == [1.0]
    assert result["confirmation_reason"] == "confirm_timeout"
    assert result["code"] == "SEND_FAILED" and result["ext_msg_id"] is None
    assert result["confirm_ms"] == 1100
    proof = result["confirmation_proof"]
    assert proof["captured_at_s"] == T0 and proof["max_seq"] == 10
    assert proof["native_pid"] == rig.identity[1] and proof["self_uid"] == rig.identity[0]
    assert proof["send_started_at_s"] == T0 and proof["action_completed_at_s"] == T0 + 8
    assert proof["anchor"]["text_sha256"] == hashlib.sha256(b"Previous unrelated payload").hexdigest()
    assert proof["text_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()


@pytest.mark.parametrize("delta", [-0.015, 0.0, 0.015])
async def test_reader_timeout_is_not_budget_expiry_even_when_wall_deadline_has_passed(monkeypatch, delta):
    rig, clock = arrange(monkeypatch, "reader-timeout", delta=delta)
    result = await rig.send(timeout_ms=1100)
    assert len(rig.inputs) == 1 and clock.read_count == 2
    assert result["confirmation_reason"] == "read_failed"
    assert result.get("confirmation_proof") is None and result["ext_msg_id"] is None


async def test_budget_expiry_without_a_trusted_snapshot_cannot_preserve_proof(monkeypatch):
    rig, clock = arrange(monkeypatch, "outer-budget", trusted_first=False)
    result = await rig.send(timeout_ms=1100)
    assert len(rig.inputs) == 1 and clock.read_count == 1
    assert result["confirmation_reason"] == "read_failed"
    assert result.get("confirmation_proof") is None


@pytest.mark.parametrize("mode,reason", [
    ("identity-changed", "identity_changed"),
    ("anchor-changed", "anchor_changed"),
    ("ambiguous", "ambiguous_candidate"),
])
async def test_invalid_later_snapshot_revokes_previously_trusted_evidence(monkeypatch, mode, reason):
    rig, clock = arrange(monkeypatch, mode)
    result = await rig.send(timeout_ms=1100)
    assert len(rig.inputs) == 1 and clock.read_count == 2
    assert result["confirmation_reason"] == reason and result.get("confirmation_proof") is None
