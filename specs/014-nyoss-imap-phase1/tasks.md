# 任务 — 014 第一期

顺序按依赖。标「他库」的不在本仓库由本任务直接改。

## 本仓库

1. `ossbox/crypto.py`：`seal` / `open_sealed`
2. `ossbox/keys.py`：四类对象名的构造与校验
3. `ossbox/schema.py`：目录、指令、回执、消息校验；目录不含 `label`
4. `ossbox/bind.py`：存款群过滤、询价企点全量、两种去重、`command_allowed`
5. `ossbox/imap_out.py`：`should_send_imap`，以及用 `ibquote-163-v1` 渲染选中群消息
6. 运行时循环：拉指令、执行现有发送、写回执、按绑定上传消息、刷新目录
7. 控制台配置入口：展示目录中的工作台、微信号、群、企点号（只读选择，备注不可当键）

## 他库

8. 存款：绑定存储与配置页；OSS 入站接入现有解析；与 IMAP 按 `ext_msg_id` 去重
9. 询价：绑定一个企点号；监控页展示该号全部消息；发送改为 OSS 指令，红线闸保持

## 完成判据

`specs/014-nyoss-imap-phase1/speckit.md` 的验收 1 至 8 有对应单测。验收 9 由 `command_allowed` 单测覆盖。真实 OSS、真实邮箱、真实三通道不在单测里。
