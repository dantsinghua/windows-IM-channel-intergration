"""Late readback must retain evidence captured before this one native input."""
import asyncio
import hashlib
import json

import pytest

from qtrade_winagent.errors import NOT_READY, WaError
from qtrade_winagent.win import wechat as native


T0 = 1_793_000_000.0
TEXT = "Synthetic late readback payload"
PEER = "synthetic@chatroom"


class ConfirmationBudgetExpired(asyncio.CancelledError):
    """A deterministic stand-in for cancellation by the enclosing read budget."""


class NativeRig:
    def __init__(self, monkeypatch):
        self.backend = native.WinWeChat()
        self.inputs = []
        self.reads = []
        self.identity = ("wxid_synthetic", 71, 123.0, 72, 124.0, "synthetic-data")
        self.before = [{"talker": PEER, "seq": 10, "time": T0 - 20,
                        "isSelf": True, "content": "Previous unrelated payload", "type": 1}]
        monkeypatch.setattr(native, "require_windows", lambda _: None)
        monkeypatch.setattr(native.time, "time", lambda: T0 + (8 if self.inputs else 0))
        monkeypatch.setattr(self.backend, "_send_identity", lambda: self.identity)
        monkeypatch.setattr(self.backend, "_resolve_send_target", lambda _: "Synthetic target")
        monkeypatch.setattr(self.backend, "_require_foreground", lambda: 7)
        monkeypatch.setattr(self.backend, "_keyboard_send_text", self.input)
        monkeypatch.setattr(self.backend, "_readback_rows", self.read)

    def input(self, *args):
        self.inputs.append(args)

    def read(self, talker, text):
        self.reads.append((talker, text, bool(self.inputs)))
        return list(self.before)

    async def send(self, timeout_ms=20):
        return await self.backend.send(session_name=PEER, text=TEXT, image_path=None,
                                       confirm_timeout_ms=timeout_ms)


def completed_snapshot_before_deadline(monkeypatch, rig):
    """Exercise proof generation after known input/read completion, without OS timing."""
    class ConfirmationIO:
        def __init__(self):
            self.elapsed = 0.0
            self.snapshots = []
            self.waits = []
            self.sleeps = []

        def __getattr__(self, name):
            return getattr(asyncio, name)

        def get_running_loop(self):
            return self

        def time(self):
            return self.elapsed

        async def wait_for(self, awaitable, timeout):
            self.waits.append(timeout)
            try:
                return await awaitable
            except ConfirmationBudgetExpired:
                raise asyncio.TimeoutError from None

        def timeout(self, delay):
            clock = self
            class Scope:
                budget_expired = False

                async def __aenter__(self):
                    clock.waits.append(delay)
                    return self

                async def __aexit__(self, kind, value, traceback):
                    if kind is ConfirmationBudgetExpired:
                        self.budget_expired = True
                        raise asyncio.TimeoutError from None
                    return False

                def expired(self):
                    return self.budget_expired
            return Scope()

        def expire_read(self):
            self.elapsed += self.waits[-1]
            raise ConfirmationBudgetExpired

        async def sleep(self, delay):
            assert self.snapshots == [PEER], "Only advance the deadline after the completed snapshot"
            self.sleeps.append(delay)
            self.elapsed += delay

    clock = ConfirmationIO()

    async def snapshot(talker):
        assert len(rig.inputs) == 1
        clock.snapshots.append(talker)
        return rig.identity, rig.read(talker, None)

    # Replace this module's reference, never the real asyncio module/event-loop clock.
    # Negative timeout/read-error tests keep the original I/O and scheduling behavior.
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", snapshot)
    monkeypatch.setattr(native, "asyncio", clock)
    return clock


async def test_native_timeout_returns_original_preinput_capture_proof(monkeypatch):
    rig = NativeRig(monkeypatch)
    clock = completed_snapshot_before_deadline(monkeypatch, rig)
    result = await rig.send()
    assert clock.snapshots == [PEER] and clock.waits == [0.020] and clock.sleeps == [0.020]
    assert clock.elapsed == 0.020 and result["confirm_ms"] == 20
    assert result["code"] == "SEND_FAILED" and result["ext_msg_id"] is None
    assert len(rig.inputs) == 1 and rig.reads[0] == (PEER, None, False)
    proof = result.get("confirmation_proof")
    assert isinstance(proof, dict), "Completed native input must retain its original pre-input evidence"
    assert proof == {
        "version": 1, "self_uid": "wxid_synthetic", "native_pid": 71, "talker": PEER,
        "max_seq": 10,
        "anchor": {"seq": 10, "time_s": T0 - 20,
                   "text_sha256": hashlib.sha256(b"Previous unrelated payload").hexdigest()},
        "captured_at_s": T0, "send_started_at_s": T0, "action_completed_at_s": T0 + 8,
        "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
    }, "Baseline capture time and anchor must come from before the input, not timeout reconstruction"
    serialized = json.dumps(proof, allow_nan=False)
    assert TEXT not in serialized and "Previous unrelated payload" not in serialized
    assert "synthetic-data" not in serialized, "Local reader path must not escape in proof"


async def test_timeout_inside_read_without_trusted_snapshot_cannot_issue_proof(monkeypatch):
    rig = NativeRig(monkeypatch)
    async def blocked_read(_):
        await asyncio.Event().wait()
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", blocked_read)
    result = await rig.send()
    assert len(rig.inputs) == 1
    assert result.get("confirmation_proof") is None


@pytest.mark.parametrize("boundary", ["identity", "anchor-missing", "anchor-time-changed", "anchor-body-changed"])
async def test_invalidated_native_identity_or_watermark_cannot_issue_proof(monkeypatch, boundary):
    rig = NativeRig(monkeypatch)
    async def changed_snapshot(_):
        identity, rows = rig.identity, list(rig.before)
        if boundary == "identity":
            identity = ("wxid_other", *identity[1:])
        elif boundary == "anchor-missing":
            rows = []
        elif boundary == "anchor-time-changed":
            rows = [{**rows[0], "time": T0 - 1}]
        else:
            rows = [{**rows[0], "content": "Reused sequence in a different history"}]
        return identity, rows
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", changed_snapshot)
    result = await rig.send()
    assert result["code"] == "SEND_FAILED" and len(rig.inputs) == 1
    assert result.get("confirmation_proof") is None


async def test_frontend_refusal_does_not_issue_proof_or_input(monkeypatch):
    rig = NativeRig(monkeypatch)
    def refused():
        raise WaError(NOT_READY, "Synthetic refusal", reason="wechat_window_unverified")
    monkeypatch.setattr(rig.backend, "_require_foreground", refused)
    with pytest.raises(WaError):
        await rig.send()
    assert not rig.inputs


async def test_service_binds_request_key_without_mutating_native_proof(monkeypatch):
    from qtrade_winagent.config import WechatConfig
    from qtrade_winagent.fakes import FakeWeChat
    from qtrade_winagent.wechat import WeChatSession
    backend = FakeWeChat()
    backend.data_key = backend.img_key = "synthetic-key"
    original = {"version": 1, "self_uid": "wxid_synthetic", "native_pid": 71,
                "talker": PEER, "max_seq": 0, "anchor": None, "captured_at_s": T0,
                "send_started_at_s": T0, "action_completed_at_s": T0 + 1,
                "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest()}
    async def completed(**kwargs):
        return {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                "confirm_ms": 10000, "confirmation_proof": original}
    monkeypatch.setattr(backend, "send", completed)
    service = WeChatSession(backend, WechatConfig(enabled=True))
    result = await service.send(session_name=PEER, text=TEXT, idempotency_key="attempt-key")
    assert result.get("confirmation_proof") == {**original, "idempotency_key": "attempt-key"}
    assert "idempotency_key" not in original


async def test_complete_empty_baseline_can_preserve_first_message_proof(monkeypatch):
    rig = NativeRig(monkeypatch)
    rig.before = []
    clock = completed_snapshot_before_deadline(monkeypatch, rig)
    result = await rig.send()
    assert clock.snapshots == [PEER] and clock.elapsed == 0.020 and result["confirm_ms"] == 20
    proof = result.get("confirmation_proof")
    assert proof and proof["max_seq"] == 0 and proof["anchor"] is None
    assert len(rig.inputs) == 1


async def test_read_deadline_after_trusted_snapshot_retains_original_proof(monkeypatch):
    rig = NativeRig(monkeypatch)
    clock = completed_snapshot_before_deadline(monkeypatch, rig)
    snapshots = []
    async def snapshot(talker):
        snapshots.append(True)
        clock.snapshots.append(talker)
        if len(snapshots) == 1:
            return rig.identity, rig.read(talker, None)
        # Cancel through the outer budget; an I/O TimeoutError is a distinct failure.
        clock.expire_read()
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", snapshot)
    result = await rig.send(timeout_ms=1100)
    assert len(snapshots) == 2 and len(rig.inputs) == 1
    assert clock.waits == pytest.approx([1.1, 0.1]) and clock.sleeps == [1.0]
    assert clock.elapsed == pytest.approx(1.1)
    assert result["confirm_ms"] == 1100
    assert isinstance(result.get("confirmation_proof"), dict)


async def test_early_read_error_after_trusted_snapshot_still_cannot_issue_proof(monkeypatch):
    rig = NativeRig(monkeypatch)
    snapshots = []
    async def snapshot(_):
        snapshots.append(True)
        if len(snapshots) == 1:
            return rig.identity, list(rig.before)
        raise OSError("Synthetic reader unavailable")
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", snapshot)
    result = await rig.send(timeout_ms=1100)
    assert len(snapshots) == 2 and len(rig.inputs) == 1
    assert result.get("confirmation_proof") is None


async def test_ambiguous_two_new_same_text_rows_cannot_issue_proof(monkeypatch):
    rig = NativeRig(monkeypatch)
    async def ambiguous_snapshot(_):
        return rig.identity, rig.before + [
            {"talker": PEER, "seq": seq, "time": T0 + 1, "type": 1,
             "isSelf": True, "content": TEXT} for seq in (11, 12)]
    monkeypatch.setattr(rig.backend, "_read_send_snapshot", ambiguous_snapshot)
    result = await rig.send()
    assert result["code"] == "SEND_FAILED" and len(rig.inputs) == 1
    assert result.get("confirmation_proof") is None


async def test_service_never_forwards_unknown_proof_fields(monkeypatch):
    from qtrade_winagent.config import WechatConfig
    from qtrade_winagent.fakes import FakeWeChat
    from qtrade_winagent.wechat import WeChatSession
    backend = FakeWeChat()
    backend.data_key = backend.img_key = "synthetic-key"
    proof = {"version": 1, "self_uid": "wxid_synthetic", "native_pid": 71,
             "talker": PEER, "max_seq": 0, "anchor": None, "captured_at_s": T0,
             "send_started_at_s": T0, "action_completed_at_s": T0 + 1,
             "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
             "raw_text": TEXT, "authorization": "synthetic-sensitive-value"}
    async def completed(**kwargs):
        return {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None,
                "confirm_ms": 10000, "confirmation_proof": proof}
    monkeypatch.setattr(backend, "send", completed)
    service = WeChatSession(backend, WechatConfig(enabled=True))
    result = await service.send(session_name=PEER, text=TEXT, idempotency_key="attempt-key")
    serialized = json.dumps(result)
    assert TEXT not in serialized and "synthetic-sensitive-value" not in serialized
    assert "raw_text" not in serialized and "authorization" not in serialized
