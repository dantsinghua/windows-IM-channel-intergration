# QTrade 安装器出包说明

规格唯一出处为 [03 安装引导](../../docs/03-安装引导与自动化配置.md)，本轮按 00 §15g R6-101～104。本文依据 2026-10-13 当前源码说明生产路径；组件候选与验证状态见 [本轮交接](../../docs/handoffs/2026-10-13-portable-installer.md)。

**当前交付定位：可交付受控安装验收的内部测试包。**最终 r2 已通过包内容/源码身份/签名完整性/限定原生自检的独立 PASS，唯一旧包级阻断 ARTIFACT-01 已关闭；同包 SFX 欢迎页完整可见、取消退出通过。完整全新安装、清空重装、内核切换及真实业务账号尚未验收，MQ 按安琳要求跳过，不宣称全域零回归。

## 本次成品与证据

交付目录：`C:\Users\anlin\Desktop\QTrade-1.0.2-20261013`。安装文件 `QTrade-Setup-1.0.2.exe`，2,598,056,832 字节，SHA256 `98ec27e99065dcecaf732b68f4e4778cda23193d6d6fca0e8cb54df5a7e26d68`；交付目录含公开 README 和五个 signing 公开文件，无 PFX/私钥。

独立 [r2 最终报告](../../.codex/package-final-20261013/artifact-r2-final-verification.md)核对 103 个 installer 来源、3377 清单项、3351 份原资产、25 个第一方签名与 DigiCert 时间戳，以及双 r8 EXE/embedded Python 自检。同 SHA 的 SFX 启动/欢迎页/取消 53.30s，进程与 stage 都 exit 2，原 SCM PID 4076 保持 running、正式 QTrade 根未创建；未点击 Next 或 Install。旧 ed6e5e 候选继续 REJECT，不分发。

签名者指纹 `E36AFD96A801DD666F953AC8E3B3C992AB8CD60E`，当前仅有自签根未信任错误 `CERT_E_UNTRUSTEDROOT`，不写 Windows Trusted/Valid；目标管理员核对后决定是否导入。安装的共享 WSL 内核切换可能在确认后停止其他 WSL/Docker Desktop 进程并要求重启；其他工作负载不在清空范围不代表运行不受影响，应在受控测试机安排中断窗口。

本次 README 是验货后的纯文档更新，不在包内、不参与执行；103 来源中只有本文件相对冻结改变，产品码与冻结构建副本保持原样。精确文档差异见本轮 `docs-delivery-final-verification.json`，不改写原验货 SHA。H03 自动检查/重启及初启 not_ready 后 recover 两项差异继续开放，详见 [运行交接](../../docs/handoffs/2026-10-13-local-runtime-e2e.md)。

## 1. 正式出包边界

- `build.ps1` 使用当前已冻结源码与独占产物根，显式给版本、SourceRoot、OutDir 及已核验的 SfxStubPath；不把本机旧产物目录当最新源码。
- 外壳必须是本版带随机暂存实现的 `QTradeSD.sfx`。`Stub=official` 被拒绝，`auto` 也不回退官方存根；旧 `precheck-disk.cmd` 搬运入口已禁用。只改文件名或使用旧 marker 不构成新存根。
- SFX 经 Known Folder 定位 ProgramData，在 `QTrade-Staging/<GUID>` 独立暂存，先全量校验再确认清空旧安装。不得直接解压覆盖正在使用的正式根。
- 新包是全量离线载荷，含版本化 rootfs/镜像/APK、WSL MSI、内核、Windows 工具链运行文件与本版引擎；安装目标机不依赖构建机 Docker、WSL 名称、代理、地址或账号。
- `AllowMissing`、`SkipEngine`、`SelfCheck` 属验证/诊断用途，结果不能交付为正式完整安装包。`CheckOnly` 不等于完成归档、签名或真装。
- 不在被收集的 payload、out、dist、release 根作为工作目录，避免工作状态文件进入归档；不携带 `.omc/.omx/.codex`、node_modules 源树、私钥、令牌、登录态或测试数据。

## 2. 工具和输入

| 项 | 当前要求 |
|---|---|
| PowerShell | Windows PowerShell 5.1；脚本 Parser 只解析，保留 BOM/既有换行，新 Windows 字符串/注释使用 ASCII |
| 引擎编译 | Inno Setup 6，中文语言资源随仓库；版本由本次 Version 传递 |
| 归档 | 7z；保持两段压缩与归档路径复核 |
| SFX | 固定来源 SDK、当前补丁编译结果与原验货门通过；[SFX 说明](../sfx-stub/README.md) |
| 源载荷 | collector 的 SourceRoot/各项精确覆盖路径；kernel/third_party 可复用经验证的静态件；Agent/WinAgent/console 必须由本轮冻结源产生 |
| 签名 | `Sign`、`CertThumbprint`、`SignToolPath`、可选 TimestampUrl；使用既有获准证书，不输出私钥或凭据 |
| rootfs | [rootfs 说明](../rootfs/README.md)；18 个运行依赖的 lock、wheel 实物清单、pip check 不放宽 |

WinAgent 包括 svc/user 两个 onedir 程序及嵌入式 Python 内的当前 `qtrade_winagent` 模块，不能只替换 EXE 而携带旧 site-packages。控制台须从 ext4 独占副本构建目录产物，不在 `/mnt/c` 安装 node_modules。

r8 svc/user 两 spec 另只移除 `.dist-info/direct_url.json` 构建来源，保留其它 metadata、入口点与 license；双平台各 874 项和实际 r8 组件通过，最终包无该来源记录。用户 EXE 不带构建机生成的 `comtypes.gen.*` 包装或绝对 typelib 路径；保留基包，运行时按目标 Windows 类型库仅在内存生成。运行钩子保留默认 MTA 和外部显式 COM 设置，导入后恢复原先不存在的标志；不能以清除构建缓存为由改变线程模型或豁免新警告。r6 的 STA 警告曾被独立门拦截，最终 r7 自检 stderr 空、包和缓存无新增写入，详见 [COM 独立报告](../../.codex/package-final-20261013/comtypes-test-report.md)。

scrcpy 附带的 `adb.exe`、`AdbWinApi.dll`、`AdbWinUsbApi.dll` 须与主 platform-tools 及 rootfs 的 37.0.1 对齐。本轮只在新私有载荷根覆盖三件套，旧 37.0.0 来源与其余 scrcpy 文件保持不变；旧/新摘要和来源见 [ADB 覆盖记录](../../.codex/package-final-20261013/payload-adb-override.json)，没有执行 ADB。

## 3. 收集、签名与完整性顺序

1. 对明确输入做冻结、记录源码/产物 SHA 和工具版本；组件测试必须对应这些输入。
2. 编译引擎；第一方 PowerShell、WinAgent 两程序、Electron 主程序按现有签名流程在副本中签名，第三方二进制保持上游签名。
3. collector 将所有实际文件枚举到 manifest：精确相对 path、最终 dest、SHA256、size。tree/glob 只用于来源发现，正式 manifest 不能残留通配项、空摘要或目录占位。
4. 最终 `install/engine` 的 EXE、CMD、PowerShell 模块一并收集；签名或复制任何文件后重新生成清单，不能沿用签名前摘要。
5. 归档后复核 manifest 路径；拼本版 SFX；最后对整体 EXE 签名，签名覆盖存根、配置和归档。保留归档 hash、包 hash、证书状态与完整 manifest。
6. 对 stage/最终包执行独立验货，确认 `lightweight=false`、无缺件、无开发杂物、无本机配置；产物尚未运行必须明确标注。

原脚本编码、Parser、CMD CRLF、防火墙唯一拥有者、微信禁止静默卸载、SFX 配置与身份、签名、版本锁定和 wheel 门均保留。质量门失败应修产品或交质量门责任人核验，不能为出包放宽。

## 4. 目标机行为与凭据

运行正式包会请求管理员权限；是否进行真装须在已授权隔离机器执行。清空确认只作用于归属已核实的 QTrade 根、服务、任务和专用发行版；微信/QQ 客户端自有数据和其他 WSL/Docker 不在清空范围。旧 `upgrade/repair/uninstall` 入口明确停用，详见 03 §2.13/§2.14。

清空中断可凭同一包、SID、精确根路径、目录创建身份与随机事务 marker 恢复；旧 `install_state` 已被删不代表可以猜测归属。同一已确认私有根内的普通残留文件属于用户整根清空范围，**不宣称逐文件鉴别来源**。根被替换、reparse 跳出、外来服务/任务/发行版均拒绝；进入 `root_empty` 后若出现新子项也拒绝，最终根只做非递归空目录删除。

rootfs 里没有本机 install.env、machine-id、令牌或账号。目标机选 Docker 网段，所有候选冲突就停止；WinAgent 以安装 SID 绑定 SCM/命名管道，安装器通过可信 bootstrap 和 stdin 完成 guest 初始凭据，正式控制台从同一管道发现实际 Agent 地址。

内部自签名证书不等于任何目标机自动信任；仅分发公钥证书和获准导入说明，不分发 PFX/私钥。目标机信任策略、SmartScreen/AppLocker 与真实启动结果须分别记录，不能以构建机的验签结果替代。

## 5. 代码签名(自签名阶段)

`New-QtSelfSignedCert.ps1` 是既有建证书入口；本轮应复用已授权证书，不因打包自动创建新信任根。总装签名参数组合为 `-Sign -CertThumbprint`，取实际证书指纹；公司 CA/OV 证书同样按指纹选择，须核私钥可用及有效期。

公钥 CER 的默认导出目录由操作系统当前用户 `LocalApplicationData` 推导为 `QTrade\signing`（通常显示为 `%LOCALAPPDATA%\QTrade\signing`），不再包含固定用户名或构建机路径。显式 `CertDir` 优先；只读取默认配置不会创建目录、生成证书或导出文件。私钥仍在所选证书存储，默认目录不是私钥导出目录。

目标机仅分发公钥 CER、`Import-QtCodeSigningCert.ps1`、`Remove-QtCodeSigningCert.ps1`、`QTrade.Signing.psm1` 与 `导入QTrade签名证书.cmd`；不分发 PFX 或私钥。导入脚本核 ExpectedThumbprint 后写 LocalMachine 的 Root 与 TrustedPublisher，是整机信任变更，须在用户已授权的目标机执行。文档核对脚本行为，不在本批导入证书。

**怎么撤销**：既有 `Remove-QtCodeSigningCert.ps1` 按精确 Thumbprint 从上述两个存储移除对应证书。撤销信任不卸载应用，但后续签名信任判断会改变。没有在本批执行撤销或新增可执行命令示例。

## 6. 本轮验证和交付门

文档更新本身不执行构建、证书导入或安装 EXE；没有新增可直接执行的安装命令样例。具体已执行命令及结果保留在 `.codex/package-20261013/` 的各责任人日志，并由可携带交接概括。

本轮完整 Pester 928 passed / 0 failed / 0 skipped，安装器 Python 271 passed / 0 failed / 0 skipped；精确冻结范围、三份冷归档摘要和旧 RED 记录见 [独立报告](../../.codex/package-final-20261013/remaining-tests-report.md)。假宿主与编译检查不执行安装 EXE、不证明真实清空重装或安装完成页视觉。

安装器只读终审已 [APPROVE](../../.codex/package-final-20261013/installer-review.md)。本轮总装签名与最终包独立复核已通过；后续修改必须重新核输入和必要验收，完整目标机安装仍须在获准环境验证。Hyper-V 历史日志证明完整 WSL 曾启动，新本地 E2E 也已有真实 SCM/Electron 联动证据；两者均不证明新微型 kcheck、内核切换或 1.0.2 完整真装通过。不得把历次分批通过数字相加宣称零回归。

**真机验证清单**还必须包含小盘 VM 的解压前空间拒绝、stage/live 复制中满盘与恢复边界；不得只用静态算式替代低空间实测。
