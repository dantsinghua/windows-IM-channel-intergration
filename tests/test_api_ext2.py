"""02 §3.4 第二批补齐的端点(#8/#16/#27/#33/#34/#35/#37/#50/#52/#53/#54/#82/#83)的开发者测试。

每个端点:正例 + 鉴权负例 + 参数负例 +(danger 的)无 `confirm` 拒绝且**无副作用**。
全部后端是假件(假适配器 / 假画面流 / 假 ASR / 假 Fs / 假停机钩子),**不碰真 docker / adb / WinAgent / 不出网**。
"""
from __future__ import annotations

import json
import struct
from typing import Any, AsyncIterator, Optional

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.wechat import FakeWeChatWinAgent
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig, RuntimeConfig
from qtrade_agent.maintenance import BackupConfig, FakeDisk
from qtrade_agent.models import Command, CommandResult, Message, Session
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs as RuntimeFakeFs
from qtrade_agent.vault_client import FakeVault
from qtrade_agent.webhook import FakeHttp
from tests.conftest import Clock

TOK_A = "ext2-admin-token"
TOK_W = "ext2-write-token"
TOK_R = "ext2-read-token"
P = "/api/v1"


def H(token: str = TOK_A, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


# ══════════════════════════════════════════════════ 假执行体
class FakeFs:
    """#8 的 Fs 假件:只记路径,**不真删盘上任何东西**。"""

    def __init__(self) -> None:
        self.dirs: dict[str, float] = {}
        self.removed: list[str] = []

    def exists(self, path: str) -> bool:
        return path in self.dirs

    def du_mb(self, path: str) -> float:
        return self.dirs.get(path, 0.0)

    def rmtree(self, path: str) -> bool:
        if path not in self.dirs:
            return False
        self.dirs.pop(path)
        self.removed.append(path)
        return True


class FakeShotAdapter:
    """企点适配器的替身:``screenshot`` 回裸 ``bytes``(R6-58 (bb):适配器契约里 `data.png` 就是 bytes)。"""

    channel = "qidian"
    capabilities = frozenset({"get_state", "screenshot", "list_sessions", "read_messages", "send_text"})

    def __init__(self, png: bytes = b"\x89PNG\r\n\x1a\nFAKE-SHOT") -> None:
        self.png = png
        self.calls: list[dict[str, Any]] = []

    async def start(self, acct: Account) -> None: ...
    async def stop(self, acct: Account, *, graceful: bool) -> None: ...

    async def get_state(self, acct: Account) -> str:
        return acct.state

    async def execute(self, acct: Account, cmd: Command) -> CommandResult:
        if cmd.op != "screenshot":
            return CommandResult(ok=False, code="UNSUPPORTED", trace_id=cmd.trace_id or "", source="ui")
        self.calls.append(dict(cmd.args))
        return CommandResult(ok=True, code="OK", trace_id=cmd.trace_id or "", source="screenshot",
                             data={"png": self.png, "width": 720, "height": 1280})

    async def send(self, acct: Account, cmd: Command) -> CommandResult:
        return CommandResult(ok=False, code="UNSUPPORTED", trace_id=cmd.trace_id or "", source="ui")

    async def confirm_probe(self, acct: Account, cmd: Command) -> bool:
        return False

    async def poll(self, acct: Account, *, only_sessions: Optional[list[str]] = None) -> None: ...


class FakeStreamSession:
    def __init__(self, frames: list[tuple[int, bytes]]) -> None:
        self.meta = {"codec": "h264", "width": 360, "height": 640, "fps": 15, "seq0": 0}
        self._frames = frames
        self.controls: list[dict[str, Any]] = []
        self.closed = False

    async def frames(self) -> AsyncIterator[tuple[int, bytes]]:
        for f in self._frames:
            yield f

    async def control(self, msg: dict[str, Any]) -> None:
        self.controls.append(msg)

    async def close(self) -> None:
        self.closed = True


class FakeStreamBackend:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.injected: list[dict[str, Any]] = []
        self.sessions: list[FakeStreamSession] = []

    async def open(self, account_id: str, *, profile: str) -> FakeStreamSession:
        self.opened.append((account_id, profile))
        s = FakeStreamSession([(1234, b"\x00\x00\x00\x01NAL-1"), (1300, b"\x00\x00\x00\x01NAL-2")])
        self.sessions.append(s)
        return s

    async def inject(self, account_id: str, msg: dict[str, Any]) -> dict[str, Any]:
        self.injected.append({"account_id": account_id, **msg})
        return {"injected": True}


class FakeAsrBackend:
    def __init__(self, text: str = "你好这是转写结果") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def transcribe(self, *, path: str, mime: Optional[str], media_id: int) -> dict[str, Any]:
        self.calls.append({"path": path, "mime": mime, "media_id": media_id})
        return {"text": self.text, "confidence": 0.88}


# ══════════════════════════════════════════════════ 夹具
class Rig:
    def __init__(self, tmp_path):
        self.clock = Clock(auto_step_ms=50)
        self.wa = FakeWeChatWinAgent()
        cfg = AgentConfig(backup=BackupConfig(backup_dir=str(tmp_path / "backup")),
                          runtime=RuntimeConfig(accounts_dir=str(tmp_path / "accounts")))
        self.agent = AgentApp(cfg, db_path=str(tmp_path / "agent.db"), clock=self.clock,
                              containers=FakeContainers(), adb=FakeAdb(), vault=FakeVault(),
                              winagent_transport=self.wa, winagent_base_url="http://winagent.fake:17610",
                              winagent_token=self.wa.token, fs=RuntimeFakeFs(), wsl_total_mb=11264, boot_poll_s=0,
                              http=FakeHttp(), disk=FakeDisk(free=100_000), data_dir=str(tmp_path)).open()
        self.agent.pool.set_windows(total_mb=16384, wechat_enabled=True, known=True)
        self.agent.health.set_winagent(True, version=self.wa.version, user_agent=True)
        self.store = self.agent.store
        self.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_A)
        self.store.upsert_api_client(app_id="writer", name="可写", level="write", token=TOK_W)
        self.store.upsert_api_client(app_id="reader", name="只读", level="read", token=TOK_R)
        self.store.ensure_account("qd01", "qidian", state="running", self_uid="3007373675")
        self.store.ensure_account("qq01", "qq", state="running", self_uid="415011447")
        self.store.ensure_account("wx01", "wechat", state="running")
        self.fs = FakeFs()
        self.agent.fs = self.fs                           # #8 的 Fs 执行体(可注入)
        self.client = TestClient(self.agent.create_api(), client=("127.0.0.1", 40000))
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        self.store.close()

    def job(self, job_id: str) -> dict[str, Any]:
        r = self.client.get(f"{P}/jobs/{job_id}", headers=H())
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def wait_job(self, job_id: str, *, tries: int = 80) -> dict[str, Any]:
        for _ in range(tries):
            row = self.job(job_id)
            if row["state"] in ("succeeded", "failed", "cancelled"):
                return row
            self.client.get(f"{P}/system/version", headers=H())     # 让事件循环转一圈
        raise AssertionError(f"作业 {job_id} 没有进终态")

    def tick(self, n: int = 5) -> None:
        for _ in range(n):
            self.client.get(f"{P}/system/version", headers=H())

    def soft_delete(self, account_id: str) -> None:
        row = self.store.get_account(account_id)
        r = self.client.delete(f"{P}/accounts/{account_id}", headers=H(), params={"confirm": row["label"]})
        assert r.status_code == 200, r.text

    def add_message(self, *, account_id: str = "qd01", type: str = "text", text: Optional[str] = "hi",
                    media: Optional[list[dict[str, Any]]] = None, ts_ms: Optional[int] = None) -> str:
        msg = Message(account_id=account_id, channel=self.store.get_account(account_id)["channel"],
                      session=Session(account_id, "415011447", "private", "对端"), dir="in", type=type, text=text,
                      ts_ms=ts_ms if ts_ms is not None else self.clock(), source="qidian_db",
                      ext_msg_id=f"ext-{self.clock()}", sender_id="415011447", media=media or [])
        res = self.store.ingest(msg)
        mid = res.id if getattr(res, "id", None) else self.store.query_messages(account_id=account_id, limit=1)[0][0]["id"]
        if media:
            with self.store._tx() as c:
                c.execute("UPDATE messages SET media_json=? WHERE id=?", (json.dumps(media, ensure_ascii=False), mid))
        return mid


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


# ══════════════════════════════════════════════════ #8 purge(danger)
def test_purge_requires_admin_and_confirm_and_soft_delete(rig):
    """#8:写级令牌 403;confirm 不等于 id ⇒ 400 且**一行都不删**;没先软删 ⇒ 409。"""
    assert rig.client.post(f"{P}/accounts/qd01/purge", headers=H(TOK_W), json={"confirm": "qd01"}).status_code == 403
    r = rig.client.post(f"{P}/accounts/qd01/purge", headers=H(), json={"confirm": "qd0X"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "confirm_mismatch"
    assert rig.store.get_account("qd01") is not None and not rig.fs.removed
    r = rig.client.post(f"{P}/accounts/qd01/purge", headers=H(), json={"confirm": "qd01"})
    assert r.status_code == 409 and r.json()["error"]["reason"] == "not_soft_deleted"


def test_purge_deletes_rows_and_dir_and_keeps_tombstone(rig):
    """#8 正例:软删后 purge → 202 job;库行(messages/sessions/cursors)与数据目录都没了,``accounts`` 行留墓碑。"""
    rig.add_message(account_id="qd01", text="要被删掉的")
    rig.store.cursor_set("qd01", "qidian_rowid:415011447", 42)
    rig.fs.dirs[str(rig.agent.data_dir) + "/accounts/qd01"] = 12.5
    rig.soft_delete("qd01")
    r = rig.client.post(f"{P}/accounts/qd01/purge", headers=H(), json={"confirm": "qd01"})
    assert r.status_code == 202 and r.json()["ok"] is True
    row = rig.wait_job(r.json()["job_id"])
    assert row["state"] == "succeeded", row
    assert row["result"]["deleted"]["messages"] == 1 and row["result"]["deleted"]["cursors"] == 1
    assert row["result"]["dir_removed"] is True and rig.fs.removed
    assert rig.store.con.execute("SELECT COUNT(*) FROM messages WHERE account_id='qd01'").fetchone()[0] == 0
    assert rig.store.con.execute("SELECT COUNT(*) FROM sessions WHERE account_id='qd01'").fetchone()[0] == 0
    # id 不复用:accounts 行仍在(墓碑),device_profiles 行不动
    assert rig.store.get_account_full("qd01") is not None


def test_purge_job_not_cancellable_after_irreversible(rig):
    """#108:``account_purge`` 进入不可逆阶段后取消 ⇒ ``409 NOT_CANCELLABLE``(不是「终态」那条 reason)。"""
    rig.soft_delete("qd01")
    job_id = rig.client.post(f"{P}/accounts/qd01/purge", headers=H(), json={"confirm": "qd01"}).json()["job_id"]
    rig.wait_job(job_id)
    with rig.store._tx() as c:      # 造一个「还在跑 + 已不可逆」的行,单测里不好卡到真实那一瞬
        c.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job_id,))
    r = rig.client.post(f"{P}/jobs/{job_id}/cancel", headers=H())
    assert r.status_code == 409 and r.json()["code"] == "NOT_CANCELLABLE"
    assert r.json()["error"]["reason"] == "irreversible"


# ══════════════════════════════════════════════════ #16 logout
def test_logout_wechat_goes_through_winagent(rig):
    """#16 正例(微信):经 WinAgent ``POST /wa/v1/wechat/logout``(P-31),回 202。"""
    before = rig.wa.logout_calls
    r = rig.client.post(f"{P}/accounts/wx01/logout", headers=H(TOK_W))
    assert r.status_code == 202 and r.json()["via"] == "winagent", r.text
    assert rig.wa.logout_calls == before + 1
    assert any(a["action"] == "account.logout" for a in rig.store.list_audit())


def test_logout_qq_not_applicable_and_qidian_reports_missing_executor(rig):
    """QQ 无「登出」概念 ⇒ 409 NOT_APPLICABLE;企点本期无执行体 ⇒ 503 且 reason 写明,**不假装登出过**。"""
    r = rig.client.post(f"{P}/accounts/qq01/logout", headers=H(TOK_W))
    assert r.status_code == 409 and r.json()["error"]["reason"] == "channel_no_logout"
    r = rig.client.post(f"{P}/accounts/qd01/logout", headers=H(TOK_W))
    assert r.status_code == 503 and r.json()["error"]["reason"] == "logout_backend_missing"


def test_logout_auth_and_unknown_account(rig):
    assert rig.client.post(f"{P}/accounts/wx01/logout", headers=H(TOK_R)).status_code == 403
    assert rig.client.post(f"{P}/accounts/wx01/logout").status_code == 401
    assert rig.client.post(f"{P}/accounts/nope/logout", headers=H(TOK_W)).status_code == 404


# ══════════════════════════════════════════════════ #27 / #27b 单会话
def _session_id(rig) -> str:
    rig.add_message(account_id="qd01")
    return rig.store.list_sessions(account_id="qd01")[0]["id"]


def test_patch_session_updates_and_returns_session(rig):
    sid = _session_id(rig)
    r = rig.client.patch(f"{P}/accounts/qd01/sessions/{sid}", headers=H(TOK_W),
                         json={"muted": True, "retention_days": 7, "capture_text": None})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == sid and data["muted"] is True and data["retention_days"] == 7
    assert rig.client.get(f"{P}/accounts/qd01/sessions/{sid}", headers=H(TOK_R)).json()["data"]["muted"] is True


def test_patch_session_rejects_bad_fields(rig):
    sid = _session_id(rig)
    r = rig.client.patch(f"{P}/accounts/qd01/sessions/{sid}", headers=H(TOK_W), json={"name": "改名"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_field"
    r = rig.client.patch(f"{P}/accounts/qd01/sessions/{sid}", headers=H(TOK_W), json={"retention_days": 31})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_retention_days"
    assert rig.client.patch(f"{P}/accounts/qd01/sessions/{sid}", headers=H(TOK_R), json={"muted": True}).status_code == 403
    assert rig.client.patch(f"{P}/accounts/qd01/sessions/nope", headers=H(TOK_W), json={"muted": True}).status_code == 404


# ══════════════════════════════════════════════════ #33 截图
def test_screenshot_returns_png_bytes_and_media_ref(rig):
    """#33 正例:回**二进制图**(不是 JSON),并按 §2.8.2 落 media、把 media_id/sha256 放响应头。"""
    ad = FakeShotAdapter()
    rig.agent.adapters["qidian"] = ad
    r = rig.client.get(f"{P}/accounts/qd01/screenshot", headers=H(TOK_R), params={"region": "0,0,100,200"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png") and r.content == ad.png
    assert r.headers.get("X-QT-Media-Id") and r.headers.get("X-QT-Sha256")
    assert ad.calls[0]["region"] == {"x": 0, "y": 0, "w": 100, "h": 200}
    # R6-58 (cz):落库的 data 里不许有裸 bytes —— 经 json_safe 换成占位
    row = rig.store.get_command_result(r.headers["X-QT-Trace-Id"])
    assert json.loads(row["data_json"])["png"]["__binary__"]


def test_screenshot_qq_not_applicable_and_bad_params(rig):
    r = rig.client.get(f"{P}/accounts/qq01/screenshot", headers=H(TOK_R))
    assert r.status_code == 409 and r.json()["error"]["reason"] == "not_applicable"
    r = rig.client.get(f"{P}/accounts/qd01/screenshot", headers=H(TOK_R), params={"region": "0,0,100"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_region"
    r = rig.client.get(f"{P}/accounts/qd01/screenshot", headers=H(TOK_R), params={"format": "webp"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_format"
    assert rig.client.get(f"{P}/accounts/qd01/screenshot").status_code == 401


def test_screenshot_without_executor_reports_unsupported(rig):
    """缺执行体(真企点适配器本期没接截图)⇒ 如实回 `UNSUPPORTED`,不返回空图。"""
    r = rig.client.get(f"{P}/accounts/qd01/screenshot", headers=H(TOK_R))
    assert r.status_code == 409 and r.json()["code"] == "UNSUPPORTED"


# ══════════════════════════════════════════════════ #34 画面流(WS)
def test_stream_ws_auth_and_param_negatives(rig):
    """无令牌 ⇒ 4401;profile 非法 ⇒ 4400;QQ/微信通道 ⇒ 4409 —— 都必须是 **accept 之后**的关闭码。"""
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4401
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=huge") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4400
    with rig.client.websocket_connect(f"{P}/accounts/qq01/stream?token={TOK_R}") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4409


def test_stream_ws_without_backend_closes_4503(rig):
    """🔴 本期没有 scrcpy-server:如实关 4503,**一帧都不伪造**。"""
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus") as ws:
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
    assert ei.value.code == 4503


def test_stream_ws_with_fake_backend_frames_and_input(rig):
    """假后端下跑通「订阅 → 首帧 meta → 收到帧(8 字节 PTS + NAL)→ 回注一次输入」。"""
    be = FakeStreamBackend()
    rig.agent.stream_backend = be
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus") as ws:
        meta = ws.receive_json()
        assert meta["codec"] == "h264" and meta["profile"] == "focus"
        frame = ws.receive_bytes()
        assert struct.unpack(">Q", frame[:8])[0] == 1234 and frame[8:] == b"\x00\x00\x00\x01NAL-1"
        ws.send_json({"type": "touch", "action": "down", "x": 10, "y": 20})
        for _ in range(20):                       # 等服务端把控制帧收下
            if be.sessions[0].controls:
                break
            rig.tick(1)
    assert be.opened == [("qd01", "focus")]
    assert be.sessions[0].controls[0]["type"] == "touch"
    assert any(a["kind"] == "stream_input" and a["action"] == "stream.touch" for a in rig.store.list_audit())


def test_stream_ws_second_focus_is_rejected(rig):
    """02 #34:同账号只允许 1 个 ``focus*`` 连接,后来者关 4410。"""
    rig.agent.stream_backend = FakeStreamBackend()
    with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus") as ws1:
        ws1.receive_json()
        with rig.client.websocket_connect(f"{P}/accounts/qd01/stream?token={TOK_R}&profile=focus15") as ws2:
            with pytest.raises(WebSocketDisconnect) as ei:
                ws2.receive_json()
        assert ei.value.code == 4410


# ══════════════════════════════════════════════════ #35 REST 输入注入
def test_stream_input_without_backend_is_503(rig):
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W), json={"type": "tap", "x": 1, "y": 2})
    assert r.status_code == 503 and r.json()["error"]["reason"] == "stream_backend_missing"


def test_stream_input_happy_path_and_text_is_masked_in_audit(rig):
    """正例:注入到假后端;``text`` 在审计里**只留 sha8:len**(00 §8.1 R-06:登录密码不许落审计)。"""
    be = FakeStreamBackend()
    rig.agent.stream_backend = be
    assert rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W),
                           json={"type": "tap", "x": 5, "y": 6}).status_code == 200
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W),
                        json={"type": "text", "text": "my-secret-password"})
    assert r.status_code == 200 and be.injected[-1]["text"] == "my-secret-password"
    rows = [a for a in rig.store.list_audit() if a["kind"] == "stream_input" and a["action"] == "stream.text"]
    assert rows and "my-secret-password" not in (rows[-1]["detail_json"] or "")
    assert json.loads(rows[-1]["detail_json"])["text"].endswith(":18")     # sha8:len,只有 8+1+2 位


def test_stream_input_negatives(rig):
    rig.agent.stream_backend = FakeStreamBackend()
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W), json={"type": "fly"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_type"
    r = rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_W), json={"type": "tap"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_coords"
    assert rig.client.post(f"{P}/accounts/qd01/stream/input", headers=H(TOK_R),
                           json={"type": "tap", "x": 1, "y": 2}).status_code == 403


# ══════════════════════════════════════════════════ #37 广播汇总
def test_broadcast_summary_after_36(rig):
    rig.agent.adapters["qidian"] = FakeShotAdapter()
    r = rig.client.post(f"{P}/broadcast/commands", headers=H(TOK_W),
                        json={"account_ids": ["qd01"], "op": "screenshot", "args": {}})
    assert r.status_code == 200, r.text
    bid = r.json()["broadcast_id"]
    g = rig.client.get(f"{P}/broadcast/{bid}", headers=H(TOK_R))
    assert g.status_code == 200 and g.json()["counts"] == {"total": 1, "ok": 1, "failed": 0}
    assert g.json()["op"] == "screenshot" and "qd01" in g.json()["results"]
    assert rig.client.get(f"{P}/broadcast/nope", headers=H(TOK_R)).status_code == 404
    assert rig.client.get(f"{P}/broadcast/{bid}").status_code == 401


# ══════════════════════════════════════════════════ #50 媒体字节流
def _put_media(rig, data: bytes = b"\xff\xd8\xffJPEGBODY") -> dict[str, Any]:
    return rig.agent.media.put_bytes(data, kind="image", origin={"kind": "test"})


def test_media_bytes_happy_path_and_negatives(rig):
    out = _put_media(rig)
    mid = rig.add_message(media=[{"media_id": out["media_id"], "kind": "image", "state": "ready"}], type="image", text=None)
    r = rig.client.get(f"{P}/messages/{mid}/media/0", headers=H(TOK_R))
    assert r.status_code == 200 and r.content == b"\xff\xd8\xffJPEGBODY"
    assert r.headers["X-QT-Media-Id"] == str(out["media_id"])
    assert rig.client.get(f"{P}/messages/{mid}/media/3", headers=H(TOK_R)).status_code == 404
    assert rig.client.get(f"{P}/messages/nope/media/0", headers=H(TOK_R)).status_code == 404
    assert rig.client.get(f"{P}/messages/{mid}/media/0").status_code == 401


def test_media_expired_is_410(rig):
    """E-10:按 ``media_days`` 到期删文件后 ⇒ ``410``(消息行还在,图取不到)。"""
    out = _put_media(rig)
    mid = rig.add_message(media=[{"media_id": out["media_id"], "kind": "image"}], type="image", text=None)
    with rig.store._tx() as c:
        c.execute("UPDATE media SET status='expired' WHERE id=?", (out["media_id"],))
    r = rig.client.get(f"{P}/messages/{mid}/media/0", headers=H(TOK_R))
    assert r.status_code == 410 and r.json()["error"]["reason"] == "expired"


# ══════════════════════════════════════════════════ #52 导出产物
def _export_job(rig) -> str:
    rig.add_message(text="导出我")
    r = rig.client.post(f"{P}/messages/export", headers=H(), json={"fmt": "jsonl", "filter": {}})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert rig.wait_job(job_id)["state"] == "succeeded"
    return job_id


def test_exports_status_and_download(rig):
    job_id = _export_job(rig)
    r = rig.client.get(f"{P}/exports/{job_id}", headers=H())
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    assert r.json()["rows"] == 1 and r.json()["download_url"].endswith(f"/exports/{job_id}/file")
    f = rig.client.get(f"{P}/exports/{job_id}/file", headers=H())
    assert f.status_code == 200 and "导出我".encode() in f.content
    assert "attachment" in f.headers["content-disposition"]


def test_exports_only_own_job_and_no_path_escape(rig):
    """只许取本人作业的产物;``file_path`` 越出 ``exports/`` 一律 403(路径穿越防护)。"""
    job_id = _export_job(rig)
    assert rig.client.get(f"{P}/exports/{job_id}", headers=H(TOK_R)).status_code == 403     # reader 不是发起人
    assert rig.client.get(f"{P}/exports/nope", headers=H()).status_code == 404
    with rig.store._tx() as c:
        c.execute("UPDATE jobs SET result_json=? WHERE job_id=?",
                  (json.dumps({"file_path": "/etc/passwd", "rows": 1, "bytes": 1}), job_id))
    r = rig.client.get(f"{P}/exports/{job_id}/file", headers=H())
    assert r.status_code == 403 and r.json()["error"]["reason"] == "path_escape"


# ══════════════════════════════════════════════════ #53 语音转文字
def _voice_message(rig) -> tuple[str, dict[str, Any]]:
    out = rig.agent.media.put_bytes(b"RIFFfake-wav", kind="voice", origin={"kind": "test"})
    mid = rig.add_message(type="voice", text=None, media=[{"media_id": out["media_id"], "kind": "voice"}])
    return mid, out


def test_asr_without_backend_is_503(rig):
    mid, _ = _voice_message(rig)
    r = rig.client.post(f"{P}/messages/{mid}/asr", headers=H(TOK_W))
    assert r.status_code == 503 and r.json()["error"]["reason"] == "asr_backend_missing"


def test_asr_happy_path_writes_back(rig):
    """正例:202 {trace_id};转写完回写 ``asr_text``/``asr_state='done'``,原消息无文本 ⇒ ``text_source='asr'`` 并填 ``text``。"""
    be = FakeAsrBackend()
    rig.agent.asr_backend = be
    mid, media = _voice_message(rig)
    r = rig.client.post(f"{P}/messages/{mid}/asr", headers=H(TOK_W))
    assert r.status_code == 202 and r.json()["trace_id"]
    for _ in range(40):
        row = rig.store.get_message_full(mid)
        if row["asr_state"] in ("done", "failed"):
            break
        rig.tick(1)
    row = rig.store.get_message_full(mid)
    assert row["asr_state"] == "done" and row["asr_text"] == be.text
    assert row["text"] == be.text and row["text_source"] == "asr"
    assert be.calls[0]["media_id"] == media["media_id"]


def test_asr_negatives(rig):
    rig.agent.asr_backend = FakeAsrBackend()
    text_mid = rig.add_message(type="text", text="不是语音")
    r = rig.client.post(f"{P}/messages/{text_mid}/asr", headers=H(TOK_W))
    assert r.status_code == 409 and r.json()["error"]["reason"] == "not_voice"
    mid, _ = _voice_message(rig)
    assert rig.client.post(f"{P}/messages/{mid}/asr", headers=H(TOK_R)).status_code == 403
    assert rig.client.post(f"{P}/messages/nope/asr", headers=H(TOK_W)).status_code == 404


# ══════════════════════════════════════════════════ #54 messages/purge(danger)
def test_messages_purge_without_confirm_has_no_side_effect(rig):
    rig.add_message(text="不许删")
    r = rig.client.post(f"{P}/messages/purge", headers=H(), json={"account_id": "qd01", "mode": "all"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "confirm_required"
    assert rig.store.count_messages("qd01") == 1
    assert rig.client.post(f"{P}/messages/purge", headers=H(TOK_W),
                           json={"account_id": "qd01", "mode": "all", "confirm": True}).status_code == 403
    r = rig.client.post(f"{P}/messages/purge", headers=H(), json={"mode": "nope", "confirm": True})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_mode"
    assert rig.store.count_messages("qd01") == 1


def test_messages_purge_all_and_text_only(rig):
    rig.add_message(text="删掉我")
    r = rig.client.post(f"{P}/messages/purge", headers=H(), json={"account_id": "qd01", "mode": "all", "confirm": True})
    assert r.status_code == 202
    assert rig.wait_job(r.json()["job_id"])["result"]["rows"] == 1
    assert rig.store.count_messages("qd01") == 0
    mid = rig.add_message(text="只清正文")
    r = rig.client.post(f"{P}/messages/purge", headers=H(),
                        json={"account_id": "qd01", "mode": "text_only", "confirm": True})
    assert rig.wait_job(r.json()["job_id"])["state"] == "succeeded"
    assert rig.store.count_messages("qd01") == 1                     # 行还在
    assert rig.store.get_message_full(mid)["text"] is None           # 正文没了


# ══════════════════════════════════════════════════ #82 drain / #83 shutdown
def test_drain_stops_new_commands_and_keeps_desired_state(rig):
    """#82:``{drained:true}``;此后指令类端点一律 503 draining;``desired_state`` 保留(升级后要靠它恢复)。"""
    rig.agent.adapters["qidian"] = FakeShotAdapter()
    rig.store.transition("qd01", "running", desired_state="running")   # transition 才会建 account_runtime 行
    assert rig.client.post(f"{P}/system/drain", headers=H(TOK_W), json={}).status_code == 403
    r = rig.client.post(f"{P}/system/drain", headers=H(), json={"timeout_s": 1})
    assert r.status_code == 200 and r.json()["drained"] is True and r.json()["inflight"] == 0
    assert rig.store.get_account_full("qd01")["desired_state"] == "running"
    c = rig.client.post(f"{P}/accounts/qd01/commands", headers=H(TOK_W),
                        json={"op": "screenshot", "args": {}})
    assert c.status_code == 503 and c.json()["error"]["reason"] == "draining"
    assert rig.client.get(f"{P}/accounts", headers=H(TOK_R)).status_code == 200      # 只读照常


def test_drain_bad_timeout(rig):
    r = rig.client.post(f"{P}/system/drain", headers=H(), json={"timeout_s": 0})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "bad_timeout"
    assert not getattr(rig.agent, "drained", False)


def test_shutdown_requires_confirm_and_calls_hook_once(rig):
    """#83:无 ``confirm`` ⇒ 400 且钩子**一次都没调**;带 confirm ⇒ 202 且钩子被调一次(测试里不真退进程)。"""
    calls: list[int] = []

    async def hook() -> None:
        calls.append(1)

    rig.agent.shutdown_hook = hook
    r = rig.client.post(f"{P}/system/shutdown", headers=H(), json={})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "confirm_required" and not calls
    assert rig.client.post(f"{P}/system/shutdown", headers=H(TOK_W), json={"confirm": True}).status_code == 403
    assert not calls
    r = rig.client.post(f"{P}/system/shutdown", headers=H(), json={"confirm": True})
    assert r.status_code == 202 and r.json()["stopping"] is True
    for _ in range(20):
        if calls:
            break
        rig.tick(1)
    assert calls == [1]
