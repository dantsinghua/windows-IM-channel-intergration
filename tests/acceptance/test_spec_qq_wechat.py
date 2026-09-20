"""按设计文档写的独立验收用例 —— 第五批:QQ 通道 + 微信通道(Agent 半)。

🔴 断言只依据规格原文,不按实现反推。规格出处(册 §节 / 端点编号 / 裁决号):

**QQ 通道**
- 02 §2.2.3 `adapters` 的 qq 行(补 `get_msg` 读回 / 限速上提到 bus / `on_message` → `store.ingest` / 推型回调只入库不写动作)、
  §2.8.1 每通道入库路径表 QQ 行(ext = `"{原生会话ID}:{message_id}"`、**不走 `ON CONFLICT`** 的「先 SELECT 再插」三分支、
  `#n` 复用后缀、出向行 `source='onebot'`、入库顺序 R4-7/R6-16、`msg_count` R6-51)、§2.8.3 游标表 `onebot_seq:<session>`/`ws_last_event`。
- 06 §2.9.1 每通道入库路径 → 统一 `Message`(原生标识 / `source` / 入库时机 / 字段映射要点:`ext_msg_id`、`session.id`、`kind`、
  `type`、`ts`、`received_at`、`self`)、§2.9.2 去重键组合规则(QQ 两条 ①②、`fingerprint` 序列化 R6-51、`norm()` R6-47、通用兜底)、
  §2.9.4 撤回(标记不删 / 来源 / 发 `message` 事件 `payload.revoked=true`)、§2.12 发送确认对消息库的依赖(QQ 行 `get_msg` →
  `confirmed_by=get_msg`;出向先写 `SENDING`;R6-48 入口校验;R6-39 多候选定序;D-2 的 ①②③)。
- 04 §2.3 H08(napcat WS 心跳 15 s 探、30 s 无心跳 warn→crit、`get_status.online=false` 持续 2 min 转 `login_required`、**不自动重登**)。
- 05 §2.3 QQ 首登(①~⑧ 全序、§2.3.2 二维码不落盘、§2.3.3 免扫失效、§2.3.4 状态序列不跳段 / `qr_max_wait` / 失败码)、
  §2.5.4 QQ 掉线行(检测手段 / 系统动作 / 人如何重登;`ACCOUNT_OFFLINE` 告警口径、`login_remind_interval_s` 提醒)。
- 00 §8.1 Account.state 与 `state_code` 三组、§8.3 结果码表、§8.4 通道原生登录方式。

**微信通道(Agent 半)**
- 02 §2.2.5 `pool` 微信槽位 C-01(`wechat_slots` 六字段与空值口径、行级 claim、三条释放路径、`pending → holder`、R4-8/R5-4 接管)、
  §2.2.3 的 wechat 行(全部经 `/wa/v1/wechat/*`、`degraded(KEY_FAIL)` 读写都拒)、§2.8.1 微信行(ext = `"{talker}:{seq}"`、
  `source='chatlog'`、10 s 未读回 `FAILED`)、§3.4.1 #16b/#17/#18/#19、#69 `GET /resources`、§3.6 #28~#43 调用契约、§7.1 `[wechat]`/`[adapters.wechat]`。
- 05 §2.4 全节:§2.4.2 槽位与 `account_id`(字段表 / 空值口径 / R-04 三条释放 / R4-8+R5-4 接管)、§2.4.2.1 分配顺序 0)~5) 与
  **五条失败回滚**、§2.4.3 讲述人仪式、§2.4.4 完整流程 ①~⑧、§2.4.4a 取钥三段与两钥同轮、§2.4.5 切换 1)~6)、§2.4.7 `KEY_FAIL` 读写门;
  §2.5.4 微信三行(掉线 / chatlog 挂 / 锁屏)。
- 06 §2.9.1 微信行、§2.9.2 微信去重、§2.9.3 游标 `chatlog_seq:<talker>`、§2.9.4 撤回、§2.12 微信读回确认行。
- 00 §7.6 `ResourcePool`、§11.18 [SLOT] ①~④、§8.1/§8.3。

夹具:全假后端(`FakeOneBot` / `FakeWeChatWinAgent` / `FakeContainers` / `FakeAdb` / `FakeVault` / `FakeFs`),绝不碰真 docker/napcat/WinAgent。
HTTP 用例走 starlette `TestClient`;纯服务层用例用 `async def` 直接 await。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from starlette.testclient import TestClient

from qtrade_agent.adapters.base import Account
from qtrade_agent.adapters.qq.onebot import FakeOneBot
from qtrade_agent.adapters.wechat.client import FakeWeChatWinAgent
from qtrade_agent.app import AgentApp
from qtrade_agent.config import AgentConfig
from qtrade_agent.models import Command, CommandOrigin
from qtrade_agent.runtime import FakeAdb, FakeContainers
from qtrade_agent.runtime.runtime import FakeFs
from qtrade_agent.vault_client import FakeVault

try:
    from tests.conftest import Clock
except ImportError:                                   # pytest 以 rootdir 载入 conftest 时的别名
    from conftest import Clock                        # type: ignore

# ────────────────────────────────────────────────────────────────────── 常量(规格自抄)
P = "/api/v1"                                          # 00 §10 前缀
TOK_A = "tok-a"                                        # admin
QQ = "qq03"                                            # 00 §6:account_id = {qd|qq|wx}{NN}
QQ_SEQ = 3
SELF_UIN = "415011447"                                 # qq03 的登录号(conftest 同款)
PEER = "888"                                           # 单聊对端 uin
GROUP = "123456"                                       # 群号
T0_MS = 1_758_240_000_000                              # conftest Clock 的起始时刻(ms)
T0_S = T0_MS // 1000
HOUR_S = 3600                                          # 06 §2.9.2 QQ ②:`|ts 差| > 1 小时` 判 id 复用
ASYNC_TIMEOUT = 20                                     # 单个异步用例的真实时间上限(秒)

# 00 §7.6 / 02 §2.2.5 / 05 §2.4.2 槽位视图字段表
SLOT_KEYS = {"used", "max", "holder", "pending", "pending_expires_at", "pending_login_session_id"}
# 02 §7.1 [wechat](agent.toml) / docs/07 §[wechat]
SLOT_PENDING_TTL_S = 600
SLOT_REAPER_INTERVAL_S = 60
SLOT_ERROR_TAKEOVER_S = 300
# 02 §7.1 [adapters.wechat] / [adapters.qq] / [bus] / 05 §7 [accounts]
WECHAT_POLL_INTERVAL_S = 5
WECHAT_CONFIRM_POLL_MS = 1000
SWITCH_DRAIN_TIMEOUT_S = 60
QQ_HISTORY_BACKFILL = 50
CONFIRM_TIMEOUT_WECHAT_MS = 10000
CONFIRM_TIMEOUT_QQ_MS = 5000
QR_MAX_WAIT_S = 1800
QQ_RECONNECT_GRACE_S = 60
LOGIN_REMIND_INTERVAL_S = 300
# 04 §2.3 H08 行字面
H08_NO_HEARTBEAT_S = 30
H08_OFFLINE_TO_LOGIN_REQUIRED_S = 120


# ────────────────────────────────────────────────────────────────────── 规格自抄的参考实现
def spec_norm(s: Optional[str]) -> str:
    """06 §2.9.2 R6-47 原文照抄的 `norm()`:只做三件事——NFKC、折叠空白、去首尾空白;**不剥 U+0014**。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def spec_fingerprint(account_id: str, session_id: str, sender: Optional[str], text: Optional[str], ts_ms: int,
                     media_sha256s: Optional[list[str]] = None) -> str:
    """06 §2.9.2 R6-51 逐字序列化:`sha256("|".join([account_id, session_id, sender_id or sender_name or "",
    norm(text), ",".join(sorted(media_sha256s)), str(ts_ms // 1000)]).encode("utf-8")).hexdigest()`。"""
    raw = "|".join([account_id, session_id, sender or "", spec_norm(text),
                    ",".join(sorted(media_sha256s or [])), str(ts_ms // 1000)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def spec_ext(native_id: str, message_id: Any) -> str:
    """06 §2.9.2 / 02 §2.8.1:QQ 的 `ext_msg_id = "{原生会话ID}:{message_id}"`。"""
    return f"{native_id}:{message_id}"


def H(tok: Optional[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ────────────────────────────────────────────────────────────────────── 夹具
@dataclass
class Rig:
    agent: AgentApp
    ob: FakeOneBot
    wa: FakeWeChatWinAgent
    clock: Clock
    store: Any

    @property
    def qq(self):
        return self.agent.adapters["qq"]

    @property
    def wx(self):
        return self.agent.adapters["wechat"]

    @property
    def slot(self):
        return self.agent.wechat_slot

    def acct(self, aid: str = QQ, channel: str = "qq") -> Account:
        row = self.store.get_account(aid) or {}
        return Account(id=aid, channel=channel, state=row.get("state", "running"), self_uid=row.get("self_uid"),
                       self_nick=row.get("self_nick"), state_code=row.get("state_code"))


def build(tmp_path, *, cfg: Optional[AgentConfig] = None, clock: Optional[Clock] = None,
          online: bool = True, db_name: str = "agent.db") -> Rig:
    clock = clock or Clock(auto_step_ms=50)
    ob = FakeOneBot(self_id=int(SELF_UIN), clock=clock, online=online)
    wa = FakeWeChatWinAgent()
    agent = AgentApp(cfg or AgentConfig(), db_path=str(tmp_path / db_name), clock=clock,
                     containers=FakeContainers(), adb=FakeAdb(), vault=FakeVault(),
                     winagent_transport=wa, winagent_base_url="http://winagent.fake:17610", winagent_token=wa.token,
                     fs=FakeFs(), wsl_total_mb=11264, boot_poll_s=0,
                     qq_transport_factory=lambda acct: ob).open()
    return Rig(agent=agent, ob=ob, wa=wa, clock=clock, store=agent.store)


@asynccontextmanager
async def qq_rig(tmp_path, **kw):
    """已建连的 QQ 账号(02 §2.2.3:`start()` 只建连/挂 hook,不阻塞到登录)。"""
    r = build(tmp_path, **kw)
    r.store.ensure_account(QQ, "qq", state="running", login_mode="qrcode", self_uid=SELF_UIN)
    r.store.upsert_runtime(QQ, kind=r.store.RUNTIME_KIND["qq"])
    r.store.set_desired_state(QQ, "running")          # 00 §8.1:用户意图列,H08 只盯 `desired_state='running'` 的账号
    try:
        await r.qq.start(r.acct())
        yield r
    finally:
        await r.qq.close()
        r.store.close()


@asynccontextmanager
async def wx_rig(tmp_path, **kw):
    """微信槽位/登录流用的 rig(不建 QQ 连接)。"""
    r = build(tmp_path, **kw)
    try:
        yield r
    finally:
        r.store.close()


def install_tokens(rig: Rig) -> None:
    rig.store.upsert_api_client(app_id="console", name="控制台", level="admin", token=TOK_A)


@contextmanager
def api_ctx(rig: Rig):
    install_tokens(rig)
    with TestClient(rig.agent.create_api(), client=("127.0.0.1", 40000)) as c:
        yield c


# ────────────────────────────────────────────────────────────────────── 小工具
def ev_private(message_id: int, *, text: str = "你好", ts_s: int = T0_S, user_id: str = PEER,
               nickname: str = "张三", message: Optional[list] = None, **kw) -> dict[str, Any]:
    """OneBot v11 私聊 `post_type=message` 事件(06 §2.9.1 QQ 行)。"""
    e = {"post_type": "message", "message_type": "private", "message_id": message_id, "time": ts_s,
         "user_id": int(user_id), "self_id": int(SELF_UIN),
         "message": message if message is not None else [{"type": "text", "data": {"text": text}}],
         "sender": {"user_id": int(user_id), "nickname": nickname}}
    e.update(kw)
    return e


def ev_group(message_id: int, *, text: str = "群消息", ts_s: int = T0_S, group_id: str = GROUP,
             user_id: str = PEER, nickname: str = "张三", message: Optional[list] = None, **kw) -> dict[str, Any]:
    e = {"post_type": "message", "message_type": "group", "message_id": message_id, "time": ts_s,
         "group_id": int(group_id), "user_id": int(user_id), "self_id": int(SELF_UIN),
         "message": message if message is not None else [{"type": "text", "data": {"text": text}}],
         "sender": {"user_id": int(user_id), "nickname": nickname}}
    e.update(kw)
    return e


async def push(rig: Rig, event: dict[str, Any]) -> None:
    """把一条事件推给已建连的客户端,并等它被处理完(推型通道:`on_message` → `store.ingest`,02 §2.2.3)。"""
    sess = rig.qq.session_of(QQ)
    assert sess is not None, "QQ 会话未建立"
    before = sum(sess.events_seen.values())
    rig.ob.push_event(event)
    for _ in range(600):
        await asyncio.sleep(0.005)
        if sum(sess.events_seen.values()) > before:
            return
    raise AssertionError("事件未在时限内被处理")


def rows_of(rig: Rig, account_id: str = QQ, **kw) -> list[dict[str, Any]]:
    return rig.store.list_messages(account_id, **kw)


def row_by_ext(rig: Rig, ext: str, account_id: str = QQ) -> Optional[dict[str, Any]]:
    for r in rows_of(rig, account_id, limit=1000):
        if r["ext_msg_id"] == ext:
            return r
    return None


def sessions_of(rig: Rig, account_id: str = QQ) -> list[dict[str, Any]]:
    return rig.store.list_sessions(account_id=account_id, limit=1000)


def events_of(rig: Rig, event: str, account_id: Optional[str] = None) -> list[dict[str, Any]]:
    return rig.store.list_events(event=event, account_id=account_id)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 一、QQ 入库路径与去重键(02 §2.8.1 QQ 行、06 §2.9.1/§2.9.2)
# ══════════════════════════════════════════════════════════════════════════════════════════════

async def test_qq_ext_msg_id_是会话id加冒号加message_id(tmp_path):
    """06 §2.9.2 / 02 §2.8.1 QQ 行(R6-51 订正):ext = `"{原生会话ID}:{message_id}"`,不是裸 `message_id`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        rows = rows_of(r, limit=10)
        assert len(rows) == 1
        assert rows[0]["ext_msg_id"] == spec_ext(PEER, 11) == "888:11"


async def test_qq_群消息原生id带g下划线前缀(tmp_path):
    """06 §2.9.1 字段映射要点:群加 `g_` 前缀与企点同惯例,防对端 uin 与群号数值相撞。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_group(21))
        rows = rows_of(r, limit=10)
        assert rows[0]["session_id"] == f"{QQ}:g_{GROUP}"
        assert rows[0]["ext_msg_id"] == spec_ext(f"g_{GROUP}", 21)


async def test_qq_session_id_是账号冒号原生id(tmp_path):
    """00 §6 / 06 §2.9.1:`session.id = {account_id}:{原生ID}`;02 §2.8.1 拆分规则 C-11:前 4 字符 = account_id、第 5 字符必为 `:`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        sid = rows_of(r, limit=10)[0]["session_id"]
        assert sid == f"{QQ}:{PEER}"
        assert sid[:4] == QQ and sid[4] == ":"


async def test_qq_kind_按有无group_id判群或私聊(tmp_path):
    """06 §2.9.1:`kind` 取基线 §7.4 枚举 `group|private`;QQ `group_id` 有 → `group`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        await push(r, ev_group(21))
        kinds = {s["native_id"]: s["kind"] for s in sessions_of(r)}
        assert kinds[PEER] == "private"
        assert kinds[f"g_{GROUP}"] == "group"


async def test_qq_source_恒为onebot(tmp_path):
    """02 §2.8.1 QQ 行 `messages.source` 列 = `onebot`;06 §2.9.1 同。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        assert rows_of(r, limit=10)[0]["source"] == "onebot"


async def test_qq_dedup_kind_是native(tmp_path):
    """06 §2.9.2:通道有原生 ID 的一律 `dedup_kind='native'`(只有企点 UI 兜底路线是 `anchor`)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        assert rows_of(r, limit=10)[0]["dedup_kind"] == "native"


async def test_qq_跨群同message_id不撞(tmp_path):
    """02 §2.8.1 QQ 行 / 06 §2.9.2 ①:会话编进 ext,跨群同号不撞 —— 两条都要在库里。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_group(7, group_id="100"))
        await push(r, ev_group(7, group_id="200"))
        exts = sorted(x["ext_msg_id"] for x in rows_of(r, limit=10))
        assert exts == ["g_100:7", "g_200:7"]


async def test_qq_同键一小时内视为同一条_只更新不新增行(tmp_path):
    """06 §2.9.2 QQ ②:同键命中且 `|ts 差| ≤ 1 小时` 视为同一条(更新那一行的 `revoked/text` 补齐),不新增行。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="原文", ts_s=T0_S))
        await push(r, ev_private(11, text="原文", ts_s=T0_S + HOUR_S - 1))
        rows = rows_of(r, limit=10)
        assert len(rows) == 1, "1 小时内同键不得写第二行"
        assert rows[0]["ext_msg_id"] == "888:11"


async def test_qq_同键超一小时判id复用_新行ext追加井号2(tmp_path):
    """06 §2.9.2 QQ ②:`|ts 差| > 1 小时` 视为 ID 复用、当新消息入,新行 `ext = 原 ext + "#" + (族行数 + 1)`(首次复用 `#2`)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="旧", ts_s=T0_S))
        await push(r, ev_private(11, text="新", ts_s=T0_S + HOUR_S + 1))
        exts = sorted(x["ext_msg_id"] for x in rows_of(r, limit=10))
        assert exts == ["888:11", "888:11#2"], "复用 id 的新消息必须另起一行、不得被静默吞掉(99c C-03)"


async def test_qq_再次复用追加井号3(tmp_path):
    """06 §2.9.2 QQ ②:再次复用 `#3`…(族行数 + 1)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="a", ts_s=T0_S))
        await push(r, ev_private(11, text="b", ts_s=T0_S + HOUR_S + 1))
        await push(r, ev_private(11, text="c", ts_s=T0_S + 2 * HOUR_S + 2))
        exts = sorted(x["ext_msg_id"] for x in rows_of(r, limit=10))
        assert exts == ["888:11", "888:11#2", "888:11#3"]


async def test_qq_比ts的对象是同键族里ts最大的一行(tmp_path):
    """06 §2.9.2 QQ ② R6-51:比的对象逐字定死 = **同键族(原行 + 已有 `#n` 行)取 `ts` 最大的一行**。

    造:原行 T0、`#2` 行 T0+2h;再来一条 T0+2h+10s —— 与族内最大(T0+2h)差 10 s ≤ 1h ⇒ 同一条,不得再开 `#3`。
    """
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="a", ts_s=T0_S))
        await push(r, ev_private(11, text="b", ts_s=T0_S + 2 * HOUR_S))
        await push(r, ev_private(11, text="b", ts_s=T0_S + 2 * HOUR_S + 10))
        exts = sorted(x["ext_msg_id"] for x in rows_of(r, limit=10))
        assert exts == ["888:11", "888:11#2"], "与族内 ts 最大的一行只差 10 s,应判同一条"


async def test_qq_复用后两行正文各自保留(tmp_path):
    """02 §2.8.1 QQ 行:不走 `ON CONFLICT`(会把复用 id 的新消息当重复静默吞掉,99c C-03)——两行正文互不覆盖。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="旧文", ts_s=T0_S))
        await push(r, ev_private(11, text="新文", ts_s=T0_S + HOUR_S + 1))
        assert row_by_ext(r, "888:11")["text"] == "旧文"
        assert row_by_ext(r, "888:11#2")["text"] == "新文"


async def test_qq_fingerprint按R6_51逐字序列化(tmp_path):
    """06 §2.9.2 R6-51:`fingerprint = sha256("|".join([...]))`,64 位小写十六进制、无前缀;无媒体则 `media_sha256s` 为空串。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="报价 1.70", ts_s=T0_S))
        row = rows_of(r, limit=10)[0]
        want = spec_fingerprint(QQ, f"{QQ}:{PEER}", PEER, "报价 1.70", T0_S * 1000)
        assert row["fingerprint"] == want
        assert len(row["fingerprint"]) == 64 and row["fingerprint"].islower()


async def test_qq_fingerprint里的norm折叠空白且不剥U0014(tmp_path):
    """06 §2.9.2 R6-47:`norm()` 只做 NFKC + 折叠空白 + strip,**不剥 `U+0014`**(那是 `clean_text` 的事)。"""
    async with qq_rig(tmp_path) as r:
        text = " 报价\t\n1.70 \u0014A "
        await push(r, ev_private(12, text=text, ts_s=T0_S))
        row = rows_of(r, limit=10)[0]
        assert row["fingerprint"] == spec_fingerprint(QQ, f"{QQ}:{PEER}", PEER, text, T0_S * 1000)
        assert "\u0014" in spec_norm(text), "norm() 不得剥掉 U+0014"


async def test_qq_通用兜底的fingerprint每行都存(tmp_path):
    """06 §2.9.2「通用兜底」/ 02 §2.8.1 末:所有通道另算一份 `fingerprint` 存列(不唯一、建索引),供跨源合并与发送确认匹配。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        await push(r, ev_group(21))
        assert all(x["fingerprint"] for x in rows_of(r, limit=10))


async def test_qq_新会话第一条消息先建会话行(tmp_path):
    """02 §2.8.1 R4-7:`store.ingest` 同一事务里**先** upsert `sessions` **再**写 `messages`;漏了第一步 = `FOREIGN KEY constraint failed`。"""
    async with qq_rig(tmp_path) as r:
        assert sessions_of(r) == []
        await push(r, ev_private(11))
        assert [s["native_id"] for s in sessions_of(r)] == [PEER]
        assert rows_of(r, limit=10)[0]["session_id"] == f"{QQ}:{PEER}"


async def test_qq_msg_count只在真新插入时加一(tmp_path):
    """02 §2.8.1 R6-51:`msg_count` **只在该消息真新插入时 +1** —— 重扫撞键 +0。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, ts_s=T0_S))
        await push(r, ev_private(12, ts_s=T0_S + 1))
        await push(r, ev_private(11, ts_s=T0_S))          # 重扫撞键
        assert sessions_of(r)[0]["msg_count"] == 2


async def test_qq_last_msg_ms取max不倒退(tmp_path):
    """02 §2.8.1 R6-51:`last_msg_ms` 取 max 不倒退。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, ts_s=T0_S + 100))
        await push(r, ev_private(12, ts_s=T0_S))          # 更早的一条后到
        assert sessions_of(r)[0]["last_msg_ms"] == (T0_S + 100) * 1000


async def test_qq_type映射图片为image(tmp_path):
    """06 §2.9.1 字段映射 `type`:QQ 段类型 text/image/record(→voice)/file/video/其它→`unknown`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(31, message=[{"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}}]))
        assert rows_of(r, limit=10)[0]["type"] == "image"


async def test_qq_type映射record为voice(tmp_path):
    """06 §2.9.1:`record` → `voice`(拼写按基线 §7.4)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(32, message=[{"type": "record", "data": {"file": "a.silk"}}]))
        assert rows_of(r, limit=10)[0]["type"] == "voice"


async def test_qq_type映射file与video(tmp_path):
    """06 §2.9.1:`file`/`video` 原样。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(33, message=[{"type": "file", "data": {"file": "a.pdf"}}]))
        await push(r, ev_private(34, message=[{"type": "video", "data": {"file": "a.mp4"}}]))
        types = {x["ext_msg_id"]: x["type"] for x in rows_of(r, limit=10)}
        assert types["888:33"] == "file" and types["888:34"] == "video"


async def test_qq_未知段类型落unknown(tmp_path):
    """06 §2.9.1:其它段类型 → `unknown`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(35, message=[{"type": "forward", "data": {"id": "x"}}]))
        assert rows_of(r, limit=10)[0]["type"] == "unknown"


async def test_qq_多段混合取首个非text段类型且正文保留text段(tmp_path):
    """06 §2.9.1:多段混合取**首个非 text 段**的类型,**正文保留 text 段**。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(36, message=[{"type": "text", "data": {"text": "看图:"}},
                                              {"type": "image", "data": {"file": "a.jpg"}},
                                              {"type": "video", "data": {"file": "b.mp4"}}]))
        row = rows_of(r, limit=10)[0]
        assert row["type"] == "image", "取首个非 text 段的类型"
        assert row["text"] == "看图:"


async def test_qq_self判据是user_id等于self_id(tmp_path):
    """06 §2.9.1 字段映射 `self`:QQ `user_id == self_id`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(41, user_id=PEER))
        await push(r, ev_private(42, user_id=SELF_UIN, target_id=int(PEER)))
        flags = {x["ext_msg_id"]: x["is_self"] for x in rows_of(r, limit=10)}
        assert flags[spec_ext(PEER, 41)] == 0
        assert flags[spec_ext(PEER, 42)] == 1


async def test_qq_ts取通道时间而received_ms是入库时刻(tmp_path):
    """06 §2.9.1:`ts` = 通道给的消息时间;`received_at` = 入库时刻。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, ts_s=T0_S - 600))
        row = rows_of(r, limit=10)[0]
        assert row["ts_ms"] == (T0_S - 600) * 1000
        assert row["received_ms"] >= T0_MS > row["ts_ms"]


async def test_qq_入库时机是事件到达即入库(tmp_path):
    """06 §2.9.1 QQ 行「入库时机」列:事件到达即入库(推型,不靠轮询)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        assert r.store.count_messages(QQ) == 1
        await r.qq.poll(r.acct())                      # 02 §2.2.3:推型通道 poll 为空实现
        assert r.store.count_messages(QQ) == 1


async def test_qq_每条入库都发message事件(tmp_path):
    """02 §2.2.3:适配器把通道的推/拉抹平成 `store.ingest(message)` + `events.emit(...)`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        await push(r, ev_private(12))
        assert len(events_of(r, "message", QQ)) == 2


async def test_qq_重扫撞键且内容无变化不重复发事件(tmp_path):
    """02 §2.2.2/§2.8.1 的幂等口径:重扫撞键(既没插入也没改动)不应再产出一条 `message` 事件。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="原文"))
        await push(r, ev_private(11, text="原文"))
        assert len(events_of(r, "message", QQ)) == 1


# ────────────────────────────────────────────────────────────────────── 撤回(06 §2.9.4)
async def test_qq_群撤回标记不删(tmp_path):
    """06 §2.9.4:**标记不删** —— `revoked=true`、`revoked_ms`、`revoked_by`;正文与媒体保留。来源:QQ `notice.group_recall`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_group(21, text="要撤回的话"))
        await push(r, {"post_type": "notice", "notice_type": "group_recall", "group_id": int(GROUP),
                       "user_id": int(PEER), "operator_id": int(PEER), "message_id": 21, "time": T0_S + 5})
        row = row_by_ext(r, spec_ext(f"g_{GROUP}", 21))
        assert row["revoked"] == 1
        assert row["text"] == "要撤回的话", "撤回不删正文"
        assert row["revoked_ms"] == (T0_S + 5) * 1000
        assert row["revoked_by"] == PEER


async def test_qq_私聊撤回同样标记(tmp_path):
    """06 §2.9.4:来源另一支 `notice.friend_recall`(带 `message_id`)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="私聊撤回"))
        await push(r, {"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                       "operator_id": int(PEER), "message_id": 11, "time": T0_S + 5})
        assert row_by_ext(r, "888:11")["revoked"] == 1


async def test_qq_撤回不新增行(tmp_path):
    """06 §2.9.4「标记不删」:撤回是对同一行的更新,不产生第二行。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        await push(r, {"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                       "operator_id": int(PEER), "message_id": 11, "time": T0_S + 5})
        assert r.store.count_messages(QQ) == 1


async def test_qq_撤回发message事件且payload带revoked(tmp_path):
    """06 §2.9.4:撤回发 §7.5 `message` 事件(`payload.revoked=true`)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        await push(r, {"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                       "operator_id": int(PEER), "message_id": 11, "time": T0_S + 5})
        evs = events_of(r, "message", QQ)
        assert len(evs) == 2
        assert json.loads(evs[-1]["payload_json"])["revoked"] is True


async def test_qq_撤回撞到不存在的消息不臆造行(tmp_path):
    """06 §2.9.4 的态度「匹配不到就……不臆造」:库里没有这条消息时,撤回通知不得凭空造一行。"""
    async with qq_rig(tmp_path) as r:
        await push(r, {"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                       "operator_id": int(PEER), "message_id": 999, "time": T0_S + 5})
        assert r.store.count_messages(QQ) == 0


async def test_qq_撤回命中族里ts最大的一行(tmp_path):
    """06 §2.9.2 QQ ② R6-51 的同一口径:`#n` 族里按 `ts` 最大的一行定位 —— 撤回该落在新那条上。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, text="旧", ts_s=T0_S))
        await push(r, ev_private(11, text="新", ts_s=T0_S + HOUR_S + 1))
        await push(r, {"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                       "operator_id": int(PEER), "message_id": 11, "time": T0_S + HOUR_S + 5})
        assert row_by_ext(r, "888:11")["revoked"] == 0
        assert row_by_ext(r, "888:11#2")["revoked"] == 1


# ────────────────────────────────────────────────────────────────────── 游标与补历史(02 §2.8.1 P-13 / §2.8.3、06 §2.9.3)
async def test_qq_游标onebot_seq按会话记message_seq(tmp_path):
    """06 §2.9.3 / 02 §2.8.3:`owner=qq03, kind=onebot_seq:<session>, value_int=message_seq`(补历史用)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, message_seq=1001))
        cur = r.store.cursor_get(QQ, f"onebot_seq:{PEER}")
        assert cur is not None and cur.value_int == 1001


async def test_qq_游标只前进不后退(tmp_path):
    """游标语义是「水位」(02 §2.8.3:`value_int` 存可比较的整数水位),后到的小 seq 不得把水位拉低。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11, message_seq=1005))
        await push(r, ev_private(12, message_seq=1002))
        assert r.store.cursor_get(QQ, f"onebot_seq:{PEER}").value_int == 1005


async def test_qq_ws_last_event只记监控用(tmp_path):
    """06 §2.9.3:`kind=ws_last_event, value_int=last_event_ms`,**只用于监控「多久没事件了」,不做拉取**。"""
    async with qq_rig(tmp_path) as r:
        r.ob.push_heartbeat()
        for _ in range(600):
            await asyncio.sleep(0.005)
            if r.store.cursor_get(QQ, "ws_last_event") is not None:
                break
        cur = r.store.cursor_get(QQ, "ws_last_event")
        assert cur is not None and cur.value_int >= T0_MS
        assert r.store.count_messages(QQ) == 0, "心跳不入消息库"


async def test_qq_重连后补历史默认五十条(tmp_path):
    """02 §2.8.1 QQ 行 P-13:掉线重连后调 `get_group_msg_history`/`get_friend_msg_history` 补 `cursors` 之后的,**默认 50 条**。"""
    async with qq_rig(tmp_path) as r:
        assert r.agent.cfg.qq.history_backfill_on_reconnect == QQ_HISTORY_BACKFILL
        await push(r, ev_private(1, message_seq=1))
        for i in range(2, 12):
            r.ob.record_history(PEER, ev_private(i, text=f"史{i}", ts_s=T0_S + i, message_seq=i))
        n = await asyncio.wait_for(r.qq.backfill(r.acct()), ASYNC_TIMEOUT)
        assert n == 10
        assert r.store.count_messages(QQ) == 11


async def test_qq_补历史只补游标之后的(tmp_path):
    """02 §2.8.1 QQ 行:补的是「`cursors` 之后的」。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(5, message_seq=5))
        for i in (3, 4, 5, 6, 7):
            r.ob.record_history(PEER, ev_private(i, text=f"史{i}", ts_s=T0_S + i, message_seq=i))
        await asyncio.wait_for(r.qq.backfill(r.acct()), ASYNC_TIMEOUT)
        exts = sorted(x["ext_msg_id"] for x in rows_of(r, limit=100))
        assert exts == ["888:5", "888:6", "888:7"], "水位 5 之前的不得回灌"


async def test_qq_补拉行的received_ms明显晚于ts_ms(tmp_path):
    """02 §2.8.1 QQ 行原话:补拉行 `received_ms` 明显晚于 `ts_ms`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(1, message_seq=1))
        r.ob.record_history(PEER, ev_private(2, text="史", ts_s=T0_S - 7200, message_seq=2))
        await asyncio.wait_for(r.qq.backfill(r.acct()), ASYNC_TIMEOUT)
        row = row_by_ext(r, "888:2")
        assert row["received_ms"] - row["ts_ms"] > 3600 * 1000


async def test_qq_补历史后游标推进(tmp_path):
    """02 §2.8.3:游标是水位,补完历史后应推进到本轮见过的最大 `message_seq`。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(1, message_seq=1))
        for i in (2, 3):
            r.ob.record_history(PEER, ev_private(i, ts_s=T0_S + i, message_seq=i))
        await asyncio.wait_for(r.qq.backfill(r.acct()), ASYNC_TIMEOUT)
        assert r.store.cursor_get(QQ, f"onebot_seq:{PEER}").value_int == 3


async def test_qq_补历史撤回由后续revoked更新覆盖(tmp_path):
    """02 §2.8.1 QQ 行:补拉行「撤回由后续 `revoked` 更新覆盖」。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(1, message_seq=1))
        r.ob.record_history(PEER, ev_private(2, text="补的", ts_s=T0_S + 2, message_seq=2))
        await asyncio.wait_for(r.qq.backfill(r.acct()), ASYNC_TIMEOUT)
        await push(r, {"post_type": "notice", "notice_type": "friend_recall", "user_id": int(PEER),
                       "operator_id": int(PEER), "message_id": 2, "time": T0_S + 9})
        assert row_by_ext(r, "888:2")["revoked"] == 1


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 二、QQ 发送与读回确认(06 §2.12 QQ 行、02 §2.2.3/§2.8.1 出向行、00 §8.3)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def send_cmd(text: str = "报价 1.70", *, session: str = f"{QQ}:{PEER}", key: Optional[str] = None, **kw) -> Command:
    return Command(account_id=QQ, op="send_text", args={"session": session, "text": text},
                   idempotency_key=key or f"k-{uuid.uuid4().hex[:10]}", origin=CommandOrigin(), **kw)


async def submit(rig: Rig, cmd: Command, timeout: int = ASYNC_TIMEOUT):
    return await asyncio.wait_for(rig.agent.bus.submit(cmd), timeout)


def out_rows(rig: Rig) -> list[dict[str, Any]]:
    return [x for x in rows_of(rig, limit=1000) if x["dir"] == "out"]


async def test_qq_发送前先落一行SENDING出向(tmp_path):
    """06 §2.12 / 02 §2.8.1 出向行 C-21:`bus` 在**调用通道之前**先写一行 `dir=out`、`state=SENDING`、`ext_msg_id=null`、
    带 `idempotency_key`/`trace_id`。让 `send_private_msg` 失败即可看到这一行确实先写了。"""
    async with qq_rig(tmp_path) as r:
        r.ob.fail_actions["send_private_msg"] = 1200
        res = await submit(r, send_cmd("发不出去", key="k-fail"))
        assert res.ok is False
        rows = out_rows(r)
        assert len(rows) == 1
        assert rows[0]["trace_id"] == res.trace_id
        assert rows[0]["idempotency_key"] == "k-fail"
        assert rows[0]["is_self"] == 1


async def test_qq_出向行source是onebot(tmp_path):
    """02 §2.8.1 出向行 R6-25:由本系统发出的出向行 = `onebot`(QQ)。"""
    async with qq_rig(tmp_path) as r:
        await submit(r, send_cmd("一条"))
        assert out_rows(r)[0]["source"] == "onebot"


async def test_qq_读回确认走get_msg绑定ext并置DELIVERED(tmp_path):
    """06 §2.12 QQ 行:OneBot 返回 `message_id` → `get_msg` 存在 → 绑定该行 `ext_msg_id`,`state=DELIVERED`、`confirmed_by=get_msg`。"""
    async with qq_rig(tmp_path) as r:
        res = await submit(r, send_cmd("确认我"))
        assert res.code == "DELIVERED", res
        row = out_rows(r)[0]
        assert row["state"] == "DELIVERED"
        assert row["confirmed_by"] == "get_msg"
        assert row["ext_msg_id"] == spec_ext(PEER, r.ob.sent[-1]["message_id"])


async def test_qq_读回确认后只有一行出向(tmp_path):
    """06 §2.12「入向轮询撞到自己发的」:读回不得再插第二行 —— 合并进 `SENDING` 那一行。"""
    async with qq_rig(tmp_path) as r:
        await submit(r, send_cmd("只一行"))
        assert len(out_rows(r)) == 1


async def test_qq_get_msg读不到则判待核(tmp_path):
    """06 §2.12 / 00 §8.3:企点/QQ 期限内未读回 ⇒ `SEND_CALLED_BUT_UNCONFIRMED`(「发了未在期限内读回」),出向行 `UNCONFIRMED`。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=2000)) as r:
        r.ob.fail_actions["get_msg"] = 1404                   # 读回查不到
        res = await submit(r, send_cmd("待核"))
        assert res.code == "SEND_CALLED_BUT_UNCONFIRMED", res
        assert out_rows(r)[0]["state"] == "UNCONFIRMED"


async def test_qq_待核码的retryable与needs_human(tmp_path):
    """00 §8.3 结果码表:`SEND_CALLED_BUT_UNCONFIRMED` 的 `retryable=否(转"待核")`、`needs_human=否`。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=2000)) as r:
        r.ob.fail_actions["get_msg"] = 1404
        res = await submit(r, send_cmd("待核2", key="k-wait"))
        assert res.error is not None
        assert res.error.retryable is False and res.error.needs_human is False
        rec = r.store.get_command_result(res.trace_id)
        assert rec["code"] == "SEND_CALLED_BUT_UNCONFIRMED"


async def test_qq_确认窗是bus的confirm_timeout_qq_ms(tmp_path):
    """02 §7.1 `[bus] confirm_timeout_qq_ms = 5000`(docs/07 §[bus] 镜像)。"""
    async with qq_rig(tmp_path) as r:
        assert r.agent.cfg.bus.confirm_timeout_qq_ms == CONFIRM_TIMEOUT_QQ_MS


async def test_qq_出向行文本是发送原文不做改写(tmp_path):
    """06 §2.12 R6-47/R6-48:出向 `SENDING` 行的 `text` = `send_*` 的**发送原文**,入库前不过 `clean_text`、不做任何改写。"""
    async with qq_rig(tmp_path) as r:
        text = "全角Ａ 与 emoji 😀  两个空格"
        await submit(r, send_cmd(text))
        assert out_rows(r)[0]["text"] == text


async def test_qq_发送文本含控制字符直接四百(tmp_path):
    """06 §2.12 R6-48(三通道同一条规则):`send_text.text` 含 `U+0014` ⇒ `INVALID_ARGS`,
    `error.reason='text_has_control_chars'`、`error.details[0].pointer='/text'`,**不写 SENDING 行、不进 `commands`**。"""
    async with qq_rig(tmp_path) as r:
        res = await submit(r, send_cmd("坏\u0014文本", key="k-ctrl"))
        assert res.code == "INVALID_ARGS"
        assert res.error.reason == "text_has_control_chars"
        assert res.error.details[0]["pointer"] == "/text"
        assert out_rows(r) == []
        assert r.store.get_command_result(res.trace_id) is None


async def test_qq_换行与制表符照常放行(tmp_path):
    """06 §2.12 R6-48「放行的」:`\\t`/`\\n`/`\\r`(多行文本)、全角/半角、任何 ≥U+0020 的字符含 Unicode emoji。"""
    async with qq_rig(tmp_path) as r:
        res = await submit(r, send_cmd("第一行\n第二行\t尾"))
        assert res.code == "DELIVERED", res


async def test_qq_登录阶段的发送直接回LOGIN_REQUIRED(tmp_path):
    """06 §2.12 D-2 ①:发送前账号已是 `login_required` ⇒ 总线直接回 `LOGIN_REQUIRED`,**不写 `SENDING` 行**;
    `commands` 留一行 `status='failed'`、`started_ms IS NULL`(02 B-30)。00 §8.1:IM 写类在登录阶段一律拒。"""
    async with qq_rig(tmp_path) as r:
        r.store.set_account_state(QQ, "login_required", state_code="WAIT_QRCODE")
        res = await submit(r, send_cmd("登录期间", key="k-login"))
        assert res.code == "LOGIN_REQUIRED"
        assert res.error.needs_human is True
        assert out_rows(r) == []
        row = r.store.con.execute("SELECT status, started_ms FROM commands WHERE trace_id=?", (res.trace_id,)).fetchone()
        assert row["status"] == "failed" and row["started_ms"] is None


async def test_qq_幂等命中DONE回IDEMPOTENT_REPLAY(tmp_path):
    """02 §2.2.2 幂等三态 C.4.3:命中 `DONE` → 直接返回上次 `command_results`,码 `IDEMPOTENT_REPLAY`,不再真发。"""
    async with qq_rig(tmp_path) as r:
        r1 = await submit(r, send_cmd("幂等", key="k-idem"))
        assert r1.code == "DELIVERED"
        n = len(r.ob.sent)
        r2 = await submit(r, send_cmd("幂等", key="k-idem"))
        assert r2.code == "IDEMPOTENT_REPLAY"
        assert len(r.ob.sent) == n, "幂等重放不得再真发一次"


async def test_qq_同键不同参数回INVALID_ARGS(tmp_path):
    """00 §8.3 / C-10:同键不同参数返回 `INVALID_ARGS`,同样不执行。"""
    async with qq_rig(tmp_path) as r:
        await submit(r, send_cmd("原参数", key="k-x"))
        n = len(r.ob.sent)
        res = await submit(r, send_cmd("换了参数", key="k-x"))
        assert res.code == "INVALID_ARGS"
        assert len(r.ob.sent) == n


async def test_qq_confirm_probe用get_msg复核(tmp_path):
    """02 §2.2.3 协议注释:`confirm_probe`「QQ 可 `get_msg`」——本库已有绑定好的出向行 ⇒ 上次确实已发。"""
    async with qq_rig(tmp_path) as r:
        cmd = send_cmd("复核我", key="k-probe")
        await submit(r, cmd)
        assert await asyncio.wait_for(r.qq.confirm_probe(r.acct(), cmd), ASYNC_TIMEOUT) is True


async def test_qq_confirm_probe没发过回False(tmp_path):
    """同上反向:本库查不到窗内同会话同 `norm(text)` 的已绑定出向行 ⇒ 判「没发出去」。"""
    async with qq_rig(tmp_path) as r:
        assert await asyncio.wait_for(r.qq.confirm_probe(r.acct(), send_cmd("从没发过")), ASYNC_TIMEOUT) is False


async def test_qq_state列只对出向有意义(tmp_path):
    """06 §2.12 末:`messages.state` 只对 `dir=out` 有意义(`SENDING → DELIVERED | UNCONFIRMED | FAILED`);`in` 行恒空。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        await submit(r, send_cmd("出向一条"))
        ins = [x for x in rows_of(r, limit=100) if x["dir"] == "in"]
        assert all(x["state"] in (None, "", "DELIVERED") for x in ins)
        assert out_rows(r)[0]["state"] == "DELIVERED"


async def test_qq_入向撞到自己发的合并进待核行(tmp_path):
    """06 §2.12「入向轮询撞到自己发的」:存在 `dir=out` 且 `state ∈ (SENDING, UNCONFIRMED)` 的行,
    同 `session_id` + `norm(text)` 相等 + `|ts 差| ≤ out_merge_window_s` ⇒ **合并进那一行**(补 ext、置 `DELIVERED`),不插第二行。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=2000)) as r:
        r.ob.fail_actions["get_msg"] = 1404
        res = await submit(r, send_cmd("合并我"))
        assert res.code == "SEND_CALLED_BUT_UNCONFIRMED"
        out = out_rows(r)[0]
        r.ob.fail_actions.pop("get_msg")
        await push(r, {"post_type": "message_sent", "message_type": "private", "message_id": 777,
                       "time": out["ts_ms"] // 1000, "user_id": int(SELF_UIN), "target_id": int(PEER),
                       "self_id": int(SELF_UIN), "message": [{"type": "text", "data": {"text": "合并我"}}]})
        rows = out_rows(r)
        assert len(rows) == 1, "撞到自己发的不得插第二行"
        assert rows[0]["state"] == "DELIVERED"


async def test_qq_非本系统发出的我方消息单独入库(tmp_path):
    """02 §2.8.1 出向行 R6-40 / 06 §2.12:都不命中才按「非本系统发出」入库(人在手机 QQ 上发的,`trace_id IS NULL`)。"""
    async with qq_rig(tmp_path) as r:
        await push(r, {"post_type": "message_sent", "message_type": "private", "message_id": 91, "time": T0_S,
                       "user_id": int(SELF_UIN), "target_id": int(PEER), "self_id": int(SELF_UIN),
                       "message": [{"type": "text", "data": {"text": "我在手机上发的"}}]})
        rows = out_rows(r)
        assert len(rows) == 1 and rows[0]["trace_id"] is None


async def test_qq_发送限速在bus不在适配器(tmp_path):
    """02 §2.2.3 qq 行:`NapCatGateway` 的限速(`MIN_SEND_INTERVAL=1.5 + random*1.5`)**上提到 `bus`**,不在适配器里重写;
    02 §7.1 `[bus] send_min_interval_ms=1500`。两条连发的间隔不得小于该下限。"""
    # 夹具时钟冻结(auto_step_ms=0):bus 用注入时钟算「距上次发出还差多少」,自动步进的假时钟每被读一次就走 50 ms,
    # 会把应睡时长不定量地吃掉(墙钟偶发红的真因);冻结后应睡 = 完整下限,墙钟断言才是确定的。
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        assert r.agent.cfg.bus.send_min_interval_ms == 1500 and r.agent.cfg.bus.send_rand_extra_ms == 1500
        # 限速是真实等待(bus 的 asyncio.sleep),不是可拨时钟的账。
        # 总控订正(R6-59 后):限速量的是「两次**发出**之间」的间隔;原写法从第一条 submit **返回后**起表,
        # 把第一条的读回确认耗时从窗口里扣掉了,属墙钟偶发红(实测 1277 ms)。改为 t0 ≤ 第一次发出、
        # 结束 ≥ 第二次发出,故 (结束 − t0) ≥ 两次发出间隔 ≥ 下限,恒成立且不放松判据。
        t0 = time.monotonic()
        await submit(r, send_cmd("第一条"))
        await submit(r, send_cmd("第二条"))
        assert (time.monotonic() - t0) * 1000 >= r.agent.cfg.bus.send_min_interval_ms
        assert r.qq.__class__.__module__.endswith("adapters.qq.adapter")
        assert not hasattr(r.qq, "_rate_limit"), "限速不得在适配器里重写(02 §2.2.3 qq 行)"


async def test_qq_能力集合含四项(tmp_path):
    """主文档 B.2 能力矩阵在 QQ 通道的落点:`get_state` / `list_sessions` / `read_messages` / `send_text`。"""
    async with qq_rig(tmp_path) as r:
        assert r.qq.capabilities == frozenset({"get_state", "list_sessions", "read_messages", "send_text"})


async def test_qq_截图回NOT_APPLICABLE(tmp_path):
    """00 §8.3:`NOT_APPLICABLE` = 通道无此概念 —— QQ 走 OneBot 协议,napcat 容器没有画面。"""
    async with qq_rig(tmp_path) as r:
        res = await asyncio.wait_for(r.qq.execute(r.acct(), Command(account_id=QQ, op="screenshot", args={})), ASYNC_TIMEOUT)
        assert res.code == "NOT_APPLICABLE"


async def test_qq_推型回调只入库与发事件不做写动作(tmp_path):
    """02 §2.2.3 并发段:推型通道(QQ)的接收回调在连接 task 里跑,**只做入库与发事件,不做写动作**。"""
    async with qq_rig(tmp_path) as r:
        await push(r, ev_private(11))
        assert r.ob.sent == [], "收消息不得触发任何 send_*_msg"


async def test_qq_不做自动重登_适配器没有relogin(tmp_path):
    """02 §2.2.3 D-2:适配器**没有** `relogin()` 这类方法。"""
    async with qq_rig(tmp_path) as r:
        for name in ("relogin", "re_login", "auto_login"):
            assert not hasattr(r.qq, name)


async def test_qq_get_state连不上时沿用库里状态(tmp_path):
    """02 §2.2.3 D-2:运行期检测到掉线只置 `login_required` + 发事件,适配器不擅自改状态;连不上就沿用库里的状态。"""
    async with qq_rig(tmp_path) as r:
        r.ob.fail_actions["get_status"] = 1404
        st = await asyncio.wait_for(r.qq.get_state(r.acct()), ASYNC_TIMEOUT)
        assert st == "running"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 三、QQ 首次启动与首登(05 §2.3 全节、00 §8.1/§8.4)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def create_qq(client, *, label: str = "QQ 号", uin: Optional[str] = None):
    """02 #2 `POST /accounts`;05 §2.3.1 ③:`login.mode=qrcode`、`identity={"qq_uin": 向导填的 QQ 号(可空)}`。"""
    body: dict[str, Any] = {"channel": "qq", "label": label, "login": {"mode": "qrcode"},
                            "idempotency_key": f"k-{uuid.uuid4().hex[:10]}"}
    if uin is not None:
        body["identity"] = {"qq_uin": uin}
    return client.post(f"{P}/accounts", json=body, headers=H(TOK_A))


def state_seq(rig: Rig, aid: str) -> list[str]:
    return [json.loads(e["payload_json"]).get("state") for e in events_of(rig, "account_state", aid)]


async def test_qq_端口按序号推导三段(tmp_path):
    """05 §2.3.1 ② / 02 §2.2.4 C-07:`WS=16100+NN`、`HTTP=16200+NN`、`WebUI=16300+NN`;`port_plan('qq', seq)` 返回 `{ws, http, webui}`(R6-55)。"""
    from qtrade_agent.runtime import port_plan
    plan = port_plan("qq", QQ_SEQ)
    assert plan == {"ws": 16100 + QQ_SEQ, "http": 16200 + QQ_SEQ, "webui": 16300 + QQ_SEQ}


async def test_qq_ws地址绑本地回环(tmp_path):
    """05 §2.3.1 ④:三端口全部绑 `127.0.0.1`;⑤ 轮询 `ws://127.0.0.1:161NN`。"""
    async with qq_rig(tmp_path) as r:
        assert r.qq.ws_url_for(r.acct()) == f"ws://127.0.0.1:{16100 + QQ_SEQ}"


async def test_qq_通道原生登录方式是扫码(tmp_path):
    """00 §8.4:`qq: qrcode`(qq_data 指纹免扫)。"""
    async with wx_rig(tmp_path) as r:
        with api_ctx(r) as c:
            resp = create_qq(c)
            assert resp.status_code == 201, resp.text
            aid = resp.json()["data"]["id"]
            assert r.store.get_account(aid)["login_mode"] == "qrcode"


async def test_qq_免扫命中转running并写身份列(tmp_path):
    """05 §2.3.1 ⑤a/⑦:`get_login_info` 返回 `user_id` ⇒ `logging_in→running`;写 `self_uid`/`self_nick`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=200)) as r:
        with api_ctx(r) as c:
            aid = create_qq(c).json()["data"]["id"]
            c.post(f"{P}/accounts/{aid}/start", headers=H(TOK_A))
            c.portal.call(r.agent.accounts.wait_idle, aid)
            row = r.store.get_account(aid)
            assert row["state"] == "running", row
            assert row["self_uid"] == SELF_UIN
            assert row["self_nick"] == "假QQ"


async def test_qq_免扫未命中转login_required_WAIT_QRCODE(tmp_path):
    """05 §2.3.1 ⑤b / §2.3.3:窗内没登上 ⇒ `login_required(WAIT_QRCODE)`,等人扫码(**不自动重登**,D-2)。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=8000), online=False) as r:
        with api_ctx(r) as c:
            aid = create_qq(c).json()["data"]["id"]
            c.post(f"{P}/accounts/{aid}/start", headers=H(TOK_A))
            c.portal.call(r.agent.accounts.wait_idle, aid)
            row = r.store.get_account(aid)
            assert row["state"] == "login_required"
            assert row["state_code"] == "WAIT_QRCODE"


async def test_qq_等待免扫的窗口是qq_quick_login_wait_s(tmp_path):
    """05 §7 `[accounts] qq_quick_login_wait_s = 20`(§2.3.3:容器起来后 `get_login_info` 长期不返回 user_id 即判失效)。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.accounts.qq_quick_login_wait_s == 20


async def test_qq_扫码等待上限是三十分钟(tmp_path):
    """05 §2.3.4 / §7 `[accounts] qr_max_wait_s = 1800`(30 分钟)。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.accounts.qr_max_wait_s == QR_MAX_WAIT_S


async def test_qq_登录阶段的事件带prompt引导(tmp_path):
    """05 §2.3.1 ⑤b / §2.3.2:`account_state` 事件带 `prompt{kind:WAIT_QRCODE, …}`,控制台据此渲染。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=8000), online=False) as r:
        with api_ctx(r) as c:
            aid = create_qq(c).json()["data"]["id"]
            c.post(f"{P}/accounts/{aid}/start", headers=H(TOK_A))
            c.portal.call(r.agent.accounts.wait_idle, aid)
            last = json.loads(events_of(r, "account_state", aid)[-1]["payload_json"])
            assert (last.get("prompt") or {}).get("kind") == "WAIT_QRCODE"


async def test_qq_二维码不进消息库也不进审计(tmp_path):
    """05 §2.3.2 末:二维码 PNG **不落盘、不进消息库、不进审计**;事件 payload 只在 WS 上飞一次。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=8000), online=False) as r:
        with api_ctx(r) as c:
            aid = create_qq(c).json()["data"]["id"]
            c.post(f"{P}/accounts/{aid}/start", headers=H(TOK_A))
            c.portal.call(r.agent.accounts.wait_idle, aid)
            assert r.store.count_messages(aid) == 0
            blob = json.dumps(r.store.list_audit(), ensure_ascii=False)
            assert "qrcode_png_b64" not in blob


async def test_qq_状态序列不跳段(tmp_path):
    """05 §2.3.4 / 00 §8.1:`created → provisioning → starting → login_required → logging_in → running`;
    **免扫命中时 `login_required`/`logging_in` 各停留 0 秒但仍发事件**(状态序列不跳段,便于控制台/审计一致,05-P5)。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=200)) as r:
        with api_ctx(r) as c:
            aid = create_qq(c).json()["data"]["id"]
            c.post(f"{P}/accounts/{aid}/start", headers=H(TOK_A))
            c.portal.call(r.agent.accounts.wait_idle, aid)
            seq = state_seq(r, aid)
            assert seq[0] == "created" and seq[-1] == "running"
            assert seq == ["created", "provisioning", "starting", "login_required", "logging_in", "running"], seq


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 四、QQ 掉线与 H08(05 §2.5.4 QQ 行、04 §2.3 H08、00 §8.1 ②)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def alert_events(rig: Rig, aid: str) -> list[dict[str, Any]]:
    return [json.loads(e["payload_json"]) for e in events_of(rig, "alert", aid)]


async def heartbeat(rig: Rig, *, online: bool = True) -> None:
    """04 §2.3 H08 判据源:OneBot 正向 WS 的 `meta_event.heartbeat`(默认 5 s 一次),`status.online` 即账号在线与否。"""
    await push(rig, {"post_type": "meta_event", "meta_event_type": "heartbeat", "interval": 5000,
                     "status": {"online": online, "good": True}})


async def test_qq_掉线只置login_required不自动重登(tmp_path):
    """05 §2.5.4 原则(D-2):掉线 ⇒ 只做三件事——置 `login_required` + `state_code`、推 `account_state`、`alert(warn)` 一条;
    **不用保险库账密自动填、不重启容器去快速登录、不自动重扫码**。"""
    async with qq_rig(tmp_path) as r:
        row = r.agent.accounts.mark_offline(QQ, "TOKEN_EXPIRED")
        assert row["state"] == "login_required" and row["state_code"] == "TOKEN_EXPIRED"
        assert not any(a["action"] == "account.login" for a in r.store.list_audit())


async def test_qq_掉线原因码属第二组四码(tmp_path):
    """00 §8.1 ② 掉线原因组 = `KICKED | LOGGED_OUT | TOKEN_EXPIRED | LOGIN_TIMEOUT`(共 4);05 §2.5.4 QQ 行给其中两码。"""
    async with qq_rig(tmp_path) as r:
        for code in ("KICKED", "LOGGED_OUT", "TOKEN_EXPIRED", "LOGIN_TIMEOUT"):
            r.store.set_account_state(QQ, "running", state_code=None)
            assert r.agent.accounts.mark_offline(QQ, code)["state_code"] == code
        with pytest.raises(ValueError):
            r.store.set_account_state(QQ, "running", state_code=None)
            r.agent.accounts.mark_offline(QQ, "RATE_LIMITED")      # R-12:已停产,不是掉线原因


async def test_qq_掉线发ACCOUNT_OFFLINE告警且subject是账号(tmp_path):
    """05 §2.5.4 事件与告警口径:掉线那一刻 `alert(warn)` 一条,码名 `ACCOUNT_OFFLINE`、`subject=account:<id>`、
    `evidence{code, offline_count_1h, last_offline_at}`。"""
    async with qq_rig(tmp_path) as r:
        r.agent.accounts.mark_offline(QQ, "KICKED")
        a = alert_events(r, QQ)[-1]
        assert a["code"] == "ACCOUNT_OFFLINE"
        assert a["subject"] == f"account:{QQ}"
        assert a["severity"] == "warn"
        assert set(a["evidence"]) >= {"code", "offline_count_1h", "last_offline_at"}


async def test_qq_一小时内第三次掉线升crit且不改账号state(tmp_path):
    """05 §2.5.4 R6-56:同一账号 1 小时内第 3 次掉线升级 —— 「升 error」= 告警 `severity` 升 `crit`,**不改账号 `state`**。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        sev = []
        for _ in range(3):
            r.store.set_account_state(QQ, "running", state_code=None)
            row = r.agent.accounts.mark_offline(QQ, "KICKED")
            r.clock.advance(1000)
            sev.append(alert_events(r, QQ)[-1]["severity"])
        assert sev[0] == "warn" and sev[-1] == "crit"
        assert row["state"] == "login_required", "升的是告警级别,不是账号状态"


async def test_qq_掉线计数按一小时滚动(tmp_path):
    """05 §2.5.4:`offline_count_1h` 按 `account_runtime.last_offline_ms` 滚动 1 小时 —— 超过 1 小时重新计数。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.agent.accounts.mark_offline(QQ, "KICKED")
        r.clock.advance(3600_001)
        r.store.set_account_state(QQ, "running", state_code=None)
        r.agent.accounts.mark_offline(QQ, "KICKED")
        assert alert_events(r, QQ)[-1]["evidence"]["offline_count_1h"] == 1


async def test_qq_掉线不动容器(tmp_path):
    """05 §2.5.4 QQ 行:掉线后容器**保持运行、不自动重启**、不自动出码。"""
    async with qq_rig(tmp_path) as r:
        before = dict(getattr(r.agent.runtime, "containers", object()).__dict__) if hasattr(r.agent.runtime, "containers") else None
        r.agent.accounts.mark_offline(QQ, "TOKEN_EXPIRED")
        audits = [a["action"] for a in r.store.list_audit()]
        assert not any(x.startswith("runtime.restart") or x == "account.restart" for x in audits)


async def test_qq_登录期间按间隔重发同trace_id提醒(tmp_path):
    """05 §2.5.4 末:`login_required` 期间 Agent 每 `login_remind_interval_s`(300)重发一条**同 `trace_id`** 的提醒事件(控制台去重)。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.agent.accounts.mark_offline(QQ, "LOGGED_OUT")
        assert await r.agent.accounts.login_remind() == 0, "刚置位不该立刻再提醒"
        r.clock.advance(LOGIN_REMIND_INTERVAL_S * 1000)
        assert await r.agent.accounts.login_remind() == 1
        t1 = events_of(r, "account_state", QQ)[-1]["trace_id"]
        r.clock.advance(LOGIN_REMIND_INTERVAL_S * 1000)
        assert await r.agent.accounts.login_remind() == 1
        assert events_of(r, "account_state", QQ)[-1]["trace_id"] == t1


async def test_qq_提醒不产出告警(tmp_path):
    """05 §2.5.4 末:提醒**不计入告警**、04 健康项不把它当异常。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.agent.accounts.mark_offline(QQ, "LOGGED_OUT")
        n = len(events_of(r, "alert", QQ))
        r.clock.advance(LOGIN_REMIND_INTERVAL_S * 1000)
        await r.agent.accounts.login_remind()
        assert len(events_of(r, "alert", QQ)) == n


async def test_h08_探测周期十五秒(tmp_path):
    """04 §2.3 H08 行「周期」列 = 15 s;Agent 侧 `scheduler` 按此注册。"""
    from qtrade_agent.adapters.qq.config import H08_INTERVAL_S
    async with wx_rig(tmp_path) as r:
        assert H08_INTERVAL_S == 15
        assert r.agent.scheduler.jobs["health_napcat"].interval_s == 15


async def test_h08_告警阈值是三十秒无心跳(tmp_path):
    """04 §2.3 H08 行「阈值」列 = 30 s 无心跳(告警阈值键 04 §7 `[health] napcat_heartbeat_timeout_s=30`;
    与 02 §7.1 `[adapters.qq] heartbeat_timeout_s=40` 的传输层自判重连不同源)。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        assert r.agent.cfg.health.napcat_heartbeat_timeout_s == H08_NO_HEARTBEAT_S
        r.clock.advance(H08_NO_HEARTBEAT_S * 1000 + 1000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert r.agent.qqhealth.last[QQ]["lost"] is True


async def test_h08_无心跳先告warn(tmp_path):
    """04 §2.3 H08「级别」列 `warn→crit`:先 `warn`(30 s 无心跳),`crit` 留给持续离线。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.ob.online = True
        r.clock.advance(H08_NO_HEARTBEAT_S * 1000 + 1000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert alert_events(r, QQ)[-1]["severity"] == "warn"


async def test_h08_自愈动作是重连ws不是重登(tmp_path):
    """04 §2.3 H08「动作」列:重连 WS(**这是我方到 napcat 的本地连接,不是登录**);D-2:不自动重登。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        before = r.ob.connect_calls
        r.clock.advance(H08_NO_HEARTBEAT_S * 1000 + 1000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert r.ob.connect_calls > before, "应重连本地 WS"
        assert r.store.get_account(QQ)["state"] == "running", "重连不改账号登录态"


async def test_h08_离线未满两分钟不转login_required(tmp_path):
    """04 §2.3 H08:`get_status.online=false` **持续 2 min** 才转 `login_required` —— 未满不转。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.ob.online = False
        await heartbeat(r, online=False)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        r.clock.advance(60_000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert r.store.get_account(QQ)["state"] == "running"


async def test_h08_离线满两分钟转login_required并推事件(tmp_path):
    """04 §2.3 H08 / F-08:`get_status.online=false` 持续 2 min 转账号 `login_required` 并推事件,**不自动重登**(D-2)。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.ob.online = False
        await heartbeat(r, online=False)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        r.clock.advance(H08_OFFLINE_TO_LOGIN_REQUIRED_S * 1000 + 1000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        row = r.store.get_account(QQ)
        assert row["state"] == "login_required"
        assert row["state_code"] in ("TOKEN_EXPIRED", "KICKED"), "05 §2.5.4 QQ 行给的两码之一"
        assert state_seq(r, QQ)[-1] == "login_required"


async def test_h08_持续离线升crit(tmp_path):
    """04 §2.3 H08「级别」列 `warn→crit`:持续离线到转 `login_required` 那一刻升 `crit`。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.ob.online = False
        await heartbeat(r, online=False)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        r.clock.advance(H08_OFFLINE_TO_LOGIN_REQUIRED_S * 1000 + 1000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert alert_events(r, QQ)[-1]["severity"] == "crit"


async def test_h08_恢复在线后告警resolved(tmp_path):
    """04 §2.4 告警生命周期(`firing → resolved`):心跳与 `online` 恢复后该告警应 `resolved`。"""
    async with qq_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        r.ob.online = False
        await heartbeat(r, online=False)
        r.clock.advance(H08_NO_HEARTBEAT_S * 1000 + 1000)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert alert_events(r, QQ)[-1]["state"] == "firing"
        r.ob.online = True
        await heartbeat(r, online=True)
        await asyncio.wait_for(r.agent.qqhealth.check(), ASYNC_TIMEOUT)
        assert alert_events(r, QQ)[-1]["state"] == "resolved"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 五、微信槽位(02 §2.2.5 C-01/R-04/R4-8/R5-4、00 §7.6/§11.18 [SLOT]、05 §2.4.2)
# ══════════════════════════════════════════════════════════════════════════════════════════════

WX = "wx01"
WX2 = "wx02"
WXID = "wxid_demo01"                                   # FakeWeChatWinAgent 默认在登的 wxid
LS1 = "ls_01HZZZZZZZZZZZZZZZZZZZZZZ1"                  # 05 §2.4.2:login_session_id 形如 `ls_` + ULID
LS2 = "ls_01HZZZZZZZZZZZZZZZZZZZZZZ2"


def make_wx(rig: Rig, aid: str = WX, *, state: str = "stopped", self_uid: Optional[str] = None) -> None:
    rig.store.ensure_account(aid, "wechat", state=state, login_mode="qrcode", self_uid=self_uid)
    rig.store.upsert_runtime(aid, kind=rig.store.RUNTIME_KIND["wechat"])


def slot_view(rig: Rig) -> dict[str, Any]:
    """#69 `GET /resources` 的 `windows.wechat_slots` 同一份真值(02 §2.2.5:落 `resource_pools(pool='windows')`)。"""
    return rig.agent.pool.snapshot()["pools"]["windows"]["wechat_slots"]


async def test_wx_槽位视图六字段(tmp_path):
    """00 §7.6 / 02 §2.2.5 / 05 §2.4.2 字段表:`{used, max, holder, pending, pending_expires_at, pending_login_session_id}`(R6-6 新增末项)。"""
    async with wx_rig(tmp_path) as r:
        assert set(slot_view(r)) == SLOT_KEYS


async def test_wx_max恒一且used按holder算(tmp_path):
    """05 §2.4.2 字段表:`used / max` —— 恒 `max=1`;`used` = `holder` 非空即 1。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        assert slot_view(r)["max"] == 1 and slot_view(r)["used"] == 0
        assert r.slot.claim(WX, LS1) is True
        assert r.slot.promote(WX) is True
        v = slot_view(r)
        assert v["holder"] == WX and v["used"] == 1


async def test_wx_空值口径三个串空串只有过期时刻是null(tmp_path):
    """05 §2.4.2 🔴 空值口径(02 已落):`pending`、`pending_login_session_id` 无值时一律 `""`;**只有 `pending_expires_at` 是 `null`**;
    `holder` 同为字符串字段(02 §2.2.5:三个字符串字段无值时一律出 `""`)。"""
    async with wx_rig(tmp_path) as r:
        v = slot_view(r)
        assert v["holder"] == "" and v["pending"] == "" and v["pending_login_session_id"] == ""
        assert v["pending_expires_at"] is None


async def test_wx_claim三列同写(tmp_path):
    """02 §2.2.5 R6-6:`slot_pending`、`slot_pending_expires_ms`、`slot_pending_login_session_id` **在同一条 UPDATE 里一起写**。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        assert r.slot.claim(WX, LS1) is True
        v = slot_view(r)
        assert v["pending"] == WX
        assert v["pending_login_session_id"] == LS1
        assert v["pending_expires_at"] is not None


async def test_wx_pending过期时刻按ttl算(tmp_path):
    """02 §2.2.5 / 00 §11.18 ①:`pending_expires_at = now + agent.toml [wechat] slot_pending_ttl_s×1000`(默认 600)。
    库列真值 = `resource_pools.slot_pending_expires_ms`(05 §2.4.2 字段表:INTEGER ms);出参 `pending_expires_at` 在无 pending 时恒 `null`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        assert r.agent.cfg.wechat.slot_pending_ttl_s == SLOT_PENDING_TTL_S
        now = r.clock.now_ms
        r.slot.claim(WX, LS1)
        assert r.store.pool_get("windows")["slot_pending_expires_ms"] == now + SLOT_PENDING_TTL_S * 1000
        assert slot_view(r)["pending_expires_at"] is not None


async def test_wx_pending尝试id不是账号id(tmp_path):
    """02 §2.2.5 R6-6:`slot_pending_login_session_id` 的值 = 本次登录尝试的 `login_session_id`,**不是账号 id**。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        v = slot_view(r)
        assert v["pending_login_session_id"] == LS1 != v["pending"]


async def test_wx_holder非空时claim抢不到(tmp_path):
    """02 §2.2.5:claim 的条件是 `WHERE pool='windows' AND slot_holder='' AND slot_pending=''`,按 `rowcount==1` 判抢到。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        assert r.slot.claim(WX2, LS2) is False


async def test_wx_已有pending时claim抢不到(tmp_path):
    """同上条件的另一半:`slot_pending=''` —— 正在登录中时别人抢不到。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        make_wx(r, WX2)
        assert r.slot.claim(WX, LS1) is True
        assert r.slot.claim(WX2, LS2) is False


async def test_wx_成功才把pending转holder且三列一起清(tmp_path):
    """02 §2.2.5 / 00 §11.18 ④:**成功**才把 `pending` 转 `holder`;`holder` 不记 `login_session_id`(尝试已结束,三列随 pending 一起清)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        assert r.slot.promote(WX) is True
        v = slot_view(r)
        assert v["holder"] == WX
        assert v["pending"] == "" and v["pending_login_session_id"] == "" and v["pending_expires_at"] is None


async def test_wx_失败即释放三列一起清(tmp_path):
    """02 §2.2.5 ① / 00 §11.18 ②「失败即释放」:绑定/切换/登录流任一步失败或被取消 ⇒ **同步**清三列(不等 TTL)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        assert await r.slot.release_pending(WX, reason="取钥失败") == 1
        v = slot_view(r)
        assert v["pending"] == "" and v["pending_login_session_id"] == "" and v["pending_expires_at"] is None


async def test_wx_释放时同步回收中间产物(tmp_path):
    """02 §2.2.5 ① / 00 §11.18 ③:释放时必须**同时**回收该次绑定的中间产物(临时 hook 进程、半截密钥、`accounts/<id>/tmp`),
    复用 `runtime._purge_ephemeral`;否则下次绑定读到脏状态。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        seen: list[str] = []

        async def fake_purge(row):
            seen.append(row["id"])
        r.slot._purge = fake_purge
        r.slot.claim(WX, LS1)
        await r.slot.release_pending(WX, reason="扫码超时")
        assert seen == [WX]


async def test_wx_reaper到期释放并记审计(tmp_path):
    """02 §2.2.5 ② / 00 §11.18 ②:TTL 到期由 reaper 释放三列,对被释放的目标发 `account_state` 事件 + 记审计 `slot_pending_expired`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        r.clock.advance(SLOT_PENDING_TTL_S * 1000 + 1)
        assert await r.slot.reap() == [WX]
        assert slot_view(r)["pending"] == ""
        assert [a["action"] for a in r.store.list_audit(action="slot_pending_expired")] == ["slot_pending_expired"]
        assert events_of(r, "account_state", WX), "应对被释放的目标发 account_state"


async def test_wx_reaper未到期不释放(tmp_path):
    """02 §2.2.5 ②:判据是 `slot_pending_expires_ms < :now` —— 未到点一列不动。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        r.clock.advance(SLOT_PENDING_TTL_S * 1000 - 1000)
        assert await r.slot.reap() == []
        assert slot_view(r)["pending"] == WX


async def test_wx_reaper幂等(tmp_path):
    """02 §2.2.5 ②「幂等」:多实例/重入不会重复释放(条件一旦置 NULL 就不再命中)。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        r.clock.advance(SLOT_PENDING_TTL_S * 1000 + 1)
        assert await r.slot.reap() == [WX]
        assert await r.slot.reap() == []
        assert len(r.store.list_audit(action="slot_pending_expired")) == 1


async def test_wx_reaper周期六十秒(tmp_path):
    """02 §2.2.5 ② / 00 §11.18 ①:`scheduler` 注册 `wechat_slot_reaper` 每 **60 s** 扫一轮(`agent.toml [wechat] slot_reaper_interval_s=60`)。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.wechat.slot_reaper_interval_s == SLOT_REAPER_INTERVAL_S
        assert r.agent.scheduler.jobs["wechat_slot_reaper"].interval_s == SLOT_REAPER_INTERVAL_S


async def test_wx_reaper释放后pending剩余秒数为空(tmp_path):
    """05 §2.4.2 R-04:`P-ACCT` 显示 pending 剩余秒数;无 pending 时没有剩余秒数可显示。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        assert r.slot.pending_remaining_s() == SLOT_PENDING_TTL_S
        r.clock.advance(SLOT_PENDING_TTL_S * 1000 + 1)
        await r.slot.reap()
        assert r.slot.pending_remaining_s() is None


async def test_wx_holder释放只走停止路径(tmp_path):
    """02 §2.2.5 / 00 §11.18 ④:`holder` 的释放**只走**正常 `stopping → stopped`(#6/#10),不受 TTL 约束。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.clock.advance(SLOT_PENDING_TTL_S * 1000 * 10)
        assert await r.slot.reap() == [], "reaper 不碰 holder"
        assert slot_view(r)["holder"] == WX
        assert r.slot.release_holder(WX) == 1
        assert slot_view(r)["holder"] == ""


async def test_wx_error的holder仍占槽(tmp_path):
    """02 §2.2.5 R4-8 / 05 §2.4.2:`holder` 跑成 `error` 时 `slot_holder` **仍指向它、不自动释放**(它代表真实登录态)。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL", state_reason="取钥失败")
        r.clock.advance(SLOT_ERROR_TAKEOVER_S * 1000 * 5)
        await r.slot.reap()
        assert slot_view(r)["holder"] == WX


async def test_wx_can_add三条件(tmp_path):
    """02 §2.2.5 算法:`can_add(wechat) = windows.wechat_enabled AND slot.holder == '' AND slot.pending == ''
    and windows.total_mb - windows.reserved_mb >= quota.wechat`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.agent.pool.set_windows(total_mb=32768, wechat_enabled=True)
        ok, _reason, _alts = r.agent.pool.can_add("wechat")
        assert ok is True
        r.slot.claim(WX, LS1)
        assert r.agent.pool.can_add("wechat")[0] is False, "pending 非空即不可新增"
        r.slot.promote(WX)
        assert r.agent.pool.can_add("wechat")[0] is False, "holder 非空即不可新增"


async def test_wx_模块未启用时不可新增(tmp_path):
    """02 §2.2.5:`can_add(wechat)` 的第一条 = `windows.wechat_enabled`(Agent 从 `GET /wa/v1/health` 拿的 `winagent.toml [wechat] enabled` 快照,C-43)。"""
    async with wx_rig(tmp_path) as r:
        r.agent.pool.set_windows(total_mb=32768, wechat_enabled=False)
        assert r.agent.pool.can_add("wechat")[0] is False


async def test_wx_windows池快照未到手时状态未知(tmp_path):
    """02 §2.2.5 R6-54:`GET /resources` 的 `windows` 出参带 `status:'ok'|'unknown'`(WinAgent 快照到手前 `unknown`,此时 `can_add.wechat=0`)。"""
    async with wx_rig(tmp_path) as r:
        win = r.agent.pool.snapshot()["pools"]["windows"]
        assert win["status"] == "unknown"
        assert r.agent.pool.snapshot()["can_add"]["wechat"] == 0


async def test_wx_槽位被占时新增账号回409并给切换提示(tmp_path):
    """02 §3.4.1 #2:微信槽位被占 → `409 RESOURCE_EXHAUSTED`,`error.hint_actions=["wechat_switch"]`(C-01;不再是「已存在」);
    §2.2.5 R6-54:`alternatives[].kind='wechat_switch'` 带 `holder`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.agent.pool.set_windows(total_mb=32768, wechat_enabled=True)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        with api_ctx(r) as c:
            resp = c.post(f"{P}/accounts", json={"channel": "wechat", "label": "新微信", "login": {"mode": "qrcode"},
                                                 "idempotency_key": "k-wx-occupied"}, headers=H(TOK_A))
            assert resp.status_code == 409, resp.text
            err = resp.json()["error"]
            assert resp.json()["code"] == "RESOURCE_EXHAUSTED"
            assert "wechat_switch" in (err.get("hint_actions") or [x.get("kind") for x in err.get("alternatives") or []])


async def test_wx_resources端点出槽位视图(tmp_path):
    """02 §3.4.1 #69 `GET /resources`:出参 `windows.wechat_slots` 六字段,空值口径与 #16b 一致。"""
    async with wx_rig(tmp_path) as r:
        with api_ctx(r) as c:
            resp = c.get(f"{P}/resources", headers=H(TOK_A))
            assert resp.status_code == 200, resp.text
            slots = resp.json()["pools"]["windows"]["wechat_slots"]
            assert set(slots) == SLOT_KEYS
            assert slots["pending_login_session_id"] == "" and slots["pending_expires_at"] is None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 六、取消登录 #16b 与切换/接管 #17/#18(02 §3.4.1、§2.2.5 R4-8/R5-4、05 §2.4.2/§2.4.5)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def cancel(client, aid: str, login_session_id: Optional[str] = None):
    body = {} if login_session_id is None else {"login_session_id": login_session_id}
    return client.post(f"{P}/accounts/{aid}/login/cancel", json=body, headers=H(TOK_A))


async def test_wx_取消绑定命中当前尝试(tmp_path):
    """02 #16b / 05 §2.4.2 R-04 ③:`login_session_id == 当前 slot_pending_login_session_id` → 走「用户手动释放」,
    三列一起清 + 同步回收中间产物,回 `200 {cancelled:true, stale:false}`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.slot.claim(WX, LS1)
        with api_ctx(r) as c:
            resp = cancel(c, WX, LS1)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["cancelled"] is True and body["stale"] is False
            assert slot_view(r)["pending"] == "" and slot_view(r)["pending_login_session_id"] == ""


async def test_wx_取消绑定命中旧尝试时幂等no_op(tmp_path):
    """02 #16b 🔴:`≠ 当前值` → **幂等 no-op 回 `200 {cancelled:false, stale:true, current_login_session_id:"<当前那次>"}`,
    槽位一列不动、绝不误杀新尝试**(不是 404/409——用户点了个过期按钮不是错误)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        r.slot.claim(WX, LS2)                       # 后台已重开新一次尝试
        with api_ctx(r) as c:
            body = cancel(c, WX, LS1).json()        # 用户点的是上一次的按钮
            assert body["cancelled"] is False and body["stale"] is True
            assert body["current_login_session_id"] == LS2
            assert slot_view(r)["pending"] == WX and slot_view(r)["pending_login_session_id"] == LS2


async def test_wx_槽位已彻底释放时回空串(tmp_path):
    """02 #16b:「**或槽位已彻底释放**」同样走 stale 分支;空值口径与 #69 一致 —— `current_login_session_id` 出 `""` 空串、不是 `null`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        with api_ctx(r) as c:
            body = cancel(c, WX, LS1).json()
            assert body["stale"] is True
            assert body["current_login_session_id"] == ""


async def test_wx_取消后账号停成stopped并记审计(tmp_path):
    """02 #16b 微信分支:释放 pending 三列 + `_purge_ephemeral` + 账号 `stopped` + 审计 `slot_pending_cancelled`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="starting")
        r.slot.claim(WX, LS1)
        with api_ctx(r) as c:
            assert cancel(c, WX, LS1).json()["cancelled"] is True
        assert r.store.get_account(WX)["state"] == "stopped"
        assert r.store.list_audit(action="slot_pending_cancelled")


async def test_wx_切换只对微信通道(tmp_path):
    """02 #17:非微信 `NOT_APPLICABLE`(05 §2.4.5 切换是微信单槽的事)。"""
    async with qq_rig(tmp_path) as r:
        with api_ctx(r) as c:
            resp = c.post(f"{P}/accounts/{QQ}/switch", json={}, headers=H(TOK_A))
            assert resp.status_code == 409
            assert resp.json()["code"] == "NOT_APPLICABLE"


async def test_wx_接管阈值是三百秒(tmp_path):
    """02 §7.1 `[wechat] slot_error_takeover_s = 300`(R4-8/R5-4 接管阈值,判据列 = `account_runtime.error_since_ms`)。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.wechat.slot_error_takeover_s == SLOT_ERROR_TAKEOVER_S


async def test_wx_error_since_ms迁入写迁出清(tmp_path):
    """02 §2.2.5 R5-4 / §2.6:`error_since_ms` 由账号状态机维护 —— **迁入 `error` 时写当前 ms、迁出 `error` 时清 NULL**。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        assert (r.store.get_runtime(WX) or {}).get("error_since_ms") is None
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        assert r.store.get_runtime(WX)["error_since_ms"] == r.clock.now_ms
        r.agent.accounts.transition(WX, "running", state_code=None)
        assert r.store.get_runtime(WX)["error_since_ms"] is None


async def test_wx_error_since_ms为空则恒不可接管(tmp_path):
    """02 §2.2.5 R5-4:`NULL` = 当前不在 `error`,**判定恒为不可接管**。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        assert r.slot.takeover_check() == (False, None)


async def test_wx_未达阈值不可接管(tmp_path):
    """02 §2.2.5 R5-4:可接管 ⟺ `error_since_ms IS NOT NULL AND (:now_ms - error_since_ms) >= slot_error_takeover_s*1000`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        r.clock.advance(SLOT_ERROR_TAKEOVER_S * 1000 - 1000)
        ok, seconds = r.slot.takeover_check()
        assert ok is False and seconds == SLOT_ERROR_TAKEOVER_S - 1


async def test_wx_达阈值后可接管(tmp_path):
    """02 §2.2.5 R5-4:达阈值即「允许人接管」的前提(不是自动接管);`已故障 N 秒 = (:now_ms - error_since_ms) // 1000`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        r.clock.advance(SLOT_ERROR_TAKEOVER_S * 1000)
        ok, seconds = r.slot.takeover_check()
        assert ok is True and seconds == SLOT_ERROR_TAKEOVER_S


async def test_wx_不带confirm命中故障holder一律409(tmp_path):
    """02 §2.2.5 R5-4 / #17:**不带 `confirm:true` 则接管分支永不进入、只回 `409`**,
    `error.reason='slot_held_by_error'`、`hint_actions=["wechat_switch"]`、message 带「已故障 N 秒」。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        r.clock.advance(SLOT_ERROR_TAKEOVER_S * 1000 + 5000)
        with api_ctx(r) as c:
            resp = c.post(f"{P}/accounts/{WX2}/switch", json={}, headers=H(TOK_A))
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["code"] == "RESOURCE_EXHAUSTED"
            assert body["error"]["reason"] == "slot_held_by_error"
            assert body["error"]["hint_actions"] == ["wechat_switch"]
            assert "已故障" in body["error"]["message"]


async def test_wx_未达阈值也回409(tmp_path):
    """02 §2.2.5 R5-4:未到阈值(或 `error_since_ms IS NULL`)一律复用既有 `409 RESOURCE_EXHAUSTED` 信封。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        with api_ctx(r) as c:
            resp = c.post(f"{P}/accounts/{WX2}/switch", json={"confirm": True}, headers=H(TOK_A))
            assert resp.status_code == 409 and resp.json()["error"]["reason"] == "slot_held_by_error"


async def test_wx_确认后接管先停故障号再抢槽(tmp_path):
    """02 §2.2.5 R5-4:收到 `confirm:true` 且判定通过时,后端在一个事务里**先把故障 `holder` 走 `→ stopped`(释放 holder)、再让本次 switch 抢占**;
    claim 的 SQL 抢占条件不变(仍 `WHERE slot_holder=''`)。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        r.clock.advance(SLOT_ERROR_TAKEOVER_S * 1000 + 1000)
        stopped: list[str] = []
        begun: list[tuple[str, str]] = []

        async def stop_account(aid: str):
            stopped.append(aid)
            r.slot.release_holder(aid)
            r.agent.accounts.transition(aid, "stopped", state_reason="已被接管")

        async def begin_login(aid: str, ls: str):
            begun.append((aid, ls))

        res = await asyncio.wait_for(r.slot.switch(WX2, confirm=True, stop_account=stop_account, begin_login=begin_login), ASYNC_TIMEOUT)
        assert stopped == [WX]
        assert r.store.get_account(WX)["state"] == "stopped"
        assert res["holder_before"] == WX and res["target"] == WX2
        assert slot_view(r)["pending"] == WX2
        assert begun and begun[0][0] == WX2


async def test_wx_接管记审计(tmp_path):
    """02 §2.2.5 R5-4 / 05 §2.4.2:接管「并记审计」。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        make_wx(r, state="running")
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.agent.accounts.transition(WX, "error", state_code="KEY_FAIL")
        r.clock.advance(SLOT_ERROR_TAKEOVER_S * 1000 + 1000)

        async def stop_account(aid: str):
            r.slot.release_holder(aid)
            r.agent.accounts.transition(aid, "stopped")

        await asyncio.wait_for(r.slot.switch(WX2, confirm=True, stop_account=stop_account), ASYNC_TIMEOUT)
        actions = [a["action"] for a in r.store.list_audit()]
        assert any("switch" in a for a in actions), actions


async def test_wx_切换中再调回409(tmp_path):
    """02 #17:切换中再调 `409`(C-13)—— 已有 `pending` 时不接受第二次切换。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r)
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        with api_ctx(r) as c:
            resp = c.post(f"{P}/accounts/{WX2}/switch", json={}, headers=H(TOK_A))
            assert resp.status_code == 409, resp.text


async def test_wx_新号切换立刻建账号行且pending是wxNN(tmp_path):
    """02 #18 R-23(基线 §11.23 [WXID]):`POST /accounts/switch {target:"new"}` **立刻建 `accounts` 行**
    (`id=wxNN`、`state='created'`、`wxid=NULL`)再起登录会话;槽位 `pending=wxNN`(**不再是 `"new"`**)。"""
    async with wx_rig(tmp_path) as r:
        r.agent.pool.set_windows(total_mb=32768, wechat_enabled=True)
        r.wa.phase = "qrcode"                       # 停在「等人扫码」,以便观察登录尚未确立时的槽位形态
        with api_ctx(r) as c:
            resp = c.post(f"{P}/accounts/switch", json={"target": "new"}, headers=H(TOK_A))
            assert resp.status_code in (200, 202), resp.text
            target = resp.json().get("target") or resp.json().get("data", {}).get("target")
            assert target and target.startswith("wx")
            row = r.store.get_account(target)
            assert row["wxid"] is None
            assert slot_view(r)["pending"] == target != "new"
            cancel(c, target, slot_view(r)["pending_login_session_id"])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 七、微信登录流(05 §2.4.4 / §2.4.4a / §2.4.2.1、02 §3.6 #31/#33/#33c)
# ══════════════════════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def login_rig(tmp_path, *, script: list[dict[str, Any]], aid: str = WX, state: str = "created", **kw):
    """驱动 `WechatLoginFlow`:`phases_script` 每次 #33 弹一帧(02 §3.6 #33 的 `phase` 枚举以 05 为准,R3-3)。"""
    async with wx_rig(tmp_path, **kw) as r:
        make_wx(r, aid, state=state)
        r.wa.phases_script = list(script)
        r.agent.wechat_login._sleep = lambda s: asyncio.sleep(0)     # 轮询节拍在测试里不真实等待
        r.slot.claim(aid, LS1)
        yield r


def last_state(rig: Rig, aid: str = WX) -> dict[str, Any]:
    return json.loads(events_of(rig, "account_state", aid)[-1]["payload_json"])


def state_with_code(rig: Rig, aid: str, code: str) -> Optional[dict[str, Any]]:
    for e in events_of(rig, "account_state", aid):
        p = json.loads(e["payload_json"])
        if p.get("state_code") == code:
            return p
    return None


async def test_wx_登录流经31起登录会话(tmp_path):
    """05 §2.4.4 ③ / 02 §3.6 #31:Agent 调 `POST /wa/v1/wechat/login/start {account_id, login_session_id}` 拉起微信 + chatlog + 试钥 + 讲述人仪式。"""
    async with login_rig(tmp_path, script=[{"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        starts = [c for c in r.wa.calls if c[1].endswith("/wechat/login/start")]
        assert starts and starts[0][2]["login_session_id"] == LS1 and starts[0][2]["account_id"] == WX


async def test_wx_相位qrcode映射WAIT_QRCODE(tmp_path):
    """05 §2.4.4 ③:人扫码期间 `state=login_required(WAIT_QRCODE)`;00 §8.1 ① 等人组含 `WAIT_QRCODE`。"""
    async with login_rig(tmp_path, script=[{"phase": "qrcode"}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        p = state_with_code(r, WX, "WAIT_QRCODE")
        assert p is not None and p["state"] == "login_required"
        assert (p.get("prompt") or {}).get("kind") == "WAIT_QRCODE"


async def test_wx_相位narrator映射WAIT_NARRATOR并带倒计时(tmp_path):
    """05 §2.4.3 ④:状态 `login_required(WAIT_NARRATOR)`,`prompt` 带倒计时(`narrator_min_seconds` 默认 300)。"""
    async with login_rig(tmp_path, script=[{"phase": "narrator"}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        p = state_with_code(r, WX, "WAIT_NARRATOR")
        assert p is not None and p["state"] == "login_required"
        assert (p.get("prompt") or {}).get("countdown_s") == 300


async def test_wx_取钥第一段是等打开图片(tmp_path):
    """05 §2.4.4a b) / 00 §8.1 ① N-12:`WAIT_KEY_IMG` = 等用户**打开任意图片**取 `img_key`,窗口约 60 s。"""
    async with login_rig(tmp_path, script=[{"phase": "keytry", "key": {"stage": "img"}}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        p = state_with_code(r, WX, "WAIT_KEY_IMG")
        assert p is not None and p["state"] == "login_required"
        assert (p.get("prompt") or {}).get("countdown_s") == 60


async def test_wx_取钥第二段是等退出重登(tmp_path):
    """05 §2.4.4a c) / 00 §8.1 ①:`WAIT_KEY_RELOGIN` = 等用户**退出微信重新登录**取 `data_key`,窗口约 30 s。"""
    async with login_rig(tmp_path, script=[{"phase": "keytry", "key": {"stage": "relogin"}}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        p = state_with_code(r, WX, "WAIT_KEY_RELOGIN")
        assert p is not None and (p.get("prompt") or {}).get("countdown_s") == 30


async def test_wx_取钥两码独立不合成(tmp_path):
    """05 §2.4.4a 🔴 / 00 §8.1 ①:两者**必须是独立的码、不许合成一个「等取钥」**——指示完全不同(打开图片 vs 退出重登)。"""
    async with login_rig(tmp_path, script=[{"phase": "keytry", "key": {"stage": "img"}},
                                           {"phase": "keytry", "key": {"stage": "relogin"}},
                                           {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        a, b = state_with_code(r, WX, "WAIT_KEY_IMG"), state_with_code(r, WX, "WAIT_KEY_RELOGIN")
        assert a is not None and b is not None
        assert (a["prompt"]["text"], a["prompt"]["countdown_s"]) != (b["prompt"]["text"], b["prompt"]["countdown_s"])


async def test_wx_identified相位调bind绑wxid(tmp_path):
    """02 §3.6 #33 ⚠️ / #33c、05 §2.4.2.1 第 1)/4) 步:`identified` 是枢纽相位 —— Agent 见此调
    `POST /wa/v1/wechat/bind {wxid, account_id}` 把 `wxid` 绑到 `wxNN`。"""
    async with login_rig(tmp_path, script=[{"phase": "identified", "wxid": WXID}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        binds = [c for c in r.wa.calls if c[1].endswith("/wechat/bind")]
        assert binds and binds[0][2] == {"wxid": WXID, "account_id": WX}


async def test_wx_新wxid回填self_uid(tmp_path):
    """05 §2.4.2.1 第 3) a):新 wxid(`accounts` 无同 `self_uid` 的其它行)⇒ 回填 `self_uid=wxid`。"""
    async with login_rig(tmp_path, script=[{"phase": "identified", "wxid": WXID}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        assert r.store.get_account(WX)["self_uid"] == WXID


async def test_wx_ready转running并把pending转holder(tmp_path):
    """05 §2.4.2.1 第 5) 步:`created→…→running` 事件序列后 `wechat_slots.holder=最终 id`、`pending=""`、`pending_expires_at` 清空。"""
    async with login_rig(tmp_path, script=[{"phase": "identified", "wxid": WXID}, {"phase": "ready"}]) as r:
        assert await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT) == "running"
        assert r.store.get_account(WX)["state"] == "running"
        v = slot_view(r)
        assert v["holder"] == WX and v["pending"] == "" and v["pending_expires_at"] is None


async def test_wx_登录状态序列不跳段(tmp_path):
    """00 §8.1 / 05 §2.4.2.1 第 5) 步:「中间态 0 秒停留,基线 §8.1 不跳段」——从 `created` 起要先过 `provisioning` → `starting`。"""
    async with login_rig(tmp_path, script=[{"phase": "identified", "wxid": WXID}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        seq = state_seq(r, WX)
        assert seq[:3] == ["provisioning", "starting", "logging_in"] or seq[:2] == ["provisioning", "starting"], seq
        assert seq[-1] == "running"


async def test_wx_取钥失败转degraded_KEY_FAIL(tmp_path):
    """05 §2.4.4 ⑧ / §2.4.2.1 第 2 步:只拿到一把或全 DLL 失败 ⇒ 整轮作废、`degraded(KEY_FAIL)`。"""
    async with login_rig(tmp_path, script=[{"phase": "key_failed", "key": {"ok": False, "error": "只拿到 data_key"}}]) as r:
        assert await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT) == "degraded"
        row = r.store.get_account(WX)
        assert row["state"] == "degraded" and row["state_code"] == "KEY_FAIL"


async def test_wx_取钥失败同步释放pending(tmp_path):
    """05 §2.4.2.1 失败回滚表第 2 步:取钥失败 ⇒ **失败即同步释放 `pending`** + 回收中间产物,**不进 bind**。"""
    async with login_rig(tmp_path, script=[{"phase": "key_failed", "key": {"ok": False}}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        v = slot_view(r)
        assert v["pending"] == "" and v["pending_login_session_id"] == ""
        assert not [c for c in r.wa.calls if c[1].endswith("/wechat/bind")], "不进 bind"


async def test_wx_bind返回409时归并到老档案(tmp_path):
    """05 §2.4.2.1 失败回滚表第 4 步 409:以 **winagent.db 已有的 `account_id` 为准**,把刚分配的 `wxNN` 行标
    `merged_into=<老 id>`+`deleted_ms`(软删、永不复用),改用 409 里带回的 id。"""
    async with login_rig(tmp_path, script=[{"phase": "identified", "wxid": WXID}, {"phase": "ready"}]) as r:
        make_wx(r, WX2, state="stopped", self_uid=WXID)
        r.wa.bind_conflict = WX2
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        row = r.store.get_account_full(WX)
        assert row["merged_into"] == WX2 and row["deleted_ms"] is not None


async def test_wx_扫码超时转stopped并释放pending(tmp_path):
    """05 §2.4.2.1「断连期间的规则」:会话代理不可达 / 人一直没扫 ⇒ Agent 已建的 `wxNN` 行停在 `starting`,
    超 `qr_max_wait_s` 转 `stopped`、释放 `pending`。"""
    async with login_rig(tmp_path, script=[{"phase": "qrcode"}], clock=Clock(auto_step_ms=0)) as r:
        async def jump(_s):                                   # 每轮把时钟推过 qr_max_wait_s
            r.clock.advance(QR_MAX_WAIT_S * 1000)
        r.agent.wechat_login._sleep = jump
        assert await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT) == "stopped"
        assert r.store.get_account(WX)["state"] == "stopped"
        assert slot_view(r)["pending"] == ""


async def test_wx_login_start失败即停并释放(tmp_path):
    """05 §2.4.2.1 失败回滚表第 1 步:读 wxid 前失败(hook 没装上 / 进程被杀)⇒ 临时 `wxNN` 行 `stopped`;
    **槽位 `pending` 同步置空**(失败即释放,不等 TTL)。"""
    async with login_rig(tmp_path, script=[{"phase": "ready"}]) as r:
        r.wa.fail_paths["/wa/v1/wechat/login/start"] = 99
        assert await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT) == "stopped"
        assert slot_view(r)["pending"] == ""


async def test_wx_登录流全程带同一login_session_id(tmp_path):
    """05 §2.4.2:`login_session_id` 语义 =「一次登录尝试」的标识,**贯穿该次尝试的所有 `prompt`/`account_state` 事件**。"""
    async with login_rig(tmp_path, script=[{"phase": "qrcode"}, {"phase": "keytry", "key": {"stage": "img"}}, {"phase": "ready"}]) as r:
        await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT)
        ids = {json.loads(e["payload_json"]).get("login_session_id") for e in events_of(r, "account_state", WX)}
        assert ids - {"", None} == {LS1}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 八、微信读写门与掉线(05 §2.4.7、§2.5.4 微信三行、00 §8.1 degraded 通道语义)
# ══════════════════════════════════════════════════════════════════════════════════════════════

def wx_acct(rig: Rig, *, state: str = "running", code: Optional[str] = None) -> Account:
    return Account(id=WX, channel="wechat", state=state, state_code=code, self_uid=WXID)


async def test_wx_取钥失败下写类直接拒(tmp_path):
    """05 §2.4.7 / 00 §8.1:`degraded(KEY_FAIL)` 下写类指令直接拒 `NOT_READY`,
    `error.message="微信解密不可用,无法确认送达,已拒绝发送"`(逐字)。"""
    async with wx_rig(tmp_path) as r:
        blocked = r.wx.write_guard(wx_acct(r, state="degraded", code="KEY_FAIL"))
        assert blocked is not None and blocked.code == "NOT_READY"
        assert blocked.error.message == "微信解密不可用,无法确认送达,已拒绝发送"


async def test_wx_取钥失败下读也不开放(tmp_path):
    """05 §2.4.7 结论:`degraded(KEY_FAIL)` 下**读与写都不开放** —— chatlog 拿不到 Data Key,`read_messages` 不可用。"""
    async with wx_rig(tmp_path) as r:
        assert r.wx.read_guard(wx_acct(r, state="degraded", code="KEY_FAIL")) is not None


async def test_wx_锁屏下写拒读放行(tmp_path):
    """05 §2.5.4 锁屏行(C-20)/ 00 §8.1:`degraded(SCREEN_LOCKED)` —— **读取正常**(chatlog 不依赖桌面),
    **写类指令拒 `NOT_READY`**(与 `KEY_FAIL` 的「读写都拒」不同)。"""
    async with wx_rig(tmp_path) as r:
        acct = wx_acct(r, state="degraded", code="SCREEN_LOCKED")
        assert r.wx.write_guard(acct) is not None
        assert r.wx.read_guard(acct) is None


async def test_wx_锁屏文案逐字(tmp_path):
    """05 §2.5.4 锁屏行:`degraded(SCREEN_LOCKED, state_reason="Windows 已锁屏,发送不可用")`。"""
    async with wx_rig(tmp_path) as r:
        r.wa.screen_locked = True
        state, code, reason = await asyncio.wait_for(r.wx.probe_state(wx_acct(r)), ASYNC_TIMEOUT)
        assert (state, code) == ("degraded", "SCREEN_LOCKED")
        assert reason == "Windows 已锁屏,发送不可用"


async def test_wx_微信退出登录判掉线(tmp_path):
    """05 §2.5.4 微信行:主窗口消失 / `Weixin.exe` 进程退出 ⇒ `login_required(LOGGED_OUT)`。"""
    async with wx_rig(tmp_path) as r:
        r.wa.wechat["logged_in"] = False
        state, code, _ = await asyncio.wait_for(r.wx.probe_state(wx_acct(r)), ASYNC_TIMEOUT)
        assert (state, code) == ("login_required", "LOGGED_OUT")


async def test_wx_掉线时holder保持不自动拉起(tmp_path):
    """05 §2.5.4 微信行:掉线后槽位 `holder` **保持为它**(它仍是「该在线的那个」),不自动拉起微信、不自动登回。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running")
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        r.wa.wechat["logged_in"] = False
        await asyncio.wait_for(r.wx.get_state(wx_acct(r)), ASYNC_TIMEOUT)
        assert slot_view(r)["holder"] == WX
        assert r.wa.logout_calls == 0 and not [c for c in r.wa.calls if c[1].endswith("/login/start")]


async def test_wx_chatlog挂但微信在线不算掉线(tmp_path):
    """05 §2.5.4 第四行:`wechat/read` 连续 3 次异常而主窗口仍在 ⇒ **不是掉线**:`degraded(KEY_FAIL)`。"""
    async with wx_rig(tmp_path) as r:
        r.wa.fail_paths["/wa/v1/wechat/read"] = 99
        view = r.wx._view(wx_acct(r))
        for _ in range(3):
            await asyncio.wait_for(r.agent.wechat_poller.poll(view), ASYNC_TIMEOUT)
        assert r.agent.wechat_poller.degraded_key_fail(WX) is True
        state, code, _ = await asyncio.wait_for(r.wx.probe_state(wx_acct(r)), ASYNC_TIMEOUT)
        assert (state, code) == ("degraded", "KEY_FAIL")


async def test_wx_会话代理不在线时端点不可用(tmp_path):
    """02 §3.6 表头 / 05 §2.4.1 C-02:执行体 `user` 的端点在**会话代理不在线**时一律 `503 NOT_READY`;
    00 §8.1 ③:`WINAGENT_USER_OFFLINE` 属失败/异常组,通常伴随 `degraded`。"""
    async with wx_rig(tmp_path) as r:
        r.wa.user_agent = False
        state, code, _ = await asyncio.wait_for(r.wx.probe_state(wx_acct(r)), ASYNC_TIMEOUT)
        assert state == "degraded" and code in ("WINAGENT_USER_OFFLINE", "WINAGENT_OFFLINE")


async def test_wx_企点读库失效不算degraded的对照(tmp_path):
    """00 §8.1 `degraded` 的通道语义:`degraded` 只给微信 `KEY_FAIL`/`SCREEN_LOCKED` 这类;一切正常时恒 `running`。"""
    async with wx_rig(tmp_path) as r:
        state, code, reason = await asyncio.wait_for(r.wx.probe_state(wx_acct(r)), ASYNC_TIMEOUT)
        assert (state, code, reason) == ("running", None, "")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 九、微信入库 / 游标 / 10 s 读回确认(02 §2.8.1 微信行、06 §2.9.1/§2.9.2/§2.9.3/§2.9.4/§2.12、05 §2.4.4 ⑦)
# ══════════════════════════════════════════════════════════════════════════════════════════════

TALKER = "wxid_peer01"
ROOM = "12345678@chatroom"


async def wx_poll(rig: Rig, *, only: Optional[list[str]] = None) -> int:
    return await asyncio.wait_for(rig.agent.wechat_poller.poll(rig.wx._view(wx_acct(rig)), only), ASYNC_TIMEOUT)


async def test_wx_ext是talker冒号seq(tmp_path):
    """02 §2.8.1 微信行 / 06 §2.9.2:ext = `"{talker}:{seq}"`(C-22:带 talker 无论 seq 是否全局递增都安全)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="你好", seq=7, ts_ms=T0_MS)
        assert await wx_poll(r) == 1
        assert rows_of(r, WX, limit=10)[0]["ext_msg_id"] == f"{TALKER}:7"


async def test_wx_source是chatlog(tmp_path):
    """02 §2.8.1 微信行 `messages.source` 列 = `chatlog`;06 §2.9.1 同。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="一条", seq=1, ts_ms=T0_MS)
        await wx_poll(r)
        assert rows_of(r, WX, limit=10)[0]["source"] == "chatlog"


async def test_wx_群会话判据是chatroom后缀(tmp_path):
    """06 §2.9.1:微信 `talker` 以 `@chatroom` 结尾 → `group`(chatlog `isChatRoom`)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=ROOM, content="群里一条", seq=1, ts_ms=T0_MS)
        r.wa.add_row(talker=TALKER, content="私聊一条", seq=2, ts_ms=T0_MS)
        await wx_poll(r)
        kinds = {s["native_id"]: s["kind"] for s in sessions_of(r, WX)}
        assert kinds[ROOM] == "group" and kinds[TALKER] == "private"


async def test_wx_session_id是账号冒号talker(tmp_path):
    """00 §6 / 06 §2.9.1:`session.id = {account_id}:{原生ID}`,微信原生 ID = `talker`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="x", seq=1, ts_ms=T0_MS)
        await wx_poll(r)
        assert rows_of(r, WX, limit=10)[0]["session_id"] == f"{WX}:{TALKER}"


async def test_wx_游标按会话记seq水位(tmp_path):
    """06 §2.9.3 / 02 §2.8.3:`owner=wx01, kind=chatlog_seq:<talker>`,`value_int = last_seq`(已见最大 seq、也是 `since_seq`),
    `value` 里另存 `last_ts_ms`(chatlog 按时间拉,只存 seq 就得反推时间,99c C-19)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="a", seq=3, ts_ms=T0_MS)
        r.wa.add_row(talker=TALKER, content="b", seq=9, ts_ms=T0_MS + 1000)
        await wx_poll(r)
        cur = r.store.cursor_get(WX, f"chatlog_seq:{TALKER}")
        assert cur is not None and cur.value_int == 9
        assert json.loads(cur.value)["last_seq"] == 9 and "last_ts_ms" in json.loads(cur.value)


async def test_wx_游标推进与落库同事务(tmp_path):
    """06 §2.9.3 末 / 02 §2.2.8:**游标推进与消息落库同一事务**(会话 → 消息 → 该会话水位)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="a", seq=5, ts_ms=T0_MS)
        await wx_poll(r)
        assert r.store.count_messages(WX) == 1
        assert r.store.cursor_get(WX, f"chatlog_seq:{TALKER}").value_int == 5


async def test_wx_重叠窗重复行靠唯一键挡住(tmp_path):
    """06 §2.9.2 微信:`cursor − lookback_overlap_s` 的重叠窗口必然重复取到边界那几条,**全靠 `(talker, seq)` 这个键挡**。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="边界", seq=4, ts_ms=T0_MS)
        await wx_poll(r)
        r.store.cursor_set(WX, f"chatlog_seq:{TALKER}", 0)     # 模拟重叠窗把同一条又取回来
        await wx_poll(r)
        assert r.store.count_messages(WX) == 1


async def test_wx_撤回标记不删(tmp_path):
    """06 §2.9.4:微信 chatlog 行 `isRevoked`,轮询到同 `seq` 且 `isRevoked` 变真时更新;正文保留。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        row = r.wa.add_row(talker=TALKER, content="要撤回", seq=6, ts_ms=T0_MS)
        await wx_poll(r)
        row["isRevoked"] = True
        r.store.cursor_set(WX, f"chatlog_seq:{TALKER}", 5)
        await wx_poll(r)
        m = rows_of(r, WX, limit=10)[0]
        assert m["revoked"] == 1 and m["text"] == "要撤回"


async def test_wx_拉取用since_seq增量(tmp_path):
    """06 §2.9.1 微信行 / 02 §3.6 #39:`GET /wa/v1/wechat/read?talker=&since_seq=&limit=`,每轮拉增量。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="旧", seq=1, ts_ms=T0_MS)
        await wx_poll(r)
        r.wa.add_row(talker=TALKER, content="新", seq=2, ts_ms=T0_MS + 1000)
        assert await wx_poll(r) == 1, "只应取回水位之后的那一条"
        assert r.store.count_messages(WX) == 2


async def test_wx_拉取周期默认五秒(tmp_path):
    """05 §2.4.4 ⑦ / 02 §7.1 `[adapters.wechat] poll_interval_s = 5`:Agent 每 5 s 调 `GET /wa/v1/wechat/read`。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.wechat_adapter.poll_interval_s == WECHAT_POLL_INTERVAL_S
        assert r.agent.wechat_poller.interval_s(WX) == float(WECHAT_POLL_INTERVAL_S)


async def test_wx_发送确认期把周期加密到一秒(tmp_path):
    """05 §2.4.4 ⑦(C.4.2):发送确认期(send 后 10 s 窗口)Agent 把周期临时调到 1 s;
    02 §7.1 `[adapters.wechat] confirm_poll_interval_ms = 1000`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=0)) as r:
        assert r.agent.cfg.wechat_adapter.confirm_poll_interval_ms == WECHAT_CONFIRM_POLL_MS
        r.agent.wechat_poller.note_send(WX)
        assert r.agent.wechat_poller.interval_s(WX) == WECHAT_CONFIRM_POLL_MS / 1000
        r.clock.advance(CONFIRM_TIMEOUT_WECHAT_MS + 1)
        assert r.agent.wechat_poller.interval_s(WX) == float(WECHAT_POLL_INTERVAL_S)


async def test_wx_确认窗是十秒(tmp_path):
    """02 §7.1 `[bus] confirm_timeout_wechat_ms = 10000`;06 §2.12 微信行:10 s 内没有 → `state=FAILED`。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.bus.confirm_timeout_wechat_ms == CONFIRM_TIMEOUT_WECHAT_MS


def wx_send_cmd(text: str = "微信一条", *, key: Optional[str] = None) -> Command:
    return Command(account_id=WX, op="send_text", args={"session": f"{WX}:{TALKER}", "text": text},
                   idempotency_key=key or f"k-{uuid.uuid4().hex[:10]}", origin=CommandOrigin())


async def test_wx_发送读回确认绑定ext并置DELIVERED(tmp_path):
    """06 §2.12 微信行:chatlog 读到 `isSelf=true` 且 `norm(content) == norm(text)` 的行 ⇒
    绑定 `ext_msg_id`、`state=DELIVERED`、`confirmed_by=chatlog`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        res = await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        assert res.code == "DELIVERED", res
        row = [x for x in rows_of(r, WX, limit=10) if x["dir"] == "out"][0]
        assert row["state"] == "DELIVERED" and row["confirmed_by"] == "chatlog"
        assert row["ext_msg_id"].startswith(f"{TALKER}:")


async def test_wx_发送前先落SENDING行且source是chatlog(tmp_path):
    """06 §2.12 / 02 §2.8.1 出向行 R6-25:本系统发出的微信出向行 `source='chatlog'`,发送前先写 `SENDING`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        row = [x for x in rows_of(r, WX, limit=10) if x["dir"] == "out"][0]
        assert row["source"] == "chatlog" and row["is_self"] == 1 and row["trace_id"]


async def test_wx_十秒未读回判FAILED(tmp_path):
    """06 §2.12 微信行(C.4.2 微信口径):10 s 内没读到 → `state=FAILED`;00 §8.3:`SEND_FAILED` = 「微信 10s 未读回」,`retryable=是`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=4000)) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.send_result = {"ok": True, "code": "SENT", "ext_msg_id": None, "confirm_ms": None}   # WinAgent 只回「点完了」
        res = await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        assert res.code == "SEND_FAILED", res
        assert res.error.retryable is True
        row = [x for x in rows_of(r, WX, limit=10) if x["dir"] == "out"][0]
        assert row["state"] == "FAILED"


async def test_wx_没有待核这个态(tmp_path):
    """00 §8.3:`SEND_CALLED_BUT_UNCONFIRMED`「企点/QQ 才有;**微信无此态**」——微信超时一律 `SEND_FAILED`。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=4000)) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.send_result = {"ok": True, "code": "SENT", "ext_msg_id": None, "confirm_ms": None}
        res = await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        assert res.code != "SEND_CALLED_BUT_UNCONFIRMED"
        assert [x for x in rows_of(r, WX, limit=10) if x["dir"] == "out"][0]["state"] != "UNCONFIRMED"


async def test_wx_WinAgent报SEND_FAILED直接判失败(tmp_path):
    """02 §3.6 #38:`{ok, code:'DELIVERED|SEND_FAILED', ext_msg_id, confirm_ms}`;**不重试**。"""
    async with wx_rig(tmp_path, clock=Clock(auto_step_ms=2000)) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.send_result = {"ok": False, "code": "SEND_FAILED", "ext_msg_id": None, "confirm_ms": None}
        res = await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        assert res.code == "SEND_FAILED"
        assert len(r.wa.sent) == 1, "#38 不重试"


async def test_wx_取钥失败下发送不写SENDING行(tmp_path):
    """05 §2.4.7 / 02 §2.2.3 C-19:`degraded(KEY_FAIL)` 下写返回 `NOT_READY`;
    写路径前置闸命中时不该留下一条注定失败的出向行。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="degraded", self_uid=WXID)
        r.store.set_account_state(WX, "degraded", state_code="KEY_FAIL")
        res = await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        assert res.code == "NOT_READY"
        assert [x for x in rows_of(r, WX, limit=10) if x["dir"] == "out"] == []
        assert r.wa.sent == [], "闸拦下就不该真发"


async def test_wx_发送经38且带幂等键与确认窗(tmp_path):
    """02 §3.6 #38:`{session_name, text?, image_path?, idempotency_key, confirm_timeout_ms:10000}`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd(key="k-wx-1")), ASYNC_TIMEOUT)
        sent = r.wa.sent[-1]
        assert sent["session_name"] == TALKER and sent["idempotency_key"] == "k-wx-1"
        assert sent["confirm_timeout_ms"] == CONFIRM_TIMEOUT_WECHAT_MS


async def test_wx_确认期内的加速轮只查目标会话(tmp_path):
    """05 §2.4.4 ⑦ / 02 §2.2.3 协议注释:`poll(acct, only_sessions=[…])` 给定 native_id 列表 = **只取这些会话**。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="目标会话", seq=1, ts_ms=T0_MS)
        r.wa.add_row(talker=ROOM, content="别的会话", seq=2, ts_ms=T0_MS)
        assert await wx_poll(r, only=[TALKER]) == 1
        assert [x["session_id"] for x in rows_of(r, WX, limit=10)] == [f"{WX}:{TALKER}"]


async def test_wx_我方消息入库为出向行(tmp_path):
    """06 §2.12:微信轮询本来就会把我方消息当普通行拉回来(`self=true`,读回行构造为 `dir=out`)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="我在手机上发的", seq=1, ts_ms=T0_MS, is_self=True)
        await wx_poll(r)
        row = rows_of(r, WX, limit=10)[0]
        assert row["dir"] == "out" and row["is_self"] == 1 and row["trace_id"] is None


async def test_wx_入库同样先建会话行(tmp_path):
    """02 §2.8.1 R4-7:拉型通道同样走「同一事务里先 upsert `sessions` 再写 `messages`」。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        assert sessions_of(r, WX) == []
        r.wa.add_row(talker=TALKER, content="第一条", seq=1, ts_ms=T0_MS)
        await wx_poll(r)
        assert [s["native_id"] for s in sessions_of(r, WX)] == [TALKER]


async def test_wx_每条入库都发message事件(tmp_path):
    """02 §2.2.3:适配器把拉型通道的增量抹平成 `store.ingest` + `events.emit`;05 §2.4.4 ⑦「Agent ingest 后自己发 message 事件」。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="a", seq=1, ts_ms=T0_MS)
        r.wa.add_row(talker=TALKER, content="b", seq=2, ts_ms=T0_MS + 1)
        await wx_poll(r)
        assert len(events_of(r, "message", WX)) == 2


async def test_wx_agent只经wa端点不直连chatlog(tmp_path):
    """02 §2.2.3 wechat 行 / 05 §2.4.4 ⑦(C-03/C-25):Agent 侧**不直接碰 chatlog/pyweixin**,全部经 WinAgent `/wa/v1/wechat/*`;
    **WinAgent 从不回调 Agent**。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.add_row(talker=TALKER, content="a", seq=1, ts_ms=T0_MS)
        await wx_poll(r)
        assert r.wa.calls and all(c[1].startswith("/wa/v1/wechat/") for c in r.wa.calls)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# 十、微信 WinAgent 端点契约补充与切换细节(02 §3.6 #28/#34/#35/#41/#42、05 §2.4.5/§2.4.6)
# ══════════════════════════════════════════════════════════════════════════════════════════════

async def test_wx_状态查询走28端点(tmp_path):
    """02 §3.6 #28 `GET /wa/v1/wechat/status`:`{enabled, wechat:{…}, chatlog:{…}, ritual_done, screen_locked, login_session?}`。"""
    async with wx_rig(tmp_path) as r:
        st = await asyncio.wait_for(r.agent.wechat_client.status(), ASYNC_TIMEOUT)
        assert set(st) >= {"enabled", "wechat", "chatlog", "ritual_done", "screen_locked"}
        assert st["wechat"]["wxid"] == WXID


async def test_wx_模块关闭时其余为空(tmp_path):
    """02 §3.6 #28:模块关闭时 `enabled:false` 其余 null;05 §2.4.1:`winagent.toml [wechat] enabled=false` ⇒ 微信项灰显。"""
    async with wx_rig(tmp_path) as r:
        r.wa.enabled = False
        st = await asyncio.wait_for(r.agent.wechat_client.status(), ASYNC_TIMEOUT)
        assert st["enabled"] is False and st["wechat"] is None and st["chatlog"] is None


async def test_wx_手动重试取钥走35端点(tmp_path):
    """02 §3.6 #35 `POST /wa/v1/wechat/key/retry` → `{ok, dll, error}`;05 §2.4.7 恢复路径:成功即 `running`。"""
    async with wx_rig(tmp_path) as r:
        res = await asyncio.wait_for(r.agent.wechat_client.key_retry(), ASYNC_TIMEOUT)
        assert set(res) >= {"ok", "dll"}
        assert r.wa.key_retry_calls == 1


async def test_wx_会话列表走41端点(tmp_path):
    """02 §3.6 #41 `GET /wa/v1/wechat/sessions?keyword=&limit=` → `ChatlogClient.list_chatrooms` + 联系人。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.session_rows = [{"talker": ROOM, "name": "某群"}]
        res = await asyncio.wait_for(r.wx.execute(wx_acct(r), Command(account_id=WX, op="list_sessions", args={})), ASYNC_TIMEOUT)
        assert res.ok and res.data["items"] == r.wa.session_rows


async def test_wx_截图走42端点(tmp_path):
    """02 §3.6 #42 `GET /wa/v1/wechat/screenshot`:微信主窗口截图 PNG(`P-SCREEN` 微信预览)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        res = await asyncio.wait_for(r.wx.execute(wx_acct(r), Command(account_id=WX, op="screenshot", args={})), ASYNC_TIMEOUT)
        assert res.ok and res.data["png"].startswith(b"\x89PNG")


async def test_wx_截图在锁屏时不可用(tmp_path):
    """02 §3.6 #42:锁屏时 `NOT_READY`(会话代理拿不到前台窗口)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        r.wa.user_agent = False
        res = await asyncio.wait_for(r.wx.execute(wx_acct(r), Command(account_id=WX, op="screenshot", args={})), ASYNC_TIMEOUT)
        assert res.code == "NOT_READY"


async def test_wx_能力集合五项(tmp_path):
    """02 §3.10 能力目录里 `wechat='supported'` 的五个 op:`send_text`/`read_messages`/`get_state`/`screenshot`/`list_sessions`。"""
    async with wx_rig(tmp_path) as r:
        assert r.wx.capabilities == frozenset({"send_text", "read_messages", "get_state", "screenshot", "list_sessions"})


async def test_wx_停止拉取不顺手登出(tmp_path):
    """05 §2.4.5 第 2 步 / 02 §3.6 #34:Agent `stop` 只停对该账号的 `wechat/read` 拉取;**登出微信是 #34**,
    由槽位切换 / #16 logout 发起 —— 顺手登出会误踢在用的号。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        await asyncio.wait_for(r.wx.stop(wx_acct(r), graceful=True), ASYNC_TIMEOUT)
        assert r.wa.logout_calls == 0


async def test_wx_切换排空超时是六十秒(tmp_path):
    """05 §2.4.5 第 1 步 / 02 §7.1 `[adapters.wechat] switch_drain_timeout_s = 60`:队列里已受理的写指令等它跑完(上限 60 s)。"""
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.wechat_adapter.switch_drain_timeout_s == SWITCH_DRAIN_TIMEOUT_S


async def test_wx_切换先排空再登出再换槽(tmp_path):
    """05 §2.4.5 1)~5):排空 holder → 停 chatlog → 登出(#34)→ `holder → stopped` → `pending=目标`。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        make_wx(r, WX2)
        r.slot.claim(WX, LS1)
        r.slot.promote(WX)
        order: list[str] = []

        async def drain(aid: str, timeout_s: float) -> bool:
            order.append(f"drain:{aid}")
            return True

        async def stop_account(aid: str):
            order.append(f"stop:{aid}")
            r.slot.release_holder(aid)
            r.agent.accounts.transition(aid, "stopped", state_reason="已切换到 wx02")

        res = await asyncio.wait_for(r.slot.switch(WX2, stop_account=stop_account, drain=drain), ASYNC_TIMEOUT)
        assert order == [f"drain:{WX}", f"stop:{WX}"], order
        assert res["holder_before"] == WX
        assert slot_view(r)["holder"] == "" and slot_view(r)["pending"] == WX2


async def test_wx_登出用34端点(tmp_path):
    """05 §2.4.6(D-1 拍板)/ 02 §3.6 #34:微信登出 = 结束进程(先 `WM_CLOSE`,失败再结束进程),经 `POST /wa/v1/wechat/logout`。"""
    async with wx_rig(tmp_path) as r:
        await asyncio.wait_for(r.agent.wechat_client.logout(), ASYNC_TIMEOUT)
        assert r.wa.logout_calls == 1
        assert r.wa.wechat["logged_in"] is False


async def test_wx_登录阶段拒IM写类(tmp_path):
    """00 §8.1 R-06:`login_required` ❌ **拒绝**一切 IM 语义写类(回 `LOGIN_REQUIRED`,`needs_human=true`)。"""
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="login_required", self_uid=WXID)
        r.store.set_account_state(WX, "login_required", state_code="WAIT_QRCODE")
        res = await asyncio.wait_for(r.agent.bus.submit(wx_send_cmd()), ASYNC_TIMEOUT)
        assert res.code == "LOGIN_REQUIRED" and res.error.needs_human is True


async def test_wx_bind重试耗尽转error并释放pending(tmp_path):
    """05 §2.4.2.1 失败回滚表第 4 步:`bind` 网络失败/503 ⇒ 每 5 s 重试(幂等),超 `bind_retry_max`(默认 12)
    ⇒ `error(VAULT_UNAVAILABLE)` 同款环境类错误、**失败即同步释放 `pending`**、`alert`。"""
    async with login_rig(tmp_path, script=[{"phase": "identified", "wxid": WXID}] * 30) as r:
        r.wa.fail_paths["/wa/v1/wechat/bind"] = 999
        assert await asyncio.wait_for(r.agent.wechat_login.run(WX, LS1), ASYNC_TIMEOUT) == "error"
        row = r.store.get_account(WX)
        assert row["state"] == "error" and row["state_code"] == "VAULT_UNAVAILABLE"
        assert slot_view(r)["pending"] == ""


async def test_wx_bind幂等同wxid重绑(tmp_path):
    """05 §2.4.2.1 第 4) 步 / 02 §3.6 #33c:`bind` **幂等:已绑同 id 直接 200**;同 `wxid` 重绑幂等。"""
    async with wx_rig(tmp_path) as r:
        a = await asyncio.wait_for(r.agent.wechat_client.bind(WXID, WX), ASYNC_TIMEOUT)
        b = await asyncio.wait_for(r.agent.wechat_client.bind(WXID, WX), ASYNC_TIMEOUT)
        assert a["account_id"] == b["account_id"] == WX
