---
name: qidian-read-via-db-2026-09-19
description: "企点读消息=旁路读库;🔴正线=主库{uin}.db(会话分表mr_*、XOR密钥02:00:00:00:00:00、游标各表_id、延迟2~12s);索引库IndexQQMsg.db降兜底。含R6-36"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5986b5d0-151c-4c07-aca1-85afa9d985c0
  modified: 2026-09-19T03:56:43.599Z
---

2026-09-19 QTrade redroid+企点 RPA 方向,读取路线定稿。全程只读、没碰企点进程。

## 🔴 结论:企点收消息走「旁路读库」,不走 frida hook
读取器已实现:`~/work/xunjia-agent/relay/side_a/db_reader.py`(9.2KB,零写操作、零 attach、`mode=ro`+`.backup`副本)。

## 关键事实(实测,不是推断)
- **消息库**:`/data/data/com.tencent.qidian/databases/{登录uin}-IndexQQMsg.db`(本机 uin=3007373675)。**明文 SQLite,非 SQLCipher**(只 org.db 组织库加密)。
- **压缩**:FTS4 建表带 `compress=qqcompress`,但本版 **qqcompress 实测就是 Base64**(不是 zlib、不是加密)。base64 解码即得明文正文。**比微信 chatlog 还简单——企点连 SQLCipher 密钥都不用。**
- **一张表读全**:`IndexContent`(FTS4)的影子表 `IndexContent_content` 装单聊+群消息。**靠 `c4ext1` 的 ZzZ 后缀区分:`ZzZ0`=单聊、`ZzZ1`=群**(前缀=对端uin/群号)。群里的债券报价都在这、明文。
- **字段语义**(单聊 `IndexContent_content`):`c1content`=base64→正文;`c5ext2`=发送方uin(==登录uin→出向 / ==对端→入向,据此判 in/out);`c4ext1`=会话`{对端}ZzZ{0|1}`;`c13exts` 前8字节大端=时间戳(秒);`c0type`=类型(单聊全 type=1 文本);`c3oid`=序号。
- **增量游标**:`docid` 单调递增,`WHERE docid>last_docid` 即增量(比 SyncCursor 简单)。
- **覆盖面**:单聊 37525 条全 type=1 文本;报价是文本,足够。非文本消息不在此库。
- ⚠️ **`TroopIndex` 不是群消息**,是群成员名片索引(全 type=2/content=null)——一度误判,已纠正。读消息只用 IndexContent。

## 两条铁律(与保密无关,是工程正确性)
1. **只读,绝不写库**:企点自己在用,写它可能损坏用户消息库或和企点写操作打架。姿势=`sqlite3 'file:xxx?mode=ro' ".backup 副本"` 出一致副本再读(防读到写一半的页)。
2. **只读库文件,不碰企点进程**:读库价值就在零 attach/零崩溃,别为"验证更全"回头 frida。

## 为什么不走 frida hook(踩过的坑)
- frida 在 arm64 翻译层下**可用**(attach 成功、Java 层枚举 27268 类零影响)——头号技术风险已排除,但**不需要用**。
- 企点这版消息引擎**不走 mobileqq 老门面层**(QQMessageFacade/MsgProxy/MessageHandler 共 545 方法全 hook 零触发)、**非 QQNT 架构**——网上所有手Q/企点 frida hook 老教程失效。
- 🔴 **翻译层下企点主进程对 frida 重负载极脆弱**:545 方法广谱 hook 崩过一次、全量 `Java.choose` 堆遍历崩过一次(两次登录态侥幸没丢)。**严禁广谱 hook / 全量堆扫描**。这也是转向读库的直接推力。

## 设计意义
企点旁路读库 = **与微信 chatlog 同架构**(直读解码后的库),两通道读取路线统一;出参已对齐 06/02 的 §7.4 Message(text/发送方判in-out/session含单群/ts/type/ext_msg_id/fingerprint)。⚠️ **`ext_msg_id` 现为 `qd:{uniseq}`(R6-36 改主库后;原索引库 `qdidx:docid` 已作废)**。

## 附:冷启动链路(同目录另一交付)
`relay/side_a/qidian_cold_start.sh` —— 重启redroid→frida→企点冷启→**免登恢复**(登录态/data卷持久化,企点无无人扫码,这是唯一机制)→搜索→私聊→发送。发送默认关闭。三个坑在头注:①`adb root`后设备卡offline,救法=容器内`stop adbd;start adbd`+宿主`adb kill-server/start-server`;②frida-server在`/data/local/tmp`持久卷、重启后进程要重起+需adb root;③中文必须走ADBKeyboard `ADB_INPUT_B64` base64广播,`input text`打不了中文。

**Why:** 这是企点通道能否成立的命门,五个阶段(frida可行→hook落点难→转读库→明文base64→读取器)走下来,最终答案和微信一样是"读库"。
**How to apply:** 企点读消息一律用 db_reader.py,别再碰 frida hook;发送走 RPA(qidian_cold_start.sh)。相关 [[wsl-redroid-feasibility]]、[[wechat-pc-install-facts-2026-09-18]]、[[qtrade-design-docs-2026-09-18]]。

## 2026-09-19 补:`adb root` 是冷启动标准动作(安琳定)
读库要读 `/data/data/com.tencent.qidian/databases/*-IndexQQMsg.db`,**非 root 读不到**;冷启动后 adbd 默认非 root。提权曾藏在 frida 步里,frida 下读取链后被一并跳过 ⇒ 「能发不能读」。
**How to apply:** 容器(重)启动后一律先跑 `qidian_cold_start.sh root`(已并入 `all`,frida 步已移出);设计口径 = 基线 §15g R6-18 / 06 §2.9.5。真机全链路(发 dantsinghua「test 020201」→ docid 37528 读回)当日跑通。

## 2026-09-19 傍晚 🔴 重大修正:IndexQQMsg.db 是滞后索引,实时要读主库
实测回环(`relay/side_a/echo_loop_maindb.py`):`{uin}-IndexQQMsg.db` 落库滞后 **13~36 s** 且只有文本 —— 不能当实时读取正线。
**主库 `{uin}.db`**(WAL)才是消息库:单聊表 `mr_friend_{MD5(对端uin)大写}_New`,群表 `mr_troop_{MD5(群号)大写}_New`;`msgData`、`senderuin` 等逐字节 XOR,**密钥 = `02:00:00:00:00:00`(安卓默认 MAC)循环**(由 senderuin 密文反推得出,四条明文全对);列 `_id/issend/time/msgtype(-1000=文本)/msgseq/shmsgseq/uniseq`;游标 = 表内 `_id`。读法:设备上 `sqlite3 'file:…?mode=ro'` 只查增量行 + `hex(msgData)`,单次 0.01 s,不拉整库。
**延迟上限来自企点自己**:新消息先进内存(logcat `Q.msg.MsgProxy insertToList MessageRecord`,发出后≈1.8 s),**`transSaveToDatabase` 每 10 s 批量落库**(logcat 可见 `writeRunable msgQueue size:N`)⇒ 读库延迟 ≈ 1.8 + U(0,10) s,实测 3.0/3.4/11.4 s。我方发出的消息落库也滞后 7~9 s ⇒ 发送后读回确认必须异步。
**How to apply:** 接 RPA 的 S1 读循环读主库、按表 `_id` 做游标;索引库只作历史/全文检索兜底。对「实时」的承诺口径 = 2~12 s(均值约 7 s);要更快只能加 logcat 触发(知道有消息≈2 s,无正文)或碰企点进程(需安琳同意)。设计文档 06 §2.9.5 待按此修订(实测记录已登基线 §15g 末尾)。

## 2026-09-19 傍晚(2):设计文档已全维度改齐(R6-36 定库 + R6-37 收半改)
R6-36 只改了 06 §2.9.5 详节、邻表没跟;那轮 cursor 复评点名后由 R6-37 收口、独立终审判 ACCEPT。**读库的几个"一处一真值"量,现在全套文档一致如下**(编码时照这个,别再撞旧值):
- **去重键** `messages.ext_msg_id = 'qd:'+uniseq`(明文整数、实测全库唯一;原索引库 `qdidx:{docid}` 作废——`docid` 是索引库的量,主库没有)。
- **游标** `cursors.kind='qidian_rowid:<native_id>'`,`value_int`=该会话表已处理最大 `_id`,**单库多水位**(主库按会话分表,一表一水);原单一 `qidian_docid` 作废。
- **`native_id`/`kind`/会话来源**取**主库 `XOR(frienduin)` 列 + `istroop`**:单聊 `frienduin`=对端uin、群 `frienduin`=群号(群加 `g_` 前缀);`istroop` `0`→private/`1`→group,只判 kind、不进 id。⚠️ **原「取自索引库 `IndexContent.c4ext1` 前缀/ZzZ 后缀」全作废**——主库没有 `c4ext1` 列,照旧写会去读滞后 13~36s、可能未建的索引库。
- **`ensure_root` 判据** = `whoami==root`(提权本身);读库可读性以**登录后能打开主库 `{uin}.db`** 为准,**不验已降兜底、可能未建的 `*-IndexQQMsg.db`**(在 ⑤b 装企点前就调,那时任何 db 都没有,只有 `whoami==root` 是提权判据)。
- 对账脚本新增 `qdidx:`、`c4ext1` 两条 FORBIDDEN(glob `0[1-7]` 排除 00 裁决表),挡这两个旧量再被活写。

## 2026-09-19 晚补充(R6-38~R6-42,真机数据;规格唯一出处 06 §2.9.5)
- 🔴 **XOR 密钥 = ASCII 字符串 `b"02:00:00:00:00:00"` 的 17 字节循环**(`in[i]^KEY[i%17]`),不是 6 个原始字节——按 6 字节解全是乱码(我自己踩过)。
- 主库 64 MB ⇒ 正线 = 设备上 `sqlite3 'file:…?mode=ro'` 只读直查增量;`.backup` 整库副本只属索引库读取器 `db_reader.py`。
- 被踢(15:05 `ACCOUNT_KICKED`「身份验证失败」,原因未明)→ 点「重新登录」走**免登零交互**;水位持久在 `cursors`,各会话表各自续读;离线期间的**私聊**重登后补同步(原始 `time`、`_id` 续排);库里大量「`_id` 靠后而 `time` 更早」⇒ 水位只能用 `_id`。
- 🔴 **群离线消息读库补不回来**:112 张群表 `shmsgseq` 缺 37.5%(最大 46.6%),缺口全是大块、正对离线时段。探测 = 近 3 天窗口无状态统计 → warn `QIDIAN_MSG_GAP`;**私聊 `shmsgseq` 不可用**(对端全局序号,缺口 99.9%);自动补拉未验证。
- 首登不回灌历史靠**历史闸**(`time < bootstrap 时刻 − 120 s` 不入库),不是靠 `MAX(_id)`;库被重建靠**水位自检**(`value.last_uniseq`)→ 从 0 重扫,`message` 事件只对 `inserted or changed` 发。⚠️ `uniseq` 跨库重建是否稳定**未实测**(开工带着的开放项,正反两向都要验)。
- 延迟实测:读取均值 6.8 s(1.9~11.5,大头=10 s 批量落库);纯回复链路 ≈2.2 s;我方回复落库可见 9.3~11.4 s ⇒ `confirm_timeout_qidian_ms` 必须 15000(8000 必然 UNCONFIRMED)。
- 🔴 **消息类型路由(R6-43~R6-46,规格 06 §2.9.5;参考实现 `xunjia-agent/relay/side_a/qidian_msgdata_decode.py`)**:读库路**只产出文本**。文本族 `{-1000,-1051,-1049(含@)}` 整体 UTF-8(全库 36013 条 100% 合法);`-1035` 图文混排 = protobuf repeated Elem,有文本段才产出、`type=text`、图片段写 `[图片]` **按 Elem 原顺序**(79% 图在前);`-2000` 图片/`-2017` 群文件/`-2011` 链接卡片(Java 序列化 `AC ED 00 05`)/`-2006,-5040,-2018` 系统/未知类型 → **不产出、水位照常越过**。`clean_text`:字符层面把 `U+0014`+后 1 字符换成 **`[表情]`**(不删:17 条纯表情消息删了会空),其余 <U+0020 除 `\t\n\r` 丢弃;除 `U+0014` 外库里没有独立控制字节;`@` 无任何属性字节。原口径漏 `-1049` = 群里 @ 消息全丢。
- 延迟逐跳:11.5 s = 推送 1.27 + 等 10.000 s 批量落库 9.96(86.5%)+ 轮询 0.28;SLA 读取 P95 ≤ 11 s(安琳接受);logcat `Recv Msg` 行无正文、通知栏前台 0 条,不碰进程没法更快。
- 被踢 = 服务端强制下线(`ACCOUNT_KICKED` Intent,进程不死);原因码在 logcat `main`(原 256 KiB/63 分钟,已扩 4 MiB,容器重启失效)——下次 60 分钟内先抓。
- ⚠️ `echo_loop_maindb.py` 的 `send()` 按固定坐标点当前会话:**回环在跑时切界面 = 自动回复发进真实的群**;起回环前必须看截图核对聊天页标题。

