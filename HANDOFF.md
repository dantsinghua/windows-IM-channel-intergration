# 交接入口 / HANDOFF(2026-10-08)

> **2026-10-08 最终整合当前入口：**安琳已确认前端全部重改完成，原等待 UI 状态解除。已找回 HyperV-Exchange 安装报告并按当前源码修复，正式口径合入 R6-80/81/82/83；统一目录迁移、最终全量回归与独立验收仍在进行。先读 [本轮阶段交接](docs/handoffs/2026-10-08-final-integration.md)及下方 §3h。旧“嵌套 WSL 完全不能启动”仅为历史判断，已被 10 月 8 日完整 WSL 启动成功证据替代；真实 kcheck/切内核/真装仍未验证。

> **2026-10-08 21:01 UI 最新补充：**本 UI 工作流已完成首页细化并更新同一私有 Sites 预览，见 §3g。保留下方关机交接的真实账号、联调和目录迁移开放项；UI 源码与示例预览更新不等于这些事项已完成，不自动重载服务或启动真实账号。

> **2026-10-08 关机前最新入口：**先读 [阶段总结](docs/handoffs/2026-10-08-stage-summary.md)与下方 §3f。等待另一客户端UI明确交付后再做API对齐、联调和正式目录迁移；§3e的UI工作及更早快照均保留，不据旧测试数字宣布当前通过。现行规范以 `AGENTS.md` 与安琳当前指令为准。

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
| `docs/` | **唯一真值**:`00` 基线 + `01~06` 六册 + `07` 配置总表 + `check-truth-tables.py` 真值表对账器(改文档后必跑,须 exit 0)。裁决表在 `00` §15g,当前主树已承接 **R6-80 网络/S4、R6-81 最终 UI、R6-82 安装器、R6-83 QQ**，验证范围见 §3h | `docs/00-共享基线与口径.md` §14/§15~§15g |
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

1. **先只读检查**:核 cwd、`git status --short --branch`、HEAD和在途任务，读 `AGENTS.md`、本文件最新节及项目skill。原“先fetch/pull再读skill”要求自2026-10-08废止；不自动fetch/pull/检出/快进，不覆盖并行编辑。
2. **按当前任务选择验证，接手不自动全跑或启动服务**。下列命令/数字保留为2026-09-21历史入口，先核路径、假后端及授权范围；本次关机收尾不运行这些业务测试：

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

3. **读 `.agents/skills/qtrade-redroid-resume/SKILL.md` 与 `.codex/PROJECT_CONTEXT.md` 的最新交接**；原 `.claude/` skill仅作历史追溯，不覆盖当前规范；
   再按你这批要动的东西读对应规格段落与 `reference/e2e-lessons-2026-09-21.md`。

## 3b. 🔴 2026-09-21 23:45 交接快照(带签名正式包已出、独立校验通过;下文 §4~§6 已同步到同一时点)

> 更细的版本在原机 `.omc/handoffs/team-lead-status.md`(git 忽略、不随仓库走)。本节是它的可携带摘要。

**现状一句话**:**带签名的正式包已出并经独立校验「通过」**,下一步是真机装验证(要安琳定时机:重启主机启用 Hyper-V + 提供另一台物理机)。HEAD 见 `git log`(本节写入时产品侧最后一个提交 = `801a52d`,其后只有文档提交),工作区干净、已全部 push。

**正式包**:`installer/out/QTrade-Setup-1.0.0.exe`(`installer/out/` 被 git 忽略,只在原机)—— **2,259,695,672 B**,sha256 **`d10f46b7b7a1708914ef1951ca3f189efe4760b09e4047c56041e822aacc504c`**;Authenticode 自签名,指纹 `E36AFD96A801DD666F953AC8E3B3C992AB8CD60E`(`CN=QTrade Internal Code Signing, O=QTrade`,2029-09-21 到期,带 DigiCert 时间戳;目标机须先按 `fieldtest/真机验收手册.md` §1.4 导入证书)。包内 `install/manifest.json` sha256 `0c444a2d…fa13f7f7`(`lightweight=false`、`missing` 空、13 项载荷 + 引擎)。⚠️ 同目录留着作废旧包 `*.void-20260921-0306`(凌晨 03:06 未签名那版,装上必起不来),**分发前按 sha256 核对,别拷错**。**这个包至今没在任何机器上运行过。**

**包里装的是什么(全部溯源闭环,独立校验方自己重算过)**:
| 载荷 | 来源 / 指纹 |
|---|---|
| rootfs.tar | 2,171,043,840 B,sha256 `732d1169…137a9fc3`(第四次重建;依赖锁 + 两处 `--only-binary=:all:` + 构建期逐包 wheel 断言 + pip 钉 26.2.1) |
| Agent wheel | 506,656 B,`d28efc15…fefbb9de3`(源码 = 冻结点 `c7d48d9`,之后 `src/` 无改动) |
| WinAgent svc / user | 冻结代码重打(内置 pytest 门 329 passed);出包时在预签副本上覆盖重签 ⇒ 包内 sha256 = `7fbc363a…` / `a6f0ca2a…`,剥签名块后与产物根原件 `ED9C0685…` / `7E16E37B…` 逐字节相同 |
| 控制台 | 冻结代码重打(`console/` 最后提交 `f06de76`;单测 234 + typecheck + lint 全绿);`app.asar` = `fde6df63…`,主 exe 剥签名后 = `8e9d7a04…` |
| 安装引擎 | 19 个 ps1/psm1 剥签名后与 HEAD `installer/engine/**` 逐字一致 |

**本段(19:50~23:45)做了什么**:①WinAgent 两 exe 重打 + 签名;②发现交接漏项——产物根的控制台载荷是 01:56 旧件、其后 11 个 console 提交没进包 ⇒ 重打;③新 rootfs 独立复测三轮:第三轮揪出 **E3-1**(锁同时登记 wheel+sdist hash,wheel hash 坏了 pip 静默退回 sdist 联网现编)→ 修(`0c621f0`)→ 第四轮揪出 **E4-1**(websockets 有两个可用 wheel,平台版 hash 坏了静默改装纯 Python 版)→ 按「类」修(`801a52d`:`wheels.expected` + `verify-wheels.py` 构建期断言,顺带 E3-O1 钉 pip)→ **第五轮通过**(对断言做了 27 个定点篡改全部拦下);④真机验收手册补 §1.4「先导入证书」(`95603a7`)并回填 §1.1 签名包数值;⑤出带签名正式包 + 独立校验(8 项全过)。

**接下来(都要安琳定时机 / 给资源,任何 agent 不得自行执行)**:
1. **重启主机启用 Hyper-V**(`fieldtest/vm-lab/01-启用HyperV.ps1`,先 `-WhatIfOnly`;重启会中断 WSL 内全部 13 个容器、企点登录态、qb 行情采集;网络异常立即 `01b-撤销HyperV.ps1`)→ 建虚拟机 / 装 Win11 / 打 `clean-baseline` 检查点(`02`/`02b`/`03`)。
2. **真机装验证**:`fieldtest/真机验收手册.md` A~I 九组 51 条,另一台物理机为主 + 本机虚拟机为辅。**未经安琳同意不运行安装 EXE。**
3. 验完再谈:分支合 `main`、要不要回给 cursor 做第九轮评审。

**攒着没做的(不阻塞真机验证)**:
- rootfs 线:E3-O2(janitor 单元无 `StartLimit*`,涉及规格待裁)、E5-O1(断言失败消息 sha 只显示前 12 位)、E5-O2(同名 dist-info 多份时取最后一份)、apt 层与 `FROM ubuntu:22.04` 未钉版本 / digest、Agent 本体 wheel 无 hash 安装(靠 `contents.json` 登记)。
- 打包线:`winagent/build/build.ps1` 签名步调裸 `signtool`(不在 PATH 时必败,应复用 `Find-QtSignTool`);manifest 对 `winagent/app/*`、`console/*` 是通配条目、无单文件 sha256 ⇒ 安装期无法按 manifest 复核这两个目录(V3-4);包内 `winagent/python/site-packages/bin` 有两个同名未签名的 pip 启动器(V3-2,引擎不用);控制台 `npm audit` 19 条依赖告警;控制台仍用默认 Electron 图标。
- 文档 01 侧登记、待实现项(企点名称列 UTF-16 异或解密等)、待安琳裁的几条(#95 / #42 / R6-60 f / #88 / 磁盘门槛)、前端连带 —— 原样沿用上一版快照,见 `.omc/handoffs/team-lead-status.md` §5。
- 产物根 `rootfs\out\` 攒了 5 份旧 rootfs tar(各约 2.1 GB)、`winagent\dist\*.old-20260921`、`console\release\win-unpacked.old-20260921`、构建副本 `~/work/qtrade-build/console-pack-2/` —— 删不删由安琳定。

**新增工作纪律**(已入项目记忆):🔴 **绝不 `cd` 进会被打包收集的目录**(`installer/out/**`、`winagent/dist/**`、`console/release/**`、产物根)——本机 OMC 钩子会在当前目录写 `.omc/state/…`,被通配收集带进安装包(本段出包第一轮因此作废重跑);一律绝对路径,出包后的校验必须含「包内与 stage 零 `.omc`」。实现方的自检脚本不能当结论(第三批自检 `docker exec` 缺 `-i` ⇒ heredoc 检查空跑恒通过,靠独立复测兜住)⇒ 每条检查都要有「确实执行了」的证据。其余沿用:发给忙碌 agent 的消息要等它这轮结束才到;收尾只 `kill <自己 PID>`、禁 `pkill`/`killall`;删目录前 `git ls-files`;复跑用独占副本;只按精确路径 `git add`;打 wheel / 全量前 `rm -rf build/` + 清杂散 `.omc/`。安琳直接带的羿珩行情采集 + 企点只读监控两条线不在安装包关键路径、已收口。

## 3c. 🔴 2026-09-26 15:30 交接快照(cursor 联调提交经评审+两轮返修+两轮独立验收后已推送;**正式包未重出**)

**现状一句话**:开发分支 `claude/lucid-dijkstra-uu5max` 已推到 `47b16cb`(11 个提交:cursor 的联调提交 `c86a670` + 10 个修复/返修);第二轮独立验收 **ACCEPT**、五套检查全绿;**但 9/21 的正式包没有重出**——包里还是旧引擎/旧代码,真装验证要等重出重签。

**这批改了什么(评审编号见 `.omc/handoffs/review-bcda8e6-2026-09-26.md`,原机 git 忽略)**:
- 引擎(installer/engine):首个步骤前引擎脚本落盘(`dontcopy`+`ExtractTemporaryFile` 到 `{tmp}\qte`)、向导页不再展开 `{app}`、`RunStep` 不阻塞界面且可取消、告知页 `wsl --list` 3 s 超时 + `WSL_UTF8`、内核失败文案看回滚结果、dmesg 在注销 kcheck 前抓、`.cmd` 全 ASCII、`AppName=QTrade`、`JsonStr` 反转义;**新增 ISCC 编译门 + `[Code]` 保留字守卫**(`installer/tests/test_engine_iss_compile.py`)。
- Agent(src/qtrade_agent):画面流执行体 = **scrcpy-server 4.1**(`screen_scrcpy.py`:先连视频/控制两条 socket 再读头、4 字节 codec id + 12 字节 session 包、SESSION/CONFIG/KEY = bit63/62/61、touch/scroll/key/text 直写控制 socket 不经 shell、scroll ÷16、断线/换档/#101 重建并发 `{type:'restart'}`);删 cursor 的企点登录假升级;`rotate` 控制帧删;#35 REST 兜底 `shlex.quote` + 规格外字段 `duration_ms`;contacts 端点异步化。
- WinAgent:`serve(allow_sid=)` 三处签名同步(4 个测试文件挂死的根因)、磁盘检查走 `SysBackend.path_exists`、`host_snapshot` 首轮不回 0、Windows `SO_EXCLUSIVEADDRUSE` + WSL 地址缺失不阻断启动并周期补绑、`UserAgentLink` 可停。
- 控制台:恢复硬解→软解→静态预览三档、最小化暂停、`setPointerCapture` + down/move/up 原样透传(长按 = 不发)、SPS 变化重配、解码背压、vue-tsc 归零、浏览器调试 README(`QT_DEV_TOKEN=… npx vite`)。
- vm-lab 脚本:`$ProgressPreference`、`$PSScriptRoot` 兜底、文案/编号。

**验收数字(第二轮,验收方副本实跑)**:Agent+installer pytest 2526 / winagent 515 / console vitest 289 + tsc 0 / Pester 655(4 跳 = SfxStub 缺二进制)/ ISCC `Successful compile`。

**待安琳裁决(合成一批 R6-69~,文档方回写 + `check-truth-tables.py`)**:①N1:scrcpy 4.1 视频线程一结束整个 server 退出,A.2「暂停断视频留控制」做不到 → 改语义或接受断连自愈;②01 §2.7.4 丢帧阈值 3 vs 代码 6;③#101 真重建推翻 R6-58(ai);④#34 注入级别 R vs W;⑤安装取消退出码 10 撞「停车等用户」→ 新码;⑥补登:01 元素表删 rotate、#35 `duration_ms`、配置键 `scrcpy_server_path/_version`、`GET /accounts/{id}/contacts`;⑦可接受:微信盘不存在只 warn、删假升级后手动登完仍须走 #12。

**真机才能验(全卡在 `qtrade-redroid` 未拉起,安琳自己 `up.sh` 后先 `scrcpy -s 127.0.0.1:5555 --max-size 720` 验编码器)**:两条 socket 实际顺序与 session 包、N1 实证、滚轮距离、触控坐标、中文输入(scrcpy text 打不出中文,走 ADBKeyboard)、静止画面 10 s 是否误触发 H07 重建、安装器运行期(取消/重绘/内核失败文案,内核切换只能物理机)、WinAgent 独占绑定与补绑。**本机 Hyper-V 虚拟机里 WSL2 起不来**(原装内核也超时,见 `.omc/handoffs/vm-rehearsal-2026-09-22.md`),只能验 KERNEL_SWITCH 之前的步骤。

**编排纪律新增**:worktree 隔离派工的三个坑(起点是旧提交要 reset、写不了主树 `.omc`、跑不了 powershell/ISCC)见记忆 `agent-worktree-gotchas`;合并用 cherry-pick。`qb_tap.user.js`(与本项目无关)已从提交剔除并 gitignore。

**2026-09-26 晚补记(scrcpy 三、四轮返修 + 真机实证,已推送)**:安琳指令拉起 `qtrade-redroid`(`up.sh`,10 s 就绪,serial `emulator-5554`),总控用随包 4.1 server 跑真机探针 + 真 Agent `ScrcpyBackend` 端到端冒烟,全部通过:`app_process` 拉起、`OMX.google.h264.encoder`、连接顺序(先连齐两条 socket 再读头)、12 字节 session 无载荷、首关键帧 8 ms、touch/scroll/key/text 注入生效、pause 0 帧、`RESET_VIDEO`→关键帧 63 ms、第二订阅者 3 s 内出画、close 后 5 s 拆 forward、server 退出。真机才发现的三条已修(r3/r4,独立验收 ACCEPT):静止画面 0 帧是正常(健康判据改为进程/socket 存活)、`i-frame-interval` 无效(新观看者/resume 用 `TYPE_RESET_VIDEO`=17)、慢客户端 RESET 风暴(2 s 节流 + 队列上限 + 连续掉队摘下发 restart + 发帧 5 s 超时 1011)。N1 坐实:断视频 socket 即 server 退出 ⇒ 暂停按「只停转发、socket 不动、resume 发 RESET」实现,**A.2 仍待安琳裁决**。新增待登记:关闭码 1011、`[adapters.qidian]` 4 个新键(节流/掉队/key_wait)。细节:`.omc/handoffs/realdevice-scrcpy41-2026-09-26.md`、`accept-r3/r4-*.md`。探针触摸曾落在桌面搜索框(企点未在前台),已按 BACK 恢复桌面;scrcpy text 注入丢首字符,中文一律走 ADBKeyboard。

**2026-09-26 深夜补记(九项裁决落地 + H07 告警,已推送)**:安琳当晚定了 **R6-69~R6-77**(A.2 暂停 = 只停转发不断 socket/resume 发 RESET_VIDEO;01 §2.7.4 丢帧阈值 6;#101 真重建覆盖 R6-58(ai);#34 注入提到 **W** 级(R 令牌注入 ⇒ 审计 `ws_rejected` ⇒ 关闭码 **4403**,控制台转只读不自动重连);安装取消退出码 **11 `E_INSTALL_CANCELLED`**;发帧超时关闭码 **4408**;#35 `duration_ms`;`[adapters.qidian]` 六个 scrcpy_* 键与 `GET /accounts/{id}/contacts`(#3b)登记;01 元素表删 rotate、加只读横幅/按钮),文档方回写 00/01/02/03/04/07/README/验收手册,对账脚本新增 ⑮ CROSS 8 条 + 「画面流健康旧判据」FORBIDDEN(改前备份全红);代码方落地三条 + 控制台 4403;随后按安琳裁决**补上 H07 告警接线**(`H07_SCRCPY_STALLED` warn/resolve,`/system/health` H07 三态;真机:杀 server 同刻 firing、+0.30 s resolve)。三轮独立验收(裁决批 ACCEPT;H07 批 REVISE×2 → ACCEPT:两次都是文档半改——引用了 worktree 内 SHA `870bdb4`、04 H07 行与 F-07 口径打架——**其中一次是总控代改文档所致,已记入记忆**)。数字:pytest 2556 / vitest 296 + tsc 0 / Pester 658 / ISCC 编过 / 对账 exit 0。真机另证:`forward --remove` 不打断已建立的流(A5-06 造故障改为杀 server)。遗留:G1 `main._attach_screen` 的 `alerts=` 接线无用例守护;00 R6-69 引 02 行号已偏(历史描述);SKILL §5 那条 NEGATION 收紧待办仍开着。**正式包仍未重出**。

## 3d. 🔴 2026-09-27 01:30 交接快照(**1.0.1 带签名正式包已出、独立校验两轮 → ACCEPT**;版本对齐本机)

**包身份**:`installer/out/QTrade-Setup-1.0.1.exe`(git 忽略,只在原机)—— **2,250,367,904 B**,sha256 **`4c93508df200c27396f5e24be2f318a0c954a0d74f057aa31f460c603a39326a`**,外壳签名指纹 `E36AFD96…D60E`(DigiCert 时间戳 2026-09-26T17:05:02Z),源码 **`0f7a7fb`**;包内引擎 1.0.1(VersionInfo/AppVersion/`package_version` 三处均 1.0.1);manifest 14 条、`lightweight=false`、`missing` 空。同目录旧件:`1.0.0.exe`(9/21,**作废**:引擎自报 1.0.0 + WinAgent exe 启动即崩)、`1.0.0.exe.void-20260921-0306`、`1.0.1.exe.void-20260927-r1`(第一轮,引擎版本错)——分发只认 `4c93508d…`。

**安琳定的对齐口径(2026-09-27)**:WSL 本体 MSI = **本机 2.5.9.0**(`C:\Windows\Installer\98b31.msi`,与 GitHub 官方 `wsl.2.5.9.0.x64.msi` sha256 逐字节相同 `ffc88065…`;替换 9/21 的 2.7.14);内核 `bzImage-6.6` `35a985bc…` = 本机现役 `bzImage.v4`;redroid `d1ca0815…`、napcat `2cc70b45…` = 本机 docker;scrcpy-server 4.1、platform-tools 37.0.1、发行版内 `docker.io` **不对齐本机**(Agent 按 4.1 协议实现且真机全绿;规格 §2.7.3 定 docker.io);包版本 1.0.1。

**载荷溯源(独立校验方逐项重算)**:rootfs.tar 2,171,279,360 B `7dc85006…`(ubuntu:22.04 基础镜像 digest `b8b6ee6a…` 与 9/21 相同;`--require-hashes`/`--only-binary`/wheel 逐包断言照常;基础镜像因代理 7890 失效由构建方直连拉取后 `docker load`,docker 配置未改)| Agent wheel `8c1532d3…`(HEAD `src/`,含 `screen_scrcpy.py`)| WinAgent svc/user 剥签名 `b2762218…`/`a27e5dad…`(**第三轮**才过:第一轮被平台用例挡、第二轮 exe 启动即崩)| 控制台 app.asar `5db066df…`、主 exe 剥签名 `14935289…`(含 4408/4403/只读模式)| 引擎 19 脚本剥签名 = HEAD。

**本轮揪出的三条潜伏缺陷(9/21 包也有,都已修入 `dda050e`~`0f7a7fb`)**:①WinAgent 两个 PyInstaller exe **启动即崩**(spec 把包内 `main_*.py` 当入口,相对导入失败;user 的 spec 还排除了 fastapi;venv 缺 `websockets`)→ 包外薄入口 `entry_*.py` + `--selfcheck` + build.ps1 冒烟门 [5/6] + pyproject 补 websockets + 守卫用例;②`.iss` `#define EngineVersion "1.0.0"` 无条件覆盖 `/D` 传入值 → `#ifndef` + 用例;③winagent 一条用例假定 POSIX socket 语义,真 Windows 上红 → 改平台无关。**教训(已进记忆 `exe-smoke-gate`)**:pytest/签名/sha 全绿都不证明产物能起,打包线必须对产物做最小执行。

**独立校验 11 项(`.omc/handoffs/verify-package-1.0.1-r2.md`)**:大小/sha/签名、三段切段 + `7z t`、全表零杂物、manifest 7 条 sha、版本对齐逐项对本机、引擎 20 文件零间隙、三 exe 剥签名、**从包内解出的 svc/user 真跑 `--help`/`--selfcheck` 退出码 0**、rootfs 抽件 sha、SHA256SUMS 17/17、B1 专项。遗留(非阻塞):通配条目无单文件 sha(老问题);svc `--help` GBK 输出;升级路径是否真走 upgrade、目标机导入证书后签名 Valid、安装器运行期行为 —— **全部要真机**。

**接下来**:①清理中间产物(安琳 2026-09-27 指令,清单先过目);②真机验收:先按验收手册 §1.4 导入证书,再按 §1.1 核 `4c93508d…`,本机 Hyper-V 虚拟机只能验到 KERNEL_SWITCH 之前(WSL2 在嵌套里起不来),内核切换及之后要物理机;③浏览器联调(拉 Agent + `QT_DEV_TOKEN=… npx vite`)前先只读核 Agent 启动对企点账号的副作用。

## 3e. 2026-10-08 账号中心与紫金 UI 升级交接(R6-81)

安琳本轮要求以 IM 账号为主体重排 `console/` 全部保留页面,并明确删除复杂低频配置及指令台。本轮以工作区源码为对象,分支仍为 `claude/lucid-dijkstra-uu5max`、HEAD `f25489a`;已有其它任务修改保留,原仓库没有提交/推送,未重出安装包。R6-80 留给在途目录拆分候选,本轮不占用该裁决。

- **当前布局**:首页/消息/日志/资源/环境五项主导航,账号与邮件二级入口;首页为五指标、企点/微信/QQ 三组账号与代码事实架构。统一紫 `#7547A8`、金黄 `#F2AD38`、玻璃分层与柔和光晕,有减少动态和无模糊支持降级。
- **简化范围**:删除指令台、工作流、邮件模板三页,旧路由分别重定向 `/dash`、`/dash`、`/set`;偏好仅桌面提醒、托盘、登录后自启与只读告知。账号详情按通道承接画面或 NapCat,复杂参数/端口/批量调试退役;邮件只收件/发件/待确认;环境只基本检查、受控维护。没有删除后端能力、权限、确认、审计或 R6-78 登录编排。
- **数据事实**:WSL 内存/CPU 读取既有 #77;Windows 宿主实测未采集,累计消息缺聚合接口,故显示 `—` 并说明。首页告警为当前活动集合,日志告警为本次会话集合,均不声称完整近 7 天历史;资源池/账号配额明示预算,不冒充实占。真实部署卷仍按 R6-79。SQLite/redroid/NapCat 无独立探针时架构显示未知。
- **核心链路**:消息保留实际账号/会话/时间/关键词/方向/类型查询、分页和导出,FTS 条件不被实时前插污染;日志保留系统/操作查询及明确范围的告警列表。消息/日志的本地时间输入经 `console/src/utils/datetime.ts` 转成带 `Z` 的 ISO UTC 后查询,分页/导出共用同一筛选值,避免浏览器与 WSL 时区不同造成偏移;这不改变 API。账号深链不在首页分页时按 ID 补取;详情画面明确点击才打开。重启/回滚和邮件危险确认沿原保护,Agent #84 仅补严格 `confirm is True` 到 WinAgent 的透传。
- **最终开发者验证(r5,2026-10-08 19:50 CST)**:在独占副本实际运行类型检查 exit 0、完整前端测试 **21 文件 / 381 passed**(2.83s)、`TZ=Asia/Shanghai` 新 UI 专项 **26/26**(含本地 15:00 → UTC 07:00Z、回显/清空/导出范围)、renderer 构建 exit 0(5.36s)。#84 假后端专项沿用本轮 r3 **7 passed / 33 deselected**(1.10s),源码与测试再次逐字节核对一致。87 文件 SHA 复核匹配,该时点主树与副本 src/tests/unit/docs 无漂移。日志和清单见 `.codex/ui-redesign-20261008/developer-validation.md` 最终 r5 节及 `developer-evidence/`;保留主 chunk 超过 500 kB、既有 Ant 浅挂载与 Python 弃用警告。r3/r4 较早数字不再作为最终结果。
- **独立浏览器验收 ACCEPT**:Windows Edge 隔离预览覆盖 13 条保留路由的 1440/1024/390 宽度、焦点/减少动态、消息/日志查询分页、三通道与列表外账号深链、取消零提交和独立 stub 确认参数。r3 共 **78 项,77 通过/1 失败**;唯一首次设置 1024 宽装饰光晕溢出由实现修复,最终增量 **7/7 通过**并关闭(另含消息/日志上海 19–20 点 → UTC 11–12 点带 Z)。各轮资源 SHA 前后稳定,真实业务网络/运维动作/页面异常均为 0。完整证据与未覆盖边界见 `.codex/ui-redesign-20261008/acceptance/acceptance-report.json`、`acceptance-report.md`,最终截图在 `acceptance/final-delta/`;该 ACCEPT 限隔离 UI,不外推真登录/视频或真实动作。
- **独立文档回写**:原字节备份、SHA、增量及记录在 `.codex/ui-redesign-20261008/documentation/`;`docs/` 下 `timeout 120s python3 check-truth-tables.py` 与 `git diff --check -- docs HANDOFF.md .codex/PROJECT_CONTEXT.md` 均 exit 0,保留 5 项既有 MIRROR 提示。testid 动态模板的文档登记已按原门对齐,没有修改质量门。
- **隔离与交付边界**:开发副本为 `/home/anlin/work/qtrade-build/ui-redesign-20261008/console`;浏览器预览使用示例 fixtures,与真实账户无连接,未执行真实外发、ADB、重启、内核回滚或安装程序。Sites 设计预览不代表真实桌面桥、三通道登录/视频或安装器已验收。正式 1.0.1 包与真装开放项仍按 §3d,本轮没有替代该包。
- **接续入口**:裁决和完整代码证据在 `.codex/ui-redesign-20261008/decision.md`、`evidence.md`、`architecture.json`;实现交接为同目录 `implementation-shell.md`、`implementation-account-env.md` 与 `implementation-mail-setup.md`;最终页面/元素对照在 `docs/01-控制台前端设计.md` §2.7/§4。接手先重新核进程、工作区和本轮验证报告,不能仅凭预览截图启动真实 Agent 或复跑安装/账号操作。

## 3f. 2026-10-08 关机前续接：等待最终UI，再联调与正式迁移

安琳最新指令优先：另一客户端仍在美化UI并删增功能，先等其明确交付，不覆盖其页面、不应用旧目录迁移清单、不重载联调服务。§3e的UI交接原文保留；完整可携带事实与证据入口见 [阶段总结](docs/handoffs/2026-10-08-stage-summary.md)。

- 正式仍为 `console/`、`src/`、`tests/`、`winagent/`；`frontend/backend`只在布局候选。R6-80网络/S4规格尚未写回主树，R6-81 UI已在主树，合并前统一核编号与语义。
- 新企点 `qd82 / 3007378246` 已真实登录并确认running，3条新入向/3WS/0重复及页面/接口/主库hash吻合；**16:27 WAIT_PASSWORD/协议页是历史，不再作为当前状态**。未外发，未改历史核查的实时游标。
- 网络/S4候选2626通过、独立APPROVE；布局3308通过/1失败后纯注释收口专项1通过、文档exit0，尚无修后全量。前端352仅旧候选；私有页面guard144项通过但未live部署，notice未ack导航与B13新QQ首次QR仍待做。
- 关机前运行根、端口/PID见总结，仅作快照；开机重核，不凭旧PID终止或自动恢复账号。旧 `3007373675` 与旧redroid保持禁区，NapCat A一次重启授权已消费，无外发授权。

## 3g. 2026-10-08 首页细化与固定侧栏交付(R6-81 同日补充)

安琳在 §3e 界面基础上明确要求固定左侧底部功能、去掉首页底部重复内存/磁盘卡、重排顶部指标并改进架构分区/连线。本批只承接该 UI 细化；分支仍为 `claude/lucid-dijkstra-uu5max`、HEAD `f25489a`，原仓库未提交/推送，未应用目录迁移或替换正式安装包。§3f 所列其它任务成果和开放项保留。

- **页面结果**：浏览器/Electron 壳层按可视窗口限高，右内容独立滚动；左下偏好、工作空间和折叠操作留在窗口底部，短窗口上部导航可以独立滚动。首页仅顶部五指标、三通道账号、系统架构；架构下两张重复大卡删除。架构分 WSL 紫色、Windows 金色区块，用圆弧连接既有真实关系；窄屏保留分区，隐藏拥挤连线，不增加虚构调用或健康信号。
- **指标与清理**：“总内存”采用已向安琳说明的默认解释：宿主物理内存总容量，辅文显示已用量，不是 WSL 已用或程序预算。硬盘容量为当前客户端目录普通文件长度合计，硬链接去重、跳过符号链接/联接，不声称为分配簇占用。标题旁 `qt-dash-cleanup` 只跳 `/res?section=cleanup` 并聚焦资源清理区，零自动清理；原受控清理确认与作业反馈未改。#77/R6-79 的部署卷、水位和保护事实仍在资源页。
- **只读桌面采样**：新增无参数 `qt.app.localMetrics()`，主进程固定安装版 EXE 所在目录或开发版应用目录，返回内存/目录数字与有限错误码，不回路径、文件名、文件内容或任意目录权限。默认 5 秒、100000 项、64 层预算，30 秒缓存，并发合并；失败或不完整为未知，真实零有效。普通浏览器/旧桥无能力时显示 `—`，不混用 WSL 或分区数值。实现交接见 [本机指标说明](.codex/ui-home-refinement-20261008/metrics-implementation.md)。
- **本轮开发验证**：独占 ext4 副本 `/home/anlin/work/qtrade-build/ui-home-refinement-20261008/console`，r2 完整前端 **22 文件 / 403 passed**，类型检查与 renderer/Electron 构建均 exit 0。随后 r3 仅修正 `SystemTopology.vue` 的 SVG `gradientUnits`，消除水平/竖直段因零包围盒而不可见；增量类型检查与 renderer 重建 exit 0，**没有再次运行 403 项全量**。最新 112 输入 SHA 与工作区全匹配，36 产物登记；见 [开发者报告](.codex/ui-home-refinement-20261008/developer-validation.md)及 `developer-evidence/source-and-artifacts-r3.json`。既有 Ant 浅挂载警告和 chunk 大小提示保留。
- **独立浏览器 ACCEPT / 只读终审 APPROVE**：[本轮验收 15/15](.codex/ui-home-refinement-20261008/acceptance/report.md)覆盖 1440/1920/1280/390 四尺寸、1280×420 短窗和手动折叠；右侧滚动前后左下控件坐标不变，无横向溢出。390/1440 的清理入口均定位并获焦，非 GET 请求和确认弹框为 0；r3 六条架构路径直段/圆弧完整，分区与 WinAgent 节点导航正确。截图以 `acceptance/r3-*` 为最终架构证据。[独立终审](.codex/ui-home-refinement-20261008/final-review.md)重算 112 输入 SHA 全匹配，未发现本批 P1/P2。既有折叠后按钮标题仍为“折叠导航”属非阻断低优先级文案观察，实际可展开，未扩大本批修改范围。
- **同站私有发布**：[QTrade 紫金工作台](https://qtrade-purple-gold-workspace.tartfrost.chatgpt.site) 已于 `2026-10-08T13:01:26Z` 更新成功，仍为 owner-private；Site 源提交 `59b98d0ec3f51b0d1b7a85db2ade0a8a0f16972b`、部署 `appgdep_6ac7941e4f0c8191a57a783d7d7631b7`。身份与版本见 [发布记录](.codex/ui-home-refinement-20261008/site-publish.json)。使用明确标识的示例数据，未连接真实账号；Sites 示例桥不是 Windows 客户端真实采样验收。
- **文档与边界**：正式规格同步 00/01/04 的 R6-81 同日细化，原字节备份与校验见 [文档记录](.codex/ui-home-refinement-20261008/documentation/report.md)。本批未运行 Electron、安装 EXE、WinAgent、真实 Agent 或真实账号操作，未执行 ADB、清理、重启、内核切换或外发；ProgramData 检查仍不存在。源码构建通过不能替代已安装 Electron/真实清理/完整安装器验收。接手先核本批证据与当前工作区，再按 §3f 单独收口 API 联调和布局候选，不覆盖本批最终 UI。

## 3h. 2026-10-08 最终 UI 与安装日志整合（当前阶段）

安琳本轮明确 UI 已完成，授权继续两部分修复及最终回归；§3f 的等待状态不再适用。分支/HEAD 仍为 `claude/lucid-dijkstra-uu5max` / `f25489a`，大量未提交集成修改保留，未 commit/push 或重出安装包。

- **交换位置已找回**：`C:\Users\Public\Documents\HyperV-Exchange\QTrade-Diagnostic-20261008`，WSL 为 `/mnt/c/Users/Public/Documents/HyperV-Exchange/QTrade-Diagnostic-20261008`。报告是 9 月 22 日 **1.0.0** 安装问题及 10 月 8 日补充探针；清单 59/59 哈希匹配，子目录 60 文件。历史共享名 `\\LAPTOP-4NGU6M66\HyperV-Exchange` 本轮未重测 SMB。
- **纠正历史归因**：同一 VM 完整 rootfs 约 21.19s 返回 uname、Docker active；微型 kcheck 仍超时。§3c/§3d 的“嵌套 WSL2 起不来/只能验切换前”保留为当日快照，现行结论以这次成功证据为准。官方内核缺 binder 与载荷自编内核有 binder 分开；本轮未切内核，`/init` 镜像根因未证。
- **正式文档已承接**：R6-80 网络/S4 精确合入，02 DDL 镜像为总 27/失败 14；保留 R6-81 首页/导航细化并补微信启用、原地企点验证画面、QQ refresh-qr、notice 重试。R6-82 统一真实 argv/exit/stdin/日志、kcheck 写前 68 和 shutdown 非零 69、正常/resume 严格结果。R6-83 明确官方 4.18.28 PNG/disableWebUI、实际容器 ACCOUNT 与归属、新 WS 同 UID 收尾、K6 到期重试、QR 不进 outbox/webhook/重放。
- **验证按时点**：本轮 531 文件基线 Agent 2505 / WinAgent 523 / Installer Python 160 / 前端 403 通过；网络专项 201，WinWsl 11，UI 同一用例 9 RED→9 GREEN。安装 61/61 后源码变化，最终 Native 11/11、无害 probe 8/8；QQ 187 既有专项后又有 3 RED 返修，新增 QQ 专项最终 69/69。全部只归各冻结/增量时点，不能拼成最终全量或全域零回归。
- **当前未完**：正式目录迁移、含最终 QQ/安装/UI 的同一快照全量、独立浏览器与安装/QQ 验收、终审；新源码不在旧 1.0.1 包内。真实微型 kcheck/内核切换/完整真装/扫码重建和 K6 真实到期仍未验证，未新增真实操作授权。
- **接续材料**：[可携带阶段交接](docs/handoffs/2026-10-08-final-integration.md)含日志结论、修复/证据矩阵及顺序；详细材料在 `.codex/final-integration-20261008/`。文档原字节备份在 `documentation/before/`，本批最终 checker exit 0（5 既有 MIRROR 提示），未改门。后续迁移/验收结果继续回写同一交接。

## 3i. 2026-10-10 最终整合收口：候选修复回合主树 + 同一冻结全量回归

HEAD `5e15b6f`（10-09 以原目录布局提交了 10-08 的集成修改）。10-08 深夜落在 ext4 布局候选上的最后一批修复（R6-83 实时 WS 二维码 `live_payload`、#97 webui 幂等与 `until`、控制台登录尝试隔离/`self_uid`）与独立维护者的测试更新此前**未回到主树**；本轮按归一化差异逐文件回合（产品 6 文件、测试/配置/文档若干，新增 3 份测试），目录迁移仍未执行。

- **同一冻结版本全量**（主树 == ext4 副本，rsync 零差异）：Agent **2709** passed、WinAgent **534**、安装器 Python **160**、Pester **695/0/0**（PS 5.1）、文档门 exit 0、控制台 **425** 单测 / typecheck 0 / lint 0 errors / build 0、HTTP-WS 联调 **127**（真 Agent + 全假后端）、Playwright **47**、内置浏览器冒烟（五步向导含守卫窄例外、企点/QQ 建号到完成页、13 条路由零运行时错误）。
- 过程中修掉三处仅属测试/配置的红：三份新 Pester 测试缺 BOM、新增前端测试夹具 `trace_id: null` 类型错误、旧 e2e-real vitest config 的 alias 落点；均已在最终复跑中覆盖。
- 未覆盖：acceptance 专用宿主用例、Windows 真路径 WinAgent、真实 kcheck/内核切换/真装/扫码重建/K6、Electron 桥、目录迁移、重出包；1.0.1 包不含本轮源码。未 commit/push。
- **真实链路联调（15:42 安琳授权）**：隔离的真 Docker/adb/scrcpy Agent（17650，容器 `qtrade-qd90`，不碰历史容器）首轮停在 `UI_UNEXPECTED`，真机排出四条此前被 [推测] 假树掩盖的缺陷并修复：`uiautomator dump /dev/tty` 在非 tty 会话不出 XML（改落 `/data/local/tmp` 再读）、协议弹窗「同意」锚点被正文/「不同意」抢先（`find_node` id 优先 + `dialogRightBtn`）、账号框无 resource-id（加 desc 锚点）、点登录后二次协议弹窗未处理。修后真机 17 s 到 `WAIT_PASSWORD`，#33 真截图与最终 UI 的 scrcpy 视频流均通过；新增 `tests/test_qidian_real_device_697.py`（真机控件树），企点相关 425 passed，Agent 全量复跑见交接。`app_version` 回填缺失、登录后锚点仍待真机核对。
- **安琳当日 UI 指令（均已落源码并过单测/文档门）**：16:34 账号详情页重排（操作入头部、状态卡入右栏、名称 hover 原位改名）；17:10 **删除首次设置向导「阅读须知」步 → 裁决 R6-84**（向导四步、守卫只看 `done`、`qt-setup-notice-*` 退役、告知仅偏好页只读查看），控制台单测 423、Playwright 44、typecheck/lint 0 errors。另 Electron 壳 CSP/`connect-src` 写死 17600 导致真实链路全部 API 被拒已修（`electron/main/origins.ts` + preload `qt.endpoint.agent`）。17:40 再修三处：托盘对已销毁窗口崩溃（`closed` 置空 + 托盘重建窗口）；**R6-85** Agent 加 `[api] console_origins` CORS 放行（默认只 `null`，回环源可配，预检不进审计），否则桌面壳跨源直连 Agent 一条都发不出去；**R6-86** 微信向导步①② 以 02 #28/#29 为准（01 旧字段 `module_enabled/user_agent/match` WinAgent 从不返回），白名单③补 `wechat.version-match`。
- 细节与命令见 [本轮可携带交接](docs/handoffs/2026-10-10-final-regression.md)，本机证据在 `.codex/final-regression-20261010/`。

## 4. 现在卡在哪(2026-09-21 深夜)

安琳要的终点 = **一个能装、装完各功能能用的单 EXE 安装包,且须经端到端验证**。

- ✅ **带签名的正式包已出、独立校验 8 项全过**(身份、载荷溯源、校验要点见上面 §3b)。它修掉了凌晨那一版「装上必定起不来」的全部已知缺陷(首装 `--init-db`、控制台连不上真后端、rootfs 六项、依赖锁两处绕过)。
- 🔴 **但它至今没在任何机器上运行过** —— 「校验通过」只证明包的身份与内容正确,**不证明装得上、跑得起来**。真装验证是下一步,而且**卡在两件要安琳定的事**上:重启主机启用 Hyper-V 的时机、另一台 Windows 物理机。
- 教训(值得先看一眼):**四套单测全绿 ≠ 装得上**;出包夜又添五条「看起来过了、其实没验到」(交接漏了一项旧载荷、依赖锁承诺两次被证伪、实现方自检空跑恒通过、`cd` 进产物目录把钩子文件打进包、重签改变 sha256)。
  逐条见 `.claude/skills/qtrade-redroid-resume/reference/e2e-lessons-2026-09-21.md`(16 条 + 五条通用判据)。

## 5. 下一步(按先后)

1. **安琳重启主机启用 Hyper-V**(`fieldtest/vm-lab/01-启用HyperV.ps1`,先 `-WhatIfOnly`;任何 agent 不得自行执行)→ 重启后拉回容器、核 Hyper-V 状态 → `02b` 应答 ISO → `02` 建虚拟机 → 装 Win11 → `03` 打 `clean-baseline` 检查点。逐步说明与撤销法见 `fieldtest/vm-lab/README.md`。
2. **真装验证**:`fieldtest/真机验收手册.md` A~I 九组 51 条(另一台物理机为主 + 本机虚拟机为辅);**先按 §1.4 导入证书**、按 §1.1 核包的 sha256(别拷成同目录的 `.void` 作废旧包)。**未经安琳同意不运行安装 EXE。**
3. 真装揪出的缺陷 → 新起对口 agent 修 → 独立复测 → **整包重出重签**(外壳签名覆盖整个 EXE,改任何载荷都必须重出)。
4. 验完再谈:合 `main`、cursor 第九轮评审、§3b 里「攒着没做的」那张清单。

## 6. 要问安琳的事(未决,别自行假设)

- **何时重启主机启用 Hyper-V**(会中断 WSL 内 13 个容器、企点登录态、qb 行情采集、企点只读监控)——由安琳决定时机,任何 agent 不得自行重启。
- 提供另一台 Windows 物理机(Win10 22H2/Win11、x64、BIOS 开虚拟化、≥8 GB 内存、≥40 GB 空闲盘)。
- 产物根与 `installer/out/` 里的旧件删不删(5 份旧 rootfs tar 约 10 GB、各 `*.old-20260921`、`.void` 作废旧包、`~/work/qtrade-build/console-pack-2/`)。
- E3-O2:janitor 单元要不要像 Agent 单元一样配 `StartLimit*`(规格没要求,现状 = 一直失败时无限重启)。
- **发送失败是否计入限速**(倾向计入,护号)——开放项 R6-60 (f)。
- `#88` settings 读回「当前生效值」而非「已保存待重启值」⇒ 表单回填旧值,正式设计待定。
- #95 审计 JSON 时间键(规格两可)、#42 删除成功 200 / 204。
- 磁盘门槛是否按实测体积重算(现偏保守 = 安全侧)。
- 将来正式分发用公司内部 CA 还是 OV 证书(现阶段自签名 + 目标机导入;换证书只换 `-CertThumbprint`)。
- 分支何时合 `main`;要不要把这一版回给 cursor 做第九轮评审。

## 7. 三条硬规矩(展开见 `CLAUDE.md` 与 SKILL §4/§5b)

1. **禁区**:绝不自行 `wsl --shutdown` / 重启 WSL 或容器;企点/微信是**真实工作账号**(会离开本机的动作要先复述等确认,读消息一律走旁路读库、不 attach);
   真机 `C:\ProgramData\QTrade` 在非真装期间**必须不存在**;**未经安琳同意不运行**产出的安装 EXE / WinAgent exe;docker 只 `build/create/export/save/pull` 且用 `qtrade-build/` 前缀。
2. **多 agent 编排纪律**:角色独立(**被某道质量门挡住的人不修那道门**,验收用例只由独立验收方改);agent 关了就新起对口的;一个 agent 只给一批活;重活不用 sonnet;重要产出落 `.omc/handoffs/` 且边做边写盘。
3. **改文档的固定流程**:改 `docs/` → `cd docs && python3 check-truth-tables.py`(exit 0)→ 仓库根 `pytest -q`(代码↔文档对账在里面)→ `git commit` → `git push`。
   新裁决**追加到 `docs/00` §15g 末尾、编号续 R6-N**(别插中间,会让编号漂移);新增脚本规则须在改前备份上**反向验证能红**。
