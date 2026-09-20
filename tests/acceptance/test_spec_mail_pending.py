"""邮件摆渡验收 —— **待修实现缺陷**(每条都是红的,规格说 A、实现做 B)。

这些用例与 `test_spec_mail.py` 同源同风格、同样只依据规格写,分诊结论是**实现缺陷**而非用例误读;
按验收流程它们不留在主验收文件里(主文件不得有红),总控修完实现后把本文件的用例并回
`test_spec_mail.py` 即可(用例本身不需要改)。

逐条的「规格原句 / 实际行为 / 建议修法」见本批验收报告;本文件只保留可执行的判据。
"""
from __future__ import annotations

import pytest

from qtrade_agent.mail import codes as C
from qtrade_agent.mail.backends import SmtpTemporaryError

from tests.acceptance.test_spec_mail import (MAILBOX, SMTP_HOST, Rig, _queue_receipt, build_command_mail,
                                             cmd_body, make_cfg, parse)


@pytest.fixture
def rig(tmp_path, clock):
    r = Rig(tmp_path=tmp_path, clock=clock)
    yield r
    r.store.close()


# ── 缺陷 1:§2.6.5 IMAP 第 1 步「进入终态立即 UID MOVE 到 processed_folder」未接线 ────────────────

def test_M15_terminal_command_mail_moves_to_processed(rig):
    """06 §2.6.5 IMAP 第 1 步:处理完(进入终态)且 `status ∉ NEVER_DELETE` ⇒ 立即 `UID MOVE` 到
    `processed_folder`(默认 `QTrade/processed`,不存在则 `CREATE`);`mail_inbox` 记新夹与新 UID。

    实际:`MailCleanup.on_terminal()` 写好了但**没有任何调用点**,邮件始终留在 INBOX
    (连带 §2.6.5 第 2 步「到期删除只在专用夹里做」也落空)。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, from_addr="stranger@x.example"))
    row = rig.one_inbox()
    assert row["status"] == C.SENDER_DENIED
    assert [d for _, d in rig.imap.moved] == [rig.cfg.inbound.processed_folder]
    assert row["folder"] == rig.cfg.inbound.processed_folder


# ── 缺陷 2:§2.3.2「参数」JSON 形式的跨行续行未实现 ──────────────────────────────────────────

def test_M49_args_json_multiline_continuation():
    """06 §2.3.2「参数的两种形式」JSON 形式:值可跨行——从 `参数：` 之后**续行到下一个已知键为止**,
    拼接后整体 `json.loads`。

    实际:只有展开形式(`参数.text`)实现了续行;JSON 形式只取 `参数：` 那一行,
    任何被客户端折行的 JSON 都落 `PARSE_FAILED: args_json`。"""
    got = parse(cmd_body('参数：{"text":"第一行', '第二行"}'))
    assert got.status != C.PARSE_FAILED
    assert got.args.get("text") in ("第一行\n第二行", "第一行第二行")


def test_M49b_args_json_split_at_syntax_boundary():
    """同上:即使折行折在 JSON 语法边界(逗号后),拼接后也应是合法对象。"""
    got = parse(cmd_body('参数：{"text":"x",', '"limit":3}'))
    assert got.status != C.PARSE_FAILED and got.args.get("limit") in (3, "3")


# ── 缺陷 3:受理路径丢弃解析备注,`mail_inbox.reason` 恒 NULL ────────────────────────────────

def test_M54_subject_mismatch_body_wins(rig):
    """06 §2.3.1:主题与正文不一致以**正文为准**,并在 `mail_inbox.reason` 记 `subject_mismatch` 供人看。

    实际:解析器确实产出了 `subject_mismatch` 备注,但落库时只在失败分支写 `reason`;
    邮件被受理(`ACCEPTED`)时备注被丢掉,`reason` 为 NULL。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="B-1", nonce="nb1",
                                       subject="QTRADE指令 v1 [qd01] read_messages 主题里是另一个ID"))
    row = rig.one_inbox()
    assert row["req_id"] == "B-1"
    assert "subject_mismatch" in (row["reason"] or "")


def test_M54b_unknown_key_noted_in_reason(rig):
    """06 §2.3.3:未知键(模板漂移/拼错)⇒ 记 `mail_inbox.reason += unknown_key:<键>`。

    实际:同上——受理成功的邮件 `reason` 为 NULL,`P-MAIL` 看不到模板漂移的早期信号。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, req_id="UK-1", nonce="uk1",
                                       extra_lines=("莫名其妙键：值",)))
    assert "unknown_key" in (rig.one_inbox()["reason"] or "")


# ── 缺陷 4:重发回执对 SENT/DISCARDED 的行没有「复制一行」 ────────────────────────────────────

async def test_M158_resend_receipt_is_new_row(rig):
    """06 §2.5 末:`P-MAIL`【重发回执】就是**复制一行重新入队**;02 #62:`DEAD/DISCARDED/SENT` 重投
    (新行,`dedup_key` 加后缀 `#2`)。

    实际:只有 `DEAD` 走「复制新行」,`SENT`/`DISCARDED` 被就地改回 `QUEUED`——
    原来那封已发出的回执的投递留痕(`sent_ms`/`smtp_response`)被覆盖,追溯断线。"""
    from tests.acceptance.test_spec_mail import FakeBus
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="send_text", session="张三",
                                       args={"text": "x"}, req_id="RCP-12", nonce="rc12"))
    await rig.svc.ingest.dispatch(FakeBus())
    rig.svc.send_once()
    first = rig.outbox(kind="receipt")[0]
    assert first["status"] == "SENT"
    new_id = rig.svc.sender.resend(first["id"])
    rows = rig.outbox(kind="receipt")
    assert new_id != first["id"] and len(rows) == 2
    assert rows[1]["dedup_key"].endswith("#2") and rows[1]["ref_inbox_id"] == first["ref_inbox_id"]


# ── 缺陷 5:`MAIL_SMTP_FAILING` 的 subject 不是 `smtp:<host>` ───────────────────────────────

def test_M164_smtp_failing_alert_subject(rig):
    """06 §2.7 告警表:`MAIL_SMTP_FAILING` 的 `subject` = **`smtp:<host>`**(去重键 `(code, subject)`;
    02 §3.7 R6-35「`MAIL_*` subject 七种」同句)。

    实际:发的是常量 `smtp:*`,多路由/多 SMTP 时所有主机挤在同一个去重键上,
    `P-MAIL` 也认不出是哪台 SMTP 在失败。"""
    for i in range(3):
        _queue_receipt(rig, dedup=f"receipt:f{i}")
    rig.smtp.fail_times = 99
    rig.smtp.fail_exc = SmtpTemporaryError("451")
    rig.svc.send_once()
    firing = rig.events.of(C.MAIL_SMTP_FAILING, "firing")
    assert firing and firing[0]["payload"]["subject"] == f"smtp:{SMTP_HOST}"


# ── 缺陷 6:`op_unknown` 这类 PARSE_FAILED 不发 `MAIL_PARSE_FAILED` ─────────────────────────

def test_M209c_op_unknown_also_alerts(rig):
    """06 §2.7 告警表:`MAIL_PARSE_FAILED`(`subject=inbox:<id>`,info)的触发 =
    **白名单发件人的指令邮件 `PARSE_FAILED/SIG_INVALID/EXPIRED`**。

    实际:只有正文解析阶段的 `PARSE_FAILED` 与 `SIG_INVALID`/`EXPIRED` 发了告警;
    `操作` 不在能力目录里的 `PARSE_FAILED: op_unknown`(§2.3.2)静默落库,不告警。"""
    rig.ingest_imap(build_command_mail(clock=rig.clock, op="发消息", req_id="PF-9", nonce="pf9"))
    assert rig.one_inbox()["status"] == C.PARSE_FAILED
    assert rig.events.of(C.MAIL_PARSE_FAILED, "firing")
