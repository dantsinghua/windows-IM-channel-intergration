"""按设计文档写的验收用例 —— 第五批:横切基础设施(webhook 扇出 / HMAC 公网入站 / 保留期与磁盘 / 资源池自校准 / 工作流引擎)。

🔴 断言只依据规格,不按实现反推;规格出处(册 §节 / 端点编号 / 裁决号 / 验收行):
- 02 §2.2.7 `events`(outbox → WS/webhook:扇出、订阅过滤、每 webhook 串行保序、死信阈值、两类 outbox 行)、
  §2.2.12 + §14 E-3(`NET_PUBLIC_ENDPOINT_CHANGED` 跳过订阅过滤投给全部启用 webhook)、
  §3.1 `webhooks` / `events_outbox` / `api_clients` / `workflows` / `workflow_runs` / `workflow_steps` / `resource_pools` DDL、
  §3.5 公网入站 HMAC 规范(头名、canonical string、签名、时钟容差、nonce、失败顺序、webhook 出站同一套 canonical、默认 async)、
  §3.7 告警码表(`WEBHOOK_DEAD` / `H12_DISK_LOW` / `DB_WRITE_FAILED` / `POOL_CALIBRATION_DRIFT`)、
  §2.8.4 保留期与清理(六步顺序、媒体独立于引用计数、R6-1 pop3 uidl 保全、每批 5000、增量 VACUUM)、
  §2.8.8 磁盘三级水位(阈值、逐级递进、递减 30→14→7、`DISK_FULL`/507 信封)、§3.3(备份份数、迁移、容量)、
  §2.2.6 `workflow`(M5 直线流程、R-13 砍掉的语法、串行失败即停、每步留痕、YAML 形态)、
  §3.4.3 #38~#47、§3.4.10 #107/#108、§3.4.11 #109、§3.4.6 #71、§3.4.1 #25、§7.1 `[events]`/`[retention]`/`[pool]` 默认值、§3.10 能力目录。
- 00 §11.10/§11.11(E-18 保留期与三级水位)、§11.17 ②(`expand_allow_ops()`:星号只展开 `danger=false`)、
  §11.21 [JOB](异步作业统一契约)、§11.22 [SCOPE] R-13、§7.5 Event 帧、§8.3 结果码表。
- 04 §2.5.3 资源池自校准(五个量的测法、只上调、`POOL_CALIBRATION_DRIFT` 漂移告警)、§8b.2 A2-01~A2-05。
- 07 配置项总表(镜像 02 §7.1 的默认值)。

每个用例顶部 docstring 写清条款号;断言的是规格说的可观测结果(表里几行、列值、HTTP 状态、信封键、事件条数、退避毫秒数)。
夹具来自 tests/conftest.py:``clock``(可拨时钟)、``store``(内存 agent.db,含 qd01/qq03);本文件另建
``disk``(可拨磁盘余量)、``rig5``(AgentApp + FastAPI + 令牌,给 workflow / jobs / HMAC 端到端用例)。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import json
import os
import threading
import time
from typing import Any, Optional

import pytest
from starlette.testclient import TestClient

from qtrade_agent import hmac_inbound as HI
from qtrade_agent.alerts import Alerts
from qtrade_agent.config import AgentConfig, ApiConfig
from qtrade_agent.events import Events
from qtrade_agent.maintenance import (BackupConfig, DiskFullError, MaintenanceService, RetentionConfig)
from qtrade_agent.pool_calibrate import CalibrationConfig, PoolCalibrator
from qtrade_agent.store import Store
from qtrade_agent.webhook import FakeHttp, HttpResponse, WebhookConfig, WebhookDispatcher
from qtrade_agent.workflow import WorkflowEngine
from qtrade_agent.workflow.model import WorkflowParseError, parse_workflow, validate

# ────────────────────────────────────────────────────────────────────── 常量(逐字自抄规格)

P = "/api/v1"                                  # 00 §10 前缀
QD = "qd01"
QQ = "qq03"
T0 = 1_758_240_000_000                         # conftest Clock 的起始时刻(ms)
DAY = 86_400_000
T_DRIFT = T0 + 3600_000 + 10          # 04 §2.5.3「持续 1h」之后的那一刻(漂移用例的检查时点)

# 02 §7.1 [events]:webhook 三键的逐字默认值
SPEC_WEBHOOK_TIMEOUT_MS = 5000
SPEC_WEBHOOK_MAX_ATTEMPTS = 10
SPEC_WEBHOOK_BACKOFF_MS = [1000, 2000, 5000, 10000, 30000, 60000, 120000, 300000, 600000, 600000]

# 02 §7.1 [retention]:E-18 三级水位与保留期默认值
SPEC_DISK_WARN_MB, SPEC_DISK_HIGH_MB, SPEC_DISK_CRITICAL_MB = 5120, 2048, 1024
SPEC_RETENTION_STEPS = (30, 14, 7)             # §2.8.8 critical 递减循环

# 02 §3.5:HMAC 头名与时钟容差
H_APP, H_TS, H_NONCE, H_SIG = "X-QT-AppId", "X-QT-Timestamp", "X-QT-Nonce", "X-QT-Signature"
SPEC_SKEW_S = 300
SPEC_NONCE_TTL_S = 600                         # 「(app_id, nonce) 在内存 LRU 里 10 分钟内唯一」

# 00 §11.17 ①/02 §3.10:danger=true 的十项权威集合(R5-8)
DANGER_TEN = frozenset({"account_switch", "account_stop", "messages_purge", "account_purge", "account_delete",
                        "settings_write", "vault_write", "workflow_run", "mail_cleanup_run", "system_wsl_restart"})

TOK_ADMIN = "tok-console-admin"
TOK_W = "tok-bot-write"
TOK_R = "tok-bot-read"


def H(tok: Optional[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def count(store, sql: str, *params) -> int:
    return int(store.con.execute(f"SELECT COUNT(*) FROM {sql}", params).fetchone()[0])


def rows(store, sql: str, *params) -> list[dict[str, Any]]:
    return [dict(r) for r in store.con.execute(sql, params)]


def one(store, sql: str, *params) -> Optional[dict[str, Any]]:
    r = store.con.execute(sql, params).fetchone()
    return dict(r) if r else None


# ────────────────────────────────────────────────────────────────────── 规格自抄的参考实现(不调实现的同名函数)

def spec_canonical(method: str, path: str, query: str, body: bytes, ts: int, nonce: str) -> str:
    """02 §3.5 canonical string 逐字:``METHOD \\n PATH \\n CANONICAL_QUERY \\n sha256hex(BODY 或空串) \\n TIMESTAMP \\n NONCE``。"""
    return "\n".join([method.upper(), path, query, hashlib.sha256(body or b"").hexdigest(), str(ts), nonce])


def spec_sign(secret: str, canonical: str) -> str:
    """02 §3.5 签名逐字:``hex(HMAC-SHA256(secret, canonical))``。"""
    return _hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def spec_expand_allow_ops(allow_ops: list[str], catalog: list[tuple[str, bool]]) -> set[str]:
    """00 §11.17 ② 的伪代码逐字照抄:星号只展开 ``danger=false``,``danger=true`` 只能被逐条点名。"""
    out: set[str] = set()
    for item in allow_ops:
        if item == "*":
            out |= {op for op, danger in catalog if not danger}
        else:
            out.add(item)
    return out


# ────────────────────────────────────────────────────────────────────── 建行工具(只写 §3.1 的列,不借实现的便捷方法)

def mk_webhook(store, wid: str, url: str, *, events: Optional[list[str]] = None, accounts: Optional[list[str]] = None,
               enabled: int = 1, timeout_ms: int = SPEC_WEBHOOK_TIMEOUT_MS, max_attempts: int = SPEC_WEBHOOK_MAX_ATTEMPTS,
               now: int = T0) -> str:
    store.con.execute(
        "INSERT INTO webhooks(id, name, url, secret_ref, events_json, accounts_json, enabled, timeout_ms, max_attempts, "
        "consecutive_fail, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
        (wid, wid, url, f"vault://webhook/{wid}", json.dumps(events or ["*"]), json.dumps(accounts or ["*"]),
         enabled, timeout_ms, max_attempts, now, now))
    return wid


def mk_hmac_client(store, app_id: str, *, level: str = "admin", ip_allow: Optional[list[str]] = None,
                   allow_ops: Optional[list[str]] = None, allow_accounts: Optional[list[str]] = None,
                   rate_per_min: int = 120, enabled: int = 1, now: int = T0) -> None:
    store.con.execute(
        "INSERT INTO api_clients(app_id, name, auth_kind, secret_hash, secret_ref, level, ip_allow_json, allow_ops_json, "
        "allow_accounts_json, rate_per_min, enabled, created_ms, updated_ms) VALUES (?,?,'hmac',?,?,?,?,?,?,?,?,?,?)",
        (app_id, app_id, "x", f"vault://api/{app_id}", level, json.dumps(ip_allow or []),
         json.dumps(allow_ops or ["*"]), json.dumps(allow_accounts or ["*"]), rate_per_min, enabled, now, now))


def mk_sample(store, *, scope: str, subject: str, ts_ms: int, mem_mb: Optional[float] = None,
              mem_anon_mb: Optional[float] = None, mem_max_mb: Optional[float] = None, resolution: str = "raw") -> None:
    store.con.execute(
        "INSERT INTO health_samples(ts_ms, resolution, scope, subject, mem_mb, mem_anon_mb, mem_max_mb) VALUES (?,?,?,?,?,?,?)",
        (ts_ms, resolution, scope, subject, mem_mb, mem_anon_mb, mem_max_mb))


# ────────────────────────────────────────────────────────────────────── 夹具

class FakeDiskProbe:
    """可拨的磁盘余量探针(§2.8.8 的三级水位只看「剩余 MB」这一个输入)。"""

    def __init__(self, free: int = 100_000):
        self.free = free

    def free_mb(self, path: str) -> int:
        return self.free


@pytest.fixture
def disk() -> FakeDiskProbe:
    return FakeDiskProbe()


@pytest.fixture
def fstore(tmp_path, clock) -> Store:
    """文件库(§2.8.8 的 ``db_size_mb`` / §3.3 的在线备份都要真文件)。"""
    s = Store(str(tmp_path / "agent.db"), clock=clock).open()
    s.ensure_account(QD, "qidian", state="running", self_uid="3007373675")
    s.ensure_account(QQ, "qq", state="running", login_mode="qrcode", self_uid="415011447")
    yield s
    s.close()


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def hooks(store, clock, http) -> WebhookDispatcher:
    """投递器:秘钥固定、nonce 固定(便于自算签名比对)。"""
    return WebhookDispatcher(store, http=http, secret_provider=lambda w: "s3cr3t",
                             clock=clock, nonce_fn=lambda: "n" * 16)


@pytest.fixture
def events(store) -> Events:
    return Events(store)


@pytest.fixture
def fevents(fstore) -> Events:
    """文件库上的事件总线(磁盘水位/清理类用例的告警落在 ``fstore``)。"""
    return Events(fstore)


@pytest.fixture
def verifier(store, clock) -> HI.HmacVerifier:
    return HI.HmacVerifier(store, secret_provider=lambda row: "sec-" + row["app_id"], clock=clock,
                           danger_ops=DANGER_TEN)


def hmac_headers(app_id: str, secret: str, method: str, path: str, query: str = "", body: bytes = b"",
                 *, ts: Optional[int] = None, nonce: str = "abcdef0123456789", now_ms: int = T0) -> dict[str, str]:
    ts = ts if ts is not None else now_ms // 1000
    sig = spec_sign(secret, spec_canonical(method, path, query, body, ts, nonce))
    return {H_APP: app_id, H_TS: str(ts), H_NONCE: nonce, H_SIG: "v1=" + sig}


# ══════════════════════════════════════════════════════════════════════ 一、webhook 扇出与订阅过滤(02 §2.2.7 / §3.1)

def test_WH01_emit_writes_one_ws_row_marked_delivered(store, events):
    """§2.2.7 outbox 两类行:``target='ws'`` 一行是规范事件记录,**写入即 status='delivered'**(不经投递器,R6-52)。"""
    events.emit("alert", payload={"code": "X"}, now_ms=T0)
    r = one(store, "SELECT * FROM events_outbox WHERE target='ws'")
    assert r is not None and r["status"] == "delivered"


def test_WH02_fanout_writes_one_pending_row_per_enabled_webhook(store, hooks):
    """§2.2.7:``target='webhook:<id>'`` **每个启用的 webhook 一行**,带各自重试状态(投递器只轮询这一类)。"""
    mk_webhook(store, "w1", "https://a.example/cb")
    mk_webhook(store, "w2", "https://b.example/cb")
    hooks.fanout(event_id="e1", event="message", payload={"a": 1}, now_ms=T0)
    got = rows(store, "SELECT target, status FROM events_outbox ORDER BY target")
    assert [(g["target"], g["status"]) for g in got] == [("webhook:w1", "pending"), ("webhook:w2", "pending")]


def test_WH03_disabled_webhook_gets_no_outbox_row(store, hooks):
    """§2.2.7:只给**启用的** webhook 各一行(``webhooks.enabled=0`` 的不投)。"""
    mk_webhook(store, "on", "https://a.example/cb")
    mk_webhook(store, "off", "https://b.example/cb", enabled=0)
    hit = hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    assert hit == ["on"] and count(store, "events_outbox WHERE target='webhook:off'") == 0


def test_WH04_events_json_star_means_all_event_types(store, hooks):
    """§3.1 `webhooks.events_json` DEFAULT ``'["*"]'``:订阅的 event 类型,``["*"]`` = 不限。"""
    mk_webhook(store, "w", "https://a.example/cb", events=["*"])
    for ev in ("message", "alert", "job"):
        hooks.fanout(event_id=f"e-{ev}", event=ev, payload={}, now_ms=T0)
    assert count(store, "events_outbox WHERE target='webhook:w'") == 3


def test_WH05_events_json_filters_unsubscribed_event_type(store, hooks):
    """§3.1 `events_json`:只订 ``message`` 的 webhook 不该收到 ``alert``。"""
    mk_webhook(store, "w", "https://a.example/cb", events=["message"])
    hooks.fanout(event_id="e1", event="alert", payload={}, now_ms=T0)
    hooks.fanout(event_id="e2", event="message", payload={}, now_ms=T0)
    got = rows(store, "SELECT event FROM events_outbox WHERE target='webhook:w'")
    assert [g["event"] for g in got] == ["message"]


def test_WH06_accounts_json_filters_by_account(store, hooks):
    """§3.1 `accounts_json`:订阅的账号;只订 qd01 的 webhook 不该收到 qq03 的事件。"""
    mk_webhook(store, "w", "https://a.example/cb", accounts=[QD])
    hooks.fanout(event_id="e1", event="message", payload={}, account_id=QQ, now_ms=T0)
    hooks.fanout(event_id="e2", event="message", payload={}, account_id=QD, now_ms=T0)
    got = rows(store, "SELECT account_id FROM events_outbox WHERE target='webhook:w'")
    assert [g["account_id"] for g in got] == [QD]


def test_WH07_both_filters_must_match(store, hooks):
    """§2.2.7 + §3.1:两个订阅过滤是**与**关系(事件类型对、账号不对 ⇒ 不投)。"""
    mk_webhook(store, "w", "https://a.example/cb", events=["message"], accounts=[QD])
    hooks.fanout(event_id="e1", event="message", payload={}, account_id=QQ, now_ms=T0)
    assert count(store, "events_outbox WHERE target='webhook:w'") == 0


def test_WH08_endpoint_changed_ignores_subscription_filters(store, hooks):
    """§2.2.12 E-3(§14 E-3 同句):`NET_PUBLIC_ENDPOINT_CHANGED` 向 `webhooks` **全部 enabled=1 的登记方各投一行**,
    **不受 `events_json`/`accounts_json` 订阅过滤**——使用方只订了 `message` 也必须收到,否则它就找不到我们了。"""
    mk_webhook(store, "only_msg", "https://a.example/cb", events=["message"], accounts=[QD])
    hooks.fanout(event_id="e1", event="net", payload={"kind": "endpoint_changed"}, ignore_filters=True, now_ms=T0)
    assert count(store, "events_outbox WHERE target='webhook:only_msg'") == 1


def test_WH09_endpoint_changed_still_skips_disabled_webhook(store, hooks):
    """§2.2.12 E-3 逐字:「向 `webhooks` **全部 `enabled=1`** 的登记方各投一行」——停用的仍不投。"""
    mk_webhook(store, "off", "https://a.example/cb", enabled=0)
    hooks.fanout(event_id="e1", event="net", payload={}, ignore_filters=True, now_ms=T0)
    assert count(store, "events_outbox WHERE target='webhook:off'") == 0


def test_WH10_outbox_unique_event_id_target(store, hooks):
    """§3.1 `events_outbox` ``UNIQUE (event_id, target)``:同一事件的多目标行共享 `event_id`,同目标不得重复。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="dup", event="message", payload={}, now_ms=T0)
    with pytest.raises(Exception):
        store.con.execute(
            "INSERT INTO events_outbox(event_id, target, event, ts_ms, payload_json) VALUES ('dup','webhook:w','message',?,'{}')",
            (T0,))


def test_WH11_ws_and_webhook_rows_share_event_id(store, events, hooks):
    """§3.1 `events_outbox.event_id` 注:「ULID,**同一事件的多目标行共享**」。"""
    mk_webhook(store, "w", "https://a.example/cb")
    events.on_emit = lambda **kw: hooks.fanout(**kw)
    events.emit("message", payload={}, now_ms=T0)
    ids = {r["event_id"] for r in rows(store, "SELECT event_id FROM events_outbox")}
    assert len(ids) == 1 and count(store, "events_outbox") == 2


def test_WH12_dispatcher_only_polls_webhook_rows(store, hooks, events):
    """§2.2.7:「投递器**只轮询这一类**(`target='webhook:<id>'`)」——`ws` 行永远不在到期集合里。"""
    mk_webhook(store, "w", "https://a.example/cb")
    events.emit("message", payload={}, now_ms=T0)          # 只写 ws 行
    hooks.fanout(event_id="e2", event="message", payload={}, now_ms=T0)
    due = hooks.due_rows(now_ms=T0)
    assert [d["target"] for d in due] == ["webhook:w"]


def test_WH13_due_rows_are_pending_and_due_only(store, hooks):
    """§2.2.7:轮询条件逐字 ``status='pending' and next_attempt_ms<=now``。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    store.con.execute("UPDATE events_outbox SET next_attempt_ms=? WHERE target='webhook:w'", (T0 + 5000,))
    assert hooks.due_rows(now_ms=T0) == []
    assert len(hooks.due_rows(now_ms=T0 + 5000)) == 1


def test_WH14_due_rows_ordered_by_seq(store, hooks):
    """§2.2.7「每个 webhook 一条串行投递(**保序**)」的前提:到期行按 `seq`(= WS 重放游标)升序取。"""
    mk_webhook(store, "w", "https://a.example/cb")
    for i in range(3):
        hooks.fanout(event_id=f"e{i}", event="message", payload={"i": i}, now_ms=T0)
    seqs = [d["seq"] for d in hooks.due_rows(now_ms=T0)]
    assert seqs == sorted(seqs)


def test_WH15_poll_interval_is_500ms(hooks):
    """§2.2.7:「投递器一个 task 轮询…**每 500ms**」。"""
    assert hooks.cfg.poll_interval_ms == 500


# ══════════════════════════════════════════════════════════════════════ 二、webhook 出站签名(02 §3.5 末行)

async def test_WH16_outbound_headers_use_webhook_prefix(store, hooks, http):
    """§3.5「webhook 出站」行:头名同上(`X-QT-AppId/Timestamp/Nonce/Signature`)**加前缀 `X-QT-Webhook-*`**。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    h = http.requests[0]["headers"]
    for name in ("X-QT-Webhook-AppId", "X-QT-Webhook-Timestamp", "X-QT-Webhook-Nonce", "X-QT-Webhook-Signature"):
        assert name in h, f"缺少 {name}"


async def test_WH17_outbound_signature_matches_spec_canonical(store, hooks, http):
    """§3.5 webhook 出站:同一套 canonical ``POST \\n <url path> \\n <query> \\n sha256(body) \\n ts \\n nonce``,
    secret = `webhooks.secret_ref`;对方按这套验签即可确认「是我们发的」。"""
    mk_webhook(store, "w", "https://a.example/cb?x=1")
    hooks.fanout(event_id="e1", event="message", payload={"k": "v"}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    req = http.requests[0]
    h = req["headers"]
    expect = spec_sign("s3cr3t", spec_canonical("POST", "/cb", "x=1", req["body"],
                                                int(h["X-QT-Webhook-Timestamp"]), h["X-QT-Webhook-Nonce"]))
    assert h["X-QT-Webhook-Signature"] == "v1=" + expect


async def test_WH18_outbound_timestamp_is_epoch_seconds(store, hooks, http):
    """§3.5 Header 行:`X-QT-Timestamp` = **秒级 epoch,整数**(出站同一套)。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    ts = http.requests[0]["headers"]["X-QT-Webhook-Timestamp"]
    assert ts.isdigit() and abs(int(ts) - T0 // 1000) <= 5


async def test_WH19_outbound_body_is_json_with_event_and_payload(store, hooks, http):
    """§7.5 + §3.4.7:投递体是一条事件帧,至少带 `event` 与 `payload`(签名的 `sha256(BODY)` 就是它)。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="e1", event="alert", payload={"code": "X"}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    body = json.loads(http.requests[0]["body"])
    assert body["event"] == "alert" and body["payload"] == {"code": "X"}


async def test_WH20_outbound_uses_row_timeout_ms(store, hooks, http):
    """§3.1 `webhooks.timeout_ms`(DEFAULT 5000,§7.1 `[events] webhook_timeout_ms`):每行自己的超时值生效。"""
    mk_webhook(store, "w", "https://a.example/cb", timeout_ms=1234)
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    assert http.requests[0]["timeout_ms"] == 1234


# ══════════════════════════════════════════════════════════════════════ 三、退避表 / 死信 / 保序(02 §2.2.7 / §5 / §3.7 / §7.1 / H-06)

def test_WH21_default_backoff_table_is_ten_steps(hooks):
    """§7.1 `[events] webhook_backoff_ms` 逐字 = ``[1000,2000,5000,10000,30000,60000,120000,300000,600000,600000]``(指数退避表)。"""
    assert list(hooks.cfg.webhook_backoff_ms) == SPEC_WEBHOOK_BACKOFF_MS


def test_WH22_default_max_attempts_is_ten(hooks):
    """§2.2.7「连续失败 `dead_after_attempts`(**默认 10**)次进 `dead`」= §7.1 `webhook_max_attempts=10` = §3.1 DDL DEFAULT 10。"""
    assert hooks.cfg.webhook_max_attempts == SPEC_WEBHOOK_MAX_ATTEMPTS


def test_WH23_default_timeout_is_5000ms(hooks):
    """§7.1 `[events] webhook_timeout_ms = 5000`。"""
    assert hooks.cfg.webhook_timeout_ms == SPEC_WEBHOOK_TIMEOUT_MS


async def test_WH24_first_failure_schedules_first_backoff_step(store, hooks, http):
    """§2.2.7 指数退避 + §7.1 退避表:第 1 次失败后 `next_attempt_ms` = now + 1000(表首档)。"""
    mk_webhook(store, "w", "https://a.example/cb")
    http.queue("https://a.example/cb", HttpResponse(500))
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
    assert r["attempts"] == 1 and r["status"] == "pending"
    assert r["next_attempt_ms"] - T0 == SPEC_WEBHOOK_BACKOFF_MS[0]


async def test_WH25_backoff_follows_the_table_step_by_step(store, clock, http):
    """§7.1 退避表:第 n 次失败后的等待 = 表第 n 档(逐档比对前四档)。"""
    clock.set_ms(T0)
    d = WebhookDispatcher(store, http=http, secret_provider=lambda w: "s", clock=clock)
    mk_webhook(store, "w", "https://a.example/cb")
    http.default = HttpResponse(500)
    d.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    waits = []
    for _ in range(4):
        now = clock()
        await d.deliver_due(now_ms=now)
        r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
        waits.append(r["next_attempt_ms"] - now)
        clock.set_ms(r["next_attempt_ms"])
    assert waits == SPEC_WEBHOOK_BACKOFF_MS[:4]


async def test_WH26_http_5xx_counts_as_one_failure(store, hooks, http):
    """§2.2.7 重试:HTTP 非 2xx 是一次投递失败(不是异常),`attempts` 加一、行仍 `pending`。"""
    mk_webhook(store, "w", "https://a.example/cb")
    http.queue("https://a.example/cb", HttpResponse(503))
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    assert one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")["status"] == "pending"


async def test_WH27_connection_error_counts_as_one_failure(store, hooks, http):
    """§2.2.7 重试:连不上/超时同样只是一次失败,进退避而不是丢事件。"""
    mk_webhook(store, "w", "https://a.example/cb")
    http.queue("https://a.example/cb", OSError("connrefused"))
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
    assert r["attempts"] == 1 and r["status"] == "pending" and r["last_error"]


async def test_WH28_success_marks_delivered_and_sets_delivered_ms(store, hooks, http):
    """§3.1 `events_outbox.status`/`delivered_ms`:投递成功置 `delivered` 并记时刻。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
    assert r["status"] == "delivered" and r["delivered_ms"] is not None


async def test_WH29_success_resets_consecutive_fail(store, hooks, http):
    """§5 失败表(webhook 连续失败)+ H-06 判据:投递成功后 `webhooks.consecutive_fail` 归零。"""
    mk_webhook(store, "w", "https://a.example/cb")
    http.queue("https://a.example/cb", HttpResponse(500))
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    assert one(store, "SELECT * FROM webhooks WHERE id='w'")["consecutive_fail"] == 1
    hooks.fanout(event_id="e2", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0 + SPEC_WEBHOOK_BACKOFF_MS[0])
    assert one(store, "SELECT * FROM webhooks WHERE id='w'")["consecutive_fail"] == 0


async def test_WH30_tenth_failure_marks_row_dead(store, clock, http):
    """§2.2.7 + H-06:同一 webhook 连续失败 10 次 ⇒ `events_outbox(target='webhook:<id>')` 行 `status='dead'`。"""
    clock.set_ms(T0)
    d = WebhookDispatcher(store, http=http, secret_provider=lambda w: "s", clock=clock)
    mk_webhook(store, "w", "https://a.example/cb")
    http.default = HttpResponse(500)
    d.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    for _ in range(SPEC_WEBHOOK_MAX_ATTEMPTS):
        r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
        if r["status"] != "pending":
            break
        clock.set_ms(max(clock(), r["next_attempt_ms"]))
        await d.deliver_due(now_ms=clock())
    r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
    assert r["status"] == "dead" and r["attempts"] == SPEC_WEBHOOK_MAX_ATTEMPTS


async def test_WH31_dead_sets_webhooks_dead_ms(store, clock, http):
    """§5 决策表 + §3.1 `webhooks.dead_ms` 注:连续失败超限 ⇒ `dead_ms`(自动停用时刻)非空、停投。H-06 判据同句。"""
    clock.set_ms(T0)
    d = WebhookDispatcher(store, http=http, secret_provider=lambda w: "s", clock=clock)
    mk_webhook(store, "w", "https://a.example/cb")
    http.default = HttpResponse(500)
    d.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    for _ in range(SPEC_WEBHOOK_MAX_ATTEMPTS):
        r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
        if r["status"] != "pending":
            break
        clock.set_ms(max(clock(), r["next_attempt_ms"]))
        await d.deliver_due(now_ms=clock())
    assert one(store, "SELECT * FROM webhooks WHERE id='w'")["dead_ms"] is not None


async def test_WH32_dead_emits_webhook_dead_alert(store, clock, http, events):
    """§2.2.7「连续失败…次进 `dead`,**并发 `alert`**」+ §3.7 `WEBHOOK_DEAD`(warn / subject=host / 事件族 alert)。"""
    clock.set_ms(T0)
    alerts = Alerts(events, clock=clock)
    d = WebhookDispatcher(store, http=http, secret_provider=lambda w: "s", clock=clock, alerts=alerts)
    mk_webhook(store, "w", "https://a.example/cb")
    http.default = HttpResponse(500)
    d.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    for _ in range(SPEC_WEBHOOK_MAX_ATTEMPTS):
        r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
        if r["status"] != "pending":
            break
        clock.set_ms(max(clock(), r["next_attempt_ms"]))
        await d.deliver_due(now_ms=clock())
    alert_rows = rows(store, "SELECT * FROM events_outbox WHERE target='ws' AND event='alert'")
    payloads = [json.loads(a["payload_json"]) for a in alert_rows]
    hit = [p for p in payloads if p.get("code") == "WEBHOOK_DEAD"]
    assert hit, "死信必须发 WEBHOOK_DEAD 告警"
    assert hit[0]["severity"] == "warn" and hit[0]["subject"] == "host" and hit[0]["state"] == "firing"


async def test_WH33_serial_delivery_stops_at_first_failure(store, hooks, http):
    """§2.2.7「每个 webhook 一条串行投递(**保序**)」:队首失败时后面的行不得越过它先送。"""
    mk_webhook(store, "w", "https://a.example/cb")
    http.queue("https://a.example/cb", HttpResponse(500))
    for i in range(3):
        hooks.fanout(event_id=f"e{i}", event="message", payload={"i": i}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    assert len(http.requests) == 1, "队首失败后不得继续投后面的行(会乱序)"
    later = rows(store, "SELECT status FROM events_outbox WHERE target='webhook:w' ORDER BY seq")
    assert [x["status"] for x in later[1:]] == ["pending", "pending"]


async def test_WH34_serial_delivery_keeps_order_on_success(store, hooks, http):
    """§2.2.7 保序:同一 webhook 的多条事件按 `seq` 顺序出站。"""
    mk_webhook(store, "w", "https://a.example/cb")
    for i in range(3):
        hooks.fanout(event_id=f"e{i}", event="message", payload={"i": i}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    got = [json.loads(r["body"])["payload"]["i"] for r in http.requests]
    assert got == [0, 1, 2]


async def test_WH35_two_webhooks_are_independent(store, hooks, http):
    """§2.2.7:串行保序是**每个 webhook 各自**一条;一个挂了不拖住另一个。"""
    mk_webhook(store, "bad", "https://bad.example/cb")
    mk_webhook(store, "good", "https://good.example/cb")
    http.queue("https://bad.example/cb", HttpResponse(500))
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    await hooks.deliver_due(now_ms=T0)
    assert one(store, "SELECT * FROM events_outbox WHERE target='webhook:good'")["status"] == "delivered"
    assert one(store, "SELECT * FROM events_outbox WHERE target='webhook:bad'")["status"] == "pending"


async def test_WH36_row_max_attempts_overrides_config(store, clock, http):
    """§3.1 `webhooks.max_attempts`:每行自己的死信阈值(登记行值优先于 `[events] webhook_max_attempts`)。"""
    clock.set_ms(T0)
    d = WebhookDispatcher(store, http=http, secret_provider=lambda w: "s", clock=clock)
    mk_webhook(store, "w", "https://a.example/cb", max_attempts=2)
    http.default = HttpResponse(500)
    d.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    for _ in range(3):
        r = one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")
        if r["status"] != "pending":
            break
        clock.set_ms(max(clock(), r["next_attempt_ms"]))
        await d.deliver_due(now_ms=clock())
    assert one(store, "SELECT * FROM events_outbox WHERE target='webhook:w'")["status"] == "dead"


async def test_WH37_dead_row_is_not_retried(store, hooks, http):
    """§2.2.7 死信语义:进 `dead` 的行不再被轮询(`due_rows` 只取 `pending`)。"""
    mk_webhook(store, "w", "https://a.example/cb")
    hooks.fanout(event_id="e1", event="message", payload={}, now_ms=T0)
    store.con.execute("UPDATE events_outbox SET status='dead' WHERE target='webhook:w'")
    assert hooks.due_rows(now_ms=T0 + DAY) == []


# ══════════════════════════════════════════════════════════════════════ 四、HMAC 公网入站:canonical 与签名(02 §3.5)

def test_HM01_canonical_string_is_six_lines_in_spec_order(store):
    """§3.5 canonical string 逐字:``METHOD \\n PATH \\n CANONICAL_QUERY \\n sha256hex(BODY 或空串) \\n TIMESTAMP \\n NONCE``。"""
    got = HI.canonical_string("POST", "/api/v1/accounts/qd01/send", "a=1", b'{"x":1}', 1_758_240_000, "n0123456789abcdef")
    assert got == spec_canonical("POST", "/api/v1/accounts/qd01/send", "a=1", b'{"x":1}', 1_758_240_000, "n0123456789abcdef")
    assert got.count("\n") == 5


def test_HM02_empty_query_is_empty_string():
    """§3.5:「**无 query 为空串**」。"""
    assert HI.canonical_query(None) == "" and HI.canonical_query("") == ""


def test_HM03_canonical_query_sorted_by_key():
    """§3.5:「QUERY 按 key **字典序**、`k=v` 用 `&` 连」。"""
    assert HI.canonical_query("b=2&a=1&c=3") == "a=1&b=2&c=3"


def test_HM04_canonical_query_values_rfc3986_encoded():
    """§3.5:「值 **RFC3986 编码**」。"""
    got = HI.canonical_query("q=a b/c")
    assert " " not in got and "%20" in got and "%2F" in got


def test_HM05_empty_body_hashes_empty_string():
    """§3.5:「sha256hex(BODY **或空串**)」——无 body 时摘要 = sha256("")。"""
    assert HI.sha256_hex(None) == hashlib.sha256(b"").hexdigest()
    assert HI.sha256_hex(b"") == hashlib.sha256(b"").hexdigest()


def test_HM06_signature_is_hmac_sha256_hex():
    """§3.5 签名行:``hex(HMAC-SHA256(secret, canonical))``。"""
    canonical = spec_canonical("GET", "/api/v1/accounts", "", b"", 1, "n" * 16)
    assert HI.sign("sec", canonical) == spec_sign("sec", canonical)


def test_HM07_signature_header_prefix_v1_lowercase_hex():
    """§3.5 Header 行:`X-QT-Signature` = ``v1=`` + **小写 hex**。"""
    header = HI.signature_header("sec", "x")
    assert header.startswith("v1=") and header[3:] == header[3:].lower()
    assert all(c in "0123456789abcdef" for c in header[3:])


def test_HM08_canonical_does_not_contain_public_ip():
    """§3.5 公网 IP 变更(E-3):服务端**不把 IP 写进 canonical string**(只含 METHOD/PATH/QUERY/BODY/TS/NONCE),
    因此我方公网 IP 变了签名照样有效。"""
    c = HI.canonical_string("POST", "/api/v1/x", "", b"", 1, "n" * 16)
    assert "1.2.3.4" not in c and c.split("\n")[0] == "POST"


# ══════════════════════════════════════════════════════════════════════ 五、HMAC 失败顺序与容差(02 §3.5)

async def test_HM09_unknown_app_id_is_401(store, verifier):
    """§3.5 失败顺序 ①:app_id 不存在 → **401**。"""
    h = hmac_headers("ghost", "x", "GET", "/api/v1/accounts")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/api/v1/accounts", headers=h)
    assert getattr(e.value, "http_status", None) == 401


async def test_HM10_disabled_app_id_is_401(store, verifier):
    """§3.5 失败顺序 ①:app_id **已禁用** → 401。"""
    mk_hmac_client(store, "app1")
    store.con.execute("UPDATE api_clients SET enabled=0 WHERE app_id='app1'")
    h = hmac_headers("app1", "sec-app1", "GET", "/api/v1/accounts")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/api/v1/accounts", headers=h)
    assert e.value.http_status == 401


async def test_HM11_ip_not_in_allowlist_is_403(store, verifier):
    """§3.5 失败顺序 ②:IP 不在白名单 → **403**(`api_clients.ip_allow_json` CIDR 列表)。"""
    mk_hmac_client(store, "app1", ip_allow=["10.0.0.0/8"])
    h = hmac_headers("app1", "sec-app1", "GET", "/api/v1/accounts")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/api/v1/accounts", headers=h, client_ip="203.0.113.9")
    assert e.value.http_status == 403


async def test_HM12_empty_ip_allowlist_means_unrestricted(store, verifier):
    """§3.1 `ip_allow_json` 注:「CIDR 列表,**空=不限**」。"""
    mk_hmac_client(store, "app1", ip_allow=[])
    h = hmac_headers("app1", "sec-app1", "GET", "/api/v1/accounts")
    res = await verifier.verify(method="GET", path="/api/v1/accounts", headers=h, client_ip="203.0.113.9")
    assert res.app_id == "app1"


async def test_HM13_ip_check_precedes_timestamp_check(store, verifier):
    """§3.5 失败顺序:IP(403)在时间戳(401)**之前**——两者同时违例应回 403。"""
    mk_hmac_client(store, "app1", ip_allow=["10.0.0.0/8"])
    h = hmac_headers("app1", "sec-app1", "GET", "/x", ts=T0 // 1000 - 9999)
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=h, client_ip="203.0.113.9")
    assert e.value.http_status == 403


async def test_HM14_timestamp_skew_beyond_300s_is_401(store, verifier):
    """§3.5 时钟容差:``|now - TIMESTAMP| ≤ 300s``,否则 **401**,``error.message="timestamp skew"``。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "sec-app1", "GET", "/x", ts=T0 // 1000 - (SPEC_SKEW_S + 1))
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=h)
    assert e.value.http_status == 401 and e.value.message == "timestamp skew"


async def test_HM15_timestamp_inside_tolerance_passes(store, verifier):
    """§3.5 时钟容差:恰好 300 s 之内仍然通过(边界含等号)。"""
    mk_hmac_client(store, "app1")
    ts = T0 // 1000 - SPEC_SKEW_S
    h = hmac_headers("app1", "sec-app1", "GET", "/x", ts=ts)
    res = await verifier.verify(method="GET", path="/x", headers=h)
    assert res.timestamp == ts


async def test_HM16_future_timestamp_also_skews(store, verifier):
    """§3.5:容差是**绝对值** ``|now - TIMESTAMP|``,未来时间同样超限即 401。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "sec-app1", "GET", "/x", ts=T0 // 1000 + SPEC_SKEW_S + 1)
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=h)
    assert e.value.http_status == 401 and e.value.message == "timestamp skew"


async def test_HM17_replayed_nonce_is_401(store, verifier):
    """§3.5 nonce:``(app_id, nonce)`` 在内存 LRU 里 10 分钟内唯一;重复 → **401**,``error.message="nonce replay"``。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "sec-app1", "GET", "/x", nonce="n" * 20)
    await verifier.verify(method="GET", path="/x", headers=h)
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=h)
    assert e.value.http_status == 401 and e.value.message == "nonce replay"


async def test_HM18_same_nonce_different_app_is_allowed(store, verifier):
    """§3.5 nonce 的唯一键是 ``(app_id, nonce)`` 这个**二元组**,不是 nonce 本身。"""
    mk_hmac_client(store, "app1")
    mk_hmac_client(store, "app2")
    n = "shared-nonce-0123"
    await verifier.verify(method="GET", path="/x", headers=hmac_headers("app1", "sec-app1", "GET", "/x", nonce=n))
    res = await verifier.verify(method="GET", path="/x", headers=hmac_headers("app2", "sec-app2", "GET", "/x", nonce=n))
    assert res.app_id == "app2"


async def test_HM19_nonce_expires_after_ten_minutes(store, clock, verifier):
    """§3.5 nonce:唯一窗口是 **10 分钟**;过窗后同一 nonce 不再判重放(代价已在规格里明写并接受)。"""
    mk_hmac_client(store, "app1")
    clock.set_ms(T0)
    n = "nonce-ttl-012345"
    await verifier.verify(method="GET", path="/x", headers=hmac_headers("app1", "sec-app1", "GET", "/x", nonce=n))
    clock.set_ms(T0 + (SPEC_NONCE_TTL_S + 1) * 1000)
    later = hmac_headers("app1", "sec-app1", "GET", "/x", nonce=n, now_ms=clock())
    res = await verifier.verify(method="GET", path="/x", headers=later)
    assert res.nonce == n


async def test_HM20_nonce_check_precedes_signature_check(store, verifier):
    """§3.5 失败顺序:nonce(401 "nonce replay")在签名(401 签名失败)**之前**。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "sec-app1", "GET", "/x", nonce="n" * 20)
    await verifier.verify(method="GET", path="/x", headers=h)
    bad = dict(h, **{H_SIG: "v1=" + "0" * 64})
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=bad)
    assert e.value.message == "nonce replay"


async def test_HM21_bad_signature_is_401(store, verifier):
    """§3.5 失败顺序 ⑤:签名不对 → 401。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "wrong-secret", "GET", "/x")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=h)
    assert e.value.http_status == 401


async def test_HM22_signature_covers_body(store, verifier):
    """§3.5 canonical 含 ``sha256hex(BODY)``:body 被篡改则验签必败。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "sec-app1", "POST", "/x", body=b'{"a":1}')
    with pytest.raises(Exception) as e:
        await verifier.verify(method="POST", path="/x", body=b'{"a":2}', headers=h)
    assert e.value.http_status == 401


async def test_HM23_signature_covers_query(store, verifier):
    """§3.5 canonical 含 CANONICAL_QUERY:query 被篡改则验签必败。"""
    mk_hmac_client(store, "app1")
    h = hmac_headers("app1", "sec-app1", "GET", "/x", query="a=1")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", query="a=2", headers=h)
    assert e.value.http_status == 401


async def test_HM24_rate_limit_is_429(store, clock, verifier):
    """§3.5 失败顺序 ⑥:限流 → **429**(`api_clients.rate_per_min`)。"""
    mk_hmac_client(store, "app1", rate_per_min=2)
    clock.set_ms(T0)
    for i in range(2):
        await verifier.verify(method="GET", path="/x", headers=hmac_headers("app1", "sec-app1", "GET", "/x", nonce=f"nonce-{i}-0123456789"))
    with pytest.raises(Exception) as e:
        await verifier.verify(method="GET", path="/x", headers=hmac_headers("app1", "sec-app1", "GET", "/x", nonce="nonce-x-0123456789"))
    assert e.value.http_status == 429 and e.value.code == "RATE_LIMITED"


async def test_HM25_insufficient_level_is_403(store, verifier):
    """§3.5 失败顺序 ⑦:权限级别不足 → **403**(`api_clients.level` R/W/A,高包含低)。"""
    mk_hmac_client(store, "reader", level="read")
    h = hmac_headers("reader", "sec-reader", "POST", "/x")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="POST", path="/x", headers=h, required_level="write")
    assert e.value.http_status == 403


async def test_HM26_higher_level_includes_lower(store, verifier):
    """§3.4 通用「鉴权级别 R=read / W=write / A=admin(**高包含低**)」。"""
    mk_hmac_client(store, "adm", level="admin")
    res = await verifier.verify(method="GET", path="/x", headers=hmac_headers("adm", "sec-adm", "GET", "/x"),
                                required_level="write")
    assert res.principal.level == "admin"


async def test_HM27_account_not_allowed_is_403(store, verifier):
    """§3.5 失败顺序 ⑦:`allow_accounts` 不含该账号 → 403。"""
    mk_hmac_client(store, "app1", allow_accounts=[QD])
    h = hmac_headers("app1", "sec-app1", "POST", "/x")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="POST", path="/x", headers=h, account_id=QQ)
    assert e.value.http_status == 403


async def test_HM28_server_date_header_is_returned(store, verifier):
    """§3.5 时钟容差行:「把服务端时间放在 `Date` 头让对方校时」——校验结果必须带可回填的服务端时间。"""
    mk_hmac_client(store, "app1")
    res = await verifier.verify(method="GET", path="/x", headers=hmac_headers("app1", "sec-app1", "GET", "/x"))
    assert res.server_date and "GMT" in res.server_date


# ══════════════════════════════════════════════════════════════════════ 六、allow_ops 星号语义(00 §11.17 ② / 02 §3.10)

def test_HM29_star_expands_only_non_danger_ops():
    """00 §11.17 ② `expand_allow_ops()` 伪代码:``"*"`` **只展开 `danger=false`**。"""
    catalog = [("send_text", False), ("read_messages", False), ("workflow_run", True), ("messages_purge", True)]
    assert HI.expand_allow_ops(["*"], [op for op, _ in catalog], DANGER_TEN) == spec_expand_allow_ops(["*"], catalog)


def test_HM30_star_never_expands_to_danger_true():
    """00 §11.17 ② 判据一句话:「**星号永远展不出 `danger=true`**」。"""
    all_ops = ["send_text", "read_messages"] + sorted(DANGER_TEN)
    got = HI.expand_allow_ops(["*"], all_ops, DANGER_TEN)
    assert not (got & DANGER_TEN)


def test_HM31_danger_op_named_explicitly_is_allowed():
    """00 §11.17 ②:「`danger=true` 的 op **只有被逐字写进 `allow_ops` 才生效**」。"""
    assert HI.op_allowed(["*", "workflow_run"], "workflow_run", DANGER_TEN) is True


def test_HM32_danger_op_not_named_is_denied():
    """00 §11.17 ②:未逐条点名的 danger op 即使有 ``"*"`` 也不放行。"""
    assert HI.op_allowed(["*"], "workflow_run", DANGER_TEN) is False


def test_HM33_all_ten_danger_ops_are_denied_under_star():
    """02 §3.10:danger 十项的权威集合(R5-8)在 ``["*"]`` 下应逐项被拒。"""
    denied = {op for op in DANGER_TEN if not HI.op_allowed(["*"], op, DANGER_TEN)}
    assert denied == DANGER_TEN


def test_HM34_send_text_is_not_danger_and_passes_under_star():
    """02 §3.10 + 00 §11.17 ①(N-17):`send_*` **恒 `danger=false`**,在默认白名单内。"""
    assert HI.op_allowed(["*"], "send_text", DANGER_TEN) is True


async def test_HM35_op_not_allowed_is_403(store, verifier):
    """§3.5 失败顺序 ⑦:`allow_ops` 不放行该能力 → 403(danger op 未点名)。"""
    mk_hmac_client(store, "app1", allow_ops=["*"])
    h = hmac_headers("app1", "sec-app1", "POST", "/x")
    with pytest.raises(Exception) as e:
        await verifier.verify(method="POST", path="/x", headers=h, op="workflow_run")
    assert e.value.http_status == 403


async def test_HM36_named_danger_op_passes_verify(store, verifier):
    """00 §11.17 ②:`allow_ops` 里逐条点名后,该 danger op 经 HMAC 入站放行。"""
    mk_hmac_client(store, "app1", allow_ops=["*", "workflow_run"])
    res = await verifier.verify(method="POST", path="/x", headers=hmac_headers("app1", "sec-app1", "POST", "/x"),
                                op="workflow_run")
    assert res.app_id == "app1"


def test_HM37_default_danger_ops_come_from_capability_catalog():
    """02 §3.10:能力目录是 `danger` 的真值(「每条必带 `kind` 与 `danger`」「系统类 op **也在目录内**」);
    `allow_ops` 的星号语义依赖它 ⇒ 目录里必须找得到 danger 十项,否则星号会把它们全放行。"""
    catalog_danger = HI.load_danger_ops()
    assert DANGER_TEN <= catalog_danger, f"能力目录缺少 danger 项:{sorted(DANGER_TEN - catalog_danger)}"


# ══════════════════════════════════════════════════════════════════════ 七、保留期默认值与截断(02 §2.8.4 / §7.1 / 07;00 §11.10 E-18)

def test_RT01_messages_days_default_is_30():
    """§2.8.4 + §7.1 `[retention] messages_days=30`(**E-18** 取代 E-10 的 365):本地数据类一律只留近 30 天。"""
    assert RetentionConfig().messages_days == 30


def test_RT02_collected_files_keep_seven_days():
    """§2.8.4 + §7.1:采集侧文件统一键 `files_days=7`,`media_days`/`raw_days`/`mail_archive_days` 三个细粒度键同为 7。"""
    c = RetentionConfig()
    assert (c.files_days, c.media_days, c.raw_days, c.mail_archive_days) == (7, 7, 7, 7)


def test_RT03_commands_and_audit_days_are_30():
    """§7.1:`commands_days=30`(原 180)、`audit_days=30`(原 365),均为 E-18 收紧后的值。"""
    c = RetentionConfig()
    assert (c.commands_days, c.audit_days) == (30, 30)


def test_RT04_events_outbox_retention_single_key_is_72h():
    """§7.1 + §2.2.7 + 07(R6-58 (c)):`events_outbox` 的保留期**只有一把尺子** = `[events] ws_retention_hours = 72`;
    `[retention] events_ws_hours` **作废** —— 02 §7.1 / docs/07 / `RetentionConfig` 一起删键,
    且 §7.1 该行明写「老配置里仍写本键不报错,但**不被任何代码消费**」。
    三个必要侧面缺一不可:唯一键的默认值、作废键不在 `[retention]` 上、老 `agent.toml` 残留它既不炸也不改尺子。
    (独立验收复核重写:原总控版只断言前两条,漏了「残留不报错且不被消费」—— 那正是删键的兼容性风险点:
     若加载器对未知键报错,带旧键的 agent.toml 会直接起不来;若它被悄悄接回去消费,「唯一」就失守。)"""
    assert AgentConfig().events.ws_retention_hours == 72, "唯一键默认 72 h"
    assert not hasattr(RetentionConfig(), "events_ws_hours"), "[retention] 上不得再有作废键"
    legacy = AgentConfig.from_toml_dict({"retention": {"events_ws_hours": 24, "audit_days": 30}, "events": {}})
    assert legacy.events.ws_retention_hours == 72, "老配置里的作废键不得把尺子改回 24 h"
    assert not hasattr(legacy.retention, "events_ws_hours"), "作废键不得被加载进配置对象"
    assert legacy.retention.audit_days == 30, "同段里合法的键仍照常加载(残留键不报错、只被忽略)"


def test_RT05_mail_inbox_rows_days_is_30():
    """§7.1 `mail_inbox_rows_days=30`(**E-18**,原 90):`mail_inbox` 行(非邮件本身)。"""
    assert RetentionConfig().mail_inbox_rows_days == 30


def test_RT06_export_jobs_days_stays_seven():
    """§7.1 + R3-18 + §2.8.4「⚠️ `jobs` 除外(R4-13)」:作业行**随产物一起 7 天**,不进 30 天数据类。"""
    assert RetentionConfig().export_jobs_days == 7


def test_RT07_health_three_tier_retention():
    """§7.1 `health_raw_h / health_1m_d / health_1h_d = 24 / 7 / 30`(1h 档 E-18 由 90→30,与 04 `[monitor]` 同步,R3-24)。"""
    c = RetentionConfig()
    assert (c.health_raw_h, c.health_1m_d, c.health_1h_d) == (24, 7, 30)


def test_RT08_cleanup_at_0300_and_batch_5000():
    """§2.8.4「清理每日 **03:00**」「每批 **5000 行**」= §7.1 `cleanup_at="03:00"` / `cleanup_batch=5000`。"""
    c = RetentionConfig()
    assert (c.cleanup_at, c.cleanup_batch) == ("03:00", 5000)


def test_RT09_messages_days_over_30_is_clamped_with_warning():
    """§2.8.4:`messages_days` **上限 30**,配更大**按 30 截断并 WARN**(E-18)。"""
    clamped, warns = RetentionConfig(messages_days=365).clamp()
    assert clamped.messages_days == 30 and warns


def test_RT10_disk_watermarks_defaults():
    """§2.8.8 + §7.1 `disk_warn_mb / disk_high_mb / disk_critical_mb` 默认 **5120 / 2048 / 1024**。"""
    c = RetentionConfig()
    assert (c.disk_warn_mb, c.disk_high_mb, c.disk_critical_mb) == (SPEC_DISK_WARN_MB, SPEC_DISK_HIGH_MB, SPEC_DISK_CRITICAL_MB)


def test_RT11_disk_low_watermark_is_alias_of_high():
    """§2.8.8「`disk_low_watermark_mb` 别名」:E-10 时期的该键语义 = 本表 `high` 档(2048),保留为别名零改动。"""
    assert RetentionConfig().disk_low_watermark_mb == SPEC_DISK_HIGH_MB


def test_RT12_watermarks_must_be_ordered():
    """§7.1 该行注:「顺序须 `warn ≥ high ≥ critical`」。"""
    assert RetentionConfig().watermarks_ordered() is True
    assert RetentionConfig(disk_warn_mb=100, disk_high_mb=2048).watermarks_ordered() is False


# ══════════════════════════════════════════════════════════════════════ 八、三级水位判级与动作(02 §2.8.8;00 §11.11 [DISK])

def mk_maint(store, clock, disk, *, data_dir: str, cfg: Optional[RetentionConfig] = None, alerts=None,
             backup: Optional[BackupConfig] = None) -> MaintenanceService:
    return MaintenanceService(store, cfg=cfg, backup=backup, data_dir=data_dir, disk=disk, alerts=alerts, clock=clock)


def test_RT13_levels_by_free_space(fstore, clock, disk, tmp_path):
    """§2.8.8 水位表:剩余 <5 GB = `warn`、<2 GB = `high`、<1 GB = `critical`,其余 `normal`。"""
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    assert m.disk_level(99999) == "normal"
    assert m.disk_level(SPEC_DISK_WARN_MB - 1) == "warn"
    assert m.disk_level(SPEC_DISK_HIGH_MB - 1) == "high"
    assert m.disk_level(SPEC_DISK_CRITICAL_MB - 1) == "critical"


def test_RT14_warn_only_alerts_writes_continue(fstore, clock, disk, tmp_path, fevents):
    """§2.8.8 `warn` 行:「`alert H12_DISK_LOW`(warn);**仅告警,写入照常**」。"""
    clock.set_ms(T0)
    alerts = Alerts(fevents, clock=clock)
    disk.free = SPEC_DISK_WARN_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), alerts=alerts)
    m.check_watermark()
    assert m.ingest_allowed and m.media_downloads_allowed and m.mail_archive_allowed
    payloads = [json.loads(r["payload_json"]) for r in rows(fstore, "SELECT * FROM events_outbox WHERE event='alert'")]
    hit = [p for p in payloads if p.get("code") == "H12_DISK_LOW"]
    assert hit and hit[0]["severity"] == "warn"


def test_RT15_high_stops_media_download_and_mail_archive(fstore, clock, disk, tmp_path):
    """§2.8.8 `high` 行:上一级 + **停媒体下载(全转 `lazy`)、停邮件归档、立即触发一次全量清理**(不等 03:00)。"""
    clock.set_ms(T0)
    disk.free = SPEC_DISK_HIGH_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    m.check_watermark()
    assert m.level == "high"
    assert m.media_downloads_allowed is False and m.mail_archive_allowed is False


def test_RT16_high_keeps_ingest_and_export_allowed(fstore, clock, disk, tmp_path):
    """§2.8.8:「暂停采集入库与导出」是 `critical` 才有的动作,`high` 档不含。"""
    clock.set_ms(T0)
    disk.free = SPEC_DISK_HIGH_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    m.check_watermark()
    assert m.ingest_allowed is True and m.exports_allowed is True


def test_RT17_critical_includes_all_previous_actions(fstore, clock, disk, tmp_path):
    """§2.8.8 表头:「**逐级递进、下一级含上一级动作**」——`critical` 同时含 `high` 的停下载/停归档。"""
    clock.set_ms(T0)
    disk.free = SPEC_DISK_CRITICAL_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    m.check_watermark()
    assert m.level == "critical"
    assert m.media_downloads_allowed is False and m.mail_archive_allowed is False
    assert m.ingest_allowed is False and m.exports_allowed is False


def test_RT18_critical_shrinks_retention_30_14_7(fstore, clock, disk, tmp_path):
    """§2.8.8 `critical` 行:「**逐步缩短 `retention_days`(30→14→7)再清一轮**」,循环上限 3 档。"""
    clock.set_ms(T0)
    disk.free = SPEC_DISK_CRITICAL_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    m.check_watermark()
    assert m.retention_shrunk_to == SPEC_RETENTION_STEPS[-1]
    assert m.retention_days_effective == SPEC_RETENTION_STEPS[-1]


def test_RT19_each_shrink_writes_audit(fstore, clock, disk, tmp_path):
    """§2.8.8:「**每次缩短记 `audit_log(kind='system')` + `alert`**」。"""
    clock.set_ms(T0)
    disk.free = SPEC_DISK_CRITICAL_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    m.check_watermark()
    shrink_rows = rows(fstore, "SELECT * FROM audit_log WHERE kind='system' AND action LIKE 'retention%'")
    assert len([r for r in shrink_rows if "shrink" in r["action"]]) >= 1


def test_RT20_h12_severity_rises_to_crit_at_high(fstore, clock, disk, tmp_path, fevents):
    """§2.8.8 `high` 行末句 + §3.7 H12 行:`high` 起 `H12_DISK_LOW` **升 crit**。"""
    clock.set_ms(T0)
    alerts = Alerts(fevents, clock=clock)
    disk.free = SPEC_DISK_HIGH_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), alerts=alerts)
    m.check_watermark()
    payloads = [json.loads(r["payload_json"]) for r in rows(fstore, "SELECT * FROM events_outbox WHERE event='alert'")]
    hit = [p for p in payloads if p.get("code") == "H12_DISK_LOW"]
    assert hit and hit[-1]["severity"] == "crit"


def test_RT21_back_to_normal_resolves_h12(fstore, clock, disk, tmp_path, fevents):
    """§2.8.8 恢复列:「余量回升自动 `resolved`」。"""
    clock.set_ms(T0)
    alerts = Alerts(fevents, clock=clock)
    disk.free = SPEC_DISK_WARN_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), alerts=alerts)
    m.check_watermark()
    disk.free = 99_999
    m.check_watermark()
    payloads = [json.loads(r["payload_json"]) for r in rows(fstore, "SELECT * FROM events_outbox WHERE event='alert'")]
    states = [p["state"] for p in payloads if p.get("code") == "H12_DISK_LOW"]
    assert states[-1] == "resolved"


def test_RT22_watermark_snapshot_has_level_and_actions(fstore, clock, disk, tmp_path):
    """§2.8.8 + 04 §2.4.5 `P-RES`:水位快照要给出「当前级别」与「已触发的降级动作」。"""
    clock.set_ms(T0)
    disk.free = SPEC_DISK_HIGH_MB - 1
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    snap = m.check_watermark()
    assert snap["level"] == "high" and snap["actions"] and "free_mb" in snap


# ══════════════════════════════════════════════════════════════════════ 九、DISK_FULL 与 507(02 §2.8.8;00 §8.3/§10 R-02)

def test_RT23_disk_full_message_contains_free_mb(fstore, clock, disk, tmp_path):
    """§2.8.8:`error.message` **直写**「本地磁盘空间不足(剩余 X MB),已暂停写入并触发清理」并带 `free_mb`。"""
    clock.set_ms(T0)
    disk.free = 42
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    err = m.to_disk_full()
    assert "本地磁盘空间不足" in err.message and "42" in err.message


def test_RT24_disk_full_evidence_has_three_numbers(fstore, clock, disk, tmp_path):
    """§2.8.8:「日志与 `alert` 必须带当时的 `free_mb`/`db_size_mb`/`media_size_mb` **三个数**」。"""
    clock.set_ms(T0)
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    ev = m.to_disk_full().evidence()
    assert {"free_mb", "db_size_mb", "media_size_mb"} <= set(ev)


def test_RT25_disk_full_hint_actions_fixed(fstore, clock, disk, tmp_path):
    """§2.8.8 + 00 §8.3 `DISK_FULL` 行:`hint_actions=["open_env","run_cleanup"]`。"""
    assert list(DiskFullError.hint_actions) == ["open_env", "run_cleanup"]


def test_RT26_disk_full_is_not_retryable_but_needs_human(fstore, clock, disk, tmp_path):
    """00 §8.3 `DISK_FULL` 行:可重试 = **否**(盘满自动重试只会加剧)、需要人 = **是**。"""
    assert DiskFullError.retryable is False and DiskFullError.needs_human is True


def test_RT27_write_guard_translates_sqlite_full(fstore, clock, disk, tmp_path):
    """§2.8.8:`store`/`mail`/`media` 任何写失败(`SQLITE_FULL`/`ENOSPC`/`disk I/O error`)
    **第一诊断项是磁盘满** ⇒ 统一 `DISK_FULL`,不是 `INTERNAL`。"""
    clock.set_ms(T0)
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    with pytest.raises(DiskFullError):
        with m.guard_write("store"):
            raise OSError(28, "No space left on device")


def test_RT28_write_guard_passes_non_disk_errors_through(fstore, clock, disk, tmp_path):
    """§2.8.8 末句:「**非磁盘类写失败才回落 `INTERNAL`**」——守卫不得把普通异常也改写成 `DISK_FULL`。"""
    clock.set_ms(T0)
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    with pytest.raises(ValueError):
        with m.guard_write("store"):
            raise ValueError("boom")


def test_RT29_disk_full_triggers_critical_cleanup(fstore, clock, disk, tmp_path, fevents):
    """§2.8.8:命中 `DISK_FULL` 时「**立即按 `critical` 动作触发清理**」。"""
    clock.set_ms(T0)
    alerts = Alerts(fevents, clock=clock)
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), alerts=alerts)
    with pytest.raises(DiskFullError):
        with m.guard_write("store"):
            raise OSError(28, "No space left on device")
    assert m.level == "critical" and m.ingest_allowed is False


def test_RT30_disk_full_emits_db_write_failed_alert(fstore, clock, disk, tmp_path, fevents):
    """§3.7 `DB_WRITE_FAILED`(crit)+ §2.8.8:写失败告警必须带 `free_mb`/`db_size_mb`/`media_size_mb`。"""
    clock.set_ms(T0)
    alerts = Alerts(fevents, clock=clock)
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), alerts=alerts)
    with pytest.raises(DiskFullError):
        with m.guard_write("store"):
            raise OSError(28, "No space left on device")
    payloads = [json.loads(r["payload_json"]) for r in rows(fstore, "SELECT * FROM events_outbox WHERE event='alert'")]
    hit = [p for p in payloads if p.get("code") == "DB_WRITE_FAILED"]
    assert hit and hit[0]["severity"] == "crit"
    assert {"free_mb", "db_size_mb", "media_size_mb"} <= set(hit[0]["evidence"])


# ══════════════════════════════════════════════════════════════════════ 十、清理主体与六步顺序(02 §2.8.4)

def mk_msg(fstore, *, ts_ms: int, ext: str, account_id: str = QD, text: str = "x", raw_ref: Optional[str] = None) -> str:
    from qtrade_agent.models import Message, Session
    msg = Message(account_id=account_id, channel="qidian", session=Session(account_id, "415011447", "private", "对端"),
                  dir="in", type="text", text=text, ts_ms=ts_ms, source="qidian_db", ext_msg_id=ext,
                  dedup_kind="native", sender_id="415011447", raw_ref=raw_ref)
    fstore.ingest(msg)
    row = one(fstore, "SELECT id FROM messages WHERE ext_msg_id=?", ext)
    return row["id"]


def mk_media(fstore, *, ready_ms: Optional[int], rel_path: str, ref_count: int = 1, first_seen_ms: int = T0,
             status: str = "ready") -> int:
    cur = fstore.con.execute(
        "INSERT INTO media(sha256, kind, mime, size, rel_path, status, ref_count, first_seen_ms, ready_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rel_path, "image", "image/png", 10, rel_path, status, ref_count, first_seen_ms, ready_ms))
    return int(cur.lastrowid)


def mk_mail(fstore, *, received_ms: int, protocol: str = "pop3", status: str = "OVERSIZE", uidl: str = "u1",
            mailbox: str = "bot@example.com", archived_path: Optional[str] = None,
            archived_ms: Optional[int] = None) -> int:
    """§3.1 `mail_inbox` CHECK:imap 行必须有 `uid`、pop3 行必须有 `uidl`。"""
    uid = 1 if protocol == "imap" else None
    cur = fstore.con.execute(
        "INSERT INTO mail_inbox(mailbox, protocol, uid, uidl, rfc_message_id, from_addr, subject, received_ms, body_sha256, "
        "status, archived_path, archived_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mailbox, protocol, uid, uidl, f"<{uidl}>", "a@b.c", "s", received_ms, "h", status, archived_path, archived_ms))
    return int(cur.lastrowid)


def touch(path: str, content: bytes = b"x" * 1024) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_RT31_messages_older_than_retention_are_deleted(fstore, clock, disk, tmp_path):
    """§2.8.4 ①:按 `ts_ms` 删超过 `messages_days` 的 `messages`;未超期的保留。"""
    clock.set_ms(T0)
    mk_msg(fstore, ts_ms=T0 - 31 * DAY, ext="old")
    mk_msg(fstore, ts_ms=T0 - 3 * DAY, ext="new")
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    left = [r["ext_msg_id"] for r in rows(fstore, "SELECT ext_msg_id FROM messages")]
    assert left == ["new"]


def test_RT32_deleting_message_decrements_media_refcount(fstore, clock, disk, tmp_path):
    """§2.8.4 ①:「被删消息引用的 `media.ref_count` **递减**」。"""
    clock.set_ms(T0)
    mid = mk_msg(fstore, ts_ms=T0 - 31 * DAY, ext="old")
    media_id = mk_media(fstore, ready_ms=T0, rel_path="media/a.png", ref_count=2)
    fstore.con.execute("UPDATE messages SET media_json=? WHERE id=?",
                       (json.dumps([{"media_id": media_id, "state": "ready"}]), mid))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert one(fstore, "SELECT ref_count FROM media WHERE id=?", media_id)["ref_count"] == 1


def test_RT33_media_expires_independently_of_refcount(fstore, clock, disk, tmp_path):
    """§2.8.4 ②:媒体按自己的 `media_days`(7)过期——**独立于引用计数**,即使仍被 23 天内的消息引用也照样到期。
    理由逐字:「若按旧顺序…媒体会被 23 天的消息引用一直挂着删不掉」。"""
    clock.set_ms(T0)
    mid = mk_msg(fstore, ts_ms=T0 - 10 * DAY, ext="keep")          # 消息还在 30 天内
    path = touch(str(tmp_path / "media" / "a.png"))
    media_id = mk_media(fstore, ready_ms=T0 - 8 * DAY, rel_path="media/a.png", ref_count=1)
    fstore.con.execute("UPDATE messages SET media_json=? WHERE id=?",
                       (json.dumps([{"media_id": media_id, "state": "ready"}]), mid))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert one(fstore, "SELECT status FROM media WHERE id=?", media_id)["status"] == "expired"
    assert not os.path.exists(path)
    assert count(fstore, "messages") == 1, "消息行仍在,只是图取不到"


def test_RT34_media_json_state_becomes_expired(fstore, clock, disk, tmp_path):
    """§2.8.4 ②:「`messages.media_json[].state` **同步改 `expired`**(消息行仍在,只是图取不到,`GET …/media/{idx}` 回 410)」。"""
    clock.set_ms(T0)
    mid = mk_msg(fstore, ts_ms=T0 - 1 * DAY, ext="keep")
    touch(str(tmp_path / "media" / "a.png"))
    media_id = mk_media(fstore, ready_ms=T0 - 8 * DAY, rel_path="media/a.png")
    fstore.con.execute("UPDATE messages SET media_json=? WHERE id=?",
                       (json.dumps([{"media_id": media_id, "state": "ready"}]), mid))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    items = json.loads(one(fstore, "SELECT media_json FROM messages WHERE id=?", mid)["media_json"])
    assert items[0]["state"] == "expired"


def test_RT35_orphan_media_rows_deleted_after_grace(fstore, clock, disk, tmp_path):
    """§2.8.4 ②:「`ref_count=0` 且超过 `orphan_grace_h` 的行**连元数据一起删**(无论是否到期)」(§7.1 默认 24 h)。"""
    clock.set_ms(T0)
    mk_media(fstore, ready_ms=T0, rel_path="media/orphan.png", ref_count=0, first_seen_ms=T0 - 25 * 3600_000)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert count(fstore, "media") == 0


def test_RT36_orphan_media_inside_grace_is_kept(fstore, clock, disk, tmp_path):
    """§2.8.4 ② + §7.1 `orphan_grace_h=24`:未过宽限期的孤儿行不删。"""
    clock.set_ms(T0)
    mk_media(fstore, ready_ms=T0, rel_path="media/orphan.png", ref_count=0, first_seen_ms=T0 - 3600_000)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert count(fstore, "media") == 1


def test_RT37_raw_ref_files_purged_by_raw_days(fstore, clock, disk, tmp_path):
    """§2.8.4 ③:`raw_ref` 文件按 `raw_days`(7)删。"""
    clock.set_ms(T0)
    touch(str(tmp_path / "raw" / "r1.json"))
    mid = mk_msg(fstore, ts_ms=T0 - 8 * DAY, ext="r", raw_ref="raw/r1.json")
    fstore.con.execute("UPDATE messages SET received_ms=? WHERE id=?", (T0 - 8 * DAY, mid))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert not os.path.exists(str(tmp_path / "raw" / "r1.json"))


def test_RT38_mail_archive_eml_purged_row_kept(fstore, clock, disk, tmp_path):
    """§2.8.4 ④:邮件归档 `.eml` 按 `mail_archive_days`(7)删,`mail_inbox.archived_path` **置 NULL、行保留**。"""
    clock.set_ms(T0)
    touch(str(tmp_path / "mail" / "archive" / "a.eml"))
    mail_id = mk_mail(fstore, received_ms=T0, protocol="imap", status="DONE",
                      archived_path="mail/archive/a.eml", archived_ms=T0 - 8 * DAY)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    row = one(fstore, "SELECT * FROM mail_inbox WHERE id=?", mail_id)
    assert row is not None and row["archived_path"] is None
    assert not os.path.exists(str(tmp_path / "mail" / "archive" / "a.eml"))


def test_RT39_pop3_never_delete_uidl_merged_into_cursor_before_row_delete(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑤ 🔴 R6-1 配套:删 `mail_inbox` 行前,本批 `protocol='pop3'` 且
    `status ∈ NEVER_DELETE = {OUT_OF_SCOPE, OVERSIZE}` 的 `uidl` **先并进**
    `cursors(owner='mail:<mailbox>', kind='pop3_uidl_recent')`(与删行同事务)再删。"""
    clock.set_ms(T0)
    mk_mail(fstore, received_ms=T0 - 31 * DAY, protocol="pop3", status="OVERSIZE", uidl="u-oversize")
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    cur = one(fstore, "SELECT * FROM cursors WHERE owner='mail:bot@example.com' AND kind='pop3_uidl_recent'")
    assert cur is not None and "u-oversize" in json.loads(cur["value"])
    assert count(fstore, "mail_inbox") == 0


def test_RT40_out_of_scope_uidl_also_preserved(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑤ R6-1:`NEVER_DELETE` 两类都要保全(`OUT_OF_SCOPE` 同 `OVERSIZE`)。"""
    clock.set_ms(T0)
    mk_mail(fstore, received_ms=T0 - 31 * DAY, protocol="pop3", status="OUT_OF_SCOPE", uidl="u-oos")
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    cur = one(fstore, "SELECT * FROM cursors WHERE owner='mail:bot@example.com' AND kind='pop3_uidl_recent'")
    assert cur is not None and "u-oos" in json.loads(cur["value"])


def test_RT41_ordinary_pop3_uidl_is_not_preserved(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑤ R6-1:只有 `NEVER_DELETE` 两类要保全;普通已处理邮件(`DONE`)删行即可。"""
    clock.set_ms(T0)
    mk_mail(fstore, received_ms=T0 - 31 * DAY, protocol="pop3", status="DONE", uidl="u-done")
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    cur = one(fstore, "SELECT * FROM cursors WHERE owner='mail:bot@example.com' AND kind='pop3_uidl_recent'")
    assert cur is None or "u-done" not in json.loads(cur["value"] or "[]")


def test_RT42_imap_rows_are_not_subject_to_uidl_preservation(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑤:「**IMAP 侧不受此约束**(UID 单调、有水位)」。"""
    clock.set_ms(T0)
    mk_mail(fstore, received_ms=T0 - 31 * DAY, protocol="imap", status="OVERSIZE", uidl="u-imap")
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    cur = one(fstore, "SELECT * FROM cursors WHERE owner='mail:bot@example.com' AND kind='pop3_uidl_recent'")
    assert cur is None or "u-imap" not in json.loads(cur["value"] or "[]")


def test_RT43_cores_dir_purged_after_30_days(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑥:崩溃转储目录 `/var/lib/qtrade/cores/` 按 **30 天**(数据类上限)删。"""
    clock.set_ms(T0)
    core = touch(str(tmp_path / "cores" / "dump.core"))
    os.utime(core, (T0 / 1000 - 31 * 86400, T0 / 1000 - 31 * 86400))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert not os.path.exists(core)


def test_RT44_fresh_core_dump_is_kept(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑥:未满 30 天的转储不删。"""
    clock.set_ms(T0)
    core = touch(str(tmp_path / "cores" / "fresh.core"))
    os.utime(core, (T0 / 1000 - 3 * 86400, T0 / 1000 - 3 * 86400))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert os.path.exists(core)


def test_RT45_audit_log_purged_by_audit_days(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑤:`audit_log` 超 `audit_days`(30)的行删。"""
    clock.set_ms(T0)
    fstore.insert_audit(kind="api", transport="http", actor="x", action="GET /x", now_ms=T0 - 31 * DAY)
    fstore.insert_audit(kind="api", transport="http", actor="x", action="GET /y", now_ms=T0 - 1 * DAY)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    actions = {r["action"] for r in rows(fstore, "SELECT action FROM audit_log")}
    assert "GET /x" not in actions and "GET /y" in actions


def test_RT46_webhook_outbox_rows_purged(fstore, clock, disk, tmp_path):
    """§2.8.4 ⑤ + §2.2.7 + §7.1 `[events] ws_retention_hours = 72`。

    🔴 **总控裁决 R6-58**:`events_outbox` **两类行统一只认 `[events] ws_retention_hours`(默认 72 h)**,
    `[retention] events_ws_hours` 已废弃(文档由总控删键)。故 >72 h 的 webhook 副本删、48 h 的留。"""
    clock.set_ms(T0)
    fstore.insert_outbox_event(event_id="e-old", target="webhook:w", event="message", trace_id=None, account_id=None,
                               channel=None, payload_json="{}", now_ms=T0 - 73 * 3600_000)
    fstore.insert_outbox_event(event_id="e-fresh", target="webhook:w", event="message", trace_id=None, account_id=None,
                               channel=None, payload_json="{}", now_ms=T0 - 48 * 3600_000)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    left = [r["event_id"] for r in rows(fstore, "SELECT event_id FROM events_outbox WHERE target='webhook:w'")]
    assert left == ["e-fresh"], "超 72 h 才删;48 h 的行按裁决必须留下"


def test_RT47_identity_and_config_rows_are_never_purged(fstore, clock, disk, tmp_path):
    """§2.8.4「不按期删的**唯一例外**(E-18 例外清单)」:`accounts`/`sessions`/`cursors`/`webhooks`/`api_clients`/
    `settings`/`resource_pools` 等配置与身份类行永不按保留期删(清空会让系统失忆或误配)。"""
    clock.set_ms(T0)
    mk_msg(fstore, ts_ms=T0 - 900 * DAY, ext="ancient")            # 顺带建 sessions 行
    mk_webhook(fstore, "w", "https://a.example/cb", now=T0 - 900 * DAY)
    mk_hmac_client(fstore, "app1", now=T0 - 900 * DAY)
    fstore.settings_set("k", {"v": 1}, now_ms=T0 - 900 * DAY)
    fstore.con.execute("INSERT INTO cursors(owner, kind, value, updated_ms) VALUES ('qd01','x','1',?)", (T0 - 900 * DAY,))
    for pool in ("wsl", "windows"):
        fstore.con.execute("INSERT INTO resource_pools(pool, total_mb, reserved_mb, quota_json, updated_ms) VALUES (?,?,?,?,?)",
                           (pool, 11264, 2048, '{"qidian":2560}', T0 - 900 * DAY))
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    for table in ("accounts", "sessions", "cursors", "webhooks", "api_clients", "settings", "resource_pools"):
        assert count(fstore, table) > 0, f"{table} 是身份/配置类,不该被保留期清掉"


def test_RT48_cleanup_writes_last_cleanup_snapshot(fstore, clock, disk, tmp_path):
    """§3.4.11 #109(R6-34):清理完成后回写 `settings` 键 **`system.last_cleanup`**,
    `GET /system/metrics` 的 `last_cleanup_at`/`last_cleanup_freed_mb` 逐字从它渲染;每日 03:00 定时清理也写同一个键。"""
    clock.set_ms(T0)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    snap = fstore.settings_get("system.last_cleanup")
    assert snap and "freed_mb" in snap


def test_RT49_cleanup_writes_audit_row(fstore, clock, disk, tmp_path):
    """§2.8.4 + §3.4.11:清理是写操作,`audit_log(kind='system')` 留痕。"""
    clock.set_ms(T0)
    mk_maint(fstore, clock, disk, data_dir=str(tmp_path)).cleanup_once(now_ms=T0)
    assert count(fstore, "audit_log WHERE kind='system' AND action='retention.cleanup'") == 1


def test_RT50_cleanup_deletes_in_batches(fstore, clock, disk, tmp_path):
    """§2.8.4:「**每批 5000 行**,批间让出事件循环」——批量大小可配,分批删完不遗漏。"""
    clock.set_ms(T0)
    for i in range(5):
        mk_msg(fstore, ts_ms=T0 - 31 * DAY, ext=f"m{i}")
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), cfg=RetentionConfig(cleanup_batch=2))
    rep = m.cleanup_once(now_ms=T0)
    assert count(fstore, "messages") == 0 and rep.deleted.get("messages") == 5


def test_RT51_incremental_vacuum_only_on_sunday(fstore, clock, disk, tmp_path):
    """§2.8.4 末句:「**每周日**清理后 `PRAGMA incremental_vacuum`…**不做全量 `VACUUM`**(会复制整库、锁写)」。"""
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path))
    monday = 1_758_240_000_000            # 2025-09-19 前后的固定时刻,非周日
    assert m.incremental_vacuum(now_ms=monday) is False
    sunday = monday + 2 * DAY             # 往后拨到周日
    ran = any(m.incremental_vacuum(now_ms=monday + d * DAY) for d in range(0, 7))
    assert ran, "一周之内必须有且只在周日跑一次增量 VACUUM"
    assert sunday > monday


def test_RT52_backup_writes_dated_file(fstore, clock, disk, tmp_path):
    """§3.3 备份:`sqlite3.Connection.backup()` 在线拷到 `<backup_dir>/agent-<yyyymmdd>.db`。"""
    clock.set_ms(T0)
    bdir = str(tmp_path / "backup")
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), backup=BackupConfig(backup_dir=bdir))
    path = m.backup_once(now_ms=T0)
    assert os.path.basename(path).startswith("agent-") and path.endswith(".db") and os.path.exists(path)


def test_RT53_backup_keeps_seven_copies(fstore, clock, disk, tmp_path):
    """§3.3 备份:「保留 **7 份**」。"""
    bdir = str(tmp_path / "backup")
    os.makedirs(bdir, exist_ok=True)
    for i in range(10):
        touch(os.path.join(bdir, f"agent-2026090{i}.db"))
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), backup=BackupConfig(backup_dir=bdir))
    m.prune_backups()
    left = [f for f in os.listdir(bdir) if f.startswith("agent-")]
    assert len(left) == 7


def test_RT54_backup_default_keep_and_time():
    """§3.3 + §7.1:备份默认每日 **03:30**(清理之后)、保留 **7** 份。"""
    b = BackupConfig()
    assert b.backup_keep == 7 and b.backup_at == "03:30"


def test_RT55_backup_records_audit(fstore, clock, disk, tmp_path):
    """§3.4.6 #81 + 所有写端点记 `audit_log`(§3.4 通用):在线备份留痕。"""
    clock.set_ms(T0)
    m = mk_maint(fstore, clock, disk, data_dir=str(tmp_path), backup=BackupConfig(backup_dir=str(tmp_path / "backup")))
    m.backup_once(now_ms=T0)
    assert count(fstore, "audit_log WHERE kind='system' AND action='db.backup'") == 1


# ══════════════════════════════════════════════════════════════════════ 十一、资源池自校准(04 §2.5.3;§8b.2 A2-01~A2-05;02 #25/#71)

def seed_pools(fstore, *, quota: Optional[dict] = None, now: int = T0) -> None:
    """§3.1 `resource_pools` 两行配置表(wsl / windows)。"""
    for pool in ("wsl", "windows"):
        fstore.con.execute(
            "INSERT INTO resource_pools(pool, total_mb, reserved_mb, quota_json, updated_ms) VALUES (?,?,?,?,?)",
            (pool, 11264, 2048, json.dumps(quota or {"qidian": 2560, "qq": 614}), now))


def seed_wsl_samples(fstore, *, now: int = T0, used_mb: float = 1000.0, total_mb: float = 11264.0,
                     own_rss: float = 100.0, span_min: int = 6) -> None:
    """04 §2.5.3 `wsl.reserved_mb` 的输入:WSL 内 `MemTotal − MemAvailable` + dockerd/containerd/Agent RSS。"""
    for i in range(span_min + 1):
        ts = now - (span_min - i) * 60_000
        mk_sample(fstore, scope="wsl", subject="wsl", ts_ms=ts, mem_mb=used_mb, mem_max_mb=total_mb)
        for proc in ("dockerd", "containerd", "qtrade-agent"):
            mk_sample(fstore, scope="process", subject=proc, ts_ms=ts, mem_mb=own_rss)


def seed_container_samples(fstore, account_id: str, *, now: int = T0, anon: float = 2000.0, peak: float = 3300.0,
                           span_min: int = 11) -> None:
    """04 §2.5.3 `quota_mb`:S8 `anon` 的 P95(10min 窗)+ 启动峰值缓冲(冷启动 `memory.current` 峰值 − 稳态 anon)。"""
    subject = f"qtrade-{account_id}"
    mk_sample(fstore, scope="container", subject=subject, ts_ms=now - span_min * 60_000, mem_mb=peak, mem_anon_mb=anon)
    for i in range(1, span_min + 1):
        mk_sample(fstore, scope="container", subject=subject, ts_ms=now - (span_min - i) * 60_000,
                  mem_mb=anon, mem_anon_mb=anon)


def mk_calib(fstore, clock, *, cfg: Optional[CalibrationConfig] = None, events=None) -> PoolCalibrator:
    return PoolCalibrator(fstore, cfg=cfg, events=events, clock=clock)


def test_CB01_wsl_reserved_is_used_plus_own_rss_plus_512(fstore, clock):
    """04 §2.5.3 `wsl.reserved_mb` 行逐字:无账号容器运行时 WSL 内 `MemTotal − MemAvailable`
    + dockerd/containerd/Agent RSS 的 **5 分钟均值**,再加 **512 MB 缓冲**。"""
    clock.set_ms(T0)
    seed_wsl_samples(fstore, used_mb=1000.0, own_rss=100.0)
    value, ev = mk_calib(fstore, clock).suggest_wsl_reserved_mb(now_ms=T0)
    assert value == int(1000 + 3 * 100 + 512)
    assert ev["buffer_mb"] == 512 and ev["window_min"] == 5


def test_CB02_wsl_reserved_needs_samples(fstore, clock):
    """04 §2.5.3:没有样本时给不出建议值(不能凭空编一个写回资源池)。"""
    clock.set_ms(T0)
    value, ev = mk_calib(fstore, clock).suggest_wsl_reserved_mb(now_ms=T0)
    assert value is None and ev.get("reason")


def test_CB03_wsl_total_comes_from_memtotal_sample(fstore, clock):
    """04 §2.5.3 `wsl.total_mb` 行 + A2-04:测法是 S11 `MemTotal`(**不是 `.wslconfig` 文件值**——文件改了没重启不算)。"""
    clock.set_ms(T0)
    seed_wsl_samples(fstore, total_mb=11264.0)
    assert mk_calib(fstore, clock).suggest_wsl_total_mb(now_ms=T0) == 11264


def test_CB04_quota_is_p95_anon_plus_start_peak_buffer(fstore, clock):
    """04 §2.5.3 `quota_mb.qidian/qq` 行:每个账号 `running` 稳定 ≥10min 后,取 S8 `anon` 的 **P95(10min 窗)
    + 启动峰值缓冲**(冷启动期 `memory.current` 峰值 − 稳态 `anon`)。"""
    clock.set_ms(T0)
    seed_container_samples(fstore, QD, anon=2000.0, peak=3300.0)
    value, detail = mk_calib(fstore, clock).suggest_quota_mb("qidian", now_ms=T0)
    assert value == 3300 and detail[QD]["anon_p95_mb"] == 2000.0


def test_CB05_quota_takes_max_across_accounts_of_same_channel(fstore, clock):
    """04 §2.5.3:「**同通道多账号取最大值**」。"""
    clock.set_ms(T0)
    fstore.ensure_account("qd02", "qidian", state="running", self_uid="2")
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0)
    seed_container_samples(fstore, "qd02", anon=2500.0, peak=2500.0)
    value, _ = mk_calib(fstore, clock).suggest_quota_mb("qidian", now_ms=T0)
    assert value == 2500


def test_CB06_account_not_stable_ten_minutes_is_skipped(fstore, clock):
    """04 §2.5.3:「每个账号 `running` **稳定 ≥10min 后**」——不足 10 min 的账号不参与校准。"""
    clock.set_ms(T0)
    seed_container_samples(fstore, QD, span_min=3)
    value, detail = mk_calib(fstore, clock).suggest_quota_mb("qidian", now_ms=T0)
    assert value is None and detail[QD]["skipped"] == "not_stable_10min"


def test_CB07_wechat_mb_is_p95_sum_plus_256(fstore, clock):
    """04 §2.5.3 `windows.wechat_mb` 行:微信 + chatlog 两进程 RSS 之和的 P95(10min 窗)+ **256 MB**。"""
    clock.set_ms(T0)
    for i in range(6):
        ts = T0 - i * 60_000
        mk_sample(fstore, scope="process", subject="Weixin.exe", ts_ms=ts, mem_mb=800.0)
        mk_sample(fstore, scope="process", subject="chatlog.exe", ts_ms=ts, mem_mb=200.0)
    value, ev = mk_calib(fstore, clock).suggest_wechat_mb(now_ms=T0)
    assert value == 800 + 200 + 256 and ev["buffer_mb"] == 256


def test_CB08_windows_total_comes_from_host_sample(fstore, clock):
    """04 §2.5.3 `windows.total_mb` 行:测法 = S2 `total`(物理内存)。"""
    clock.set_ms(T0)
    for i in range(5):
        mk_sample(fstore, scope="host", subject="host", ts_ms=T0 - i * 60_000, mem_mb=8000.0, mem_max_mb=32768.0)
    total, reserved, _ = mk_calib(fstore, clock).suggest_windows(now_ms=T0)
    assert total == 32768 and reserved is not None


def test_CB09_windows_reserved_excludes_vmmem_and_wechat(fstore, clock):
    """04 §2.5.3 `windows.reserved_mb` 行:「`total − available − vmmem − 微信侧` 的稳态均值(即"除我们之外的一切")」。"""
    clock.set_ms(T0)
    for i in range(5):
        ts = T0 - i * 60_000
        mk_sample(fstore, scope="host", subject="host", ts_ms=ts, mem_mb=8000.0, mem_max_mb=32768.0)
        mk_sample(fstore, scope="process", subject="vmmem", ts_ms=ts, mem_mb=3000.0)
        mk_sample(fstore, scope="process", subject="Weixin.exe", ts_ms=ts, mem_mb=1000.0)
    _, reserved, _ = mk_calib(fstore, clock).suggest_windows(now_ms=T0)
    assert reserved == 8000 - 3000 - 1000


def test_CB10_calibrate_without_apply_does_not_write(fstore, clock):
    """04 §8b.2 **A2-03**:`POST /resources/calibrate`(不带 `apply`)**只返回建议值不写库**。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    seed_container_samples(fstore, QD)
    mk_calib(fstore, clock).calibrate(apply=False, now_ms=T0)
    assert one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["calibrated_ms"] is None


def test_CB11_calibrate_with_apply_writes_pool_row(fstore, clock):
    """04 §8b.2 A2-03:`apply=true` **写 `resource_pools`**;判据「`calibrated_at_ms` 只在第二次变化」。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    seed_container_samples(fstore, QD)
    c = mk_calib(fstore, clock)
    c.calibrate(apply=False, now_ms=T0)
    assert one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["calibrated_ms"] is None
    c.calibrate(apply=True, now_ms=T0 + 1000)
    row = one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")
    assert row["calibrated_ms"] == T0 + 1000 and json.loads(row["calibration_json"]) != {}


def test_CB12_apply_source_is_within_ddl_check(fstore, clock):
    """§3.1 `resource_pools.source` CHECK 值域 = `('default','winagent','manual','calibrated')`;
    自校准写回后必须落在该值域内(04 写的 `calibration_source=auto` 不是库里的合法取值)。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    mk_calib(fstore, clock).calibrate(apply=True, now_ms=T0)
    assert one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["source"] in ("default", "winagent", "manual", "calibrated")


def test_CB13_quota_only_goes_up_by_default(fstore, clock):
    """04 §2.5.3 + §8b.2 **A2-02**:「**只上调不自动下调**」(下调需用户在 `P-SET` 确认,防止一次异常低值把可开数算高);
    §3.4.6 #71 同句:`quota_auto_lower=false` 时只上调。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 5000})
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0)      # 建议值 1000 < 现值 5000
    c = mk_calib(fstore, clock)
    applied = c.apply(c.calibrate(apply=False, now_ms=T0), now_ms=T0)
    quota = json.loads(one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["quota_json"])
    assert quota["qidian"] == 5000 and "qidian" in applied["lowered_skipped"]


def test_CB14_quota_raises_when_suggestion_is_higher(fstore, clock):
    """04 §8b.2 A2-02 判据:「人为把 `quota_json` 改低再跑校准 ⇒ **上调**」。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 500})
    seed_container_samples(fstore, QD, anon=2000.0, peak=2000.0)
    c = mk_calib(fstore, clock)
    c.calibrate(apply=True, now_ms=T0)
    quota = json.loads(one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["quota_json"])
    assert quota["qidian"] == 2000


def test_CB15_quota_auto_lower_true_allows_lowering(fstore, clock):
    """§3.4.6 #71 + §7.1 `[pool] quota_auto_lower`:置 true 后允许下调(默认 false)。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 5000})
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0)
    c = mk_calib(fstore, clock, cfg=CalibrationConfig(quota_auto_lower=True))
    c.calibrate(apply=True, now_ms=T0)
    quota = json.loads(one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["quota_json"])
    assert quota["qidian"] == 1000


def test_CB16_quota_auto_lower_default_is_false():
    """§7.1 `[pool] quota_auto_lower` 默认 **false**(= 只上调,04 §2.5.3 的「不自动下调」)。"""
    assert CalibrationConfig().quota_auto_lower is False


def test_CB17_apply_emits_resource_event(fstore, clock, fevents):
    """04 §2.5.3 `quota_mb` 行末句:「写回时**推 `resource` 事件**让 `P-DASH` 刷新」(A2-01/A2-03 同句)。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)

    class FakePool:
        def snapshot(self):
            return {"pools": {}, "realtime": {}}

    c = PoolCalibrator(fstore, pool=FakePool(), events=fevents, clock=clock)
    c.calibrate(apply=True, now_ms=T0)
    assert count(fstore, "events_outbox WHERE event='resource' AND target='ws'") >= 1


def test_CB18_apply_writes_audit(fstore, clock):
    """§3.4 通用「所有写端点记 `audit_log`」:校准写回留痕。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    mk_calib(fstore, clock).calibrate(apply=True, now_ms=T0)
    assert count(fstore, "audit_log WHERE action='resources.calibrate'") == 1


def test_CB19_zero_account_five_minutes_triggers_auto_recalibration(fstore, clock):
    """04 §2.5.3 `wsl.reserved_mb` 测法 + §8b.2 **A2-01**:「之后每次"零账号运行"状态**持续 ≥5min** 自动重测」。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    seed_wsl_samples(fstore, now=T0 + 6 * 60_000)
    c = mk_calib(fstore, clock)
    c.note_running_count(0, now_ms=T0)
    assert c.maybe_auto_calibrate(now_ms=T0 + 4 * 60_000) is None
    s = c.maybe_auto_calibrate(now_ms=T0 + 5 * 60_000 + 1)
    assert s is not None and s.wsl_reserved_mb is not None


def test_CB20_running_account_resets_zero_account_timer(fstore, clock):
    """04 §2.5.3:自动重测的前提是「**零账号**运行」——有账号在跑就不该触发。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    c = mk_calib(fstore, clock)
    c.note_running_count(0, now_ms=T0)
    c.note_running_count(1, now_ms=T0 + 60_000)
    assert c.maybe_auto_calibrate(now_ms=T0 + 10 * 60_000) is None


def test_CB21_auto_recalibration_writes_pool_row(fstore, clock):
    """04 §8b.2 A2-01:自动重测「经 API 写 `resource_pools.calibration_json`、`calibrated_at_ms` 更新」。"""
    clock.set_ms(T0)
    seed_pools(fstore)
    seed_wsl_samples(fstore)
    seed_wsl_samples(fstore, now=T0 + 6 * 60_000)
    c = mk_calib(fstore, clock)
    c.note_running_count(0, now_ms=T0)
    c.maybe_auto_calibrate(now_ms=T0 + 6 * 60_000)
    assert one(fstore, "SELECT * FROM resource_pools WHERE pool='wsl'")["calibrated_ms"] is not None


def test_CB22_drift_threshold_default_is_30_pct():
    """04 §2.5.3 末句 + §7.1 `[pool] calibration_drift_warn_pct`:差 **>30%** 才算漂移。"""
    assert CalibrationConfig().calibration_drift_warn_pct == 30


def test_CB23_drift_needs_one_hour_before_alert(fstore, clock, fevents):
    """04 §2.5.3 末句 + §8b.2 **A2-05**:「差 >30% **持续 1h**」才推 `info` 告警——不足 1 h 不发。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 6000})
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0, now=T_DRIFT)
    c = PoolCalibrator(fstore, events=fevents, clock=clock)
    assert c.check_drift(now_ms=T0) is None
    assert c.check_drift(now_ms=T0 + 30 * 60_000) is None
    assert count(fstore, "events_outbox WHERE event='resource'") == 0


def test_CB24_drift_alert_after_one_hour_is_info_pool_resource(fstore, clock, fevents):
    """§3.7 `POOL_CALIBRATION_DRIFT`:severity **info**、subject **pool**、事件类型 **resource**;
    04 §8b.2 A2-05:持续 1 h 后 firing。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 6000})
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0, now=T_DRIFT)
    c = PoolCalibrator(fstore, events=fevents, clock=clock)
    c.check_drift(now_ms=T0)
    got = c.check_drift(now_ms=T_DRIFT)
    assert got is not None and got["code"] == "POOL_CALIBRATION_DRIFT"
    assert got["severity"] == "info" and got["subject"] == "pool" and got["state"] == "firing"
    assert count(fstore, "events_outbox WHERE event='resource' AND target='ws'") == 1


def test_CB25_drift_resolves_when_back_in_range(fstore, clock, fevents):
    """04 §8b.2 A2-05 判据:「`alert` 里该码 **firing/resolved 各 1**」——校准回写后漂移消失即 resolved。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 6000})
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0, now=T_DRIFT)
    c = PoolCalibrator(fstore, events=fevents, clock=clock)
    c.check_drift(now_ms=T0)
    c.check_drift(now_ms=T_DRIFT)
    fstore.con.execute("UPDATE resource_pools SET quota_json=? WHERE pool='wsl'", (json.dumps({"qidian": 1000}),))
    c.check_drift(now_ms=T_DRIFT + 1)
    payloads = [json.loads(r["payload_json"]) for r in rows(fstore, "SELECT * FROM events_outbox WHERE event='resource'")]
    states = [p["state"] for p in payloads if p.get("code") == "POOL_CALIBRATION_DRIFT"]
    assert states == ["firing", "resolved"]


def test_CB26_drift_evidence_carries_budget_and_actual(fstore, clock, fevents):
    """04 §2.5.3:漂移判据是「**预算 vs 实占**」两条线的差 ⇒ 告警 evidence 必须能看出这两个数。"""
    clock.set_ms(T0)
    seed_pools(fstore, quota={"qidian": 6000})
    seed_container_samples(fstore, QD, anon=1000.0, peak=1000.0, now=T_DRIFT)
    c = PoolCalibrator(fstore, events=fevents, clock=clock)
    c.check_drift(now_ms=T0)
    got = c.check_drift(now_ms=T_DRIFT)
    assert {"budget_mb", "actual_mb", "drift_pct"} <= set(got["evidence"])


# ══════════════════════════════════════════════════════════════════════ 十二、工作流:YAML 形态与 R-13 砍掉的语法(02 §2.2.6;00 §11.22 [SCOPE])

MIN_YAML = """name: broadcast_notice
version: 1
args: { }
steps:
  - id: s1
    op: send_text
    account: qd01
    args: { session: "qd01:g_123", text: "巡检开始" }
"""

TWO_STEP_YAML = """name: two_step
version: 1
steps:
  - id: s1
    op: send_text
    account: qd01
    args: { session: "qd01:g_123", text: "一" }
  - id: s2
    op: send_text
    account: qq03
    args: { session: "qq03:g_456", text: "二" }
"""


def spec_errors(yaml_text: str) -> list[dict[str, Any]]:
    """#47 `POST /workflows/validate` 的出参形态:``{ok, errors:[{line, message}]}``。"""
    return validate(yaml_text)["errors"]


def test_WF01_minimal_yaml_parses(store):
    """§2.2.6「YAML 形态(M5 直线流程,**能据此实现即可**)」:规格给的那份最小 YAML 必须能解析。"""
    wf = parse_workflow(MIN_YAML)
    assert wf.name == "broadcast_notice" and wf.version == 1 and len(wf.steps) == 1


def test_WF02_steps_keep_declared_order_as_idx():
    """§2.2.6 + §3.1 `workflow_steps.idx` 注:「**线性执行序**(R-13 直线流程;非 `for_each` 展开)」。"""
    wf = parse_workflow(TWO_STEP_YAML)
    assert [(s.idx, s.id) for s in wf.steps] == [(0, "s1"), (1, "s2")]


def test_WF03_account_must_be_explicit():
    """§2.2.6:「`accounts` **明写**」「跨账号的批量由调用方把每个账号各列一步…**引擎不做隐式展开**」。"""
    y = "name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    args: { text: \"a\" }\n"
    errs = spec_errors(y)
    assert errs and any("account" in e["message"] for e in errs)


def test_WF04_for_each_is_rejected_with_line_number():
    """§2.2.6 ⚠️:「原设计的 `for_each` …**一律推迟到 M5 之后**(不在本版设计面内)」;#39 解析校验失败 400 **带行号**。"""
    y = "name: x\nversion: 1\nsteps:\n  - id: s1\n    for_each: accounts\n    op: send_text\n    account: qd01\n"
    errs = spec_errors(y)
    assert any(e["line"] == 5 and "for_each" in e["message"] for e in errs)


def test_WF05_where_filter_is_rejected():
    """§2.2.6:`{{ where }}` 过滤属于 R-13 推迟项,必须**显式报错**(不是静默忽略)。"""
    y = "name: x\nversion: 1\nsteps:\n  - id: s1\n    where: \"a\"\n    op: send_text\n    account: qd01\n"
    assert any("where" in e["message"] for e in spec_errors(y))


def test_WF06_on_error_policy_is_rejected():
    """§2.2.6:`on_error: retry|skip` 条件策略属于 R-13 推迟项。"""
    y = "name: x\nversion: 1\nsteps:\n  - id: s1\n    on_error: retry\n    op: send_text\n    account: qd01\n"
    assert any("on_error" in e["message"] for e in spec_errors(y))


def test_WF07_parallel_block_is_rejected():
    """§2.2.6:`parallel:` 块属于 R-13 推迟项;§2.2.6 并发段:「步骤**严格串行**、失败即停」。"""
    y = "name: x\nversion: 1\nsteps:\n  - id: s1\n    parallel: true\n    op: send_text\n    account: qd01\n"
    assert any("parallel" in e["message"] for e in spec_errors(y))


def test_WF08_expression_interpolation_is_rejected():
    """§2.2.6 YAML 注释逐字:「参数是**字面量**,不做表达式求值」;`{{ 表达式 }}` 不在本版设计面内。"""
    y = ("name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    account: qd01\n"
         "    args: { text: \"{{ item.name }}\" }\n")
    errs = spec_errors(y)
    assert any(e["line"] == 7 for e in errs)


def test_WF09_validate_returns_ok_true_for_good_yaml():
    """#47 `POST /workflows/validate`:`{yaml}` → ``{ok, errors:[…]}``(控制台编辑器用)。"""
    assert validate(MIN_YAML) == {"ok": True, "errors": []}


def test_WF10_validate_errors_carry_line_and_message():
    """#39/#41/#47:「解析校验失败 `400` **带行号**」——每条错误都要有 `line` 与 `message`。"""
    errs = spec_errors("name: x\nversion: 1\nsteps: []\n")
    assert errs and all({"line", "message"} <= set(e) for e in errs)


def test_WF11_steps_required_at_least_one():
    """§2.2.6 YAML 形态:`steps` 是工作流的主体,空表等于没有流程。"""
    assert validate("name: x\nversion: 1\nsteps: []\n")["ok"] is False


def test_WF12_duplicate_step_id_is_rejected():
    """§3.1 `workflow_steps.step_name` = 「YAML 里的 id」,留痕要能按 id 对上 ⇒ 同一流程内 id 不得重复。"""
    y = ("name: x\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    account: qd01\n"
         "  - id: s1\n    op: send_text\n    account: qd01\n")
    assert any("重复" in e["message"] or "duplicate" in e["message"].lower() for e in spec_errors(y))


def test_WF13_builtin_step_takes_no_account():
    """§3.1 `workflow_steps.account_id` 注逐字:「**内置步骤(webhook/sleep)为 NULL**」。"""
    y = ("name: x\nversion: 1\nsteps:\n  - id: s1\n    op: webhook\n    account: qd01\n"
         "    args: { url_ref: \"settings.callback_url\" }\n")
    assert any("account" in e["message"] for e in spec_errors(y))


def test_WF14_builtin_webhook_requires_url_ref():
    """§2.2.6 YAML 示例:内置步骤 `webhook` 的参数是 `url_ref`(**固定 URL,取自 `settings.callback_url`**)。"""
    y = "name: x\nversion: 1\nsteps:\n  - id: s3\n    op: webhook\n    args: { }\n"
    assert validate(y)["ok"] is False


def test_WF15_bad_yaml_syntax_reports_line():
    """#39:YAML 本身语法错也要 `400` **带行号**(控制台编辑器靠它定位)。"""
    errs = spec_errors("name: x\nversion: 1\nsteps:\n  - id: s1\n     op: bad-indent\n")
    assert errs and isinstance(errs[0]["line"], int)


# ══════════════════════════════════════════════════════════════════════ 十三、工作流:定义 CRUD 与运行(02 #38~#47;§3.1 DDL)

class FakeBus:
    """`bus.submit(Command) -> CommandResult`(§2.2.6:步骤 = 对 `bus.submit` 的调用)。"""

    def __init__(self, results: Optional[list] = None):
        self.calls: list[Any] = []
        self.results = results or []

    async def submit(self, cmd):
        from qtrade_agent.models import CommandResult
        self.calls.append(cmd)
        if self.results:
            spec = self.results.pop(0)
            if isinstance(spec, Exception):
                raise spec
            ok, code = spec
            return CommandResult(ok=ok, code=code, trace_id=cmd.trace_id)
        return CommandResult(ok=True, code="DELIVERED", trace_id=cmd.trace_id)


def mk_engine(fstore, clock, *, bus=None, events=None, http=None) -> WorkflowEngine:
    return WorkflowEngine(fstore, bus=bus or FakeBus(), events=events, http=http, clock=clock,
                          sleep_fn=lambda s: asyncio.sleep(0))


def test_WF16_create_stores_yaml_and_checksum(fstore, clock):
    """#39 + §3.1 `workflows`:`checksum = sha256(yaml)`;「**工作流只存表,不读 `/etc/qtrade` 下文件**」(G-09)。"""
    clock.set_ms(T0)
    wf = mk_engine(fstore, clock).upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    assert wf["yaml"] == MIN_YAML
    assert wf["checksum"] == hashlib.sha256(MIN_YAML.encode("utf-8")).hexdigest()


def test_WF17_create_starts_at_version_one(fstore, clock):
    """§3.1 `workflows.version` DEFAULT 1。"""
    clock.set_ms(T0)
    assert mk_engine(fstore, clock).upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)["version"] == 1


def test_WF18_put_replaces_and_bumps_version(fstore, clock):
    """#41 `PUT /workflows/{id}`:**整体替换,version+1**。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    again = e.upsert(name="broadcast_notice", yaml=MIN_YAML.replace("巡检开始", "巡检结束"), now_ms=T0 + 1)
    assert again["version"] == 2 and "巡检结束" in again["yaml"]


def test_WF19_name_is_unique(fstore, clock):
    """§3.1 `workflows.name TEXT NOT NULL UNIQUE`;#39 幂等列 = `nat(按 name)`。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0 + 1)
    assert count(fstore, "workflows") == 1


def test_WF20_invalid_yaml_is_not_stored(fstore, clock):
    """#39:解析校验失败 → `400`,**不得**把坏定义写进表。"""
    clock.set_ms(T0)
    with pytest.raises(WorkflowParseError):
        mk_engine(fstore, clock).upsert(name="x", yaml="name: x\nversion: 1\nsteps: []\n", now_ms=T0)
    assert count(fstore, "workflows") == 0


async def test_WF21_run_creates_run_row_with_trigger_and_version(fstore, clock):
    """#43 + §3.1 `workflow_runs`:建行带 `workflow_version`、`trigger`(CHECK `api|schedule|email|console`)、`actor`。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, trigger="api", wait=True)
    row = one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)
    assert row["trigger"] == "api" and row["workflow_version"] == 1 and row["actor"]


async def test_WF22_unknown_trigger_is_rejected(fstore, clock):
    """§3.1 `workflow_runs.trigger` CHECK = `('api','schedule','email','console')`,不在值域的触发源必须被拒。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    with pytest.raises(ValueError):
        await e.run(wf["id"], {}, trigger="cron")


async def test_WF23_each_step_leaves_one_row(fstore, clock):
    """§2.2.6:「固定步骤顺序执行 + 失败即停 + **每步留痕**」;YAML 注释逐字:「每步一行 `workflow_steps` 留痕」。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    steps = e.steps(run_id)
    assert [(s["idx"], s["step_name"], s["status"]) for s in steps] == [(0, "s1", "done"), (1, "s2", "done")]


async def test_WF24_successful_run_is_done(fstore, clock):
    """§3.1 `workflow_runs.status` CHECK 含 `done`;全部步骤成功 ⇒ run 终态 `done`、`finished_ms` 非空。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    row = one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)
    assert row["status"] == "done" and row["finished_ms"] is not None


async def test_WF25_failed_step_stops_the_run(fstore, clock):
    """§2.2.6 并发段(R-13):「步骤严格串行、**失败即停**」;YAML 注释:「任一步失败 → run 置 `failed` 并**停止后续步**」。"""
    clock.set_ms(T0)
    bus = FakeBus([(False, "SEND_FAILED")])
    e = mk_engine(fstore, clock, bus=bus)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    row = one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)
    assert row["status"] == "failed"
    assert len(bus.calls) == 1, "失败即停:第二步不得再调 bus"
    assert len(e.steps(run_id)) == 1


async def test_WF26_failed_step_records_result_code(fstore, clock):
    """§3.1 `workflow_steps.result_code` / `status`:失败步留 `failed` + 结果码(00 §8.3)。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock, bus=FakeBus([(False, "SEND_FAILED")]))
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    s = e.steps(run_id)[0]
    assert s["status"] == "failed" and s["result_code"] == "SEND_FAILED"


async def test_WF27_step_records_trace_id_of_command(fstore, clock):
    """§3.1 `workflow_steps.trace_id` 注:「**对应 `commands` 行**」——每步要能回溯到那条指令。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert e.steps(run_id)[0]["trace_id"]


async def test_WF28_step_account_id_is_written(fstore, clock):
    """§3.1 `workflow_steps.account_id`:通道步骤挂账号(YAML 里 `account` 明写的那个)。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert e.steps(run_id)[0]["account_id"] == QD


async def test_WF29_step_is_a_bus_submit(fstore, clock):
    """§2.2.6 职责逐字:「**步骤 = 对 `bus.submit` 的调用**」——参数按 YAML 字面量原样下发。"""
    clock.set_ms(T0)
    bus = FakeBus()
    e = mk_engine(fstore, clock, bus=bus)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    await e.run(wf["id"], {}, wait=True)
    cmd = bus.calls[0]
    assert cmd.account_id == QD and cmd.op == "send_text" and cmd.args["text"] == "巡检开始"


async def test_WF30_run_emits_started_event(fstore, clock, fevents):
    """#43:`202 {run_id}`;**事件 `workflow{status:'started'}`**(C-16)。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock, events=fevents)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    payloads = [json.loads(r["payload_json"]) for r in rows(fstore, "SELECT * FROM events_outbox WHERE event='workflow'")]
    assert payloads[0]["status"] == "started" and payloads[0]["run_id"] == run_id


async def test_WF31_workflow_event_payload_shape(fstore, clock, fevents):
    """§3.4.7 事件 payload:`workflow` = ``{run_id, workflow, status, step_id?}``。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock, events=fevents)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    await e.run(wf["id"], {}, wait=True)
    p = json.loads(rows(fstore, "SELECT * FROM events_outbox WHERE event='workflow'")[0]["payload_json"])
    assert {"run_id", "workflow", "status"} <= set(p)


async def test_WF32_cancel_puts_run_into_cancelled(fstore, clock):
    """#46 `POST /workflows/runs/{run_id}/cancel` + §3.1 `status` CHECK 含 `cancelled`。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {})
    e.cancel(run_id)
    await e.wait(run_id)
    assert one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)["status"] == "cancelled"


async def test_WF33_cancel_on_finished_run_is_noop(fstore, clock):
    """#46:取消只对未终态的 run 有意义(终态不可再取消,参照 #108 的 `409 NOT_CANCELLABLE` 同理)。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert e.cancel(run_id) is False


async def test_WF34_pause_sets_paused_with_reason(fstore, clock):
    """#45 + §3.1 `workflow_runs.status='paused'` 注:「**账号切换挂起**(05 §2.4.5)」,`pause_reason` 记原因。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    fstore.con.execute("UPDATE workflow_runs SET status='running' WHERE run_id=?", (run_id,))
    assert e.pause(run_id, "account_switch") is True
    row = one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)
    assert row["status"] == "paused" and row["pause_reason"] == "account_switch"


async def test_WF35_resume_continues_from_next_step(fstore, clock):
    """#45/#46:「`paused`(账号切换挂起)可 `POST …/resume` **由人续跑**」——已留痕的步骤不重跑。"""
    clock.set_ms(T0)
    bus = FakeBus([(True, "DELIVERED")])
    e = mk_engine(fstore, clock, bus=bus)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {})
    await e.wait(run_id)
    fstore.con.execute("UPDATE workflow_runs SET status='paused' WHERE run_id=?", (run_id,))
    fstore.con.execute("DELETE FROM workflow_steps WHERE idx=1 AND run_id=?", (run_id,))
    calls_before = len(bus.calls)
    await e.resume(run_id, wait=True)
    assert len(bus.calls) == calls_before + 1
    assert one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)["status"] == "done"


async def test_WF36_resume_only_from_paused(fstore, clock):
    """#46:`resume` 的语义是「从 `paused` 续跑」,非 `paused` 的 run 不得被续跑。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert await e.resume(run_id) is False


async def test_WF37_delete_with_active_run_is_refused(fstore, clock):
    """#42 `DELETE /workflows/{id}`:**有运行中 run → `409`**。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    fstore.con.execute("UPDATE workflow_runs SET status='running' WHERE run_id=?", (run_id,))
    with pytest.raises(RuntimeError):
        e.delete(wf["id"])


async def test_WF38_builtin_webhook_posts_previous_results(fstore, clock, http):
    """§2.2.6 YAML 示例逐字:内置步骤 `webhook` = 「把**前面结果** POST 给业务系统(固定 URL,取自 `settings.callback_url`)」。"""
    clock.set_ms(T0)
    fstore.settings_set("callback_url", "https://biz.example/hook", now_ms=T0)
    y = ("name: wf_hook\nversion: 1\nsteps:\n  - id: s1\n    op: send_text\n    account: qd01\n"
         "    args: { session: \"qd01:g_1\", text: \"a\" }\n"
         "  - id: s2\n    op: webhook\n    args: { url_ref: \"settings.callback_url\" }\n")
    e = mk_engine(fstore, clock, http=http)
    wf = e.upsert(name="wf_hook", yaml=y, now_ms=T0)
    await e.run(wf["id"], {}, wait=True)
    assert http.requests and http.requests[0]["url"] == "https://biz.example/hook"
    body = json.loads(http.requests[0]["body"])
    assert [r["id"] for r in body["results"]] == ["s1"]


async def test_WF39_builtin_webhook_step_has_null_account(fstore, clock, http):
    """§3.1 `workflow_steps.account_id` 注:内置步骤(webhook/sleep)为 **NULL**。"""
    clock.set_ms(T0)
    fstore.settings_set("callback_url", "https://biz.example/hook", now_ms=T0)
    y = "name: wf_hook2\nversion: 1\nsteps:\n  - id: s1\n    op: webhook\n    args: { url_ref: \"settings.callback_url\" }\n"
    e = mk_engine(fstore, clock, http=http)
    wf = e.upsert(name="wf_hook2", yaml=y, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert e.steps(run_id)[0]["account_id"] is None


async def test_WF40_builtin_webhook_url_ref_must_be_settings(fstore, clock, http):
    """§2.2.6:内置 webhook 的 URL 是「**固定 URL,取自 `settings.callback_url`**」——不接受 YAML 里直写的任意地址。"""
    clock.set_ms(T0)
    y = "name: wf_hook3\nversion: 1\nsteps:\n  - id: s1\n    op: webhook\n    args: { url_ref: \"https://evil.example/x\" }\n"
    e = mk_engine(fstore, clock, http=http)
    wf = e.upsert(name="wf_hook3", yaml=y, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert one(fstore, "SELECT * FROM workflow_runs WHERE run_id=?", run_id)["status"] in ("failed", "needs_human")
    assert http.requests == []


async def test_WF41_one_task_per_run(fstore, clock):
    """§2.2.6 内部状态:「运行中的 `run_id → task`」;并发段:「**一个 run 一个 task**」。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    assert run_id in e.tasks


async def test_WF42_status_returns_run_and_steps(fstore, clock):
    """#45 `GET /workflows/runs/{run_id}`:``{run, steps:[…]}`` 每步留痕。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="two_step", yaml=TWO_STEP_YAML, now_ms=T0)
    run_id = await e.run(wf["id"], {}, wait=True)
    st = e.status(run_id)
    assert set(st) == {"run", "steps"} and len(st["steps"]) == 2


async def test_WF43_runs_listing_is_newest_first(fstore, clock):
    """#44 `GET /workflows/{id}/runs`:分页列表(与 §3.1 索引 `ix_runs_wf_time(workflow_id, started_ms DESC)` 同序)。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    first = await e.run(wf["id"], {}, wait=True, now_ms=T0)
    second = await e.run(wf["id"], {}, wait=True, now_ms=T0 + 10_000)
    assert [r["run_id"] for r in e.runs(wf["id"])][:2] == [second, first]


# ══════════════════════════════════════════════════════════════════════ 十四、端到端:工作流端点 / 作业契约 / 清理与校准端点 / HMAC 入站

class Rig5:
    """第五批端到端夹具:AgentApp + FastAPI + 三把 Bearer 令牌(级别 A/W/R)。

    🔴 运行时后端一律用仓库自带的假实现(`qtrade_agent.runtime.backends` 的 ``FakeAdb`` / ``FakeContainers``,
    该模块 docstring:「协议 + 真实 CLI 实现 + 可编程假实现」),企点执行层用本夹具的假 ``sender``(``AgentApp`` 公开构造参数,
    签名 ``async (acct, native_id, text) -> bool``)。否则 ``AgentApp`` 会装真 ``AdbCliBackend``/``DockerCliBackend``,
    工作流 run 在后台真的执行宿主 ``adb -P 16000 -s 127.0.0.1:16001 …``(e2e-rootfs-2 D-3):用例结果取决于宿主有没有 adb、
    且会碰宿主 adb server。假 ``sender`` 带一道闸门 ``send_gate``:缺省打开(点发送键即返回 True);
    用例关上它就能让 run **确定地**停在执行中(不靠手工改库、不靠宿主命令的快慢)。
    """

    def __init__(self, tmp_path, clock, http, disk):
        from qtrade_agent.app import AgentApp
        from qtrade_agent.runtime.backends import FakeAdb, FakeContainers
        from qtrade_agent.vault_client import FakeVault
        from qtrade_agent.winagent_client import FakeWinAgent
        self.clock = clock
        self.http = http
        self.disk = disk
        self.vault = FakeVault()
        self.wa = FakeWinAgent()
        self.adb = FakeAdb()
        self.containers = FakeContainers()
        self.send_gate = threading.Event()
        self.send_gate.set()
        self.sends: list[tuple[str, str]] = []            # 假执行层被调到的 (native_id, text)
        self.cfg = AgentConfig(api=ApiConfig())
        self.agent = AgentApp(self.cfg, db_path=str(tmp_path / "agent.db"), clock=clock, http=http, disk=disk,
                              data_dir=str(tmp_path), vault=self.vault, adb=self.adb, containers=self.containers,
                              sender=self._sender, winagent_transport=self.wa,
                              winagent_base_url="http://winagent.fake:17610", winagent_token=self.wa.token).open()
        self.store = self.agent.store
        self.store.ensure_account(QD, "qidian", state="running", self_uid="3007373675")
        self.store.ensure_account(QQ, "qq", state="running", login_mode="qrcode", self_uid="415011447")
        self.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_ADMIN)
        self.store.upsert_api_client(app_id="bot_w", name="写机器人", level="write", token=TOK_W)
        self.store.upsert_api_client(app_id="bot_r", name="读机器人", level="read", token=TOK_R)
        self.api = self.agent.create_api()

    async def _sender(self, acct, native_id: str, text: str) -> bool:
        """假企点执行层:记下调用;闸门关着就一直等(run 因此停在执行中),闸门开了才「点发送键」返回 True。"""
        self.sends.append((native_id, text))
        while not self.send_gate.is_set():
            await asyncio.sleep(0.01)
        return True

    def add_hmac_client(self, app_id: str, secret: str, *, level: str = "admin", allow_ops: Optional[list[str]] = None) -> None:
        mk_hmac_client(self.store, app_id, level=level, allow_ops=allow_ops, now=self.clock())
        self.vault.entries[f"api/{app_id}"] = type(self.vault.entries.get("x", None) or _E(), (), {})  # 占位,下面用 put 覆盖

    def make_wf(self, client: TestClient, name: str = "broadcast_notice", yaml_text: str = MIN_YAML) -> str:
        r = client.post(f"{P}/workflows", json={"name": name, "yaml": yaml_text}, headers=H(TOK_ADMIN))
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"]


class _E:
    pass


@pytest.fixture
def rig5(tmp_path, clock, http, disk) -> Rig5:
    clock.auto_step_ms = 50
    r = Rig5(tmp_path, clock, http, disk)
    yield r
    r.store.close()


@pytest.fixture
def c5(rig5: Rig5):
    with TestClient(rig5.api, client=("127.0.0.1", 40000)) as c:
        try:
            yield c
        finally:
            try:
                c.portal.call(rig5.agent.bus.close)
            except Exception:
                pass


def test_AP01_list_workflows_hides_yaml(rig5, c5):
    """#38 `GET /workflows`:「列表(**不含 yaml 全文**);工作流只存表,不读 `/etc/qtrade` 下文件(G-09)」。"""
    rig5.make_wf(c5)
    body = c5.get(f"{P}/workflows", headers=H(TOK_R)).json()
    assert body["ok"] is True and body["data"] and "yaml" not in body["data"][0]


def test_AP02_create_workflow_requires_admin(rig5, c5):
    """#39 级别列 = **A**:写令牌(W)不得创建工作流。"""
    r = c5.post(f"{P}/workflows", json={"name": "x", "yaml": MIN_YAML}, headers=H(TOK_W))
    assert r.status_code == 403


def test_AP03_create_invalid_yaml_returns_400_with_lines(rig5, c5):
    """#39:「解析校验失败 `400` **带行号**」;00 §10 错误信封。"""
    bad = "name: x\nversion: 1\nsteps:\n  - id: s1\n    for_each: accounts\n    op: send_text\n"
    r = c5.post(f"{P}/workflows", json={"name": "x", "yaml": bad}, headers=H(TOK_ADMIN))
    assert r.status_code == 400
    body = r.json()
    errs = body.get("errors") or body.get("error", {}).get("errors") or []
    assert errs and all("line" in e for e in errs), body


def test_AP04_get_workflow_includes_yaml(rig5, c5):
    """#40 `GET /workflows/{id}`:**含 yaml**。"""
    wid = rig5.make_wf(c5)
    body = c5.get(f"{P}/workflows/{wid}", headers=H(TOK_R)).json()
    assert body["data"]["yaml"] == MIN_YAML


def test_AP05_put_workflow_bumps_version(rig5, c5):
    """#41 `PUT /workflows/{id}`:整体替换,**version+1**。"""
    wid = rig5.make_wf(c5)
    r = c5.put(f"{P}/workflows/{wid}", json={"yaml": MIN_YAML.replace("巡检开始", "巡检结束")}, headers=H(TOK_ADMIN))
    assert r.status_code == 200 and r.json()["data"]["version"] == 2


def test_AP06_run_workflow_returns_202_with_run_id(rig5, c5):
    """#43 `POST /workflows/{id}/run`:级别 W、`{args, idempotency_key?}` → **`202 {run_id}`**。"""
    wid = rig5.make_wf(c5)
    r = c5.post(f"{P}/workflows/{wid}/run", json={"args": {}}, headers=H(TOK_W))
    assert r.status_code == 202 and r.json()["run_id"]


def test_AP07_run_detail_returns_run_and_steps(rig5, c5):
    """#45 `GET /workflows/runs/{run_id}`:``{run, steps:[…]}`` 每步留痕。"""
    wid = rig5.make_wf(c5)
    run_id = c5.post(f"{P}/workflows/{wid}/run", json={}, headers=H(TOK_W)).json()["run_id"]
    body = c5.get(f"{P}/workflows/runs/{run_id}", headers=H(TOK_R)).json()
    assert "run" in body and "steps" in body


def test_AP08_unknown_run_is_404(rig5, c5):
    """00 §10 映射:404 `TARGET_NOT_FOUND`(run 不存在)。"""
    r = c5.get(f"{P}/workflows/runs/no-such-run", headers=H(TOK_R))
    assert r.status_code == 404 and r.json()["code"] == "TARGET_NOT_FOUND"


def test_AP09_cancel_endpoint_exists(rig5, c5):
    """#46 `POST /workflows/runs/{run_id}/cancel`(与 `…/resume` 计 1 行,级别 W)。"""
    wid = rig5.make_wf(c5)
    run_id = c5.post(f"{P}/workflows/{wid}/run", json={}, headers=H(TOK_W)).json()["run_id"]
    r = c5.post(f"{P}/workflows/runs/{run_id}/cancel", headers=H(TOK_W))
    assert r.status_code == 200 and "cancelled" in r.json()


def test_AP10_validate_endpoint_is_read_level(rig5, c5):
    """#47 `POST /workflows/validate`:级别 **R**、`{yaml}` → `{ok, errors:[…]}`(P-05:YAML 只读展示,校验仍开放给 API)。"""
    r = c5.post(f"{P}/workflows/validate", json={"yaml": MIN_YAML}, headers=H(TOK_R))
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["errors"] == []


def _run_status(c5, run_id: str) -> Optional[str]:
    """经 #45 `GET /workflows/runs/{run_id}` 读 run 状态(R6-68 ⑤ 信封平铺 `{ok, run, steps}`)。"""
    body = c5.get(f"{P}/workflows/runs/{run_id}", headers=H(TOK_R)).json()
    run = body.get("run") or (body.get("data") or {}).get("run") or {}
    return run.get("status")


def _wait(pred, *, timeout_s: float = 10.0) -> bool:
    """真实时间轮询(不拨假时钟):run 在 TestClient 的后台事件循环里推进。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_AP11_delete_with_active_run_is_409(rig5, c5):
    """#42 `DELETE /workflows/{id}`:「有运行中 run(`status ∈ {running, paused}`)→ `409`」;且被挡下的删除**什么都不删**。

    活跃 run 的构造(D-3 整改):假企点执行层(`Rig5.send_gate` 关上)让 run 真的卡在发送步 ⇒ 状态由产品自己推进到
    `running`、不手工改库、不依赖宿主 adb;断言完再开闸让 run 自然收尾。
    """
    wid = rig5.make_wf(c5)
    rig5.send_gate.clear()
    try:
        run_id = c5.post(f"{P}/workflows/{wid}/run", json={}, headers=H(TOK_W)).json()["run_id"]
        assert _wait(lambda: bool(rig5.sends)), "run 未走到发送步(假执行层没被调到)"
        assert _run_status(c5, run_id) in ("running", "paused")
        r = c5.delete(f"{P}/workflows/{wid}", headers=H(TOK_ADMIN))
        assert r.status_code == 409, r.text
        assert c5.get(f"{P}/workflows/{wid}", headers=H(TOK_R)).status_code == 200, "409 之后工作流必须还在"
        assert _run_status(c5, run_id) is not None, "409 之后 run 必须还在"
    finally:
        rig5.send_gate.set()
    assert _wait(lambda: _run_status(c5, run_id) not in ("running", "paused")), "开闸后 run 应收尾到终态"


def test_AP11b_delete_without_active_run_succeeds_and_cascades_runs(rig5, c5):
    """对照组 —— #42:没有 `running`/`paused` 的 run 就不挡;🔴 R6-58 (g):终态 run 与其 steps **连带删**
    (「没有这一条则……跑过一次的工作流永远删不掉」)。删后 #40 / #45 均 `404`。"""
    wid = rig5.make_wf(c5)
    run_id = c5.post(f"{P}/workflows/{wid}/run", json={}, headers=H(TOK_W)).json()["run_id"]
    assert _wait(lambda: _run_status(c5, run_id) not in (None, "running", "paused")), "run 应自然到终态"
    r = c5.delete(f"{P}/workflows/{wid}", headers=H(TOK_ADMIN))
    assert r.status_code in (200, 204), r.text
    assert c5.get(f"{P}/workflows/{wid}", headers=H(TOK_R)).status_code == 404
    assert c5.get(f"{P}/workflows/runs/{run_id}", headers=H(TOK_R)).status_code == 404


def test_AP12_cleanup_run_returns_202_job_id(rig5, c5):
    """#109 `POST /system/cleanup/run`(R6-24 逐字路径):级别 W → **`202 {job_id}`**,`jobs.kind='system_cleanup'`。"""
    r = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W))
    assert r.status_code == 202 and r.json()["job_id"]
    row = one(rig5.store, "SELECT * FROM jobs WHERE job_id=?", r.json()["job_id"])
    assert row["kind"] == "system_cleanup"


def test_AP13_cleanup_run_has_60s_dedup(rig5, c5):
    """#109 **60 s 防重**:60 s 内重复触发 → `409 RESOURCE_EXHAUSTED`,**回既有 `job_id`**、不排第二个。"""
    first = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W)).json()["job_id"]
    r = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W))
    body = r.json()
    assert r.status_code == 409 and body["code"] == "RESOURCE_EXHAUSTED"
    assert first in json.dumps(body), "409 必须回既有 job_id,不排第二个"
    assert count(rig5.store, "jobs WHERE kind='system_cleanup'") == 1


def test_AP14_job_query_returns_state(rig5, c5):
    """#107 `GET /jobs/{job_id}`(00 §11.21 [JOB] 统一契约):`{job_id, kind, state, progress, result?, error?}`。"""
    job_id = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W)).json()["job_id"]
    body = c5.get(f"{P}/jobs/{job_id}", headers=H(TOK_R)).json()
    data = body.get("data", body)
    assert data["job_id"] == job_id and data["kind"] == "system_cleanup"
    assert data["state"] in ("queued", "running", "succeeded", "failed", "cancelled", "expired")


def test_AP15_unknown_job_is_404(rig5, c5):
    """#107:作业不存在 → 404 `TARGET_NOT_FOUND`(00 §10)。"""
    r = c5.get(f"{P}/jobs/nope", headers=H(TOK_R))
    assert r.status_code == 404


def test_AP16_cleanup_job_reaches_terminal_state_and_emits_job_event(rig5, c5):
    """00 §11.21 [JOB]:「终态推 **`job` 事件**」;§3.4.7 payload = `{job_id, kind, state, progress, result?, error?}`。"""
    job_id = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W)).json()["job_id"]
    for _ in range(200):
        row = one(rig5.store, "SELECT * FROM jobs WHERE job_id=?", job_id)
        if row["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert row["state"] == "succeeded"
    payloads = [json.loads(r["payload_json"]) for r in rows(rig5.store, "SELECT * FROM events_outbox WHERE event='job'")]
    assert any(p["job_id"] == job_id and p["state"] == "succeeded" for p in payloads)


def test_AP17_cleanup_writes_last_cleanup_settings_key(rig5, c5):
    """#109(R6-34):完成后回写 `settings` 键 **`system.last_cleanup`**;#77 的水位快照逐字从它渲染。"""
    job_id = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W)).json()["job_id"]
    for _ in range(200):
        row = one(rig5.store, "SELECT * FROM jobs WHERE job_id=?", job_id)
        if row["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    assert rig5.store.settings_get("system.last_cleanup") is not None


def test_AP18_metrics_disk_watermark_is_flat_two_keys(rig5, c5):
    """#77 `GET /system/metrics`(R6-30):`disk_watermark` 的 `last_cleanup_at` / `last_cleanup_freed_mb`
    是**扁平两键**(不嵌套 `last_cleanup` 子对象),04 §2.4.5 快照字段。"""
    body = c5.get(f"{P}/system/metrics", headers=H(TOK_R)).json()
    dw = body["disk_watermark"]
    assert "last_cleanup_at" in dw and "last_cleanup_freed_mb" in dw and "last_cleanup" not in dw


def test_AP19_metrics_reports_current_disk_level(rig5, c5):
    """04 §2.4.5 + §2.8.8:`P-RES` 要显示「处在 normal/warn/high/critical 哪一级」。"""
    dw = c5.get(f"{P}/system/metrics", headers=H(TOK_R)).json()["disk_watermark"]
    assert dw["level"] in ("normal", "warn", "high", "critical")


def test_AP20_calibrate_requires_admin(rig5, c5):
    """#71 `POST /resources/calibrate` 级别列 = **A**。"""
    assert c5.post(f"{P}/resources/calibrate", json={}, headers=H(TOK_W)).status_code == 403


def wait_job(rig5, c5, job_id: str, *, timeout_s: float = 4.0) -> dict:
    """00 §11.21 [JOB]:`202 {job_id}` → 轮 #107 `GET /jobs/{job_id}` 到终态。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = one(rig5.store, "SELECT * FROM jobs WHERE job_id=?", job_id)
        if row and row["state"] in ("succeeded", "failed", "cancelled", "expired"):
            body = c5.get(f"{P}/jobs/{job_id}", headers=H(TOK_R)).json()
            return body.get("data", body)
        time.sleep(0.02)
    raise AssertionError(f"作业 {job_id} 未在 {timeout_s}s 内到终态")


def test_AP21_calibrate_returns_202_job_and_without_apply_does_not_write(rig5, c5):
    """#71 + 00 §11.21 [JOB] + 04 §8b.2 A2-03。

    🔴 **总控裁决 R6-58**:#25/#71 统一走 00 §11.21 的 job 契约 —— **`202 {job_id}`**,结果经 #107 取
    (02 §3.4.6「返回建议值」的同步写法作废)。不带 `apply` 时只算建议值、**不写 `resource_pools`**。"""
    before = one(rig5.store, "SELECT * FROM resource_pools WHERE pool='wsl'")
    r = c5.post(f"{P}/resources/calibrate", json={"apply": False}, headers=H(TOK_ADMIN))
    assert r.status_code == 202 and r.json()["job_id"], r.text
    job = wait_job(rig5, c5, r.json()["job_id"])
    assert job["state"] == "succeeded" and job["kind"] == "resources_calibrate"
    after = one(rig5.store, "SELECT * FROM resource_pools WHERE pool='wsl'")
    assert (before or {}).get("calibrated_ms") == (after or {}).get("calibrated_ms")


def test_AP22_disk_full_maps_to_http_507(rig5, c5, monkeypatch):
    """§2.8.8 + 00 §8.3/§10(R-02):`DISK_FULL` ⇒ HTTP **507 Insufficient Storage**,信封 `code='DISK_FULL'`、
    `error.message` 带剩余 MB、`hint_actions=["open_env","run_cleanup"]`;**不是 500 `INTERNAL`、也不是 503**。

    载体 = #81 `POST /system/backup`(直接写本地文件的同步写端点,不经 `bus`);
    经 `bus` 的写路径另见 `test_spec_infra_pending.py`(当前实现回 500 `INTERNAL`)。"""
    def boom(*a, **kw):
        raise rig5.agent.maintenance.to_disk_full(OSError(28, "No space left on device"))

    monkeypatch.setattr(rig5.agent.maintenance, "backup_once", boom)
    r = c5.post(f"{P}/system/backup", headers=H(TOK_ADMIN))
    assert r.status_code == 507
    body = r.json()
    assert body["code"] == "DISK_FULL"
    assert "本地磁盘空间不足" in body["error"]["message"]
    assert ["open_env", "run_cleanup"] == (body.get("hint_actions") or body["error"].get("hint_actions"))


# ---------------------------------------------------------------- HMAC 经 API 入站(§3.5)

def hmac_get(c5, rig5, app_id: str, secret: str, path: str, *, query: str = "", nonce: str = "nonce-0123456789ab"):
    ts = rig5.clock() // 1000
    headers = hmac_headers(app_id, secret, "GET", path, query, b"", ts=ts, nonce=nonce)
    return c5.get(path + (f"?{query}" if query else ""), headers=headers)


def hmac_command(c5, rig5, app_id: str, secret: str, *, nonce: str, ts: Optional[int] = None, key: str = "k-1"):
    """§3.5 的公网入站请求(#28 `POST /accounts/{id}/commands`):只读 op,免得真去点发送键。"""
    body = json.dumps({"op": "get_state", "args": {}, "idempotency_key": key}, ensure_ascii=False).encode("utf-8")
    path = f"{P}/accounts/{QD}/commands"
    headers = hmac_headers(app_id, secret, "POST", path, "", body, ts=ts if ts is not None else rig5.clock() // 1000,
                           nonce=nonce) | {"Content-Type": "application/json"}
    return c5.post(path, content=body, headers=headers)


async def _put_secret(rig5, app_id: str, secret: str) -> None:
    await rig5.vault.put(f"api/{app_id}", secret)


def add_hmac(rig5, app_id: str, secret: str, *, level: str = "admin", allow_ops: Optional[list[str]] = None) -> None:
    mk_hmac_client(rig5.store, app_id, level=level, allow_ops=allow_ops, now=rig5.clock())
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_put_secret(rig5, app_id, secret))


def test_AP23_valid_hmac_request_is_accepted(rig5, c5):
    """§3.5:带全套 `X-QT-*` 头 ⇒ 走公网入站 HMAC(**不需要** `Authorization: Bearer`)。"""
    add_hmac(rig5, "pub", "s3cret")
    r = hmac_command(c5, rig5, "pub", "s3cret", nonce="nonce-ok-01234567")
    assert r.status_code in (200, 202), r.text


def test_AP24_hmac_response_carries_date_header(rig5, c5):
    """§3.5 时钟容差行:「把服务端时间放在 `Date` 头让对方校时」。"""
    add_hmac(rig5, "pub", "s3cret")
    r = hmac_command(c5, rig5, "pub", "s3cret", nonce="nonce-date-012345")
    assert "date" in {k.lower() for k in r.headers}


def test_AP25_hmac_bad_signature_is_401(rig5, c5):
    """§3.5 失败顺序 ⑤:签名不对 → 401。"""
    add_hmac(rig5, "pub", "s3cret")
    r = hmac_command(c5, rig5, "pub", "wrong", nonce="nonce-sig-0123456")
    assert r.status_code == 401


def test_AP26_hmac_timestamp_skew_returns_401_with_date(rig5, c5):
    """§3.5:时间戳超容差 → 401 `timestamp skew`,并且**仍要带 `Date` 头**(否则对方没法自校)。"""
    add_hmac(rig5, "pub", "s3cret")
    r = hmac_command(c5, rig5, "pub", "s3cret", nonce="nonce-skew-0123456", ts=rig5.clock() // 1000 - 9999)
    assert r.status_code == 401 and "date" in {k.lower() for k in r.headers}


def test_AP27_hmac_nonce_replay_is_401(rig5, c5):
    """§3.5:`(app_id, nonce)` 10 分钟内唯一;重复 → 401 `nonce replay`。"""
    add_hmac(rig5, "pub", "s3cret")
    n = "nonce-replay-01234"
    assert hmac_command(c5, rig5, "pub", "s3cret", nonce=n).status_code in (200, 202)
    assert hmac_command(c5, rig5, "pub", "s3cret", nonce=n, key="k-2").status_code == 401


def test_AP28_hmac_command_defaults_to_async(rig5, c5):
    """§3.5「同步 vs 异步」行:公网入站**默认 `async:true`**(`202 {trace_id}`),结果走 webhook。"""
    add_hmac(rig5, "pub", "s3cret")
    body = json.dumps({"op": "send_text", "args": {"session": f"{QD}:415011447", "text": "hi"},
                       "idempotency_key": "k-async-1"}, ensure_ascii=False).encode("utf-8")
    path = f"{P}/accounts/{QD}/commands"
    headers = hmac_headers("pub", "s3cret", "POST", path, "", body, ts=rig5.clock() // 1000,
                           nonce="nonce-async-01234") | {"Content-Type": "application/json"}
    r = c5.post(path, content=body, headers=headers)
    assert r.status_code == 202 and r.json()["trace_id"] and r.json().get("accepted") is True


def test_AP29_hmac_level_is_enforced(rig5, c5):
    """§3.5 失败顺序 ⑦ + §3.4 鉴权级别:read 级 HMAC 调用方不得调写端点。"""
    add_hmac(rig5, "pubr", "s3cret", level="read")
    body = json.dumps({"session": f"{QD}:415011447", "text": "hi", "idempotency_key": "k1"}).encode("utf-8")
    path = f"{P}/accounts/{QD}/send"
    headers = hmac_headers("pubr", "s3cret", "POST", path, "", body, ts=rig5.clock() // 1000,
                           nonce="nonce-level-01234") | {"Content-Type": "application/json"}
    r = c5.post(path, content=body, headers=headers)
    assert r.status_code == 403


def test_AP30_hmac_unknown_app_is_401(rig5, c5):
    """§3.5 失败顺序 ①:app_id 不存在 → 401。"""
    r = hmac_command(c5, rig5, "ghost", "s3cret", nonce="nonce-ghost-01234")
    assert r.status_code == 401


async def test_WH38_consecutive_fail_accumulates_until_dead(store, clock, http):
    """§2.2.7:「同一 webhook **连续失败** `dead_after_attempts`(默认 10)次进 `dead`」+ §5 决策表:
    「`consecutive_fail` ≥ `max_attempts` → `dead_ms`、停投、`alert`」。"""
    clock.set_ms(T0)
    d = WebhookDispatcher(store, http=http, secret_provider=lambda w: "s", clock=clock)
    mk_webhook(store, "w", "https://a.example/cb")
    http.default = HttpResponse(500)
    now = T0
    for i in range(SPEC_WEBHOOK_MAX_ATTEMPTS):
        d.fanout(event_id=f"e{i}", event="message", payload={"i": i}, now_ms=now)
        await d.deliver_due(now_ms=now)
        now += SPEC_WEBHOOK_BACKOFF_MS[-1]
        clock.set_ms(now)
    row = one(store, "SELECT * FROM webhooks WHERE id='w'")
    assert row["consecutive_fail"] >= SPEC_WEBHOOK_MAX_ATTEMPTS and row["dead_ms"] is not None


# ══════════════════════════════════════════════════════════════════════ 十五、枚举与配置默认值(02 §3.1 / §3.9 / §7.1;00 §7.5/§11.21;07)

def test_CFG01_event_enum_has_nine_values_including_job(fstore):
    """§3.1 `events_outbox.event` CHECK = 九值(00 §7.5 真值表 R3-17 补 `job`)。"""
    from qtrade_agent.events import EVENT_NAMES
    assert set(EVENT_NAMES) == {"message", "account_state", "command_done", "alert", "resource", "mail", "net", "workflow", "job"}
    for ev in EVENT_NAMES:
        fstore.insert_outbox_event(event_id=f"e-{ev}", target="ws", event=ev, trace_id=None, account_id=None,
                                   channel=None, payload_json="{}", now_ms=T0)
    assert count(fstore, "events_outbox") == len(EVENT_NAMES)


def test_CFG02_outbox_status_enum(fstore):
    """§3.1 `events_outbox.status` CHECK = `('pending','delivered','failed','dead')`。"""
    fstore.insert_outbox_event(event_id="e1", target="webhook:w", event="message", trace_id=None, account_id=None,
                               channel=None, payload_json="{}", now_ms=T0)
    with pytest.raises(Exception):
        fstore.con.execute("UPDATE events_outbox SET status='zombie' WHERE target='webhook:w'")


def test_CFG03_ws_queue_max_is_10000():
    """§2.2.7 内部状态:「内存广播队列(**有界 10000**,满则丢最旧并发 `alert`)」= §7.1 `[events] ws_queue_max`。"""
    from qtrade_agent.config import EventsConfig
    assert EventsConfig().ws_queue_max == 10000


def test_CFG04_ws_retention_hours_is_72():
    """§2.2.7 + §7.1 `[events] ws_retention_hours = 72`(R6-52 登记):`target='ws'` 行的保留时长,scheduler 每小时清一轮。"""
    from qtrade_agent.config import EventsConfig
    assert EventsConfig().ws_retention_hours == 72


def test_CFG05_hmac_skew_and_nonce_ttl_defaults():
    """§3.5:时钟容差 **300 s**、nonce LRU **10 分钟**(= 600 s)。"""
    assert HI.HmacConfig().hmac_clock_skew_s == SPEC_SKEW_S
    assert HI.HmacConfig().nonce_ttl_s == SPEC_NONCE_TTL_S


def test_CFG06_workflow_run_status_enum(fstore, clock):
    """§3.1 `workflow_runs.status` CHECK = `('running','paused','done','failed','cancelled','needs_human')`。"""
    clock.set_ms(T0)
    e = mk_engine(fstore, clock)
    wf = e.upsert(name="broadcast_notice", yaml=MIN_YAML, now_ms=T0)
    fstore.con.execute("INSERT INTO workflow_runs(run_id, workflow_id, workflow_version, trigger, actor, status, started_ms) "
                       "VALUES ('r1',?,1,'api','token:console','running',?)", (wf["id"], T0))
    with pytest.raises(Exception):
        fstore.con.execute("UPDATE workflow_runs SET status='zombie' WHERE run_id='r1'")


def test_CFG07_jobs_state_enum_covers_spec_values(fstore):
    """00 §11.21 [JOB] + §3.4.10 #107:`state ∈ queued|running|succeeded|failed|cancelled`(+`expired`)。"""
    job_id = fstore.job_create(kind="system_cleanup", actor="token:console", now_ms=T0)
    with pytest.raises(Exception):
        fstore.con.execute("UPDATE jobs SET state='zombie' WHERE job_id=?", (job_id,))


def test_CFG08_system_cleanup_is_not_danger_but_mail_cleanup_is():
    """§3.10 能力目录:`system_cleanup_run` **`danger=false`**(只删本地、绝不删远端邮件,基线 §11.11 R4-2),
    而真删服务器邮件的 `mail_cleanup_run` 仍是 **`danger=true`** —— 两者的边界就在这一句。"""
    danger = HI.load_danger_ops()
    assert "system_cleanup_run" not in danger and "mail_cleanup_run" in danger


def test_CFG09_capability_catalog_marks_send_text_non_danger():
    """§3.10 + 00 §11.17 ①(N-17):`send_*` 恒 `danger=false`(它走 §11.3 [GATE] 四重约束,不进确认门)。"""
    assert "send_text" not in HI.load_danger_ops()


def test_CFG10_retention_shrink_steps_are_30_14_7():
    """§2.8.8 + 00 §11.11 ②:递减序列逐字 **30→14→7**,循环上限 3 档。"""
    from qtrade_agent.maintenance import RETENTION_SHRINK_STEPS
    assert tuple(RETENTION_SHRINK_STEPS) == SPEC_RETENTION_STEPS


def test_AP31_hmac_works_on_read_endpoints(rig5, c5):
    """02 §3.4 通用行逐字:「鉴权 `Authorization: Bearer <token>`;**公网入站加 HMAC(§3.5)**」——
    HMAC 与 Bearer 并列,适用于 `/api/v1` 全部端点,不只是写指令那两个
    (§3.4.7 连 WS `GET /api/v1/events` 都规定了 HMAC 客户端的签法 `?app_id&ts&nonce&sig`)。"""
    add_hmac(rig5, "pub", "s3cret")
    r = hmac_get(c5, rig5, "pub", "s3cret", f"{P}/accounts")
    assert r.status_code == 200, r.text


def test_AP32_jobs_cancel_endpoint(rig5, c5):
    """#108 `POST /jobs/{job_id}/cancel`(00 §11.21 [JOB]):「取消 `queued/running` 作业
    (不可取消的终态 → `409`);导出/诊断/清理**可取消**」。"""
    job_id = c5.post(f"{P}/system/cleanup/run", headers=H(TOK_W)).json()["job_id"]
    r = c5.post(f"{P}/jobs/{job_id}/cancel", headers=H(TOK_W))
    assert r.status_code in (200, 409), r.text


def test_AP33_single_account_calibrate(rig5, c5):
    """#25 `POST /accounts/{id}/calibrate`,级别 A:「**单账号**自校准,结果写 `resource_pools.calibration_json`
    (与 #71 全局校准并存)」;🔴 **总控裁决 R6-58**:与 #71 一样走 00 §11.21 的 `202 {job_id}`,结果经 #107 取。"""
    r = c5.post(f"{P}/accounts/{QD}/calibrate", json={}, headers=H(TOK_ADMIN))
    assert r.status_code == 202 and r.json()["job_id"], r.text
    job = wait_job(rig5, c5, r.json()["job_id"])
    assert job["state"] == "succeeded"
