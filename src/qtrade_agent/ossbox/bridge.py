"""出站轮询的纯函数骨架。

给定目录、绑定和消息列表，返回本轮要上传的对象名与密文。
不连接 OSS，不发邮件，不操作企点、QQ 或微信。调用方按 ``POLL_INTERVAL_SEC`` 周期调用。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from .bind import Dedup, deposit_allows, xunjia_allows
from .crypto import seal
from .images import pick_original_image
from .keys import out_object_name
from .schema import validate_message
from .split import catalog_pieces

# 规格默认轮询间隔。本模块不睡眠、不拉网。
POLL_INTERVAL_SEC = 5


@dataclass(frozen=True)
class PlannedUpload:
    """一个待上传对象。``ciphertext`` 是密封后的字节，不是明文。"""

    name: str
    ciphertext: bytes


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj[key] if key in obj else default
    try:
        return getattr(obj, key)
    except AttributeError:
        return default


def _plaintext(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _transport(binding: Any) -> str:
    value = str(_field(binding, "transport", "") or "").strip()
    if value not in ("oss", "imap"):
        raise ValueError("请选择公网上报或邮件中转，不能同时使用")
    return value


def plan_uploads(
    catalog: Any,
    binding: Any,
    messages: list[Any],
    *,
    key: bytes,
    consumer: str,
    dedup: Dedup | None = None,
    seq_start: int = 1,
    images: dict[str, list[dict[str, Any]]] | None = None,
) -> list[PlannedUpload]:
    """编排一轮出站上传。

    目录分片不从这里上传。单钥密封会盖掉询价和存款都能解开的双封套。
    存款只在 transport 为 oss 时上传绑定群的消息。询价上传所绑企点号的全部消息。
    同一个 ``ext_msg_id`` 在 ``dedup`` 里只出现一次。序号从 ``seq_start``（默认 1）递增。
    """
    if consumer not in ("deposit", "xunjia"):
        raise ValueError("consumer 只能是 deposit 或 xunjia")
    if type(seq_start) is not int or seq_start < 1:
        raise ValueError("seq 必须是从 1 起的十进制正整数")

    pieces = catalog_pieces(catalog)
    workbench_id = str(pieces[0][1]["workbench_id"])
    bound_id = _field(binding, "workbench_id")
    if bound_id not in (None, "") and bound_id != workbench_id:
        raise ValueError("绑定与目录的工作台不一致")

    uploads: list[PlannedUpload] = []
    if consumer == "deposit" and _transport(binding) == "imap":
        return uploads

    allow = deposit_allows if consumer == "deposit" else xunjia_allows
    seen = dedup if dedup is not None else Dedup()
    seq = seq_start
    for message in messages:
        if allow(binding, message) is not True:
            continue
        ext_msg_id = _field(message, "ext_msg_id")
        if ext_msg_id in (None, "") or seen.seen_event(ext_msg_id) is not True:
            continue
        payload = dict(message) if isinstance(message, dict) else message
        chosen = pick_original_image((images or {}).get(str(ext_msg_id), []))
        if chosen is not None and isinstance(payload, dict):
            payload = dict(payload)
            payload["image_kind"] = "原图"
            payload["image_name"] = str(chosen.get("name") or "origin.bin")
            if _transport(binding) == "oss":
                payload["image_b64"] = base64.b64encode(chosen["body"]).decode("ascii")
        body = validate_message(payload)
        uploads.append(
            PlannedUpload(out_object_name(workbench_id, consumer, seq), seal(key, _plaintext(body))),
        )
        seq += 1
    return uploads
