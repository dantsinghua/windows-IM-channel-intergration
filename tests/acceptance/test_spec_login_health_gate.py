"""按设计文档写的独立验收用例 —— 第四批:登录门与安全闸 / #12~#16b 登录与凭据 / 掉线检测 / H04·H05·H06 / E-19 内存水位 / #20·#22·#23 / health。

🔴 断言只依据规格,不按实现反推;用例失败一律保留给分诊(实现缺陷 / 用例误读 / 规格问题)。规格出处(册 §节 / 端点编号 / 裁决号):
- 02 §2.2.2 bus 七段流水(鉴权→限流→幂等→安全闸→路由→执行→审计)、登录门(D-2 / R-06 / B-30 / R6-51:LOGIN_REQUIRED 留痕
  status='failed'+started_ms IS NULL、不排队、不写 idempotency、不写 SENDING 行)、P-11 args_json 不存正文、总线与适配器都不自动重登;
  §2.2.5 E-19 运行期内存水位硬保护(warn 告警 / critical 阻断 #2 与 recover() + LRU 建议名单 / 只建议不自动停 / auto_stop_on_pressure);
  §3.4.1 #12 login、#13 PUT credential、#14 DELETE credential、#15 prompt、#16b login/cancel、#20 capabilities、#22 settings、#23 batch;
  §3.7 告警码表(H04 crit / H05 crit / H06 warn→crit / CONTAINER_OOM_KILLED / MEM_PRESSURE / AUTO_RESTART_EXHAUSTED);
  §5 失败与自愈表(容器意外退出 / 掉线不自动重登 / 企点拿不到 root 仍 running / 内存水位 / 单容器 OOM);§6 安全(login 类 secret 擦 "***"、
  审计不含正文与密码、出口词表 settings gate.blocklist 热更);§7.1 [bus] gate_blocklist_ref = "settings:gate.blocklist"。
- 00 §7.5 告警类事件统一 payload {code, severity, state, subject, …, evidence} 与 account_state payload(prompt / login_session_id);
  §8.1 状态机与 login_required 可做集合(R-06)、state_code 三组;§10 API 基线(错误信封 / HTTP 映射);§11.3 [GATE]。
- 04 §2.3 H04(退避 1/2/5/10 min、5 次上限、OOMKilled 先推 CONTAINER_OOM_KILLED)、H05(稳态 1→非 1 crit)、
  H06(R6-32:(a) 连接态 disconnect+connect→ensure_root、h06_fail_streak 三振 crit、宽限窗;(b) root 态恒 warn、不并入 (a)、绝不 kill-server);
  §7 [health] container_check_s=10 / adb_check_s=30 / adb_root_grace_s=15 / container_restart_backoff_s / container_restart_max=5;
  [monitor] mem_warn_mb=2048 / mem_critical_mb=1024 / auto_stop_on_pressure=false;F-04 / F-33 / F-34。
- 05 §2.0 公共约定(state_code 三组、登录阶段界定、login_session_id 格式 ls_+ULID 与生命周期、凭据不落 WSL、保险库账密何时用 D-2);
  §2.2.7 「不保存」登录流程;§2.5.2 内存压力下的账号处置;§2.5.4 掉线检测三件事 + ACCOUNT_OFFLINE 口径 + login_remind_interval_s;
  §2.5.5 账号级设置表;§7 [accounts] login_remind_interval_s=300。

夹具全部来自 tests/conftest.py 的 make_rig(全假后端:FakeContainers / FakeAdb / FakeVault / FakeWinAgent / FakeFs),绝不碰真 docker/adb/WinAgent。
HTTP 用例走 starlette TestClient;凡用了 TestClient 的用例,后台协程一律经 client.portal.call 在同一事件循环里跑;纯服务层用例用 async def 直接 await。
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import re
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Optional

import pytest
from starlette.testclient import TestClient

from qtrade_agent.alerts import (
    ACCOUNT_OFFLINE,
    CONTAINER_OOM_KILLED,
    H04_CONTAINER_EXITED,
    H05_BOOT_INCOMPLETE,
    H06_ADB_OFFLINE,
    MEM_PRESSURE,
    QIDIAN_NOT_ROOT,
)
from qtrade_agent.config import AccountsConfig, AgentConfig, HealthConfig, PoolConfig
from qtrade_agent.models import Command, CommandOrigin, Message, Session

try:
    from tests.conftest import Clock, make_rig
except ImportError:                                   # pytest 以 rootdir 载入 conftest 时的别名
    from conftest import Clock, make_rig              # type: ignore

# ────────────────────────────────────────────────────────────────────── 常量(规格自抄)
P = "/api/v1"                                          # 00 §10 前缀
TOK_A = "tok-a"                                        # admin 令牌
SERIAL_01 = "127.0.0.1:16001"                          # 00 §3:企点 adb 串 = 127.0.0.1:(16000+NN)
SERIAL_02 = "127.0.0.1:16002"
CNAME_01 = "qtrade-qd01"                               # 容器名口径(brief)
CNAME_02 = "qtrade-qd02"
SECRET = "p@ss-w0rd-独一无二-9f3a"                       # 用作「密码不得落库」的探针字符串
LS_RE = re.compile(r"^ls_[0-9A-HJKMNP-TV-Z]{26}$")      # 05 §2.0:login_session_id = ls_ + ULID(Crockford base32 26 位)
OFFLINE_CODES = ("KICKED", "LOGGED_OUT", "TOKEN_EXPIRED", "LOGIN_TIMEOUT")   # 05 §2.0 掉线原因组(共 4)
WAIT_CODES = ("WAIT_SMS", "WAIT_CAPTCHA", "WAIT_DEVICE_CONFIRM", "WAIT_QRCODE")  # 05 §2.0 等人组的子集
AUTO_RESTART_EXHAUSTED = "AUTO_RESTART_EXHAUSTED"      # 02 §3.7 登记的码(每小时自动重启 ≥5 次停止自愈)
TRI_STATE = {"ok", "firing", "unknown"}                # /system/health.checks 三态(brief 只说「三态」;给定规格段落未钉词汇,取值按实测词汇,见报告疑点)


# ────────────────────────────────────────────────────────────────────── 通用小工具
def _j(x: Any) -> Any:
    """列值兼容:JSON 字符串 → 对象;已是对象则原样。"""
    if x is None or isinstance(x, (dict, list, int, float, bool)):
        return x
    try:
        return json.loads(x)
    except Exception:
        return x


def _attr(o: Any, name: str, default: Any = None) -> Any:
    if o is None:
        return default
    if isinstance(o, dict):
        return o.get(name, default)
    return getattr(o, name, default)


def _data(r) -> Any:
    j = r.json()
    return j["data"] if isinstance(j, dict) and "data" in j else j


def H(tok: Optional[str] = TOK_A) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _row_id(row: Any) -> Any:
    if isinstance(row, dict):
        return row.get("id")
    return getattr(row, "id", row)


async def _call(fn: Callable, *a, **k):
    """同步/异步两可的服务方法统一调用。"""
    r = fn(*a, **k)
    if inspect.isawaitable(r):
        r = await r
    return r


async def _wait_idle(rig, aid: str, timeout_s: float = 20.0) -> None:
    """等该账号的后台序列结束。login_cancel 之后 wait_idle 会把被取消任务的 CancelledError 冒给等待者(等待者自身并未被取消),
    此时退化为轮询 busy()。"""
    try:
        r = rig.agent.accounts.wait_idle(aid)
        if inspect.isawaitable(r):
            await asyncio.wait_for(r, timeout_s)
    except asyncio.CancelledError:
        pass
    t0 = time.monotonic()
    while rig.agent.accounts.busy(aid) and time.monotonic() - t0 < timeout_s:
        await asyncio.sleep(0.01)


def wait_idle(client, rig, aid: str) -> None:
    client.portal.call(_wait_idle, rig, aid)


def wait_for(pred: Callable[[], bool], timeout_s: float = 8.0, step_s: float = 0.02) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return True
        time.sleep(step_s)
    return bool(pred())


def full(rig, aid: str) -> dict[str, Any]:
    row = rig.store.get_account_full(aid)
    assert row is not None, f"账号 {aid} 不存在"
    return row


def state_of(rig, aid: str) -> tuple[str, Optional[str]]:
    row = full(rig, aid)
    return row["state"], row.get("state_code")


def settings_of(rig, aid: str) -> dict[str, Any]:
    return _j(full(rig, aid).get("settings_json")) or {}


def _payload(e: dict[str, Any]) -> dict[str, Any]:
    return _j(e.get("payload") if "payload" in e else e.get("payload_json")) or {}


def state_events(rig, aid: str) -> list[dict[str, Any]]:
    return rig.store.list_events(event="account_state", account_id=aid)


def last_state_event(rig, aid: str) -> Optional[dict[str, Any]]:
    evs = state_events(rig, aid)
    return evs[-1] if evs else None


def last_ls(rig, aid: str) -> Optional[str]:
    e = last_state_event(rig, aid)
    return _payload(e).get("login_session_id") if e else None


def alert_events(rig, code: Optional[str] = None, subject: Optional[str] = None) -> list[dict[str, Any]]:
    """告警类事件(00 §7.5):alert 与 resource 两种 event 都可能承载告警 payload。"""
    out = []
    for ev in ("alert", "resource"):
        for e in rig.store.list_events(event=ev):
            p = _payload(e)
            if code and p.get("code") != code:
                continue
            if subject and p.get("subject") != subject:
                continue
            out.append(p)
    return out


def active(rig, code: str, subject: str):
    return rig.agent.alerts.active.get((code, subject))


def firing(rig, code: str, subject: str) -> bool:
    return bool(rig.agent.alerts.is_firing(code, subject))


def table_has(store, table: str, needle: str) -> bool:
    rows = store.con.execute(f"SELECT * FROM {table}").fetchall()
    return any(needle in json.dumps(dict(r), default=str, ensure_ascii=False) for r in rows)


def out_rows(store) -> int:
    return int(store.con.execute("SELECT count(*) FROM messages WHERE dir='out'").fetchone()[0])


def count_calls(calls: list[tuple[str, str]], op: str, target: Optional[str] = None) -> int:
    return sum(1 for c in calls if c[0] == op and (target is None or c[1] == target))


# ────────────────────────────────────────────────────────────────────── 夹具
class LoginStub:
    """执行层桩(login_fn):记录每次被调 (row.id, account, secret);``result`` 可改;``hang_s``>0 模拟卡住(测 #16b 取消)。"""

    def __init__(self, result: Optional[str] = "running", hang_s: float = 0.0):
        self.calls: list[tuple[Any, Any, Any]] = []
        self.result = result
        self.hang_s = hang_s
        self.cancelled = 0

    async def __call__(self, row, account, secret):
        self.calls.append((_row_id(row), account, secret))
        if self.hang_s:
            try:
                await asyncio.sleep(self.hang_s)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return self.result


async def login_ok(row, account, secret):
    return "running"


@contextmanager
def rig_ctx(tmp_path, **kw):
    r = make_rig(tmp_path, **kw)
    try:
        yield r
    finally:
        r.store.close()


@contextmanager
def api_ctx(rig):
    rig.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_A)
    with TestClient(rig.agent.create_api(), client=("127.0.0.1", 40000)) as c:
        yield c


def create(client, channel: str = "qidian", label: Optional[str] = None, *, account: Optional[str] = "u1", secret: Optional[str] = None,
           remember: Optional[bool] = None, key: Optional[str] = None, mode: Optional[str] = None):
    """02 #2:`{channel, label, login:{mode, account?, secret?, remember?}, idempotency_key}`。"""
    login: dict[str, Any] = {"mode": mode or ("password" if channel == "qidian" else "qrcode")}
    if account is not None:
        login["account"] = account
    if secret is not None:
        login["secret"] = secret
    if remember is not None:
        login["remember"] = remember
    body = {"channel": channel, "label": label or f"{channel}-{uuid.uuid4().hex[:6]}", "login": login, "idempotency_key": key or f"k-{uuid.uuid4().hex[:12]}"}
    return client.post(f"{P}/accounts", json=body, headers=H())


def created_id(resp) -> str:
    assert resp.status_code == 201, resp.text
    return _data(resp)["id"]


def start(client, rig, aid: str) -> None:
    r = client.post(f"{P}/accounts/{aid}/start", headers=H())
    assert r.status_code in (200, 202), r.text
    wait_idle(client, rig, aid)


def up_running(client, rig, *, secret: Optional[str] = SECRET, remember: Optional[bool] = True, label: Optional[str] = None) -> str:
    """HTTP 全流程把一个企点账号带到 running(需要 make_rig(login_fn=...) 返回 'running')。"""
    aid = created_id(create(client, "qidian", label, secret=secret, remember=remember))
    start(client, rig, aid)
    assert state_of(rig, aid)[0] == "running", state_of(rig, aid)
    return aid


def to_login_required(client, rig, label: Optional[str] = None) -> str:
    """05 §2.2.7:不带密码新建企点 → start → login_required(WAIT_PASSWORD)。"""
    aid = created_id(create(client, "qidian", label, secret=None, remember=None))
    start(client, rig, aid)
    assert state_of(rig, aid) == ("login_required", "WAIT_PASSWORD"), state_of(rig, aid)
    return aid


def seed_account(rig, *, state: str = "running", state_code: Optional[str] = None, settings: Optional[dict[str, Any]] = None,
                 label: Optional[str] = None) -> str:
    """纯服务层用例:直接落库造一个企点账号并置状态(不发事件、不起容器)。"""
    row = rig.store.create_account(channel="qidian", label=label or f"企点-{uuid.uuid4().hex[:6]}", login_mode="password", quota_mb=2560, settings=settings)
    aid = row["id"]
    rig.store.transition(aid, state, state_code=state_code, desired_state="running")
    return aid


async def submit(rig, aid: str, op: str = "send_text", *, text: str = "你好", session: Optional[str] = None, key: Optional[str] = None,
                 args: Optional[dict[str, Any]] = None):
    a = args if args is not None else {"session": session or f"{aid}:415011447", "text": text}
    return await rig.agent.bus.submit(Command(account_id=aid, op=op, args=a, idempotency_key=key or f"k-{uuid.uuid4().hex[:8]}", origin=CommandOrigin()))


def ingest_in(rig, aid: str, native: str, n: int) -> None:
    rig.store.ingest(Message(account_id=aid, channel="qidian", session=Session(aid, native, "private"), dir="in", type="text", text="x",
                             ts_ms=rig.clock.now_ms, source="qidian_db", ext_msg_id=f"qd:{aid}:{n}", sender_id=native))


def probe(client, rig, avail_mb: Optional[int] = None, *, offline: bool = False) -> None:
    if avail_mb is not None:
        rig.winagent.host["available_mb"] = avail_mb
    rig.winagent.offline = offline
    client.portal.call(rig.agent.winagent_probe)


# ══════════════════════════════════════════════════════════════════════ 一、登录门与安全闸(02 §2.2.2 七段流水)
async def test_login_gate_send_text_rejected_LOGIN_REQUIRED(tmp_path):
    """02 §2.2.2 登录门:state=login_required 时 IM 写类回 `LOGIN_REQUIRED`,`error.needs_human=true`、`error.retryable=false`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        res = await submit(rig, aid)
        assert res.ok is False
        assert res.code == "LOGIN_REQUIRED"
        assert res.error is not None and res.error.needs_human is True and res.error.retryable is False


async def test_login_gate_leaves_trace_failed_started_null(tmp_path):
    """02 §2.2.2 B-30/R6-51:登录门拒绝要留痕——`commands` 一行 `status='failed'`、`started_ms IS NULL`(没进队列)+ `command_results(code='LOGIN_REQUIRED')`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        res = await submit(rig, aid)
        cmd = rig.store.get_command(res.trace_id)
        assert cmd is not None, "登录门拒绝须在 commands 留痕"
        assert cmd["status"] == "failed"
        assert cmd["started_ms"] is None
        cr = rig.store.get_command_result(res.trace_id)
        assert cr is not None and cr["code"] == "LOGIN_REQUIRED"


async def test_login_gate_no_SENDING_row(tmp_path):
    """02 §2.2.2:登录门拒绝「不写 `SENDING` 行」——messages 里没有 dir='out' 的行。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        await submit(rig, aid)
        assert out_rows(rig.store) == 0


async def test_login_gate_no_idempotency_row(tmp_path):
    """02 §2.2.2:登录门拒绝「不写 `idempotency`」——同键不占位。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        await submit(rig, aid, key="k-gate-1")
        assert rig.store.idem_get(aid, "k-gate-1") is None


async def test_login_gate_precedes_safety_gate(tmp_path):
    """02 §2.2.2:登录门在「进队列之前」,先于七段中的安全闸——即便正文命中出口词表,登录阶段仍回 `LOGIN_REQUIRED` 而不是 `GATE_BLOCKED`。"""
    with rig_ctx(tmp_path) as rig:
        rig.store.settings_set("gate.blocklist", ["机密"])
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        res = await submit(rig, aid, text="这是机密")
        assert res.code == "LOGIN_REQUIRED"


async def test_login_gate_logging_in_also_rejected(tmp_path):
    """02 §2.2.2:`state ∈ {login_required, logging_in}` 都属登录阶段,`logging_in` 同样拒 IM 写类。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="logging_in")
        res = await submit(rig, aid)
        assert res.code == "LOGIN_REQUIRED"


@pytest.mark.parametrize("code", WAIT_CODES)
async def test_login_gate_wait_codes_rejected(tmp_path, code):
    """02 §2.2.2 / 05 §2.0:`WAIT_*` 一切验证环节都属登录阶段,IM 写类一律 `LOGIN_REQUIRED`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code=code)
        res = await submit(rig, aid)
        assert res.code == "LOGIN_REQUIRED"


@pytest.mark.parametrize("op", ["get_state", "screenshot"])
async def test_login_gate_allows_screen_ops(tmp_path, op):
    """00 §8.1 R-06 / 02 §2.2.2:`screenshot`、`get_state` 在 login_required 下允许——不得回 `LOGIN_REQUIRED`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        res = await submit(rig, aid, op, args={})
        assert res.code != "LOGIN_REQUIRED", res


async def test_login_gate_same_key_resend_after_login(tmp_path):
    """02 §2.2.2:登录门拒后「人登录后原键重发即可」——同键在 running 下不被幂等命中(不是 IDEMPOTENT_REPLAY),也不再是 LOGIN_REQUIRED。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        r1 = await submit(rig, aid, key="k-same")
        assert r1.code == "LOGIN_REQUIRED"
        rig.store.transition(aid, "running")
        r2 = await submit(rig, aid, key="k-same")
        assert r2.code not in ("LOGIN_REQUIRED", "IDEMPOTENT_REPLAY"), r2


async def test_running_send_passes_gate_to_executor(tmp_path):
    """00 §8.1:只有 running|degraded 接受写类;放行后到执行层(未接)的结果是 `SEND_FAILED`,不是任何闸门码。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running")
        res = await submit(rig, aid)
        assert res.code == "SEND_FAILED", res


async def test_allowlist_blocks_non_whitelisted_GATE_BLOCKED(tmp_path):
    """05 §2.5.5 `sessions.allowlist`:非白名单一律 `GATE_BLOCKED`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running", settings={"sessions": {"allowlist": [f"qd01:1"]}})
        res = await submit(rig, aid, session=f"{aid}:415011447")
        assert res.ok is False and res.code == "GATE_BLOCKED", res


async def test_allowlist_native_id_form_allows_and_others_blocked(tmp_path):
    """05 §2.5.5:白名单项可为「会话名或原生 ID」——原生 ID 命中放行(到执行层 SEND_FAILED),未命中 `GATE_BLOCKED`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running", settings={"sessions": {"allowlist": ["415011447"]}})
        ok = await submit(rig, aid, session=f"{aid}:415011447")
        assert ok.code == "SEND_FAILED", ok
        no = await submit(rig, aid, session=f"{aid}:9")
        assert no.code == "GATE_BLOCKED", no


async def test_allowlist_default_star_allows_all(tmp_path):
    """05 §2.5.5:`sessions.allowlist` 默认 `["*"]`——未配置时任何会话都不被 allowlist 拦。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running")
        res = await submit(rig, aid, session=f"{aid}:任意会话")
        assert res.code != "GATE_BLOCKED", res


async def test_allowlist_reads_unrestricted(tmp_path):
    """05 §2.5.5:allowlist「读不受限」——白名单为空集时 `get_state` 也不 `GATE_BLOCKED`。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running", settings={"sessions": {"allowlist": ["nobody"]}})
        res = await submit(rig, aid, "get_state", args={})
        assert res.code != "GATE_BLOCKED", res


async def test_blocklist_hot_reload_via_settings(tmp_path):
    """02 §6 / §7.1 `[bus] gate_blocklist_ref="settings:gate.blocklist"`:出口词表在 settings 表、热更——改 settings 立即生效、不重启;清空后同正文放行。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running")
        rig.store.settings_set("gate.blocklist", ["机密"])
        assert rig.store.settings_get("gate.blocklist") == ["机密"]
        r1 = await submit(rig, aid, text="含机密二字")
        assert r1.ok is False and r1.code == "GATE_BLOCKED", r1
        rig.store.settings_set("gate.blocklist", [])
        r2 = await submit(rig, aid, text="含机密二字")
        assert r2.code == "SEND_FAILED", r2


async def test_blocklist_added_word_takes_effect_without_restart(tmp_path):
    """02 §2.2.2 内部状态「安全闸词表(从 settings 加载,热更新)」:先放行的正文,加入词表后同一进程内立即被拦。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running")
        r1 = await submit(rig, aid, text="今天成交价")
        assert r1.code == "SEND_FAILED", r1
        rig.store.settings_set("gate.blocklist", ["成交价"])
        r2 = await submit(rig, aid, text="今天成交价")
        assert r2.code == "GATE_BLOCKED", r2


async def test_custom_gate_referenced_blocks(tmp_path):
    """05 §2.5.5 `gates.custom`:引用已注册的自定义闸名;闸返回 False ⇒ `GATE_BLOCKED`。"""
    with rig_ctx(tmp_path) as rig:
        rig.agent.gate.register("night_block", lambda row, op, args: False)
        aid = seed_account(rig, state="running", settings={"gates": {"custom": ["night_block"]}})
        res = await submit(rig, aid)
        assert res.code == "GATE_BLOCKED", res


async def test_custom_gate_not_referenced_does_not_apply(tmp_path):
    """05 §2.5.5:`gates.custom` 默认 `[]`——已注册但未被该账号引用的闸不生效。"""
    with rig_ctx(tmp_path) as rig:
        rig.agent.gate.register("night_block", lambda row, op, args: False)
        aid = seed_account(rig, state="running")
        res = await submit(rig, aid)
        assert res.code != "GATE_BLOCKED", res


async def test_custom_gate_sees_op_and_original_args(tmp_path):
    """02 §2.2.2 / §6:安全闸拿到的是脱敏前的内存对象(op 与原文),脱敏只发生在入库。"""
    with rig_ctx(tmp_path) as rig:
        seen: dict[str, Any] = {}

        def g(row, op, args):
            seen.update(op=op, args=dict(args), row_id=_row_id(row))
            return True

        rig.agent.gate.register("spy", g)
        aid = seed_account(rig, state="running", settings={"gates": {"custom": ["spy"]}})
        await submit(rig, aid, text="原文正文")
        assert seen.get("op") == "send_text"
        assert seen.get("args", {}).get("text") == "原文正文"
        assert seen.get("row_id") == aid


async def test_gate_blocked_trace_args_json_no_text(tmp_path):
    """02 §2.2.2 P-11 / §6:`commands.args_json` 不存正文——GATE_BLOCKED 留痕行里 `text` 被替换成 `{text_sha8, text_len}`。"""
    with rig_ctx(tmp_path) as rig:
        rig.store.settings_set("gate.blocklist", ["机密"])
        aid = seed_account(rig, state="running")
        text = "这是机密正文"
        res = await submit(rig, aid, text=text)
        assert res.code == "GATE_BLOCKED"
        cmd = rig.store.get_command(res.trace_id)
        assert cmd is not None, "GATE_BLOCKED 须在 commands 留痕"
        args = _j(cmd["args_json"])
        assert "text" not in args, args
        assert args.get("text_len") == len(text)
        assert isinstance(args.get("text_sha8"), str) and len(args["text_sha8"]) == 8
        assert not table_has(rig.store, "commands", text)


async def test_gate_blocked_no_SENDING_row(tmp_path):
    """02 §2.2.2 C-21:出向 SENDING 行是在「调用适配器之前」写的;被安全闸拦下的指令根本没到那一步,messages 无 out 行。"""
    with rig_ctx(tmp_path) as rig:
        rig.store.settings_set("gate.blocklist", ["机密"])
        aid = seed_account(rig, state="running")
        res = await submit(rig, aid, text="机密")
        assert res.code == "GATE_BLOCKED"
        assert out_rows(rig.store) == 0


async def test_passed_send_args_json_scrubbed_too(tmp_path):
    """02 §2.2.2 P-11:放行的 send_* 入库同样把 `args.text` 换成 `text_sha8+text_len`(总线一处统一做)。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="running")
        text = "放行正文-独一无二-7c1"
        res = await submit(rig, aid, text=text)
        cmd = rig.store.get_command(res.trace_id)
        assert cmd is not None
        assert "text" not in _j(cmd["args_json"])
        assert not table_has(rig.store, "commands", text)


async def test_login_op_secret_scrubbed_in_commands(tmp_path):
    """02 §6:`commands.args_json` 里若含密码(`login` 类 op)入库前擦成 `"***"`——commands 表任何行不含明文密码。"""
    with rig_ctx(tmp_path) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        res = await submit(rig, aid, "login", args={"secret": SECRET, "remember": False})
        assert not table_has(rig.store, "commands", SECRET)
        cmd = rig.store.get_command(res.trace_id)
        if cmd is not None and "secret" in (_j(cmd["args_json"]) or {}):
            assert _j(cmd["args_json"])["secret"] == "***"


# ══════════════════════════════════════════════════════════════════════ 二、#12 login(02 #12 / 05 §2.0 / §2.2.7 / §2.5.4)
def test_start_uses_vault_when_human_started(tmp_path):
    """05 §2.0「保险库账密何时用」:人发起的 `start` 自动填入 Vault 账密——login_fn 收到的 secret 就是保存的那份,且 Vault 有读记录。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig, secret=SECRET, remember=True)
        assert stub.calls and stub.calls[-1][0] == aid and stub.calls[-1][2] == SECRET
        assert len(rig.vault.reads) >= 1


def test_remember_default_false_no_vault_write(tmp_path):
    """02 #12 `remember` 默认 false / 05 §2.0:不勾保存 ⇒ 不落 Vault、`accounts.remember=0`、`credential_ref` 为空。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian", secret=SECRET, remember=None))
        assert rig.vault.entries == {}
        row = full(rig, aid)
        assert not row["remember"]
        assert row.get("credential_ref") in (None, "")


def test_remember_true_writes_vault_and_credential_ref(tmp_path):
    """02 #2/#13:`remember:true` 才写 Vault 与 `credential_ref`,`accounts.remember=1`。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian", secret=SECRET, remember=True))
        assert any(e.value == SECRET for e in rig.vault.entries.values()), rig.vault.entries
        row = full(rig, aid)
        assert row["remember"]
        assert row.get("credential_ref")


def test_login_no_save_path_202_logging_in(tmp_path):
    """05 §2.2.7 + 02 #12:WAIT_PASSWORD 下 `POST …/login {secret}` 回 `202 {state:'logging_in'}`(内存传递),继续登录到 running;仍不落 Vault。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        r = client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert r.status_code == 202, r.text
        assert _data(r)["state"] == "logging_in"
        wait_idle(client, rig, aid)
        assert state_of(rig, aid)[0] == "running"
        assert stub.calls[-1][2] == SECRET
        assert rig.vault.entries == {}


def test_login_with_remember_true_writes_vault(tmp_path):
    """05 §2.2.7「可再次勾"保存到保险库"」:`POST …/login {secret, remember:true}` 写 Vault。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        r = client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET, "remember": True}, headers=H())
        assert r.status_code == 202, r.text
        wait_idle(client, rig, aid)
        assert any(e.value == SECRET for e in rig.vault.entries.values())
        assert full(rig, aid)["remember"]


def test_login_session_id_format_ls_ulid(tmp_path):
    """05 §2.0 `login_session_id`:格式 `ls_`+ULID;00 §7.5:凡进入登录流程的 account_state 事件就带它。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        ls = [_payload(e).get("login_session_id") for e in state_events(rig, aid) if _payload(e).get("state") == "logging_in"]
        assert ls and ls[-1], "logging_in 事件须带 login_session_id"
        assert LS_RE.match(ls[-1]), ls[-1]


def test_login_session_id_spans_all_events_of_one_attempt(tmp_path):
    """05 §2.0:`login_session_id` 贯穿该次尝试的所有 prompt/account_state——logging_in 与随后的 WAIT_SMS 事件同一个 id。"""
    stub = LoginStub("WAIT_SMS")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        n0 = len(state_events(rig, aid))
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        assert state_of(rig, aid) == ("login_required", "WAIT_SMS")
        evs = [_payload(e) for e in state_events(rig, aid)[n0:]]
        ids = {p.get("login_session_id") for p in evs}
        assert len(ids) == 1 and None not in ids, evs
        assert any(p.get("state") == "logging_in" for p in evs) and any(p.get("state_code") == "WAIT_SMS" for p in evs)


def test_login_session_id_new_after_terminal_state(tmp_path):
    """05 §2.0:该次尝试到达终态(running)即失效——再发起登录是新的一次、换新 id。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        ls1 = [_payload(e).get("login_session_id") for e in state_events(rig, aid) if _payload(e).get("state") == "logging_in"][-1]
        client.portal.call(_call, rig.agent.accounts.mark_offline, aid, "KICKED")
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        ls2 = [_payload(e).get("login_session_id") for e in state_events(rig, aid) if _payload(e).get("state") == "logging_in"][-1]
        assert ls1 and ls2 and ls1 != ls2


def test_login_session_id_null_outside_login_phase(tmp_path):
    """05 §2.0:「非登录态事件为 null」——stopped 事件的 payload.login_session_id 为空。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        r = client.post(f"{P}/accounts/{aid}/stop", headers=H())
        assert r.status_code in (200, 202), r.text
        wait_idle(client, rig, aid)
        e = last_state_event(rig, aid)
        assert _payload(e).get("state") == "stopped"
        assert _payload(e).get("login_session_id") in (None, "")


def test_secret_never_in_commands_events_audit(tmp_path):
    """05 §2.0「凭据不落 WSL」/ 02 §6:密码不进 `commands`、不进事件 payload、不进审计——三张表都不含明文。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET, "remember": True}, headers=H())
        wait_idle(client, rig, aid)
        client.put(f"{P}/accounts/{aid}/credential", json={"secret": SECRET + "-2", "remember": True}, headers=H())
        for t in ("commands", "events_outbox", "audit_log", "accounts", "command_results"):
            assert not table_has(rig.store, t, SECRET), f"{t} 含明文密码"


def test_bad_credential_error_and_vault_suspect(tmp_path):
    """02 §2.2.2 / 05 §2.0 失败组:执行层判 `bad_credential` ⇒ 账号 `error(BAD_CREDENTIAL)`,Vault 条目打 `suspect`。"""
    stub = LoginStub("bad_credential")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian", secret=SECRET, remember=True))
        start(client, rig, aid)
        assert state_of(rig, aid) == ("error", "BAD_CREDENTIAL"), state_of(rig, aid)
        assert rig.vault.entries and all(e.suspect for e in rig.vault.entries.values()), rig.vault.entries


def test_offline_no_auto_relogin(tmp_path):
    """05 §2.0 D-2 / 02 §2.2.2 / §5:运行期掉线后**绝不自动取用** Vault、不自动重登——mark_offline 后 login_fn 不再被调、Vault 读数不变。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        n_calls, n_reads = len(stub.calls), len(rig.vault.reads)
        client.portal.call(_call, rig.agent.accounts.mark_offline, aid, "KICKED")
        wait_idle(client, rig, aid)
        rig.clock.advance(400_000)
        client.portal.call(_call, rig.agent.accounts.login_remind)
        client.portal.call(_call, rig.agent.accounts.recover)
        wait_idle(client, rig, aid)
        assert state_of(rig, aid) == ("login_required", "KICKED")
        assert len(stub.calls) == n_calls, "掉线后不得自动重登"
        assert len(rig.vault.reads) == n_reads, "掉线后不得自动取用 Vault"


def test_login_after_offline_uses_vault_if_present(tmp_path):
    """05 §2.5.4 企点行:掉线后人点「登录」`POST …/login {}`——Vault 有条目则自动填(人发起)。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        client.portal.call(_call, rig.agent.accounts.mark_offline, aid, "LOGGED_OUT")
        n = len(stub.calls)
        r = client.post(f"{P}/accounts/{aid}/login", json={}, headers=H())
        assert r.status_code == 202, r.text
        wait_idle(client, rig, aid)
        assert len(stub.calls) == n + 1 and stub.calls[-1][2] == SECRET
        assert state_of(rig, aid)[0] == "running"


def test_login_after_offline_without_vault_waits_password(tmp_path):
    """05 §2.5.4 企点行 / §2.2.7:人点「登录」而 Vault 无条目 ⇒ `login_required(WAIT_PASSWORD)`,事件 prompt.kind=WAIT_PASSWORD。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        assert state_of(rig, aid)[0] == "running" and rig.vault.entries == {}
        client.portal.call(_call, rig.agent.accounts.mark_offline, aid, "KICKED")
        r = client.post(f"{P}/accounts/{aid}/login", json={}, headers=H())
        wait_idle(client, rig, aid)
        assert r.status_code == 202, f"05 §2.5.4:人点「登录」无 Vault 条目应转 WAIT_PASSWORD 而非拒绝;实际 {r.status_code} {r.text}"
        assert state_of(rig, aid) == ("login_required", "WAIT_PASSWORD")
        assert (_payload(last_state_event(rig, aid)).get("prompt") or {}).get("kind") == "WAIT_PASSWORD"


# ══════════════════════════════════════════════════════════════════════ 三、#13 / #14 凭据
def test_put_credential_writes_vault_only_no_login(tmp_path):
    """02 #13:`PUT …/credential {secret, remember:true}` 只写 Vault 与 `credential_ref`,**不登录**(C-40)——login_fn 不被调、状态不变。"""
    stub = LoginStub("running")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        n = len(stub.calls)
        r = client.put(f"{P}/accounts/{aid}/credential", json={"account": "u1", "secret": SECRET, "remember": True}, headers=H())
        assert r.status_code in (200, 204), r.text
        wait_idle(client, rig, aid)
        assert len(stub.calls) == n
        assert state_of(rig, aid) == ("login_required", "WAIT_PASSWORD")
        assert any(e.value == SECRET for e in rig.vault.entries.values())
        row = full(rig, aid)
        assert row["remember"] and row.get("credential_ref")


def test_put_credential_response_does_not_echo_secret(tmp_path):
    """02 #13:「响应不回显」——响应正文不含密码。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian"))
        r = client.put(f"{P}/accounts/{aid}/credential", json={"secret": SECRET, "remember": True}, headers=H())
        assert r.status_code in (200, 204), r.text
        assert SECRET not in r.text


def test_delete_credential_clears_remember_and_ref(tmp_path):
    """02 #14:`DELETE …/credential` 删 Vault 条目,`remember=0, credential_ref=NULL`。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian", secret=SECRET, remember=True))
        assert rig.vault.entries
        r = client.delete(f"{P}/accounts/{aid}/credential", headers=H())
        assert r.status_code in (200, 204), r.text
        row = full(rig, aid)
        assert not row["remember"]
        assert row.get("credential_ref") is None
        assert rig.vault.entries == {} and rig.vault.deleted


# ══════════════════════════════════════════════════════════════════════ 四、#15 prompt / #16b login/cancel
def test_prompt_equals_event_prompt_object(tmp_path):
    """02 #15 C-12:`GET …/prompt` 与 `account_state.payload.prompt` 是同一对象(刷新页面后重取)。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        r = client.get(f"{P}/accounts/{aid}/prompt", headers=H())
        assert r.status_code == 200, r.text
        ev_prompt = _payload(last_state_event(rig, aid)).get("prompt")
        assert ev_prompt and ev_prompt.get("kind") == "WAIT_PASSWORD"
        keys = ("kind", "text", "qrcode_png_b64", "expires_at", "countdown_s")      # #15 定义的 prompt 对象属性
        got = _data(r)
        assert {k: got.get(k) for k in keys} == {k: ev_prompt.get(k) for k in keys}, (got, ev_prompt)


def test_prompt_kind_null_when_no_wait(tmp_path):
    """02 #15:无等待 → `{kind:null}`。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        r = client.get(f"{P}/accounts/{aid}/prompt", headers=H())
        assert r.status_code == 200, r.text
        assert _data(r).get("kind") is None


def test_prompt_stale_login_session_id_returns_null(tmp_path):
    """02 #15 `?login_session_id=` / 05 §2.0:旧尝试的 id 已失效 → 回 `{kind:null}`;当前尝试的 id → 当前 prompt。"""
    stub = LoginStub("WAIT_SMS")
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        ls1 = last_ls(rig, aid)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        wait_idle(client, rig, aid)
        ls2 = last_ls(rig, aid)
        assert ls1 and ls2 and ls1 != ls2
        stale = client.get(f"{P}/accounts/{aid}/prompt", params={"login_session_id": ls1}, headers=H())
        assert stale.status_code == 200 and _data(stale).get("kind") is None, stale.text
        cur = client.get(f"{P}/accounts/{aid}/prompt", params={"login_session_id": ls2}, headers=H())
        assert cur.status_code == 200 and _data(cur).get("kind") == "WAIT_SMS", cur.text


def test_cancel_with_stale_id_is_idempotent_noop(tmp_path):
    """02 #16b:带的 `login_session_id` ≠ 当前那次 ⇒ 幂等 no-op `200 {cancelled:false, stale:true, current_login_session_id:"<当前那次>"}`(不是 404/409)。"""
    stub = LoginStub("running", hang_s=5.0)
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert wait_for(lambda: len(stub.calls) == 1 and last_ls(rig, aid))
        ls1 = last_ls(rig, aid)
        r1 = client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls1}, headers=H())
        assert r1.status_code == 200 and _data(r1).get("cancelled") is True, r1.text
        wait_idle(client, rig, aid)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert wait_for(lambda: len(stub.calls) == 2 and last_ls(rig, aid) not in (None, ls1))
        ls2 = last_ls(rig, aid)
        r2 = client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls1}, headers=H())
        assert r2.status_code == 200, r2.text
        d = _data(r2)
        assert d.get("cancelled") is False and d.get("stale") is True
        assert d.get("current_login_session_id") == ls2
        client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls2}, headers=H())
        wait_idle(client, rig, aid)


def test_cancel_with_stale_id_does_not_kill_new_attempt(tmp_path):
    """02 #16b / 00 §10:「绝不误杀新尝试」——带旧 id 取消后,新尝试仍在 logging_in,login_fn 未被取消。"""
    stub = LoginStub("running", hang_s=5.0)
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert wait_for(lambda: len(stub.calls) == 1 and last_ls(rig, aid))
        ls1 = last_ls(rig, aid)
        client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls1}, headers=H())
        wait_idle(client, rig, aid)
        cancelled_before = stub.cancelled
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert wait_for(lambda: len(stub.calls) == 2 and last_ls(rig, aid) not in (None, ls1))
        ls2 = last_ls(rig, aid)
        client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls1}, headers=H())
        time.sleep(0.1)
        assert state_of(rig, aid)[0] == "logging_in", state_of(rig, aid)
        assert stub.cancelled == cancelled_before, "旧 id 不得取消新尝试"
        assert last_ls(rig, aid) == ls2
        client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls2}, headers=H())
        wait_idle(client, rig, aid)


def test_cancel_with_current_id_succeeds(tmp_path):
    """02 #16b:`login_session_id` == 当前那次 ⇒ `200 {cancelled:true, stale:false}`,该次尝试结束、状态离开 logging_in。"""
    stub = LoginStub("running", hang_s=5.0)
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert wait_for(lambda: len(stub.calls) == 1 and last_ls(rig, aid))
        ls = last_ls(rig, aid)
        r = client.post(f"{P}/accounts/{aid}/login/cancel", json={"login_session_id": ls}, headers=H())
        assert r.status_code == 200, r.text
        d = _data(r)
        assert d.get("cancelled") is True and d.get("stale") is False
        wait_idle(client, rig, aid)
        assert state_of(rig, aid)[0] not in ("logging_in", "running"), state_of(rig, aid)
        assert stub.cancelled == 1


def test_cancel_without_id_cancels_current_attempt(tmp_path):
    """02 #16b:不带 `login_session_id` = 取消「当前这次」(兼容路径)。"""
    stub = LoginStub("running", hang_s=5.0)
    with rig_ctx(tmp_path, login_fn=stub) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        client.post(f"{P}/accounts/{aid}/login", json={"secret": SECRET}, headers=H())
        assert wait_for(lambda: len(stub.calls) == 1 and last_ls(rig, aid))
        r = client.post(f"{P}/accounts/{aid}/login/cancel", json={}, headers=H())
        assert r.status_code == 200 and _data(r).get("cancelled") is True, r.text
        wait_idle(client, rig, aid)
        assert state_of(rig, aid)[0] != "logging_in"


def test_cancel_when_no_attempt_in_progress_NOT_APPLICABLE(tmp_path):
    """02 #16b:「无进行中尝试 → NOT_APPLICABLE」。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        r = client.post(f"{P}/accounts/{aid}/login/cancel", json={}, headers=H())
        assert r.json().get("code") == "NOT_APPLICABLE", r.text


# ══════════════════════════════════════════════════════════════════════ 五、掉线检测(05 §2.5.4 / 05 §2.0 / 02 §5)
async def _seed_running(rig) -> str:
    aid = seed_account(rig, state="created")
    await _call(rig.agent.accounts.transition, aid, "running")
    assert state_of(rig, aid)[0] == "running"
    return aid


async def test_offline_does_exactly_three_things(tmp_path):
    """05 §2.5.4 原则:掉线 ⇒ ①`login_required(掉线原因)` ②`account_state` 事件带 prompt 引导点「登录」 ③`alert(warn)` 一条 `ACCOUNT_OFFLINE`(subject=account:<id>)。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
        assert state_of(rig, aid) == ("login_required", "KICKED")
        p = _payload(last_state_event(rig, aid))
        assert p.get("state") == "login_required" and p.get("state_code") == "KICKED"
        assert (p.get("prompt") or {}).get("kind") == "KICKED", p
        assert "登录" in ((p.get("prompt") or {}).get("text") or "")
        al = active(rig, ACCOUNT_OFFLINE, f"account:{aid}")
        assert al is not None and _attr(al, "severity") == "warn"
        assert len(alert_events(rig, ACCOUNT_OFFLINE, f"account:{aid}")) == 1


@pytest.mark.parametrize("code", OFFLINE_CODES)
async def test_offline_state_code_in_offline_group(tmp_path, code):
    """05 §2.0 掉线原因组 `KICKED / LOGGED_OUT / TOKEN_EXPIRED / LOGIN_TIMEOUT`:四码都伴随 `login_required`,prompt.kind 同码。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, code)
        assert state_of(rig, aid) == ("login_required", code)
        assert (_payload(last_state_event(rig, aid)).get("prompt") or {}).get("kind") == code
        assert firing(rig, ACCOUNT_OFFLINE, f"account:{aid}")


async def test_offline_third_within_1h_escalates_to_crit_not_state(tmp_path):
    """05 §2.5.4 R6-56:同一账号 1 小时内第 3 次掉线 ⇒ 告警 `severity` 升 `crit`,**不改账号 state**(仍 login_required)。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        for i in range(3):
            if i:
                await _call(rig.agent.accounts.transition, aid, "running")
                rig.clock.advance(60_000)
            await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
            sev = _attr(active(rig, ACCOUNT_OFFLINE, f"account:{aid}"), "severity")
            assert sev == ("crit" if i == 2 else "warn"), (i, sev)
        assert state_of(rig, aid)[0] == "login_required"
        assert rig.store.get_runtime(aid)["offline_count_1h"] == 3


async def test_offline_alert_evidence_keys(tmp_path):
    """05 §2.5.4:`ACCOUNT_OFFLINE` 的 `evidence{code, offline_count_1h, last_offline_at}`。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, "TOKEN_EXPIRED")
        ev = _attr(active(rig, ACCOUNT_OFFLINE, f"account:{aid}"), "evidence") or {}
        assert {"code", "offline_count_1h", "last_offline_at"} <= set(ev.keys()), ev
        assert ev["code"] == "TOKEN_EXPIRED" and ev["offline_count_1h"] == 1


async def test_offline_runtime_columns(tmp_path):
    """05 §2.5.4:`offline_count_1h` 按 `account_runtime.last_offline_ms` 滚动——runtime 行记 last_offline_ms / last_offline_code / offline_count_1h。"""
    with rig_ctx(tmp_path, clock=Clock(auto_step_ms=0)) as rig:
        aid = await _seed_running(rig)
        t0 = rig.clock.now_ms
        await _call(rig.agent.accounts.mark_offline, aid, "LOGGED_OUT")
        rt = rig.store.get_runtime(aid)
        assert rt["last_offline_code"] == "LOGGED_OUT"
        assert rt["last_offline_ms"] == t0
        assert rt["offline_count_1h"] == 1


async def test_offline_count_window_rolls_after_1h(tmp_path):
    """05 §2.5.4:「1 小时内」按 last_offline_ms 滚动——超过 1 小时后再掉线不算第 3 次,不升 crit。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        for _ in range(2):
            await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
            await _call(rig.agent.accounts.transition, aid, "running")
        rig.clock.advance(3_600_000 + 1_000)
        await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
        assert rig.store.get_runtime(aid)["offline_count_1h"] == 1
        assert _attr(active(rig, ACCOUNT_OFFLINE, f"account:{aid}"), "severity") == "warn"


async def test_login_remind_resends_same_trace_id_not_alert(tmp_path):
    """05 §2.5.4:`login_required` 期间每 `login_remind_interval_s`(300)重发一条**同 trace_id** 的提醒事件,**不计入告警**。"""
    cfg = AgentConfig(accounts=AccountsConfig(login_remind_interval_s=300))
    with rig_ctx(tmp_path, cfg=cfg) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
        first = last_state_event(rig, aid)
        n_ev, n_al = len(state_events(rig, aid)), len(alert_events(rig, ACCOUNT_OFFLINE))
        al_count = _attr(active(rig, ACCOUNT_OFFLINE, f"account:{aid}"), "count")
        rig.clock.advance(300_000 + 1_000)
        n = await _call(rig.agent.accounts.login_remind)
        assert n == 1
        evs = state_events(rig, aid)
        assert len(evs) == n_ev + 1
        assert evs[-1]["trace_id"] == first["trace_id"]
        assert _payload(evs[-1]).get("state") == "login_required"
        assert len(alert_events(rig, ACCOUNT_OFFLINE)) == n_al
        assert _attr(active(rig, ACCOUNT_OFFLINE, f"account:{aid}"), "count") == al_count


async def test_login_remind_not_before_interval(tmp_path):
    """05 §7 `login_remind_interval_s=300`:未到间隔不重发(返回 0 条)。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
        n_ev = len(state_events(rig, aid))
        rig.clock.advance(100_000)
        assert await _call(rig.agent.accounts.login_remind) == 0
        assert len(state_events(rig, aid)) == n_ev


async def test_login_remind_repeats_every_interval(tmp_path):
    """05 §2.5.4:提醒按间隔**反复**重发(第二个间隔再发一条,仍同 trace_id)。"""
    with rig_ctx(tmp_path) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
        tid = last_state_event(rig, aid)["trace_id"]
        rig.clock.advance(301_000)
        assert await _call(rig.agent.accounts.login_remind) == 1
        rig.clock.advance(301_000)
        assert await _call(rig.agent.accounts.login_remind) == 1
        assert state_events(rig, aid)[-1]["trace_id"] == tid


async def test_wait_phase_not_counted_as_offline(tmp_path):
    """05 §2.0 登录阶段界定 / §2.5.4 R6-56:登录阶段里的 `WAIT_*` 不计入掉线次数、不发 ACCOUNT_OFFLINE——掉线 1 次后人登录卡短信,计数仍 1。"""
    stub = LoginStub("WAIT_SMS")
    with rig_ctx(tmp_path, login_fn=stub) as rig:
        aid = await _seed_running(rig)
        await _call(rig.agent.accounts.mark_offline, aid, "KICKED")
        n_al = len(alert_events(rig, ACCOUNT_OFFLINE))
        await _call(rig.agent.accounts.login, aid, {"secret": SECRET}, actor="t")
        await _wait_idle(rig, aid)
        assert state_of(rig, aid) == ("login_required", "WAIT_SMS")
        assert rig.store.get_runtime(aid)["offline_count_1h"] == 1
        assert len(alert_events(rig, ACCOUNT_OFFLINE)) == n_al
        assert _attr(active(rig, ACCOUNT_OFFLINE, f"account:{aid}"), "count") == 1


async def test_wait_state_in_login_phase_is_not_offline(tmp_path):
    """05 §2.0:验证环节「不是运行期故障」——首次登录卡 WAIT_SMS(从未掉线),不产生 ACCOUNT_OFFLINE、runtime 掉线计数为 0。"""
    stub = LoginStub("WAIT_SMS")
    with rig_ctx(tmp_path, login_fn=stub) as rig:
        aid = seed_account(rig, state="login_required", state_code="WAIT_PASSWORD")
        await _call(rig.agent.accounts.login, aid, {"secret": SECRET}, actor="t")
        await _wait_idle(rig, aid)
        assert state_of(rig, aid) == ("login_required", "WAIT_SMS")
        assert not firing(rig, ACCOUNT_OFFLINE, f"account:{aid}")
        assert (rig.store.get_runtime(aid) or {}).get("offline_count_1h") in (0, None)


# ══════════════════════════════════════════════════════════════════════ 六、H04 容器存活(04 §2.3 H04 / 02 §5 / 02 §3.7)
def _exit(rig, cname: str = CNAME_01, code: int = 137, oom: bool = False) -> None:
    c = rig.containers.containers[cname]
    c.running, c.exit_code, c.oom_killed = False, code, oom


def _check_containers(client, rig, aid: Optional[str] = None) -> None:
    """跑一轮 H04;自动重拉是后台序列,传 aid 则等它跑完。"""
    client.portal.call(rig.agent.healthloop.check_containers)
    if aid:
        wait_idle(client, rig, aid)


def _run_a_while(rig, ms: int = 5_000) -> None:
    """容器「跑一会儿」再退出:跳出 02 #19 的 inspect 读缓存(≤2 s),让下一次 inspect 看到真实退出。"""
    rig.clock.advance(ms)


def test_h04_exit_sets_error_reason_and_crit_alert(tmp_path):
    """02 §5「容器意外退出」:`state=error`、`state_reason=exit code`、事件 account_state;02 §3.7 `H04_CONTAINER_EXITED` crit,subject=account:<id>。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        assert CNAME_01 in rig.containers.containers
        _exit(rig, code=137)
        _check_containers(client, rig)
        row = full(rig, aid)
        assert row["state"] == "error" and row.get("state_code") == "CONTAINER_EXIT", (row["state"], row.get("state_code"))
        assert "137" in (row.get("state_reason") or "")
        assert _payload(last_state_event(rig, aid)).get("state") == "error"
        al = active(rig, H04_CONTAINER_EXITED, f"account:{aid}")
        assert al is not None and _attr(al, "severity") == "crit"


def test_h04_oom_killed_pushes_CONTAINER_OOM_KILLED_first(tmp_path):
    """04 §2.3 H04 E-19:`OOMKilled=true` ⇒ **先推** `CONTAINER_OOM_KILLED`(subject=account:<id>),再走同一退避重拉(H04 也发)。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _exit(rig, code=137, oom=True)
        _check_containers(client, rig)
        assert firing(rig, CONTAINER_OOM_KILLED, f"account:{aid}")
        assert firing(rig, H04_CONTAINER_EXITED, f"account:{aid}")
        codes = [p.get("code") for p in alert_events(rig, subject=f"account:{aid}") if p.get("code") in (CONTAINER_OOM_KILLED, H04_CONTAINER_EXITED)]
        assert codes.index(CONTAINER_OOM_KILLED) < codes.index(H04_CONTAINER_EXITED), codes


def test_h04_backoff_first_two_steps_60_then_120_s(tmp_path):
    """04 §2.3 H04「退避重拉 1/2/5/10 min」= 04 §7 `container_restart_backoff_s=[60,120,300,600]`:第 1 次退避 60 s,第 2 次 120 s。"""
    with rig_ctx(tmp_path, login_fn=login_ok, clock=Clock(auto_step_ms=0)) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _run_a_while(rig)
        _exit(rig)
        t0 = rig.clock.now_ms
        _check_containers(client, rig)
        ri = rig.agent.healthloop.restart[aid]
        assert _attr(ri, "next_ms") - t0 == 60_000, (_attr(ri, "next_ms"), t0)
        rig.clock.advance(60_000)
        _check_containers(client, rig, aid)
        assert rig.containers.containers[CNAME_01].running is True, "到点须自动 start"
        _run_a_while(rig)
        _exit(rig)
        t1 = rig.clock.now_ms
        _check_containers(client, rig)
        assert _attr(rig.agent.healthloop.restart[aid], "next_ms") - t1 == 120_000


def test_h04_restart_waits_until_next_ms(tmp_path):
    """04 §2.3 H04 退避:未到 `next_ms` 不拉起;到点才 `start`。"""
    cfg = AgentConfig(health=HealthConfig(container_restart_backoff_s=(1, 1, 1, 1), container_restart_max=2))
    with rig_ctx(tmp_path, cfg=cfg, login_fn=login_ok, clock=Clock(auto_step_ms=0)) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        n0 = count_calls(rig.containers.calls, "start", CNAME_01)
        _run_a_while(rig)
        _exit(rig)
        _check_containers(client, rig, aid)
        _check_containers(client, rig, aid)
        assert count_calls(rig.containers.calls, "start", CNAME_01) == n0, "退避未到不得拉起"
        assert rig.containers.containers[CNAME_01].running is False
        rig.clock.advance(1_001)
        _check_containers(client, rig, aid)
        assert count_calls(rig.containers.calls, "start", CNAME_01) == n0 + 1
        assert rig.containers.containers[CNAME_01].running is True
        assert _attr(rig.agent.healthloop.restart[aid], "count") == 1


def test_h04_restart_max_then_stop_self_heal_and_alert(tmp_path):
    """02 §5:`restart_count<5` 才自动 start;超限「停止自愈并 alert」(02 §3.7 `AUTO_RESTART_EXHAUSTED` crit;04 F-04)。配置缩到 max=2。"""
    cfg = AgentConfig(health=HealthConfig(container_restart_backoff_s=(1, 1, 1, 1), container_restart_max=2))
    with rig_ctx(tmp_path, cfg=cfg, login_fn=login_ok, clock=Clock(auto_step_ms=0)) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        n0 = count_calls(rig.containers.calls, "start", CNAME_01)
        for _ in range(2):
            _run_a_while(rig)
            _exit(rig)
            _check_containers(client, rig, aid)
            rig.clock.advance(1_001)
            _check_containers(client, rig, aid)
        assert count_calls(rig.containers.calls, "start", CNAME_01) == n0 + 2
        assert rig.containers.containers[CNAME_01].running is True
        _run_a_while(rig)
        _exit(rig)
        _check_containers(client, rig, aid)
        rig.clock.advance(1_001)
        _check_containers(client, rig, aid)
        rig.clock.advance(1_001)
        _check_containers(client, rig, aid)
        assert count_calls(rig.containers.calls, "start", CNAME_01) == n0 + 2, "超限后不得再自动拉起"
        ri = rig.agent.healthloop.restart[aid]
        assert _attr(ri, "exhausted") is True
        assert firing(rig, AUTO_RESTART_EXHAUSTED, f"account:{aid}")
        assert _attr(active(rig, AUTO_RESTART_EXHAUSTED, f"account:{aid}"), "severity") == "crit"
        assert state_of(rig, aid)[0] == "error"


def test_h04_restart_count_resets_hourly(tmp_path):
    """02 §5:重启计数「每小时清零」——超限 1 小时后自愈恢复,可再自动拉起。"""
    cfg = AgentConfig(health=HealthConfig(container_restart_backoff_s=(1, 1, 1, 1), container_restart_max=2))
    with rig_ctx(tmp_path, cfg=cfg, login_fn=login_ok, clock=Clock(auto_step_ms=0)) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        for _ in range(3):
            _run_a_while(rig)
            _exit(rig)
            _check_containers(client, rig, aid)
            rig.clock.advance(1_001)
            _check_containers(client, rig, aid)
        assert _attr(rig.agent.healthloop.restart[aid], "exhausted") is True
        n = count_calls(rig.containers.calls, "start", CNAME_01)
        rig.clock.advance(3_600_000 + 1_000)
        _check_containers(client, rig, aid)
        rig.clock.advance(1_001)
        _check_containers(client, rig, aid)
        assert count_calls(rig.containers.calls, "start", CNAME_01) > n, "一小时后计数清零、自愈应恢复"
        assert _attr(rig.agent.healthloop.restart[aid], "count") <= 1


def test_h04_resolved_when_container_back(tmp_path):
    """00 §7.5 告警去重键 (code, subject) + state firing|resolved:容器回来后 `H04_CONTAINER_EXITED` 转 resolved。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _exit(rig)
        _check_containers(client, rig)
        assert firing(rig, H04_CONTAINER_EXITED, f"account:{aid}")
        rig.containers.containers[CNAME_01].running = True
        rig.containers.containers[CNAME_01].exit_code = None
        _check_containers(client, rig)
        assert not firing(rig, H04_CONTAINER_EXITED, f"account:{aid}")
        assert any(p.get("state") == "resolved" for p in alert_events(rig, H04_CONTAINER_EXITED, f"account:{aid}"))


def test_h04_only_for_containers_that_should_be_running(tmp_path):
    """04 §2.3 H04:判据是「本应 running 的账号容器退出」——人停掉的账号(desired_state=stopped)容器不在则不告警、不拉起。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        r = client.post(f"{P}/accounts/{aid}/stop", headers=H())
        assert r.status_code in (200, 202), r.text
        wait_idle(client, rig, aid)
        assert state_of(rig, aid)[0] == "stopped"
        n = count_calls(rig.containers.calls, "start", CNAME_01)
        if CNAME_01 in rig.containers.containers:
            _exit(rig, code=0)
        _check_containers(client, rig)
        assert not firing(rig, H04_CONTAINER_EXITED, f"account:{aid}")
        assert count_calls(rig.containers.calls, "start", CNAME_01) == n
        assert state_of(rig, aid)[0] == "stopped"


# ══════════════════════════════════════════════════════════════════════ 七、H06 adb 连接态 / root 态(04 §2.3 H06,R6-32)
def _check_adb(client, rig) -> None:
    client.portal.call(rig.agent.healthloop.check_adb)


def _fail_connect_for(rig, serials: set[str]):
    """夹具编程:让指定序列号的 `adb connect` 恒失败(其它序列号照旧),用来制造 (a) 连接态连续失败。"""
    orig = rig.adb.connect

    async def connect(serial: str) -> bool:
        if serial in serials:
            rig.adb.calls.append(("connect", serial))
            return False
        return await orig(serial)

    rig.adb.connect = connect


def _past_grace(rig) -> None:
    rig.clock.advance(20_000)      # > adb_root_grace_s=15:跳出启动期 ensure_root 的宽限窗


def test_h06_offline_disconnect_connect_then_ensure_root(tmp_path):
    """04 §2.3 H06 (a):`offline` ⇒ 先 `adb disconnect` + `adb connect`,成功后对企点账号追加 `ensure_root`(adb root)——三步按此顺序。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        up_running(client, rig)
        _past_grace(rig)
        rig.adb.state[SERIAL_01] = "offline"
        n = len(rig.adb.calls)
        _check_adb(client, rig)
        ops = [c[0] for c in rig.adb.calls[n:] if c[1].startswith(SERIAL_01)]
        assert "disconnect" in ops and "connect" in ops and "root" in ops, ops
        assert ops.index("disconnect") < ops.index("connect") < ops.index("root"), ops


def test_h06_fail_streak_counts_and_crit_at_three(tmp_path):
    """04 §2.3 H06 (a):`h06_fail_streak` 按 account_id 计数;第 1、2 次 warn(02 §3.7 H06 warn),达 3 ⇒ 该账号 crit。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _past_grace(rig)
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"
        for i in (1, 2, 3):
            _check_adb(client, rig)
            rig.adb.state.setdefault(SERIAL_01, "offline")
            assert rig.agent.healthloop.h06_fail_streak.get(aid) == i, rig.agent.healthloop.h06_fail_streak
            al = active(rig, H06_ADB_OFFLINE, f"account:{aid}")
            assert al is not None, "连接失败应告警 H06_ADB_OFFLINE"
            assert _attr(al, "severity") == ("crit" if i == 3 else "warn"), (i, _attr(al, "severity"))


def test_h06_reconnect_command_success_does_not_clear_streak(tmp_path):
    """04 §2.3 H06 (a)「连续失败计数器 h06_fail_streak(成功一次即清零)」——**「成功」= 下一轮 `adb devices -l` 看到该 serial 为 `device`,
    `adb connect` 命令返回成功不算(connect 成功但状态仍 offline 是常态;00 §15g R6-57 ③ 钉死)**:两轮重连失败(streak=2)后,
    第三轮 `devices` 仍 offline、重连命令成功并进 ensure_root ⇒ 本轮 streak 仍 +1(=3、crit),清零要等下一轮看到 device。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _past_grace(rig)
        orig = rig.adb.connect
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"
        _check_adb(client, rig)
        _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid) == 2
        rig.adb.connect = orig
        rig.adb.state[SERIAL_01] = "offline"
        n = len(rig.adb.calls)
        _check_adb(client, rig)
        ops = [c[0] for c in rig.adb.calls[n:] if c[1].startswith(SERIAL_01)]
        assert "connect" in ops and "root" in ops, ops                      # 这一轮重连命令成功并进了 ensure_root
        assert rig.agent.healthloop.h06_fail_streak.get(aid, 0) == 3, rig.agent.healthloop.h06_fail_streak
        assert _attr(active(rig, H06_ADB_OFFLINE, f"account:{aid}"), "severity") == "crit"
        assert state_of(rig, aid)[0] == "running"                            # (a) 三振只告警,不改 state
        assert not any("kill" in c[0] for c in rig.adb.calls)


def test_h06_device_seen_next_round_clears_and_resolves(tmp_path):
    """04 §2.3 H06 (a) / 00 §7.5:设备回到 `device` 后 streak 清零、`H06_ADB_OFFLINE` 转 resolved。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _past_grace(rig)
        orig = rig.adb.connect
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"
        _check_adb(client, rig)
        _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid) == 2 and firing(rig, H06_ADB_OFFLINE, f"account:{aid}")
        rig.adb.connect = orig
        _past_grace(rig)
        rig.adb.state[SERIAL_01] = "device"
        _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid, 0) == 0
        assert not firing(rig, H06_ADB_OFFLINE, f"account:{aid}")
        assert any(p.get("state") == "resolved" for p in alert_events(rig, H06_ADB_OFFLINE, f"account:{aid}"))


def test_h06_grace_window_skips_connection_judgement(tmp_path):
    """04 §2.3 H06 宽限窗(R6-32 ③):自 `ensure_root` 开始起 ≤ `adb_root_grace_s` 秒内不判定 (a)——`h06_fail_streak` 不增不清、不发连接态告警。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _past_grace(rig)
        rig.adb.state[SERIAL_01] = "offline"
        _check_adb(client, rig)                                  # 重连成功 → ensure_root 开始 → 宽限窗打开
        assert rig.agent.health.is_rooting(aid) is True
        streak = rig.agent.healthloop.h06_fail_streak.get(aid, 0)
        n_al = len(alert_events(rig, H06_ADB_OFFLINE, f"account:{aid}"))
        sev = _attr(active(rig, H06_ADB_OFFLINE, f"account:{aid}"), "severity")
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"                    # ensure_root 内 stop adbd 造成的短暂 offline 属标准动作
        for _ in range(3):
            _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid, 0) == streak, "宽限窗内 streak 不递增"
        assert len(alert_events(rig, H06_ADB_OFFLINE, f"account:{aid}")) == n_al, "宽限窗内不发连接态告警"
        assert _attr(active(rig, H06_ADB_OFFLINE, f"account:{aid}"), "severity") == sev != "crit"


def test_h06_grace_window_default_15s_then_judges(tmp_path):
    """04 §7 `adb_root_grace_s=15`:窗口外才按真故障计——过 15 s 后同样的 offline 开始递增 streak。"""
    with rig_ctx(tmp_path, login_fn=login_ok, clock=Clock(auto_step_ms=0)) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _past_grace(rig)
        rig.adb.state[SERIAL_01] = "offline"
        _check_adb(client, rig)
        assert rig.agent.health.is_rooting(aid) is True
        base = rig.agent.healthloop.h06_fail_streak.get(aid, 0)
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"
        rig.clock.advance(14_000)
        _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid, 0) == base, "窗内(14 s)不判定"
        rig.clock.advance(2_000)
        assert rig.agent.health.is_rooting(aid) is False
        rig.adb.state[SERIAL_01] = "offline"
        _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid, 0) == base + 1, "窗外(16 s)按真故障计"


def test_h06_whoami_not_root_warn_only_never_crit(tmp_path):
    """04 §2.3 H06 (b) R6-32:`whoami != root` ⇒ 只推 warn `QIDIAN_NOT_ROOT`;连续 3 次也只保持 firing、**永不升 crit**。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        rig.adb.whoami_after_root[SERIAL_01] = "shell"
        for _ in range(3):
            _past_grace(rig)
            rig.adb.state[SERIAL_01] = "device"
            _check_adb(client, rig)
            al = active(rig, QIDIAN_NOT_ROOT, f"account:{aid}")
            assert al is not None and _attr(al, "severity") == "warn", al
        assert rig.agent.runtime.qidian_root_fail_streak.get(aid, 0) >= 3
        assert _attr(active(rig, QIDIAN_NOT_ROOT, f"account:{aid}"), "severity") == "warn"


def test_h06_root_failure_not_merged_into_connection_streak_state_running(tmp_path):
    """04 §2.3 H06 (b) R6-32 逐字:拿不到 root「不计入 (a) 的 h06_fail_streak 三振、账号 state 保持 running(不进 degraded)」,不产生 H06 crit。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        rig.adb.whoami_after_root[SERIAL_01] = "shell"
        for _ in range(4):
            _past_grace(rig)
            rig.adb.state[SERIAL_01] = "device"
            _check_adb(client, rig)
        assert rig.agent.healthloop.h06_fail_streak.get(aid, 0) == 0, rig.agent.healthloop.h06_fail_streak
        assert _attr(active(rig, H06_ADB_OFFLINE, f"account:{aid}"), "severity") != "crit"
        assert state_of(rig, aid)[0] == "running"


def test_h06_root_alert_evidence_ensure_root_attempts(tmp_path):
    """04 §2.3 H06 (b) R6-35:`QIDIAN_NOT_ROOT` 的 `evidence.ensure_root_attempts` **就是** `qidian_root_fail_streak` 的当前值(两名同物)。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        rig.adb.whoami_after_root[SERIAL_01] = "shell"
        for _ in range(2):
            _past_grace(rig)
            rig.adb.state[SERIAL_01] = "device"
            _check_adb(client, rig)
        ev = _attr(active(rig, QIDIAN_NOT_ROOT, f"account:{aid}"), "evidence") or {}
        assert ev.get("ensure_root_attempts") == rig.agent.runtime.qidian_root_fail_streak.get(aid), (ev, rig.agent.runtime.qidian_root_fail_streak)


def test_h06_root_regained_resolves_without_state_change(tmp_path):
    """02 §5 企点拿不到 root 行:拿回 root 即 `resolved`,「全程 state 没动过」。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        rig.adb.whoami_after_root[SERIAL_01] = "shell"
        _past_grace(rig)
        rig.adb.state[SERIAL_01] = "device"
        _check_adb(client, rig)
        assert firing(rig, QIDIAN_NOT_ROOT, f"account:{aid}")
        rig.adb.whoami_after_root[SERIAL_01] = "root"
        _past_grace(rig)
        rig.adb.state[SERIAL_01] = "device"
        _check_adb(client, rig)
        assert not firing(rig, QIDIAN_NOT_ROOT, f"account:{aid}")
        assert rig.agent.runtime.qidian_root_fail_streak.get(aid, 0) == 0
        assert all(_payload(e).get("state") == "running" for e in state_events(rig, aid) if _payload(e).get("state") in ("running", "degraded", "error"))
        assert state_of(rig, aid)[0] == "running"


def test_h06_never_kill_server(tmp_path):
    """04 §2.3 H06 R6-12:三振 crit「只动该账号,不 kill-server」;拿不到 root 也不是 kill-server 时机——假 adb 没有该方法且调用记录里没有。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        _past_grace(rig)
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"
        for _ in range(4):
            _check_adb(client, rig)
        assert _attr(active(rig, H06_ADB_OFFLINE, f"account:{aid}"), "severity") == "crit"
        assert not hasattr(rig.adb, "kill_server")
        assert not any("kill" in c[0] or "kill-server" in c[1] for c in rig.adb.calls), rig.adb.calls


def test_h06_only_affects_that_account(tmp_path):
    """04 §2.3 H06 R6-12「只动该账号」:qd01 三振 crit 时,qd02 无 H06 告警、streak 为 0、仍 running。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        _past_grace(rig)
        _fail_connect_for(rig, {SERIAL_01})
        rig.adb.state[SERIAL_01] = "offline"
        rig.adb.state[SERIAL_02] = "device"
        for _ in range(3):
            _check_adb(client, rig)
            rig.adb.state[SERIAL_02] = "device"
        assert _attr(active(rig, H06_ADB_OFFLINE, f"account:{a1}"), "severity") == "crit"
        assert not firing(rig, H06_ADB_OFFLINE, f"account:{a2}")
        assert rig.agent.healthloop.h06_fail_streak.get(a2, 0) == 0
        assert state_of(rig, a2)[0] == "running"


# ══════════════════════════════════════════════════════════════════════ 八、H05 boot_completed 稳态(04 §2.3 H05 / 02 §3.7)
def test_h05_boot_completed_lost_is_crit(tmp_path):
    """04 §2.3 H05:稳态 `boot_completed` 从 1 变非 1 ⇒ `H05_BOOT_INCOMPLETE` crit(subject=account:<id>)。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        rig.adb.boot_completed[SERIAL_01] = "0"
        client.portal.call(rig.agent.healthloop.check_boot)
        al = active(rig, H05_BOOT_INCOMPLETE, f"account:{aid}")
        assert al is not None and _attr(al, "severity") == "crit"


def test_h05_resolved_when_boot_completed_back(tmp_path):
    """04 §2.3 H05:稳态丢失=Android 重启,「等它回来」——回 1 后 resolved。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        rig.adb.boot_completed[SERIAL_01] = "0"
        client.portal.call(rig.agent.healthloop.check_boot)
        assert firing(rig, H05_BOOT_INCOMPLETE, f"account:{aid}")
        rig.adb.boot_completed[SERIAL_01] = "1"
        client.portal.call(rig.agent.healthloop.check_boot)
        assert not firing(rig, H05_BOOT_INCOMPLETE, f"account:{aid}")
        assert any(p.get("state") == "resolved" for p in alert_events(rig, H05_BOOT_INCOMPLETE, f"account:{aid}"))


def test_h05_steady_state_ok_no_alert(tmp_path):
    """04 §2.3 H05:稳态 boot_completed=1 时不告警。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        client.portal.call(rig.agent.healthloop.check_boot)
        assert not firing(rig, H05_BOOT_INCOMPLETE, f"account:{aid}")


# ══════════════════════════════════════════════════════════════════════ 九、E-19 内存水位(02 §2.2.5 / 02 §5 / 05 §2.5.2 / 04 §7 [monitor])
def test_pressure_evaluate_levels_by_config(tmp_path):
    """04 §7 [monitor] `mem_warn_mb=2048` / `mem_critical_mb=1024`(02 [pool] 同名同默认):可用 <warn ⇒ warn,<critical ⇒ critical,否则 ok。"""
    cfg = AgentConfig(pool=PoolConfig(mem_warn_mb=2048, mem_critical_mb=1024))
    with rig_ctx(tmp_path, cfg=cfg) as rig:
        ev = rig.agent.pressure.evaluate
        assert ev(3000) == "ok"
        assert ev(2048) == "ok"
        assert ev(2047) == "warn"
        assert ev(1024) == "warn"
        assert ev(1023) == "critical"


def test_pressure_warn_alert_with_avail_evidence(tmp_path):
    """02 §2.2.5 ①:低于 `mem_warn_mb` ⇒ `alert MEM_PRESSURE`(warn),`evidence.avail_mb` 随告警上报(04 F-33)。"""
    with rig_ctx(tmp_path) as rig, api_ctx(rig) as client:
        probe(client, rig, 1500)
        assert rig.agent.pressure.level == "warn"
        al = active(rig, MEM_PRESSURE, "host")
        assert al is not None and _attr(al, "severity") == "warn"
        assert (_attr(al, "evidence") or {}).get("avail_mb") == 1500


def test_pressure_critical_crit_then_resolved(tmp_path):
    """02 §3.7 MEM_PRESSURE:`mem_critical_mb` 升 crit;02 §5:余量回升 ⇒ `resolved`、恢复新增。"""
    with rig_ctx(tmp_path) as rig, api_ctx(rig) as client:
        probe(client, rig, 500)
        assert rig.agent.pressure.level == "critical"
        assert _attr(active(rig, MEM_PRESSURE, "host"), "severity") == "crit"
        assert rig.agent.pressure.blocked() is True
        probe(client, rig, 4000)
        assert rig.agent.pressure.level == "ok"
        assert not firing(rig, MEM_PRESSURE, "host")
        assert rig.agent.pressure.blocked() is False
        assert create(client, "qidian").status_code == 201


def test_pressure_critical_blocks_post_accounts_409_mem_pressure(tmp_path):
    """02 §2.2.5 ②:critical ⇒ `POST /accounts` 一律 `409 RESOURCE_EXHAUSTED`(`error.reason='mem_pressure'`),并带 `error.alternatives`(LRU 建议名单)。"""
    with rig_ctx(tmp_path) as rig, api_ctx(rig) as client:
        probe(client, rig, 500)
        r = create(client, "qidian")
        assert r.status_code == 409, r.text
        j = r.json()
        assert j.get("ok") is False and j.get("code") == "RESOURCE_EXHAUSTED"
        assert j["error"].get("reason") == "mem_pressure"
        assert isinstance(j["error"].get("alternatives"), list)


def test_pressure_critical_blocks_recover(tmp_path):
    """02 §2.2.5 ②:critical ⇒ §2.6 自动恢复 `recover()` 同样 `409 RESOURCE_EXHAUSTED(mem_pressure)`——不拉起任何账号。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        client.post(f"{P}/accounts/{aid}/stop", headers=H())
        wait_idle(client, rig, aid)
        rig.store.transition(aid, "stopped", desired_state="running")     # 模拟「开机前本应在跑」的自恢复判据
        n = count_calls(rig.containers.calls, "start", CNAME_01)
        probe(client, rig, 500)
        res = client.portal.call(_call, rig.agent.accounts.recover)
        wait_idle(client, rig, aid)
        assert count_calls(rig.containers.calls, "start", CNAME_01) == n, "critical 下自动恢复不得拉起"
        assert state_of(rig, aid)[0] == "stopped"
        if res is not None:
            s = json.dumps(res, default=lambda o: getattr(o, "__dict__", str(o)), ensure_ascii=False)
            assert "mem_pressure" in s, s


def test_pressure_lru_list_oldest_first_with_shape(tmp_path):
    """02 §2.2.5:LRU 名单每项 `{account_id, last_active_at, rss_mb}`,`last_active_at`=该账号最近一次收发,**最久没收发的排前面**。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        ingest_in(rig, a1, "415011447", 1)
        rig.clock.advance(60_000)
        ingest_in(rig, a2, "415011448", 1)
        probe(client, rig, 500)
        r = create(client, "qidian")
        assert r.status_code == 409, r.text
        alts = r.json()["error"]["alternatives"]
        lru = [x for x in alts if isinstance(x, dict) and "account_id" in x]
        assert [x["account_id"] for x in lru][:2] == [a1, a2], alts
        for x in lru:
            assert {"account_id", "last_active_at", "rss_mb"} <= set(x.keys()), x


def test_pressure_lru_order_flips_with_recent_activity(tmp_path):
    """02 §2.2.5 / 05 §2.5.2:名单按最近一次收发时间升序——最近有收发的账号往后排。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        ingest_in(rig, a2, "415011448", 1)
        rig.clock.advance(60_000)
        ingest_in(rig, a1, "415011447", 1)
        probe(client, rig, 500)
        alts = create(client, "qidian").json()["error"]["alternatives"]
        ids = [x["account_id"] for x in alts if isinstance(x, dict) and "account_id" in x]
        assert ids[:2] == [a2, a1], alts


def test_pressure_alert_evidence_lru_suggest(tmp_path):
    """02 §3.7 MEM_PRESSURE:`evidence` 带 `avail_mb` 与 LRU 建议名单 `lru_suggest[]`,顺序与 `error.alternatives` 一致。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        ingest_in(rig, a1, "415011447", 1)
        rig.clock.advance(60_000)
        ingest_in(rig, a2, "415011448", 1)
        probe(client, rig, 500)
        ev = _attr(active(rig, MEM_PRESSURE, "host"), "evidence") or {}
        assert ev.get("avail_mb") == 500
        lru = ev.get("lru_suggest")
        assert isinstance(lru, list) and [x["account_id"] for x in lru][:2] == [a1, a2], ev
        assert all({"account_id", "last_active_at", "rss_mb"} <= set(x.keys()) for x in lru)


def test_pressure_default_only_suggests_never_stops(tmp_path):
    """02 §2.2.5「只建议不自动停」/ 04 F-34「不自动停用户账号」:`auto_stop_on_pressure` 默认 false ⇒ `enforce()` 停 0 个,在跑账号不动。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        assert settings_of(rig, a1).get("auto_stop_on_pressure", False) is False
        probe(client, rig, 500)
        stopped = client.portal.call(rig.agent.pressure.enforce)
        wait_idle(client, rig, a1)
        wait_idle(client, rig, a2)
        assert list(stopped or []) == []
        assert state_of(rig, a1)[0] == "running" and state_of(rig, a2)[0] == "running"
        assert not rig.store.list_audit(action="stop") or all(a.get("account_id") not in (a1, a2) for a in rig.store.list_audit(action="stop"))


def test_pressure_auto_stop_stops_lru_and_audits(tmp_path):
    """02 §2.2.5 / 05 §2.5.2 唯一自动停例外:`settings_json.auto_stop_on_pressure=true` 的账号才按 LRU 逐个 `stop`,每次 `stop` 记审计;未开的不动。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        ingest_in(rig, a1, "415011447", 1)
        rig.clock.advance(60_000)
        ingest_in(rig, a2, "415011448", 1)
        probe(client, rig, 500)                                  # 两账号都未开开关:critical 只出建议
        wait_idle(client, rig, a1)
        wait_idle(client, rig, a2)
        assert state_of(rig, a1)[0] == "running" and state_of(rig, a2)[0] == "running"
        r = client.patch(f"{P}/accounts/{a1}/settings", json={"auto_stop_on_pressure": True}, headers=H())
        assert r.status_code == 200, r.text
        n_audit = len(rig.store.list_audit())
        stopped = client.portal.call(rig.agent.pressure.enforce)
        wait_idle(client, rig, a1)
        wait_idle(client, rig, a2)
        assert list(stopped) == [a1], stopped
        assert state_of(rig, a1)[0] == "stopped"
        assert state_of(rig, a2)[0] == "running", "未开 auto_stop_on_pressure 的账号不得被系统停掉"
        new_audit = rig.store.list_audit()[n_audit:]
        assert any("stop" in str(a.get("action", "")) and a.get("account_id") == a1 for a in new_audit), new_audit


def test_pressure_auto_stop_until_back_above_warn(tmp_path):
    """02 §2.2.5:开了自动停的账号「按 LRU 逐个 stop 直到 win_available_mb ≥ mem_warn_mb」——停掉最久空闲的一个后余量回到 warn 线以上即停手。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        ingest_in(rig, a1, "415011447", 1)
        rig.clock.advance(60_000)
        ingest_in(rig, a2, "415011448", 1)
        probe(client, rig, 500)                                  # 先在未开开关时进入 critical(只建议不停)
        wait_idle(client, rig, a1)
        wait_idle(client, rig, a2)
        assert rig.agent.pressure.level == "critical"
        for a in (a1, a2):
            assert client.patch(f"{P}/accounts/{a}/settings", json={"auto_stop_on_pressure": True}, headers=H()).status_code == 200
        seq = iter([500, 2100, 2100, 2100])
        last = {"v": 500}

        def read_avail():
            try:
                last["v"] = next(seq)
            except StopIteration:
                pass
            return last["v"]

        stopped = client.portal.call(functools.partial(rig.agent.pressure.enforce, read_avail=read_avail))
        wait_idle(client, rig, a1)
        wait_idle(client, rig, a2)
        assert list(stopped) == [a1], stopped
        assert state_of(rig, a2)[0] == "running"


def test_pressure_warn_does_not_stop_anyone(tmp_path):
    """05 §2.5.2:`warn` ⇒ 只告警、控制台显红,**不停任何账号**(即便开了 auto_stop_on_pressure)。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig)
        assert client.patch(f"{P}/accounts/{a1}/settings", json={"auto_stop_on_pressure": True}, headers=H()).status_code == 200
        probe(client, rig, 1500)
        stopped = client.portal.call(rig.agent.pressure.enforce)
        wait_idle(client, rig, a1)
        assert list(stopped or []) == []
        assert state_of(rig, a1)[0] == "running"


def test_pressure_warn_does_not_block_post_accounts(tmp_path):
    """02 §2.2.5 / 05 §2.5.2:阻断新增只在 `critical`;`warn` 仅告警。
    ⚠️ 与 04 F-33「level=warn … 暂停新增账号与自动恢复」字面相悖——本用例按 02(判据与算法的 owner)写,若失败请当规格矛盾分诊。"""
    with rig_ctx(tmp_path) as rig, api_ctx(rig) as client:
        probe(client, rig, 1500)
        assert rig.agent.pressure.level == "warn"
        assert rig.agent.pressure.blocked() is False
        assert create(client, "qidian").status_code == 201


def test_pressure_unknown_when_winagent_offline_does_not_block(tmp_path):
    """02 §2.2.5「WinAgent 读失败沿用上次」/ 04 §2.4.2 抑制:WinAgent 离线时水位 `unknown`,不阻断新增。"""
    with rig_ctx(tmp_path) as rig, api_ctx(rig) as client:
        probe(client, rig, offline=True)
        assert rig.agent.pressure.level == "unknown"
        assert rig.agent.pressure.blocked() is False
        assert create(client, "qidian").status_code == 201
        assert not firing(rig, MEM_PRESSURE, "host")


def test_pressure_lru_suggest_excludes_not_running(tmp_path):
    """02 §2.2.5 / 05 §2.5.2:LRU 名单针对「已经在跑」的账号——stopped 的账号不进建议名单。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        client.post(f"{P}/accounts/{a2}/stop", headers=H())
        wait_idle(client, rig, a2)
        probe(client, rig, 500)
        lru = rig.agent.pressure.lru_suggest()
        ids = [_attr(x, "account_id") for x in lru]
        assert a1 in ids and a2 not in ids, lru


# ══════════════════════════════════════════════════════════════════════ 十、#20 能力矩阵 / #22 settings / #23 batch
def test_capabilities_matrix_login_required(tmp_path):
    """02 #20:`matrix:{op:'supported|unsupported|not_applicable'}` 按「该账号当前实际可用」——login_required 下 IM 写类不可用、screenshot/get_state 可用。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = to_login_required(client, rig)
        r = client.get(f"{P}/accounts/{aid}/capabilities", headers=H())
        assert r.status_code == 200, r.text
        d = _data(r)
        assert isinstance(d.get("capabilities"), list) and isinstance(d.get("matrix"), dict)
        m = d["matrix"]
        assert set(m.values()) <= {"supported", "unsupported", "not_applicable"}, m
        assert m.get("send_text") in ("unsupported", "not_applicable"), m
        assert m.get("get_state") == "supported", m
        if "screenshot" in m:
            assert m["screenshot"] == "supported", m


def test_capabilities_matrix_running(tmp_path):
    """02 #20:running 下 `send_text` 为 supported。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        m = _data(client.get(f"{P}/accounts/{aid}/capabilities", headers=H()))["matrix"]
        assert m.get("send_text") == "supported", m


def test_settings_patch_keys_and_columns(tmp_path):
    """02 #22:键子集写 `accounts.settings_json`,其中 `auto_recover/capture_text/retention_days` 落各自列、其余留 JSON。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian"))
        body = {"send": {"min_interval_ms": 2000, "jitter_ms": 300, "max_per_minute": 10}, "sessions": {"allowlist": ["qd01:1"]},
                "gates": {"custom": []}, "auto_recover": False, "capture_text": False, "retention_days": 7, "auto_stop_on_pressure": True}
        r = client.patch(f"{P}/accounts/{aid}/settings", json=body, headers=H())
        assert r.status_code == 200, r.text
        row = full(rig, aid)
        assert not row["auto_recover"] and not row["capture_text"] and row["retention_days"] == 7
        s = settings_of(rig, aid)
        assert s.get("send", {}).get("min_interval_ms") == 2000 and s.get("send", {}).get("max_per_minute") == 10
        assert s.get("sessions", {}).get("allowlist") == ["qd01:1"]
        assert s.get("auto_stop_on_pressure") is True
        assert "retention_days" not in s and "capture_text" not in s and "auto_recover" not in s, s


def test_settings_retention_days_over_30_is_400(tmp_path):
    """02 #22 E-18 / 05 §2.5.5:`retention_days > 30 → 400`。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian"))
        r = client.patch(f"{P}/accounts/{aid}/settings", json={"retention_days": 31}, headers=H())
        assert r.status_code == 400, r.text
        assert r.json().get("code") == "INVALID_ARGS"
        assert full(rig, aid).get("retention_days") != 31
        ok = client.patch(f"{P}/accounts/{aid}/settings", json={"retention_days": 30}, headers=H())
        assert ok.status_code == 200, ok.text


def test_settings_mail_route_id_must_exist_400(tmp_path):
    """02 #22 / 05 §2.5.5:`mail_route_id` 须指向 `mail_routes` 中同账号或同通道的行,否则 `400`。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian"))
        r = client.patch(f"{P}/accounts/{aid}/settings", json={"mail_route_id": "route-不存在"}, headers=H())
        assert r.status_code == 400, r.text
        assert "mail_route_id" not in settings_of(rig, aid)


def test_settings_allowlist_takes_effect_immediately_on_bus(tmp_path):
    """02 #22「立即生效」/ 05 §2.5.5「网关侧读账号设置是每条指令时读」:PATCH allowlist 后总线立刻按新值判 GATE_BLOCKED,再放宽立刻放行。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        assert client.patch(f"{P}/accounts/{aid}/settings", json={"sessions": {"allowlist": ["qd01:1"]}}, headers=H()).status_code == 200
        r1 = client.portal.call(functools.partial(submit, rig, aid, session=f"{aid}:415011447"))
        assert r1.code == "GATE_BLOCKED", r1
        assert client.patch(f"{P}/accounts/{aid}/settings", json={"sessions": {"allowlist": ["*"]}}, headers=H()).status_code == 200
        r2 = client.portal.call(functools.partial(submit, rig, aid, session=f"{aid}:415011447"))
        assert r2.code != "GATE_BLOCKED", r2


def test_settings_patch_writes_audit(tmp_path):
    """05 §2.5.5:设置改动经 PATCH「写 audit_log」。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian"))
        n = len(rig.store.list_audit())
        assert client.patch(f"{P}/accounts/{aid}/settings", json={"retention_days": 7}, headers=H()).status_code == 200
        assert len(rig.store.list_audit()) > n


def test_settings_auto_stop_on_pressure_default_false(tmp_path):
    """05 §2.5.5 / 04 §7:`auto_stop_on_pressure` 默认 false——新账号不带该键或为 false。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = created_id(create(client, "qidian"))
        assert settings_of(rig, aid).get("auto_stop_on_pressure", False) is False


def test_batch_rejects_star(tmp_path):
    """02 #23:`{ids:[…]}` 显式列 id,**不接受 `*`** ⇒ 400 INVALID_ARGS。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        up_running(client, rig)
        r = client.post(f"{P}/accounts/batch", json={"ids": ["*"], "action": "stop"}, headers=H())
        assert r.status_code == 400, r.text
        assert r.json().get("code") == "INVALID_ARGS"


def test_batch_results_per_id_partial_failure_no_rollback(tmp_path):
    """02 #23:响应 `{results:{id:{ok, code}}}`;内部逐个执行,部分失败不回滚——存在的账号照常 stop,不存在的单独报错。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        aid = up_running(client, rig)
        r = client.post(f"{P}/accounts/batch", json={"ids": [aid, "qd99"], "action": "stop"}, headers=H())
        assert r.status_code == 200, r.text
        res = _data(r)["results"]
        assert res[aid]["ok"] is True and "code" in res[aid]
        assert res["qd99"]["ok"] is False and res["qd99"]["code"]
        wait_idle(client, rig, aid)
        assert state_of(rig, aid)[0] == "stopped"


def test_batch_start_stays_globally_serial(tmp_path):
    """02 #23「start 仍全局串行」/ 05 §2.0 启动串行:批量 start 两个账号,同时在拉起的容器数 ≤ 1。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        a1 = up_running(client, rig, label="一")
        a2 = up_running(client, rig, label="二")
        for a in (a1, a2):
            client.post(f"{P}/accounts/{a}/stop", headers=H())
            wait_idle(client, rig, a)
        r = client.post(f"{P}/accounts/batch", json={"ids": [a1, a2], "action": "start"}, headers=H())
        assert r.status_code == 200, r.text
        wait_idle(client, rig, a1)
        wait_idle(client, rig, a2)
        assert state_of(rig, a1)[0] == "running" and state_of(rig, a2)[0] == "running"
        assert rig.agent.runtime.max_concurrent_starts <= 1


# ══════════════════════════════════════════════════════════════════════ 十一、/system/health 与调度周期
def test_system_health_checks_tristate_and_mem(tmp_path):
    """02 §3.4 `GET /system/health`(带令牌):`checks` 含 H04/H05/H06/H24 三态项,`mem{level, avail_mb}`。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        up_running(client, rig)
        r0 = client.get(f"{P}/system/health", headers=H())
        assert r0.status_code == 200, r0.text
        c0 = _data(r0).get("checks") or {}
        assert {"H04", "H05", "H06", "H24"} <= set(c0.keys()), c0
        assert all(c0[k] in TRI_STATE for k in ("H04", "H05", "H06", "H24")), c0
        for fn in (rig.agent.healthloop.check_containers, rig.agent.healthloop.check_adb, rig.agent.healthloop.check_boot):
            client.portal.call(fn)
        probe(client, rig, 1500)
        d = _data(client.get(f"{P}/system/health", headers=H()))
        checks = d.get("checks") or {}
        assert checks["H04"] == "ok" and checks["H05"] == "ok" and checks["H06"] == "ok", checks
        assert checks["H24"] == "firing", checks
        mem = d.get("mem") or checks.get("mem") or {}
        assert mem.get("level") == "warn" and mem.get("avail_mb") == 1500, mem


def test_system_health_h04_reflects_container_exit(tmp_path):
    """02 §3.4 `/system/health.checks.H04` 与告警联动:容器退出 ⇒ H04 项为 crit。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig, api_ctx(rig) as client:
        up_running(client, rig)
        _exit(rig)
        _check_containers(client, rig)
        checks = _data(client.get(f"{P}/system/health", headers=H())).get("checks") or {}
        assert checks.get("H04") == "firing", checks
        assert firing(rig, H04_CONTAINER_EXITED, "account:qd01")


def test_scheduler_health_periods_from_config(tmp_path):
    """04 §7 [health] `container_check_s=10`、`adb_check_s=30`;04 §2.3 H05 稳态 60 s;05 §7 `login_remind_interval_s=300`——调度器注册周期与之一致。"""
    with rig_ctx(tmp_path) as rig:
        jobs = rig.agent.scheduler.jobs
        assert _attr(jobs["health_containers"], "interval_s") == 10
        assert _attr(jobs["health_adb"], "interval_s") == 30
        assert _attr(jobs["health_boot"], "interval_s") == 60
        assert 0 < _attr(jobs["login_remind"], "interval_s") <= 300


def test_scheduler_health_periods_follow_overrides(tmp_path):
    """04 §7 [health]:周期来自配置键(同名),改配置即改周期。"""
    cfg = AgentConfig(health=HealthConfig(container_check_s=7, adb_check_s=13))
    with rig_ctx(tmp_path, cfg=cfg) as rig:
        jobs = rig.agent.scheduler.jobs
        assert _attr(jobs["health_containers"], "interval_s") == 7
        assert _attr(jobs["health_adb"], "interval_s") == 13
