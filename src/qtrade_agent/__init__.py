"""QTrade Agent(WSL 侧 systemd 服务)—— 设计规格 = docs/00 基线 + docs/01~06 六册 + docs/07。

包结构与 02 §2.2 模块划分一一对应(本期 = M2 骨架里能在无 redroid 环境下编码与单测的部分):
- text / ids / models / config   —— 横切:norm()/clean_text()、ULID、Command/CommandResult/Message、agent.toml 默认值
- store                          —— 唯一读写 agent.db 的模块(DDL 逐字抽自 02 §3.1;ingest 三元组 + 同事务顺序)
- events / alerts / audit        —— events_outbox、告警去重键、audit_log
- adapters.qidian                —— XOR 解码、msgtype 一级路由、poll_maindb / check_group_gaps(06 §2.9.5 伪代码逐分支)
- bus                            —— send_text 入口校验(R6-48)、出向先落库、幂等三态、队列外等确认(R6-38)
- scheduler / health / api / app —— 周期任务、进程内健康态、FastAPI + WS 事件流、进程装配(第二批,R6-52/R6-53)
- runtime                        —— docker/adb 后端协议 + CLI 实现 + 假实现、端口推导、_purge_ephemeral、ensure_root(第三批,R6-54)
- pool / accounts                —— 资源池 can_add/reserve/snapshot、账号生命周期状态机(#2/#4/#5/#6/#7/#9/#10/#11/#19、02 §2.6 恢复)
- vault_client / winagent_client / timesync —— WinAgent /wa/v1 薄封装(契约 02 §2.5)、Vault 条目、H13 校时
"""
__version__ = "0.1.0"
