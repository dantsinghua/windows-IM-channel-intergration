# 交接入口 / Handoff

本仓库 = QTrade「redroid 多实例 IM 控制台 + 统一 RPA」项目的**设计文档 + Agent 侧代码**(2026-09-19 第八轮评审收口后开工,已交付三批;真机接入仍只在安琳原机)。

- **设计文档**:`docs/`(基线 `00` + 六册 `01~06` + 配置总表 `07` + 对账脚本 `check-truth-tables.py`;裁决表在 `docs/00-共享基线与口径.md` §15g,当前至 **R6-55**(R6-47~R6-50 = 第八轮 cursor 评审收口;R6-51/R6-53/R6-55 = 三批代码独立验收的收口;R6-52/R6-54 = 第二、三批的编码口径))。
- **代码**:`src/qtrade_agent/`(Agent 侧:store / 企点读库 / bus / scheduler / health / api / runtime / pool / accounts / vault·winagent 客户端 / H13 校时 / 装配)+ `tests/`(420 条:开发者 125 + 独立验收 295);`python3 -m pytest -q`;起服务 `python3 -m qtrade_agent.main --db <path>`。开发容器里一律注入假后端(`runtime.FakeContainers/FakeAdb`、`vault_client.FakeVault`、`winagent_client.FakeWinAgent`),**绝不在开发机碰真 docker / adb / WinAgent**。
- **断点续接 skill**:`.claude/skills/qtrade-redroid-resume/SKILL.md` —— 接手先读它全文(§5「最新状态」有三批代码的范围、口径与「接手下一步」)。
  - 在本克隆目录里开 Claude Code,`/qtrade-redroid-resume` 会自动可用(项目级 skill);
  - 或把 `.claude/skills/qtrade-redroid-resume/` 复制到 `~/.claude/skills/`(Windows:`%USERPROFILE%\.claude\skills\`)装成全局。
- **参考记忆快照**:`.claude/skills/qtrade-redroid-resume/reference/`(一致性教训、企点读库实测、中文头等规范)。

改文档后:在 `docs/` 里跑 `python3 check-truth-tables.py`(须全绿)→ `python3 -m pytest -q`(代码 ↔ 文档对账也在里面)→ `git commit` → `git push`。
