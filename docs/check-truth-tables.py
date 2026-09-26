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

    # ---- R6-58(第五批接线)新增两条。两条都在 docs-before-R6-58 备份上实测能红。----
    # (x):#25/#71 统一走 00 §11.21 [JOB] 的 `202 {job_id}`;#25 原写的 `202 {run_id}` 作废
    #      (`run_id` 是 workflow_runs 的主键名,两个概念共用一个键名 ⇒ 控制台拿 run_id 去查 /jobs/{job_id} 恒 404)。
    # ⚠️ 刻意只盯【同一行里同时出现 calibrate 与 run_id】——#78 selftest 的 `202 {run_id}` 是合法的,
    #    blanket 规则会把它误报,而「误报比漏报更伤」。
    ("自校准端点仍写 run_id(应为 job_id)",
     "0[1-7]-*.md",
     # ⚠️ 首跑校准:窗口原为 140 字,把 01 §C-40 端点名清单里「…/calibrate 保留;…GET /workflows/runs/{run_id}」
     #    这种【两个不相干端点排在同一行】误报了一处。收窄到「calibrate 近旁 60 字内的 `202 {run_id}` 字面」——
     #    盯的是那一格返回体,不是「这行提过 calibrate 也提过 run_id」。
     r"calibrate[^\n]{0,60}202 \{run_id\}|202 \{run_id\}[^\n]{0,60}calibrate",
     "R6-58 (x):#25/#71 统一 `202 {job_id}`(00 §11.21 [JOB] 优先于 02 §3.4.6);`run_id` 是 workflow_runs 的主键名,不得共用。"),

    # (v):QQ 游标 kind 迁移 —— owner 列已带账号,再带一次 {account_id}: 前缀会把同一会话写成两种 kind。
    #     迁移一个 token 到处改,必须在同一次给脚本加旧 token 的 FORBIDDEN(R6-37 教训:否则那个维度是脚本级假绿)。
    ("QQ 游标旧写法 onebot_seq:<session>",
     "0[1-7]-*.md",
     r"onebot_seq:<session>",
     "R6-58 (v):逐字为 `onebot_seq:<native_id>`(与企点 `qidian_rowid:<native_id>` 同惯例);旧写法会造出两种 kind。"),

    # (de):实测采样的出参键 `candidates` 作废(改 `rows`,元素 = probe_targets_observed 的行 + in_config),
    #      且 sample 不产生 probe_result ⇒ 没有 run_id。⚠️ 刻意盯【旧形状的字面】而不是裸词 `candidates` ——
    #      `docker_pool_candidates` / `mail_cleanup_log.candidates` / 06 伪代码的局部变量都叫 candidates,
    #      盯裸词会一次误报 6 处。反向验证:改前备份 04:587 命中 1 处。
    ("实测采样出参旧形状 candidates:[{channel",
     "0[1-7]-*.md",
     r"candidates:\s*\[\s*\{\s*channel",
     "R6-58 (de):sample 出参逐字 `{sampled_at, duration_s, rows, skipped}`,`rows` 的元素 = 02 §3.2 行 + `in_config`;`candidates` 与 `run_id` 均作废。"),

    # ⚠️ 这里曾加过一条「`PUT /settings/probe {qidian_hosts` 旧入参」的 FORBIDDEN,**首跑在改前备份上不红 ⇒ 是空规则,已删**:
    #    04 §2.8.4 那一行里带着「**替换**整表不追加」,而「替换」在行级 NEGATION 词表里 ⇒ 整行被跳过,这条永不红。
    #    (这是 R6-51 ⑫ 同一个坑的第二次出现。)该维度改由 PAIRED「#76b 入参 observed_ids:02 → 01」与
    #    FORBIDDEN「实测采样出参旧形状」两条覆盖,两条都实测能红。
]

# R6-51(首批验收发现):「入向撞到自己发的」合并窗在 06 §2.12 表里是配置项 out_merge_window_s,同节下文与 02 §2.8.1 却写死 60s;
# 配置一改两处分叉。⚠️ 这条**不能**放进 FORBIDDEN:那两句必含「不插第二行」之类否定词,行级 NEGATION 整行跳过 ⇒ 永不红
# (2026-09-19 首跑实测又抓到一条空规则),故单列成 ⑫ 不走 NEGATION。反向验证:R8 改前备份上 02/06 各 1 处 ⇒ 红。
MERGE_WINDOW_LITERAL = r"\|ts 差\| ≤ 60s"


def check_merge_window_literal():
    red = []
    print()
    print("=" * 78)
    print("⑫ LITERAL —— 出向合并窗不得写死「|ts 差| ≤ 60s」(唯一出处 02 §7.1 [bus] out_merge_window_s,R6-51)")
    print("=" * 78)
    hits = []
    for f in sorted(glob.glob(os.path.join(HERE, "0[1-7]-*.md"))):
        for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
            if re.search(MERGE_WINDOW_LITERAL, ln):
                hits.append((os.path.basename(f)[:2], i, ln.strip()[:110]))
    if hits:
        red.append("出向合并窗写死 60s")
        print(f"  ❌ 字面「|ts 差| ≤ 60s」仍在 —— {len(hits)} 处(改配置后与 out_merge_window_s 分叉)")
        for vol, i, ln in hits[:6]:
            print(f"       {vol}:{i}  {ln}")
    else:
        print("  ✅ 现行册无字面 60s 合并窗,全部引用 [bus] out_merge_window_s")
    return red

# ---------------------------------------------------------------- LITERAL(不走行级 NEGATION)
# 为什么要这一族:FORBIDDEN 走「整行出现任一否定词就跳过」,而本套文档里「不存在 / 替换 / 作废」这类词
# 在**可抄行与验收行**里极常见 —— 04 §8b A1-21 那一行就带着「`tcpdump` 进程在窗口外不存在」,
# 放进 FORBIDDEN 会被整行吞掉、变成空规则(R6-51 ⑫ 与本轮删掉的那条「qidian_hosts 旧入参」都是这个坑)。
# 故:**正则必须足够窄、窄到不可能出现在正确写法里**,然后绕开 NEGATION 直接判。
LITERALS = [
    # 终审第一轮 MAJOR 3:01 §3 端点摘要表漏改(裁决只点了 §2.7.9/§4/§9 三处)。
    # 窄:只盯 `settings/probe {targets`,正确写法是 `{observed_ids`,不可能误命中。
    ("#76b 旧入参 settings/probe {targets",
     "0[1-7]-*.md",
     r"settings/probe\s*\{\s*targets",
     "R6-58 (z)/(db):入参逐字 `{observed_ids:[行 id…]}`(采纳后的全集,`[]`=清空);照旧写法一调就 400 use_observed_ids。"),

    # 终审第一轮 MAJOR 5:04 §8b A1-21 写 `candidates[]` + 一整套 02 §3.2 DDL 里不存在的列名。
    # 上一轮的 FORBIDDEN 只盯初稿那一种形态 `candidates:[{channel`,打不中 `candidates[]` —— 规则不空,但过窄。
    # 现按终审给的验收标准放宽为两种形态,并用「同行须含 实测采样|sample」把 docker_pool_candidates /
    # mail_cleanup_log.candidates / 06 伪代码局部变量 candidates 三类正当用法排除掉。
    ("实测采样出参旧键 candidates(应为 rows)",
     "0[1-7]-*.md",
     # 🔴 终审第二轮 N2:原写法 `(?=.*…)` 的先行断言是从 `candidates` **出现的位置**往后看,
     #    要求 `candidates` 之后还有「实测采样|sample」—— 旧句能红纯属它后半句恰好带一个 `samples`。
     #    最可能的回潮形态(改用 DDL 新列名、句尾不再出现 sample 字样)在盲区里,副本实测 exit=0。
     #    改为**锚到行首**:整行任意位置含「实测采样|sample」即可,同型变体能红且现行文档不误报。
     r"^(?=.*(?:实测采样|sample)).*candidates(?:\[\]|:\s*\[)",
     "R6-58 (de):sample 出参逐字 `{sampled_at, duration_s, rows, skipped}`,行的字段名用 02 §3.2 DDL 的列;`candidates`/`run_id`/`resolved_by` 均作废。"),

    # ---- 终审第二轮 N4:DEAD_KEYNAMES 的 bind_* 在**行内含「→」**的说明行里被 _KEYNAME_RECORD_MARKS
    #      整行吞掉(副本实测 exit=0),而真实回潮几乎必然发生在那种说明行里。
    #      🔴 **不动共享的 NEGATION / _KEYNAME_RECORD_MARKS 逻辑**(R6-37:没把握前别动共享逻辑),
    #      改为在本族各补一条 —— 绕开行级否定吞噬,代价是正则必须窄到只命中
    #      「**作为现行键被赋值**」这一种形态(`键 = ` 的 TOML 赋值),并显式排除记录/作废语境。
    #      现行文档的三处残留都在排除面内:02 §7.2 与 07 §2 带 `~~` 删除线、04 §7 注释写「已作废」。
    ("winagent.toml [api] 作废键 bind_loopback 被当现行键赋值",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再)).*\bbind_loopback\s*=",
     "R6-58 (av):服务监听地址的键唯一落点 = 04 §7 `[net] listen_loopback`;02 §7.2 `[api]` 那一套已作废,写它等于静默失效。"),

    ("winagent.toml [api] 作废键 bind_wsl_adapter 被当现行键赋值",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再)).*\bbind_wsl_adapter\s*=",
     "R6-58 (av):同上 —— 应为 04 §7 `[net] listen_wsl_adapter`。"),

    # ---- R6-60 (b):机型档案库字段名统一 profile_key(02 §3.1 DDL 为准),05 三处旧名 template_key 作废。
    #      同样放 LITERAL2:05 §8b R4 那一行是验收断言行,极易带否定词而被 FORBIDDEN 整行吞掉。
    #      正则窄到「裸词 template_key」,再排除记录/作废语境(本轮改后的两处都写着「作废 / no such column」)。
    ("device_profiles 旧字段名 template_key(应为 profile_key)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|no such column)).*\btemplate_key\b",
     "R6-60 (b):库列名以 02 §3.1 DDL 的 `profile_key` 为准(实现 `schema_agent.sql` 与 `device_profiles.json` 亦然);照 `template_key` 写的断言 `no such column`。"),

    # ---- R6-61 ①:N1(SFX 空间判据)的回潮守卫。第三轮终审副本实测:把 03:119 还原成
    #      「不足 6 GB 直接退 26」→ 脚本 exit=0,这个维度**完全没有守卫**,而它已经回潮过两次
    #      ((dj) 半改 + 229 行的第三种说法连着两轮无人无脚本发现)。
    #      🔴 难点 = **不能误伤回退路径那句合法的「硬 6 GB 粗判」**,而它常与首选路径写在同一行(119/125/229)。
    #      故**不用行级排除**(整行含「回退」就跳过 = 119 行还原后照样绿 = 空规则),改用
    #      **命中片段内的 tempered token**:只管「阈值 → 处置」这一小段里不出现回退/作废语境。
    #      两个分支 = 旧单阈值的两种形态:①「不足/低于/< 6 GB … 退 26/直接退/即拒」;
    #      ②「解压前 … 6 GB 粗判」(把回退路径的阈值安到首选路径的时机上 = 229 行那第三种说法)。
    #      `(?<![0-9])` 挡住 `16 GB` 的子串误命中(§2.2.2 硬门槛「< 16 GB 即拒」就是这个形态)。
    ("SFX 空间判据写死单阈值 6 GB(应按首选/回退两条路径分写)",
     "0[1-7]-*.md",
     r"(?:不足|低于|少于|小于|<)\s*\**\s*(?<![0-9])6\s?G(?:B|iB)\**(?:(?!回退|官方存根|作废|只作追溯|max\()[^\n]){0,60}?(?:退\s*\**\s*(?:出码\s*)?`?\s*26|直接退|即拒)"
     r"|(?:解压前|SFX\s*解压)(?:(?!回退|官方存根|作废|原写|只作追溯)[^\n]){0,30}?(?<![0-9])6\s?G(?:B|iB)\**\s*粗判",
     "R6-58 (dj)/R6-61:SFX 阶段的空间判据有**两条路径** —— 首选(自编存根补丁 (2))= `max(6 GiB, 归档解包总大小 × 1.1)`、**解压动作之前**判;回退(官方存根 + `precheck-disk.cmd`)= 硬 6 GB、**引擎跑之前**判。写死单阈值「6 GB」= 首选路径的真门槛随载荷长、写死会漏判;把「6 GB」安到「解压前」= 两条真实路径都对不上。验收断言照旧写法写会恒绿也恒错(03 §8b M0 ① 踩过)。"),

    # ---- R6-61 ②:N3(workflow paused/cancelled 不是事件)的回潮守卫。第三轮终审副本实测:
    #      把 05:1066 还原成「…;工作流 `paused(reason=account_switching)`」写在事件序列句里 → exit=0。
    #      🔴 这一族专治**结构性缺口**:三轮下来 01:1517 / 04:1009 / 05:1065 三处同型漏网**全部出在 §8b 验收表**,
    #      而 §8b 的行几乎必然自带否定词 ⇒ 放 FORBIDDEN/DEAD_KEYNAMES 会被行级 NEGATION 整行吞掉。
    #      放 LITERAL2(不走 NEGATION)是让脚本「看见 §8b」的最小做法 —— 扫描范围本来就含 §8b,缺的是**形态对得上的规则**。
    #      排除面 = 现行正确写法必然带的澄清词(不推 / 不在上面 / #45 / 不是事件 / 恒红 / 拆出),
    #      回潮写法(把 paused 当事件序列的一项)不会带它们。
    #      🔴 **R6-62 (m) 收窄排除面:删掉「轮询」这一项**。第四轮终审副本实测:回潮写法只要同行
    #      出现「轮询」二字(而「前端轮询刷新列表」这类句子在 01/05 出现极自然)整行就被跳过 ⇒ exit=0。
    #      「轮询」太常见、不具备「这一句在澄清 paused 不推事件」的指示性;真正有指示性的是
    #      `不推` / `不在上面` / `#45` / `不是事件` 这几个 —— 现行六处正确写法各自至少带其中一个(已实测零误报)。
    ("workflow 的 paused/cancelled 被写进「事件序列」栏(它不推事件,只能轮询读)",
     "0[1-7]-*.md",
     r"^(?!.*(?:不推|不在上面|#45|不是事件|恒红|拆出)).*事件序列[^\n]*?`?(?:paused|cancelled)",
     "R6-58 (cw):`workflow` 的 `paused`/`cancelled` **不推事件**,由 `workflows` store 轮询 `#45 GET /workflows/runs/{run_id}` 读 `status`/`pause_reason`;写进事件序列 = 照字面断言恒红、P-FLOW 永远停在 running。"),

    # ---- R6-61 ③:DEAD_KEYNAMES 其余 6 个键的赋值形态旁路(与上面两条 `bind_*` 同款)。
    #      为什么要:⑦ KEYNAME 族走 NEGATION + _KEYNAME_RECORD_MARKS 双重整行跳过,
    #      而 `→` 在 _KEYNAME_RECORD_MARKS 里 ⇒ **真实回潮形态(带「→」的说明行、带「|」的表格行)会被整行吞掉**。
    #      🔴 **不动共享的 NEGATION / _KEYNAME_RECORD_MARKS**(R6-37:没把握前别动共享逻辑),
    #      在本族给每个键补一条只盯「**作为现行键被赋值**」(`键 = `)的窄规则。
    #      排除面比 `bind_*` 那两条多 `不是|vs|为准|键名|分歧|同设置|同一设置` —— 首跑实测这 6 个键在
    #      `07:118`(同设置两个键名的分歧汇总)与 `04:305`(「不是 `calibration_source='auto'`」括注)有**正当出现**,
    #      不排除就是 2 处误报(误报比漏报更伤)。代价同 `bind_*`:**只盯赋值形态,「提键名不赋值」仍在盲区**。
    ("废弃键名 holder_fault_takeover_s 被当现行键赋值(应为 slot_error_takeover_s)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再|不是|vs|为准|键名|分歧|同设置|同一设置)).*\bholder_fault_takeover_s\s*=",
     "02 §7.1 是配置键的家,05 是引用方;代码读哪个,另一个静默失效。"),

    ("废弃键名 dll_candidates 被当现行键赋值(应为 wxkey_dlls)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再|不是|vs|为准|键名|分歧|同设置|同一设置)).*\bdll_candidates\s*=",
     "以 02 §7.2 [wechat] wxkey_dlls 为准;代码读哪个,另一个静默失效。"),

    ("废弃键名 events_ws_hours 被当现行键赋值(应为 ws_retention_hours)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再|不是|vs|为准|键名|分歧|同设置|同一设置)).*\bevents_ws_hours\s*=",
     "R6-58 (c):events_outbox 两类行只有一把尺子 = [events] ws_retention_hours=72;代码读哪个,另一个静默失效。"),

    ("废弃键名 dead_after_attempts 被当现行键赋值(应为 webhook_max_attempts)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再|不是|vs|为准|键名|分歧|同设置|同一设置)).*\bdead_after_attempts\s*=",
     "R6-58 (d):死信阈值键名唯一;行值 webhooks.max_attempts 优先于全局值;代码读哪个,另一个静默失效。"),

    ("废弃键名 calibration_source 被当现行键赋值(应为 source)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再|不是|vs|为准|键名|分歧|同设置|同一设置)).*\bcalibration_source\s*=",
     "R6-58 (bk):resource_pools 的列名以 02 §3.1 DDL 为准(值域 default|winagent|manual|calibrated,没有 auto);代码读哪个,另一个静默失效。"),

    ("废弃键名 calibrated_at_ms 被当现行键赋值(应为 calibrated_ms)",
     "0[1-7]-*.md",
     r"^(?!.*(?:~~|作废|已改名|取代|旧名|不得再|不是|vs|为准|键名|分歧|同设置|同一设置)).*\bcalibrated_at_ms\s*=",
     "R6-58 (bk):同上,列名逐字以 02 §3.1 DDL 为准;代码读哪个,另一个静默失效。"),

    # ---- R6-61 ⑨:W4 登记的同一条裁决自带守卫(P3 的教训:**新改对的语义必须在同一次加规则**,
    #      否则下一轮回潮时既无人也无脚本发现 —— 终审 E1/E2 两个 exit=0 就是这么来的)。
    #      旧语义「`--init-db` 幂等 = 库已存在即跳过」照字面实现会**跳过迁移**,升级路径上静默留一个旧 schema,
    #      要到下次起 Agent 撞上 02 §2.1 第 4 步「迁移失败拒绝启动」才炸 —— 典型的「两边都能跑、只是行为不同」。
    #      🔴 **不用行级排除**:三处正当引述(`02:239`/`03:4`/`03:621`)的否定词都在命中片段之外,
    #      行级排除会把「半改」(换掉正确语义但保留同行那句 ⚠️ 作废注)整行吞掉 —— 副本实测过,那正是 exit=0。
    #      改判**右边界**:正当引述一律把旧语义放在 `「…」` 里(收尾是 `」`),`02:239` 那句的后文是「跳过**迁移**」;
    #      坏写法的收尾是 `)`/`、`/`|`。故 `跳过(?!」|迁移)` + 近邻窗口 `{0,3}` 即可,首跑零误报、四种回潮形态全红。
    # ---- 🔴 R6-62 (m) **放宽本条的左侧词形与近邻窗口**。第四轮终审副本实测两个盲区(均 exit=0):
    #      (M2a)「库已存在**的时候就直接**跳过」—— 近邻窗口 `{0,3}` 装不下中间那 6 个字;
    #      (M2b)「`agent.db` 已存在,跳过 `--init-db`」—— 🔴 **这正是首启脚本自己那行日志的措辞**,
    #            也是最可能被原样抄进文档的形态,而旧正则只认「库已存在 / 库文件已存在」两个词形。
    #      故:左侧改成「(库 | 库文件 | `agent.db` | 数据库[文件]) + 已?存在」,窗口放到 `{0,12}`。
    #      右边界维持 `跳过(?!」|迁移)` 不动 —— 正当引述一律把旧语义放进 `「…」`(收尾 `」`),
    #      `02` owner 段那句的后文是「跳过**迁移**」;放宽左侧不影响这两条出口,首跑仍零误报。
    ("--init-db 的幂等写成「库已存在即跳过」(照字面实现会跳过迁移)",
     "0[1-7]-*.md",
     r"(?:库|库文件|`?agent\.db`?|数据库(?:文件)?)\s*已?\s*存在[^\n]{0,12}?跳过(?!」|迁移)",
     "R6-61 (W4)/R6-62 (i):幂等的确切语义 = **已是最新 ⇒ 一条语句都不写、回 0;落后 ⇒ 跑迁移;损坏/版本过新 ⇒ 非零退出且不动该文件**(owner = 02 §2.1「Agent 命令行参数与 --init-db 退出码」,实现 `src/qtrade_agent/main.py` 逐字一致)。写成「见到库文件就 return 0」= 跳过迁移;**调用方在外面加一道同义的前置闸门、后果完全一样**(R6-62 (i))。"),

    # ---- R6-62 (d):幂等键的位置。联调实测前端把它放进请求头 ⇒ 服务端读不到、每条写请求必 400,
    #      而全 docs 本来就只有「body 的 `idempotency_key`」这一种写法(02 §3.4 幂等列)。
    #      🔴 迁移/定位一个名字,就要在同一次给脚本加「错误形态」的规则,否则这个维度是脚本级假绿(R6-37 的明文规矩)。
    #      本族不走 NEGATION ⇒ 排除面须自带:正当引述(01 §3 写「**没有**这个请求头」、裁决表引文)一律带否定词。
    # ---- 🔴 R6-62/Q4 轮(第五轮终审副本实测盲区 I-C,exit=0):只认字面量 `X-Idempotency-Key` ⇒
    #      **不带该字面量的中文描述**(把位置说成请求头、body 不带那个键)整条打不中。
    #      补两个近邻分支(窗口 16 字,两个方向各一)。误报面实测:全册「幂等 ↔ 请求头」共现只有 2 行,
    #      间距均 > 16 字且都带否定词 ⇒ 首跑仍零误报。
    ("幂等键被写成请求头 X-Idempotency-Key(实为 body 的 idempotency_key)",
     "0[1-7]-*.md",
     r"^(?!.*(?:没有|不是|作废|禁用|别写|勿写|不存在|不得|错误形态)).*"
     r"(?:X-Idempotency-Key|幂等键[^\n]{0,16}?(?:请求头|[Hh]eader)|(?:请求头|[Hh]eader)[^\n]{0,16}?幂等键)",
     "R6-62 (d):幂等键的唯一位置 = **请求 body 的 `idempotency_key`**(02 §3.4 幂等列;实现 `api/app.py` / `routes_ext.py` 读的都是 body)。写成请求头 ⇒ 服务端读不到,每条写类请求必回 `400 INVALID_ARGS(idempotency_key_required)`。"),

    # ---- R6-62 (h):R6-61 初稿新造的那个首启失败原因码的回潮守卫。
    #      🔴 这是「文档写了一个 owner / 实现里不存在的名字」这类缺陷的**第三次**(前两次:Message 的一个不存在的列、
    #      机型档案库的旧字段名),脚本对这一整类本来没有任何守卫 —— 它只比 docs 内部的 token,看不见实现。
    #      最小处置 = 把这个已作废的字面量做成 FORBIDDEN 式的窄规则(与 R6-37「迁移一个 token 必须同一次加规则」同型),
    #      现行文档里它只剩 00 §15g 两条裁决里的**追溯引文**,一律带作废/订正类词 ⇒ 行级排除即可零误报。
    #      真正的结构性解法(从 03 §2.3 现读原因码列、与 §5.1/§8b 的 FAILED:<步>:<码> 串互为子集)见终审转呈的开放项,须另起裁决。
    ("首启失败写成 R6-61 初稿新造的那个原因码(03 §2.3 / §3.4 / run-step.ps1 三处零落点)",
     "0[1-7]-*.md",
     r"^(?!.*(?:作废|初稿|订正|新造|零落点|只作追溯)).*AGENT_INIT_DB_FAILED",
     "R6-62 (h):首启 `--init-db` 失败在引擎侧**统一落 `AGENT_NOT_READY`**(退出码 75 `E_INSTALL_AGENT_NOT_READY`,03 §2.3/§3.4 与 `installer/engine/run-step.ps1` 三方同名);`--init-db` 自己的 3/4/5 只在 WSL 侧首启日志可见。写一个实现产不出的码 ⇒ 验收恒红,或逼实现者新增一个两张 owner 表都没有的码。"),

    # ---- R6-62 (a):WS 鉴权失败的握手时序。旧句「accept 前直接关闭 + 关闭码 4401」自相矛盾 ——
    #      握手没完成就没有关闭帧可发,ASGI 服务端会退化成「拒绝握手」,客户端只看得到 1006,
    #      控制台按关闭码分诊那条路**恒不触发**。这是典型的「照字面实现两边都能跑、只是行为不同」,
    #      且症状(前端无限重连)离病灶很远 ⇒ 必须在改对它的同一次加守卫。
    #      现行三处正当引述(02 §3.4.7 的作废注、00 §15g R6-62 的裁决引文)一律带自相矛盾/作废/1006 之一。
    # ---- 🔴 R6-62/Q4 轮(第五轮终审副本实测三个盲区,均 exit=0):
    #      (K-C)「accept **之**前就关闭」—— 旧正则只认「accept 前」,差一个「之」字就穿过去,
    #            而带「之」的写法在中文里更自然 ⇒ `前` 放宽成 `(?:之)?前`;
    #      (K-D/K-E) **反向词序**(先写关闭动作、再补一句「不做 accept」/「不 accept,直接 close」)——
    #            旧正则要求「accept…前…关闭」这一个词序,反过来写就整条打不中 ⇒ 补一个「(不|未|无需|跳过)(做|经|走|调用)? accept」分支。
    #      两条分支共用一个前置 lookahead「同行须出现 close( / 关闭 / 4401 / 4400」,把 `pickFile(accept)`、
    #      `accepted_versions` 这类与握手无关的 accept 挡在外面;行级排除面不动 ⇒ 首跑仍零误报。
    ("WS 鉴权失败写成「accept 前关闭」(客户端只会拿到 1006,4401 恒不触发)",
     "0[1-7]-*.md",
     r"^(?!.*(?:作废|自相矛盾|1006|订正|只作追溯))(?=.*(?:`?close`?\s*\(|关闭|4401|4400)).*"
     r"(?:`?accept`?\s*(?:\(\))?\s*(?:之)?前[^\n]{0,10}?关闭"
     r"|(?:不|未|无需|跳过)\s*(?:做|经|走|调用)?\s*`?accept`?)",
     "R6-62 (a):无令牌/令牌无效与非法首帧都必须**先 `accept()` 把握手做完,再 `close(code, reason)`**(02 §3.4.7 是 owner,01 §5.1 承接)。在 accept 之前关闭 = 拒绝握手,浏览器与 ws 客户端只看得到 `1006`,`4401`/`4400` 永远拿不到。"),

    # ---- R6-62 Ⅷ/Q6:承接方把 `#36` 并进「指令端点」闭集的回潮守卫。
    #      病灶不是笔误,是**承接方自创一个 owner 没有的编号闭集**:02 #36 的出参顶层是
    #      `{broadcast_id, results:{account_id: CommandResult}}`,顶层**不自带**指令 trace
    #      (实现 `api/routes_ext.py` 的 #36 全段不改 `request.state.trace_id`)⇒ 顶层那串就是请求级的那一串,
    #      与 #28/#29 **正相反**。照旧写法写的断言(`响应.trace_id !== 我发的 X-Trace-Id`)在 #36 上恒红,
    #      排障时还会让人放弃唯一查得到审计行的那一串。
    #      🔴 **不用行级排除**(副本实测:行级排除面写「请求级」会把还原旧写法那行整行吞掉 ⇒ exit=0,
    #      因为旧写法自己就有「比**请求级**的更准」这半句 —— 与 R6-61 ⑨ 踩过的是同一个坑:
    #      排除词只要在**坏写法里也会出现**,这条规则对真正要挡的那个形态就是空的)。
    #      改判**命中片段内**的 tempered token:从「指令端点」走到「#36」的这一小段里不出现澄清词。
    #      现行正确写法两处出口:①闭集写 `#28`/`#29` 后,隔了近 90 字才出现 `#36`(超窗口);
    #      ②`#36` 之后紧跟「不在此列」「不自带」(tempered 阻断)。两条分支 = 两种回潮形态:
    #      ①把 #36 列进「指令端点」的编号闭集(含顿号/「与」分隔);②直接说 #36 自带 trace。
    #      🔵 R6-63 ③(第七轮终审 M6 / §2.3 漏网变体)补两支,与 Q7 的「反向词序」对称:
    #      ③**反向词序**「#36 与 #28/#29 一样都算指令端点」(排除词加「不是」,放过「#36 不是指令端点」这种正确写法);
    #      ④**同义改写**「#36 回的 trace_id 是指令自己的那一串」(触发词不再写死「自带」;排除词加「请求级」,
    #      放过「#36 顶层 trace 是请求级那串」)。副本矩阵:9 个坏形态全红、7 条合法对照全绿、现行文档首跑零误报;
    #      把规则还原成上一版后,4 个新增坏形态里 ③④ 对应的三条**全绿**(= 真修复,不是换写法)。
    ("把 #36 广播并进「指令端点」闭集 / 说它自带 CommandResult 的 trace(实为请求级 trace)",
     "0[1-7]-*.md",
     r"(?:指令端点(?:(?!不在此列|不自带|各账号自己)[^\n]){0,40}?#36"
     r"|#36(?:(?!不在此列|不自带|各账号自己|不是)[^\n]){0,40}?指令端点"
     r"|#36(?:(?!不在此列|不自带|各账号自己)[^\n]){0,60}?(?:自带|指令自己|指令自带)(?:(?!非空)[^\n]){0,24}?trace"
     r"|#36(?:(?!不在此列|不自带|各账号自己|请求级)[^\n]){0,60}?trace[^\n]{0,20}?(?:是|=|即)[^\n]{0,4}?指令(?:自己|自带|那))",
     "R6-62 Ⅷ/Q6:「指令端点回的是 `CommandResult` 自带的 trace」这条闭集**只含 #28/#29**(实现 `api/app.py` 的 `_submit` 是全仓唯一把 `request.state.trace_id` 改写成 `cmd.trace_id` 的地方)。`#36 POST /broadcast/commands` 顶层不自带 trace,顶层那串 = 请求级(拿它查审计**查得到**),各账号自己的那串在 `results.<account_id>.trace_id` 里(键路径见 `api/serialize.py` 的 `result_view`)。"),

    # ---- R6-62 Ⅷ/Q7:`/system/health` 的审计例外被写窄成「只有免鉴权摘要不记」的回潮守卫。
    #      与 W1(trace_id 那处)**同型**:实现 `api/app.py` 的审计中间件排除条件是
    #      `path != f"{API_PREFIX}/system/health"` —— **按 path 整端点**,带令牌全量同样不记。
    #      写窄了的后果是有人据此判「实现漏记审计」并去补,而该路径被 04 H01(30 s 一轮)与控制台面板反复探,
    #      补上就把 `audit_log` 刷爆(检测很慢:要等表涨起来)。
    #      🔴 **不用行级排除**(副本实测:只改限定词、保留同行那句 🔵「按 path 整端点」注的**半改**,
    #      行级排除会把整行吞掉 ⇒ exit=0 —— 而「半改」正是 W1/Q7 这条裁决已经发生过一次的形态)。
    #      改判**命中片段内**的 tempered token:从「免鉴权」走到「不记」的这一小段里不出现放宽词。
    #      前置 lookahead 只做**正向**限定(同行须出现该路径或 #72),把别处的「免鉴权」句挡在外面(全册共 16 行)。
    #      🔴 **lookahead 必须锚到行首 `^`**:本族用的是 `re.search` 逐行匹配,不锚行首时断言是从
    #      「免鉴权」**出现的位置**往后看,而 `/system/health` 恰在它**前面** ⇒ 断言恒假、整条规则为空
    #      (副本实测 exit=0,与终审第二轮 N2 判过的是同一个坑)。
    #      🔴 **反向词序那一分支须带收窄词 `只有|仅`**:副本实测,只写「不记 … 免鉴权」会误伤
    #      **正确**写法「这个路径两种形态都不记审计,免鉴权摘要与带令牌全量一视同仁」——
    #      那里的放宽词落在「不记」**左边**,片段内 tempered 看不见它。坏写法必带收窄词
    #      (「不记的**只有** … 免鉴权那一支」),正确写法不会带 ⇒ 以它分界,首跑与误报对照均零误报。
    #      🔵 R6-63 ③(第七轮终审 M6 / §2.3 漏网变体)三处放宽:①两个近邻窗口 {0,28}→{0,60}(本套文档满篇括号注,
    #      一个括号就超 28 字);②触发词「免鉴权」加同义「不带令牌|无令牌」;③前置 lookahead 加「健康检查|探活」,
    #      接住不提路径、只说「健康检查的免鉴权摘要不记」的行。🔴 **不加裸词 `health`**:副本实测它会误伤
    #      「无令牌的探测请求不记 …(与 health 无关)」这类别处的「不记」,误报比漏报更伤。
    ("/system/health 的审计例外被写窄成「只有免鉴权摘要不记」(实为整路径两种形态都不记)",
     "0[1-7]-*.md",
     r"^(?=.*(?:/system/health|#72|健康检查|探活))[^\n]*?"
     r"(?:(?:免鉴权|不带令牌|无令牌)(?:(?!两种形态|整端点|整路径|都不记)[^\n]){0,60}?不记"
     r"|不记(?:的)?(?:只有|仅)(?:(?!两种形态|整端点|整路径)[^\n]){0,60}?(?:免鉴权|不带令牌|无令牌))",
     "R6-62 Ⅷ/Q7:审计的唯一例外 = **`/system/health` 这个路径**,免鉴权摘要与带令牌全量**两种形态都不记**(owner = 02 §2.2.1,与 §3.4 例外① 同一个路径、同一种写法)。按「只有免鉴权那支不记」去补实现 ⇒ 高频探活把 `audit_log` 刷爆(保留期 02 §2.8.4 `audit_days`);总控已裁决维持现状。"),
]


def check_literals():
    red = []
    print()
    print("=" * 78)
    print("⑬ LITERAL2 —— 窄字面旧写法(**不走行级 NEGATION**,专治「验收行自带否定词」那类空规则)")
    print("=" * 78)
    for title, pat_glob, pat, why in LITERALS:
        hits = []
        for f in sorted(glob.glob(os.path.join(HERE, pat_glob))):
            for i, ln in enumerate(lines_of(io.open(f, encoding="utf-8").read()), 1):
                if re.search(pat, ln):
                    hits.append((os.path.basename(f)[:2], i, ln.strip()[:110]))
        if hits:
            red.append(title)
            print(f"\n  ❌ {title}  —— {len(hits)} 处")
            print(f"     理由:{why}")
            for vol, i, ln in hits[:8]:
                print(f"       {vol}:{i}  {ln}")
        else:
            print(f"  ✅ {title}")
    return red


# ---------------------------------------------------------------- PAIRED
# (标题, 声明方文件, 声明方正则, 消费方文件, 消费方正则, 说明)
PAIRED = [
    # ---- R6-56(第四批代码口径)----
    # 05 §2.5.4 掉线那一刻的告警此前没有码名,02 §3.7(机器码单一来源)也没登记 ⇒ 05 产生 → 02 必须登记。
    # 反向验证:改前备份(git HEAD 的 02)无 `ACCOUNT_OFFLINE` ⇒ 红(2026-09-20 实测)。
    ("ACCOUNT_OFFLINE:05 §2.5.4 产生 → 02 §3.7 必须登记",
     "05-*.md", r"`ACCOUNT_OFFLINE`",
     "02-*.md", r"`ACCOUNT_OFFLINE` \| warn \| account:<id>",
     "R6-56:告警码枚举由 02 登记;只在产生方出现 = 下游渲染/订阅对着空气。"),
    # ---- R6-54(第三批代码口径)----
    # #2/#9 的 409 信封引用 error.alternatives,而形态此前三册从未定义 ⇒ 02 §2.2.5 必须给 kind 枚举。
    # 反向验证:改前备份(git HEAD 的 02)上 §2.2.5 无 `alternatives[].kind` ⇒ 红(2026-09-20 实测)。
    ("error.alternatives:02 §3.4.1 #2/#9 引用 → 02 §2.2.5 必须定 alternatives[].kind 形态",
     "02-*.md", r"error\.alternatives",
     "02-*.md", r"alternatives\[\]\.kind ∈ \{add_other_channel, stop_one, wechat_switch\}",
     "R6-54:控制台按 kind 渲染「改开 QQ / 停用一个」;没有形态 = 前后端各猜一套字段名。"),
    # 02 §7.1 新登记的 [runtime] accounts_dir 必须镜像到 07 配置总表(07 是全表镜像,漏一键即分叉)。
    # 反向验证:改前备份的 07 无 accounts_dir ⇒ 红。
    ("accounts_dir:02 §7.1 [runtime] 登记 → 07 配置总表必须镜像",
     "02-*.md", r"`accounts_dir` \| `\"/var/lib/qtrade/accounts\"`",
     "07-*.md", r"`accounts_dir=\"/var/lib/qtrade/accounts\"`",
     "R6-54:07 是配置项全表镜像;02 登记 07 不镜像 = 两表键集不一致。"),
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

    # ---- R6-58(第五批接线)新增四条。每条都用「抹掉消费方那一行的副本」实测能红(只在改前备份上跑不够:
    #      声明方当时也没有该词,只会出 ⚠️,那是空规则)。----
    # (an):02 §3.6 新增 #48 PUT /wa/v1/probes/adopt 是 #76b 的 WinAgent 半;04 §2.8.4 是「实测采样」的行为 owner,
    #       它若不点名这个端点,实现方读 04 会以为采纳动作在 Agent 侧自己做(那会与「写 winagent.db」背离)。
    ("#48 PUT /wa/v1/probes/adopt:02 §3.6 声明 → 04 §2.8.4 必须点名",
     "02-*.md", r"PUT /wa/v1/probes/adopt",
     "04-*.md", r"PUT /wa/v1/probes/adopt",
     "R6-58 (an):上游缺这个端点时 Agent 只能回 503;只在 02 登记而 04 不提,采纳动作会被实现成 Agent 侧另存一份。"),

    # (ca):06 §2.4.2 回执的「送达状态」栏逐字取 00 §8.3 的结果码;CONFIRM_REQUIRED 此前只是 mail_inbox.status 的值。
    ("结果码 CONFIRM_REQUIRED:06 回执使用 → 00 §8.3 必须登记",
     "06-*.md", r"送达状态：CONFIRM_REQUIRED",
     "00-*.md", r"\| \*\*`CONFIRM_REQUIRED`\*\* \|",
     "R6-58 (ca):回执栏的取值域 = 00 §8.3;不登记则实现方要么编一个码、要么误写成 FORBIDDEN(语义是「等人批」不是「被拒」)。"),

    # (ab):02 §3.4.6 补登的 #79b(不带 run_id 取最近一轮)是给 01 的 P-ENV 自检块用的;01 不承接 = 页面直开仍是空白。
    ("#79b GET /system/selftest(最近一轮):02 登记 → 01 必须承接",
     "02-*.md", r"79b",
     "01-*.md", r"79b",
     "R6-58 (ab):01 §2.7.9 要「页面直开就显示上次 09:30 ✔」;只在 02 登记而 01 仍只写 POST,前端不会去调它。"),

    # (z):#76b 的入参键名在 01 与 02 曾各写一套(targets vs observed_ids)——前端按 01 写完会在真机撞 400。
    ("#76b 入参 observed_ids:02 定名 → 01 必须回改",
     "02-*.md", r"observed_ids",
     "01-*.md", r"observed_ids",
     "R6-58 (z):端点 owner 是 02;01 仍写 {targets} 则前端一调就 400 use_observed_ids。"),

    # (db):#76b 出参新增的通道分组键;04 §2.8.4 是「实测采样」的行为 owner,它若不提这个键,
    #       面板就会退回去按通道各发一次入参(那正是 (z)/(db) 刚消掉的旧形态)。
    #       反向验证:把 04 里的该词抹掉的副本上 ⇒ 红(2026-09-20 实测)。
    ("#76b 出参 hosts_by_channel:02 定形 → 04 §2.8.4 必须承接",
     "02-*.md", r"hosts_by_channel",
     "04-*.md", r"hosts_by_channel",
     "R6-58 (db):通道维度只出现在出参的 hosts_by_channel;04 不提 = 面板按旧的 *_hosts 入参写回去。"),

    # (cz):落库与出 JSON 两处共用 models.json_safe();02 §2.2.2 声明 → §3.10 的 screenshot 行必须给出 result_schema,
    #       否则「图片体怎么回流」在目录里是空白,实现方又会把裸 bytes 塞进 data 直接 dumps。
    # 终审第一轮 CRITICAL 1:(cw) 只写了产生方 00 §7.5,01 三处仍靠事件 ⇒ 零承接。
    #   这一类「裁决说『不推事件』」最凶:后端不发、前端等,**测试不红、日志不报**,只有真机跑到才暴露。
    #   负向验证:把 01 里的承接句抹掉的副本上 ⇒ 红(2026-09-20 实测)。
    ("(cw) paused/cancelled 不推事件:00 §7.5 声明 → 01 必须有轮询 #45 的承接句",
     "00-*.md", r"R6-58 \(cw\)",
     "01-*.md", r"轮询 `?#45",
     "R6-58 (cw):01 §2.5/§2.8/§2.7.6/§8b M5-1 四处都得改成轮询承接;只改 00 = 运行中切微信时 P-FLOW 永远停在 running。"),

    ("screenshot 的 result_schema:02 §2.2.2 定序列化出口 → §3.10 必须给形状",
     "02-*.md", r"models\.json_safe\(\)",
     "02-*.md", r"`result_schema` = `\{png_b64, width, height\}`",
     "R6-58 (cz):只写「用 json_safe」而不给 screenshot 的 result_schema,图片怎么回流仍是空白。"),
]

# ---------------------------------------------------------------- KEYNAME
# 「同一设置、同一文件、两个键名」—— 比同名不同物更隐蔽,因为【grep 都对不上】。
# 已出现 3 次:dpapi_scope/scope、wxkey_dlls/dll_candidates、slot_error_takeover_s/holder_fault_takeover_s。
# 后果:代码读哪个,另一个就静默失效(不报错、不崩溃,只是配置不生效)。
# 规则很简单:废弃的那个拼写,除「勘误/作废」语境外不该再出现。
DEAD_KEYNAMES = [
    ("holder_fault_takeover_s", "slot_error_takeover_s", "02 §7.1 是配置键的家,05 是引用方"),
    ("dll_candidates",          "wxkey_dlls",            "以 02 配置总表为准"),
    # ---- R6-58(第五批接线)新增四条。每条都在 docs-before-R6-58 备份上实测能红。----
    # (c):events_outbox 同一张表同一批行曾有两个键两个值(24 vs 72),清理器按哪个写都「有据可依」。
    ("events_ws_hours",         "ws_retention_hours",    "R6-58 (c):events_outbox 两类行只有一把尺子 = [events] ws_retention_hours=72"),
    # (d):§2.2.7 用 dead_after_attempts、§7.1 登记的是 webhook_max_attempts —— grep 都对不上。
    ("dead_after_attempts",     "webhook_max_attempts",  "R6-58 (d):死信阈值键名唯一;行值 webhooks.max_attempts 优先于全局值"),
    # (bk):04 写 calibration_source/calibrated_at_ms,02 §3.1 的真列是 source/calibrated_ms(且 CHECK 无 'auto')。
    ("calibration_source",      "source",                "R6-58 (bk):resource_pools 的列名以 02 §3.1 DDL 为准(值域 default|winagent|manual|calibrated)"),
    ("calibrated_at_ms",        "calibrated_ms",         "R6-58 (bk):同上,列名逐字以 02 §3.1 DDL 为准"),
    # (av):02 §7.2 [api] bind_loopback/bind_wsl_adapter 与 04 §7 [net] listen_* 是同一件事的两套键。
    ("bind_wsl_adapter",        "listen_wsl_adapter",    "R6-58 (av):服务监听地址的键收敛到 04 §7 [net];02 §7.2 [api] 那一套作废"),
    # 终审第一轮补:(av) 一次作废了【两个】键,上一轮只收了 bind_wsl_adapter,bind_loopback 回潮抓不到。
    ("bind_loopback",           "listen_loopback",       "R6-58 (av):同上 —— 改名类裁决必须把旧名【逐个】进表,漏一个就漏一条回潮路径"),
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
        # R6-58:各册头部的「变更记录」逐轮累积,冻结句会被往下挤;窗口由 6 行放宽到 12 行。
        # 只是存在性检查,放宽不会引入误报(冻结句只在头部出现)。
        head = "\n".join(lines_of(io.open(f, encoding="utf-8").read())[:12])
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


def check_result_codes_to_01():
    """终审第一轮 CRITICAL 2:00 §8.3 结果码表是**结果码的唯一来源**,01 §2.10 是它的中文对照表;
    本轮新登记 `CONFIRM_REQUIRED` 而 01 没跟(连一直漏着的 `CONFIRM_EXPIRED` 一起补上了)。

    🔴 **值集从 00 §8.3 现读**(不写死清单)—— 写死的清单管不了「owner 新增」,而这正是出事的那一类
    (与 ⑨ DYNAMIC 同一个道理;R6-33 的教训)。
    ⚠️ 只切 §8.3 那一段再取值:00 里还有 §8.1 state_code / §8.2 安装态 / §8.5 环境矩阵三张表,
    全文扫会把它们一起算进来、一次误报几十个 —— 误报比漏报更伤。
    """
    red = []
    print()
    print("=" * 78)
    print("⑭ DYNAMIC2 —— 00 §8.3 结果码全集(现读)→ 01 §2.10 中文对照表必须逐个收录")
    print("=" * 78)
    src00 = next(iter(glob.glob(os.path.join(HERE, "00-*.md"))), None)
    src01 = next(iter(glob.glob(os.path.join(HERE, "01-*.md"))), None)
    if not src00 or not src01:
        print("  ⚠️  文件缺失,跳过")
        return red
    t00 = io.open(src00, encoding="utf-8").read()
    i = t00.find("### 8.3")
    j = t00.find("### 8.4", i + 1)
    if i < 0 or j < 0:
        red.append("00 §8.3 切不出来")
        print("  ❌ 00 §8.3 段落切不出来(标题变了?)——规则失效,必须修")
        return red
    codes = set()
    for ln in lines_of(t00[i:j]):
        if not ln.startswith("|") or ln.startswith("|---") or "`code`" in ln:
            continue
        first = ln.split("|")[1]                      # 只取第一格(码名格)
        codes.update(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", first))
    if not codes:
        red.append("00 §8.3 取不到结果码")
        print("  ❌ 00 §8.3 一个码都没取到(表格形态变了?)——规则失效,必须修")
        return red
    t01_full = io.open(src01, encoding="utf-8").read()
    # 🔴 终审第二轮 W2 加固:原先只查「码是否出现在 01 **全文**」—— 变更记录 / §9 提及同一个码
    #    就能让「§2.10 表行被删」静默放过(CONFIRM_EXPIRED 已同时出现在 01:6 / 01:635,实测 exit=0)。
    #    改为**只切 01 §2.10 那一段**再查;切不出来即判红(规则失效必须被发现,不能静默变空)。
    m = re.search(r"^#{2,4}\s*2\.10\b", t01_full, re.M)
    if not m:
        red.append("01 §2.10 切不出来")
        print("  ❌ 01 §2.10 段落切不出来(标题变了?)——规则失效,必须修")
        return red
    nxt = re.search(r"^#{1,3}\s", t01_full[m.end():], re.M)
    t01 = t01_full[m.start(): m.end() + nxt.start()] if nxt else t01_full[m.start():]
    missing = sorted(c for c in codes if c not in t01)
    if missing:
        red.append(f"01 缺 {len(missing)} 个结果码")
        print(f"\n  ❌ 01 缺 {len(missing)}/{len(codes)} 个 —— {', '.join(missing)}")
        print("     理由:01 §2.10 明令「不能靠解析中文」;码表缺一个,控制台拿到它就渲染不出引导"
              "(与 WINAGENT_USER_OFFLINE 那次事故同型)。")
        print("     ⚠️ 本检查只看 01 §2.10 **那一段**(R6-60):别处(变更记录/§9)提到该码不算收录。")
    else:
        print(f"  ✅ 01 收录完整({len(codes)}/{len(codes)}) —— {', '.join(sorted(codes))}")
    return red


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


# ---------------------------------------------------------------- ⑮ CROSS(R6-69~R6-77,2026-09-26)
# 安琳 2026-09-26 晚九项裁决的守卫。R6-37 的明文规矩:**改对一个语义,同一次就给脚本加规则**,
# 否则下一轮回潮时既无人也无脚本发现。本族与 ⑩ VALUE 同型(按值判、不走行级 NEGATION),
# 其中 ① ⑥ 两条**跨出 docs/** 读实物(installer 的退出码模块、Agent 的 config.py)——
# 03 §3.4 是退出码唯一出处、02 §7.1 是配置键唯一出处,但「文档写了、实现里不存在 / 值不同」
# 这一类缺陷只比 docs 内部 token 是看不见的(R6-62 (h) 已点名过第三次)。
# ⚠️ 保守设计同 ⑤ MIRROR:实物文件缺失 / 实物里还没这个名字 ⇒ 只报 ⚠️ 不判红(实现方在途时文档先行是常态);
#    **两边都有而值不同** 才判红 —— 那才是真分叉。
# 每条都在 docs-before-R6-69 备份上实测能红(见 .omc/handoffs/docs-rulings-2026-09-26.md)。
ROOT = os.path.dirname(HERE)
EXIT_PSM1 = os.path.join(ROOT, "installer", "engine", "modules", "QTrade.Exit.psm1")
AGENT_CONFIG_PY = os.path.join(ROOT, "src", "qtrade_agent", "config.py")


def _load_glob(pat):
    p = next(iter(sorted(glob.glob(os.path.join(HERE, pat)))), None)
    return io.open(p, encoding="utf-8").read() if p else None


def _slice_between(text, start_pat, end_pat):
    """从 start_pat 首次命中处切到其后 end_pat 首次命中处;start 找不到回 None。"""
    m = re.search(start_pat, text, re.M)
    if not m:
        return None
    rest = text[m.start():]
    nl = rest.find("\n")
    if nl < 0:
        return rest
    e = re.search(end_pat, rest[nl + 1:], re.M)       # 从起始行的下一行起找终点,别让起始行自己命中
    return rest if not e else rest[: nl + 1 + e.start()]


def _parse_03_exit_table(t03):
    """03 §3.4「退出码」表 → {名: 码}。解析法与 installer/tests/test_spec_consistency.py 同源(按「码 + 反引号名」逐组抓)。"""
    block = _slice_between(t03, r"^\*\*退出码\*\*", r"^---\s*$|^#{2,4} ")
    codes = {}
    if block is None:
        return codes
    for ln in block.splitlines():
        if not ln.lstrip().startswith("|"):
            continue
        for code, name in re.findall(r"(\d+)\s*\|?\s*`(E_INSTALL_[A-Z0-9_]+)`", ln):
            codes[name] = int(code)
    return codes


def _endpoint_level(t02, num, path_pat):
    """02 端点表里 `| num | `path…` | 级 |` 行的「级」列原文;找不到回 None。"""
    # 路径列在反引号里、可能自带 `|`(#34 的 `?profile=thumb|focus|…`)⇒ 先吃完整对反引号再数列
    m = re.search(r"^\|\s*" + re.escape(num) + r"\s*\|\s*`" + path_pat + r"[^`\n]*`[^|\n]*\|\s*([^|\n]+)\|", t02, re.M)
    return m.group(1).strip() if m else None


def check_rulings_0926():
    red, warn = [], 0
    print()
    print("=" * 78)
    print("⑮ CROSS —— R6-69~R6-77 守卫(跨册同现 / 跨出 docs 读实物比值)")
    print("=" * 78)
    t00 = _load_glob("00-*.md") or ""
    t01 = _load_glob("01-*.md") or ""
    t02 = _load_glob("02-*.md") or ""
    t03 = _load_glob("03-*.md") or ""
    t07 = _load_glob("07-*.md") or ""

    # ① R6-73:退出码 11 E_INSTALL_CANCELLED 在 03 §3.4;03 表与 QTrade.Exit.psm1 逐名同值
    spec = _parse_03_exit_table(t03)
    if spec.get("E_INSTALL_CANCELLED") != 11:
        red.append("R6-73 退出码 11")
        print(f"\n  ❌ R6-73:03 §3.4 退出码表须有 `11 E_INSTALL_CANCELLED`(现读到 {spec.get('E_INSTALL_CANCELLED')!r})")
        print("     理由:用户取消与「停车等用户(10)」是两件事;03 §3.4 是唯一出处,Exit.psm1 / .iss / 验收手册都从它抄。")
    else:
        print("  ✅ R6-73:03 §3.4 有 `11 E_INSTALL_CANCELLED`")
    if not os.path.isfile(EXIT_PSM1):
        warn += 1
        print(f"  ⚠️  R6-73:找不到 {os.path.relpath(EXIT_PSM1, ROOT)},跨文件比值跳过")
    else:
        impl = {k: int(v) for k, v in re.findall(r"^\s*'(OK|E_INSTALL_[A-Z0-9_]+)'\s*=\s*(\d+)",
                                                    io.open(EXIT_PSM1, encoding="utf-8-sig").read(), re.M)}
        diff = [f"{k}: 03={spec[k]} / psm1={impl[k]}" for k in sorted(set(spec) & set(impl)) if spec[k] != impl[k]]
        if diff:
            red.append("03 §3.4 ↔ Exit.psm1 退出码值")
            print("\n  ❌ 03 §3.4 ↔ QTrade.Exit.psm1 同名退出码**值不同**(以 03 为准):")
            for x in diff[:8]:
                print(f"       {x}")
        else:
            print(f"  ✅ 03 §3.4 ↔ QTrade.Exit.psm1:{len(set(spec) & set(impl))} 个同名退出码值一致")
        only_doc = sorted(set(spec) - set(impl))
        if only_doc:
            warn += 1
            print(f"  ⚠️  03 有、Exit.psm1 还没有:{', '.join(only_doc)}(实现方在途时属正常;合流后应消失)")

    # ② R6-74 / R6-72:#34 关闭码 4408(客户端接收超时)与 4403(无权限,令牌级别不足)在 02 §3.4.7 与 01 §5.1 同现;
    #    且 02 §3.4.7 不得再有「没有 `4403`」一句(总控 2026-09-26 补充:该句已删,回潮 = 两句自相矛盾)。
    s02 = _slice_between(t02, r"^#### 3\.4\.7 ", r"^#{2,4} ") or ""
    s01 = _slice_between(t01, r"^### 5\.1 ", r"^#{2,4} ") or ""
    for code, rid in (("4408", "R6-74"), ("4403", "R6-72")):
        # 只认**登记形态**(加粗的码 `**`4408`**…`):节首指针句「再补 `4403`/`4408`」这种顺带提及不算登记,
        # 否则删掉真正的登记行、留着指针句照样绿(反向验证首跑实测抓到的空规则)。
        miss = [n for n, s in (("02 §3.4.7", s02), ("01 §5.1", s01)) if f"**`{code}`" not in s]
        if miss:
            red.append(f"{rid} 关闭码 {code}")
            print(f"\n  ❌ {rid}:#34 关闭码 `{code}` 须在 02 §3.4.7 与 01 §5.1 同现 —— 缺:{', '.join(miss)}")
            print("     理由:服务端发了一个客户端码表里没有的码 ⇒ 控制台只能当未知错误,不知道该不该重试。")
        else:
            print(f"  ✅ {rid}:`{code}` 在 02 §3.4.7 与 01 §5.1 同现")
    # ②b R6-72 承接(总控 2026-09-26 补充):01 §5.1 登记了 4403 ⇒ §4 P-SCREEN 元素表必须有只读模式两元素
    #     (否则分诊句说「挂横幅 / 点按钮重连」,元素表里却没有 testid ⇒ 验收脚本无从断言)。
    s4 = _slice_between(t01, r"^## 4\. ", r"^## 5\. ") or ""
    if "**`4403`**" in s01:
        # 只认**表行首列**(别的行里顺带引用一句「与 `qt-screen-readonly-banner` 同时显示」不算有这个元素)
        lack = [e for e in ("qt-screen-readonly-banner", "qt-screen-readonly-reconnect")
                if not re.search(r"^\|\s*`" + re.escape(e) + r"`\s*\|", s4, re.M)]
        if lack:
            red.append("R6-72 只读模式元素")
            print(f"\n  ❌ R6-72:01 §5.1 有 `4403` 分诊,但 §4 元素表缺:{', '.join(lack)}")
        else:
            print("  ✅ R6-72:01 §4 有只读模式两元素(承接 `4403`)")
    if re.search(r"没有\s*\**`4403`", s02):
        red.append("R6-72 §3.4.7 残留「没有 4403」")
        print("\n  ❌ R6-72:02 §3.4.7 仍写「没有 `4403`」—— 与同节登记的 #34 `4403` 自相矛盾")
    else:
        print("  ✅ R6-72:02 §3.4.7 无「没有 `4403`」残句")

    # ③ R6-72:#34 级别列含 W(注入)且与 #35 一致
    lv34 = _endpoint_level(t02, "34", r"GET /accounts/\{id\}/stream")
    lv35 = _endpoint_level(t02, "35", r"POST /accounts/\{id\}/stream/input")
    if lv34 is None or lv35 is None:
        red.append("R6-72 #34/#35 行")
        print(f"\n  ❌ R6-72:02 端点表找不到 #34 或 #35 行(#34={lv34!r} #35={lv35!r})—— 措辞变了请同步本规则")
    elif not re.search(r"\bW\b", lv34) or lv35 != "W":
        red.append("R6-72 #34 注入级别")
        print(f"\n  ❌ R6-72:#34 级别列须标注入类为 W、且 #35 为 W —— 现 #34={lv34!r} / #35={lv35!r}")
        print("     理由:#34 整条 R ⇒ 只读令牌开 WS 就能点屏幕、打字,绕过 #35 的 W 级。")
    else:
        print(f"  ✅ R6-72:#34 级别列 {lv34!r} 含 W,#35 = W")

    # ④ R6-77 ②:01 不再有画面工具条的 rotate testid
    hits = [i for i, ln in enumerate(lines_of(t01), 1)
            if re.search(r"qt-screen-tool-(?:rotate\b|\{[^}]*\brotate\b)", ln)]
    if hits:
        red.append("R6-77 rotate testid")
        print(f"\n  ❌ R6-77:01 仍列画面工具条的 rotate testid —— 行 {hits[:8]}")
        print("     理由:控制台没有这个按钮;元素表是 testid 唯一出处,列着 = 验收脚本去点一个不存在的元素。")
    else:
        print("  ✅ R6-77:01 无画面工具条 rotate testid")

    # ⑤ R6-71:00 的 R6-58 (ai) 标「被 R6-71 覆盖」;02 #101 行承接 R6-71
    # 只在 R6-58 那一行里找(00 头部增补行也写着「(ai) 就地标『已被 R6-71 覆盖』」,全文搜会被它顺带满足)
    row58 = re.search(r"^\| \*\*R6-58\*\* \|[^\n]*", t00, re.M)
    if not row58 or not re.search(r"\*\*\(ai\)\*\*[^;]{0,40}被 R6-71 覆盖", row58.group(0)):
        red.append("R6-58 (ai) 覆盖标注")
        print("\n  ❌ R6-71:00 §15g R6-58 **(ai)** 须就地标「已被 R6-71 覆盖」")
        print("     理由:(ai)「stream_restarted 恒 false」照字面实现 = 有人在看也不真重建。")
    else:
        print("  ✅ R6-71:R6-58 (ai) 已标被 R6-71 覆盖")
    row101 = re.search(r"^\|\s*101\s*\|[^\n]*", t02, re.M)
    if not row101 or "R6-71" not in row101.group(0) or "`stream_restarted:true`" not in row101.group(0):
        red.append("R6-71 #101 行")
        print("\n  ❌ R6-71:02 #101 行未承接 R6-71(有画面流在跑 ⇒ 真重建、stream_restarted:true)")
    else:
        print("  ✅ R6-71:02 #101 行已承接")

    # ⑥ R6-76:config.py 的 [adapters.qidian] scrcpy_* 键逐个登记在 02 §7.1 与 07
    if not os.path.isfile(AGENT_CONFIG_PY):
        warn += 1
        print(f"  ⚠️  R6-76:找不到 {os.path.relpath(AGENT_CONFIG_PY, ROOT)},跳过")
    else:
        src = io.open(AGENT_CONFIG_PY, encoding="utf-8").read()
        cls = _slice_between(src, r"^class QidianAdapterConfig\b", r"^(?:class |@dataclass)") or ""
        keys = re.findall(r"^\s+(scrcpy_[a-z0-9_]+)\s*:", cls, re.M)
        if not keys:
            warn += 1
            print("  ⚠️  R6-76:config.py QidianAdapterConfig 里没读到 scrcpy_* 键(措辞变了?),跳过")
        else:
            s71 = _slice_between(t02, r"^### 7\.1 ", r"^### 7\.2 ") or ""
            lack = [f"{k}({w})" for k in keys for w, s in (("02 §7.1", s71), ("07", t07)) if f"`{k}" not in s and f"{k}=" not in s]
            if lack:
                red.append("R6-76 scrcpy_* 键登记")
                print(f"\n  ❌ R6-76:config.py 已落键但未登记:{', '.join(lack)}")
                print("     理由:02 §7.1 是配置键唯一出处、07 是全表镜像;实现有键、文档没有 = 现场改不了也查不到。")
            else:
                print(f"  ✅ R6-76:config.py 的 {len(keys)} 个 scrcpy_* 键在 02 §7.1 与 07 均已登记")

    # ⑦ R6-70:解码背压阈值不得回到 3 帧
    hits = [i for i, ln in enumerate(lines_of(t01), 1) if re.search(r"队列\s*>\s*3\s*帧", ln)]
    if hits:
        red.append("R6-70 背压阈值")
        print(f"\n  ❌ R6-70:01 仍写「队列 > 3 帧」—— 行 {hits}(裁决为 6 帧)")
    else:
        print("  ✅ R6-70:01 背压阈值不再是 3 帧")

    if warn:
        print(f"\n  (⚠️ {warn} 项跳过/在途——保守起见不判红,请人工核)")
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
    red += check_merge_window_literal()
    red += check_literals()
    red += check_result_codes_to_01()
    red += check_rulings_0926()
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
