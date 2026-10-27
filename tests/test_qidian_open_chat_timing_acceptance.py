"""Independent nested timing acceptance; all Android operations use fake ADB."""
import json
import logging

import pytest

from test_chain_timing import timings
from test_qidian_ui import (
    CHAT_TREE, PKG, acct, chat_tree_with_input, node, rig as ui_rig, tree)


NAVIGATION = ["preflight", "launch", "prepare_home", "search_entry", "search_clear",
              "search_input", "search_results", "card_identity", "enter_chat"]


def navigation_trees(identity="10001"):
    search = tree(node(rid=f"{PKG}:id/et_search_keyword", text="", bounds="[100,160][620,220]"))
    result = tree(node(rid=f"{PKG}:id/title", text="QQ好友", bounds="[100,230][600,270]"),
                  node(rid=f"{PKG}:id/item1", text="张三-华泰固收", bounds="[100,280][600,330]"))
    card = tree(node(rid=f"{PKG}:id/account_uin", text=identity, bounds="[100,200][600,250]"),
                node(rid=f"{PKG}:id/title", text="张三-华泰固收", bounds="[100,120][600,180]"),
                node(text="发消息", bounds="[480,1070][580,1120]"))
    return [search, search, result, card]


def assert_nested(events, expected):
    parents = [(index, row) for index, row in enumerate(events) if row["stage"] == "qidian.open_chat"]
    assert [row["phase"] for _, row in parents] == ["start", "end"]
    children = [(index, row) for index, row in enumerate(events)
                if row["stage"].startswith("qidian.open_chat.")]
    assert [(row["stage"], row["phase"]) for _, row in children] == [
        ("qidian.open_chat." + stage, phase) for stage in expected for phase in ("start", "end")]
    assert all(parents[0][0] < index < parents[-1][0] for index, _ in children)
    assert all(0 <= row["elapsed_ms"] <= parents[-1][1]["elapsed_ms"]
               for _, row in children if row["phase"] == "end")
    assert all(row["account_id"] == "qd01" and row["status"] != "DELIVERED" for row in events)
    assert all(set(row) <= {"stage", "phase", "trace_id", "account_id", "status",
                            "elapsed_ms", "error_type", "outcome"} for row in events)
    assert all(row["outcome"] in {"matched", "navigate", "verified", "rejected"}
               and row["outcome"].isascii() for row in events if "outcome" in row)
    encoded = json.dumps(events, ensure_ascii=False)
    assert all(value not in encoded for value in ("10001", "99999", "张三", "华泰", "SYNTHETIC_NESTED_PRIVATE"))
    return [row for _, row in children if row["phase"] == "end"]


async def test_navigation_subspans_are_ordered_inside_the_original_parent_without_extra_ui_actions(ui_rig, caplog):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb, _clock = ui_rig
    body = "SYNTHETIC_NESTED_PRIVATE"
    adb.dumps = navigation_trees() + [CHAT_TREE, CHAT_TREE, chat_tree_with_input(body), CHAT_TREE, CHAT_TREE]
    result = await ui.send(acct(), "10001", body)
    assert result.ok
    events = timings(caplog)
    children = assert_nested(events, NAVIGATION)
    assert children[0]["outcome"] == "navigate" and children[-2]["outcome"] == "verified"
    assert adb._dump_i == 9 and adb.typed == ["10001", body]
    assert adb.taps.count("input tap 660 1085") == 1
    assert not any("sqlite3" in cmd or "select" in cmd.lower() for cmd in adb.cmds)


@pytest.mark.parametrize("failure", ["identity_refusal", "preflight_exception"])
async def test_failed_open_chat_keeps_only_executed_prefix_and_never_inputs_or_sends_body(ui_rig, caplog, monkeypatch, failure):
    caplog.set_level(logging.INFO, logger="qtrade.timing")
    ui, adb, _clock = ui_rig
    body = "SYNTHETIC_NESTED_PRIVATE"
    if failure == "identity_refusal":
        adb.dumps = navigation_trees("99999")
        result = await ui.send(acct(), "10001", body)
        assert not result.ok and result.reason == "card_identity_mismatch"
        children = assert_nested(timings(caplog), NAVIGATION[:-1])
        assert children[-1]["outcome"] == "rejected" and adb.typed == ["10001"]
        assert "input tap 530 1095" not in adb.taps
    else:
        calls = []
        async def unavailable(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("SYNTHETIC_NESTED_PRIVATE 10001")
        monkeypatch.setattr(ui, "_assert_session", unavailable)
        with pytest.raises(RuntimeError, match="SYNTHETIC_NESTED_PRIVATE"):
            await ui.send(acct(), "10001", body)
        events = timings(caplog)
        children = assert_nested(events, ["preflight"])
        assert calls == [1] and children[-1]["status"] == "error"
        assert children[-1]["error_type"] == "RuntimeError"
        parent = next(row for row in events if row["stage"] == "qidian.open_chat" and row["phase"] == "end")
        assert parent["status"] == "error" and parent["error_type"] == "RuntimeError"
        assert not adb.typed
    assert "input tap 660 1085" not in adb.taps
