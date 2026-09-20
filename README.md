# QTrade 多实例 IM 控制台 + 统一 RPA

> 🔴 本仓库协作的头等规范:**所有思考与交流一律使用中文**(见 `CLAUDE.md`)。

| 目录 | 内容 |
|---|---|
| `docs/` | 详细设计(唯一真值 = `00` 基线 + `01~06` 六册 + `07` 配置总表;裁决表 `00` §15g 当前至 **R6-50**);`check-truth-tables.py` 真值表对账器,改文档后必跑 |
| `src/qtrade_agent/` | Agent(WSL 侧 systemd 服务)代码。2026-09-19 第八轮评审收口后开工,首批 = M2 骨架里能在无 redroid 环境下编码与单测的部分:`store`(DDL 逐字抽自 02 §3.1、`ingest` 三元组、同事务顺序、出向合并)、`adapters/qidian`(XOR 解码、`msgtype` 路由、`poll_maindb`/`check_group_gaps`)、`bus`(R6-48 入口校验、出向先落库、幂等三态、队列外等确认)、`events`/`alerts`、`text.norm/clean_text` |
| `tests/` | pytest 单测(49 条,含「发送 → 读库合并 → DELIVERED」集成用例与「代码 ↔ 文档」对账) |
| `.claude/skills/qtrade-redroid-resume/` | 断点续接 skill:接手先读它全文 |
| `HANDOFF.md` / `CLAUDE.md` | 交接入口 / 项目级规范 |

## 跑起来

```bash
pip install pytest pytest-asyncio
python3 -m pytest -q                      # 单测(pythonpath=src 已在 pyproject.toml)
cd docs && python3 check-truth-tables.py  # 文档对账,须 exit 0
```

代码以文档为准:代码与文档冲突时改代码,或先在 `docs/00` §15g 追加裁决(编号续 R6-N)再改文档,并给 `check-truth-tables.py` 加规则(改前备份上反向验证能红)。
