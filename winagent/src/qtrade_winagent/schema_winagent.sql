-- winagent.db 全部 DDL —— 逐字抽自 docs/02 §3.2(不得改写;新增表/列先进 02 §3.2 再同步这里)。
-- 抽取方式:docs/02「### 3.2 `winagent.db` 全部 DDL」下第一个 ```sql 代码块的全文。
PRAGMA journal_mode = WAL; PRAGMA auto_vacuum = INCREMENTAL;

CREATE TABLE schema_version (          -- 同 agent.db
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_ms INTEGER NOT NULL, checksum TEXT NOT NULL
) STRICT;

-- install_state:安装状态机的持久化(03 是状态机唯一出处;键拼写=基线 v1.1 §8.2)。单行 JSON 列(C-36,03 §3.3)
--   03 的 install_state.json 与本表双写:json 给"重启续跑"(RunOnce 时 DB 未必可用),表给控制台/WinAgent 查询;以 json 为准、表落后时按 json 回填
CREATE TABLE install_state (
  key             TEXT PRIMARY KEY CHECK (key = 'current'),   -- 固定单行
  state           TEXT NOT NULL,                 -- PRECHECK … DONE / FAILED:<步>:<原因码>(含 PAYLOAD_STAGED)
  substate        TEXT,                          -- 步内子阶段(03 定义)
  parked_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(parked_json)),     -- 03 的 parked(用户暂缓的动作,如 shutdown 时机)
  package_version TEXT NOT NULL,                 -- 安装器版本
  env_json        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(env_json)),        -- 环境矩阵快照:wsl_state/kernel_state/net_state/policy_reason + 04 的 wslconfig_path/vhdx_path/other_distros
  wslconfig_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(wslconfig_json)),  -- 写入前后的 .wslconfig 键值与备份路径
  distro_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(distro_json)),     -- 发行版导入:名字/版本/vhdx/同名冲突处理
  clients_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(clients_json)),    -- CLIENTS_CHECKED 结果(微信 match/action、企点/QQ)
  resume_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(resume_json)),     -- RunOnce 续跑标记、重试次数
  updated_ms      INTEGER NOT NULL
) STRICT;

-- install_history:每次状态迁移一行(C-36,03 §3.3);安装/升级/修复/卸载全记
CREATE TABLE install_history (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  at_ms           INTEGER NOT NULL,
  from_state      TEXT,
  to_state        TEXT NOT NULL,                 -- 🔴 R6-58 (au):**无 CHECK 是有意的** —— 取值 = 00 §8.2 的安装状态机键,**外加内核流程的七个非安装态迁移名**(登记在此,
                                                 --   实现方不得另起同义名):KERNEL_APPLYING / KERNEL_APPLIED / KERNEL_APPLY_FAILED /
                                                 --   KERNEL_VERIFIED / KERNEL_VERIFY_FAILED / KERNEL_ROLLING_BACK / KERNEL_ROLLED_BACK(#26/#27/#46 写)。
  package_version TEXT NOT NULL,
  actor           TEXT NOT NULL DEFAULT 'installer' CHECK (actor IN ('installer','winagent','console','user')),
  note            TEXT,
  detail_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))
) STRICT;
CREATE INDEX ix_install_history_ts ON install_history (at_ms DESC);

-- health_samples:与 agent.db 同结构(04 §3.1;C-31),**只落 Windows 侧样本**(整机 CPU/内存/磁盘、vmmem、微信/chatlog RSS);
--   R-13:不与 agent.db 同步,P-RES 两组数由 Agent 查询期合并;三级降采样与保留见 §2.8.4
CREATE TABLE health_samples (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms           INTEGER NOT NULL,
  resolution      TEXT NOT NULL CHECK (resolution IN ('raw','1m','1h')),
  scope           TEXT NOT NULL CHECK (scope IN ('host','wsl','container','process','disk','net')),
  subject         TEXT NOT NULL,
  cpu_pct         REAL, mem_mb REAL, mem_anon_mb REAL, mem_max_mb REAL,
  disk_free_mb    REAL, disk_total_mb REAL,
  net_rx_kbps     REAL, net_tx_kbps REAL,
  extra_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra_json)),
  agg_max_json    TEXT CHECK (agg_max_json IS NULL OR json_valid(agg_max_json))
) STRICT;
CREATE INDEX ix_health_lookup ON health_samples (scope, subject, resolution, ts_ms);
CREATE INDEX ix_health_ts ON health_samples (resolution, ts_ms);

-- probe_results:连通性探测(04 §3.2;C-31)。只在 winagent.db 一份:03 在 Agent 不存在的阶段也要读;
--   WSL/容器侧结果由 Agent 探后 PUT /wa/v1/probes 回写。03 的 phase 映射为 trigger='install' + detail 前缀 'selftest:';
--   ⚠️ PRECHECK 阶段本表尚不存在(WinAgent 未装),安装器基础探测结果只进 install_state.env_json.precheck_probes(99c)
CREATE TABLE probe_results (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL,                 -- 一轮探测一个 ULID
  ts_ms           INTEGER NOT NULL,
  trigger         TEXT NOT NULL CHECK (trigger IN ('install','boot','periodic','net_event','resume','manual','wizard')),
  target          TEXT NOT NULL CHECK (target IN ('apk_url','mail_pop3','mail_imap','mail_smtp','qidian_msf','qq_servers','wechat_servers','docker_registry','winagent_from_wsl','agent_from_windows')),
  side            TEXT NOT NULL CHECK (side IN ('windows','wsl','container')),
  host            TEXT NOT NULL, port INTEGER,   -- 实际探的地址(一目标多端口一端口一行)
  level_reached   TEXT NOT NULL DEFAULT 'none' CHECK (level_reached IN ('none','dns','tcp','tls','http','proto')),
  result          TEXT NOT NULL CHECK (result IN ('OK','DNS_FAIL','TCP_TIMEOUT','TCP_REFUSED','TLS_FAIL','HTTP_4XX','HTTP_5XX','PROXY_REQUIRED','BLOCKED_BY_POLICY','SKIPPED')),  -- +SKIPPED(C-18)
  latency_ms      INTEGER,                       -- 到达最深一级的耗时
  net_state       TEXT CHECK (net_state IS NULL OR net_state IN ('DIRECT','SYSTEM_PROXY','VPN_ACTIVE','VPN_ACTIVE_WITH_PROXY','OFFLINE')),
  detail          TEXT CHECK (detail IS NULL OR length(detail) <= 200)   -- 错误摘要;不含 URL 凭据、不含代理密码
) STRICT;
CREATE INDEX ix_probe_run ON probe_results (run_id);
CREATE INDEX ix_probe_target_ts ON probe_results (target, ts_ms DESC);

-- wechat_profiles:本机见过的微信登录实体,per-wxid(05 §3.1;C-38);与 agent.db accounts(channel=wechat) 一一对应
CREATE TABLE wechat_profiles (
  wxid            TEXT PRIMARY KEY,
  account_id      TEXT NOT NULL UNIQUE CHECK (account_id GLOB 'wx[0-9][0-9]'),   -- Agent 侧 wxNN(字符串,无 FK;C-01)
  nickname        TEXT,
  alias           TEXT,                          -- 微信号
  avatar_ref      TEXT,                          -- Agent media sha256(经 Agent 拉过去后回填)或本地缓存路径
  data_dir        TEXT,                          -- xwechat_files\<wxid>
  first_login_ms  INTEGER, last_login_ms INTEGER,
  login_count     INTEGER NOT NULL DEFAULT 0,
  last_wechat_version TEXT,
  last_wxkey_dll  TEXT,                          -- 最近一次试钥成功的 DLL
  main_wnd_class  TEXT,                          -- 🔴 R6-58 (at) 新增:微信主窗口类名(4.x 为 'Qt51514QWindowIcon',3.x 为 'WeChatMainWndForPC')。
                                                 --   05 §7 把它当落点引用,而本表此前没有这一列 ⇒ 实现方只能改读 winagent.toml [wechat] main_wnd_class。
                                                 --   口径:**winagent.toml 的值是默认/兜底,本列是该 wxid 实测到的值**;NULL = 没测过,按配置值走。
  ritual_done_ms  INTEGER,                       -- 讲述人仪式完成时刻(B.7);NULL=未做
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  deleted_ms      INTEGER
) STRICT;

-- wechat_install:本机微信"安装"级信息(与 wxid 无关),单行(C-38,03 §3.3)
CREATE TABLE wechat_install (
  key             TEXT PRIMARY KEY CHECK (key = 'current'),
  path            TEXT,                          -- Weixin.exe 路径(MULTIPLE_INSTALLS 时用户选定的那处)
  version         TEXT,                          -- 四段版本(三来源一致后的值;来源见 exe_version/version_subdir)
  exe_version     TEXT,                          -- ①<安装目录>\Weixin.exe 的 FileVersion(权威;**不读注册表 DisplayVersion**,实测陈旧)
  version_subdir  TEXT,                          -- ②安装目录下的版本子目录名(4.x 按版本分子目录,取最大)
  data_root_ini   TEXT,                          -- ③数据根来源:%APPDATA%\Tencent\xwechat\config\<32hex>.ini(取 LastWrite 最新的那个)
  data_root       TEXT,                          -- 该 ini 第一行(本机实测 D:\Program Files\Tencent\Saved Files)
  data_dir        TEXT,                          -- = <data_root>\xwechat_files(实测 22.36 GB / 146,954 文件)
  appdata_dir     TEXT,                          -- %APPDATA%\Tencent\xwechat(实测 2.56 GB:log/XPlugin/update/config/login)
  backup_dir      TEXT,                          -- 重装前备份目录(copy|rename|skip 三模式,03 §2.9.3)
  reinstalled_ms  INTEGER,
  auto_update_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(auto_update_json)),   -- {disabled_by_us, update_pkg_path, last_check_ms}
  selected_ms     INTEGER,                       -- 用户选定安装位置的时刻
  updated_ms      INTEGER NOT NULL
) STRICT;

-- wechat_version_matrix:微信版本 ↔ wx_key DLL 的事实表(03 §2.9.2/§3.3;C-37)。只记事实,match/action 运行时按「与随包版本逐段比较 + 是否 verified」算
CREATE TABLE wechat_version_matrix (
  version         TEXT PRIMARY KEY,              -- 精确四段版本 '4.1.12.26'
  dll             TEXT,                          -- 'wx_key2.dll';status=unknown 时可 NULL
  status          TEXT NOT NULL CHECK (status IN ('verified','failed','unknown')),
  source          TEXT NOT NULL CHECK (source IN ('bundled','runtime','manual')),   -- 随包 / 运行期试钥回写 / 人工
  verified_ms     INTEGER,
  note            TEXT
) STRICT;
-- 初始行:('4.1.12.26','wx_key2.dll','verified','bundled',…) 随包实测组合;
--         ('4.1.11.52','wx_key1.dll','failed','bundled',…) 已知失败组合;
--         ('4.1.13.12',NULL,'unknown','bundled','开发机现役版本,wx_key 未测')  ← 验证报告 2026-09-18

-- probe_targets_observed:C-1「实测采样」的观测结果(04 §2.8.4;POST /system/probe {mode:'sample'} 写入,PUT /settings/probe 确认后才成为正式探测目标)
CREATE TABLE probe_targets_observed (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id      TEXT,                          -- 哪个账号的连接(NULL = 宿主机进程,如微信 PC)
  channel         TEXT CHECK (channel IS NULL OR channel IN ('qidian','qq','wechat')),
  remote_host     TEXT,                          -- 反查到的域名(取不到则 NULL)
  remote_ip       TEXT NOT NULL,
  remote_port     INTEGER NOT NULL,
  proto           TEXT NOT NULL DEFAULT 'tcp' CHECK (proto IN ('tcp','udp')),
  side            TEXT NOT NULL CHECK (side IN ('container','wsl','windows')),   -- 从哪一侧采到
  first_seen_ms   INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL,
  hits            INTEGER NOT NULL DEFAULT 1,    -- 多轮采样命中次数,越高越可能是常连服务器
  adopted_ms      INTEGER                        -- 非空 = 已被 PUT /settings/probe 采纳为正式探测目标
) STRICT;
CREATE UNIQUE INDEX ux_probe_obs ON probe_targets_observed (COALESCE(account_id,''), remote_ip, remote_port, proto);
CREATE INDEX ix_probe_obs_seen ON probe_targets_observed (last_seen_ms DESC);

-- vault_index:只存条目名与元数据;密文在 DPAPI blob 文件 vault\blobs\<sha256(name)>.bin(C-06/C-09)
CREATE TABLE vault_index (
  name            TEXT PRIMARY KEY,              -- 'account/qd01' | 'mail/pop3' | 'mail/hmac/cmd/ops' | 'mail/hmac/confirm' | 'api/<app_id>' | 'winagent/agent_token' | 'winagent/console_token'
                                                 -- ⚠️ R6-10:邮件 HMAC 钥路径全套统一为 `mail/hmac/cmd/<短名>`(指令邮件验签,**每发件人一把**,#67 写入)
                                                 --   + `mail/hmac/confirm`(服务端单钥,永不下发,§11.17 ③ 双钥双通道);**`cmd/` 这一段不得省**(少了它就会被实现成单钥)
                                                 -- 🔴 R6-25:**`mail/hmac/confirm` 不经任何端点读回/显示/导出** —— **v1 只生成、只保管、无消费者**;M6+ 做 oob 时才用于确认签名,
                                                 --   没有任何消费者需要看到明文:#67 只发指令验签钥(cmd/<短名>)且只一次性回显那一把;
                                                 --   #80 诊断包、#89 PUT /settings、#51 导出、审计与日志一律不含它(§2.9 脱敏表同款,Vault 一切 *_ref 只写不读)。
                                                 --   一旦它能被读回,邮箱失陷方就能自己造确认签名,§11.17 ③ 的双钥双通道整条作废。
  scope           TEXT NOT NULL CHECK (scope IN ('account','mail','api','webhook','winagent','asr','other')),   -- R6-68 ⑨:#89 设置类密钥(settings/<group>/…)用 'other'
  blob_path       TEXT NOT NULL,                 -- 'blobs\<sha256(name)>.bin'(相对 [vault] dir)
  blob_sha256     TEXT NOT NULL,                 -- 密文文件摘要,只用于校验未被替换
  version         INTEGER NOT NULL DEFAULT 1,    -- PUT 覆盖 +1
  owner           TEXT NOT NULL,                 -- 写入方 actor(令牌 id)
  created_ms      INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
  last_read_ms    INTEGER, read_count INTEGER NOT NULL DEFAULT 0,
  suspect         INTEGER NOT NULL DEFAULT 0 CHECK (suspect IN (0,1)),   -- POST …/flag:登录报密码错
  deleted_ms      INTEGER                        -- DELETE 后元数据保留一行(blob 先 0 填充再删)
) STRICT;

-- settings(R2-5 补 DDL:此前只有键注释、无建表语句,导致 04 的 hosts/powercfg/探测/crash dump 真值无处落盘):
--   WinAgent 侧设置真值。⚠️ 与 agent.db.settings 是**两张独立表**,各存各侧消费的设置、**互不同步**——同一项只有一个家
--   (基线 §11.10 [CFGSRC+RETENTION]);Agent 需要这里的值一律经 /wa/v1 拿快照,不反向写本表。key 前缀约定(04 v0.3 N5~N10 / B-2):
--     power.backup_json     改 powercfg 前的**用户原值**(keep_awake_mode=powercfg 时写,关模块/卸载时还原);power.keepawake_current 当前生效值
--     wechat.hosts_block    hosts 屏蔽:{enabled, domains:[两个下载域名], marker_lines:[写入的标记行], applied_at, last_result}(B-2)
--     wechat.enabled_snapshot 微信模块总开关的本机快照(真值在 winagent.toml [wechat] enabled,这里只留最近读到的值供降级展示)
--     probe.targets         正式探测目标(PUT /settings/probe 采纳后写)。🔴 R6-58 (dd):值形状逐字 = ["host:port", …]
--                           —— 与 #76b/#48 回的 targets、04 §2.8.4 的 [probe] *_hosts 元素**同一种形状**,
--                           同一事实不许两种形状(否则采纳一次就要在两处之间翻译一次,翻译必出错)。
--                           probe.interval_s 探测周期
--     crashdump.backup_json  crash dump 两键(DumpCount/DumpType 或 core_pattern 语义相关)的**用户原值**,卸载/关模块时还原
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value_json  TEXT    NOT NULL CHECK (json_valid(value_json)),
  updated_ms  INTEGER NOT NULL,
  updated_by  TEXT    NOT NULL DEFAULT ''       -- actor 串('installer' | 'svc' | 'user' | 'console')
) STRICT;
-- wa_audit_log:WinAgent 自己的审计(谁在何时读了哪个密钥、动了 .wslconfig、管道调度了什么);保留 [retention] wa_audit_days(G-13)
CREATE TABLE wa_audit_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms           INTEGER NOT NULL,
  actor           TEXT NOT NULL,                 -- 'agent' | 'console' | 'installer' | 'system' | 'svc→user'(管道调度)
  ip              TEXT,
  action          TEXT NOT NULL,                 -- 'vault.read' | 'vault.write' | 'vault.delete' | 'vault.flag' | 'wsl.stop' | 'wslconfig.write' | 'wechat.login_start' | 'alert' …
  target          TEXT,                          -- 条目名 / 发行版名 / 告警 code:subject
  result          TEXT NOT NULL,                 -- 'OK' | 错误码
  trace_id        TEXT,
  detail_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json))   -- 不含值;.wslconfig 改动记前后 diff 与备份路径;告警缓冲记 payload
) STRICT;
CREATE INDEX ix_wa_audit_ts ON wa_audit_log (ts_ms DESC);
CREATE INDEX ix_wa_audit_action ON wa_audit_log (action, ts_ms DESC);
