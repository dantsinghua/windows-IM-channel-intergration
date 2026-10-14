# 2026-10-13 可移植安装包交接

## 最终交付结论：内部测试包，供受控安装验收

最终 r2 **有限范围 PASS**，ARTIFACT-01 CLOSED，无遗留包级阻断。结论限包内内容、源码身份、签名完整性及限定原生自检；不是完整真装或真实账号功能通过。旧 `ed6e5e…` 候选继续 REJECT，拒交付标记与所有失败记录保留。

- 交付目录：`C:\Users\anlin\Desktop\QTrade-1.0.2-20261013`，由 root 将已准备的 delivery 原子移动至该位置。
- 文件：`QTrade-Setup-1.0.2.exe`，**2,598,056,832 字节**。
- SHA256：**`98ec27e99065dcecaf732b68f4e4778cda23193d6d6fca0e8cb54df5a7e26d68`**。
- 成品来源：本轮 `out-r2` 完整重签总装，没有复用旧归档或跳门。

[独立最终验货](../../.codex/package-final-20261013/artifact-r2-final-verification.md)/[结构化证据](../../.codex/package-final-20261013/artifact-r2-final-verification.json)：103 installer 输入、260 业务/构建绑定核对；3378 个实际解包文件及3377清单项的路径、size、SHA吻合，非轻量且无缺件；3351份原资产未变，三份签名EXE代码绑定、ASAR/embedded源一致；双r8/embedded自检通过，原成品、解包原文件及payload源在自检后保持不变。旧来源文件与随包生成COM包装均为零。非冻结 embedded Python 曾在私有验收副本生成目标机包装的观察及断言纠偏保留，未进入源码载荷或安装EXE，不掩盖旧红。

同一SHA的 [SFX启动/取消](../../.codex/package-final-20261013/sfx-welcome-r2-result.json) **ACCEPT**：53.30s，欢迎页完整可见（主控已检查截图），取消后进程exit2、stage_exit2；SCM PID4076仍running且不变，正式QTrade根不存在。只到欢迎/取消，没有执行安装主步骤；不以此代替全新安装、清空重装或内核切换。

25个第一方文件签名者均为 `E36AFD96A801DD666F953AC8E3B3C992AB8CD60E`，都有DigiCert时间戳，私有副本单字节篡改被HashMismatch拦截。WinVerifyTrust仅报自签根未信任，**不声称Windows Trusted/Valid**；无证书库写入。公开README含指纹、核对与目标管理员决定导入的说明，signing目录仅五份公开文件、无PFX/私钥。

**实际安装影响与未验边界**：安装涉及共享WSL内核切换，确认后可能停止其他现役WSL/Docker Desktop进程并要求重启；“不在清空范围”不代表运行不受影响。该包定位受控测试机安装验收；完整真装/清空重装、任意机器兼容性、真实企点/微信/QQ业务未验，MQ按要求跳过，H03检查/自动restart与初启recover重试仍open，不作全部缺陷清零承诺。

验货后允许同步 `installer/build/README.md`：仅文档、不在包内、不执行；对冻结103来源的唯一差异为该README，产品和冻结副本不变，增量以 `docs-delivery-final-verification.json` 登记，不回写旧冻结。

## 验货前阶段：r2 已总装，等待独立结果

r2 全流程总装成功（1470.04s），没有复用旧归档或跳门。候选路径为 `C:\Users\anlin\qtrade-build\release-1.0.2-final-20261013-4f6lkc12\out-r2\QTrade-Setup-1.0.2.exe`，2,598,056,832 字节，SHA256 **`98ec27e99065dcecaf732b68f4e4778cda23193d6d6fca0e8cb54df5a7e26d68`**，原始记录 [build-result-r2.json](../../.codex/package-final-20261013/build-result-r2.json)。

**当前尚无最终 ACCEPT，不交付。**独立 `artifact-r2` 实物验货及该同包 SFX 启动/取消冒烟正在运行；旧 `ed6e5e…` 的任一通过项不能替代新包验货，旧包 REJECT/DO-NOT-DELIVER 保持。root 仅在全部验收通过后把交付材料移至计划目录 `C:\Users\anlin\Desktop\QTrade-1.0.2-20261013`，该路径现在是发布目标，不冒称已发布。

公开交付说明已准备于本轮构建根 `delivery/README.md`，同目录 `signing/` 仅有公钥 CER、Import/Remove 脚本、签名模块与 CMD 共五份公开文件，无 PFX/私钥。CER 指纹已只读核为 `E36AFD96A801DD666F953AC8E3B3C992AB8CD60E`；未导入证书。README 不含私有证据绝对路径、现场配置、MQ 地址或内部测试凭据，状态暂为验货进行中，等待独立结论后更新。

冻结 `installer/build/README.md` 暂不改，保持验货源核对；最终通过后可单列纯文档差异。完整真装、真实账号和 MQ 尚未验收，MQ 按要求跳过；H03/recover 开放项与自签信任边界保持。

## r2 总装前检查点：首候选 REJECT，r8 修复通过

首个已签名候选为 `C:\Users\anlin\qtrade-build\release-1.0.2-final-20261013-4f6lkc12\out\QTrade-Setup-1.0.2.exe`，2,598,075,984 字节，SHA256 `ed6e5ea84d68fc014cc4722b5a5bdf259e98ebac44386e271ea9d680d1ec8bac`。同包真实 SFX 解压/向导启动/取消共 73.31s、exit 2，未执行安装步骤，正式 QTrade 根保持不存在；截图只抓到窗口一角，不能当完整界面验收。见 [SFX 冒烟](../../.codex/package-final-20261013/sfx-smoke-summary.json)。

**该候选独立验货为 REJECT，不交付**，`out/DO-NOT-DELIVER.txt` 留存。唯一实际交付阻断为 WinAgent app 的 `.dist-info/direct_url.json` 含本机构建来源；它不是运行时依赖，但仍违反可移植载荷要求。原签名、manifest、COM、rootfs 等已通过的检查保留，不撤销或混成包已放行。见 [旧包独立验货](../../.codex/package-final-20261013/artifact-candidate-review.md)。

r8 仅在 svc/user 两 spec 按包内目的路径过滤该来源记录，保留其它 metadata、license、入口点、版本发现和普通业务同名文件。独立 [metadata ACCEPT](../../.codex/package-final-20261013/metadata-test-report.md)、[构建终审 APPROVE](../../.codex/package-final-20261013/metadata-build-review.md)：双平台各 **874 passed**，82 测试输入无漂移，47 编译输入匹配；运行代码语义及保留 metadata 与旧版一致，不声称 EXE 二进制相同。用户自检 stderr 空，服务既有弃用 warning 与冻结基线逐字一致，没有宽泛忽略 warning。r8 组件通过不能套用于旧候选。

r8 svc SHA256 `40e5c021c51a2c39f05220a9fdd196f2469f3c5211101f9cd59f2d65ad889b34`；user SHA256 `bc2709a1c4df1ddfbc347382d8cbad94354ba6a9c3392937f9a92103bbb7b9f3`。最新载荷为 app r8/rootfs r4，3355 文件、4,222,948,162 字节；来源清单 SHA256 `6e85d268a4ddfce4e312bf6aa6933e417d35e5c986f804a8b5e36f1770e17987`，见 [替换记录](../../.codex/package-final-20261013/payload-r8-update.json)。当前 `out-r2` 全流程重新签名/压缩进行中，尚未交付；最终新 EXE 路径、摘要及独立验货须另记，不能沿用 `ed6e5e…`。

PS5 构建 UTF-8 读取门也已修正：旧 5 RED/8 PASS，隐藏控制台与无控制台最终各 14 PASS，邻接 112 Pester/141 Python，独立 [APPROVE](../../.codex/package-final-20261013/build-encoding-review.md)。旧归档、旧源码及失败日志保留；四代码页覆盖、非零退出和缺路径/坏哈希拒绝不放宽，见 [编码验收](../../.codex/package-final-20261013/build-encoding-verification.json)。这些是分批边界，不拼成一个全域测试数。

自签发布者的信任是明确发布边界：旧候选 25 个第一方文件签名者和 DigiCert 时间戳已核，单字节篡改负例返回 HashMismatch；原生信任结果仅为 `CERT_E_UNTRUSTEDROOT`，本机未导入信任，不写 Windows Trusted/Valid。新包须重验，目标机信任配置另行处理。MQ 继续 skipped，不记认证通过；完整真装、真实账号未验，H03/初启 recover 两项实现差异仍开放。r8 本地真实运行结果见 [运行交接](2026-10-13-local-runtime-e2e.md)。

## 首候选前阶段：r4 rootfs 已验证，7z 压缩中

r7 首轮 Docker false 已定位为 `qtrade-docker-config.service` 的隐式 basic/socket 排序环。最小单 unit 修复已 APPROVE，旧 r3 排序图 1 RED、新 r4 10 PASS，冷归档/关联 57 PASS；最终 r4 tar SHA256 `0562050447e2d7ec4224045ebab3de992ffcefa647e0352474e09bdf99890de1`。真实自有 qtrade 经 WA stop/start 后 12 次健康全 true，跨 probe 2→3；WS 手动重新取令牌/重连后稳定恢复。详见 [本地运行交接](2026-10-13-local-runtime-e2e.md)与 [排序验收](../../.codex/package-final-20261013/docker-ordering-report.md)。

最终包已进入 7z 压缩，尚未成包、未取得最终签名 EXE 哈希。下面 r3 三归档与 r7 payload 清单属于换 r4 前冻结历史，不作为最终包摘要；最终总装须以本轮新清单绑定 r4。MQ skipped 仍不等于认证通过。H03 systemctl/自动 restart 及初启 not_ready 后再次 recover 的实现张力单列开放，未随本次 unit 修复关闭；不宣称全仓缺陷清零。

## 最新裁决与接续入口

安琳同日后续明确“跳过 MQ 认证测试”，**取消真实 MQ 认证作为本轮打包前置**。既有 401/页面认证失败保留为事实，不改写成通过；不再据下方旧暂停条件停止当前工作。页面配置与 Vault 契约保持，包不固化现场或合成参数。

最新本地桌面/服务的限定范围 E2E 已通过：r5 180.09s 持续在线且未采样到新增可见黑框，显停 WSL 后 131.16s 未复活，8 页稳定复验无页面错误；rootfs r3 同归档 39 项通过，程序仍在运行。结果、摘要与仍未覆盖的真实账号、探针 fixture、完整 SFX 安装范围见 [本地运行交接](2026-10-13-local-runtime-e2e.md)。

安装器 R3～R6 实现及独立回归现已完成：Windows PowerShell 5.1 Pester **928 passed / 0 failed / 0 skipped**，安装器 Python **271 passed / 0 failed / 0 skipped**；578 个输入及三份冷归档零漂移，Pester 的 PS/ISS/CMD 输入也在最终复核中保持一致。只读终审已 [APPROVE](../../.codex/package-final-20261013/installer-review.md)，限 R3～R6 与签名默认路径；继续最终冻结、总装/签名/独立验货。截至本次回写最新总安装包尚未生成，未跑完整 SFX 真装。报告和精确边界见 [独立回归报告](../../.codex/package-final-20261013/remaining-tests-report.md)、[冻结记录](../../.codex/package-final-20261013/remaining-tests-verification.json)。

## r7 载荷可移植性修复

scrcpy 原三件套 ADB 为 37.0.0，本轮新私有载荷内 `adb.exe`、`AdbWinApi.dll`、`AdbWinUsbApi.dll` 已统一为主 platform-tools/rootfs 的 37.0.1。旧资产未改、其余 scrcpy 文件不变，无 ADB 执行；具体来源和旧/新摘要见 [覆盖记录](../../.codex/package-final-20261013/payload-adb-override.json)。

用户包原含构建机 COM 包装与绝对 typelib 路径。初版 r6 过滤后引入 `Revert to STA COM threading mode`，严格 stderr 门正确拒绝，原失败不删除、不豁免。r7 在目标机内存生成包装并保留默认 MTA/外部显式设置，独立 [COM 验收 ACCEPT](../../.codex/package-final-20261013/comtypes-test-report.md)：Linux/Windows 全 WinAgent 各 852 passed，81 输入零漂移，47 编译输入与当前源逐 SHA 匹配；17 模块自检 exit 0、stderr 空、包/缓存零写入，合成 child 信号 helper 也通过。未验证真实微信/chatlog 流程或其他 Windows 版本。

r7 用户 EXE SHA256 为 `ef208a892dea6f91fa7b9e3224005338c853eb47d5bc9501ee84ac12a35f5a88`。最终 SourceRoot 为 `C:\Users\anlin\qtrade-build\release-1.0.2-final-20261013-4f6lkc12\payload-source`；[r7 替换记录](../../.codex/package-final-20261013/payload-r7-update.json) 为 3356 文件、4222948457 字节，其中 app 413 文件，其余 2943 文件未动。清单 SHA256 为 `0aaa7e9e62ffc9f27a7c6bd7f0ecb2cf10861e18e1be3f4de95c226ffbd1cbe7`；早期 `payload-report.md` 的 r5 3360 文件/46 编译输入仅为历史，不用于当前收集。上述均是载荷摘要，不是最终签名安装包摘要。

本地已部署 r7，413 文件校验，90.08s/6 个状态样本全在线且未采样到新增可见黑框，正式 QTrade 根仍不存在。首个健康快照和后续 uptime 67～100s 的 12 个样本均 `dockerd=false`，连续健康验证 `accepted=false`；这段历史失败后来由文首的 r4 排序修复和新真实观测关闭。见 [本地运行交接](2026-10-13-local-runtime-e2e.md)；r5 的 180s 及旧 Docker 绿灯不能移记为 r7 通过。

## 安装器 R3～R6 最新闭环范围

- R3：每次 kcheck 注销/删除前重核实际 SID、state/manifest、精确 BasePath 与路径属性；注销失败停止，prepared 恢复也重核服务、任务和发行版归属。
- R4：防火墙消费实际 `result` 枚举，401/超时/坏响应不能登记成功；策略阻断保留 WARN，DONE 的 IT/17610 提示进入安装完成页。当前是分支/编译证据，真实完成页视觉尚未验收。
- R5：清空 journal 和随机 marker 绑定同包、SID、精确 root 与目录创建身份；旧 install_state 先删而其他内容删除失败时，可凭同事务继续。同一已确认私有 root 内的普通残留文件属于用户全根清空范围，不逐文件鉴别来源。root 替换、reparse 跳出、外来服务/任务/发行版、`root_empty` 后新增子项均拒绝，最后根删除为非递归。
- R6：四个 Docker CIDR 候选均冲突返回 76，不写 env、不启动 Docker；systemd 未就绪仍 72，未登记原因保留结构化失败。
- 签名公钥默认目录已改为 OS 当前用户 `LocalApplicationData\QTrade\signing`，显式 `CertDir` 优先；读取默认值不生成目录、证书或导出动作，不再固化 `C:\Users\anlin`。

在 928/271 冻结测试后，`New-QtSelfSignedCert.ps1` 的三处旧路径/默认说明另做了纯注释校正，保留 BOM/原换行。PS 5.1 Parser 通过，忽略 Comment/NewLine 后可执行 token 逐个一致，见 [注释变更记录](../../.codex/package-final-20261013/signing-comments-change.json)与 [验证日志](../../.codex/package-final-20261013/signing-comments-verification.log)。这不重写旧测试 SHA，也不冒充当前脚本仍与旧冻结逐字节相同；最终总装需使用更新后的输入摘要。

三份实物为 rootfs r3（SHA256 `bba363f13e070fca16521ee096dbcff14444eefa9eae5065582461cd847cba7f`）、kcheck（`57bef622fec8dcddafa1049023f8c181956f8a1ccb4d0e37c2879af5c44bf96e`）、新 SFX（`b30c5e5876f563a362b52b0ecbf759db2bfb2c31cced3d76091c873542c6f64f`）；这些摘要不等于最终签名安装 EXE 的摘要。旧专项 30 RED、签名默认目录 1 RED，以及源码篡改负例均保留在上述报告中。

## 早期状态记录（认证暂停条件已被上方裁决替代）

**总装暂停，尚未交付 1.0.2。**安琳最新要求先确认新 MQ STS 认证，再继续打包。目标接口已提供：`http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts`，鉴权参考为 `https://git.frontnode.net/its/its-accesspoint`，鉴权代码 HEAD `ac26e9ed` 已由总控取得，SM4-CBC 匹配且请求 token 时间戳容差 ±60 秒（不等于 OSS 凭据 Expiration）。目标未鉴权与示例配置请求均返回 HTTP 401；示例不是部署密钥，`invalidciphertext` 不构成算法失败结论。两端时钟与服务 Date 一致。安琳随后明确提供的是合成回归值，且两钥须由页面填写；总控用这组值请求仍为 401 `invalid_ciphertext`（`sts-mq-user-values-result.json`），QTrade 真认证尚未通过。此前“原定 nytoken 尚未部署”的说明属于历史，不再作为当前部署事实。凭据不得写入本文件或仓库。

R6-104 已增加管理员页面 STS 配置及 Vault 保存，初值全空，已移除 nytoken 默认地址；页面 profile 优先于旧环境入口，下次 Agent 启动应用，失效不回退。测试只申请临时凭据，不操作 OSS/业务。`allow_http`（环境兼容键 `OSS_STS_ALLOW_HTTP`）默认 false，HTTP 显式 opt-in；MQ URL、部署密钥及合成值均不固化到包。见 [STS 配置更新](2026-10-13-sts-integration.md)。rootfs-r2 因本次鉴权源码变更已过时，不能沿用。

此前 HTTP 鉴权增量为同冻结 **161 passed、零源码漂移**（`sts-mq-current-source.json`），只覆盖当时的协议/假服务/边界验证，不覆盖随后新增页面、Vault 和启动接线。页面批已取得后端相关 173、前端全量 513/typecheck 0/lint 0 error（37 条既有 warning）、独立验收 75、真实 Chromium + 真 HTTP API + 假 Vault/回环 STS 的浏览器 7/7 通过，证据与范围见 [STS 交接](2026-10-13-sts-integration.md)。保存未知结果须 GET 核实、JSON null 行不得回退环境两项修复均有回归。最终同源全 Agent **3116 passed / 0 failed、6 条 warning、308.59 秒**（含 75 项独立验收）；566 个输入主树/快照核对无漂移，前端 build exit 0，见 [最终报告](../../.codex/sts-settings-20261013/final-verification.json)。本处为测试后的纯文档回写，原冻结与本次记录的 SHA 差异见 STS 交接。真实 MQ 仍 401，打包继续暂停；这些仅是源码与非发布验证，不是认证或安装包放行。不要再次索要聊天明文，部署认证值由页面配置。

本轮尚有待总控收口的 health review P1。已有 rootfs、控制台、WinAgent 和 SFX 的组件构建/验证候选，不能当作最终安装包通过；最终全源冻结、总装、签名、独立整包校验及目标机真装未完成。当前策略与旧保留升级方案有明确变化，接手先读本节及 00 §15g R6-101～104。

仓库分支 `claude/lucid-dijkstra-uu5max`，本批接手 HEAD `6c5b941482d4f711a4481d18b771f9b48e16d917`，存在大量既有未提交修改，正式布局仍为 `console/`、`src/`、`tests/`、`winagent/`。本批没有提交、推送、目录迁移或真实安装授权扩展。

## 安琳已确定的本轮边界

| 项 | 当前合同 |
|---|---|
| 换版 | 撤回复杂保留升级；完整包先独立暂存和校验，再明示清空、核归属、停止并清空已证 QTrade 资源，fresh install |
| 数据 | 不迁移 QTrade 配置、数据库、Vault、企点/QQ 登录态及专用卷；重新配置和登录。不动微信/QQ 客户端自有数据、其他 WSL/Docker 或未知路径 |
| 续跑 | 同包、同 manifest、同安装 SID、同 live 根且未 DONE 才续跑；DONE 重跑必须新确认 |
| 危险旧入口 | upgrade/repair/uninstall 明确拒绝；工作台发行版修复显示 NOT_READY / repair_requires_verified_plan，不自动导出/注销或假报成功 |
| 可移植性 | 目标机不依赖开发主机路径、代理、WSL/Docker 状态、个人配置和账户；资产全量离线，现场网络/身份/凭据由安装流程生成 |
| 支持边界 | 仍受 Windows x64、WSL2/虚拟化、权限、内存、空间与组织策略的正式预检约束；不承诺任意硬件或禁用虚拟化的机器强行安装 |
| 业务范围 | 不扩展本轮未批准的 32 项业务整改；安琳后续跳过 MQ 认证测试，不再作为本轮总装前置，认证状态仍未验证/已有失败 |

## 历史安装失败证据

原日志交换目录为 `C:\Users\Public\Documents\HyperV-Exchange\QTrade-Diagnostic-20261008`。原报告为 2026-09-22 的 1.0.0，另含 10 月 8 日探针，59/59 清单哈希匹配。

完整 rootfs 在原 Hyper-V Windows 中曾约 21.19 秒启动，Docker active；微型 kcheck 则在不同探针预算下超时。这说明不能再使用“嵌套 WSL 完全无法启动”作为结论。旧 kcheck 根 `/init` 指向 BusyBox；本轮已从归档去掉该链接，由 WSL 注入 init。静态和 syscall 夹具支持该路径缺陷的整改依据，但**新 kcheck 在 Hyper-V 实际启动、切换自编内核和新版完整安装仍未验证**。

旧正式包 `installer/out/QTrade-Setup-1.0.1.exe` 只作历史保留，不代表当前源码。新版本目标为 1.0.2 / rootfs-1.0.2；不要把早期候选改名或复用旧 engine/WinAgent/Electron 产物当新包。

## 本轮源码承接

| 范围 | 关键变化 | 当前证据边界 |
|---|---|---|
| SFX/Stage | Known Folder + 随机 GUID 暂存；不覆盖 live；payload 校验在清空前；路径/reparse/SID/服务/任务/BasePath/ARP 归属核验；受控清空后复制复核 | 原生存根与 Inno 编译、Parser/假 OS 测试不等于真实提权、服务停机或删除验收 |
| manifest | 每实际文件记录 path/dest/SHA/size；最终 engine/CMD/PS 模块同包；坏 hash、通配、目录占位、重复/逃逸/reparse 拒绝 | 最终总装仍需按最终签名文件重收集及归档复核 |
| rootfs | 空 machine-id/首启生成；现场 install.env 先于 Docker；全冲突拒绝；首启凭据门；离线 APK/镜像校验并写 runtime；NapCat v4.18.28、pull=never | r3 已有独立冷归档与本轮 Python 全量复核；完整 SFX 导入/安装仍待实测 |
| WinAgent | 真 SCM dispatcher、显式安装 SID/root、可信管道、提权 bootstrap、OS SID ACL；发现已运行受管 guest 的真实地址；危险修复停用 | 最新本地 SCM/管道/专用 WSL 联动已验证；完整安装创建链及其他机器边界尚未验收 |
| Agent | 仅安装器使用的 runtime bootstrap 与 console token hash 绑定；不启动业务；普通自检无执行器明确 skipped | 可信 bootstrap/health DTO 已独立回归，真实本地服务健康通过；不替代最终安装 EXE 链路 |
| console | 正式包禁开发 URL/token；可信 pipe 返回地址与令牌；HTTP/WS/画面统一源；去个人/实验默认配置；终止后异步画面探测不再建连接 | 正式 Electron 本地握手与 8 页联动已验证；完整 SFX 安装后的首次启动待验 |
| 安装自检 | 临时 redroid 不挂账号卷、不 ADB/登录；按本次 ID/随机 label 清理，清理成功才回成功 | 尚需获准隔离 Windows/WSL 的真实执行证据 |

## 已有验证怎样使用

本轮各责任人证据均在 `.codex/package-20261013/`；该目录可能不随仓库分发，因此本节保留结论和限制，精确命令/输入 SHA 仍以对应报告为准。

- `tests-final-verification.json`：当时冻结的载荷完整性 Pester 62/62、新 kcheck 归档 12/12；后续 stage 双根和其他源改动不能套用这个冻结。
- `winagent-portable-verification.json`：WinAgent 当时 76 个输入无漂移，全量 651 通过；未真 SCM/EXE。
- 独立前端测试批已报告 unit 485、假 HTTP/WS 127、Playwright 44 通过，typecheck 无错误、lint 0 errors/37 warnings；须以该批输入清单为界，后续改动重新核漂移。
- `clean-reinstall-implementation.md`、`console-portable-handoff.md`、`winagent-portable-implementation.md` 是各实现阶段交接；其中阶段结果不能替代最终验收。clean-reinstall 早期 14 项通过先于后续 ARP/入口修改。
- 本文档批验证命令为在 docs 运行 `timeout 120s python3 check-truth-tables.py`，结果随文档批记录。没有在本批运行安装/引擎/WinAgent EXE、真实 WSL/ADB、账户恢复、外发或清空动作。

不能把不同冻结、旧分支或不同测试根的通过数相加称“0 回归”。上列为历史批次；当前安装器最终 928/271 及本地 E2E 见文首。完整新包尚未生成，当前是“终审已通过、待最终总装验货”，不能写成已交付。

## 构建材料与下一步

本次独占构建材料根为 `/home/anlin/work/qtrade-build/release-1.0.2-20261013-57_xsxmn`，Windows 对应 `C:\Users\anlin\qtrade-build\release-1.0.2-20261013-57_xsxmn`。它们是本轮证据路径，不是产品配置或目标机要求。旧 `C:\Users\anlin\qtrade-payload` 仅可提供已核 hash 的静态第三方资产；不得从中复制旧 WinAgent/console 当新组件。

1. 保留 MQ 失败状态，按安琳新决定跳过本轮真实认证测试；页面配置/Vault/启动读取已有独立与真实本地证据，密钥不入包、不入日志。
2. 安装器 R3～R6 只读终审已通过；继续核当前源码与已验冻结及在途构建是否一致，发生新改动时由相应职责方补验证，不复用过期摘要。
3. 以已核组件和三份冷归档完成总装；检查嵌入式 Windows Python 不夹旧模块，最终已签文件生成每文件 manifest，再总装签名和独立验货。
4. 在明确获准的隔离 Windows/Hyper-V 环境验证首次安装、清空重装、同包中断续跑、SCM/可信 pipe、正式 Electron 启动及安装自检，记录其他发行版/工作负载不变。
5. 只有取得真实对应证据后才更新发布结论；不能把 fake/Parser/归档检查写成真装成功。

## 历史文档回写与遗留（当前对齐结果见文首）

下列早期一致性门失败已由独立测试方按 R6-101/102 更新合同并保留反向篡改，纳入最终 928/271；不再作为当前未关闭测试失败。历史报告不删除，真实安装与业务未覆盖边界保持。

本批通用文档真值表与新增本地链接检查通过；额外安装器源码/文档一致性检查仍发现旧 upgrade/官方 SFX/UninstallRun 等预期未随裁决维护，以及新清空确认开关尚未登记 SW_QT_ 常量。该跨职责检查未通过，已返实现/测试责任人，不以通用文档门绿色代替。

本批新增 R6-101～103，同步 00/01/02/03/04/05/07、安装构建/rootfs/SFX README、HANDOFF 与 PROJECT_CONTEXT；06 邮件业务不扩写。旧 A-5、未知同名发行版导出后注销、全冲突继续安装、官方 SFX 回退与通用卸载的现行正文已收口；历史裁决/变更记录保留日期和退役指向。

旧独立验收手册及测试若仍要求“保留升级”或官方回退，应由各自责任人按本轮裁决维护并保留有效负例；文档角色不改质量门。另有旧 runtime 数据库设计长期条款与实际实现未完全对齐，本轮仅撤销安装保留承诺，不借出包扩展迁移平台。
