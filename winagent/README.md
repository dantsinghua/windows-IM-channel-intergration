# WinAgent —— QTrade 的 Windows 侧执行体

> 规格唯一真值 = `docs/00-共享基线与口径.md`(基线 v1.3 + §15g 裁决表)+ `docs/02`(§2.4 模块划分、§2.4.1 IPC 契约、
> §2.5 调用契约、§2.9 日志格式、§3.2 `winagent.db` DDL、§3.6 `/wa/v1` 端点矩阵、§3.7 告警码、§7.2 配置)
> + `docs/03`(安装/令牌/内核)+ `docs/04`(采样、健康项、网络、探测、keep-awake)+ `docs/05`(Vault、微信)。
> **代码与文档冲突时改代码;要改文档先出裁决。**

## 1. 这是什么

Windows 侧只做「**必须在 Windows 上**」的四件事:守 DPAPI 保险库、采 Windows 侧指标、探 Windows 侧网络、开微信通道。
它拆成**两个可执行体**(02 §2.4,C-02):

| 执行体 | 身份 | 起法 | 干什么 | 端口 |
|---|---|---|---|---|
| `qtrade-winagent-svc.exe` | 服务 `QTradeWinAgent`,**LocalSystem**,`delayed-auto` | SCM | HTTP `/wa/v1`、Vault(DPAPI 机器级)、监控采样与告警缓冲、网络探测与**防火墙规则**、内核文件落盘、`wa_audit_log` | 监听 `127.0.0.1:17610` 与 vEthernet (WSL) 地址,**不绑 `0.0.0.0`** |
| `qtrade-winagent-user.exe` | **登录用户**,不提权 | 计划任务「用户登录时」 | `wsl.exe` 与 `.wslconfig`、微信/chatlog/讲述人/取钥、`ES_DISPLAY_REQUIRED` | **不开任何入站口**(只作命名管道客户端) |

两者经命名管道 `\\.\pipe\qtrade-winagent-user` 衔接(02 §2.4.1):**方向固定「服务 → 会话代理」**,
会话代理只回响应与心跳;服务是唯一对外面。

**方向只有 Agent → WinAgent**(C-03):WinAgent 从不回调 Agent。微信「推」型事件由 Agent 轮询 `GET /wa/v1/wechat/read`
拉取;WinAgent 本地告警由 Agent `GET /wa/v1/alerts?since=` 拉走;网络翻转与主机唤醒靠 Agent 轮询
`GET /wa/v1/net` 的 `seq` 与 `GET /wa/v1/time` 的 `last_resume_ms` 感知(R3-5)。

## 2. 目录

```
winagent/
├── pyproject.toml                       # 依赖、入口点、pytest 配置(pythonpath 含 ../src = Agent 侧,只读引用)
├── README.md                            # 本文件
├── build/                               # PyInstaller 打包(只能在 Windows 上跑)
│   ├── qtrade-winagent-svc.spec         #   服务 exe(onedir;不带 UI 自动化栈)
│   ├── qtrade-winagent-user.spec        #   会话代理 exe(onedir;带 pywinauto/pyweixin/Pillow,不带 uvicorn)
│   ├── version_svc.txt / version_user.txt
│   └── build.ps1                        #   建 venv → 跑测试 → 打两个 exe →(可选)signtool 签名
├── src/qtrade_winagent/
│   ├── __init__.py            版本与 API_VERSION
│   ├── errors.py              00 §10 错误信封 + 02 §2.4.1 混合端点的 stage/partial
│   ├── ids.py                 ULID / trace_id / run_id / ls_ 前缀
│   ├── logfmt.py              02 §2.9 日志行格式 + 脱敏表(日志 Filter 与审计序列化器同一张表)
│   ├── config.py              winagent.toml 默认值(02 §7.2 键名 + 04 §7 值 owner + 05 §7 独有键)
│   ├── db.py                  winagent.db 连接/PRAGMA/迁移 + 每日 03:00 保留期清理(R3-20)
│   ├── schema_winagent.sql    🔴 DDL **逐字抽自 docs/02 §3.2**(有测试逐字对账)
│   ├── backends.py            十一个后端协议 + 共用数据形态
│   ├── fakes.py               全部后端的可编程假实现(测试唯一使用的一套)
│   ├── win/                   Win* 真实现(pywin32/ctypes/subprocess,**全部延迟导入**)
│   ├── audit.py               wa_audit_log(不记值;vault.* 动作只留白名单键)
│   ├── vault.py               DPAPI 机器级 + 附加熵、版本、0 填充、熵自检
│   ├── alerts.py              告警去重/重复提醒/风暴合并/本地缓冲(≤1000)
│   ├── monitor.py             04 §2.2 采样 + 三级降采样 + H01/H09~H12/H14~H16/H20/H23
│   ├── netprobe.py            net_state、监听集合与重绑、防火墙唯一拥有者、逐级探测、实测采样
│   ├── power.py               keep-awake(powercfg/request/off;备份只存一次、关模块还原)
│   ├── pipe.py                IPC:帧协议、HELLO/WELCOME、心跳、超时、SID 校验与多用户仲裁
│   ├── wslctl.py              .wslconfig 十一键与 R1/R2/R3、内核三判据、发行版修复(会话代理)
│   ├── installer_ops.py       内核落盘/ACL/install_state/install_history + 混合端点两半编排
│   ├── wechat.py              M3.5 骨架:三张表、版本匹配、hosts 屏蔽、登录状态机与取钥时序
│   ├── svc.py                 FastAPI /wa/v1 全量端点 + 令牌鉴权 + 监听绑定
│   ├── user.py                会话代理装配(方法路由到 wslctl / wechat / power)
│   ├── main_svc.py            服务入口(装配 + 周期任务 + uvicorn)
│   └── main_user.py           会话代理入口(握手重试策略)
└── tests/                     274 条,全假后端,Linux 上可全跑
```

## 3. 怎么跑

```bash
cd winagent
~/.venvs/qtrade/bin/python -m pytest -q          # 274 条应全绿
```

测试**一律注入 `fakes.py` 的假后端**,因此在 WSL/Linux 上可全量跑,**绝不碰**真 DPAPI、真防火墙、真 powercfg、
真 `.wslconfig`、真注册表、真微信。想在本机看一眼服务长什么样:

```bash
# 只起 HTTP + 假后端,不动任何真系统状态
~/.venvs/qtrade/bin/python -m qtrade_winagent.main_svc --dev --root /tmp/wa-dev
```

### 契约测试

`tests/test_agent_contract.py` 用 **Agent 侧的真实客户端**(`qtrade_agent.winagent_client.WinAgentClient` +
`vault_client.WinAgentVault`)经 `httpx.ASGITransport` 打本服务,验证两侧对 02 §2.5/§3.6 的理解逐字吻合:
地址/令牌/超时/重试口径、`X-WA-Version` 头、Vault 六个动作的方法与状态码、多级条目名
(`mail/hmac/cmd/<短名>`,R6-10)、`503 NOT_READY` 的中文 message、`net.seq` 轮询、`alerts` 水位、`PUT /probes` 回写。
**本目录不改 Agent 侧一个字符。**

## 4. 🔴 须在 Windows 真机验证清单

下面这些是 `win/` 里的**真实现路径**,在 Linux 上只能验证「导入不报错 + 调用被平台守卫挡住」,
**语义正确性必须在 Windows 真机过一遍**。每条给「怎么验」与「过不了会怎样」。

### 4.1 Vault / DPAPI(`win/dpapi.py`)

| # | 验什么 | 怎么验 | 过不了的后果 |
|---|---|---|---|
| V1 | `CryptProtectData(CRYPTPROTECT_LOCAL_MACHINE, 附加熵)` 加解密往返 | 服务身份下 `PUT` 一条再 `POST …/read`,值一致 | 全部密码型登录不可用 |
| V2 | 服务账号从 LocalSystem 换成专用账号后**仍能解** | 换 `sc config obj=` 后重读同一条目 | 说明 `dpapi_scope` 落到了 user 级(05 §2.2.1 方案 A 的坑) |
| V3 | `icacls` 收紧后熵文件只剩 SYSTEM/Administrators | `icacls entropy.bin`,并跑一次 `startup_selfcheck()` | ACL 放宽检测失效 ⇒ `VAULT_ENTROPY_MISSING` 永不触发 |
| V4 | 换一台机器后**解不开**(DPAPI 性质,非缺陷) | 拷 `vault\` 到另一台机读 | 若能解,说明熵或作用域写错了 |

### 4.2 命名管道与仲裁(`win/pipes.py`)

| # | 验什么 | 怎么验 | 过不了的后果 |
|---|---|---|---|
| P1 | `PIPE_TYPE_MESSAGE` 双工、单帧 ≤ 1 MB | 起服务 + 会话代理,跑一次 `wechat/screenshot`(base64 装帧) | 截图/媒体路径直接断 |
| P2 | `GetNamedPipeClientProcessId` → SID 取得到 | 看 `wa_audit_log` 里 HELLO 的 `target`/日志 | **SID 拿不到 = R3-12 的提权校验形同虚设** |
| P3 | 快速用户切换 / RDP 双会话下仲裁正确 | 两个账号同时登录,各起一个会话代理 | 非安装用户抢到 WSL 控制权 = 提权 |
| P4 | 管道 ACL 只放 服务账号 + 安装用户 SID | 用第三个用户尝试 `CreateFile` 连管道 | 任意用户可对管道喂帧 |
| P5 | 15 s 无心跳判离线、恢复后自动接管 | 挂起会话代理进程 15 s 再恢复 | `health.user_agent` 与实际不符 |

### 4.3 网络与防火墙(`win/netinfo.py`、`win/firewall.py`)

| # | 验什么 | 怎么验 | 过不了的后果 |
|---|---|---|---|
| N1 | `vEthernet (WSL)` 查找规则命中(含 `vEthernet (WSL (Hyper-V firewall))` 变体) | `Get-NetAdapter`,对比 `GET /wa/v1/net.wsl_subnet` | 监听绑不上 WSL 网卡 ⇒ Agent 完全连不上 |
| N2 | 子网变化后重绑 + 规则 `RemoteAddress` 跟着刷 | `wsl --shutdown` 后重启 WSL(**用户确认后**),看 H16 与 `WSL_SUBNET_CHANGED` | 换网段后 Agent 失联 |
| N3 | `firewall/ensure` 的四态(created/updated/unchanged/blocked_by_policy) | 连调两次、再手改规则后调第三次 | 规则计数抖动或静默失效 |
| N4 | 组策略接管时 `ActiveStore` 里看不到 ⇒ 判 `blocked_by_policy` | 在有域策略的机器上试 | 报「成功」但实际没放行,排障方向全错 |
| N5 | WinINET 用户级代理经 `WTSQueryUserToken` 读 HKU 成功 | 用户侧设个代理,看 `GET /wa/v1/net.proxy.wininet_user` | `net_state` 判定失真 |
| N6 | `net_state` 五值在真 VPN(AnyConnect/深信服/飞连)下判对 | 连 VPN 前后各取一次 | V1~V5 全部对策走错分支 |

### 4.4 电源与锁屏(`win/power.py`、`win/sysinfo.py`)

| # | 验什么 | 怎么验 | 过不了的后果 |
|---|---|---|---|
| E1 | `powercfg /q` 解析出六项**当前值**(中英文系统都要过) | 中文 Windows 上跑一次 `query_timeouts` | 备份存成 0 ⇒ 关模块后还原不回去 |
| E2 | `powercfg /change` 六项生效 | 改完在「电源选项」里看 | 微信通道半夜睡过去 |
| E3 | 组策略锁定电源计划时 `change` 失败 ⇒ 退化 `request` | 域机器上试 | 报成功但没生效 |
| E4 | `SetThreadExecutionState` 在服务里**全局可见** | `powercfg /requests` 能看到我方请求 | keep-awake 名存实亡 |
| E5 | `ES_DISPLAY_REQUIRED` 必须由会话代理发才有效 | 分别在服务与会话代理里发,对比显示器是否熄 | Session 0 发无效这条结论要实测坐实 |
| E6 | `WTSQuerySessionInformation(WTSSessionInfoEx)` 锁屏判定 | 锁屏/解锁各取一次 | H11 与 `SCREEN_LOCKED` 全错 |
| E7 | `last_resume_ms`(Kernel-Power 107)取得到 | 睡眠后唤醒,看 `GET /wa/v1/time` | 主机唤醒事件彻底丢失(R3-5) |
| E8 | `w32tm /query /status` 中英文输出都能解析 | 中文系统上跑 | H13 校时失准 |

### 4.5 WSL 与内核(`win/wsl.py`)

| # | 验什么 | 怎么验 | 过不了的后果 |
|---|---|---|---|
| W1 | `wsl.exe` 输出是 UTF-16LE,解码正确 | `--list --verbose` 的解析结果 | 发行版状态全判错 |
| W2 | `.wslconfig` 解析到的是**安装用户**那份 | 会话代理里打印 `wslconfig_path()` | 写到 `systemprofile\.wslconfig` = 白写(R3-6) |
| W3 | R1/R2/R3 在真文件上行为正确(注释/未知键保留) | 造一份带注释与 `networkingMode=` 的文件再写 | 覆盖用户配置 |
| W4 | 内核三判据在真 binder 内核上全过 | `uname -r` / `/proc/filesystems` / 试挂 binderfs | 企点容器起不来却查不出原因 |
| W5 | 🔴 全程**没有**任何未经确认的 `wsl --shutdown` | 抓 `wa_audit_log` 与 `wsl.shutdown` 日志行 | **踩 00 §11.6 红线** |

### 4.6 微信(`win/wechat.py`,M3.5)

| # | 验什么 | 怎么验 | 过不了的后果 |
|---|---|---|---|
| X1 | 注册表定位 + `GetFileVersionInfo` 取到四段版本 | 与「设置→关于」对一遍 | 版本匹配全错(**不可读注册表 DisplayVersion**,实测陈旧) |
| X2 | `%APPDATA%\Tencent\xwechat\config\<32hex>.ini` 取 data_root | 与实际数据目录对一遍 | 备份/磁盘水位指错分区 |
| X3 | 取钥时序 a)→b)→c) 在真机复现,两把钥**同轮**落盘 | 按 05 §2.4.4a 走一遍,查 `~/.chatlog/chatlog.json` | 整轮作废、账号卡在 `keytry` |
| X4 | `chatlog.exe key --dll` 的参数形式与实际一致 | 真跑一次看 debug 日志三行 | hook 装不上 |
| X5 | pywinauto UIA 能找到会话列表 `List` + 搜索 `Edit` | 讲述人仪式前后各探一次 | 仪式判据失效、永远做仪式 |
| X6 | `FindWindow` 类名(4.x `Qt51514QWindowIcon`)在现役版本上仍对 | 真机抓一次写回 `main_wnd_class` | H10 误报进程缺失 |
| X7 | hosts 写入后 `Resolve-DnsName` 回 `0.0.0.0` | 写完立刻解析两个域名 | B 层屏蔽名存实亡(H21 应报警) |
| X8 | EDR/Defender 防篡改下 hosts 写失败 ⇒ `blocked_by_policy` | 开 Defender「篡改防护」再试 | 报成功但没写进去 |
| X9 | 发送后 10 s 读回确认真能命中自己发的那条 | 真发一条 | 「发了却报失败」——最危险的形态 |
| X10 | 🔴 重装引导**交互式、不静默** | 走一遍,确认「保留本地数据」勾选页出现 | `/S` 会把 `xwechat_files` 与登录态全删(B-3) |

### 4.7 打包与安装(`build/build.ps1`)

| # | 验什么 | 过不了的后果 |
|---|---|---|
| B1 | 两个 onedir 产物能在**没装 Python 的机器**上跑起来 | 隐藏导入漏了(`win/` 全是延迟导入,PyInstaller 静态扫不到) |
| B2 | `sc create … start= delayed-auto obj= LocalSystem` + `sc failure` 恢复策略 | 服务挂了不自恢复(04 H02 指望 SCM) |
| B3 | 计划任务以**登录用户身份、不提权**拉起会话代理 | 提权了 = C-02 的分权作废 |
| B4 | `pyweixin` 不在公共源上,需按 03 的随包清单装本地 wheel | 微信发送整条不可用 |
| B5 | 未签名 exe 在企业机上被 SmartScreen/EDR 拦 | 装不上(A-4:OV 证书起步) |

## 5. 与其它目录的边界

- **只写 `winagent/**`**。`src/qtrade_agent/`、`tests/`、`docs/`、根 `pyproject.toml` 一概不改。
- 发现 Agent 侧客户端与规格/本实现不一致,或规格自身矛盾:**不改它们**,写进 `.omc/handoffs/winagent.md`
  的「建议裁决 / 建议修复」,由安琳裁决后再动。
- `winagent.db` 只由**本服务**写(02 §2.7 两库边界);Agent 要 Windows 侧数据一律经 `/wa/v1` 拿。

## 6. 端点覆盖对照(02 §3.6 #1~#47 + #33b/#33c)

全部 49 个端点都已落地。执行体与令牌列逐行照 §3.6;`svc` 直接执行,`user` 经管道转会话代理
(离线一律 `503 NOT_READY`),`svc+user` 混合端点按 R3-15 拆两半、失败带 `stage`/`partial`。

| 组 | 端点 | 执行体 |
|---|---|---|
| 探活与版本 | #1 ping / #2 health / #3 version / #4 time | svc(🔴 探活**绝不经管道**,R3-1) |
| 监控 | #5 metrics / #6 alerts | svc |
| Vault | #7~#12 | svc(#11 仅 Agent 令牌 + 仅 loopback/WSL 子网 + 须带 `X-Trace-Id`) |
| 网络 | #13 net / #14 probe / #15 probes / #16 PUT probes / #17 firewall/ensure / #45 DELETE firewall | svc |
| 电源 | #18 power / #19 keepawake | svc + user(display 半) |
| WSL | #20~#25 / #47 | user(#25 混合:服务备份目录 + 会话代理写文件) |
| 内核 | #26 verify / #27 rollback / #46 apply | svc + user(混合) |
| 微信 | #28~#43(含 #33b hosts=svc、#33c bind) | 多数 user;#30/#33b/#43 = svc |
| 审计 | #44 | svc |
