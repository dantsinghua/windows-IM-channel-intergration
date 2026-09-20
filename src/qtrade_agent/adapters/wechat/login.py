"""微信登录会话流(Agent 半)—— 05 §2.4.4 完整流程 + §2.4.4a 取钥时序 + §2.4.2.1 id 分配/回填/合并/回滚;02 §3.6 #31/#32/#33/#33c。

全程 **Agent 驱动、轮询 #33,WinAgent 从不回调**(05 §2.4.8 图注:W→A 的箭头都是轮询的响应)。相位 → 账号状态:

| #33 `phase` | 账号状态(00 §8.1 不跳段) | 说明 |
|---|---|---|
| `narrator`   | `login_required(WAIT_NARRATOR)` | 讲述人仪式,`prompt` 带倒计时(`narrator_min_seconds` 默认 300,05 §2.4.3) |
| `qrcode`     | `login_required(WAIT_QRCODE)`   | 二维码在微信自己的窗口里、人直接手机扫;**不转发码**(微信没有取码接口),控制台只做窗口截图预览 |
| `identified` | `logging_in`                    | 🔴 枢纽相位:拿到 `wxid`(取钥/仪式未完)⇒ Agent 回填/合并 `accounts` 再调 #33c `bind` |
| `keytry`     | `login_required(WAIT_KEY_IMG / WAIT_KEY_RELOGIN)` | 🔴 **两个独立码、不许合成**(05 §2.4.4a):指示完全不同(打开图片 vs 退出重登),顺序做反整轮作废 |
| `ready`      | `running`                        | 两钥同轮落盘 + bind 成功 ⇒ `pending → holder` |
| `key_failed` | `degraded(KEY_FAIL)`             | 只拿到一把 / 全 DLL 失败 ⇒ 读写都拒(05 §2.4.7);**失败即释放 pending、不进 bind** |

回滚(05 §2.4.2.1「失败回滚」表)——原则:**微信本身已登录的事实不回滚**,只回滚我们的登记;
任一失败点均**同步释放 `pending` 槽位并回收中间产物**(TTL/reaper 只兜底进程被杀等异常路径):
- 读 wxid 前失败(扫码超时 `[accounts] qr_max_wait_s`、hook 没装上、进程被杀)⇒ 临时行 `stopped` + 释放 pending;
- 取钥失败 ⇒ `degraded(KEY_FAIL)` + 释放 pending,**不进 bind**;
- 回填/写库失败 ⇒ 不调 bind,每 5 s 重试(幂等:先查 `wxid` 再回填/合并),超上限 ⇒ 释放 pending;
- `bind` 网络失败/503 ⇒ 账号行留 `created`,每 5 s 重试(幂等),超 `bind_retry_max`(默认 12)⇒ `error(VAULT_UNAVAILABLE)` 同款环境类错误 + 释放 pending;
- `bind` 回 409(该 wxid 已绑别的 `account_id`)⇒ 与合并同路:**以 winagent.db 已有的 `account_id` 为准**,
  把刚分配的行标 `merged_into` + `deleted_ms` 软删(永不复用),改用 409 带回的 id。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from ...api.auth import ApiError
from ...config import AgentConfig
from .client import WeChatCallFailed, WeChatNotReady, WeChatWinAgent

log = logging.getLogger("qtrade.adapters.wechat.login")

NARRATOR_MIN_SECONDS = 300          # 05 §2.4.3 ④ / winagent.toml [wechat] narrator_min_seconds 的镜像默认
KEY_IMG_WINDOW_S = 60               # 05 §2.4.4a:img_key 内存扫描窗口约 60 s
KEY_RELOGIN_WINDOW_S = 30           # 05 §2.4.4a:data_key 远程 hook 只在「登录那一刻」触发,窗口前 30 s
RETRY_INTERVAL_S = 5.0              # 05 §2.4.2.1:回填/bind 失败每 5 s 重试
BIND_RETRY_MAX = 12                 # 05 §2.4.2.1 第 4 步 bind_retry_max 默认 12
STATUS_INTERVAL_S = 1.0             # 轮询 #33 的节拍(Agent 侧;05 未钉死具体值,取 1 s 与倒计时同粒度)

PROMPTS = {
    "WAIT_NARRATOR": "讲述人仪式进行中,约 5 分钟,可以静音,不要关闭讲述人",
    "WAIT_QRCODE": "请用手机扫描微信窗口里的二维码",
    "WAIT_KEY_IMG": "请在微信里打开任意一张图片(取图片密钥,约 60 秒内完成)",
    "WAIT_KEY_RELOGIN": "请退出微信登录并立刻重新登录(快捷登录即可,无需扫码;须在 30 秒内完成)",
}


class WechatLoginFlow:
    """一次登录尝试(一个 ``login_session_id``)的全程编排;返回终态 ``'running'|'degraded'|'stopped'|'error'``。"""

    def __init__(self, *, store, client: WeChatWinAgent, slot, cfg: AgentConfig,
                 transition: Callable[..., dict[str, Any]], clock: Callable[[], int],
                 sleep: Optional[Callable[[float], Awaitable[None]]] = None,
                 status_interval_s: float = STATUS_INTERVAL_S, bind_retry_max: Optional[int] = None):
        self._store = store
        self._client = client
        self._slot = slot
        self.cfg = cfg
        self._transition = transition
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._status_interval_s = status_interval_s
        # 05 §7 / docs/07 §[accounts]:``bind_retry_max`` 是 **agent.toml 的配置项**,不是写死的常量;
        # 构造参数仍可覆盖(单测用),缺省取 ``cfg.accounts.bind_retry_max``。
        self._bind_retry_max = bind_retry_max if bind_retry_max is not None else cfg.accounts.bind_retry_max

    # ------------------------------------------------------------------ 主流程
    async def run(self, account_id: str, login_session_id: str) -> str:
        aid, ls = account_id, login_session_id
        deadline = self._clock() + self.cfg.accounts.qr_max_wait_s * 1000
        self._enter_starting(aid)
        try:
            await self._client.login_start(account_id=aid, login_session_id=ls)
        except (WeChatNotReady, WeChatCallFailed) as e:
            return await self._fail_stopped(aid, ls, f"登录会话未起来:{e}")

        last_code: Optional[str] = None
        bound_to: Optional[str] = None                 # 已 bind 成功的最终 account_id(合并时可能是老 id)
        bind_attempts = 0
        while True:
            if self._clock() > deadline:
                # 会话代理不可达 / 人一直没扫:超 qr_max_wait_s 转 stopped、释放 pending(05 §2.4.2.1「断连期间的规则」)
                return await self._fail_stopped(aid, ls, f"扫码等待超过 {self.cfg.accounts.qr_max_wait_s}s")
            self._renew_pending(bound_to or aid)       # T-11:登录流在世期间续期 pending TTL,免得 reaper 在 600 s 处误杀
            try:
                st = await self._client.login_status()
            except (WeChatNotReady, WeChatCallFailed) as e:
                log.warning("微信 login/status 轮询失败 account=%s: %s", aid, e)
                await self._sleep(self._status_interval_s)
                continue
            phase = str(st.get("phase") or "idle")

            if phase == "key_failed":
                return await self._key_failed(aid, ls, st)
            if phase == "ready":
                return await self._ready(bound_to or aid, ls, st)
            if phase == "identified":
                if bound_to is None:
                    last_code = self._set_state(aid, "logging_in", None, ls, last_code)
                    try:
                        bound_to = await self._identify_and_bind(aid, ls, st)
                    except _BindRetry:
                        bind_attempts += 1
                        if bind_attempts > self._bind_retry_max:
                            return await self._bind_exhausted(aid, ls)
                        await self._sleep(RETRY_INTERVAL_S)
                        continue
            elif phase in ("narrator", "qrcode", "keytry"):
                code = self._code_for(phase, st)
                last_code = self._set_state(aid, "login_required", code, ls, last_code, prompt=self._prompt(code, st))
            await self._sleep(self._status_interval_s)

    def _renew_pending(self, target: str) -> None:
        """T-11(`.omc/handoffs/wechat-channel.md`,总控裁决草案 R6-58 (l) 取「登录流在世期间续期」):
        每轮把 ``slot_pending_expires_ms`` 推到 ``now + [wechat] slot_pending_ttl_s``。
        规格两值冲突(TTL 600 < ``qr_max_wait_s`` 1800)时,续期让 reaper 只回收**真没人管**的 pending。"""
        try:
            self._store.wechat_slot_renew_pending(target, self._clock() + self.cfg.wechat.slot_pending_ttl_s * 1000,
                                                  now_ms=self._clock())
        except Exception as e:                        # 续期失败不打断登录流(reaper 兜底)
            log.warning("续期微信槽位 pending TTL 失败 account=%s: %s", target, e)

    # ------------------------------------------------------------------ 相位 → state_code / prompt
    @staticmethod
    def _code_for(phase: str, st: dict[str, Any]) -> str:
        if phase == "narrator":
            return "WAIT_NARRATOR"
        if phase == "qrcode":
            return "WAIT_QRCODE"
        # 05 §2.4.4a 两个独立码;#33 的 `key` 对象用 `stage` 区分取钥阶段('img' 先 / 'relogin' 后),缺省按先点图
        stage = str((st.get("key") or {}).get("stage") or "img")
        return "WAIT_KEY_RELOGIN" if stage == "relogin" else "WAIT_KEY_IMG"

    @staticmethod
    def _prompt(code: str, st: dict[str, Any]) -> dict[str, Any]:
        countdown = st.get("countdown_s")
        if countdown is None:
            countdown = {"WAIT_NARRATOR": NARRATOR_MIN_SECONDS, "WAIT_KEY_IMG": KEY_IMG_WINDOW_S,
                         "WAIT_KEY_RELOGIN": KEY_RELOGIN_WINDOW_S}.get(code)
        p: dict[str, Any] = {"kind": code, "text": PROMPTS[code]}
        if countdown is not None:
            p["countdown_s"] = int(countdown)
        if code == "WAIT_NARRATOR":
            p["narrator"] = dict(st.get("narrator") or {})
        return p

    def _set_state(self, aid: str, state: str, code: Optional[str], ls: str, last_code: Optional[str],
                   prompt: Optional[dict[str, Any]] = None) -> Optional[str]:
        """同一相位重复出现不重复发事件(01 靠 `login_session_id` 区分尝试,不靠事件条数)。"""
        key = f"{state}/{code}"
        if key == last_code:
            return last_code
        self._transition(aid, state, state_code=code, login_session_id=ls, prompt=prompt)
        return key

    def _enter_starting(self, aid: str) -> None:
        """00 §8.1 不跳段:从 `created`/`stopped`/`error` 起要先过 `provisioning` → `starting`(中间态 0 秒停留仍发事件)。"""
        row = self._store.get_account_full(aid) or {}
        if row.get("state") in ("starting", "login_required", "logging_in"):
            return
        self._transition(aid, "provisioning", desired_state="running")
        self._transition(aid, "starting")

    # ------------------------------------------------------------------ identified:回填 / 合并 / bind
    async def _identify_and_bind(self, aid: str, ls: str, st: dict[str, Any]) -> str:
        wxid = st.get("wxid")
        if not wxid:
            raise _BindRetry("identified 相位没带 wxid")
        wxid = str(wxid)
        final_id = self._backfill_or_merge(aid, wxid, st.get("nickname"))
        try:
            await self._client.bind(wxid, final_id)
        except WeChatNotReady as e:
            raise _BindRetry(f"bind 时会话代理不在线:{e.reason}") from e
        except WeChatCallFailed as e:
            if e.status == 409:
                # 该 wxid 已绑别的 account_id:以 winagent.db 为准,把本行软删并改用 409 带回的 id(两库唯一一处「从表反向修主表」)
                other = str(e.body.get("account_id") or "")
                if not other:
                    raise _BindRetry("bind 409 未带 account_id") from e
                final_id = self._adopt_winagent_id(aid, wxid, other)
                self._store.insert_audit(kind="system", transport="system", actor="system:wechat_login", action="account.wechat_bind_conflict",
                                         account_id=final_id, result_code="OK", detail={"wxid": wxid, "temp_id": aid, "winagent_id": other},
                                         now_ms=self._clock())
                return final_id
            raise _BindRetry(f"bind 失败 status={e.status}") from e
        return final_id

    def _backfill_or_merge(self, aid: str, wxid: str, nickname: Optional[Any]) -> str:
        """05 §2.4.2.1 第 3 步:按 wxid 命中判定 —— a) 新 wxid ⇒ 回填;b) 命中老档案 ⇒ 合并到老 id(临时行软删、永不复用)。

        幂等:先查 `wxid` 再回填/合并,重复进入不产生第二条墓碑。"""
        now = self._clock()
        row = self._store.get_account_full(aid) or {}
        if row.get("merged_into"):
            return str(row["merged_into"])
        if row.get("wxid") == wxid:
            return aid
        old = self._store.con.execute(
            "SELECT id FROM accounts WHERE wxid=? AND deleted_ms IS NULL AND merged_into IS NULL AND id<>?", (wxid, aid)).fetchone()
        if old is None:
            with self._store._tx() as c:              # handoff:建议收进 store.wechat_backfill_wxid()(与 02 §2.6 规范 SQL 同形)
                c.execute("UPDATE accounts SET wxid=?, self_uid=?, updated_ms=? WHERE id=?", (wxid, wxid, now, aid))
            if nickname:
                self._store.con.execute("UPDATE accounts SET self_nick=?, updated_ms=? WHERE id=?", (str(nickname), now, aid))
            return aid
        return self._merge_into(aid, str(old["id"]), wxid, now)

    def _adopt_winagent_id(self, aid: str, wxid: str, winagent_id: str) -> str:
        """bind 409 分支:`accounts` 无此 id 时按该 id **重建**一行(`state='created'`、`wxid` 已填),再把临时行合并过去。"""
        now = self._clock()
        template = None
        if self._store.get_account_full(winagent_id) is None:
            row = self._store.get_account_full(aid) or {}
            template = {"label": row.get("label") or winagent_id, "quota_mb": row.get("quota_mb") or 1536}
        return self._merge_into(aid, winagent_id, wxid, now, create_template=template)

    def _merge_into(self, temp_id: str, old_id: str, wxid: str, now: int,
                    create_template: Optional[dict[str, Any]] = None) -> str:
        """合并墓碑:临时行 `merged_into=<老 id>` + `deleted_ms`(软删、`account_id` 永不复用),槽位 `pending` 改指老 id。

        ⚠️ 语句顺序不可调,被两条库约束夹住:① ``ux_accounts_wxid`` 是「未软删且未合并」的部分唯一索引 ⇒
        **必须先给临时行落 `deleted_ms`** 才能把同一个 `wxid` 落到老行/重建行上;② ``merged_into REFERENCES accounts(id)``
        ⇒ **老行必须已经存在** 才能写 `merged_into`。故顺序 = 软删临时行 → 重建老行(如缺)→ 写墓碑指针 → 回填老行 → 槽位改指。
        该事务已收进 ``store.wechat_merge_account``(语句顺序同上),本处只转调并补审计。"""
        self._store.wechat_merge_account(temp_id, old_id, wxid, create_template=create_template, now_ms=now)
        self._store.insert_audit(kind="system", transport="system", actor="system:wechat_login", action="account.wechat_merge",
                                 account_id=old_id, result_code="OK", detail={"merged_from": temp_id, "wxid": wxid}, now_ms=now)
        return old_id

    # ------------------------------------------------------------------ 终态
    async def _ready(self, aid: str, ls: str, st: dict[str, Any]) -> str:
        """两钥同轮落盘 + bind 成功:`pending → holder`,再发 `logging_in → running`(不跳段)。"""
        wxid = st.get("wxid")
        row = self._store.get_account_full(aid) or {}
        if row.get("state") != "logging_in":
            self._transition(aid, "logging_in", login_session_id=ls)
        self._slot.promote(aid)
        self._transition(aid, "running", state_code=None, login_session_id=ls, self_uid=str(wxid) if wxid else None)
        return "running"

    async def _key_failed(self, aid: str, ls: str, st: dict[str, Any]) -> str:
        """05 §2.4.7:只拿到一把 / 全 DLL 失败 ⇒ 整轮作废、`degraded(KEY_FAIL)`、**失败即同步释放 pending**、不进 bind。"""
        err = (st.get("key") or {}).get("error")
        await self._slot.release_pending(aid, reason="key_fail", actor="system:wechat_login")
        self._transition(aid, "degraded", state_code="KEY_FAIL",
                         state_reason=f"取钥失败({err or '两钥未同轮落盘'}),读写都拒", login_session_id=ls)
        return "degraded"

    async def _fail_stopped(self, aid: str, ls: str, reason: str) -> str:
        await self._slot.release_pending(aid, reason=reason, actor="system:wechat_login")
        self._transition(aid, "stopped", state_reason=reason, login_session_id=ls, desired_state="stopped")
        return "stopped"

    async def _bind_exhausted(self, aid: str, ls: str) -> str:
        """`bind` 超 `bind_retry_max` ⇒ `error(VAULT_UNAVAILABLE)` 同款环境类错误 + 释放 pending(05 §2.4.2.1 第 4 步)。"""
        await self._slot.release_pending(aid, reason="bind_retry_exhausted", actor="system:wechat_login")
        self._transition(aid, "error", state_code="VAULT_UNAVAILABLE",
                         state_reason=f"绑定 wxid 重试超过 {self._bind_retry_max} 次(WinAgent 不可达)", login_session_id=ls)
        return "error"


class _BindRetry(Exception):
    """回填/bind 的可重试失败(05 §2.4.2.1 第 3/4 步:每 5 s 重试,幂等)。"""


def require_wechat(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    """公共前置:目标必须是未软删的微信账号,否则 404 / ``409 NOT_APPLICABLE``(#17/#18 与 #16b 同一条)。"""
    if row is None or row.get("deleted_ms"):
        raise ApiError(404, "TARGET_NOT_FOUND", "账号不存在")
    if row["channel"] != "wechat":
        raise ApiError(409, "NOT_APPLICABLE", "该端点只对微信通道", reason="not_wechat")
    return row
