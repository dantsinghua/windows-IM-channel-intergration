#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QTrade 设计文档 · 真值表对账器
===============================
为什么有这个脚本(2026-09-18 第二轮评审的教训):

  第一轮回改后,我们的终检查的是「关键词存在性」——「DISK_FULL 出现了吗」「jobs 出现了吗」。
  全绿。但第二轮评审(只认正文、不认变更记录)照样打出 6.8/10,原因是:
      散文改了、真值表没改。
  典型:00/06 的安全章节写「allow_ops 默认白名单」,而 02 的【配置总表】、06 的【字段表】
  仍写「默认 ["*"] = 全能力目录」。实现者是照配置表写码的 —— 照着写就是邮件全开。

  所以本脚本不查「有没有出现」,只查「同一个事实在多个地方是不是同一个答案」。
  规则分两类:
    FORBIDDEN —— 真值表里不该再出现的旧语义(出现即红,除非在明确的否定/勘误语境里)
    PAIRED    —— 必须成对出现的事实(A 处声明了,B 处就必须有对应行)

用法:  python3 check-truth-tables.py            # 全量
       python3 check-truth-tables.py --verbose  # 打印每条命中行
退出码:0 = 全绿;1 = 有红项
"""
import io
import os
import re
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
VERBOSE = "--verbose" in sys.argv

# 出现这些词,说明该行是在「否定/勘误/追溯」旧语义,不算残留
NEGATION = (
    "作废", "勘误", "取代", "禁止", "不得", "改回", "已改", "历史", "残留",
    "0 次", "清掉", "删掉", "替换", "不出现", "不存", "更不", "不再", "此前",
    "旧语义", "曾", "原写", "改为", "改成", "而不是", "不是",
    # 2026-09-18 首跑校准:下面这些是「正确地否定旧语义」的写法,不是残留。
    # 漏了它们会让 keep_data 一条就误报 8 处 —— 误报比漏报更伤,它会让人从此不看这个脚本。
    "恒软删", "软删",
)

# 通用否定模式:命中词的【前面 12 个字符内】出现这些,说明是"正确地声明不该有它",不是残留。
# 比逐条往 NEGATION 里加「无 `keep_data`」「全程无 `account_id=""`」这种字面量可靠 ——
# 那样每出现一种新写法就要再打一次补丁,而这里一条规则覆盖全部。
NEGATION_NEAR = ("无", "没有", "不存在", "不带", "不再有", "禁止", "去掉", "删除")


def _is_negated(line, match_start):
    """命中点左侧近邻是否有否定词。"""
    left = line[max(0, match_start - 12):match_start]
    return any(k in left for k in NEGATION_NEAR)


def load(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    return io.open(path, encoding="utf-8").read()


def lines_of(text):
    return text.split("\n")


# ---------------------------------------------------------------- FORBIDDEN
# (标题, 文件 glob, 正则, 说明为什么这是红项)
FORBIDDEN = [
    ("allow_ops 旧语义「全能力目录/全开」",
     "0[1-6]-*.md",
     r"allow_ops[^\n]{0,80}(全能力目录|全开)|(全能力目录|全开)[^\n]{0,40}allow_ops",
     "基线 §11.17② 定死 `[\"*\"]` 只展开 danger=false。配置表/字段表写「全能力目录」= 实现者做出邮件全开。"),

    ("微信新增流的 account_id=\"\"",
     "0[1-6]-*.md",
     r'account_id\s*[=:]\s*""|account_id=""',
     "基线 §11.23 [WXID] 定死新增即建 wxNN,全流程不存在 account_id=\"\"。留着会做出两套微信主键。"),

    ("事件码 ENDPOINT_CHANGED(应为 NET_PUBLIC_ENDPOINT_CHANGED)",
     "0[1-6]-*.md",
     r'(?<!NET_PUBLIC_)(?<!MAIL_)\bENDPOINT_CHANGED\b',
     "E-3 已定码名 NET_PUBLIC_ENDPOINT_CHANGED;分叉会让订阅方收不到。"),

    # ⚠️ 刻意只盯【微信安装包】这一个 sha256,不盯 manifest 里其它占位。
    # 理由:wsl.msi / adb / rootfs.tar / dll 的 sha256 在设计阶段是占位符属正常(版本未定稿),
    # 全盯会一次报 16 处、其中 15 处是正常状态 —— 那样这条规则就废了。
    # 微信包不同:两个候选包【外层 VersionInfo 完全相同】,sha256 是唯一能分辨 4.1.12.26 与
    # 4.1.12.55 的手段,占位 = 装错版本且校验不出来,所以它必须钉死。
    ("随包微信安装包 sha256 未钉真值",
     "03-*.md",
     r'pkg/wechat/[^\n]{0,80}"sha256"\s*:\s*"…"|"sha256"\s*:\s*"…"[^\n]{0,80}pkg/wechat',
     "两个候选包外层 VersionInfo 完全相同,占位 sha256 = 会装错版本(4.1.12.55)且校验不出来。"),

    ("会话代理 exe 误名 -agent.exe",
     "0[1-6]-*.md",
     r"winagent-agent\.exe",
     "agent 是定死术语(Agent = WSL 侧服务),会话代理应为 qtrade-winagent-user.exe。"),

    ("删账号仍带 keep_data 参数",
     "0[1-6]-*.md",
     r"DELETE[^\n]{0,60}keep_data|keep_data[^\n]{0,30}参数",
     "R-11:DELETE 恒软删无参,真删走 purge。留着会做出软删/真删两套 API。"),

    # ---- 第六轮(基线 §15g R6-*)新增:专治「主表改了、邻表没改」 ----
    ("邮件 HMAC 钥路径缺 cmd/ 段(单钥旧写法)",
     "0[0-7]-*.md",
     r"mail/hmac/<短名>",
     "R6-10:统一 `mail/hmac/cmd/<短名>` + `mail/hmac/confirm`;抄旧路径 = 单钥,邮箱失陷即管理员。"),
    ("邮件 HMAC 钥路径:光杆 hmac/cmd(缺短名)或 hmac/<具体名>(缺 cmd/ 段)",
     "0[1-7]-*.md",
     r"mail/hmac/cmd(?!/)|mail/hmac/(?!cmd\b|confirm\b|<)[A-Za-z0-9_]+",
     "R6-10 / 终审 N-12:作废的三种写法都要抓;合法只有 mail/hmac/cmd/<短名>(或具体短名)与 mail/hmac/confirm。"),
    ("企点会话原生 ID 写成 docid / 带 ZzZ 后缀",
     "0[0-7]-*.md",
     r"原生 ID[^\n]{0,20}优先取[^\n]{0,12}docid|session\.id[^\n]{0,30}\{c4ext1\}",
     "R6-21:单聊=<对端uin>、群=g_<群号>;docid 是消息水位不是会话标识。"),
    # R6-37:R6-36 把读取正线切到主库 {uin}.db 后,去重键必须是 ext_msg_id='qd:{uniseq}'、游标各表 _id。
    # 邻表(入库路径表/字段映射/02 去重键列)若仍抄索引库 'qdidx:{docid}' = 做出第二套企点去重键、与
    # qidian_rowid 水位对不上。这正是 R6-36「半改」漏网的一类,脚本原本不查它 ⇒ 全绿是假绿(评审点名)。
    # glob 用 0[1-7]-*.md 排除 00:§15x 历史裁决表(R6-9/R6-36/R6-37 会引述旧句 'qdidx');活写才红,
    # 历史/否定语境(取代/作废/不再/已改…)由 NEGATION 自动跳过。
    ("企点去重键写成索引库 qdidx:{docid}(R6-36 正线已改主库 qd:{uniseq})",
     "0[1-7]-*.md",
     r"qdidx:",
     "R6-36/R6-37:读取正线=主库 {uin}.db,去重键 ext_msg_id='qd:{uniseq}'、游标各表 _id;抄索引库 qdidx:{docid}=第二套企点去重键,与 qidian_rowid 对不上。"),
    # R6-37 终审补:native_id/kind/会话来源维度此前脚本零覆盖(假绿)。R6-36 把读取正线切主库后,
    # 主库【没有 c4ext1 列】——native_id/kind/会话来源一律取主库 XOR(frienduin)+istroop(§2.9.5)。
    # 邻表/DDL/提议若仍写「取自索引库 c4ext1 前缀」= 去滞后13~36s、可能未建的索引库取会话来源 ⇒
    # 会话分组错 / 复现「能发不能读」。这正是 R6-37 第六轮漏网的 5 处(06:719/748/1182/1470、02:552)。
    # 历史/否定语境(取自…已作废 / 不是索引库 c4ext1 …)由行级 NEGATION 跳过;活写才红。
    ("企点 native_id/kind/会话来源写成索引库 c4ext1(R6-36 正线已改主库 frienduin+istroop)",
     "0[1-7]-*.md",
     r"c4ext1",
     "R6-36/R6-37:主库无 c4ext1 列;native_id/kind/会话来源取主库 XOR(frienduin)+istroop。抄索引库 c4ext1=去滞后/可能未建的索引库取会话来源,会话分组错、复现能发不能读。"),
    # R6-38(第七轮 cursor 评审):R6-37 收了去重键/native_id,但 06 §2.9.1 字段映射的 type/ts/self 仍抄
    # 索引库列 c0type/c13exts/c5ext2,而主库正线是 msgtype/time/issend ⇒ 照抄则方向与时间全错、发送确认对不上。
    # 又一次「迁移 token 却没给旧 token 加守卫」= 脚本级假绿(评审原话:全绿不能当闭环)。
    # 现行册正文一律不再出现这三个列名(旧名只留在 00 §15g 裁决表,glob 0[1-7] 已排除)。
    ("企点字段映射写成索引库列 c0type/c13exts/c5ext2(主库正线 = msgtype/time/issend)",
     "0[1-7]-*.md",
     r"c0type|c13exts|c5ext2",
     "R6-38:企点读库字段映射唯一出处 06 §2.9.5——type←msgtype、ts←time、self←issend;索引库列名照抄 = 方向/时间判错。"),
    ("企点读库正线的发送确认仍写「会话历史 / history」",
     "0[1-7]-*.md",
     # ⚠️ 06 §2.12 那一行本身含「历史」二字,会被行级 NEGATION 整行跳过 ⇒ 这里只守 02 B-07 的期望三元组;
     #    06 §2.12 企点行靠只读终审核对(行级 NEGATION 的已知假阴,见 R6-37 挂账)。
     r"DELIVERED,<原生id>,history",
     "R6-38:读库正线确认 = ingest 合并(confirmed_by=ingest_merge、ext_msg_id=qd:{uniseq});history 仅控件树降级路线。"),
    ("崩溃转储 L2 仍写「A|D 二选一」(只认方案 D)",
     "0[0-7]-*.md",
     r"内核构建时二选一|或方案 A[=:]|走方案 A/D",
     "R6-38:L2 只认方案 D(真机现役 v4);写成二选一 = CI 把 CONFIG_COREDUMP=n 当合法交付。"),
    ("manifest coredump_l2 仍允许 \"A\"",
     "0[1-7]-*.md",
     r'"D"` 或 `"A"|A=CONFIG_COREDUMP=n\)',
     "R6-38:coredump_l2 恒为 \"D\",其它值 CI 构建失败。"),
    ("企点主库正线仍写先 .backup 整库副本再读",
     "0[1-7]-*.md",
     r"定时 `\.backup`|先 `\.backup` 出一致副本|`\.backup` 一致副本",
     "R6-38:主库正线 = 设备上 sqlite3 mode=ro 只读直查增量(实测读取器 echo_loop_maindb.py);.backup 整库副本只属索引库兜底,主库 64 MB、确认窗 1 s 一轮做不了。"),
    # R6-39:06 §2.9.5 曾把企点 XOR 密钥写成「MAC 6 字节循环」;真机对照 = ASCII 字符串 "02:00:00:00:00:00" 的 17 字节循环,
    # 按 6 个原始字节解出来全是乱码。正文勘误句刻意不复述旧字面量,故这里直接禁该字面量。
    ("企点 XOR 密钥写成 6 字节循环(实为 ASCII 字符串 17 字节循环)",
     "0[1-7]-*.md",
     r"6 字节循环",
     "R6-39:KEY = b\"02:00:00:00:00:00\"(17 字节 ASCII),in[i] ^ KEY[i % 17];按 MAC 原始 6 字节取模解出全是乱码。"),
    ("Adapter.poll 仍是无参签名(企点确认窗加速轮无法实现)",
     "02-*.md",
     r"async def poll\(acct\) ->",
     "R6-39:poll(acct, *, only_sessions=None);无该形参则 [adapters.qidian] confirm_poll_interval_ms 无可编码语义。"),
    ("ingest 仍是两元组返回(承不起 inserted or changed 的发事件规则)",
     "02-*.md",
     r"-> \(inserted: bool, id\)",
     "R6-41:ingest -> (inserted, changed, id);message 事件发出条件三通道统一 inserted or changed。"),
    # R6-43:企点文本族 = {-1000,-1051,-1049};旧口径只认两种 ⇒ 群里所有 @ 消息(-1049,占 2.1%)被当非文本丢掉。
    ("企点文本 msgtype 仍写成只认 -1000/-1051 两种(漏 -1049 含@文本)",
     "0[1-7]-*.md",
     r"\{-1000, ?-1051\}|\(-1000, ?-1051\)|\{-1000 普通文本, -1051",
     "R6-43/R6-44:按 msgtype 一级路由(06 §2.9.5 权威表);文本族三种 + -1035 有文本段者(图片段写 [图片] 占位)才产出 Message,其余 msgtype 一律不产出、水位照常越过。"),
    # R6-44:基线 §7.4 Message 与 02 messages DDL 都没有 origin_json 字段(它只存在于 media 表与 media_json[] 元素内)。
    ("引用了不存在的字段 Message.origin_json",
     "0[1-7]-*.md",
     # R6-46:再放宽——`Message` 与 `origin_json` 相距 ≤ 12 字即命中(「随 `Message` 落 `origin_json`」「写入 `Message` 的 `origin_json`」这类分写);
     #        首跑即打红 06 §2.9.1 那句旧话(确实在引用不存在的字段)⇒ 已把那句改掉,不靠 NEGATION 放行。
     r"Message\.origin_json|`?Message`?[^\n`]{0,12}`?origin_json",
     "R6-44:Message 没有 origin_json;读库路只产出文本,非文本行不产出 Message(06 §2.9.5)。"),
    ("confirm_expires_ms 写成过期即清空",
     "0[1-7]-*.md",
     r"清空\s*`?confirm_expires_ms|confirm_expires_ms`?\s*清空",
     "R6-19:一经写入即保留(02 DDL 口径);写「清空」会让 06 验收判挂 02 规范 SQL。"),
    ("全量清理端点不带 /run",
     "0[1-7]-*.md",
     r"POST /system/cleanup(?!/run)",
     "R6-24:正式名 #109 POST /system/cleanup/run。"),
    ("旧审计动作名 confirm:approve",
     "0[1-7]-*.md",
     r"confirm:(approve|reject|expire)",
     "R6-25:以 audit_log owner 02 为准 = mail.pending_confirm.approved|rejected|expired。"),
    ("提权恢复序列里出现 kill-server(06/05/02)",
     "0[256]-*.md",
     r"(adb root|提权)[^\n]{0,200}adb kill-server|adb kill-server[^\n]{0,80}(adb root|提权)",
     "R6-28:提权后的 offline 恢复只重连该账号;kill-server 时机唯一出处 = 04 §2.7.4③,会波及其它企点账号。"),
    ("last_cleanup 嵌套形状",
     "0[1-7]-*.md",
     r"last_cleanup\s*[:.]\s*\{?\s*(at|freed_mb)|last_cleanup\.freed_mb",
     "R6-30:扁平 disk_watermark.last_cleanup_at / last_cleanup_freed_mb(04 §2.4.5 owner)。"),
    ("引用不存在的节号 02 §retention",
     "0[1-7]-*.md",
     r"02 §retention",
     "R6-30:实体是 02 §2.8.4。"),
    ("07 使用他册行号锚(NN:行号)",
     "07-*.md",
     r"(?<![0-9A-Za-z:/.])0[0-6]:[0-9]{2,4}\b",
     "R6-25/R6-31:行号必漂(终审实测 10 处已指向无关键);一律小节锚。"),
    ("确认钥写成 v1 用于算确认签名",
     "0[0-7]-*.md",
     r"用于算确认签名",
     "R6-31:v1 只生成、只保管、无消费者;M6+ oob 才用。"),
    ("danger 清单写成九项",
     "0[1-7]-*.md",
     r"danger[^\n]{0,12}九项(?![^\n]*十项)",
     "R5-8/R6-31:权威集合十项(含 account_stop);按九项实现 = 邮件默认就能停账号。"),
    ("邮件清理判据只排除 OUT_OF_SCOPE(漏 OVERSIZE)",
     "0[26]-*.md",
     r"status\s*(!=|≠)\s*OUT_OF_SCOPE",
     "R6-1:一律 `status ∉ NEVER_DELETE`({OUT_OF_SCOPE, OVERSIZE});漏了 = 超大信 30 天后被删远端。"),
    ("metrics `1h` 仍回 90d",
     "0[1-7]-*.md",   # 00 §15x 是历史裁决表,会引述旧句,不扫,
     r"`1h`[^\n]{0,12}回\s*\**90",
     "R6-3:30 天红线(§11.10);DTO 说明句曾两轮漏改。"),
    ("docker 选段仍挂 DISTRO_IMPORTED 之后",
     "0[34]-*.md",
     r"`DISTRO_IMPORTED`\s*之后[^\n]{0,30}(选段|pick_docker_pool|执行)",
     "R6-2/R5-9:选段在 wsl --import 后、首起 dockerd 前,不以 .imported 为前置;否则 172.17 已被占。"),
    ("HDRSAN 旧锚点(§11.17 ④/⑤)",
     "0[1-7]-*.md",   # 00 §15x 是历史裁决表,会引述旧句,不扫,
     r"11\.17\s*[④⑤]\s*(的\s*`?hdr_sanitize|\[HDRSAN\])",
     "R6-11:锚点是 §11.17 ⑥ [HDRSAN];④ 是「指令集同一份」、⑤ 是 N-4。"),
    ("confirm_nonce/confirm_via 写成「有列恒 NULL」",
     "0[0-7]-*.md",
     r"confirm_(nonce|via)[^\n]{0,40}(列先建好|保留但)",
     "R6-7 补裁:v1 无此两列(02 DDL 为准);「无列」与「恒 NULL」不得并存。"),
    ("控制台直调 WinAgent 微信 login 端点",
     "0[15]-*.md",
     r"Agent/控制台\*{0,2}主动调",
     "R6-5:login/start|cancel、bind、logout、read 只由 Agent 调;控制台直打 17610 cancel 会卡死 pending。"),
    ("备份后缀 bak.<ts>(应为 bak-<ts>)",
     "0[34]-*.md",
     r"\.bak\.(<|\*)",
     "R6-14:统一 bak-<yyyyMMdd-HHmmss>,与已投放脚本一致。"),
    ("hosts BEGIN/END 围栏写法",
     "0[345]-*.md",
     r"# >>> QTrade",
     "R6-15:hosts 是逐行行尾标记(04 §2.5.4),不是围栏块。"),
]

# ---------------------------------------------------------------- PAIRED
# (标题, 声明方文件, 声明方正则, 消费方文件, 消费方正则, 说明)
PAIRED = [
    # ---- R6-47~R6-50(第八轮 cursor 评审 8.4/10)----
    # R6-47:norm() 三册到处引用、无一处可抄函数体(第八轮唯一 P0)。凡引用 norm(text) 的册,06 §2.9.2 必须有那一行 def。
    # 反向验证:改前备份上 06 无 `def norm(` ⇒ 两条都红(2026-09-19 实测)。
    ("norm():02 §2.8.1 引用 → 06 §2.9.2 必须有可抄函数体",
     "02-*.md", r"norm\(text\)",
     "06-*.md", r"def norm\(s: str \| None\) -> str:",
     "R6-47:norm() 是 fingerprint 的输入与出向合并判据;无定义 = 三通道各写一套归一化,确认与去重对不上。"),
    ("norm():06 §2.12/§2.9.2 引用 → 06 §2.9.2 必须有可抄函数体",
     "06-*.md", r"norm\(text\)",
     "06-*.md", r"def norm\(s: str \| None\) -> str:",
     "R6-47:同上;06 是 owner,定义只许在 §2.9.2 出现一次(⑪ NORM 另查函数体)。"),
    # R6-48:出向文本入口校验的 reason 值 —— 06 §2.12 定名 → 02 §3.10 校验段承接 → 01 §2.10 文案承接。
    # 反向验证:用「抹掉登记行」的副本(声明方当时也没有该词,只在改前备份上跑会只出 ⚠️,不够)。
    ("text_has_control_chars:06 §2.12 定名 → 02 §3.10 bus 校验段必须承接",
     "06-*.md", r"text_has_control_chars",
     "02-*.md", r"error\.reason='text_has_control_chars'",
     "R6-48:校验点在 bus(02);06 只引用。02 没有 = 含 U+0014 的出向文本照发,该行恒 UNCONFIRMED。"),
    ("text_has_control_chars:02 §3.10 → 01 §2.10 INVALID_ARGS 文案必须承接",
     "02-*.md", r"text_has_control_chars",
     "01-*.md", r"text_has_control_chars",
     "R6-48:01 不认这个 reason = 用户只看到「参数错误」、不知道该删什么。"),
    # R6-49:00 §7.4 事件专属三字段 → 01 P-MSG 必须有承接 testid(此前 01 零渲染,评审 P1)。
    # 反向验证:改前备份上 01 无这两个 testid ⇒ 两条都红。
    ("lag_s/late:00 §7.4 事件专属字段 → 01 P-MSG 必须有 qt-msg-row-{id}-late",
     "00-*.md", r"`lag_s`",
     "01-*.md", r"qt-msg-row-\{id\}-late",
     "R6-49:迟到消息在画面上看不见;06 验收已在断言 payload.late,01 不渲染 = 断言无 UI 对应物。"),
    ("origin=external:00 §7.4 → 01 P-MSG 必须有 qt-msg-row-{id}-origin-external",
     "00-*.md", r'origin: "rpa"\|"external"',
     "01-*.md", r"qt-msg-row-\{id\}-origin-external",
     "R6-49:他端代发的我方消息在画面上看不见。"),
    # R6-50:审计动作名 owner = 02(R6-25「以 02 为准」)。06 产生 qidian.rebootstrap → 02 必须登记、05 必须同名。
    # 反向验证:改前备份上 02/05 均无该名 ⇒ 两条都红。
    ("qidian.rebootstrap:06 §2.9.5 ③ 产生 → 02 §3.1 audit_log 必须登记",
     "06-*.md", r'audit\("qidian\.rebootstrap"',
     "02-*.md", r"action='qidian\.rebootstrap'",
     "R6-50:审计名以 02 为准;06 写了 02 没登记 = P-LOG 无固定动作名、实现者另起同义名。"),
    ("qidian.rebootstrap:06 产生 → 05 §2.1.1 ⑪a 必须同名引用",
     "06-*.md", r'audit\("qidian\.rebootstrap"',
     "05-*.md", r"qidian\.rebootstrap",
     "R6-50:05 是换号语义的入口,不同名 = 两册对同一动作两个名字。"),
    # R6-50:02 §3.7 登记的两个「读取未降级」码,01 §2.10 必须写明承接方式(只进铃),否则实现者顺手并进「读取已降级」横幅。
    # 反向验证:改前备份上 01 全文 0 命中 ⇒ 两条都红。
    ("QIDIAN_TABLE_DECODE_STUCK:02 §3.7 登记 → 01 §2.10 必须写承接方式",
     "02-*.md", r"`QIDIAN_TABLE_DECODE_STUCK` \| warn",
     "01-*.md", r"QIDIAN_TABLE_DECODE_STUCK",
     "R6-50:01 不提 = 可能并进 qt-acct-detail-read-degraded 横幅,把「一张表读不到」渲染成「整账号读取已降级」。"),
    ("QIDIAN_MSG_GAP:02 §3.7 登记 → 01 §2.10 必须写承接方式",
     "02-*.md", r"`QIDIAN_MSG_GAP` \| warn",
     "01-*.md", r"QIDIAN_MSG_GAP",
     "R6-50:同上。"),
    # R6-41:新码 / 新 reason 值 —— 06 产生 → 02 §3.7 必须登记。
    ("QIDIAN_TABLE_DECODE_STUCK:06 产生 → 02 §3.7 必须登记",
     "06-*.md", r"QIDIAN_TABLE_DECODE_STUCK",
     "02-*.md", r"`QIDIAN_TABLE_DECODE_STUCK` \S warn \S account:<id>",
     "R6-41:个别表解不出用独立码(读取未降级);只在产生方出现 = 下游订阅对着空气。"),
    ("reason=clock_unsynced:06 产生 → 02 §3.7 reason 枚举必须收录",
     "06-*.md", r"clock_unsynced",
     "02-*.md", r"decode_failed, clock_unsynced\}",
     "R6-41:H13 时钟漂移时不建历史闸基准,12 轮后以该 reason 告警;02 是 evidence 枚举 owner。"),
    # R6-39:新告警码 / 新配置键 / 新协议形参 —— owner 册必须登记(产生方 06 → 登记方 02)。
    ("QIDIAN_MSG_GAP:06 产生 → 02 §3.7 必须登记",
     "06-*.md", r"QIDIAN_MSG_GAP",
     "02-*.md", r"`QIDIAN_MSG_GAP` \S warn \S account:<id>",
     "R6-39:告警码枚举由 02 登记(机器码单一来源);只在产生方出现 = 下游渲染/订阅对着空气。"),
    ("late_after_s:06 引用 → 02 §7.1 [messages] 必须登记",
     "06-*.md", r"late_after_s",
     "02-*.md", r"`late_after_s` \S `120`",
     "R6-39:message 事件 payload.late 的阈值;owner = 02 §7.1。"),
    ("群缺口三键:06 引用 → 02 §7.1 [adapters.qidian] 必须登记",
     "06-*.md", r"gap_window_days",
     "02-*.md", r"`gap_check_interval_s` / `gap_window_days` / `gap_min_missing` \S `60` / `3` / `5`",
     "R6-39:check_group_gaps 的周期/窗口/下限;owner = 02 §7.1。"),
    ("企点 self_uid 口径:06 拿它拼 {uin}.db → 05 §2.1.1 必须定义",
     "06-*.md", r"acct\.self_uid \+",
     "05-*.md", r"企点 self_uid = 登录 uin",
     "R6-39:self_uid 回填成登录账号(手机号/邮箱)则库路径必错、读取永久降级。"),
    # R6-38:主库按会话分表 ⇒ 必须有表发现算法与首次 bootstrap 标记,否则新会话永不入库 / 首登误告。
    ("企点主库分表 → 06 必须有 sqlite_master 表发现",
     "06-*.md", r"mr_friend_",
     "06-*.md", r"sqlite_master",
     "R6-38:新会话 = 新表,读循环每轮枚举 sqlite_master;没有发现算法则新会话永不入库。"),
    ("企点 qidian_bootstrap 游标:06 定义 → 02 §2.8.3 必须登记",
     "06-*.md", r"qidian_bootstrap",
     "02-*.md", r"qidian_bootstrap",
     "R6-38:判「首次 bootstrap」只认该标记行;02 是 cursors 表 owner,缺行 = 实现者按「有无 qidian_rowid 行」判首次 ⇒ 丢第一条消息。"),
    ("企点确认窗加速键:06 引用 → 02 §7.1 必须登记",
     "06-*.md", r"confirm_poll_interval_ms",
     "02-*.md", r"`confirm_poll_interval_ms` \| `1000` \| \*\*R6-38",
     "R6-38:[adapters.qidian] confirm_poll_interval_ms 由 02 §7.1 登记(owner),06 只引用。"),
    ("基线 §10 列的端点,02 端点表必须有对应行",
     "00-*.md", r"POST /api/v1/mail/pending-confirms/\{id\}/approve",
     "02-*.md", r"pending-confirms",
     "01/06 已在调这些端点;02 是端点唯一出处,缺行就是对着空气调。"),

    ("login/cancel 端点",
     "00-*.md", r"POST /api/v1/accounts/\{id\}/login/cancel",
     "02-*.md", r"login/cancel",
     "微信槽位 pending 的三条释放路径之一。"),

    ("winagent.db 的 settings 表",
     "00-*.md", r"`settings`\*\*\(\*\*R2-5|winagent\.db[^\n]{0,400}settings",
     "02-*.md", r"winagent[^\n]{0,40}settings|settings[^\n]{0,40}winagent",
     "hosts/powercfg/探测真值要落盘;基线 §7.7 已补登,02 须出 DDL。"),

    ("取钥两码 WAIT_KEY_*",
     "00-*.md", r"WAIT_KEY_IMG",
     "05-*.md", r"WAIT_KEY_IMG",
     "05 是 state_code 唯一维护方,必须登记。"),

    # 2026-09-18:04 定义了 DOCKER_POOL_ALL_CONFLICT,01 全文 0 命中 —— 与 WINAGENT_* 漏收同类。
    # 后果都是「该告警真正出现的那一刻,前端渲染不出东西」。
    # 这条冲突尤其要命:症状是「某些内网地址访问不通」,用户会去查 VPN/公司网络/DNS,
    # 极难联想到是本机 docker 网段占了那一段 —— 前端不常驻显示,他永远查不到。
    ("04 的告警码 DOCKER_POOL_ALL_CONFLICT,01 必须能渲染",
     "04-*.md", r"DOCKER_POOL_ALL_CONFLICT",
     "01-*.md", r"DOCKER_POOL_ALL_CONFLICT|docker[^\n]{0,20}网段[^\n]{0,20}冲突",
     "上游定义告警码、下游漏收 = 告警出现时前端空白。"),

    # 2026-09-18 第四轮:按册分派任务时,新增的码【必然】只落在产生方那一册 ——
    # 因为按册切的任务里没有任何一册"负责"消费方。这是【分派方式本身】的缺陷,不是谁写漏了。
    # 靠人记着不如靠机器抓:每次新增对外告警码,就在这里加一条 产生方→02登记 / 产生方→01渲染。
    ("04 新增码 WECHAT_DISK_LOW,02 必须登记",
     "04-*.md", r"WECHAT_DISK_LOW",
     "02-*.md", r"WECHAT_DISK_LOW",
     "02 是告警码枚举单一来源(§7.5);未登记则 CHECK/枚举建不出来。"),
    ("04 新增码 WECHAT_DISK_LOW,01 必须能渲染",
     "04-*.md", r"WECHAT_DISK_LOW",
     "01-*.md", r"WECHAT_DISK_LOW|微信[^\n]{0,12}盘",
     "微信盘满是持续态,用户可能在告警滚过后才来看,前端必须有常驻文案。"),
    ("06 新增码 MAIL_MSG_OVERSIZE,02 必须登记",
     "06-*.md", r"MAIL_MSG_OVERSIZE",
     "02-*.md", r"MAIL_MSG_OVERSIZE",
     "同上:告警码单一来源在 02。"),
]

# ---------------------------------------------------------------- KEYNAME
# 「同一设置、同一文件、两个键名」—— 比同名不同物更隐蔽,因为【grep 都对不上】。
# 已出现 3 次:dpapi_scope/scope、wxkey_dlls/dll_candidates、slot_error_takeover_s/holder_fault_takeover_s。
# 后果:代码读哪个,另一个就静默失效(不报错、不崩溃,只是配置不生效)。
# 规则很简单:废弃的那个拼写,除「勘误/作废」语境外不该再出现。
DEAD_KEYNAMES = [
    ("holder_fault_takeover_s", "slot_error_takeover_s", "02 §7.1 是配置键的家,05 是引用方"),
    ("dll_candidates",          "wxkey_dlls",            "以 02 配置总表为准"),
]


# 记录/对比语境的标志:出现这些就说明该行是在【描述这个键名分歧】,不是活引用。
# 首跑就误报了 4 处(裁决表 00 §15*、各册变更记录、07 配置总表的 (b) 汇总)——
# 那些地方【本来就该提到废弃拼写】,否则没法记录问题。再次印证:误报比漏报更伤。
_KEYNAME_RECORD_MARKS = ("→", "vs", "为准", "键名", "两个", "分歧", "改名", "同设置", "同一设置")


def check_keynames():
    red = []
    print()
    print("=" * 78)
    print("⑦ KEYNAME —— 同一设置的废弃拼写(grep 对不上,最隐蔽)")
    print("=" * 78)
    for dead, alive, why in DEAD_KEYNAMES:
        hits = []
        for f in sorted(glob.glob(os.path.join(HERE, "0[0-7]-*.md"))):
            for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
                if dead not in ln:
                    continue
                if any(k in ln for k in NEGATION):
                    continue
                if any(k in ln for k in _KEYNAME_RECORD_MARKS):
                    continue          # 记录语境:裁决表 / 变更记录 / 07 的分歧汇总
                hits.append(f"{os.path.basename(f)[:2]}:{i}")
        if hits:
            red.append(f"废弃键名 {dead} 残留")
            print(f"  ❌ {dead} 仍在活引用 —— {', '.join(hits[:6])}")
            print(f"     应为 {alive}({why});代码读哪个,另一个静默失效")
        else:
            print(f"  ✅ {dead:28} 已全改为 {alive}")
    return red


# ---------------------------------------------------------------- COPYABLE
# 第五轮教训:「说明段改了、可抄的句子没改」。实现者直接抄的是
# 动作表 / 表格标题 / DTO 字段注释 / 循环伪代码 / systemd 依赖顺序 —— 不是散文。
# 「半改」比「没改」更险:同一节上下两句都能写码【而且相反】。
#
# 这一类查的是【危险组合】:某个"安全的说法"与某个"危险的可抄句"同时存在。
# 每条都来自一个真实踩过的坑,不是假想。
COPYABLE = [
    ("06 磁盘 warn 行不得引用删远端的那一节",
     "06-*.md",
     r'`warn`[^\n|]*\|[^\n|]*\|[^\n]*§2\.6\.3①',
     "§2.6.3① 的循环是 archive→delete_on_server。warn 行引用它 = 本机剩 5GB 删用户邮箱服务器上的信。"),

    ("02 能力目录标题不得写「邮件触发须 confirm:true」",
     "02-*.md",
     r'能力目录[^\n]{0,80}confirm:true|confirm:true[^\n]{0,40}(?:邮件触发|九项|十项)',
     "能力目录是实现者最常抄的一张表;写着 confirm:true 就是邮箱自批,§11.17③ 失效。"),

    ("04 metrics DTO 不得按四分区取最坏",
     "04-*.md",
     r'(?:disks|disk_watermark)[^\n]{0,80}四(?:个受检)?分区',
     "微信盘是用户自己的;算进产品级水位 = 用户盘满拖垮整站。只看我方三分区。"),

    # ⚠️ 下面两条【已撤除】,保留说明作为教训。
    #
    # 原本想查:①06 入站循环无条件 retr;②03 写 daemon.json 的单元排在 After=docker.service。
    # 两条都是真实的坑(R5-6 / R5-9),但【不适合用单行正则查】——首跑 4 处全是误报:
    #   06:104 「SIZE 门必须在这条循环里」   ← 说明文字
    #   06:112 「绝不 FETCH BODY.PEEK[]」    ← 否定句(否定词在 12 字符窗口外)
    #   06:130 「raw = pop.retr(n)  # 过门后才取正文」 ← 改对之后的代码,注释里有模式词
    #   03:613 「qtrade-docker-config.service(Before=docker.service)」 ← 正确写法,同行恰好也提到 After
    # 根因:这两条查的是【代码模式】,而代码块里的注释、旁边的说明文字、以及"正确写法"本身
    # 都会含同样的关键词。要可靠就得解析代码块结构,成本远超收益。
    #
    # ✅ 上面三条(warn 行 / 能力目录标题 / DTO)能可靠工作,因为它们匹配的是【特定结构】
    #    ——表格行、标题、DTO 字段注释,位置固定、不会被散文污染。
    # ⇒ 结论:COPYABLE 这一类只收「结构化位置」的规则;「代码模式」类交给人工评审。
    #    宁可这条规则少查两项,也不能让它误报——误报比漏报更伤(本脚本已因此校准三次)。
]


def check_copyable():
    red = []
    print()
    print("=" * 78)
    print("⑧ COPYABLE —— 可抄的句子(动作表/标题/DTO/循环/依赖顺序)")
    print("=" * 78)
    for title, pat_glob, pat, why in COPYABLE:
        hits = []
        for f in sorted(glob.glob(os.path.join(HERE, pat_glob))):
            for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
                m = re.search(pat, ln)
                if m and not any(k in ln for k in NEGATION) and not _is_negated(ln, m.start()):
                    hits.append((os.path.basename(f)[:2], i, ln.strip()[:95]))
        if hits:
            red.append(title)
            print(f"\n  ❌ {title} —— {len(hits)} 处")
            print(f"     后果:{why}")
            for vol, i, ln in hits[:4]:
                print(f"       {vol}:{i}  {ln}")
        else:
            print(f"  ✅ {title}")
    return red


def check_forbidden():
    red = []
    print("=" * 78)
    print("① FORBIDDEN —— 真值表里不该再出现的旧语义")
    print("=" * 78)
    for title, pat_glob, pat, why in FORBIDDEN:
        hits = []
        for f in sorted(glob.glob(os.path.join(HERE, pat_glob))):
            for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
                m = re.search(pat, ln)
                if m:
                    if any(k in ln for k in NEGATION):
                        continue
                    if _is_negated(ln, m.start()):
                        continue
                    hits.append((os.path.basename(f)[:2], i, ln.strip()[:110]))
        if hits:
            red.append(title)
            print(f"\n  ❌ {title}  —— {len(hits)} 处")
            print(f"     理由:{why}")
            for vol, i, ln in hits[:8]:
                print(f"       {vol}:{i}  {ln}")
            if len(hits) > 8:
                print(f"       … 另 {len(hits)-8} 处")
        else:
            print(f"  ✅ {title}")
    return red


def check_paired():
    red = []
    print()
    print("=" * 78)
    print("② PAIRED —— 声明方有、消费方也必须有")
    print("=" * 78)
    for title, dfile, dpat, cfile, cpat, why in PAIRED:
        dsrc = next(iter(glob.glob(os.path.join(HERE, dfile))), None)
        csrc = next(iter(glob.glob(os.path.join(HERE, cfile))), None)
        if not dsrc or not csrc:
            print(f"  ⚠️  {title}:文件缺失,跳过")
            continue
        declared = bool(re.search(dpat, io.open(dsrc, encoding="utf-8").read()))
        consumed = bool(re.search(cpat, io.open(csrc, encoding="utf-8").read()))
        if declared and not consumed:
            red.append(title)
            print(f"\n  ❌ {title}")
            print(f"     {os.path.basename(dfile)[:2]} 声明了,但 {os.path.basename(cfile)[:2]} 没有对应行")
            print(f"     理由:{why}")
        elif not declared:
            print(f"  ⚠️  {title}:声明方未找到(可能措辞变了,请人工确认)")
        else:
            print(f"  ✅ {title}")
    return red


# ---------------------------------------------------------------- ENUM
# 基线定义了枚举值,消费方必须【逐个】收录。
# 2026-09-18 教训:基线 §8.1 有 WINAGENT_OFFLINE / WINAGENT_USER_OFFLINE,
# 而 01 与 05 全文 0 命中 —— PAIRED 那类检查抓不到它(它只看"有没有提过这个概念"),
# 漏的后果很具体:WinAgent 一掉线,账号带着一个 codes.ts 查不到的码推到前端,
# 控制台渲染不出引导卡片(设计上明令"不能靠解析中文"),而这正是该码最可能出现的时刻。
ENUMS = [
    ("state_code 全集(基线 §8.1 → 01 中文表 / 05 维护表)",
     ["WAIT_PASSWORD", "WAIT_SMS", "WAIT_CAPTCHA", "WAIT_DEVICE_CONFIRM", "WAIT_QRCODE",
      "WAIT_NARRATOR", "WAIT_UI_TREE", "WAIT_KEY_IMG", "WAIT_KEY_RELOGIN",
      "KICKED", "LOGGED_OUT", "TOKEN_EXPIRED", "LOGIN_TIMEOUT",
      "BOOT_TIMEOUT", "APK_UNAVAILABLE", "INSTALL_FAILED", "BAD_CREDENTIAL",
      "VAULT_UNAVAILABLE", "UI_UNEXPECTED", "ONEBOT_UNREACHABLE", "CONTAINER_EXIT",
      "NARRATOR_UNAVAILABLE", "KEY_FAIL", "SCREEN_LOCKED",
      "WINAGENT_OFFLINE", "WINAGENT_USER_OFFLINE"],
     ["01-*.md", "05-*.md"],
     "漏一个 = 该状态到了前端渲染不出引导卡片。"),
    # 只盯【有页面承接元素】的两个企点码;QIDIAN_PROFILE_FALLBACK 之类转瞬即逝的告警按 01 §2.10 规矩
    # 由 Agent 带 title/message、前端不收录 —— 全盯会误报(首跑就误报了它),误报比漏报更伤。
    ("企点读取降级告警码(02 §3.7 登记 → 01 文案 / 05 失败表 / 06 §2.9.5 / 07)",
     ["QIDIAN_NOT_ROOT", "QIDIAN_DB_UNAVAILABLE"],
     ["01-*.md", "02-*.md", "05-*.md", "06-*.md", "07-*.md"],
     "四审 N-1:两种成因两个码;漏了 = root 正常而 schema 变更时无码可发或发错码。"),
]


def check_status_mirror():
    """06 的 mail_inbox 失败终态全集 ⊆ 02 的 status CHECK。

    2026-09-18 第四轮:02 的 CHECK 少了 4 个 06 终态码,其中 3 个是【前两轮就漏的】
    (CONFIRM_REQUIRED/CONFIRM_EXPIRED 来自 R-03、ROUTE_MISMATCH 来自 E-5),一直没人发现。
    后果是硬的:06 一写 status='OVERSIZE',CHECK 直接 INSERT 失败。

    风险是【不对称】的 —— 02 多收一个没人写的值 = 无害;少收一个会写的值 = 崩溃。
    所以这条只查「06 有而 02 没有」,不查反向。
    """
    red = []
    print()
    print("=" * 78)
    print("⑥ STATUS —— 06 的 mail_inbox 失败终态,02 的 CHECK 必须逐值镜像")
    print("=" * 78)
    p06 = next(iter(glob.glob(os.path.join(HERE, "06-*.md"))), None)
    p02 = next(iter(glob.glob(os.path.join(HERE, "02-*.md"))), None)
    if not (p06 and p02):
        print("  ⚠️  文件缺失,跳过")
        return red
    t06 = io.open(p06, encoding="utf-8").read()
    t02 = io.open(p02, encoding="utf-8").read()
    m = re.search(r'^失败终态[：:]([^\n]+)$', t06, re.M)
    if not m:
        print("  ⚠️  06 未找到「失败终态：」行(措辞可能变了),需人工核")
        return red
    codes = sorted(set(re.findall(r'`([A-Z_]{4,})`', m.group(1))))
    missing = [c for c in codes if c not in t02]
    print(f"  06 失败终态共 {len(codes)} 个")
    if missing:
        red.append(f"02 的 status CHECK 缺 {len(missing)} 个 06 终态码")
        print(f"  ❌ 02 缺 {len(missing)} 个 —— {', '.join(missing)}")
        print("     06 一旦写这些值,02 的 CHECK 直接 INSERT 失败(硬崩,不是口径分歧)")
    else:
        print(f"  ✅ 02 已逐值镜像({len(codes)}/{len(codes)})")
    return red


def check_enums():
    red = []
    print()
    print("=" * 78)
    print("④ ENUM —— 基线枚举值,消费方必须逐个收录")
    print("=" * 78)
    for title, values, consumers, why in ENUMS:
        for cglob in consumers:
            src = next(iter(glob.glob(os.path.join(HERE, cglob))), None)
            if not src:
                continue
            text = io.open(src, encoding="utf-8").read()
            missing = [v for v in values if v not in text]
            vol = os.path.basename(src)[:2]
            if missing:
                red.append(f"{vol} 缺 {len(missing)} 个 state_code")
                print(f"\n  ❌ {vol} 缺 {len(missing)}/{len(values)} 个 —— {', '.join(missing)}")
                print(f"     理由:{why}")
            else:
                print(f"  ✅ {vol} 收录完整({len(values)}/{len(values)})")
    return red


# ---------------------------------------------------------------- MIRROR
# 02 §7 是「配置总表」,它 restate 了 owner 分册(04/05/06)的配置键。
# 基线 R3-39 裁决:**保留 02 的完整键值(总表价值就在一处看全貌),双写风险用工具消除**
# —— 「当『单一来源』与『可用性』冲突时,优先保可用性 + 上自动对账,而不是硬拆文档」。
# 本检查就是那个对账:比对 02 的镜像值与 owner 的值,不等即红。
#
# ⚠️ 保守设计:提取不到值时报「需人工核」而不是报红。
#    误报会让人从此不看这个脚本 —— 这是本脚本首跑 24 处误报换来的教训。
MIRRORS = [
    # (键名, owner 文件 glob, owner 分册名)
    ("keep_awake_mode", "04-*.md", "04"),
    ("narrator_min_seconds", "05-*.md", "05"),
    ("chatlog_port", "05-*.md", "05"),
    ("processed_folder", "06-*.md", "06"),
    ("h1_retention_d", "04-*.md", "04"),
    ("public_ip_check_interval_s", "04-*.md", "04"),
]


def _val_in_toml(text, key):
    """从 owner 的 toml 代码块里取 `key = value`(去注释、去引号)。"""
    m = re.search(r'^\s*' + re.escape(key) + r'\s*=\s*([^#\n]+)', text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'").strip()


def _val_in_02_table(text, key):
    """从 02 的 markdown 配置表取该键那一行的『默认值』列。"""
    m = re.search(r'^\|[^|\n]*\|\s*`' + re.escape(key) + r'`\s*\|\s*([^|\n]+)\|', text, re.M)
    if not m:
        return None
    v = m.group(1).strip().strip('`').strip('*').strip().strip('"').strip("'")
    return v


def check_mirrors():
    red, warn = [], 0
    print()
    print("=" * 78)
    print("⑤ MIRROR —— 02 配置总表的镜像值 vs owner 分册的值(R3-39)")
    print("=" * 78)
    p02 = next(iter(glob.glob(os.path.join(HERE, "02-*.md"))), None)
    if not p02:
        print("  ⚠️  02 不存在,跳过")
        return red
    t02 = io.open(p02, encoding="utf-8").read()
    for key, oglob, oname in MIRRORS:
        osrc = next(iter(glob.glob(os.path.join(HERE, oglob))), None)
        if not osrc:
            continue
        ov = _val_in_toml(io.open(osrc, encoding="utf-8").read(), key)
        mv = _val_in_02_table(t02, key)
        if mv is not None and ("唯一出处" in mv or "owner" in mv.lower() or "见 " in mv or "= 0" == mv.strip()[:3]):
            print(f"  ✅ {key:28} 02 已改为引用 owner({oname}),无镜像值")
            continue
        if ov is None or mv is None:
            warn += 1
            print(f"  ⚠️  {key:28} 取不到值(02={mv!r} / {oname}={ov!r}),需人工核")
            continue
        if ov == mv:
            print(f"  ✅ {key:28} 两侧一致 = {ov!r}")
        else:
            red.append(f"{key} 镜像值不一致")
            print(f"  ❌ {key:28} **不一致** —— 02={mv!r} vs {oname}={ov!r}")
            print(f"       以 {oname} 为准;02 是镜像(R3-39)")
    if warn:
        print(f"\n  (⚠️ {warn} 项取不到值——保守起见不判红,请人工核;宁可漏报也不误报)")
    return red


def check_versions():
    print()
    print("=" * 78)
    print("③ 版本与依据行")
    print("=" * 78)
    red = []
    for f in sorted(glob.glob(os.path.join(HERE, "0[1-6]-*.md"))):
        head = "\n".join(lines_of(io.open(f, encoding="utf-8").read())[:6])
        vol = os.path.basename(f)[:2]
        has_base = "v1.3" in head
        frozen = ("历史参考" in head) or ("已冻结" in head)
        ok = has_base and frozen
        if not ok:
            red.append(f"{vol} 依据行")
        mark = "✅" if ok else "❌"
        print(f"  {mark} {vol}  基线v1.3={has_base}  主文档已降级={frozen}")
    return red


# ---------------------------------------------------------------- 动态全集(R6-33)
# 值集合不写死在脚本里,而是从 owner 册【现读】—— owner 加一个码,消费方漏了就立刻红。
# 由来:06 §2.7 发 14 个 MAIL_* 码,02 §3.7(机器码单一来源)只登了 12 个、01 还在用自造的 imap_fallback,
# 三轮终审才发现。ENUMS 那种写死清单的检查管不了「owner 新增」这一类。
DYNAMIC_SETS = [
    ("邮件告警码 MAIL_*(owner 06 §2.7 表 → 02 §3.7 登记 / 01 中文文案)",
     "06-*.md", r"^\| `(MAIL_[A-Z_]+)`", ["02-*.md", "01-*.md"],
     "06 发了而 02 没登记 = 告警码表不是单一来源;01 没收录 = 前端渲染不出这条告警。"),
    ("邮件告警 subject 前缀(owner 06 §2.7 表第 2 列 → 02 §3.7 / 01 跳转 / 07 粒度表)",
     "06-*.md", r"^\| `MAIL_[A-Z_]+` \| `([a-z]+):", ["02-*.md", "01-*.md", "07-*.md"],
     "四审 N-1b:06 已 7 种而 07 仍 5 种;粒度写错 = 告警被错误合并或炸开。值形如 `mailbox`,消费方须出现 `mailbox:`。"),
    ("mail_cleanup_log.trigger 取值(owner 06 §3.1 → 02 DDL CHECK)",
     "06-*.md", r"`trigger`[^\n]{0,200}", ["02-*.md"],
     "06 写日志用的 trigger 值 02 的 CHECK 不认 = 该轮清理 INSERT 被拒(R6-26/R6-31 复发过一次)。"),
]


def check_dynamic_sets():
    red = []
    print()
    print("=" * 78)
    print("⑨ DYNAMIC —— 值集合从 owner 册现读,消费方必须逐个收录")
    print("=" * 78)
    for title, owner_glob, pat, consumers, why in DYNAMIC_SETS:
        owner = next(iter(glob.glob(os.path.join(HERE, owner_glob))), None)
        if not owner:
            continue
        otext = io.open(owner, encoding="utf-8").read()
        if "trigger" in title:
            # 06 §3.1 写 trigger 取值的那一行不是表格首列,没法稳妥现读;退而求其次:五值定死,
            # 但校验点是 02 的【CHECK 那一行】逐值都在(R6-31:缺 max_kept ⇒ INSERT 被拒)。
            vals = {"retention", "capacity", "max_kept", "manual", "archive_rotation"}
            src02 = next(iter(glob.glob(os.path.join(HERE, "02-*.md"))))
            chk = [l for l in io.open(src02, encoding="utf-8") if "trigger" in l and "CHECK" in l and "'retention'" in l]   # 02 有三张表带 trigger CHECK,用 'retention' 锁定 mail_cleanup_log 那张
            miss = sorted(v for v in vals if not chk or f"'{v}'" not in chk[0])
            if miss:
                red.append("02 trigger CHECK 缺值"); print(f"  ❌ {title}:02 CHECK 行缺 {miss}\n     理由:{why}")
            else:
                print(f"  ✅ 02 trigger CHECK 五值齐全 —— {title[:30]}")
            continue
        else:
            vals = set(re.findall(pat, otext, re.M))
        if not vals:
            red.append(f"{title[:12]}… 取不到全集"); print(f"  ❌ {title}:owner 册取不到任何值(正则失效?)"); continue
        for cglob in consumers:
            src = next(iter(glob.glob(os.path.join(HERE, cglob))), None)
            text = io.open(src, encoding="utf-8").read()
            needle = (lambda v: v + ':') if 'subject' in title else (lambda v: v)
            missing = sorted(v for v in vals if needle(v) not in text)
            vol = os.path.basename(src)[:2]
            if missing:
                red.append(f"{vol} 缺 {len(missing)} 个({title[:10]}…)")
                print(f"\n  ❌ {title}\n     {vol} 缺 {len(missing)}/{len(vals)} —— {', '.join(missing)}\n     理由:{why}")
            else:
                print(f"  ✅ {vol} 收录完整({len(vals)}/{len(vals)}) —— {title[:34]}")
    return red


QIDIAN_CONFIRM_MIN_MS = 15000   # R6-38:须盖住主库出向落库滞后 7~12 s(06 §2.9.5)+ 1 s 加速轮询


def check_qidian_confirm_window():
    """⑩ 企点确认窗 ≥ 落库滞后上限。凡写出 confirm_timeout_qidian_ms 取值的地方都要 ≥ 15000。"""
    red, seen = [], 0
    print()
    print("=" * 78)
    print("⑩ VALUE —— 企点发送确认窗必须盖住主库落库滞后(R6-38)")
    print("=" * 78)
    pats = [
        r"confirm_timeout_qidian_ms`?\s*=\s*`?(\d+)",                                   # 散文/伪代码里的 key = N
        r"`confirm_timeout_qidian_ms`[^|\n]*\|\s*`\d+`\s*/\s*`(\d+)`",                # 02 §7.1 三键合一行
        r"confirm_timeout_qq/qidian/wechat_ms=\d+/(\d+)/\d+",                          # 07 镜像行
    ]
    for f in sorted(glob.glob(os.path.join(HERE, "0[1-7]-*.md"))):
        for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
            for pat in pats:
                for m in re.finditer(pat, ln):
                    seen += 1
                    v = int(m.group(1))
                    if v < QIDIAN_CONFIRM_MIN_MS:
                        red.append(f"confirm_timeout_qidian_ms={v}<{QIDIAN_CONFIRM_MIN_MS}")
                        print(f"  ❌ {os.path.basename(f)[:2]}:{i}  confirm_timeout_qidian_ms = {v}  < {QIDIAN_CONFIRM_MIN_MS}")
    # R6-39(终审点名):只校验 ≥ 下限还不够——02 §7.1(owner)与 07(镜像)必须同值
    def _one(globpat, pat):
        f = next(iter(glob.glob(os.path.join(HERE, globpat))), None)
        m = re.search(pat, io.open(f, encoding="utf-8").read()) if f else None
        return int(m.group(1)) if m else None
    v02, v07 = _one("02-*.md", pats[1]), _one("07-*.md", pats[2])
    if v02 is not None and v07 is not None and v02 != v07:
        red.append(f"confirm_timeout_qidian_ms 02={v02} ≠ 07={v07}")
        print(f"  ❌ 02 §7.1 = {v02} 与 07 镜像 = {v07} 不相等(以 02 为准)")
    # R6-40(终审点名):R6-39 新增的键同样要 02(owner)↔ 07(镜像)同值
    for label, p02, p07 in [
        ("late_after_s", r"`late_after_s` \S `(\d+)`", r"late_after_s=(\d+)"),
        ("gap 三键", r"`gap_check_interval_s` / `gap_window_days` / `gap_min_missing` \S `(\d+)` / `(\d+)` / `(\d+)`",
                    r"gap_check_interval_s/gap_window_days/gap_min_missing=(\d+)/(\d+)/(\d+)"),
    ]:
        def _grp(globpat, pat):
            f = next(iter(glob.glob(os.path.join(HERE, globpat))), None)
            m = re.search(pat, io.open(f, encoding="utf-8").read()) if f else None
            return m.groups() if m else None
        a02, a07 = _grp("02-*.md", p02), _grp("07-*.md", p07)
        if a02 is None or a07 is None:
            red.append(f"{label} 在 02 或 07 取不到值"); print(f"  ❌ {label}:02={a02} 07={a07}(应两处都有)")
        elif a02 != a07:
            red.append(f"{label} 02≠07"); print(f"  ❌ {label}:02={a02} ≠ 07={a07}(以 02 为准)")
        else:
            print(f"  ✅ {label:14} 02 = 07 = {'/'.join(a02)}")
    if seen < 2:
        red.append("confirm_timeout_qidian_ms 取值处不足 2(02 §7.1 + 07 至少各一处)")
        print(f"  ❌ 只找到 {seen} 处取值,02 §7.1 与 07 至少应各有一处 —— 规则可能失配,须人工核")
    elif not red:
        print(f"  ✅ 共 {seen} 处取值,全部 ≥ {QIDIAN_CONFIRM_MIN_MS}")
    return red


NORM_DEF = r"def norm\(s: str \| None\) -> str:"
NORM_BODY_MUST = (
    ('unicodedata.normalize("NFKC"', "NFKC"),
    ('re.sub(r"\\s+", " ", s).strip()', "折叠空白 + strip"),
    ('return ""', "空值回空串"),
)
NORM_BODY_MUST_NOT = (
    ("\\u0014", "norm() 不剥 U+0014(那是 clean_text)"),
    ("[表情]", "norm() 不改 [表情] 占位"),
    (".replace(", "norm() 不做任何替换"),
)


def check_norm_definition():
    """⑪ NORM(R6-47):norm() 的可抄函数体只在 06 §2.9.2 出现一次,且做的事逐字对得上规格。

    第八轮评审的唯一 P0:fingerprint 与 §2.12 出向合并到处写 norm(text),三册无一处函数体。
    PAIRED 只查「有没有 def」;这里查「def 里写的是不是规格说的那三件事」——半改(有 def 但顺手把
    U+0014 剥掉、或把 [表情] 还原)比没 def 更险:实现者照抄就会与 clean_text 分工打架。
    反向验证(2026-09-19):改前备份无 def ⇒ 红;把函数体里的 NFKC 行删掉 ⇒ 红;在函数体里加一行
    s = s.replace("\\u0014", "") ⇒ 红。
    """
    red = []
    print()
    print("=" * 78)
    print("⑪ NORM —— norm() 的唯一可抄定义(06 §2.9.2,R6-47)")
    print("=" * 78)
    defs = []
    for f in sorted(glob.glob(os.path.join(HERE, "0[0-7]-*.md"))):
        for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
            if re.search(r"^\s*def norm\(", ln):
                defs.append((os.path.basename(f)[:2], i))
    if len(defs) != 1 or defs[0][0] != "06":
        red.append("norm() 定义处数 != 1(应只在 06 §2.9.2)")
        print(f"  ❌ `def norm(` 出现 {len(defs)} 处 —— {defs or '无'};应恰在 06 §2.9.2 出现一次(其它册只引用)")
        return red
    p06 = next(iter(glob.glob(os.path.join(HERE, "06-*.md"))))
    t06 = io.open(p06, encoding="utf-8").read()
    m = re.search(NORM_DEF + r"(.*?)```", t06, re.S)
    if not m:
        red.append("norm() 签名与规格不符")
        print("  ❌ 06 有 `def norm(` 但签名不是 `def norm(s: str | None) -> str:`(评审给的原句)")
        return red
    body = m.group(1)
    ok = True
    for needle, what in NORM_BODY_MUST:
        if needle not in body:
            ok = False; red.append(f"norm() 函数体缺 {what}")
            print(f"  ❌ norm() 函数体缺:{what}(找不到 `{needle}`)")
    for needle, what in NORM_BODY_MUST_NOT:
        if needle in body:
            ok = False; red.append(f"norm() 函数体多做了:{what}")
            print(f"  ❌ norm() 函数体不该有 `{needle}` —— {what}")
    if ok:
        print(f"  ✅ 06:{defs[0][1]}  `def norm(s: str | None) -> str:` 唯一;函数体 = NFKC + 折叠空白 + strip,不碰 U+0014/[表情]")
    return red


def main():
    red = []
    red += check_copyable()
    red += check_forbidden()
    red += check_paired()
    red += check_enums()
    red += check_status_mirror()
    red += check_keynames()
    red += check_mirrors()
    red += check_dynamic_sets()
    red += check_qidian_confirm_window()
    red += check_norm_definition()
    red += check_versions()
    print()
    print("=" * 78)
    if red:
        print(f"总判定:❌ {len(red)} 项未过 —— {', '.join(red)}")
        print("=" * 78)
        return 1
    print("总判定:✅ 真值表全部一致")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
