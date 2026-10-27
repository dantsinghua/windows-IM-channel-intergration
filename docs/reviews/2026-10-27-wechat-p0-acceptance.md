# 微信发送 P0：受控文本链验收（2026-10-27）

安琳，本轮结果为 **PASS：P0 修复、受控本地更新、微信群通讯录刷新，以及 Greydan 指定群一次新文本指令的真实链路均已完成**。attempt4 的原生记录、QTrade 唯一绑定、南银 `sent`、文件网关投递、ODS、Dify 和 shim 上游有逐项关联证据。此结论限于本次指定群、单条文本，不代表全部媒体、长期运行或安装验收通过。

本报告承接[原诊断与整改方案](2026-10-26-wechat-send-diagnosis-and-remediation.md)和[配置变更保护机制](../../.codex/CONFIG_CHANGE_GUARD.md)。原方案的“仅诊断、待确认”是授权前历史；安琳随后批准 P0、通讯录刷新及 Greydan E2E，并明确追加群名称元数据和合法独立人数标题支持。P0/T10 迟到确认遗漏在本轮补齐。P1 发送目标名称 fallback、积压调度改造及 P2 审计重构未整体实施。

## 1. 基线与分工

本轮读取与验收绑定 QTrade `main` / `f4d915fb89ac197ff4245b39a91cdc5a813794c2`；原 dirty 保留，不能用 HEAD 代替候选字节。证据根为 `/home/anlin/.local/state/qtrade-wechat-p0-20261026`，下文简称“证据根”。原始八文件前像、摘要、换行和差异见 `before/`、`baseline.json`、`preexisting.patch`；追加阶段各自保存前像并在修改前 CAS 核对。

结果/UI、原生及迟到 proof、Bus/Store 实现分别按分配文件负责；开发者测试、旧合同 fixture 维护、独立组合验收、只读现场验收和本次文档分别承担。实现者不修改质量门；总控负责授权、集成与现役操作。本次文档仅新增本报告、更新本轮交接并给旧方案增加历史提示，不改产品或测试，不提交、推送。

## 2. 已交付行为与合同承接

基线入口为[00 共享基线](../00-共享基线与口径.md)、[02 后端设计](../02-后端与本地数据库设计.md)的 §2.8.1、WinAgent #38/#39/#41，以及[06 消息存取](../06-邮件摆渡与消息存取.md)的 §2.12。本节登记本轮已批准、已实现的具体承接；不新增 R6 编号，也不宣称 `docs/00～07` 已全部同步。旧的通用正文/时间窗合并描述不能用于放宽下列微信精确确认门。

| 原问题 / 接口 | 本轮行为 | 保留边界 |
|---|---|---|
| 明确前置拒发被降级 | Bus 保留 `TARGET_NOT_FOUND` 及原错误；列表查询补现有三列错误字段 | 不把所有 `SEND_FAILED` 当确定失败，不改对端共享结果 schema |
| 消息方向掩盖失败、状态不刷新 | 方向、发送状态、原生证据分开；同 ID 按序号更新、命令终态权威回查、防旧 REST 覆盖；保留筛选/游标/详情与去重 | 手工原生消息显示“已采集原生消息”，不称程序发出或对方已读 |
| WinAgent #41 群目录名称 | 增加 `display_name`，按稳定原生 ID 从真实 contact/room 备注或昵称取名；群名不使用会话预览发言者，Agent 目录优先承接此字段 | 元数据读取有界，未覆盖/缺名群回退稳定 ID；目录名称修复不等同发送目标 resolver 的 P1 fallback |
| 群标题带独立成员数 | 群模式接受唯一精确群名，或精确群名加唯一合法独立人数节点 | 保留同根、进程、页面、master 祖先、可见性、叶节点、矩形及不同 runtimeID；私聊、重名、多 count、未知文本仍拒绝 |
| WinAgent #38 即时/迟到证明 | 可选 `confirmation_proof` 只来自同次真实发送前 baseline 与完成输入证据，由 Service 绑定本请求幂等键；无可信快照、身份/anchor 异常或多候选不签发 | proof 不是送达结果，不含正文、凭据或路径；原有无 proof 响应仍可读取，不能补造旧 proof |
| 微信真实读回 | adapter 不用请求正文合成原生消息；严格 UID/PID/epoch、账号/会话、anchor、时间、完整正文 SHA、唯一新 seq；Store 按原 trace 精确绑定 | 不按子串、规范化正文或时间邻近猜发送归属；缺媒体身份不确认 |
| 持久迟到确认 | v2 settings 保存原 proof；后续只读查原动作窗口的新鲜唯一原生事实。绑定与 `confirmed` 同事务 CAS，`invalidated` 不能被旧快照覆盖 | v1 exact-ext 保持兼容；v1 无 ext、无 proof 的后来同文不能确认。临时读失败/不完整扫描保留 pending，明确失效保留原因 |
| 同键恢复与未知结果 | DONE 同键可返回原 trace 已确认的只读视图，不改历史结果、不发送；ABANDONED、关闭和总等待超时保留幂等键；`native_inflight` 未结束不报空闲 | 旧 UNKNOWN 不回填、不重发；持久 pending 有界公平轮询，防前几条缺证造成饥饿 |
| 诊断字段 | #38 的 `confirmation_reason` 与 CommandResult.data 的 `native_confirmation_reason` 仅承接七种值 | `confirmed`、`confirm_timeout`、`identity_changed`、`anchor_changed`、`ambiguous_candidate`、`invalid_snapshot`、`read_failed`；拒绝任意字符串/对象，不改顶层状态合同 |

确认语义仍是“本次输入完成后，在同账号/稳定会话观察到唯一新鲜精确原生记录”。微信没有命令因果 ID，单条人工同文巧合不可观测；本次不证明协议级因果、对方已读或所有并发情形。

## 3. 最终组合隔离验收

[独立组合报告](/home/anlin/.local/state/qtrade-wechat-p0-20261026/full-qa-proof-r2/REPORT.md)及 [RESULTS.json](/home/anlin/.local/state/qtrade-wechat-p0-20261026/full-qa-proof-r2/RESULTS.json)判定 PASS。冻结输入 837 份，集合 SHA256 为 `c948bf3baf95c72343f7daf718bf183efb7adde67984190a036462d83786b130`；验收终点主树、冻结副本、Windows 副本、协议源、peer 和复用产物零漂移。文档收口在该冻结验收之后进行，不冒称更新后的三份文档属于当时快照。

| 门 | 结果 | 实际来源 |
|---|---:|---|
| Agent | 3709 passed | `full-qa-final` 新执行，proof-r2 按未变输入复用 |
| Windows WinAgent | 1304 passed | `full-qa-proof-r2` 单次完整新执行 |
| HTTP/WS | 127 passed | `full-qa-final` 新执行，proof-r2 按未变输入复用 |
| 前端单测 | 652 passed | `full-qa`；后续相关输入未变，按摘要复用 |
| Playwright | 44 passed | `full-qa`；后续相关输入未变，按摘要复用 |
| 类型 / lint / 构建 / 文档真值表 | 各 exit 0 | 原执行及相关输入、20 份复用产物摘要逐一核对 |

各测试门均零失败、零错误、零跳过；不同轮次没有相加为“一次全量”。保留 Agent 6 条依赖弃用 warning、前端 lint 37 条 warning 和既有构建 chunk 提示。

上一轮 Windows 1303 通过/1 失败完整保留。原 20ms 正向夹具未保证可信快照已完成；独立方将三个正向测试改为确定性完成必要前提后推进原截止，保留原 11 条断言并增加 5 条预算/次数/截止断言。产品、原预算、负向门及 `NativeRig` 未改，不使用 skip/xfail、盲重试或扩大 timeout。受控探针证明缺可信快照时拒签合法，不冒充复原原失败当刻的线程延迟；证据见 `full-qa-proof-r2/fixture-review.json` 与原 `full-qa-final/`。

## 4. 现役身份与源码

现役受控更新后的只读核验见[现场独立报告](/home/anlin/.local/state/qtrade-wechat-p0-20261026/final-live-proof4/REPORT.md)。147 份 Agent 源匹配主树及 `runtime-proof`，61 份前端产品源匹配；User EXE 41/41、svc EXE 37/37 内嵌模块匹配。svc 未内嵌的 4 份用户模块未计作匹配。

| 项目 | 本次验收快照 |
|---|---|
| 运行入口 | API PID `63356`，`runtime-proof/source`；Vite PID `37754`，`runtime/console`；Electron PID `620`（总控部署记录） |
| Windows | SCM PID `56904`、User PID `24372`；真实微信客户端未重新登录 |
| svc EXE SHA256 | `bdc006d39a903fd94024f145ce1a7b09c167857de279c8b971fd04c26af87290` |
| User EXE SHA256 | `2b94f17666b356655a1a9687fe2b7d5b06fa17bbd694516f74a9b5dd8cb60953` |
| Win 原生源 | `55cb7da214aa34a6c7dbb18150895b6dde61b4e09af930291fc18f0528efe73e` |
| Win Service 调用层 | `79b712146146483f3e6422991d0b6c7c0039aa7297b1b1605bd037e18a127d9c` |
| adapter / Store | `c73bab34b7084882ceb134d2efb6b25ba058562de8911fc2d47b113e1767d471` / `5e825d7e8ff8fd4ef89fa43a34981696c2327f6d1775706c623155dbe194f0e1` |
| Bus / app | `e261f5af9f36d5df5c8f62a3c88d176c2cb1be20feaca1856386ec7b4e94da5e` / `529da937f868c66f2e9d208200230818e01c32563d93f957f42c8c81dc737237` |

配置与 `shared_internal` 未改，正式 `C:\ProgramData\QTrade` 不存在。00:28:40—00:29:22 三次健康、鉴权与 WS 连接检查通过；该观察区间不代表长期稳定，序号未变也不证明持续新事件流量。PID 仅为验收时快照，续接需重新核实。

## 5. attempt4 真实闭环

通过测试并刷新、核对目标身份后，使用新 command ID `8b0781a8-1e48-4a1e-b8a6-501ac7c11ec5`，向 `Greydan / 45686445455@chatroom` 单次发送非交易文本。标记为 `Greydan P0 E2E 20261027-002720`，正文 SHA256 为 `1e2125bdad98353b32c73ce925a977d6e2d368e256a79bb26d3041218deca2a1`。

| 链路 | 独立直接证据 |
|---|---|
| 原生事实 | 目标群查询精确正文仅 1 条，`isSelf=true`，seq `1793032062000037`，时间 00:27:42 |
| v2 确认 | 持久证明 `status=confirmed`；原 baseline `1793024476000036`，新 seq 严格前进，UID/epoch/talker/完整正文摘要/幂等键一致 |
| QTrade | 原命令与 ledger 均 `done/DELIVERED`；唯一消息 `msg_01M5WGKJ7GSZXN0YD7WECKKQ86`，trace `01M5WGKJ7FRBT56D5SVN1XXEXX`，`confirmed_by=chatlog`，无重复占位 |
| 南银 | `sent`，对应相同原生群/seq；`attempted=1`、`submitted=1` |
| out 与传输 | `producer.source_command_id` 精确等于本 command ID；OSS route `uploaded=1`，`fg_upload=UPLOADED`，delivery `attempts=1/acked=1`，consumer `acked=1`，正式事件映射 `persisted` |
| ODS | email `1558` / message `1517`，原生 identity 与正文摘要一致，`SKIPPED_NOISE`，无 parse_error |
| Dify / shim | Workflow `0f32d642-5b2a-4e68-8c35-b47d052d50b5` succeeded；实际 LLM 节点 `1005`、模型 `dify-test-proxy`；shim 上游 run `b6fa5897-a42d-4227-bc0c-36e86af074d7` succeeded，system/user/output 三摘要同时唯一匹配 |
| 实际消息页 | `#/msg` 目标会话唯一行显示“发送 / 已由原生记录确认”；DOM/store 的 msg/trace/ext 与 ledger 一致 |
| 群目录 | 原生 #41 与南银目录均为 `Greydan` 且稳定 ID 一致；消息元信息仍显示稳定 ID，保留这一展示边界 |

证据为 `final-live-proof4/` 下 `chain-summary.json`、`binding-ledger.json`、`native-chatlog.json`、`nanyin-current.json`、`chain-map.json`、`parse-progress.json` 与 `ui-render.json`。`tx_event` 全局 `uploaded=0` 原事实另列保留，投递结论依据实际路由标志、原生 ACK 和下游落库共同成立；不拿某一个标志替代全链。截图操作超时未取得 PNG，UI 结论限于实际 DOM/store，不宣称像素截图验收。

## 6. 保留的失败、历史与未验

| 阶段 | 保留事实与处理 |
|---|---|
| 原 `hello` | 无可信搜索名称，输入前拒发；原失败与南银 UNKNOWN 不改写 |
| attempt1 | 合法群人数标题未被识别，前置拒发；经独立反例修复批准范围 |
| attempt2 | 实际发出，但当时缺原始 baseline/proof；UNKNOWN 保留，不事后补造、不重发 |
| attempt3 | 额外图片 `PreviewWindow` 触发安全拒发，原生匹配 0；总控核实无编辑后精确关闭普通查看窗，未放宽窗口识别门 |
| attempt4 | 新 ID、新幂等键单次实发成功，独立报告 PASS；不是前三条的重发 |
| 好友目录追平 | 2971 份旧快照经同锁及原认证 journal 消费追平，零新增 ambiguous；未清队列，未实施 P1 调度改造 |

所有阶段红灯、备份和失败记录保留。各阶段前像与报告见 `group-name/`、`group-header/impl/`、`acceptance-contract/`、`late-contract/`、`late-proof/{impl,bus,tests}/`；其中原子 CAS 与诊断门见 `late-proof/tests/ATOMIC-REPORT.md`。只读 review、独立测试和最终验收分开，早期限定 GREEN 不替代最终冻结或现场证据。

原诊断记录的 7 条真实群文本是 2026-10-26 17:32—17:34 的另一个样本（5 入站、2 自己发送，ODS 1509—1515）；不包括当日 18:03 的失败指令，也不包括后续 attempt1～4。本文 attempt4 以独立 command、原生 seq 和 ODS1517 标识，不把早期接收/分类成功当作本次发送成功。

本次非报价测试文本经实际节点判断为噪声，未运行正式报价抽取节点 `1007`、图片转录节点 `1003`，不推广到语音、视频、文件、其它媒体、并发人工同文、真实跨重启恢复、长期稳定、跨机器、完整安装或客户现场。`usage=0` 不是费用证据。旧 UNKNOWN 全部保留，本批不新增后续自动重试任务。

## 7. 文档验证与停止点

本轮文档门通过本机 `powershell.exe` → `wsl.exe -d Ubuntu-24.04`，在仓库 `docs/` 执行带 60 秒超时的 `python3 check-truth-tables.py`；最终退出码、链接存在性、三文件空白检查及旧方案正文保留检查记录于证据根 `final-doc-validation.json`，原始真值表输出为 `final-doc-truth.log`。本文无新增代码示例；产品测试结论引用独立原件，文档方未重跑产品或真实发送。

停止点：本次批准的 P0、追加群名称/标题支持、通讯录刷新与指定文本闭环已完成。剩余媒体、长期运行、P1/P2 和安装验证不属于本次通过范围，不以这些未授权扩展阻止本批收口。
