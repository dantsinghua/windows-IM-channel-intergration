"""公网入站 HMAC(02 §3.5):canonical 逐字、``v1=`` 小写 hex、时钟容差 300 s、nonce 10 分钟、失败顺序七级、
``allow_ops`` 的星号只展开 ``danger=false``(00 §11.17 ②)。"""
from __future__ import annotations

import hashlib
import json

import pytest

from qtrade_agent.api.auth import ApiError
from qtrade_agent.hmac_inbound import (NonceCache, RateLimiter, HmacConfig, HmacVerifier, canonical_query,
                                       canonical_string, expand_allow_ops, has_hmac_headers, load_danger_ops,
                                       op_allowed, sign, signature_header, signatures_equal, split_url)

SECRET = "s3cr3t-app-key"
NONCE = "0123456789abcdef"          # 16 字符,正好在 §3.5 的 16~64 下界


def _client(store, *, app_id="partner", level="write", ip_allow=None, allow_ops=None, allow_accounts=None,
            rate=120, enabled=1, auth_kind="hmac", now=1):
    store.con.execute(
        "INSERT INTO api_clients(app_id, name, auth_kind, secret_hash, secret_ref, level, ip_allow_json, allow_ops_json, "
        "allow_accounts_json, rate_per_min, enabled, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (app_id, app_id, auth_kind, hashlib.sha256(SECRET.encode()).hexdigest(), f"vault://api/{app_id}", level,
         json.dumps(ip_allow or []), json.dumps(allow_ops or ["*"]), json.dumps(allow_accounts or ["*"]),
         rate, enabled, now, now))
    return app_id


def _verifier(store, clock, **kw):
    return HmacVerifier(store, secret_provider=lambda row: SECRET, clock=clock,
                        danger_ops=kw.pop("danger_ops", frozenset({"workflow_run"})), **kw)


def _headers(*, method="POST", path="/api/v1/accounts/qd01/send", query="", body=b"", ts=None, nonce=NONCE,
             app_id="partner", secret=SECRET, clock=None):
    ts = ts if ts is not None else clock() // 1000
    canonical = canonical_string(method, path, query, body, ts, nonce)
    return {"X-QT-AppId": app_id, "X-QT-Timestamp": str(ts), "X-QT-Nonce": nonce,
            "X-QT-Signature": signature_header(secret, canonical)}


# ---------------------------------------------------------------- canonical / 签名
def test_canonical_string_is_six_lines_in_spec_order():
    c = canonical_string("post", "/api/v1/x", "b=2&a=1", b"hello", 1700000000, "n" * 16)
    parts = c.split("\n")
    assert parts[0] == "POST"                                  # METHOD 大写
    assert parts[1] == "/api/v1/x"
    assert parts[2] == "a=1&b=2"                               # QUERY 按 key 字典序
    assert parts[3] == hashlib.sha256(b"hello").hexdigest()    # sha256hex(BODY)
    assert parts[4] == "1700000000" and parts[5] == "n" * 16
    assert len(parts) == 6


def test_canonical_query_encodes_rfc3986_and_empty_query_is_empty_string():
    assert canonical_query(None) == "" and canonical_query("") == ""
    assert canonical_query("?k=a b&j=x/y") == "j=x%2Fy&k=a%20b"
    assert canonical_query([("z", "1"), ("a", "2")]) == "a=2&z=1"


def test_signature_header_is_v1_prefix_lowercase_hex():
    canonical = canonical_string("POST", "/p", "", b"", 1, NONCE)
    header = signature_header(SECRET, canonical)
    assert header.startswith("v1=") and header[3:] == header[3:].lower()
    assert header[3:] == sign(SECRET, canonical)
    assert signatures_equal(sign(SECRET, canonical), header)
    assert signatures_equal(sign(SECRET, canonical), header[3:].upper())     # hex 大小写不敏感
    assert not signatures_equal(sign(SECRET, canonical), "v1=" + "0" * 64)


def test_sha256_of_empty_body_not_empty_string():
    assert canonical_string("GET", "/p", "", None, 1, NONCE).split("\n")[3] == hashlib.sha256(b"").hexdigest()


def test_split_url_gives_path_and_query():
    assert split_url("https://h.example/cb?a=1&b=2") == ("/cb", "a=1&b=2")
    assert split_url("https://h.example") == ("/", "")


def test_has_hmac_headers_is_case_insensitive():
    assert has_hmac_headers({"x-qt-appid": "a", "X-QT-Timestamp": "1", "x-qt-nonce": "n", "X-QT-SIGNATURE": "v1=x"})
    assert not has_hmac_headers({"x-qt-appid": "a"})


# ---------------------------------------------------------------- nonce / 限流
def test_nonce_cache_rejects_replay_within_ttl_and_accepts_after():
    c = NonceCache(ttl_s=600)
    assert c.check_and_put("app", "n1", 1_000_000) is True
    assert c.check_and_put("app", "n1", 1_000_000) is False
    assert c.check_and_put("other", "n1", 1_000_000) is True         # 键是 (app_id, nonce)
    assert c.check_and_put("app", "n1", 1_000_000 + 600_001) is True  # 10 分钟后可复用


def test_rate_limiter_sliding_window():
    r = RateLimiter()
    assert all(r.allow("a", 3, 1_000 + i) for i in range(3))
    assert r.allow("a", 3, 1_100) is False
    assert r.allow("a", 3, 1_000 + 60_001) is True


# ---------------------------------------------------------------- allow_ops(00 §11.17 ②)
def test_star_expands_only_danger_false_ops():
    all_ops = ["send_text", "read_messages", "workflow_run", "account_purge"]
    danger = ["workflow_run", "account_purge"]
    assert expand_allow_ops(["*"], all_ops, danger) == {"send_text", "read_messages"}
    assert expand_allow_ops(["*", "workflow_run"], all_ops, danger) == {"send_text", "read_messages", "workflow_run"}
    assert op_allowed(["*"], "send_text", danger) is True
    assert op_allowed(["*"], "workflow_run", danger) is False       # danger 必须逐条列名
    assert op_allowed(["workflow_run"], "workflow_run", danger) is True


def test_load_danger_ops_reads_capability_catalog():
    ops = load_danger_ops()
    assert isinstance(ops, frozenset)
    assert "send_text" not in ops                                    # 目录里五个 op 当前都是 danger=false


# ---------------------------------------------------------------- verify:正路
async def test_verify_ok_returns_principal_and_server_date(store, clock):
    _client(store)
    v = _verifier(store, clock)
    res = await v.verify(method="POST", path="/api/v1/accounts/qd01/send", body=b"{}",
                         headers=_headers(body=b"{}", clock=clock), op="send_text", account_id="qd01", required_level="write")
    assert res.app_id == "partner" and res.principal.level == "write"
    assert res.principal.actor == "app:partner"
    assert res.server_date.endswith("GMT")
    assert res.nonce == NONCE and res.timestamp == clock() // 1000


async def test_verify_with_query_string(store, clock):
    _client(store)
    v = _verifier(store, clock)
    h = _headers(method="GET", path="/api/v1/messages", query="limit=50&since=2026-01-01", body=b"", clock=clock)
    res = await v.verify(method="GET", path="/api/v1/messages", query="limit=50&since=2026-01-01", body=b"", headers=h)
    assert res.app_id == "partner"


# ---------------------------------------------------------------- verify:失败顺序七级(§3.5)
async def test_unknown_or_disabled_app_is_401(store, clock):
    v = _verifier(store, clock)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(clock=clock))
    assert e.value.http_status == 401 and e.value.reason == "app_unknown"
    _client(store, app_id="off", enabled=0)
    with pytest.raises(ApiError) as e2:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(app_id="off", clock=clock))
    assert e2.value.http_status == 401


async def test_bearer_client_cannot_use_hmac(store, clock):
    _client(store, app_id="bear", auth_kind="bearer")
    v = _verifier(store, clock)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(app_id="bear", clock=clock))
    assert e.value.reason == "auth_kind_mismatch"


async def test_ip_not_allowed_is_403_and_beats_bad_timestamp(store, clock):
    """失败顺序:IP(403)在时间戳(401)之前——故意给一个过期时间戳,仍应回 403。"""
    _client(store, ip_allow=["203.0.113.0/24"])
    v = _verifier(store, clock)
    h = _headers(ts=1, clock=clock)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=h, client_ip="198.51.100.7")
    assert e.value.http_status == 403 and e.value.reason == "ip_not_allowed"


async def test_ip_allowlist_hit_passes(store, clock):
    _client(store, ip_allow=["203.0.113.0/24"])
    v = _verifier(store, clock)
    res = await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", clock=clock), client_ip="203.0.113.9")
    assert res.app_id == "partner"


async def test_timestamp_skew_message_is_verbatim(store, clock):
    _client(store)
    v = _verifier(store, clock)
    stale = clock() // 1000 - HmacConfig().hmac_clock_skew_s - 1
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", ts=stale, clock=clock))
    assert e.value.http_status == 401 and e.value.message == "timestamp skew"


async def test_timestamp_at_exact_tolerance_passes(store, clock):
    _client(store)
    v = _verifier(store, clock)
    edge = clock() // 1000 - HmacConfig().hmac_clock_skew_s
    res = await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", ts=edge, clock=clock))
    assert res.timestamp == edge


async def test_nonce_replay_message_is_verbatim(store, clock):
    _client(store)
    v = _verifier(store, clock)
    h = _headers(path="/p", clock=clock)
    await v.verify(method="POST", path="/p", body=b"", headers=h)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=h)
    assert e.value.http_status == 401 and e.value.message == "nonce replay"


async def test_nonce_out_of_length_range_rejected(store, clock):
    _client(store)
    v = _verifier(store, clock)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", nonce="short", clock=clock))
    assert e.value.reason == "nonce_invalid"


async def test_bad_signature_is_401(store, clock):
    _client(store)
    v = _verifier(store, clock)
    h = _headers(path="/p", clock=clock) | {"X-QT-Signature": "v1=" + "0" * 64}
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=h)
    assert e.value.http_status == 401 and e.value.reason == "bad_signature"


async def test_body_tamper_breaks_signature(store, clock):
    _client(store)
    v = _verifier(store, clock)
    h = _headers(path="/p", body=b'{"a":1}', clock=clock)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b'{"a":2}', headers=h)
    assert e.value.reason == "bad_signature"


async def test_rate_limited_is_429_after_signature(store, clock):
    _client(store, rate=1)
    v = _verifier(store, clock)
    await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", nonce="n" * 16, clock=clock))
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", nonce="m" * 16, clock=clock))
    assert e.value.http_status == 429 and e.value.code == "RATE_LIMITED"


async def test_level_and_op_and_account_are_403(store, clock):
    _client(store, level="read", allow_accounts=["qq03"])
    v = _verifier(store, clock)
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", nonce="a" * 16, clock=clock),
                       required_level="write")
    assert e.value.http_status == 403 and e.value.reason == "level_insufficient"
    with pytest.raises(ApiError) as e2:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", nonce="b" * 16, clock=clock),
                       op="workflow_run")
    assert e2.value.reason == "op_not_allowed"                 # 星号不展开 danger=true
    with pytest.raises(ApiError) as e3:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", nonce="c" * 16, clock=clock),
                       account_id="qd01")
    assert e3.value.reason == "account_not_allowed"


async def test_secret_unavailable_is_401_not_500(store, clock):
    _client(store)
    v = HmacVerifier(store, secret_provider=lambda row: None, clock=clock, danger_ops=frozenset())
    with pytest.raises(ApiError) as e:
        await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", clock=clock))
    assert e.value.http_status == 401 and e.value.reason == "secret_unavailable"


async def test_async_secret_provider_is_awaited(store, clock):
    _client(store)

    async def provider(_row):
        return SECRET

    v = HmacVerifier(store, secret_provider=provider, clock=clock, danger_ops=frozenset())
    res = await v.verify(method="POST", path="/p", body=b"", headers=_headers(path="/p", clock=clock))
    assert res.app_id == "partner"
