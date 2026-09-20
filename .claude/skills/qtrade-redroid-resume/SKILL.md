---
name: qtrade-redroid-resume
description: 断点续接交接 —— QTrade「redroid 多实例 IM 控制台 + 统一 RPA」项目(企点/QQ/微信三通道、WSL2+redroid+自编 binder 内核、单 EXE 离线安装)。新 session 接手本项目、或需要那套设计文档知识库时读本 skill。含设计文档索引、六轮评审决策脉络、企点读库(主库)/微信取钥/内核切换的实操指针、禁区与已踩坑。
---

# QTrade redroid 多实例 IM 控制台 —— 工作交接 / 断点续接

> 🔴 **头等规范(安琳 2026-09-19 定,优先级高于本文其它一切)**:**所有思考与所有交流一律使用中文**——工具输出/代码/报错是英文也不切换;标识符、命令、路径、错误原文原样保留,说明用中文。此规范同时钉在仓库 `CLAUDE.md`(project scope)、`~/.claude/CLAUDE.md`(user scope)与项目 memory 目录(随仓库快照 = `reference/zh-only-rule.md`);换机器接手时先把三处带过去。

> 2026-09-19 创建、当日多次更新(最新:**R6-47~R6-50 收第八轮 cursor 评审 8.4/10(`norm()` 定义 / (r) 拍板 A / P-MSG 三字段 / `qidian.rebootstrap` 登记),脚本 PAIRED 27 + ⑪ NORM;此前 R6-38~R6-46 收第七轮 + 企点消息类型路由**,详见 §5 顶部「🔴 最新状态」)。这是**独立于 ibquote(南银报价平台)** 的另一个项目。接手先读本文件全部,再按需读桌面知识库。
> ⚠️ 本 skill 是**入口与指针**,不复述文档内容 —— 每个主题都指向"看哪个文件的哪一节",省 token。

## 🧳 跨机器迁移(换一台机器接手本项目时先读这一段)

本文件是在**安琳的原机(WSL2 box,redroid 容器 + 企点/微信 RPA 就跑在这台)**上写的,里面有大量本机绝对路径。搬到别的机器时,先分清哪些跟着走、哪些留在原机:

- **✅ 跟着 GitHub 走(别的机器要的就是这些)**:设计文档 = 私有仓库 **`git@github.com:dantsinghua/windows-IM-channel-intergration.git`**(分支 `main`),文档在克隆目录的 `docs/` 下。**本 skill 与两份核心记忆已一并提交进该仓库的 `.claude/skills/qtrade-redroid-resume/`**(`SKILL.md` + `reference/*.md`),所以 `git clone` 一次就全拿到。文中凡写 `Desktop\work\docs\` 的,在别的机器一律理解为 **`<你的克隆目录>/docs/`**。
- **⛔ 只在原机、搬不走(除非你要在新机上真跑 redroid/RPA)**:`~/work/xunjia-agent/`(企点 RPA 七件套、`echo_loop_maindb.py`、`qidian_msgdata_decode.py`)、`~/work/qtrade-redroid-installer/{kernel,rootfs}`(自编内核、redroid rootfs)、`/mnt/c/.../weChatlog/`(微信取钥工具)、`~/.claude/projects/-home-anlin-work-qtrade-ibquote/memory/`(全部记忆原件)。**只想续设计/评审工作 → 这些全不需要**;要在新机真跑收发,得先把整套 WSL2+redroid+内核环境复刻过去,那是另一件事,不在本 skill 范围。
- **别的机器上装本 skill,三选一**:
  1. **仓库内自动加载(最省事)**:`git clone` 后,在克隆目录里开 Claude Code —— `.claude/skills/qtrade-redroid-resume/` 会作为**项目级 skill 自动被发现**,`/qtrade-redroid-resume` 直接可用,无需复制。
  2. **装成全局 skill**:把克隆目录里的 `.claude/skills/qtrade-redroid-resume/` 整个复制到新机的 `~/.claude/skills/`(Windows 原生 Claude Code 是 `%USERPROFILE%\.claude\skills\`),任意目录都能 `/qtrade-redroid-resume`。
  3. **只要内容不要 skill 机制**:直接读克隆目录里的 `.claude/skills/qtrade-redroid-resume/SKILL.md` 全文 + `reference/` 两份,再读 `docs/`。
- **记忆**:随仓库带过去的是 `reference/design-doc-consistency-lessons.md`(多 agent 写文档的一致性教训)与 `reference/qidian-read-via-db.md`(企点读库正线/延迟/消息类型的实测结论)两份**快照**;新机上它们不是「活记忆」,是随本 skill 的参考件。原机的活记忆仍在 ibquote memory 目录、随本项目继续更新。

## 0. 项目一句话

给交易/资金团队做的**多通道 IM 自动化控制台**:一个 Windows 单 EXE 离线安装 → WSL2 + 自编 binder 内核 + redroid(Docker 里的 Android 11)跑**企点/QQ**、Windows 侧跑**微信 PC**,统一 RPA 收发消息、邮件摆渡驱动、多账号管理。**2026-09-19 第八轮 cursor 评审(8.4/10)由 R6-47~R6-50 收口后已开工:仓库 `src/qtrade_agent/` 有 Agent 侧 M2 骨架(store / 企点读库 / 总线,49 条单测全绿,见 §5「代码现状」);真机接入(redroid / RPA 执行层)仍只在安琳原机;企点收发已真机验证。此前:R6-38~R6-46 收第七轮评审与企点专项,R6-1~R6-37 六轮回改与终审。**

- 🔴 **设计文档唯一的源(2026-09-19 夜安琳定;改这里、读这里)**:`/mnt/c/Users/anlin/Desktop/work/docs/`(= `C:\Users\anlin\Desktop\work\docs\`)。它所在的 `Desktop\work\` 是 git 仓库,远端 = **`git@github.com:dantsinghua/windows-IM-channel-intergration.git`(私有,分支 `main`)**;**每轮改完:跑对账脚本 → `git add -A && git commit` → `git push`**。提交身份只在该仓库本地配置(`dantsinghua` + GitHub noreply 邮箱),没动全局 git 配置。
- ⛔ **已停止维护、不要再改也不要再同步**:WSL 的 `~/work/qtrade-redroid-installer/docs/design/`(内容停在 R6-46、与 GitHub 首个提交 `a2e93ee` 逐文件一致,放了一份「⛔已迁移」说明;文件未删,只作历史留底)。原桌面知识库 `Desktop\QTrade详细设计-20260918\` **已被安琳挪进 `Desktop\work\docs\`、原路径不存在了**——本文件下面凡写「桌面知识库 / 桌面副本 / cp 回桌面」的旧句,一律理解为 `Desktop\work\docs\`。
- 上位规划(已冻结,别当规格):`~/work/qtrade-redroid-installer/docs/DESIGN-多实例控制台与统一RPA.md`
- 企点/微信 RPA 实现与验证:`~/work/xunjia-agent/`(relay/side_a、docs/04-redroid-rpa.md)

## 1. 🔴 知识库 = 桌面设计文档(唯一真值,先读顺序)

**规格 = 00 基线 + 01~06 六册 + 07 配置总表,仅此而已**(基线 §11.16 [DOCFREEZE];主文档与 99 系列已冻结、只作追溯)。裁决全在基线 **§14 拍板 + §15/§15c~§15g(六轮 R-*/R2~R6)**,新裁决追加到 §15g 末尾、编号续 R6-N。冲突裁决顺序:`00 > 02(表/端点)/01(页面/元素ID) > 03~06 > 主文档/99`。

| 序 | 文件 | 读它解决什么 |
|---|---|---|
| **先读** | `00-共享基线与口径.md` (v1.3 + §15g 增补) | 术语/拓扑/端口/目录/配置/ID/数据模型/状态机/枚举/表名/页面ID/**红线§11**/**§14 拍板**/**§15~§15g 六轮回改+第七轮复评裁决表(R-*~R6-37)** |
| 1 | `02-后端与本地数据库设计.md` (v0.4.5) | Agent/WinAgent 模块、SQLite 两库全部 DDL、`/api/v1`+`/wa/v1` 端点、并发规约、IPC 契约 |
| 2 | `01-控制台前端设计.md` (v0.4.5) | Electron 三进程、页面/元素 ID 唯一出处、testid、事件→UI |
| 3 | `03-安装引导与自动化配置.md` (v0.4.5) | 单 EXE 离线打包、环境矩阵、内核切换与回滚、微信版本匹配/重装、退出码 |
| 4 | `04-系统监控与本地网络.md` (v0.4.5) | 健康项、连通性探测、WSL⇄Windows 网络、`.wslconfig` 十一键、防火墙 |
| 5 | `05-账号配置与多账号管理.md` (v0.4.6) | 三通道首登、DPAPI Vault、多账号切换、微信单在线、**企点读取=旁路读库(§非frida)**;§2.1.1 ⑪a **企点 `self_uid` = 登录 uin(纯数字)**(R6-39) |
| 6 | `06-邮件摆渡与消息存取.md` (v0.4.6) | 邮件收发/模板/去重、消息本地存取、**企点旁路读库 §2.9.5(主库;🔴 R6-38~R6-42 重写:`poll_maindb` 伪代码=表发现+bootstrap+历史闸+水位自检+掉线续读、`check_group_gaps` 群缺口、发送确认阻塞语义)**、30天保留 |
| 附 | `07-配置项总表.md` (v0.2) | 约130键跨册对账基准(owner/默认/消费方) |
| 附 | `check-truth-tables.py` | **真值表对账器,改完必跑**(FORBIDDEN/PAIRED/ENUM/STATUS/KEYNAME/COPYABLE/MIRROR/**⑨DYNAMIC=值集合从owner册现读**/**⑩VALUE=企点确认窗≥15000 且 02↔07 同值**/版本;**73 条规则**(R6-37 `qdidx:`/`c4ext1`;R6-38~R6-41 新增索引库列名、L2 二选一、`.backup`、6 字节密钥、`poll` 无参签名、`ingest` 两元组、新码/新键登记等;**每条新规则都在改前备份上反向验证过能红**);`python3 check-truth-tables.py`,退出0=全绿 |
| 代码 | `src/qtrade_agent/` + `tests/` | Agent 侧首批代码(2026-09-19 开工):`text.py`(`norm`/`clean_text` 逐字 = 06 §2.9.2/§2.9.5)、`store/`(`schema_agent.sql` 逐字抽自 02 §3.1;`ingest` 三元组、同事务顺序、出向合并定序、QQ id 复用、`qidian_bootstrap_rebase` 含审计)、`adapters/qidian/`(`xor.py` 17 字节密钥、`msgdata.py` msgtype 路由 + `-1035` protobuf Elem、`maindb.py` 本地/adb 两后端、`poll.py` = 06 §2.9.5 两段伪代码逐分支)、`bus/`(R6-48 校验、出向先落库、幂等三态、队列外等确认)、`events.py`/`alerts.py`、`capabilities/*.json`;`python3 -m pytest -q` |

⚠️ **没有「同步副本」了**:唯一的源是 `Desktop\work\docs\`(见 §0),改完跑对账脚本、提交、推送。该目录在 Windows 盘上(`/mnt/c`),**不是 git 之外还有一份备份**——每轮改前仍先 `cp` 一份到 scratchpad 供只读终审 diff,但真正的还原点现在是 git 历史(`git diff` / `git checkout -- <file>`)。

## 2. 关键决策脉络(细节看基线 §14 拍板 + §15~§15g 六轮裁决表)

- **无大模型**(§11.13):全程序只有 `voice_to_text` 的 ASR 用多模态(安琳拍板 = A,维持,进邮件默认白名单);企点读取走 **旁路读库(正线)→控件树→截图+离线OCR**(**frida 不在读取链**);编排是固定工作流。
- **账密可存**(§11.1):存 WinAgent Vault(DPAPI),不是"不存密码"(那是被冻结主文档的旧口径)。
- **WinAgent 拆两进程**:服务(LocalSystem,监听 17610)+ 用户会话代理(不开入站口);调用恒 Agent→WinAgent,从不回调。
- **本地数据留 30 天、采集侧文件 7 天**(E-18);磁盘三级水位硬保护;写失败第一诊断=磁盘满(`DISK_FULL`/HTTP507)。
- **微信每 wxid 一个 wxNN、共用一个在线槽位**;禁静默卸载(会清聊天记录)。
- **六轮评审的失败模式教训**已固化成对账脚本 + 记忆 [[design-doc-consistency-lessons]]:①终检按真值表不按关键词;②"半改比没改险"(改了说明段漏了动作表/DTO/标题/邻表);③裁决要产生方+承接方两侧点名;④跨册名字/判据在裁决里逐字定死再分派;⑤收敛期由总控直接改、别再分派;⑥脚本全绿≠通过,须只读独立终审。

## 3. 实操指针(要跑什么 → 看哪,不在此复述步骤)

| 要做的事 | 看这里 | 一句话注意 |
|---|---|---|
| **切自编内核** | `~/work/qtrade-redroid-installer/kernel/out/APPLY-WSL.md` + Windows 双击 `C:\Users\anlin\.qtrade-redroid\apply-binder-kernel.cmd` | 会 `wsl --shutdown` 杀掉全部 WSL;失败自动回滚官方内核。**v4 已于 2026-09-19 现役**(`.wslconfig kernel=…\bzImage.v4`);退 v3 = 改 `kernel=` 回 `bzImage` 再 shutdown;⚠️ `rollback-kernel.ps1` 回的是官方内核不是 v3 |
| **重编内核** | `~/work/qtrade-redroid-installer/kernel/build-kernel.sh` | 已接入 core_pattern patch + 产物硬校验;⚠️ vsock 三项动了会真机关机(v2 教训) |
| **redroid 起容器 + 装企点** | `~/work/qtrade-redroid-installer/rootfs/out-wsl-local/` 的 `up.sh` + `install-qidian.sh` | WSL 重启后不自动回来,跑 up.sh;adb 只启动时扫一次 loopback:5555 |
| **企点冷启动→登录→发送** | `~/work/xunjia-agent/relay/side_a/qidian_cold_start.sh` | 分步可单跑,发送默认关闭;**★标准序列 `all` = root→qidian-start→check-login→search→open-chat**(`adb root` 是标准动作:不提权则能发不能读;frida 步已移出 `all`);2026-09-19 真机全链路跑通(发 dantsinghua + 读库读回) |
| **企点读消息(实时)** | **主库** `{uin}.db`:参考实现 `~/work/xunjia-agent/relay/side_a/echo_loop_maindb.py` 的 `query()`;规格 06 §2.9.5(**R6-36 已改主库**) | 会话分表 `mr_friend_{MD5(对端uin)大写}_New`/`mr_troop_{MD5(群号)大写}_New`;`msgData/senderuin/frienduin` 逐字节 **XOR,密钥 = ASCII 字符串 `b"02:00:00:00:00:00"` 的 17 字节循环(`in[i]^KEY[i%17]`;🔴 不是 6 个原始字节——R6-39 勘误,按 6 字节解出来全是乱码)**;游标=各表 `_id`;延迟 **2~12 s**(见 §5);**前置=先 `ensure_root`**;零attach零崩溃、`.backup` 只读副本 |
| **企点读消息(历史/全文兜底)** | 索引库 `{uin}-IndexQQMsg.db`:`db_reader.py`(Base64、单表 `IndexContent_content`、单 `docid` 水位) | **滞后 13~36 s、只索文本**,不做实时正线(R6-36);只作历史回填/全文检索 |
| **微信取钥** | `/mnt/c/Users/anlin/Desktop/盈米/蜂鸟项目/南银理财/weChatlog/` (chatlog + wx_key DLL) | 三段固定序:起hook→点图片(img_key)→退出重登(data_key),两钥同轮才落盘;详见 [[wechat-pc-install-facts-2026-09-18]] |
| **改设计文档** | 改 `/mnt/c/Users/anlin/Desktop/work/docs/` → 跑 `check-truth-tables.py`(在该目录里跑)→ `git commit` → `git push` | 新裁决追加到基线 §11 末尾、引用写"§11.N [锚名]";别插中间(会导致编号漂移) |
| **改代码 / 跑单测** | 仓库根 `python3 -m pytest -q`(需 `pytest`、`pytest-asyncio`;`pythonpath=src` 在 pyproject.toml);`tests/test_docs_consistency.py` 把代码与文档对账(配置默认值 = 02 §7.1、schema 文件 = 02 §3.1 sql 块、`norm` 函数体 = 06 §2.9.2) | 代码以文档为准;改表先改 02 再重新抽 `schema_agent.sql`(抽取方式写在文件头);真机执行层(点发送键等)以 `sender` 回调注入,`tests/test_bus.py::FakeSender` 是假的 |

## 4. 🔴 禁区(违反会出大事)

- ❌ **绝不自行** `wsl --shutdown` / 重启 WSL / 重启 docker 容器 —— 会中断安琳的登录态和其它工作负载(Dify/MySQL/GaussDB 等 12 个容器都在同一 WSL)。要重启先明示、等确认。
- ❌ **绝不动** `~/.config/frp` frpc、不改 FlClash 配置(飞连/OpenVPN 可调)。
- ❌ 企点/微信是**真实工作账号**:任何**会离开本机**的动作(发消息/加好友/进退群/改资料)—— **有指令且对象内容逐字给定→复述即发;需要推断→复述后等回话;碰红线(价格/成交)→无论如何等回话**。判据=出不出这台机器。
- ❌ 企点主进程**在翻译层下对 frida 重负载极脆弱**(广谱hook/全量堆扫崩过两次)—— 读消息一律走**旁路读库**(主库 `echo_loop_maindb.py` 的 `query()`;frida 不在读取链),别 attach 进程。
- ⚠️ 内核崩溃转储:v2 关机根因=`CONFIG_VIRTIO_VSOCKETS=y` 抢 vsock;改内核配置前必查 vsock 三项与现役 v3 一致。

## 5. 当前进度与下一步(2026-09-19 更新至 R6-37 收口 + 边缘风险已补)

### 🔴 最新状态(2026-09-19 夜,接手先读这一段;下面「已完成/挂着的」是更早的记录)

**第八轮 cursor 评审(8.4/10;R7 五条 CLOSED、R6-38~R6-46 零回归;1 P0 + 3 P1)已由 R6-47~R6-50 收口(2026-09-19,在 GitHub 仓库分支上改、脚本 exit 0)**:
- **R6-47(P0)`norm()` 三册无可抄定义** → 06 §2.9.2 落函数体(评审原句照收):`not s → ""`;NFKC;`re.sub(r"\s+"," ",s).strip()`;**不剥 `U+0014`**。分工逐字定死:`clean_text` = 读库解码侧、只在 `to_message()` 调一次;`norm()` = 比较侧、两侧各算一次、不改 `[表情]`/`[图片]`;**出向 `SENDING` 行 = 发送原文、入库前不过 `clean_text`**;00 §7.4 / 02 §2.8.1 改指针。脚本 ⑪ NORM 查唯一 `def norm(` + 函数体三件事、且不含 `\u0014`/`.replace(`。
- **R6-48 开放项 (r) 拍板 = A(总控定,安琳可改)**:`send_text.text` 须 `clean_text(text)==text`,违例 `400 INVALID_ARGS`/`error.reason='text_has_control_chars'`/`pointer='/text'`,校验点 02 §3.10 `bus` 参数校验段、三通道同一条、不写 `SENDING` 行不占幂等键。不选 B 的理由:`U+0014` 是客户端内部转义、不可输入;B 会让库里「我方发出的内容」与实际发出的不一致。**真机待验一格**:企点会不会把 Unicode emoji 改写成 `U+0014` 落库(06 §8b M2「含表情/控制字符的发送」④,超时且读回含 `[表情]` 即记实测结论另裁,不预设)。
- **R6-49(P1)** 01 §2.7.7 `P-MSG` 渲染事件专属三字段:`late` 琥珀「迟到 {lag_s} s」`qt-msg-row-{id}-late`、`origin="external"`「外部来源」`qt-msg-row-{id}-origin-external`;只进本次会话 store,`GET /messages` 不带、重拉后消失 = 预期;§8b M2-1/M2-2。
- **R6-50(P1)** 02 §3.1 `audit_log` 登记 `action='qidian.rebootstrap'`(`kind/transport='system'`、`actor='system:qidian_adapter'`、`detail_json{old_uin,new_uin,deleted_cursors}`),05 ⑪a 同名引用;01 §2.10 写明 `QIDIAN_TABLE_DECODE_STUCK`/`QIDIAN_MSG_GAP` **只进铃与角标**、不上 `qt-acct-detail-read-degraded` 横幅。
- 脚本新增 PAIRED ×10(norm 定义 ×2、`text_has_control_chars` ×2、三字段 testid ×2、`qidian.rebootstrap` ×2、两码 01 承接 ×2)+ ⑪ NORM;**每条都在改前备份与「抹掉登记行」副本上反向验证能红**(harness 留在 session scratchpad,不入库)。评审「开工门」:M1、内核 D、WinAgent、Electron 路由与 testid、微信向导、邮件取信、企点 poll 骨架可开;`norm()`/`send_*` 确认匹配已补 ⇒ **无阻塞项,进入编码**(见下「代码现状」)。

**🔴 代码现状(2026-09-19 夜,首批;GitHub 分支 `claude/lucid-dijkstra-uu5max`,尚未合入 `main`)**:
- 范围 = 02 §8 里 **M2** 行中能在无 redroid 环境下编码与单测的部分:`store` + `agent.db` 基线 DDL(逐字抽自 02 §3.1,SQLite 3.45 实跑;`schema_version` 记 sha256)、`bus` 单账号队列(出向先落库经 `store.ingest`、幂等三态、R6-48 入口校验、登录门、队列外等确认 + 加速轮投递)、`adapters.qidian` 读库正线(`poll_maindb`/`check_group_gaps` 按 06 §2.9.5 伪代码逐分支,含历史闸/水位自检/换号审计/H13 守卫/加速轮不动计数)、`events`(outbox + 事件专属 `lag_s`/`late`/`origin`)、`alerts`(去重键 `(code,subject)`)。**没做**:`api`(FastAPI 端点)、`runtime`(docker/redroid)、`pool`、`workflow`、`mail`、`vault_client`、`scheduler`、`health`、UI 执行层(搜索/打开会话/ADBKeyboard/点发送)——后者以 `QidianAdapter(sender=…)` 回调注入,真机接入时把 `qidian_cold_start.sh` 的动作包成同签名回调。
- 单测 49 条全绿(`tests/`):`norm`/`clean_text` 全部边界(纯表情不为空、串尾孤零、后继 `\t`)、XOR 17 字节自反、路由表五行、`-1035` 顺序、`ingest` 重扫 `(False,False)`、撤回 `changed`、出向合并定序/窗口/空文本守卫、QQ `#n`、空批推游标、单事务回滚、首登历史闸、新会话不丢第一条、重扫不重放、被踢重登 `late`/`external`、加速轮不动计数、首登 12 轮宽限、H13 换号只审计一次、个别表 STUCK 独立码、整库解不出一条告警、群缺口、bus 端到端「send_text → 假 RPA 落库 → poll 合并 → DELIVERED」、控制字符 400 且不进 commands、UNCONFIRMED 留 SENDING、登录门、send 让出队列。
- **代码 ↔ 文档对账**(`tests/test_docs_consistency.py`):配置默认值 = 02 §7.1、`schema_agent.sql` = 02 §3.1 sql 块、`norm` 函数体逐行 = 06 §2.9.2、能力目录会话参数只叫 `session`。改文档后跑单测也会红。
- **接手下一步(按优先级;②已在第二批完成)**:①真机接 `sender` 回调 + `AdbMainDb`(需 `ensure_root`,06 §2.9.5)跑一次 06 §8b M2「含表情/控制字符的发送」五例——(r) 拍板 A 的那格「企点会不会把 Unicode emoji 转成 `U+0014`」只能真机验;起服务 = `python3 -m qtrade_agent.main --db /var/lib/qtrade/agent.db`(先 `store.upsert_api_client` 建控制台令牌);②~~scheduler + api 骨架~~ 已完成;③下一批候选:`runtime`(docker/redroid 起停、端口推导、`ensure_root` 接 adb)、账号生命周期端点(#2/#9/#10/#12)、`pool` 最小配额、WinAgent `/wa/v1/time` 探测接 H13;④把 `main` 合入(或 PR)由安琳定。
- ⚠️ 已知取舍:`Store` 是同步 sqlite3 核心 + `AsyncStore` 分片锁包装(文档要求 aiosqlite;容器无网络装包,且 aiosqlite 本质也是线程 + sqlite3);bus 的 GATE 安全闸(§6 双闸门)本期为空放行,留 TODO;`Bus.submit` 对 `SENDING` 幂等行的等待用进程内 future,跨进程重启走 `ABANDONED → confirm_probe`。

**🔴 第二批代码(2026-09-19 夜)= 基线 §15g R6-52,独立验收 = R6-53**:`scheduler.py`(计时循环与执行分离:到点/trigger 只启动一轮、正在跑则 `skipped+1`)、`health.py`(H13 firing / `mark_rooting` 宽限窗 / 免鉴权摘要,均进程内内存态)、`api/`(FastAPI:Bearer 鉴权按 `api_clients` 表、级别 R/W/A、`allow_accounts` 收窄;00 §10 错误信封;`X-QT-Api-Version`/`X-QT-Agent-Version`/`X-QT-Capabilities-Version` 头与 `X-QT-Api-Min` → 426;端点 `system/version|health`、`capabilities`、`accounts`(00 §7.1 视图、端口按序号推导)、`accounts/{id}/commands|send`(#28/#29/#30/#31:HTTP 层错误按 00 §10 映射、业务结果一律 200 + CommandResult、REPLAY 409 带完整信封、`async`/同步超时 202)、`sessions`、`messages`(#48:FTS/LIKE 混合、G-16 JSON cursor、ISO 时间;#49)、`audit`、WS `/events`(订阅/`since_seq` 重放/`truncated`/过滤/`allow_accounts` 二次收窄/ping/重发订阅帧);每次调用记 `audit_log`,health 免鉴权摘要除外)、`app.py`(装配:开库→崩溃恢复→模块→scheduler 注册 `qidian_poll_all`/`qidian_gaps_all`/outbox 保留期)、`main.py`(uvicorn `ws="websockets"`)。**没做**:`runtime`(docker/redroid)、`pool`、账号生命周期端点(`POST /accounts`/start/stop/login…)、`mail`、`vault_client`、WinAgent 探测、HMAC 公网入站、webhook 投递器。第三位独立验收者(`tests/acceptance/test_spec_api.py` 77 条)首跑 3 失败——全是规格明写实现漏了(`disk_free_mb`、`X-QT-Capabilities-Version` 头、WS 帧 `ts` 未 ISO);另揪出 **R6-52 自己与 02 §3.4 通用约定打架**(#48 写 `items` 而通用是 `data`;cursor 写 `"ts_ms:id"` 而 G-16 是 JSON)——按通用约定改回,教训:**新写专条前先 grep 同册通用段**。终态 277 条全绿(开发者 65 + 验收 212)。

**🔴 首批独立验收(2026-09-19,安琳要求「子 agent 按需求文档独立写用例」)= 基线 §15g R6-51**:两位只读规格、**不看开发者测试**的撰写者各写一份 `tests/acceptance/test_spec_qidian_read.py`(94 项)与 `test_spec_store_bus.py`(41 项),首跑 2 失败:①**B-30 登录门留不留 `commands` 行**三处措辞互斥,实现选了「不留」——按 B-30 改为留 `failed` 行(`started_ms IS NULL`)、不写 idempotency;②**B-08 `confirmed_by` 由 probe 回填**实现漏做——回填到首次 trace、`code` 不改。另 20 条规格张力(`fingerprint` 序列化未定、QQ ext 两册不一致、`#n` 比对基准、合并窗字面 60s、`external` 窗口基准、孤零 `U+0014` 空串、Java 魔数隐式例外、事件时间键名 `ts`/`received_at`、`get_state` 返回…)全部按「只收措辞、不加设计」落进 02/06,代码同步(events payload 改 ISO 8601、`get_state` 返回 OK、QQ `#n` 与族内 `ts` 最大行比、gated WARNING 带表名/time 范围、`app_version` 取 `account_runtime`)。脚本加 ⑫ LITERAL(字面 60s;**不能放 FORBIDDEN**——那两句含否定词会被行级 NEGATION 整行跳过,首跑实测是空规则,改成独立检查后在 R8 改前备份上验能红)。终态:**184 条全绿(原 49 + 验收 135)**,文档对账 exit 0。**验收撰写者报告里值得记住的两条方法论**:①「用例失败先问是不是自己夹具违反了规格」(读库组首轮 2 条误报就是直接落 `UNCONFIRMED` 行、绕过了 SENDING→UPDATE 的规格路径);②规格里「示意公式」(`fingerprint`)、「省略号」(`msg_count+…`)、「同一量两种写法」(`60s` vs 配置项)这三类,独立实现者一定会各算各的——发现一处就逐字定死一处。环境:`.claude/hooks/session-start.sh`(仅远程容器装 pytest/pytest-asyncio)+ `requirements-dev.txt`。

**第七轮 cursor 评审(7.8/10,5 条 P0)已收口:基线 §15g R6-38 → R6-42,五轮只读独立终审 REVISE×4 → ACCEPT**(会改行为的问题数 8 → 13 → 7 → 2 → 0;末轮 0 阻塞、置信度高)。桌面副本已同步、脚本全绿(规则数以脚本实跑为准:FORBIDDEN 38 / PAIRED 17 / COPYABLE 3 / MIRROR 6 / DYNAMIC 3 + KEYNAME/ENUM/⑩值检查)。
- **R6-38(评审 5 条 P0,均核实属实)**:06 §2.9.1 `type/ts/self` 改主库 `msgtype/time/issend`(唯一出处 §2.9.5);`[bus] confirm_timeout_qidian_ms` 8000→**15000** + 新键 `[adapters.qidian] confirm_poll_interval_ms=1000`(真机实测我方回复落库可见 9.3~11.4 s,8 s 必然 `UNCONFIRMED`);§2.12 企点确认 = **ingest 合并**(`confirmed_by=ingest_merge`、`ext=qd:{uniseq}`;`history` 仅控件树降级路线;回执「确认方式」取的是 `CommandResult.source`=`qidian_db`,别和 `confirmed_by` 混);表发现算法;L2 **只认方案 D**(manifest 恒 `"D"` + CI 硬门)。总控另查出:方案 A 残留多 3 处、`.backup` 与实测读取器相反(主库 64 MB,正线 = `mode=ro` 只读直查增量)。
- **R6-39(安琳追问「被踢重登能否对上水位」+ 真机数据)**:🔴 **XOR 密钥口径勘误(17 字节 ASCII)**;`poll_maindb` 重写;**历史闸**(`time < qidian_bootstrap.value_int/1000 − 120` 不入库不发事件——首登时库近空、漫游历史后到,单靠 `MAX(_id)` 挡不住);**水位自检**(游标 `value.last_uniseq`,不符=库被重建→本表从 0 重扫);掉线续读语义;企点 `self_uid`=登录 uin。
- **R6-40/41/42(终审逐轮收)**:内存态全挂 `acct.`(多企点账号隔离);**失败计数与告警只由全量轮维护**(加速轮每秒一轮,不得清零/resolve);H13 时钟漂移时不建闸基准、走 `fail("clock_unsynced")` 12 轮告警、判定在换号 DELETE **之前**、删水位+写新基准同一事务;个别表解不出用独立码 **`QIDIAN_TABLE_DECODE_STUCK`**;`ingest -> (inserted, changed, id)`,`message` 事件只对 `inserted or changed` 发(重扫不重放邮件),空批只推游标不得早退;R6-41 写过的「`fingerprint` 第二去重键」**已撤回**(02 无落点、与 §2.12 冲突)。

**🔴 真机事实(2026-09-19,已进规格)**:
- 被踢重登:15:05 `ACCOUNT_KICKED`(「身份验证失败」,**原因未查明**),16:56 点「重新登录」**走免登零交互**恢复;水位在 `cursors` 表、各会话表各一条,重登后各自续读。离线期间对端发的**私聊**重登后补同步(原始 `time`、`_id` 续排)。⚠️ 库里大量「`_id` 靠后但 `time` 更早」的行 ⇒ 水位必须用 `_id`。
- 🔴 **群消息读库补不回来**:112 张群表按 `shmsgseq` 统计**缺 37.5%**(最大 46.6%),缺口 100% 是 ≥20 条大块、正对离线时段——企点重登只补私聊、群只拉最近一小段。⇒ `check_group_gaps`(**只对群**、近 3 天窗口、无状态、自动 resolved)→ warn `QIDIAN_MSG_GAP`。**私聊 `shmsgseq` 是对端全局发送序号,缺口 99.9%,不能用。** 自动补拉(RPA 上翻触发漫游)**未验证、本期不做**(安琳拍板:先探测+告警)。
- 别的端(人)发的消息以 `issend=1` 进本机库(241 行里群 102 行非本机发);判据 `dir='out' AND trace_id IS NULL` 且窗内无同文本带 `trace_id` 的出向行 ⇒ 事件 `payload.origin="external"`;**处置策略安琳定:留 S4**。
- 迟到消息:事件带 `payload.lag_s`/`payload.late`(`[messages] late_after_s=120`);安琳定:**动/不动两种都要支持、由指令与编排决定**(`on_late: skip|act`,缺省 skip;v1 无承载,属 S4 前置约束)。
- 延迟实测(重登后 7 条):读取延迟均值 **6.8 s**(1.9~11.5,大头 = 企点每 10 s 批量落库);纯回复链路 ≈ **2.2 s**;端到端均值 7.3 s。
- 🔴 **消息类型已查清并进规格(R6-43~R6-46,06 §2.9.5 路由表;四轮只读终审 REVISE×3→ACCEPT)**:全库 37009 行只有三种编码族、10 种 `msgtype`。**读库路只产出文本**——文本族 `{-1000 单行, -1051 多行(报价), -1049 含@}` 整体 UTF-8(36013 条 100% 合法);`-1035` 图文混排是 protobuf(顶层 repeated Elem),**有文本段才产出、`type=text`、图片段写 `[图片]` 占位且必须按 Elem 原顺序**(85 条里 79% 图在前);图片 `-2000`/群文件 `-2017`/链接卡片 `-2011`(这两种是 Java 序列化 `AC ED 00 05`)/系统 `-2006/-5040/-2018`/未知类型 **一律不产出 `Message`、水位照常越过**(未知类型计数 + 首见 WARNING)。**`clean_text`**:解码后在**字符层面**把 `U+0014`+其后 1 个字符替换成 **`[表情]`**(安琳拍板:不删——全库 17 条整条只有表情,删了正文为空;索引 ≥`U+0080` 占 2 字节,按字节处理会破坏 UTF-8),其余 <`U+0020` 除 `\t\n\r` 丢弃。⚠️ 原口径只认 `-1000/-1051`,会把群里所有 @ 消息(773 条/2.1%)当非文本丢掉。
- **参考解码器** = `~/work/xunjia-agent/relay/side_a/qidian_msgdata_decode.py`(专项 agent 新建,纯标准库;`decode()` 返回 `type/text/origin/emit`,`emit` = 规格让不让入库;`--selftest` 只读测试号私聊表 + 离线用例;全库 36013 条与 1099 条分层回归均验过,总控另做过独立断言)。
- **延迟模型定稿(安琳拍板:接受现状,SLA 读取 P95 ≤ 11 s、均值 ≈ 6.3 s)**:1.3 s 推送 + U(0,10.000) s 批量落库(占 86.5%,周期恒定相位固定,未找到配置开关);不碰企点进程能省的只有轮询 0.28 s。logcat `C2CMessageProcessor: Recv Msg`(+1.27 s)有会话/类型/长度但**无正文**;通知栏在企点前台时 0 条;**不做** logcat 事件触发、**不反编译** APK。
- **被踢 = 服务端强制下线**(`mqq.qidian.intent.action.ACCOUNT_KICKED` 由企点自身 uid 发起;已排除崩溃/OOM/断网;**原因码没拿到**)。被踢时**进程不死**,探活应看该 Intent/`NotificationActivity`。logcat `main` 缓冲区原 256 KiB 只留 63 分钟,**安琳拍板已扩到 4 MiB**(`logcat -b main -G 4M`,容器重启失效);`system/events` 留 4.5 h+。下次被踢 **60 分钟内**先 `logcat -d -b main` 搜 `PushForceOffline|ReqMSFOffline|kick`。`frida-server` 二进制留在设备上(安琳定:留着)。v4 内核下 4h39m 零崩溃零 ANR(「每小时 SIGSEGV」未复现,待 24 h 观察)。

**开工时必须带着的开放项(不阻塞编码;全文见基线 §15g R6-42 行 (a)~(i))**:`uniseq` 跨库重建稳定性(正反两向真机验)/「恰在被踢期间由别的端发出」无样本 / `on_late` 无承载 / 群缺口自动补拉未验证 / `mark_rooting` 重启即丢 / 历史闸 120 s 余量 / **对账脚本不读伪代码控制流(伪代码只能靠逐分支人读)** / `origin=external` 策略留 S4 / 脚本待补两条规则。

**下个 session 第一件事**:①问安琳要不要把这一版回给 cursor 做第八轮评审(R6-38~R6-46 改动面大,06 §2.9.5 基本重写);②S1 读循环仍须安琳亲自 gate(开工时解码直接用参考解码器的口径);③企点专项第一批(延迟/消息类型/被踢/体检)**已收完**,开放项见基线 §15g R6-42 (a)~(i) + R6-43~R6-46 (j)~(s),其中要真机验的:`uniseq` 跨库重建稳定性、被踢期间别的端发出的样本、**我方发出含表情的文本能否读回确认**((r):出向行存原文、读回是 `[表情]`,`norm(text)` 不等)、群缺口自动补拉;`norm()` 三册无定义((s))。**子 agent 的结论该进规格的由总控落,别让它改 `docs/design/`。**

**🔴 方法论(本轮新增,已写入 [[design-doc-consistency-lessons]])**:①**每一轮的新问题都出自上一轮新写的东西**——收敛期「只动点名处、不扩面」,宁可撤回一条未验证前提的补偿规则、留成开放项,也不要再加设计;②**伪代码要当真代码逐分支跑**(变量在每个分支有没有值、每个 `return` 出口处计数/告警状态是否一致),脚本看不见这一层;③换数据源时旧源**整张表的全部列名**都要进 FORBIDDEN;④同册写下一个实测量就要 grep 所有依赖它的阈值,并做成脚本的**值检查**;⑤新规则必须在改前备份上**反向验证能红**(本轮抓到 3 条自己写的空规则:否定词自吞、正则竖线多转义一层);⑥评审给的替换句也要核后果(`value_int=0` 会回灌全部历史)、终审给的建议也要核(「把 fail 挪到 stuck 之后」会一因两告警);⑦**新语义要沿事件流往下游走一遍**(R6-43 让非文本行也产出 `Message`,没接到出站信息邮件〔无类型过滤、只认 text|image〕、图片恒 MISSING、还引用了不存在的 `Message.origin_json`——解法是**收窄**回「只产出文本」,不是往下游接);⑧**数据清洗规则要问「清完会不会变空」**(删表情 → 17 条空正文 → 空邮件或被静默过滤;改成 `[表情]` 占位);⑨子 agent 的结论采信前到源头抽查(`-1049`/`-1035` 两条我都独立核过),它的推测要让它用全库数据证实或证伪(「@ 属性控制字节」后来被它自己的全库统计否定)。

**⚠️ 回环测试的坑**:`echo_loop_maindb.py` 的 `send()` 按**固定坐标**点「当前打开的会话」——**回环在跑时绝不能切企点界面**,否则下一条自动回复会发进真实的群;脚本做不了聊天页标题强校验,起回环前必须人看截图确认标题是测试号。回环只认 `msgtype=-1000`,窗口到点自停(`--minutes`)。


**已完成**:
- 内核 v4 现役(L2 转储防护实测拦住)。
- **第六轮回改闭环**:基线 §15g **R6-1~R6-35**;六册 v0.4.5/v0.4.6、07 v0.2;对账脚本 51 条(含 ⑨ DYNAMIC:值集合从 owner 册现读)。独立终审五次复核:前四次 FAIL、**第五次 PASS,结论「已达可开工基线」**。`voice_to_text` 安琳拍板 = A(维持)。
- **R6-36→R6-37(2026-09-19 傍晚,那轮 cursor 评审 = 安琳等的「再一轮」)**:cursor 评审综合 7.6/10,判 R6-36「读取正线改主库」是**半改**——06 §2.9.5 详节切了主库,但入库路径表/字段映射/去重键/`native_id`+`kind`+会话来源/`ensure_root` 判据/序列图这些**邻表全没跟**(5 条 P0),且对账脚本不查现行 `qdidx` = 假绿。**R6-37 总控直接改齐 21 处**:去重键 `qdidx:{docid}`→**`qd:{uniseq}`/各表 `_id`**;`native_id`/`kind`/会话来源 `IndexContent.c4ext1` 前缀→**主库 `XOR(frienduin)`+`istroop`**;`ensure_root` 判据 `*-IndexQQMsg.db`→**`whoami==root`(提权本身)+ 登录后能开主库 `{uin}.db`(读可读)**;05 §2.1.2 序列图补 ⑤b;04 十一键 L2/03 M1-32 = **内核封装方案 D**(去运行期改 sysctl、去方案 A 验收);§7.1 `state_code` 两组→三组;删 §15g 文末「待修订」段。**对账脚本 51→53 条**(新增 `qdidx:`、`c4ext1` 两条 FORBIDDEN,glob `0[1-7]` 排除 00 裁决表,实测能红能绿)。**独立只读终审两轮:第一轮 REVISE(揪出 `native_id` 来源另有 5 处邻表没跟——`06:719/748/1182/1470`、`02:552`)、补完后第二轮 ACCEPT(会改行为问题 0、新引入 0、「达可开工基线」)**。教训:半改的回改自己也会是半改,脚本全绿+自称闭环都不算,只读独立终审才抓得住(已并入 [[design-doc-consistency-lessons]])。
- 企点真机全链路跑通:冷启动→免登→搜索→私聊→发送→读回;`adb root` 定为冷启动标准动作(脚本 `root` 步、基线 R6-18/R6-28)。
- **企点读→回 回环实测**(`relay/side_a/echo_loop_maindb.py`,只回测试号 dantsinghua/QQ 415011447):读取延迟 3.0/3.4/11.4 s;「对端发出→我方回复发出」5 s/5 s/13 s。

**🔴 企点读取正线 = 主库(2026-09-19 真机实测,文档已按 R6-36 改完)**:
- `{uin}-IndexQQMsg.db` 是**滞后的全文索引**(13~36 s,只有文本)——已从正线降为兜底(R6-36)。
- **即时的是主库 `{uin}.db`**(WAL):单聊表 `mr_friend_{MD5(对端uin)大写}_New`、群表 `mr_troop_{MD5(群号)大写}_New`;`msgData`/`senderuin`/`frienduin` 逐字节 XOR,密钥 = `02:00:00:00:00:00`(安卓默认 MAC)循环,`issend/istroop/time/uniseq` 是明文整数;**游标 = 各会话表 `_id`(cursors.kind=`qidian_rowid:<native_id>`,单库多水位)**;`ext_msg_id='qd:'+uniseq`(实测全库唯一);`native_id` 单聊=`XOR(frienduin)`、群=`g_`+群号;设备上 `sqlite3 'file:…?mode=ro'` 只查增量、单次 0.01 s。
- 主库也非逐条即时:企点 **`transSaveToDatabase` 每 10 s 批量落库** ⇒ 读库延迟 ≈ 1.8 s + U(0,10) s(2~12 s,均值约 7 s)。消息进内存的时刻可从 logcat `Q.msg.MsgProxy insertToList MessageRecord` 拿(≈1.8 s,无正文)。
- 我方发出的消息落库同样滞后(≈7~9 s)⇒ 发送后的读回确认**必须异步**,否则会阻塞下一条回复。

**方法论教训(第六轮,已写入 [[design-doc-consistency-lessons]])**:①返工自己会造新接缝(各路自洽、合起来打架)——跨册的名字/判据原句必须在裁决里**逐字定死**再分派;②总控自己的裁决也会错(⑤/⑥ 锚点、§2.7.5/§2.7.4、提权序列里的 kill-server、「九项已闭合」)——子 agent 与终审的异议要认真看;③「脚本全绿」≠ 通过,存在性检查会给假阳性的安全感,必须有只读的独立终审;④收敛期(只剩「owner 写对、消费方没跟」的单点)由总控**直接改**比再分派更稳(第五次零新引入);⑤值集合类检查要「从 owner 册现读」,写死清单管不了 owner 新增。

**挂着的**:
1. **企点接进 RPA 五步节奏**(⚠️ 安琳 2026-09-19:**读循环先不开工,等再一轮 cursor 评审良性后才开**):S1 读循环(**读主库**,游标 = 各表 `_id`;启动先 `ensure_root`)→ S2 统一入库 → S3 发送闭环(异步读回确认、会话校验)→ S4 喂谈判核心(先 mock)→ S5 守护/降级。红线:真实发送只发测试号。
2. ✅ **设计文档企点读取节已改主库**(2026-09-19,基线 §15g **R6-36**):06 §2.9.5 + §2.9.1 + 02 §2.8.1/§2.8.3 + 00 §6 全部换成主库 `{uin}.db`(会话分表、XOR 密钥 `02:00:00:00:00:00`、游标 `cursors.kind='qidian_rowid:<native_id>'`、`ext_msg_id='qd:{uniseq}'`、延迟 2~12 s、异步读回)。索引库降为兜底(`db_reader.py`);主库参考实现 = `echo_loop_maindb.py` 的 `query()`。✅ **那轮 cursor 评审已回**(R6-36 半改已由 R6-37 收口、邻表全跟上、独立终审判 ACCEPT/可开工)——安琳设的「读循环等一轮 cursor 评审良性后才开 S1」这个前置**现已满足**。🔴 **下个 session 第一件事 = 跟安琳确认是否开 S1 读循环**(条件已满足,但安琳要亲自 gate、别自作主张开工;S1 = 读主库、游标各表 `_id`、启动先 `ensure_root`、只回测试号)。
3. 待安琳定:要不要做「logcat 触发」(≈2 s 知道有新消息,只读日志、低风险);要不要探索让内容也秒级(提前落库/读界面,都要碰企点,有风险)。
4. 里程碑收尾清单(不阻塞):`mark_rooting` 进程重启即丢的处理;脚本补「全套行号锚扫描」「跨册同义量命名唯一性」;**脚本 FORBIDDEN 的 NEGATION 由「整行含否定词即跳」收紧到「命中点近邻窗口」**(现整行判会漏「活写坏 token + 同行无关否定词」,qdidx/c4ext1 两规则都受此限,终审已点名——但按「误报比漏报更伤」没把握前别动共享 NEGATION 逻辑)。
   ✅ **边缘风险已补(2026-09-19,安琳定"先补边缘风险再说")**:①`fp:` 前缀分叉——02 §2.8.1(529)去重键 + 02 DDL(925)注释都对齐 owner 06 §2.9.2 = `ext_msg_id=fingerprint`(full sha256、无前缀;原 `"fp:"+[:32]` 按 99c C-03 作废);②01 §2.5 白名单③ 补 `GET /wa/v1/wechat/login/status`、`ui-visible`(05 §3.2 R6-5 判控制台只读可直调、01 §3 已列,是 01 内部漏列);③01 §4 P-ACCT-DETAIL 新增 `qt-acct-detail-read-degraded`(warn 横幅、**无按钮**,守住 R6-35 已删的"重试提权";兑现 01:873 悬空声明)+ §2.7.3.4 概览承接;④README 行数/版本刷新到实际。脚本全绿、桌面同步。
5. 企点当前**已退回消息列表页**(2026-09-19 已做);redroid 容器 `qtrade-redroid` 在跑、已登录态、adb root 在位。

## 6. 记忆索引(项目相关,`~/.claude/projects/-home-anlin-work-qtrade-ibquote/memory/`)

- [[qtrade-design-docs-2026-09-18]] — 设计文档唯一真值口径、六册分工、开工边界、Cursor 15条P0
- [[design-doc-consistency-lessons]] — 🔴多agent并行写文档的7类缺陷+我犯的错;对账脚本八类检查的由来
- [[qidian-read-via-db-2026-09-19]] — 企点读消息=旁路读库;🔴 **正线=主库 `{uin}.db`**(会话分表、XOR 密钥 `02:00:00:00:00:00`、游标各表 `_id`、延迟 2~12 s、去重键 `qd:{uniseq}`、native_id 主库 `frienduin`+`istroop`);索引库降兜底。含 R6-36/R6-37 全部实测与收口
- [[redroid-crash-dump-disk-eater-2026-09-18]] — 企点每小时SIGSEGV吃147GB;反面结论:只设容器ulimit core=0无效
- [[qtrade-core-pattern-in-kernel]] — core_pattern封装进随包内核;改编译期默认值无效(是/init覆盖)
- [[wechat-pc-install-facts-2026-09-18]] — 微信安装/卸载/取钥三段序/数据目录实测
- [[wsl-redroid-feasibility]] — 内核编译状态、redroid就绪、别重复探测
- [[wsl-tun-tcp-blackhole-2026-09-17]] — WSL经FlClash TUN的新建TCP黑洞(已修)

## 7. 环境约束(本机)
- 内网访问需飞连VPN在线;回复与思考一律中文。
- Bash 每条命令后 cwd 重置到 `/home/anlin/work/yingmi`,跨命令用绝对路径。
- 防自杀 hook:`pgrep/pkill -f` 明文被拦(exit 144),用单字符方括号规避(如 `[f]rida`);`pkill -f xxx` 会把自己这条命令也算进去而误杀,慎用。
- ⚠️ **改文档的 python 脚本别写 `open(p,'w').write(open(p).read())` 这种"读回自己"的收尾行**——`'w'` 先截断,读到空、写回空,会把整册清零(2026-09-19 踩过,靠桌面副本恢复)。每轮改完 `cp` 回桌面 = 唯一可靠还原点,务必保留这个习惯。
- 对账脚本 `check-truth-tables.py` 新增规则首跑**必看命中**:误报比漏报更伤(会让人不再看它);历史裁决表 §15x 会引述旧句,规则要用否定词近邻/范围收窄排除。
