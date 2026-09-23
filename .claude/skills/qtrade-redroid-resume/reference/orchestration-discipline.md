# 多 agent 编排纪律快照(安琳 2026-09-21 定)

> 规范原件在两处,**本文件只是随仓库走的快照**:
> - 仓库 `CLAUDE.md`(project scope)「🔴 多 agent 编排纪律」段 —— 与本文件同源,冲突时以 `CLAUDE.md` 为准;
> - 原机活记忆 `/home/anlin/.claude/projects/-mnt-c-Users-anlin-Desktop-work/memory/agent-role-independence.md`(含每条的踩坑经过);
>   同目录另有 `always-chinese.md`(一律中文)、`pull-before-resume-skill.md`(先 pull 再读 skill)。
> 换机器接手时:这三条规范要一起带过去(`CLAUDE.md` 与本文件随 git,活记忆不随)。

---

## 1. 角色独立(最硬的一条)

实现 / 开发者测试 / 独立验收 / 文档回写 / 只读终审,**各由不同 agent 承担**。

- 开发者**不改自己的测试**。
- 🔴 **被某道质量门挡住的人,永远不是修那道门的人选**;绕过更不行。
  (踩过:winagent agent 已关,总控把「修 winagent 两条测试 + build.ps1」授权给了**正被那道测试门挡住的** payload agent。)
- 验收用例只由「**只读规格、不看实现方测试**」的独立验收方撰写与改写;**总控也不代改**。
  (踩过:验收撰写者关闭后,总控亲手改了 RT04 与 QQ 发送限速两条用例;后来另起独立复核者补的。)
- 因裁决需要翻面的验收断言,由**新起的验收 agent** 按裁决改,不由实现方或总控动手。

## 2. agent 关了就新起,不借用还活着的

某条线的 agent 面板已关、又来了新活 → **新起一个职责对口的独立 agent**(靠 `.omc/handoffs/*.md` 交接文件传上下文),
不让别的存活 agent 或总控"顺手代办"。关闭面板前先想清楚这条线后续还有没有活。

## 3. 保住上下文窗口:一个 agent 只给一批活

- 一个 agent 只给**一批边界清楚的活**,做完写交接、关闭;下一批**新起** agent。
- 不对同一个 agent 连发五六批(踩过:installer 连做六批,integrator/docs-scribe/payload 也各滚了多轮,上下文被耗尽、后面的活质量下降)。
- 上下文靠**交接文件 + README + 提交记录**传,不靠同一个 agent 的记忆。

## 4. 换人前先查在途任务

关一个 agent 或另起接替者之前:①问它 / 看它最后的状态;②查它起的**后台进程**(`ps` / `Get-CimInstance Win32_Process`)与**输出目录时间戳**;
③它拒绝关闭且理由正当(中断会留半成品)就让它跑完,把接替者改成**独立核查者**——执行与核查分离反而更好。
(踩过:只看到「WinAgent 构建进程没了」就断定 packager 卡住并新起接替者,其实它已在压 3.3 GB 正式包,两个 agent 差点同时写 `installer/out/`。)

## 5. 重活不用 sonnet

构建 / 打包 / 跨 WSL↔Windows 的长任务 / 要等后台任务的活 / 多步编排,一律 **opus 或更高**;
sonnet 只配给真正的小活(单文件小改、查一个事实)。派工时默认 `model=opus`,想省成本先问「这活出错的代价是什么」。

## 6. 重要产出边做边落盘,且不放会话临时目录

凡要跨会话 / 跨 agent 用的报告、交接、备份,一律落 **`.omc/handoffs/`**(被 git 忽略、在仓库盘上、断线不丢),
并**分段追加落盘**、不攒到最后一次写。(踩过:326 行终审报告写在 `/tmp/.../scratchpad/`,会话一断就没了;final-reviewer-2、signing 也这么丢过。)

⚠️ 由此产生一条对**交接文档**的要求:`.omc/` 不随 git 走,所以接手**必需**的信息(现状、待办、禁区、纪律、跑法)
要写进受 git 管理的文件本身(`SKILL.md` / `HANDOFF.md` / `README.md` / `CLAUDE.md`),`.omc/handoffs/` 只作为「原机上还有更细的记录」来指。

## 7. 一律中文

所有思考与所有交流用中文、每次回复称呼「安琳」;**没有例外**——工具调用之间的过渡句、一句话进度说明、
给子 agent 的派工提示、子 agent 的汇报、交接文件、提交说明都算。派子 agent 时在提示里写明「全部用中文」。
(长会话、英文工具输出、英文系统提示都会诱发漂移,总控滑回英文过好几次。)

---

## 8. 测试环境与本机已装工具(派工时要交代给子 agent)

| 事项 | 口径 |
|---|---|
| 主仓 pytest | `~/.venvs/qtrade/bin/python -m pytest -q`(**系统 python 没装 pytest**;这个 venv 是 uv 建的、**没有 pip 模块**) |
| 打 wheel | 用 `/usr/bin/python3 -m pip wheel`(上面那个 venv 打不了) |
| 控制台 | 先 `rsync -a --exclude node_modules --exclude dist console/ ~/work/qtrade-build/console/`,在**那边** `npm test`(仓库里不装 `node_modules`;`/mnt/c` 上跑 node 太慢) |
| Pester | 经 `installer/tests/run-pester.ps1`(Windows 侧) |
| 真 Windows Python | `C:\Python312`;WinAgent 测试 venv = `C:\Users\anlin\qtrade-payload\.venv-winagent-test` |
| 本机已装(安琳同意) | Inno Setup 6、VS2019 Build Tools(MSVC 14.29 + Win SDK 10.0.19041)、Python 3.12;`7zSD.sfx` 取自官方 LZMA SDK 2301 |
| docker 纪律 | 只 `build/create/export/save/pull`,镜像/容器用 `qtrade-build/` 前缀,**绝不碰现有 12 个容器**,绝不 `wsl --shutdown` |

## 9. 开工顺序

在 `Desktop/work` 开工:**先 `git fetch --all` / `git pull` 拉到最新开发分支,再加载 `qtrade-redroid-resume` skill**,
读的是**仓库内**那份 `.claude/skills/qtrade-redroid-resume/SKILL.md`——全局 `~/.claude/skills/` 那份会落后好几批。

## 10. 🔴 绝不 `cd` 进会被打包收集的目录(2026-09-21 晚新增)

本机装了 oh-my-claudecode 钩子,**会在当前工作目录写 `.omc/state/…`**。出包方曾因 `cd` 进 `installer/out/presign/winagent-app/` 查签名,让这个状态文件被 collect 收进载荷,整轮出包作废重跑(总控自己随后也在 `fieldtest/vm-lab` 犯了一次,幸而不在打包路径上)。

- 打包 / 出包 / 校验类 agent 的派工硬禁区里**必须写**:不 `cd` 进 `installer/out/**`、`winagent/dist/**`、`console/release/**`、产物根 `C:\Users\anlin\qtrade-payload\**`;一律绝对路径,工作目录停在仓库根或 scratch。
- 出包后的独立校验**必须含**「包内与 stage 零 `.omc` / `.old-*` / `.void-*`」。
- 删这类杂物前照旧先 `git ls-files` 查跟踪、看 mtime 确认是自己这个会话产生的。

## 11. 实现方自检不当结论;复测方同时是新守卫的验收方(2026-09-21 晚新增)

- rootfs 线一晚走了「复测三轮 → 修两轮」:E3-1(wheel hash 坏了退回 sdist)→ E4-1(多候选 wheel 静默换件)→ 通过。每一轮的新问题都出在上一轮的修复留下的缝里——与文档终审「每轮 REVISE 都出自上一轮新写的句子」是同一个规律。
- 收敛靠三条:①修法要求**按「类」设防**(对全部 15 个包成立的断言),不是只补当下那一个包;②派复测方时明说「你同时是这条新守卫的验收方,专门找绕过,每条怀疑都实跑」;③明说「**不采信实现方的自检**」——那一晚实现方的自检脚本就被查出 `docker exec` 缺 `-i`、检查空跑恒通过。
- 出包同理:出包方自校验之后,**另起**独立校验方全部重算(三段切段、逐条 sha256、剥签名比对、引擎脚本对 HEAD)。
- 保留项要不要先修再出包,**由安琳定**(那一晚两次都选了「先修」);总控给建议,不替她定。

