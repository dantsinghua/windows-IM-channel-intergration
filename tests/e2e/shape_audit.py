"""端点形状对账:同一份「控制台真实调用清单」分别打真 Agent 与 console/mock,落 JSON 供逐字段比对。

用法::

    python tests/e2e/shape_audit.py --base http://127.0.0.1:37851 --token e2e-admin-token --out /tmp/real.json --kind both
    python tests/e2e/shape_audit.py --base http://127.0.0.1:37852 --token mock          --out /tmp/mock.json --kind both

清单逐条抄自 ``console/src/api/client.ts``(控制台唯一的 URL 拼装处),``src`` 列 = 客户端方法名。

🔴 第二轮(独立验收复测)改动:
- 清单从 49 条扩到覆盖 client.ts 的**全部 JSON 端点**,含后端第二轮新补的 12 个(#24/#36/#51/#74/#75/#78/#80/#84/#85/#86/#87/#90~#93)
  与两个兄弟端点(#79b/#85b);二进制端点(#33 截图、#55 媒体)不在此表(非 JSON,02 §3.4 通用段例外②)。
- 修正三处编号笔误:`/system/metrics` 是 **#77** 不是 #70、`/system/notice` 是 **#86** 不是 #75、
  `/system/docker-proxy` 是 **#85** 不是 #102。
- `#51` 入参改成 02 #51 逐字的 ``{fmt, with_media, filter}``(原先写的 ``format``/``include_media`` 在 docs 里不存在)。
- 新增 ``CALLS_AFTER`` 阶段:用前一阶段真实拿到的 ``job_id`` / ``run_id`` / ``app_id`` 回填占位符,
  这样 #107 / #79 / #92 / #93 这些「要先有个 id」的端点也能进对账。
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# (id, 客户端方法, method, path, query, body)
CALLS_READ = [
    ("#1 accounts.list", "accountsApi.list", "GET", "/accounts", {}, None),
    ("#3 accounts.get", "accountsApi.get", "GET", "/accounts/{ACC}", {}, None),
    ("#15 accounts.prompt", "accountsApi.prompt", "GET", "/accounts/{ACC}/prompt", {}, None),
    ("#19 accounts.state", "(stores/accounts 轮询)", "GET", "/accounts/{ACC}/state", {}, None),
    ("#20 accounts.capabilities", "accountsApi.capabilities", "GET", "/accounts/{ACC}/capabilities", {}, None),
    ("#21 capabilities", "commandsApi.capabilities", "GET", "/capabilities", {}, None),
    ("#24 deviceProfiles", "commandsApi.deviceProfiles", "GET", "/device-profiles/templates", {}, None),
    ("#26 sessions", "messagesApi.sessions", "GET", "/sessions", {}, None),
    ("#48 messages", "messagesApi.list", "GET", "/messages", {"limit": 5}, None),
    ("#69 resources", "resourcesApi.get", "GET", "/resources", {}, None),
    ("#77 metrics", "resourcesApi.metrics", "GET", "/system/metrics", {"snapshot": 1}, None),
    ("#72 health", "systemApi.health", "GET", "/system/health", {}, None),
    ("#73 version", "systemApi.version", "GET", "/system/version", {}, None),
    ("#74 env", "systemApi.env", "GET", "/system/env", {}, None),
    ("#76 probes result", "systemApi.probes", "GET", "/system/probes", {}, None),
    ("#76b probes observed", "systemApi.observedProbes", "GET", "/system/probes", {"kind": "observed"}, None),
    ("#79b selftest 最近一轮", "systemApi.selftestResult", "GET", "/system/selftest", {}, None),
    ("#85b docker-proxy 读", "(P-ENV 开关当前态)", "GET", "/system/docker-proxy", {}, None),
    ("#86 notice", "systemApi.notice", "GET", "/system/notice", {}, None),
    ("#102 public-endpoint", "systemApi.publicEndpoint", "GET", "/system/public-endpoint", {}, None),
    ("#88 settings/mail", "settingsApi.get('mail')", "GET", "/settings/mail", {}, None),
    ("#88 settings/api", "settingsApi.get('api')", "GET", "/settings/api", {}, None),
    ("#88 settings/retention", "settingsApi.get('retention')", "GET", "/settings/retention", {}, None),
    ("#88 settings/resources", "settingsApi.get('resources')", "GET", "/settings/resources", {}, None),
    ("#88 settings/runtime", "settingsApi.get('runtime')", "GET", "/settings/runtime", {}, None),
    ("#88 settings/pool", "settingsApi.get('pool')", "GET", "/settings/pool", {}, None),
    ("#88 settings/events", "settingsApi.get('events')", "GET", "/settings/events", {}, None),
    ("#88 settings/log", "settingsApi.get('log')", "GET", "/settings/log", {}, None),
    ("#90 api-clients", "settingsApi.apiClients", "GET", "/settings/api-clients", {}, None),
    ("#94 settings/webhooks", "settingsApi.webhooks", "GET", "/settings/webhooks", {}, None),
    ("#105 settings/mail/routes", "settingsApi.mailRoutes", "GET", "/settings/mail/routes", {}, None),
    ("#103 settings/mail/templates", "settingsApi.mailTemplates", "GET", "/settings/mail/templates", {}, None),
    ("#95 audit", "auditApi.list", "GET", "/audit", {"limit": 5}, None),
    ("#56 mail/status", "mailApi.status", "GET", "/mail/status", {}, None),
    ("#57 mail/inbox", "mailApi.inbox", "GET", "/mail/inbox", {}, None),
    ("#58 mail/outbox", "mailApi.outbox", "GET", "/mail/outbox", {}, None),
    ("#66 mail/cleanup/log", "mailApi.cleanupLog", "GET", "/mail/cleanup/log", {}, None),
    ("mail/hmac-keys 读", "mailApi.hmacKeys", "GET", "/mail/hmac-keys", {}, None),
    ("#68b pending-confirms", "mailApi.pendingConfirms", "GET", "/mail/pending-confirms", {}, None),
    ("#41 workflows", "workflowsApi.list", "GET", "/workflows", {}, None),
    ("#31 command by trace(不存在)", "commandsApi.get", "GET", "/accounts/{ACC}/commands/01ZZZZZZZZZZZZZZZZZZZZZZZZ", {}, None),
    ("未知路由(00 §10 信封)", "—", "GET", "/no-such-endpoint", {}, None),
]

CALLS_WRITE = [
    ("#28 commands send_text", "commandsApi.run", "POST", "/accounts/{ACC}/commands", {},
     {"op": "send_text", "args": {"session": "415011447", "text": "shape-audit"}, "idempotency_key": "e2e-shape-cmd-1"}),
    ("#75 probe full", "systemApi.probe", "POST", "/system/probe", {}, {"mode": "full", "targets": ["example.com:443"], "trigger": "manual"}),
    ("#75 probe sample", "systemApi.probeSample", "POST", "/system/probe", {}, {"mode": "sample", "duration_s": 1}),
    ("#78 selftest run", "systemApi.selftestRun", "POST", "/system/selftest", {}, None),
    ("#80 diagnostics", "systemApi.diagnostics", "POST", "/system/diagnostics", {}, {"with_screenshots": False}),
    ("#109 cleanup/run", "resourcesApi.cleanupRun", "POST", "/system/cleanup/run", {}, None),
    ("#70 resources/precheck", "resourcesApi.precheck", "POST", "/resources/precheck", {}, {"channel": "qidian"}),
    ("#25 resources/calibrate", "resourcesApi.calibrate", "POST", "/resources/calibrate", {}, {"apply": False}),
    ("#51 messages/export", "messagesApi.export", "POST", "/messages/export", {},
     {"filter": {"limit": 10}, "fmt": "jsonl", "with_media": "none"}),
    ("#51 messages/export eml(本期 400)", "messagesApi.export", "POST", "/messages/export", {},
     {"filter": {}, "fmt": "eml", "with_media": "none"}),
    ("#36 broadcast", "commandsApi.broadcast", "POST", "/broadcast/commands", {},
     {"account_ids": ["{ACC}"], "op": "get_state", "args": {}, "idempotency_key": "e2e-shape-bc-1"}),
    ("#59 mail/test", "mailApi.test", "POST", "/mail/test", {}, {"which": "inbound", "route_id": None}),
    ("#67 hmac-keys 建", "mailApi.createHmacKey", "POST", "/mail/hmac-keys", {}, {"sender": "ops@corp", "short_name": "e2eops"}),
    ("#104 模板预览", "settingsApi.previewMailTemplate", "POST", "/mail/templates/default/preview", {}, {"sample_message_id": "m1"}),
    ("#91 api-clients 建", "settingsApi.createApiClient", "POST", "/settings/api-clients", {},
     {"name": "e2e-shape", "level": "read", "auth_kind": "bearer"}),
    ("#85 docker-proxy 写", "systemApi.dockerProxy", "POST", "/system/docker-proxy", {}, {"enable": False}),
    ("#76b settings/probe 采纳", "systemApi.adoptProbeTargets", "PUT", "/settings/probe", {}, {"observed_ids": []}),
    ("#84 wsl-restart 无 confirm(必拒)", "systemApi.wslRestart", "POST", "/system/wsl-restart", {}, {"mode": "shutdown"}),
    ("#87 notice/ack(版本不符)", "systemApi.noticeAck", "POST", "/system/notice/ack", {}, {"notice_version": "not-a-version"}),
    ("#22 账号级设置", "accountsApi.patchSettings", "PATCH", "/accounts/{ACC}/settings", {}, {"capture_text": True}),
]

# 用前面阶段真实拿到的 id 回填:{JOB} / {RUN} / {APPID}
CALLS_AFTER = [
    ("#107 jobs/{id}", "jobsApi.get", "GET", "/jobs/{JOB}", {}, None),
    ("#79 selftest/{run_id}", "systemApi.selftestResult(run)", "GET", "/system/selftest/{RUN}", {}, None),
    ("#92 api-clients rotate", "settingsApi.rotateApiClient", "POST", "/settings/api-clients/{APPID}/rotate", {}, {"grace_minutes": 10}),
    ("#93 api-clients revoke", "settingsApi.revokeApiClient", "DELETE", "/settings/api-clients/{APPID}", {}, None),
]


def call(base: str, token: str, method: str, path: str, query: dict, body, *, extra_headers: dict | None = None):
    url = base + "/api/v1" + path
    q = {k: v for k, v in (query or {}).items() if v is not None}
    if q:
        url += "?" + urllib.parse.urlencode(q)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            status, headers = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status, headers = e.code, dict(e.headers)
    except Exception as e:                                    # noqa: BLE001
        return {"status": None, "error": repr(e), "body": None, "headers": {}}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {"__raw__": raw[:400]}
    return {"status": status, "headers": headers, "body": parsed}


def _dig(body, key):
    """在信封顶层或 ``data`` 里找一个 id 键。"""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get(key), str):
        return body[key]
    d = body.get("data")
    if isinstance(d, dict) and isinstance(d.get(key), str):
        return d[key]
    return None


def run_batch(calls, base, token, account, out, captured):
    for cid, src, method, path, query, body in calls:
        p = path.replace("{ACC}", account)
        for k, v in captured.items():
            p = p.replace("{" + k + "}", v)
        if re.search(r"\{[A-Z]+\}", p):
            out[cid] = {"status": None, "error": f"占位符未回填:{p}", "body": None, "headers": {}, "src": src,
                        "request": {"method": method, "path": p, "query": query, "body": body}}
            print(f"{'skip':>5}  {method:6} {p}")
            continue
        b = json.loads(json.dumps(body).replace("{ACC}", account)) if body is not None else None
        res = call(base, token, method, p, query, b)
        res["src"] = src
        res["request"] = {"method": method, "path": p, "query": query, "body": b}
        out[cid] = res
        # 🔴 捕获要**按端点限定**:`run_id` 在 #75(探测轮)与 #78(自检轮)里是两种东西,
        #    不限定就会拿探测的 run_id 去查自检,查出 404 而误判成「端点缺失」。
        for key, slot, want in (("job_id", "JOB", "#109"), ("run_id", "RUN", "#78"), ("app_id", "APPID", "#91")):
            if not cid.startswith(want):
                continue
            v = _dig(res.get("body"), key)
            if v:
                captured[slot] = v
        print(f"{res['status']!s:>5}  {method:6} {p}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--account", default="qd01")
    ap.add_argument("--kind", default="read", choices=["read", "write", "both"])
    args = ap.parse_args()
    out: dict = {}
    captured: dict[str, str] = {}
    if args.kind in ("read", "both"):
        run_batch(CALLS_READ, args.base, args.token, args.account, out, captured)
    if args.kind in ("write", "both"):
        run_batch(CALLS_WRITE, args.base, args.token, args.account, out, captured)
        run_batch(CALLS_AFTER, args.base, args.token, args.account, out, captured)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
