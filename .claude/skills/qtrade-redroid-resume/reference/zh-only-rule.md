---
name: zh-only-rule
description: 🔴 头等规范(安琳 2026-09-19 定):所有思考与交流一律中文;同时钉在 ~/.claude/CLAUDE.md(user)与仓库 CLAUDE.md(project)
metadata:
  node_type: memory
  type: feedback
  modified: 2026-09-19
---

**规范原句(安琳,2026-09-19,QTrade 项目会话中)**:「所有思考和交流全部使用中文,请记住并钉死在你的 CLAUDE.md 以及本地 user scope、project scope 的记忆文件里,是头等重要的规范。」

**怎么执行**:
- 思考(含可见推理)与交流(回复、解释、提问、总结)全部中文;工具输出/代码/报错是英文也不切换。
- 代码标识符、命令、路径、错误原文按原样保留,围绕它们的说明用中文。
- 优先级高于其它一切风格约定。

**钉在哪(三处同步)**:
1. `~/.claude/CLAUDE.md`(user scope)
2. 仓库 `CLAUDE.md`(project scope,随 git 走)+ `.claude/skills/qtrade-redroid-resume/SKILL.md` 顶部
3. 项目 memory 目录下的同名文件(`~/.claude/projects/<项目路径>/memory/zh-only-rule.md`);本文件是它的随仓库快照——换机器接手时把本文件复制回新机的项目 memory 目录

**Why:** 安琳明确要求,是本项目协作的前提,不是风格偏好。
**How to apply:** 每个新 session 接手本项目(或任何安琳的项目)时,第一件事就是按此规范工作;换机器时把三处一起带过去。
