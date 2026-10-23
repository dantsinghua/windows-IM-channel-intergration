"""模板渲染与校验 —— 规格:docs/06 §2.4.1(信息邮件信息段逐字段)、§2.4.2(回执)、§2.14(E-4 模板配置)。"""
from __future__ import annotations

import pytest

from qtrade_agent.mail.config import (PROFILE_COLLECTOR, PROFILE_IBQUOTE, PROFILE_QTRADE, InboundTemplateConfig,
                                      OutboundTemplateConfig)
from qtrade_agent.mail.templates import (CORE_PLACEHOLDERS, IBQUOTE_BODY_FIELDS, TemplateInvalid, TemplateLocked,
                                         body_fields_of, defaults_for, render, render_message_mail,
                                         render_receipt, scope_subject_prefixes, validate_template)

CTX = {
    "account_id": "qd01", "channel": "qidian", "channel_cn": "企点", "session_id": "qd01:g_123456",
    "session_name": "固收报价群", "sender_kind": "group", "sender_name": "张三", "sender_id": "wxid_abc",
    "ts": "2026-09-18T10:03:00+08:00", "ts_cn": "2026年9月18日 10:03", "msg_type": "text", "msg_type_cn": "文本",
    "summary": "3M 1.52 可谈", "media_count": 0, "message_id": "msg_01J8K3", "ext_msg_id": "qd:1001",
    "fingerprint": "abc", "seq": 1, "seq_total": 1, "day_seq": 7, "dir": "in",
    "text": "3M 1.52 可谈", "attachments": "-", "image_kind": "", "revoked": False,
    "media_ref": "", "oversize": "", "capture_text": "", "template_version": "v1",
}


def test_default_subject_matches_spec_sample():
    tpl = OutboundTemplateConfig()
    r = render_message_mail(tpl, CTX)
    assert r.subject == "转发：微信消息 [固收报价群] 3M 1.52 可谈 2026年9月18日 10:03 - 1"


def test_ibquote_info_segment_is_verbatim_and_after_title():
    """§2.4.1:扩展块在标题行**之前**,信息段在标题行之后;信息段键名/顺序一个字不能动。"""
    r = render_message_mail(OutboundTemplateConfig(), CTX)
    lines = [ln for ln in r.body_text.splitlines() if ln.strip()]
    title_at = lines.index("微信群聊消息通知")
    ext = [ln.split("：", 1)[0] for ln in lines[:title_at]]
    info = [ln.split("：", 1)[0] for ln in lines[title_at + 1:]]
    assert ext[:3] == ["来源账号", "通道", "会话ID"]
    assert info == ["群聊", "消息序号", "昵称", "消息时间", "每日序号", "消息ID", "消息类型",
                    "文本消息内容", "图片/文件附件", "是否撤回"]        # 图片类型 empty=omit,非图片消息不出现


def test_empty_dash_and_omit_semantics():
    ctx = dict(CTX, sender_name="", media_ref="")
    r = render_message_mail(OutboundTemplateConfig(), ctx)
    assert "昵称：-" in r.body_text                 # dash:键在、值 -
    assert "媒体引用" not in r.body_text            # omit:整行不出现


def test_required_empty_is_recorded_but_not_blocking():
    r = render_message_mail(OutboundTemplateConfig(), dict(CTX, sender_name=""))
    assert "empty:昵称" in r.render_notes and r.body_text


def test_sender_kind_custom_labels():
    assert render("{sender_kind:群|个人}", {"sender_kind": "group"}) == "群"
    assert render("{sender_kind:群|个人}", {"sender_kind": "private"}) == "个人"


def test_revoked_bool_renders_chinese():
    r = render_message_mail(OutboundTemplateConfig(), dict(CTX, revoked=True))
    assert "是否撤回：是" in r.body_text


def test_collector_profile_is_html_only_with_aliases():
    tpl = OutboundTemplateConfig(compat_profile=PROFILE_COLLECTOR,
                                 subject_pattern=defaults_for(PROFILE_COLLECTOR)["subject_pattern"],
                                 info_title=defaults_for(PROFILE_COLLECTOR)["info_title"],
                                 mime="html_only", body_fields=defaults_for(PROFILE_COLLECTOR)["body_fields"])
    r = render_message_mail(tpl, CTX)
    assert "图片/文件附件：见附件" in r.body_text           # collector 原样
    assert r.subject.startswith("微信群聊消息通知 - 固收报价群 - 张三 - qd:1001")   # target/sender 别名


def test_qtrade_profile_subject_has_version():
    tpl = OutboundTemplateConfig(compat_profile=PROFILE_QTRADE,
                                 subject_pattern=defaults_for(PROFILE_QTRADE)["subject_pattern"],
                                 info_title=defaults_for(PROFILE_QTRADE)["info_title"],
                                 body_fields=defaults_for(PROFILE_QTRADE)["body_fields"])
    validate_template(tpl)
    r = render_message_mail(tpl, CTX)
    assert r.subject.startswith("QTrade消息 v1 [企点·群] [qd01] 固收报价群 文本")


# ---------------------------------------------------------------- §2.14 校验
def test_compat_profile_locks_info_fields():
    tpl = OutboundTemplateConfig()
    fields = [dict(f) for f in IBQUOTE_BODY_FIELDS]
    fields[12]["key"] = "呢称"                      # 改信息段「昵称」键名
    tpl.body_fields = fields
    with pytest.raises(TemplateLocked) as e:
        validate_template(tpl)
    assert "昵称" in str(e.value)


def test_compat_profile_locks_info_title():
    tpl = OutboundTemplateConfig(info_title="我的通知")
    with pytest.raises(TemplateLocked):
        validate_template(tpl)


def test_subject_pattern_is_changeable_under_compat_profile():
    tpl = OutboundTemplateConfig(subject_pattern="转发：微信消息 [{channel_cn}][{session_name}] {summary}")
    validate_template(tpl)                          # 主题可以随便改、信息段不能
    assert "企点" in render_message_mail(tpl, CTX).subject


def test_unknown_placeholder_rejected_at_config_time():
    with pytest.raises(TemplateInvalid):
        validate_template(OutboundTemplateConfig(subject_pattern="转发：微信消息 {no_such_thing}"))


def test_qtrade_profile_requires_template_version_and_only_core_placeholders():
    tpl = OutboundTemplateConfig(compat_profile=PROFILE_QTRADE, subject_pattern="QTrade消息 [{account_id}]",
                                 info_title="QTrade 消息通知 v1",
                                 body_fields=defaults_for(PROFILE_QTRADE)["body_fields"])
    with pytest.raises(TemplateInvalid):
        validate_template(tpl)                      # 缺 {template_version}


def test_core_placeholder_count_is_twelve_groups():
    """§2.14.2:12 个核心占位符(含各自的变体名)。"""
    groups = {"channel", "account_id", "session_name", "sender_kind", "sender_name", "ts", "msg_type",
              "summary", "media_count", "message_id", "fingerprint", "seq"}
    assert groups <= CORE_PLACEHOLDERS and len(groups) == 12


def test_scope_prefixes_derived_from_patterns():
    prefixes = scope_subject_prefixes([OutboundTemplateConfig()], [InboundTemplateConfig().subject_pattern])
    assert prefixes == ["QTRADE指令", "转发：微信消息", "QTRADE回执"]


def test_body_fields_default_comes_from_profile():
    assert body_fields_of(OutboundTemplateConfig()) == IBQUOTE_BODY_FIELDS


# ---------------------------------------------------------------- §2.4.2 回执
def test_receipt_subject_and_fixed_fields():
    ctx = {"template_version": "v1", "account_id": "qd01", "op": "send_text",
           "req_id": "20260918-ops-0007", "result_code": "DELIVERED"}
    values = {"指令ID": "20260918-ops-0007", "账号": "qd01", "操作": "send_text", "会话": "张三-固收",
              "送达状态": "DELIVERED", "结果": "成功", "确认方式": "qidian_db", "消息ID": "msg_1",
              "耗时毫秒": "1650", "追踪ID": "01J8", "错误": "-", "可重试": "否", "需人工": "否",
              "执行时间": "2026-09-18T10:03:02+08:00", "签名": "hmac-sha256=ab"}
    r = render_receipt(OutboundTemplateConfig(), ctx, values)
    assert r.subject == "QTRADE回执 v1 [qd01] send_text 20260918-ops-0007 DELIVERED"
    assert r.body_text.splitlines()[0] == "QTrade 回执 v1"
    assert "送达状态：DELIVERED" in r.body_text and "结果：成功" in r.body_text
    assert "文本消息内容" not in r.body_text        # 回执不含消息正文
