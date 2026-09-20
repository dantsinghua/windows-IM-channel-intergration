"""04 §2.3 **H08 napcat WS 心跳** —— 规格唯一出处:docs/04 §2.3 H08 行 + §5 F-08 + §8 A5-07;告警码 docs/02 §3.7。

H08 行逐字:
| H08 | napcat WS 心跳 | WSL | OneBot 正向 WS 的 ``meta_event.heartbeat``(默认 5 s 一次);备用 ``GET http://127.0.0.1:162NN/get_status``
| 15 s | 30 s 无心跳 | warn→crit | 重连 WS(这是我方到 napcat 的**本地连接,不是登录**);``get_status.online=false`` 持续 2 min
转账号 ``login_required`` 并推事件,**不自动重登**(D-2,基线 §11.15 [LOGINPHASE])|

落地口径:
- 周期 ``H08_INTERVAL_S=15``(04 字面);无心跳阈值 = ``[health] napcat_heartbeat_timeout_s=30``(owner=04,``config.py`` 已镜像)。
- 「warn→crit」的升级点规格没写死 ⇒ 取 **warn = 心跳丢 / 刚判离线**,**crit = ``online=false`` 已持续 2 min**
  (与转 ``login_required`` 同一刻),见 handoff「规格张力 ④」。级别翻转由 ``alerts.firing`` 再发一次 ``firing``(R6-56 ⑦)。
- 转 ``login_required`` 用哪个 ``state_code`` 规格没写死 ⇒ 取 05 §2.5.4 QQ 行两码里的 ``TOKEN_EXPIRED``,见 handoff「规格张力 ⑤」。
- 🔴 **动作止于「重连 WS + 置状态 + 推事件」**:不重启容器、不出码、不重发凭据(D-2;05 §2.5.4 QQ 行「容器保持运行、不自动重启」)。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ...config import AgentConfig
from ..base import Account
from .adapter import QQAdapter, seq_of
from .config import H08_INTERVAL_S, H08_OFFLINE_TO_LOGIN_REQUIRED_S
from .onebot import OneBotClosed, OneBotError, http_url

log = logging.getLogger("qtrade.adapters.qq.health")

H08_NAPCAT_HEARTBEAT_LOST = "H08_NAPCAT_HEARTBEAT_LOST"
"""02 §3.7 登记:``warn`` · ``subject=account:<id>`` · 来源 Agent · 事件 ``alert``。
``alerts.REGISTERED`` 本期没登记它(那张表只收前四批用到的码),``Alerts.firing`` 对未登记码默认 ``warn`` —— 与 §3.7 一致;
总控接线时把它补进 ``alerts.REGISTERED``(建议片段见 handoff)。"""

H08_LOGIN_REQUIRED_STATE_CODE = "TOKEN_EXPIRED"
"""05 §2.5.4「掉线原因」组(``KICKED / LOGGED_OUT / TOKEN_EXPIRED / LOGIN_TIMEOUT``)里 QQ 行给的两码之一。"""

WATCH_STATES = ("starting", "login_required", "logging_in", "running", "degraded", "error")

StatusProbe = Callable[[Account], Awaitable[Optional[dict[str, Any]]]]
LoginRequiredFn = Callable[[str, str], Awaitable[None]]


def status_url(account_id: str) -> str:
    """04 H08 备用探测地址:``GET http://127.0.0.1:162NN/get_status``(00 §3 端口段,``http = 16200 + NN``)。"""
    from ...runtime import port_plan
    return f"{http_url(port_plan('qq', seq_of(account_id))['http'])}/get_status"


@dataclass
class H08State:
    """每账号一份的进程内内存态(**不落库**,与 H06 的 ``h06_fail_streak`` 同款)。"""
    offline_since_ms: Optional[int] = None
    login_required_sent: bool = False
    reconnects: int = 0
    heartbeat_losses: int = 0
    disconnected_since_ms: Optional[int] = None      # WS 断连起点(05 §2.5.4 QQ 行 ①:超 qq_reconnect_grace_s 转 login_required)


class QQHealth:
    """H08 探测器。``check()`` 可直接被 ``scheduler.register('health_napcat', H08_INTERVAL_S, qqhealth.check)`` 注册。"""

    def __init__(self, *, adapter: QQAdapter, store, alerts, cfg: AgentConfig,
                 on_login_required: Optional[LoginRequiredFn] = None,
                 status_probe: Optional[StatusProbe] = None,
                 busy: Callable[[str], bool] = lambda account_id: False,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._adapter = adapter
        self._store = store
        self._alerts = alerts
        self.cfg = cfg
        self._on_login_required = on_login_required
        self._status_probe = status_probe            # 备用通道(HTTP);None = 只走 WS,不发任何网络请求
        self._busy = busy
        self._clock = clock
        self.state: dict[str, H08State] = {}
        self.last: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 公共
    def state_of(self, account_id: str) -> H08State:
        return self.state.setdefault(account_id, H08State())

    def checks(self) -> dict[str, str]:
        """``/system/health.checks`` 的 H08:任一账号 firing ⇒ firing;没跑过 ⇒ unknown(与 healthloop.checks 同款)。"""
        if not self.last:
            return {"H08": "unknown"}
        firing = any(k[0] == H08_NAPCAT_HEARTBEAT_LOST for k in self._alerts.active)
        return {"H08": "firing" if firing else "ok"}

    def watched(self) -> list[dict[str, Any]]:
        out = []
        for row in self._store.list_accounts(channel="qq"):
            if row.get("host") != "wsl" or not row.get("enabled") or row.get("desired_state") != "running":
                continue
            if row.get("state") not in WATCH_STATES or self._busy(row["id"]):
                continue
            out.append(row)
        return out

    # ------------------------------------------------------------------ 一轮
    async def check(self) -> None:
        now = self._clock()
        hb_timeout_ms = self.cfg.health.napcat_heartbeat_timeout_s * 1000
        for row in self.watched():
            aid = row["id"]
            sess = self._adapter.session_of(aid)
            if sess is None:
                continue                                        # 还没 start(适配器没建连)——不是 H08 的事
            subject = f"account:{aid}"
            st = self.state_of(aid)
            client = sess.client
            silence = client.silence_ms(now)
            lost = silence is None or silence >= hb_timeout_ms
            if lost:
                st.heartbeat_losses += 1
                if await client.reconnect():                    # 04 H08 自愈动作:重连 WS(本地连接,不是重登)
                    st.reconnects += 1
                online = await self._probe_status(sess.acct, sess)
            else:
                status = client.last_status or {}
                online = status.get("online") if "online" in status else None
            # 05 §2.5.4 QQ 行 ①:**WS 断连 `[accounts] qq_reconnect_grace_s`(60 s)内仍连不上 ⇒ login_required**。
            # 这是与「心跳丢 / get_status.online=false」并列的第三条判据(rulings R6-58 (n-qq));
            # 判在 H08 这一轮里(04 H08 行只管心跳与 get_status,但 `OneBotClient` 会一直退避重连、自己不会判)。
            grace_ms = self.cfg.accounts.qq_reconnect_grace_s * 1000
            if not client.connected:
                st.disconnected_since_ms = st.disconnected_since_ms or now
            else:
                st.disconnected_since_ms = None
            disconnected_s = ((now - st.disconnected_since_ms) // 1000) if st.disconnected_since_ms else 0
            self.last[aid] = {"silence_ms": silence, "lost": lost, "online": online, "checked_ms": now,
                              "connected": client.connected, "disconnected_s": disconnected_s}
            if st.disconnected_since_ms is not None and now - st.disconnected_since_ms >= grace_ms:
                await self._reconnect_grace_exceeded(aid, subject, st, disconnected_s=disconnected_s, now=now)
                continue
            await self._judge(aid, subject, st, lost=lost, online=online, silence=silence, now=now)

    async def _reconnect_grace_exceeded(self, account_id: str, subject: str, st: H08State, *,
                                        disconnected_s: int, now: int) -> None:
        """WS 断连超 ``qq_reconnect_grace_s``:crit 告警 + 转 ``login_required``(**不自动重登**,D-2)。"""
        self._alerts.firing(H08_NAPCAT_HEARTBEAT_LOST, subject=subject, severity="crit", account_id=account_id,
                            evidence={"connected": False, "disconnected_s": disconnected_s,
                                      "qq_reconnect_grace_s": self.cfg.accounts.qq_reconnect_grace_s},
                            hint_actions=["open_acct_detail"])
        if st.login_required_sent:
            return
        st.login_required_sent = True
        log.warning("H08:账号 %s 的 OneBot WS 断连已 %d s(> qq_reconnect_grace_s),置 login_required(%s);**不自动重登**(D-2)",
                    account_id, disconnected_s, H08_LOGIN_REQUIRED_STATE_CODE)
        if self._on_login_required is not None:
            await self._on_login_required(account_id, H08_LOGIN_REQUIRED_STATE_CODE)

    async def _probe_status(self, acct: Account, sess) -> Optional[bool]:
        """先走 WS ``get_status``(重连成功时它是通的);不通再走 04 指定的备用 HTTP 探针(未注入 ⇒ 判不出、回 None)。"""
        if sess.client.connected:
            try:
                data = await sess.client.call_action("get_status")
                if isinstance(data, dict) and "online" in data:
                    return bool(data["online"])
            except (OneBotClosed, OneBotError) as e:
                log.info("H08 WS get_status 未取到 account=%s: %s", acct.id, e)
        if self._status_probe is None:
            return None
        try:
            data = await self._status_probe(acct)
        except Exception as e:                                   # 备用探针异常不杀本轮
            log.info("H08 备用 get_status 探测失败 account=%s: %s", acct.id, e)
            return None
        if isinstance(data, dict) and "online" in data:
            return bool(data["online"])
        return None

    async def _judge(self, account_id: str, subject: str, st: H08State, *, lost: bool, online: Optional[bool],
                     silence: Optional[int], now: int) -> None:
        evidence: dict[str, Any] = {"silence_ms": silence, "heartbeat_timeout_s": self.cfg.health.napcat_heartbeat_timeout_s,
                                    "online": online, "reconnects": st.reconnects}
        if online is False:
            if st.offline_since_ms is None:
                st.offline_since_ms = now
            offline_s = (now - st.offline_since_ms) // 1000
            evidence["offline_s"] = offline_s
            if offline_s >= H08_OFFLINE_TO_LOGIN_REQUIRED_S:
                self._alerts.firing(H08_NAPCAT_HEARTBEAT_LOST, subject=subject, severity="crit", account_id=account_id,
                                    evidence=evidence, hint_actions=["open_acct_detail"])
                if not st.login_required_sent:
                    st.login_required_sent = True
                    log.warning("H08:账号 %s 的 napcat 报离线已 %d s,置 login_required(%s);**不自动重登**(D-2)",
                                account_id, offline_s, H08_LOGIN_REQUIRED_STATE_CODE)
                    if self._on_login_required is not None:
                        await self._on_login_required(account_id, H08_LOGIN_REQUIRED_STATE_CODE)
            else:
                self._alerts.firing(H08_NAPCAT_HEARTBEAT_LOST, subject=subject, severity="warn", account_id=account_id,
                                    evidence=evidence, hint_actions=["open_acct_detail"])
            return
        st.offline_since_ms = None
        st.login_required_sent = False
        if lost:
            self._alerts.firing(H08_NAPCAT_HEARTBEAT_LOST, subject=subject, severity="warn", account_id=account_id,
                                evidence=evidence, hint_actions=["open_acct_detail"])
            return
        self._alerts.resolve(H08_NAPCAT_HEARTBEAT_LOST, subject=subject, account_id=account_id)


__all__ = ["H08_INTERVAL_S", "H08_LOGIN_REQUIRED_STATE_CODE", "H08_NAPCAT_HEARTBEAT_LOST", "H08State", "QQHealth", "status_url"]
