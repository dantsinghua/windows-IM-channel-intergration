"""第二轮接线:补齐的端点(#57/#66/#88/#89/#94/#95/#96~#101/#103)与执行体(media / monitor 采样 / jobs_reclaimer / #102)。

规格:02 §3.4.5/§3.4.6 端点表、§2.8.2 媒体策略、§3.1 ``jobs``/``health_samples``、§2.2.12 + #102 公网出口。
全程假件:``FakeDownloader``/``FakeProcReader``/``FakeHttp``/``FakeImap``…… **不出网、不碰真设备**。
"""
from __future__ import annotations

import json

import pytest

from qtrade_agent.media import FakeDownloader, MediaStore, sniff_mime
from qtrade_agent.monitor import FakeProcReader, JobsReclaimer, PublicEndpointProbe, Sampler
from qtrade_agent.webhook import HttpResponse
from tests.test_integration_wiring_common import TOKEN_READ, TOKEN_WRITE, H, close_rig, make_rig

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 8
JPG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 4


@pytest.fixture
def rig(tmp_path):
    r = make_rig(tmp_path)
    r.agent.media._dl = FakeDownloader()
    yield r
    close_rig(r)


def _dl(rig) -> FakeDownloader:
    return rig.agent.media._dl


# ══════════════════════════════════════════════════════════════════ 媒体子系统(02 §2.8.2)
def test_media_sniffs_mime_by_magic_not_content_type():
    """§2.8.2 原句:**按字节头识别、不信 `Content-Type`**(ibquote 教训)。"""
    assert sniff_mime(PNG[:16]) == "image/png"
    assert sniff_mime(JPG[:16]) == "image/jpeg"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"
    assert sniff_mime(b"whatever") == "application/octet-stream"


def test_media_put_bytes_writes_no_extension_file_under_yyyymm(rig, tmp_path):
    """落盘 = ``<media_dir>/<yyyymm>/<sha256>``(**无扩展名**,类型在 `media.mime`);中间文件走 `media/tmp/` 再 rename。"""
    import os
    out = rig.agent.media.put_bytes(PNG, kind="image", origin={"url": "https://x/1.png"})
    assert out["status"] == "ready" and out["mime"] == "image/png" and out["size"] == len(PNG)
    path = rig.agent.media.abs_path(out["rel_path"])
    assert os.path.isfile(path) and os.path.basename(path) == out["sha256"]     # 文件名 = sha256、无扩展名
    assert "." not in os.path.basename(path)
    assert open(path, "rb").read() == PNG
    assert os.listdir(os.path.join(rig.agent.media.media_dir, "tmp")) == []     # 中间文件已 rename 走


def test_media_dedups_by_sha256_across_accounts(rig):
    """去重按 `sha256`:同一张图被多账号收到只存一份,`ref_count` 各记各的(P-17)。"""
    a = rig.agent.media.put_bytes(PNG, kind="image", origin={"url": "u1", "account_id": "qd01"})
    b = rig.agent.media.put_bytes(PNG, kind="image", origin={"url": "u2", "account_id": "qq01"})
    assert a["media_id"] == b["media_id"] and b.get("deduped") is True
    assert rig.agent.media.get(a["media_id"])["ref_count"] == 2


def test_media_oversize_is_skipped_and_never_retried(rig):
    """C-23 上限:`image` 20 MB;超限 `status='skipped_oversize'`(**不重试**)。"""
    big = b"\x89PNG\r\n\x1a\n" + b"x" * (20 * 1024 * 1024)
    out = rig.agent.media.put_bytes(big, kind="image", origin={"url": "u"})
    assert out["status"] == "skipped_oversize" and out["sha256"] is None
    row = rig.agent.media.get(out["media_id"])
    assert row["status"] == "skipped_oversize" and row["attempts"] == 0


async def test_media_fetch_into_downloads_and_marks_ready(rig):
    _dl(rig).blobs["https://x/a.png"] = PNG
    mid = rig.agent.media.note(kind="image", origin={"url": "https://x/a.png"})
    out = await rig.agent.media.fetch_into(mid)
    assert out["status"] == "ready" and out["mime"] == "image/png"
    assert rig.agent.media.read_file(mid) == PNG


async def test_media_fetch_failure_records_reason(rig):
    _dl(rig).fail.add("https://x/bad")
    mid = rig.agent.media.note(kind="image", origin={"url": "https://x/bad"})
    out = await rig.agent.media.fetch_into(mid)
    assert out["status"] == "failed" and out["fail_reason"] == "fake_download_failed"
    assert rig.agent.media.get(mid)["attempts"] == 1


async def test_media_downloads_stop_at_high_watermark(rig):
    """§2.8.8:磁盘 high 水位起**停下载**(转 lazy),行仍是 `pending`,不算失败。"""
    _dl(rig).blobs["https://x/a.png"] = PNG
    mid = rig.agent.media.note(kind="image", origin={"url": "https://x/a.png"})
    rig.disk.free = 1500                       # < disk_high_mb(2048)
    await rig.agent.scheduler.run_once("disk_watermark")
    out = await rig.agent.media.fetch_into(mid)
    assert out["status"] == "pending" and out["fail_reason"] == "disk_high_watermark"
    assert _dl(rig).calls == []                # 一次都没下


def test_media_rows_are_not_written_at_critical_watermark(rig):
    rig.disk.free = 500                        # < disk_critical_mb(1024)
    rig.agent.scheduler.jobs["disk_watermark"]  # noqa: B018 —— 任务已注册
    rig.agent.maintenance.check_watermark()
    assert rig.agent.media.note(kind="image", origin={"url": "u"}) is None


async def test_media_tick_creates_rows_from_messages_and_fetches_eager(rig):
    """§2.8.2:`image`/`voice` 是 `eager`(立即下载),`file`/`video` 是 `lazy`(只建行、等 #96)。"""
    from qtrade_agent.models import Message, Session
    _dl(rig).blobs["https://x/pic.png"] = PNG
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    for kind, url in (("image", "https://x/pic.png"), ("video", "https://x/clip.mp4")):
        rig.store.ingest(Message(account_id="qd01", channel="qidian", session=Session("qd01", "123", "private", name="x"),
                                 dir="in", type=kind, text=None, ts_ms=rig.clock(), source="qidian_db",
                                 ext_msg_id=f"qd:{kind}", media=[{"kind": kind, "url": url, "state": "pending"}]))
    await rig.agent.scheduler.run_once("media_fetch")
    rows = {r["kind"]: dict(r) for r in rig.store.con.execute("SELECT * FROM media")}
    assert rows["image"]["status"] == "ready" and rows["video"]["status"] == "pending"      # lazy 的不下
    msgs = {m["ext_msg_id"]: json.loads(m["media_json"]) for m in rig.store.list_messages("qd01")}
    assert msgs["qd:image"][0]["media_id"] == rows["image"]["id"]                           # media_id 写回消息行


async def test_media_endpoints_fetch_and_get(rig):
    """#96 触发懒加载 → `{media_id, sha256, state}`;#55 按 hash 取字节;到期(expired)→ 410。"""
    from qtrade_agent.models import Message, Session
    _dl(rig).blobs["https://x/f.bin"] = b"PK\x03\x04payload"
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    res = rig.store.ingest(Message(account_id="qd01", channel="qidian", session=Session("qd01", "123", "private", name="x"),
                                   dir="in", type="file", text=None, ts_ms=rig.clock(), source="qidian_db",
                                   ext_msg_id="qd:f", media=[{"kind": "file", "url": "https://x/f.bin", "state": "pending"}]))
    r = rig.client.post(f"/api/v1/messages/{res.id}/media/0/fetch", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["state"] == "ready" and r.json()["sha256"]
    sha = r.json()["sha256"]
    got = rig.client.get(f"/api/v1/media/{sha}", headers=H(TOKEN_READ))
    assert got.status_code == 200 and got.content == b"PK\x03\x04payload"
    assert rig.client.get("/api/v1/media/deadbeef", headers=H(TOKEN_READ)).status_code == 404
    assert rig.client.post(f"/api/v1/messages/{res.id}/media/9/fetch", headers=H(TOKEN_WRITE)).status_code == 404


# ══════════════════════════════════════════════════════════════════ monitor 采样(04 §2.4.5)
async def test_sampler_writes_health_samples(tmp_path):
    """🔴 采样是 `pool_calibrate` 与 #77 的**唯一数据源**;没有它自校准在空库上恒回「无建议」。"""
    r = make_rig(tmp_path)
    try:
        r.agent.sampler = Sampler(r.store, reader=FakeProcReader(containers={"qtrade-qd01": {"mem_mb": 2100.0, "mem_anon_mb": 1800.0}}),
                                  data_dir=str(tmp_path), clock=r.clock, disk=r.disk)
        r.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
        r.store.upsert_runtime("qd01", kind="redroid", container_name="qtrade-qd01")
        n = await r.agent.sampler.sample_once()
        assert n == 4                                   # wsl + process + disk + 一个容器
        by_scope = {row["scope"]: dict(row) for row in r.store.con.execute("SELECT * FROM health_samples")}
        assert by_scope["wsl"]["mem_max_mb"] == 11264.0 and by_scope["wsl"]["cpu_pct"] == 12.5
        assert by_scope["container"]["mem_anon_mb"] == 1800.0 and by_scope["container"]["subject"] == "qtrade-qd01"
        assert by_scope["disk"]["disk_free_mb"] == 100000.0
        assert json.loads(by_scope["wsl"]["extra_json"])["mem_available_mb"] == 6100.0
    finally:
        close_rig(r)


async def test_sampler_skips_unreadable_items(tmp_path):
    """任何一项读不到就**跳过那一行**:不写 0、不抛(否则 scheduler 的任务会被一次读失败打红)。"""
    r = make_rig(tmp_path)
    try:
        r.agent.sampler = Sampler(r.store, reader=FakeProcReader(mem={}, cpu=None, rss=None), data_dir=str(tmp_path),
                                  clock=r.clock, disk=r.disk)
        assert await r.agent.sampler.sample_once() == 1        # 只剩 disk 那行
    finally:
        close_rig(r)


async def test_monitor_tick_feeds_calibrator(rig, tmp_path):
    """采样接上之后,#71 自校准就不再是空手而回。"""
    rig.agent.sampler = Sampler(rig.store, reader=FakeProcReader(), data_dir=str(tmp_path), clock=rig.clock, disk=rig.disk)
    for _ in range(3):
        rig.clock.advance(10_000)
        await rig.agent.scheduler.run_once("monitor_sample")
    assert rig.store.con.execute("SELECT COUNT(*) FROM health_samples WHERE resolution='raw'").fetchone()[0] >= 3
    assert rig.agent.calibrator.suggest_wsl_total_mb(now_ms=rig.clock()) == 11264


async def test_sampler_rolls_up_to_1m(tmp_path):
    r = make_rig(tmp_path)
    try:
        s = Sampler(r.store, reader=FakeProcReader(), data_dir=str(tmp_path), clock=r.clock, disk=r.disk)
        for _ in range(3):
            await s.sample_once()
            r.clock.advance(10_000)
        r.clock.advance(61_000)
        assert s.roll_up()["1m"] >= 1
        row = r.store.con.execute("SELECT * FROM health_samples WHERE resolution='1m' AND scope='wsl'").fetchone()
        assert row is not None and json.loads(row["agg_max_json"])["mem_max_mb"] == 11264.0
    finally:
        close_rig(r)


# ══════════════════════════════════════════════════════════════════ jobs_reclaimer(02 §3.1 R6-16)
async def test_jobs_reclaimer_requeues_then_kills(rig):
    """超龄 `running`:`attempt_count < 3` 退回 `queued`;`>= 3` 判 `failed`(**不无限重跑**)。"""
    rc = JobsReclaimer(rig.store, reclaim_after_s=900, events=rig.agent.events, clock=rig.clock)
    young = rig.store.job_create(kind="messages_export", actor="a")
    old = rig.store.job_create(kind="diagnostics", actor="a")
    rig.store.job_start(young)
    rig.store.job_start(old)
    assert (await rc.reclaim_once())["requeued"] == 0                  # 还没超龄
    rig.clock.advance(901_000)
    rig.store.con.execute("UPDATE jobs SET updated_ms=? WHERE job_id IN (?,?)", (rig.clock() - 901_000, young, old))
    out = await rc.reclaim_once()
    assert out == {"requeued": 2, "failed": 0}
    assert rig.store.job_get(old)["state"] == "queued"
    rig.store.con.execute("UPDATE jobs SET state='running', attempt_count=3, updated_ms=? WHERE job_id=?",
                          (rig.clock() - 901_000, old))
    out = await rc.reclaim_once()
    assert out["failed"] == 1 and rig.store.job_get(old)["state"] == "failed"
    assert json.loads(rig.store.job_get(old)["error_json"])["code"] == "TIMEOUT"
    assert [e["payload"]["state"] for e in rig.events_of("job")] == ["failed"]


async def test_jobs_reclaimer_is_registered(rig):
    assert "jobs_reclaimer" in rig.agent.scheduler.jobs
    assert await rig.agent.scheduler.run_once("jobs_reclaimer") is True


# ══════════════════════════════════════════════════════════════════ #102 公网出口探测(E-3)
async def test_public_endpoint_probe_is_off_by_default(rig):
    """🔴 `[api] public_ip_check_interval_s` **默认 0 = 关**(§11.22 [SCOPE]):一个字节都不出网。"""
    assert rig.agent.cfg.api.public_ip_check_interval_s == 0
    assert await rig.agent.endpoint_probe.tick() is None
    assert rig.http.requests == []
    r = rig.client.get("/api/v1/system/public-endpoint", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["public_ip"] is None


async def test_public_endpoint_probe_emits_net_event_on_change(rig):
    probe = PublicEndpointProbe(rig.store, http=rig.http, events=rig.agent.events,
                                urls=("https://probe.example/ip",), interval_s=60, clock=rig.clock)
    rig.http.queue("https://probe.example/ip", HttpResponse(200, b"203.0.113.9\n"))
    st = await probe.tick()
    assert st["public_ip"] == "203.0.113.9" and st["unreachable_rounds"] == 0
    evs = [e["payload"] for e in rig.events_of("net")]
    assert evs and evs[-1]["code"] == "NET_PUBLIC_ENDPOINT_CHANGED" and evs[-1]["public_ip"] == "203.0.113.9"
    r = rig.client.get("/api/v1/system/public-endpoint", headers=H(TOKEN_READ))
    assert r.json()["public_ip"] == "203.0.113.9" and r.json()["checked_at"]


async def test_public_endpoint_probe_counts_unreachable_rounds(rig):
    probe = PublicEndpointProbe(rig.store, http=rig.http, events=rig.agent.events,
                                urls=("https://probe.example/ip",), interval_s=60, clock=rig.clock)
    rig.http.queue("https://probe.example/ip", OSError("no route"))
    st = await probe.probe()
    assert st["unreachable_rounds"] == 1 and st.get("public_ip") is None
    assert [e for e in rig.events_of("net")] == []           # 探不到不算「变化」,不发事件


# ══════════════════════════════════════════════════════════════════ #88 / #89 配置分组
def test_settings_group_read_levels_and_secret_hiding(rig):
    """#88:``api``/``mail`` 组要 A、其余 R;``*_ref`` 只回引用、密码类字段不出现。"""
    r = rig.client.get("/api/v1/settings/bus", headers=H(TOKEN_READ))
    assert r.status_code == 200 and r.json()["data"]["confirm_timeout_qidian_ms"] == 15000
    assert rig.client.get("/api/v1/settings/api", headers=H(TOKEN_READ)).status_code == 403      # api 组要 A
    api = rig.client.get("/api/v1/settings/api", headers=H()).json()["data"]
    assert api["port"] == 17600 and not any(k.endswith(("secret", "password", "token")) for k in api)
    ad = rig.client.get("/api/v1/settings/adapters", headers=H(TOKEN_READ)).json()["data"]
    assert set(ad) == {"qidian", "qq", "wechat"} and ad["qq"]["heartbeat_timeout_s"] == 40
    assert rig.client.get("/api/v1/settings/nope", headers=H()).status_code == 404


def test_settings_retention_truncates_days_and_checks_watermark_order(rig):
    """#89:数据类 ``*_days > 30`` **按 30 截断并 WARN**(E-18);三级水位顺序非法 → 400。"""
    r = rig.client.put("/api/v1/settings/retention", headers=H(), json={"messages_days": 365, "audit_days": 30})
    assert r.status_code == 200 and r.json()["data"]["messages_days"] == 30 and r.json()["warnings"]
    assert r.json()["restart_required"] is True and r.json()["config_written"] is False        # 没给 config_path
    bad = rig.client.put("/api/v1/settings/retention", headers=H(), json={"disk_high_mb": 9999})
    assert bad.status_code == 400 and bad.json()["error"]["reason"] == "watermark_order"
    assert rig.client.put("/api/v1/settings/retention", headers=H(TOKEN_WRITE), json={}).status_code == 403


def test_settings_put_writes_agent_toml_atomically(tmp_path):
    """#89:写回 ``agent.toml`` 用**临时文件 + rename**;没给 ``config_path`` 就只落 settings 并回 ``config_written:false``。"""
    import os
    toml = tmp_path / "agent.toml"
    toml.write_text("# seed\n", encoding="utf-8")
    r = make_rig(tmp_path)
    try:
        r.agent.config_path = str(toml)
        resp = r.client.put("/api/v1/settings/messages", headers=H(), json={"late_after_s": 90, "capture_text": False})
        assert resp.status_code == 200 and resp.json()["config_written"] is True
        text = toml.read_text(encoding="utf-8")
        assert "[messages]" in text and "late_after_s = 90" in text and "capture_text = false" in text
        assert not os.path.exists(str(toml) + ".tmp")                     # 临时文件已 rename 走
    finally:
        close_rig(r)


def test_settings_put_secret_only_writes_never_reads(rig):
    """#89:密码类字段**只写不读** —— body 里给 ``secret`` 即写 Vault 并回 ``secret_ref``,原值不落 settings。"""
    r = rig.client.put("/api/v1/settings/winagent", headers=H(), json={"timeout_ms": 5000, "secret": "p@ss"})
    assert r.status_code == 200 and r.json()["secret_refs"]["secret"] == "vault://settings/winagent/secret"
    assert "secret" not in r.json()["data"]
    assert rig.agent.vault.entries["settings/winagent/secret"].value == "p@ss"
    assert "p@ss" not in json.dumps(rig.store.settings_get("config.winagent"))


def test_settings_resources_group_writes_resource_pools(rig):
    """#89:``resources`` 组**直接写 ``resource_pools``**(C-40 替代原 `PATCH /resources`),不需要重启。"""
    r = rig.client.put("/api/v1/settings/resources", headers=H(),
                       json={"pools": {"wsl": {"reserved_mb": 3072}}, "quota_mb": {"qq": 700}})
    assert r.status_code == 200 and r.json()["restart_required"] is False
    assert rig.store.pool_get("wsl")["reserved_mb"] == 3072 and rig.store.pool_get("wsl")["quota"]["qq"] == 700
    assert rig.store.pool_get("wsl")["source"] == "manual"
    bad = rig.client.put("/api/v1/settings/resources", headers=H(), json={"quota_mb": {"telegram": 1}})
    assert bad.status_code == 400 and bad.json()["error"]["reason"] == "bad_quota"


# ══════════════════════════════════════════════════════════════════ #94 webhooks CRUD
def _mk_webhook(rig, name="wh") -> tuple[str, str]:
    r = rig.client.post("/api/v1/settings/webhooks", headers=H(), json={"name": name, "url": "https://hook.example/qt"})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"], r.json()["secret"]


def test_webhook_crud_and_secret_is_one_shot(rig):
    wid, secret = _mk_webhook(rig)
    assert len(secret) > 20 and rig.agent.vault.entries[f"webhook/{wid}"].value == secret
    lst = rig.client.get("/api/v1/settings/webhooks", headers=H()).json()["data"]
    assert [w["id"] for w in lst] == [wid]
    assert lst[0]["secret_ref"] == f"vault://webhook/{wid}" and "secret" not in lst[0]   # 只回引用,不回读
    r = rig.client.patch(f"/api/v1/settings/webhooks/{wid}", headers=H(), json={"events": ["message"], "enabled": False})
    assert r.status_code == 200 and r.json()["data"]["events"] == ["message"] and r.json()["data"]["enabled"] is False
    assert rig.client.delete(f"/api/v1/settings/webhooks/{wid}", headers=H()).status_code == 200
    assert rig.client.get("/api/v1/settings/webhooks", headers=H()).json()["data"] == []
    assert f"webhook/{wid}" not in rig.agent.vault.entries


def test_webhook_reenable_clears_dead_counters(rig):
    """🔴 rulings (al):``enabled=1`` 同时清 ``consecutive_fail``/``dead_ms``,否则重新启用后下一次失败立刻又进死信。"""
    wid, _ = _mk_webhook(rig)
    rig.store.con.execute("UPDATE webhooks SET enabled=0, consecutive_fail=10, dead_ms=? WHERE id=?", (rig.clock(), wid))
    r = rig.client.patch(f"/api/v1/settings/webhooks/{wid}", headers=H(), json={"enabled": True})
    assert r.json()["data"]["enabled"] is True and r.json()["data"]["consecutive_fail"] == 0
    # 第六批 S-9:出参 `dead_ms` → `dead_at`(ISO,00 §6);判据不松 —— 出参为 null 且库列 `dead_ms` 真被清
    assert r.json()["data"]["dead_at"] is None and "dead_ms" not in r.json()["data"]
    assert rig.store.con.execute("SELECT dead_ms FROM webhooks WHERE id=?", (wid,)).fetchone()[0] is None


def test_webhook_bad_input_and_404(rig):
    assert rig.client.post("/api/v1/settings/webhooks", headers=H(), json={"name": "x"}).status_code == 400
    r = rig.client.post("/api/v1/settings/webhooks", headers=H(), json={"name": "x", "url": "ftp://n"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_url"
    assert rig.client.patch("/api/v1/settings/webhooks/nope", headers=H(), json={"enabled": True}).status_code == 404
    assert rig.client.post("/api/v1/settings/webhooks/nope/test", headers=H()).status_code == 404
    assert rig.client.get("/api/v1/settings/webhooks", headers=H(TOKEN_WRITE)).status_code == 403


async def test_webhook_test_endpoint_delivers_through_the_normal_path(rig):
    """#94 的 ``/test``:走正常的 outbox → 投递器(**不绕过签名**)。"""
    wid, _ = _mk_webhook(rig)
    rig.http.default = HttpResponse(200)
    r = rig.client.post(f"/api/v1/settings/webhooks/{wid}/test", headers=H())
    assert r.status_code == 200 and r.json()["status"] == "delivered" and r.json()["delivered"] == 1
    req = rig.http.requests[-1]
    assert req["headers"]["X-QT-Webhook-AppId"] == wid and req["headers"]["X-QT-Webhook-Signature"].startswith("v1=")
    assert json.loads(req["body"])["payload"]["code"] == "WEBHOOK_TEST"


# ══════════════════════════════════════════════════════════════════ #95 审计
def test_audit_filters_csv_and_actor_scope(rig):
    rig.client.get("/api/v1/accounts", headers=H())                     # 造几条 api 审计
    rig.client.get("/api/v1/accounts", headers=H(TOKEN_READ))
    r = rig.client.get("/api/v1/audit", headers=H(), params={"kind": "api", "limit": 5})
    assert r.status_code == 200 and all(x["kind"] == "api" for x in r.json()["data"])
    mine = rig.client.get("/api/v1/audit", headers=H(TOKEN_READ)).json()["data"]
    assert mine and {x["actor"] for x in mine} == {"app:reader"}         # 非 A 级只看自己
    admin_all = rig.client.get("/api/v1/audit", headers=H(), params={"actor": "app:reader"}).json()["data"]
    assert admin_all and {x["actor"] for x in admin_all} == {"app:reader"}
    csv = rig.client.get("/api/v1/audit", headers=H(), params={"fmt": "csv"})
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].startswith("id,ts_ms,kind,")
    assert rig.client.get("/api/v1/audit", headers=H(), params={"kind": "nope"}).status_code == 400
    assert rig.client.get("/api/v1/audit", headers=H(), params={"fmt": "xml"}).status_code == 400


def test_audit_cursor_pagination(rig):
    for _ in range(4):
        rig.client.get("/api/v1/accounts", headers=H())
    first = rig.client.get("/api/v1/audit", headers=H(), params={"limit": 2}).json()
    assert len(first["data"]) == 2 and first["next_cursor"]
    second = rig.client.get("/api/v1/audit", headers=H(), params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert {x["id"] for x in first["data"]}.isdisjoint({x["id"] for x in second["data"]})


# ══════════════════════════════════════════════════════════════════ #97~#101 运行时动作
def _qq(rig, aid="qq01", state="running"):
    rig.store.ensure_account(aid, "qq", state=state, self_uid="415011447")
    rig.store.upsert_runtime(aid, kind="napcat", desired_state="running")
    return aid


def test_webui_open_close_and_channel_guard(rig):
    """#97/#98:仅 QQ;``running`` 下开需重启容器 ⇒ 响应带 ``restart``;已是目标态 ⇒ no-op。"""
    aid = _qq(rig)
    r = rig.client.post(f"/api/v1/accounts/{aid}/webui/open", headers=H(TOKEN_WRITE), json={"minutes": 10})
    assert r.status_code == 200 and r.json()["url"].endswith(":16301/") and r.json()["changed"] is True
    assert rig.store.get_runtime(aid)["webui_published_until_ms"] > rig.clock()
    again = rig.client.post(f"/api/v1/accounts/{aid}/webui/open", headers=H(TOKEN_WRITE), json={})
    assert again.json()["changed"] is False                                   # 已开:不白重启一次容器
    assert rig.client.post(f"/api/v1/accounts/{aid}/webui/close", headers=H(TOKEN_WRITE)).json()["changed"] is True
    assert rig.store.get_runtime(aid)["webui_published_until_ms"] is None
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="300")
    bad = rig.client.post("/api/v1/accounts/qd01/webui/open", headers=H(TOKEN_WRITE), json={})
    assert bad.status_code == 409 and bad.json()["error"]["reason"] == "not_applicable"


async def test_export_identity_requires_stopped_and_makes_a_tar(rig, tmp_path):
    """#99 仅 QQ:账号须 ``stopped`` 否则 409;产物是 tar,路径进 ``jobs.result_json``。"""
    import os
    import tarfile
    aid = _qq(rig, state="running")
    assert rig.client.post(f"/api/v1/accounts/{aid}/export-identity", headers=H()).status_code == 409
    rig.store.transition(aid, "stopped")
    os.makedirs(rig.agent.runtime.data_dir(aid), exist_ok=True)
    with open(os.path.join(rig.agent.runtime.data_dir(aid), "qq_data.bin"), "wb") as f:
        f.write(b"identity")
    r = rig.client.post(f"/api/v1/accounts/{aid}/export-identity", headers=H())
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    _drain2(rig, lambda: rig.store.job_get(job_id)["state"] != "running")
    result = json.loads(rig.store.job_get(job_id)["result_json"])
    assert result["bytes"] > 0 and tarfile.is_tarfile(result["file_path"])
    rig.store.ensure_account("qd01", "qidian", state="stopped", self_uid="300")
    assert rig.client.post("/api/v1/accounts/qd01/export-identity", headers=H()).status_code == 409   # 企点不给导


def test_reconnect_adb_and_restart_stream_are_qidian_only(rig):
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    rig.store.upsert_runtime("qd01", kind="redroid", adb_serial="127.0.0.1:16001", stream_port=16501)
    r = rig.client.post("/api/v1/accounts/qd01/runtime/reconnect-adb", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["serial"] == "127.0.0.1:16001" and "adb_state" in r.json()
    r = rig.client.post("/api/v1/accounts/qd01/runtime/restart-stream", headers=H(TOKEN_WRITE))
    assert r.status_code == 200 and r.json()["stream_port"] == 16501
    assert r.json()["stream_restarted"] is False                 # 画面流没有执行体:如实回 false,不假装
    aid = _qq(rig)
    assert rig.client.post(f"/api/v1/accounts/{aid}/runtime/reconnect-adb", headers=H(TOKEN_WRITE)).status_code == 409


# ══════════════════════════════════════════════════════════════════ #57 / #66 / #103 邮件
def test_mail_fetch_and_test_endpoints(rig):
    assert rig.client.post("/api/v1/mail/fetch", headers=H(TOKEN_WRITE), json={}).status_code == 202
    assert rig.client.post("/api/v1/mail/fetch", headers=H(TOKEN_WRITE), json={"route_id": 999}).status_code == 404
    r = rig.client.post("/api/v1/mail/test", headers=H(), json={"which": "sideways"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_which"


def test_mail_templates_crud_rejects_removed_custom_profile(rig):
    """#103 + 总控裁决 (bb):``compat_profile`` 的 ``custom`` 已被 R-13 删除 ⇒ **API 层拒**,请用 ``qtrade-v1``。"""
    body = {"name": "出站-自定义", "kind": "outbound", "subject_pattern": "QTRADE {template_version} {summary}",
            "body_fields": [{"key": "text", "label": "正文", "order": 1}], "compat_profile": "qtrade-v1"}
    r = rig.client.post("/api/v1/settings/mail/templates", headers=H(), json=body)
    assert r.status_code == 201 and r.json()["data"]["compat_profile"] == "qtrade-v1"
    tid = r.json()["data"]["id"]
    bad = rig.client.post("/api/v1/settings/mail/templates", headers=H(), json={**body, "name": "x", "compat_profile": "custom"})
    assert bad.status_code == 400 and bad.json()["error"]["reason"] == "profile_custom_removed"
    lst = rig.client.get("/api/v1/settings/mail/templates", headers=H(), params={"kind": "outbound"}).json()["data"]
    assert [t["id"] for t in lst] == [tid]
    nover = rig.client.put(f"/api/v1/settings/mail/templates/{tid}", headers=H(), json={**body, "version": 1})
    assert nover.status_code == 400 and nover.json()["error"]["reason"] == "version_not_increasing"
    ok = rig.client.put(f"/api/v1/settings/mail/templates/{tid}", headers=H(), json={**body, "version": 2})
    assert ok.status_code == 200 and ok.json()["data"]["version"] == 2
    assert rig.client.delete(f"/api/v1/settings/mail/templates/{tid}", headers=H()).status_code == 200
    assert rig.client.delete(f"/api/v1/settings/mail/templates/{tid}", headers=H()).status_code == 404


def test_mail_template_in_use_cannot_be_deleted(rig):
    body = {"name": "出站-在用", "kind": "outbound", "subject_pattern": "x {summary}", "body_fields": []}
    tid = rig.client.post("/api/v1/settings/mail/templates", headers=H(), json=body).json()["data"]["id"]
    rid = rig.agent.mail.ms.route_upsert(channel="qidian", account_id=None, inbound_json={}, outbound_json={})
    rig.store.con.execute("UPDATE mail_routes SET outbound_template_id=? WHERE id=?", (tid, rid))
    r = rig.client.delete(f"/api/v1/settings/mail/templates/{tid}", headers=H())
    assert r.status_code == 409 and r.json()["error"]["reason"] == "template_in_use"


def _drain2(rig, done) -> None:
    import asyncio
    for _ in range(200):
        if done():
            return
        rig.client.portal.call(asyncio.sleep, 0.01)
    raise AssertionError("作业没在预期时间内收口")


# ══════════════════════════════════════════════════════════════════ 裁决① + §2.8.8 写入先判磁盘满
async def test_webhook_outbox_uses_events_ws_retention_hours(rig):
    """🔴 总控裁决:``events_outbox`` **两类行统一只认 `[events] ws_retention_hours`(72 h)**;
    `[retention] events_ws_hours`(24 h)**已废弃、不再参与判定** —— 48 h 的 webhook 副本应当**留着**。"""
    now = rig.clock()
    for name, age_h in (("old", 73), ("fresh", 48)):
        rig.store.insert_outbox_event(event_id=f"e-{name}", target="webhook:w", event="message", trace_id=None,
                                      account_id=None, channel=None, payload_json="{}", now_ms=now - age_h * 3600_000)
    assert rig.agent.maintenance.ws_retention_hours == 72
    rig.agent.maintenance.cleanup_once(now_ms=now)
    left = [r["event_id"] for r in rig.store.con.execute("SELECT event_id FROM events_outbox WHERE target='webhook:w'")]
    assert left == ["e-fresh"]                       # 24 h 的旧尺子下 48 h 这条会被误删


async def test_disk_full_through_bus_is_507_not_internal(rig):
    """§2.8.8 逐字:写失败**第一诊断项是磁盘满** ⇒ `DISK_FULL`(HTTP 507)、`retryable=false`、`needs_human=true`;
    **非磁盘类写失败才回落 `INTERNAL`** —— 不能被 `bus` 消费者的 `except Exception` 兜成 500 + 可重试。"""
    import sqlite3
    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    real = rig.store._ingest_one
    rig.store._ingest_one = boom
    try:
        r = rig.client.post("/api/v1/accounts/qd01/send", headers=H(TOKEN_WRITE),
                            json={"session": "qd01:415011447", "text": "盘满了", "idempotency_key": "k-507"})
        assert r.status_code == 507 and r.json()["code"] == "DISK_FULL"
        assert r.json()["error"]["retryable"] is False and r.json()["error"]["needs_human"] is True
    finally:
        rig.store._ingest_one = real

    def other(*a, **kw):
        raise sqlite3.OperationalError("no such table: nope")

    rig.store._ingest_one = other
    try:
        r = rig.client.post("/api/v1/accounts/qd01/send", headers=H(TOKEN_WRITE),
                            json={"session": "qd01:415011447", "text": "别的错", "idempotency_key": "k-500"})
        assert r.status_code == 500 and r.json()["code"] == "INTERNAL"    # 非磁盘类仍回落 INTERNAL
    finally:
        rig.store._ingest_one = real


def test_bind_retry_max_is_configurable(rig):
    """D-2:``[accounts] bind_retry_max``(05 §2.4.2.1 / docs/07)是 **agent.toml 的配置项**,不是写死的常量。"""
    from qtrade_agent.config import AccountsConfig, AgentConfig
    assert rig.agent.cfg.accounts.bind_retry_max == 12
    assert rig.agent.wechat_login._bind_retry_max == 12
    cfg = AgentConfig.from_toml_dict({"accounts": {"bind_retry_max": 3}})
    assert cfg.accounts.bind_retry_max == 3
    assert AccountsConfig().bind_retry_max == 12


# ══════════════════════════════════════════════════════════════════ §2.8.8 三条兜底路径(写失败先判磁盘满)
async def test_disk_full_in_confirm_window_is_disk_full_not_timeout(rig):
    """确认窗跑在 ``create_task`` 里、**不在消费者的兜底内**:盘满时若没人接,``job.done`` 永不完成 ⇒
    ``submit`` 只能等成 ``TIMEOUT``(**retryable=true**),正是 §2.8.8 禁止的自动重试。应回 ``DISK_FULL``。"""
    from qtrade_agent.models import CommandResult

    rig.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
    boom = rig.agent.maintenance.to_disk_full(OSError(28, "No space left on device"))

    async def ok_send(acct, cmd):
        return CommandResult(ok=True, code="SENT", trace_id=cmd.trace_id, source="ui")

    async def confirm_boom(*a, **kw):
        raise boom

    rig.agent.adapters["qidian"].send = ok_send
    rig.agent.bus._confirm_loop = confirm_boom
    r = rig.client.post("/api/v1/accounts/qd01/send", headers=H(TOKEN_WRITE),
                        json={"session": "qd01:415011447", "text": "确认窗盘满", "idempotency_key": "k-confirm-507"})
    assert r.status_code == 507, r.json()
    assert r.json()["code"] == "DISK_FULL" and r.json()["error"]["retryable"] is False


async def test_disk_full_in_a_job_body_lands_disk_full_not_internal(rig):
    """00 §11.21 的作业体(导出/清理/备份)写失败同样先判磁盘满:``jobs`` 终态与 ``job`` 事件都带 ``DISK_FULL``。"""
    boom = rig.agent.maintenance.to_disk_full(OSError(28, "No space left on device"))

    async def body():
        raise boom

    jid = rig.store.job_create(kind="diagnostics", actor="test")
    await rig.agent.spawn_job(jid, "diagnostics", body)
    row = rig.store.job_get(jid)
    assert row["state"] == "failed"
    err = json.loads(row["error_json"])
    assert err["code"] == "DISK_FULL" and err["retryable"] is False and err["needs_human"] is True
    assert err["evidence"]["free_mb"] == boom.free_mb              # §2.8.8:必须带这三个数
    ev = [e["payload"] for e in rig.store.list_events(event="job")][-1]
    assert ev["error"]["code"] == "DISK_FULL"


async def test_disk_full_in_a_workflow_step_is_needs_human(rig):
    """工作流步骤里的写失败:码 ``DISK_FULL``(不是 ``INTERNAL``)、``needs_human=true`` ⇒ run 落 ``needs_human``,
    **不是** ``failed`` 后让它自己重跑(盘满重跑只会更满)。"""
    boom = rig.agent.maintenance.to_disk_full(OSError(28, "No space left on device"))

    async def step_boom(*a, **kw):
        raise boom

    wf = rig.agent.workflows.upsert(name="disk_drill", yaml="name: disk_drill\nsteps:\n  - id: s1\n    op: sleep\n    args: { seconds: 0 }\n")
    rig.agent.workflows._run_step = step_boom
    run_id = await rig.agent.workflows.run(wf["id"], trigger="api", actor="test", wait=True)
    st = rig.agent.workflows.status(run_id)
    assert st["run"]["status"] == "needs_human"
    assert st["steps"][0]["status"] == "needs_human" and st["steps"][0]["result_code"] == "DISK_FULL"


def test_media_write_is_guarded_like_store_writes(rig, tmp_path):
    """§2.8.8 原句是「`store`/`mail`/`media` 任何写失败」—— 媒体**落盘**这一路此前没包 guard,
    `ENOSPC` 会原样冒上去被兜成 `INTERNAL`;且半截 `.part` 还继续占着已经满了的盘。"""
    import os

    from qtrade_agent.maintenance import DiskFullError

    real = open
    def boom(path, mode="r", *a, **kw):
        if str(path).endswith(".part") and "w" in mode:
            f = real(path, mode, *a, **kw)
            f.close()
            raise OSError(28, "No space left on device")
        return real(path, mode, *a, **kw)

    import builtins
    builtins.open = boom
    try:
        with pytest.raises(DiskFullError) as ei:
            rig.agent.media.put_bytes(PNG, kind="image", origin={"by": "test"})
    finally:
        builtins.open = real
    assert ei.value.code == "DISK_FULL" and ei.value.retryable is False
    tmp_dir = os.path.join(rig.agent.media.media_dir, "tmp")
    assert not os.path.exists(tmp_dir) or os.listdir(tmp_dir) == []     # 盘满时不留半截 .part
