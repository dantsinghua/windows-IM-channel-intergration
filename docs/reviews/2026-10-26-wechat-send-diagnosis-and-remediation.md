# 微信发送状态不一致：根因、影响与整改方案（待安琳确认）

> **2026-10-27 历史保留说明：**安琳已随后批准 P0 修复、测试、通讯录刷新及 Greydan 指定群 E2E，并追加群名称元数据、合法独立人数标题支持及 P0/T10 迟到确认补齐。最终组合隔离验收和 attempt4 单条文本真实链均 PASS，见[本轮验收报告](2026-10-27-wechat-p0-acceptance.md)。下文“尚未实施、待确认、停止点”均为 2026-10-26 授权前快照，保留原诊断与失败叙述；原 `hello`、attempt2 等历史 UNKNOWN 未改写或重发，P1/P2 未整体实施。

安琳，本次“hello, 测试”没有实际发出。现役后端已记录发送失败，消息页面却只展示“发送”方向而没有展示失败状态；同时，明确的发送前拒绝被总线降成通用错误，导致南银只能保守保留 `unknown`。本文件汇总本次实证、追加发现及分批整改方案，**尚未实施这些整改，也不是新规格裁决**。

安琳要求“定位根因、继续查找本次暴露的其他 bug，并把根因、修复方案、影响范围、整改措施、待澄清点、测试方案发确认”。该指令之后的工作限于只读取证、纯离线复现与文档；未再实发、重试、点击发送、改群资料、部署或修改产品代码。既有服务和历史记录保持。后续实施遵循[配置变更保护机制](../../.codex/CONFIG_CHANGE_GUARD.md)，由实现、测试、独立验收、文档、终审分别负责。

## 1. 结论与证据边界

| 范围 | 本轮结论 | 不能推出的结论 |
|---|---|---|
| 18:03 的微信发送 | 目标群没有可信可搜索名称，正文输入前被拒绝；QTrade 消息状态为 `FAILED` | 没有原生 `DELIVERED`，不能写成“微信发出后丢失” |
| 用户看到的消息页 | 列表仅展示方向，失败和原生确认依据不可见 | “发送”标签不是送达回执 |
| 南银最终回执 | 本条为 `unknown`，没有伪造 `sent`；上游明确拒发码丢失使其无法定为失败 | 不能把全部 `SEND_FAILED` 一律改成 `failed` |
| 原生读回确认 | 纯离线反例证明存在误确认风险 | 本条在目标定位阶段已失败，没有触发该风险；未证明现场发生错发 |
| 文件网关 | 本轮实测上传、下载成功；积压曾造成长延迟，最终业务队列为 0 | 无服务端内部追踪，不能将客户端、网络、TLS的整体耗时全部归因于服务器 |
| 群接收与 Dify | 另有7条真实文本完成全链，5入站、2自己发送，均被流程判为噪声 | 不覆盖本次失败发送、正式报价抽取、真实图片或其它媒体 |

记录绑定于 2026-10-26 的本机现场。QTrade 为 `main`、HEAD `f4d915fb89ac197ff4245b39a91cdc5a813794c2`；南银为 `main`、HEAD `aef15bf0c997b216553beae36df6d99712cd0a73`，两库均有保留的未提交修改，不能只用 HEAD 代表运行字节。前一阶段独立核验现役 User EXE 41/41、svc EXE 37/37 内嵌模块匹配当前源码；此次失效不能直接归为旧二进制。来源摘要、运行身份见[前阶段独立终审](/home/anlin/.local/state/qtrade-local-start-20261026/final-review/REPORT.md)。

## 2. 本次指令的完整因果链

目标群为 `45686445455@chatroom`，本机账号槽位为 `wx05`。南银指令 ID 为 `7835e16c-ecfc-4c11-8bf8-0c591601a3dc`，QTrade trace 为 `01M5VTMRQSJM4VHW9QZ3NAPNA2`。

| 层级 | 本轮直接证据 | 判定 |
|---|---|---|
| 原生群资料 | `/contact` 与 `/chatroom` 各有一条精确群ID记录，`remark`、`nickName` 全空，群成员数为4 | UI成员展示标题未形成可信、可搜索的稳定群ID映射 |
| 会话预览 | `/session.nickName` 与截图的完整群标题不同 | 不能拿预览昵称兜底搜索群，否则可能定位错对象 |
| WinAgent 审计 | 18:03:37.812 入口鉴权 `OK`；18:03:37.986 用户代理实际执行返回 `TARGET_NOT_FOUND` | 首条 `wechat.send OK` 只证明鉴权通过 |
| 原生执行 | 真实 `_resolve_send_target()` 只读复现相同错误；定位发生在正文输入和发送按钮动作之前 | 本次没有输入该正文或点击发送；准备阶段可能调前台，不等于发送 |
| QTrade 适配器 | WinAgent 404 被转换为 `TARGET_NOT_FOUND`，不可重试 | 此层保留了明确前置拒发语义 |
| QTrade 总线 | 仅保留 `NOT_READY/GATE_BLOCKED/LOGIN_REQUIRED`，将 `TARGET_NOT_FOUND` 改为 `SEND_FAILED` | 丢失了“原生发送未开始”的确定性 |
| QTrade 结果与消息 | 191ms 完成，命令 `failed/SEND_FAILED`；消息 `FAILED`，`ext_msg_id=null`，`confirmed_by=null` | 是失败的本地发送占位，不是微信原生消息 |
| 中转与南银 | `NativePort` 对无原生确认的泛化失败返回 `unknown`；南银保留 `no-conclusive-native-evidence` | 未把 accepted、submitted 或 HTTP 200 当成送达 |
| QTrade 页面 | 列表和详情均未渲染 `Message.state`；方向为 out 就显示“发送” | 页面隐藏了已存在的失败事实 |
| 原生复核 | 对该群的有效 Chatlog JSON 查询，`keyword=hello` 返回0条 | 与用户截图和前置拒绝一致 |

错误原文为：`联系人 45686445455@chatroom 没有可搜索的备注或昵称`。仅绕过 contact 提前返回也不能解决本群：对应 chatroom 的名称同样为空。

证据：[实际发送全链](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/actual-send-chain.json)、[持久结果原文](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/command-result-native.json)、[原生审计](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/native/native-audit.json)、[目标解析只读复现](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/native/target-resolution.json)。

## 3. 问题清单、影响与拟整改

优先级是待确认的实施顺序。**P0**先保证不误报、不误确认及状态可解释；**P1**补群定位能力与积压恢复；**P2**完善审计可读性。未把纯离线反例写成已发生的现场事故。

| 编号 / 优先级 | 问题及证据状态 | 根因 | 影响范围 | 拟整改与保留项 |
|---|---|---|---|---|
| B01 / P1 | 无显式群名无法发送；**本次实证的能力缺口**，前置拒绝本身合理 | 发送依赖备注/昵称，UI自动标题尚无已证可信ID映射 | 未命名群的发送；不影响按原生ID读取和群摄入 | 保留拒发；先完整展示原因。常规群沿用可信名称定位。无名群按第7节选择方案，禁止取首条搜索结果、拼接成员名或用预览昵称凑名称 |
| B02 / P0 | 明确拒发被降成 UNKNOWN；**真实链与AST复现证实** | 总线抹掉 `TARGET_NOT_FOUND`，现有 NativePort 失去前置拒发信息 | 总线、中转回执、南银发送状态 | 首批保留已定义的 `TARGET_NOT_FOUND` 及原错误，使现有 `_PRE_EXECUTION` 映射为 `failed`；不改共享结果schema，不泛化 `SEND_FAILED→failed` |
| B03 / P0 | 失败占位看起来已经发出；**截图与源码证实** | 列表、详情只显示方向，不展示状态与原生确认依据；占位预填 `source=chatlog` | 所有渠道的消息页及详情 | 方向与结果分栏；展示发送中、已由原生记录确认、结果待确认、失败。无原生ID不得用来源字段暗示实际采集成功 |
| B04 / P0 | 状态变化可能不刷新；**源码证实** | 同ID消息事件被忽略；消息store未订阅命令终态刷新，DB改状态不能保证页面更新 | 当前列表、打开的详情、断线后恢复 | 同ID按新证据更新；命令终态关联刷新消息；保留筛选、游标与分页，不生成重复行，不允许旧事件覆盖较新终态 |
| B05 / P0 | 命令列表缺错误原因；**真实API与内存SQLite复现证实** | `list_commands()` 漏选三列错误字段，序列化因此不返回 `error` | 命令列表、排障页、依赖列表结果的调用方 | 补查询投影，验证列表与详情一致；该修正无需DDL。持久 `reason` 或新跨端字段另审合同，不能单端添加 |
| B06 / P1 | 空contact遮蔽有效room名称；**纯离线证实** | 精确匹配contact后立即返回，即使名称为空也不继续查同ID群资料 | 同时存在contact与room且名称分布不同的群 | 仅在同一稳定原生ID内合并可信非空名称；名称冲突和跨ID重名仍拒绝。本群两来源都空，单修此项不足以恢复发送 |
| B07 / P0 | 旧/非精确读回可能被确认；**纯函数反例证实，非本次原因** | 无效时间放行、正文子串匹配、未要求发送前水位前进、本人标志truthiness | 微信文本确认；图片确认也有身份不足风险 | 严格本人标志、有效时间、新原生身份/序号、正确会话、规范化正文精确匹配联合确认；证据不足保留未确认。图片另核媒体身份，不只看type |
| B08 / P2 | 鉴权OK易被误读为发送成功；**本次审计证实** | 同一action下鉴权和执行各写一行，阶段语义不显著 | 运维日志、审计检索、告警展示 | 明确展示鉴权/执行/原生确认阶段并关联trace；不改写旧审计，不把入口OK做业务成功统计 |
| B09 / P1 | 停机积压造成长延迟和陈旧目录降级，现场原因不可见；**实际积压与量测证实** | 常规消费每轮目录8、好友4、业务30，元数据与业务共用处理周期；缺少积压和年龄解释 | 启动恢复、账号可用性提示、收消息时效、回执可见性 | 采用现有上限内的有界自适应批量（最多200），保证元数据/ACK公平调度，显示pending、最老事件年龄、目录年龄与追平状态；保留TTL fail-closed、下载日志、认证与UNKNOWN语义 |

### 关键源码定位

| 问题 | 源码入口 |
|---|---|
| B01、B06、B07 | [目标名称与读回函数](/mnt/c/Users/anlin/Desktop/work/winagent/src/qtrade_winagent/win/wechat.py:70)、[真实发送路径](/mnt/c/Users/anlin/Desktop/work/winagent/src/qtrade_winagent/win/wechat.py:1060) |
| B02、B03 | [适配器保留404](/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/adapters/wechat/adapter.py:247)、[发送占位及失败映射](/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/bus/bus.py:308) |
| B02跨端承接 | [现役NativePort](/home/anlin/.local/state/xunjia-negotiation-implementation-20261023/candidate-0038/app/relay/qtrade_native_port.py:156)、[南银回执合并](/home/anlin/work/qtrade/ibquote/src/njcb_deposit_agent/wechat_collect/hub.py:303)、[南银状态文案](/home/anlin/work/qtrade/ibquote/web/src/pages/WechatCollectPage.vue:32) |
| B03、B04 | [消息页](/mnt/c/Users/anlin/Desktop/work/console/src/pages/msg/MsgPage.vue:84)、[同ID事件更新](/mnt/c/Users/anlin/Desktop/work/console/src/stores/messages.ts:107)、[消息状态持久化](/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/store/store.py:914) |
| B05 | [列表查询投影](/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/store/store.py:1239)、[结果序列化](/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/api/serialize.py:129) |
| B08 | [WinAgent鉴权审计](/mnt/c/Users/anlin/Desktop/work/winagent/src/qtrade_winagent/svc.py:160)、[发送入口](/mnt/c/Users/anlin/Desktop/work/winagent/src/qtrade_winagent/svc.py:780) |
| B09 | [目录消费](/home/anlin/work/qtrade/ibquote/src/njcb_deposit_agent/wechat_collect/hub.py:154)、[业务tick](/home/anlin/work/qtrade/ibquote/src/njcb_deposit_agent/wechat_collect/hub.py:343)、[采集端快照发布](/mnt/c/Users/anlin/Desktop/work/src/qtrade_agent/ossbox/institution.py:239) |

NativePort链接是本次已核验的**对端运行快照**，不代表本报告获得该对端源码或协议的写权限。首批修正应在本仓保留已有错误码；如确需增加跨端证据字段，先冻结双方schema/hash、明确所有权并形成共同合同。

## 4. 新文件网关是否为卡点

本轮主要等待来自停机期间累计的目录346份、业务上行3937份积压。目标7条消息在采集端均已真实上传，南银当时仍在顺序消费较早文件。通过既有认证消费流程追平后，业务pending为0，目标7条均落ODS并完成Dify；未清队列、未跳过认证、未将旧UNKNOWN强制改状态。

| 实际操作 | 样本与结果 | 时延 |
|---|---|---|
| 下载 | 64个真实文件，全部HTTP200，零错误 | 中位327.17ms，P95 376.26ms |
| 正常ACK上传 | 1次，HTTP200 | 415.77ms |
| pending查询 | 3次，全部HTTP200 | 中位294.00ms，P95 515.36ms |
| 整批处理 | 应用64条，无合成上传 | 总7.672s；poll 4.882s，本地apply 2.211s |

以上是客户端观测，包含网络、TLS、服务响应及客户端处理。**本轮没有持续上传/下载失败或单个请求卡死的证据，但不能据此排除其它时段慢请求，也不能把每次约0.3—0.4秒全部算成服务端处理耗时。** 历史握手错误和UNKNOWN仍保留。目录TTL过期时停止相信账号在线是必要保护；应改善追平和原因展示，而非取消TTL或伪造在线。

量测见[真实批次原始记录](/home/anlin/.local/state/nanyin-group-parse-20261026/relay-measured-batch.json)及[独立重算](/home/anlin/.local/state/nanyin-group-parse-20261026/live-verify/relay-measurement-recheck.json)。B09应先在隔离队列验证调度，再评估是否需要服务端内部追踪；不预先承诺服务器改造。

## 5. 拟实施批次与放行门

以下均未实施。每批先由独立测试方固定现状反例，再由实现方修正，验收方用冻结输入核验；失败时不改门或跳过用例。

| 批次 | 文件与责任边界 | 交付行为 | 独立放行门 |
|---|---|---|---|
| P0-A：结果语义与可观察性 | `src/qtrade_agent/bus/bus.py`、`store/store.py`；`console/src/pages/msg/MsgPage.vue`、`console/src/stores/messages.ts`，必要的已有DTO承接；审计展示只在现有字段内区分阶段 | 保留明确前置拒发码；列表/详情呈现同一失败原因；方向、状态、原生证据分开；同ID状态更新可见 | T01—T06；适配器→Bus→现有NativePort→南银的整链测试，不只测适配器；前端单测、类型、lint、隔离浏览器 |
| P0-B：原生确认保护 | `winagent/src/qtrade_winagent/win/wechat.py`、调用方 `winagent/src/qtrade_winagent/wechat.py`；必要时核对适配器读回合并 | 发送前按账号与稳定会话取得原生水位，读回必须有较新序号及未使用原生ID；不以请求正文、旧同文或类型标签制造成功 | T07—T11；Windows原生测试与隔离无写反例先通过；确认迟到、重复消息、并发人工发送仍不误确认或重发 |
| P1-A：群定位 | 同一原生解析模块；是否增加无名群能力由第7节决定 | 修空contact遮蔽room；名称冲突/同名群拒绝；无名群没有可信映射时继续前置拒绝 | T12—T14；仅修名称来源缺口不能声称本群恢复；无名群能力须另过“稳定群ID与UI会话唯一对应”门 |
| P1-B：积压恢复与提示 | 南银 `wechat_collect/hub.py`、已有状态API和`WechatCollectPage.vue`；共享传输默认只读 | 在现有poll上限内自适应批量；元数据/ACK不饿死；展示积压和陈旧原因，保留选群、身份及顺序语义 | T15—T18；断网/重启/积压下不丢、不错路、不重复业务摄入，UNKNOWN不被清空；现有8线程和200上限不得绕过 |
| P2：诊断和文档收口 | 现有审计序列化/展示、规格文档及交接 | 鉴权、动作、原生确认分别可查；按批准结果登记必要裁决与责任边界 | 同trace阶段核对、脱敏检查、规格对账；所有变更有来源与验证范围 |

发布前后要求：

1. 冻结两库分支/HEAD/dirty、涉及文件原字节与换行、当前EXE与配置摘要、peer schema/hash和进程身份；保留他人修改。文档写入不构成部署授权。
2. 做一致性备份时保留SQLite WAL及其它数据库的一致性语义，不复制运行中的主库单文件冒充完整备份；备份不得把凭据或聊天正文纳入公开报告。
3. 不清除历史消息、已发回执、UNKNOWN、下载日志、登录态或机构绑定。前端在ext4独占副本构建；Windows脚本只用ASCII，经Parser校验；只用本机PowerShell，必要提权走正常gsudo/UAC。
4. 本地更新须先确认无在途原生发送，使用精确进程/文件身份与可恢复前像；只更新受影响组件，不重启WSL、Docker或无关服务，不启动安装器。
5. 回滚恢复本批软件/配置前像；不得用回滚覆盖发布后产生的业务消息或回执。新增持久语义若无法向后读取，须先单独评审迁移与回退方案。
6. 真实验收必须使用新command_id和新幂等键，且由安琳指定目标与时机。**不重发本条历史指令，也不复用旧UNKNOWN的键。** 当前只批准诊断，真实发送须待方案及该动作范围确认后开展。

确认门的具体口径：水位须绑定当前账号身份与稳定会话，不能取别群最大序号；序号重置或身份变化须重新建立基线，无法取得可信基线时前置拒绝。正文默认逐字相等，仅可复用现有采集合同明确允许的表示层转换，禁止增加trim、子串或删除片段式宽松匹配。消息页面更新应比较已有事件序号或重新获取权威持久状态，不能按客户端到达先后覆盖；若缺少可比较版本，回查而不是推断终态。

## 6. 测试矩阵

这些是实施阶段待执行的验收要求，不是本轮已通过数字。除下文已标出的纯离线复现外，不以测试计划冒充执行结果。

| 编号 | 场景 | 必须观察到的结果 |
|---|---|---|
| T01 | 本次原生前置拒绝：群无可信搜索名 | 正文输入/发送动作0次；`TARGET_NOT_FOUND`经过适配器、Bus、NativePort、南银仍是明确拒发；消息页显示失败原因 |
| T02 | 原生动作已尝试但没有确认、响应丢失、调用中断 | 保持`unknown/UNCONFIRMED`；同command ID仅查询或对账，不再次调用发送 |
| T03 | 正常真实发送且读回证据齐全 | 账号/稳定会话/正文/新原生ID匹配后才`DELIVERED/sent`；原生群可见，接收侧或原生数据库证据齐全；占位合并一次 |
| T04 | 同ID从SENDING到FAILED、UNCONFIRMED或DELIVERED | 列表和已打开详情实时更新；方向不变、结果可辨，无重复行，筛选/游标不重置 |
| T05 | 断线恢复、旧事件晚到、命令终态先于消息事件 | 不用旧事件把较新终态回退；按既有重连规则补齐状态；不因此触发业务重发 |
| T06 | 同一失败分别走命令列表与详情 | `error_message/retryable/needs_human`一致；鉴权OK不能替代执行结果；列表SQL修正无需DDL |
| T07 | 无时间、非法时间、非有限时间或未超过发送前水位的旧同文 | 均不能确认新发送；有效旧消息仍只保留原事实 |
| T08 | 正文子串、不同会话相同正文、`isSelf="false"`或非法类型 | 不确认；首批按原文逐字比较，不自行trim、大小写折叠或子串匹配。确需表示层转换须引用既有合同，否则单列确认 |
| T09 | 同正文连续发送、同时人工发送、原生消息乱序或重复 | 每条指令只绑定可证明属于本次的新证据；无法区分则保持未确认；重复入库/重复发送均为0 |
| T10 | 原生读回延迟、临界超时、超时后迟到 | 在原预算及状态合同内收敛；迟到真实证据可更新确认，未有证据不得猜成功；不自动重试 |
| T11 | 图片/文件返回相同类型但媒体身份不一致 | 不凭type=3/49确认；所需媒体证据不足时保持未确认，不能把文本确认通过推广为媒体通过 |
| T12 | contact名称空、同ID room有可信唯一名称 | 能取到该room名称；已有联系人的优先级和大小写字段兼容不回归 |
| T13 | 双来源名称冲突、同名不同群/联系人、目标不存在 | 明确前置拒绝，输入/发送0次；不得选择第一项或预览昵称 |
| T14 | 无名群映射、两个群标题及成员完全相同、后续成员/标题/账号变化 | 若批准扩展，映射须绑定稳定群ID并能失效；同标题同成员仍不能唯一验证时拒绝；不自动改群资料 |
| T15 | 目录346份、业务3937份量级的隔离积压 | 有界追平，元数据/ACK/业务均推进；页面区分陈旧目录和真实离线；不取消TTL |
| T16 | 下载成功后落盘/应用/ACK各边界中断 | 从既有journal恢复；持久前不ACK，重复交付不重复ODS；不清UNKNOWN或伪造已应用 |
| T17 | TLS失败、超时、服务端4xx/5xx、网络恢复 | 准确分阶段显示错误、待确认和队列年龄；不以HTTP200或pending减少代替业务完成 |
| T18 | 双工作台/双账号/非选定群、切换群选择与并发刷新 | owner/账号/工作台/peer隔离不变；非选群不进解析，已保存选择不被seed覆盖，旧context不执行新命令 |
| T19 | 目标群真实in/out与Dify分类回归 | 原生事件→ODS身份→本地run→实际LLM节点→shim上游逐条关联；不预筛正文，不制造样例；自身消息不丢，失败发送占位不得冒充原生消息 |

建议补门位置：既有 `tests/test_wechat_adapter.py` 已覆盖适配器404，但缺其经过Bus后的错误码承接；由独立测试方补总线/NativePort整链反例。原生新增门放在 `winagent/tests/`，前端状态与更新门放在 `console/tests/unit/` 和隔离浏览器用例中；南银调度和回执门放在其 `tests/test_wechat_collect_*` 邻接范围。实施方不能为了绿灯修改这些门。

## 7. 请安琳确认的决策

| 决策 | 建议 | 需要确认的边界 |
|---|---|---|
| 是否分批整改 | 按P0结果语义/显示与确认保护，再P1群定位和积压恢复推进 | 各批通过独立门后再进入受控本地更新；不把方案确认等同任意真实外发授权 |
| 本次无名群如何支持发送 | 在可信映射形成前继续拒发并显示明确原因 | A：安琳自行设置唯一可搜索备注/群名后沿用现有路径；B：追加无名群稳定ID↔UI会话映射能力，并先验证可行性。助手不自动改名，不把成员名称拼接当身份依据 |
| 本条历史UNKNOWN如何处理 | 保留原记录，追加关联此次确证“正文输入前拒绝”的审计说明 | 是否进一步授权对**这一条**做带前像、审计和一致性验证的定点状态修正；不批量改历史UNKNOWN，不自动重发 |
| “所有群消息”的媒体范围 | 已落实真实文本/图片的双方向摄入规则；内容由Dify判断 | 若还要求语音、视频、文件、链接卡片等类型进入解析，需另明确各类型处理合同；本次没有这些类型的通过证据 |

无名群能力目前仍缺实施前证据：UI是否暴露可可靠读取的稳定群ID，或能否取得其它能唯一绑定该原生群ID的证据。**两个群可能标题和成员都相同，标题加成员仍不能自动视为唯一身份。** 若无法证明，B方案不能以“搜索命中看起来相似”放行。该技术调查可纳入批准后的第一步，但不提前承诺发送可恢复；P0的状态与错误语义修正也不会让本无名群自动可发送。

## 8. 本轮已完成事项与仍未验证范围

在安琳提出本次只诊断要求之前，已按“选定群所有真实消息，包括自己发送”修改南银唯一方向过滤点。独立测试固定RED为9失败/13通过；修正后同一22项与26项既有邻接合计48通过、零失败/错误/跳过。私聊出站、非选群、身份、去重、修改/撤回、附件完整性保护保留；未为此新增自动发送。

实际群里的7条文本（5入站、2自己发送）均唯一落入ODS 1509—1515，原文摘要一致；7个本地Dify运行均成功，实际分类节点1005的模型均为`dify-test-proxy`。每条的system/user/output三份摘要与且仅与1个shim上游成功运行匹配，确证调用了`dify-test.frontnode.net`。结果全部为`SKIPPED_NOISE`，来自Dify判断，没有正文预筛或特殊样例。[独立解析报告](/home/anlin/.local/state/nanyin-group-parse-20261026/live-verify/REPORT.md)

这7条消息时间为17:32—17:34，**不包括18:03失败发送**。本次没有触发正式报价结构化抽取节点1007，也没有实际运行图片转录节点1003；仅核到已发布三个LLM节点都配置为shim。语音、视频、文件等类型、长期运行、跨机器与完整安装均未验。`usage=0`是shim既有占位，不能当作模型费用或用量依据。

## 9. 证据索引与本报告验证

| 证据 | 支持内容 |
|---|---|
| [原生诊断报告](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/native/REPORT.md) | 群名称缺失、原生拒绝、空contact遮蔽及读回反例 |
| [离线读回反例](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/native/offline-readback-probes.json) | B06、B07，未执行原生发送 |
| [根线程复跑的AST探针](/home/anlin/.local/state/nanyin-send-diagnosis-20261026/offline-receipt-probes.json) | 明确拒发码与UNKNOWN映射、命令列表/详情错误字段不一致；只用源码AST和内存SQLite |
| [独立方向回归](/home/anlin/.local/state/nanyin-group-parse-20261026/direction-tests/REPORT.md) | 已批准的in/out过滤修正及48项限定通过 |
| [真实解析汇总](/home/anlin/.local/state/nanyin-group-parse-20261026/live-verify/summary.json) | 7条原生消息、ODS、Dify、shim的逐条对应 |
| [配置变更保护机制](../../.codex/CONFIG_CHANGE_GUARD.md) | 所有权、身份、UNKNOWN、备份及独立验收约束 |

证据原件位于本机受控目录，不复制凭据、群成员姓名或无关聊天正文到本报告。修复计划的测试尚未执行；本轮执行的是只读取证、离线缺陷复现及前阶段限定验收。

本报告仅新增文档及同内容证据副本，未变更产品、测试、规格裁决或运行状态。文档验证通过本机PowerShell调用WSL，在`docs`目录执行带60秒超时的`python3 check-truth-tables.py`；结果与链接/空白校验记录保存在诊断证据目录。最终退出码以该目录的`report-validation.json`为准。

**停止点：等待安琳确认第7节决策；确认前不实施上述整改，不重试历史指令。**
