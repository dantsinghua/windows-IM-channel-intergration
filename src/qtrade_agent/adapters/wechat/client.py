"""WinAgent 微信端点客户端(02 §3.6 #28~#43;Agent 是**调用方**,WinAgent 从不回调,C-03/C-25)。

- 鉴权/地址/令牌/超时/版本头一律复用 ``winagent_client.WinAgentClient``(02 §2.5 调用契约),本模块只拼路径与出入参。
- 重试(02 §2.5):**只读类 1 次重试**(status/version-match/profiles/login-status/ui-visible/read/sessions);
  **写类不重试**(login/start|cancel、bind、logout、key/retry、send)——重发一次写动作可能重复发消息。
- 执行体列为 ``user``(会话代理)的端点在**用户未登录 Windows** 时回 ``503 NOT_READY``(02 §3.6 表头),
  本模块统一抛 ``WeChatNotReady``,由适配器翻成 ``NOT_READY`` 结果码 / 账号 ``degraded``。
- ``send`` 是 **仅 A 令牌** 的写端点(#38),``confirm_timeout_ms`` 缺省 10000 = 02 §7.1 ``[bus] confirm_timeout_wechat_ms``。

本文件另带 ``FakeWeChatWinAgent`` —— **传输层**假实现(与 ``winagent_client.FakeWinAgent`` 同签名、可直接塞进
``WinAgentClient(transport=...)``),开发容器里一律用它,**绝不碰真 WinAgent / 真微信**(SKILL §4 禁区)。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from ...winagent_client import WinAgentClient, WinAgentUnavailable

# 02 §2.5 超时表:微信端点走「通用」一档;send 另按 confirm_timeout_ms 放宽(它要等 WinAgent 侧读回)
WECHAT_TIMEOUT_S = 5.0
WECHAT_SEND_EXTRA_S = 5.0          # send 的 HTTP 超时 = confirm_timeout_ms/1000 + 本值(留 WinAgent 收尾余量)

# #33 GET /wa/v1/wechat/login/status 的相位枚举(以 05 为准,R3-3;identified 是两钥取钥流程的枢纽相位)
PHASES = ("idle", "narrator", "qrcode", "identified", "keytry", "ready", "key_failed")


class WeChatNotReady(Exception):
    """会话代理不在线 / 微信模块未启用 / 锁屏 ⇒ 02 §3.6 的 ``503 NOT_READY``。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class WeChatCallFailed(Exception):
    """非 503 的失败状态码(400/403/404/409/500…);``status`` 与 ``body`` 原样带出供上层判 409 合并等分支。"""

    def __init__(self, status: int, body: Optional[dict[str, Any]], path: str):
        super().__init__(f"{path} -> {status} {body}")
        self.status = status
        self.body = body or {}
        self.path = path


class WeChatWinAgent:
    """02 §3.6 #28~#43 的 Agent 侧调用封装。"""

    def __init__(self, wa: WinAgentClient, *, timeout_s: float = WECHAT_TIMEOUT_S):
        self._wa = wa
        self._timeout_s = timeout_s

    # ------------------------------------------------------------------ 内部
    async def _call(self, method: str, path: str, *, body: Optional[dict[str, Any]] = None, retry: bool = False,
                    timeout_s: Optional[float] = None) -> dict[str, Any]:
        try:
            status, parsed = await self._wa.request(method, path, json=body, timeout_s=timeout_s or self._timeout_s, retry=retry)
        except WinAgentUnavailable as e:
            raise WeChatNotReady("winagent_unreachable", e.detail) from e
        if status == 503:
            raise WeChatNotReady("user_agent_offline", (parsed or {}).get("error", {}).get("message", "") if parsed else "")
        if status >= 400:
            raise WeChatCallFailed(status, parsed, path)
        return parsed or {}

    async def _raw(self, method: str, path: str, *, timeout_s: Optional[float] = None) -> bytes:
        """二进制端点(#40 media / #42 screenshot):走 ``WinAgentClient.request(..., raw=True)``(总控已补),
        回 ``(status, headers, bytes)``,不经 JSON 解析。"""
        try:
            status, _rh, raw = await self._wa.request(method, path, timeout_s=timeout_s or self._timeout_s, raw=True)
        except WinAgentUnavailable as e:
            raise WeChatNotReady("winagent_unreachable", e.detail) from e
        if status == 503:
            raise WeChatNotReady("user_agent_offline")
        if status >= 400:
            raise WeChatCallFailed(status, None, path)
        return raw

    # ------------------------------------------------------------------ 只读(1 次重试)
    async def status(self) -> dict[str, Any]:
        """#28:``{enabled, wechat:{installed,version,running,logged_in,wxid,nickname,pid}, chatlog:{running,port,key_ok,dll},
        ritual_done, screen_locked, login_session?}``;模块关闭时 ``enabled:false`` 其余 null。"""
        return await self._call("GET", "/wa/v1/wechat/status", retry=True)

    async def version_match(self) -> dict[str, Any]:
        """#29:``{match, action, current_version, bundled_version, dll, status}``(基线 §8.6)。"""
        return await self._call("GET", "/wa/v1/wechat/version-match", retry=True)

    async def profiles(self) -> list[dict[str, Any]]:
        """#30:``wechat_profiles`` 全表(供 Agent 同步到 ``accounts(channel=wechat)``)。"""
        body = await self._call("GET", "/wa/v1/wechat/profiles", retry=True)
        return list(body.get("data") or body.get("profiles") or [])

    async def login_status(self) -> dict[str, Any]:
        """#33:``{phase, wxid?, account_id?, nickname?, countdown_s?, narrator:{state,remaining_s}, key:{ok,dll,error}}``。"""
        return await self._call("GET", "/wa/v1/wechat/login/status", retry=True)

    async def ui_visible(self) -> dict[str, Any]:
        """#36:``{visible, minimized, locked}`` 微信主窗口是否可自动化。"""
        return await self._call("GET", "/wa/v1/wechat/ui-visible", retry=True)

    async def read(self, *, talker: Optional[str] = None, since_seq: Optional[int] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """#39 ``GET /wa/v1/wechat/read?talker=&since_seq=&limit=``:WinAgent 内调 chatlog 取增量并归一化后回 ``Message[]``。

        取窗 ``start = cursor − lookback_overlap_s`` 在 **WinAgent 侧**做(06 §2.9.1:该参数在 winagent.toml ``[wechat]``,不在 Agent 册),
        重叠窗必然重复取到边界几条 —— 全靠去重键 ``(account_id, ext_msg_id="{talker}:{seq}")`` 挡(06 §2.9.2)。
        """
        q = []
        if talker:
            q.append("talker=" + _q(talker))
        if since_seq is not None:
            q.append(f"since_seq={int(since_seq)}")
        if limit is not None:
            q.append(f"limit={int(limit)}")
        path = "/wa/v1/wechat/read" + ("?" + "&".join(q) if q else "")
        body = await self._call("GET", path, retry=True)
        return list(body.get("data") or body.get("messages") or [])

    async def sessions(self, *, keyword: Optional[str] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """#41:``ChatlogClient.list_chatrooms`` + 联系人。"""
        q = []
        if keyword:
            q.append("keyword=" + _q(keyword))
        if limit is not None:
            q.append(f"limit={int(limit)}")
        path = "/wa/v1/wechat/sessions" + ("?" + "&".join(q) if q else "")
        body = await self._call("GET", path, retry=True)
        return list(body.get("data") or body.get("sessions") or [])

    async def screenshot(self) -> bytes:
        """#42:微信主窗口截图 PNG(锁屏时 ``NOT_READY``)。"""
        return await self._raw("GET", "/wa/v1/wechat/screenshot")

    async def media(self, key: str) -> bytes:
        """#40:代理 chatlog ``/image/<key>``(同 ``ChatlogClient.download_image`` 的同源限制与 20MB 上限)。"""
        return await self._raw("GET", f"/wa/v1/wechat/media/{key}")

    # ------------------------------------------------------------------ 写类(不重试)
    async def login_start(self, *, account_id: Optional[str] = None, login_session_id: Optional[str] = None) -> dict[str, Any]:
        """#31:拉起微信 + chatlog + 试钥(多 DLL 轮试)+ 讲述人仪式 → ``202 {login_session_id}``。"""
        body: dict[str, Any] = {}
        if account_id:
            body["account_id"] = account_id
        if login_session_id:
            body["login_session_id"] = login_session_id
        return await self._call("POST", "/wa/v1/wechat/login/start", body=body)

    async def login_cancel(self) -> dict[str, Any]:
        """#32:取消登录会话(扫码超时/用户放弃);**微信已登进去的不登出**(05 §2.4.2.1 失败回滚原则)。"""
        return await self._call("POST", "/wa/v1/wechat/login/cancel", body={})

    async def bind(self, wxid: str, account_id: str) -> dict[str, Any]:
        """#33c:把取钥读到的 ``wxid`` 绑到 Agent 已建的 ``wxNN``(05 §2.4.2.1 第 4 步);同 ``wxid`` 重绑幂等,
        绑了别的 id ⇒ ``409``(调用方按 05 的合并逻辑以 winagent.db 已有的 ``account_id`` 为准)。"""
        return await self._call("POST", "/wa/v1/wechat/bind", body={"wxid": wxid, "account_id": account_id})

    async def logout(self) -> dict[str, Any]:
        """#34:登出(切换前置;P-31/05 §2.4.6:先 WM_CLOSE,10s 未退再结束进程)。"""
        return await self._call("POST", "/wa/v1/wechat/logout", body={})

    async def key_retry(self) -> dict[str, Any]:
        """#35:手动重试取钥 → ``{ok, dll, error}``(05 §2.4.7 恢复路径)。"""
        return await self._call("POST", "/wa/v1/wechat/key/retry", body={})

    async def send(self, *, session_name: str, text: Optional[str] = None, image_path: Optional[str] = None,
                   idempotency_key: str, confirm_timeout_ms: int = 10000) -> dict[str, Any]:
        """#38 **仅 A 令牌**:pyweixin 写 + 临时加速 chatlog 轮询读回 → ``{ok, code:'DELIVERED|SEND_FAILED', ext_msg_id, confirm_ms}``;**不重试**。"""
        body: dict[str, Any] = {"session_name": session_name, "idempotency_key": idempotency_key, "confirm_timeout_ms": int(confirm_timeout_ms)}
        if text is not None:
            body["text"] = text
        if image_path is not None:
            body["image_path"] = image_path
        return await self._call("POST", "/wa/v1/wechat/send", body=body, timeout_s=confirm_timeout_ms / 1000 + WECHAT_SEND_EXTRA_S)


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ---------------------------------------------------------------------- 假实现(开发容器唯一允许的 WinAgent)
class FakeWeChatWinAgent:
    """传输层假 WinAgent(只认 ``/wa/v1/wechat/*``,其余路径委托给 ``winagent_client.FakeWinAgent``)。

    可编程点:``phase``/``wxid``/``key``/``status_body``/``send_result``/``rows``/``bound``/``offline``/``user_agent``;
    ``calls`` 记每次请求(method, path, body)。**不改 winagent_client.py**:组合而非继承(FakeWinAgent 只是委托对象)。
    """

    def __init__(self, *, token: str = "wa-token", version: str = "1.0.0"):
        from ...winagent_client import FakeWinAgent
        self._base = FakeWinAgent(token=token, version=version)
        self._base.wechat_enabled = True
        self.token = token
        self.version = version
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.offline = False                   # True ⇒ 每次请求抛 OSError(WinAgent 不可达)
        self.fail_next = 0                     # 接下来 N 次微信请求抛 OSError(测「只读重试一次 / 写不重试」)
        self.fail_paths: dict[str, int] = {}   # {路径: 次数} 只让某个端点失败(测「bind 网络失败重试、轮询抖动」)

        # 微信模块状态(#28)
        self.enabled = True
        self.user_agent = True                 # False ⇒ 执行体 user 的端点一律 503 NOT_READY
        self.wechat = {"installed": True, "version": "4.1.12.26", "running": True, "logged_in": True, "wxid": "wxid_demo01",
                       "nickname": "安琳", "pid": 4321}
        self.chatlog = {"running": True, "port": 5030, "key_ok": True, "dll": "wx_key2.dll"}
        self.ritual_done = True
        self.screen_locked = False

        # 登录会话(#31/#33)
        self.phase = "ready"
        self.countdown_s: Optional[int] = None
        self.key: dict[str, Any] = {"ok": True, "dll": "wx_key2.dll", "error": None, "stage": None}
        self.narrator: dict[str, Any] = {"state": "off", "remaining_s": 0}
        self.login_session_id: Optional[str] = None
        self.phases_script: list[dict[str, Any]] = []      # 非空时每次 #33 弹一帧(模拟相位推进)
        self.bound: dict[str, str] = {}                    # wxid -> account_id(#33c)
        self.bind_conflict: Optional[str] = None           # 非空 ⇒ #33c 回 409 且带这个 account_id

        # 消息(#39)与发送(#38)
        self.rows: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.send_result: dict[str, Any] = {"ok": True, "code": "DELIVERED", "ext_msg_id": None, "confirm_ms": 1200}
        self.next_seq = 1
        self.session_rows: list[dict[str, Any]] = []
        self.screenshot_png = b"\x89PNG\r\n\x1a\n-fake"
        self.logout_calls = 0
        self.key_retry_calls = 0
        self.cancel_calls = 0

    # -- 传输层协议(与 FakeWinAgent.__call__ 同签名)
    async def __call__(self, method: str, url: str, headers: dict[str, str], body: Optional[bytes], timeout_s: float):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[1] if "://" in url else url
        if not path.startswith("/wa/v1/wechat"):
            return await self._base(method, url, headers, body, timeout_s)
        data = json.loads(body.decode()) if body else {}
        self.calls.append((method, path, data))
        if self.offline:
            raise OSError("connection refused")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise OSError("transient")
        hit = path.split("?", 1)[0]
        if self.fail_paths.get(hit, 0) > 0:
            self.fail_paths[hit] -= 1
            raise OSError(f"transient on {hit}")
        rh = {"X-WA-Version": self.version, "Content-Type": "application/json"}
        if headers.get("Authorization") != f"Bearer {self.token}":
            return 401, rh, b'{"ok":false,"code":"UNAUTHORIZED"}'
        bare = path.split("?", 1)[0]
        query = _parse_query(path)
        # §3.6 表头:执行体 `user` 的端点在会话代理不在线时一律 503(#28 status 的执行体也是 user,故不例外;
        # 辨别「会话代理在不在」走 #2 `GET /wa/v1/health` 的 `user_agent` 布尔,02 §2.4.1)
        if not self.user_agent:
            return 503, rh, _j({"ok": False, "code": "NOT_READY", "error": {"message": "会话代理不在线"}})
        if not self.enabled and bare != "/wa/v1/wechat/status":
            return 503, rh, _j({"ok": False, "code": "NOT_READY", "error": {"message": "微信模块未启用"}})
        return self._route(method, bare, query, data, rh)

    def _route(self, method: str, bare: str, query: dict[str, str], data: dict[str, Any], rh: dict[str, str]):
        if bare == "/wa/v1/wechat/status":
            if not self.enabled:
                return 200, rh, _j({"enabled": False, "wechat": None, "chatlog": None, "ritual_done": None, "screen_locked": None})
            return 200, rh, _j({"enabled": True, "wechat": dict(self.wechat), "chatlog": dict(self.chatlog),
                                "ritual_done": self.ritual_done, "screen_locked": self.screen_locked,
                                "login_session": self.login_session_id})
        if bare == "/wa/v1/wechat/login/status":
            if self.phases_script:
                frame = self.phases_script.pop(0)
                self.phase = frame.get("phase", self.phase)
                self.key = {**self.key, **frame.get("key", {})}
                self.countdown_s = frame.get("countdown_s", self.countdown_s)
                if "wxid" in frame:
                    self.wechat["wxid"] = frame["wxid"]
            out: dict[str, Any] = {"phase": self.phase, "narrator": dict(self.narrator), "key": dict(self.key),
                                   "countdown_s": self.countdown_s, "nickname": self.wechat.get("nickname")}
            if self.phase in ("identified", "keytry", "ready"):
                out["wxid"] = self.wechat.get("wxid")
                out["account_id"] = self.bound.get(str(self.wechat.get("wxid")))
            return 200, rh, _j(out)
        if bare == "/wa/v1/wechat/login/start" and method == "POST":
            self.login_session_id = data.get("login_session_id")
            return 202, rh, _j({"login_session_id": self.login_session_id})
        if bare == "/wa/v1/wechat/login/cancel" and method == "POST":
            self.cancel_calls += 1
            self.phase = "idle"
            return 200, rh, _j({"ok": True})
        if bare == "/wa/v1/wechat/bind" and method == "POST":
            wxid, aid = data["wxid"], data["account_id"]
            if self.bind_conflict and self.bind_conflict != aid:
                return 409, rh, _j({"ok": False, "code": "CONFLICT", "wxid": wxid, "account_id": self.bind_conflict})
            self.bound[wxid] = aid
            return 200, rh, _j({"ok": True, "wxid": wxid, "account_id": aid})
        if bare == "/wa/v1/wechat/logout" and method == "POST":
            self.logout_calls += 1
            self.wechat["logged_in"] = False
            self.wechat["running"] = False
            return 202, rh, _j({"ok": True})
        if bare == "/wa/v1/wechat/key/retry" and method == "POST":
            self.key_retry_calls += 1
            return 200, rh, _j({"ok": self.chatlog["key_ok"], "dll": self.chatlog["dll"], "error": None})
        if bare == "/wa/v1/wechat/ui-visible":
            return 200, rh, _j({"visible": not self.screen_locked, "minimized": False, "locked": self.screen_locked})
        if bare == "/wa/v1/wechat/version-match":
            return 200, rh, _j({"match": True, "action": "continue", "current_version": self.wechat["version"],
                                "bundled_version": "4.1.12.26", "dll": self.chatlog["dll"], "status": "SUPPORTED"})
        if bare == "/wa/v1/wechat/profiles":
            return 200, rh, _j({"data": [{"wxid": w, "account_id": a} for w, a in self.bound.items()]})
        if bare == "/wa/v1/wechat/send" and method == "POST":
            self.sent.append(dict(data))
            res = dict(self.send_result)
            if res.get("code") == "DELIVERED" and not res.get("ext_msg_id"):
                res["ext_msg_id"] = f"{data['session_name']}:{self.next_seq}"
                self.next_seq += 1
            return 200, rh, _j(res)
        if bare == "/wa/v1/wechat/read":
            talker = query.get("talker")
            since = int(query.get("since_seq") or 0)
            rows = [r for r in self.rows if int(r["seq"]) > since and (talker is None or r["talker"] == talker)]
            limit = int(query["limit"]) if "limit" in query else None
            return 200, rh, _j({"data": rows[:limit] if limit else rows})
        if bare == "/wa/v1/wechat/sessions":
            return 200, rh, _j({"data": list(self.session_rows)})
        if bare == "/wa/v1/wechat/screenshot":
            return 200, {"X-WA-Version": self.version, "Content-Type": "image/png"}, self.screenshot_png
        if bare.startswith("/wa/v1/wechat/media/"):
            return 200, {"X-WA-Version": self.version, "Content-Type": "image/jpeg"}, b"jpegbytes"
        return 404, rh, b'{"ok":false,"code":"TARGET_NOT_FOUND"}'

    # -- 造数便利
    def add_row(self, *, talker: str, content: str, seq: Optional[int] = None, ts_ms: Optional[int] = None,
                is_self: bool = False, type: int = 1, **kw: Any) -> dict[str, Any]:
        s = seq if seq is not None else self.next_seq
        if seq is None:
            self.next_seq += 1
        row = {"talker": talker, "seq": s, "content": content, "isSelf": is_self, "type": type,
               "time": (ts_ms if ts_ms is not None else int(time.time() * 1000)), **kw}
        self.rows.append(row)
        return row


def _j(o: Any) -> bytes:
    return json.dumps(o, ensure_ascii=False).encode("utf-8")


def _parse_query(path: str) -> dict[str, str]:
    if "?" not in path:
        return {}
    from urllib.parse import parse_qsl
    return dict(parse_qsl(path.split("?", 1)[1]))
