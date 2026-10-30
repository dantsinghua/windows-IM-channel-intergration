# 2026-10-29 最新采集端回归最终记录

安琳，最新补充：安琳已登录新槽位qq97并提供完成向导截图，当前qq97在线及原生只读查询已验通。此前主控持续核查旧qq96属于检查对象错误，旧结论不适用于qq97，不能暗示安琳未登录。此前最终超时版源码全门、独立复审、部署及限定桌面现场验证记录保留；真实发送及下游新增ACK仍未验，PW一次间歇导航失败未复现且原因未定，不宣布全部业务通过。

依据 [配置变更保护机制](../../.codex/CONFIG_CHANGE_GUARD.md)；阶段入口见 [本轮交接](../../.codex/regression-20261029/HANDOFF.md)。基线 `main@402e5a5c8b35cf12a27ea25b937503bd8c041ee8`，保留既有dirty。本角色独占这两份文档，不改产品、测试、配置或运行。

证据根为 `/home/anlin/.local/state/qtrade-regression-20261029`。下列路径均相对此根；不读取或转录真实凭据。PID为报告时点身份，不能当未来运行保证。

本次仅补充两份文档，源码未改，未重跑前套；下列4279/1547/680/PW44等均为前轮证据，不是本次新跑。独立现场补验 `qq97-acceptance/RESULT.json`、`REPORT.md` 已完成，结论PASS_LIMITED。

| 用户要求 / 场景 | 旧行为或现行合同 | 当前结果与边界 | 分类 |
|---|---|---|---|
| 最新源码启动采集服务 | 旧API隔离副本不能代表当前工作树 | 全152份替换，覆盖当前134份Python/SQL；API52554，unit `qtrade-qa-collector-latest-20261029` | 修改 |
| WinAgent及桌面更新 | 既有配置、登录态和依赖保持 | 双EXE已部署，SCM50300/User21356；Electron49380、Vite5314 | 修改 / 保留 |
| 桌面正常访问API | wrapper原origins缺5314 | 保留旧origins追加5314；六页129个HTTP全200，零网络错误 | 修改 |
| 重连回读保持行为且有界 | 对全部消息无界并发；原context/request/revision围栏保留 | messages.ts加4并发许可、排队后围栏复查、finally释放；client.ts追加15秒传输取消，7例GREEN、最终全门通过、复审APPROVE，已部署；最终桌面5快照/101HTTP/errors0/writes0，Debugger确认两模块实际加载SHA=served | 修改 / 限定验证通过 |
| 企点与微信持续采集 | 不能以UI状态替代真实采集证据 | 新PID62秒3采样：企点轮询持续但无新消息；微信新增1条入库 | 保留 / 有限实测 |
| QQ可用 | 区分新旧slot，结合用户登录事实、API与原生连接 | 安琳已登录qq97并提供完成向导截图；`qq97-current.json` 确认连接，`qq97-native.json` 确认online/good及身份匹配、好友203/群8查询成功；旧qq96未登录状态不适用于qq97 | 在线及只读查询通过 / 3采样PASS_LIMITED |
| 地址、认证与工作台身份 | URL可编辑、两钥不回显、改名不换稳定ID | 本轮未专项验证空白机器、更换地址、改名；不缩小既有合同 | 保留 / 本轮未验 |
| 多采集端、多账号、目录 | 所有权独立，slot增删不影响其它路径；预览不等于对端发现 | 未验双采集端、错误归属、目录发现/选择；不以单现场证明 | 保留 / 本轮未验 |
| 邮件中转、文件网关 | 收发、路由、发布、消费、回执分别证明 | 文件网关后台运行；新增发布/下游消费/ACK、邮件收发路由与真实外发未验 | 保留 / 本轮未验 |
| 持久化、Vault及旧状态 | 本地所有权、只读seed、保存不回退、CAS/identity/UNKNOWN保持 | 配置未变；数据库副本兼容通过；未专项验证配置保存/重启与对端对齐 | 保留 / 本轮未验 |
| 文案和共享dirty | 不改未授权功能或并行工作，不回显秘密 | 两文档定点新增，不变更产品文案；全量产品文案扫描非本角色验收 | 保留 / 本轮未验 |

验证分层：

- 最新qq97现场快照：`qq97-current.json` 中qq97/wx07/qd92均running，qq_connected含qq97，运行数3/3，无告警或网关错误；`qq97-native.json` 中OneBot online=true、good=true、identity_matches=true，get_status/get_login_info/get_friend_list/get_group_list均HTTP200、status=ok、retcode=0，好友203、群8。此为主控原生查询证据，独立补验未重查OneBot。
- 独立qq97补验：`qq97-acceptance/RESULT.json`、`REPORT.md` 为PASS_LIMITED，20:09:37–20:10:37（Asia/Shanghai）、60.011秒3采样；三账号持续running、qq97连接、HTTP全200、errors/alerts为空、scheduler errors为0，只读DB绑定成立。消息178→178，窗口无新消息，不能据此判断接收端到端通过或失败；微信skipped增28，不证明吞吐通过。真实发送、下游ACK与长时间稳定性未验。
- 源码/合成：修前console673单测、Playwright44，类型/构建/lint exit0，lint37 warnings；WinAgent Windows1547，Linux1543及4平台skip。分别见 `console/REPORT.md`、`winagent/REPORT.md`，不混算不同快照。
- Agent：首轮有效全量4257通过、4失败、18error均缺jks；完整依赖环境 `agent/run-184504` 同次4279通过、0失败、0跳过、5 warnings，739.35s。文档已复核pytest末行、exit0，18:57:40结束；主树与peer漂移均为空，各轮不累加。
- 数据库：`agent/migration-183220/result.json` 与 `agent/STATUS.md` 证明schema v3副本34993条消息及既有行摘要不变、重复打开一致；迁移专项25通过。没有在真实库执行迁移。
- 重连初版历史：独立6例SHA256 `4fa80c225570e717d411c2b3c5762db4e2ed050565ffda3913a56e0cca16f8e1`；修前4失败2通过、修后6通过；初版单测679通过、原完整Playwright44通过，均不替代下列最终超时版证据。PW首轮43通过1失败为导航间歇失败，基线/当前各5次复验均绿，原因未定，风险保留；不能归因为产品回归，也不能认定风险消失。
- 终审与追加修复：首轮独立终审REVISE指出4个请求无timeout而持续挂起会占满许可、阻塞新查询。主控已在 `console/src/api/client.ts` 的 `messagesApi.get` 内部单行加入 `signal: AbortSignal.timeout(15000)`，签名保持。新增传输取消独立RED为原6绿+1红，修后7GREEN；原6例字节前缀保留，冻结7例SHA256 `334cc4fc42664ddedb57e48d79dcc09e8e8269a81ed5495921c7cf8b1d119da6` 已核。两产品文件diff共13insert/1delete，未计入原有其它dirty。`message-refresh-fix/review/RESULT.json` 最终APPROVE，唯一P2阻断CLOSED；旧REVISE保留。15秒约束取得许可后的详情传输，不保证排队或认证/429退避链全程上界。
- 最终超时版同快照：已读取 `message-refresh-fix/tests/FINAL-REPORT.md`，unit680/680（包含独立新增7例）、typecheck/build/lint exit0（37 warnings/0 errors）、finalPW44/44；211份输入固定于 `final-validated-inputs.json`，构建37文件见 `final-build-sha256.json`（均在该tests目录）。独立合成测试证明实际abort及许可释放、新查询恢复，不是真实账号故障注入。
- 部署与运行快照：主控已部署2产品文件及新测试到5314运行副本，替换Windows desktop `dist/renderer` 并保留备份；`message-refresh-fix/deployment.json` 三份源hash与APPROVE记录一致。此前读取的根 `final-runtime.json`：两处served_source marker均true，API52554、errors与告警为空、WinAgent及用户代理在线、企点/微信running，QQ为WAIT_QRCODE且无原生连接；此为旧qq96读取时点快照，不适用于当前已登录qq97。服务响应hash与源文件hash分别记录，不混为同一字节层。
- 最终桌面现场：已核 `message-refresh-fix/tests/desktop-readonly/desktop-readonly.json`，Windows node探针实际单次reload+30秒观察，5快照/101HTTP/errors0/writes0；首样本auth ok/WS open。该目录 `script-binding.json` 经Debugger读取实际加载的 `/src/stores/messages.ts` 与 `/src/api/client.ts`，loadedSha256分别为 `88c0ee45d4e83935ad9cb70567767eb4132d58ac340ee0bc5ef193126469972c`、`703ee1dcfbfbd767d707f4882ee7b49ac51e41cca6ed15ac161954509c6b9d71`，均与servedSha256一致，match=true、complete=true；这是最新模块实际加载的直接证据，不扩大为QQ或真实发送/ACK验收。
- 启动清单：主控已更新核对当前111文件，旧版保留Windows `before-message-refresh`，不影响下次同启动脚本；文档角色未运行脚本或另起探针。
- 旧qq96历史复核（对象错误已更正）：安琳反馈“QQ显示在线呀”后，19:15:51（Asia/Shanghai）主控仍检查旧qq96，追加同目录 `qq96-current.json`；`#/acct/qq96`文字为“登录中·等待你操作”，Pinia与直接API均login_required/WAIT_QRCODE，最新5条状态事件replayed=false且全为WAIT_QRCODE。根 `qq-native-status.json` 只读get_status/get_login_info均ConnectionReset。这些历史事实不适用于当前已登录qq97；错误归因由主控承担，不据此要求安琳扫码或重登。
- 现场：`live-acceptance/REPORT.md` 新PID窗口18:48:26–18:49:28，62秒3采样；首样本H06 warn后恢复，微信调度skipped存在，不证明吞吐性能通过。
- 修复前桌面历史：`console/desktop-readonly-r2/REPORT.md` 六页HTTP129全200、零网络错误/未捕获异常/写请求；该轮仅运行目录hash、进程cwd及URL绑定成立，未直接证明加载模块字节。该缺口已由上述最终script-binding对两处修复模块补证，两轮计数不混算。

原始备份与所有权：数据库备份 `agent-before.db` 的不变性已由副本报告核对；wrapper原件 `runtime/serve-collector.before-cors.py`、消息源码原件 `message-refresh-fix/messages.before.ts` 留存。Agent对端快照见 `agent/run-184504/peer-before.json`；对端只读，不授权写入或发布新协议。备份完整性及最终peer漂移结论以责任角色最终报告为准。

本次文档原字节、SHA256及换行备份：`qq97-docs-before-daf1pr3s/manifest.json`。本次不修改配置、对端或质量门，所有权及待对齐边界保持上述矩阵口径。

停止点：qq97登录事实、当前在线及原生只读查询证据已登记，旧qq96结论明确限定为历史；独立 `qq97-acceptance` 60.011秒3采样PASS_LIMITED已纳入，本次文档补充完成。真实发送/新增ACK等业务未验项及PW一次未归因、未复现的间歇导航风险保留。文档在`docs/`运行`timeout 60s python3 check-truth-tables.py`得到exit0；两文档4个相对链接均存在，未修改检查器。
