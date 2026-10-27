# 动态槽位与微信复测（2026-10-27）

安琳，本批动态槽位与微信超时分类修复已部署并验证：动态槽位独立合成回归 **165 项**及只读终审通过；微信修复 Windows 定向 **152 项**、全套 **1314 项**通过。企点/QQ 双向真实文本通过，Greydan 一条新指令的原生发送、回执、采集上传、ODS、Dify 和 shim 完整闭环获独立 **PASS**。该消息在本地 Dify 网关恢复后按原重试机制解析成功；不是首次解析即成功。

**仍有两项明确未闭合：**共享 UNKNOWN 对未归属自发消息的暂扣，与“所有群消息进入解析”存在合同冲突；Dify 启动时错误上游缓存的长期治理未实施。不能把本批成功外推为全部群消息、所有功能或未来重启均无问题。12:49 首轮完整链 **FAIL** 的历史证据不改写。

本报告遵循[配置变更保护机制](../../.codex/CONFIG_CHANGE_GUARD.md)，承接[凌晨 P0 验收](2026-10-27-wechat-p0-acceptance.md)。证据根为 `/home/anlin/.local/state/qtrade-resume-20261027`，以下简称“证据根”。所有运行 PID、槽位和结果仅代表对应采样时点。

## 1. 目标、基线与边界

安琳要求启动本地采集端，继续 `Greydan / 45686445455@chatroom` 收发与南银 Dify 解析；随后明确要求微信、QQ 按企点方式处理动态采集槽位。稳定账号身份与临时槽位必须分开：旧槽位停止、禁用或删除后，当前唯一活动来源仍应可用；多个活动来源冲突时必须拒绝，不能选择任意一个。

本轮主树为 `main`，HEAD `f4d915fb89ac197ff4245b39a91cdc5a813794c2`，已有大量未提交改动。只按各责任方前像和冻结摘要认定本轮增量，不把整个工作区差异归为本轮。

| 用户要求 / 旧行为 | 本轮处理 | 保留或未验边界 |
|---|---|---|
| 动态槽位不应固定在旧账号编号 | 按通道、稳定 UID 和唯一活动来源解析；QQ 连接增加代际及 epoch 校验 | 多活动来源仍拒绝；历史账号行不删除 |
| 保留已登录账号继续测试 | 登录后同一微信身份现为 `wx06`，目录唯一匹配 Greydan | 不是把旧 `wx05` 强行改成新槽位 |
| 群内所有消息，包括采集账号自己发送的消息，都进入 Dify | 南银原有双向群消息选择保持；发现共享 UNKNOWN 保护仍会暂扣部分出向事实 | 该共享协议冲突未闭合，不能宣称全部消息已进入解析 |
| 真实消息发送状态可信 | 使用新 command ID，核原生记录、命令、回执及下游 | 不重发旧指令，不把 accepted/pending/unknown 改成 sent |
| 原配置及对端所有权 | 地址、凭据、绑定、共享 peer 保持原样 | QQ 本轮只修已有本地通道，不新增文件网关协议支持 |

本地采集 API、桌面、WinAgent、南银与 shim 已启动；桌面鉴权、连接及事件流已取到正常证据。Dify 容器访问 shim 模型列表 HTTP 200，只证明连通性。初启文件网关装配失败后，Windows 服务就绪，沿用原配置重新应用恢复正常；当次内部异常未记录，不能仅凭恢复断定初启根因。启动证据见证据根 `HANDOFF.md`、`desktop-check.json`、`gateway-apply.json`。

## 2. 问题、根因与整改

| 编号 | 问题与根因 | 影响范围 | 整改及当前状态 |
|---|---|---|---|
| S01 | 原运行副本缺少主树已有的唯一活动源选择、目录选择及消费者迁移逻辑 | 企点、微信同 UID 保留旧停止槽位时，可能影响当前来源；旧消费者停止失败时不能贸然启新实例 | 主树通用逻辑已涵盖微信；候选按方法移植，避免混入无关工作区改动。终审通过，已部署 |
| S02 | 共享 NativePort 按全部未删除同 UID 行计数，与上层“唯一活动源”规则不一致 | 企点、微信的旧停止或禁用槽位可能让当前唯一活动源被判歧义 | 本仓 `_ActiveNativeAccounts` 只提供活动、启用、未删除来源，其余 Store 和 epoch 行为委托原实现；共享 peer 未改。独立回归通过 |
| S03 | QQ 旧事件闭包只保存槽位 ID，收到回调时重新取当前 session；发送及入库缺部分身份校验 | 同槽位更换身份、连接替换、跨 await 读回/历史，以及旧 start/stop 可能影响新连接 | 回调固定原 session；校验唯一活动 UID、epoch、原生登录身份；生命周期串行及代际保护；入库和游标附原生 UID。独立回归及当前企点/QQ 双向真实文本通过 |
| W01 | 外层 timeout 已触发，但 Windows 粗粒度 `loop.time()` 可能仍早于 deadline；原代码据再次读钟区分异常来源 | 正常确认超时可能误判 `read_failed` 并丢失 proof；反方向也可能把临界时刻的内部 IO 超时误判为可保留 proof 的外层超时 | 改用外层 timeout 的 `expired()`，确认预算与身份门不变。独立新增 10 例修前 3 红、修后全绿；Windows 全套 1314 项通过，已部署，新指令保留原 proof 并确认成功 |
| W02 | 同群存在 UNKNOWN 且原生出向消息尚无命令归属映射时，`rx_deferred` 持续 waiting，即使 deadline 已过 | 消息已被本地采集，但未进入传输队列、南银 ODS 或 Dify；与“所有群消息进入解析”冲突 | 共享协议未改。须区分采集事实与发送归属，保留 UNKNOWN、自动发送闸及人工接管规则；需对端合同和承接方共同验证 |
| D01 | 本地 Dify Nginx 缓存了旧上游 `198.18.0.41:5001`，实际 API 容器为 `192.168.210.9:5001` | 新微信消息虽已进入 ODS，调用本地 Workflow 时仍遇到 502 | 配置校验通过后平滑 reload，恢复当前连接；无配置、代理或 DNS 变更，无容器重启。尚未治理启动时的错误解析缓存，今后重启仍可能复现 |

S01/S02 不是本次真实指令失败的已证实原因：本次命令实际绑定 `wx06`，并成功发出。W01 与历史 `read_failed` 强吻合，但当次没有异常来源与分段计时，不能声称已经百分之百证明其为唯一原因。

## 3. 动态槽位已实施内容与测试

产品增量位于 [institution.py](../../src/qtrade_agent/ossbox/institution.py) 和 [QQ adapter](../../src/qtrade_agent/adapters/qq/adapter.py)。新增独立测试为 [test_dynamic_slot_channels_20261027.py](../../tests/test_dynamic_slot_channels_20261027.py)。实现、独立测试、只读终审、部署及本报告分别承担；文档角色不改产品或质量门。

[独立测试报告](/home/anlin/.local/state/qtrade-resume-20261027/dynamic-slot-tests/REPORT.md)记录 **165 passed、0 failed、exit 0，21.22 秒**：新增 40 例与既有 125 例；8 份输入运行前后零漂移。覆盖旧槽位停止/禁用/删除、新槽位 running/degraded、多活动来源拒绝、跨通道隔离、epoch 变化、旧消费者停止失败，以及 QQ 迟到事件、重连、读回、历史和过期启停。

初版 27 例的 19 个失败及后续 stale start 红例均保留。独立测试维护者修正了正向测试对 RPC 调用位置的限制，改为核发送恰好一次、目标及正文精确；同时按既有 OneBot 段数组合同修正预期。未放宽安全断言，未使用跳过或改产品迁就测试。详见 `dynamic-slot-tests/final.json` 与各轮原日志。

| 冻结对象 | SHA256 |
|---|---|
| QQ adapter | `49d9efb53d405cc8c3bb59d353fc9dd16dace4b453ea2d6734c043a0c23bc42b` |
| 主树 institution | `5c5278203236ec492e0581bbea61324cb6148f7cf923c891a9678c2e6a29af7c` |
| 新增独立测试 | `85f2c1a258ee611a2f5de069eccbed28ad4c57ee08d13190caed7ee3dfd04cf8` |

主树 institution 含其他既有改动，不能把其完整文件摘要冒充运行候选摘要。候选以此前验收源码为底，仅移植本轮指定方法与 QQ 文件。[只读终审](/home/anlin/.local/state/qtrade-resume-20261027/dynamic-slot-review/REPORT.md)为 **APPROVE**：相较此前副本只有两份产品文件变化，institution 的 AST 变化限定为四个动态来源方法及新增视图，没有混入主树其他组织信息变更；审查方针对精确候选再执行 **165 passed，11.77 秒，exit 0**。这与主树独立测试是不同运行，不相加为一次全量。前像见证据根 `dynamic-slot-impl/institution.before.py`、`dynamic-slot-impl/baseline.json`、`qq-slot-impl/adapter.py.before`、`qq-slot-impl/before.json`。

微信超时分类产品修改位于 [win/wechat.py](../../winagent/src/qtrade_winagent/win/wechat.py)：用原剩余预算创建外层 timeout 上下文，仅当其 `expired()` 且已有可信快照时保留原始 proof。内部读取超时、身份变化、anchor 变化及多候选仍拒绝 proof，不扩大预算或补造旧证据。

[超时独立测试报告](/home/anlin/.local/state/qtrade-resume-20261027/timeout-tests/REPORT.md)记录新增 10 例在修前 **3 failed / 7 passed**：早醒误判 1 例，内部 IO 超时正点/晚到被误判并签发 proof 2 例。修后 Windows 定向 **152 passed**，全套 **1314 passed**，零失败、错误或跳过；两次执行各自相关输入前后零漂移。既有计时 fixture 由独立测试所有者改为模拟真实的外层取消语义，**31 条原 assert AST 完全不变**，原字节及维护理由留存。产品 SHA256 为 `dace3d221952c9675c531a646060ad5cf340cc44b7fcfdf28ff6f4abc0450dd6`，保持原 CRLF。离线通过本身不证明部署或现场闭环，部署证据另列如下。

### 受控部署证据

[独立包验](/home/anlin/.local/state/qtrade-resume-20261027/timeout-release-review/REPORT.md)为 **APPROVE**：svc 37/37、user 41/41 个项目模块匹配冻结源码；独立追加 23 项定向测试通过，两个候选及其与现役依赖组合的四次隔离自检均 exit 0。依赖差异已逐项解释，获准只替换两个 EXE，保留现役 `_internal`。

[部署结果](/mnt/c/Users/anlin/qtrade-build/wechat-timeout-20261027/deployment-result.json)记录 2026-10-27 13:10:10（北京时间）更新成功，配置不变、共享依赖未替换。API 使用本轮 `runtime/source`，PID `3353`；SCM PID `48100`，用户代理 PID `48580`。桌面刷新后鉴权正常、API 可达、WinAgent 在线、事件流 open，见证据根 `runtime/process.json`、`postfix/desktop-refreshed.json`。

| 组件 | 已部署 EXE SHA256 |
|---|---|
| svc | `f3ab9a1cd4511693698a3ae24c47ee83163db0c4f1be08c040ee210b5d50568c` |
| user | `5591fdd9770dbc0eac49f6c15986540e20db5834ef1bac85808d1994d9c4d600` |

最终现场快照见证据根 `final-runtime.json`：API PID `3353`，源摘要零漂移，文件网关 active、无 errors；当前 `qd92`、`wx06`、`qq95` 均 running，QQ 连接为 `qq95`。新微信 command `b26e9fb9-50ae-4858-b54f-f959737208d7` 与 `qd92 ↔ qq95` 双向真实文本证据如下；结论依据逐段取证，不以提交状态或服务健康代替。

## 4. 本轮真实文本链：保留 FAIL

命令 `ae1d83f7-8a2d-46e1-be5e-ce4ea16fa460`，正文摘要 `1be422991e551fdfb687484fef470144a574854b74a6246412ccf8c09f076217`。完整证据见[独立现场报告](/home/anlin/.local/state/qtrade-resume-20261027/verification/REPORT.md)。

| 链路 | 本轮结果 |
|---|---|
| 账号及群 | 当前 `wx06`，南银仅选择目标群，目录名称 Greydan；无发往旧槽位的证据 |
| 到达采集端 | 12:50:23 登记 `native-execution-claimed`，QTrade trace `01M5XV3Y632YK6QB9AS70FPQ08` |
| 微信原生发送 | 12:50:35，seq `1793076635000038`，目标群精确正文唯一，`isSelf=true` |
| 确认及回执 | `SEND_FAILED/read_failed`，proof 数 0；南银 `unknown`，本次只提交、执行一次 |
| 消息显示依据 | 一条原生 `DELIVERED` 无 trace，一条本指令 `UNCONFIRMED` 占位；不能视为正确绑定或闭环 |
| 采集与传输 | host_event 已存在，但实际留在 `rx_deferred.waiting`；`published=1` 仅表示交给 deferred，不证明上传 |
| ODS / Dify / shim | 未找到本条对应的上传 ACK、ODS 或 Workflow，完整链路 FAIL |

[后续诊断补充](/home/anlin/.local/state/qtrade-resume-20261027/readback-diagnosis/REPORT.md)纠正了早期“首轮没有可信快照”的推断：Chatlog 在确认窗内 12:50:38、41、45 三次查询均成功且为毫秒级；当前完整只读快照探针用时约 2.546 秒。不能把窗口内查询慢、其后 UNKNOWN 留置或本次失败直接归为 `its-relay.yingmi.cn` 上传下载故障。

### 部署后的真实复测结果

企点与 QQ 先按既有稳定 UID 解析到当前唯一活动槽位 `qd92`、`qq95`，再各发送一次不同标记文本。

| 方向 | 本轮直接证据 |
|---|---|
| `qd92 → qq95` | trace `01M5XWBTKT3HZ5MFJGMFGRWXCH`，发送端 `done/DELIVERED`、`qidian_db/ingest_merge`；QQ 精确入站 1 条，来源 OneBot |
| `qq95 → qd92` | trace `01M5XWAR2AERN4N9PNZ8REGVSE`，发送端 `done/DELIVERED`、`onebot/get_msg`；企点精确入站 1 条，来源主库 |

证据：[正向](/home/anlin/.local/state/qtrade-resume-20261027/qq-live/forward-send.json)、[反向](/home/anlin/.local/state/qtrade-resume-20261027/qq-live/reverse-send.json)。独立验收方随后只读现役账本，确认两方向均为发送端精确出向 1 条、接收端精确入向 1 条，当前稳定身份和目标会话吻合，结果见 `postfix-verification/qq-live.json`。实发由主控执行，独立方未再次发送。这证明当前动态槽位双向文本链可用，不外推全部媒体或长期运行。

新微信指令由[修后独立现场报告](/home/anlin/.local/state/qtrade-resume-20261027/postfix-verification/REPORT.md)判定 **PASS**，13 项直接证据检查全部通过。正文摘要为 `232a2243cac2515f0ae40656ee4b97753d3a71ba0682abe8e4130d9a5ab560ff`。

| 新微信链路 | 独立直接证据 |
|---|---|
| 原生发送 | 13:11:28，seq `1793077888000039`；目标群精确正文仅 1 条，`isSelf=true` |
| 正确超时与迟到确认 | 用户日志 `confirm_timeout proof=True`；v2 proof `confirmed`，原发送前 `max_seq=1793076635000038` 严格前进，UID/epoch/talker/正文摘要/command 一致 |
| 命令与消息 | trace `01M5XWA5KXTCR36SCW4ATSBVNN`，`done/DELIVERED`；唯一绑定消息 `msg_01M5XWA5KYEG2K0F3VGQ6RY4DZ`，`confirmed_by=chatlog`，无同文占位重复 |
| 南银回执 | `sent`，`attempted=1/submitted=1`；证据指向同一原生 ext，目录与群选择正确 |
| 归属及传输 | `rx_native` 唯一映射新 command；event `qtrade-6674284c5653d547ee4495ad1ac419adc6e78e3fa5407756af184da702f63c5c` 精确携带 `source_command_id`；路由上传一次、delivery attempts1/acked1、consumer ACK1 |
| ODS | email `1559` / message `1518`，正常重试后 `SKIPPED_NOISE`、`parse_error` 为空；普通测试文本判噪声符合预期 |
| Dify / shim | Workflow `4b1db9d8-d159-4cd7-ba72-9159eb555ba4` succeeded，LLM `1005`、模型 `dify-test-proxy`；shim 上游 `d4802385-feaa-46e2-b738-15b1de46839c` succeeded，使用 `dify-test.frontnode.net`，system/user/output 三摘要同时唯一关联 |
| 实际界面 | 主控 Electron 采样唯一目标行 `DELIVERED/confirmed_by=chatlog`、确认文案可见；独立方核其 ID/ext 与账本一致，不冒称由独立方执行浏览器操作 |
| 历史保护 | 12:49 失败指令及旧 attempt2 仍 UNKNOWN，旧失败 event 仍 waiting，未为新指令成功改写或重发旧消息 |

本条消息入 ODS 后曾因本地 Workflow HTTP 502 而 `FAILED`，记录 `retry_cnt=3` 和原有 next_retry。主控查明 Nginx 旧上游缓存，校验配置后平滑 reload，保留原配置、代理/DNS 及容器生命周期。恢复后同一 message `1518` 按既有重试于 **13:13:45** 启动 Workflow、**13:13:47** 成功；没有另造原生消息或重发 command。失败原件与恢复过程见 `postfix-verification/parse-error.json`、`chain-map-before-parse-recovery.json`、`nanyin-current-before-parse-recovery.json` 及[网关恢复记录](/home/anlin/.local/state/qtrade-resume-20261027/dify-gateway-recovery.json)。

传输总字段 `tx_event.uploaded` 仍为 0 的原事实另列保留；上传完成结论依据实际路由标志、真实 ACK 与 ODS 共同成立，不能仅凭该总字段判断上传失败。完整新指令证据和界面采样归属见 `postfix-verification/RESULT.json`、`chain-map.json`、`parse-progress.json`、`native-lineage.json` 与 `postfix/message-page-prepared.json`。

这次 reload 是运行时恢复，没有修改 Nginx 解析或启动配置。以后重启若再次缓存 fake-IP，仍可能复现；长期整改应由 Dify 部署配置所有者落实启动后的 API 解析校验、后端就绪等待及正确 resolver，不能把本次恢复写成永久 DNS 修复或根因全部清零。

## 5. 未闭合项、待对齐点与后续测试

动态槽位、W01 修复及本次指定文本链已达到本批验收停止点。以下事项明确保留，不能算作完成：

1. **W02：未归属自发消息采集。**待对齐的是“只解析消费”的协议用途、授权与归属元数据。采集事实可以独立去重进入解析，但不能由此把 UNKNOWN 改为 sent、伪造人工消息或解除自动发送闸。后续精确归属更新不得重复解析。现有消费者可能依赖缺少 `source_command_id` 判断人工接管，不能简单删除 unknown 条件。须由共享协议所有者与南银承接方共同明确 `capture-only` 合同，再独立覆盖 UNKNOWN 并存、后续补归属、去重、权限和禁止自动补发。
2. **D01：Dify 启动解析长期治理。**本次平滑 reload 恢复当前连接，启动解析/后端就绪/resolver 配置仍未改。由部署配置所有者治理并验证冷启动及地址变化；本轮未通过容器重启来验收此项。
3. **范围外验收。**新微信只走文本分类 LLM1005，未触发报价抽取1007、图片1003、其他媒体或长时并发。企点/QQ 为当前两账号双向文本，不代表所有账号、所有功能、真实跨重启或客户安装通过。

未提交、推送、清历史或修改共享 peer。本报告没有可直接重放的发送命令；后续运行须重新核验身份和在途任务。历史失败保留，后续补验使用新指令，不借成功样本覆盖此前 UNKNOWN。

## 6. 文档验证

终稿在 `docs/` 执行 `timeout 120s python3 check-truth-tables.py`，**exit 0，真值表全部一致**；两份本轮文档 **19 个本地链接有效，无行尾空白**。文档角色只写正式报告与本轮 HANDOFF，未增加规格裁决、未修改文档质量门，也未执行或重放报告中的业务发送操作。
