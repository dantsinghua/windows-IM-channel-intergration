"""按设计文档写的验收用例 —— 第二批:API 骨架 + WS 事件流 + scheduler。

🔴 断言只依据规格,不按实现反推;规格出处(册 §节 / 端点编号 / 裁决号 / 验收行):
- 00 §6(时间 = ISO 8601 带 +08:00;trace_id = ULID)、§7.1 Account 信封、§7.2 Command、§7.3 CommandResult、§7.4 Message(API 视图;
  事件专属 lag_s/late/origin **不**在 GET /messages,R6-49)、§7.5 Event 帧结构、§8.3 结果码表、§10 API 基线(前缀 / Bearer / 错误信封 / HTTP 映射 /
  /system/health 对 loopback 免鉴权只回布尔摘要)、§15g R6-4 / R6-48 / R6-51 / R6-52。
- 02 §2.2.1 api(每次调用记 audit_log)、§2.2.2 bus(登录门 / 幂等三态 / args_json 不存正文)、§2.2.4 端口推导(C-07)、§2.2.7 events、
  §2.2.11 scheduler(同名任务不重入:上一轮没跑完就跳过本轮并计数;register/trigger)、§2.8.6 检索(≥3 字 FTS、2 字 LIKE、多词 AND、ts_ms DESC、
  有 LIKE 词即 slow_match)、§3.1 api_clients / audit_log DDL、§3.4 通用(C-42 分页参数、G-16 cursor)、§3.4.1 #1/#3 + Account 序列化表、
  #21 GET /capabilities、§3.4.2 #26/#28/#29/#30/#31、§3.4.4 #48/#49、§3.4.6 #72/#73/#95、§3.4.7 WS /events、§3.8 版本协商、§3.10 能力目录、
  §7.1 [api]/[events] 配置默认值、§8b B-06/B-07/B-25/B-30/B-40。
- 01 §2.8 首段(客户端可重发订阅帧收窄过滤;since_seq 重放、按 seq 不按 ts)。

每个用例顶部注释写清条款;断言的是规格说的可观测结果(HTTP 状态、信封键、响应头、表里几行、事件几帧、计数)。
夹具来自 tests/conftest.py:``clock``(可拨时钟)、``maindb``(假企点主库);本文件自建 ``rig``(AgentApp + FastAPI + 令牌 + 三通道账号)。
"""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, ApiConfig, BusConfig, QidianAdapterConfig
from qtrade_agent.models import Message, Session
from qtrade_agent.scheduler import Scheduler

# ────────────────────────────────────────────────────────────────────── 常量(规格自抄)

P = "/api/v1"                    # 00 §10 前缀
QD = "qd01"
SELF_UID = "3007373675"          # = FakeMainDb 默认 self_uin
PEER = "415011447"               # 企点单聊对端 uin(00 §6 / R6-21:单聊 native_id = <对端uin>)
QD_SESSION = f"{QD}:{PEER}"
QQ = "qq03"
QQ_GROUP = "g_456"
QQ_SESSION = f"{QQ}:{QQ_GROUP}"
WX = "wx01"
BASE_MS = 1_758_240_000_000      # conftest Clock 的起始时刻(ms)

TOK_ADMIN = "tok-console-admin"  # app_id=console → actor 'token:console'(00 §7.2)
TOK_W = "tok-bot-write"          # app_id=bot_w, level=write, allow_accounts=["*"]
TOK_R = "tok-bot-read"           # app_id=bot_r, level=read
TOK_LIM = "tok-bot-limited"      # app_id=bot_lim, level=write, allow_accounts=["qd01"]

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")     # 00 §6:2026-09-18T10:03:00+08:00
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")                       # 00 §6:trace_id = ULID
TZ8 = timezone(timedelta(hours=8))

# 00 §7.3 CommandResult 顶层键
RESULT_KEYS = {"ok", "code", "data", "cost_ms", "trace_id", "source", "state_before", "state_after"}
# 00 §7.1 Account 信封顶层键(R6-4 加 error_since_ms;R-23 wxid/merged_into 由 02 §3.4.1 序列化表带出)
ACCOUNT_KEYS = {"id", "channel", "label", "host", "state", "state_code", "state_reason", "error_since_ms", "enabled", "auto_recover",
                "deleted_ms", "runtime", "identity", "login", "capabilities", "quota_mb", "self_nick", "self_uid", "created_at", "updated_at",
                "last_seen_at"}
# 00 §7.4 Message(API 视图)顶层键
MESSAGE_KEYS = {"id", "ext_msg_id", "account_id", "channel", "session", "dir", "type", "state", "text", "text_len", "fingerprint", "media",
                "sender", "self", "ts", "received_at", "source", "revoked", "raw_ref"}
EVENT_ONLY_KEYS = {"lag_s", "late", "origin"}                            # 00 §7.4 / R6-49:只在事件里、GET /messages 不带
# 00 §7.5 Event 帧
EVENT_FRAME_KEYS = {"event", "ts", "trace_id", "account_id", "channel", "seq", "payload"}
# 00 §10 错误信封
ERROR_INNER_KEYS = {"message", "retryable", "needs_human"}


def spec_iso(ms: int) -> str:
    """00 §6:ISO 8601 带时区偏移,时区固定 +08:00。"""
    return datetime.fromtimestamp(ms / 1000, tz=TZ8).isoformat(timespec="seconds")


def iso_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def H(tok: Optional[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def count(store, sql, *params) -> int:
    return store.con.execute(sql, params).fetchone()[0]


def rows(store, sql, *params):
    return [tuple(r) for r in store.con.execute(sql, params).fetchall()]


def wait_until(pred: Callable[[], bool], timeout: float = 20.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def assert_error_envelope(body: dict, code: str) -> None:
    """00 §10:`{ok:false, code, error:{message, reason?, retryable, needs_human}, trace_id}` 逐键。"""
    assert body["ok"] is False
    assert body["code"] == code
    err = body["error"]
    assert ERROR_INNER_KEYS <= set(err), err
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["retryable"], bool)
    assert isinstance(err["needs_human"], bool)
    if "reason" in err and err["reason"] is not None:
        assert isinstance(err["reason"], str)
    assert "trace_id" in body


def assert_version_headers(resp, api_version: str = "1.0") -> None:
    """02 §3.8:每个响应带 `X-QT-Api-Version`(主.次)与 `X-QT-Agent-Version`。"""
    assert resp.headers.get("X-QT-Api-Version") == api_version
    assert re.fullmatch(r"\d+\.\d+", resp.headers["X-QT-Api-Version"])
    assert resp.headers.get("X-QT-Agent-Version"), "缺 X-QT-Agent-Version"


def ws_recv(ws, timeout: float = 3.0) -> Any:
    """带超时的 receive_json(WebSocketTestSession.receive 走 portal.call,可在辅助线程调用;超时即判帧未到,不让用例挂死)。"""
    q: queue.Queue = queue.Queue()

    def run():
        try:
            q.put(("ok", ws.receive_json()))
        except BaseException as e:      # noqa: BLE001
            q.put(("err", e))

    threading.Thread(target=run, daemon=True).start()
    try:
        kind, val = q.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"{timeout}s 内未收到 WS 帧")
    if kind == "err":
        raise val
    return val


def emit(client: TestClient, agent: AgentApp, event: str, **kw) -> int:
    """在 TestClient 的事件循环线程里 emit(events.emit 同步、非阻塞;返回 outbox seq)。"""
    return client.portal.call(functools.partial(agent.events.emit, event, **kw))


# ────────────────────────────────────────────────────────────────────── 装配夹具


class Rig:
    def __init__(self, tmp_path, clock, maindb, *, api_cfg: Optional[ApiConfig] = None, land_delay_s: float = 0.0):
        self.clock = clock
        self.maindb = maindb
        self.sent: list[tuple[str, str]] = []           # 通道里实际出现的消息(点了几次发送键)
        self.land_delay_s = land_delay_s                # 假企点把我方消息落进主库前的真实等待(模拟 7~12 s 出向落库滞后)
        self.cfg = AgentConfig(bus=BusConfig(send_min_interval_ms=0, send_rand_extra_ms=0),
                               qidian=QidianAdapterConfig(confirm_poll_interval_ms=10), api=api_cfg or ApiConfig())
        # 🔴 运行时后端 / WinAgent 一律假实现(e2e-rootfs-2 D-3 同型整改):不注入则 AgentApp 装真 AdbCliBackend/DockerCliBackend、
        # 真 WinAgent HTTP 客户端与真 QidianUi 登录执行层,任何走到它们的端点都会碰宿主 adb/docker/网络。
        from qtrade_agent.runtime.backends import FakeAdb, FakeContainers
        from qtrade_agent.winagent_client import FakeWinAgent
        self.fake_adb, self.fake_containers, self.fake_wa = FakeAdb(), FakeContainers(), FakeWinAgent()
        self.agent = AgentApp(self.cfg, db_path=str(tmp_path / "agent.db"), clock=clock, sender=self._sender,
                              maindb_factory=lambda uid, acct: LocalSqliteMainDb(maindb.path),
                              adb=self.fake_adb, containers=self.fake_containers,
                              winagent_transport=self.fake_wa, winagent_base_url="http://winagent.fake:17610",
                              winagent_token=self.fake_wa.token).open()
        self.store = self.agent.store
        s = self.store
        s.ensure_account(QD, "qidian", state="running", self_uid=SELF_UID, label="张三-固收")
        s.ensure_account(QQ, "qq", state="running", login_mode="qrcode", self_uid="415011447", label="测试QQ")
        s.ensure_account(WX, "wechat", state="running", login_mode="qrcode", label="测试微信")
        s.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_ADMIN)
        s.upsert_api_client(app_id="bot_w", name="写机器人", level="write", token=TOK_W)
        s.upsert_api_client(app_id="bot_r", name="读机器人", level="read", token=TOK_R)
        s.upsert_api_client(app_id="bot_lim", name="限账号机器人", level="write", token=TOK_LIM, allow_accounts=[QD])
        self.api = self.agent.create_api()
        self.primed = False

    async def _sender(self, acct, native_id: str, text: str) -> bool:
        """假执行层:点发送键即返回 True;模拟企点 9 s 后把我方消息落进主库(读回 = poll 的 ingest 合并,06 §2.12)。"""
        self.sent.append((native_id, text))

        async def land():
            await asyncio.sleep(self.land_delay_s)
            self.clock.advance(9000)
            self.maindb.insert_text(native_id, text, time_s=self.clock.now_s, issend=1)

        asyncio.create_task(land())
        return True

    def prime(self, client: TestClient) -> None:
        """发送前先让主库有一条历史并跑一轮读库全量轮,会话表映射建好。"""
        if not self.primed:
            self.maindb.insert_text(PEER, "历史", time_s=self.clock.now_s - 3600)
            client.portal.call(self.agent.qidian_poll_all)
            self.primed = True

    def send(self, client: TestClient, tok: str, key: Optional[str], text: str, *, account: str = QD, session: str = QD_SESSION, **extra):
        body: dict[str, Any] = {"session": session, "text": text, **extra}
        if key is not None:
            body["idempotency_key"] = key
        return client.post(f"{P}/accounts/{account}/send", json=body, headers=H(tok))


@pytest.fixture
def rig(tmp_path, clock, maindb) -> Rig:
    clock.auto_step_ms = 200         # 起 bus/API 的用例:可拨时钟每次读取自动前进,确认窗才到得了 deadline
    r = Rig(tmp_path, clock, maindb)
    yield r
    r.store.close()


@pytest.fixture
def client(rig: Rig):
    """loopback 来源的 TestClient;退出前把 bus 的账号队列收掉。"""
    with TestClient(rig.api, client=("127.0.0.1", 40000)) as c:
        try:
            yield c
        finally:
            try:
                c.portal.call(rig.agent.bus.close)
            except Exception:
                pass


# 消息数据集(GET /messages 用例共用;ts 相对 BASE_MS 固定,便于按 ISO 断言)
MSG_A = ("A", BASE_MS - 50_000)   # qd01 入向 "国债期货收益率上行"
MSG_B = ("B", BASE_MS - 40_000)   # qd01 入向 "你好世界"
MSG_C = ("C", BASE_MS - 30_000)   # qd01 出向 DELIVERED "报价 1Y 1.70"
MSG_D = ("D", BASE_MS - 20_000)   # qd01 入向 image(text None)
MSG_F = ("F", BASE_MS - 10_000)   # qd01 出向 SENDING "待发送"
MSG_E = ("E", BASE_MS - 5_000)    # qq03 群入向 "收益率 债券"(张三)


def seed_messages(rig: Rig) -> dict[str, str]:
    """直接经 store.ingest 灌一组消息;返回 {标签: message_id}。"""
    s = rig.store
    qd_sess = Session(QD, PEER, "private", "对端")
    qq_sess = Session(QQ, QQ_GROUP, "group", "测试群")
    ids = {}
    ids["A"] = s.ingest(Message(QD, "qidian", qd_sess, "in", "text", "国债期货收益率上行", MSG_A[1], "qidian_db", ext_msg_id="qd:101",
                                dedup_kind="native", sender_id=PEER)).id
    ids["B"] = s.ingest(Message(QD, "qidian", qd_sess, "in", "text", "你好世界", MSG_B[1], "qidian_db", ext_msg_id="qd:102",
                                dedup_kind="native", sender_id=PEER)).id
    ids["C"] = s.ingest(Message(QD, "qidian", qd_sess, "out", "text", "报价 1Y 1.70", MSG_C[1], "ui", ext_msg_id="qd:103",
                                sender_id=SELF_UID, self=True, state="DELIVERED", trace_id="TRACE-C", idempotency_key="k-c")).id
    ids["D"] = s.ingest(Message(QD, "qidian", qd_sess, "in", "image", None, MSG_D[1], "qidian_db", ext_msg_id="qd:104", dedup_kind="native",
                                sender_id=PEER, media=[{"ref": "media/202609/abc", "sha256": "abc", "mime": "image/png", "size": 10,
                                                        "kind": "image", "state": "pending"}])).id
    ids["F"] = s.ingest(Message(QD, "qidian", qd_sess, "out", "text", "待发送", MSG_F[1], "ui", ext_msg_id=None,
                                sender_id=SELF_UID, self=True, state="SENDING", trace_id="TRACE-F", idempotency_key="k-f")).id
    ids["E"] = s.ingest(Message(QQ, "qq", qq_sess, "in", "text", "收益率 债券", MSG_E[1], "onebot", ext_msg_id="g_456:501",
                                sender_id="10001", sender_name="张三")).id
    return ids


def items_of(body: dict) -> list:
    """#48:响应 `{ok, data: Message[], next_cursor, slow_match?}`——R6-53 按 02 §3.4 通用(C-42)把 R6-52 的 `items` 改回 `data`;本函数由总控同步改。"""
    assert "data" in body, f"#48 响应键为 data(§3.4 通用),实际键 {sorted(body)}"
    return body["data"]




# ══════════════════════════════════════════════════════════════════════ 一、版本协商(02 §3.8)


def test_V01_every_response_carries_two_version_headers(rig, client):
    """02 §3.8 响应头:Agent 每个响应带 `X-QT-Api-Version: 主.次` 与 `X-QT-Agent-Version`;02 §7.1 [api] api_version 默认 "1.0"。
    成功响应与错误响应(401)都要带。"""
    ok = client.get(f"{P}/system/version", headers=H(TOK_ADMIN))
    assert ok.status_code == 200
    assert_version_headers(ok)
    bad = client.get(f"{P}/accounts")
    assert bad.status_code == 401
    assert_version_headers(bad)


def test_V02_api_min_unsatisfied_returns_426_upgrade_required(rig, client):
    """02 §3.8:请求带 `X-QT-Api-Min: 1.2` 声明所需最低次版本,服务端(1.0)不满足回 `426 {code:'UPGRADE_REQUIRED'}`;
    00 §10 错误信封逐键;版本头照带。"""
    r = client.get(f"{P}/system/version", headers=H(TOK_ADMIN) | {"X-QT-Api-Min": "1.2"})
    assert r.status_code == 426
    assert_error_envelope(r.json(), "UPGRADE_REQUIRED")
    assert_version_headers(r)


def test_V03_api_min_satisfied_passes(rig, client):
    """02 §3.8:声明的最低次版本 ≤ 当前(1.0)即满足,正常处理。"""
    r = client.get(f"{P}/system/version", headers=H(TOK_ADMIN) | {"X-QT-Api-Min": "1.0"})
    assert r.status_code == 200
    assert r.json()["api_version"] == "1.0"


def test_V04_api_min_major_mismatch_returns_426(rig, client):
    """02 §3.8:API 主版本单独计(`/api/v1`),主版本不等即不满足 → 426。"""
    r = client.get(f"{P}/system/version", headers=H(TOK_ADMIN) | {"X-QT-Api-Min": "2.0"})
    assert r.status_code == 426
    assert r.json()["code"] == "UPGRADE_REQUIRED"


# ══════════════════════════════════════════════════════════════════════ 二、鉴权(00 §10;02 §3.4 通用;§3.1 api_clients)


def test_A01_missing_token_401_envelope(rig, client):
    """00 §10:鉴权 `Authorization: Bearer <token>`;缺令牌 → HTTP 401,错误信封 `{ok:false, code:'UNAUTHORIZED', error{...}, trace_id}` 逐键。"""
    r = client.get(f"{P}/accounts")
    assert r.status_code == 401
    assert_error_envelope(r.json(), "UNAUTHORIZED")


def test_A02_bad_token_401_envelope(rig, client):
    """00 §10 / 02 §3.1 api_clients:令牌按 sha256 比对 secret_hash;错令牌 → 401 UNAUTHORIZED 信封。"""
    r = client.get(f"{P}/accounts", headers=H("no-such-token"))
    assert r.status_code == 401
    assert_error_envelope(r.json(), "UNAUTHORIZED")


def test_A03_read_level_token_on_write_endpoint_403(rig, client):
    """02 §3.4 通用:鉴权级别 R/W/A 高包含低;#29 `POST /accounts/{id}/send` 是 W 级 → read 令牌 403 FORBIDDEN 信封逐键;
    未受理 ⇒ `commands` 无行、`idempotency` 未占。"""
    r = rig.send(client, TOK_R, "k-a03", "hello")
    assert r.status_code == 403
    assert_error_envelope(r.json(), "FORBIDDEN")
    assert count(rig.store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='k-a03'") == 0
    assert count(rig.store, "SELECT COUNT(*) FROM idempotency WHERE idem_key='k-a03'") == 0


def test_A04_allow_accounts_narrows_access_403(rig, client):
    """02 §3.1 `api_clients.allow_accounts_json` 收窄账号访问(["*"] = 不限):令牌只允许 qd01 时,
    `GET /accounts/qq03` → 403 FORBIDDEN 信封;`GET /accounts` 只列出 qd01;`POST /accounts/qq03/send` → 403 且 commands 无行。"""
    r = client.get(f"{P}/accounts/{QQ}", headers=H(TOK_LIM))
    assert r.status_code == 403
    assert_error_envelope(r.json(), "FORBIDDEN")
    lst = client.get(f"{P}/accounts", headers=H(TOK_LIM))
    assert lst.status_code == 200
    assert [a["id"] for a in lst.json()["data"]] == [QD]
    r2 = rig.send(client, TOK_LIM, "k-a04", "hi", account=QQ, session=QQ_SESSION)
    assert r2.status_code == 403
    assert count(rig.store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='k-a04'") == 0
    # 允许的账号照常可读
    assert client.get(f"{P}/accounts/{QD}", headers=H(TOK_LIM)).status_code == 200


def test_A05_disabled_or_revoked_client_401(rig, client):
    """02 §3.1 api_clients:`enabled=0` 或 `revoked_ms` 非空(#93 吊销)的令牌不再有效 → 401。"""
    rig.store.con.execute("UPDATE api_clients SET enabled=0 WHERE app_id='bot_r'")
    assert client.get(f"{P}/accounts", headers=H(TOK_R)).status_code == 401
    rig.store.con.execute("UPDATE api_clients SET enabled=1, revoked_ms=? WHERE app_id='bot_r'", (rig.clock.now_ms,))
    assert client.get(f"{P}/accounts", headers=H(TOK_R)).status_code == 401


# ══════════════════════════════════════════════════════════════════════ 三、/system/health、/system/version(02 #72/#73;B-25;R6-52 ⑥)


def test_H01_health_loopback_unauth_boolean_summary(rig, client):
    """00 §10 / 02 #72 / B-25 / R6-52 ⑥:loopback 来源免鉴权,只回布尔级摘要,固定五键 `{ok, agent, dockerd, winagent, user_agent}`,值均布尔。"""
    r = client.get(f"{P}/system/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "agent", "dockerd", "winagent", "user_agent"}, body
    assert all(isinstance(v, bool) for v in body.values())
    assert body["ok"] is True and body["agent"] is True


def test_H02_health_non_loopback_unauth_401(rig):
    """02 #72 / B-25:免鉴权只对 loopback 与 WSL 网关;局域网另一台机器无令牌 → 401。"""
    with TestClient(rig.api, client=("10.1.2.3", 40001)) as c:
        r = c.get(f"{P}/system/health")
        assert r.status_code == 401
        assert_error_envelope(r.json(), "UNAUTHORIZED")


def test_H03_health_with_token_full_body(rig, client):
    """02 #72:带令牌回全量 `{ok, agent:{version, api_version, uptime_s, db_mb, wal_mb}, dockerd, winagent:{online, version, user_agent},
    accounts:{running, n}, checks, alerts}`(B-25:含 checks/alerts)。"""
    r = client.get(f"{P}/system/health", headers=H(TOK_R))
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    assert {"version", "api_version", "uptime_s", "db_mb", "wal_mb"} <= set(b["agent"])
    assert b["agent"]["api_version"] == "1.0"
    assert isinstance(b["agent"]["uptime_s"], int) and b["agent"]["uptime_s"] >= 0
    assert "dockerd" in b
    assert {"online", "version", "user_agent"} <= set(b["winagent"])
    assert {"running", "n"} <= set(b["accounts"])
    assert b["accounts"]["running"] == 3 and b["accounts"]["n"] == 3
    assert "checks" in b and isinstance(b["alerts"], list)


def test_H04_health_full_has_disk_free_mb(rig, client):
    """02 #72 全量体逐字含 `disk_free_mb`(B-35 判据也读 `GET /system/health.disk_free_mb`)。"""
    b = client.get(f"{P}/system/health", headers=H(TOK_R)).json()
    assert "disk_free_mb" in b, sorted(b)


def test_H05_system_version_keys(rig, client):
    """02 #73 / A-13:`GET /system/version` 含 `api_version / capabilities_version / schema_version` 与 Agent 版本;
    `capabilities_version` 与 `GET /capabilities` 下发的同值(§3.10)。"""
    v = client.get(f"{P}/system/version", headers=H(TOK_R))
    assert v.status_code == 200
    b = v.json()
    assert b["api_version"] == "1.0"
    assert isinstance(b["capabilities_version"], str) and b["capabilities_version"]
    assert b["schema_version"] is not None
    assert b["agent"]["version"]
    caps = client.get(f"{P}/capabilities", headers=H(TOK_R)).json()
    assert caps["capabilities_version"] == b["capabilities_version"]


# ══════════════════════════════════════════════════════════════════════ 四、GET /capabilities(02 #21;§3.10;§2.2.2 session 命名)


def cap_items(body: dict) -> list[dict]:
    lst = next((v for v in body.values() if isinstance(v, list)), None)
    assert lst is not None, f"目录响应无列表:{sorted(body)}"
    return lst


def test_C01_capabilities_catalog_shape(rig, client):
    """02 #21:全局能力目录,每项 `{op, kind:'read'|'write'|'admin', channels:{qidian,qq,wechat}, args_schema, result_schema, danger, confirmable}`,
    `high_risk` 为 `danger` 的只读别名同值输出;响应带 `capabilities_version`。"""
    r = client.get(f"{P}/capabilities", headers=H(TOK_R))
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    assert isinstance(b["capabilities_version"], str) and b["capabilities_version"]
    items = cap_items(b)
    assert items, "目录为空"
    for c in items:
        assert {"op", "kind", "channels", "args_schema", "result_schema", "danger", "confirmable", "high_risk"} <= set(c), c["op"]
        assert c["kind"] in ("read", "write", "admin")
        assert isinstance(c["danger"], bool) and c["high_risk"] == c["danger"]
        assert {"qidian", "qq", "wechat"} <= set(c["channels"])
        assert isinstance(c["args_schema"], dict)


def test_C02_send_text_entry(rig, client):
    """02 §3.10:`send_text` 是 `kind=write`、`danger:false`(普通发送)、`confirmable:true`;args_schema 的会话参数一律叫 `session`(§2.2.2)。"""
    caps = {c["op"]: c for c in cap_items(client.get(f"{P}/capabilities", headers=H(TOK_R)).json())}
    assert "send_text" in caps
    st = caps["send_text"]
    assert st["kind"] == "write" and st["danger"] is False and st["confirmable"] is True
    props = st["args_schema"].get("properties", {})
    assert "session" in props and "text" in props


def test_C03_capabilities_channel_filter(rig, client):
    """02 #21:`?channel=` 过滤 → 只回该通道 `supported` 的 op。"""
    r = client.get(f"{P}/capabilities", params={"channel": "qidian"}, headers=H(TOK_R))
    assert r.status_code == 200
    for c in cap_items(r.json()):
        assert c["channels"]["qidian"] == "supported"


def test_C04_no_legacy_session_param_names(rig, client):
    """02 §2.2.2:`peer/chat/target/talker/group_id/user_id` 等旧名一律作废(CI:凡带会话语义的属性名不是 `session` 即构建失败)。"""
    legacy = {"peer", "chat", "target", "talker", "group_id", "user_id"}
    for c in cap_items(client.get(f"{P}/capabilities", headers=H(TOK_R)).json()):
        props = set(c["args_schema"].get("properties", {}))
        assert not (props & legacy), f"{c['op']} 含旧会话参数名 {props & legacy}"


def test_C05_capabilities_version_header(rig, client):
    """02 §3.10:`capabilities_version` 随 `GET /capabilities`、`GET /system/version`、**`X-QT-Capabilities-Version` 响应头**下发。"""
    r = client.get(f"{P}/capabilities", headers=H(TOK_R))
    assert r.headers.get("X-QT-Capabilities-Version") == r.json()["capabilities_version"]


# ══════════════════════════════════════════════════════════════════════ 五、Account 信封(00 §7.1;02 #1/#3 + Account 序列化表;C-07 端口推导;R6-4)


def test_K01_account_envelope_qidian(rig, client):
    """00 §7.1 Account 逐键;02 §3.4.1 Account 序列化:`error_since_ms` 仅 state=error 时非空(running → null);
    02 §2.2.4 C-07 / R6-52 ⑥:缺 account_runtime 行时端口按序号推导 qidian adb=16000+seq、stream=16500+seq,容器 `qtrade-qd01`;
    `login{mode, credential_ref, remember}`;`created_at/updated_at` ISO +08:00;`host=wsl`。"""
    r = client.get(f"{P}/accounts/{QD}", headers=H(TOK_R))
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    a = b["data"]
    assert ACCOUNT_KEYS <= set(a), ACCOUNT_KEYS - set(a)
    assert a["id"] == QD and a["channel"] == "qidian" and a["host"] == "wsl" and a["label"] == "张三-固收"
    assert a["state"] == "running" and isinstance(a["state_code"], str) and isinstance(a["state_reason"], str)
    assert a["error_since_ms"] is None
    assert a["enabled"] is True and isinstance(a["auto_recover"], bool) and a["deleted_ms"] is None
    rt = a["runtime"]
    assert rt["kind"] == "redroid" and rt["container"] == f"qtrade-{QD}"
    assert rt["adb_port"] == 16001 and rt["stream_port"] == 16501
    assert set(a["login"]) >= {"mode", "credential_ref", "remember"} and a["login"]["mode"] == "password"
    assert isinstance(a["capabilities"], list) and {"send_text", "read_messages"} <= set(a["capabilities"])
    assert a["quota_mb"] == 2560 and a["self_uid"] == SELF_UID
    assert isinstance(a["identity"], dict)
    assert ISO_RE.match(a["created_at"]) and ISO_RE.match(a["updated_at"])
    assert a["last_seen_at"] is None or ISO_RE.match(a["last_seen_at"])
    # 02 §3.4.1 序列化表:wxid / merged_into 微信专有,非微信恒 null
    assert a.get("wxid") is None and a.get("merged_into") is None


def test_K02_account_runtime_qq_ports(rig, client):
    """02 §2.2.4 C-07:qq ws=16100+seq、http=16200+seq;runtime.kind=napcat;00 §7.1 runtime 示例键 ws_port/http_port。"""
    a = client.get(f"{P}/accounts/{QQ}", headers=H(TOK_R)).json()["data"]
    assert a["runtime"]["kind"] == "napcat"
    assert a["runtime"]["ws_port"] == 16103 and a["runtime"]["http_port"] == 16203
    assert a["login"]["mode"] == "qrcode"


def test_K03_account_runtime_wechat(rig, client):
    """00 §7.1:微信 `host=windows`,runtime `{kind:'wechat_pc', wechat_version, wxkey_dll}`(无端口,C-07);`wxid/merged_into` 键存在。"""
    a = client.get(f"{P}/accounts/{WX}", headers=H(TOK_R)).json()["data"]
    assert a["host"] == "windows"
    assert a["runtime"]["kind"] == "wechat_pc"
    assert {"wechat_version", "wxkey_dll"} <= set(a["runtime"])
    assert "wxid" in a and "merged_into" in a


def test_K04_error_since_ms_only_when_state_error(rig, client):
    """00 §7.1 / R6-4 / 02 §3.4.1 序列化表:`error_since_ms` = account_runtime.error_since_ms,INTEGER ms,**仅 state='error' 时非空**,
    离开 error 即 null。"""
    rig.store.upsert_runtime(QD, kind="redroid", error_since_ms=1_700_000_000_000)
    rig.store.set_account_state(QD, "error", state_code="CONTAINER_EXIT", state_reason="容器退出")
    a = client.get(f"{P}/accounts/{QD}", headers=H(TOK_R)).json()["data"]
    assert a["state"] == "error" and a["error_since_ms"] == 1_700_000_000_000
    assert a["state_code"] == "CONTAINER_EXIT" and a["state_reason"] == "容器退出"
    rig.store.set_account_state(QD, "running")
    a2 = client.get(f"{P}/accounts/{QD}", headers=H(TOK_R)).json()["data"]
    assert a2["state"] == "running" and a2["error_since_ms"] is None


def test_K05_list_and_detail_same_envelope(rig, client):
    """02 §3.4.1 ⚠️:#1 列表元素与 #3 详情必须同值同字段名(只改一处 = 列表页与推送打架)。"""
    lst = {a["id"]: a for a in client.get(f"{P}/accounts", headers=H(TOK_R)).json()["data"]}
    assert set(lst) == {QD, QQ, WX}
    for aid in (QD, QQ, WX):
        detail = client.get(f"{P}/accounts/{aid}", headers=H(TOK_R)).json()["data"]
        assert lst[aid] == detail, aid


def test_K06_account_not_found_404(rig, client):
    """00 §10:404 `TARGET_NOT_FOUND`(会话/联系人/账号不存在)+ 错误信封。"""
    r = client.get(f"{P}/accounts/qd77", headers=H(TOK_R))
    assert r.status_code == 404
    assert_error_envelope(r.json(), "TARGET_NOT_FOUND")


def test_K07_list_filters_and_soft_deleted(rig, client):
    """02 #1:`?channel=&state=&enabled=&include_deleted=false`;B-04:软删行默认不含、`?include_deleted=true` 含。"""
    only_qq = client.get(f"{P}/accounts", params={"channel": "qq"}, headers=H(TOK_R)).json()["data"]
    assert [a["id"] for a in only_qq] == [QQ]
    rig.store.con.execute("UPDATE accounts SET deleted_ms=? WHERE id=?", (rig.clock.now_ms, QQ))
    ids = [a["id"] for a in client.get(f"{P}/accounts", headers=H(TOK_R)).json()["data"]]
    assert QQ not in ids
    ids2 = [a["id"] for a in client.get(f"{P}/accounts", params={"include_deleted": "true"}, headers=H(TOK_R)).json()["data"]]
    assert QQ in ids2


# ══════════════════════════════════════════════════════════════════════ 六、指令(02 #28/#29/#30/#31;R6-52 ①;B-06/B-07/B-30/B-40;00 §7.3)


def test_M01_write_op_without_idempotency_key_400(rig, client):
    """02 #28(R6-52 ①):写类必带 `idempotency_key`,缺 → `400 INVALID_ARGS`,`error.reason='idempotency_key_required'`;
    参数校验层 400 不进 `commands`(R6-48 同层)。"""
    r = rig.send(client, TOK_W, None, "hello")
    assert r.status_code == 400
    b = r.json()
    assert_error_envelope(b, "INVALID_ARGS")
    assert b["error"]["reason"] == "idempotency_key_required"
    assert count(rig.store, "SELECT COUNT(*) FROM commands") == 0
    r2 = client.post(f"{P}/accounts/{QD}/commands", json={"op": "send_text", "args": {"session": QD_SESSION, "text": "x"}}, headers=H(TOK_W))
    assert r2.status_code == 400 and r2.json()["error"]["reason"] == "idempotency_key_required"


def test_M02_read_op_with_read_token_200_ok(rig, client):
    """02 #28「W(只读 op 为 R)」:`get_state` 用 read 级令牌可调;同步 → 200 + 00 §7.3 CommandResult;
    R6-51 ⑭:企点 `get_state` 返回 `OK {state, state_code, self_uid}`;trace_id 为 ULID(00 §6)。"""
    r = client.post(f"{P}/accounts/{QD}/commands", json={"op": "get_state", "args": {}}, headers=H(TOK_R))
    assert r.status_code == 200, r.text
    b = r.json()
    assert RESULT_KEYS <= set(b), RESULT_KEYS - set(b)
    assert b["ok"] is True and b["code"] == "OK"
    assert b["data"]["state"] == "running" and "state_code" in b["data"] and b["data"]["self_uid"] == SELF_UID
    assert ULID_RE.match(b["trace_id"]), b["trace_id"]


def test_M03_send_end_to_end_delivered(rig, client):
    """02 #29 端到端:`POST /accounts/{id}/send {session, text, idempotency_key}` → 200 + 00 §7.3 信封;code=DELIVERED;
    §3.10 / R6-51 ⑭:`data = {message_id, ext_msg_id, confirmed_by}`;B-07:出向行完成后 `DELIVERED, qd:<uniseq>, ingest_merge`,行数恒 1;
    通道里只出现一条消息。"""
    rig.prime(client)
    r = rig.send(client, TOK_W, "k9", "a")
    assert r.status_code == 200, r.text
    b = r.json()
    assert RESULT_KEYS <= set(b), RESULT_KEYS - set(b)
    assert b["ok"] is True and b["code"] == "DELIVERED"
    assert set(b["data"]) == {"message_id", "ext_msg_id", "confirmed_by"}, b["data"]
    assert b["data"]["ext_msg_id"].startswith("qd:") and b["data"]["confirmed_by"] == "ingest_merge"
    assert isinstance(b["cost_ms"], int) and ULID_RE.match(b["trace_id"])
    assert rows(rig.store, "SELECT state, ext_msg_id, confirmed_by FROM messages WHERE idempotency_key='k9'") == [("DELIVERED", b["data"]["ext_msg_id"], "ingest_merge")]
    assert rig.sent == [(PEER, "a")]


def test_M04_B06_idempotent_replay_and_args_mismatch(rig, client):
    """B-06 / 02 §2.2.2 幂等三态 / R6-52 ①:第 1 次 200 DELIVERED;立即再发同 body → `409 IDEMPOTENT_REPLAY`,响应体仍是完整 CommandResult 且
    `data` 与第 1 次逐字相同;再发 `text:'b'` 同 key → `400 INVALID_ARGS`(同键不同参,C-10);
    `idempotency` 里 k9 = `DONE,DELIVERED` 一行;通道里只出现一条消息。"""
    rig.prime(client)
    r1 = rig.send(client, TOK_W, "k9", "a")
    assert r1.status_code == 200 and r1.json()["code"] == "DELIVERED"
    r2 = rig.send(client, TOK_W, "k9", "a")
    assert r2.status_code == 409, r2.text
    b2 = r2.json()
    assert b2["code"] == "IDEMPOTENT_REPLAY"
    assert RESULT_KEYS <= set(b2), RESULT_KEYS - set(b2)
    assert b2["data"] == r1.json()["data"]
    r3 = rig.send(client, TOK_W, "k9", "b")
    assert r3.status_code == 400, r3.text
    assert r3.json()["code"] == "INVALID_ARGS"
    assert rows(rig.store, "SELECT status, result_code FROM idempotency WHERE idem_key='k9'") == [("DONE", "DELIVERED")]
    assert rig.sent == [(PEER, "a")]


def test_M05_B40_control_chars_rejected_at_entry(rig, client):
    """B-40 / R6-48 / 02 §3.10:`text` 含控制字符 → `400 INVALID_ARGS`、`error.reason='text_has_control_chars'`、`error.details[0].pointer='/text'`,
    未进总线:`commands` 无 k10 行、`idempotency` 未占;第三次 `"1Y\\n1.70"` 放行 → 200 DELIVERED;
    最终 `commands` k10 = 1 行、`idempotency` k10 一行、`messages` k10 只一行且 text 含换行原文。"""
    rig.prime(client)
    for bad in ("收到\u0014A", "收到\u0008"):
        r = rig.send(client, TOK_W, "k10", bad)
        assert r.status_code == 400, r.text
        b = r.json()
        assert_error_envelope(b, "INVALID_ARGS")
        assert b["error"]["reason"] == "text_has_control_chars"
        assert b["error"]["details"][0]["pointer"] == "/text"
    assert count(rig.store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='k10'") == 0
    assert count(rig.store, "SELECT COUNT(*) FROM idempotency WHERE idem_key='k10'") == 0
    r3 = rig.send(client, TOK_W, "k10", "1Y\n1.70")
    assert r3.status_code == 200 and r3.json()["code"] == "DELIVERED", r3.text
    assert count(rig.store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='k10'") == 1
    assert count(rig.store, "SELECT COUNT(*) FROM idempotency WHERE idem_key='k10'") == 1
    assert rows(rig.store, "SELECT text FROM messages WHERE idempotency_key='k10'") == [("1Y\n1.70",)]


def test_M06_B30_login_gate(rig, client):
    """B-30 / 02 §2.2.2 登录门 / R6-51 ① / R6-52 ①:账号 login_required 时 send **立即**返回业务结果 `LOGIN_REQUIRED`(HTTP 200 + CommandResult),
    `error.needs_human=true`、`error.retryable=false`(00 §8.3);`commands` 有 send 行 `status='failed'`、`started_ms IS NULL`(没进队列)
    + `command_results(code='LOGIN_REQUIRED')`;`idempotency` 无该 key;不写 SENDING 行;未点发送键。人登录后原键重发即可 → DELIVERED。"""
    rig.prime(client)
    rig.store.set_account_state(QD, "login_required", state_code="WAIT_SMS")
    r = rig.send(client, TOK_W, "k30", "登录门")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is False and b["code"] == "LOGIN_REQUIRED"
    assert b["error"]["needs_human"] is True and b["error"]["retryable"] is False
    assert rows(rig.store, "SELECT status, started_ms FROM commands WHERE idempotency_key='k30'") == [("failed", None)]
    assert rows(rig.store, "SELECT r.code FROM command_results r JOIN commands c ON c.trace_id=r.trace_id WHERE c.idempotency_key='k30'") == [("LOGIN_REQUIRED",)]
    assert count(rig.store, "SELECT COUNT(*) FROM idempotency WHERE idem_key='k30'") == 0
    assert count(rig.store, "SELECT COUNT(*) FROM messages WHERE idempotency_key='k30'") == 0
    assert rig.sent == []
    rig.store.set_account_state(QD, "running")
    r2 = rig.send(client, TOK_W, "k30", "登录门")
    assert r2.status_code == 200 and r2.json()["code"] == "DELIVERED", r2.text


def test_M07_async_true_202_trace_id_and_command_done(rig, client):
    """02 #28:`async:true` → `202 {trace_id}`,完成后 `command_done` 事件;#31 可查到最终 `result.code=DELIVERED`。"""
    rig.prime(client)
    r = rig.send(client, TOK_W, "k-async", "异步", **{"async": True})
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["ok"] is True and ULID_RE.match(b["trace_id"])
    trace = b["trace_id"]
    assert wait_until(lambda: count(rig.store, "SELECT COUNT(*) FROM command_results WHERE trace_id=?", trace) == 1), "异步指令未完成"
    done = [e for e in rig.store.list_events(event="command_done") if e["trace_id"] == trace]
    assert len(done) == 1 and done[0]["payload"]["code"] == "DELIVERED"
    g = client.get(f"{P}/accounts/{QD}/commands/{trace}", headers=H(TOK_R))
    assert g.status_code == 200
    assert g.json()["result"]["code"] == "DELIVERED"


def test_M08_wechat_confirm_false_400(rig, client):
    """02 #28(C-30):微信 `confirm:false` → `400 INVALID_ARGS`(拒绝比静默忽略清楚);参数校验层不进 `commands`。"""
    r = client.post(f"{P}/accounts/{WX}/commands", json={"op": "send_text", "args": {"session": f"{WX}:wxid_x", "text": "hi"},
                                                        "idempotency_key": "kw1", "confirm": False}, headers=H(TOK_W))
    assert r.status_code == 400, r.text
    assert_error_envelope(r.json(), "INVALID_ARGS")
    assert count(rig.store, "SELECT COUNT(*) FROM commands WHERE idempotency_key='kw1'") == 0


def test_M09_get_command_detail_shape(rig, client):
    """02 #31:`GET /accounts/{id}/commands/{trace_id}` → `{command, result}`;command 按 00 §7.2(trace_id/account_id/op/args/idempotency_key/
    confirm/timeout_ms/origin{transport,actor,ip}/submitted_at);02 §2.2.2 P-11:`args.text` 入库前替换为 `{text_sha8, text_len}`,不含正文;
    origin.actor = `app:<app_id>`(00 §7.2)。"""
    rig.prime(client)
    text = "明文不入 args"
    r = rig.send(client, TOK_W, "k31", text)
    assert r.status_code == 200 and r.json()["code"] == "DELIVERED"
    trace = r.json()["trace_id"]
    g = client.get(f"{P}/accounts/{QD}/commands/{trace}", headers=H(TOK_R))
    assert g.status_code == 200
    b = g.json()
    assert set(b) >= {"command", "result"}
    c, res = b["command"], b["result"]
    assert {"trace_id", "account_id", "op", "args", "idempotency_key", "confirm", "timeout_ms", "origin", "submitted_at"} <= set(c), sorted(c)
    assert c["trace_id"] == trace and c["account_id"] == QD and c["op"] == "send_text" and c["idempotency_key"] == "k31"
    assert c["confirm"] is True and isinstance(c["timeout_ms"], int)
    assert ISO_RE.match(c["submitted_at"])
    assert c["origin"]["actor"] == "app:bot_w" and c["origin"]["transport"] in ("local", "http")
    assert "text" not in c["args"]
    assert c["args"]["text_sha8"] == hashlib.sha256(text.encode()).hexdigest()[:8] and c["args"]["text_len"] == len(text)
    assert c["args"]["session"] == QD_SESSION
    assert res["code"] == "DELIVERED" and res["ok"] is True and res["trace_id"] == trace
    assert set(res["data"]) == {"message_id", "ext_msg_id", "confirmed_by"}
    # 库列同样不存正文(00 §11.9 [NOBODY])
    assert text not in rig.store.con.execute("SELECT args_json FROM commands WHERE trace_id=?", (trace,)).fetchone()[0]


def test_M10_list_commands_with_filters(rig, client):
    """02 #30:`GET /accounts/{id}/commands?status=&op=` → `(Command+CommandResult)[]`,每元素带 command 与 result。"""
    rig.prime(client)
    assert rig.send(client, TOK_W, "k30a", "一").json()["code"] == "DELIVERED"
    assert client.post(f"{P}/accounts/{QD}/commands", json={"op": "get_state", "args": {}}, headers=H(TOK_R)).status_code == 200
    r = client.get(f"{P}/accounts/{QD}/commands", headers=H(TOK_R))
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 2
    for e in data:
        assert "command" in e and "result" in e
        assert e["result"]["code"] in ("DELIVERED", "OK")
    only_send = client.get(f"{P}/accounts/{QD}/commands", params={"op": "send_text"}, headers=H(TOK_R)).json()["data"]
    assert [e["command"]["op"] for e in only_send] == ["send_text"]
    done = client.get(f"{P}/accounts/{QD}/commands", params={"status": "done"}, headers=H(TOK_R)).json()["data"]
    assert len(done) == 2 and all(e["command"]["status"] == "done" for e in done)


def test_M11_command_not_found_404(rig, client):
    """00 §10:指令不存在 / 属于别的账号 → 404 TARGET_NOT_FOUND。"""
    rig.prime(client)
    trace = rig.send(client, TOK_W, "k11", "x").json()["trace_id"]
    r = client.get(f"{P}/accounts/{QD}/commands/01ARZ3NDEKTSV4RRFFQ69G5FAV", headers=H(TOK_R))
    assert r.status_code == 404 and r.json()["code"] == "TARGET_NOT_FOUND"
    r2 = client.get(f"{P}/accounts/{QQ}/commands/{trace}", headers=H(TOK_R))
    assert r2.status_code == 404


def test_M12_unknown_op_400(rig, client):
    """02 §3.10 校验:op 不在能力目录 → 400 INVALID_ARGS 带 `error.details[]`(JSON Pointer)。"""
    r = client.post(f"{P}/accounts/{QD}/commands", json={"op": "fly_to_moon", "args": {}, "idempotency_key": "k12"}, headers=H(TOK_W))
    assert r.status_code == 400
    b = r.json()
    assert_error_envelope(b, "INVALID_ARGS")
    assert isinstance(b["error"].get("details"), list) and b["error"]["details"]


def test_M13_send_to_unknown_account_404(rig, client):
    """00 §10 / R6-52 ①:账号不存在 → `404 TARGET_NOT_FOUND`(HTTP 层错误)。"""
    r = rig.send(client, TOK_W, "k13", "x", account="qd77", session="qd77:1")
    assert r.status_code == 404
    assert_error_envelope(r.json(), "TARGET_NOT_FOUND")


def test_M14_send_confirm_always_true(rig, client):
    """02 #29:`/send` 等价 #28 且 `confirm` 恒 true —— body 带 `confirm:false` 也照常读回确认(DELIVERED),`commands.confirm=1`。"""
    rig.prime(client)
    r = rig.send(client, TOK_W, "k14", "恒确认", confirm=False)
    assert r.status_code == 200 and r.json()["code"] == "DELIVERED", r.text
    assert rows(rig.store, "SELECT confirm FROM commands WHERE idempotency_key='k14'") == [(1,)]


def test_M15_sync_wait_timeout_202_pending(tmp_path, clock, maindb):
    """02 #28(R6-52 ①)/ §7.1 [api] http_sync_max_wait_ms(P-10):同步等待超过上限 → `202 {trace_id, accepted:true, pending:true}`,
    指令继续执行、结果走 `command_done` 事件与 #31。"""
    clock.auto_step_ms = 200
    # 同步上限压到 1 ms:企点确认至少要一轮 confirm_poll_interval_ms=10 的加速 poll,必超时;确认窗本身按可拨时钟仍充裕
    r = Rig(tmp_path, clock, maindb, api_cfg=ApiConfig(http_sync_max_wait_ms=1))
    try:
        with TestClient(r.api, client=("127.0.0.1", 40000)) as c:
            try:
                r.prime(c)
                resp = r.send(c, TOK_W, "k15", "慢落库")
                assert resp.status_code == 202, resp.text
                b = resp.json()
                assert b["ok"] is True and b["accepted"] is True and b["pending"] is True and ULID_RE.match(b["trace_id"])
                trace = b["trace_id"]
                assert wait_until(lambda: count(r.store, "SELECT COUNT(*) FROM command_results WHERE trace_id=?", trace) == 1), "超时转 202 后指令未继续执行"
                assert rows(r.store, "SELECT code FROM command_results WHERE trace_id=?", trace) == [("DELIVERED",)]
                assert [e["trace_id"] for e in r.store.list_events(event="command_done")] == [trace]
                g = c.get(f"{P}/accounts/{QD}/commands/{trace}", headers=H(TOK_R))
                assert g.status_code == 200 and g.json()["result"]["code"] == "DELIVERED"
            finally:
                c.portal.call(r.agent.bus.close)
    finally:
        r.store.close()


# ══════════════════════════════════════════════════════════════════════ 七、GET /messages(02 #48/#49;§2.8.6;00 §7.4;R6-49;R6-52 ②)


def test_Q01_message_view_keys_and_no_event_only_fields(rig, client):
    """00 §7.4 Message(API 视图)逐键 + R6-52 ② 另带 `confirmed_by/trace_id`;R6-49:**不带** `lag_s/late/origin`;
    `session{id,name,kind}`、`sender{id,name}`、`media[].kind/state`;`ts/received_at` ISO 8601 +08:00(00 §6);#48 响应 `{items, next_cursor}`。"""
    ids = seed_messages(rig)
    r = client.get(f"{P}/messages", params={"account_id": QD, "session_id": QD_SESSION}, headers=H(TOK_R))
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and "next_cursor" in b
    items = items_of(b)
    assert len(items) == 5
    for m in items:
        assert MESSAGE_KEYS <= set(m), MESSAGE_KEYS - set(m)
        assert {"confirmed_by", "trace_id"} <= set(m)
        assert not (EVENT_ONLY_KEYS & set(m)), EVENT_ONLY_KEYS & set(m)
        assert set(m["session"]) >= {"id", "name", "kind"} and m["session"]["id"] == QD_SESSION and m["session"]["kind"] == "private"
        assert set(m["sender"]) >= {"id", "name"}
        assert ISO_RE.match(m["ts"]) and ISO_RE.match(m["received_at"])
        assert isinstance(m["media"], list) and isinstance(m["self"], bool) and isinstance(m["revoked"], bool)
        if m["text"] is not None:
            assert m["text_len"] == len(m["text"])
    by = {m["id"]: m for m in items}
    a = by[ids["A"]]
    assert a["ts"] == spec_iso(MSG_A[1]) and a["dir"] == "in" and a["type"] == "text" and a["state"] == "DELIVERED" and a["self"] is False
    assert a["sender"]["id"] == PEER and a["source"] == "qidian_db" and a["ext_msg_id"] == "qd:101"
    assert isinstance(a["fingerprint"], str) and len(a["fingerprint"]) == 64
    d = by[ids["D"]]
    assert d["type"] == "image" and d["text"] is None and d["media"][0]["kind"] == "image" and d["media"][0]["state"] == "pending"
    c = by[ids["C"]]
    assert c["dir"] == "out" and c["self"] is True and c["state"] == "DELIVERED" and c["trace_id"] == "TRACE-C"
    f = by[ids["F"]]
    assert f["state"] == "SENDING" and f["ext_msg_id"] is None


def test_Q02_order_ts_desc(rig, client):
    """02 §2.8.6:不做相关度排序,按 `ts_ms DESC`。"""
    ids = seed_messages(rig)
    items = items_of(client.get(f"{P}/messages", headers=H(TOK_R)).json())
    assert [m["id"] for m in items] == [ids["E"], ids["F"], ids["D"], ids["C"], ids["B"], ids["A"]]
    ts = [iso_to_ms(m["ts"]) for m in items]
    assert ts == sorted(ts, reverse=True)


def test_Q03_filters_dir_type_state_sender_session(rig, client):
    """02 #48:`dir/type/state/sender/session_id/account_id` 过滤(§2.8.6 检索接口支持:账号、会话、方向、类型、发送者)。"""
    ids = seed_messages(rig)

    def q(**params):
        return [m["id"] for m in items_of(client.get(f"{P}/messages", params=params, headers=H(TOK_R)).json())]

    assert set(q(dir="out")) == {ids["C"], ids["F"]}
    assert set(q(dir="in")) == {ids["A"], ids["B"], ids["D"], ids["E"]}
    assert q(type="image") == [ids["D"]]
    assert q(state="SENDING") == [ids["F"]]
    assert q(sender="10001") == [ids["E"]]
    assert q(sender="张三") == [ids["E"]]           # 发送者按 id 或名字
    assert q(session_id=QQ_SESSION) == [ids["E"]]
    assert set(q(account_id=QQ)) == {ids["E"]}


def test_Q04_since_until_iso_and_ms(rig, client):
    """02 §3.4 通用 C-42 / #48:`since/until` 参数(ISO 8601 或毫秒),时间窗按 `ts`。"""
    ids = seed_messages(rig)

    def q(**params):
        return {m["id"] for m in items_of(client.get(f"{P}/messages", params=params, headers=H(TOK_R)).json())}

    assert q(since=spec_iso(MSG_C[1])) == {ids["C"], ids["D"], ids["F"], ids["E"]}
    assert q(until=spec_iso(MSG_B[1])) == {ids["A"], ids["B"]}
    assert q(since=str(MSG_B[1]), until=str(MSG_D[1])) == {ids["B"], ids["C"], ids["D"]}
    bad = client.get(f"{P}/messages", params={"since": "昨天"}, headers=H(TOK_R))
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_ARGS"


def test_Q05_pagination_cursor(rig, client):
    """02 #48(R6-52 ②)/ §3.4 C-42 / G-16:`limit` + 透传 `cursor`;`next_cursor` 仅本页满 limit 时非空;`cursor = base64url(JSON{"ts_ms","id"})`(G-16,R6-53),
    服务端按 (ts_ms, id) 双键定位、翻页不重不漏;非法 cursor → `400 INVALID_ARGS`。"""
    seed_messages(rig)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"account_id": QD, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        b = client.get(f"{P}/messages", params=params, headers=H(TOK_R)).json()
        items = items_of(b)
        pages += 1
        seen += [m["id"] for m in items]
        if len(items) == 2:
            assert b["next_cursor"], "满页必须给 next_cursor"
            pad = "=" * (-len(b["next_cursor"]) % 4)
            raw = base64.urlsafe_b64decode(b["next_cursor"] + pad).decode()
            obj = __import__("json").loads(raw)                  # R6-53:按 §3.4 通用 G-16 = base64url(JSON{"ts_ms","id"})(R6-52 的 "ts_ms:id" 已改回;总控同步改本行)
            ts_s, id_s = str(obj["ts_ms"]), obj["id"]
            assert id_s == items[-1]["id"] and int(ts_s) == iso_to_ms(items[-1]["ts"])
            cursor = b["next_cursor"]
        else:
            assert b["next_cursor"] is None
            break
    assert pages == 3 and len(seen) == 5 and len(set(seen)) == 5
    bad = client.get(f"{P}/messages", params={"cursor": "!!not-a-cursor!!"}, headers=H(TOK_R))
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_ARGS"


def test_Q06_two_char_query_like_slow_match(rig, client):
    """02 §2.8.6 / #48 / B-22:2 字查询走不了 trigram,回退 `LIKE '%xx%'`,响应带 `slow_match:true`。"""
    ids = seed_messages(rig)
    b = client.get(f"{P}/messages", params={"q": "你好"}, headers=H(TOK_R)).json()
    assert [m["id"] for m in items_of(b)] == [ids["B"]]
    assert b.get("slow_match") is True


def test_Q07_three_char_query_fts(rig, client):
    """02 §2.8.6 / #48 / B-22:≥3 字走 FTS trigram(任意 ≥3 字符子串命中、跨账号),`slow_match` 只在含 <3 字词时出现。"""
    ids = seed_messages(rig)
    b = client.get(f"{P}/messages", params={"q": "收益率"}, headers=H(TOK_R)).json()
    assert {m["id"] for m in items_of(b)} == {ids["A"], ids["E"]}
    assert not b.get("slow_match")
    b2 = client.get(f"{P}/messages", params={"q": "国债期货"}, headers=H(TOK_R)).json()
    assert [m["id"] for m in items_of(b2)] == [ids["A"]]
    assert "slow_match" not in b2 or b2["slow_match"] is False


def test_Q08_multi_word_and(rig, client):
    """02 §2.8.6(R6-52 定死):多词 AND;≥3 字词走 FTS、<3 字词各自 LIKE、两组再 AND;有 LIKE 词即 `slow_match=true`。"""
    ids = seed_messages(rig)

    def q(s):
        b = client.get(f"{P}/messages", params={"q": s}, headers=H(TOK_R)).json()
        return {m["id"] for m in items_of(b)}, bool(b.get("slow_match"))

    assert q("国债期货 收益率") == ({ids["A"]}, False)     # 两词都 ≥3 字:纯 FTS AND,不出 slow_match
    assert q("国债期货 上行") == ({ids["A"]}, True)        # 混合:国债期货 FTS + 上行(2 字)LIKE
    assert q("国债期货 债券") == (set(), True)             # 混合且 AND 无交集:结果空,但走过 LIKE 仍出 slow_match
    assert q("收益率 债券") == ({ids["E"]}, True)          # 混合:收益率 FTS + 债券 LIKE
    assert q("收益率 期货") == ({ids["A"]}, True)


def test_Q09_limited_token_sees_only_allowed_accounts(rig, client):
    """02 §3.1 `allow_accounts_json` 收窄账号访问:不带 account_id 的全局列表也只回该令牌有权的账号。"""
    ids = seed_messages(rig)
    b = client.get(f"{P}/messages", headers=H(TOK_LIM)).json()
    got = {m["account_id"] for m in items_of(b)}
    assert got == {QD}
    assert ids["E"] not in {m["id"] for m in items_of(b)}


def test_Q10_get_single_message_and_404(rig, client):
    """02 #49:`GET /messages/{id}` 单条(与列表同一视图、逐字相同);不存在 → 404 TARGET_NOT_FOUND;越权账号的消息不可见(403/404)。"""
    ids = seed_messages(rig)
    r = client.get(f"{P}/messages/{ids['A']}", headers=H(TOK_R))
    assert r.status_code == 200
    single = r.json()["data"]
    listed = next(m for m in items_of(client.get(f"{P}/messages", params={"account_id": QD}, headers=H(TOK_R)).json()) if m["id"] == ids["A"])
    assert single == listed
    assert not (EVENT_ONLY_KEYS & set(single))
    nf = client.get(f"{P}/messages/msg_01ARZ3NDEKTSV4RRFFQ69G5FAV", headers=H(TOK_R))
    assert nf.status_code == 404
    assert_error_envelope(nf.json(), "TARGET_NOT_FOUND")
    assert client.get(f"{P}/messages/{ids['E']}", headers=H(TOK_LIM)).status_code in (403, 404)


def test_Q11_sent_message_visible_with_trace(rig, client):
    """00 §7.4 + R6-52 ②:真实发送后的出向行经 #48 读回 `dir=out, state=DELIVERED, self=true, source=ui, confirmed_by=ingest_merge, trace_id=<该指令>`。"""
    rig.prime(client)
    res = rig.send(client, TOK_W, "k-q11", "读回视图").json()
    assert res["code"] == "DELIVERED"
    items = items_of(client.get(f"{P}/messages", params={"account_id": QD, "dir": "out"}, headers=H(TOK_R)).json())
    m = next(x for x in items if x["id"] == res["data"]["message_id"])
    assert m["state"] == "DELIVERED" and m["self"] is True and m["source"] == "ui"
    assert m["confirmed_by"] == "ingest_merge" and m["trace_id"] == res["trace_id"] and m["ext_msg_id"] == res["data"]["ext_msg_id"]
    assert m["text"] == "读回视图"


# ══════════════════════════════════════════════════════════════════════ 八、GET /sessions(02 #26)


def test_S01_sessions_global_list_and_filters(rig, client):
    """02 #26:全局会话列表(跨账号)`Session[]`,`?account_id=&keyword=&kind=`;元素含 id/account_id/channel/native_id/name/kind。"""
    seed_messages(rig)
    r = client.get(f"{P}/sessions", headers=H(TOK_R))
    assert r.status_code == 200
    data = r.json()["data"]
    by = {s["id"]: s for s in data}
    assert {QD_SESSION, QQ_SESSION} <= set(by)
    for s in data:
        assert {"id", "account_id", "channel", "native_id", "name", "kind"} <= set(s)
    assert by[QQ_SESSION]["kind"] == "group" and by[QQ_SESSION]["name"] == "测试群" and by[QQ_SESSION]["native_id"] == QQ_GROUP
    assert by[QD_SESSION]["kind"] == "private" and by[QD_SESSION]["account_id"] == QD
    assert [s["id"] for s in client.get(f"{P}/sessions", params={"account_id": QD}, headers=H(TOK_R)).json()["data"]] == [QD_SESSION]
    assert [s["id"] for s in client.get(f"{P}/sessions", params={"kind": "group"}, headers=H(TOK_R)).json()["data"]] == [QQ_SESSION]
    assert [s["id"] for s in client.get(f"{P}/sessions", params={"keyword": "测试群"}, headers=H(TOK_R)).json()["data"]] == [QQ_SESSION]
    # allow_accounts 收窄
    assert [s["id"] for s in client.get(f"{P}/sessions", headers=H(TOK_LIM)).json()["data"]] == [QD_SESSION]


# ══════════════════════════════════════════════════════════════════════ 九、审计(02 §2.2.1;#95;§3.1 audit_log;R6-52 ⑥)


def test_L01_api_calls_are_audited(rig, client):
    """02 §2.2.1:api 把每次调用记 `audit_log`;§3.1 audit_log:`kind='api'`、`actor = token:console | app:xxx`、`action = 'METHOD path'`;
    #95 `GET /audit?kind=api`(A 级可看全部 actor)。
    行键:#95 JSON 行键集 02 没写死,唯一逐字给出的列名是 R6-58 (ag) CSV 十列;取其中非时间的五列断言。
    ⚠️ 旧版还断了 `ts_ms`:那是库列/CSV 列名,JSON 出参按 00 §6「时间(API)= ISO 8601 带偏移」不该下发毫秒键
    (R6-62 (f) 只给 Account 的两个键开了例外)⇒ 删去;JSON 里时间键叫什么规格空白,不断。"""
    assert client.get(f"{P}/accounts", headers=H(TOK_W)).status_code == 200
    assert client.get(f"{P}/accounts/{QD}", headers=H(TOK_ADMIN)).status_code == 200
    r = client.get(f"{P}/audit", params={"kind": "api"}, headers=H(TOK_ADMIN))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data and all(row["kind"] == "api" for row in data)
    for row in data:
        assert {"kind", "transport", "actor", "action", "result_code"} <= set(row)
    assert any(row["action"] == f"GET {P}/accounts" and row["actor"] == "app:bot_w" for row in data), data
    assert any(row["action"] == f"GET {P}/accounts/{QD}" and row["actor"] == "token:console" for row in data), data


def test_L02_health_unauth_summary_not_audited(rig, client):
    """R6-52 ⑥:`/system/health` 免鉴权摘要不记审计(H01 探活每几秒一次)。"""
    before = count(rig.store, "SELECT COUNT(*) FROM audit_log WHERE action LIKE '%/system/health'")
    for _ in range(3):
        assert client.get(f"{P}/system/health").status_code == 200
    assert count(rig.store, "SELECT COUNT(*) FROM audit_log WHERE action LIKE '%/system/health'") == before == 0


def test_L03_non_admin_sees_only_own_audit_rows(rig, client):
    """02 #95:R 级只看自己的行,A 级可看全部 actor。"""
    assert client.get(f"{P}/accounts", headers=H(TOK_ADMIN)).status_code == 200
    assert client.get(f"{P}/accounts", headers=H(TOK_W)).status_code == 200
    mine = client.get(f"{P}/audit", params={"kind": "api"}, headers=H(TOK_W)).json()["data"]
    assert mine and all(row["actor"] == "app:bot_w" for row in mine)
    everyone = client.get(f"{P}/audit", params={"kind": "api"}, headers=H(TOK_ADMIN)).json()["data"]
    assert {row["actor"] for row in everyone} >= {"app:bot_w", "token:console"}


def test_L04_write_endpoint_audit_row_has_account_and_trace(rig, client):
    """02 §3.4 通用:所有写端点记 audit_log;§3.1 audit_log 列 `account_id`/`trace_id`/`result_code`;#95 `?action=` 过滤。"""
    rig.prime(client)
    res = rig.send(client, TOK_W, "k-l04", "审计").json()
    assert res["code"] == "DELIVERED"
    action = f"POST {P}/accounts/{QD}/send"
    data = client.get(f"{P}/audit", params={"kind": "api", "action": action}, headers=H(TOK_ADMIN)).json()["data"]
    assert len(data) == 1
    row = data[0]
    assert row["account_id"] == QD and row["trace_id"] == res["trace_id"] and row["actor"] == "app:bot_w"
    assert row["result_code"] is not None


def test_L05_rejected_calls_still_audited(rig, client):
    """02 §2.2.1「每次调用」含被拒的调用:401/403 也记一行(actor 无法识别时仍有行、result_code 记 HTTP 状态)。"""
    assert client.get(f"{P}/accounts").status_code == 401
    assert client.get(f"{P}/accounts/{QQ}", headers=H(TOK_LIM)).status_code == 403
    codes = [r[0] for r in rows(rig.store, "SELECT result_code FROM audit_log WHERE kind='api' AND action LIKE 'GET %/accounts%' ORDER BY id")]
    assert "401" in codes and "403" in codes, codes


# ══════════════════════════════════════════════════════════════════════ 十、WS /api/v1/events(02 §3.4.7;00 §7.5;01 §2.8;R6-52 ③)


def _emit_three(client, rig) -> list[int]:
    """依次 emit:qd01 message、qq03 alert、qd01 account_state;返回三条 seq。"""
    s1 = emit(client, rig.agent, "message", payload={"id": "msg_x", "text": "hi"}, account_id=QD, channel="qidian", trace_id=None)
    s2 = emit(client, rig.agent, "alert", payload={"code": "H06_ADB_OFFLINE", "severity": "warn", "state": "firing", "subject": f"account:{QQ}"},
              account_id=QQ, channel="qq")
    s3 = emit(client, rig.agent, "account_state", payload={"state": "running", "error_since_ms": None}, account_id=QD, channel="qidian")
    return [s1, s2, s3]


def _real_ws_close_code(app, path: str, *, first_frame: str | None = None, timeout_s: float = 15.0):
    """用**真 WebSocket 客户端**(uvicorn + websockets)连一次,返回服务端关闭帧里的 close code。

    🔴 为什么不能只用 TestClient:内存传输层里「accept 前 close」与「accept 后 close」都会被 Starlette
    包成同一个 `WebSocketDisconnect(code=…)`,两者分不出来;而 R6-62 (a) 要的恰恰是这个区别 ——
    只有先 `accept()` 把握手做完,关闭码才真的过得了线;在 accept 之前 close,ASGI 服务端会退化成
    「拒绝握手」,线上客户端只看得到 `1006`。

    返回:`("closed", code)` = 握手成功、收到带码的关闭帧;`("handshake_rejected", status)` = 握手就被拒(⇒ 线上是 1006)。
    """
    import uvicorn
    import websockets

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", ws="websockets", lifespan="off")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        deadline = time.time() + timeout_s
        while not server.started:
            assert time.time() < deadline, "uvicorn 没起来"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]

        async def go():
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}{path}", open_timeout=10) as ws:
                    if first_frame is not None:
                        await ws.send(first_frame)
                    await ws.recv()
                return ("closed", 1000)
            except websockets.exceptions.ConnectionClosed as e:
                rcvd = getattr(e, "rcvd", None)
                return ("closed", rcvd.code if rcvd is not None else 1006)
            except websockets.exceptions.InvalidStatus as e:            # 握手阶段就被拒
                return ("handshake_rejected", e.response.status_code)
            except websockets.exceptions.InvalidHandshake as e:         # 其它握手失败
                return ("handshake_rejected", repr(e))

        return asyncio.run(go())
    finally:
        server.should_exit = True
        th.join(timeout=10)


def test_W01_no_or_bad_token_closed_4401(rig, client):
    """02 §3.4.7 **R6-62 (a)**:握手 `?token=`/`Authorization`;无令牌或令牌无效 →
    服务端**先 `accept()` 把握手做完,再 `close(code=4401, reason)`**。

    🔴 本条断言按 **R6-62 (a) 裁决**改写。原文(R6-52)写的是「`accept` **前**直接关闭 + 关闭码 `4401`」,
    该句**自相矛盾、已作废**:WebSocket 的关闭码是**关闭帧**里的字段,握手没完成就没有连接、也就没有帧可发,
    ASGI 服务端会把它退化成「拒绝握手」,客户端只看得到 `1006` ⇒ 控制台按关闭码分诊(01 §5.1)那条路恒不触发。
    原用例用 `pytest.raises(WebSocketDisconnect)` 断「握手被拒」,断的正是那个矛盾的旧句。

    分两层验:
    ① **真 WebSocket 客户端**(uvicorn + `websockets`,非 TestClient)—— 只有它能区分「握手被拒(线上 1006)」
       和「握手成功后收到 4401 关闭帧」,这正是本裁决的要害;
    ② TestClient 层顺带守住「能进得了 `with` 块」= 握手确实完成了。
    """
    # ① 真客户端:不带令牌 / 令牌无效,都必须是「握手成功 + 关闭码 4401」
    for path, what in ((f"{P}/events", "不带令牌"), (f"{P}/events?token=bad-token", "令牌无效")):
        kind, code = _real_ws_close_code(rig.api, path)
        assert kind == "closed", f"{what}:握手就被拒了({code}) ⇒ 线上客户端只会看到 1006,拿不到 4401"
        assert code == 4401, f"{what}:关闭码是 {code},规格要求 4401"

    # ② TestClient 层:**进得了 `with` 块**本身就说明握手做完了(accept 前 close 会在这一行抛);
    #    随后收到的第一帧是带 4401 的 close 帧。
    for path, what in ((f"{P}/events", "不带令牌"), (f"{P}/events?token=bad-token", "令牌无效")):
        with client.websocket_connect(path) as ws:
            frame = ws.receive()
        assert frame["type"] == "websocket.close", f"{what}:第一帧不是关闭帧:{frame}"
        assert frame["code"] == 4401, f"{what}:关闭码是 {frame.get('code')},规格要求 4401"


def test_W02_bad_first_frame_closed_4400(rig, client):
    """02 §3.4.7(R6-52 ③):首帧 10 s 内没收到合法订阅 JSON → 关闭码 4400。"""
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_text("this is not json")
        with pytest.raises(WebSocketDisconnect) as ei:
            ws_recv(ws, timeout=5)
    assert ei.value.code == 4400


def test_W03_since_seq_replays_all_in_order(rig, client):
    """02 §3.4.7 / 00 §7.5 / 01 §2.8:首帧订阅带 `since_seq` → 从 events_outbox(target='ws') 重放,每帧一个 Event + `seq`,`seq` 单调递增、
    顺序与 emit 一致;帧键 `{event, ts, trace_id, account_id, channel, seq, payload}`;Authorization 头握手同样可用。"""
    seqs = _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events", headers=H(TOK_ADMIN)) as ws:
        ws.send_json({"subscribe": {"events": [], "accounts": ["*"], "channels": [], "since_seq": 0}})
        frames = [ws_recv(ws) for _ in range(3)]
    assert [f["seq"] for f in frames] == seqs and seqs == sorted(seqs)
    assert [f["event"] for f in frames] == ["message", "alert", "account_state"]
    for f in frames:
        assert EVENT_FRAME_KEYS <= set(f), EVENT_FRAME_KEYS - set(f)
    assert frames[0]["account_id"] == QD and frames[0]["channel"] == "qidian" and frames[0]["payload"]["id"] == "msg_x"
    assert frames[1]["account_id"] == QQ and frames[1]["payload"]["code"] == "H06_ADB_OFFLINE"


def test_W04_frame_ts_is_iso8601(rig, client):
    """00 §6 时间(API/事件/邮件)= ISO 8601 带时区偏移(+08:00);00 §7.5 Event 帧顶层 `ts`。"""
    _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"since_seq": 0}})
        f = ws_recv(ws)
    assert isinstance(f["ts"], str) and ISO_RE.match(f["ts"]), f["ts"]


def test_W05_events_filter(rig, client):
    """02 §3.4.7 / 00 §7.5:首帧订阅过滤按事件类型 —— `events:["alert"]` 只收 alert。"""
    seqs = _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"events": ["alert"], "accounts": ["*"], "since_seq": 0}})
        f = ws_recv(ws)
        assert f["event"] == "alert" and f["seq"] == seqs[1]
        # 哨兵:再 emit 一个 alert,下一帧必须是它(证明 message/account_state 被过滤,不是延迟)
        s4 = emit(client, rig.agent, "alert", payload={"code": "X", "severity": "info", "state": "firing", "subject": "host"})
        f2 = ws_recv(ws)
        assert f2["event"] == "alert" and f2["seq"] == s4


def test_W06_accounts_filter(rig, client):
    """02 §3.4.7 / 00 §7.5:订阅过滤按账号 —— `accounts:["qq03"]` 只收 qq03 的事件。"""
    seqs = _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"accounts": [QQ], "since_seq": 0}})
        f = ws_recv(ws)
        assert f["account_id"] == QQ and f["seq"] == seqs[1]
        s4 = emit(client, rig.agent, "message", payload={"id": "m2"}, account_id=QQ, channel="qq")
        f2 = ws_recv(ws)
        assert f2["seq"] == s4 and f2["account_id"] == QQ


def test_W07_channels_filter(rig, client):
    """02 §3.4.7:订阅示例 `channels:["qq"]` —— 只收 channel=qq 的事件。"""
    seqs = _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"channels": ["qq"], "since_seq": 0}})
        f = ws_recv(ws)
        assert f["channel"] == "qq" and f["seq"] == seqs[1]
        s4 = emit(client, rig.agent, "alert", payload={"code": "Y"}, account_id=QQ, channel="qq")
        assert ws_recv(ws)["seq"] == s4


def test_W08_allow_accounts_second_narrowing(rig, client):
    """02 §3.4.7:过滤按 `api_clients.allow_accounts_json` 二次收窄(订阅 `*` 也只给它有权的账号)。"""
    seqs = _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_LIM}") as ws:
        ws.send_json({"subscribe": {"accounts": ["*"], "since_seq": 0}})
        f1, f2 = ws_recv(ws), ws_recv(ws)
        assert [f1["seq"], f2["seq"]] == [seqs[0], seqs[2]]
        assert f1["account_id"] == QD and f2["account_id"] == QD
        s4 = emit(client, rig.agent, "message", payload={"id": "m-qq"}, account_id=QQ, channel="qq")
        s5 = emit(client, rig.agent, "message", payload={"id": "m-qd"}, account_id=QD, channel="qidian")
        f3 = ws_recv(ws)
        assert f3["seq"] == s5 and f3["account_id"] == QD and s4 < s5


def test_W09_live_events_without_since_seq(rig, client):
    """02 §3.4.7(R6-52 ③):不带 `since_seq` = 只收订阅之后的新事件(不重放);新事件实时推到。"""
    _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"events": [], "accounts": ["*"]}})
        s_new = emit(client, rig.agent, "alert", payload={"code": "NEW"}, account_id=QD, channel="qidian")
        f = ws_recv(ws)
        assert f["seq"] == s_new and f["payload"]["code"] == "NEW"


def test_W10_resubscribe_frame_narrows_filter(rig, client):
    """01 §2.8 首段 / 02 §3.4.7(R6-52 ③):客户端随时可重发订阅帧收窄过滤(P-MSG 不在前台时收窄 message)。"""
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"events": [], "accounts": ["*"]}})
        s1 = emit(client, rig.agent, "message", payload={"id": "m1"}, account_id=QD, channel="qidian")
        assert ws_recv(ws)["seq"] == s1
        ws.send_json({"subscribe": {"events": ["alert"], "accounts": ["*"]}})
        time.sleep(0.6)                      # 让服务端处理完重订阅帧再产生新事件
        emit(client, rig.agent, "message", payload={"id": "m2"}, account_id=QD, channel="qidian")
        s3 = emit(client, rig.agent, "alert", payload={"code": "A1"}, account_id=QD, channel="qidian")
        f = ws_recv(ws)
        assert f["event"] == "alert" and f["seq"] == s3


def test_W11_since_seq_older_than_retention_truncated(rig, client):
    """02 §3.4.7(R6-52 ③)/ §2.2.7 / §7.1 [events] ws_retention_hours:`since_seq` 早于当前保留的最小 seq(ws 行已按保留期清理)→
    首帧 `{"replay":"truncated","from_seq":<最小 seq>}`,随后从该 seq 起推。"""
    old1 = emit(client, rig.agent, "alert", payload={"code": "OLD1"}, account_id=QD, channel="qidian")
    old2 = emit(client, rig.agent, "alert", payload={"code": "OLD2"}, account_id=QD, channel="qidian")
    rig.clock.advance(73 * 3600 * 1000)      # 超过默认保留期 72 h
    new1 = emit(client, rig.agent, "alert", payload={"code": "NEW1"}, account_id=QD, channel="qidian")
    new2 = emit(client, rig.agent, "alert", payload={"code": "NEW2"}, account_id=QD, channel="qidian")
    cutoff = rig.clock.now_ms - rig.cfg.events.ws_retention_hours * 3600 * 1000
    assert rig.store.purge_outbox_ws(cutoff) == 2
    assert rig.store.outbox_seq_bounds() == (new1, new2)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"since_seq": old1 - 1}})
        first = ws_recv(ws)
        assert first == {"replay": "truncated", "from_seq": new1}
        f1, f2 = ws_recv(ws), ws_recv(ws)
        assert [f1["seq"], f2["seq"]] == [new1, new2]
        assert f1["payload"]["code"] == "NEW1"
    assert old2 < new1


def test_W12_reconnect_with_last_seq_gets_only_newer(rig, client):
    """00 §7.5 G-08 / 01 §2.8:重连一律带 `since_seq`(= 已收到的 last_seq),只重放其后的、不按 ts 丢弃;按 seq 去重。"""
    seqs = _emit_three(client, rig)
    with client.websocket_connect(f"{P}/events?token={TOK_R}") as ws:
        ws.send_json({"subscribe": {"since_seq": seqs[1]}})
        f = ws_recv(ws)
        assert f["seq"] == seqs[2]
        s4 = emit(client, rig.agent, "alert", payload={"code": "Z"}, account_id=QD, channel="qidian")
        assert ws_recv(ws)["seq"] == s4


# ══════════════════════════════════════════════════════════════════════ 十一、scheduler(02 §2.2.11;R6-52 ⑤)


async def test_T01_interval_execution():
    """02 §2.2.11:`register(name, interval, fn)`,每任务一个 task,按间隔周期执行;`stop` 后不再跑。"""
    s = Scheduler()
    n = 0

    async def tick():
        nonlocal n
        n += 1

    s.register("tick", 0.05, tick)
    await s.start()
    await asyncio.sleep(0.4)
    await s.stop()
    snap = s.snapshot()["tick"]
    assert n >= 3 and snap["runs"] == n and snap["errors"] == 0
    frozen = n
    await asyncio.sleep(0.15)
    assert n == frozen


async def test_T02_trigger_runs_immediately():
    """02 §2.2.11:`trigger(name)` 立即触发一轮(不等间隔)。"""
    s = Scheduler()
    n = 0

    async def job():
        nonlocal n
        n += 1

    s.register("job", 1000, job)
    await s.start()
    await asyncio.sleep(0.05)
    assert n == 0
    s.trigger("job")
    await asyncio.sleep(0.1)
    assert n == 1 and s.snapshot()["job"]["runs"] == 1
    await s.stop()


async def test_T03_slow_task_skipped_not_reentered():
    """02 §2.2.11 / R6-52 ⑤:同名任务不重入 —— 上一轮没跑完就到点(或被 trigger)⇒ 跳过本轮并计数 `skipped`,任一时刻并发 ≤ 1。"""
    s = Scheduler()
    active = 0
    max_active = 0

    async def slow():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.3)
        finally:
            active -= 1

    s.register("slow", 0.05, slow, run_immediately=True)
    await s.start()
    await asyncio.sleep(0.2)
    snap = s.snapshot()["slow"]
    assert max_active == 1
    assert snap["skipped"] >= 1 and snap["runs"] == 0
    before = snap["skipped"]
    s.trigger("slow")
    await asyncio.sleep(0.05)
    assert s.snapshot()["slow"]["skipped"] >= before + 1
    await asyncio.sleep(0.3)
    assert s.snapshot()["slow"]["runs"] >= 1 and max_active == 1
    await s.stop()


async def test_T04_exception_counted_task_survives():
    """02 §2.2.11 每任务一个 task:单轮异常记 `errors`/`last_error`,任务不死、下一轮照跑。"""
    s = Scheduler()
    n = 0

    async def flaky():
        nonlocal n
        n += 1
        if n == 1:
            raise RuntimeError("boom")

    s.register("flaky", 0.05, flaky, run_immediately=True)
    await s.start()
    await asyncio.sleep(0.4)
    await s.stop()
    snap = s.snapshot()["flaky"]
    assert snap["errors"] == 1 and "boom" in (snap["last_error"] or "")
    assert snap["runs"] >= 2 and n == snap["runs"] + 1


async def test_T05_duplicate_register_rejected():
    """02 §2.2.11「同名任务不重入」的前提:同名不能注册两次(规格未写异常类型,只断言拒绝)。"""
    s = Scheduler()

    async def a():
        pass

    s.register("dup", 1, a)
    with pytest.raises(Exception):
        s.register("dup", 2, a)
    assert set(s.snapshot()) == {"dup"} and s.snapshot()["dup"]["interval_s"] == 1


async def test_T06_stop_cancels_running_round():
    """R6-52 ⑤:计时循环与执行分离,`stop` 同时取消两者 —— 正在跑的一轮被取消、之后不再有新轮。"""
    s = Scheduler()
    active = 0
    cancelled = False

    async def slow():
        nonlocal active, cancelled
        active += 1
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            active -= 1

    s.register("slow", 0.05, slow, run_immediately=True)
    await s.start()
    await asyncio.sleep(0.1)
    assert active == 1
    await s.stop()
    assert active == 0 and cancelled
    runs = s.snapshot()["slow"]["runs"]
    await asyncio.sleep(0.15)
    assert s.snapshot()["slow"]["runs"] == runs


async def test_T07_run_once_reports_skip_when_running():
    """02 §2.2.11 不重入的同步观察面:`run_once(name)` 在该任务正在跑时不重入、返回 False 并 `skipped+1`。"""
    s = Scheduler()
    gate = asyncio.Event()

    async def wait_gate():
        await gate.wait()

    s.register("g", 1000, wait_gate)
    t = asyncio.create_task(s.run_once("g"))
    await asyncio.sleep(0.02)
    assert await s.run_once("g") is False
    assert s.snapshot()["g"]["skipped"] == 1
    gate.set()
    assert await t is True
    assert s.snapshot()["g"]["runs"] == 1
