# QTrade 多实例 IM 控制台 + 统一 RPA

> 🔴 本仓库协作的头等规范:**所有思考与交流一律使用中文**,每次回复称呼「安琳」(见 `CLAUDE.md`)。
> **接手先读 [`HANDOFF.md`](HANDOFF.md)**(第一入口:现状、接手第一步、下一步),再读断点续接 skill `.claude/skills/qtrade-redroid-resume/SKILL.md`。

一个 Windows 单 EXE 离线安装 → WSL2 + 自编 binder 内核 + redroid(Docker 里的 Android 11)跑**企点 / QQ**、Windows 侧跑**微信 PC**,统一 RPA 收发消息、邮件摆渡驱动、多账号管理。
**阶段(2026-09-21 深夜)**:全部部件已编码;**带签名的正式包已出、独立校验通过**(修掉了凌晨那一版「装上起不来」的全部已知缺陷),但**尚未在任何机器上运行过**——下一步是真装验证,时机与测试机待安琳定。详见 `HANDOFF.md` §3b / §4。

## 目录地图

| 目录 | 内容 | 测试 / 跑法 |
|---|---|---|
| `docs/` | **唯一真值**:`00` 基线 + `01~06` 六册 + `07` 配置总表;裁决表 `00` §15g 当前至 **R6-68** | `cd docs && python3 check-truth-tables.py`(真值表对账器,改文档后必跑,须 **exit 0**) |
| `src/qtrade_agent/` | **Agent**(WSL 侧 systemd 服务):`store`(DDL 逐字抽自 02 §3.1)、`adapters/qidian`(旁路读库 + UI 执行层)、`adapters/qq`(OneBot)、`adapters/wechat` + `login`、`bus` + `gate`(安全闸)、`scheduler`、`health`/`healthloop`、`api/`(FastAPI + WS + HMAC middleware)、`runtime/`(docker·adb 后端 + 可编程假实现)、`pool`/`pressure`、`accounts`(生命周期)、`vault_client`/`winagent_client`/`timesync`、`mail/`、`maintenance`/`media`/`monitor`/工作流引擎。**开发容器里一律注入假后端,绝不碰真 docker/adb/WinAgent** | 见下方「跑起来」 |
| `winagent/` | **WinAgent**:服务 + 会话代理两进程、`/wa/v1` 全量、Vault(DPAPI)、monitor/netprobe/power、命名管道 IPC、wslctl、installer_ops、wechat;`winagent.db` DDL 逐字抽自 `docs/02` §3.2;PyInstaller 打包 | `cd winagent && ~/.venvs/qtrade/bin/python -m pytest -q` = **329**(真 Windows 上同样 329) |
| `console/` | **Electron + Vue3 控制台**(`docs/01` 全册);`src/i18n/zh-CN/codes.ts` 与 00 §8.1/§8.3、02 §3.7 逐码对账;`mock/` 已逐端点对齐真后端并有 `mock-shape` 自检 | 仓库里**不装 `node_modules`**:先 `rsync -a --exclude node_modules --exclude dist console/ ~/work/qtrade-build/console/`,再在那边 `npm test` = **151** |
| `installer/` | **单 EXE 安装器**:`engine/`(Inno Setup 6 + 17 个 PowerShell 模块 + 派发器)、`sfx-stub/`(自编 SFX 存根 `QTradeSD.sfx`)、`rootfs/`(发行版镜像构建)、`signing/`(代码签名,自签名阶段、**未真执行**)、`build/`(载荷收集 13 项 + 三段流式总装)、`out/`(产物) | `~/.venvs/qtrade/bin/python -m pytest -q installer/tests` = **122**(规格对账);Windows 侧另跑 `installer/tests/run-pester.ps1` = **Pester 633** |
| `fieldtest/` | **真机验收**:`真机验收手册.md`(目标机要求、17 项改动清单、A~I 九组 **51 条**用例、排障回滚)、`collect-evidence.ps1`(只读取证)、`vm-lab/`(本机 Hyper-V 测试虚拟机脚本) | 一条都还没在真机跑过 |
| `tests/` | 开发者测试 `tests/test_*.py` + 独立验收 `tests/acceptance/test_spec_*.py`(由只读规格、不看开发者测试的撰写者写)+ `tests/test_docs_consistency.py`(代码 ↔ 文档对账)+ **`tests/e2e/`**(控制台 ↔ 真 Agent 联调、66 端点形状对账、`console-real/` 73 条) | 根 `~/.venvs/qtrade/bin/python -m pytest -q` = **2136** |
| `.claude/skills/qtrade-redroid-resume/` | **断点续接 skill** + `reference/` 五份快照(中文规范 / 文档一致性教训 / 企点读库实测 / **端到端教训** / **编排纪律**) | 接手先读 `SKILL.md` 全文 |
| `HANDOFF.md` / `CLAUDE.md` | 交接第一入口 / 项目级规范(中文头等规范、多 agent 编排纪律、禁区) | — |

> 🔴 **上表的测试条数是 2026-09-21 07:20~07:23 CST 的实跑值,以你跑那一刻为准**,别拿它当断言——各部件仍在改。

## 跑起来

```bash
# 主仓单测(系统 python 没装 pytest;这个 venv 是 uv 建的,没有 pip 模块)
~/.venvs/qtrade/bin/python -m pytest -q                 # 2136,约 5 分钟

# 其余四套
cd winagent && ~/.venvs/qtrade/bin/python -m pytest -q  # 329
~/.venvs/qtrade/bin/python -m pytest -q installer/tests # 122
cd docs && python3 check-truth-tables.py                # 须 exit 0
rsync -a --exclude node_modules --exclude dist console/ ~/work/qtrade-build/console/ \
  && cd ~/work/qtrade-build/console && npm test          # 151

# 起 Agent(真机)
python3 -m qtrade_agent.main --config /etc/qtrade/agent.toml --db /var/lib/qtrade/agent.db
python3 -m qtrade_agent.main --init-db --db <path>       # 只建库 + 迁移后退出(幂等,退出码 0/2/3/4/5)
```

打 wheel 用 `/usr/bin/python3 -m pip wheel`(上面那个 venv 打不了);**正式出包前先 `rm -rf build/`**(setuptools 的 `build/lib/` 是增量缓存,会把陈旧文件带进 wheel、或掩盖漏声明的数据文件)。

## 规矩

- **代码以文档为准**:冲突时改代码,或先在 `docs/00` §15g **末尾追加**裁决(编号续 R6-N)再改文档,并给 `check-truth-tables.py` 加规则(**在改前备份上反向验证能红**)。
- **改文档流程**:改 `docs/` → `check-truth-tables.py`(exit 0)→ 仓库根 `pytest -q` → `git commit` → `git push`。
- **禁区**(详见 `CLAUDE.md` 与 SKILL §4):绝不自行 `wsl --shutdown` / 重启容器;企点/微信是真实工作账号;真机 `C:\ProgramData\QTrade` 在非真装期间必须不存在;**未经安琳同意不运行**产出的安装 EXE / WinAgent exe;docker 只 `build/create/export/save/pull` 且用 `qtrade-build/` 前缀。
- **多 agent 编排**:角色独立(被某道质量门挡住的人不修那道门、验收用例只由独立验收方改)、agent 关了就新起、一个 agent 只给一批活;详见 `CLAUDE.md` 与 `.claude/skills/qtrade-redroid-resume/reference/orchestration-discipline.md`。
