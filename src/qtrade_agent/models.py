"""核心数据模型(API 视图)—— 规格:docs/00 §7.2 Command、§7.3 CommandResult、§7.4 Message。

只放跨模块共用的结构;DDL 见 02 §3.1(src/qtrade_agent/store/schema_agent.sql)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Channel = Literal["qidian", "qq", "wechat"]
Dir = Literal["in", "out"]
MsgType = Literal["text", "image", "voice", "file", "video", "system", "unknown"]
OutState = Literal["SENDING", "DELIVERED", "UNCONFIRMED", "FAILED"]
Source = Literal["chatlog", "onebot", "qidian_db", "frida", "uiautomator", "screenshot", "ui"]
SessionKind = Literal["group", "private"]


@dataclass
class Session:
    """``session.id = {account_id}:{native_id}``(00 §6);企点 native_id 单聊 = <对端uin>、群 = g_<群号>。"""
    account_id: str
    native_id: str
    kind: SessionKind
    name: str = ""

    @property
    def id(self) -> str:
        return f"{self.account_id}:{self.native_id}"


@dataclass
class Message:
    """00 §7.4 Message 的一行(落库视图)。事件专属的 lag_s/late/origin 不在这里,见 events.message_payload()。"""
    account_id: str
    channel: Channel
    session: Session
    dir: Dir
    type: MsgType
    text: Optional[str]
    ts_ms: int
    source: Source
    ext_msg_id: Optional[str] = None            # 出向 SENDING 行为 None
    dedup_kind: Literal["native", "anchor"] = "native"
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    self: bool = False
    state: OutState = "DELIVERED"               # 入向恒 DELIVERED(CHECK 约束)
    media: list[dict[str, Any]] = field(default_factory=list)
    revoked: bool = False
    revoked_ms: Optional[int] = None
    revoked_by: Optional[str] = None
    trace_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    raw_ref: Optional[str] = None
    id: Optional[str] = None                    # msg_ + ULID,入库时分配
    received_ms: Optional[int] = None           # 入库时刻,store 填
    fingerprint: Optional[str] = None           # store 入库时算一次(06 §2.9.2)

    @property
    def session_id(self) -> str:
        return self.session.id


@dataclass
class CommandOrigin:
    transport: Literal["local", "http", "email"] = "local"
    actor: str = "token:console"
    ip: Optional[str] = None


@dataclass
class Command:
    """00 §7.2:进入总线的统一对象。"""
    account_id: str
    op: str
    args: dict[str, Any]
    idempotency_key: Optional[str] = None
    confirm: bool = True
    timeout_ms: int = 30000
    origin: CommandOrigin = field(default_factory=CommandOrigin)
    trace_id: Optional[str] = None              # 进入总线时生成
    submitted_at_ms: Optional[int] = None


@dataclass
class CommandError:
    message: str
    reason: str = ""
    retryable: bool = False
    needs_human: bool = False
    details: list[dict[str, Any]] = field(default_factory=list)   # [{pointer:'/text', ...}](JSON Pointer)


@dataclass
class CommandResult:
    """00 §7.3;``code`` 取值见 00 §8.3。"""
    ok: bool
    code: str
    trace_id: str
    data: dict[str, Any] = field(default_factory=dict)
    cost_ms: int = 0
    source: Optional[str] = None
    state_before: Optional[str] = None
    state_after: Optional[str] = None
    error: Optional[CommandError] = None


# 00 §8.3 结果码 → (retryable, needs_human);仅登记本期骨架会产出的码
RESULT_CODES: dict[str, tuple[bool, bool]] = {
    "OK": (False, False),
    "DELIVERED": (False, False),
    "SEND_CALLED_BUT_UNCONFIRMED": (False, False),
    "SEND_FAILED": (True, False),
    "IDEMPOTENT_REPLAY": (False, False),
    "GATE_BLOCKED": (False, True),           # 闸拦下 = 要人改白名单/词表,不是重试能过的
    "UNSUPPORTED": (False, False),
    "NOT_APPLICABLE": (False, False),
    "NOT_READY": (True, False),
    "TARGET_NOT_FOUND": (False, False),
    "LOGIN_REQUIRED": (False, True),
    "CAPTCHA_REQUIRED": (False, True),
    "TIMEOUT": (True, False),
    "RESOURCE_EXHAUSTED": (False, False),
    "INVALID_ARGS": (False, False),
    "UNAUTHORIZED": (False, False),
    "FORBIDDEN": (False, False),
    "RATE_LIMITED": (False, False),
    "DISK_FULL": (False, True),
    "CONFIRM_EXPIRED": (False, False),
    "INTERNAL": (True, False),
}


def json_safe(value: Any) -> Any:
    """把 ``CommandResult.data`` 里的**裸二进制**换成可序列化的占位,供**落库**与**出 JSON** 两处共用。

    截图这类能力的 ``data`` 里确实会带图片体(02 §3.4.2「返回图片体」),而
    ``command_results.data_json`` 与 HTTP 响应都要 ``json.dumps`` —— 不换就 ``TypeError: Object of type
    bytes is not JSON serializable``,整条能力经总线直接 ``INTERNAL``(见 rulings R6-58 (cu))。
    占位保留长度,调用方想要真字节走端点的二进制出口或 ``media/``。
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__binary__": True, "len": len(bytes(value))}
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value
