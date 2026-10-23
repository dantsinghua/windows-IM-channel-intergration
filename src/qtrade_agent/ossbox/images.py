"""聊天图片原图选择。表情包不送。只有缩略图时不把文件当原图送出。"""
from __future__ import annotations

from typing import Any

_EMOJI = frozenset({"表情", "表情包", "emoji"})
_THUMB = frozenset({"缩略图"})
_PHOTO_TYPES = frozenset({3, "3", "image", "原图", "高清图"})
_EMOJI_TYPES = frozenset({47, "47"})


def _kind(item: dict[str, Any]) -> str:
    raw = item.get("type", item.get("msgType", item.get("kind", item.get("image_kind"))))
    label = str(item.get("kind") or item.get("image_kind") or "")
    if raw in _EMOJI_TYPES or label in _EMOJI:
        return "表情"
    if label in _THUMB:
        return "缩略图"
    if raw in _PHOTO_TYPES or label in _PHOTO_TYPES:
        return "原图"
    return "其他"


def pick_original_image(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    """在同一条图片消息的文件里选最大的原图。表情包和纯缩略图返回空。"""
    originals = [item for item in files if _kind(item) == "原图" and item.get("body")]
    if not originals:
        return None
    return max(originals, key=lambda item: int(item.get("size") or len(item.get("body") or b"")))
