# 2026-10-13 最新本地桌面与服务 E2E 交接

## 最终 r2 结论与本地验证边界

最终包已获独立有限范围 PASS：包级旧阻断 ARTIFACT-01 关闭，内容/源码/签名完整性/限定自检通过；同SHA SFX欢迎页启动及取消53.30s通过、欢迎完整可见，未执行安装步骤。交付定位为**供受控安装验收的内部测试包**，桌面目录 `C:\Users\anlin\Desktop\QTrade-1.0.2-20261013`。文件2,598,056,832字节，SHA256 `98ec27e99065dcecaf732b68f4e4778cda23193d6d6fca0e8cb54df5a7e26d68`，细节见 [安装交接](2026-10-13-portable-installer.md)。

原 r8 本地90.03s/12健康/手动WS恢复证据维持其范围。完整全新安装、清空重装、共享内核切换、真实账号与MQ未验，H03/初启recover差异仍开放；自签根未获系统信任。实际安装在确认后可能中断其他WSL/Docker Desktop进程并要求重启，应使用受控测试机；不在清空范围不等于没有运行影响。下方历次失败和待验状态均是时间快照，当前包状态以上段为准。

## 验货前出包状态：r2 总装成功，等待独立验货

r8/rootfs r4 新候选已全流程总装，安装 EXE 2,598,056,832 字节，SHA256 `98ec27e99065dcecaf732b68f4e4778cda23193d6d6fca0e8cb54df5a7e26d68`。独立包验货与同包 SFX startup/cancel 正在执行，**尚未最终 ACCEPT、尚未交付**。下方真实 r8 本地环境结果保持有效范围；不能以此替代整包验货或完整首次安装。具体路径与公开交付 README 状态见 [安装交接](2026-10-13-portable-installer.md)。

## r8 运行与总装前检查点

首签名候选 `ed6e5ea84d68fc014cc4722b5a5bdf259e98ebac44386e271ea9d680d1ec8bac` 的 SFX 启动/取消冒烟通过，但独立验货因 `.dist-info/direct_url.json` 残留本机构建来源而 REJECT，不能交付。r8 两 spec 最小过滤后独立组件 ACCEPT/终审 APPROVE、WinAgent Linux/Windows 各 874 passed，82 测试及 47 构建输入匹配；运行代码语义与其余 metadata 保持。新包 `out-r2` 正在全流程重签压缩，组件修复不能代替新包验货。详见 [安装交接](2026-10-13-portable-installer.md)，旧包的拒交付标记和历史证据均保留。

本地已替换为 r8 的 412 个 app 文件，部署校验全部通过，未强杀旧 SCM；当时 SCM PID 4076、UserAgent PID 41804（仅快照，操作前重核）。`runtime-watch-r8.log` 连续 90.03s，6 个状态样本均 SCM running/UserAgent alive/qtrade running；100ms 窗口采样无新增可见控制台，正式 QTrade 根保持不存在。

`desktop-stable-health-r8.log` 的 12 次真实健康均 HTTP 200、dockerd true、WA/user_agent 在线，跨 docker_probe runs 6→7，accepted=true。早期 `desktop-final-health-r8.log` 的 `Failed to fetch` 留档，不改成成功。首次 WS 重连点击被告警 drawer 遮罩拦住而超时；关闭遮罩后，`desktop-ws-reconnect-r8-after-alert.log` 为 hadRetry=true/connected=true/retryVisible=false/errors=[]，后续 10s 实时连接稳定、无 pageerror。该结果证明人工恢复出口，不能宣称初次点击、所有网络时刻或无限自动重连均成功。

r8 svc SHA256 `40e5c021c51a2c39f05220a9fdd196f2469f3c5211101f9cd59f2d65ad889b34`，user SHA256 `bc2709a1c4df1ddfbc347382d8cbad94354ba6a9c3392937f9a92103bbb7b9f3`；rootfs 沿用下方已验 r4。元数据修复证据为 [独立验收](../../.codex/package-final-20261013/metadata-test-report.md)、[终审](../../.codex/package-final-20261013/metadata-build-review.md)。服务自检保留原依赖 warning 且逐字匹配冻结基线，用户自检仍 stderr 空；没有把“既有 warning 未新增”写成全部警告消除。

本地 fixture（reachability_probe=[]、CAP_SYS_TIME 限制、空账号）与下方 H03/初启 recover 开放项不变。MQ skipped、完整安装/真实账号未验，自签根未获本机信任；此次连续运行与健康证据不代表所有缺陷清零或最新包已交付。

## r7 + rootfs r4 阶段：Docker 冷启顺序修复，真实健康恢复

已定位此前 Docker 一直 false 的根因：普通 service 隐式 `After=basic.target` 与配置单元必须早于 docker.socket 的关系构成冷启循环。只改 `qtrade-docker-config.service`，关闭默认依赖并显式补回 sysinit、local-fs、shutdown 顺序，原有条件、配置 marker、60s 超时和失败阻断保持。真实 guest 修改前 verify 虽 exit 0，却输出 ordering cycle/job deleted；修后 stderr 为空，未通过忽略诊断或反复重试掩盖。

最终 r4 rootfs 为同一构建根 `rootfs-r4/out/rootfs.tar`，2,917,068,800 字节，SHA256 `0562050447e2d7ec4224045ebab3de992ffcefa647e0352474e09bdf99890de1`。新 unit SHA256 `da2cffa35d5c705a6d28243b7a042ee048ef340502499ddefe76dd0e71590830`。独立完整排序图旧 r3 **1 failed / 9 passed**、新 r4 **10 passed**；r4 冷归档和关联 shell **57 passed**，另有只读单 unit 终审 APPROVE。报告：[排序独立验收](../../.codex/package-final-20261013/docker-ordering-report.md)、[最小差异终审](../../.codex/package-final-20261013/docker-ordering-review.md)。离线 host systemd 255 对归档的解析不冒称 guest systemd 249 真执行。

真实操作仅经 WA stop/start 本轮自有 qtrade，两次均 HTTP 200；已配置 guest 的 Docker service/socket 自动恢复 running，配置单元因既有 marker 跳过符合预期。这是已配置环境冷启，不是重新导入 r4 或完整首次安装。`desktop-stable-health-r7-rootfs-r4.log` 的 12 次健康均 HTTP 200、auth ok、dockerd true、WA/user_agent 在线，跨 docker_probe runs 2→3，errors/skipped 均 0，`accepted=true`。

长停机触发控制台原有有界 WS 重连暂停，点击重新取令牌并重连后恢复真实实时连接，10s 稳定观察无 pageerror；`desktop-ws-reconnect-r7.log` 为 `hadRetry=true/connected=true/retryVisible=false/errors=[]`。这证明人工恢复出口，不能写成长时间断开后无限自动重连。下方 r7 首轮失败与 r5 证据保留，不删除或重标。

最终安装包已进入 7z 压缩，尚未产出最终签名包与哈希；MQ 仍按安琳要求跳过、不记通过。`reachability_probe=[]`、guest 去除 `CAP_SYS_TIME` 和空账号隔离保持。剩余开放项见下节，不能宣布全部缺陷清零。

## 明确开放项：H03 与初启账号恢复

- 04 H03/F-03 规格要求 `systemctl is-active docker` 加 `docker info` 及自动 restart/恢复；当前 `AgentApp.dockerd_probe()` → `Runtime.dockerd_ok()` → Docker `info` 仅更新健康/告警，没有执行 systemctl 状态检查或自动重启。这次只改 rootfs unit，未改或验收该运行期动作。
- 02 §5 写初启 `not_ready` 后每 10s 重探、就绪再 recover；当前 `AgentApp.start()` 只调度一次 `accounts.recover()`，30s Docker probe 没有重触发该流程。Docker 迟到就绪后的账号恢复闭环有实现/规格张力，本轮空账号冷启不能证明其通过。
- 这些差异已在 02/04 原条款附近登记，保留规格要求，不用降级文档冒充实现完成。真实微信/chatlog 卸载、业务账号收发、其他 Windows 版本和完整 SFX 真装仍未覆盖。

## r7 首轮检查点（历史）：COM 与无窗通过，Docker 健康未通过

安装器 R3～R6 与签名默认路径已独立 APPROVE（Pester 928、Python 271），用户 COM 打包修复 r7 已独立 ACCEPT；最终签名安装包仍未总装。下面 r5 的 180s/8 页面/健康结果保留为该阶段证据，不能移记为 r7 结论。

r7 已在同一隔离根部署 413 个文件（原 r5 为 417），SCM PID 56108、UserAgent PID 54404 为本次快照，操作前重核。`runtime-watch-r7.log` 连续 90.08s：6 个 SCM/UserAgent/qtrade 样本全在线，100ms 窗口采样无新增可见控制台；正式 `C:\ProgramData\QTrade` 仍不存在。

**Docker 健康尚未通过**：首个 `desktop-final-health-r7.log` 在 Agent uptime 12s 时 `dockerd=false`；后续独立 `desktop-stable-health-r7.log` 的 12 个样本（uptime 67～100s）均为 HTTP 200、auth ok、WA/user_agent 在线，但 `dockerd=false`，`accepted=false`。这是当时真实运行未闭环的证据，后由上方 unit 修复及独立新观测关闭；不能解释成无需修复的短暂启动状态，也不能以服务进程存活或 r5 旧绿灯替代。

COM 修复的独立范围：旧 r5 冷包含 4 个构建机包装及 2 处绝对 typelib 路径；r6 删除缓存后改变默认线程模型，新增 STA 警告被原严格门拦下。r7 保留默认 MTA/外部显式设置，按目标 Windows 在内存生成包装；Linux/Windows 全量各 852 passed，81 输入无漂移，47 构建输入匹配。17 模块原生自检 stderr 空、包与缓存零写入，合成 child helper 验收通过；未运行真实微信/chatlog 业务，也未在不同 Windows 版本真装。详见 [独立报告](../../.codex/package-final-20261013/comtypes-test-report.md)、[验证索引](../../.codex/package-final-20261013/comtypes-verification.json)。

r7 用户 EXE SHA256 为 `ef208a892dea6f91fa7b9e3224005338c853eb47d5bc9501ee84ac12a35f5a88`，服务 EXE 仍为 `234e325472e883c4a4a68c2b501c2df1e31f66b598ef0bd68f1f23ae05ae9c01`。本节运行证据在 `.codex/local-e2e-20261013/`；r6 失败、r5 历史与 r7 新结果分别保留，不累计次数。MQ 按安琳要求跳过，不记作通过。

## r5 阶段结论与本轮授权

安琳要求本地 Windows 桌面和本地服务均更新至最新代码，并做功能 E2E；已明确授权本次使用 gsudo 与 UAC。最新打包桌面、WinAgent 和专用 WSL 的下述本地启动/恢复/页面联动验证已通过；仍不能宣布全部真实业务完成或全域“0 回归”，也没有生成可交付的 1.0.2 总安装包。安琳后续明确“跳过 MQ 认证测试”，因此真实 MQ 认证不再作为本轮打包前置；既有 401 与页面认证失败保留，不能写成认证通过。

本轮基线为 `claude/lucid-dijkstra-uu5max` / `6c5b941482d4f711a4481d18b771f9b48e16d917`，大量共享未提交修改，正式布局仍是 `console/`、`src/`、`tests/`、`winagent/`；不自动提交、拉取或重置。局部报告各有冻结摘要，不能将不同运行的通过数累加为同版总验收。

- 真实测试安装根：`C:\ProgramData\QTrade-E2E`；正式 `C:\ProgramData\QTrade` 仍须保持不存在。
- 本轮身份：`local-e2e-20261013-w9ftw0du`；所有权标记为测试根下 `local-e2e.json`，操作前核验标记、目标路径与实际进程身份。
- Windows 构建/桌面状态根：`C:\Users\anlin\qtrade-build\local-e2e-20261013-w9ftw0du`；WSL 构建根：`/home/anlin/work/qtrade-build/local-e2e-20261013-w9ftw0du`。桌面使用独立 userData/APPDATA/LOCALAPPDATA，不接旧账号数据。
- 真实组件：SCM `QTradeWinAgent`、安装用户拥有的专用 `qtrade` WSL、打包 Electron；本轮 SCM 设为 demand，避免测试服务随开机恢复。这是测试隔离设置，不改产品默认启动方式。
- 授权不扩展到旧工作账号、真实外发、ADB、全局 WSL shutdown、宿主重启或其他容器生命周期；本轮不改 frp/FlClash。

## 已观察到的真实故障与修复

| 问题 | 实际原因与修复 | 当前证明范围 |
|---|---|---|
| 普通用户代理启动 `PermissionError` | 用户 EXE 写入 SYSTEM 服务日志目录。改为 `%LOCALAPPDATA%\QTrade\logs\user\winagent.log`；缺完整用户目录则拒启，不放宽服务/Vault ACL | r5 用户 EXE 已在普通用户会话运行并写入独立用户日志，未放宽服务根 ACL |
| 桌面反复闪黑框 | 后台周期调用 `wsl.exe` 未设置隐藏控制台。公共 Windows CLI 和异步 WSL 入口统一使用 `CREATE_NO_WINDOW`，保留输出、错误码和取消处理 | 保留当时停止 PID 17664 的过程记录；r5 连续 180.09s 窗口采样未发现新增可见控制台。真实微信账号动作未覆盖 |
| Docker 首启失败 | rootfs 没有 PATH `python3`，配置脚本却使用裸命令。改为随包 `/opt/qtrade/agent/venv/bin/python` | 精确脚本覆盖到本轮 guest 后真实 firstboot 成功；r2 权限 RED 保留，r3 同归档内容与权限 39 项通过，见下文 |
| `host.json` 无法写入 | 会话代理也依赖裸 `python3`。改为随包解释器，仅在已运行的受管 guest 写入公共地址元数据 | 已进入最新 EXE，本轮 Agent→WA 与桌面联动在线；解释器/写入失败边界另有独立回归 |
| WA 监听错网卡 | 实际描述带 `Hyper-V Virtual Ethernet Adapter #2`，旧精确相等检查漏掉，错误回退 Default Switch | 源码现场探针识别 WSL 网卡；SCM 已更新 r5，最终 Agent 健康显示 WA 与用户代理在线 |
| WSL 启动后空闲退出 | systemd 服务本身不足以维持 WSL 生命周期，原 `/bin/true` 立即退出 | 移除临时诊断持有者后由产品 UserAgent 自动启动；r5 180.09s 的 12 个状态样本均在线，显式 stop 后 131.16s 未复活 |
| guest 不在线时无法恢复 | 凭据管道把全部令牌一起拒绝，控制台拿不到调用 WA `wsl.start` 的凭据 | 保留 OS SID 门，允许 WA-only 凭据及严格 null/null Agent 字段；未知/旧 Agent 源仍不能获令牌或业务请求。Setup 恢复与版本字段修复已过独立回归，真实桌面已恢复连接；8 页面稳定复验健康 200、页面错误 0 |

正式契约已同步：00 R6-103、01 §2.3、02 §2.1/§7.2、03 §2.7.3、04 §2.6.3、07 `[wsl]`。这批修复不增加新的业务能力或配置开关。

生命周期细节：`autostart` 初始化 `desired_running`；手动 stop 撤销意图，60s 维护循环及管道重连不重置它。READY 等待最多 30s，取消也回收已创建的 child；正常退出先关 stdin，5s 未退才终止本实例 child。最终退出不自动全局 shutdown。只有 `issue_bootstrap` 额外要求提权，普通控制台凭据仍只核安装用户 OS SID；地址发现 5s、客户端总等待 8s。

## r5 阶段真实执行与测试隔离

本轮真实完成 SCM LocalSystem 启动、DPAPI bootstrap、guest WA token 注入与 console hash 绑定、firstboot 离线镜像加载及 Agent 建库启动。最终运行快照为桌面 r2 PID 30428、SCM r5 PID 53348、UserAgent PID 51872；PID 仅用于证据定位，接手必须重新核身份，不能按旧 PID 操作。

| 真实场景 | 结果与证据 |
|---|---|
| r5 自动启动和持续在线 | `session/runtime-watch-r5.json`：180.09s，12 个样本均为 SCM running / UserAgent alive / qtrade running；100ms 窗口采样未发现新增可见控制台窗口。不是逐帧全桌面检测 |
| SCM 正常停止与重连 | 阶段 r4 的 `scm-r4-restart-test.log`：stop 0.2s，重新启动 34.91s 后 Running，用户代理重连。r5 替换记录 `update-runtime-r5-native.log`：未强杀旧服务，417 文件校验成功；不要把 r4 时间移记为 r5 测量 |
| 显式停止 WSL | `session/wsl-stop-stability.json`：131.16s 内 UserAgent 存活、专用 qtrade 持续停止，`passed=true`；随后恢复当前运行环境。未触发其他发行版或全局 shutdown |
| 实际桌面页面 | `session/desktop-routes-r2.json`：`/dash /acct /msg /mail /log /res /env /set` 共 8 页，`pageerrors=[]`、`network=[]`、health 200。早期一次瞬时 fetch 失败保留原日志，以上为后续稳定复验，不声称全过程从未出错 |
| STS 页面→实际 Vault | `session/sts-real-vault-result.json`：使用合成参数保存至真实 DPAPI Vault，成功后清空秘密输入，刷新仍显示已配置，公开响应/浏览器持久化未发现明文。认证测试为失败，不改成通过 |
| 新 Agent 启动读取配置 | `session/final-health-r5.json`：auth ok，WA/user_agent online、dockerd true、账户数 0；`restart_required=false`，询价/目录仍 `configured=false`，所需业务配置未齐，未启动业务 |

`session/` 上述相对路径均位于本轮 Windows 构建根；SCM 等日志在 `.codex/local-e2e-20261013/`。r5 服务 EXE SHA256 为 `234e325472e883c4a4a68c2b501c2df1e31f66b598ef0bd68f1f23ae05ae9c01`，用户 EXE 为 `77c9bb41bb18b4fa32932753e6e87f6278952df75b70b0ecfcddf24838929d91`；两者构建和 `--selfcheck` 均 exit 0，合并 417 文件且 shared zip payload 相同，见 `runtime-r5-build-result.log` 与 `merge-runtime-r5.log`。

guest 保留测试专用 systemd drop-in，移除 `CAP_SYS_TIME`，避免改变共享 VM 时钟；WinAgent 测试配置 `reachability_probe=[]`，因此此轮不证明常规外网连通探针或 H13 调时成功。微信模块关闭、数据库无真实账号，临时诊断持有者已退出，保活由产品承担。若后续恢复常规探针，须另记实际配置与验证，不能抹掉本轮隔离条件。

桌面独立状态目录可写，这不等于正式 ProgramData ACL 下全部偏好写入场景已验收。真实微信/chatlog DLL 卸载、账号收发、企点询价、微信选群/转发、OSS 业务与完整 SFX 安装均未被本轮空账号环境覆盖。实际 MQ 成功认证按安琳新决定跳过，状态仍为未验证/已有失败。

## 独立回归证据

以下各项按报告自己的冻结点成立，包含重叠用例，不作累计总数。

| 证据 | 实跑结果 | 限制 |
|---|---|---|
| [启动辅助脚本报告](../../.codex/local-e2e-20261013/bootstrap-tests-report.md)、[冻结记录](../../.codex/local-e2e-20261013/bootstrap-verification.json) | PS 5.1 六文件 220 passed / 0 failed / 0 skipped，15 输入无漂移；覆盖可信 bootstrap 顺序、health DTO、完成态 token 传递及地址数组解析 | 宿主动作均 mock，不是 SCM/WSL 真装 |
| [r5 最终记录](../../.codex/local-e2e-20261013/runtime-r5-final/verification.json) | WinAgent Linux 829 passed、Windows 829 passed；82 输入、46 构建源文件无漂移，AST 通过 | 同冻结假后端全量，不能把两平台相加为 1658 个独立用例；725/87 等为中间报告 |
| [冷启动恢复报告](../../.codex/local-e2e-20261013/console-recovery-tests-report.md)、[冻结记录](../../.codex/local-e2e-20261013/console-recovery-verification.json) | Python pipe/shutdown 17 passed；前端专项 61、全量 541 passed；typecheck 0；lint 0 error / 37 既有 warning；178 输入无漂移。旧 Python 4 RED、旧前端 11 RED | 假 IPC/API/OS，不替代 Electron 真机恢复 |
| [r2 冷归档记录](../../.codex/local-e2e-20261013/runtime-cold-archive-r2.json) | 11 passed；包内脚本与修复源码逐字一致，解释器解析到可执行 ELF | 仅内容/身份检查；不能覆盖下面权限失败 |
| [r2 权限 RED](../../.codex/local-e2e-20261013/rootfs-permission-r2-red.json) | 18 failed / 10 passed；存在 13 个 0777 文件，另有静态数据多余执行位 | 历史失败归档保留，不交付 |
| [r3 权限与身份联合复验](../../.codex/local-e2e-20261013/rootfs-permission-r3-green.json) | 同一最终 tar 39 passed / 0 failed / 0 skipped，四输入无漂移；沿用 RED 的原始权限测试 | 冷归档元数据与隔离脚本，未将 r3 完整重导入或执行 SFX 安装 |
| [Agent 新一轮全量](../../.codex/local-e2e-20261013/agent-full-report.md) | 3116 passed / 0 failed / 0 skipped，6 warnings，351.59s；311 冻结输入无漂移 | 本轮新跑，区别于较早 STS 批 308.59s；仍为源码回归 |
| [整改终审](../../.codex/local-e2e-20261013/final-review.md)、[r5 helper 真执行](../../.codex/local-e2e-20261013/wx-background-frozen-helper.json) | 原三项问题关闭，APPROVE；冻结 EXE helper accepted=true，自造 child 收到 SIGBREAK、cleanup、exit 0，七组非法参数 exit 2 | 终审只覆盖原三项；helper 目标为合成子进程，未验证真实微信/chatlog DLL 卸载 |

r2 历史归档为 `rootfs-r2/out/rootfs.tar`，大小 2917068800 字节，SHA256 `ff2c7a8c173c91832b3e01e8c3d713723220616990d98bb545df6633a7546e9c`。内容检查与权限失败针对同一摘要，禁止拿前者覆盖后者。修复后的 r3 位于同一构建根 `rootfs-r3/out/rootfs.tar`，大小同为 2917068800 字节，SHA256 `bba363f13e070fca16521ee096dbcff14444eefa9eae5065582461cd847cba7f`；构建步骤已归一权限，独立 39 项检查指向该摘要。

`.codex/local-e2e-20261013/` 为本机证据目录，不保证随仓库携带；本文件保留必要结论、边界与摘要，原始日志需要另行带走。

## 接续顺序与发布状态

安琳新裁决“跳过 MQ 认证测试”只取消本轮真实 MQ 认证前置，不是放宽认证校验、不随包写入测试配置，也不是宣称 MQ 已成功。既有 401 与页面测试失败继续保留；部署后仍需管理员填匹配的配置才能使用对应业务。

上述 r5 阶段验收及 r7+rootfs r4 的 Docker 健康已在各自范围通过，见最新检查点。安装器 R3～R6 与单 unit 修复均已独立验收，最终包已进入压缩，继续签名/校验；截至本交接未生成最新总安装包，完整 SFX 安装、真机覆盖与真实账号功能不冒称完成。发布状态见 [可移植安装交接](2026-10-13-portable-installer.md)与 [STS 交接](2026-10-13-sts-integration.md)。

本次为测试后的纯文档回写。以上无漂移结论属于各测试报告的执行时点；本文档改动不冒充与此前冻结逐字节相同。未修改产品、测试或质量门，文档校验结果另记 `docs-final-verification.json`。
