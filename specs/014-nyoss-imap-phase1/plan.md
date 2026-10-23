# 开发规格 — 014 公网信箱与 IMAP（第一期）

配套：`speckit.md`。本文只定本仓库工作台要落地的模块、对象和与另外两个系统的边界。存款与询价的页面改造列在任务里，但不在本仓库实现。

## 模块

新建 `src/qtrade_agent/ossbox/`，纯逻辑先于网络：

| 模块 | 职责 |
|---|---|
| `crypto.py` | `seal` / `open_sealed` |
| `keys.py` | 对象名构造与校验 |
| `schema.py` | 目录、指令、回执、消息的校验 |
| `bind.py` | 存款微信群过滤、询价企点全量、`ext_msg_id` 与 `idempotency_key` 去重 |
| `imap_out.py` | 判断是否发信、是否因缩略图跳过，并调用现有 `ibquote-163-v1` 渲染 |

不在这些模块里发真实 HTTP、不连真实 OSS、不连真实 IMAP。传输适配器后接，第一期单测不覆盖真实网络。

工作台运行时再增加一个轮询循环（实现任务，不在纯模块里）：

- 每 5 秒列出 `in/{consumer}/`
- 解密、校验、执行现有发送
- 写 `ack/`
- 扫描新消息，按绑定加密写入 `out/`
- 刷新 `catalog.json`
- 对需要 IMAP 的微信群调用现有邮件发送

## 对象名

```text
wb/{workbench_id}/catalog.json
wb/{workbench_id}/out/{consumer}/{seq}.bin
wb/{workbench_id}/in/{consumer}/{cmd_id}.bin
wb/{workbench_id}/ack/{cmd_id}.bin
```

`consumer` ∈ {`deposit`, `xunjia`}。`seq` 从 1 递增。校验函数拒绝其它前缀、空段、正文夹带。

## 明文结构

目录账号：

```text
channel, account_uid, account_nick, groups[{native_id, name}]
```

指令：

```text
cmd_id, workbench_id, channel, account_uid, op="send_text",
session.kind, session.native_id, text, idempotency_key
```

回执：

```text
cmd_id, ok, code, reason?
```

消息：

```text
event_id, workbench_id, channel, account_uid, account_nick?,
session.kind, session.native_id, session.name?,
dir, text, ts, ext_msg_id
```

`channel` ∈ {`wechat`, `qidian`, `qq`}。`session.kind` ∈ {`private`, `group`}。`dir` ∈ {`in`, `out`}。

## 过滤

`deposit_allows(binding, message) -> bool`

- `binding.wxid` 必须等于 `message.account_uid`
- `message.channel` 必须是 `wechat`
- `message.session.kind` 必须是 `group`
- `message.session.native_id` 必须在 `binding.group_native_ids`
- 空群列表一律不允许

`xunjia_allows(binding, message) -> bool`

- `message.channel` 必须是 `qidian`
- `message.account_uid` 必须等于绑定的企点号
- 不看群、不看是否有债券

`Dedup`：`seen_event(ext_msg_id)`、`seen_command(idempotency_key)`。第二次返回假，调用方不得再上传或再发送。

## IMAP

`should_send_imap(binding, message, image_kind) -> bool`

- 绑定 `transport` 为 `imap` 或 `both`
- 且 `deposit_allows` 为真
- 且 `image_kind` 不是 `缩略图`

渲染必须走现有 `ibquote-163-v1`，信息段标题为「微信群聊消息通知」。不得新写一套字段名。

## 询价红线

工作台不实现词表。询价在写 OSS 指令之前沿用 `Monitor.vue` 已有拦截。规格只要求：被拦截的文本不会变成指令对象。本仓库用一个纯函数 `command_allowed(text, blocked) -> bool` 表达「调用方已判定拦截则拒绝产生指令」。

## 配置默认

- 轮询间隔 5 秒
- 对象保留 7 天（桶生命周期，不在代码里删历史正文）
- 存款 `transport` 默认 `both`
- 密钥环境变量：`OSSBOX_KEY_DEPOSIT`、`OSSBOX_KEY_XUNJIA`。缺失则该消费者的收发不启动，不影响本机控制台

## 另外两个系统（不在本仓库改代码）

存款：配置页三级选择；OSS 解密后写入现有入站；IMAP 仍由现有 fetcher 解析。

询价：`/api/im/links` 增加工作台 + 企点号；`/api/monitor/chat` 合并该号全部消息，无券的消息保留；`/api/im/send` 在红线通过后写 OSS 指令。

## 测试边界

单测只覆盖 `ossbox` 纯函数和「缩略图不发信、模板标题仍在」。不连 `nyoss.yingmi.com`，不发真实邮件，不操作真实企点、QQ、微信。
