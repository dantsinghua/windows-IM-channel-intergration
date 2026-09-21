"""backend-sec-2:密钥处理只看顶层键、嵌套的漏网 —— #89 `_stash_secrets` 与指令总线 `bus.redact_args`。

规格:00 §11.1 [CRED] 密钥不落库 / §11.2 [NOLOG];02 #88/#89「密码类只写不读、`*_ref` 只回引用」。
断言一律**直接查 SQLite 原始行 / 生成的 agent.toml 原文**(不经任何出参视图),证明明文不在。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from qtrade_agent.bus.bus import Bus, canonical_args_hash, redact_args
from qtrade_agent.config import AgentConfig
from qtrade_agent.events import Events
from qtrade_agent.models import Command, CommandResult
from qtrade_agent.settings_secrets import migrate_plaintext
from tests.test_integration_wiring_common import H, close_rig, make_rig

P = "/api/v1"
PW1, PW2, PW3, PW4 = "S3c-嵌套-一", "S3c-嵌套-二", "S3c-数组-三", "S3c-顶层-四"
ALL_PW = (PW1, PW2, PW3, PW4)


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    r.toml = tmp_path / "agent.toml"
    r.toml.write_text("# seed\n", encoding="utf-8")
    r.agent.config_path = str(r.toml)
    yield r
    close_rig(r)


def _settings_raw(rig) -> str:
    """settings 全表原始行拼起来(明文藏在哪个键、哪层嵌套都搜得到)。"""
    rows = [tuple(x) for x in rig.store.con.execute("SELECT key, value_json, updated_by FROM settings ORDER BY key")]
    return json.dumps(rows, ensure_ascii=False)


def _vault(rig) -> dict[str, str]:
    return {k: e.value for k, e in rig.agent.vault.entries.items()}


def _nested_body() -> dict[str, Any]:
    return {"qidian": {"confirm_timeout_ms": 15000, "password": PW1, "fallback": {"host": "h", "api_key_secret": PW2},
                       "servers": [{"name": "s0", "token": PW3}]}}


# ══════════════════════════════════════════════════ ① 嵌套密钥:库 / toml / 出参都没有明文,Vault 取得回
def test_nested_secrets_never_reach_sqlite(rig):
    r = rig.client.put(f"{P}/settings/adapters", headers=H(), json=_nested_body())
    assert r.status_code == 200, r.text
    raw = _settings_raw(rig)
    assert not any(pw in raw for pw in ALL_PW), raw
    assert not any(pw in r.text for pw in ALL_PW), "PUT 回显了明文"
    v = _vault(rig)
    assert v["settings/adapters/qidian/password"] == PW1
    assert v["settings/adapters/qidian/fallback/api_key_secret"] == PW2
    assert v["settings/adapters/qidian/servers/0/token"] == PW3
    stored = rig.store.settings_get("config.adapters")["qidian"]
    assert stored["password_ref"] == "vault://settings/adapters/qidian/password"
    assert stored["fallback"] == {"host": "h", "api_key_secret_ref": "vault://settings/adapters/qidian/fallback/api_key_secret"}
    assert stored["servers"] == [{"name": "s0", "token_ref": "vault://settings/adapters/qidian/servers/0/token"}]
    assert stored["confirm_timeout_ms"] == 15000, "非密钥键原样保留"


def test_agent_toml_never_contains_plaintext(rig):
    """数组里的对象会被 `_toml_dump` 串成字符串写进 agent.toml —— 数组嵌套的密钥是 toml 的漏点。"""
    r = rig.client.put(f"{P}/settings/api", headers=H(),
                       json={"public_ip_probe_urls": [{"url": "https://ip.example", "token": PW3}], "secret": PW4})
    assert r.status_code == 200 and r.json()["config_written"] is True, r.text
    text = rig.toml.read_text(encoding="utf-8")
    assert "[api]" in text and "public_ip_probe_urls" in text
    assert not any(pw in text for pw in ALL_PW), text
    assert not any(pw in _settings_raw(rig) for pw in ALL_PW)
    assert _vault(rig)["settings/api/public_ip_probe_urls/0/token"] == PW3


def test_get_never_returns_plaintext(rig):
    rig.client.put(f"{P}/settings/adapters", headers=H(), json=_nested_body())
    rig.client.put(f"{P}/settings/winagent", headers=H(), json={"timeout_ms": 5000, "secret": PW4})
    for g in ("adapters", "winagent"):
        got = rig.client.get(f"{P}/settings/{g}", headers=H())
        assert got.status_code == 200 and not any(pw in got.text for pw in ALL_PW), got.text


def test_empty_nested_secret_keeps_previous_ref(rig):
    """表单密码框留空(空串/null)= 不改:沿用上一版同位置的 `*_ref`,Vault 值不动(与顶层留空同义)。"""
    rig.client.put(f"{P}/settings/adapters", headers=H(), json=_nested_body())
    ver = rig.agent.vault.entries["settings/adapters/qidian/password"].version
    body = _nested_body()
    body["qidian"].update(password="", fallback={"host": "h2", "api_key_secret": None})
    body["qidian"]["servers"][0]["token"] = PW1 + "-新"
    assert rig.client.put(f"{P}/settings/adapters", headers=H(), json=body).status_code == 200
    stored = rig.store.settings_get("config.adapters")["qidian"]
    assert stored["password_ref"] == "vault://settings/adapters/qidian/password"
    assert stored["fallback"]["api_key_secret_ref"] == "vault://settings/adapters/qidian/fallback/api_key_secret"
    assert rig.agent.vault.entries["settings/adapters/qidian/password"].version == ver
    assert _vault(rig)["settings/adapters/qidian/servers/0/token"] == PW1 + "-新"
    assert "password" not in stored and "api_key_secret" not in stored["fallback"]


# ══════════════════════════════════════════════════ ② Vault 不可用 ⇒ 503,整个请求不落库
def test_vault_down_is_503_and_nothing_written(rig):
    before, toml_before = _settings_raw(rig), rig.toml.read_text(encoding="utf-8")
    rig.agent.vault.offline = True
    for group, body in (("api", {"public_domain": "a.example", "secret": PW4}), ("adapters", _nested_body())):
        r = rig.client.put(f"{P}/settings/{group}", headers=H(), json=body)
        assert r.status_code == 503, r.text
        err = r.json()["error"]
        assert r.json()["code"] == "NOT_READY" and err["reason"] == "vault_unavailable" and err["retryable"] is True
        assert not any(pw in r.text for pw in ALL_PW)
    assert _settings_raw(rig) == before, "Vault 失败后库里有改动(含 public_domain 旁路)"
    assert rig.store.settings_get("api.public_domain") is None
    assert rig.toml.read_text(encoding="utf-8") == toml_before


def test_unknown_key_still_400_before_any_vault_write(rig):
    """#89 第四批「未知键 ⇒ 400 且不落库」不回退:也不许先把密钥写进 Vault。"""
    before = _settings_raw(rig)
    r = rig.client.put(f"{P}/settings/retention", headers=H(), json={"text_days": 1, "db_password": PW4})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "unknown_key"
    assert _settings_raw(rig) == before and _vault(rig) == {}


# ══════════════════════════════════════════════════ ③ 已有顶层键:Vault 路径与改前逐字一致
def test_top_level_vault_paths_unchanged(rig):
    r = rig.client.put(f"{P}/settings/winagent", headers=H(), json={"timeout_ms": 5000, "secret": PW4, "db_password": PW1})
    assert r.status_code == 200, r.text
    assert r.json()["secret_refs"] == {"secret": "vault://settings/winagent/secret",
                                       "db_password": "vault://settings/winagent/db_password"}
    assert rig.store.settings_get("config.winagent.secret_ref") == "vault://settings/winagent/secret"
    assert rig.store.settings_get("config.winagent.db_password_ref") == "vault://settings/winagent/db_password"
    assert _vault(rig) == {"settings/winagent/secret": PW4, "settings/winagent/db_password": PW1}
    # 🔴 WinAgent vault_index.scope 只收七类(02 §3.2);原先传 "settings" 在真机上会 400
    assert {e.scope for e in rig.agent.vault.entries.values()} == {"other"}
    assert not any(pw in _settings_raw(rig) for pw in ALL_PW)


# ══════════════════════════════════════════════════ ④ 存量迁移(运行期调度任务,不进 --init-db)
def _seed_plaintext(rig) -> None:
    blob = _nested_body()
    rig.store.settings_set("config.adapters", blob, actor="old")
    rig.store.settings_set("config.__all__", {"adapters": blob, "messages": {"late_after_s": 90}}, actor="old")


def test_migration_moves_plaintext_then_is_idempotent(rig):
    _seed_plaintext(rig)
    assert asyncio.run(migrate_plaintext(rig.store, rig.agent.vault)) == 2
    assert not any(pw in _settings_raw(rig) for pw in ALL_PW), _settings_raw(rig)
    assert rig.store.settings_get("config.adapters")["qidian"]["password_ref"] == "vault://settings/adapters/qidian/password"
    assert rig.store.settings_get("config.__all__")["adapters"] == rig.store.settings_get("config.adapters")
    assert _vault(rig)["settings/adapters/qidian/servers/0/token"] == PW3
    raw, vers = _settings_raw(rig), {k: e.version for k, e in rig.agent.vault.entries.items()}
    changes = rig.store.con.total_changes
    assert asyncio.run(migrate_plaintext(rig.store, rig.agent.vault)) == 0
    assert _settings_raw(rig) == raw and rig.store.con.total_changes == changes, "第二轮不许有任何写"
    assert {k: e.version for k, e in rig.agent.vault.entries.items()} == vers


def test_migration_vault_down_leaves_rows_untouched(rig):
    _seed_plaintext(rig)
    raw = _settings_raw(rig)
    rig.agent.vault.offline = True
    assert asyncio.run(migrate_plaintext(rig.store, rig.agent.vault)) == 0
    assert _settings_raw(rig) == raw, "Vault 没写成就不许动库(否则明文丢了、Vault 也没有)"
    rig.agent.vault.offline = False
    assert asyncio.run(migrate_plaintext(rig.store, rig.agent.vault)) == 2


def test_migration_all_blob_diverging_uses_own_prefix(rig):
    """`config.__all__` 里的同组值与 `config.<group>` 不同 ⇒ 另起前缀,不覆盖 `config.<group>` 引用指向的 Vault 值。"""
    rig.store.settings_set("config.adapters", _nested_body(), actor="old")
    rig.store.settings_set("config.__all__", {"adapters": {"qidian": {"password": PW4}}}, actor="old")
    assert asyncio.run(migrate_plaintext(rig.store, rig.agent.vault)) == 2
    v = _vault(rig)
    assert v["settings/adapters/qidian/password"] == PW1 and v["settings/__all__/adapters/qidian/password"] == PW4
    assert not any(pw in _settings_raw(rig) for pw in ALL_PW)


def test_migration_runs_as_scheduler_job(rig):
    _seed_plaintext(rig)
    assert rig.agent.scheduler.jobs["settings_secret_migrate"].run_immediately is True
    asyncio.run(rig.agent.settings_secret_migrate_tick())
    assert not any(pw in _settings_raw(rig) for pw in ALL_PW)


# ══════════════════════════════════════════════════ ⑤ bus.redact_args:递归擦除,执行体仍收原值,幂等语义不变
def _args() -> dict[str, Any]:
    return {"group": "adapters", "value": {"qidian": {"password": PW1, "fallback": {"api_key_secret": PW2}},
                                           "list": [{"token": PW3}]}, "secret": PW4}


def test_redact_args_recursive_and_does_not_mutate():
    args = _args()
    snapshot = json.dumps(args, sort_keys=True, ensure_ascii=False)
    out = redact_args(args)
    assert json.dumps(args, sort_keys=True, ensure_ascii=False) == snapshot, "脱敏改到了原参数"
    assert out["secret"] == "***" and out["value"]["qidian"]["password"] == "***"
    assert out["value"]["qidian"]["fallback"]["api_key_secret"] == "***" and out["value"]["list"][0]["token"] == "***"
    assert out["group"] == "adapters"
    t = redact_args({"session": "qd01:1", "text": "收到"})                # P-11 原行为不变
    assert "text" not in t and t["text_len"] == 2 and len(t["text_sha8"]) == 8


class _RecAdapter:
    """最小适配器:记下执行体真正收到的参数。"""
    capabilities = frozenset({"settings_write"})

    def __init__(self):
        self.seen: list[dict[str, Any]] = []

    async def execute(self, acct, cmd):
        self.seen.append(cmd.args)
        return CommandResult(ok=True, code="OK", trace_id=cmd.trace_id or "", data={"n": len(self.seen)})

    async def confirm_probe(self, acct, cmd):
        return False

    async def poll(self, acct, *, only_sessions=None):
        return None


@pytest.fixture
async def bus_rig(store, clock):
    ad = _RecAdapter()
    bus = Bus(store=store, events=Events(store), adapters={"qidian": ad}, cfg=AgentConfig(), clock=clock)
    yield bus, ad, store
    await bus.close()


async def test_bus_executor_gets_original_args_but_db_has_none(bus_rig):
    bus, ad, store = bus_rig
    res = await bus.submit(Command("qd01", "settings_write", _args(), idempotency_key="s2-1"))
    assert res.ok, res
    assert ad.seen == [_args()], "执行体收到的不是原值"
    raw = json.dumps([tuple(r) for r in store.con.execute("SELECT * FROM commands")], ensure_ascii=False)
    assert not any(pw in raw for pw in ALL_PW), raw
    row = store.con.execute("SELECT args_json FROM commands WHERE trace_id=?", (res.trace_id,)).fetchone()
    assert json.loads(row[0]) == redact_args(_args())


async def test_bus_idempotency_semantics_unchanged(bus_rig):
    """幂等 `args_hash` 仍按**脱敏前**原值算:同参重放 = IDEMPOTENT_REPLAY;只改嵌套密码 = 参数不同(脱敏后两者相同,若误用脱敏值就判不出)。"""
    bus, ad, store = bus_rig
    first = await bus.submit(Command("qd01", "settings_write", _args(), idempotency_key="s2-2"))
    assert store.idem_get("qd01", "s2-2")["args_hash"] == canonical_args_hash(_args())
    again = await bus.submit(Command("qd01", "settings_write", _args(), idempotency_key="s2-2"))
    assert again.code == "IDEMPOTENT_REPLAY" and again.trace_id == first.trace_id and len(ad.seen) == 1
    changed = _args()
    changed["value"]["qidian"]["password"] = PW1 + "-改"
    bad = await bus.submit(Command("qd01", "settings_write", changed, idempotency_key="s2-2"))
    assert bad.code == "INVALID_ARGS" and bad.error.reason == "idempotency_args_mismatch"
