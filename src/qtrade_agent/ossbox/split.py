"""把一份工作台目录拆成总索引和按账号分开的通讯录。

总索引只保留工作台和账号身份。微信群、企点通讯录、QQ 通讯录各自一个对象。
改某一份通讯录时，其它账号的对象正文保持不变。
"""
from __future__ import annotations

from typing import Any

from .keys import book_object_name, index_object_name
from .schema import validate_catalog

_HEAVY = ("groups", "contacts", "org")
_CHANNELS = frozenset({"wechat", "qidian", "qq"})


def catalog_pieces(catalog: Any) -> list[tuple[str, dict[str, Any]]]:
    """返回 ``(对象名, 明文)``。第一份是索引，后面每个账号一份。

    索引里没有 groups、contacts、org。微信对象只有群，企点和 QQ 对象只有通讯录。
    """
    data = validate_catalog(catalog)
    workbench_id = str(data["workbench_id"])
    accounts_in = data.get("accounts")
    if not isinstance(accounts_in, list):
        raise ValueError("目录缺少 accounts")
    index_accounts: list[dict[str, Any]] = []
    books: list[tuple[str, dict[str, Any]]] = []
    for account in accounts_in:
        if not isinstance(account, dict):
            raise ValueError("目录账号必须是映射")
        channel = account.get("channel")
        if channel not in _CHANNELS:
            raise ValueError("channel 只能是 wechat、qidian、qq")
        uid = str(account["account_uid"])
        collector = str(account.get("collector_id") or "").strip()
        name = book_object_name(workbench_id, str(channel), uid, collector)
        head = {key: value for key, value in account.items() if key not in _HEAVY}
        head["book"] = name[len(f"wb/{workbench_id}/"):]
        index_accounts.append(head)
        books.append((name, _book(account, str(channel), uid, workbench_id)))
    index = {
        "v": 1,
        "workbench_id": workbench_id,
        "accounts": index_accounts,
    }
    if data.get("updated_at") not in (None, ""):
        index["updated_at"] = data["updated_at"]
    return [(index_object_name(workbench_id), index), *books]


def _book(account: dict[str, Any], channel: str, uid: str, workbench_id: str) -> dict[str, Any]:
    if channel == "wechat":
        return {
            "v": 1,
            "workbench_id": workbench_id,
            "channel": "wechat",
            "account_uid": uid,
            "groups": account.get("groups") or [],
        }
    book: dict[str, Any] = {
        "v": 1,
        "workbench_id": workbench_id,
        "channel": channel,
        "account_uid": uid,
        "contacts": account.get("contacts") or [],
    }
    if "org" in account and account["org"] is not None:
        book["org"] = account["org"]
    return book
