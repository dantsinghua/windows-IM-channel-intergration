# 2026-10-08 关机前阶段总结

安琳的最新要求：**等另一 Codex 客户端完成 UI 美化及功能删增后，再对齐后端 API、联调并正式迁移目录。** 当前暂停覆盖主树 UI、应用旧迁移清单和测试服务重载；安静一段时间不等于对方已交付。本文件是可携带摘要，优先于下列历史快照。

## 当前范围与目录

- 主树分支 `claude/lucid-dijkstra-uu5max`、HEAD `f25489a`，大量未提交改动；正式目录仍为 `console/`、`src/`、`tests/`、`winagent/`。
- 新 `frontend/`、`backend/agent/`、`backend/winagent/` 仅在候选 `/home/anlin/work/qtrade-build/layout-split-20261008-hkus3sn1`，尚未应用正式迁移。不要覆盖另一客户端已修改或删除的页面。
- 主树已有 **R6-81 UI** 裁决，未发现主树 R6-80 裁决正文；R6-80 网络/S4条款在布局候选中。主树称该编号为在途候选保留，合入前须复核编号与语义，不能把候选规格当作主树已统一。

## 已完成与证据边界

| 事项 | 本次可确认结果 | 不能据此推定 |
|---|---|---|
| 新企点 `qd82 / 3007378246` | 用户已真实登录、现场确认 `running`；3条新入向、3个WS事件、0重复，API/页面/主库内容hash一致 | 16:27的协议页/WAIT_PASSWORD已是历史；私有wrapper不等于正式部署版本 |
| 历史只读核查 | 读取10行、解码8条，3条新消息hash吻合；未导入历史、未改实时游标、未输出正文、未外发 | 未知 `msgtype=-1013` 按既有策略跳过，不宣称全类型支持 |
| 正式网络/S4候选 | Agent **2626 passed**，独立复审 **APPROVE / 0 issues**；含协议准备、Android网络门、只读身份确认、降级读库与脱敏异常修复 | 主树尚未套入；旧2615是返修前快照 |
| 布局候选 | 全量 **3308 passed / 1 failed**；3行SQL注释镜像修正后，目标schema测试 **1 passed**、文档对账 **exit 0**，非注释SQL未变 | 修后尚未重跑全量，不能写3309全绿 |
| 前端 | **352 passed** 是旧候选快照 | 不代表另一客户端正在修改的新UI通过 |
| 私有页面读取guard | **144 passed / 0 failed / 0 errors / 0 skipped**，stage配置/依赖/资产哈希核验闭环 | 候选尚未live部署，实际页面FORBIDDEN尚未验收恢复 |

主要证据：[网络/S4最终报告](../../.codex/test-results/20261008-redroid-network/developer/review-final-developer-report.json)、[独立复审](../../.codex/test-results/20261008-redroid-network/final-review.json)、[布局注释收尾](../../.codex/test-results/20261008-layout-split/schema-comment-fix/report.json)、[页面读取144项](../../.codex/test-results/20261008-runtime-pinning/page-read-tests/report.json)、[stage哈希核验](../../.codex/test-results/20261008-runtime-pinning/page-read-tests/stage-hash-verification.json)、[历史只读核查](../../.codex/test-results/20261008-qidian-receive/history-read-verification.json)、[页面hash核对](../../.codex/test-results/20261008-qidian-receive/ui-verification.json)。即使这些本机证据未随仓库复制，本节结果与限制仍是接手必读信息。

## 未完成

1. 等另一客户端明确交付最终保留/删除/新增页面；再核 API、参数、返回字段、状态、权限与错误展示。产品日志页读取 `/audit`，不存在 `/logs`。
2. 私有DB尚无 `compliance.ack`；放开 `/system/notice` 后旧UI会跳 `/setup`，只读guard未开放确认接口。与最终UI一起解决，**不伪造ack、不代用户勾选**。
3. **B13 新QQ首次建号取二维码仍未接线**，既有NapCat在线不能代替首次扫码验收；保留[缺陷回归矩阵](../../.codex/test-results/20261008-redroid-network/bug-regression-matrix.json)的未完成项。
4. 私有补丁与正式网络/S4、布局、文档需重新合并冻结；旧bundle/release摘要不能直接复用。之后先专项，再完整pytest、前端测试/类型/构建及实际只读API→页面联调。完整安装器真装仍未完成。

## 运行根与安全边界（仅关机前快照）

- `5294 / 17640`：新企点私有前端/API，运行根 `/home/anlin/work/qtrade-build/qidian-8246-20261008-opny_r00`；ADB server `16000`。`persistent-services.json` 当时记录 ADB PID `20078`、API PID `22143`、前端 PID `41724`；本轮交接没有停止它们。
- `5273 / 17600`：运行根 `/home/anlin/work/qtrade-build/manual-20261008-a0stq5rk`，QQ真接收与其它假后端混合的手测环境，不能与私有站或正式安装版本互换结论。
- 私有根下 `page-read-stage`、`deployment-stage` 均未激活；对应 `manifest.json`、`manifest-after-page-read.json`、`frozen-config.json` 与上述stage核验报告需成套复核。schema注释已变，后续release也须同步并重新冻结；不要批量更新摘要掩盖源码漂移。凭据仅留私有路径，不复制到交接或仓库。
- 开机先只读核路径、源码/manifest哈希、端口、容器ID、账号绑定、数据目录以及进程cmdline/cwd/starttime；**不能凭旧PID停止进程，也不能自动启动恢复账号**。不直接运行默认 `AgentApp.start(recover=True)`，私有服务依赖原guard、原数据与 `recover=False`。
- **旧账号 `3007373675` 禁止启动**；旧 `qtrade-redroid` / `qtrade-qd01` 不启动、不ADB、不登录、不复制登录态。NapCat A一次授权重启已消费，不再重启。没有任何外发授权。
- Codex原生OMX **0.21.8**、claude-mem **13.34.2 codex摘要**及 **828K** 配置继续沿用；此项目续接不重新迁移或调整这套环境。

## 下次顺序

1. 读 `AGENTS.md`、本总结、`HANDOFF.md` 最新入口与项目skill；只读核git和并行任务，不自动fetch/pull。
2. 取得另一客户端明确交付，保留其成果，先完成UI→API合同对齐和notice导航收口。
3. 重新核候选与私有依赖，按各自职责合并正式修复和目录迁移，再冻结新清单。
4. 每个bug保留回归/专项证据，重跑最终全量及真实只读联调；只按新结果宣布完成。持续服务不用测试timeout包生命周期；`diff`退出1只表示有差异。

本次关机收尾只写交接并做文档对账，不运行pytest、不启动或重启服务、不提交。主控详细操作快照见 [19:36交接](../../.codex/QTRADE-20261008-1936-UI-WAIT-HANDOFF.md)。
