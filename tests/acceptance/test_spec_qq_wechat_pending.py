"""第五批验收里**确认的实现缺陷**用例(主文件 `test_spec_qq_wechat.py` 不留红,故移到这里)。

规矩同主文件:断言只依据规格原文。每条给出「规格原句 / 实际行为 / 建议修法」。
本文件**预期为红**,修完实现后应逐条搬回主文件。跑法:

    ~/.venvs/qtrade/bin/python -m pytest -q tests/acceptance/test_spec_qq_wechat_pending.py
"""
from __future__ import annotations

import asyncio

from qtrade_agent.models import Command

try:
    from tests.acceptance.test_spec_qq_wechat import ASYNC_TIMEOUT, WX, WXID, make_wx, wx_rig
except ImportError:                                   # pytest 以 rootdir 载入时的别名(与主文件同款写法)
    from test_spec_qq_wechat import ASYNC_TIMEOUT, WX, WXID, make_wx, wx_rig   # type: ignore


async def test_wx_截图经总线可用(tmp_path):
    """**规格原句**:02 §3.6 #42 `GET /wa/v1/wechat/screenshot` = 微信主窗口截图 PNG(`P-SCREEN` 微信预览);
    00 §8.1 R-06:`login_required` 的可做集合 ✅ 含 `screenshot`;00 §8.3:只读类成功回 `OK`。

    **实际行为**:`screenshot` 经 `bus.submit` 恒回 `INTERNAL`,`error.message='Object of type bytes is not JSON serializable'`
    —— 适配器把原始 PNG 字节放进 `CommandResult.data['png']`,`bus._finalize → store.finish_command` 把 `data` 整体
    `json.dumps` 落 `command_results.data_json` 时炸掉。`running` 与 `login_required` 两种状态下都复现,
    即**微信截图这条能力经总线完全不可用**(直调适配器 `execute` 则正常)。

    **建议修法**:适配器不把裸 `bytes` 放进 `data` —— 02 §3.4.2 的截图端点本就是「返回图片体」而不是「把图片塞进 CommandResult」;
    `data` 里只留 `{png_len, mime, (可选)media_id/sha256}`,图片体由端点层直接回流(或按 §2.8.2 落 `media/` 后给引用),
    与 QQ 的 `NOT_APPLICABLE`、企点的截图路保持同一口径。
    """
    async with wx_rig(tmp_path) as r:
        make_wx(r, state="running", self_uid=WXID)
        res = await asyncio.wait_for(r.agent.bus.submit(Command(account_id=WX, op="screenshot", args={})), ASYNC_TIMEOUT)
        assert res.ok is True and res.code == "OK", res


async def test_wx_bind重试上限是可配项(tmp_path):
    """**规格原句**:05 §2.4.2.1 失败回滚表第 4 步「超 `bind_retry_max`(默认 12 次)⇒ `error(VAULT_UNAVAILABLE)`」;
    05 §7 `[accounts]` 与 docs/07 §[accounts] 都把 `bind_retry_max=12` 登记为 **agent.toml 的配置项**
    (docs/07 §「单侧独有」明列 `[accounts] … bind_retry_max=12`)。

    **实际行为**:`AgentConfig.accounts` 没有 `bind_retry_max` 字段(`AttributeError`);重试上限写死在
    `adapters/wechat/login.py::BIND_RETRY_MAX = 12`,现场无法按机器/网络状况调整,也与 07 的配置项对账表对不上。

    **建议修法**:在 `AccountsConfig` 补 `bind_retry_max: int = 12`,`WechatLoginFlow` 的默认值取
    `cfg.accounts.bind_retry_max`(保留构造参数覆盖),并让 07 的镜像对账把它标为已落地。
    """
    async with wx_rig(tmp_path) as r:
        assert r.agent.cfg.accounts.bind_retry_max == 12
