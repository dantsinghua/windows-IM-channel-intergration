"""STS 全量回归中暴露的既有边界：直播 EOF 与企点回读失败，全部使用假后端。"""
from __future__ import annotations

import json
import logging
import struct

import pytest

from qtrade_agent.adapters.qidian.ui import QidianUi
from tests.test_api_ext2 import (
    P,
    TOK_R,
    FakeStreamBackend,
    FakeStreamSession,
    Rig,
    _close_code_within,
)
from tests.test_qidian_ui import (
    CHAT_TREE,
    PKG,
    Clk,
    UiFakeAdb,
    _FakeStore,
    acct,
    chat_tree_with_input,
    node,
    tree,
)


class FiniteStreamSession(FakeStreamSession):
    """明确结束的视频源；与等待后续帧的 live 夹具区分。"""

    async def frames(self):
        for frame in self._frames:
            yield frame


class FiniteStreamBackend(FakeStreamBackend):
    async def open(self, account_id, *, profile):
        self.opened.append((account_id, profile))
        session = FiniteStreamSession([(1234, b"\x00\x00\x00\x01EOF-NAL")])
        self.sessions.append(session)
        return session


@pytest.mark.parametrize("profile", ["focus", "thumb"])
def test_stream_source_eof_closes_1001_and_releases_session(tmp_path, profile):
    """流已结束必须通知客户端重连；不能把修复 live 夹具变成吞掉 EOF。"""
    rig = Rig(tmp_path)
    backend = FiniteStreamBackend()
    rig.agent.stream_backend = backend
    try:
        # 第二次连接同时证明 EOF 清理释放 focus 所有权。
        for _ in range(2):
            with rig.client.websocket_connect(
                f"{P}/accounts/qd01/stream?token={TOK_R}&profile={profile}"
            ) as ws:
                assert ws.receive_json()["profile"] == profile
                assert ws.receive_bytes() == struct.pack(">Q", 1234) + b"\x00\x00\x00\x01EOF-NAL"
                assert _close_code_within(ws) == 1001
            assert backend.sessions[-1].closed is True
        assert len(backend.sessions) == 2
    finally:
        rig.close()


@pytest.mark.parametrize("readback", ["Different-private-readback-Ё", None], ids=["mismatch", "missing"])
@pytest.mark.parametrize("entrypoint", ["send", "send_text"])
async def test_qidian_readback_failure_returns_without_send_or_body_diagnostics(caplog, readback, entrypoint):
    """回读不同或不可用均明确失败；正文和可还原字符码不得进入日志或返回证据。"""
    intended = "Confidential-intended-body-Ж"
    adb, clock = UiFakeAdb(), Clk()

    async def advance_clock(seconds):
        clock.ms += int(seconds * 1000)

    ui = QidianUi(adb=adb, clock=clock, sleep=advance_clock)
    ui._store = _FakeStore({"10001": "张三-华泰固收"})
    readback_tree = (
        chat_tree_with_input(readback) if readback is not None
        else tree(node(rid=f"{PKG}:id/title", text="张三-华泰固收"))
    )
    adb.dumps = [CHAT_TREE, CHAT_TREE, CHAT_TREE, readback_tree]
    with caplog.at_level(logging.DEBUG, logger="qtrade_agent.adapters.qidian.ui"):
        result = await getattr(ui, entrypoint)(acct(), "10001", intended)

    assert adb.typed == [intended]
    assert "input tap 660 1085" not in adb.taps
    if entrypoint == "send":
        assert result.ok is False
        assert result.reason == "input_readback_mismatch"
        assert result.evidence == {
            "trace": {
                "back_is_none": readback is None,
                "expect_len": len(intended),
                "back_len": None if readback is None else len(readback),
            }
        }
        diagnostics = json.dumps(result.evidence, ensure_ascii=False) + caplog.text
    else:
        assert result is False
        assert "input_readback_mismatch" in caplog.text
        diagnostics = caplog.text
    assert intended not in diagnostics
    assert readback is None or readback not in diagnostics
    for forbidden in ("expect_cp", "back_cp", "first_diff", "U+0416", "U+0401", "Confidential", "Different-private"):
        assert forbidden not in diagnostics
