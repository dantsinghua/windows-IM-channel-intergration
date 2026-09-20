/** Agent `/api/v1` 的数据模型(00 §7,02 §3.4 出参口径) */

import type { AccountState, Channel, ResultCode, Severity } from '@/i18n/zh-CN/codes'

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
  settings?: Record<string, unknown>
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
    }
  }
  realtime: { wsl_anon_mb: number; win_available_mb: number }
  quota_mb: Record<Channel, number>
  can_add: Record<Channel, number>
  accounts?: { id: string; anon_mb: number; current_mb: number; cpu_pct: number }[]
  accounts_by_channel?: Record<string, number>
  metrics_snapshot?: MetricsSnapshot
}

/* ── 监控快照(02 #77,E-19) ── */

export type WatermarkLevel = 'normal' | 'warn' | 'high' | 'critical'

export interface MetricsSnapshot {
  hardware: {
    mem: { total_mb: number; used_mb: number; avail_mb: number; vmmem_mb: number }
    cpu: { logical_cores: number; load_pct: number }
    disks: { mount: string; total_mb: number; free_mb: number }[]
  }
  ours: {
    procs: { agent_mb: number; winagent_mb: number; console_mb: number }
    accounts: { id: string; anon_mb: number; current_mb: number; cpu_pct: number; quota_mb: number }[]
    wechat: { chatlog_mb: number; wechat_pc_mb: number }
    storage: {
      db_mb: number; media_mb: number; mail_mb: number
      accounts_mb: number; backup_mb: number; vhdx_mb: number
    }
  }
  budget_vs_actual: { id: string; quota_mb: number; rss_mb: number; drift_pct: number }[]
  disk_watermark: {
    level: WatermarkLevel
    free_mb: number
    actions: string[]
    retention_shrunk_to?: number | null
    /** R6-30:扁平两键,不嵌套 */
    last_cleanup_at: string | null
    last_cleanup_freed_mb: number | null
    vhdx_grown_mb: number
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
  sender: { id: string; name: string }
  self: boolean
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
}

export interface SessionRow {
  id: string
  account_id: string
  channel: Channel
  name: string
  kind: 'group' | 'private'
  last_ts?: string | null
  unread?: number
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
  created_at?: string
  updated_at?: string
  expires_at?: string
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

export interface MailInboxRow {
  id: string
  received_at: string
  route: string
  from_addr: string
  subject: string
  status: string
  reason?: string | null
  trace_id?: string | null
  req_id?: string | null
  receipt_status?: string | null
  archived?: boolean
}

export interface MailInboxDetail extends MailInboxRow {
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

export interface MailOutboxRow {
  id: string
  kind: string
  to: string
  subject: string
  status: string
  attempts: number
  next_attempt_at?: string | null
  last_error?: string | null
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
  agent: string
  winagent: { version: string; online: boolean; user_agent: boolean }
  kernel: string
  kernel_state: string
  wsl: string
  wsl_state: string
  docker: string
  distro: string
  api_version: string
  capabilities_version: string
  schema_version: number
  wa_schema_version?: number
  migration?: { state: 'idle' | 'pending' | 'running' | 'failed'; from?: number; to?: number; at?: string }
}

export interface SystemHealth {
  ok: boolean
  agent: { version: string; api_version: string; uptime_s: number; db_mb: number; wal_mb: number } | boolean
  dockerd: boolean
  winagent: { online: boolean; version: string; user_agent: boolean } | boolean
  accounts?: Record<string, number>
  disk_free_mb?: number
  checks?: Record<string, 'ok' | 'firing' | 'unknown'>
  alerts?: AlertPayload[]
}

export interface SystemEnv {
  net_state: string
  proxy?: string | null
  vpn_adapter?: string | null
  wsl_subnet?: string
  host_ip?: string
  mtu?: number
  clock_drift_s?: number
  pending_restart?: boolean
  kernel_state?: string
  wsl_state?: string
  wslconfig?: Record<string, unknown>
  docker_cidr: string
  /** N-21 / V4:常驻冲突行的数据源 */
  docker_conflict: { state: 'ok' | 'conflict'; source?: 'install' | 'runtime_vpn'; overlap_prefix?: string; at?: string }
}

export interface ProbeRow {
  target: string
  result: string
  hint?: string | null
  at?: string
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
  level: 'ok' | 'warn' | 'error'
  message?: string
}

export interface PublicEndpoint {
  public_ip: string
  checked_at: string
  configured_host?: string | null
  dns_resolved_ip?: string | null
  matches: boolean
  last_changed_at?: string | null
  history: { at: string; from_ip: string; to_ip: string }[]
}

/* ── 审计 ── */

export interface AuditRow {
  id: string
  ts: string
  kind: string
  account_id?: string | null
  op?: string | null
  action?: string | null
  actor?: string | null
  transport?: string | null
  ip?: string | null
  code?: string | null
  cost_ms?: number | null
  http_status?: number | null
  method?: string | null
  path?: string | null
  sig_ok?: boolean | null
  args_digest?: string | null
  trace_id?: string | null
  source?: string | null
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

export interface ApiClientRow {
  app_id: string
  name: string
  prefix6: string
  level: 'read' | 'write' | 'admin'
  ip_allow: string[]
  created_at: string
  last_used_at?: string | null
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

export interface DeviceProfileTemplate {
  profile_key: string
  brand: string
  model: string
  release?: string
}
