# Windows IM 兼容整改与 1.0.7 发布约束（2026-10-30）

安琳，本轮 R6-119 兼容修复、完整 1.0.7 包静态实物验货、桌面交付及精确清理均已完成，**最终事实见 §8**；真实安装、重启恢复和真实账号业务仍未验，本报告不将本地或包级通过冒充现场验收。

**安装入口交叉核对（重打包过程留痕）**：安装自检 `Get-QtAgentBaseUrl` 与运行入口一致，先用 `/usr/bin/wslinfo --networking-mode` 确认 mirrored，再取 `http://127.0.0.1:17600`，未知保留 NAT/offline，依据[官方 `hostAddressLoopback` 默认 false、127.0.0.1 不依赖该开关的说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config#experimental-settings)；主控当时仅修正该模块、保持其余组件并在清理未交付 out 后同目录 fresh 重打，最终包已验货交付，SHA 见 §8。

## 1. 来源、权限与修改前核验

- 工作区 `/mnt/c/users/anlin/desktop/work`，分支 `main`，HEAD `402e5a5c8b35cf12a27ea25b937503bd8c041ee8`；实际基线包含大量既有 dirty，不以 HEAD 单独代替文件字节。
- 遵循 [AGENTS](../../AGENTS.md) 与 [配置变更保护机制](../../.codex/CONFIG_CHANGE_GUARD.md)。已读项目续接技能、PROJECT_CONTEXT、HANDOFF 顶部；其中历史交付和待办不覆盖 10 月 30 日新指令。
- 输入：[本轮 PLAN](../../outputs/windows-im-release-20261030/PLAN.md)、[before-manifest](../../outputs/windows-im-release-20261030/before-manifest.json)、[安装实现交接](../../outputs/windows-im-release-20261030/installer-implementation.md)、[运行实现交接](../../outputs/windows-im-release-20261030/runtime-implementation.md)、[上轮审查](../../outputs/windows-im-audit-20261030/REPORT.md)。上轮 14 项按 INST 5、RUN 5、网络 1、启动 1、包闭包 2 计，不累计不同快照的测试数。
- 修改前 00～07 共 8 份文档 SHA256 均匹配 before 清单与主控 `before/docs/` 原字节备份。最新编号为 R6-118，本轮登记唯一 R6-119；03 保持 LF，其余保持 CRLF，无批量换行或全文替换。
- 本席仅改上述七份分册、本报告及本轮 `docs-report.md`/文档验证证据；06 保持原字节。不改产品、测试、规则扫描脚本或其他角色报告，不暂存、commit/push，不构建、真装、重启或清理运行环境。

## 2. 用户原要求、旧行为与处置矩阵

下表保留本席首次规格修订时的逐项合同与现场未验边界；同日主控已完成代码修复及本地独验，最新冻结与分层结果见新增 §7。旧实现报告的候选状态不作为当前未修复判断，已通过的本地独验也不替代真实安装/账号验收。其余地址/认证、账号、邮件路由和原功能保持。

| 项目 | 旧行为/冲突 | R6-119 本轮合同与 owner | 状态/保留边界 |
|---|---|---|---|
| INST-01 | Inno 未正确消费 resume_step，verify-kernel 恢复先撞 PRECHECK/写前 baseline | 03 §2.12：先校验同 SID/版本/manifest，分派内核专用验证或回滚，再继续未完成阶段 | 修改；保留原确认和恢复门，实际 Inno 入口/真实坏内核重启待验 |
| INST-02 | 新 EXE 随机 stage 找不到旧 journal | 03 §2.13：先验新载荷，再按同 SID/manifest/live 根创建时间/marker/事务 ID 找唯一可信旧事务，沿旧 phase 续跑 | 修改；ACL/reparse/未知或歧义拒绝；DONE 仍须新清空确认，中断真装待验 |
| INST-03 | WSUS 不区分已启用功能而误拦 | 03 §2.4.2：仅需获取/启用载荷且策略阻断时拦截；Enabled/EnablePending 不因 WSUS 本身失败 | 修改；重启等待、载荷移除和其他策略门保持，域环境未验 |
| INST-04 | 无公网默认路由时找不到本地 Agent | 03 §2.7：只在受管 qtrade/eth0 取唯一 global RFC1918 IPv4，歧义/公网/其他发行版拒绝 | 修改；不遍历端点送 token，不要求公网作为离线前置；离线真装未验 |
| INST-05 | explorer SID 查询失败被当作无人值守 | 03 §2.4.1：无 explorer 与查询失败分开；空 SID、多 SID、查询异常均拒绝 | 修改；不猜选提权用户，跨用户 UAC 实机未验 |
| RUN-01 | 机器级偏好写受保护 config 根失败；自启与保存可能半成功 | 01 §7、03 §6，00/02/07 引用：固定 `config/console/console.toml`，仅安装 SID 对专用目录 Modify；新文件优先，旧 `config/console.toml` 仅读兼容；失败恢复自启与缓存一致性 | 修改；父根/config 仍 Users RX，Administrators/SYSTEM 保持 FullControl，不放宽可执行载荷或 Vault；真实普通用户 ACL/UI 未验 |
| RUN-02 | 登录任务未覆盖默认 72h 上限 | 03 §2.8.2：显式 TimeSpan.Zero / PT0S，无限执行期，保留 3 次/PT1M 重试与电池设置 | 修改；不注册真实任务、不把纯设置对象或短测当跨 72h 实测 |
| RUN-03 | 缺锁屏参数被转换为 false，active console 不等于 holder 会话 | 04 §2.5.1、02 §2.5：实际持有会话探针，未知保守锁屏，不信请求参数覆盖，慢探测后复核 | 修改；锁屏写闸和读取原职责不变，不重登；锁屏/RDP 多会话未验 |
| RUN-04 | SYSTEM 全局按 Narrator.exe 名称结束进程 | 05 §2.4.3、02 承接：本次仪式创建目标，经实际管道对端身份校验，再在同一 handle 复核 PID/creation/SID/session/系统路径后终止 | 修改；不认领既存/手动进程、PID 复用或身份不明拒绝、失败审计、公开响应无内部目标；真实启动/终止未验，不用真实 kill 复现 |
| RUN-05 | stage 前捕获 None，apply 落入清除 kernel 语义 | 02 §2.4.1：stage 成功后才构造非空路径；缺路径/stage 失败不下发 apply；None 只归 rollback | 修改；原确认、验证、回滚、审计不变，真实内核切换未验 |
| NET-MIRRORED | 无 NAT 网卡时自动候选缺回环且可能取 LAN 网关/DNS | 04 §2.6.3、02 承接：本地 `/usr/bin/wslinfo --networking-mode` 确认 mirrored，显式配置优先，自动只取回环；hint 写前重新核模式 | 修改；保留 NAT、私网过滤、鉴权与窄监听，不改宿主模式；真实模式切换未验 |
| BOOT-RETRY | collector 首次暂态失败后，主应用 started 导致不再初始化 | 02 §2.6：本地调度器 5/10/20/40/60 秒封顶退避，成功复位，停止取消、单 worker；永久配置错误保留诊断 | 修改；封顶的是间隔，不虚构最大轮次；不重放业务或改对端；本席只读核 app.py，最终冻结回归由主控/独立测试方提供 |
| PACKAGE-CLOSURE / SHARED | 干净 Agent wheel 缺 relay/server 运行闭包，依赖外部 checkout | 03 §2.2：声明并带入受控 vendor runtime wheel，冻结来源/hash，离线关键 import/初始化，不带对端应用与现场状态 | 修改；只消费冻结资产，不取得对端配置写权限；归档 65 项与最终 EXE 静态实物验货已通过（§8），真实安装仍未验 |
| PACKAGE-CLOSURE / PYJKS | pyjks 声明与 rootfs 锁/轮子不一致 | 03 §2.2：同步 requirements 输入/锁/wheels.expected 与传递依赖，离线安装与 pip check | 修改；不跳过依赖门，不拿下载完成代替目标 runtime/包验证 |

## 3. 配置保护与对端所有权

| 必查维度 | 本轮处理与保持项 | 证据边界 |
|---|---|---|
| 地址/认证 | 原可编辑 URL 与两钥、空白机输入、保存/认证/运行应用分离；不回显秘密 | 保留；本席未运行 UI/API 验收 |
| 工作台身份 | 显示名可改、稳定 ID 保持，可信已有 ID 不重建 | 保留；不修改现役身份 |
| 多采集端/账号 | 独立 slot、可见性/授权和多实例目录差异；单绑定不是发现池 | 保留；不启用旧账号或伪造联合验收 |
| 目录 | 控制台偏好专用目录只涉及本机文件；线上目录预览/发现/发布仍各自独立 | 修改偏好路径；不借此发布协议或改变其他 slot 路径 |
| 邮件 | 原 radio、表单、保存、收发和指定路由不删；原 SMTP/选路缺口继续单列 | 保留；06 未改，不拿入站认证当双向成功 |
| 持久化/运行 | 本地 owner、Vault、首次只读 seed、本地保存后不被对端覆盖 | 保留；collector 暂态重试不代表配置已应用 |
| 安全/旧状态 | CAS、owner、UNKNOWN/journal、登录态、历史数据与既有 dirty 不动 | 保留；构建后清理只限可再生产物，不能借机清业务状态 |
| 文案 | 新条款采用文件网关/邮件等现行术语，既有技术标识保留 | 不新增 UI 文案或扫描门 |

共享资产输入以主控 [peer-snapshot.json](../../outputs/windows-im-release-20261030/peer-snapshot.json) 为本轮来源证据：`xunjia_collector_runtime-2.2.12-py3-none-any.whl`，SHA256 `699d16cc6d2f799f4b3400b135f4ca2e9e61b055342351d39f72460e2e3552e6`，该快照记录 18 个 relay/server 源文件逐字节一致。本席只核读快照及 vendor 说明，未重新读取对端运行配置/凭据，未验证对端当前提交号；该来源记录不冒充对端 schema 或现役配置冻结。对端若漂移须由主控复核来源差异，不能自动覆盖或重绑。本地配置合同与 R6-114 待对齐边界仍有效。

## 4. wslinfo 更正与可追溯性

主控同轮复审已纠正初稿路径；本席只读确认 `src/qtrade_agent/winagent_client.py` 与 `winagent/src/qtrade_winagent/user.py` 当前均使用 **`/usr/bin/wslinfo`**。Microsoft WSL 2.0.4 公告明确该路径并新增 `--networking-mode` 参数；已在线核对。[官方公告](https://github.com/microsoft/WSL/discussions/10590)

`runtime-implementation.md` 早期 `/usr/lib/wsl/wslinfo` 是被纠正的历史记录，不是现行合同，本席不改其他角色报告。主控另提供本机 readlink 指向 `/init` 的证据；本席未重复执行该探针，不把该描述当本席实测。主控现已提供最新 source-freeze（见 §7）；早期测试数与源码 hash 不单独作为当前同源证据。

## 5. 本轮打包磁盘规则与目标包

本节承接 03 §2.2.2；仅为本轮构建资源硬门，不修改安装机最低空间或 R6-118 运行期阈值。

| 项目 | 本轮约束 |
|---|---|
| 目标 | 完整离线 `QTrade-Setup-1.0.7.exe`；目标文件名不是已存在或已交付证据 |
| 每卷余量 | C、D 每卷至少 60 GiB reserve，构建前核实际宿主/WSL 承载卷，不能只看 ext4 内剩余 |
| 峰值 | 本轮可再生产物跨卷合计峰值不超过 35 GiB，payload 预算 8 GiB；阶段开始前估算额外写入与余量 |
| 工作根/并发 | 单工作根、同时单个大型构建阶段；不复制多份大包/rootfs/镜像；7z 两线程 |
| 监控 | 每 30 秒记录 C/D 空闲及本轮增长，阶段前复核；预测/实测越界立即停止新增大写入并安全收尾，仅限本任务已核进程/session |
| 历史保护 | 旧 archive/final 不覆盖，不清共享缓存、现役数据、账号状态或凭据，不用 prune/宽泛删除腾空间 |
| 最终清理 | 交付后按精确本轮清单，核路径、归属、git 跟踪与进程占用，只清已核验可再生产物；保留最终包、SHA/manifest、证据/日志、before 备份及交接 |

最终 EXE 身份、源码冻结、静态实物验货、桌面复制与精确清理回执均已齐备，见 §8 及 [DELIVERY](../../outputs/windows-im-release-20261030/DELIVERY.md)；受控启动/真装及真实业务仍未验，不能由交付完成推定通过。

## 6. 文档验证与独立验收边界

- 在 `docs/` 运行 `timeout 60s python3 check-truth-tables.py`，最终稿 **exit 0，真值表全部一致**；命令、日志、hash 与结果见 [本席交接](../../outputs/windows-im-release-20261030/docs-report.md)及[文档核验证据](../../outputs/windows-im-release-20261030/docs-verification.json)。
- 本轮只矫正条款，不新增扫描规则，不修改门消红；因此不新增规则负例。若后续新增扫描规则，须由独立责任方先在定点篡改副本上证明会红。
- 文档门有 5 项现有数值提取提示，不能把 exit 0 表述为无警告；本席核对涉及条款与 owner，不扩改历史无关口径。
- 安装/运行实现报告中的定向测试归各角色、各自快照。旧 fixture 失败和构建未跑是历史记录，保留追溯；最新全量与归档结果见 §7，不由文档角色改测试或累加重叠结果。
- 本席未进行产品构建或真实执行。Win10/11 代表 SKU、域策略、离线安装、中断/重启恢复、非提权 ACL、多用户/RDP、锁屏、mirrored/NAT、跨 72h 运行、真实 IM/ACK 与原图链路均须独立记录，当前不能签署正式生产交付。

本席已核对 E 中最终交付、静态实物与清理回执并完成文档回写；不读取已删除 W/L，不修改产品/测试/独验报告，主控随后只需原子同步更新后的桌面 README。


## 7. 同日总装阶段记录（已收口）

早期总装、旧候选重打及等待验货/清理的状态已由 §8 最终完成事实替代，原文留在本轮原字节备份中，不再作为当前待办；twofish 1qtrade1 最小定位补丁、安装器 mirrored 自检与 native/console 同源复用均已纳入最终包验证。桌面 EXE/SHA256/公共 CER/README 已交付，精确清理完成，本次最终 README 文本同步由主控负责。

## 8. 最终交付与清理完成（2026-10-30）

主控 [final-source-check.json](../../outputs/windows-im-release-20261030/final-source-check.json) 最后核 **1536 files、changed=[]**，指纹与本节最终源码冻结相同，零漂移；代码、包、交付和清理已完成。

以下为当前事实；上方“压缩中”“未交付”“待回执”、旧冻结和旧测试数字均按阶段留痕，不覆盖本节。本席只读取 `outputs/windows-im-release-20261030`（E）留存证据，不读取已删除 W/L 构建根。

| 项目 | 最终结果与证据 |
|---|---|
| 桌面包 | `C:\Users\anlin\Desktop\QTrade-1.0.7-20261030\QTrade-Setup-1.0.7.exe`，**2,602,028,472 bytes**；[交付回执](../../outputs/windows-im-release-20261030/delivery-result.json) verified=true，复制后 SHA 复核 |
| 最终 EXE SHA256 | **`7efd61d9358493b0e2ac7500949c702cb0d8b9f971c32818fc37382afff4265f`** |
| 最终源码冻结 | **`afbd3e900584cd0d6417d62c41d4526735b13e170069ba4af078d08ee6fc1668`**，1536 files；替代较早候选指纹，文档不纳入产品清单 |
| 静态实物 | [独立 ACCEPT](../../outputs/windows-im-release-20261030/build-verify/DELIVERY-STATIC-REPORT.md)，session22095 / exit0；3391 manifest、1437 rootfs 绑定、26 个依赖、native/console 同源，27 签名/时间戳与篡改 0x80096010 拒绝 |
| 最终 Pester | [回执](../../outputs/windows-im-release-20261030/pester-full-result.json)与 XML：**1018 passed / 0 failed / 0 skipped**，已补齐旧 5 项实物门；Agent4296、WinAgent1572、Console690、PW44及归档65各自保留，不累加重叠数 |
| 文件清理 | [cleanup-result.json](../../outputs/windows-im-release-20261030/cleanup-result.json) success；**20,860,932,539 bytes / 70,015 files**，29 个精确根（含 W/L 两根）无剩余，最终包 SHA 不变 |
| 镜像清理 | [docker-cleanup-result.json](../../outputs/windows-im-release-20261030/docker-cleanup-result.json) success；逐本轮 label/ID 以 --no-prune 删除15镜像，无剩余；原共享base同ID、无全局prune/现役stop |

[final-disk.json](../../outputs/windows-im-release-20261030/final-disk.json) 给出 **475 次采样 / blocked_samples=0**：采样最低空闲 C **102,836,207,616 bytes（95.774 GiB）**、D **109,806,301,184 bytes（102.265 GiB）**；最终空闲 C **117,337,567,232 bytes（109.279 GiB）**、D **109,502,652,416 bytes（101.982 GiB）**。最终 D 低于采样最低，分列不混用；所有已知读数仍高于60 GiB，替代早期验货阶段98/103 GiB数值。

验货**测量峰值 23.292 GiB**（25,009,105,404 bytes，含4 GiB Docker预留）；加桌面包副本得到27,611,133,876 bytes，约25.715 GiB，向上留量的**保守上界约25.716 GiB**仍小于35 GiB；上界不是新测量。此前未交付旧候选约9.94 GB单独清理，不并入本次同时峰值或本次删除合计；文件删除字节不声称等于宿主物理回收。

保留桌面EXE/SHA256/公共CER/README、源码vendor固定输入和小报告/原字节备份；旧共享缓存、已运行账号、凭据与其他项目未动，未WSL shutdown或虚拟盘压缩。原共享base ID与完整精确清单见上述回执，勿凭旧路径复跑。未来仍先预算监测保余量，再定点清理，不改变60/35/8 GiB及单工作根、7z两线程约束。

**真实安装、重启恢复及账号业务仍未验**；内部自签根仍未信任（0x800B0109），只交公共CER、未改信任库，不代表公共CA/SmartScreen/企业白名单放行。[桌面README](../../outputs/windows-im-release-20261030/README-desktop.md)已据最终事实更新，主控依据已留旧hash再原子同步，不能把本席文档回写当该同步已发生。本轮四份原文已备份，HANDOFF原历史字节保持，文档门与最终hash见 [final-docs-verification.json](../../outputs/windows-im-release-20261030/final-docs-verification.json)。
