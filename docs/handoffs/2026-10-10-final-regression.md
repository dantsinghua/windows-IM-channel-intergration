# 2026-10-10 最终整合收口：候选修复回合主树 + 同一冻结版本全量回归

承接 [2026-10-08 阶段交接](2026-10-08-final-integration.md)。本页记录 10 月 10 日在主树（原目录布局 `console/`、`src/`、`tests/`、`winagent/`、`installer/`）上完成的最后一批回合与同一冻结版本的全量回归；所有数字只归本页列出的冻结时点与命令。

## 起点与差异核对

- 分支 `claude/lucid-dijkstra-uu5max`，HEAD `5e15b6f`（2026-10-09 09:00 提交，把 10 月 8 日集成修改以原目录布局入库）。工作区开始时仅 `.gitignore` 有改动（忽略 `.codex/`、`.agents/`）。
- 10 月 8 日最后一批修复落在 WSL ext4 的布局候选 `/home/anlin/work/qtrade-build/final-layout-20261008-p6aioddg`（`frontend/`、`backend/agent/`、`backend/winagent/`），**没有回到主树**，HEAD `5e15b6f` 也不含它们。本轮用归一化比较（撤销 `backend.agent.tests.`→`tests.` 等纯路径改写后逐文件 diff）确认候选相对主树的实质差异共 66 处，其余 62 处只是路径适配；完整报告见本机 `.codex/final-regression-20261010/candidate-vs-main-normalized-diff.txt`。
- 目录迁移（`frontend/backend`）本轮**未执行**：主树已以原布局提交，安琳本轮要求的是启动测试环境并做全量回归；迁移是另一项改变全部路径的工作，仍列为开放项。

## 回合到主树的内容

| 类别 | 文件 | 来源与说明 |
| --- | --- | --- |
| 产品（Agent） | `src/qtrade_agent/events.py`、`src/qtrade_agent/api/app.py` | R6-83 实时 WS 二维码：`Events.live_payload()` 仅向本次订阅之后、且 seq 等于当前内存码的 `account_state` 帧补 `qrcode_png_b64`；重连回放、outbox、webhook 不含码（原 `ws-live-fix.patch`）。 |
| 产品（Agent） | `src/qtrade_agent/accounts.py`、`src/qtrade_agent/api/app.py` | #97 `webui/open` 幂等：已开且登记截止未过期才视为 no-op；响应 `until` 取 `res["until_ms"]`，重复打开不重启容器。 |
| 产品（控制台） | `console/src/stores/accounts.ts`、`console/src/api/types.ts`、`console/src/pages/acct/AcctNewPage.vue` | 登录尝试隔离：退役 `login_session_id` 的迟到整帧不覆盖新状态/身份，`refreshPrompt` 结果按当时会话与请求序号校验；`AccountStatePayload.self_uid`；QQ 完成页显示「QQ 号」（原 `ui-session-fix-r2.patch`）。 |
| 测试（Agent） | `tests/test_qq_qr_websocket_delivery.py`（新增）、`tests/test_integration_wiring_round2.py` | 真实 FastAPI WebSocket 双订阅、重连不回放码、SQLite/webhook 脱敏、ACL/过滤；#97 幂等与非受管容器拒绝（导入已改回主树写法）。 |
| 测试（控制台） | `console/tests/unit/account-session-regressions.spec.ts`（新增）、`console/tests/unit/testids-coverage.spec.ts` | 会话边界回归；01 §4 退役行只在类型+要求两列同时标明才豁免，`refresh-qr` 动态 testid 需工厂、绑定、处理分支齐全。新增用例的夹具 `trace_id: null` 与 `QtEvent.trace_id?: string` 不符，本轮删去该字段（仅夹具，不改断言）。 |
| 测试（控制台 e2e） | `console/tests/e2e-pw/{danger-ops,pages-smoke,paging,setup-wizard}.pw.mjs`、`console/tests/acceptance/*.mjs` | 独立维护者按 R6-81 最终 UI 更新的 Playwright 用例；acceptance 的 `node_modules` 默认路径改为相对。 |
| 测试（联调） | `tests/e2e/console-real/set-page-real.spec.ts`、`settings-contract-real.spec.ts`（新增）、`vitest.e2e.config.ts` | 偏好页按 R6-81 真挂载；退役 #88/#89 契约移入 settings-contract；config 改为 `root`+`../../src`，复制到 `console/tests/e2e-real/` 后由 `npm run test:e2e-real` 运行。 |
| 测试（安装器） | `installer/tests/QTrade.{KernelFailure,RehearsalKernel,RunningDistros}.Tests.ps1` | 断言对齐 R6-82 英文诊断文案；RunningDistros 用临时编译的假 `wsl.exe` 代替真实 WSL 枚举。`QTrade.{Kernel.Boundary,Kernel.Resume,Native.Process}.Tests.ps1` 仅补 UTF-8 BOM（`test_all_scripts_are_utf8_with_bom` 在主树原文件上为红）。 |
| 配置 | `console/package.json`（`test:e2e-real`）、`console/vitest.config.ts`（include 收窄为 `tests/unit/**`） | 让单测与 HTTP 联调分开入口；未带入候选的 `cacheDir`。 |
| 文档 | `docs/00`、`docs/01` | 三处「离线」→「WinAgent 服务离线」措辞、8b 的 Playwright 目录改为 `console/tests/e2e-pw/`；候选 `docs/README.md` 含空路径串的段落未带回。 |

未带回：候选中仅为布局适配的 README/构建脚本提示（含 `collect-payload.ps1`、`build-rootfs.sh`、`QTrade.Signing.psm1` 的英文化 Hint/Desc 与空串），以及 `cacheDir` 类候选专用配置。

## 测试环境与运行方式

- 独占 ext4 副本 `/home/anlin/work/qtrade-build/final-regression-20261010/`，由主树 rsync（排除 `.git`/`node_modules`/`dist`/`release`/`installer/out`/`.venv-build`/`sfx-stub/work`/`.codex`/`.omc`/`.omx`）；`console/node_modules` 软链到既有 ext4 依赖 `ui-home-refinement-20261008/console/node_modules`，未在 `/mnt/c` 安装依赖。
- Python：`~/.venvs/qtrade/bin/python`（3.12）。Pester：Windows PowerShell 5.1 + 缓存目录的 Pester 5，在主树 `installer/tests` 运行 `run-pester.ps1`，所有外部调用均被 Mock/假 exe 替代。
- 联调服务：`python -m tests.e2e.serve_fake_agent --port 37851 --dir <隔离目录> --seed-api`（真 Agent HTTP/WS，容器/ADB/Vault/WinAgent/OneBot/HTTP/磁盘全部假件）；浏览器预览另起 37861 + Vite 5312（`QT_DEV_TOKEN` 为假后端固定测试令牌，仅代理注入）。全部进程按记录 PID 结束，未触碰 8081/17600/17610/5273 等既有端口。
- 运行前后只读检查 `C:\ProgramData\QTrade` 均不存在；未运行安装/引擎/WinAgent EXE，未操作真实账号、容器、ADB、WSL 或外发。

## 最终冻结版本的结果（主树 == 副本，rsync dry-run 零差异）

| 套件 | 命令（均带 timeout） | 结果 |
| --- | --- | --- |
| Agent | 根目录 `pytest -q`（含 `tests/acceptance`、`tests/e2e/test_admin_line.py`） | **2709 passed**，6 项既有弃用警告，232s |
| WinAgent | `winagent/` `pytest -q` | **534 passed** |
| 安装器 Python | `pytest -q installer/tests` | **160 passed**（首轮 1 失败 = 三个无 BOM 的 Tests.ps1，补 BOM 后复跑） |
| 安装器 Pester | `installer/tests/run-pester.ps1`（PS 5.1） | **695 passed / 0 failed / 0 skipped**（BOM 修复前后各一次均为此数） |
| 文档门 | `docs/` `python3 check-truth-tables.py` | exit 0，「真值表全部一致」 |
| 控制台单测 | `npm test` | **24 文件 / 425 passed** |
| 控制台类型 | `npm run typecheck` | exit 0（首轮 1 处为新增测试夹具类型错误，已修；最终复跑 0） |
| 控制台 lint | `npm run lint` | 0 errors / 37 warnings（基线 36 + `final-integration.spec.ts` 1 条既有风格提示） |
| 控制台构建 | `npm run build`（renderer + Electron main/preload） | exit 0，保留既有大 chunk 提示 |
| HTTP/WS 联调 | `npm run test:e2e-real` ↔ 假后端真 Agent 37851 | **5 文件 / 127 passed**（首轮因旧 config alias 收 0 用例，换新 config 后复跑） |
| 浏览器 Playwright | `npx playwright test -c tests/e2e-pw`（自起 mock 17620 + Vite 5283） | **47 passed** |
| 浏览器冒烟 | Cursor 内置浏览器 → Vite 5312 → 真 Agent 37861 | 首次设置五步（告知真实 ack、连接、自检、步 4 → `/acct/new?ch=qidian&from=setup` 守卫窄例外放行、未完成时直开 `/acct/qd01` 被打回步 4）、企点与 QQ 建号到完成页（QQ 完成页显示 QQ 号）、13 条路由含 `/cmd`、`/flow`、`/set/mail-templates` 重定向，`window.error`/`unhandledrejection` 为 0；画面页如实显示 4503 `stream_backend_missing` |

日志、junit、源码 SHA 清单（489 文件，起跑/最终两份，仅 6 个预期文件变化且均已在对应套件复跑）、截图与脚本在本机 `.codex/final-regression-20261010/`（已被 `.gitignore` 忽略，仅作本机证据）。

## 真实链路联调（安琳 15:42 授权；真 Docker / adb / scrcpy，隔离账号与端口）

环境 `/home/anlin/work/qtrade-build/real-chain-20261010/`：`run_agent.py` 复刻 `qtrade_agent.main` 的生产装配（`DockerCliBackend`/`AdbCliBackend`/scrcpy 4.1 已接上），仅三处环境附加——ADBKeyboard.apk 路径、`seq.qidian/seq.qq` 预置 89（账号从 qd90/qq90 起，容器名 `qtrade-qd90` 永不碰历史 `qtrade-qd01`/`qtrade-redroid`/`qtrade-build-qidian-8246-*`）、本地 admin 令牌文件。`agent.toml` 把 API 绑 `127.0.0.1:17650`、库/卷/APK 缓存/媒体全部落该目录，`[winagent] url` 指向关闭端口 `127.0.0.1:17611`（无 WinAgent，凭据不进任何真实 Vault）。前端 Vite `5313` 代理注入该令牌。企点 APK `2cb9564c…`（6.9.7）与 ADBKeyboard 取自 10-08 已核 SHA 的资产。

**用户态走通的真实链路**：`POST /accounts`（qidian，`login.mode=manual`）→ `start` → 真 `docker create/start` redroid 11 → boot → root → 网络预检 → 装企点 + ADBKeyboard → 协议处理 → **`login_required / WAIT_PASSWORD`（17 s）**；#15 prompt、#33 真实 PNG 截图（720×1280）200；最终 UI `/#/screen/qd90` 真 scrcpy 视频流（avc1 软解）显示企点登录页，事件流断后「重新取令牌并重连」恢复；Agent 进程被杀后重启，`recover` 把 qd90 恢复为 `login_required`。未填任何真实凭据，登录由安琳在画面里完成。

首轮启动在真机上停在 `error UI_UNEXPECTED 企点登录界面准备失败`，逐项排到四条产品缺陷（旧测试全部用 [推测] 假树，故此前全绿）：

| # | 真机事实（redroid11_x86_64 + 企点 6.9.7） | 旧实现 | 修复 |
| --- | --- | --- | --- |
| 1 | `uiautomator dump /dev/tty` 在非 tty 的 adb 会话只回一行「UI hierchary dumped to: /dev/tty」，XML 不进 stdout | `_dump`/`probe_login` 都用 `/dev/tty` ⇒ 控件树恒空 ⇒ 90 s 超时 | `ui.UI_DUMP_CMD`：落 `/data/local/tmp/qtrade-ui.xml` 再 `cat`（02 §2.2.4 规定的落点，停号随临时数据清理） |
| 2 | 协议弹窗正文 `dialogText` 与左键「不同意」在文档序上先于右键「同意」，三者都含「同意」 | `find_node` 按文档序取第一个文本子串命中 ⇒ 点到正文/「不同意」 | `find_node` 改为 **id 全等优先**，`agree_button` 锚点加 `id: dialogRightBtn`；去掉 `text_any:"同意"`（登录页底部常驻「我已阅读并同意…」会被误判成弹窗） |
| 3 | 登录页账号框**没有 resource-id**（text「企点账号」desc「请输入企点号码或手机或邮箱」） | 锚点 `id/account`+「请输入账号」命不中 ⇒ 表单永远未就绪 | `login_account` 加 `desc_any`，密码框/登录键 id 实测命中并标 [实测 6.9.7] |
| 4 | 底部协议未勾选时点「登录」再弹「请阅读并同意相关协议」（同一 `dialogRightBtn`） | `_fill_login` 点完登录不管 ⇒ ⑪ 只能 login_timeout | 提交后再读一次控件树，若有协议弹窗点一次（与 ⑦ 同样最多一次；验证码/设备锁仍交人） |

**安琳 16:2x 在画面里真实登录后的两条观察**（均非本链路缺陷，记录供接手）：① 对端看到「对方当前不在中国大陆」、企点弹「登录失败 · 配置拉取失败」—— 开发机路由器 `192.168.3.2` 按目的地分流（国内直连 `14.153.73.159` 深圳电信 / 境外经 `74.211.99.72` 美国节点），企点 MSF 会连腾讯云国际段（logcat 实见 `129.226.107.x:14000`），这部分流量被甩到美国出口触发腾讯风控；容器 DNS/TCP/HTTPS 本身全通。部署目标机（[PLAINNET]）无此策略路由。② 登录成功后 Agent 仍停在 `WAIT_PASSWORD`：该行是本轮用 API 以 `login.mode=manual` 且**未声明 `login.account`** 建的，R6-80 只读观察要求观察到的 `self_uid` 与声明账号一致，身份为空就按设计永不确认（UI 建号路径必填账号，不会走到这里）。经 #12 `{"mode":"manual","account":"<uin>"}` 声明后 15 s 内走免登判定到 `running`、`self_uid` 正确。建议裁决：manual 模式建号/#12 必填账号，或在详情页提示「请先声明企点账号」。

证据：`tests/test_qidian_real_device_697.py`（8 条，真机控件树原样截取；在旧源码上 2 passed / 4 failed 的 RED 已实跑确认，另 2 条针对 dump 命令为新增）；企点相关 9 个测试文件 **425 passed**；修复后 `probe_prepare.py` 对真机 `prepare_login` 9.7 s 返回 True，随后 Agent 实跑达到 WAIT_PASSWORD。四处测试夹具（`test_qidian_apk_preparation/initialization_network/manual_login/login_structure`）的「同意」假节点补上真实 `dialogRightBtn` id，断言未改。`app_version` 回填（`account_runtime.app_version` 产品侧从不写入，profile 永远走 default）仍是开放项；`main_marker` 等登录后锚点仍为 [推测]，需安琳登录后在同一容器核对。

## 16:34 安琳要求的账号详情页重排（已落源码）

`console/src/pages/acct/AcctDetailPage.vue`：操作按钮（查询历史消息/启动|停止|重启/全部账号）并入头部右侧并与状态文字同列；原横在中间的状态引导卡（含 `qt-acct-detail-state-card*`、密码/刷新码等动作）移入右侧栏首位；工作区（实时画面）直接跟在头部之后。「改名」独立按钮退役：名称悬停露出编辑 icon（`qt-acct-detail-label-edit`），点名称或 icon 原位变输入框（`qt-acct-detail-label-save`），失焦/回车即 `PATCH` 并用返回行刷新展示，空值/未改动不发请求，Esc 放弃。01 §4 元素表同步；新增 `console/tests/unit/acct-detail-inline-rename.spec.ts`（3 条），前端全量 **25 文件 / 428 passed**、typecheck 0、lint 0 errors、文档门 exit 0；真实链路页面（5313）上实际改名→保存→改回均成功。

## 17:10 安琳要求「阅读须知这步删掉」（已落源码 + 裁决 R6-84）

背景：真实链路在 Electron 壳里首屏「阅读须知」因 #86 不可达把人卡死（滑块不出、勾选禁用、重试无反应）；安琳决定整步删除。改动：`SetupPage.vue` 向导改四步「连接服务 → 检查环境 → 添加账号 → 准备完成」，告知区/滚到底/勾选/重勾逻辑整段移除；`stores/setup.ts` 删 `reackRequired/scrolledToBottom/refreshAck`，`SETUP_LAST_STEP = 3`，`readStep` 上限 3，`ack()` 保留给偏好页；`router/index.ts` 守卫 gate 只剩 `done`；`App.vue` 不再调 `refreshAck`；`testids.ts` 删 `noticeText/noticeFixed/noticeAck`。文档：`docs/00` §15g 追加 **R6-84**，`docs/01` §2.7.1 改四步、§4 三条 `qt-setup-notice-*` 标「历史按钮(退役)…不再要求渲染」，`docs/05` §6.1 加 R6-84 退役注记；`check-truth-tables.py` exit 0。测试：删 `tests/unit/setup-notice.spec.ts`，`setup-wizard.spec.ts`/`final-integration.spec.ts` 改为四步用例（含「不请求 #86/#87」「已完成向导不因告知改版回退」），`dashboard-metrics`/`ui-redesign` 删 `refreshAck` 桩，Playwright `helpers.mjs` 与 `setup-wizard.pw.mjs` 重写。结果（RUN 副本）：前端单测 **423 passed**、typecheck 0、lint 0 errors（37 warnings 为既有）、Playwright 全量 **44 passed**（原 47 含 3 条告知步用例已随之退役）。Vite 5313 已提供新源码；Electron 宿主若仍显示旧向导按 Ctrl+R 刷新即可（无需重启）。

## 17:40 安琳「开始修」—— 托盘崩溃 / 桌面壳 CORS / 微信步①② 口径（R6-85、R6-86）

安琳截图叠着三个问题，根因各异：

1. **Electron 主进程弹窗 `Object has been destroyed at TrayController.toggleWindow`**：测试 `console.toml` `minimize_to_tray_on_close=false` 关窗后窗口被销毁，`index.ts` 没监听 `closed`，`mainWindow` 留死引用，点托盘即崩且再也打不开。修：`win.on('closed')` 置空；`tray.ts` 新增 `liveWindow()`（null/`isDestroyed()` 都算无窗口），`toggle/show` 无窗口时经新注入的 `createWindow` 回调重建；`render-process-gone` 新建窗口后把旧壳 `destroy()`。
2. **桌面壳渲染进程 → Agent 全部被 CORS 拦**（`electron-stderr.log` 17:07–17:14 共 86 条）：上一轮按 `qt.endpoint.agent` 绝对地址直连后，页面来源（Vite 5313 / 正式包 `file://`）与 Agent 跨源，带 `X-Trace-Id`/`X-QT-Api-Min` 触发预检，Agent 无 CORS、`OPTIONS` 回 405。全套文档无跨域口径 ⇒ **裁决 R6-85**：Agent 最外层 `CORSMiddleware`，放行表 = 新键 `[api] console_origins`（默认 `["null"]`，`console_cors_origins()` 只收 `null` 与回环 http(s) 源），预检不进鉴权/审计，放行≠鉴权。改 `config.py`、`api/app.py`，02 §7.1、07 §2 登记；新增 `tests/test_api_console_cors.py`（5 条）。真实链路 `agent.toml` 加 `console_origins=["null","http://127.0.0.1:5313"]` 并重启 Agent（新 pid 见 `agent.pid`，`qd90` 保持 running），实测预检 200，Electron 重启后 CORS 报错 0、Agent 收到渲染进程请求。
3. **微信步①「未启用 / 不在线 / 请先登录 Windows 桌面」**：01 §2.7.3.3 把 `#28 wechat/status` 写成 `{module_enabled, user_agent, installed[], match, action, bundled_version}`，控制台照抄；02 #28 真实形态是 `{enabled, wechat:{installed, version,…}, …}`，且 R6-58 (al) 规定会话代理在线与否看 #2 health，WinAgent 按 02 实现 ⇒ 真机永远过不了步①。**裁决 R6-86** 以 02 为准：`AcctNewPage.vue` 用 `enabled` 判模块、`session.userAgentOnline`（Agent #72 `winagent.user_agent`）判会话代理、`wechat.version` 显示版本，步② 结论/处理/随包版本改调 02 #29 `wechat/version-match`（`wa-whitelist.ts` 补 `wechat.version-match` 只读项）；01 §2.5 白名单③、§2.7.3.3 ①②、§3、§4 对应行改写；`final-integration.spec.ts` 的 `wa.invoke` 桩改成 02 形态。红字「请在已连接 WinAgent 的桌面控制台启用」是安琳当时在**浏览器**（无 `window.qt`）里操作所致，文案本身正确。
   - 遗留：`MULTIPLE_INSTALLS` 时的安装路径列表在 02 #28/#29 都没有字段，控制台单选框暂无数据源，待 02 补字段后再接。

结果：Agent `tests/test_api_console_cors.py`+`test_api.py`+`test_system_version_sources.py` 32 passed（全量另跑，见下）；前端单测 423、typecheck 0、lint 0 errors；文档门 exit 0；Electron 宿主已用新 main/preload 重启（`restart_electron.ps1`）。

## 18:03 微信「开始重装」FileNotFoundError / 「尚未确认已启用」/ 托盘图标 —— R6-87、R6-88

安琳要求「不要临时修复,要考虑全新机器各种情景」。按根因逐层修:

- **R6-87 重装缺文件**:① `[wechat] bundled_installer` 默认 `pkg\WeChatSetup.exe`,而安装器实际落包 `pkg\wechat\weixin_4.1.12.26.exe`,安装器从不写本键(99b 评审早有记录未落)⇒ 任何全新安装后重装必缺;默认值改为实际落包路径,新增 `bundled_version`/`bundled_sha256`(R2-6 钉死值)。② 会话代理 `reinstall` 拉起前两道门:文件在位、sha256 相符,任一不过 ⇒ `404 TARGET_NOT_FOUND`(reason `bundled_installer_missing`/`bundled_installer_sha_mismatch`,message 给可照做的修复法),不拉起。③ #29 `version-match` 原只查 `wechat_install` 记档,全新机器恒 `NOT_INSTALLED` 把装着 4.1.12.26 的机器也引去重装;现以会话代理 **本机实际安装**(新管道方法 `wechat.locate`)为事实来源并回填,本机未装则旧行不作数,会话代理离线才按记档判。④ `status` 改线程执行;微信未运行不探 UIA;`WinProc.find` 只按名扫、命中才取内存;`ui_tree_visible` 不再各白等 2 s;上线/启用模块预热 pywinauto。真机 `status` 从 8.7–12 s(恒超 9 s deadline)降到 1.3–4 s。
- **R6-88 启用后「尚未确认已启用」**:#43 PUT 只改服务内存 + DB 快照,从不写回 toml(C-43 真值在 toml);会话代理只认自己启动时读的 toml,服务握手也不同步 ⇒ 服务 health 说 `enabled`、#28 说 `enabled:false`;全新机器「服务先起、用户后登录」必撞。修:PUT 写回 `winagent.toml`(新 `config.write_toml_keys`,行级编辑保留 BOM/CRLF,临时文件 + `os.replace`,`--dev` 不落盘);`PipeHub` 新增 `welcome_extras`/`on_holder`,`WELCOME` 随带 `wechat_enabled`,会话代理握手即对齐;`wechat.module` 幂等(同值不动作,只有 true→false 才登出,不误杀用户微信);`read_toml` 按 `utf-8-sig` 容忍 BOM(记事本/PS 5.1 写过的 toml 曾让服务起不来)。
- **控制台**:`AcctNewPage.vue` 操作失败一律 3 秒 toast(`toastError`),页面不再留红字(`wxEnableError`/`startError`/`passwordError`/`cancelError`/`restoreError` 的内联 `<p class="qt-danger">` 全部移除,恢复按钮保留);两条单测随契约改为断言 `notices.error(..., 3)`。
- **命令卡住复盘**(安琳问):`Start-Process -Verb RunAs -Wait` 等待的是**提权后的 PowerShell 进程**,而 `restart_svc_elevated.ps1` 里又用 `Start-Process -WindowStyle Hidden` 拉起常驻的 `start_svc_elevated.ps1`,子进程继承控制台句柄致父进程不退出 ⇒ 我的 `-Wait` 永远等不到。不是产品 bug,是我的指令写法问题;改为分离启动 + 轮询 17610 监听者 pid 变化,6 s 完成。svc 本身在 18:14:37 已正常起来(日志可证)。
- 验证:WinAgent **551 passed**(新增 9 条:缺文件 404、sha 不符 404、默认路径、live locate 回填/陈旧行/离线、握手对齐不登出、同值 PUT 不登出、toml 回写保 BOM/CRLF、未运行不探 UIA);前端 423、typecheck 0、lint 0 errors;文档门 exit 0;真机:PUT enabled=true 后 toml 已写 `enabled = true`,会话代理重启后 status `enabled:true`,`version-match` 对本机 4.1.12.26 回 `SUPPORTED`。
- 开发实例 `winagent.toml` 加了 `bundled_installer = C:\Users\anlin\Downloads\weixin_4.1.12.26.exe`(sha256 已核与钉死值一致);新脚本 `restart_svc_elevated.ps1`。
- **未做**:托盘图标仍是 `nativeImage.createEmpty()`(安琳要求给小图标)——仓库里没有任何 ico/png 资源,需要先定图标来源;`status` 在微信运行时仍 ~4 s(UIA 全树找 List/Edit),可再降但已远离超时线。

## 18:52 讲述人仪式起不来 / 令牌到期门禁 / 「去环境页」关不掉 —— R6-89

- **讲述人**(安琳:「点开启讲述人没反应」):步④ 按钮原本没有 `@click`;真正起讲述人的是会话代理,但 `create_subprocess_exec(Narrator.exe)` 在真机必报 `OSError(740 请求的操作需要提升)`(uiAccess 清单),`login/start` 恒 500、wx01 被打 `stopped`。实测 `os.startfile`(ShellExecute)可起;起来后讲述人跑在高完整性,会话代理 `taskkill` 拒绝访问、提权可杀。修:`WinWeChat.narrator_start` 走 ShellExecute(已在跑不重复起);`narrator_stop` 回「是否已停」,停不掉 ⇒ `LoginSession.narrator_stop_pending` → #33 `narrator.stop_pending` / cancel `narrator_stop_pending`,**服务侧**(`SvcDeps.narrator_killer` = `WinWeChat.kill_narrator_elevated`,提权)结束并记审计 `wechat.narrator_stop`;状态机等它确实停了再探可见性,不重复 taskkill、不加轮次。步④ 按钮改为读 #33 以 toast 告知在跑/未跑/正在关闭。#33/#35/#36 令牌列补 `C`(01 §2.5 ③ 早判控制台只读可直调,WinAgent 只收 A ⇒ 恒 403)。真机:重启后 wx01 → `login_required/WAIT_NARRATOR`,讲述人由 WinAgent 拉起,控制台令牌读 #33 得 `narrator.state=running, remaining_s=74`。
- **排查时的误伤**:我用脚本直接拉起过一次讲述人来验证 740/ShellExecute,结果关不掉(拒绝访问),安琳以为是点「重试」触发的;已提权结束并当场说明。
- **令牌门禁**:Electron 主进程令牌包 `expires_at = now + 1h`,没有任何续签路径 ⇒ 17:51 重启、18:51 准时弹「控制台令牌失效」;生产命名管道同样带 `expires_at`。修 `token.ts`:取到令牌即按到期 80% 处预约续签(至少留 60 s),失败 30 s 重试,并发合并;`qt:auth.state` 改 `ensureFresh()`(到期先续签再回答)。新增 `tests/unit/electron-token-renewal.spec.ts`(5 条,注入假管道/时钟/定时器)。
- **「去环境页」关不掉门禁**:门禁覆盖层按规格保留原路由,`go('/env')` 只换路由。修:`/env` 不被门禁覆盖(环境页本身是恢复入口),按钮导航后顺带 `retryToken`。
- 文档:00 §15g **R6-89**;02 #33/#35/#36 令牌列、#33 响应 `narrator.stop_pending`;01 §4 `qt-acct-new-wx-narrator-open`。
- 验证:WinAgent **554 passed**(新增 3 条:停不掉交服务 + 等停继续、cancel 带 stop_pending、服务侧提权结束 + 审计);前端 **428**、typecheck 0、lint 0 errors;文档门 exit 0。svc/会话代理/Electron 已重启到新代码。
- 又一次「命令卡住」复盘:在同一条命令里前台运行 `restart_electron.ps1`,它用 `Start-Process -RedirectStandardOutput` 起 Electron 后自身不退出;实际 Electron 19:12:57 已起、新 bundle 在跑,只是我的外层 shell 等不到。已结束那个启动器子进程(核过命令行是我自己的);以后 `restart_electron.ps1` 一律分离运行。

## 19:34 讲述人死等 5 分钟 / 取钥始终失败 —— R6-90

- **取钥从未真正跑过**(只读排查:chatlog 进程列表为空、`~\.chatlog` 从未生成):① 会话代理用 `chatlog.exe key --dll wx_keyN.dll`,实测 `key` 子命令无 `--dll`(只有 `-f/-p/-x`),`Error: unknown flag: --dll` 秒退,输出被丢进 DEVNULL;② chatlog 只认 `<cwd 或 exe 旁>/lib/windows_x64/wx_key.dll`(临时副本实测:该位置「加载成功,将使用DLL方式」,平铺放 exe 旁「加载失败,使用原生方式」),payload 是平铺的;③ #33 缺 `key.stage`,Agent 恒判 `WAIT_KEY_IMG`,永远不提示「退出重登」。另 19:23 起 wx01 进 `degraded(KEY_FAIL)` 后 healthloop 自动 `key/retry` 三次,每次都 stop+start 坏掉的 chatlog,同一根因。修:`stage_wx_key_dll()` 把选中 DLL 复制成 `<chatlog_work_dir>/lib/windows_x64/wx_key.dll`,cwd=该目录跑 `chatlog.exe key --debug`,输出落 `chatlog-key.log`;新键 `chatlog_work_dir`(默认 `%LOCALAPPDATA%\QTrade\chatlog`,会话代理普通用户、pkg 只读);`key.stage` 随 `state_code` 给出。
- **讲述人**:安琳问为何死等 5 分钟。讲述人开着时 UI 树本就可见,真判据是「关掉后仍可见」——本轮实测第 1 轮满 5 分钟关后不可见、第 2 轮才可见,印证不能「一可见就关」。改:已登录 + 可见 + 满 `narrator_probe_min_seconds`(新键 60)即提前试关复探,成则省掉剩余等待;不成重开讲述人、不扣轮次、之后须满 300 s。
- 文档:00 §15g **R6-90**;02 §7.2 `chatlog_work_dir`/`narrator_probe_min_seconds`/`narrator_min_seconds` 注释;05 §2.4.3、§2.4.4a a);07。
- 验证:WinAgent **559 passed**(新增 5 条:提前探测成功、提前探测失败不扣轮次、DLL 落位、源码不含 `--dll`、`key.stage`);文档门 exit 0。会话代理已重启;开发实例 `chatlog_work_dir` 指向隔离根 `chatlog-work`。
- **未验证**:真机 hook 是否「Hook安装成功」、两钥同轮落盘,需安琳在向导里点「重新取钥」后按提示操作;取钥成功后读消息要靠 `chatlog server`(:5030),代码里**尚无任何地方启动它**——`probe_state` 要求 chatlog running + HTTP 200,取钥成功后账号仍会被判 `KEY_FAIL`。这是下一处必修缺口,需先确认 `chatlog server` 是否按 `chatlog.json` 自取密钥(避免把密钥放命令行)。

## 20:14 取钥后的读路径与全新机器兼容 —— R6-91

安琳要求「修法要确保干净机器各种情况都能用」。先只读/隔离实测 chatlog(假路径假钥、独立端口、try/finally 清理,`~\.chatlog` 测完还原):
- `chatlog server` 空配置报 `dataDir or workDir is required`,**不读** key 模式存的钥;读 `~\.chatlog\chatlog-server.json`(下划线键名 `http_addr/data_dir/work_dir/data_key/img_key/platform/version/auto_decrypt` 实测生效)⇒ 密钥可不进命令行。
- chatlog 把两把钥**明文**打进日志(`server config: &{... DataKey:…}`)。
- 真实路由:`/health`→`{"status":"ok"}`;`/api/v1/session|contact|chatroom` 默认回 **CSV**,须 `format=json`;查无回 **404**;`/api/v1/chatlog` **time 与 talker 必填**(缺任一 400),time 形如 `2023-01-01~2023-12-31`(内置说明页)。

修(详见 00 §15g R6-91 十条):① 两钥落盘 ⇒ 停 key、起 `chatlog server`(配置文件传钥,数据目录按真实 `.db` 探测兼容 v3/v4,起不来判 key_failed 不假装 ready);`ensure_server()` 自愈挂在 #28(开机/会话代理重启/chatlog 崩溃,30 s 节流,登录流中不插手)。② 读路径带 `format=json`、`/chatlog` 补 time+talker、404 当空。③ H09 改 `/health`。④ 发送读回认 `isSelf/content`。⑤ Agent 巡检只对已回填 self_uid 的号自动试钥;WinAgent `key_retry` 本轮在跑则幂等。⑥ #33 补顶层 `state_code`,`WAIT_UI_TREE` 不再判 KEY_FAIL。⑦ 微信 degraded(KEY_FAIL/WAIT_UI_TREE) 可 #9 start 重跑登录流(不登出微信);控制台「重新取钥 / 再做一次仪式」对未绑定号走 start。⑧ 子进程输出 `redact_keys` 打码落盘。⑨ Agent→WinAgent 超时对齐(login/start、key/retry 5→65 s,logout 30,read 15)—— 这正是 20:2x 实测 `TimeoutError` 的根因。⑩ 登录流 login/start 后连续 3 轮 idle(会话代理丢会话)⇒ 同 ls 重发,最多 2 次,否则 stopped 写明原因。

测试:WinAgent **570**(新增 11:脱敏、读钥、数据目录探测、server 配置、time 区间、读路径参数、server 先于 ready、server 失败不假 ready、自愈+节流+不插手登录流、`key.stage`…);Agent 全量 **2728**(新增 6:未绑定号不自动试钥、degraded 可重跑×2、WAIT_UI_TREE、会话丢失重发、反复丢失放弃;既有「每小时 3 次自动试钥」用例夹具补 wxid——运行中的微信号必然已回填 wxid,原意不变);前端 428、typecheck/lint 0 errors;文档门 exit 0。真实链路 Agent、会话代理已重启到新代码;wx01 当前 `stopped`,待安琳从向导重新发起。

打包核对:会话代理 spec 已带 `pywinauto/pyweixin/PIL` hiddenimports,`pkg/chatlog` 整树与 VC 运行库在载荷清单;取钥 DLL 改为运行时复制到用户可写 `chatlog_work_dir`,不依赖包内子目录。**开放项**:① `pyweixin` 不在公共源,开发机 venv 也没装 ⇒ 发送整条不可用,需按 B-1 渠道给本地 wheel;② `pkg/chatlog` 在载荷清单是 `Critical=$false`,源缺失会静默出包,建议改 critical(改清单属安装器线,未动);③ 数据目录真实布局与 server 端到端读出消息,须真机取钥成功后核验;④ 我重启会话代理时撞上 Agent 正发 login/start,旧进程拉起的讲述人成孤儿(需提权才能关),下次登录会复用它。

## 21:xx 资源漂移误报 / pyweixin 内置 / 讲述人 5 分钟 / 微信画面 —— R6-92~94(安琳连发四项)

- **R6-92 资源漂移误报 + pyweixin 内置**:① 告警抽屉常驻 `POOL_CALIBRATION_DRIFT`——采样器按容器名拼 cgroup 路径,而 dockerd(systemd/cgroupfs)都按**容器完整 ID** 命名,真机库 0 条容器样本 ⇒ 漂移把「无样本」当「实占 0」恒 100%。修:`cgroup_candidates()` 按 ID 优先、名字兜底,回填 `container_mem_anon_mb`,漂移检查**无样本不判**,告警带中文 title/message,`calibrate` 进 hint_actions→「去资源页校准」。② pyweixin 取上游 `Hello-Mr-Crab/pywechat@8589baa`(LGPL)原始源码自建 wheel `pywechat127-1.9.8`(sha `a98ab028…`)放 `winagent/vendor/pyweixin/`,载荷 `pkg/pyweixin/` critical+sha 钉死(13→16 项),`build.ps1` 强装校验、spec `collect_all` 冻结;发送改用真实 API `Messages.send_messages_to_friend`/`Files.send_files_to_friend`(上游无 `WeixinClient`!),显式 `close_weixin=False`,按 talker 解析显示名、**重名拒发**(404→Agent 判 `TARGET_NOT_FOUND`),读回用 keyword+isSelf+发送时刻后。
- **R6-93 讲述人 5 分钟 + --pid**:真机只读探测 `descendants=1`——4.1.12.26 上讲述人已无法让 UI 树可见(上游 Weixin4.0.md 实录)。取钥是内存 hook、读走 chatlog server,**都不需要 UI 树**;只有发送(pyweixin)需要。修:默认 `auto` 不再做阻塞仪式,login_start 直奔取钥;仪式只在显式 `narrator_ritual="always"` 时跑;发送前检查 UI 树,不可见→`NOT_READY reason=ui_tree_invisible`(读/取钥照常)。另真机日志发现 `chatlog key` 不带 `--pid` 掉进交互式进程选择器挂住(多 Weixin.exe)⇒ 传登录窗口 pid。
- **R6-94 微信不要实时画面**:账号详情/新增向导步⑤/`P-SCREEN` 三处每 2s 拉窗口截图全部去掉——窗口就在本机桌面。`P-SCREEN` 只给企点画面,微信号显示「在本机窗口直接操作」;详情页微信给静态卡+「查看消息」;向导步⑤只提示本机登录。`qt-acct-new-wx-qr-preview`/`qt-screen-wechat-preview`/`qt-screen-wechat-notready` 退役(#33 截图端点保留给排障)。
- 文档:00 §15g **R6-92/93/94**;02 §3.7/§7.2/#38/#39、03 §2.2.1 载荷清单、05 §2.4.3/§2.4.4、01 §2.7.3.3/§2.7.4/§3/§4。测试:WinAgent **577**、Agent **2735**、控制台 **428**、Playwright **44**、installer Pester **697/0/0** + Python **160**、typecheck/lint 0 errors、文档门 exit 0;全程 `C:\ProgramData\QTrade` 未创建。
- **真机现状/开放**:~~`模式匹配失败` 属第三方 DLL 与微信子版本适配~~ —— **已更正(R6-96,22:10 真机 `chatlog key --debug` 对照)**:同一 `wx_key2.dll` 挂登录主进程 23424 即「检测到的微信版本 4.1.12.26 / 目标函数地址命中 / Hook安装成功」,DLL 与版本适配无问题;失败是 `--pid` 挂错进程(托盘态 `FindWindow` 无窗口 / 落到 `--type=` 子进程)。已改为按进程树选登录主进程、key 进程退出自动重开一轮、日志 GBK 解码读进度、微信未打开先拉起、托盘态唤出主窗口(WinAgent 590 / 控制台 429 / 文档门全绿;会话代理已用新代码重启,pid 54524)。真正取钥仍需安琳手动「打开一张图片 → 立刻退出并重新登录」,尚未实测落盘。另 R6-95:告警 `*_at` 改 ISO 8601,修告警铃 `localeCompare` 崩溃(Agent 2737 全绿;运行中的 Agent 未重启,控制台侧已加防御)。pyweixin 冻结后 exe 实际可导入、真机发送仍须 Windows 构建机实跑 `build.ps1`+冒烟并经授权发送验证。

## 未覆盖与开放项

- `console/tests/acceptance/*.pw.mjs` 依赖独立验收方的专用宿主（`/__accept__` 控制口、Windows 卷探针、指定 UI URL），本轮未运行。
- WinAgent 的 Windows 真路径验证、真实 kcheck 单变量对照、内核切换、全新安装/升级、真实扫码与容器重建、K6 真实到期、已安装 Electron 桥均未执行；现有 1.0.1 签名包不含本轮源码，重出包需另行授权。
- 目录迁移到 `frontend/`、`backend/`（R6-80 布局候选）未执行；旧 r4 包已过时，若要迁移须按当前主树重新生成候选并重跑本页全部套件。
- 真实链路只验证到企点登录页与画面流；登录后的会话列表锚点、读库、发送、QQ/微信真实链路未在本轮真机执行。真实链路环境（17650/5313、容器 `qtrade-qd90`、adb server 16000）在安琳验证期间保持运行，停用时按 `real-chain-20261010/agent.pid`、`vite.pid` 与容器名精确收尾，不碰其它容器。
- 本轮未 commit/push；主树工作区改动清单见 `git status`。
