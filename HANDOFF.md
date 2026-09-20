# 交接入口 / Handoff

本仓库 = QTrade「redroid 多实例 IM 控制台 + 统一 RPA」项目的**设计文档**(尚未编码)。

- **设计文档**:`docs/`(基线 `00` + 六册 `01~06` + 配置总表 `07` + 对账脚本 `check-truth-tables.py`;裁决表在 `docs/00-共享基线与口径.md` §15g,当前至 **R6-53**(第八轮 cursor 评审 8.4/10 已收口;R6-51/R6-53 = 两批代码独立验收的收口,R6-52 = 第二批口径))。
- **代码**:`src/qtrade_agent/`(Agent 侧:store / 企点读库 / bus / scheduler / health / api / 装配)+ `tests/`(277 条:开发者 65 + 独立验收 212);`python3 -m pytest -q`;起服务 `python3 -m qtrade_agent.main --db <path>`。
- **断点续接 skill**:`.claude/skills/qtrade-redroid-resume/SKILL.md` —— 接手先读它全文。
  - 在本克隆目录里开 Claude Code,`/qtrade-redroid-resume` 会自动可用(项目级 skill);
  - 或把 `.claude/skills/qtrade-redroid-resume/` 复制到 `~/.claude/skills/`(Windows:`%USERPROFILE%\.claude\skills\`)装成全局。
- **参考记忆快照**:`.claude/skills/qtrade-redroid-resume/reference/`(一致性教训、企点读库实测)。

改文档后:在 `docs/` 里跑 `python3 check-truth-tables.py`(须全绿)→ `git commit` → `git push`。
