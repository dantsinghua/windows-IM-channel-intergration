"""目录、指令、回执、消息的明文校验。

身份只用稳定号码。``label``（含嵌套）一律剔掉。
回执可以带失败原因，但不带回文本正文。
消息没有债券字段也保留，正文留在 ``text``。
"""
from __future__ import annotations

from typing import Any

_CHANNELS = frozenset({"wechat", "qidian", "qq"})
_KINDS = frozenset({"private", "group"})
_DIRS = frozenset({"in", "out"})
_COMMAND_FIELDS = (
    "cmd_id",
    "workbench_id",
    "channel",
    "account_uid",
    "op",
    "text",
    "idempotency_key",
)
_MESSAGE_FIELDS = (
    "event_id",
    "workbench_id",
    "channel",
    "account_uid",
    "dir",
    "text",
    "ts",
    "ext_msg_id",
)


def _copy(value: Any, *, drop_label: bool = False, drop_text: bool = False) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if drop_label and key == "label":
                continue
            if drop_text and key == "text":
                continue
            out[str(key)] = _copy(item, drop_label=drop_label, drop_text=drop_text)
        return out
    if isinstance(value, list):
        return [_copy(item, drop_label=drop_label, drop_text=drop_text) for item in value]
    return value


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ValueError(f"{where}缺少 {key}")
    return data[key]


def _require_session(data: dict[str, Any], where: str) -> dict[str, Any]:
    session = data.get("session")
    if not isinstance(session, dict):
        raise ValueError(f"{where}缺少 session")
    for key in ("kind", "native_id"):
        if key not in session or session[key] in (None, ""):
            raise ValueError(f"{where}缺少 session.{key}")
    if session["kind"] not in _KINDS:
        raise ValueError("session.kind 只能是 private 或 group")
    return session


def _blank_token(value: Any) -> bool:
    """编号只认非空白。缺省、空串和纯空白都是空。"""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _check_org(account: dict[str, Any]) -> None:
    """有 ``org`` 时，部门列表和成员列表必须是列表，条目的 id / uin 不能是空的。

    没有 ``org``、列表为空、部门没有名称，都仍然通过。旧目录不会被这道校验拒绝。
    ``org`` 存在时，``departments`` 和 ``members`` 两个键都必须在，不能缺一个再当成空列表。
    """
    if "org" not in account or account["org"] is None:
        return
    if account.get("channel") != "qidian":
        raise ValueError("组织只能挂在企点账号上")
    org = account["org"]
    if not isinstance(org, dict):
        raise ValueError("目录 org 必须是映射")
    if "departments" not in org or "members" not in org:
        raise ValueError("目录 org 的 departments 和 members 必须是列表")
    departments = org["departments"]
    members = org["members"]
    if not isinstance(departments, list) or not isinstance(members, list):
        raise ValueError("目录 org 的 departments 和 members 必须是列表")
    for item in departments:
        if not isinstance(item, dict):
            raise ValueError("目录 org 部门必须是映射")
        if "id" not in item or _blank_token(item["id"]):
            raise ValueError("目录 org 部门缺少 id")
    for item in members:
        if not isinstance(item, dict):
            raise ValueError("目录 org 成员必须是映射")
        if "uin" not in item or _blank_token(item["uin"]):
            raise ValueError("目录 org 成员缺少 uin")


def validate_catalog(catalog: Any) -> dict[str, Any]:
    """去掉全部 label 后返回目录。账号缺 ``account_uid`` 则拒绝；没有昵称仍保留。

    企点账号可以带 ``org``。没有这份组织、或组织列表是空的，仍然通过。
    """
    data = _copy(catalog, drop_label=True)
    if not isinstance(data, dict):
        raise ValueError("目录必须是映射")
    _require(data, "workbench_id", "目录")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("目录缺少 accounts")
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("目录账号必须是映射")
        if "account_uid" not in account or account["account_uid"] in (None, ""):
            raise ValueError("目录账号缺少 account_uid")
        if account.get("channel") not in _CHANNELS:
            raise ValueError("channel 只能是 wechat、qidian、qq")
        _check_org(account)
    return data


def validate_command(command: Any) -> dict[str, Any]:
    """第一期指令只接受 ``send_text``，通道和会话类型按规格枚举。"""
    data = _copy(command)
    if not isinstance(data, dict):
        raise ValueError("指令必须是映射")
    for key in _COMMAND_FIELDS:
        _require(data, key, "指令")
    _require_session(data, "指令")
    if data["op"] != "send_text":
        raise ValueError("op 只能是 send_text")
    if data["channel"] not in _CHANNELS:
        raise ValueError("channel 只能是 wechat、qidian、qq")
    return data


def validate_receipt(receipt: Any) -> dict[str, Any]:
    """失败必须有 reason。返回值不含 ``text`` 正文。"""
    data = _copy(receipt, drop_text=True)
    if not isinstance(data, dict):
        raise ValueError("回执必须是映射")
    for key in ("cmd_id", "ok", "code"):
        if key not in data or data[key] in (None, ""):
            raise ValueError(f"回执缺少 {key}")
    if not data["ok"] and not data.get("reason"):
        raise ValueError("失败回执必须有 reason")
    return data


def validate_message(message: Any) -> dict[str, Any]:
    """校验上行消息。对不上券（没有债券字段）也不丢，正文保留。"""
    data = _copy(message)
    if not isinstance(data, dict):
        raise ValueError("消息必须是映射")
    for key in _MESSAGE_FIELDS:
        _require(data, key, "消息")
    _require_session(data, "消息")
    if data["channel"] not in _CHANNELS:
        raise ValueError("channel 只能是 wechat、qidian、qq")
    if data["dir"] not in _DIRS:
        raise ValueError("dir 只能是 in 或 out")
    return data
