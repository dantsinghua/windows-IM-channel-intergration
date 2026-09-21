/** Agent `/api/v1` 的数据模型(00 §7,02 §3.4 出参口径) */

import type { AccountState, Channel, ProbeResultMeta, ResultCode, Severity } from '@/i18n/zh-CN/codes'
// 自检行的探测结论要按 01 §2.7.9 的色/中文渲染,文案与芯片色只此一份来源(01 §2.9 约定 6)
import { PROBE_RESULTS } from '@/i18n/zh-CN/codes'

/* ── 统一信封(00 §10 / 02 §3.4) ── */

export interface ApiError {
  message: string
  /** 机器可读子原因(00 §7.3;可空) */
  reason?: string | null
  retryable: boolean
  needs_human: boolean
  /** 409 RESOURCE_EXHAUSTED 时 Agent 给的替代方案原文 */
  alternatives?: unknown[]
  /** 表单标红用(JSON Pointer) */
  details?: { pointer: string; message: string }[]
  hint_actions?: string[]
}

export interface Envelope<T> {
  ok: boolean
  code?: string
  data?: T
  error?: ApiError
  trace_id?: string
  next_cursor?: string | null
  [k: string]: unknown
}

/* ── Account(00 §7.1;02「Account 序列化」R6-4) ── */

export interface AccountRuntime {
  kind?: 'redroid' | 'napcat' | 'wechat_pc'
  container?: string
  adb_port?: number
  stream_port?: number
  ws_port?: number
  http_port?: number
  wechat_version?: string
  wxkey_dll?: string
  [k: string]: unknown
}

export interface Account {
  id: string
  channel: Channel
  host: 'wsl' | 'windows'
  label: string
  state: AccountState
  /** 库列为 NULL 时信封给空串(R6-53) */
  state_code: string
  state_reason: string
  /**
   * R6-4:INTEGER 毫秒,**仅 state=error 时非空**,离开 error 即 null。
   * 「已故障 N 分钟」与强制释放按钮灰态的唯一判据;控制台不读 account_runtime 库列。
   */
  error_since_ms: number | null
  enabled: boolean
  auto_recover: boolean
  deleted_ms: number | null
  runtime: AccountRuntime
  identity?: Record<string, unknown>
  login?: { mode: 'password' | 'qrcode' | 'manual'; credential_ref?: string | null; remember: boolean }
  capabilities: string[]
  quota_mb: number
  self_nick?: string
  self_uid?: string
  /** 微信专有(R-23);非微信恒 null */
  wxid?: string | null
  merged_into?: string | null
  created_at?: string
  updated_at?: string
  last_seen_at?: string | null
  /* 🔴 总控裁决④:**不带 `settings` 子对象** —— 账号级设置只经 #22 `PATCH /accounts/{id}/settings` 写,
     `auto_recover`/`quota_mb` 等已在本对象顶层;详情页不要指望这里有一份完整设置树。 */
}

/* ── 等人提示(#15 GET /accounts/{id}/prompt) ── */

export interface Prompt {
  kind: string | null
  text?: string
  /** 只渲染到 <img>,不落盘、不进环形缓冲、不进日志(§6-10) */
  qrcode_png_b64?: string
  expires_at?: string
  countdown_s?: number
}

/* ── 资源池(00 §7.6) ── */

export interface WechatSlots {
  used: number
  max: number
  /** 在线的 wxNN */
  holder: string
  /** 正在切给谁;**无 pending 时为空串**,判据用非空串、不用 != null(R-04/§11.18) */
  pending: string
  pending_expires_at: string | null
  /** R6-6:该 pending 那一次登录尝试的 ls_+ULID;无 pending 时 "" */
  pending_login_session_id: string
}

export interface ResourcePool {
  pools: {
    wsl: { total_mb: number; reserved_mb: number; used_mb: number; free_mb: number }
    windows: {
      total_mb: number
      reserved_mb: number
      wechat_mb: number
      wechat_slots: WechatSlots
      status?: string
      wechat_enabled?: boolean
    }
  }
  /** 采不到时后端给 `null`,不编造(#77 同规则) */
  realtime: { wsl_anon_mb: number | null; win_available_mb: number | null }
  quota_mb: Record<Channel, number>
  can_add: Record<Channel, number>
  accounts?: { id: string; anon_mb: number; current_mb: number; cpu_pct: number }[]
  accounts_by_channel?: Record<string, number>
  metrics_snapshot?: MetricsSnapshot
}

/* ── 监控快照(02 #77,E-19) ── */

export type WatermarkLevel = 'normal' | 'warn' | 'high' | 'critical'

export interface MetricsSnapshot {
  /**
   * 整机组(02 #77 与 `ours` 两组并排)。
   * 🔴 **可选**:后端本期还没下发这一组(S-07,后端侧会补)。页面一律走可选链 + 「—」占位,
   * 绝不写 `m.hardware.mem.total_mb` —— 那是一个 TypeError,整页白屏。
   */
  hardware?: {
    mem?: { total_mb: number; used_mb: number; avail_mb: number; vmmem_mb: number }
    cpu?: { logical_cores: number; load_pct: number }
    disks?: { mount: string; total_mb: number; free_mb: number }[]
  }
  ours: {
    procs: { agent_mb: number | null; winagent_mb: number | null; console_mb: number | null }
    /** R6-58 (aa):每进程明细(采样缺失时值为 `null`,不编造) */
    procs_detail?: { name: string; rss_mb: number | null; cpu_pct: number | null }[]
    accounts: {
      id: string
      rss_mb?: number | null
      anon_mb?: number | null
      current_mb?: number | null
      cpu_pct: number | null
      quota_mb: number
    }[]
    wechat?: { chatlog_mb: number | null; wechat_pc_mb: number | null }
    storage?: {
      db_mb?: number; media_mb?: number; mail_mb?: number
      accounts_mb?: number; backup_mb?: number; vhdx_mb?: number
    }
  }
  budget_vs_actual: { id: string; quota_mb: number; rss_mb: number | null; drift_pct: number | null }[]
  disk_watermark: {
    level: WatermarkLevel
    free_mb: number
    actions: string[]
    retention_shrunk_to?: number | null
    /** R6-30:扁平两键,不嵌套 */
    last_cleanup_at: string | null
    last_cleanup_freed_mb: number | null
    /** 后端本期未下发(S-07) */
    vhdx_grown_mb?: number | null
  }
  mem_watermark: {
    level: WatermarkLevel
    avail_mb: number
    warn_mb?: number
    critical_mb?: number
    lru_suggest: { id: string; last_seen_at: string | null; rss_mb: number; auto_stop_on_pressure?: boolean }[]
  }
}

/* ── 指令与结果(00 §7.2/§7.3) ── */

export interface CommandResult {
  ok: boolean
  code: ResultCode | string
  data?: Record<string, unknown> & {
    shots?: { before?: string; after?: string }
    needs_review?: boolean
    message_id?: string
    confirmed_by?: string
    confirm_ms?: number
  }
  cost_ms: number
  trace_id: string
  source?: string
  state_before?: string
  state_after?: string
  error?: ApiError
}

/**
 * #28/#29 的 `202` 受理体(R6-53):同步等待超 `[api] http_sync_max_wait_ms` 或 `async:true`。
 * 结果随后走 `command_done` 事件与 #31。
 */
export interface CommandAccepted {
  ok: true
  accepted: true
  pending?: boolean
  trace_id: string
}

/** #28/#29 的两种正常回包 */
export type CommandOutcome = CommandResult | CommandAccepted

export function isCommandAccepted(o: CommandOutcome): o is CommandAccepted {
  return (o as CommandAccepted).accepted === true && (o as CommandResult).code === undefined
}

export interface CapabilityDef {
  op: string
  kind: 'read' | 'write' | 'admin'
  channels: Record<Channel, 'supported' | 'unsupported' | 'not_applicable'>
  args_schema: JsonSchema
  result_schema?: JsonSchema
  danger: boolean
  confirmable: boolean
}

export interface JsonSchema {
  type?: string
  title?: string
  description?: string
  properties?: Record<string, JsonSchema>
  required?: string[]
  enum?: (string | number)[]
  default?: unknown
  items?: JsonSchema
  minimum?: number
  maximum?: number
}

/* ── 消息(00 §7.4) ── */

export interface MessageMedia {
  ref: string
  sha256?: string
  mime?: string
  size?: number
  kind: 'image' | 'voice' | 'file' | 'video'
  state: 'pending' | 'ready' | 'skipped_oversize' | 'failed'
}

export interface Message {
  id: string
  ext_msg_id?: string
  account_id: string
  channel: Channel
  session: { id: string; name: string; kind: 'group' | 'private' }
  dir: 'in' | 'out'
  type: 'text' | 'image' | 'voice' | 'file' | 'video' | 'system' | 'unknown'
  state: 'SENDING' | 'DELIVERED' | 'UNCONFIRMED' | 'FAILED'
  text: string | null
  text_len: number
  fingerprint?: string
  media: MessageMedia[]
  sender: { id: string; name: string | null }
  self: boolean
  /** 出向追溯(02 #48) */
  confirmed_by?: string | null
  trace_id?: string | null
  ts: string
  received_at: string
  source: string
  revoked: boolean
  raw_ref?: string | null
  needs_review?: boolean
  /**
   * 事件专属、**不落库**的三字段(00 §7.4,R6-49)。
   * 只在 WS `message` 事件前插的行上存在;`GET /messages` 不带 —— 重拉后消失是预期行为。
   */
  lag_s?: number
  late?: boolean
  origin?: 'rpa' | 'external'
  /**
   * #53 语音转文字的回写(backend-api-2 §1)。
   * `text_source='asr'` 时 `text` 才是转写出来的;否则转写只落在 `asr_text` 里,不覆盖原文。
   */
  asr_text?: string | null
  asr_state?: 'pending' | 'done' | 'failed' | null
  text_source?: string | null
}

export interface SessionRow {
  id: string
  account_id: string
  channel: Channel
  name: string
  kind: 'group' | 'private'
  /** 🔴 总控裁决②:会话最后消息时间字段名 = `last_msg_at`(ISO 8601 带时区偏移,00 §7) */
  last_msg_at?: string | null
  /**
   * @deprecated 一次性兼容:后端若仍下发 `last_ts` 则映射到 `last_msg_at`。
   * **后端改完即删本键与 `normalizeSession()` 里的那一行。**
   */
  last_ts?: string | null
  unread?: number
  native_id?: string | null
  msg_count?: number | null
  member_count?: number | null
  muted?: boolean
  capture_text?: boolean | null
  retention_days?: number | null
}

/** 裁决②的一次性兼容映射:`last_ts` → `last_msg_at`(后端改完删除) */
export function normalizeSession(row: SessionRow): SessionRow {
  if (row.last_msg_at === undefined && row.last_ts !== undefined) {
    return { ...row, last_msg_at: row.last_ts }
  }
  return row
}

/* ── 异步作业(§11.21 [JOB]) ── */

export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'expired'

export interface Job {
  job_id: string
  kind: string
  state: JobState
  progress: number
  result?: Record<string, unknown> & { freed_mb?: number; download_url?: string }
  error?: { code: string; message: string }
  /** 02 #107 定死 `*_at`(ISO 8601 带时区偏移,00 §6「API/事件时间一律 ISO」) */
  created_at?: string
  updated_at?: string
  expires_at?: string
  /**
   * @deprecated 一次性兼容:后端当前下发 epoch 毫秒 `*_ms`(S-05,后端侧会改成 `*_at`)。
   * **后端改完即删这三键与 `normalizeJob()`。**
   */
  created_ms?: number
  updated_ms?: number
  expires_ms?: number
  actor?: string | null
  attempt_count?: number
  params?: Record<string, unknown>
  account_id?: string | null
}

function msToIso(ms: number | undefined): string | undefined {
  return typeof ms === 'number' && Number.isFinite(ms) ? new Date(ms).toISOString() : undefined
}

/** S-05 的一次性兼容:`created_ms/updated_ms/expires_ms` → ISO `*_at`(后端改完删除) */
export function normalizeJob(j: Job): Job {
  return {
    ...j,
    created_at: j.created_at ?? msToIso(j.created_ms),
    updated_at: j.updated_at ?? msToIso(j.updated_ms),
    expires_at: j.expires_at ?? msToIso(j.expires_ms),
  }
}

/* ── 工作流 ── */

export interface WorkflowDef {
  id: string
  name: string
  version: number
  steps: number
  enabled: boolean
  schedule_cron?: string | null
  last_run_at?: string | null
  yaml?: string
  inputs?: { key: string; label: string; required?: boolean }[]
}

export interface WorkflowStep {
  step_id: string
  name: string
  op: string
  args_digest?: string
  code?: string
  state_before?: string
  state_after?: string
  cost_ms?: number
  status: string
  shot?: string | null
  needs_human?: boolean
}

export interface WorkflowRun {
  run_id: string
  workflow: string
  status: string
  account_ids: string[]
  started_at?: string
  finished_at?: string | null
  steps: WorkflowStep[]
}

/* ── 邮件 ── */

export interface MailRouteStatus {
  route_id: string
  channel: string | null
  account_id: string | null
  scope: string
  /** #56 逐字:`route:{id,channel,account_id,outbound_template_id,inbound_template_id}` */
  route?: {
    id: string
    channel: string | null
    account_id: string | null
    outbound_template_id?: string | null
    inbound_template_id?: string | null
  }
  inbound: {
    protocol_configured: 'imap' | 'pop3'
    protocol_active: 'imap' | 'pop3'
    fallback: { since_at: string; reason: string } | null
    folders: { name: string; uidvalidity: number; last_uid: number }[]
    last_success_at: string | null
    last_error?: string | null
    idle_supported: boolean
    consecutive_failures: number
    quota: { used_mb: number; limit_mb: number; source: 'server' | 'estimate' }
  }
  outbound: {
    queued: number
    retrying: number
    dead: number
    last_sent_at: string | null
    consecutive_failures: number
    rate_per_min: number
  }
  cleanup: { last_run_at: string | null; last_status: string; archived_mb: number; next_run_at: string | null }
}

export interface MailStatus {
  enabled: boolean
  routes: MailRouteStatus[]
}

/**
 * #56 不带 `route_id` 时应回 `{enabled, routes:[…]}`;后端当前把它做成了列表端点
 * (`{ok:true, data:[…]}`)。两形都接住,免得 P-MAIL 因为多一层数组白屏。
 */
export function normalizeMailStatus(raw: unknown): MailStatus {
  if (Array.isArray(raw)) return { enabled: raw.length > 0, routes: raw as MailRouteStatus[] }
  const o = (raw ?? {}) as Partial<MailStatus> & { route?: MailRouteStatus }
  if (Array.isArray(o.routes)) return { enabled: o.enabled !== false, routes: o.routes }
  if (o.route) return { enabled: o.enabled !== false, routes: [o.route] }
  return { enabled: o.enabled === true, routes: [] }
}

/**
 * `#58 GET /mail/inbox` 的行。🔴 键集以 backend-api-4 §1 P-1 的出参视图为准
 * (时间一律 ISO `*_at`、`id` 为字符串、**列表不含 `body_text`**,详情 `#59` 才给)。
 * 这里只列页面真正用到的键;后端那张表还有 `mailbox`/`uid`/`attach_json` 等库列,用到再补。
 */
export interface MailInboxRow {
  id: string
  received_at: string
  /** 派生列:`default` / `qidian|qq|wechat` / `<account_id>` —— **scope 名,不是中文显示名** */
  route: string | null
  route_id?: number | string | null
  from_addr: string
  subject: string
  status: string
  reason?: string | null
  trace_id?: string | null
  req_id?: string | null
  /**
   * ⚠️ **后端没有这个键**(`mail_inbox` 无此列,02 也没写它的算法;回执其实是 `mail_outbox` 里
   * `ref_inbox_id=… AND kind='receipt'` 的行)。定义与出处**待裁决**,在那之前一律当它不存在:
   * 页面显示处必须做空值兜底,不许依赖它。
   */
  receipt_status?: string | null
  /** 派生列:`archived_path`/`archived_ms` 任一非空即真(01 §2.7.8「是否已归档」) */
  archived?: boolean
  date_at?: string | null
  confirm_expires_at?: string | null
  archived_at?: string | null
  deleted_at?: string | null
  sig_ok?: boolean | null
  to_addrs?: string | null
  attach_cnt?: number | null
}

/** `#59 GET /mail/inbox/{id}` = `#58` 的行 **+ `body_text`**,其余一字不差(backend-api-4 §1 P-1) */
export interface MailInboxDetail extends MailInboxRow {
  /** 正文只在详情下发;列表端点一律不带(02 #58 逐字) */
  body_text?: string | null
  template_alias?: string
  parsed?: {
    op: string
    args: Record<string, string>
    args_digest?: string
    target?: string
    req_id?: string
    nonce?: string
    sig_ok?: boolean
  }
  result?: { code: string; cost_ms: number }
}

/**
 * `#61 GET /mail/outbox` 的行(13 键)。键集出处 = 01 §2.7.8 发件队列逐字
 * `kind/to/subject/status/attempts/next_attempt_at/last_error/ref`,
 * 加 `id`/`route_id`/`route`/`created_at`/`sent_at`(backend-api-4 §1 P-1)。
 * 🔴 不下发正文(`body_text`/`body_html`)与 `smtp_response`/`dedup_key` 等库列。
 */
export interface MailOutboxRow {
  id: string
  kind: string
  to: string
  subject: string
  status: string
  attempts: number
  /** ISO;`next_attempt_ms=0`(DDL 默认 = 没有下次)后端回 `null`,不回 1970 */
  next_attempt_at?: string | null
  /** C-42 的排序列 */
  created_at?: string | null
  sent_at?: string | null
  /** 派生列,同 `#58`:scope 名,不是中文显示名 */
  route?: string | null
  route_id?: number | string | null
  last_error?: string | null
  /** 派生:`ref_trace_id` → `ref_message_id` → `str(ref_inbox_id)`,都空则 `null` */
  ref?: string | null
}

export interface MailCleanupRow {
  id: string
  at: string
  deleted: number
  archived: number
  status: string
  error?: string | null
}

/**
 * 待确认危险指令(02 #68b)。
 * 🔴 出参逐字八键 —— **没有** `confirm_via` / `confirm_nonce`(v1 无此列,R6-7/R6-25)。
 */
export interface PendingConfirm {
  id: string
  op: string
  from_addr: string
  account_id: string | null
  args_digest: string
  created_at: string
  expires_at: string
  /** 服务端算,前端不自己减(时钟不一致会把已过期的显示成还能批) */
  remaining_ttl_s: number
}

/* ── 系统 / 环境 ── */

export interface SystemVersion {
  console?: string
  /**
   * 🔴 总控裁决①:`agent` 的形状**以后端现实现为准** = `{version}` 对象。
   * (此前前端按裸字符串渲染,真后端下发对象 ⇒ 版本栏显示 `[object Object]`,S-09。)
   */
  agent: { version: string; api_version?: string }
  winagent: { version: string | null; online: boolean; user_agent?: boolean }
  /** 未探到时后端给 `null`(不是空串)——页面显示「未知」 */
  kernel?: string | null
  kernel_state?: string | null
  wsl?: string | null
  wsl_state?: string | null
  docker?: string | null
  distro?: string | null
  images?: Record<string, unknown>
  api_version: string
  capabilities_version: string
  schema_version: number
  wa_schema_version?: number
  migration?: { state: 'idle' | 'pending' | 'running' | 'failed'; from?: number; to?: number; at?: string }
}

export type HealthCheckState = 'ok' | 'firing' | 'unknown'

/**
 * #72 `checks`。R6-58 (y):全局 `Hxx` 键集一个字不动,**另带** per-account 子键
 * `accounts: {"<account_id>": {H04..H08}}`(01 §2.7.3.4 的账号健康行取这里)。
 */
export interface SystemHealthChecks {
  accounts?: Record<string, Record<string, HealthCheckState>>
  [k: string]: HealthCheckState | Record<string, Record<string, HealthCheckState>> | undefined
}

/** 取一个全局健康项;未接入时该键缺席(R6-53)⇒ `unknown` */
export function globalCheck(checks: SystemHealthChecks | undefined, code: string): HealthCheckState {
  const v = checks?.[code]
  return typeof v === 'string' ? v : 'unknown'
}

/** 取某账号的 H04~H08(R6-58 (y)) */
export function accountCheck(
  checks: SystemHealthChecks | undefined,
  accountId: string,
  code: string,
): HealthCheckState {
  return checks?.accounts?.[accountId]?.[code] ?? 'unknown'
}

export interface SystemHealth {
  ok: boolean
  /** 免鉴权来源只回布尔级摘要(C-33);带令牌回全量对象 */
  agent: { version: string; api_version: string; uptime_s: number; db_mb: number; wal_mb: number } | boolean
  dockerd: boolean
  winagent: { online: boolean; version: string | null; user_agent: boolean } | boolean
  user_agent?: boolean
  accounts?: Record<string, number>
  disk_free_mb?: number
  mem?: { avail_mb: number; level: WatermarkLevel }
  checks?: SystemHealthChecks
  /** 各定时任务 `runs/skipped/errors`(#72) */
  scheduler?: Record<string, { runs?: number; skipped?: number; errors?: number }>
  alerts?: AlertPayload[]
}

/**
 * #74 `GET /system/env`(P-ENV 环境快照,C-32)。
 * 🔴 形状按真后端 = 02 #74 的描述:**Windows 侧(`GET /wa/v1/net`)与 WSL 侧分成两半**,
 * WinAgent 不可达时 `windows` 为 `null` 且 `windows_error` 给原因(四态)——页面显示「未知 + 原因」,
 * 不是把 net_state 当空串糊过去。
 */
export interface SystemEnvWindows {
  net_state?: string
  proxy?: string | null
  vpn_adapter?: string | null
  wsl_subnet?: string | null
  host_ip?: string | null
  /** N-21 / V4:常驻冲突行的数据源(WinAgent 侧算) */
  docker_conflict?: { state: 'ok' | 'conflict'; source?: 'install' | 'runtime_vpn'; overlap_prefix?: string; at?: string }
  [k: string]: unknown
}

export interface SystemEnvWsl {
  iface?: string
  mtu?: number | null
  resolv_conf?: { source?: string; nameservers?: string[] }
  docker?: { default_address_pools?: { base: string; size: number }[] }
  ksm?: { run?: number | null }
  zram?: { disksize_mb?: number | null }
  kernel_release?: string | null
  clock?: { drift_ms?: number | null; last_probe_at?: string | null }
  adb_server?: { running?: boolean | null; reason?: string | null }
  [k: string]: unknown
}

export interface SystemEnv {
  windows: SystemEnvWindows | null
  /** WinAgent 侧取不到时的原因(`winagent_offline` / `winagent_error` / …) */
  windows_error?: string | null
  wsl: SystemEnvWsl | null
  /** `.wslconfig` 生效对比(会话代理不在线时为 null) */
  wslconfig?: Record<string, unknown> | null
  reboot_required?: boolean | null
  winagent?: { online: boolean; version: string | null; user_agent?: boolean }
}

/** docker 网段:WSL 侧 `default_address_pools` 的第一段(冲突判定在 Windows 侧) */
export function dockerCidrOf(env: SystemEnv | null): string | null {
  return env?.wsl?.docker?.default_address_pools?.[0]?.base ?? null
}

/**
 * #75/#76 的探测行。
 * 🔴 键名以真后端(= 04 的 `probe_results` 列)为准:结论列叫 **`status`**、诊断文字叫 `detail`,
 * 另带 `side`(哪一侧探的)与 `level_reached`。`result`/`hint` 作一次性兼容保留。
 */
export interface ProbeRow {
  side?: 'wsl' | 'windows' | string
  target: string | null
  status?: string
  /** @deprecated 兼容旧字段名(后端统一成 `status` 后删) */
  result?: string
  level_reached?: string | null
  detail?: string | null
  /** @deprecated 兼容旧字段名(后端统一成 `detail` 后删) */
  hint?: string | null
  at?: string | null
}

/** 探测结论(00 §8.5):后端列名 `status`,旧实现叫 `result` */
export function probeStatusOf(row: ProbeRow): string {
  return row.status ?? row.result ?? 'UNKNOWN'
}

/** 诊断文字:后端 `detail`,旧实现 `hint` */
export function probeDetailOf(row: ProbeRow): string | null {
  return row.detail ?? row.hint ?? null
}

/**
 * #79/#79b 一轮自检的出参(02 #79 逐字 `{redroid_boot_ms, napcat_ok, winagent_ok, probes}`)。
 * 🔴 它是**一个对象**,不是行数组 —— 01 §4 的自检表要行,所以行由 `selftestRows()` 派生。
 */
export interface SelftestRun {
  run_id?: string | null
  redroid_boot_ms?: number | null
  napcat_ok?: boolean | null
  winagent_ok?: boolean | null
  winagent_version?: string | null
  probes?: ProbeRow[]
  started_at?: string | null
  finished_at?: string | null
  /** 没有执行体的步骤(`{step, reason}`)—— 不假装 ok,渲染成 warn + 原因 */
  skipped?: { step: string; reason: string }[]
}

/**
 * #76 `?kind=observed` 的行(R6-58 (dc))。
 * `id` = `probe_targets_observed.id`(#76b 的 `observed_ids` 就是它,**不得用行下标**);
 * `in_config` = 该行是否已在 `settings['probe.targets']` 里(01 §2.7.9「默认只勾新增项」的判据)。
 */
export interface ObservedProbeRow {
  id: number
  account_id?: string | null
  channel?: Channel | null
  remote_host?: string | null
  remote_ip: string
  port: number
  proto?: string
  samples?: number
  first_seen_at?: string | null
  last_seen_at?: string | null
  adopted_at?: string | null
  in_config: boolean
}

/** #76b `PUT /settings/probe` 出参(R6-58 (db) 逐字四键) */
export interface AdoptProbeResult {
  /** 与入参同维度 = 行 id */
  adopted: number[]
  adopted_rows?: ObservedProbeRow[]
  /** 元素逐字 `"host:port"`,**无** `channel_` 前缀 */
  targets: string[]
  hosts_by_channel?: { qidian_hosts?: string[]; qq_hosts?: string[]; wechat_hosts?: string[] }
}

export interface SampleRow {
  account_id: string
  channel: Channel
  remote_host?: string | null
  remote_ip: string
  port: number
  proto: string
  samples: number
}

export interface SelftestRow {
  item: string
  label: string
  /**
   * `skip` = 灰「未探测」:**不计红黄、不阻断**(C-18,01 §2.7.9 探测结论表 `SKIPPED` 灰;
   * 01 M4-7「SKIPPED 不计红项,P-SETUP 步 3 不因它阻断」)。把「没测」显示成「有问题」= 界面说假话。
   */
  level: 'ok' | 'warn' | 'error' | 'skip'
  message?: string
}

/**
 * 探测 `detail` 的**已知枚举** → 中文(04 §3.4/§4/§9;真后端就发这些串)。
 * 枚举之外的 `detail` 是诊断摘要原文(errno、TLS issuer、HTTP 状态,04 §3.2),按原文显示。
 * ⚠️ 文案单一来源本应是 `i18n/zh-CN/codes.ts`(01 §2.9 约定 6);本批为守文件边界暂放这里,建议后续搬家。
 */
const PROBE_DETAIL_ZH: Record<string, string> = {
  not_configured: '目标未配置',
  agent_probe_disabled: 'Agent 未开启出网探测',
  agent_unreachable: 'Agent 不可达',
  wrong_side: '该目标不归这一侧探测',
}

/** 未知结论不伪装成「正常」:按黄处理(诚实标不确定),色/文案仍走 i18n 单一来源 */
function probeToneOf(status: string): ProbeResultMeta['tone'] {
  return PROBE_RESULTS[status]?.tone ?? 'warn'
}

/** 一条探测 → 人话一句:结论走 i18n 中文(不甩裸枚举),detail 已知枚举转中文、未知按原文 */
export function probeLineZh(r: ProbeRow): string {
  const status = probeStatusOf(r)
  const statusZh = PROBE_RESULTS[status]?.zh ?? `未知结论(${status})`
  const detail = probeDetailOf(r)
  const detailZh = detail ? (PROBE_DETAIL_ZH[detail] ?? detail) : ''
  return `${r.target ?? r.side ?? '?'}:${statusZh}${detailZh ? `(${detailZh})` : ''}`
}

function boolRow(item: string, label: string, v: boolean | null | undefined, skipped: string | null): SelftestRow {
  if (v === true) return { item, label, level: 'ok' }
  if (v === false) return { item, label, level: 'error', message: skipped ?? '检查未通过' }
  // null/undefined = 没跑(本期没有执行体)⇒ warn + 原因,**不假装 ok**
  return { item, label, level: 'warn', message: skipped ?? '未执行' }
}

/** #79/#79b 的一轮对象 → 01 §4 自检表的行;已经是行数组就原样用 */
export function selftestRows(run: SelftestRun | SelftestRow[] | null | undefined): SelftestRow[] {
  if (!run) return []
  if (Array.isArray(run)) return run
  const why = (step: string): string | null => run.skipped?.find((x) => x.step === step)?.reason ?? null
  const rows: SelftestRow[] = [
    run.redroid_boot_ms != null
      ? { item: 'redroid_boot', label: '临时容器启动', level: 'ok', message: `${run.redroid_boot_ms} ms` }
      : boolRow('redroid_boot', '临时容器启动', null, why('redroid_boot')),
    boolRow('napcat', 'NapCat 可用', run.napcat_ok, why('napcat')),
    boolRow(
      'winagent',
      `WinAgent 健康${run.winagent_version ? `(${run.winagent_version})` : ''}`,
      run.winagent_ok,
      why('winagent'),
    ),
  ]
  const probes = run.probes ?? []
  if (probes.length) {
    /**
     * 色按 01 §2.7.9 探测结论表的**芯片色**单一来源(`PROBE_RESULTS[*].tone`),不再自列枚举名
     * (原先写死的 `FAIL/TCP_FAIL/BLOCKED` 在 00 §8.5 里根本不存在 ⇒ 真红项 `TCP_TIMEOUT`/
     * `PROXY_REQUIRED`/`BLOCKED_BY_POLICY` 反而被判成黄)。`SKIPPED`(tone=`na`)不计红黄:
     * 全是 `na` ⇒ 整行灰 `skip`;与 OK 混排时按 OK 算。
     */
    const tones = probes.map((r) => probeToneOf(probeStatusOf(r)))
    const level: SelftestRow['level'] = tones.includes('fail')
      ? 'error'
      : tones.includes('warn')
        ? 'warn'
        : tones.every((t) => t === 'na')
          ? 'skip'
          : 'ok'
    rows.push({
      item: 'probes',
      label: `连通性探测(${probes.length} 项)`,
      level,
      message: probes.map(probeLineZh).join('、'),
    })
  }
  return rows
}

/**
 * #102 `GET /system/public-endpoint`(E-3)。
 * 🔴 键集逐字按 02 #102:`{public_ip, public_ip_v6?, configured_domain?, checked_at, changed_at,
 * probe:{url, unreachable_rounds}}`。此前前端自造的 `configured_host / dns_resolved_ip /
 * matches / last_changed_at / history` 在 docs 里**不存在**(S-04)。
 * `configured_domain` = `settings api.public_domain`,写回走 `PUT /settings/api {public_domain}`。
 */
export interface PublicEndpoint {
  public_ip: string | null
  public_ip_v6?: string | null
  configured_domain?: string | null
  checked_at: string | null
  changed_at?: string | null
  probe?: { url: string | null; unreachable_rounds: number }
}

/* ── 审计 ── */

/**
 * #95 `GET /audit` 的行。
 * 🔴 R6-58 (ag) 把列集定死为十列:`id, ts_ms, kind, transport, actor, action, account_id,
 * trace_id, result_code, detail_json` —— `cost_ms / ip / http_status / method / path / sig_ok`
 * **在 `detail_json` 里**,不是独立列(S-01;此前前端按 `ts/op/code/...` 取,P-LOG 全列空)。
 */
export interface AuditRow {
  id: number | string
  /** epoch 毫秒(库列即毫秒;本端点是 CSV 列序的唯一出处,不做 ISO 转换) */
  ts_ms: number
  kind: string
  transport?: string | null
  actor?: string | null
  /** 指令行 = op 名;api 行 = `"METHOD /path"` */
  action?: string | null
  account_id?: string | null
  trace_id?: string | null
  /** 指令行 = 结果码;api 行 = HTTP 状态的字符串形 */
  result_code?: string | null
  /** JSON 字符串(后端序列化后原样下发);解析用 `auditDetail()` */
  detail_json?: string | Record<string, unknown> | null
  /* 后端同时下发、但真值在 `detail_json` 里的几列(恒 null),留着只为不丢键 */
  cost_ms?: number | null
  http_status?: number | null
  ip?: string | null
}

/** `detail_json` 解开后的常见键(端点各异,一律可空) */
export interface AuditDetail {
  http_status?: number
  cost_ms?: number
  ip?: string
  method?: string
  path?: string
  sig_ok?: boolean
  op?: string
  args_digest?: string
  source?: string
  reason?: string
  [k: string]: unknown
}

/** 解析 `detail_json`(字符串或已解开的对象都吃得下;坏 JSON 回空对象,不抛) */
export function auditDetail(row: AuditRow): AuditDetail {
  const d = row.detail_json
  if (!d) return {}
  if (typeof d === 'object') return d as AuditDetail
  try {
    const parsed = JSON.parse(d) as unknown
    return typeof parsed === 'object' && parsed !== null ? (parsed as AuditDetail) : {}
  } catch {
    return {}
  }
}

/** 审计行时间 → 本地可读串(`ts_ms` 是毫秒,不是 ISO) */
export function auditTsText(row: AuditRow): string {
  return Number.isFinite(row.ts_ms) ? new Date(row.ts_ms).toLocaleString('zh-CN', { hour12: false }) : '—'
}

/* ── 事件(00 §7.5) ── */

export type EventKind =
  | 'message' | 'account_state' | 'command_done' | 'workflow'
  | 'alert' | 'resource' | 'mail' | 'net' | 'job'

export interface AlertPayload {
  code: string
  severity: Severity
  state: 'firing' | 'resolved'
  subject: string
  title: string
  message: string
  hint_actions: string[]
  first_seen_at: string
  last_seen_at: string
  count: number
  evidence?: Record<string, unknown>
}

export interface QtEvent<P = unknown> {
  event: EventKind
  ts: string
  seq: number
  trace_id?: string
  account_id?: string
  channel?: Channel
  payload: P
}

export interface AccountStatePayload {
  state: AccountState
  state_code: string
  state_reason: string
  error_since_ms: number | null
  enabled: boolean
  runtime: AccountRuntime
  capabilities: string[]
  self_nick?: string
  prompt?: Prompt
  /** 标识**一次登录尝试**;非登录态为 null(N-3) */
  login_session_id?: string | null
}

/* ── 设置 ── */

/** #90 `GET /settings/api-clients`(不含 secret;键集按真后端) */
export interface ApiClientRow {
  app_id: string
  name: string
  auth_kind?: 'bearer' | 'hmac'
  level: 'read' | 'write' | 'admin'
  ip_allow: string[]
  allow_ops?: string[]
  allow_accounts?: string[]
  rate_per_min?: number
  api_version_min?: number
  enabled?: boolean
  secret_ref?: string | null
  builtin?: boolean
  /** 令牌前 6 位(只在创建那一次有;列表不回) */
  prefix6?: string
  created_at: string
  updated_at?: string
  last_used_at?: string | null
  revoked_at?: string | null
}

export interface VaultEntry {
  id: string
  scope: string
  version: number
  updated: string
  last_read?: string | null
  suspect: boolean
}

export interface MailTemplate {
  template_id: string
  name: string
  kind: 'inbound' | 'outbound'
  compat_profile: 'ibquote-163-v1' | 'collector-v1'
  subject_pattern: string
  body_fields: { key: string; label: string; order: number; required: boolean }[]
  referenced_by: string[]
  version: number
  updated_at: string
}

export interface MailRouteOverride {
  account_id: string
  channel: Channel
  inherit_from: string
  enabled: boolean
}

/** #24 `GET /device-profiles/templates`(字段名统一 `profile_key`) */
export interface DeviceProfileTemplate {
  profile_key: string
  brand: string
  model: string
  release?: string
  /** 随机挑档案时的权重(02 #24 出参列) */
  weight?: number
}
