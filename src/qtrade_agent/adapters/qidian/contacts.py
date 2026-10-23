"""企点通讯录(只读主库,不翻聊天页)。

对接:询价工作台 ``GET /api/v1/accounts/{id}/contacts``。
SQL 与 ``qidian_monitor_all.Names.SQL`` 的好友两句一致:``Friends`` 与 ``QidianExternalInfo``。
``uin`` 逐字节 XOR;名称列按 UTF-16 码元 XOR。显示名优先备注,没有再用昵称。
"""
from __future__ import annotations

from .xor import decode_name_hex, decode_uin

CONTACT_SQL = (
    "SELECT 'F', hex(uin), hex(remark), hex(name) FROM Friends "
    "UNION ALL "
    "SELECT 'X', hex(uin), '', hex(nickname) FROM QidianExternalInfo"
)


def list_private_contacts(rows: list[tuple[str, str, str, str] | list[str]]) -> list[dict[str, str]]:
    """把主库两句查询的行合成 ``{uin, name, remark, kind:private}``。群不进这个列表。"""
    book: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in rows:
        if len(row) < 4:
            continue
        kind, uin_hex, remark_hex, name_hex = row[0], row[1] or "", row[2] or "", row[3] or ""
        if kind not in ("F", "X"):
            continue
        uin = "".join(ch for ch in decode_uin(uin_hex) if ch.isdigit())
        if not uin:
            continue
        slot = book.get(uin)
        if slot is None:
            slot = {"remark": "", "nick": ""}
            book[uin] = slot
            order.append(uin)
        if kind == "F":
            slot["remark"] = decode_name_hex(remark_hex)
            slot["nick"] = decode_name_hex(name_hex) or slot["nick"]
        elif not slot["nick"]:
            slot["nick"] = decode_name_hex(name_hex)
    out = []
    for uin in order:
        slot = book[uin]
        remark = slot["remark"]
        out.append({"uin": uin, "name": remark or slot["nick"], "remark": remark, "kind": "private"})
    return out
