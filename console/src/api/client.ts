/**
 * 控制台视角的 `/api/v1` 端点清单(01 §3;端点名以 02 §3.4 为准)。
 * 路径与入参逐字按 02 写,页面只调这里、不自己拼 URL。
 */

import { request, requestList, requestBlob, requestEnvelope, newIdempotencyKey } from './http'
import type {
  Account, ApiClientRow, AuditRow, CapabilityDef, CommandResult, DeviceProfileTemplate, Job,
  MailCleanupRow, MailInboxDetail, MailInboxRow, MailOutboxRow, MailRouteOverride, MailStatus,
  MailTemplate, Message, MetricsSnapshot, PendingConfirm, Prompt, ProbeRow, PublicEndpoint,
  ResourcePool, SampleRow, SelftestRow, SessionRow, SystemEnv, SystemHealth, SystemVersion,
  WorkflowDef, WorkflowRun,
} from './types'
import type { Channel } from '@/i18n/zh-CN/codes'

/* ───────────────── 账号 ───────────────── */

export const accountsApi = {
  /** #1;微信档案列表 = `?channel=wechat&include_stopped=true`(C-01/C-40) */
  list: (q?: { channel?: Channel; state?: string; enabled?: boolean; include_stopped?: boolean }) =>
    requestList<Account>('/accounts', { query: q as Record<string, unknown> }),

  get: (id: string) => request<Account>(`/accounts/${id}`),

  /** #2 `{channel,label,profile_key?,login:{mode,account?,secret?,remember?}}` */
  create: (body: {
    channel: Channel
    label: string
    profile_key?: string
    login: { mode: 'password' | 'qrcode' | 'manual'; account?: string; secret?: string; remember?: boolean }
  }) => request<Account>('/accounts', { method: 'POST', body, idempotencyKey: newIdempotencyKey() }),

  patch: (id: string, body: Record<string, unknown>) =>
    request<Account>(`/accounts/${id}`, { method: 'PATCH', body }),

  start: (id: string) => request<{ state: string }>(`/accounts/${id}/start`, { method: 'POST' }),
  stop: (id: string) => request<{ state: string }>(`/accounts/${id}/stop`, { method: 'POST', body: { graceful: true } }),
  restart: (id: string) => request<{ state: string }>(`/accounts/${id}/restart`, { method: 'POST' }),
  enable: (id: string) => request<{ state: string }>(`/accounts/${id}/enable`, { method: 'POST' }),
  disable: (id: string) => request<{ state: string }>(`/accounts/${id}/disable`, { method: 'POST', body: { graceful: true } }),

  /** #7:**恒软删、无 `keep_data`**(R-11);`confirm` = 当前 label */
  softDelete: (id: string, confirmLabel: string) =>
    request<{ deleted: boolean; data_kept: boolean }>(`/accounts/${id}`, {
      method: 'DELETE',
      query: { confirm: confirmLabel },
    }),

  /** #8 真删:须手输账号 id 原文 */
  purge: (id: string) => request<{ job_id: string }>(`/accounts/${id}/purge`, { method: 'POST', body: { confirm: id } }),

  /** #12 密码登录 / 触发登录流 */
  login: (id: string, body: { secret?: string; remember?: boolean; mode?: string; account?: string }) =>
    request<{ state: string; state_code?: string; login_session_id: string }>(`/accounts/${id}/login`, {
      method: 'POST', body,
    }),

  /**
   * #16b 取消一次登录尝试 / 释放微信槽位 pending。
   * 🔴 R6-6:控制台**恒带** `login_session_id`(来源 = `wechat_slots.pending_login_session_id`),
   * 不带会误杀刚重开的新尝试(N-3)。
   * 回包两形状:`{cancelled:true,stale:false}` / **幂等 no-op** `{cancelled:false,stale:true,current_login_session_id}`。
   */
  loginCancel: (id: string, loginSessionId: string) =>
    request<{ cancelled: boolean; stale: boolean; current_login_session_id?: string }>(
      `/accounts/${id}/login/cancel`, { method: 'POST', body: { login_session_id: loginSessionId } },
    ),

  /** #15 当前等人提示;刷新/重连后重取(二维码不落盘) */
  prompt: (id: string, loginSessionId?: string) =>
    request<Prompt>(`/accounts/${id}/prompt`, { query: loginSessionId ? { login_session_id: loginSessionId } : undefined }),

  /** #17 微信单槽切换;`confirm:true` = 用户已确认接管故障 holder(R5-4) */
  switchTo: (wxNN: string, confirm = false) =>
    request<{ holder_before: string; target: string }>(`/accounts/${wxNN}/switch`, { method: 'POST', body: { confirm } }),

  /** #18 新增微信号 */
  switchNew: (confirm = false) =>
    request<{ target: string; merged_into?: string }>('/accounts/switch', {
      method: 'POST', body: { target: 'new', confirm },
    }),

  /** #22 账号级设置 */
  patchSettings: (id: string, body: Record<string, unknown>) =>
    request<Account>(`/accounts/${id}/settings`, { method: 'PATCH', body }),

  /** #23 批量:显式列 id,不接受 `*` */
  batch: (ids: string[], action: 'start' | 'stop' | 'restart' | 'enable' | 'disable') =>
    request<{ results: Record<string, { ok: boolean; code: string }> }>('/accounts/batch', {
      method: 'POST', body: { ids, action },
    }),

  /** #13 / #14 凭据:只改 Vault 不登录 */
  putCredential: (id: string, body: { account?: string; secret: string; remember: true }) =>
    request<{ ok: boolean }>(`/accounts/${id}/credential`, { method: 'PUT', body }),
  deleteCredential: (id: string) => request<{ ok: boolean }>(`/accounts/${id}/credential`, { method: 'DELETE' }),

  /** #20 该账号当前实际可用能力 */
  capabilities: (id: string) =>
    request<{ capabilities: string[]; matrix: Record<string, string> }>(`/accounts/${id}/capabilities`),

  /** #97/#98 QQ 临时 WebUI(C-35) */
  webuiOpen: (id: string, minutes = 10) =>
    request<{ url: string; until: string }>(`/accounts/${id}/webui/open`, { method: 'POST', body: { minutes } }),
  webuiClose: (id: string) => request<{ ok: boolean }>(`/accounts/${id}/webui/close`, { method: 'POST' }),

  /** #99 仅 QQ:导出 qq_data → job */
  exportIdentity: (id: string) => request<{ job_id: string }>(`/accounts/${id}/export-identity`, { method: 'POST' }),

  /** #100/#101 企点运行时自愈(P-26) */
  reconnectAdb: (id: string) => request<{ ok: boolean; adb_state: string }>(`/accounts/${id}/runtime/reconnect-adb`, { method: 'POST' }),
  restartStream: (id: string) => request<{ ok: boolean }>(`/accounts/${id}/runtime/restart-stream`, { method: 'POST' }),

  /** #33 截图 */
  screenshot: (id: string) => requestBlob(`/accounts/${id}/screenshot`),

  /** #35 无 WS 时的 REST 注入兜底(C-08) */
  streamInput: (id: string, body: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/accounts/${id}/stream/input`, { method: 'POST', body }),
}

/* ───────────────── 指令 / 能力 ───────────────── */

export const commandsApi = {
  /** #21 全局能力目录 */
  capabilities: (channel?: Channel) =>
    requestEnvelope<CapabilityDef[]>('/capabilities', { query: channel ? { channel } : undefined })
      .then((env) => ({
        items: (env.data as CapabilityDef[]) ?? [],
        version: (env.capabilities_version as string) ?? '',
      })),

  /** #28 单账号指令 */
  run: (accountId: string, body: {
    op: string
    args: Record<string, unknown>
    idempotency_key?: string
    confirm?: boolean
    timeout_ms?: number
  }) => request<CommandResult>(`/accounts/${accountId}/commands`, {
    method: 'POST', body, idempotencyKey: body.idempotency_key,
  }),

  /** #36 广播:必须显式勾选账号,没有「全部」 */
  broadcast: (body: {
    account_ids: string[]
    op: string
    args: Record<string, unknown>
    idempotency_key: string
    confirm?: boolean
  }) => request<{ broadcast_id: string; results: Record<string, CommandResult> }>('/broadcast/commands', {
    method: 'POST', body, idempotencyKey: body.idempotency_key,
  }),

  /** #24 机型档案库 */
  deviceProfiles: () => requestList<DeviceProfileTemplate>('/device-profiles/templates'),
}

/* ───────────────── 会话 / 消息 / 媒体 ───────────────── */

export interface MessageQuery {
  account_id?: string
  session_id?: string
  dir?: 'in' | 'out'
  type?: string
  q?: string
  since?: string
  until?: string
  needs_review?: boolean
  limit?: number
  cursor?: string
}

export const messagesApi = {
  sessions: (q?: { account_id?: string; keyword?: string }) =>
    requestList<SessionRow>('/sessions', { query: q as Record<string, unknown> }),

  list: (q: MessageQuery) => requestList<Message>('/messages', { query: q as Record<string, unknown> }),

  /** #55 取媒体一律按 sha256,`ref` 只用于显示 */
  media: (sha256: string) => requestBlob(`/media/${sha256}`),

  /** #51 异步导出 → job */
  export: (body: { filter: MessageQuery; format: 'csv' | 'json'; include_media: boolean }) =>
    request<{ job_id: string }>('/messages/export', { method: 'POST', body }),
}

/* ───────────────── 异步作业(§11.21 [JOB]) ───────────────── */

export const jobsApi = {
  get: (jobId: string) => request<Job>(`/jobs/${jobId}`),
  cancel: (jobId: string) => request<{ ok: boolean }>(`/jobs/${jobId}/cancel`, { method: 'POST' }),
}

/* ───────────────── 工作流 ───────────────── */

export const workflowsApi = {
  list: () => requestList<WorkflowDef>('/workflows'),
  get: (id: string) => request<WorkflowDef>(`/workflows/${id}`),
  run: (id: string, body: { args: Record<string, unknown>; account_ids: string[] }) =>
    request<{ run_id: string }>(`/workflows/${id}/run`, { method: 'POST', body, idempotencyKey: newIdempotencyKey() }),
  getRun: (runId: string) => request<WorkflowRun>(`/workflows/runs/${runId}`),
  cancelRun: (runId: string) => request<{ ok: boolean }>(`/workflows/runs/${runId}/cancel`, { method: 'POST' }),
}

/* ───────────────── 资源 / 监控 ───────────────── */

export const resourcesApi = {
  get: () => request<ResourcePool>('/resources'),
  /** 本期只取快照;`&since=-24h&resolution=` 时间序列是 M6(R6-17) */
  metrics: (q?: { scope?: string; subject?: string }) =>
    request<MetricsSnapshot>('/system/metrics', { query: { ...(q ?? {}), snapshot: 1 } }),
  calibrate: (apply: boolean) =>
    request<{ job_id?: string; suggestions?: Record<string, number> }>('/resources/calibrate', {
      method: 'POST', body: { apply },
    }),
  /** #109 即时全量**本地**清理;202 {job_id},绝不删远端邮件 */
  cleanupRun: () => request<{ job_id: string }>('/system/cleanup/run', { method: 'POST' }),
  precheck: (channel: Channel) =>
    request<{ can_add: number; reason?: string; alternatives?: unknown[] }>('/resources/precheck', {
      method: 'POST', body: { channel },
    }),
}

/* ───────────────── 系统 / 环境(一律经 Agent,C-32) ───────────────── */

export const systemApi = {
  version: () => request<SystemVersion>('/system/version'),
  health: () => request<SystemHealth>('/system/health'),
  env: () => request<SystemEnv>('/system/env'),
  notice: () => request<{ notice_version: string; text: string }>('/system/notice'),
  probes: (kind?: 'result' | 'observed') =>
    requestList<ProbeRow>('/system/probes', { query: kind ? { kind } : undefined }),
  probe: (targets?: string[]) =>
    request<{ run_id: string; results: ProbeRow[] }>('/system/probe', {
      method: 'POST', body: { targets, trigger: 'manual' },
    }),
  /** C-1 实测采样 */
  probeSample: (durationS = 30) =>
    request<{ sampled_at: string; rows: SampleRow[] }>('/system/probe', {
      method: 'POST', body: { mode: 'sample', duration_s: durationS },
    }),
  /** 把采样行采纳为正式探测目标(经 Agent 转 WinAgent,控制台不直调 /wa/v1/probes) */
  adoptProbeTargets: (observedIds: string[]) =>
    request<{ adopted: string[]; targets: string[] }>('/settings/probe', {
      method: 'PUT', body: { observed_ids: observedIds },
    }),
  selftestRun: () => request<{ run_id: string }>('/system/selftest', { method: 'POST' }),
  selftestResult: (runId?: string) =>
    requestList<SelftestRow>('/system/selftest', { query: runId ? { run_id: runId } : undefined }),
  diagnostics: (withScreenshots: boolean) =>
    request<{ job_id: string }>('/system/diagnostics', { method: 'POST', body: { with_screenshots: withScreenshots } }),
  dockerProxy: (enable: boolean) =>
    request<{ applied: boolean; proxy?: string }>('/system/docker-proxy', { method: 'POST', body: { enable } }),
  /** C-03:Agent 自 drain 后调 WinAgent;基线 §11-6 绝不自动 */
  wslRestart: () =>
    request<{ ok: boolean }>('/system/wsl-restart', { method: 'POST', body: { mode: 'shutdown', confirm: true } }),
  publicEndpoint: (refresh = false) =>
    request<PublicEndpoint>('/system/public-endpoint', { query: refresh ? { refresh: true } : undefined }),
}

/* ───────────────── 邮件 ───────────────── */

export const mailApi = {
  status: () => request<MailStatus>('/mail/status'),
  inbox: (q?: Record<string, unknown>) => requestList<MailInboxRow>('/mail/inbox', { query: q }),
  inboxDetail: (id: string) => request<MailInboxDetail>(`/mail/inbox/${id}`),
  reparse: (id: string) => request<{ ok: boolean }>(`/mail/inbox/${id}/reparse`, { method: 'POST' }),
  outbox: (q?: Record<string, unknown>) => requestList<MailOutboxRow>('/mail/outbox', { query: q }),
  resend: (id: string) => request<{ ok: boolean }>(`/mail/outbox/${id}/resend`, { method: 'POST' }),
  discard: (id: string) => request<{ ok: boolean }>(`/mail/outbox/${id}/discard`, { method: 'POST' }),
  cleanupLog: () => requestList<MailCleanupRow>('/mail/cleanup/log'),
  /** 只清邮件(会动服务器邮件,danger);与 P-RES 的全量清理不是一回事 */
  cleanupRun: () => request<{ job_id: string }>('/mail/cleanup/run', { method: 'POST', body: { dry_run: false, trigger: 'manual' } }),
  test: (which: 'inbound' | 'outbound', routeId?: string) =>
    request<{ ok: boolean; imap_ok?: boolean; pop3_ok?: boolean }>('/mail/test', {
      method: 'POST', body: { which, route_id: routeId },
    }),
  /** #67 入参只有 sender + short_name,**无 route**(短名全局唯一) */
  createHmacKey: (sender: string, shortName: string) =>
    request<{ secret: string }>('/mail/hmac-keys', { method: 'POST', body: { sender, short_name: shortName } }),
  /** #68 按短名吊销,不按 sender */
  revokeHmacKey: (shortName: string) => request<{ ok: boolean }>(`/mail/hmac-keys/${shortName}`, { method: 'DELETE' }),

  /** #68b 出参八键;v1 无 confirm_via / confirm_nonce */
  pendingConfirms: () => requestList<PendingConfirm>('/mail/pending-confirms'),
  /** #68c 只按 {id} 操作,不核验任何 nonce */
  approve: (id: string) => request<{ job_id?: string }>(`/mail/pending-confirms/${id}/approve`, { method: 'POST' }),
  /** #68d */
  reject: (id: string) => request<{ ok: boolean }>(`/mail/pending-confirms/${id}/reject`, { method: 'POST' }),
}

/* ───────────────── 设置 ───────────────── */

export const settingsApi = {
  get: <T = Record<string, unknown>>(group: string) => request<T>(`/settings/${group}`),
  put: <T = Record<string, unknown>>(group: string, body: Record<string, unknown>) =>
    request<T>(`/settings/${group}`, { method: 'PUT', body }),

  apiClients: () => requestList<ApiClientRow>('/settings/api-clients'),
  createApiClient: (body: Record<string, unknown>) =>
    request<{ app_id: string; token: string }>('/settings/api-clients', { method: 'POST', body }),
  rotateApiClient: (appId: string) =>
    request<{ token: string }>(`/settings/api-clients/${appId}/rotate`, { method: 'POST' }),
  revokeApiClient: (appId: string) => request<{ ok: boolean }>(`/settings/api-clients/${appId}`, { method: 'DELETE' }),

  mailRoutes: () => requestList<MailRouteOverride>('/settings/mail/routes'),
  putMailRoutes: (rows: MailRouteOverride[]) =>
    request<{ ok: boolean }>('/settings/mail/routes', { method: 'PUT', body: { routes: rows } }),

  mailTemplates: () => requestList<MailTemplate>('/settings/mail/templates'),
  saveMailTemplate: (tpl: Partial<MailTemplate>) =>
    tpl.template_id
      ? request<MailTemplate>(`/settings/mail/templates/${tpl.template_id}`, { method: 'PUT', body: tpl })
      : request<MailTemplate>('/settings/mail/templates', { method: 'POST', body: tpl }),
  deleteMailTemplate: (id: string) => request<{ ok: boolean }>(`/settings/mail/templates/${id}`, { method: 'DELETE' }),
  previewMailTemplate: (id: string, sampleMessageId: string) =>
    request<{ subject: string; body_text: string; warnings: string[] }>(`/mail/templates/${id}/preview`, {
      method: 'POST', body: { sample_message_id: sampleMessageId },
    }),

  putPublicEndpoint: (configuredHost: string) =>
    request<{ ok: boolean }>('/settings/public-endpoint', { method: 'PUT', body: { configured_host: configuredHost } }),
  webhooks: () => requestList<{ id: string; url: string; enabled: boolean }>('/settings/webhooks'),
  addWebhook: (url: string) => request<{ id: string }>('/settings/webhooks', { method: 'POST', body: { url } }),
  removeWebhook: (id: string) => request<{ ok: boolean }>(`/settings/webhooks/${id}`, { method: 'DELETE' }),

  compliance: () => request<{ ack_ms: number | null; notice_version: string | null }>('/settings/compliance'),
  putCompliance: (body: { ack_ms: number; notice_version: string }) =>
    request<{ ok: boolean }>('/settings/compliance', { method: 'PUT', body }),
}

/* ───────────────── 审计 ───────────────── */

export const auditApi = {
  list: (q: {
    kind?: 'command' | 'api' | 'system' | 'stream_input'
    account_id?: string
    op?: string
    code?: string
    actor?: string
    since?: string
    until?: string
    trace_id?: string
    limit?: number
    cursor?: string
  }) => requestList<AuditRow>('/audit', { query: q as Record<string, unknown> }),
}

/* WinAgent 能力不在这里:渲染进程被 webRequest 阻断,一律经 qt.wa.invoke → 主进程(§2.5 白名单)。 */
