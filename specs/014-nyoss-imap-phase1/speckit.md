# speckit — 014 公网信箱与 IMAP 对接（第一期）

状态：待实现。规格以本文和同目录 `plan.md` 为准。审阅稿见 `docs/handoffs/2026-10-12-公网信箱与IMAP对接-开发说明.md`。

## 问题

内网的存款系统（`njcb-deposit-agent`）和询价系统（`xunjia-agent`）不能连入个人工作台。工作台能访问公网。第一期用两件事打通收发：

1. 私有对象存储 `nyoss.yingmi.com` 上的加密信箱（指令、回执、消息、目录）。
2. 工作台按已锁定的 `ibquote-163-v1` 邮件模板，把选中的微信群发给存款现有 IMAP 解析。不改 `fetcher/wechat_mail.py` 的字段和「缩略图整封不读」规则。

两条都在第一期。同一条微信群消息若两路都到，存款只入库一次，键是 `ext_msg_id`。

## 角色

| 角色 | 系统 | 第一期行为 |
|---|---|---|
| 采集端 | 本仓库 QTrade Agent | 本机收发企点、QQ、微信；出站读写 OSS；按绑定发 IMAP |
| 存款 | nanyin-deposit / `njcb-deposit-agent` | 选定一台工作台、一个微信号、若干群；拉 OSS 或收 IMAP；可向这些群发消息 |
| 询价 | `xunjia-agent` | 选定一台工作台的一个企点号；该号全部消息进 `Monitor.vue`；监控页发送改为写 OSS 指令 |

## 身份

绑定、目录、历史都不使用可编辑的账号备注（`label`）。

- 工作台：`workbench_id`
- 微信：`wxid`
- 企点：企点号 `self_uid`
- QQ：QQ 号 `self_uid`
- 群：通道原生群 id
- 展示名：登录昵称 + 稳定号码。备注改名不改变绑定

## 用户可见结果

1. 存款配置页能选：工作台 → 该工作台的微信号 → 该号的群（多选）。选项来自工作台目录，不能手填备注。
2. 选中群的新消息进入存款现有解析。OSS 与 IMAP 可同时开，重复消息只留一条。
3. 存款能向选中群发送一条文本，并看到送达或失败回执。
4. 询价配置能选一台工作台的一个企点号。
5. 该企点号的全部消息出现在机器人监控页。对得上券的仍标债券；对不上的也显示，无券锚。
6. 监控页发送仍经过现有红线闸。通过后走 OSS 指令，不直连企点。
7. QQ 用同一条指令：指定 QQ 号、对端号码或群号、正文。
8. OSS 对象体是密文。对象名不含消息正文。

## 功能需求

### F1 目录

工作台把已登录账号写入 `wb/{workbench_id}/catalog.json` 的密文。明文结构：

```text
workbench_id
accounts[]: channel, account_uid, account_nick, groups[]{native_id, name}
```

`account_nick` 来自登录昵称。没有昵称时仍必须有 `account_uid`。不得写入 `label`。

### F2 加密包

`seal(key, plaintext) -> bytes` 与 `open_sealed(key, blob) -> bytes`。

- 算法 AES-GCM
- 错误密钥或损坏密文必须失败，不得返回部分明文
- 同一明文两次密封的密文不相同（含随机数）
- 密钥不得出现在对象名、日志、异常消息里

### F3 对象名

只允许以下形态，且不得包含正文：

- `wb/{workbench_id}/catalog.json`
- `wb/{workbench_id}/out/{consumer}/{seq}.bin`
- `wb/{workbench_id}/in/{consumer}/{cmd_id}.bin`
- `wb/{workbench_id}/ack/{cmd_id}.bin`

`consumer` 只允许 `deposit` 或 `xunjia`。`seq` 为十进制正整数。`cmd_id` 为不含斜杠的标识。

### F4 指令与回执

指令明文必填：`cmd_id`、`workbench_id`、`channel`、`account_uid`、`op`、`session.kind`、`session.native_id`、`text`、`idempotency_key`。

- `op` 第一期只有 `send_text`
- `channel` 为 `wechat`、`qidian`、`qq`
- `session.kind` 为 `private` 或 `group`
- 缺字段拒绝，不执行发送
- 同一 `idempotency_key` 第二次见到时不再次发送，回第一次的回执

回执明文含：`cmd_id`、`ok`、`code`。失败时 `ok` 为假，并带 `reason`，不带正文。

### F5 上行消息

消息明文必填：`event_id`、`workbench_id`、`channel`、`account_uid`、`session.kind`、`session.native_id`、`dir`、`text`、`ts`、`ext_msg_id`。

- 存款只收到其绑定微信号、且群 id 在选中列表中的消息
- 未选中的群、该微信的私聊，不写入 `out/deposit`
- 询价收到所绑企点号的全部消息，含私聊和群，含对不上券的消息
- `ext_msg_id` 相同的消息只上传一次

### F6 存款绑定

绑定记录：`workbench_id`、`wxid`、`group_native_ids`（可空列表表示不收任何群）、`transport`。

`transport` 取值：`oss`、`imap`、`both`。默认允许 `both`。`both` 时两路都发，入库按 `ext_msg_id` 去重。

### F7 IMAP

仅当绑定包含 `imap` 或 `both`，且消息属于选中群时，工作台发送一封邮件。

- 模板固定为 `ibquote-163-v1`
- 信息段标题为「微信群聊消息通知」
- 信息段键名与顺序与 `src/qtrade_agent/mail/templates.py` 的 `IBQUOTE_BODY_FIELDS` 信息段一致
- `image_kind` 为缩略图时不发这封邮件
- 不改存款侧 `fetcher/wechat_mail.py`

### F8 询价监控

询价把拉到的企点消息交给现有监控聊天数据。每条至少有正文、时间、方向、会话。能对应到债券的附加债券上下文；不能对应的债券字段为空，消息仍保留。

发送接口仍先做红线闸。被拦截的文本不写 OSS 指令。

## 验收

1. 用错误密钥解不开密封包。
2. 对象名校验拒绝含正文或非法 `consumer` 的键。
3. 目录序列化结果不含 `label`。
4. 存款绑定之外的微信群不会进入 `out/deposit`。
5. 同一 `ext_msg_id` 第二次不产生新的上传对象。
6. 同一 `idempotency_key` 第二次不调用发送。
7. 缩略图不生成 IMAP 邮件；原图或非图片消息按锁定模板生成，且含「微信群聊消息通知」。
8. 询价侧对不上券的消息不被丢弃。
9. 含成交拦截词的文本不产生 OSS 指令。拦截词集合沿用询价现有红线，本规格不新增词表。

## 不做

- 内网入站、穿透、自建中转进程
- OSS WebSocket
- 事件通知、轻量队列
- 修改兼容邮件模板的键名或顺序
- 存款接收微信私聊
- 询价改走邮件
