"""``agent.toml [adapters.wechat]`` 的默认值 —— 规格唯一出处:02 §7.1(镜像对账表 docs/07 §[adapters.wechat])。

⚠️ **临时落点**:``config.py`` 是别的 agent 正在改的既有文件,本批不动它(文件所有权硬约束)。
本段三键与 07 §「[adapters.wechat](02 §7.1 [adapters.wechat])」逐字一致:
``poll_interval_s=5`` · ``confirm_poll_interval_ms=1000`` · ``switch_drain_timeout_s=60``(C6:归 agent.toml,05 §7 同键已删)。
接线时由总控把本 dataclass 并进 ``config.AgentConfig``(handoff `.omc/handoffs/wechat-channel.md` 给了精确 diff),
届时本文件只留 ``from ...config import WechatAdapterConfig`` 的再导出或直接删。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WechatAdapterConfig:
    poll_interval_s: int = 5                 # 05 §2.4.4 ⑦ / 06 §2.9.1:Agent 每 5 s 调 #39 read
    confirm_poll_interval_ms: int = 1000     # 发送确认期(send 后 confirm_timeout_wechat_ms 窗口)把周期加密到 1 s
    switch_drain_timeout_s: int = 60         # 05 §2.4.5 ①:排空 holder 队列的上限,超时的按 TIMEOUT 回执
