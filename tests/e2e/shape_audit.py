"""端点形状对账:同一份「控制台真实调用清单」分别打真 Agent 与 console/mock,落 JSON 供逐字段比对。

用法::

    python tests/e2e/shape_audit.py --base http://127.0.0.1:37821 --token e2e-admin-token --out /tmp/real.json --kind read
    python tests/e2e/shape_audit.py --base http://127.0.0.1:37822 --token mock         --out /tmp/mock.json --kind read

清单逐条抄自 console/src/api/client.ts(控制台唯一的 URL 拼装处),`src` 列 = 客户端方法名。
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

# (id, 客户端方法, method, path, query, body)
CALLS_READ = [
    ("#1 accounts.list", "accountsApi.list", "GET", "/accounts", {}, None),
    ("#3 accounts.get", "accountsApi.get", "GET", "/accounts/{ACC}", {}, None),
    ("#15 accounts.prompt", "accountsApi.prompt", "GET", "/accounts/{ACC}/prompt", {}, None),
    ("#20 accounts.capabilities", "accountsApi.capabilities", "GET", "/accounts/{ACC}/capabilities", {}, None),
    ("#21 capabilities", "commandsApi.capabilities", "GET", "/capabilities", {}, None),
    ("#24 deviceProfiles", "commandsApi.deviceProfiles", "GET", "/device-profiles/templates", {}, None),
    ("#26 sessions", "messagesApi.sessions", "GET", "/sessions", {}, None),
    ("#48 messages", "messagesApi.list", "GET", "/messages", {"limit": 5}, None),
    ("#69 resources", "resourcesApi.get", "GET", "/resources", {}, None),
    ("#70 metrics", "resourcesApi.metrics", "GET", "/system/metrics", {"snapshot": 1}, None),
    ("#72 health", "systemApi.health", "GET", "/system/health", {}, None),
    ("#73 version", "systemApi.version", "GET", "/system/version", {}, None),
    ("#74 env", "systemApi.env", "GET", "/system/env", {}, None),
    ("#75 notice", "systemApi.notice", "GET", "/system/notice", {}, None),
    ("#76 probes", "systemApi.probes", "GET", "/system/probes", {}, None),
    ("#76b probes observed", "systemApi.probes(observed)", "GET", "/system/probes", {"kind": "observed"}, None),
    ("#79b selftest 最近一轮", "systemApi.selftestResult", "GET", "/system/selftest", {}, None),
    ("#88 settings/mail", "settingsApi.get('mail')", "GET", "/settings/mail", {}, None),
    ("settings/api", "settingsApi.get('api')", "GET", "/settings/api", {}, None),
    ("settings/retention", "settingsApi.get('retention')", "GET", "/settings/retention", {}, None),
    ("settings/resources", "settingsApi.get('resources')", "GET", "/settings/resources", {}, None),
    ("#90 api-clients", "settingsApi.apiClients", "GET", "/settings/api-clients", {}, None),
    ("settings/webhooks", "settingsApi.webhooks", "GET", "/settings/webhooks", {}, None),
    ("settings/compliance", "settingsApi.compliance", "GET", "/settings/compliance", {}, None),
    ("settings/mail/routes", "settingsApi.mailRoutes", "GET", "/settings/mail/routes", {}, None),
    ("settings/mail/templates", "settingsApi.mailTemplates", "GET", "/settings/mail/templates", {}, None),
    ("#95 audit", "auditApi.list", "GET", "/audit", {"limit": 5}, None),
    ("mail/status", "mailApi.status", "GET", "/mail/status", {}, None),
    ("mail/inbox", "mailApi.inbox", "GET", "/mail/inbox", {}, None),
    ("mail/outbox", "mailApi.outbox", "GET", "/mail/outbox", {}, None),
    ("mail/cleanup/log", "mailApi.cleanupLog", "GET", "/mail/cleanup/log", {}, None),
    ("#68b pending-confirms", "mailApi.pendingConfirms", "GET", "/mail/pending-confirms", {}, None),
    ("workflows", "workflowsApi.list", "GET", "/workflows", {}, None),
    ("system/public-endpoint", "systemApi.publicEndpoint", "GET", "/system/public-endpoint", {}, None),
]

CALLS_WRITE = [
    ("#25 probe 手动", "systemApi.probe", "POST", "/system/probe", {}, {"targets": None, "trigger": "manual"}),
    ("C-1 probe 采样", "systemApi.probeSample", "POST", "/system/probe", {}, {"mode": "sample", "duration_s": 5}),
    ("#79 selftest run", "systemApi.selftestRun", "POST", "/system/selftest", {}, None),
    ("#71 diagnostics", "systemApi.diagnostics", "POST", "/system/diagnostics", {}, {"with_screenshots": False}),
    ("#109 cleanup/run", "resourcesApi.cleanupRun", "POST", "/system/cleanup/run", {}, None),
    ("resources/precheck", "resourcesApi.precheck", "POST", "/resources/precheck", {}, {"channel": "qidian"}),
    ("resources/calibrate", "resourcesApi.calibrate", "POST", "/resources/calibrate", {}, {"apply": False}),
    ("#51 messages/export", "messagesApi.export", "POST", "/messages/export", {}, {"filter": {"limit": 10}, "format": "csv", "include_media": False}),
    ("#36 broadcast", "commandsApi.broadcast", "POST", "/broadcast/commands", {}, {"account_ids": ["{ACC}"], "op": "get_state", "args": {}, "idempotency_key": "e2e-bc-1"}),
    ("mail/test", "mailApi.test", "POST", "/mail/test", {}, {"which": "inbound", "route_id": None}),
    ("#67 hmac-keys", "mailApi.createHmacKey", "POST", "/mail/hmac-keys", {}, {"sender": "ops@corp", "short_name": "e2eops"}),
    ("模板预览", "settingsApi.previewMailTemplate", "POST", "/mail/templates/tpl-default/preview", {}, {"sample_message_id": "m1"}),
    ("#91 api-clients 建", "settingsApi.createApiClient", "POST", "/settings/api-clients", {}, {"name": "e2e", "level": "read", "auth_kind": "bearer"}),
    ("#102 docker-proxy", "systemApi.dockerProxy", "POST", "/system/docker-proxy", {}, {"enable": False}),
    ("settings/probe 采纳", "systemApi.adoptProbeTargets", "PUT", "/settings/probe", {}, {"observed_ids": []}),
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
        with urllib.request.urlopen(req, timeout=30) as r:
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--account", default="qd01")
    ap.add_argument("--kind", default="read", choices=["read", "write", "both"])
    args = ap.parse_args()
    calls = (CALLS_READ if args.kind in ("read", "both") else []) + (CALLS_WRITE if args.kind in ("write", "both") else [])
    out = {}
    for cid, src, method, path, query, body in calls:
        p = path.replace("{ACC}", args.account)
        b = json.loads(json.dumps(body).replace("{ACC}", args.account)) if body is not None else None
        res = call(args.base, args.token, method, p, query, b)
        res["src"] = src
        res["request"] = {"method": method, "path": p, "query": query, "body": b}
        out[cid] = res
        print(f"{res['status']!s:>5}  {method:6} {p}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
