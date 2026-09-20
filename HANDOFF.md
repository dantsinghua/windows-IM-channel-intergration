# 交接入口 / HANDOFF(2026-09-21)

> 🔴 协作头等规范:**所有思考与交流一律中文,每次回复称呼「安琳」**(见 `CLAUDE.md`)。
> 这是**任何机器上的第一入口**。细节都在指针后面,本文件只说「这是什么 / 怎么接上 / 现在卡在哪 / 下一步做什么」。

---

## 1. 这个仓库是什么

QTrade「**redroid 多实例 IM 控制台 + 统一 RPA**」项目的**设计文档 + 全部代码**。
一句话:给交易/资金团队做的多通道 IM 自动化控制台——**一个 Windows 单 EXE 离线安装** → WSL2 + 自编 binder 内核 + redroid(Docker 里的 Android 11)跑**企点 / QQ**、Windows 侧跑**微信 PC**,统一 RPA 收发消息、邮件摆渡驱动、多账号管理。

**阶段**:已离开设计阶段,**全部部件都已编码,正在做端到端验证与出包**。分支 `claude/lucid-dijkstra-uu5max`,**未合 `main`**。

## 2. 目录地图(每个目录一句话 + 它的细节在哪)

| 目录 / 文件 | 一句话 | 细节看 |
|---|---|---|
| `docs/` | **唯一真值**:`00` 基线 + `01~06` 六册 + `07` 配置总表 + `check-truth-tables.py` 真值表对账器(改文档后必跑,须 exit 0)。裁决表在 `00` §15g,当前至 **R6-62** | `docs/00-共享基线与口径.md` §14/§15~§15g |
| `src/qtrade_agent/` | **Agent**(WSL 侧 systemd 服务):store / 企点读库 / 总线 + 安全闸 / scheduler / api(FastAPI + WS)/ runtime(docker·adb)/ pool / 账号生命周期 / 健康循环 / WinAgent 客户端 / mail / QQ / 微信 / 横切基础设施 | SKILL §5「代码现状总表」 |
| `winagent/` | **WinAgent**:服务 + 会话代理两进程、`/wa/v1` 全量、Vault(DPAPI)、monitor/netprobe/power、IPC、wslctl、installer_ops、wechat;PyInstaller 打包 | `winagent/README.md` |
| `console/` | **Electron + Vue3 控制台**(`docs/01` 全册);仓库里**不装 `node_modules`** | `console/README.md` |
| `installer/` | **单 EXE 安装器**:`engine/`(Inno Setup 6 + 17 个 PowerShell 模块)、`sfx-stub/`(自编 SFX 存根 `QTradeSD.sfx`)、`rootfs/`(发行版镜像构建)、`signing/`(代码签名,自签名阶段)、`build/`(载荷收集 + 总装)、`out/`(产物) | `installer/build/README.md`、`installer/rootfs/README.md`、`installer/sfx-stub/README.md` |
| `fieldtest/` | **真机验收**:`真机验收手册.md`(A~I 九组 51 条)、`collect-evidence.ps1`(只读取证)、`vm-lab/`(本机 Hyper-V 测试虚拟机脚本) | `fieldtest/vm-lab/README.md` |
| `tests/` | 开发者测试 + `tests/acceptance/`(独立验收,只读规格撰写)+ **`tests/e2e/`**(控制台 ↔ 真 Agent 联调与 66 端点形状对账) | SKILL §5 |
| `.claude/skills/qtrade-redroid-resume/` | **断点续接 skill**(接手的主文档)+ `reference/` 五份快照 | `SKILL.md` |
| `CLAUDE.md` | 项目级规范:中文头等规范、多 agent 编排纪律、禁区 | — |

> ⚠️ 原机上还有一份更细的记录:`.omc/handoffs/*.md`(约 40 份逐 agent 交接)。它**被 git 忽略、不随仓库走** ——
> 接手**必需**的信息已经写进 `SKILL.md` / 本文件 / `README.md` / `CLAUDE.md`;`.omc/handoffs/` 只在原机上查证细节时才用得着。

## 3. 接手第一步(任何机器)

1. **先拉代码**:`git fetch --all` → 检出/快进到 `claude/lucid-dijkstra-uu5max`。
   🔴 **先 pull、再读 skill** —— 全局 `~/.claude/skills/` 里那份会落后好几批。
2. **跑五套确认基线**(数字见下表,**以你跑那一刻为准**;任一条红就先别开工,先弄清是不是自己环境的问题):

   ```bash
   ~/.venvs/qtrade/bin/python -m pytest -q                      # 仓库根,约 5 分钟
   cd winagent && ~/.venvs/qtrade/bin/python -m pytest -q       # WinAgent
   ~/.venvs/qtrade/bin/python -m pytest -q installer/tests      # 安装器规格对账
   cd docs && python3 check-truth-tables.py                     # 文档真值表,须 exit 0
   # 控制台(仓库里不装 node_modules):
   rsync -a --exclude node_modules --exclude dist console/ ~/work/qtrade-build/console/
   cd ~/work/qtrade-build/console && npm test
   ```

   | 命令 | 2026-09-21 07:20~07:23 CST 实跑 |
   |---|---|
   | 根 `pytest -q` | **2136 passed** |
   | `winagent` `pytest -q` | **329 passed**(真 Windows 上同样 329) |
   | `installer/tests` | **122 passed**(另有 Windows 侧 Pester 633,须在 Windows 跑 `installer/tests/run-pester.ps1`) |
   | `docs/check-truth-tables.py` | **exit 0** |
   | `console` `npm test` | **151 passed** |

   > 系统 python 没装 pytest,必须用 `~/.venvs/qtrade/bin/python`(uv 建的 venv,**没有 pip 模块**;打 wheel 用 `/usr/bin/python3 -m pip wheel`)。

3. **读 `.claude/skills/qtrade-redroid-resume/SKILL.md` 全文**,重点 §4 禁区、§5「🔴 最新状态」与「下一步」、§5b 编排纪律;
   再按你这批要动的东西读对应规格段落与 `reference/e2e-lessons-2026-09-21.md`。

## 4. 现在卡在哪(2026-09-21 清晨)

安琳要的终点 = **一个能装、装完各功能能用的单 EXE 安装包,且须经端到端验证**。

- 13 项载荷齐全的正式包**出过一版**:`installer/out/QTrade-Setup-1.0.0.exe`,**2,258,242,349 B**,sha256 `11060a07…64ecd5a8`,**未签名**、**从未在任何机器上运行过**。
- 🔴 **那一版装上必定起不来**:三轮端到端测试揪出首装 P0(首启脚本调的 `--init-db` 参数当时不存在)与「控制台连真后端一条请求都发不出」(版本头 1.3 vs 1.0 ⇒ 全线 426 等五处硬伤)。
- **这两类问题已修,但修复后的包还没重出;真装验证尚未开始。**
- 本地有一批提交未推到 GitHub(2026-09-21 07:25 实查 **ahead 6**,当时还有 agent 在陆续提交——**以 `git status -sb` 实查为准**),待文档第七轮终审 ACCEPT、全套复跑全绿后一起 push。
- 教训(值得先看一眼):**四套单测全绿 ≠ 装得上** —— 跨部件接缝没人对账、前端对着自己的 mock 开发 = 自证、构建不报错但产物是坏的。
  逐条见 `.claude/skills/qtrade-redroid-resume/reference/e2e-lessons-2026-09-21.md`。

## 5. 下一步(按先后)

1. **重建发行版 rootfs**(2026-09-21 07:25 实查:已有 agent 在改 `installer/rootfs/`,接手前先确认这批的进度与交接,别重复开工。六项待修:firstboot 里那道不该有的 `--init-db` 前置闸门、`StartLimitIntervalSec` 挪到 `[Unit]`、Python 由 3.11.0rc1 换正式版、机型档案库落点改到 `docs/05` §7 的路径、README 里已裁决却仍标「待裁决」的陈述、**先 `rm -rf build/` 再重打 wheel**)。
2. **独立复测**(由**另起的**测试方做,不由实现方做):rootfs 冒烟 + 控制台↔真后端复测。
3. **生成自签名证书 → 带 `-Sign` 重出正式包 → 校验**(`installer/signing/`;判据 = manifest `lightweight=false` + `missing` 空 + `7z t` `Everything is Ok` + 三段拼接算术 + 包内 exe 的 sha256)。
4. **真装验证**:`fieldtest/真机验收手册.md`(物理机为主)/ `fieldtest/vm-lab/`(虚拟机为辅)。
5. **文档第七轮终审 → ACCEPT 后 push**。

## 6. 要问安琳的事(未决,别自行假设)

- 生成自签名证书的两条命令是否现在跑。
- **发送失败是否计入限速**(倾向计入,护号)——开放项 R6-60 (f)。
- `#88` settings 读回「当前生效值」而非「已保存待重启值」⇒ 表单回填旧值,正式设计待定。
- 磁盘门槛是否按实测体积重算(现偏保守 = 安全侧)。
- 提供另一台 Windows 物理机(Win10 22H2/Win11、x64、BIOS 开虚拟化、≥8 GB 内存、≥40 GB 空闲盘)。
- 将来正式分发用公司内部 CA 还是 OV 证书(现阶段自签名 + 目标机导入)。
- 分支何时合 `main`;要不要把这一版回给 cursor 做第九轮评审。
- **启用 Hyper-V 需要重启主机一次**(会中断 WSL 内 12 个容器)——由安琳决定时机,任何 agent 不得自行重启。

## 7. 三条硬规矩(展开见 `CLAUDE.md` 与 SKILL §4/§5b)

1. **禁区**:绝不自行 `wsl --shutdown` / 重启 WSL 或容器;企点/微信是**真实工作账号**(会离开本机的动作要先复述等确认,读消息一律走旁路读库、不 attach);
   真机 `C:\ProgramData\QTrade` 在非真装期间**必须不存在**;**未经安琳同意不运行**产出的安装 EXE / WinAgent exe;docker 只 `build/create/export/save/pull` 且用 `qtrade-build/` 前缀。
2. **多 agent 编排纪律**:角色独立(**被某道质量门挡住的人不修那道门**,验收用例只由独立验收方改);agent 关了就新起对口的;一个 agent 只给一批活;重活不用 sonnet;重要产出落 `.omc/handoffs/` 且边做边写盘。
3. **改文档的固定流程**:改 `docs/` → `cd docs && python3 check-truth-tables.py`(exit 0)→ 仓库根 `pytest -q`(代码↔文档对账在里面)→ `git commit` → `git push`。
   新裁决**追加到 `docs/00` §15g 末尾、编号续 R6-N**(别插中间,会让编号漂移);新增脚本规则须在改前备份上**反向验证能红**。
