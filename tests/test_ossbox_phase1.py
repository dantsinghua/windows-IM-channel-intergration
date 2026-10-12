"""014 第一期 ossbox 纯函数验收。

规格：specs/014-nyoss-imap-phase1/speckit.md 验收 1–9；
模块边界：同目录 plan.md。只调用纯函数，不连网络、OSS、邮箱或三通道。
密钥仅为测试用字节，不是环境变量里的真实密钥。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable

import pytest

from qtrade_agent.mail.templates import IBQUOTE_BODY_FIELDS
from qtrade_agent.ossbox.bind import Dedup, command_allowed, deposit_allows, xunjia_allows
from qtrade_agent.ossbox.crypto import open_sealed, seal
from qtrade_agent.ossbox.bridge import plan_uploads
from qtrade_agent.ossbox.images import pick_original_image
from qtrade_agent.ossbox.imap_out import render_selected_group, should_send_imap
from qtrade_agent.ossbox.keys import (
    ack_object_name,
    catalog_object_name,
    in_object_name,
    out_object_name,
    validate_object_name,
)
from qtrade_agent.ossbox.schema import (
    validate_catalog,
    validate_command,
    validate_message,
    validate_receipt,
)

# 32 字节测试密钥。只用于断言「解不开」和「异常里不出现密钥」。
_KEY = b"0123456789abcdef0123456789abcdef"
_WRONG_KEY = b"fedcba9876543210fedcba9876543210"
_PLAIN = "固收群报价 3M 1.52 可谈".encode()
_BODY = "3M 1.52 可谈"


class _Plain(dict):
    """规格明文。实现可读属性，也可按下标读。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _plain(data: dict[str, Any]) -> _Plain:
    out = _Plain()
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = _plain(value)
        elif isinstance(value, list):
            out[key] = [_plain(item) if isinstance(item, dict) else item for item in value]
        else:
            out[key] = value
    return out


def _must_raise(action: Callable[[], Any], why: str) -> None:
    try:
        action()
    except Exception:
        return
    raise AssertionError(why)


def _serialized(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, (bytes, bytearray)):
        return bytes(result).decode()
    if hasattr(result, "model_dump"):
        return json.dumps(result.model_dump(), ensure_ascii=False)
    if hasattr(result, "__dict__") and not isinstance(result, dict):
        return json.dumps(
            {key: value for key, value in vars(result).items() if not key.startswith("_")},
            ensure_ascii=False,
        )
    return json.dumps(result, ensure_ascii=False)


def _has_label_key(value: Any) -> bool:
    if isinstance(value, str):
        try:
            return _has_label_key(json.loads(value))
        except json.JSONDecodeError:
            return '"label"' in value
    if isinstance(value, dict):
        return "label" in value or any(_has_label_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_label_key(item) for item in value)
    if hasattr(value, "model_dump"):
        return _has_label_key(value.model_dump())
    if hasattr(value, "__dict__") and not isinstance(value, dict):
        return _has_label_key(vars(value))
    return False


def _assert_secret_absent(exc: BaseException, *secrets: bytes) -> None:
    """密钥的原文和十六进制都不得出现在异常文本或参数里。"""
    parts = [str(exc), repr(exc)]
    for arg in exc.args:
        parts.append(str(arg))
        parts.append(repr(arg))
        if isinstance(arg, (bytes, bytearray)):
            raw = bytes(arg)
            for secret in secrets:
                assert secret not in raw
    text = "\n".join(parts)
    for secret in secrets:
        assert secret.decode("ascii") not in text
        assert secret.hex() not in text


def _deposit_binding(**overrides: Any) -> _Plain:
    data: dict[str, Any] = {
        "workbench_id": "wb01",
        "wxid": "wxid_abc",
        "group_native_ids": ["g_123456", "g_999"],
        "transport": "imap",
    }
    data.update(overrides)
    return _plain(data)


def _wechat_group(native_id: str = "g_123456", **overrides: Any) -> _Plain:
    data: dict[str, Any] = {
        "event_id": "evt-wx-1",
        "workbench_id": "wb01",
        "channel": "wechat",
        "account_uid": "wxid_abc",
        "account_nick": "柜面微信",
        "session": {"kind": "group", "native_id": native_id, "name": "固收报价群"},
        "dir": "in",
        "text": _BODY,
        "ts": "2026-10-12T01:00:00+08:00",
        "ext_msg_id": "wx:1001",
    }
    data.update(overrides)
    return _plain(data)


def _xunjia_binding() -> _Plain:
    # 身份字段是 self_uid。过滤要求 message.account_uid 等于这个企点号。
    return _plain({"workbench_id": "wb01", "self_uid": "qd01", "account_uid": "qd01"})


def _qidian_message(**overrides: Any) -> _Plain:
    data: dict[str, Any] = {
        "event_id": "evt-qd-1",
        "workbench_id": "wb01",
        "channel": "qidian",
        "account_uid": "qd01",
        "account_nick": "询价企点",
        "session": {"kind": "private", "native_id": "peer01", "name": ""},
        "dir": "in",
        "text": "这句对不上任何券",
        "ts": "2026-10-12T01:00:00+08:00",
        "ext_msg_id": "qd:8001",
    }
    data.update(overrides)
    return _plain(data)


def _command() -> _Plain:
    return _plain({
        "cmd_id": "cmd01",
        "workbench_id": "wb01",
        "channel": "wechat",
        "account_uid": "wxid_abc",
        "op": "send_text",
        "session": {"kind": "group", "native_id": "g_123456"},
        "text": "你好",
        "idempotency_key": "idem-014-6",
    })


def _segment_keys(segment: str, *, image_kind: str) -> list[str]:
    """锁定信息段/扩展段里，空值按 omit 省略后应出现的键名。"""
    keys: list[str] = []
    for field in IBQUOTE_BODY_FIELDS:
        if field.get("segment") != segment:
            continue
        omitted = field.get("empty") == "omit" and not (field["key"] == "图片类型" and image_kind)
        if omitted:
            continue
        keys.append(str(field["key"]))
    return keys


def _mail_body(rendered: Any) -> str:
    if isinstance(rendered, str):
        return rendered
    return str(rendered.body_text)


def _split_mail(body: str) -> tuple[list[str], list[str]]:
    lines = [line for line in body.splitlines() if line.strip()]
    title_at = lines.index("微信群聊消息通知")
    ext = [line.split("：", 1)[0] for line in lines[:title_at]]
    info = [line.split("：", 1)[0] for line in lines[title_at + 1:]]
    return ext, info


def test_验收1_错误密钥解不开且同一明文两次密文不同(caplog: pytest.LogCaptureFixture):
    """验收 1、F2：错误密钥或损坏密文必须失败；同一明文两次密文不同；密钥不出现在异常消息里。"""
    caplog.set_level("DEBUG")
    first = seal(_KEY, _PLAIN)
    second = seal(_KEY, _PLAIN)
    assert isinstance(first, (bytes, bytearray))
    assert isinstance(second, (bytes, bytearray))
    assert bytes(first) != bytes(second)
    assert _PLAIN not in bytes(first)
    assert _KEY not in bytes(first)
    assert open_sealed(_KEY, first) == _PLAIN
    assert open_sealed(_KEY, second) == _PLAIN

    with pytest.raises(Exception) as wrong_key:
        open_sealed(_WRONG_KEY, first)
    _assert_secret_absent(wrong_key.value, _KEY, _WRONG_KEY)

    damaged = bytearray(first)
    damaged[-1] ^= 0x5A
    with pytest.raises(Exception) as broken:
        open_sealed(_KEY, bytes(damaged))
    _assert_secret_absent(broken.value, _KEY, _WRONG_KEY)
    assert _KEY.decode("ascii") not in caplog.text
    assert _WRONG_KEY.decode("ascii") not in caplog.text


def test_验收2_对象名拒绝非法消费者空段和正文夹带():
    """验收 2、F3：对象名只允许四类形态；非法 consumer、空段、其它前缀、正文夹带都拒绝。"""
    catalog = catalog_object_name("wb01")
    out_deposit = out_object_name("wb01", "deposit", 1)
    out_xunjia = out_object_name("wb01", "xunjia", 2)
    inbound = in_object_name("wb01", "xunjia", "cmd01")
    ack = ack_object_name("wb01", "cmd01")
    assert catalog == "wb/wb01/catalog.json"
    assert out_deposit == "wb/wb01/out/deposit/1.bin"
    assert out_xunjia == "wb/wb01/out/xunjia/2.bin"
    assert inbound == "wb/wb01/in/xunjia/cmd01.bin"
    assert ack == "wb/wb01/ack/cmd01.bin"
    for name in (catalog, out_deposit, out_xunjia, inbound, ack):
        assert _BODY not in name
        assert validate_object_name(name) is not False

    rejected = [
        "wb/wb01/out/qq/1.bin",
        "wb/wb01/in/Deposit/cmd01.bin",
        "wb//catalog.json",
        "wb/wb01/out//1.bin",
        "wb/wb01/in/deposit/.bin",
        "wb/wb01/ack/.bin",
        "oss/wb01/catalog.json",
        f"wb/wb01/out/deposit/1.bin/{_BODY}",
        f"wb/wb01/in/deposit/{_BODY}.bin",
        f"wb/{_BODY}/catalog.json",
        "wb/wb01/in/deposit/a/b.bin",
    ]
    for name in rejected:
        _must_raise(lambda name=name: validate_object_name(name), f"对象名应拒绝：{name}")

    _must_raise(lambda: out_object_name("wb01", "qq", 1), "非法 consumer 不得构造出站对象名")
    _must_raise(lambda: out_object_name("", "deposit", 1), "空工作台段不得构造对象名")
    _must_raise(lambda: catalog_object_name(""), "空工作台段不得构造目录对象名")
    _must_raise(lambda: in_object_name("wb01", "deposit", _BODY), "正文不得充当 cmd_id")
    _must_raise(lambda: in_object_name("wb01", "deposit", "a/b"), "cmd_id 不得含斜杠")
    _must_raise(lambda: ack_object_name("wb01", ""), "空 cmd_id 不得构造回执对象名")


def test_验收3_目录结果不含备注且缺字段指令被拒():
    """验收 3、F1、F4：目录校验结果不含 label；没有昵称仍保留账号；缺字段或非法指令被拒绝；失败回执不带正文。"""
    catalog = _plain({
        "workbench_id": "wb01",
        "label": "工作台备注",
        "accounts": [
            {
                "channel": "wechat",
                "account_uid": "wxid_abc",
                "account_nick": "柜面微信",
                "label": "微信备注",
                "groups": [{"native_id": "g_123456", "name": "固收报价群", "label": "群备注"}],
            },
            {
                "channel": "qidian",
                "account_uid": "qd01",
                "account_nick": "",
                "groups": [],
            },
        ],
    })
    checked = validate_catalog(catalog)
    canonical = catalog if checked is None else checked
    blob = _serialized(canonical)
    assert not _has_label_key(canonical)
    assert '"label"' not in blob
    assert "wxid_abc" in blob
    assert "qd01" in blob
    assert "g_123456" in blob
    assert "固收报价群" in blob
    assert "柜面微信" in blob

    missing_uid = _plain({
        "workbench_id": "wb01",
        "accounts": [{"channel": "wechat", "account_nick": "柜面微信", "groups": []}],
    })
    _must_raise(lambda: validate_catalog(missing_uid), "目录账号缺少 account_uid 应拒绝")

    sample = _command()
    validate_command(sample)
    for key in ("cmd_id", "workbench_id", "channel", "account_uid", "op", "session", "text", "idempotency_key"):
        broken = _plain(json.loads(_serialized(sample)))
        del broken[key]
        _must_raise(lambda broken=broken: validate_command(broken), f"指令缺少 {key} 应拒绝")
    for nested in ("kind", "native_id"):
        broken = _plain(json.loads(_serialized(sample)))
        del broken["session"][nested]
        _must_raise(lambda broken=broken: validate_command(broken), f"指令缺少 session.{nested} 应拒绝")

    bad_op = _plain(json.loads(_serialized(sample)))
    bad_op["op"] = "send_image"
    _must_raise(lambda: validate_command(bad_op), "第一期 op 只有 send_text")
    bad_channel = _plain(json.loads(_serialized(sample)))
    bad_channel["channel"] = "mail"
    _must_raise(lambda: validate_command(bad_channel), "channel 只能是 wechat、qidian、qq")
    bad_kind = _plain(json.loads(_serialized(sample)))
    bad_kind["session"]["kind"] = "room"
    _must_raise(lambda: validate_command(bad_kind), "session.kind 只能是 private 或 group")

    clean_receipt = _plain({"cmd_id": "cmd01", "ok": False, "code": "FAILED", "reason": "发送失败"})
    checked_receipt = validate_receipt(clean_receipt)
    receipt_blob = _serialized(clean_receipt if checked_receipt is None else checked_receipt)
    assert "发送失败" in receipt_blob
    assert "text" not in (checked_receipt if isinstance(checked_receipt, dict) else clean_receipt)

    leaked = _plain({
        "cmd_id": "cmd01",
        "ok": False,
        "code": "FAILED",
        "reason": "发送失败",
        "text": "这是不该出现的正文",
    })
    try:
        leaked_result = validate_receipt(leaked)
    except Exception:
        leaked_result = None
        leaked_blob = ""
    else:
        leaked_blob = _serialized(leaked if leaked_result is None else leaked_result)
    assert "这是不该出现的正文" not in leaked_blob

    no_reason = _plain({"cmd_id": "cmd01", "ok": False, "code": "FAILED"})
    _must_raise(lambda: validate_receipt(no_reason), "失败回执缺少 reason 应拒绝")


def test_验收4_存款绑定外的微信群和私聊不允许():
    """验收 4、F5、F6：只有绑定微信号上、且群号在列表内的群消息允许；私聊、空群列表、别的群不允许。"""
    binding = _deposit_binding()
    assert deposit_allows(binding, _wechat_group("g_123456")) is True
    assert deposit_allows(binding, _wechat_group("g_999")) is True
    assert deposit_allows(binding, _wechat_group("g_other")) is False
    private = _wechat_group("wxid_friend", **{"session": {"kind": "private", "native_id": "wxid_friend", "name": "张三"}})
    assert deposit_allows(binding, private) is False
    assert deposit_allows(_deposit_binding(group_native_ids=[]), _wechat_group("g_123456")) is False
    assert deposit_allows(binding, _wechat_group("g_123456", account_uid="wxid_other")) is False
    assert deposit_allows(binding, _wechat_group("g_123456", channel="qidian")) is False
    assert deposit_allows(binding, _wechat_group("g_123456", channel="qq")) is False


def test_验收5_同一扩展消息号第二次不再记为新上传():
    """验收 5、F5：同一 ext_msg_id 只有第一次记为新上传，第二次返回假。"""
    box = Dedup()
    assert box.seen_event("wx:1001") is True
    assert box.seen_event("wx:1001") is False
    assert box.seen_event("wx:1001") is False
    assert box.seen_event("wx:1002") is True


def test_验收6_同一幂等键第二次不再记为新发送():
    """验收 6、F4：同一 idempotency_key 只有第一次记为新发送，第二次返回假；与 ext_msg_id 不是同一集合。"""
    box = Dedup()
    assert box.seen_command("idem-014-6") is True
    assert box.seen_command("idem-014-6") is False
    assert box.seen_command("idem-014-6") is False
    assert box.seen_command("idem-014-7") is True
    assert box.seen_event("idem-014-6") is True
    assert box.seen_command("wx:1001") is True


def test_验收7_缩略图不发信且原图与非图片走锁定模板():
    """验收 7、F7：缩略图不发 IMAP；原图或非图片只在邮件中转时发送。公网上报不发信。"""
    selected = _wechat_group("g_123456")
    binding = _deposit_binding(transport="imap")
    assert should_send_imap(binding, selected, "缩略图") is False
    assert should_send_imap(binding, selected, "原图") is True
    assert should_send_imap(binding, selected, "") is True
    assert should_send_imap(_deposit_binding(transport="both"), selected, "原图") is False

    oss_binding = _deposit_binding(transport="oss")
    assert should_send_imap(oss_binding, selected, "原图") is False
    assert should_send_imap(oss_binding, selected, "") is False
    assert should_send_imap(_deposit_binding(), _wechat_group("g_other"), "原图") is False
    assert should_send_imap(_deposit_binding(group_native_ids=[]), selected, "") is False
    private = _wechat_group(
        "wxid_friend",
        **{"session": {"kind": "private", "native_id": "wxid_friend", "name": "张三"}},
    )
    assert should_send_imap(_deposit_binding(), private, "") is False

    for image_kind in ("", "原图"):
        rendered = render_selected_group(selected, image_kind=image_kind)
        body = _mail_body(rendered)
        ext, info = _split_mail(body)
        assert "微信群聊消息通知" in body.splitlines()
        assert ext == _segment_keys("ext", image_kind=image_kind)
        assert info == _segment_keys("info", image_kind=image_kind)
        if image_kind == "原图":
            assert "图片类型：原图" in body
        else:
            assert "图片类型" not in info


def test_图片原图随公网上报密封且表情包和缩略图不带文件():
    """聊天图片取最大原图。表情包不送。只有缩略图时不把文件放进密封体。"""
    thumb = {"type": 3, "kind": "缩略图", "size": 20, "body": b"thumb"}
    small = {"type": 3, "kind": "原图", "name": "small.jpg", "size": 100, "body": b"small-jpg"}
    large = {"type": 3, "kind": "原图", "name": "large.jpg", "size": 900, "body": b"large-jpg"}
    emoji = {"type": 47, "kind": "表情包", "size": 5000, "body": b"sticker"}
    assert pick_original_image([emoji, thumb, small, large])["name"] == "large.jpg"
    assert pick_original_image([emoji]) is None
    assert pick_original_image([thumb]) is None
    catalog = {
        "workbench_id": "wb-nanjing-a",
        "accounts": [{"channel": "wechat", "account_uid": "wxid_a", "nick": "安琳"}],
    }
    binding = {
        "workbench_id": "wb-nanjing-a",
        "transport": "oss",
        "wxid": "wxid_a",
        "group_native_ids": ["grp-1"],
    }
    message = {
        "event_id": "e1",
        "workbench_id": "wb-nanjing-a",
        "channel": "wechat",
        "account_uid": "wxid_a",
        "dir": "in",
        "text": "[图片]",
        "ts": "2026-10-12T02:00:00+08:00",
        "ext_msg_id": "m1",
        "session": {"kind": "group", "native_id": "grp-1"},
    }
    uploads = plan_uploads(
        catalog, binding, [message], key=_KEY, consumer="deposit",
        images={"m1": [emoji, thumb, small, large]},
    )
    opened = [open_sealed(_KEY, item.ciphertext) for item in uploads]
    origin = base64.b64encode(b"large-jpg")
    assert any(origin in item and b"large.jpg" in item and "原图".encode() in item for item in opened)
    assert all(base64.b64encode(b"sticker") not in item and base64.b64encode(b"thumb") not in item for item in opened)
    imap_binding = dict(binding, transport="imap")
    imap_uploads = plan_uploads(
        catalog, imap_binding, [message], key=_KEY, consumer="deposit",
        images={"m1": [emoji, thumb, small, large]},
    )
    assert [item.name for item in imap_uploads] == []
    assert pick_original_image([emoji, thumb, small, large])["body"] == b"large-jpg"
    assert should_send_imap(imap_binding, message, "原图") is True
    assert should_send_imap(imap_binding, message, "表情包") is False


def test_验收8_询价绑定企点号的消息全部保留():
    """验收 8、F5：绑定企点号的私聊和群都允许，对不上券的消息仍保留；别的企点号、别的通道不允许。"""
    binding = _xunjia_binding()
    private = _qidian_message()
    group = _qidian_message(**{
        "session": {"kind": "group", "native_id": "g_unlisted", "name": "随便一个群"},
        "text": "群里这句也对不上券",
        "ext_msg_id": "qd:8002",
        "dir": "out",
    })
    assert all(key not in private for key in ("bond", "quote", "security_id"))
    assert xunjia_allows(binding, private) is True
    assert xunjia_allows(binding, group) is True
    kept = validate_message(private)
    survived = private if kept is None else kept
    assert "这句对不上任何券" in _serialized(survived)
    assert xunjia_allows(binding, _qidian_message(account_uid="qd02")) is False
    assert xunjia_allows(binding, _qidian_message(channel="wechat")) is False
    assert xunjia_allows(binding, _qidian_message(channel="qq")) is False


def test_验收9_已拦截文本不允许写成指令():
    """验收 9、F8：调用方已判定拦截时 command_allowed 为假；未拦截时不因正文里的成交字样再拦一道。"""
    blocked_text = "这个价格可以成交"
    assert command_allowed(blocked_text, blocked=True) is False
    assert command_allowed("普通问候", blocked=True) is False
    assert command_allowed(blocked_text, blocked=False) is True
