# QTrade 多实例 IM 控制台 + 统一 RPA

> 🔴 本仓库协作的头等规范:**所有思考与交流一律使用中文**(见 `CLAUDE.md`)。

| 目录 | 内容 |
|---|---|
| `docs/` | 详细设计(唯一真值 = `00` 基线 + `01~06` 六册 + `07` 配置总表;裁决表 `00` §15g 当前至 **R6-59**);`check-truth-tables.py` 真值表对账器,改文档后必跑 |
| `src/qtrade_agent/` | Agent(WSL 侧 systemd 服务)代码。2026-09-19 第八轮评审收口后开工。首批 = M2 骨架里能在无 redroid 环境下编码与单测的部分:`store`(DDL 逐字抽自 02 §3.1、`ingest` 三元组、同事务顺序、出向合并)、`adapters/qidian`(XOR 解码、`msgtype` 路由、`poll_maindb`/`check_group_gaps`)、`bus`(R6-48 入口校验、出向先落库、幂等三态、队列外等确认)、`events`/`alerts`、`text.norm/clean_text`;第二批 = `scheduler`、`health`(内存态)、`api/`(FastAPI:鉴权/错误信封/版本头/accounts/commands/send/sessions/messages/audit/WS events)、`app.py` 装配、`main.py`(`python3 -m qtrade_agent.main`);第三批 = `runtime/`(docker/adb 后端协议 + 命令行实现 + 可编程假实现、端口按序号推导、`_purge_ephemeral`、`ensure_root` 三步逐字)、`pool`(02 §2.2.5 配额算法)、`accounts`(生命周期状态机 + `error_since_ms` 两个动作 + 启动恢复;端点 #2/#4/#5/#6/#7/#9/#10/#11/#19/#69)、`vault_client`/`winagent_client`/`timesync`(02 §2.5 契约、H13 校时);第四批 = `gate`(00 §11.3 安全闸:白名单/出口词表热更/自定义闸,接进总线)、`healthloop`(04 H04/H05/H06 账号级健康循环:退避重拉、三振、宽限窗、绝不 kill-server)、`pressure`(E-19 内存水位:三级、LRU 建议、critical 阻断新增与自动恢复、只停显式开关的账号)、登录阶段端点 #12/#13/#14/#15/#16b + 05 §2.5.4 掉线登记/提醒 + #20/#22/#23。开发容器里一律注入假后端,绝不碰真 docker/adb/WinAgent |
| `winagent/` | WinAgent **服务 + 会话代理**两进程、`/wa/v1` 全量、Vault(DPAPI)/monitor/netprobe/power/IPC/wslctl/installer_ops/wechat;`winagent.db` DDL 逐字抽自 `docs/02` §3.2。`cd winagent && pytest -q` = **307** |
| `console/` | Electron + Vue3 控制台(01 全册);`npm test` 要先 `rsync` 到构建目录跑(仓库里不装 `node_modules`)= **71** |
| `installer/` | Inno Setup 6 引擎 + PowerShell 模块 + 派发器 + **自编 SFX 存根 `QTradeSD.sfx`**(03 §2.1);`pytest -q installer/tests` = **90** |
| `tests/` | pytest **1934** 条:开发者单测 **843** 条(含「发送 → 读库合并 → DELIVERED」集成用例、冷启动序列不跳段、`ensure_root` 逐字、H04 退避重拉、H06 三振、内存水位、安全闸、与「代码 ↔ 文档」对账)+ `tests/acceptance/` 独立验收 **1091** 条(八位只读规格、不看开发者测试的撰写者按 06 §2.9.5/§2.12、02 §2.2/§2.5/§2.6/§2.8/§3.4/§3.6/§5/§6/§8b、00 §3/§7/§8.1/§10/§11.3、05 §2.0/§2.1.1/§2.2.7/§2.5、04 H02/H04~H06/H13 逐条写;基线 §15g R6-51/R6-53/R6-55/R6-57 是它们的收口) |
| `.claude/skills/qtrade-redroid-resume/` | 断点续接 skill:接手先读它全文 |
| `HANDOFF.md` / `CLAUDE.md` | 交接入口 / 项目级规范 |

## 跑起来

```bash
pip install pytest pytest-asyncio
python3 -m pytest -q                      # 单测(pythonpath=src 已在 pyproject.toml)
cd docs && python3 check-truth-tables.py  # 文档对账,须 exit 0
```

代码以文档为准:代码与文档冲突时改代码,或先在 `docs/00` §15g 追加裁决(编号续 R6-N)再改文档,并给 `check-truth-tables.py` 加规则(改前备份上反向验证能红)。
