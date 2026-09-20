"""按设计文档写的独立验收用例 —— 第三批:runtime / pool / accounts 状态机 / WinAgent 契约 / 校时 / 恢复。

🔴 断言只依据规格,不按实现反推。规格出处(册 §节 / 端点编号 / 裁决号):
- 00 §3 端口表(段基址 + NN、01–98、16099 保留)、§7.6 ResourcePool 形态、§8.1 Account.state 状态机(不跳段、0 秒停留也发事件、
  R6-32 提权失败仍 running)、§10 API 基线(前缀 / 错误信封 / HTTP 映射)。
- 02 §2.2.4 runtime(端口推导 C-07、启动串行 start_lock、_purge_ephemeral E-19 的 ①~⑤ 与「绝不动」、幂等、审计 detail_json)、
  §2.2.5 pool(used 公式、can_add 三通道、微信槽位空值口径、行级 claim、stopped 不占额度但 start 再过 can_add)、
  §2.5 Agent↔WinAgent 契约(超时 / 重试 / X-WA-Version / 降级)、§2.6 恢复规则与 error_since_ms 两个动作(R5-4/R6-4)、
  §3.4 通用、§3.4.1 #1/#2/#3/#5/#6/#7/#9/#10/#11/#19 + Account 序列化表(R6-4/R6-53)、#69 GET /resources、§3.6 WinAgent 端点、
  §7.1 [winagent]/[runtime]/[pool]/[wechat] 默认值、§3.1 DDL(accounts / account_runtime / resource_pools / settings)。
- 05 §2.1.1 企点冷启动全序(⑤ boot 超时、⑤b ensure_root 位置、⑪ 三类判定)、§2.5.2 运行集合与操作集合、§2.5.7 容器停止即清缓存。
- 04 §2.3 H02/H03/H06/H13、§2.9 时间同步、§2.10 睡眠唤醒。
- 06 §2.9.5 ensure_root(三步逐字、只动本账号、绝不 kill-server、mark_rooting、失败语义)。

夹具全部来自 tests/conftest.py 的 make_rig(全假后端:FakeContainers / FakeAdb / FakeVault / FakeWinAgent / FakeFs),绝不碰真 docker/adb/WinAgent。
HTTP 用例走 starlette TestClient;凡用了 TestClient 的用例,后台协程一律经 client.portal.call 在同一事件循环里跑;
纯服务层用例用 async def 直接 await。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional

import pytest
from starlette.testclient import TestClient

from qtrade_agent.alerts import H13_CLOCK_DRIFT, QIDIAN_NOT_ROOT
from qtrade_agent.config import AgentConfig, HealthConfig, RuntimeConfig, WinAgentConfig
from qtrade_agent.vault_client import VaultUnavailable, WinAgentVault
from qtrade_agent.winagent_client import WinAgentClient, WinAgentUnavailable

try:
    from tests.conftest import Clock, make_rig
except ImportError:                                   # pytest 以 rootdir 载入 conftest 时的别名
    from conftest import Clock, make_rig              # type: ignore

# ────────────────────────────────────────────────────────────────────── 常量(规格自抄)
P = "/api/v1"                                          # 00 §10 前缀
ROOT = "/var/lib/qtrade"                               # 02 §2.2.4 卷目录根 /var/lib/qtrade/accounts/<id>/data
TOK_A = "tok-a"                                        # admin
TOK_W = "tok-w"                                        # write
TOK_R = "tok-r"                                        # read
QUOTA = {"qidian": 2560, "qq": 614, "wechat": 1536}    # 02 §7.1 [pool] quota_*(仅建表初始值)
WSL_RESERVED = 2048                                    # 02 §7.1 [pool] wsl_reserved_mb
STATE_KEYS = {"state", "state_code", "state_reason", "adapter_state", "last_seen_at", "error_since_ms"}   # 02 #19
SLOT_KEYS = {"used", "max", "holder", "pending", "pending_expires_at", "pending_login_session_id"}       # 00 §7.6 / 02 §2.2.5


def serial(seq: int) -> str:
    """00 §3 / 02 §2.2.4:企点 adb 串 = 127.0.0.1:(16000+NN)。"""
    return f"127.0.0.1:{16000 + seq}"


def H(tok: Optional[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ────────────────────────────────────────────────────────────────────── 夹具
async def login_ok(row, account, secret):
    """执行层桩:拿到凭据就登录成功(05 §2.1.1 ⑪ a)。"""
    return "running"


@contextmanager
def rig_ctx(tmp_path, **kw):
    r = make_rig(tmp_path, **kw)
    try:
        yield r
    finally:
        r.store.close()


def install_tokens(rig) -> None:
    rig.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_A)
    rig.store.upsert_api_client(app_id="bot_w", name="写机器人", level="write", token=TOK_W)
    rig.store.upsert_api_client(app_id="bot_r", name="读机器人", level="read", token=TOK_R)


@contextmanager
def api_ctx(rig):
    install_tokens(rig)
    with TestClient(rig.agent.create_api(), client=("127.0.0.1", 40000)) as c:
        yield c


@pytest.fixture
def rig(tmp_path):
    with rig_ctx(tmp_path, login_fn=login_ok) as r:
        yield r


@pytest.fixture
def client(rig):
    with api_ctx(rig) as c:
        yield c


# ────────────────────────────────────────────────────────────────────── 小工具
def create(client, channel: str = "qidian", label: Optional[str] = None, *, account: Optional[str] = None,
           secret: Optional[str] = None, remember: Optional[bool] = None, key: Optional[str] = None,
           mode: Optional[str] = None, tok: str = TOK_A, with_key: bool = True):
    """02 #2:`{channel, label, login:{mode, account?, secret?, remember}, idempotency_key}`。"""
    login: dict[str, Any] = {"mode": mode or ("password" if channel == "qidian" else "qrcode")}
    if account is not None:
        login["account"] = account
    if secret is not None:
        login["secret"] = secret
    if remember is not None:
        login["remember"] = remember
    body: dict[str, Any] = {"channel": channel, "label": label or f"{channel}-号", "login": login}
    if with_key:
        body["idempotency_key"] = key or f"k-{uuid.uuid4().hex[:12]}"
    return client.post(f"{P}/accounts", json=body, headers=H(tok))


def created_id(resp) -> str:
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def idle(client, rig, aid: str) -> None:
    client.portal.call(rig.agent.accounts.wait_idle, aid)


def act(client, rig, aid: str, action: str, tok: str = TOK_A, body: Optional[dict] = None):
    r = client.post(f"{P}/accounts/{aid}/{action}", json=body, headers=H(tok))
    idle(client, rig, aid)
    return r


def full(rig, aid: str) -> dict:
    row = rig.store.get_account_full(aid)
    assert row is not None, aid
    return row


def state_events(rig, aid: str) -> list[dict]:
    evs = rig.store.list_events(event="account_state", account_id=aid)
    return sorted(evs, key=lambda e: e["ts_ms"])          # 稳定排序:同 ts 保持插入序


def state_seq(rig, aid: str, *, drop_created: bool = True) -> list[str]:
    seq = [e["payload"]["state"] for e in state_events(rig, aid)]
    return [s for s in seq if not (drop_created and s == "created")]


def detail(row: dict) -> dict:
    d = row.get("detail_json")
    return json.loads(d) if isinstance(d, str) else (d or {})


def purge_audits(rig) -> list[dict]:
    return sorted(rig.store.list_audit(action="runtime.purge_ephemeral"), key=lambda r: r["ts_ms"])


def unwrap(body: dict) -> dict:
    """单对象端点的信封:02 §3.4 通用只钉了分页列表 `{ok:true, data:[…]}`;单对象(#19 /state、#69 /resources)规格未钉是否包 data,
    这里兼容「包 data」与「顶层平铺(去掉 ok)」两种,断言只针对业务字段。"""
    assert body.get("ok") is True, body
    if "data" in body:
        return body["data"]
    return {k: v for k, v in body.items() if k != "ok"}


def envelope_error(body: dict, code: str) -> dict:
    """00 §10:`{ok:false, code, error:{message, reason?, retryable, needs_human}, trace_id}`。"""
    assert body["ok"] is False, body
    assert body["code"] == code, body
    assert {"message", "retryable", "needs_human"} <= set(body["error"]), body
    return body["error"]


def call_index(calls: list, kind: str, needle: Optional[str] = None, start: int = 0) -> int:
    for i in range(start, len(calls)):
        k, a = calls[i][0], calls[i][1]
        if k == kind and (needle is None or needle in a):
            return i
    raise AssertionError(f"adb.calls 里没有 {kind!r} {needle!r}(自 {start} 起):{calls[start:]}")


def all_db_text(rig) -> str:
    """把 agent.db 所有表所有行拼成一个大字符串(用来断言 secret 没落到任何一张表)。"""
    parts = []
    tables = [r[0] for r in rig.store.con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for t in tables:
        try:
            for row in rig.store.con.execute(f'SELECT * FROM "{t}"').fetchall():
                parts.append(repr(tuple(row)))
        except Exception:                                 # noqa: BLE001 —— FTS 影子表等读不了就跳过
            pass
    return "\n".join(parts)


def bring_windows_online(rig, client=None, *, wechat_enabled: bool = True) -> None:
    """让 windows 池 known 且 wechat_enabled 为真(02 §2.2.5:wechat_enabled 是 GET /wa/v1/health 的快照)。"""
    rig.winagent.wechat_enabled = wechat_enabled
    rig.agent.pool.set_windows(total_mb=16384, wechat_enabled=wechat_enabled, known=True)
    if client is not None:
        client.portal.call(rig.agent.winagent_probe)


# ══════════════════════════════════════════════════════════════════════ 一、端口推导(00 §3 / 02 §2.2.4)

def _port(d: dict, name: str) -> Any:
    """02 §2.2.4 只给了 `port_plan(channel, seq) -> dict`,键名按伪代码 `adb/stream/frida/ws/http/webui`;兼容 DDL 列名 `*_port`。"""
    if name in d:
        return d[name]
    return d.get(f"{name}_port")


def test_port_plan_qidian_values(rig):
    """02 §2.2.4 端口推导 / 00 §3:qidian adb=16000+seq、stream=16500+seq、frida=16600+seq;qd07 ⇒ 16007/16507/16607。"""
    d = rig.agent.runtime.port_plan("qidian", 7)
    assert _port(d, "adb") == 16007 and _port(d, "stream") == 16507 and _port(d, "frida") == 16607, d
    assert d.get("adb_serial") == serial(7), d          # account_runtime.adb_serial '127.0.0.1:160NN'(04-P7)


def test_port_plan_qq_values(rig):
    """02 §2.2.4 / 00 §3:qq ws=16100+seq、http=16200+seq、webui=16300+seq;qq03 ⇒ 16103/16203/16303。"""
    d = rig.agent.runtime.port_plan("qq", 3)
    assert _port(d, "ws") == 16103 and _port(d, "http") == 16203 and _port(d, "webui") == 16303, d


def test_port_plan_wechat_has_no_ports(rig):
    """02 §2.2.4:wechat 无端口(chatlog 固定 5030 由 WinAgent 管)——推导结果里不得出现任何 16xxx 段端口。"""
    d = rig.agent.runtime.port_plan("wechat", 1)
    nums = [v for v in d.values() if isinstance(v, int)]
    assert not any(16000 <= v <= 16699 for v in nums), d


def test_port_plan_seq_boundaries(rig):
    """00 §3 / 02 §2.2.4:NN ∈ 01–98;16000 是 adb server、16099 是安装自检保留 ⇒ seq 0 与 99 必须拒绝。"""
    assert _port(rig.agent.runtime.port_plan("qidian", 1), "adb") == 16001
    assert _port(rig.agent.runtime.port_plan("qidian", 98), "adb") == 16098
    with pytest.raises(Exception):
        rig.agent.runtime.port_plan("qidian", 0)
    with pytest.raises(Exception):
        rig.agent.runtime.port_plan("qidian", 99)


def test_account_runtime_redundant_ports_match_derivation(rig, client):
    """02 §2.2.4:`account_runtime` 冗余存推导端口但真值永远是推导;§3.1 DDL 列 adb_port/stream_port/frida_port/adb_serial/
    ws_port/http_port/webui_port/container_name/data_dir 与推导一致。"""
    assert created_id(create(client, "qidian")) == "qd01"
    assert created_id(create(client, "qq")) == "qq01"
    rt = rig.store.get_runtime("qd01")
    assert (rt["adb_port"], rt["stream_port"], rt["frida_port"]) == (16001, 16501, 16601), rt
    assert rt["adb_serial"] == serial(1)
    assert rt["container_name"] == "qtrade-qd01"                       # §3.1 DDL 注释 'qtrade-qd01'
    assert rt["data_dir"] == f"{ROOT}/accounts/qd01/data"              # §2.2.4 卷目录
    qq = rig.store.get_runtime("qq01")
    assert (qq["ws_port"], qq["http_port"], qq["webui_port"]) == (16101, 16201, 16301), qq
    assert qq["container_name"] == "qtrade-qq01"


def test_adb_serial_follows_seq_for_each_account(rig, client):
    """00 §3:每企点账号一个 adb 端口,从 16001 起按序号分配 ⇒ qd01/qd02/qd03 的 adb_serial = 127.0.0.1:16001/16002/16003。"""
    ids = [created_id(create(client, "qidian", f"企点{i}")) for i in range(3)]
    assert ids == ["qd01", "qd02", "qd03"]
    for i, aid in enumerate(ids, start=1):
        assert full(rig, aid)["adb_serial"] == serial(i)


# ══════════════════════════════════════════════════════════════════════ 二、POST /accounts(02 #2)

def test_post_accounts_requires_idempotency_key(rig, client):
    """02 §3.4 通用:幂等列 `key` = 必带 `idempotency_key`;#2 是 key 端点 ⇒ 缺失 400 INVALID_ARGS(基线 §10 映射)。"""
    r = create(client, "qidian", with_key=False)
    assert r.status_code == 400, r.text
    envelope_error(r.json(), "INVALID_ARGS")
    assert rig.store.con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_post_accounts_201_account_envelope(rig, client):
    """02 #2:`201 Account`;Account 序列化表(R6-4/R6-53):state_code 为 NULL 时给 `""` 不给 null、error_since_ms 仅 error 态非空;
    §3.1 DDL:state 默认 created、host=wsl、quota_mb 从 resource_pools.quota_json 拷贝;runtime 子对象带推导端口。"""
    r = create(client, "qidian", "张三-固收")
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["ok"] is True
    a = b["data"]
    assert a["id"] == "qd01" and a["channel"] == "qidian" and a["host"] == "wsl" and a["label"] == "张三-固收"
    assert a["state"] == "created"
    assert a["state_code"] == "", a                     # R6-53:"" 不是 null
    assert a["error_since_ms"] is None
    assert a["quota_mb"] == QUOTA["qidian"]
    assert a["enabled"] is True
    assert a["login"]["mode"] == "password"
    assert "credential_ref" in a["login"] and "remember" in a["login"]
    # 02 #3:Account 含 runtime 子对象;05 §2.1.1 ②:分配时 adb=16000+NN(runtime 子对象的完整键集由 00 §7.1 定,本文件不越权断言)
    assert isinstance(a["runtime"], dict) and 16001 in a["runtime"].values(), a["runtime"]


def test_post_accounts_id_allocated_from_settings_seq(rig, client):
    """02 §3.1 DDL:id 由 settings.seq.<channel> 单调分配,通道内独立计数(qd01 与 qq01 都是 01,02 §2.2.4)。"""
    assert created_id(create(client, "qidian", "甲")) == "qd01"
    assert created_id(create(client, "qidian", "乙")) == "qd02"
    assert created_id(create(client, "qq", "丙")) == "qq01"
    assert int(rig.store.settings_get("seq.qidian")) == 2
    assert int(rig.store.settings_get("seq.qq")) == 1
    assert rig.store.con.execute("SELECT seq FROM accounts WHERE id='qd02'").fetchone()[0] == 2


def test_post_accounts_id_not_reused_after_delete(rig, client):
    """02 #7 / §3.1:软删后 id 仍占用、不复用;05 §2.1.1 ②「含已删除的不复用」⇒ 删掉 qd01 再建得 qd02。"""
    assert created_id(create(client, "qidian", "甲")) == "qd01"
    r = client.delete(f"{P}/accounts/qd01", params={"confirm": "甲"}, headers=H(TOK_A))
    assert r.status_code == 200, r.text
    idle(client, rig, "qd01")
    assert created_id(create(client, "qidian", "乙")) == "qd02"
    assert int(rig.store.settings_get("seq.qidian")) == 2


def test_post_accounts_secret_written_to_vault_not_echoed_not_persisted(rig, client):
    """02 #2:`secret` 只在此一次传输,写 Vault 后丢弃;§3.1 credential_ref='vault://account/qd01';
    §3.6 #9 响应不回显值 ⇒ 响应体、agent.db 任一表(含 audit_log)都不得含密码原文。"""
    secret = "P@ss-秘密-9x7Q"
    r = create(client, "qidian", "甲", account="13800000000", secret=secret, remember=True)
    assert r.status_code == 201, r.text
    assert secret not in r.text
    a = r.json()["data"]
    ref = a["login"]["credential_ref"]
    assert ref == "vault://account/qd01", a["login"]
    name = ref.removeprefix("vault://")
    assert name in rig.vault.entries and rig.vault.entries[name].value == secret
    assert secret not in all_db_text(rig)


def test_post_accounts_remember_defaults_false_and_no_vault_write(rig, client):
    """02 §3.1 DDL remember(R4-6):默认不存;不勾「记住」不得落密文 ⇒ 不传 remember 时 remember=0、credential_ref NULL、Vault 无条目。"""
    secret = "only-once-秘密"
    r = create(client, "qidian", "甲", account="13800000000", secret=secret)
    assert r.status_code == 201, r.text
    a = r.json()["data"]
    assert a["login"]["remember"] is False
    assert a["login"]["credential_ref"] is None
    row = full(rig, "qd01")
    assert row["remember"] == 0 and row["credential_ref"] is None
    assert not any(e.value == secret for e in rig.vault.entries.values())
    assert secret not in all_db_text(rig)


def test_post_accounts_resource_exhausted_409_with_alternatives(tmp_path):
    """02 #2 / §2.2.5:wsl_budget = total - reserved(2048)< quota.qidian(2560)⇒ 409 RESOURCE_EXHAUSTED + error.alternatives(C.3.3);
    05 §2.1.1 ①:给替代(可改开 QQ / 停用一个企点)。"""
    with rig_ctx(tmp_path, wsl_total_mb=4096, login_fn=login_ok) as rig, api_ctx(rig) as client:
        r = create(client, "qidian", "甲")
        assert r.status_code == 409, r.text
        err = envelope_error(r.json(), "RESOURCE_EXHAUSTED")
        assert isinstance(err.get("alternatives"), list) and err["alternatives"], err
        assert rig.store.con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_post_accounts_wechat_slot_held_409_verbatim(rig, client):
    """02 #2(C-01):微信槽位被占 → 409 RESOURCE_EXHAUSTED,error.message="微信槽位被 wxNN 占用,请用切换",
    error.hint_actions=["wechat_switch"];§2.2.5:holder = 处于 starting|…|running 的微信账号 id,落 resource_pools.slot_holder。"""
    bring_windows_online(rig, client)
    assert created_id(create(client, "wechat", "微信甲")) == "wx01"
    rig.store.transition("wx01", "running")
    rig.store.pool_set("windows", slot_holder="wx01")
    r = create(client, "wechat", "微信乙")
    assert r.status_code == 409, r.text
    err = envelope_error(r.json(), "RESOURCE_EXHAUSTED")
    assert err["message"] == "微信槽位被 wx01 占用,请用切换", err
    assert err.get("hint_actions") == ["wechat_switch"], err


def test_post_accounts_wechat_503_when_winagent_offline(rig, client):
    """02 §2.5 降级(WinAgent 服务不在线)①:windows 池 unknown、can_add(wechat)=false;#9 同口径 WinAgent 离线 → 503 NOT_READY;
    04 H02:连续 3 次失败才判离线。"""
    bring_windows_online(rig, client)
    rig.winagent.offline = True
    for _ in range(3):
        client.portal.call(rig.agent.winagent_probe)
    r = create(client, "wechat", "微信甲")
    assert r.status_code == 503, r.text
    envelope_error(r.json(), "NOT_READY")


def test_post_accounts_quota_copied_from_resource_pools_quota_json(rig, client):
    """02 §2.2.5 / §7.1 [pool](C-43):quota_json 以 resource_pools 表为真,agent.toml 只是首次建表默认;
    §3.1 accounts.quota_mb 创建时从 resource_pools.quota_json 拷贝 ⇒ 改表里 qidian=3000 后新建账号 quota_mb=3000。"""
    rig.store.pool_set("wsl", quota_json=json.dumps({"qidian": 3000, "qq": 614, "wechat": 1536}))
    a = create(client, "qidian", "甲").json()["data"]
    assert a["quota_mb"] == 3000, a
    assert full(rig, "qd01")["quota_mb"] == 3000
    res = unwrap(client.get(f"{P}/resources", headers=H(TOK_R)).json())
    assert res["quota_mb"]["qidian"] == 3000, res["quota_mb"]


def test_post_accounts_same_idempotency_key_does_not_create_twice(rig, client):
    """02 §3.4 通用:#2 幂等键必带 ⇒ 同 key 重发不得再建第二个账号(seq 不再递增)。"""
    r1 = create(client, "qidian", "甲", key="k-same")
    assert r1.status_code == 201, r1.text
    r2 = create(client, "qidian", "甲", key="k-same")
    assert r2.status_code in (200, 201, 409), r2.text
    assert rig.store.con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert int(rig.store.settings_get("seq.qidian")) == 1


# ══════════════════════════════════════════════════════════════════════ 三、状态机(00 §8.1 / 05 §2.1.1)

def test_start_sequence_no_credential_stops_at_login_required(tmp_path):
    """00 §8.1:created → provisioning → starting → login_required,不跳段、每步发 account_state 事件;
    05 §2.5.2:remember=false 的企点只起容器不登录 ⇒ 无凭据时停在 login_required、执行层不被调用。"""
    calls: list = []

    async def login_fn(row, account, secret):
        calls.append((account, secret))
        return "running"

    with rig_ctx(tmp_path, login_fn=login_fn) as rig, api_ctx(rig) as client:
        create(client, "qidian", "甲")
        r = act(client, rig, "qd01", "start")
        assert r.status_code == 202, r.text
        d = r.json().get("data") or r.json()
        assert d["state"] == "starting"                                    # #9:202 {state:'starting'}
        assert state_seq(rig, "qd01") == ["provisioning", "starting", "login_required"], state_seq(rig, "qd01")
        assert full(rig, "qd01")["state"] == "login_required"
        assert calls == [], calls


def test_start_sequence_with_credential_reaches_running(rig, client):
    """00 §8.1:… → login_required → logging_in → running,免验证时中间态停留 0 秒仍发事件(05-P5);
    05 §2.1.1 ⑨⑩⑪a:取凭据 → logging_in → 主界面出现 → running;§3.1 desired_state 由 start 改为 running。"""
    create(client, "qidian", "甲", account="u", secret="p", remember=True)
    act(client, rig, "qd01", "start")
    assert state_seq(rig, "qd01") == ["provisioning", "starting", "login_required", "logging_in", "running"], state_seq(rig, "qd01")
    row = full(rig, "qd01")
    assert row["state"] == "running" and row["desired_state"] == "running"
    assert row["last_started_ms"] is not None


def test_boot_timeout_goes_error_boot_timeout(tmp_path):
    """05 §2.1.1 ⑤ / 02 §7.1 [runtime] boot_timeout_s:等 sys.boot_completed=1 超时 → error(BOOT_TIMEOUT);
    02 §2.6 ①:进 error 同事务写 error_since_ms。"""
    cfg = AgentConfig(runtime=RuntimeConfig(boot_timeout_s=2))
    with rig_ctx(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=300), login_fn=login_ok) as rig, api_ctx(rig) as client:
        create(client, "qidian", "甲")
        rig.adb.boot_completed[serial(1)] = "0"
        act(client, rig, "qd01", "start")
        row = full(rig, "qd01")
        assert row["state"] == "error" and row["state_code"] == "BOOT_TIMEOUT", row
        assert row["runtime_error_since_ms"] is not None
        assert state_seq(rig, "qd01")[-1] == "error"


def test_ensure_root_runs_after_boot_before_login(tmp_path):
    """05 §2.1.1 ⑤b:boot_completed=1 之后、首拉起企点/登录之前必做 ensure_root;06 §2.9.5 判据 whoami==root。"""
    seen: dict[str, Any] = {}
    holder: list = []

    async def login_fn(row, account, secret):
        rig = holder[0]
        seen["root_before_login"] = rig.adb.has_call("root")
        seen["whoami_before_login"] = any("whoami" in c for c in rig.adb.shell_cmds(serial(1)))
        return "running"

    with rig_ctx(tmp_path, login_fn=login_fn) as rig, api_ctx(rig) as client:
        holder.append(rig)
        create(client, "qidian", "甲", account="u", secret="p", remember=True)
        act(client, rig, "qd01", "start")
        assert full(rig, "qd01")["state"] == "running"
        assert seen.get("root_before_login") is True and seen.get("whoami_before_login") is True, seen
        calls = rig.adb.calls
        i_boot = call_index(calls, "shell", "sys.boot_completed")
        i_root = call_index(calls, "root", serial(1))
        assert i_boot < i_root, calls


def test_root_failure_does_not_block_start_state_stays_running(rig, client):
    """00 §8.1 / 05 §2.1.1 ⑤b / 06 §2.9.5(R6-32):拿不到 root ⇒ state 保持 running、不进 degraded、不阻断启动,
    只发 warn QIDIAN_NOT_ROOT(subject=account:<id>)。"""
    create(client, "qidian", "甲", account="u", secret="p", remember=True)
    rig.adb.whoami_after_root[serial(1)] = "shell"
    act(client, rig, "qd01", "start")
    row = full(rig, "qd01")
    assert row["state"] == "running", row
    assert row["state_code"] in ("", None), row
    assert rig.agent.alerts.is_firing(QIDIAN_NOT_ROOT, "account:qd01")
    assert rig.agent.alerts.active[(QIDIAN_NOT_ROOT, "account:qd01")].severity == "warn"


def test_vault_offline_login_goes_error_vault_unavailable(tmp_path):
    """02 §2.5 降级 ③:密码型登录不可取密 → 登录不进行,账号 error(state_code=VAULT_UNAVAILABLE),不回退成让人输入。"""
    calls: list = []

    async def login_fn(row, account, secret):
        calls.append(account)
        return "running"

    with rig_ctx(tmp_path, login_fn=login_fn) as rig, api_ctx(rig) as client:
        create(client, "qidian", "甲", account="u", secret="p", remember=True)
        rig.vault.offline = True
        act(client, rig, "qd01", "start")
        row = full(rig, "qd01")
        assert row["state"] == "error" and row["state_code"] == "VAULT_UNAVAILABLE", row
        assert row["runtime_error_since_ms"] is not None
        assert calls == [], calls


def test_bad_credential_goes_error_and_flags_vault_suspect(tmp_path):
    """05 §2.1.1 ⑪c:错误 toast → error(BAD_CREDENTIAL);凭据来自 Vault 则标记该条目 suspect=true。"""
    async def login_fn(row, account, secret):
        return "bad_credential"

    with rig_ctx(tmp_path, login_fn=login_fn) as rig, api_ctx(rig) as client:
        a = create(client, "qidian", "甲", account="u", secret="p", remember=True).json()["data"]
        act(client, rig, "qd01", "start")
        row = full(rig, "qd01")
        assert row["state"] == "error" and row["state_code"] == "BAD_CREDENTIAL", row
        name = a["login"]["credential_ref"].removeprefix("vault://")
        assert rig.vault.entries[name].suspect is True


def test_wait_sms_stays_login_required_with_code(tmp_path):
    """05 §2.1.1 ⑪b:短信验证页 → login_required(state_code=WAIT_SMS),推 account_state 事件,等人。"""
    async def login_fn(row, account, secret):
        return "WAIT_SMS"

    with rig_ctx(tmp_path, login_fn=login_fn) as rig, api_ctx(rig) as client:
        create(client, "qidian", "甲", account="u", secret="p", remember=True)
        act(client, rig, "qd01", "start")
        row = full(rig, "qd01")
        assert row["state"] == "login_required" and row["state_code"] == "WAIT_SMS", row
        last = state_events(rig, "qd01")[-1]["payload"]
        assert last["state"] == "login_required" and last["state_code"] == "WAIT_SMS"


def test_every_account_state_event_carries_serialization_fields(rig, client):
    """02 §3.4.1 Account 序列化(R6-4):account_state 事件 payload 与 #1/#3 共用同一份 ⇒ 每帧带 state/state_code/state_reason/error_since_ms,
    state_code 为 "" 不为 null(R6-53)。"""
    create(client, "qidian", "甲", account="u", secret="p", remember=True)
    act(client, rig, "qd01", "start")
    evs = state_events(rig, "qd01")
    assert len(evs) >= 5
    for e in evs:
        p = e["payload"]
        assert {"state", "state_code", "state_reason", "error_since_ms"} <= set(p), p
        assert isinstance(p["state_code"], str), p
        assert p["error_since_ms"] is None or p["state"] == "error", p


# ══════════════════════════════════════════════════════════════════════ 四、error_since_ms 两个动作(02 §2.6 / §3.4.1)

def test_error_since_written_on_enter_error_same_in_three_places(tmp_path):
    """02 §2.6 ①:迁入 error 写当前 ms;§3.4.1 ⚠️:#1 列表元素、#3 详情、account_state 事件 payload 三处同值同字段名;
    #19 /state 的 error_since_ms 取自 account_runtime。"""
    cfg = AgentConfig(runtime=RuntimeConfig(boot_timeout_s=2))
    with rig_ctx(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=300), login_fn=login_ok) as rig, api_ctx(rig) as client:
        create(client, "qidian", "甲")
        rig.adb.boot_completed[serial(1)] = "0"
        act(client, rig, "qd01", "start")
        db_val = rig.store.get_runtime("qd01")["error_since_ms"]
        assert isinstance(db_val, int)
        detail_ = client.get(f"{P}/accounts/qd01", headers=H(TOK_R)).json()["data"]
        listed = {a["id"]: a for a in client.get(f"{P}/accounts", headers=H(TOK_R)).json()["data"]}["qd01"]
        ev = [e for e in state_events(rig, "qd01") if e["payload"]["state"] == "error"][-1]["payload"]
        st = unwrap(client.get(f"{P}/accounts/qd01/state", headers=H(TOK_R)).json())
        assert detail_["error_since_ms"] == listed["error_since_ms"] == ev["error_since_ms"] == st["error_since_ms"] == db_val


def test_error_since_not_overwritten_on_repeated_enter(tmp_path):
    """02 §2.6 ① `WHERE error_since_ms IS NULL`:已在 error 的重复迁入不覆盖原时刻(否则「已故障 N 秒」永远归零)。"""
    clock = Clock()
    with rig_ctx(tmp_path, clock=clock) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        rig.store.transition("qd01", "error", state_code="CONTAINER_EXIT", state_reason="容器退出")
        first = rig.store.get_runtime("qd01")["error_since_ms"]
        assert isinstance(first, int)
        clock.advance(5_000)
        rig.store.transition("qd01", "error", state_code="CONTAINER_EXIT", state_reason="又退出")
        assert rig.store.get_runtime("qd01")["error_since_ms"] == first


def test_error_since_cleared_when_leaving_error_via_stop(tmp_path):
    """02 §2.6 ②:迁出到 stopped 等任一非 error 态时清 NULL;§2.2.5 R4-8 ①:error 不是终态、可被 #10 stop 驱动 → stopping → stopped。"""
    cfg = AgentConfig(runtime=RuntimeConfig(boot_timeout_s=2))
    with rig_ctx(tmp_path, cfg=cfg, clock=Clock(auto_step_ms=300), login_fn=login_ok) as rig, api_ctx(rig) as client:
        create(client, "qidian", "甲")
        rig.adb.boot_completed[serial(1)] = "0"
        act(client, rig, "qd01", "start")
        assert full(rig, "qd01")["state"] == "error"
        r = act(client, rig, "qd01", "stop", body={"graceful": True})
        assert r.status_code == 202, r.text
        row = full(rig, "qd01")
        assert row["state"] == "stopped", row
        assert row["runtime_error_since_ms"] is None
        assert client.get(f"{P}/accounts/qd01", params={"include_stopped": "true"}, headers=H(TOK_R)).json()["data"]["error_since_ms"] is None


def test_error_since_cleared_by_transition_to_non_error(tmp_path):
    """02 §2.6 ②(状态迁移是唯一写点):error → running 清 NULL;再进 error 重新起算。"""
    clock = Clock()
    with rig_ctx(tmp_path, clock=clock) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        rig.store.transition("qd01", "error", state_code="CONTAINER_EXIT")
        t1 = rig.store.get_runtime("qd01")["error_since_ms"]
        rig.store.transition("qd01", "running")
        assert rig.store.get_runtime("qd01")["error_since_ms"] is None
        clock.advance(10_000)
        rig.store.transition("qd01", "error", state_code="CONTAINER_EXIT")
        t2 = rig.store.get_runtime("qd01")["error_since_ms"]
        assert t2 is not None and t2 >= t1 + 10_000


def test_state_endpoint_has_exactly_six_fields(rig, client):
    """02 #19:`GET /accounts/{id}/state` → `{state, state_code, state_reason, adapter_state, last_seen_at, error_since_ms}`。"""
    create(client, "qidian", "甲")
    r = client.get(f"{P}/accounts/qd01/state", headers=H(TOK_R))
    assert r.status_code == 200, r.text
    d = unwrap(r.json())
    assert set(d) == STATE_KEYS, set(d) ^ STATE_KEYS
    assert d["state"] == "created" and d["state_code"] == "" and d["error_since_ms"] is None


# ══════════════════════════════════════════════════════════════════════ 五、stop / disable / enable / restart / delete

def _running(rig, client, label: str = "甲") -> str:
    aid = created_id(create(client, "qidian", label, account="u", secret="p", remember=True))
    act(client, rig, aid, "start")
    assert full(rig, aid)["state"] == "running"
    return aid


def test_stop_sequence_stopping_then_stopped(rig, client):
    """02 #10:`202 {state:'stopping'}`;00 §8.1 `* → stopping → stopped`;05 §2.5.2 停止:容器 stop,卷保留;
    §3.1 desired_state 由 stop 改 stopped、last_stopped_ms 记录。"""
    aid = _running(rig, client)
    r = act(client, rig, aid, "stop", body={"graceful": True})
    assert r.status_code == 202, r.text
    d = r.json().get("data") or r.json()
    assert d["state"] == "stopping"
    assert state_seq(rig, aid)[-2:] == ["stopping", "stopped"], state_seq(rig, aid)
    row = full(rig, aid)
    assert row["state"] == "stopped" and row["desired_state"] == "stopped"
    assert row["last_stopped_ms"] is not None


def test_stop_purges_ephemeral_after_stopped(rig, client):
    """05 §2.5.7 / 02 §2.2.4:清理挂在 stopping → stopped 收尾,账号已置 stopped 之后同步做 ⇒ 存在一条
    runtime.purge_ephemeral 审计(kind='system')其 ts_ms ≥ stopped 事件 ts_ms。"""
    aid = _running(rig, client)
    act(client, rig, aid, "stop", body={"graceful": True})
    stopped_ts = [e for e in state_events(rig, aid) if e["payload"]["state"] == "stopped"][-1]["ts_ms"]
    audits = [a for a in purge_audits(rig) if a.get("account_id") == aid or detail(a).get("account_id") == aid]
    assert audits, rig.store.list_audit()
    after = [a for a in audits if a["ts_ms"] >= stopped_ts]
    assert after, (stopped_ts, audits)
    assert all(a["kind"] == "system" for a in audits)


def test_stop_keeps_container_and_volume(rig, client):
    """05 §2.5.2 停止:容器 stop,卷保留;§2.5.7 绝不动 accounts/<id>/data/ ⇒ 有 stop、无 remove,数据卷文件仍在。"""
    aid = _running(rig, client)
    keep = f"{ROOT}/accounts/{aid}/data/wtlogin_guid"
    rig.fs.put(keep, 16)
    act(client, rig, aid, "stop", body={"graceful": True})
    name = full(rig, aid)["container_name"]
    assert ("stop", name) in rig.containers.calls
    assert ("remove", name) not in rig.containers.calls
    assert name in rig.containers.containers and rig.containers.containers[name].running is False
    assert keep in rig.fs.files


def test_disable_stops_first_then_disabled(rig, client):
    """02 #6:先 stop → enabled=0,state=disabled;05 §2.5.2 停用;00 §8.1 不跳段 ⇒ 序列尾 stopping, stopped, disabled。"""
    aid = _running(rig, client)
    r = act(client, rig, aid, "disable", body={"graceful": True})
    assert r.status_code in (200, 202), r.text
    row = full(rig, aid)
    assert row["state"] == "disabled" and row["enabled"] == 0, row
    assert state_seq(rig, aid)[-3:] == ["stopping", "stopped", "disabled"], state_seq(rig, aid)
    name = row["container_name"]
    assert ("stop", name) in rig.containers.calls and ("remove", name) not in rig.containers.calls


def test_enable_goes_to_stopped_without_autostart(rig, client):
    """02 #5:enabled=1、state=stopped、不自动 start;05 §2.5.2 启用:disabled → stopped(由人或自恢复决定)。"""
    aid = _running(rig, client)
    act(client, rig, aid, "disable", body={"graceful": True})
    starts_before = rig.containers.calls.count(("start", full(rig, aid)["container_name"]))
    r = act(client, rig, aid, "enable")
    assert r.status_code in (200, 202), r.text
    row = full(rig, aid)
    assert row["state"] == "stopped" and row["enabled"] == 1, row
    assert rig.containers.calls.count(("start", row["container_name"])) == starts_before
    assert state_seq(rig, aid)[-1] == "stopped"


def test_restart_start_segment_reruns_ensure_root_and_stop_segment_purges(rig, client):
    """02 #11 restart = stop + start;05 §2.5.2 重启:start 段的企点必须复走 ensure_root(R6-28),stop 段同样清临时(§2.5.7)。"""
    aid = _running(rig, client)
    roots_before = sum(1 for c in rig.adb.calls if c[0] == "root" and c[1] == serial(1))
    assert roots_before >= 1
    purges_before = len(purge_audits(rig))
    r = act(client, rig, aid, "restart")
    assert r.status_code == 202, r.text
    assert full(rig, aid)["state"] == "running"
    roots_after = sum(1 for c in rig.adb.calls if c[0] == "root" and c[1] == serial(1))
    assert roots_after >= roots_before + 1, rig.adb.calls
    seq = state_seq(rig, aid)
    i_stopped = len(seq) - 1 - seq[::-1].index("stopped")
    assert seq[i_stopped - 1] == "stopping" and "starting" in seq[i_stopped:] and seq[-1] == "running", seq
    assert len(purge_audits(rig)) >= purges_before + 1


def test_delete_requires_admin_level(rig, client):
    """02 #7 级别 A;§3.4 通用鉴权级别 R/W/A 高包含低 ⇒ write/read 令牌 403、admin 通过。"""
    create(client, "qidian", "甲")
    assert client.delete(f"{P}/accounts/qd01", params={"confirm": "甲"}, headers=H(TOK_W)).status_code == 403
    assert client.delete(f"{P}/accounts/qd01", params={"confirm": "甲"}, headers=H(TOK_R)).status_code == 403
    assert full(rig, "qd01")["deleted_ms"] is None
    r = client.delete(f"{P}/accounts/qd01", params={"confirm": "甲"}, headers=H(TOK_A))
    assert r.status_code == 200, r.text


def test_delete_confirm_must_equal_current_label(rig, client):
    """02 #7:`confirm` 必须等于当前 label(二次确认,基线 §11.7)⇒ 不等 400 INVALID_ARGS 且不删。"""
    create(client, "qidian", "甲")
    r = client.delete(f"{P}/accounts/qd01", params={"confirm": "乙"}, headers=H(TOK_A))
    assert r.status_code == 400, r.text
    envelope_error(r.json(), "INVALID_ARGS")
    assert full(rig, "qd01")["deleted_ms"] is None
    r = client.delete(f"{P}/accounts/qd01", headers=H(TOK_A))
    assert r.status_code == 400, r.text
    assert full(rig, "qd01")["deleted_ms"] is None


def test_delete_is_soft_and_returns_data_kept(rig, client):
    """02 #7(P-09):恒软删——deleted_ms、desired_state=stopped、`{deleted:true, data_kept:true}`;行不物理删;
    之后 #3 404 TARGET_NOT_FOUND;#1 include_deleted=true 可见、默认不可见。"""
    create(client, "qidian", "甲")
    r = client.delete(f"{P}/accounts/qd01", params={"confirm": "甲"}, headers=H(TOK_A))
    assert r.status_code == 200, r.text
    d = r.json().get("data") or r.json()
    assert d["deleted"] is True and d["data_kept"] is True, d
    idle(client, rig, "qd01")
    row = full(rig, "qd01")
    assert row["deleted_ms"] is not None and row["desired_state"] == "stopped"
    assert rig.store.con.execute("SELECT COUNT(*) FROM accounts WHERE id='qd01'").fetchone()[0] == 1
    g = client.get(f"{P}/accounts/qd01", headers=H(TOK_R))
    assert g.status_code == 404, g.text
    envelope_error(g.json(), "TARGET_NOT_FOUND")
    ids_default = [a["id"] for a in client.get(f"{P}/accounts", params={"include_stopped": "true"}, headers=H(TOK_R)).json()["data"]]
    assert "qd01" not in ids_default
    ids_deleted = [a["id"] for a in client.get(f"{P}/accounts", params={"include_deleted": "true", "include_stopped": "true"},
                                                headers=H(TOK_R)).json()["data"]]
    assert "qd01" in ids_deleted


def test_delete_running_account_stops_container_keeps_volume(rig, client):
    """02 #7:容器停并保留卷与登录态;§2.2.4 destroy(keep_data) 路径同样 stop 后 _purge_ephemeral、绝不动 /data 卷。
    ⚠️ 05 §2.5.2 删除前置写「非运行态」,与 02 #7「容器停」并存——本用例按 02 #7 断言。"""
    aid = _running(rig, client)
    keep = f"{ROOT}/accounts/{aid}/data/wtlogin_guid"
    rig.fs.put(keep, 16)
    r = client.delete(f"{P}/accounts/{aid}", params={"confirm": "甲"}, headers=H(TOK_A))
    assert r.status_code == 200, r.text
    idle(client, rig, aid)
    name = full(rig, aid)["container_name"]
    assert ("stop", name) in rig.containers.calls
    assert not any(c[0] == "remove" and c[1] == name for c in rig.containers.calls)
    assert keep in rig.fs.files
    assert full(rig, aid)["deleted_ms"] is not None


def test_stopped_not_counted_but_start_rechecks_can_add(tmp_path):
    """02 §2.2.5:stopped 的账号不占额度(used 只算 state ∉ {stopped, disabled, error});但 start 时要再过一次 can_add,不够则 RESOURCE_EXHAUSTED。"""
    total = WSL_RESERVED + QUOTA["qidian"] * 2                     # 预算正好两个企点
    with rig_ctx(tmp_path, wsl_total_mb=total, login_fn=login_ok) as rig, api_ctx(rig) as client:
        assert created_id(create(client, "qidian", "甲")) == "qd01"
        assert created_id(create(client, "qidian", "乙")) == "qd02"
        assert create(client, "qidian", "丙").status_code == 409         # created 计入 used,第三个装不下
        act(client, rig, "qd01", "start")
        rig.store.transition("qd02", "stopped", desired_state="stopped")
        assert rig.agent.pool.used_mb() == QUOTA["qidian"]              # stopped 不计
        assert created_id(create(client, "qidian", "丙")) == "qd03"      # 腾出的额度能再建
        r = client.post(f"{P}/accounts/qd02/start", headers=H(TOK_A))
        assert r.status_code == 409, r.text
        envelope_error(r.json(), "RESOURCE_EXHAUSTED")
        idle(client, rig, "qd02")
        assert full(rig, "qd02")["state"] == "stopped"


# ══════════════════════════════════════════════════════════════════════ 六、pool(02 §2.2.5 / 00 §7.6)

def test_used_mb_formula_by_state(rig):
    """02 §2.2.5:used = Σ quota_mb where host='wsl' and enabled and state ∉ {stopped, disabled, error}
    ⇒ running/created/degraded/login_required 计,stopped/error/disabled 不计。"""
    s = rig.store
    s.ensure_account("qd01", "qidian", state="running")
    s.ensure_account("qd02", "qidian", state="stopped")
    s.ensure_account("qd03", "qidian", state="error")
    s.ensure_account("qq01", "qq", state="created", login_mode="qrcode")
    s.ensure_account("qq02", "qq", state="disabled", login_mode="qrcode")
    s.transition("qq02", "disabled", enabled=False)
    s.ensure_account("qq03", "qq", state="login_required", login_mode="qrcode")
    expected = QUOTA["qidian"] + QUOTA["qq"] * 2
    assert rig.agent.pool.used_mb() == expected
    snap = rig.agent.pool.snapshot()
    wsl = snap["pools"]["wsl"]
    assert wsl["used_mb"] == expected
    assert wsl["free_mb"] == wsl["total_mb"] - wsl["reserved_mb"] - expected


def test_can_add_three_channels(rig):
    """02 §2.2.5 算法:can_add(qidian)=budget-used ≥ 2560;can_add(qq) ≥ 614;
    can_add(wechat)=windows.wechat_enabled AND holder=='' AND pending=='' AND windows.total-reserved ≥ 1536。"""
    ok, reason, alts = rig.agent.pool.can_add("qidian")
    assert ok is True
    assert rig.agent.pool.can_add("qq")[0] is True
    assert rig.agent.pool.can_add("wechat")[0] is False                 # windows 未知 / wechat 未启用
    rig.agent.pool.set_windows(total_mb=16384, wechat_enabled=True, known=True)
    assert rig.agent.pool.can_add("wechat")[0] is True
    rig.agent.pool.set_windows(total_mb=16384, wechat_enabled=False, known=True)
    assert rig.agent.pool.can_add("wechat")[0] is False
    rig.agent.pool.set_windows(total_mb=4096 + 1000, wechat_enabled=True, known=True)   # 4096 reserved,余 1000 < 1536
    assert rig.agent.pool.can_add("wechat")[0] is False


def test_get_resources_shape_and_wechat_slots_empty_values(rig, client):
    """02 #69 / 00 §7.6:ResourcePool = pools.wsl{total_mb,reserved_mb,used_mb,free_mb}、pools.windows{total_mb,reserved_mb,wechat_mb,
    wechat_slots{used,max:1,holder,pending,pending_expires_at,pending_login_session_id}}、realtime{wsl_anon_mb,win_available_mb}、
    quota_mb、can_add、accounts_by_channel;空值口径:三个串出 ""、只有 pending_expires_at 出 null(R6-6)。"""
    r = client.get(f"{P}/resources", headers=H(TOK_R))
    assert r.status_code == 200, r.text
    d = unwrap(r.json())
    assert {"total_mb", "reserved_mb", "used_mb", "free_mb"} <= set(d["pools"]["wsl"]), d["pools"]["wsl"]
    assert d["pools"]["wsl"]["total_mb"] == 11264 and d["pools"]["wsl"]["reserved_mb"] == WSL_RESERVED
    win = d["pools"]["windows"]
    assert {"total_mb", "reserved_mb", "wechat_mb", "wechat_slots"} <= set(win), win
    slots = win["wechat_slots"]
    assert set(slots) == SLOT_KEYS, set(slots) ^ SLOT_KEYS
    assert slots["max"] == 1 and slots["used"] == 0
    assert slots["holder"] == "" and slots["pending"] == "" and slots["pending_login_session_id"] == ""
    assert slots["pending_expires_at"] is None
    assert {"wsl_anon_mb", "win_available_mb"} <= set(d["realtime"]), d["realtime"]
    assert d["quota_mb"] == QUOTA
    assert set(d["can_add"]) == {"qidian", "qq", "wechat"}
    assert "accounts_by_channel" in d


def test_get_resources_can_add_counts(rig, client):
    """00 §7.6 示例 `can_add:{qidian:1, qq:6, wechat:0}` 是「还能加几个」的数值;默认 11264-2048=9216 ⇒ qidian 3、qq 15、wechat 0。"""
    d = unwrap(client.get(f"{P}/resources", headers=H(TOK_R)).json())
    budget = 11264 - WSL_RESERVED
    assert d["can_add"]["qidian"] == budget // QUOTA["qidian"]
    assert d["can_add"]["qq"] == budget // QUOTA["qq"]
    assert d["can_add"]["wechat"] == 0
    create(client, "qidian", "甲")
    d2 = unwrap(client.get(f"{P}/resources", headers=H(TOK_R)).json())
    assert d2["can_add"]["qidian"] == (budget - QUOTA["qidian"]) // QUOTA["qidian"]
    assert d2["pools"]["wsl"]["used_mb"] == QUOTA["qidian"]


def test_reserve_uses_row_level_claim(tmp_path):
    """02 §2.2.5 并发:reserve 的「检查+写库」用数据库行级 claim(条件 UPDATE + rowcount),剩余 ≥ quota 才 True。"""
    with rig_ctx(tmp_path, wsl_total_mb=WSL_RESERVED + 2652) as rig:
        assert rig.store.pool_claim_wsl(2560) is True
        assert rig.store.pool_claim_wsl(2653) is False
        rig.store.ensure_account("qd01", "qidian", state="created")     # 占 2560,余 92
        assert rig.store.pool_claim_wsl(614) is False
        assert rig.store.pool_claim_wsl(92) is True


# ══════════════════════════════════════════════════════════════════════ 七、_purge_ephemeral(02 §2.2.4 / 05 §2.5.7)

async def _provisioned_row(rig, aid: str = "qd01"):
    rig.store.ensure_account(aid, "qidian", state="stopped")
    row = full(rig, aid)
    await rig.agent.runtime.start(row)
    return full(rig, aid)


def _seed_fs(rig, aid: str = "qd01") -> dict[str, str]:
    paths = {
        "tmp": f"{ROOT}/accounts/{aid}/tmp/shot-1.png",
        "tmp2": f"{ROOT}/accounts/{aid}/tmp/dump/ui.xml",
        "data": f"{ROOT}/accounts/{aid}/data/wtlogin_guid",
        # 02 §2.2.4 ③:按下载任务 account_id 前缀筛(分隔符规格未钉,这里取 `<id>-`)
        "media_own": f"{ROOT}/media/tmp/{aid}-dl-001.part",
        "media_other": f"{ROOT}/media/tmp/qd02-dl-002.part",
        "media_stored": f"{ROOT}/media/202609/abcdef0123",
        "other_tmp": f"{ROOT}/accounts/qd02/tmp/shot-9.png",
    }
    for p in paths.values():
        rig.fs.put(p, 1024 * 1024)
    return paths


async def test_purge_clears_targets_and_never_touches_protected(tmp_path):
    """02 §2.2.4 E-19 ①~⑤:容器内 rm -rf /data/local/tmp/*、宿主 accounts/<id>/tmp/ 整目录、media/tmp/ 中属于该账号的中间文件、
    adb forward --remove tcp:166NN;绝不动 /data 卷、accounts/<id>/data/、已入库 media/<yyyymm>/、别账号在途下载。"""
    with rig_ctx(tmp_path) as rig:
        row = await _provisioned_row(rig)
        rig.adb.calls.clear()
        paths = _seed_fs(rig)
        await rig.agent.runtime._purge_ephemeral(row)
        assert paths["tmp"] not in rig.fs.files and paths["tmp2"] not in rig.fs.files
        assert paths["media_own"] not in rig.fs.files
        assert paths["data"] in rig.fs.files
        assert paths["media_other"] in rig.fs.files
        assert paths["media_stored"] in rig.fs.files
        assert paths["other_tmp"] in rig.fs.files
        cmds = rig.adb.shell_cmds(serial(1))
        assert any("rm -rf /data/local/tmp/*" in c for c in cmds), cmds
        for c in cmds:
            if "rm " in c:
                assert "/data/local/tmp" in c and "/data/data" not in c and " /data " not in c and " /data/*" not in c, c
        assert ("forward_remove", f"{serial(1)}: tcp:16601") in rig.adb.calls, rig.adb.calls


async def test_purge_is_idempotent(tmp_path):
    """02 §2.2.4:全部「存在即删、不存在跳过」,可重复调用不报错;第二次无可清 ⇒ freed_mb 为 0。"""
    with rig_ctx(tmp_path) as rig:
        row = await _provisioned_row(rig)
        _seed_fs(rig)
        r1 = await rig.agent.runtime._purge_ephemeral(row)
        r2 = await rig.agent.runtime._purge_ephemeral(row)
        r3 = await rig.agent.runtime._purge_ephemeral(row)
        assert isinstance(r1, dict) and isinstance(r2, dict) and isinstance(r3, dict)
        assert r1["freed_mb"] > 0 and r2["freed_mb"] == 0 and r3["freed_mb"] == 0, (r1, r2, r3)


async def test_purge_skips_container_step_when_container_absent(tmp_path):
    """02 §2.2.4 / 05 §2.5.7 崩溃路径:容器已不在时容器内那步跳过、只清宿主侧,不报错。"""
    with rig_ctx(tmp_path) as rig:
        rig.store.ensure_account("qd01", "qidian", state="stopped")
        row = full(rig, "qd01")
        paths = _seed_fs(rig)
        assert "qtrade-qd01" not in rig.containers.containers
        res = await rig.agent.runtime._purge_ephemeral(row)
        assert isinstance(res, dict)
        assert paths["tmp"] not in rig.fs.files and paths["media_own"] not in rig.fs.files
        assert paths["data"] in rig.fs.files
        assert not any(c[0] == "shell" and "rm " in c[1] for c in rig.adb.calls), rig.adb.calls


async def test_purge_writes_system_audit_with_detail(tmp_path):
    """02 §2.2.4:清理结果记 audit_log(kind='system', action='runtime.purge_ephemeral', detail_json={account_id, freed_mb, targets:[…]})。"""
    with rig_ctx(tmp_path) as rig:
        row = await _provisioned_row(rig)
        before = len(purge_audits(rig))
        _seed_fs(rig)
        await rig.agent.runtime._purge_ephemeral(row)
        audits = purge_audits(rig)
        assert len(audits) == before + 1
        a = audits[-1]
        assert a["kind"] == "system" and a["action"] == "runtime.purge_ephemeral"
        d = detail(a)
        assert {"account_id", "freed_mb", "targets"} <= set(d), d
        assert d["account_id"] == "qd01" and isinstance(d["targets"], list) and d["targets"]
        assert isinstance(d["freed_mb"], (int, float)) and d["freed_mb"] > 0


def test_start_runs_purge_before_provision(rig, client):
    """02 §2.2.4 崩溃/断电路径 / 05 §2.5.7:start(acct) 在 provision/inspect 之前先跑一次 _purge_ephemeral(补做上次没干净的)。"""
    create(client, "qidian", "甲")
    stale = f"{ROOT}/accounts/qd01/tmp/stale.png"
    rig.fs.put(stale, 4096)
    act(client, rig, "qd01", "start")
    assert stale not in rig.fs.files
    audits = [a for a in purge_audits(rig) if detail(a).get("account_id") == "qd01" or a.get("account_id") == "qd01"]
    assert audits, rig.store.list_audit()
    # 05 §2.1.1 ④:provisioning 是「正在组装参数 → docker run」的状态、落 starting 时容器已 run ⇒ 预清理必须早于 starting 事件
    starting_ts = [e for e in state_events(rig, "qd01") if e["payload"]["state"] == "starting"][0]["ts_ms"]
    assert audits[0]["ts_ms"] <= starting_ts, (audits[0]["ts_ms"], starting_ts)


# ══════════════════════════════════════════════════════════════════════ 八、ensure_root(06 §2.9.5 / 04 H06)

def _root_rig(tmp_path, clock: Optional[Clock] = None):
    return rig_ctx(tmp_path, clock=clock or Clock(), cfg=AgentConfig(health=HealthConfig(adb_root_grace_s=15)))


async def test_ensure_root_three_step_sequence_verbatim(tmp_path):
    """06 §2.9.5 逐字:adb root → shell "stop adbd; start adbd" → adb disconnect → adb connect → assert whoami == root;
    成功返回真。"""
    with _root_rig(tmp_path) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        row = full(rig, "qd01")
        rig.adb.calls.clear()
        ok = await rig.agent.runtime.ensure_root(row)
        assert ok is True
        calls = rig.adb.calls
        i_root = call_index(calls, "root", serial(1))
        i_restart = call_index(calls, "shell", "stop adbd; start adbd", i_root)
        i_disc = call_index(calls, "disconnect", serial(1), i_restart)
        i_conn = call_index(calls, "connect", serial(1), i_disc)
        i_who = call_index(calls, "shell", "whoami", i_conn)
        assert i_root < i_restart < i_disc < i_conn < i_who, calls


async def test_ensure_root_only_touches_own_serial_never_kill_server(tmp_path):
    """06 §2.9.5:只动该账号的连接(disconnect/connect 127.0.0.1:160NN),绝不 adb kill-server(会把兄弟账号一并打 offline)。"""
    with _root_rig(tmp_path) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        rig.store.ensure_account("qd02", "qidian", state="running")
        rig.adb.calls.clear()
        assert await rig.agent.runtime.ensure_root(full(rig, "qd02")) is True
        assert not any(serial(1) in c[1] for c in rig.adb.calls), rig.adb.calls
        kinds = {c[0] for c in rig.adb.calls}
        assert not any("kill" in k for k in kinds), kinds
        assert not any("kill-server" in c[1] or "start-server" in c[1] for c in rig.adb.calls), rig.adb.calls
        assert not hasattr(rig.adb, "kill_server")


async def test_ensure_root_marks_rooting_window_at_start(tmp_path):
    """06 §2.9.5 / 04 H06 ③:ensure_root 开始时调 health.mark_rooting(acct, adb_root_grace_s),窗口 = 自开始起 ≤ 15 s(默认)。"""
    clock = Clock()
    with _root_rig(tmp_path, clock) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        assert rig.agent.health.is_rooting("qd01") is False
        await rig.agent.runtime.ensure_root(full(rig, "qd01"))
        assert rig.agent.health.is_rooting("qd01") is True
        clock.advance(14_000)
        assert rig.agent.health.is_rooting("qd01") is True
        clock.advance(2_000)
        assert rig.agent.health.is_rooting("qd01") is False


async def test_ensure_root_failure_emits_warn_only(tmp_path):
    """06 §2.9.5 / 04 H06 (b) / 00 §8.1(R6-32/R6-35):whoami != root ⇒ 只发 warn QIDIAN_NOT_ROOT(subject=account:<id>),
    evidence{whoami, ensure_root_attempts, db_visible}(attempts = qidian_root_fail_streak 当前值),hint_actions=["open_env"];
    state 保持 running、不进 degraded。"""
    with _root_rig(tmp_path) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        rig.adb.whoami_after_root[serial(1)] = "shell"
        ok = await rig.agent.runtime.ensure_root(full(rig, "qd01"))
        assert ok is False
        key = (QIDIAN_NOT_ROOT, "account:qd01")
        assert key in rig.agent.alerts.active, rig.agent.alerts.active.keys()
        al = rig.agent.alerts.active[key]
        assert al.severity == "warn"
        assert {"whoami", "ensure_root_attempts", "db_visible"} <= set(al.evidence), al.evidence
        assert al.evidence["whoami"] == "shell" and al.evidence["ensure_root_attempts"] == 1
        assert list(al.hint_actions) == ["open_env"]
        assert rig.agent.runtime.qidian_root_fail_streak["qd01"] == 1
        row = full(rig, "qd01")
        assert row["state"] == "running", row


async def test_ensure_root_three_failures_still_warn_never_crit(tmp_path):
    """04 H06 (b)(R6-32 逐字):连续 3 次拿不到 root 也只保持 QIDIAN_NOT_ROOT firing、永不升 crit、state 仍 running;
    evidence.ensure_root_attempts 就是 qidian_root_fail_streak。"""
    with _root_rig(tmp_path) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        rig.adb.whoami_after_root[serial(1)] = "shell"
        for _ in range(3):
            assert await rig.agent.runtime.ensure_root(full(rig, "qd01")) is False
        assert rig.agent.runtime.qidian_root_fail_streak["qd01"] == 3
        al = rig.agent.alerts.active[(QIDIAN_NOT_ROOT, "account:qd01")]
        assert al.severity == "warn"
        assert al.evidence["ensure_root_attempts"] == 3
        assert full(rig, "qd01")["state"] == "running"
        assert not any(k[0] == QIDIAN_NOT_ROOT and v.severity == "crit" for k, v in rig.agent.alerts.active.items())


async def test_ensure_root_success_clears_streak_and_resolves(tmp_path):
    """04 H06 (b):whoami==root 成功一次即清零 qidian_root_fail_streak;告警 resolved(不再 firing)。"""
    with _root_rig(tmp_path) as rig:
        rig.store.ensure_account("qd01", "qidian", state="running")
        rig.adb.whoami_after_root[serial(1)] = "shell"
        assert await rig.agent.runtime.ensure_root(full(rig, "qd01")) is False
        assert rig.agent.alerts.is_firing(QIDIAN_NOT_ROOT, "account:qd01")
        rig.adb.whoami_after_root[serial(1)] = "root"
        assert await rig.agent.runtime.ensure_root(full(rig, "qd01")) is True
        assert rig.agent.runtime.qidian_root_fail_streak["qd01"] == 0
        assert not rig.agent.alerts.is_firing(QIDIAN_NOT_ROOT, "account:qd01")


# ══════════════════════════════════════════════════════════════════════ 九、启动串行 / 唤醒复提权(02 §2.2.4 / 04 §2.10)

def test_start_lock_is_a_single_asyncio_lock(rig):
    """02 §2.2.4 并发:启动串行——全局一把 start_lock。"""
    assert isinstance(rig.agent.runtime.start_lock, asyncio.Lock)


def test_two_starts_do_not_overlap(rig, client):
    """02 §2.2.4(A.5 峰值内存翻倍)/ 05 §2.5.2 启动前置「全局启动锁」:两个账号同时 start,启动序列不并发 ⇒
    两账号 account_state 事件区间(provisioning … 终态)在时间轴上不交叠。"""
    create(client, "qidian", "甲", account="u", secret="p", remember=True)
    create(client, "qidian", "乙", account="u", secret="p", remember=True)
    r1 = client.post(f"{P}/accounts/qd01/start", headers=H(TOK_A))
    r2 = client.post(f"{P}/accounts/qd02/start", headers=H(TOK_A))
    assert r1.status_code == 202 and r2.status_code == 202, (r1.text, r2.text)
    idle(client, rig, "qd01")
    idle(client, rig, "qd02")
    assert full(rig, "qd01")["state"] == "running" and full(rig, "qd02")["state"] == "running"
    a = [e["ts_ms"] for e in state_events(rig, "qd01") if e["payload"]["state"] != "created"]
    b = [e["ts_ms"] for e in state_events(rig, "qd02") if e["payload"]["state"] != "created"]
    assert max(a) <= min(b) or max(b) <= min(a), (a, b)


def test_host_resume_reruns_ensure_root_for_running_qidian(rig, client):
    """04 §2.10 睡眠唤醒 +30s(R6-28):adb 探活 reconnect 成功后对每个企点账号复跑 ensure_root,并在开始时 mark_rooting。"""
    aid = _running(rig, client)
    rig.adb.calls.clear()
    client.portal.call(rig.agent.on_host_resume, rig.clock.now_ms)
    assert ("root", serial(1)) in rig.adb.calls, rig.adb.calls
    i_root = call_index(rig.adb.calls, "root", serial(1))
    call_index(rig.adb.calls, "shell", "stop adbd; start adbd", i_root)
    assert rig.agent.health.is_rooting(aid) is True


# ══════════════════════════════════════════════════════════════════════ 十、WinAgent 契约(02 §2.5 / §3.6)

def _health_calls(rig) -> list:
    return [c for c in rig.winagent.calls if c[1].endswith("/wa/v1/health")]


async def test_readonly_request_retries_once(tmp_path):
    """02 §2.5 重试:只读类 1 次重试 ⇒ 第一次抛错、第二次成功,总共 2 次请求且结果非空。"""
    with rig_ctx(tmp_path) as rig:
        rig.winagent.fail_next = 1
        res = await rig.agent.winagent.health()
        assert res is not None
        assert len(_health_calls(rig)) == 2, rig.winagent.calls


async def test_readonly_request_gives_up_after_one_retry(tmp_path):
    """02 §2.5 重试:只读类 1 次重试——连续两次失败即放弃,不做第三次。"""
    with rig_ctx(tmp_path) as rig:
        rig.winagent.fail_next = 2
        try:
            res = await rig.agent.winagent.health()
        except WinAgentUnavailable:
            res = None
        assert res is None
        assert len(_health_calls(rig)) == 2, rig.winagent.calls


async def test_write_request_not_retried(tmp_path):
    """02 §2.5 重试:写类(vault put/delete …)不重试——一次失败即抛 VaultUnavailable,只 1 次 PUT。"""
    with rig_ctx(tmp_path) as rig:
        rig.winagent.fail_next = 1
        v = WinAgentVault(rig.agent.winagent)
        with pytest.raises(VaultUnavailable):
            await v.put("account/qd09", "secret-value")
        puts = [c for c in rig.winagent.calls if c[0] == "PUT" and c[1].endswith("/wa/v1/vault/account/qd09")]
        assert len(puts) == 1, rig.winagent.calls
        rig.winagent.fail_next = 1
        with pytest.raises(VaultUnavailable):
            await v.delete("account/qd09")
        dels = [c for c in rig.winagent.calls if c[0] == "DELETE" and c[1].endswith("/wa/v1/vault/account/qd09")]
        assert len(dels) == 1, rig.winagent.calls


async def test_ping_timeout_is_two_seconds(tmp_path):
    """02 §2.5 超时:ping/health 2s(不是 [winagent] timeout_ms 的通用 3s)⇒ 对端延迟 2.6 s 时 ping 超时判失败。"""
    with rig_ctx(tmp_path) as rig:
        rig.winagent.delay_s = 2.6
        t0 = time.monotonic()
        try:
            res = await rig.agent.winagent.ping()
        except WinAgentUnavailable:
            res = None
        dt = time.monotonic() - t0
        assert res is None, res
        assert dt < 2.6 * 2 + 1.0, dt


async def test_no_token_file_ping_ok_but_auth_endpoints_unavailable(tmp_path):
    """02 §2.5 鉴权 / §3.6:#1 GET /wa/v1/ping 无鉴权(`{agent_id, version, time, listen}`);#2 health 需 Agent 令牌
    ⇒ 无令牌文件时 ping 可用、鉴权端点不可用。"""
    with rig_ctx(tmp_path) as rig:
        cfg = WinAgentConfig(token_file=str(tmp_path / "no-such.token"))
        c = WinAgentClient(cfg, transport=rig.winagent, base_url="http://winagent.fake:17610", token=None, clock=rig.clock)
        p = await c.ping()
        assert p is not None and "version" in p, p
        try:
            h = await c.health()
        except WinAgentUnavailable:
            h = None
        assert h is None, h


def test_x_wa_version_recorded_into_health_summary(rig, client):
    """02 §2.5 版本:每个响应带 X-WA-Version,Agent 记录之;#72 /system/health 带令牌回 winagent:{online, version, user_agent}。"""
    client.portal.call(rig.agent.winagent_probe)
    b = client.get(f"{P}/system/health", headers=H(TOK_R)).json()
    assert b["winagent"]["online"] is True
    assert b["winagent"]["version"] == "1.0.0", b["winagent"]
    assert b["winagent"]["user_agent"] is True


async def test_vault_read_is_post_read_with_trace_id(tmp_path):
    """02 §3.6 #11:读 Vault 走 POST /wa/v1/vault/{name}/read(读有副作用故 POST),须带 X-Trace-Id;#9 PUT 不回显值。"""
    with rig_ctx(tmp_path) as rig:
        v = WinAgentVault(rig.agent.winagent)
        await v.put("account/qd01", "s3cret")
        puts = [c for c in rig.winagent.calls if c[0] == "PUT" and c[1].endswith("/wa/v1/vault/account/qd01")]
        assert len(puts) == 1, rig.winagent.calls
        val = await v.read("account/qd01", trace_id="01TRACE")
        assert val == "s3cret"
        reads = [c for c in rig.winagent.calls if c[0] == "POST" and c[1].endswith("/wa/v1/vault/account/qd01/read")]
        assert reads, rig.winagent.calls
        headers = {k.lower(): v_ for k, v_ in reads[-1][2].items()}
        assert headers.get("x-trace-id") == "01TRACE", headers
        assert headers.get("authorization") == "Bearer wa-token", headers


async def test_winagent_offline_windows_pool_unknown_and_wechat_not_addable(tmp_path):
    """02 §2.5 降级 ①:WinAgent 服务不在线 → windows 池 unknown、can_add(wechat)=false;04 H02:连续 3 次失败才判离线。"""
    with rig_ctx(tmp_path) as rig:
        rig.winagent.wechat_enabled = True
        await rig.agent.winagent_probe()
        rig.agent.pool.set_windows(total_mb=16384, wechat_enabled=True, known=True)
        assert rig.agent.pool.can_add("wechat")[0] is True
        rig.winagent.offline = True
        for _ in range(3):
            await rig.agent.winagent_probe()
        assert rig.agent.pool.can_add("wechat")[0] is False
        win = rig.agent.pool.snapshot()["pools"]["windows"]
        assert "unknown" in json.dumps(win).lower() or win.get("known") is False, win


async def test_h02_needs_three_consecutive_failures(tmp_path):
    """04 H02:WinAgent API 探活 30s,连续 3 次失败才判离线(R-09 去抖)⇒ 前两次失败仍在线,第三次才离线。"""
    with rig_ctx(tmp_path) as rig:
        await rig.agent.winagent_probe()
        assert rig.agent.health.winagent_online is True
        rig.winagent.offline = True
        await rig.agent.winagent_probe()
        assert rig.agent.health.winagent_online is True
        await rig.agent.winagent_probe()
        assert rig.agent.health.winagent_online is True
        await rig.agent.winagent_probe()
        assert rig.agent.health.winagent_online is False
        rig.winagent.offline = False
        await rig.agent.winagent_probe()
        assert rig.agent.health.winagent_online is True


def test_h03_dockerd_down_marks_check_firing(rig, client):
    """04 H03:dockerd 非 active 或超时 → crit;#72 /system/health 的 checks 反映 H03。"""
    rig.containers.dockerd_ok = False
    client.portal.call(rig.agent.dockerd_probe)
    b = client.get(f"{P}/system/health", headers=H(TOK_R)).json()
    assert b["checks"].get("H03") == "firing", b["checks"]


# ══════════════════════════════════════════════════════════════════════ 十一、H13 时间同步(04 §2.9 / H13)

class FakeAligner:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[int] = []
        self.clock: Optional[Clock] = None

    async def align(self, t_win_ms: int) -> bool:
        self.calls.append(t_win_ms)
        if self.ok and self.clock is not None:
            self.clock.now_ms = t_win_ms
        return self.ok


@contextmanager
def h13_rig(tmp_path, *, ok: bool = True, login_fn=None):
    clock = Clock()
    al = FakeAligner(ok)
    al.clock = clock
    with rig_ctx(tmp_path, clock=clock, aligner=al, login_fn=login_fn) as rig:
        yield rig, al


async def test_h13_drift_over_2s_triggers_align_no_alert_on_success(tmp_path):
    """04 §2.9:每 60s 取 t_win 与本地比,|Δ|>2s 判漂移 → 对齐;对齐后再测仍 >2s 才 warn ⇒ 对齐成功不告警。"""
    with h13_rig(tmp_path) as (rig, al):
        local_before = rig.clock.now_ms
        rig.winagent.now_ms = local_before + 5_000
        res = await rig.agent.timesync.probe()
        assert res["aligned"] is True, res                             # 本轮做了对齐
        assert al.calls and abs(al.calls[0] - local_before) >= 2_000, (al.calls, local_before)   # 对齐目标 = t_win
        assert res["drift"] is False, res                              # 对齐后再测:已不漂移
        assert not rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")
        assert rig.agent.health.h13_firing() is False


async def test_h13_within_threshold_no_align(tmp_path):
    """04 §2.9:|Δ| ≤ 2s 不算漂移 ⇒ 不对齐、不告警。"""
    with h13_rig(tmp_path) as (rig, al):
        rig.winagent.now_ms = rig.clock.now_ms + 1_000
        res = await rig.agent.timesync.probe()
        assert res["aligned"] is False and res["drift"] is False and al.calls == [], (res, al.calls)
        assert not rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")


def test_h13_align_failure_warns_and_health_check_firing(tmp_path):
    """04 §2.9 / H13:对齐后仍 >2s 推 warn(H13_CLOCK_DRIFT,subject=wsl);/system/health.checks.H13 = 'firing'。"""
    with h13_rig(tmp_path, ok=False) as (rig, al), api_ctx(rig) as client:
        rig.winagent.now_ms = rig.clock.now_ms + 5_000
        res = client.portal.call(rig.agent.timesync.probe)
        assert res["aligned"] is False and al.calls, (res, al.calls)
        assert rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")
        assert rig.agent.alerts.active[(H13_CLOCK_DRIFT, "wsl")].severity == "warn"
        assert rig.agent.health.h13_firing() is True
        b = client.get(f"{P}/system/health", headers=H(TOK_R)).json()
        assert b["checks"].get("H13") == "firing", b["checks"]


async def test_h13_resolves_when_back_within_threshold(tmp_path):
    """04 H13 / 02 §3.7:回到阈内 ⇒ 告警 resolved、不再 firing。"""
    with h13_rig(tmp_path, ok=False) as (rig, al):
        rig.winagent.now_ms = rig.clock.now_ms + 5_000
        await rig.agent.timesync.probe()
        assert rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")
        rig.winagent.now_ms = rig.clock.now_ms
        res = await rig.agent.timesync.probe()
        assert res["drift"] is False, res
        assert not rig.agent.alerts.is_firing(H13_CLOCK_DRIFT, "wsl")
        assert rig.agent.health.h13_firing() is False


def test_last_resume_ms_change_triggers_resume_handling(tmp_path):
    """02 §3.6 #4 / 04 §2.10(R3-5):Agent 轮询 GET /wa/v1/time.last_resume_ms 感知主机唤醒;值变化 ⇒ 唤醒处理
    (企点账号复跑 ensure_root),同值再轮询不重复触发。"""
    with h13_rig(tmp_path, login_fn=login_ok) as (rig, al), api_ctx(rig) as client:
        aid = _running(rig, client)
        rig.winagent.now_ms = rig.clock.now_ms
        rig.winagent.last_resume_ms = rig.clock.now_ms - 3_600_000  # 主机上次唤醒在 1 小时前(Agent 启动前的旧事件)
        client.portal.call(rig.agent.timesync.probe)                # 首轮:记住基线,不算唤醒
        rig.adb.calls.clear()
        rig.winagent.last_resume_ms = rig.clock.now_ms - 500        # 新的唤醒时刻
        res = client.portal.call(rig.agent.timesync.probe)
        assert res["resumed"] is True, res
        assert ("root", serial(1)) in rig.adb.calls, rig.adb.calls
        rig.adb.calls.clear()
        res2 = client.portal.call(rig.agent.timesync.probe)
        assert res2["resumed"] is False, res2
        assert ("root", serial(1)) not in rig.adb.calls


def test_scheduler_registers_60s_timesync_job(rig):
    """04 §2.9 / H13:Agent 每 60s GET /wa/v1/time ⇒ scheduler 注册了周期 60 s 的校时任务。"""
    jobs = rig.agent.scheduler.jobs
    hits = [k for k, j in jobs.items() if any(w in k.lower() for w in ("time", "h13", "clock", "sync"))]
    assert hits, list(jobs)
    assert any(jobs[k].interval_s == 60 for k in hits), {k: jobs[k].interval_s for k in hits}


# ══════════════════════════════════════════════════════════════════════ 十二、恢复(02 §2.6 / 05 §2.5.2 / 00 §8.1)

def _wants_running(rig, aid: str, channel: str = "qidian", **kw) -> None:
    rig.store.ensure_account(aid, channel, state="stopped", login_mode="password" if channel == "qidian" else "qrcode")
    rig.store.transition(aid, "stopped", desired_state="running", **kw)


async def test_recover_starts_desired_running_serially_by_seq(tmp_path):
    """02 §2.6:enabled=1 AND auto_recover=1 AND deleted_ms IS NULL 且 desired_state='running' 的账号按 seq 逐个(串行)恢复;
    05 §2.5.2:自恢复是 start 不是重登 ⇒ 无凭据的企点起来后停 login_required。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig:
        _wants_running(rig, "qd02")
        _wants_running(rig, "qd01")
        await rig.agent.accounts.recover()
        for aid in ("qd01", "qd02"):
            await rig.agent.accounts.wait_idle(aid)
            assert full(rig, aid)["state"] == "login_required", full(rig, aid)
        a = [e["ts_ms"] for e in state_events(rig, "qd01")]
        b = [e["ts_ms"] for e in state_events(rig, "qd02")]
        assert a and b
        assert max(a) <= min(b), (a, b)                              # 按 seq:qd01 整段先于 qd02


async def test_recover_skips_auto_recover_zero_and_keeps_desired_state(tmp_path):
    """02 §2.6 / 05 §2.5.5:auto_recover=0(启用但不自动拉起)跳过并保留 desired_state,由人手动 start。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig:
        _wants_running(rig, "qd01")
        _wants_running(rig, "qd02")
        rig.store.patch_account("qd02", auto_recover=False)
        await rig.agent.accounts.recover()
        await rig.agent.accounts.wait_idle("qd01")
        await rig.agent.accounts.wait_idle("qd02")
        assert full(rig, "qd01")["state"] == "login_required"
        row = full(rig, "qd02")
        assert row["state"] == "stopped" and row["desired_state"] == "running", row
        assert state_events(rig, "qd02") == []


async def test_recover_skips_disabled_deleted_and_desired_stopped(tmp_path):
    """02 §2.6 恢复规则:enabled=0、deleted_ms 非空、desired_state='stopped' 三类一律不进恢复集合。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig:
        _wants_running(rig, "qd01")
        rig.store.transition("qd01", "disabled", enabled=False)
        _wants_running(rig, "qd02", deleted_ms=rig.clock.now_ms)
        rig.store.ensure_account("qd03", "qidian", state="stopped")
        rig.store.transition("qd03", "stopped", desired_state="stopped")
        await rig.agent.accounts.recover()
        for aid in ("qd01", "qd02", "qd03"):
            await rig.agent.accounts.wait_idle(aid)
        assert full(rig, "qd01")["state"] == "disabled"
        assert full(rig, "qd02")["state"] == "stopped"
        assert full(rig, "qd03")["state"] == "stopped"
        assert not any(c[0] in ("create", "start") for c in rig.containers.calls), rig.containers.calls


async def test_recover_failure_does_not_block_next_account(tmp_path):
    """02 §2.6:任一步失败 → state=error + state_reason,同事务写 error_since_ms,发 account_state 事件;不阻塞下一账号。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig:
        _wants_running(rig, "qd01")
        _wants_running(rig, "qd02")
        rig.containers.fail_start.add("qtrade-qd01")
        await rig.agent.accounts.recover()
        await rig.agent.accounts.wait_idle("qd01")
        await rig.agent.accounts.wait_idle("qd02")
        r1 = full(rig, "qd01")
        assert r1["state"] == "error", r1
        assert r1["state_reason"], r1
        assert r1["runtime_error_since_ms"] is not None
        assert state_seq(rig, "qd01")[-1] == "error"
        assert full(rig, "qd02")["state"] == "login_required", full(rig, "qd02")


async def test_recover_runs_purge_and_ensure_root(tmp_path):
    """02 §2.6:恢复前每个账号先跑 _purge_ephemeral(崩溃残留补清);05 §2.5.2:自恢复拉起的企点同样走 ⑤b ensure_root。"""
    with rig_ctx(tmp_path, login_fn=login_ok) as rig:
        _wants_running(rig, "qd01")
        stale = f"{ROOT}/accounts/qd01/tmp/stale.bin"
        rig.fs.put(stale, 2048)
        await rig.agent.accounts.recover()
        await rig.agent.accounts.wait_idle("qd01")
        assert stale not in rig.fs.files
        assert ("root", serial(1)) in rig.adb.calls, rig.adb.calls
        assert purge_audits(rig)


async def test_recover_passes_can_add_before_start(tmp_path):
    """02 §2.6:恢复前每个账号先过 pool.can_add;§2.2.5 不够则 RESOURCE_EXHAUSTED ⇒ 预算只够一个时第二个不被拉起、第一个照常。"""
    with rig_ctx(tmp_path, wsl_total_mb=WSL_RESERVED + QUOTA["qidian"] + 100, login_fn=login_ok) as rig:
        _wants_running(rig, "qd01")
        _wants_running(rig, "qd02")
        await rig.agent.accounts.recover()
        await rig.agent.accounts.wait_idle("qd01")
        await rig.agent.accounts.wait_idle("qd02")
        assert full(rig, "qd01")["state"] == "login_required"
        assert full(rig, "qd02")["state"] in ("stopped", "error"), full(rig, "qd02")
        assert "qtrade-qd02" not in rig.containers.containers or rig.containers.containers["qtrade-qd02"].running is False
