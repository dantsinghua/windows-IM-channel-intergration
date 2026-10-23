"""微信通道适配器(Agent 侧)—— 02 §2.2.3 wechat 条 / §3.6 #28~#43;05 §2.4;06 §2.9.1/§2.9.2/§2.9.3/§2.12。

槽位状态机(切换/pending/holder/reaper/接管)不在本包里,在 ``qtrade_agent.wechat_slot``(它属于资源池语义、
落 ``resource_pools(pool='windows')``,02 §2.2.5)。
"""
from .adapter import WechatAdapter
from .client import FakeWeChatWinAgent, WeChatCallFailed, WeChatNotReady, WeChatWinAgent
from .login import WechatLoginFlow
from .normalize import ext_msg_id, session_kind, to_message
from .poll import CURSOR_PREFIX, WechatAccountView, WechatPoller
from .settings import WechatAdapterConfig

__all__ = ["WechatAdapter", "WechatPoller", "WechatAccountView", "WechatLoginFlow", "WechatAdapterConfig",
           "WeChatWinAgent", "FakeWeChatWinAgent", "WeChatNotReady", "WeChatCallFailed",
           "to_message", "ext_msg_id", "session_kind", "CURSOR_PREFIX"]
