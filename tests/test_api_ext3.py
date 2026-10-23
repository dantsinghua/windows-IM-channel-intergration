"""第三批 API 修复的开发者测试(独立复测 `.omc/handoffs/e2e-recheck.md` 的 N-1~N-5 + 三处顺带项)。

覆盖:
- **N-1**:`#91`/`#92` 的响应按 §3.4 **R6-55** 收口成**单一形状**(顶层平铺),一次性明文凭据不再因为
  「既有 `data` 又有顶层键」被客户端那一跳丢掉;
- **N-2**:`#1 /accounts`、`#26 /sessions`、`#58 /mail/inbox`、`#61 /mail/outbox` 的 **C-42 统一分页**
  (`limit` 真生效 + `next_cursor` + 游标稳定),🔴 含「`limit` 被静默忽略」的回归守卫;
- **N-3**:`GET /settings/api` 读得回 `public_domain`(与 `#102 configured_domain` 同一把键);
- **N-4**:`GET /mail/hmac-keys` 只读短名表(R6-58 (ac) 指名的唯一来源),**不回密钥值**;
- **N-5**:`#72` 两种响应的 `trace_id` 口径(按现行文档:**按 path 整端点**排除);
- 顺带:`#52` 的 `expires_at` 不再恒 null;`main.py` 给 `#83` 注入的**真**优雅停机钩子。

后端全是假件(复用 `test_api_ext.Rig`),**不碰真 docker / adb / WinAgent / 出网**。
"""
from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from test_api_ext import Rig, H, P, TOK_A, TOK_R, TOK_W


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _mk_accounts(rig, n: int) -> list[str]:
    ids = []
    for i in range(n):
        row = rig.store.create_account(channel="qidian", label=f"账号{i}", login_mode="password", quota_mb=3584)
        ids.append(row["id"])
    return ids


def _mk_session(rig, account_id: str, native: str, last_ms: int) -> str:
    sid = f"{account_id}:{native}"
    with rig.store._tx() as c:
        c.execute("INSERT INTO sessions(id, account_id, channel, native_id, name, kind, last_msg_ms, created_ms, updated_ms) "
                  "VALUES (?,?,'qidian',?,?, 'private', ?, ?, ?)",
                  (sid, account_id, native, f"对端{native}", last_ms, last_ms, last_ms))
    return sid


def _page(rig, path: str, **params: Any) -> dict[str, Any]:
    r = rig.client.get(f"{P}{path}", headers=H(), params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ══════════════════════════════════════════════════ N-1 #91/#92 一次性凭据的形状(R6-55)
def test_create_api_client_is_one_shape_with_token_at_top(rig):
    """#91:**单一形状** —— 行字段与一次性明文 `token` **同在顶层**,响应里**没有 `data`**。

    02 §3.4 R6-55:响应列写成基线 §7 对象名的端点才包 `data`;#91 的响应列是字面键集
    (「**一次性**返回 `token` 或 `secret`」)、`ApiClient` 不在 00 §7 的对象清单里 ⇒ 顶层平铺 + `ok`。
    此前两种形状各占一半,控制台 `request()` 的「有 data 就返回 data」把**只下发一次**的令牌丢了(N-1,P0)。
    """
    r = rig.client.post(f"{P}/settings/api-clients", headers=H(),
                        json={"name": "报价机器人", "level": "write", "allow_accounts": ["qd01"]})
    assert r.status_code == 201
    body = r.json()
    assert "data" not in body, "R6-55:同一响应不能既包 data 又在顶层放业务键"
    for k in ("app_id", "name", "level", "auth_kind", "enabled", "created_at"):
        assert k in body, f"行字段 {k} 应在顶层"
    assert isinstance(body["token"], str) and len(body["token"]) > 16
    assert "secret_hash" not in body
    # 一次性:列表里再也读不回明文
    row = next(x for x in rig.client.get(f"{P}/settings/api-clients", headers=H()).json()["data"]
               if x["app_id"] == body["app_id"])
    assert "token" not in row and "secret" not in row
    assert rig.client.get(f"{P}/accounts", headers=H(body["token"])).status_code == 200


def test_rotate_api_client_is_one_shape_too(rig):
    """#92:轮换回的也是只此一次的明文 ⇒ 同款顶层平铺。"""
    created = rig.client.post(f"{P}/settings/api-clients", headers=H(),
                              json={"name": "轮换形状", "level": "read"}).json()
    r = rig.client.post(f"{P}/settings/api-clients/{created['app_id']}/rotate", headers=H(), json={"grace_minutes": 5})
    assert r.status_code == 200
    body = r.json()
    assert "data" not in body and body["app_id"] == created["app_id"]
    assert isinstance(body["token"], str) and body["token"] != created["token"]
    assert body["grace_minutes"] == 5 and body["grace_until"].endswith("+08:00")
    assert body["level"] == "read" and "name" in body            # 行字段也在顶层


def test_hmac_client_returns_secret_at_top(rig):
    """`auth_kind='hmac'` 的一次性明文键名是 `secret`,同样在顶层。"""
    body = rig.client.post(f"{P}/settings/api-clients", headers=H(),
                           json={"name": "签名机器人", "level": "read", "auth_kind": "hmac"}).json()
    assert "data" not in body and isinstance(body["secret"], str) and body["secret_ref"].startswith("vault://api/")


# ══════════════════════════════════════════════════ N-2 C-42 统一分页
def test_accounts_limit_really_applies_and_pages_without_gap(rig):
    """#1:`?limit=` **真生效**,`next_cursor` 能把剩下的翻完,且不重不漏。

    02 §3.4 通用段 C-42 逐字:`?since&until&limit=50&cursor=<opaque>`,响应 `{ok, data, next_cursor}`。
    🔴 回归守卫:此前 `/accounts` 根本没有 `limit` 参数,FastAPI 把它**静默忽略**,页面以为自己限过量(N-2)。
    """
    ids = set(_mk_accounts(rig, 5))
    first = _page(rig, "/accounts", limit=2)
    assert len(first["data"]) == 2 and first["next_cursor"]
    seen = [x["id"] for x in first["data"]]
    cur = first["next_cursor"]
    while cur:
        pg = _page(rig, "/accounts", limit=2, cursor=cur)
        seen += [x["id"] for x in pg["data"]]
        cur = pg["next_cursor"]
    assert len(seen) == len(set(seen)) == len(ids) and set(seen) == ids


def test_accounts_cursor_is_stable_when_new_rows_arrive(rig):
    """G-16:游标按 `(排序列, 主键)` 双键定位、不用 OFFSET ⇒ 翻页期间新建账号也不会让老行重复或漏掉。"""
    old = set(_mk_accounts(rig, 4))
    first = _page(rig, "/accounts", limit=2)
    rig.clock.advance(1000)
    _mk_accounts(rig, 2)                                   # 翻页中途插入新行(它们排在更前面)
    rest, cur = [], first["next_cursor"]
    while cur:
        pg = _page(rig, "/accounts", limit=2, cursor=cur)
        rest += [x["id"] for x in pg["data"]]
        cur = pg["next_cursor"]
    seen = [x["id"] for x in first["data"]] + rest
    assert len(seen) == len(set(seen)), "游标漂了:同一行被翻到两次"
    assert old <= set(seen), "游标漂了:插入新行把老行挤没了"


@pytest.mark.parametrize("path", ["/accounts", "/sessions", "/mail/inbox", "/mail/outbox"])
def test_list_endpoints_have_next_cursor_and_reject_bad_limit(rig, path):
    """四个会变长的列表端点一律回 `next_cursor`,且 `limit` 非法时**报错而不是装作没看见**。"""
    body = _page(rig, path, limit=1)
    assert "next_cursor" in body and len(body["data"]) <= 1
    for bad in (0, -1, 99999):
        r = rig.client.get(f"{P}{path}", headers=H(), params={"limit": bad})
        assert r.status_code == 422 and r.json()["code"] == "INVALID_ARGS", f"{path} 的 limit={bad} 被静默忽略了"
        assert any(d["pointer"] == "/limit" for d in r.json()["error"]["details"])
    r = rig.client.get(f"{P}{path}", headers=H(), params={"cursor": "!!!不是游标!!!"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_cursor"


def test_sessions_pagination_orders_by_last_msg(rig):
    """#26:按 `last_msg_ms` 降序分页(出参键是 `last_msg_at`,R6-62 (f))。"""
    aid = _mk_accounts(rig, 1)[0]
    for i in range(3):
        _mk_session(rig, aid, f"1000{i}", 1_700_000_000_000 + i * 1000)
    first = _page(rig, "/sessions", limit=2)
    assert len(first["data"]) == 2 and first["next_cursor"]
    assert first["data"][0]["last_msg_at"] > first["data"][1]["last_msg_at"]
    second = _page(rig, "/sessions", limit=2, cursor=first["next_cursor"])
    assert len(second["data"]) == 1 and second["next_cursor"] is None
    got = {x["id"] for x in first["data"]} | {x["id"] for x in second["data"]}
    assert len(got) == 3


def test_mail_inbox_and_outbox_pagination(rig):
    """#58 / #61:收件按 `received_ms`、出件按 `created_ms` 降序分页。

    🔴 第四批(独立联调 P-1)起,两个端点的**出参**走视图:时间键是 ISO 的 `received_at`/`created_at`
    (00 §6「时间(API/事件/**邮件**)= ISO 8601 带时区偏移」),库列 `*_ms` 不再下发 ——
    **排序与游标仍按库行的 `received_ms`/`created_ms`**(这条用例断的就是排序与翻页,只把读的键换成出参键;
    同偏移的 ISO 串按字典序比较与按毫秒比较同序)。
    """
    ms = rig.agent.mail.ms
    for i in range(3):
        ms.inbox_insert(mailbox="default", protocol="imap", uid=100 + i, rfc_message_id=f"<in{i}@qtrade>",
                        body_sha256=f"sha-{i}", from_addr=f"ops{i}@corp", subject=f"指令 {i}",
                        received_ms=1_700_000_000_000 + i * 1000)
        ms.outbox_enqueue(kind="receipt", to_addrs=f"ops{i}@corp", subject=f"回执 {i}", body_text="x",
                          rfc_message_id=f"<m{i}@qtrade>", dedup_key=f"d{i}", template_version="v1",
                          now_ms=1_700_000_000_000 + i * 1000)
    for path, key in (("/mail/inbox", "received_at"), ("/mail/outbox", "created_at")):
        first = _page(rig, path, limit=2)
        assert len(first["data"]) == 2 and first["next_cursor"]
        assert first["data"][0][key] >= first["data"][1][key]
        second = _page(rig, path, limit=2, cursor=first["next_cursor"])
        assert len(second["data"]) == 1 and second["next_cursor"] is None
        ids = {x["id"] for x in first["data"]} | {x["id"] for x in second["data"]}
        assert len(ids) == 3, f"{path} 翻页重复或漏行"


def test_cursor_from_another_endpoint_is_rejected_or_empty(rig):
    """客户端**只能透传**游标:随手构造 / 张冠李戴的游标要么 400,要么至少不会把整表当成一页吐回来。"""
    _mk_accounts(rig, 3)
    forged = base64.urlsafe_b64encode(json.dumps({"ts_ms": 0, "id": "zzz"}).encode()).decode().rstrip("=")
    body = _page(rig, "/accounts", limit=10, cursor=forged)
    assert body["data"] == [] and body["next_cursor"] is None


# ══════════════════════════════════════════════════ N-3 settings/api ↔ #102 的 public_domain 往返
def test_settings_api_public_domain_round_trip(rig):
    """`PUT /settings/api {public_domain}` 写得进 ⇒ `GET /settings/api` 读得回 ⇒ `#102 configured_domain` 同值。

    02 #102 逐字:`configured_domain` = `settings api.public_domain`(P-SET 填写,`PUT /settings/api {public_domain}`);
    07 §2 `[api]` 行:该键**不在 agent.toml 里**(运行期可改、随 settings 备份)。
    此前读路径只映射 `AgentConfig.api` 的字段 ⇒ 写得进、读不回,P-SET 表单回填不了(N-3)。
    """
    assert rig.client.get(f"{P}/settings/api", headers=H()).json()["data"]["public_domain"] is None
    r = rig.client.put(f"{P}/settings/api", headers=H(), json={"port": 17600, "public_domain": "api.corp.example"})
    assert r.status_code == 200 and r.json()["data"]["public_domain"] == "api.corp.example"
    got = rig.client.get(f"{P}/settings/api", headers=H()).json()["data"]
    assert got["public_domain"] == "api.corp.example"
    assert rig.client.get(f"{P}/system/public-endpoint", headers=H(TOK_R)).json()["configured_domain"] == "api.corp.example"
    # 清空 = 传空串(P-SET 把域名删掉)
    rig.client.put(f"{P}/settings/api", headers=H(), json={"public_domain": ""})
    assert rig.client.get(f"{P}/settings/api", headers=H()).json()["data"]["public_domain"] is None
    assert rig.client.get(f"{P}/system/public-endpoint", headers=H(TOK_R)).json()["configured_domain"] is None


def test_settings_api_public_domain_not_written_into_toml_group(rig):
    """它只落 `settings['api.public_domain']`,**不混进** `config.api` 那一组(否则重启后会被当成 toml 键回灌)。"""
    rig.client.put(f"{P}/settings/api", headers=H(), json={"port": 17600, "public_domain": "d.example"})
    assert "public_domain" not in (rig.store.settings_get("config.api") or {})
    assert rig.store.settings_get("api.public_domain") == "d.example"


# ══════════════════════════════════════════════════ N-4 GET /mail/hmac-keys(短名表唯一来源)
def test_mail_hmac_keys_list_is_readable_and_never_leaks_secret(rig):
    """R6-58 (ac):`senders[].shortname` 不随 `#88 mail` 组下发,**从 `GET /mail/hmac-keys` 侧取** ⇒ 这个 GET 必须存在。

    此前只有 `#67 POST` / `#68 DELETE`,GET 回 `405`,P-SET 的 HMAC 短名表因此恒空(N-4)。
    🔴 只回引用 `secret_ref`,**绝不回密钥值**(明文只在 #67 那一次)。
    """
    assert rig.client.get(f"{P}/mail/hmac-keys", headers=H()).json()["data"] == []
    made = rig.client.post(f"{P}/mail/hmac-keys", headers=H(), json={"sender": "ops@corp", "short_name": "ops"})
    assert made.status_code == 200 and made.json()["secret"]
    rows = rig.client.get(f"{P}/mail/hmac-keys", headers=H()).json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"short_name", "sender", "route_id", "enabled", "secret_ref", "created_at"}
    assert row["short_name"] == "ops" and row["sender"] == "ops@corp" and row["enabled"] is True
    assert row["secret_ref"] == "vault://mail/hmac/cmd/ops" and row["created_at"].endswith("+08:00")
    assert "secret" not in row and made.json()["secret"] not in json.dumps(rows, ensure_ascii=False)
    # #68 吊销后不再列出
    assert rig.client.delete(f"{P}/mail/hmac-keys/ops", headers=H()).status_code == 200
    assert rig.client.get(f"{P}/mail/hmac-keys", headers=H()).json()["data"] == []


def test_mail_hmac_keys_list_requires_admin(rig):
    """级别与 #67/#68 同为 A(短名表属于 `[mail.inbound.hmac]` 配置面)。"""
    assert rig.client.get(f"{P}/mail/hmac-keys", headers=H(TOK_W)).status_code == 403
    assert rig.client.get(f"{P}/mail/hmac-keys").status_code == 401


# ══════════════════════════════════════════════════ N-5 #72 两种响应的 trace_id
def test_health_trace_id_exception_is_by_path(rig):
    """🔴 **现行文档口径**:`02` §3.4 例外① = 「`#72 GET /system/health` **这个路径**(免鉴权摘要与带令牌全量
    **两种形态都不注入** `trace_id`)…排除是**按 path 整端点**做的、与带不带令牌无关」
    (`00` §15g **R6-62 Ⅵ W1** 同句)。

    ⚠️ 独立复测的 N-5 依据的是该条**改口径之前**的原句(「例外只限免鉴权摘要」)。
    🔴 **总控 2026-09-21 已裁决:维持本实现**(两种形态都不注入),注入器不改;
    独立验收方那条「带令牌响应应带 trace_id」的联调用例由总控另派人按 R6-62 Ⅵ W1 改 —— **本用例即新口径的回归守卫**。
    """
    unauth = rig.client.get(f"{P}/system/health")
    assert unauth.status_code == 200 and "trace_id" not in unauth.json()
    full = rig.client.get(f"{P}/system/health", headers=H())
    assert full.status_code == 200 and "checks" in full.json(), "带令牌应当拿到全量体"
    assert "trace_id" not in full.json()
    # 对照组:同样是平铺键集的 #19,**在**例外之外 ⇒ 必须带 trace_id
    _mk_accounts(rig, 1)
    st = rig.client.get(f"{P}/accounts/qd01/state", headers=H())
    assert st.status_code == 200 and isinstance(st.json()["trace_id"], str) and st.json()["trace_id"]


# ══════════════════════════════════════════════════ 顺带:#52 expires_at / #83 停机钩子
def test_job_rows_carry_expires_at(rig):
    """#52 的 `expires_at` 不再恒 null:`jobs.expires_ms` 在**建行时**按 `[retention] export_jobs_days` 填(R4-13)。"""
    r = rig.client.post(f"{P}/messages/export", headers=H(), json={"fmt": "jsonl", "filter": {}})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    row = rig.job(job_id)
    assert row["expires_at"] and row["expires_at"].endswith("+08:00")
    assert row["expires_at"] > row["created_at"]
    ex = rig.client.get(f"{P}/exports/{job_id}", headers=H()).json()
    assert ex["expires_at"] == row["expires_at"]


def test_jobs_retention_days_follows_config(rig):
    """保留期跟着 `[retention] export_jobs_days` 走,不是写死的常数。"""
    assert rig.store.job_retention_days == rig.agent.cfg.retention.export_jobs_days
    now = rig.clock()
    job_id = rig.store.job_create(kind="diagnostics", actor="test", now_ms=now)
    row = rig.store.job_get(job_id)
    assert row["expires_ms"] == now + rig.agent.cfg.retention.export_jobs_days * 86_400_000


def test_main_injects_real_graceful_shutdown_hook(monkeypatch, tmp_path):
    """`#83` 的缺省钩子(`AgentApp.stop()`)**不会让进程退出** ⇒ 生产入口必须注入「stop() 之后让 uvicorn 退出」。

    这里把 `build` 与 `uvicorn.Server` 都换成假件:只验接线(钩子被装上、调用后两件事都发生),**不真起服务**。
    """
    import uvicorn

    from qtrade_agent import main as main_mod

    class FakeAgent:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeServer:
        def __init__(self, config) -> None:
            self.config = config
            self.should_exit = False
            self.ran = False

        def run(self) -> None:
            self.ran = True

    agent = FakeAgent()
    made: dict[str, Any] = {}

    def fake_server(config):
        made["server"] = FakeServer(config)
        return made["server"]

    monkeypatch.setattr(main_mod, "build", lambda cfg, db, config_path=None: (agent, object()))
    monkeypatch.setattr(uvicorn, "Server", fake_server)
    assert main_mod.main(["--config", str(tmp_path / "none.toml")]) == 0
    assert made["server"].ran is True
    hook = agent.shutdown_hook
    assert callable(hook)
    import asyncio
    asyncio.run(hook())
    assert agent.stopped is True and made["server"].should_exit is True
