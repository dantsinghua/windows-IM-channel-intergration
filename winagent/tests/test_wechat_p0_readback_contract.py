"""P0 T07/T08/T11 strict native confirmation, no native I/O."""
import asyncio
import pytest

from qtrade_winagent.errors import NOT_READY, WaError
from qtrade_winagent.win import wechat as native
from qtrade_winagent.win.wechat import pick_readback

T0 = 1_793_000_000.0


def row(**changes):
    return {'talker': 'synthetic@chatroom', 'seq': 11, 'time': T0 + 1,
            'isSelf': True, 'content': 'Exact synthetic text', 'type': 1, **changes}


def pick(rows, *, text='Exact synthetic text', image=False, **changes):
    baseline = native.ReadbackBaseline(account_id='wxid_synthetic', talker='synthetic@chatroom',
        max_seq=10, seen_ids=frozenset({'synthetic@chatroom:10'}), captured_at_s=T0)
    return pick_readback(rows, text=text, image=image, since_s=T0,
        **{'baseline': baseline, 'account_id': 'wxid_synthetic', **changes})


@pytest.mark.parametrize('value', [None, '', 'invalid', float('nan'), float('inf'), float('-inf'), True, False])
def test_missing_invalid_nonfinite_or_boolean_time_never_confirms(value):
    assert pick([row(time=value)]) is None


@pytest.mark.parametrize('value', ['false', 'true', 1, 0, [], {}, None])
def test_only_boolean_true_is_native_self_evidence(value):
    assert pick([row(isSelf=value)]) is None


@pytest.mark.parametrize('body', ['prefix Exact synthetic text', 'Exact synthetic text suffix',
                                 ' Exact synthetic text', 'Exact synthetic text ', 'exact synthetic text'])
def test_body_requires_exact_original_text(body):
    assert pick([row(content=body)]) is None


@pytest.mark.parametrize('media_type', [3, 49, '3', '49'])
def test_media_type_without_media_identity_is_not_confirmation(media_type):
    assert pick([row(type=media_type)], text=None, image=True) is None


def test_exact_fresh_single_native_row_with_trusted_baseline_can_confirm():
    candidate = row()
    assert pick([candidate]) == candidate


def test_used_native_id_cannot_confirm_another_command():
    assert pick([row()], used_ids=frozenset({'synthetic@chatroom:11'})) is None


def test_account_change_invalidates_baseline():
    assert pick([row()], account_id='wxid_other') is None


def test_legacy_call_without_baseline_fails_closed():
    assert pick_readback([row()], text='Exact synthetic text', image=False, since_s=T0) is None


class SendRig:
    """Real WinWeChat.send; every external/native boundary is isolated."""

    def __init__(self, monkeypatch, before, after):
        self.backend = native.WinWeChat()
        self.actions = []
        self.reads = []
        self.before, self.after = before, after
        monkeypatch.setattr(native, 'require_windows', lambda _: None)
        monkeypatch.setattr(native.time, 'time', lambda: T0)
        monkeypatch.setattr(self.backend, 'current_wxid', lambda: 'wxid_synthetic')
        monkeypatch.setattr(self.backend, '_send_identity',
            lambda: ('wxid_synthetic', 71, 123.0, 72, 124.0, 'synthetic-data'), raising=False)
        monkeypatch.setattr(self.backend, '_resolve_send_target', lambda _: 'Synthetic target')
        monkeypatch.setattr(self.backend, '_require_foreground', lambda: 7)
        monkeypatch.setattr(self.backend, '_keyboard_send_text', lambda *args: self.actions.append(args))
        monkeypatch.setattr(self.backend, '_readback_rows', self.read)
        monkeypatch.setattr(self.backend, '_get_items', self.get_items)

    def read(self, talker, text):
        self.reads.append((talker, text, bool(self.actions)))
        return self.before + self.after if self.actions else self.before

    def get_items(self, path, query):
        assert path == '/api/v1/chatlog', 'All native reads must stay at the synthetic boundary'
        return self.read(query['talker'], query.get('keyword'))

    async def send(self):
        try:
            return await self.backend.send(session_name='synthetic@chatroom', text='Exact synthetic text',
                                           image_path=None, confirm_timeout_ms=100)
        except WaError as exc:
            return {'ok': False, 'code': exc.code}


async def test_real_send_establishes_watermark_before_input_and_does_not_reuse_old_same_text(monkeypatch):
    old = row(seq=11, time=T0)
    rig = SendRig(monkeypatch, [old], [old])
    result = await rig.send()
    assert result['code'] != 'DELIVERED'
    assert rig.reads and rig.reads[0][2] is False, 'Native evidence must be read before any text input'
    assert len(rig.actions) <= 1


async def test_missing_baseline_refuses_before_input(monkeypatch):
    rig = SendRig(monkeypatch, [], [row()])
    def unavailable(*args):
        raise WaError(NOT_READY, 'Synthetic baseline scan failed', reason='wechat_send_baseline_unavailable')
    monkeypatch.setattr(rig.backend, '_readback_rows', unavailable)
    result = await rig.send()
    assert not rig.actions, 'An unavailable baseline is not permission to send'
    assert result['code'] != 'DELIVERED'


async def test_trusted_complete_empty_conversation_supports_first_message(monkeypatch):
    rig = SendRig(monkeypatch, [], [row()])
    result = await rig.send()
    assert result['code'] == 'DELIVERED'
    assert result['ext_msg_id'] == 'synthetic@chatroom:11'
    assert rig.reads[0][2] is False and len(rig.actions) == 1


@pytest.mark.parametrize('seq', [None, True, False, 'bad', 1.5, 0, -1, 9, 10])
async def test_real_send_rejects_invalid_or_reset_sequence(monkeypatch, seq):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0 - 10)], [row(seq=seq)])
    result = await rig.send()
    assert result['code'] != 'DELIVERED'
    assert len(rig.actions) <= 1


async def test_two_new_identical_rows_cannot_be_attributed_to_one_command(monkeypatch):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0 - 10)], [row(seq=11), row(seq=12)])
    result = await rig.send()
    assert result['code'] != 'DELIVERED'
    assert len(rig.actions) == 1


async def test_wrong_stable_conversation_is_not_confirmation(monkeypatch):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0 - 10)], [row(talker='other@chatroom')])
    result = await rig.send()
    assert result['code'] != 'DELIVERED'


async def test_real_send_confirms_one_fresh_exact_row_after_trusted_baseline(monkeypatch):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0 - 10)], [row()])
    result = await rig.send()
    assert result['code'] == 'DELIVERED'
    assert result['ext_msg_id'] == 'synthetic@chatroom:11'
    assert rig.reads[0][2] is False
    assert len(rig.actions) == 1


async def test_timeout_during_native_readback_keeps_unconfirmed_without_second_input(monkeypatch):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0-10)], [row()])
    entered = []
    async def delayed(_talker):
        entered.append(True)
        await asyncio.Event().wait()
    monkeypatch.setattr(rig.backend, '_read_send_snapshot', delayed)
    result = await rig.send()
    assert entered == [True]
    assert result['code'] == 'SEND_FAILED' and result['ext_msg_id'] is None
    assert len(rig.actions) == 1


async def test_concurrent_command_cannot_share_the_same_pending_native_readback(monkeypatch):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0-10)], [row()])
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(talker):
        entered.set()
        await release.wait()
        return rig.backend._send_identity(), rig.read(talker, None)
    monkeypatch.setattr(rig.backend, '_read_send_snapshot', held)
    first = asyncio.create_task(rig.send())
    await asyncio.wait_for(entered.wait(), 2)
    second = await rig.send()
    assert second['code'] != 'DELIVERED'
    assert len(rig.actions) == 1
    release.set()
    assert (await first)['code'] == 'DELIVERED'


async def test_same_native_record_cannot_confirm_a_second_sequential_command(monkeypatch):
    rig = SendRig(monkeypatch, [row(seq=10, time=T0-10)], [row()])
    assert (await rig.send())['code'] == 'DELIVERED'
    assert (await rig.send())['code'] != 'DELIVERED'
    assert len(rig.actions) == 2
