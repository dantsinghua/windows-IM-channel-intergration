"""OneBot 事件 → 基线 §7.4 ``Message`` 的归一化 —— 规格唯一出处:docs/06 §2.9.1 / §2.9.2 的 QQ 行,docs/02 §2.8.1 QQ 行。

逐字落地的几条:
- ``session.id = {account_id}:{原生ID}``(00 §6);**QQ 原生 ID:群 = ``g_<群号>``、单聊 = ``<对端 uin>``**
  (06 §2.9.1「群加 ``g_`` 前缀与 QQ 通道同惯例」——企点那条是跟 QQ 学的,这里是本家)。
- ``ext_msg_id = "{原生会话ID}:{message_id}"``(06 §2.9.2 / 02 §2.8.1 R6-51 订正:**会话编进 ext**,跨群同号不撞);
  ``dedup_kind='native'``。``message_id`` 复用的 ``#n`` 后缀由 ``store._ingest_one`` 统一处理,本模块不掺和。
- ``type``:段类型 ``text/image/record(→voice)/file/video``,其它 → ``unknown``;**多段混合取首个非 text 段的类型,正文保留 text 段**。
- ``self``:``user_id == self_id``(06 §2.9.1);NapCat 另有 ``post_type='message_sent'`` 的我方消息事件,一并判 ``self``。
- ``source='onebot'``(00 §7.4 / 02 §2.8.1);``ts`` = OneBot ``time``(秒)× 1000。
- 🔴 **不过 ``clean_text``**:06 §2.9.2「分工逐字定死」——``clean_text`` 只在**企点读库路**的 ``to_message()`` 里调用一次;
  QQ 是推型、正文由 napcat 给成段,本路不清洗、不改写(``norm()`` 只在比较侧由 store 调)。
"""
from __future__ import annotations

from typing import Any, Optional

from ...models import Message, MsgType, Session, SessionKind

# 06 §2.9.1:QQ 段类型 text/image/record(→voice)/file/video/其它→unknown
SEGMENT_TYPE: dict[str, MsgType] = {
    "text": "text",
    "image": "image",
    "record": "voice",
    "file": "file",
    "video": "video",
}
MEDIA_KINDS = frozenset({"image", "voice", "file", "video"})

GROUP_PREFIX = "g_"


def native_id_of(*, message_type: Optional[str] = None, group_id: Any = None, user_id: Any = None,
                 target_id: Any = None, is_self: bool = False) -> tuple[str, SessionKind]:
    """(06 §2.9.1)群 → ``("g_<群号>", "group")``;单聊 → ``("<对端 uin>", "private")``。

    我方发出的单聊事件(``post_type='message_sent'``)里 ``user_id`` 是自己、对端在 ``target_id``,故 ``is_self`` 时优先 ``target_id``。
    """
    if group_id is not None or message_type == "group":
        if group_id is None:
            raise ValueError("群消息缺少 group_id")
        return f"{GROUP_PREFIX}{group_id}", "group"
    peer = target_id if (is_self and target_id is not None) else user_id
    if peer is None:
        raise ValueError("单聊消息缺少 user_id/target_id")
    return str(peer), "private"


def ext_msg_id(native_id: str, message_id: Any) -> str:
    """06 §2.9.2:``ext_msg_id = "{原生会话ID}:{message_id}"``。"""
    return f"{native_id}:{message_id}"


def segments(message: Any) -> list[dict[str, Any]]:
    """OneBot 的 ``message`` 既可能是段数组、也可能是 CQ 码字符串(``message_format='string'``);字符串按单个 text 段处理。"""
    if message is None:
        return []
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    if isinstance(message, dict):
        return [message]
    return [s for s in message if isinstance(s, dict)]


def text_type_media(message: Any) -> tuple[str, MsgType, list[dict[str, Any]]]:
    """段数组 → ``(正文, type, media[])``。

    - 正文 = 所有 ``text`` 段的 ``data.text`` 顺序拼接(06 §2.9.1「正文保留 text 段」)。
    - ``type`` = **首个非 text 段**的映射类型;全是 text(或没有段)⇒ ``text``;未知段类型 ⇒ ``unknown``。
    - ``media[]`` = 可下载引用(02 §2.8.2 的下载/落盘本期未接,故只记引用、无 ``sha256``)。
    """
    texts: list[str] = []
    mtype: Optional[MsgType] = None
    media: list[dict[str, Any]] = []
    for seg in segments(message):
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            texts.append(str(data.get("text") or ""))
            continue
        mapped = SEGMENT_TYPE.get(str(stype), "unknown")
        if mtype is None:
            mtype = mapped
        if mapped in MEDIA_KINDS:
            media.append({"kind": mapped, "file": data.get("file"), "url": data.get("url"), "state": "pending"})
    return "".join(texts), (mtype or "text"), media


def sender_of(event: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """``(sender_id, sender_name)``:id = ``sender.user_id``(缺则事件 ``user_id``);name 取群名片优先、否则昵称。"""
    s = event.get("sender") or {}
    sid = s.get("user_id", event.get("user_id"))
    name = s.get("card") or s.get("nickname")
    return (None if sid is None else str(sid)), (None if name in (None, "") else str(name))


def is_self_event(event: dict[str, Any]) -> bool:
    """06 §2.9.1:``user_id == self_id``;NapCat 的 ``post_type='message_sent'`` 同样是我方消息。"""
    if event.get("post_type") == "message_sent":
        return True
    self_id, user_id = event.get("self_id"), event.get("user_id")
    return self_id is not None and user_id is not None and str(self_id) == str(user_id)


def to_message(account_id: str, event: dict[str, Any], *, raw_ref: Optional[str] = None,
               self_uid: Optional[str] = None) -> Message:
    """一条 OneBot ``message`` / ``message_sent`` 事件 → ``Message``(00 §7.4)。调用方负责 ``store.ingest`` 与发事件。

    我方消息落 ``dir='out'``、``state='DELIVERED'``:未命中出向 ``SENDING`` 行时它就是「非本系统发出的我方消息」
    (人在手机 QQ 上发的),命中则由 ``store._ingest_one`` ③ 合并进那一行(06 §2.12)。
    """
    is_self = is_self_event(event)
    native_id, kind = native_id_of(message_type=event.get("message_type"), group_id=event.get("group_id"),
                                   user_id=event.get("user_id"), target_id=event.get("target_id"), is_self=is_self)
    text, mtype, media = text_type_media(event.get("message"))
    sid, sname = sender_of(event)
    if is_self and self_uid:
        sid = self_uid                      # 出向行两侧 sender 取同一口径,保证 fingerprint 可比(06 §2.9.2)
    group_name = (event.get("group_name") or "") if kind == "group" else ""
    return Message(
        account_id=account_id,
        channel="qq",
        session=Session(account_id, native_id, kind, name=str(group_name)),
        dir="out" if is_self else "in",
        type=mtype,
        text=text,
        ts_ms=int(event.get("time") or 0) * 1000,
        source="onebot",
        ext_msg_id=ext_msg_id(native_id, event.get("message_id")),
        dedup_kind="native",
        sender_id=sid,
        sender_name=sname,
        self=is_self,
        state="DELIVERED",
        media=media,
        raw_ref=raw_ref,
    )


def message_seq_of(event: dict[str, Any]) -> Optional[int]:
    """``cursors(owner='qqNN', kind='onebot_seq:<session>').value_int = message_seq``(06 §2.9.3 / 02 §2.8.3,补历史用)。"""
    for key in ("message_seq", "real_seq", "seq"):
        v = event.get(key)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def split_session(account_id: str, session: str) -> tuple[str, SessionKind]:
    """``send_*``/``read_messages`` 的 ``session`` 参数(02 §2.2.2:一律叫 ``session``)→ ``(native_id, kind)``。

    接受 ``{account_id}:{原生ID}`` 与裸原生 ID 两种;拆分规则 = 00 §6(前 4 字符 = account_id、第 5 字符必为 ``:``)。
    """
    native = session[len(account_id) + 1:] if session.startswith(account_id + ":") else session
    if not native:
        raise ValueError(f"session 无原生 ID:{session!r}")
    return native, ("group" if native.startswith(GROUP_PREFIX) else "private")


def action_for(native_id: str, kind: SessionKind, text: str) -> tuple[str, dict[str, Any]]:
    """02 §2.2.3:``send_group_msg`` / ``send_private_msg`` 的参数构造(正文按单个 text 段发,不拼 CQ 码)。"""
    message = [{"type": "text", "data": {"text": text}}]
    if kind == "group":
        return "send_group_msg", {"group_id": int(native_id[len(GROUP_PREFIX):]), "message": message}
    return "send_private_msg", {"user_id": int(native_id), "message": message}


def history_action_for(native_id: str, kind: SessionKind, count: int) -> tuple[str, dict[str, Any]]:
    """02 §2.8.1 QQ 行(P-13):掉线重连后补拉 ``get_group_msg_history`` / ``get_friend_msg_history``。"""
    if kind == "group":
        return "get_group_msg_history", {"group_id": int(native_id[len(GROUP_PREFIX):]), "count": count}
    return "get_friend_msg_history", {"user_id": int(native_id), "count": count}
