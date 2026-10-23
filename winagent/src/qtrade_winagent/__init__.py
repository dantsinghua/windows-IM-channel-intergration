"""QTrade WinAgent(Windows 侧)—— 设计规格 = docs/00 基线 + docs/02 §2.4/§2.4.1/§2.5/§2.9/§3.2/§3.6/§3.7/§7.2 + docs/03/04/05。

两个可执行体(02 §2.4,C-02):
- ``qtrade-winagent-svc.exe`` —— 服务 ``QTradeWinAgent``,LocalSystem,``delayed-auto``;**HTTP 只在服务里**
  (uvicorn,``ws="websockets"``,监听 ``127.0.0.1:17610`` 与 vEthernet (WSL) 地址,**不绑 0.0.0.0**,00 §3)。
- ``qtrade-winagent-user.exe`` —— 用户会话代理,计划任务「用户登录时」以登录用户身份拉起,**不提权**;
  经命名管道 ``\\\\.\\pipe\\qtrade-winagent-user`` 被服务调度(02 §2.4.1)。

包结构与 02 §2.4 模块划分一一对应:
- errors / ids / logfmt / config —— 横切:00 §10 错误信封、ULID、02 §2.9 日志行与脱敏表、winagent.toml 默认值(02 §7.2 + 04 §7)
- db                            —— winagent.db(DDL 逐字抽自 02 §3.2 → ``schema_winagent.sql``)+ 每日 03:00 保留期清理(R3-20)
- backends                      —— 全部 Windows 专有 API 的后端协议 + ``Fake*`` 假实现(测试只用假实现,Linux 上可全跑)
- win/                          —— ``Win*`` 真实现(pywin32 / ctypes / subprocess,**延迟导入**,非 Windows 上导入本包不报错)
- audit                         —— ``wa_audit_log``(02 §2.4 audit 模块;每次 ``/wa/v1`` 调用与管道调度一行,不记值)
- vault                         —— DPAPI 机器级 + 附加熵(05 §2.2.1 方案 B);端点 #7~#12
- monitor                       —— 04 §2.2 采样项 + 健康项 H01/H09~H11/H14~H16/H20 + 告警本地缓冲;端点 #2/#5/#6/#4
- netprobe                      —— 04 §2.8 逐级探测、``net_state`` 判定、WSL 子网跟踪与 17610 重绑、防火墙规则唯一拥有者;#13~#17/#45
- power                         —— keep-awake(04 §2.5.1,默认 ``powercfg``,C-6);#18/#19
- pipe                          —— 服务 ⇄ 会话代理 IPC(02 §2.4.1:帧协议 / HELLO·WELCOME / 心跳 / 超时 / SID 校验与多用户仲裁)
- wslctl                        —— ``wsl.exe`` 与 ``.wslconfig``(会话代理执行);#20~#27/#46/#47
- installer_ops                 —— 内核验证/回滚的提权半(服务)+ ``install_history``/``install_state``
- wechat                        —— M3.5 骨架:chatlog 托管、试钥、登录会话、读写端点;#28~#43
- svc / user                    —— FastAPI ``/wa/v1`` 服务装配(令牌鉴权 + 监听绑定规则)/ 会话代理进程

🔴 红线(00 §11.6 [NOSHUTDOWN]):**绝不自动 `wsl --shutdown`**——只有显式 ``confirm``/``confirm_shutdown`` 才做,见 ``wslctl``。
"""
__version__ = "0.1.0"
API_VERSION = "1.0"          # 02 §7.2 [api] api_version(只读);/wa/v1 的主版本 = 路径里的 v1(§3.8)
