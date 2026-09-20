-- schema_agent.sql —— agent.db 全部 DDL
-- 🔴 唯一出处 = docs/02-后端与本地数据库设计.md §3.1(v0.4.6);本文件由该节的 ```sql 块逐字抽取,
--    改表先改 02 再重新抽取(不要手改本文件);SQLite ≥ 3.34(trigram FTS),实跑通过于 3.45.1。
-- 抽取方式:re.findall(r"```sql\n(.*?)```", 02 全文)[1]
PRAGMA journal_mode = WAL;
PRAGMA auto_vacuum = INCREMENTAL;     -- 建库即定,之后改不了
PRAGMA foreign_keys = ON;

-- ───────────────────────── 元数据 ─────────────────────────
-- schema_version:顺序迁移记录;version 单调,最后一行即当前版本
CREATE TABLE schema_version (
  version     INTEGER PRIMARY KEY,
  name        TEXT    NOT NULL,                 -- 迁移脚本文件名,如 0003_add_sessions_media_policy
  applied_ms  INTEGER NOT NULL,
  checksum    TEXT    NOT NULL                  -- 脚本 sha256,防被改过的脚本重跑
) STRICT;

-- settings:键值配置与运行期快照(next_seq、.wslconfig 快照、安全闸词表版本、合规告知…)
--   key 前缀约定:seq.* 序号分配;snapshot.* 来自 WinAgent 的快照(wslconfig / wechat_enabled / host_ip);
--   gate.* 安全闸;compliance.ack_ms / compliance.notice_version 合规告知(01-P3);api.version 见 §3.8;
--   api.public_domain(用户填的对外域名,E-3)/ api.public_endpoint {public_ip, checked_ms, changed_ms}(出口 IP 快照,E-3);
--   mail.inbound.fallback.<route_id>(IMAP→POP3 回落态,E-1);⚠️ 邮件模板**不在 settings**,在 mail_templates 表(E-4,基线 v1.2)
--   ⚠️ 本表是 **agent.db 侧**设置真值,只存 Agent 消费的设置;WinAgent 侧(hosts 屏蔽/powercfg/探测/crash dump/微信模块开关等)另有一张**独立的** winagent.db.settings(§3.2),
--      两表**各存各侧、互不同步**——同一项只有一个家(基线 §11.10 [CFGSRC+RETENTION];R2-5)。Agent 要 Windows 侧的值一律经 /wa/v1 取快照,不在本表镜像其真值。
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value_json  TEXT    NOT NULL CHECK (json_valid(value_json)),
  updated_ms  INTEGER NOT NULL,
  updated_by  TEXT    NOT NULL DEFAULT ''       -- actor 串,同 commands.origin_actor
) STRICT;
-- 初始行:('seq.qidian','0'),('seq.qq','0'),('seq.wechat','0')  ← account_id 永不复用的真值

-- ───────────────────────── 账号 ─────────────────────────
-- accounts:系统顶层实体(基线 v1.1 §7.1);行永不物理删除(deleted_ms 软删),id 由 settings.seq.* 单调分配
CREATE TABLE accounts (
  id              TEXT PRIMARY KEY,              -- qdNN/qqNN/wxNN,前缀与 channel 绑定见表尾 CHECK
  channel         TEXT NOT NULL CHECK (channel IN ('qidian','qq','wechat')),
  seq             INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 98),   -- 99 保留给安装自检(C-07)
  label           TEXT NOT NULL,
  host            TEXT NOT NULL CHECK (host IN ('wsl','windows')),
  state           TEXT NOT NULL DEFAULT 'created' CHECK (state IN
                    ('created','provisioning','starting','login_required','logging_in',
                     'running','degraded','stopping','stopped','error','disabled')),
  state_code      TEXT,                          -- 机器可读原因;不 CHECK(05 维护、可增,故不锁枚举)。单一来源 = 05 §2.0 = 基线 §8.1 N-16 的三组共 26 码:
                                                  --   ① 等人(9,伴 login_required+prompt):WAIT_PASSWORD|WAIT_SMS|WAIT_CAPTCHA|WAIT_DEVICE_CONFIRM|WAIT_QRCODE|WAIT_NARRATOR|WAIT_UI_TREE|WAIT_KEY_IMG|WAIT_KEY_RELOGIN
                                                  --   ② 掉线原因(4,亦伴 login_required,「为何回到登录阶段」):KICKED|LOGGED_OUT|TOKEN_EXPIRED|LOGIN_TIMEOUT  ← KICKED/LOGIN_TIMEOUT 属本组不属失败组(不是「坏了」是「等人重登」,不计告警风暴)
                                                  --   ③ 失败/异常(13,伴 error 或 degraded):BOOT_TIMEOUT|APK_UNAVAILABLE|INSTALL_FAILED|BAD_CREDENTIAL|VAULT_UNAVAILABLE|UI_UNEXPECTED|ONEBOT_UNREACHABLE|CONTAINER_EXIT|NARRATOR_UNAVAILABLE|KEY_FAIL|SCREEN_LOCKED|WINAGENT_OFFLINE|WINAGENT_USER_OFFLINE
                                                  --   RATE_LIMITED 作为 state_code 已停产(R-12,无自动重登即无限流);本表的 RATE_LIMITED 只出现在 command_results.code(HTTP 429),是另一个量(R2-12/C-17)
  state_reason    TEXT NOT NULL DEFAULT '',
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  auto_recover    INTEGER NOT NULL DEFAULT 1 CHECK (auto_recover IN (0,1)),  -- 启用但不自动拉起 = 0(C-44)
  login_mode      TEXT NOT NULL CHECK (login_mode IN ('password','qrcode','manual')),
  credential_ref  TEXT,                          -- 'vault://account/qd01';不保存密码则 NULL
  remember        INTEGER NOT NULL DEFAULT 0 CHECK (remember IN (0,1)),  -- R4-6:默认**不存**(基线「默认不存凭据」/#12「remember 默认 false」)。DEFAULT 1 会让用户不勾「记住」也落密文,涉及凭据以基线为准。只有 #2 login.remember=true(带 secret)或 #13 PUT credential 显式传 remember=true 才置 1(R6-55)
  quota_mb        INTEGER NOT NULL,              -- 资源池预算,创建时从 resource_pools.quota_json 拷贝
  capabilities_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(capabilities_json)),
  identity_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(identity_json)),  -- QQ:qq_data 指纹摘要;微信:wxid;企点:见 device_profiles
  settings_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(settings_json)),  -- 账号级设置(05 §2.5.5;PATCH /accounts/{id}/settings)
                                                 -- 含 mail_route_id(E-5,05 §2.5.5:该账号专用邮箱路由;空=按通道级/全局兜底)
                                                 -- 含 auto_stop_on_pressure(E-19,默认 false:内存水位 crit 时是否允许按 LRU 自动停用本账号;false 只建议不自动停,§2.2.5)
  wxid            TEXT,                          -- 微信:取钥成功后回填(R-23,基线 §11.23 [WXID]);建行时 NULL(id=wxNN 先建、state='created')。
                                                 --   非微信恒 NULL;回填后与 wechat_profiles.wxid 一一对应。查重/合并靠下方 ux_accounts_wxid。
  merged_into     TEXT REFERENCES accounts(id),  -- 微信合并墓碑(R-23):新建临时行取钥后发现 wxid 已有老档案 → 本行标 merged_into=老 id 并软删,槽位归老档案
  self_nick       TEXT,
  self_uid        TEXT,
  self_avatar_ref TEXT,                          -- media sha256
  capture_text    INTEGER CHECK (capture_text IN (0,1)),       -- NULL=跟全局
  retention_days  INTEGER CHECK (retention_days > 0),          -- NULL=跟全局
  media_policy_json TEXT CHECK (media_policy_json IS NULL OR json_valid(media_policy_json)),  -- per-kind {"image":"eager",…};NULL=跟全局(C-23)
  created_ms      INTEGER NOT NULL,
  updated_ms      INTEGER NOT NULL,
  last_seen_ms    INTEGER,
  last_running_ms INTEGER,
  deleted_ms      INTEGER,                        -- 软删;删后 id 仍占用
  UNIQUE (channel, seq),
  CHECK ((channel='wechat') = (host='windows')),  -- 微信恒 windows,其余恒 wsl
  CHECK ((channel='qidian' AND id GLOB 'qd[0-9][0-9]') OR (channel='qq' AND id GLOB 'qq[0-9][0-9]')
         OR (channel='wechat' AND id GLOB 'wx[0-9][0-9]'))
) STRICT;
CREATE INDEX ix_accounts_channel_state ON accounts (channel, state) WHERE deleted_ms IS NULL;
-- 微信 wxid 唯一(R-23):同一 wxid 只能有一个活档案;取钥回填时命中已存在的现役档案 → 走合并(merged_into 软删本临时行),
--   故活档案(未软删、未合并)里 wxid 唯一。软删/合并的历史行不占该唯一性。
CREATE UNIQUE INDEX ux_accounts_wxid ON accounts (wxid)
  WHERE wxid IS NOT NULL AND deleted_ms IS NULL AND merged_into IS NULL;

-- account_runtime:运行时载体(容器/端口/进程);端口冗余存,真值由序号推导(§2.2.4)
CREATE TABLE account_runtime (
  account_id      TEXT PRIMARY KEY REFERENCES accounts(id),
  kind            TEXT NOT NULL CHECK (kind IN ('redroid','napcat','wechat_pc')),
  desired_state   TEXT NOT NULL DEFAULT 'stopped' CHECK (desired_state IN ('running','stopped')),
  container_name  TEXT,                          -- 'qtrade-qd01' / 'qtrade-qq03';微信 NULL
  container_id    TEXT,
  image_ref       TEXT,                          -- 'redroid/redroid:11.0.0-latest@sha256:…'
  data_dir        TEXT,                          -- /var/lib/qtrade/accounts/<id>/data
  adb_port        INTEGER, stream_port INTEGER, frida_port INTEGER,       -- 企点
  adb_serial      TEXT,                          -- '127.0.0.1:160NN'(04-P7)
  ws_port         INTEGER, http_port INTEGER, webui_port INTEGER,         -- QQ
  webui_published_until_ms INTEGER,              -- QQ WebUI 临时开启截止(C-35)
  wechat_version  TEXT, wxkey_dll TEXT, chatlog_port INTEGER, pid INTEGER, -- 微信(来自 WinAgent)
  app_version     TEXT,                          -- 企点 APK 版本 / NapCat 版本(企点定位 profile 按此选,G-15)
  app_sha256      TEXT,
  mem_limit_mb    INTEGER,                       -- 容器 --memory(企点默认 3584)
  container_mem_anon_mb INTEGER,                 -- 最近一次 memory.stat anon(列表页不查样本表,04-P7)
  last_started_ms INTEGER, last_stopped_ms INTEGER,
  last_offline_ms INTEGER,                       -- 最近一次掉线时刻(05 §9.15;P-ACCT-DETAIL「上次掉线」)
  last_offline_code TEXT,                        -- 掉线原因码 KICKED|LOGGED_OUT|TOKEN_EXPIRED|LOGIN_TIMEOUT(D-2 不自动重登,只记因)
  offline_count_1h INTEGER NOT NULL DEFAULT 0,   -- 滚动 1 小时内掉线次数;≥3 次把告警升 error(05 §2.5.4)
  error_since_ms  INTEGER,                        -- R5-4:账号迁入 error 时写当前 epoch ms、迁出 error(→running/degraded/stopped 等任一非 error 态)时清回 NULL;槽位接管的「已故障 N 秒」= (now-error_since_ms)/1000,是 §2.2.5 ②「超阈值由用户确认后接管」判定的唯一数据来源(NULL = 当前不在 error,不可接管)
  last_boot_completed_ms INTEGER,                -- 企点 sys.boot_completed=1 时刻
  last_login_ms   INTEGER, last_login_error TEXT,
  login_attempts_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(login_attempts_json)),  -- 限流窗口计数(05)
  suspect_credential INTEGER NOT NULL DEFAULT 0 CHECK (suspect_credential IN (0,1)),      -- 登录报密码错(与 vault flag 同步)
  last_exit_code  INTEGER,
  restart_count   INTEGER NOT NULL DEFAULT 0,
  health_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(health_json)),  -- 最近一次 inspect/健康摘要(H04–H08)
  updated_ms      INTEGER NOT NULL
) STRICT;

-- device_profiles:企点账号机型档案(主文档 §3.3),一经生成永不改变
CREATE TABLE device_profiles (
  account_id      TEXT PRIMARY KEY REFERENCES accounts(id),
  profile_key     TEXT NOT NULL,                 -- 抽自内置档案库的哪一条(GET /device-profiles/templates 的 profile_key,C-40)
  brand           TEXT NOT NULL, manufacturer TEXT NOT NULL, model TEXT NOT NULL,
  device          TEXT NOT NULL, product TEXT NOT NULL,
  build_id        TEXT, release TEXT, sdk INTEGER,
  fingerprint     TEXT NOT NULL,                 -- ro.build.fingerprint
  serialno        TEXT NOT NULL UNIQUE,
  mac             TEXT NOT NULL UNIQUE,          -- 固定写死,重建容器不变
  width           INTEGER NOT NULL DEFAULT 720, height INTEGER NOT NULL DEFAULT 1280,
  dpi             INTEGER NOT NULL DEFAULT 320,
  profile_json    TEXT NOT NULL CHECK (json_valid(profile_json)),   -- 全量 ro.* 覆盖项
  generated_ms    INTEGER NOT NULL
) STRICT;
-- 不可变性由触发器钉死:任何 UPDATE 直接报错
CREATE TRIGGER trg_device_profiles_immutable BEFORE UPDATE ON device_profiles
BEGIN SELECT RAISE(ABORT, 'device_profiles is immutable'); END;

-- resource_pools:两个池(基线 §7.6);quota_json 为各通道单账号预算(此表是真值,agent.toml [pool] 只是初始默认,C-43)
CREATE TABLE resource_pools (
  pool            TEXT PRIMARY KEY CHECK (pool IN ('wsl','windows')),
  total_mb        INTEGER NOT NULL,              -- wsl:.wslconfig memory;windows:物理内存
  reserved_mb     INTEGER NOT NULL,              -- wsl:dockerd/agent/系统 2048;windows:系统+控制台+WinAgent 4096
  wechat_enabled  INTEGER NOT NULL DEFAULT 0 CHECK (wechat_enabled IN (0,1)),  -- 仅 windows 行;winagent.toml [wechat] enabled 的快照(真值在那边)
  slot_holder     TEXT NOT NULL DEFAULT '',      -- 微信槽位:当前在线/登录中的 wxNN;'' = 空(C-01)
  slot_pending    TEXT NOT NULL DEFAULT '',      -- 微信槽位:切换/登录中的目标 wxNN 或 'new'(R-23:'new' 时对应的临时 accounts 行已按 wxNN 先建)
  slot_pending_expires_ms INTEGER,               -- 微信槽位 pending TTL(R-04,基线 §11.18 [SLOT]/§15b N-2):置 pending 时 = now + agent.toml [wechat] slot_pending_ttl_s×1000;
                                                 --   NULL = 无 pending。reaper 每 60s 扫「slot_pending<>'' AND slot_pending_expires_ms<now」→ 置回 free(§2.2.5)
  slot_pending_login_session_id TEXT NOT NULL DEFAULT '',   -- R6-6:**pending 那一次登录尝试**的 login_session_id('ls_'+ULID,§3.4.7 生成规则)。
                                                 --   🔴 空值口径(三处一致):无 pending 时**出空串 ''、不是 null**——与同级 wechat_slots.pending:"" 一致;
                                                 --     只有 pending_expires_at 是 null(它是时刻、天然可空)。库列 NOT NULL DEFAULT '' 即此口径的落点。
                                                 --   与 slot_pending / slot_pending_expires_ms **同生同灭**:置 pending 时三者一起写、三条释放路径与「pending 转 holder」时三者一起清。
                                                 --   出参 = wechat_slots.pending_login_session_id(#69);消费方 01 的列表「取消绑定」必须带它回来(#16b),
                                                 --   否则用户点取消与后台重开的新尝试撞车 ⇒ 误杀新尝试(N-3 原危害)
  quota_json      TEXT NOT NULL CHECK (json_valid(quota_json)),   -- {"qidian":2560,"qq":614,"wechat":1536}
  calibration_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(calibration_json)), -- 自校准实测 anon/RSS
  calibrated_ms   INTEGER,
  source          TEXT NOT NULL DEFAULT 'default' CHECK (source IN ('default','winagent','manual','calibrated')),
  updated_ms      INTEGER NOT NULL,
  CHECK (pool = 'windows' OR (slot_holder = '' AND slot_pending = '' AND slot_pending_expires_ms IS NULL AND slot_pending_login_session_id = '' AND wechat_enabled = 0)),
  CHECK (slot_pending <> '' OR (slot_pending_expires_ms IS NULL AND slot_pending_login_session_id = ''))   -- 没有 pending 就不该有过期时刻,也不该有残留的尝试 id(R-04 / R6-6)
) STRICT;

-- ───────────────────────── 指令 ─────────────────────────
-- commands:进入总线的每条指令(基线 §7.2);status 是总线视角,结果在 command_results
CREATE TABLE commands (
  trace_id        TEXT PRIMARY KEY,              -- ULID
  account_id      TEXT NOT NULL REFERENCES accounts(id),
  op              TEXT NOT NULL,                 -- 能力名 send_text / read_messages …
  args_json       TEXT NOT NULL CHECK (json_valid(args_json)),   -- 不含正文明文(P-11/E-11 核对):args.text 入库前替换为 {"text_sha8","text_len"};login 类 secret 擦成 "***";image_ref/file_ref 只是媒体引用
  idempotency_key TEXT,                          -- 邮件入口已改写为 mail:{短名}:{req_id}(C-10)
  confirm         INTEGER NOT NULL DEFAULT 1 CHECK (confirm IN (0,1)),
  timeout_ms      INTEGER NOT NULL DEFAULT 30000,
  origin_transport TEXT NOT NULL CHECK (origin_transport IN ('local','http','email')),
  origin_actor    TEXT NOT NULL,                 -- token:console | app:xxx | mail:ops@corp
  origin_ip       TEXT,                          -- 邮件入口尽力可空(06-P4)
  status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','cancelled')),
  workflow_run_id TEXT,                          -- 由工作流发起时
  workflow_step_id TEXT,
  broadcast_id    TEXT,                          -- 同一次 /broadcast 的分组 ULID
  mail_inbox_id   INTEGER,                       -- 邮件入口来源(不做 FK:邮件行可被清理)
  submitted_ms    INTEGER NOT NULL,              -- 恒 Agent 接受时刻,邮件 Date 不进来(06-P5)
  started_ms      INTEGER,
  finished_ms     INTEGER
) STRICT;
CREATE INDEX ix_commands_account_time ON commands (account_id, submitted_ms DESC);
CREATE INDEX ix_commands_status ON commands (status) WHERE status IN ('queued','running');
CREATE INDEX ix_commands_run ON commands (workflow_run_id) WHERE workflow_run_id IS NOT NULL;

-- command_results:统一返回(基线 §7.3),一条指令一行
CREATE TABLE command_results (
  trace_id        TEXT PRIMARY KEY REFERENCES commands(trace_id) ON DELETE CASCADE,
  ok              INTEGER NOT NULL CHECK (ok IN (0,1)),
  code            TEXT NOT NULL CHECK (code IN
                    ('OK','DELIVERED','SEND_CALLED_BUT_UNCONFIRMED','SEND_FAILED','IDEMPOTENT_REPLAY',
                     'GATE_BLOCKED','UNSUPPORTED','NOT_APPLICABLE','NOT_READY','TARGET_NOT_FOUND',
                     'LOGIN_REQUIRED','CAPTCHA_REQUIRED','TIMEOUT','RESOURCE_EXHAUSTED',
                     'INVALID_ARGS','UNAUTHORIZED','FORBIDDEN','RATE_LIMITED','DISK_FULL','INTERNAL')),
                                                 -- DISK_FULL(E-18):写失败先判磁盘满(SQLITE_FULL/ENOSPC/disk I/O error),不归 INTERNAL;基线 §8.3 提议补
  data_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data_json)),
  cost_ms         INTEGER NOT NULL,
  source          TEXT CHECK (source IN ('history','get_msg','chatlog','qidian_db','frida','uiautomator','screenshot','onebot','ui','winagent')),  -- +qidian_db(企点旁路读库,读取正线,06 §2.9.5;frida 保留作追溯,企点已不产出);+winagent(C-18);screenshot = 截图 + 离线 OCR(A-1,非多模态)
  state_before    TEXT, state_after TEXT,
  error_message   TEXT,
  error_retryable INTEGER CHECK (error_retryable IN (0,1)),
  error_needs_human INTEGER CHECK (error_needs_human IN (0,1)),
  confirmed_by    TEXT,                          -- history|get_msg|chatlog|ingest_merge(送达确认方式,C.4.2;由 messages.confirmed_by 派生。企点读库正线 = ingest_merge,history 仅企点控件树降级路线,R6-38)
  confirm_ms      INTEGER,
  screenshot_before_ref TEXT, screenshot_after_ref TEXT,   -- media sha256 → 出参 data.shots{before,after}(01-P11)
  finished_ms     INTEGER NOT NULL
) STRICT;

-- idempotency:幂等键 + 账号维度唯一(C.4.3);SENDING/DONE/ABANDONED 三态
--   邮件入口的 idem_key 已由 bus 改写为 'mail:{发件人短名}:{req_id}'(C-10),表结构不变
CREATE TABLE idempotency (
  account_id      TEXT NOT NULL REFERENCES accounts(id),
  idem_key        TEXT NOT NULL CHECK (length(idem_key) <= 128),
  op              TEXT NOT NULL,
  args_hash       TEXT NOT NULL,                 -- sha256(canonical args);同 key 不同参数 → INVALID_ARGS(不执行)
  status          TEXT NOT NULL CHECK (status IN ('SENDING','DONE','ABANDONED')),
                                                 -- ⚠️ 结果 needs_human(LOGIN_REQUIRED/CAPTCHA_REQUIRED)时**不落 DONE、直接删本行**(06 §9):
                                                 --    人处理完登录后同键重试才是对的;记成 DONE 会让重试永远拿到旧的失败回放
  trace_id        TEXT NOT NULL,                 -- 首次执行的 trace
  result_code     TEXT,                          -- DONE 时冗余
  created_ms      INTEGER NOT NULL,
  updated_ms      INTEGER NOT NULL,
  expires_ms      INTEGER NOT NULL,              -- 默认 created + 7 天;过期行清理
  PRIMARY KEY (account_id, idem_key)
) WITHOUT ROWID, STRICT;
CREATE INDEX ix_idem_expires ON idempotency (expires_ms);

-- ───────────────────────── 工作流 ─────────────────────────
-- workflows:YAML 存表、经 API 导入;Agent 不读 /etc/qtrade 下任何工作流文件(G-09)
CREATE TABLE workflows (
  id              TEXT PRIMARY KEY,              -- ULID
  name            TEXT NOT NULL UNIQUE,
  version         INTEGER NOT NULL DEFAULT 1,
  yaml            TEXT NOT NULL,
  checksum        TEXT NOT NULL,                 -- sha256(yaml)
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  schedule_cron   TEXT,                          -- 内置调度(NULL=只手动/API 触发)
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  updated_by      TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE TABLE workflow_runs (
  run_id          TEXT PRIMARY KEY,              -- ULID
  workflow_id     TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,   -- 🔴 R6-58 (g):没有 CASCADE 则「run 留 30 天(§2.8.4)」与「#42 删工作流」互斥 —— 跑过一次的工作流就再也删不掉(外键拒绝)。删工作流 = 连带删它的全部 run(steps 再经下表的 CASCADE 连带删);有 status ∈ {running,paused} 的 run 时 #42 一律 409,不靠外键兜
  workflow_version INTEGER NOT NULL,
  trigger         TEXT NOT NULL CHECK (trigger IN ('api','schedule','email','console')),
  actor           TEXT NOT NULL,
  args_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(args_json)),
  status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','paused','done','failed','cancelled','needs_human')),  -- paused:账号切换挂起(05 §2.4.5)
  pause_reason    TEXT,
  error           TEXT,
  started_ms      INTEGER NOT NULL, finished_ms INTEGER
) STRICT;
CREATE INDEX ix_runs_wf_time ON workflow_runs (workflow_id, started_ms DESC);

CREATE TABLE workflow_steps (
  step_id         TEXT PRIMARY KEY,              -- ULID
  run_id          TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
  idx             INTEGER NOT NULL,              -- 线性执行序(R-13 直线流程;非 for_each 展开)
  step_name       TEXT NOT NULL,                 -- YAML 里的 id
  op              TEXT NOT NULL,
  account_id      TEXT,                          -- 内置步骤(webhook/sleep)为 NULL
  trace_id        TEXT,                          -- 对应 commands 行
  status          TEXT NOT NULL CHECK (status IN ('running','done','failed','skipped','retrying','needs_human')),
  attempt         INTEGER NOT NULL DEFAULT 1,
  result_code     TEXT,
  note            TEXT,                          -- 参数摘要/失败原因(不含正文)
  started_ms      INTEGER NOT NULL, finished_ms INTEGER
) STRICT;
CREATE INDEX ix_steps_run ON workflow_steps (run_id, idx);

-- ───────────────────────── 会话/消息/媒体 ─────────────────────────
-- sessions:通道内的聊天对象(基线 §1);id = {account_id}:{原生ID}(拆分规则 C-11)
CREATE TABLE sessions (
  id              TEXT PRIMARY KEY,
  account_id      TEXT NOT NULL REFERENCES accounts(id),
  channel         TEXT NOT NULL CHECK (channel IN ('qidian','qq','wechat')),
  native_id       TEXT NOT NULL,                 -- QQ:g_123456(群)/ 纯 uin(单聊);微信:12345@chatroom / wxid_xxx
                                                 -- 🔴 企点(R6-21 逐字定死,00 §6 同句):**单聊 = <对端uin>、群 = g_<群号>**——取值来源 R6-36 改主库
                                                 --   XOR(frienduin) 列 + istroop(单聊 frienduin=对端uin,群 frienduin=群号;istroop 0→private/1→group,不进 id);
                                                 --   群加 g_ 与 QQ 通道同惯例,**防对端 uin 与群号数值相撞**(只取前缀不加前缀会撞成同一会话)。
                                                 --   例:qd01:415011447 / qd01:g_123456。docid 是**消息**水位号、**不是**会话标识。
                                                 --   会话标题**仅**读库不可用、降级到控件树兜底路时使用(标题改名时适配器可改写并留旧名别名)
  name            TEXT NOT NULL,                 -- 可变(群改名);native_id 不变
  kind            TEXT NOT NULL CHECK (kind IN ('group','private')),
  member_count    INTEGER,
  last_msg_ms     INTEGER,
  msg_count       INTEGER NOT NULL DEFAULT 0,
  first_seen_ms   INTEGER,
  unread          INTEGER NOT NULL DEFAULT 0,
  muted           INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0,1)),        -- 不采集(仍可发)
  capture_text    INTEGER CHECK (capture_text IN (0,1)),   -- NULL=跟账号
  retention_days  INTEGER CHECK (retention_days > 0),      -- NULL=跟账号
  media_policy_json TEXT CHECK (media_policy_json IS NULL OR json_valid(media_policy_json)),  -- per-kind;NULL=跟账号
  extra_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra_json)),  -- 通道私有(群主/公告/aliases 旧标题…)
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  UNIQUE (account_id, native_id)
) STRICT;
CREATE INDEX ix_sessions_account_last ON sessions (account_id, last_msg_ms DESC);

-- messages:消息库(基线 v1.1 §7.4;C-21)。必须保留 rowid(FTS 外部内容表用),不得 WITHOUT ROWID
CREATE TABLE messages (
  id              TEXT PRIMARY KEY,              -- msg_ + ULID
  ext_msg_id      TEXT,                          -- 通道原生 id / anchor 时 = fingerprint(full sha256、无前缀,§2.8.1/06 §2.9.2;原 'fp:…' 前缀已按 99c C-03 作废);出向 SENDING 行为 NULL,确认后补
  dedup_kind      TEXT NOT NULL DEFAULT 'native' CHECK (dedup_kind IN ('native','anchor')),
  fingerprint     TEXT,                          -- 06 §2.9.2 通用指纹(跨源合并 / 出向确认匹配);不唯一
  account_id      TEXT NOT NULL REFERENCES accounts(id),
  channel         TEXT NOT NULL CHECK (channel IN ('qidian','qq','wechat')),
  session_id      TEXT NOT NULL REFERENCES sessions(id),   -- ⚠️ R4-7:写入前必须保证 sessions 行存在——入库一律「先 upsert sessions 再写 messages」同一事务(§2.8.1),否则新会话第一条消息直接违反本外键

  dir             TEXT NOT NULL CHECK (dir IN ('in','out')),
  type            TEXT NOT NULL CHECK (type IN ('text','image','voice','file','video','system','unknown')),
  state           TEXT NOT NULL DEFAULT 'DELIVERED' CHECK (state IN ('SENDING','DELIVERED','UNCONFIRMED','FAILED')),  -- 出向确认态;入向恒 DELIVERED
  text            TEXT,                          -- 正文;capture_text=0 时 NULL
  text_len        INTEGER,                       -- 正文长度(字符),正文关了也写
  text_hash       TEXT,                          -- sha256(text),正文关了也写
  text_source     TEXT NOT NULL DEFAULT 'native' CHECK (text_source IN ('native','asr')),
  media_json      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(media_json)),  -- [{media_id,ref,sha256?,mime,size,kind,state}](sha256 下载完成后回填)
  sender_id       TEXT, sender_name TEXT,
  is_self         INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0,1)),
  ts_ms           INTEGER NOT NULL,              -- 消息时间(通道给的;出向 = 发送时刻)
  received_ms     INTEGER NOT NULL,              -- 本系统入库时间
  source          TEXT NOT NULL CHECK (source IN ('chatlog','onebot','qidian_db','frida','uiautomator','screenshot','ui')),  -- +qidian_db(企点旁路读库,06 §2.9.5;frida 保留作追溯);ui:企点 UI 路线出向行(R6-31:基线 §7.4 Message.source 已采纳该值,§9 第 1 条已闭合);screenshot = 截图 + 离线 OCR 读出的文本(A-1,非多模态)
  revoked         INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1)),
  revoked_ms      INTEGER, revoked_by TEXT,      -- 撤回标记不删
  trace_id        TEXT,                          -- dir='out' 时对应发送指令
  idempotency_key TEXT,                          -- dir='out' 时的幂等键(确认阶段先查是否已发)
  confirmed_by    TEXT CHECK (confirmed_by IS NULL OR confirmed_by IN ('history','get_msg','chatlog','ingest_merge')),
  confirmed_ms    INTEGER,
  raw_ref         TEXT,                          -- 原始载荷落盘相对路径 accounts/<id>/raw/<yyyymm>/…(按开关)
  asr_state       TEXT NOT NULL DEFAULT 'none' CHECK (asr_state IN ('none','pending','done','failed')),
  asr_text        TEXT, asr_confidence REAL,     -- voice_to_text 结果
  CHECK (dir = 'out' OR state = 'DELIVERED')
) STRICT;
CREATE UNIQUE INDEX ux_messages_ext ON messages (account_id, ext_msg_id) WHERE ext_msg_id IS NOT NULL;  -- 部分唯一索引(C-21)
CREATE INDEX ix_messages_fingerprint ON messages (account_id, fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX ix_messages_session_ts ON messages (session_id, ts_ms DESC);
CREATE INDEX ix_messages_account_ts ON messages (account_id, ts_ms DESC);
CREATE INDEX ix_messages_ts ON messages (ts_ms);          -- 保留期清理
CREATE INDEX ix_messages_idem ON messages (account_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX ix_messages_out_pending ON messages (account_id, session_id, ts_ms) WHERE dir = 'out' AND state IN ('SENDING','UNCONFIRMED');

-- messages_fts:FTS5 外部内容表,trigram 分词(§2.8.6);触发器同步
CREATE VIRTUAL TABLE messages_fts USING fts5 (
  text, sender_name,
  content='messages', content_rowid='rowid',
  tokenize='trigram case_sensitive 0'
);
CREATE TRIGGER trg_messages_ai AFTER INSERT ON messages WHEN new.text IS NOT NULL BEGIN
  INSERT INTO messages_fts(rowid, text, sender_name) VALUES (new.rowid, new.text, new.sender_name);
END;
CREATE TRIGGER trg_messages_ad AFTER DELETE ON messages WHEN old.text IS NOT NULL BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, sender_name) VALUES ('delete', old.rowid, old.text, old.sender_name);
END;
CREATE TRIGGER trg_messages_au AFTER UPDATE OF text, sender_name ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, sender_name)
    SELECT 'delete', old.rowid, old.text, old.sender_name WHERE old.text IS NOT NULL;
  INSERT INTO messages_fts(rowid, text, sender_name)
    SELECT new.rowid, new.text, new.sender_name WHERE new.text IS NOT NULL;
END;

-- media:按 sha256 去重的媒体文件索引;文件在 /var/lib/qtrade/media/<yyyymm>/<sha256>(无扩展名)
--   主键是自增 id:懒加载/下载中的行还没有 sha256(99b ①);ready 后 sha256 必填且唯一,同 sha 的 pending 行下载完成后合并进既有行
CREATE TABLE media (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256          TEXT,                          -- pending/failed 态可 NULL;ready 必填
  kind            TEXT NOT NULL CHECK (kind IN ('image','voice','file','video','other')),   -- C-23
  mime            TEXT,                          -- 按字节头识别;下载前可 NULL(来源声明的类型放 origin_json)
  size            INTEGER,                       -- 下载前可 NULL(来源声明的大小放 origin_json,用于 oversize 预判)
  rel_path        TEXT,                          -- 'media/202609/<sha256>';ready 后必填
  width           INTEGER, height INTEGER, duration_ms INTEGER,
  status          TEXT NOT NULL CHECK (status IN ('pending','ready','failed','skipped_oversize','expired')),   -- expired:文件已按 media_days 删、元数据保留到引用归零(E-10)
  fail_reason     TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ms INTEGER,
  origin_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(origin_json)),   -- 首次来源 {kind:'url|chatlog_md5|qidian_ref', ref, account_id}
  first_message_id TEXT,
  ref_count       INTEGER NOT NULL DEFAULT 0,
  first_seen_ms   INTEGER NOT NULL, ready_ms INTEGER, last_ref_ms INTEGER,
  CHECK (status <> 'ready' OR (sha256 IS NOT NULL AND mime IS NOT NULL AND size IS NOT NULL AND rel_path IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX ux_media_sha ON media (sha256) WHERE sha256 IS NOT NULL;   -- 部分唯一:pending 行可无 sha
CREATE INDEX ix_media_status ON media (status, next_attempt_ms) WHERE status IN ('pending','failed');
CREATE INDEX ix_media_gc ON media (ref_count, first_seen_ms) WHERE ref_count = 0;

-- cursors:所有游标/水位一张表(§2.8.3;C-24:与消息落库同事务推进)
CREATE TABLE cursors (
  owner           TEXT NOT NULL,                 -- account_id | 'mail:<mailbox>' | 'events'
  kind            TEXT NOT NULL,                 -- 'chatlog_seq:<talker>' | 'onebot_seq:<native_id>'(R6-58 (v):原 '<session>' 写法作废,owner 列已带账号)| 'ws_last_event'(R6-58 (v):QQ meta_event 水位,只给监控看)| 'qidian_rowid:<native_id>'(R6-36:企点主库读取正线,单库多水位、一会话表一条,value_int=该表最大 _id、value={"last_uniseq"};原 'qidian_docid' 单库单水位作废)| 'qidian_bootstrap'(R6-38/R6-39:每企点账号一行,value=登录 uin、value_int=历史闸基准 ms,§2.8.3)
                                                 --   | 'qidian_anchor:<session>' / 'sessions_scan'(两者均**仅控件树兜底路**,R6-25;sessions_scan 出处 06 §2.9.3)
                                                 --   | 'imap_uid' | 'pop3_uidl_recent' | 'ws_seq' | …(全集见 §2.8.3)
  value           TEXT,                          -- 字符串态(锚块 JSON / uidvalidity / UIDL 数组)
  value_int       INTEGER,                       -- 可比较的整数水位
  updated_ms      INTEGER NOT NULL,
  PRIMARY KEY (owner, kind)
) WITHOUT ROWID, STRICT;

-- jobs:异步作业统一表(R-21,基线 §11.21 [JOB];初版基线表名,无迁移——项目未发行,§15b N-1)。
--   凡返回 202 {job_id} 的端点全部收编:消息导出(#51)、诊断包(#80)、QQ 身份卷导出(#99)、
--   messages/purge(#54)、mail/cleanup/run(#64)、**全量本地清理(#109,R6-24)**、微信重装(WinAgent #40 触发)、resources/calibrate(#25/#71)、
--   accounts/{id}/purge(#8)、mail/cleanup/run(#64,job_id 即该行主键)、镜像加载(03 触发)。统一 GET /jobs/{job_id} 查、POST /jobs/{job_id}/cancel 取消、终态推 job 事件。
--   领取(fetch/claim)语义:worker 池从 ix_jobs_active 拿最早的 queued 行,用 §2.3.1 ③ 的行级 claim 抢占——
--     `UPDATE jobs SET state='running', attempt_count=attempt_count+1, updated_ms=:now WHERE job_id=:id AND state='queued'`,按 rowcount==1 判到手(多 worker 并发只有一个抢到,天然跨进程/崩溃不残留)。
--     取到即跑,进度写 progress、终态写 result_json/error_json 并推 job 事件;崩溃留在 running 的由 reaper 按 updated_ms 超龄回收(重置 queued 或判 failed)。
--   🔴 R6-16 回收规则(超时值不再口头化,配置键 = agent.toml [jobs] reclaim_after_s,默认 900 秒;扫描周期 [jobs] reclaim_interval_s,默认 60 秒,§7.1):
--     `jobs_reclaimer` 在 scheduler 里每 reclaim_interval_s 跑一轮,走 §2.3.1 ③ 的行级 claim(幂等、多实例安全):
--       UPDATE jobs SET state='queued', updated_ms=:now
--        WHERE state='running' AND :now - updated_ms >= reclaim_after_s*1000 AND attempt_count < 3;   -- 超龄且还能再试 → 退回队列
--       UPDATE jobs SET state='failed', error_json='{"code":"INTERNAL","message":"作业超时未心跳,已回收"}', updated_ms=:now
--        WHERE state='running' AND :now - updated_ms >= reclaim_after_s*1000 AND attempt_count >= 3;  -- 反复超时 → 判死,不无限重跑
--     worker 跑长作业期间**必须定期刷 updated_ms**(写 progress 即刷),否则会被自己的 reclaim 误判超龄而重复执行。
--     reclaim_after_s 必须 > 最慢作业的单轮时长(messages_export/diagnostics 是上限),配小了 = 导出被反复重启永远跑不完。
--   泛化字段:progress(0-100)、result_json(产物:rows/bytes/file_path/download_url…)、error_json(code/message);
--   原 fmt/with_media/filter_json 收进 params_json(各 kind 自定义)。保留期 export_jobs_days(默认 7,§2.8.4);
--   R4-13:jobs **随产物 7 天**(基线 §11.10/§11.21 口径)——产物(exports/<job_id>.<ext>)也只留 7 天,产物都删了、只剩一条无法下载的作业记录留 30 天没有意义;故 jobs 不进「数据类 30 天」清单,跟产物同期过期。
CREATE TABLE jobs (
  job_id          TEXT PRIMARY KEY,              -- ULID
  kind            TEXT NOT NULL CHECK (kind IN
                    ('messages_export','diagnostics','identity_export','messages_purge',
                     'mail_cleanup','wechat_reinstall','resources_calibrate','account_purge','image_load',
                     'system_cleanup')),   -- R6-24:#109 POST /system/cleanup/run 的全量本地清理(绝不删远端邮件,基线 §11.11 R4-2;删远端只走 mail_cleanup)
  account_id      TEXT,                          -- 与账号相关的作业(identity_export/account_purge/…);无关作业 NULL
  actor           TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','succeeded','failed','cancelled','expired')),
  progress        INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  params_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json)),   -- 各 kind 入参:导出 {fmt,with_media,filter};purge {mode,before};cleanup {dry_run}…
  result_json     TEXT CHECK (result_json IS NULL OR json_valid(result_json)),  -- 终态产物:{rows,bytes,file_path,download_url,expires_at,freed_mb…}
  error_json      TEXT CHECK (error_json IS NULL OR json_valid(error_json)),    -- 失败:{code,message}
  attempt_count   INTEGER NOT NULL DEFAULT 0,    -- R6-16:被领取的次数;每次 claim 到手 +1,reclaim 判「退回队列 vs 判死」用(≥3 判死)
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL, expires_ms INTEGER
) STRICT;
CREATE INDEX ix_jobs_active ON jobs (state, updated_ms) WHERE state IN ('queued','running');
CREATE INDEX ix_jobs_kind_created ON jobs (kind, created_ms DESC);

-- ───────────────────────── 事件与外部调用方 ─────────────────────────
-- webhooks:出站回调登记(D.3)
CREATE TABLE webhooks (
  id              TEXT PRIMARY KEY,              -- ULID
  name            TEXT NOT NULL,
  url             TEXT NOT NULL,
  secret_ref      TEXT NOT NULL,                 -- vault://webhook/<id>(HMAC 密钥)
  events_json     TEXT NOT NULL DEFAULT '["*"]' CHECK (json_valid(events_json)),     -- 订阅的 event 类型
  accounts_json   TEXT NOT NULL DEFAULT '["*"]' CHECK (json_valid(accounts_json)),   -- 订阅的账号
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  timeout_ms      INTEGER NOT NULL DEFAULT 5000,
  max_attempts    INTEGER NOT NULL DEFAULT 10,
  consecutive_fail INTEGER NOT NULL DEFAULT 0,
  dead_ms         INTEGER,                       -- 连续失败超限自动停用时刻
  api_client_id   TEXT,                          -- 属于哪个 app_id(可 NULL)
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL
) STRICT;

-- events_outbox:统一事件(基线 v1.1 §7.5)的持久化与投递状态;target='ws' 行是规范记录
CREATE TABLE events_outbox (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,   -- WS 重放游标
  event_id        TEXT NOT NULL,                 -- ULID,同一事件的多目标行共享
  target          TEXT NOT NULL,                 -- 'ws' | 'webhook:<id>'
  event           TEXT NOT NULL CHECK (event IN ('message','account_state','command_done','alert','resource','mail','net','workflow','job')),  -- +net/workflow(C-16);+job(R-21 异步作业终态,§3.4.10)
  ts_ms           INTEGER NOT NULL,
  trace_id        TEXT, account_id TEXT, channel TEXT,
  payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed','dead')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_ms INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  delivered_ms    INTEGER,
  UNIQUE (event_id, target)
) STRICT;
CREATE INDEX ix_outbox_due ON events_outbox (status, next_attempt_ms) WHERE status = 'pending';
CREATE INDEX ix_outbox_ws ON events_outbox (target, seq) WHERE target = 'ws';

-- api_clients:调用方(Bearer 令牌或 HMAC app)
CREATE TABLE api_clients (
  app_id          TEXT PRIMARY KEY,              -- 'console' 内置;其余用户建
  name            TEXT NOT NULL,
  auth_kind       TEXT NOT NULL CHECK (auth_kind IN ('bearer','hmac')),
  secret_hash     TEXT NOT NULL,                 -- bearer:sha256(token);hmac:secret 存 Vault,这里存 sha256 供轮换比对
  secret_ref      TEXT,                          -- hmac:vault://api/<app_id>(验签要明文);console:vault://winagent/console_token(C-04)
  level           TEXT NOT NULL CHECK (level IN ('read','write','admin')),
  ip_allow_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(ip_allow_json)),   -- CIDR 列表,空=不限;console 固定 ["127.0.0.1/32"](P-15)
  allow_ops_json  TEXT NOT NULL DEFAULT '["*"]' CHECK (json_valid(allow_ops_json)),   -- 默认值 '["*"]' = 「全部 danger=false 能力」(不是全能力目录);经基线 §11.17 ② expand_allow_ops() 解释:星号只展开 danger=false,danger=true 的 op 须逐条列名才放行(R2-1)
  allow_accounts_json TEXT NOT NULL DEFAULT '["*"]' CHECK (json_valid(allow_accounts_json)),
  rate_per_min    INTEGER NOT NULL DEFAULT 120,
  api_version_min INTEGER NOT NULL DEFAULT 1,    -- 该调用方声明支持的最低 API 主版本(§3.8)
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  last_used_ms    INTEGER, revoked_ms INTEGER
) STRICT;

-- audit_log:API 与指令审计;不含正文与密码(基线 §11.2 [NOLOG])
CREATE TABLE audit_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms           INTEGER NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('command','api','system','stream_input')),   -- /audit?kind= 过滤(C-40);+stream_input(R-06:login_required 下画面注入类逐条留痕,text 只记 sha8:len)
  transport       TEXT NOT NULL CHECK (transport IN ('local','http','email','system')),
  actor           TEXT NOT NULL,                 -- token:console | app:xxx | mail:addr | system:scheduler
  ip              TEXT,
  action          TEXT NOT NULL,                 -- 'POST /api/v1/accounts/qd01/send' | 'op:send_text' | 'settings.update' | 'qidian.rebootstrap'(R6-50,见下)
  account_id      TEXT,
  trace_id        TEXT,
  result_code     TEXT,                          -- 基线 §8.3 码或 HTTP 状态
  http_status     INTEGER,
  cost_ms         INTEGER,
  detail_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))   -- 对象/长度/hash 摘要,不含正文(§2.9 脱敏表)
                                                 -- 🔴 R6-7:高危 op(danger=true)的审计 **不记 confirm_via** —— v1 它恒为 'console'(唯一合法取值),
                                                 --   记一个常量零信息量,还会让读审计的人以为存在别的确认通道。要记的是「谁在什么时候批的」:
                                                 --   actor(token:console)+ ts_ms + action='mail.pending_confirm.approved|rejected|expired|args_mismatch'
                                                 --   + detail_json{inbox_id, op, from_addr[, args_digest | expected_digest+actual_digest][, expired_by]}。
                                                 -- 🔴 R6-29:'…expired' 这条的 actor 分两路——reaper 触发 = 'system:scheduler' 且 detail_json.expired_by='reaper';
                                                 --   #68c/#68d 端点顺手触发 = 发起该请求的控制台操作者且 expired_by='endpoint'。expired_by ∈ {reaper, endpoint}。
                                                 -- 🔴 R6-29:mail_inbox.reason 前缀三值(OP_DENIED 三义复用的分辨手段)——NOT_ALLOWED: / REJECTED: / ARGS_TAMPERED:。
                                                 -- 🔴 R6-50:企点换登录号(同一 qdNN 先后登录不同 uin)时 06 §2.9.5 poll_maindb ③ 在删该账号全部 qidian_rowid:* 水位之前记
                                                 --   action='qidian.rebootstrap'(kind='system', transport='system', actor='system:qidian_adapter', account_id=<qdNN>, trace_id=NULL,
                                                 --   result_code='OK', detail_json{old_uin, new_uin, deleted_cursors:<被删的 qidian_rowid:* 行数>})——破坏性动作必须留痕;05 §2.1.1 ⑪a 与 06 引用的就是这个名字,不得另起同义名。
) STRICT;
CREATE INDEX ix_audit_ts ON audit_log (ts_ms DESC);
CREATE INDEX ix_audit_kind_ts ON audit_log (kind, ts_ms DESC);
CREATE INDEX ix_audit_actor ON audit_log (actor, ts_ms DESC);
CREATE INDEX ix_audit_account ON audit_log (account_id, ts_ms DESC) WHERE account_id IS NOT NULL;

-- ───────────────────────── 健康采样(WSL 侧;与 winagent.db 同结构,C-31) ─────────────────────────
-- health_samples:04 §3.1 窄表 + 三级降采样(raw 24h / 1m 7d / 1h 30d);scope/subject 见 04 §2.2
--   ⚠️ R-13 不双库同步:本表只落 **WSL 侧**采样(容器/vmmem/Agent 自身);Windows 侧采样只落 winagent.db 同名表。
--   P-RES 的整机+我方两组数由 Agent 在**查询期合并**(#77 读本表 + GET /wa/v1/metrics),不做任何跨侧同步写。
CREATE TABLE health_samples (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms           INTEGER NOT NULL,
  resolution      TEXT NOT NULL CHECK (resolution IN ('raw','1m','1h')),
  scope           TEXT NOT NULL CHECK (scope IN ('host','wsl','container','process','disk','net')),
  subject         TEXT NOT NULL,                 -- 'host' | 'vmmem' | 'qtrade-qd01' | 'Weixin.exe' | 'C:' | '/var/lib/qtrade' | 'eth0'
  cpu_pct         REAL,                          -- 容器为 核数×100
  mem_mb          REAL,                          -- 整机 used;进程 RSS;容器 memory.current
  mem_anon_mb     REAL,                          -- 容器专用 memory.stat anon(资源池真值)
  mem_max_mb      REAL,                          -- 容器 memory.max;整机 total
  disk_free_mb    REAL, disk_total_mb REAL,      -- scope='disk'
  net_rx_kbps     REAL, net_tx_kbps REAL,        -- scope='net'
  extra_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra_json)),   -- ksm_sharing_mb / zram_used_mb / clock_drift_ms …
  agg_max_json    TEXT CHECK (agg_max_json IS NULL OR json_valid(agg_max_json)) -- 1m/1h 聚合行的各列 max
) STRICT;
CREATE INDEX ix_health_lookup ON health_samples (scope, subject, resolution, ts_ms);
CREATE INDEX ix_health_ts ON health_samples (resolution, ts_ms);   -- 降采样与清理

-- ───────────────────────── 邮件摆渡(列名以 06 §3.1 为准,C-26;路由 E-5) ─────────────────────────
-- mail_routes:邮箱路由(安琳 E-5:按通道配多邮箱,预留按登录账号配)。键 (channel, account_id) 三级:
--   全局 = 两者 NULL;通道级 = channel 非空、account_id NULL;账号级 = 两者非空。查找顺序 account → channel → global(§2.2.9)。
--   SQLite 的 UNIQUE 把 NULL 视为互不相等,故唯一性用 COALESCE 表达式索引(否则可以插两行全局路由)。
--   inbound_json:{protocol:'imap|pop3', host, port, ssl, user, secret_ref, folders[], processed_folder, poll_interval_s, idle, keep_raw, allowed_senders[], require_signature, allow_ops[], scope_subject_prefix[]}(键名 = §7.1 [mail.inbound])
--   outbound_json:{enabled, host, port, ssl, user, secret_ref, from, recipients[], cc[], send_rate_per_min, compat_title, receipt_to_sender}(键名 = §7.1 [mail.outbound])
--   outbound_template_id / inbound_template_id:引用 mail_templates(E-4;模板存表,基线 v1.2)
-- mail_templates:邮件主题/正文模板(E-4;基线 v1.2「模板存表、不存 settings JSON」)。06 §2.14 是字段语义唯一出处
CREATE TABLE mail_templates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL UNIQUE,          -- 人可读名,如 'ibquote 兼容-出站'
  kind            TEXT NOT NULL CHECK (kind IN ('inbound','outbound')),
  subject_pattern TEXT NOT NULL,                 -- 带 {占位符} 的主题模板(06 §2.14:28 通用 + 3 回执专用)
  body_fields_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(body_fields_json)),
                                                 -- 有序数组 [{key,label,order,required,empty:'omit'|'dash'}]
  compat_profile  TEXT NOT NULL DEFAULT 'qtrade-v1'
                  CHECK (compat_profile IN ('ibquote-163-v1','collector-v1','qtrade-v1')),
                                                 -- 🔴 R6-58 (h):R-13 已把 `custom` 改名 `qtrade-v1`,本 CHECK 此前没跟上 ⇒ 写 qtrade-v1 直接 INSERT 失败、整张表不可用。
                                                 --   两处枚举(本 CHECK 与 §3.4.6 #103 入参)同步;`custom` 这个取值**作废**,API 层收到 `custom` 回 400 `profile_custom_removed`。
  version         INTEGER NOT NULL DEFAULT 1,    -- 渲染时写进 mail_outbox.template_version;PUT 必须递增
  builtin         INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0,1)),  -- 随包三份默认模板 builtin=1,不可删只能另存
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL
) STRICT;
CREATE INDEX ix_mail_templates_kind ON mail_templates (kind, builtin);
-- 初始三行(builtin=1,06 §2.14 给正文):'ibquote 兼容-出站'(ibquote-163-v1)、'collector 兼容-出站'(collector-v1)、'QTrade 原生-出站'(qtrade-v1,R6-58 (h))+ 对应入站模板

CREATE TABLE mail_routes (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  channel         TEXT CHECK (channel IS NULL OR channel IN ('qidian','qq','wechat')),   -- NULL = 全局
  account_id      TEXT,                          -- NULL = 通道级;非空 = 账号级(字符串,不做 FK:账号软删后路由仍可查看/清理)
  inbound_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(inbound_json)),
  outbound_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(outbound_json)),
  outbound_template_id INTEGER REFERENCES mail_templates(id) ON DELETE RESTRICT,  -- 出站用哪份模板(E-4;NULL = 用 builtin 默认)
  inbound_template_id  INTEGER REFERENCES mail_templates(id) ON DELETE RESTRICT,  -- 入站解析用哪份(NULL = 按邮件自带版本/profile 自动选)
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  CHECK (account_id IS NULL OR channel IS NOT NULL),                       -- 账号级必须带通道
  CHECK (account_id IS NULL
         OR (channel='qidian' AND account_id GLOB 'qd[0-9][0-9]')
         OR (channel='qq'     AND account_id GLOB 'qq[0-9][0-9]')
         OR (channel='wechat' AND account_id GLOB 'wx[0-9][0-9]'))
) STRICT;
CREATE UNIQUE INDEX ux_mail_routes_key ON mail_routes (COALESCE(channel,''), COALESCE(account_id,''));
CREATE INDEX ix_mail_routes_enabled ON mail_routes (enabled, channel, account_id);
-- 初始行:安装器/首次 PUT /settings/mail 建全局路由 (NULL, NULL);没有任何 enabled=1 的路由 ⇔ 邮件功能关闭

-- mail_inbox:收到的每封邮件一行;status 是结果态(**结果态全集单一来源 = 06 §2.3.5**,本 CHECK 是其镜像、必须逐值对齐),reason 是机器码+人话
CREATE TABLE mail_inbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  route_id        INTEGER REFERENCES mail_routes(id) ON DELETE SET NULL,   -- 经哪条路由收到(E-5);路由删除后置 NULL、邮件行保留
  mailbox         TEXT NOT NULL,                 -- 邮箱账户(user@host)
  protocol        TEXT NOT NULL CHECK (protocol IN ('imap','pop3')),   -- 🔴 R6-58 (i):本列 = **路由配置的**协议(`mail_routes.inbound_json.protocol`);E-1 回落期间实际用哪个看下面的 effective_protocol
  effective_protocol TEXT CHECK (effective_protocol IS NULL OR effective_protocol IN ('imap','pop3')),
                                                 -- 🔴 R6-58 (i) 新增(06 §3.1 v0.3 E-1):取信时**实际**用的协议。回落期间 = 'pop3' 而 protocol 仍是 'imap';
                                                 --   两列不等 ⟺ 正处于 E-1 回落(§2.2.9)。**不要把这两列当同一件事**——同设置两个键名会让一处静默失效
  folder          TEXT NOT NULL DEFAULT '',      -- IMAP 文件夹;POP3 空串
  uidvalidity     INTEGER, uid INTEGER,          -- IMAP 键
  uidl            TEXT,                          -- POP3 键
  rfc_message_id  TEXT NOT NULL,                 -- Message-ID 头;缺失时 'sha256-<body_sha256>'
  from_addr       TEXT NOT NULL,                 -- 小写纯地址
  to_addrs        TEXT NOT NULL DEFAULT '',
  subject         TEXT NOT NULL,                 -- ≤512
  date_ms         INTEGER,                       -- Date 头
  received_ms     INTEGER NOT NULL,              -- 取信时刻
  size_bytes      INTEGER,                       -- RFC822.SIZE / RETR 字节数(容量估算用)
  body_sha256     TEXT NOT NULL,                 -- 规范化正文 + 附件哈希(06 §2.5 第 3 层)
  body_text       TEXT,                          -- 解码后正文(范围内邮件才存;OUT_OF_SCOPE 空)
  template        TEXT NOT NULL DEFAULT 'none' CHECK (template IN ('command','message','receipt','none')),
  template_version TEXT,
  status          TEXT NOT NULL DEFAULT 'RECEIVED' CHECK (status IN
                    ('RECEIVED','ACCEPTED','DONE','RECEIPT_SENT','RECEIPT_SKIPPED',
                     'UNSUPPORTED','OUT_OF_SCOPE','PARSE_FAILED','SENDER_DENIED','SIG_INVALID','EXPIRED',
                     'DUPLICATE','DUPLICATE_NONCE','OP_DENIED','TARGET_NOT_FOUND','INGEST_ERROR',
                     'OVERSIZE','CONFIRM_REQUIRED','CONFIRM_EXPIRED','ROUTE_MISMATCH')),   -- ⚠️ 单一来源 = 06 §2.3.5;这四个曾漏收(OVERSIZE=R4-18;CONFIRM_REQUIRED/CONFIRM_EXPIRED=v0.4 R-03;ROUTE_MISMATCH=v0.3 E-5),06 一旦写这些 status,旧 CHECK 会直接 INSERT 失败——新增终态码必须两处同步
  reason          TEXT,                          -- 机器码+人话
  req_id          TEXT, account_id TEXT, op TEXT, -- 解析结果(列表筛选)
  idempotency_key TEXT,                          -- 🔴 R6-58 (i) 新增(06 §3.1,C-10):Transport 改写后的幂等键 `mail:{短名}:{req_id}`,关联 `idempotency` 表。
                                                 --   没有这一列时只能用 (req_id, from_addr) 反查定位,重投/改名场景下会定位错行
  nonce           TEXT,                          -- ⚠️ R6-7:这是**指令邮件的防重放 nonce**(HMAC 信封里的,v1 在用),唯一 (from_addr, nonce);随 nonce_ttl_h 清。
                                                 --   与「危险指令二次确认」的 confirm_nonce **不是一回事**:v1 的确认只有 console 一条路、approve/reject 按 {id} 操作、
                                                 --   **不核验任何 nonce**,故本表 **v1 无 confirm_nonce 列、也无 confirm_via 列**(R4-12 判 oob M6+ 不实现)。
                                                 --   🔴 口径钉死:是「**v1 无此列**」,**不是**「有列但恒 NULL」——两种说法不能同时出现,实现方按后者会去建一列空列并写读它。
                                                 --   M6+ 真要做 mail_out_of_band 时再加 confirm_nonce 列(那时才谈它的取值),v1 连列都不建。
  args_digest     TEXT,                          -- 🔴 R6-22:**待确认指令的参数指纹** = hex(sha256(规范化 args JSON)) 的**前 16 位**(小写 hex)。
                                                 --   与 confirm_expires_ms **同一事务**写(落 status='CONFIRM_REQUIRED' 那一刻);非待确认邮件恒 NULL。
                                                 --   「规范化 args JSON」= 与 commands.args_hash 同一套规范化(键序升序、无多余空白、UTF-8),两处算法必须同一份实现。
                                                 --   用途二:①#68b 出参 args_digest 的**唯一落盘来源**(控制台给人看「批的是哪组参数」);
                                                 --     ②#68c approve 执行前**复算比对**的基准——**防库被改**(有人绕过端点改了 mail_inbox 行的参数再让人批)。
  confirm_expires_ms INTEGER,                    -- 🔴 R6-7 补裁:**危险指令待确认的过期时刻**(epoch ms)。落 status='CONFIRM_REQUIRED' 的**同一事务**里写
                                                 --   = 受理时刻 + agent.toml/06 §7 [mail.inbound] danger_confirm_ttl_s×1000(默认 900 ⇒ 15 分钟)。
                                                 --   **写死的生命周期口径:一经写入就保留、离开 CONFIRM_REQUIRED 后不清 NULL**(approve/reject/过期都不动它)——
                                                 --     留痕用:审计要能回答「这条当时的过期时刻是几点」;清了就查不回来。
                                                 --   **读取方只在 status='CONFIRM_REQUIRED' 时把它解释为「还剩多久」**(#68b 的 expires_at / remaining_ttl_s);
                                                 --     其它 status 下它只是历史值,不得据此再判过期。非待确认邮件恒 NULL(压根没写过)。
                                                 --   没有这一列,CONFIRM_EXPIRED 这个终态无处判、不可编码(#68c 的 409 CONFIRM_EXPIRED 也就无从谈起)。
  sig_ok          INTEGER CHECK (sig_ok IN (0,1)),   -- NULL=未验
  trace_id        TEXT, command_id TEXT,         -- 进总线后的关联(command_id = commands.trace_id)
  first_inbox_id  INTEGER,                       -- DUPLICATE 时指向首封
  attach_cnt      INTEGER NOT NULL DEFAULT 0,
  attach_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attach_json)),  -- [{name,size,mime_sniffed,sha256,media_ref}]
  raw_ref         TEXT,                          -- keep_raw 时原文落盘 mail/raw/…
  archived_path   TEXT, archived_ms INTEGER,     -- mail/archive/<yyyymm>/<id>.eml
  deleted_ms      INTEGER,                       -- 已从服务器删除
  fail_count      INTEGER NOT NULL DEFAULT 0,    -- 同一封连续落库失败次数(06 §5 隔离判据)
  UNIQUE (mailbox, folder, uidvalidity, uid),
  UNIQUE (mailbox, uidl),
  UNIQUE (rfc_message_id),
  CHECK ((protocol='imap' AND uid IS NOT NULL) OR (protocol='pop3' AND uidl IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX ux_inbox_nonce ON mail_inbox (from_addr, nonce) WHERE nonce IS NOT NULL;
CREATE INDEX ix_inbox_status ON mail_inbox (status, received_ms);
CREATE INDEX ix_inbox_cleanup ON mail_inbox (mailbox, folder, received_ms) WHERE deleted_ms IS NULL;
CREATE INDEX ix_inbox_bodyhash ON mail_inbox (body_sha256);
CREATE INDEX ix_inbox_reqid ON mail_inbox (req_id) WHERE req_id IS NOT NULL;
CREATE INDEX ix_inbox_idem ON mail_inbox (idempotency_key) WHERE idempotency_key IS NOT NULL;  -- R6-58 (i)
CREATE INDEX ix_inbox_route ON mail_inbox (route_id, received_ms DESC) WHERE route_id IS NOT NULL;
CREATE INDEX ix_inbox_confirm_pending ON mail_inbox (confirm_expires_ms) WHERE status = 'CONFIRM_REQUIRED';  -- R6-7:过期 reaper 每 60s 的扫描面,只覆盖待确认行
-- 🔴 R6-7 待确认过期 reaper(规范 SQL;归 `scheduler` 注册的 `mail_confirm_reaper`,**每 60 s 一轮**——与 `wechat_slot_reaper`、
--    `jobs_reclaimer` 同一 60 s 节拍,§2.2.11;不是邮件收/发/清理那三条 per-route task,过期判定与邮箱连接无关、邮箱掉线也必须照常过期):
--      UPDATE mail_inbox
--         SET status = 'CONFIRM_EXPIRED',
--             reason = 'CONFIRM_EXPIRED:待确认超过 danger_confirm_ttl_s 未在控制台处理'
--       WHERE status = 'CONFIRM_REQUIRED'
--         AND confirm_expires_ms IS NOT NULL
--         AND confirm_expires_ms <= :now_ms;
--    rowcount>0 则**逐行**记审计 audit_log(kind='system', transport='system', actor='system:scheduler',
--      action='mail.pending_confirm.expired', result_code='CONFIRM_EXPIRED',
--      detail_json={inbox_id, op, from_addr, account_id, expires_at, expired_by:'reaper'})——**不记 confirm_via**(见 audit_log 注)。
--    🔴 R6-29 审计 actor 两条路分明:**本 reaper 触发 = actor 'system:scheduler' + detail_json.expired_by='reaper'**;
--      **#68c/#68d 端点顺手触发 = actor 为发起该请求的控制台操作者 + expired_by='endpoint'**。
--      不分开 = 事后查「这条是自己超时的、还是人点过来才发现的」查不出来。expired_by ∈ {reaper, endpoint}。
--    幂等:条件里的 status='CONFIRM_REQUIRED' 一旦转 CONFIRM_EXPIRED 就不再命中,多实例/重入不会重复过期、不会重复记审计。
--    🔴 R6-20:本 reaper **只是兜底清扫,安全性不依赖其节拍**。#68c/#68d 的过期判据是各自的**原子 claim**
--      (`WHERE id=:id AND status='CONFIRM_REQUIRED' AND confirm_expires_ms > :now_ms`,rowcount==1 才继续,否则 409 CONFIRM_EXPIRED;
--       行仍 CONFIRM_REQUIRED 而时刻已过时端点**顺手**置 CONFIRM_EXPIRED 并记 mail.pending_confirm.expired 审计,不等本 reaper)——
--      端点若只看 status,在「到期了但本 reaper 这 60 s 还没跑到」的窗口里会**放行危险指令**。

-- mail_outbox:待发/已发邮件(信息通知 + 回执 + 告警)
CREATE TABLE mail_outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT NOT NULL CHECK (kind IN ('message','receipt','alert')),
  route_id        INTEGER REFERENCES mail_routes(id) ON DELETE SET NULL,   -- 从哪条路由发出(E-5);回执与指令邮件同路由
  to_addrs        TEXT NOT NULL, cc_addrs TEXT NOT NULL DEFAULT '',
  subject         TEXT NOT NULL,
  body_text       TEXT NOT NULL, body_html TEXT, -- alternative 两份
  attachments_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(attachments_json)),  -- [{name, media_ref|path, size}]
  rfc_message_id  TEXT NOT NULL UNIQUE,          -- 本系统生成
  in_reply_to     TEXT, references_hdr TEXT,     -- 回执关联(references 是 SQL 保留字,列名加 _hdr)
  ref_inbox_id    INTEGER, ref_message_id TEXT, ref_trace_id TEXT,   -- 回执→指令邮件;信息邮件→messages.id;→trace
  template_id      INTEGER REFERENCES mail_templates(id) ON DELETE SET NULL,   -- 用哪份模板渲染(E-4)
  template_version TEXT NOT NULL,                -- = mail_templates.version 渲染那一刻的值(E-4)
  template_profile TEXT NOT NULL DEFAULT 'qtrade-v1', -- 渲染那一刻模板的 compat_profile,冻结(E-4);取值与 mail_templates.compat_profile 同一枚举(R6-58 (h):`custom` 作废)
  render_notes    TEXT,                          -- 🔴 R6-58 (i) 新增(06 §3.1 v0.3 E-4):必填字段为空按 `empty` 处理的记录(06 §2.14.3),供追溯「这封为什么缺了那一栏」
  status          TEXT NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','SENDING','SENT','RETRY','DEAD','DISCARDED')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_ms INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT, smtp_response TEXT,
  dedup_key       TEXT NOT NULL UNIQUE,          -- kind + ref_*;重发回执是新行带后缀 '#2'
  created_ms      INTEGER NOT NULL, sent_ms INTEGER
) STRICT;
CREATE INDEX ix_outbox_mail_due ON mail_outbox (status, next_attempt_ms) WHERE status IN ('QUEUED','RETRY');
CREATE INDEX ix_outbox_mail_route ON mail_outbox (route_id, created_ms DESC) WHERE route_id IS NOT NULL;

-- mail_cleanup_log:每轮清理一行(06 §2.6.8);原 02 的 skipped_unprocessed/skipped_out_of_scope 计数进 detail_json
-- 🔴 R6-1 清理判据(与 06 §2.6.1 eligible() 同一口径,本册凡涉及"删远端邮件"的动作都以此为前置):
--   NEVER_DELETE = {OUT_OF_SCOPE, OVERSIZE};候选 ⟺ mail_inbox.status 是终态 AND **status ∉ NEVER_DELETE** AND 已过 retention_days。
--   OVERSIZE(超大信,只登记元数据未取正文)必须**留在原夹原位**——人要能在邮箱客户端里看到它并在提上限后重新解析;
--   删了 = 人工重新解析变死路。**五处**一律同此前置(06 §2.6;R6-26/R6-31 由四处补为五处):
--     ① eligible();② ②容量循环(trigger='capacity');③ IMAP 终态 MOVE|COPY+\Deleted;④ POP3 cleanup_pop3;
--     ⑤ **max_kept_count 按条数触发的那轮**(trigger='max_kept',06 §2.6.3 ③:邮箱只保留最近 N 封、超出即删远端)——
--       它是第三条删远端的触发路径,漏掉它 = 超大信照样被「按条数」挤掉。
CREATE TABLE mail_cleanup_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  started_ms      INTEGER NOT NULL, finished_ms INTEGER NOT NULL,
  trigger         TEXT NOT NULL CHECK (trigger IN ('retention','capacity','max_kept','manual','archive_rotation')),   -- R6-31:补 'max_kept'(按条数触发,06 §2.6.3 ③)——缺它则该轮清理的 INSERT 必被 CHECK 拒
  protocol        TEXT NOT NULL CHECK (protocol IN ('imap','pop3')),
  folder          TEXT NOT NULL DEFAULT '',
  candidates      INTEGER NOT NULL DEFAULT 0,
  archived        INTEGER NOT NULL DEFAULT 0,
  deleted         INTEGER NOT NULL DEFAULT 0,
  failed          INTEGER NOT NULL DEFAULT 0,
  bytes_freed     INTEGER NOT NULL DEFAULT 0,
  quota_used_before INTEGER, quota_used_after INTEGER, quota_limit INTEGER,
  quota_source    TEXT CHECK (quota_source IS NULL OR quota_source IN ('imap_quota','estimate','pop3_stat','unknown')),
  archive_rotated_files INTEGER NOT NULL DEFAULT 0, archive_rotated_bytes INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL CHECK (status IN ('OK','PARTIAL','FAILED')),
  error           TEXT,
  detail_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))   -- {skipped_unprocessed, skipped_out_of_scope, skipped_oversize(R6-1), dry_run, …}
) STRICT;
CREATE INDEX ix_cleanup_log_ts ON mail_cleanup_log (started_ms DESC);
