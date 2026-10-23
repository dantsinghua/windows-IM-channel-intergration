# 2026-10-15 发布补齐：质量评价、边界与验证计划

## 修改质量评价（补齐前）

方向正确，但恢复状态机、破坏性动作授权、重试预算和证据语义尚未闭环，不能发布。六个全量失败并非全部都是旧断言；隔离复现已证明 auto_recover=false 仍重启共享 ADB，310 秒内可触发八次。新增正向测试未覆盖关闭开关、上限、跨进程计数、停止竞态以及真实超时与续读的区别。已有会话筛选、IPC 字节预算及幂等保护继续保留。

## R6-112：本轮明确执行的补齐契约

1. ADB 16000 的共享恢复只由 H06 协调器发起，一轮至多一次。所有受影响的在运行企点账号必须允许 auto_recover，且不能有忙任务、提权宽限、非本产品 serial。全局开关 adb_selfheal_enabled 默认 true；滚动一小时上限 adb_selfheal_max_per_hour 默认 3；最短间隔 60 秒。SQLite settings 的 recovery.adb.16000 在外部动作前原子记账，失败、取消均计数，健康探针或进程重启不清零；预算损坏拒绝执行。普通 ensure_root 仍不得调用 kill-server。重连后重新核实目标意图，不复活已停止账号。

2. 微信消息扫描：内部 10 秒、服务 30 秒、HTTP 客户端 35 秒；status/login_status/login_cancel 单独维持 15 秒，不重放。读取的重试统一由 Poller 持有，底层 read 不再隐式再试一次。退避 1/2/4/8/16 秒封顶，最多 10 次失败或累计 600 秒，超限告警后停止自动读。只有明确的 wechat_read_catching_up 才表示保存了续读进度；一般 HTTP 504、user_handler_timeout 保留真实原因，不冒充进度。

3. KEY_FAIL 只表示明确密钥无效；已确认密钥有效但读取失败使用 WECHAT_READ_FAILED。degraded 下继续允许受限只读恢复，禁止发送；只有一次真实读取成功且 key_ok=true 才回 running。显式重试读取不启动 key hook、不重新登录、不清数据。停止/禁用/删除竞态优先于晚到的健康或读取结果。

4. 历史窗口按读取服务本次就绪锚点向前 12 小时，日期粒度只做外层请求区间，再按消息时间和会话 nOrder/ISO nTime 精确过滤。秒/毫秒统一，未知时间保守保留。跨午夜续读不会因日期变更把尚未读取的窗口直接丢弃；公共游标仍只在实际消息落库后推进。

5. QQ H08 只能恢复它自身造成的 login_required(TOKEN_EXPIRED)，必须观察到明确在线心跳，重查 enabled/desired_state/登录尝试，不覆盖 stopped、disabled、其他 error 或扫码流程。掉线走 mark_offline。用户已发起的二维码流程连续失败 login_refresh_max_rounds=10 次时，才允许在相同容器 ID 下停止/启动；不删除数据，不重建容器，不延长原登录截止时间、不自动扫码。其每账号滚动小时预算复用 container_restart_max=5，失败也计数，并受 auto_recover 约束。

6. 新告警码 ADB_SELFHEAL_EXHAUSTED、WECHAT_READ_RETRY_EXHAUSTED、QQ_LOGIN_RECOVERY_EXHAUSTED 均为 warn，明确标记停止重试和原故障原因。既有 H06/H08 的告警严重度保持原语义，不拿其他告警的最后一条记录冒充 H08。

7. Agent 正式 systemd 启动增加 --logfile /var/log/qtrade/agent.log，按天滚动保留七份；日志路径初始化失败明确 LOG_INIT_FAILED，不能无声退化。状态转换与恢复动作带 trace_id；只记录安全元数据，凭据字段脱敏。

## 兼容测试处理

保留普通 ensure_root/单 serial 路径无共享重启的调用断言，将“接口不得存在”的旧断言改为“能力存在但本路径未调用”。H06 关闭恢复场景显式关闭新开关。微信失败重试测试推进假时钟，保留三次真实失败、恢复、游标及发送保护断言；读错误码按 R6-111 分离，不再要求错误叫 KEY_FAIL。原失败记录不删除。

## 发布边界

本轮不改 STS/OSS，不重启现场服务，不触碰真实 IM 账号，不执行安装。固定候选全量测试、原生组件自检、完整 rootfs/载荷与签名核验通过后才总装。包级通过不等于干净机器安装和业务 E2E 通过。
