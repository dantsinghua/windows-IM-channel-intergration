"""端到端(全假后端)· 管理线里 **TS 侧读不到** 的那几条断言。

浏览器/vitest 侧只能看 HTTP 响应,看不到进程内假件的调用记录。下面这几条的判据恰恰是
「**有没有真的往外发请求**」,所以必须在 Python 侧、拿 ``FakeWinAgent.calls`` 断。

规格出处:
- 02 §3.4 #84 `POST /system/wsl-restart`:级别 **A**;``shutdown`` 必须带 ``confirm:true``
  (用户已在控制台确认,**基线 §11.6 [NOSHUTDOWN]**)→ Agent 自 drain → 调 ``POST /wa/v1/wsl/restart``。
- 00 §11.6 [NOSHUTDOWN]:绝不自行 shutdown / 重启 WSL。

🔴 本文件由**独立验收方**编写,不复用实现方的 `tests/test_api_ext.py`。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.e2e.serve_fake_agent import TOKEN_ADMIN, TOKEN_WRITE, build


def _wa_calls(agent) -> list[tuple[str, str]]:
    """FakeWinAgent 记下的全部出站请求 ``(method, path)``(含委托给 `_base` 的非微信路径)。"""
    wechat = agent._e2e["wechat"]
    out: list[tuple[str, str]] = [(m, p) for m, p, _ in wechat.calls]
    out += [(m, p) for m, p, _h, _b in wechat._base.calls]
    return out


@pytest.fixture()
def client(tmp_path):
    agent, _cfg = build(str(tmp_path))
    api = agent.create_api()
    with TestClient(api) as c:
        yield c, agent


def _wsl_restart_calls(agent) -> list[tuple[str, str]]:
    return [(m, p) for m, p in _wa_calls(agent) if "wsl/restart" in p]


def test_wsl_restart_without_confirm_is_rejected_and_sends_nothing_to_winagent(client):
    """#84:``mode='shutdown'`` 不带 ``confirm`` ⇒ 400,且**一条请求都没发到 WinAgent**。

    只看 HTTP 400 是不够的:如果实现「先发了再回错」,整机照样被关掉一次 —— [NOSHUTDOWN] 就破了。
    """
    c, agent = client
    before = len(_wa_calls(agent))
    r = c.post("/api/v1/system/wsl-restart", json={"mode": "shutdown"},
               headers={"Authorization": f"Bearer {TOKEN_ADMIN}"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "INVALID_ARGS"
    assert body["error"]["reason"] == "confirm_required"
    assert _wsl_restart_calls(agent) == [], "被拒的请求不得向 WinAgent 发出 wsl/restart"
    # 更强的一条:整条链路上一条新的出站请求都不该有
    assert len(_wa_calls(agent)) == before, f"被拒后仍向 WinAgent 发了请求:{_wa_calls(agent)[before:]}"


def test_wsl_restart_requires_admin_and_sends_nothing_on_403(client):
    """#84 级别列 = **A**:write 级令牌带齐 ``confirm`` 也必须 403,且不向 WinAgent 发请求。"""
    c, agent = client
    before = len(_wa_calls(agent))
    r = c.post("/api/v1/system/wsl-restart", json={"mode": "shutdown", "confirm": True},
               headers={"Authorization": f"Bearer {TOKEN_WRITE}"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["reason"] == "level_insufficient"
    assert len(_wa_calls(agent)) == before, "鉴权不过就不该有任何出站请求"


def test_wsl_restart_is_audited_even_when_rejected(client):
    """#84:「受理与拒绝都写审计」—— 被挡下的整机级动作必须留痕,否则事后查不出谁点过。

    审计行形状 = 02 #95 R6-58 (ag) 的十列。
    """
    c, agent = client
    c.post("/api/v1/system/wsl-restart", json={"mode": "shutdown"},
           headers={"Authorization": f"Bearer {TOKEN_ADMIN}"})
    rows = c.get("/api/v1/audit", params={"limit": 50},
                 headers={"Authorization": f"Bearer {TOKEN_ADMIN}"}).json()["data"]
    hit = [r for r in rows if "wsl-restart" in str(r.get("action", ""))]
    assert hit, f"被拒的 wsl-restart 没有写审计;最近的 action:{[r.get('action') for r in rows[:10]]}"
    for col in ("id", "ts_ms", "kind", "transport", "actor", "action", "account_id",
                "trace_id", "result_code", "detail_json"):
        assert col in hit[0], f"审计行缺 R6-58 (ag) 定死的列:{col}"


def test_wsl_restart_happy_path_goes_through_winagent_client_only(client):
    """对照组:带齐 ``confirm:true`` 的 admin 请求**确实**经 WinAgent 客户端发出去。

    没有这条对照,上面两条「没发出去」可能只是因为这条路径压根不通(假绿)。
    02 #84:「Agent 自 drain → 调 `POST /wa/v1/wsl/restart`」—— 本机不执行任何 `wsl` 命令。
    """
    c, agent = client
    r = c.post("/api/v1/system/wsl-restart", json={"mode": "shutdown", "confirm": True},
               headers={"Authorization": f"Bearer {TOKEN_ADMIN}"})
    assert r.status_code in (200, 202), r.text
    assert _wsl_restart_calls(agent), "带 confirm 的请求应当经 WinAgent 客户端发出 wsl/restart"
    method, path = _wsl_restart_calls(agent)[0]
    assert method == "POST"
    assert path.endswith("/wa/v1/wsl/restart")
