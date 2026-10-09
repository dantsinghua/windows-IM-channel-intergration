# 2026-10-08 最终前端与安装日志修复：阶段交接

安琳已确认前端重改完成；此前“等待 UI”解除。本页记录最终集成阶段的现行范围，优先于同日关机快照。当前仍是 `claude/lucid-dijkstra-uu5max` / `f25489ac1edfafa755b2085ee133f0c4d2c20e12` 上的未提交修改；正式目录迁移、统一冻结全量回归、独立验收和只读终审尚未结束，不能宣布全域零回归。

## 找回的交换目录

- 宿主目录：`C:\Users\Public\Documents\HyperV-Exchange`。
- WSL 对应：`/mnt/c/Users/Public/Documents/HyperV-Exchange`。
- 历史共享名：`\\LAPTOP-4NGU6M66\HyperV-Exchange`。本轮核实本地目录和文件存在，未重测 SMB 连通。
- 主报告：`QTrade-Diagnostic-20261008/QTrade_排查报告_20261008.txt`；原始日志在同目录 `Evidence/`。
- 诊断子目录实际 60 个文件，`SHA256SUMS.txt` 所列 59 项全部匹配，清单本身不列入自身；外层 ZIP/receipt 不与子目录文件数混算。同名 HTML/DOCX 是报告副本，不算独立证据。

报告分析 **2026-09-22 的 1.0.0 安装失败**，补充探针产生于 **2026-10-08**。它不证明当前 1.0.1 已真装失败或通过；历史 1.0.1 签名包没有因本轮源码修改自动更新。

## 日志结论与修复承接

| 日志/复现事实 | 现行处理 | 验证边界 |
| --- | --- | --- |
| 原生包装执行无害 exit 42 却返回 0；多参数边界被拆开 | 真实 Process 退出码、argv 编码；sc 键值真实参数；令牌走有界 stdin | 无害 Windows PS5.1 已有定向证据；真实服务注册/安装另验 |
| 偶数字节 UTF-8 uname 被误解为 UTF-16LE | WinAgent 区分 UTF-8/UTF-16/BOM，保留非零错误和诊断 | 假 subprocess 11 项通过；未据此启动 WSL |
| 超时输出丢失，三条 grep 引号错误，恢复分支误报成功 | 脱敏有界阶段/exit/timeout/duration/stdout/stderr，清理前采证；严格版本/binder；正常与 resume 同分类 | 最终各路径需统一冻结回归，原组合数字不外推 |
| 微型 kcheck 导入成功但启动超时，先切内核导致误归因 | R6-82：写配置前在当前内核启动门，失败 68；导入失败仍 67 | 本批未改微型 rootfs；`/init` 链接仍是未证候选 |
| shutdown 非零未阻断 | R6-82：超时 66、非零 69，保留配置和 verify-kernel 标记并停止 | 不循环 shutdown；真实切换/回滚未验 |
| 最终 UI 的微信启用、企点验证画面、QQ 提示动作、告知失败入口不完整 | R6-81：新增微信第 0 步确认启用；原地画面；QQ refresh-qr；notice 失败可重试 | 同一组件用例旧 9 RED → 修复 9 GREEN；独立浏览器/真实桌面仍分开验 |
| QQ B13 缺正式来源与首登收尾；QR 可能进入持久副本 | R6-83：官方 4.18.28 URL/固定 PNG；内存及实时 WS/API 保留，outbox/webhook/重放移除；受管 ACCOUNT 收尾及 K6 到期关闭 | 既有专项通过不代替新增 B13/K6 独立验收或真实扫码 |

**更正历史环境结论**：10 月 8 日同一虚拟机的完整 rootfs 导入后，约 21.19 秒返回 uname，Docker active；微型 rootfs 则启动超时。因此 HANDOFF §3c/§3d 中“嵌套 WSL2 起不来、只能验切换前”的表述仅是当日历史判断，已被这次完整 WSL 成功证据替代。当前实际官方内核缺 binder，载荷自编内核配置包含 binder/binderfs 与所需 HYPERV_VSOCKETS，但本次未切换；不能断言自编内核兼容或不兼容。redroid 退出 255、非 OOM，空日志不足以归为唯一原因。

WSL MSI 原日志返回成功；旧 `{app}` 展开问题和部分日志/回滚文案在当前源码已有修复。签名信任、共享传输与诊断工具权限不混入产品内核根因。完整诊断见本机 [独立诊断](../../.codex/final-integration-20261008/installer-diagnosis.md)；本页已保留接手所需结论，不依赖被忽略目录才能理解范围。

## 正式口径

- R6-80 网络/S4 从候选按精确差异合入 00/01/02/04/05/06/07；不覆盖最终 UI。失败码总 27、失败组 14；`NETWORK_UNAVAILABLE` 与 Android 启动门、5s 严格只读 S4、default profile 降级读资格均已承接。SQL 产品镜像注释由总控对应同步。
- R6-81 保留五主入口、首页细化、只读桌面指标与权限边界，新增首次使用入口细则。未放宽全局 setup 路由或代做告知确认。
- R6-82 登记于 00 §8.2/§15g、03 流程/退出码/失败矩阵/验收要求；68/69 含正常与恢复分支，不新增版本不匹配码，不把失败输出写成官方内核已恢复。
- R6-83 登记于 00/01/02/04/05：实际 `disableWebUI`、固定 PNG、实际容器 ACCOUNT 受管重建（普通已填 UID 路径同样核名称/ID）、新 WS 同 UID 确认；K6 1s 到期任务仅在非 busy/无有效登录尝试时关闭，成功才清截止，失败保留重试。所有真实容器动作仍遵守当次授权。

## 本轮证据按版本区分

| 证据 | 实际结果 | 覆盖范围 |
| --- | --- | --- |
| 13:42:20Z 冻结的 531 文件基线 | Agent 2505、WinAgent 523、Installer Python 160、Frontend 403 passed；typecheck/lint/双构建 exit 0 | 修改前基线，不能证明本轮最终代码 |
| 精确合入的网络/S4 测试 | 201 passed | 该专项；不是旧候选 2626/352 的重新全量 |
| 安装开发者组合冻结 | 61/61 | 随后 Native 和清理逻辑变化，已有漂移记录；只归冻结时点 |
| 安装实现最后增量 | Native 11/11；无害进程探针 8/8；Parser 通过 | 最终六文件有 SHA 清单；不代表全套 Pester/真装 |
| UI 最终同一组件用例 | 原冻结源码 9 RED → 修复 9 GREEN；typecheck exit 0 | 假 API 组件；另需最终构建/浏览器 |
| QQ 既有定向与后续返修 | 187 passed，2 个既有警告；后续开发者新增首登 3 RED 修复，新增 QQ 专项最终 69/69 GREEN | 后续改了 runtime/backends.py 与 runtime.py：实际容器 Config.Env.ACCOUNT 决定重建，普通已有 UID 路径也核名称/ID。旧 QQ SHA 不代表最终；旧+新相关集及最终全域仍待；这组是开发者测试，独立验收尚未结束 |

基线 lint 有 36 warnings，Agent 有 6 个既有弃用警告；不隐去。开发者曾修正新测试夹具，只有同一最终用例在旧源复现出的 RED 才列缺陷证据。完整 Pester 目录包含真实 WSL 枚举，执行前须独立核安全边界。

文档原字节备份与证据在 `.codex/final-integration-20261008/documentation/`。本阶段已实际运行 `docs/` 下 `timeout 120s python3 check-truth-tables.py`，exit 0；仅 5 项既有 MIRROR 提示，未改质量门。最终迁移后的路径与验证报告由下一独立文档收尾批更新。

## 接续与权限

1. 保留最终 UI 和所有已授权修复；基于当前源码完成全新 `frontend/`、`backend/agent/`、`backend/winagent/` 迁移，不套旧候选清单。`docs/`、`installer/` 保持配套根级。
2. 独立测试方补齐 B13/R6-83/K6、安装接线及边界；路径消费者与质量门由其所有者维护，文档角色不改门。
3. 同一最终冻结版本完整运行 Agent/WinAgent/Installer Python、经安全审查的 Pester、前端测试/typecheck/lint/build；再做独立浏览器验收和只读终审，登记源码/产物 SHA 及全部未覆盖项。
4. 旧企点账号和旧容器不启动、不 ADB、不复制登录态；不运行安装/引擎/WinAgent EXE，不重启现役 WSL/Docker/Windows，不操作真实账号或外发。宿主 `C:\ProgramData\QTrade` 的不存在保护继续适用。
5. 真实 kcheck 单变量对照、内核切换、全新安装、升级、真实扫码/重建及已安装 Electron 桥另需明确环境和授权。没有这些证据时，只能报告已验证范围内无回归，不能承诺所有环境“0 回归”。

本轮未 commit/push；阶段源码修复不等于已重出包或已部署真实服务。旧服务端口/PID、私有预览和历史测试数字均只归各自快照。
