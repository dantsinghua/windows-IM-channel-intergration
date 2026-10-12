"""对象名。段内不许夹正文。

- ``wb/{workbench_id}/index.json``：这一台工作台有哪些账号
- ``wb/{workbench_id}/wechat/{account_uid}/groups.json``：这个微信号的群
- ``wb/{workbench_id}/qidian/{account_uid}/contacts.json``：这个企点号的通讯录
- ``wb/{workbench_id}/qq/{account_uid}/contacts.json``：这个 QQ 号的通讯录
- 采集端编号存在时，插在工作台和通道之间，不同采集端各写各的文件
- ``wb/{workbench_id}/catalog.json``：旧的整包，只读兼容，新发布不再写它
- ``wb/{workbench_id}/out/{consumer}/{seq}.bin``
- ``wb/{workbench_id}/in/{consumer}/{cmd_id}.bin``
- ``wb/{workbench_id}/ack/{cmd_id}.bin``

非法对象名抛异常，不返回 False。
"""
from __future__ import annotations

import re

_CONSUMERS = frozenset({"deposit", "xunjia"})
# 标识：字母数字、下划线、连字符。空格、斜杠、正文里的标点和汉字都不是标识。
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_SEQ = re.compile(r"[1-9][0-9]*")
_WB = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_NAME = re.compile(
    rf"^wb/{_WB}/(?:"
    rf"index\.json"
    rf"|catalog\.json"
    rf"|(?:{_WB}/)?wechat/{_WB}/groups\.json"
    rf"|(?:{_WB}/)?(?:qidian|qq)/{_WB}/contacts\.json"
    rf"|out/(?:deposit|xunjia)/[1-9][0-9]*\.bin"
    rf"|in/(?:deposit|xunjia)/{_WB}\.bin"
    rf"|ack/{_WB}\.bin"
    rf")$"
)


def _workbench(workbench_id: object) -> str:
    if not isinstance(workbench_id, str) or _IDENT.fullmatch(workbench_id) is None:
        raise ValueError("非法工作台段")
    return workbench_id


def _consumer(consumer: object) -> str:
    if consumer not in _CONSUMERS:
        raise ValueError("consumer 只能是 deposit 或 xunjia")
    return str(consumer)


def _seq(seq: object) -> str:
    if type(seq) is not int or seq < 1:
        raise ValueError("seq 必须是从 1 起的十进制正整数")
    text = str(seq)
    if _SEQ.fullmatch(text) is None:
        raise ValueError("seq 必须是从 1 起的十进制正整数")
    return text


def _cmd(cmd_id: object) -> str:
    """cmd_id 是标识：非空、不含斜杠，也不能拿消息正文来充。"""
    if not isinstance(cmd_id, str) or _IDENT.fullmatch(cmd_id) is None:
        raise ValueError("非法 cmd_id")
    return cmd_id


def catalog_object_name(workbench_id: str) -> str:
    """旧整包对象名。新发布改走 index 和按账号拆开的通讯录。"""
    name = f"wb/{_workbench(workbench_id)}/catalog.json"
    validate_object_name(name)
    return name


def index_object_name(workbench_id: str) -> str:
    """总文件。只列这一台工作台有哪些账号，不含群和通讯录正文。"""
    name = f"wb/{_workbench(workbench_id)}/index.json"
    validate_object_name(name)
    return name


def book_object_name(
    workbench_id: str,
    channel: str,
    account_uid: str,
    collector_id: str = "",
) -> str:
    """一个账号一份。微信是群，企点和 QQ 是通讯录。采集端不同就再隔开一层。"""
    if channel == "wechat":
        leaf = "groups.json"
    elif channel in ("qidian", "qq"):
        leaf = "contacts.json"
    else:
        raise ValueError("channel 只能是 wechat、qidian、qq")
    parts = ["wb", _workbench(workbench_id)]
    collector = str(collector_id or "").strip()
    if collector:
        parts.append(_workbench(collector))
    parts.extend([channel, _workbench(account_uid), leaf])
    name = "/".join(parts)
    validate_object_name(name)
    return name


def out_object_name(workbench_id: str, consumer: str, seq: int) -> str:
    name = f"wb/{_workbench(workbench_id)}/out/{_consumer(consumer)}/{_seq(seq)}.bin"
    validate_object_name(name)
    return name


def in_object_name(workbench_id: str, consumer: str, cmd_id: str) -> str:
    name = f"wb/{_workbench(workbench_id)}/in/{_consumer(consumer)}/{_cmd(cmd_id)}.bin"
    validate_object_name(name)
    return name


def ack_object_name(workbench_id: str, cmd_id: str) -> str:
    name = f"wb/{_workbench(workbench_id)}/ack/{_cmd(cmd_id)}.bin"
    validate_object_name(name)
    return name


def validate_object_name(name: str) -> bool:
    """合法返回 True。非法抛异常，不返回 False。"""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("非法对象名")
    return True
