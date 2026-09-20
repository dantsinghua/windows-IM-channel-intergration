"""chatlog 行 → 统一 ``Message``(06 §2.9.1 微信行 + §2.9.2 去重键)。

逐条对应规格:
- **去重键**:``ext_msg_id = "{talker}:{seq}"``(C-22 采 02:带 talker 无论 seq 是否全局递增都安全,与 ``cursors.kind=chatlog_seq:<talker>`` 配套);
  ``dedup_kind='native'``;重叠窗(``cursor − lookback_overlap_s``,WinAgent 侧取窗)必然重复取到边界几条,全靠这个键挡。
- **会话**:``session.id = {account_id}:{talker}``;``talker`` 以 ``@chatroom`` 结尾 ⇒ ``kind='group'``(chatlog ``isChatRoom``)。
- **类型**:chatlog ``type`` 1=text、3=image、34=voice、49 含 file、43=video,其余 ``unknown`` 保留 ``raw_ref``(06 §2.9.1)。
- **`self`**:chatlog ``isSelf``;``self=true`` 的行按 06 §2.12「入向轮询撞到自己发的」构造为 ``dir='out'``,
  由 ``store.ingest`` 合并进 ``SENDING`` 行(``confirmed_by='chatlog'``)。
- **撤回**:``isRevoked``(collector ``message_to_row`` 读 ``raw.isRevoked/is_revoked/revoked``),轮询到同 seq 且变真时更新(06 §2.9.4)。
- **不调 `clean_text`**:``clean_text`` 是**企点读库解码侧**专用(06 §2.9.2 分工逐字定死),微信正文由 chatlog 给出,
  两侧比较一律只过 ``norm()``(06 §2.12)。
- **媒体**:chatlog 给 ``/image/<md5>`` 引用,由 Agent 经 #40 拉(06 §2.9.1);这里只落引用,不下载、不算 sha256
  ⇒ ``fingerprint`` 的 ``media_sha256s`` 为空串(06 §2.9.2 公式:无媒体 = 空串)。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ...models import Message, MsgType, Session

# 06 §2.9.1 微信 type 映射(collector 只收 1/3,本系统扩到五类,其余 unknown 保留 raw_ref)
TYPE_MAP: dict[int, MsgType] = {1: "text", 3: "image", 34: "voice", 49: "file", 43: "video"}
MEDIA_TYPES = frozenset({"image", "voice", "file", "video"})
GROUP_SUFFIX = "@chatroom"


def session_kind(talker: str) -> str:
    """06 §2.9.1:微信 ``talker`` 以 ``@chatroom`` 结尾 → ``group``(chatlog ``isChatRoom``)。"""
    return "group" if talker.endswith(GROUP_SUFFIX) else "private"


def ext_msg_id(talker: str, seq: int) -> str:
    """06 §2.9.2 微信 ``native`` 去重键。"""
    return f"{talker}:{int(seq)}"


def _first(row: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _ts_ms(raw: Any) -> int:
    """chatlog 的 ``time``:epoch 秒 / epoch 毫秒 / ISO 8601 三种写法都扛(真实客户端各版本写法不一)。"""
    if isinstance(raw, bool):
        raise ValueError("time 不能是 bool")
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if v >= 100_000_000_000 else v * 1000        # ≥1e11 视为毫秒(1973 年之后的秒级时间戳都 <1e11)
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return _ts_ms(int(s))
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    raise ValueError(f"无法解析的 time: {raw!r}")


def _media_of(row: dict[str, Any], mtype: MsgType) -> list[dict[str, Any]]:
    if mtype not in MEDIA_TYPES:
        return []
    key = _first(row, "md5", "MD5", "key", "imgMd5")
    if not key:
        return []
    return [{"kind": mtype, "key": str(key), "ref": f"/wa/v1/wechat/media/{key}"}]     # #40 由 Agent 代拉


def to_message(account_id: str, row: dict[str, Any], *, self_uid: Optional[str] = None,
               self_nick: Optional[str] = None, raw_ref: Optional[str] = None) -> Message:
    """一行 chatlog(经 WinAgent ``normalize`` 后的形态)→ 一条 ``Message``;调用方保证 ``talker``/``seq`` 存在。"""
    talker = str(row["talker"])
    seq = int(row["seq"])
    raw_type = _first(row, "type", "msgType", "msgtype")
    mtype: MsgType = TYPE_MAP.get(int(raw_type), "unknown") if raw_type is not None else "unknown"
    is_self = bool(_first(row, "isSelf", "is_self", "self") or False)
    text = _first(row, "content", "text")
    revoked = bool(_first(row, "isRevoked", "is_revoked", "revoked") or False)
    sender_id = _first(row, "senderUserName", "sender", "talkerUserName")
    sender_name = _first(row, "senderNickName", "nickName", "senderName")
    if is_self:
        sender_id = sender_id or self_uid
        sender_name = sender_name or self_nick
    return Message(
        account_id=account_id, channel="wechat",
        session=Session(account_id, talker, session_kind(talker), name=str(_first(row, "talkerNickName", "roomName", "") or "")),
        dir="out" if is_self else "in",                        # 06 §2.12:self 行按出向读回行构造,交给 ingest 合并
        type=mtype,
        text=str(text) if text is not None else None,
        ts_ms=_ts_ms(_first(row, "time", "ts", "createTime")),
        source="chatlog",
        ext_msg_id=ext_msg_id(talker, seq),
        dedup_kind="native",
        sender_id=str(sender_id) if sender_id is not None else None,
        sender_name=str(sender_name) if sender_name is not None else None,
        self=is_self,
        state="DELIVERED",                                     # 读回行已在聊天记录里;入向行 store 恒置 DELIVERED
        media=_media_of(row, mtype),
        revoked=revoked,
        raw_ref=raw_ref,
    )
