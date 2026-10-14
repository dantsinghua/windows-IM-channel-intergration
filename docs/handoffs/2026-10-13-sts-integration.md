# STS 临时凭据接入交接（2026-10-13）

## 最新裁决：跳过本轮 MQ 认证测试

安琳后续明确“跳过 MQ 认证测试”，取消它作为本轮打包前置。既有真实 MQ 401 和实际页面认证失败继续保留，**没有认证成功结论**；不因此放宽接口鉴权或把合成参数写入产品默认。继续其余安装器整改和最终出包工作，下方旧认证暂停条件按历史阅读。

最新实际桌面已验证页面→真实 DPAPI Vault 保存、清空秘密输入、刷新后只显示已配置标志，公开响应/浏览器持久化未发现秘密；新 Agent 启动后 `restart_required=false`。询价/目录所需配置仍未齐、业务未启用；未进行 OSS 业务与真实账号外发。该结果与假 Vault 的早期浏览器 7 项不同，原始证据及范围见 [本地运行交接](2026-10-13-local-runtime-e2e.md)。本轮最新总安装包尚未生成。

## 早期入口：页面配置与真实认证闸门（前置条件已取消）

**安装包仍暂停，真实 MQ 认证未通过。**安琳已明确 `auth.key`、`auth.sm4_key` 应在页面配置，不能随安装包或源码写死。所提供的合成值只属于回归夹具，不是默认值；本文不复写这些值、不再要求在聊天中发送密钥。

目标为 `http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts`，认证参考仓库 `https://git.frontnode.net/its/its-accesspoint`，总控读取的 HEAD 为 `ac26e9ed`。源码核对 SM4-CBC、请求 token 时间戳容差 ±60 秒一致；该时间窗不是 OSS 临时凭据的 `expiration`。总控用本轮用户指定合成值请求 MQ 仍返回 HTTP 401、`invalid_ciphertext`，证据 `.codex/package-20261013/sts-mq-user-values-result.json`；无 OSS 对象操作、无凭据明文日志，不能报真实认证通过。

现行合同为 **R6-104、02 §3.4.12**：

- P-MAIL「公网上报」初始为空，管理员配置 STS URL、业务口令和 16 字节 UTF-8 SM4 密钥；HTTP 必须显式选择。可选 endpoint、bucket、workbench_id 不预填个人或 MQ 值。保存与读取归 #88/#89 的 `ossbox` 专用组，新测试接口为 #110。
- 两钥只写入一个版本化 Vault bundle；SQLite 仅存公开元数据与引用，GET 只回已配置标志，不回值或引用。秘密字段省略为保留、显式空串非法，保存成功清空输入；不写浏览器持久化或明文配置文件，Vault 失败拒绝保存。
- 测试仅请求 STS，返回认证是否成功、bucket 和 expiration；不保存草稿或临时 AK/SK/security token，不操作 OSS 对象、不启动业务。保存不等于认证成功，认证成功也不等于询价/目录业务已就绪。
- 保存后需下一次 Agent 启动应用，不自动重启或热切换运行对象。已有页面 profile 优先于环境；失效时禁止回退。旧无 profile 的环境入口兼容，但启用 STS 必须显式设置 URL；已移除 `nytoken.yingmi.cn` 的默认地址，未配置的新安装不启用 STS。

### 本批验证与最终冻结状态

本批开发者测试、独立验收和浏览器验证分别通过；它们是分层证据，不相加，也不宣称来自整仓最终同一冻结。

| 层次 | 实际结果 | 证据与限制 |
|---|---|---|
| 后端相关回归 | **173 passed**，2 条既有弃用 warning；121 个后端源码/测试输入前后无漂移 | [开发者报告](../../.codex/sts-settings-20261013/developer-tests-report.md)；这是相关组，非全 Agent |
| 前端全量与静态检查 | **513 passed**；typecheck exit 0；lint exit 0，0 error / 37 条既有 warning | 同上；独占 ext4 副本，最终新增保存结果不确定的回归已包含 |
| 独立配置验收 | **75/75 passed** | [独立报告](../../.codex/sts-settings-20261013/acceptance-report.md)；真实 FastAPI/SQLite/provider、FakeVault、仅回环 STS，运行后 hash 不冒充运行前整仓冻结 |
| 浏览器操作 | **7/7 passed**；page error、意外网络和意外 console error 均为 0 | [浏览器结果](../../.codex/sts-settings-20261013/browser-results.json)、[保存后截图](../../.codex/sts-settings-20261013/browser-config-saved.png)；真实 Chromium → 真实 HTTP Agent API，Vault 与回环 STS 为假服务，403/503 负向用例预期错误单列 |

本批两项缺陷已按同一断言复验修复：① 保存已提交但断线、坏响应或 5xx 时结果不确定，页面显示「保存结果待核实」并锁定，须重新 GET 核实后才能继续；不能笼统报失败后允许盲目覆盖。② `ossbox.config` 持久化行存在但值为合法 JSON `null` 时，必须判坏 profile，禁用 OSS 消费者，不能当作未保存而回退环境凭据；正常无行场景保留兼容。

**最终同源冻结全 Agent 回归通过：3116 passed / 0 failed，6 条 warning，308.59 秒**，其中已含 75 项独立验收，不重复相加。566 个冻结输入在主树/快照核对无漂移；前端 513 项、typecheck/lint/build 均 exit 0（lint 37 条既有 warning），浏览器 7/7。证据见 [final-verification.json](../../.codex/sts-settings-20261013/final-verification.json)。旧 R1 的 3039 passed / 2 failed 为过程记录，#78 断言由独立测试方按规范对齐并加强副作用检查后纳入本轮。以上仅为源码回归，真实 MQ 合成值仍 401，打包继续暂停。本段与另外三处交接状态的回写属于 **post-test documentation-only** 变更，发生在测试后，不冒充仍与原冻结逐字一致；修改前后 SHA 和差异见 [文档后记](../../.codex/sts-settings-20261013/post-test-docs-verification.json)，仅重验文档门。

此前 **161 passed、零源码漂移**仅覆盖 HTTP opt-in/协议批，**不覆盖后来新增的页面配置、Vault 保存与启动接线**。rootfs-r2 也早于这些改动，必须从最终源码重建。

恢复打包的前提仍是实际部署配置通过真实认证，并完成本批回归；不得以合成服务、页面保存或历史冻结代替该闸门。安装链路其余未验项见 [安装包交接](2026-10-13-portable-installer.md)。

## 1. 授权与交付边界

安琳要求按仓库根 [STS接口调用说明.html](../../STS接口调用说明.html) 对接并修改代码，随后确认 `nytoken.yingmi.cn` 尚未就绪，先预留接口、之后联调。本批实现 STS 鉴权和现有 OSS 调用接线；没有批准整份 [32 项整改设计](../superpowers/specs/2026-10-13-qtrade-occam-remediation-design.md)，D5 正文采集政策及 AES-GCM 封套替换仍未确认。

最初批曾预留 `nytoken.yingmi.cn`，当时未请求真实 STS；该默认已按同日 R6-104 移除。当前地址必须显式配置，MQ 请求结果以文首最新记录为准；仍未向真实 OSS 写对象或向业务账号发送消息。

最初批仅提供 Agent 环境变量。**现行入口为 R6-104 的页面配置及 Vault，环境只保留无页面 profile 的兼容路径**；绑定管理未由本批补齐，不得把保存成功当作 STS 或业务已启用。

## 2. 当前接线

- `ossbox/sts.py` 负责生成鉴权头、获取并校验五个响应字段、内存缓存和按需刷新；使用 `cryptography` 的 SM4-CBC，不手写 SM4。
- `ossbox/oss_http.py` 的旧部署仍由 `OssV1Box.from_env()` 选择凭据模式；页面 profile 则由 `ossbox/settings.py` 在启动期装配并优先使用；同一次 OSS 请求使用同一份临时 ID、secret、token，`x-oss-security-token` 同时进入请求头和 V1 签名串。
- `app.py` 为询价收件箱与目录发布器装配共用的 OSS box；这些消费者把阻塞 OSS 调用放到线程，SQLite 操作仍由原调用线程执行。
- 两个消费者各用 `ObjectIO` 跟踪本轮底层 IO。取消异步等待不会中断正在工作的线程；该线程真实退出前，同一消费者下一轮会以 `oss_io_in_progress` 拒绝重入，避免取消后叠加新请求。
- 原静态凭据方式和 `ossbox/crypto.py` 的 AES-GCM 消息封套保持原合同。STS 的 SM4 鉴权钥不能替代询价/存款封套钥。

## 3. 配置口径

页面五个公开字段、两钥与持久化以 [02 §3.4.12](../02-后端与本地数据库设计.md) 为唯一合同。下表仅记录无页面 profile 时的**兼容环境入口**，不作为页面默认值，也不是可直接启动真实 Agent 的命令。

| 环境变量 | 配置要求 |
|---|---|
| `OSS_AUTH_MODE` | STS 接入显式设为 `sts`。未指定模式但存在任一 `OSS_STS_*` 变量时也会选择 STS；显式 `static` 与这些变量同时存在会拒绝配置 |
| `OSS_STS_URL` | 启用 STS 时必填，无默认地址；现场地址不固化进包。拒绝 URL 用户信息、query、fragment 和控制字符，不跟随重定向 |
| `OSS_STS_ALLOW_HTTP` | 默认 `false`；仅显式 `true` 才允许非回环 HTTP STS。环境值 trim/lower 后只接受 true/false，其他值拒绝；不影响 OSS endpoint 的独立 HTTPS 约束。回环 HTTP 假服务仍可用；构造参数 `allow_http` 必须为真实 bool |
| `OSS_STS_AUTH_KEY` / `OSS_STS_AUTH_KEY_FILE` | 二选一，内容必须与服务端 `auth.key` 完全相同；这是业务口令，不是 SM4 钥 |
| `OSS_STS_SM4_KEY` / `OSS_STS_SM4_KEY_FILE` | 二选一，内容与 `auth.sm4_key` 对应，按 UTF-8 编码必须正好 **16 字节**；不作 hex/Base64 解码，16 个字符不一定是 16 字节 |
| `OSS_ENDPOINT` | 必填，使用服务方提供的 OSS endpoint；不要包含 bucket、对象路径或查询参数。bucket 单独取自 STS 响应 |
| `OSS_BUCKET` | 可选的预期 bucket；配置后必须与 STS 响应相等。未配置时首次成功响应确定本进程的 bucket，后续刷新不得切换 |
| `OSS_WORKBENCH_ID` | 现有工作台标识，消费者继续需要；不是 STS 返回值 |
| `OSS_SEAL_KEY_XUNJIA` | 现有询价消息封套钥，格式及权限不变；兼容别名为 `OSSBOX_KEY_XUNJIA` |
| `OSS_SEAL_KEY_DEPOSIT` | 现有存款目录/消息封套钥，相关消费者按原要求读取；兼容别名为 `OSSBOX_KEY_DEPOSIT` |

询价收件箱需要工作台标识和询价封套钥；当前目录发布器同时需要工作台标识及询价、存款两把封套钥。封套钥仍是 hex 编码的 16/24/32 字节 AES 钥，与本批 SM4 的 16 字节 UTF-8 配置不是同一种格式。

每项秘密的直接值与 `_FILE` 不得同时设置。文件读取 UTF-8 内容，允许末尾一个 LF 或 CRLF；SM4 钥文件不带 BOM。文件来源应是仓库外、权限受控的私有位置，不把真实值放入源码、样例、命令行参数、日志或 `localStorage`。该路径仅兼容既有环境部署；页面配置使用 Vault，不生成这类明文文件，也不放宽正式生产的凭据保险库规则。

STS 模式缺 endpoint、鉴权材料或材料格式不合法时，在装配阶段拒绝且不出网；构造 provider 本身也不请求 STS。只有配置完整后的实际 OSS 操作才按需取证。STS 配错、鉴权失败或刷新失败均不会回退到 `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET`；旧静态方式仅在明确的静态配置路径继续使用。

## 4. 协议与失败处理

每次 STS 请求调用配置的完整 URL（接口说明原路径 `/api/sts`；当前 MQ 路径 `/its-accesspoint/v1/api/sts`），在 `access_token` 头中放标准 Base64 编码的 `IV16 + 密文`。密文为 UTF-8 JSON 经 PKCS7（128 位分组）填充后使用 SM4-CBC 加密的结果；JSON 含业务 `key` 及 Unix **秒字符串** `ts`，每次使用新的随机 16 字节 IV。服务说明规定时间差不得超过 60 秒，联调需核双方时钟；不长期复用鉴权头。

成功响应必须含非空 `access_key_id`、`access_key_secret`、`security_token`、RFC3339 `expiration` 和合法 `bucket`。临时凭据只在内存缓存，进入到期前 5 分钟时按需刷新；不创建后台刷新进程。剩余有效期不超过 30 秒的凭据拒绝使用。

瞬时网络错误或可重试 HTTP 状态允许继续使用仍有安全有效期的缓存，并短暂退避；只有实际尝试刷新失败且复用缓存时记录固定 `sts_refresh_deferred` 告警，退避期内的缓存命中不重复告警。认证失败、响应格式错误、bucket 不符或凭据已过期直接拒绝。错误只保留固定错误码，不回显 STS 错误正文或鉴权材料；请求建立或响应读取中的 `HTTPException` 也收敛为 `sts_unavailable` / `oss_unavailable`，不输出底层异常原文。OSS 明确返回 token 过期/无效时使对应缓存失效，当前请求不立即自动重做；下一次调用重新取证。网络超时或未知结果不在此层触发业务消息重发。

OSS 列表最多读取 100 页，并使用 30 秒分页逻辑预算；分页没有推进、超过 100 页仍未列完或耗尽预算均使整轮失败，不消费已取得的部分列表。增量拉取属于整体整改方案的后续工作，本批没有完成历史对象的增量分页。获取 STS 凭据所耗时间会从该分页请求后续 OSS socket timeout 中扣除。底层 STS socket timeout 为 10 秒，OSS 为至多 30 秒或分页剩余预算；这些是网络调用的超时设置，不是可以硬中断线程的总墙钟承诺，不能据此声称整个操作必在 30 秒内退出。

## 5. 最初 STS 批的依赖与验证历史

本批新增直接运行依赖 `cryptography`，用途仅为上述 SM4-CBC 鉴权；锁版为 `cryptography==50.0.2`，必要依赖为 `cffi==2.1.1`、`pycparser==3.11`。现有 15 包闭包扩为 18 包，仍使用 hash 锁、仅 wheel 安装、`wheels.expected` 精确清单及 `pip check` 原门。依赖构建不等于完整 rootfs、安装包或 Windows 真装验收。

依赖专项已在 Ubuntu 22.04.5（Jammy）/ glibc 2.35 / portable CPython 3.12.12 x86_64 中完成：18 包清单生成（emit）、实装清单核验（check）、`pip check` 及 SM4-CBC/PKCS7 冒烟通过；原 15 包清单行保持不变，未修改原校验门。证据为 `.codex/sts-integration-20261013/dependency-result.json` 及 `dependency-target/`。这只证明目标依赖运行环境，不代表完整 rootfs 构建或安装验收。

文档依据当前源码和原始接口说明核对，产品专项、冻结全量、依赖构建与独立黑盒结果由各责任方写入 `.codex/sts-integration-20261013/`。交接初稿不登记在途测试的成功数字；最终版本、命令、退出码与未验证项以该目录最终证据报告为准。本文不宣称整体零回归。

### 5.1 R1 全量暴露的既有阻断

R1 Agent 全量结果为 **2912 passed / 8 failed**。对失败的八项，以精确的 STS 实施前源码和原有 venv 复跑，同样八项失败；STS 实施后的对照复跑也复现同组失败，确认它们是本轮全量发现的既有阻断，不是 STS 引入的回归。对照记录为 `.codex/sts-integration-20261013/regression-comparison.json`，R1 全量日志为同目录 `full-agent.log`。

- 两项企点用例进入输入回读不符分支时，引用未定义的 `readback_trace` 而报错。产品仅补为回读是否为 `None`、期望长度和实际长度的诊断；仍返回 `input_readback_mismatch` 并拒绝点击发送，不记录输入正文。
- 六项视频用例使用的 Fake 只产出两帧就 EOF，与已存在的真实 EOF 关闭码 `1001` 冲突，控制帧的权限/格式断言来不及执行。独立测试维护者将 live Fake 改为等待 close 事件，原有 `4403` / `4400` 断言保持不变，并补真正 EOF 应关闭为 `1001` 的用例；没有撤销生产 EOF 关闭逻辑。

以上变更仅用于收口本次回归门，不表示 32 项整体整改已获准实施。R1 和定向回归保留为过程证据，修复后的最终结果如下。

### 5.2 R2 最终冻结结果

R2 基于 HEAD `6c5b941` 加当前工作区修改，冻结 620 个源码、依赖及测试输入，运行根为 `/home/anlin/work/qtrade-build/sts-integration-20261013-5t8bflcu/snapshot-r2`。本轮结果：Agent **2926 passed / 0 failed**（272.34 秒，6 条既有弃用 warning），WinAgent **602 passed**（11.56 秒），安装器 Python **160 passed**（10.17 秒），文档真值表检查 **exit 0**。运行后主树与快照的上述 620 个输入均无漂移；冻结与比对记录由主控汇入 `.codex/sts-integration-20261013/final-verification.json`，原始日志为同目录 `r2-full-agent.log`、`r2-full-winagent.log`、`r2-full-installer-python.log`、`r2-docs-check.log`。

26 项 STS 独立验收已包含在 Agent 全量中，不额外相加；最终证据为同目录 `acceptance-final.log`、`acceptance-report.md`、`acceptance-freeze.json`，中间轮次 `acceptance-r2.log` 的 22 项不是最终结果。此前 126 项 OSS 回归和 143 项相关守护仅作为定向证据。此次交接结果回写属于交付文档输出，不改变上述产品测试输入，交付文档最终 SHA 另行记录。

上述最初批的结论限于代码、假服务及隔离 Jammy 依赖验证。真实 STS/OSS 当时按安琳要求后置，现行顺序已由文首更新为“先 STS 认证、再总装”；前端本批未改、未跑，Windows Pester、完整安装包及真装未跑；未部署、未 commit/push。不能将这些自动化结果外推为真实链路或整个 32 项方案完成。

## 6. 页面配置后的联调顺序

1. 在管理员页面填写部署方确认的 STS URL 与两把密钥；HTTP 测试地址显式选择允许，正式 HTTPS 不开启该选项。先运行只读认证测试，不要求把密钥贴到聊天、写入安装包或手改配置文件。
2. 先在隔离运行根核对配置、双方时钟和 STS 五字段，证据仅记录状态、到期时间及脱敏错误码。不得以默认地址已写入代码冒充服务可达。
3. 使用约定的只读测试前缀验证 OSS 列表/读取及 token 签名，再验证刷新、短时网络失败和鉴权拒绝。真实对象写入、回执投递或业务消息发送须按各自授权范围单独执行。
4. 记录同版本源码、锁与 wheel 清单哈希、测试前缀、命令、结果及未覆盖项。实际部署鉴权未成功期间保持“待联调、打包暂停”；假服务通过不关闭真实 STS、OSS 权限或完整安装验收。页面行为以该批独立测试证据另行登记。

本文未提供可直接执行的真实服务命令；文首真实 MQ 请求由总控执行并脱敏留证。没有要求接手者直接运行默认 `AgentApp.start(recover=True)`，避免恢复真实账号并触发任务。
