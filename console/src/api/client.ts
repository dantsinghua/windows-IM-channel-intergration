/**
 * 控制台视角的 `/api/v1` 端点清单(01 §3;端点名以 02 §3.4 为准)。
 * 路径与入参逐字按 02 写,页面只调这里、不自己拼 URL。
 */

import {
  request, requestCommand, requestList, requestBinary, requestEnvelope,
  pickOnceSecret, newIdempotencyKey,
} from './http'
import { normalizeMailStatus, selftestRows } from './types'
import type {
  Account, AdoptProbeResult, ApiClientRow, AuditRow, CapabilityDef, CommandOutcome,
  CommandResult, DeviceProfileTemplate, Job, MailCleanupRow, MailInboxDetail, MailInboxRow,
  MailOutboxRow, MailRouteOverride, MailTemplate, Message, MetricsSnapshot,
  ObservedProbeRow, PendingConfirm, Prompt, ProbeRow, PublicEndpoint, ResourcePool, SampleRow,
  SelftestRow, SelftestRun, SessionRow, SystemEnv, SystemHealth, SystemVersion, WorkflowDef, WorkflowRun,
} from './types'
import type { Channel } from '@/i18n/zh-CN/codes'

/* ───────────────── 账号 ───────────────── */

export const accountsApi = {
  /** #1;微信档案列表 = `?channel=wechat&include_stopped=true`(C-01/C-40) */
  list: (q?: { channel?: Channel; state?: string; enabled?: boolean; include_stopped?: boolean }) =>
    requestList<Account>('/accounts', { query: q as Record<string, unknown> }),

  get: (id: string) => request<Account>(`/accounts/${id}`),

  /**
   * #2 `{channel,label,profile_key?,login:{mode,account?,secret?,remember?}}`。
   * 🔴 幂等键进 **body 的 `idempotency_key`**(02 §3.4 #2 入参列 / R6-54);不带 ⇒ `400 INVALID_ARGS`
   * `reason=idempotency_key_required`(E-02)。
   */
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

  /**
   * #8 账号彻底删除(danger,admin 级)。
   * 🔴 `confirm` 要**逐字等于账号 id**(手输,不是勾选);须先 #7 软删,否则 `409 not_soft_deleted`。
   * 回 `202 {job_id}` —— 进度与结果一律走 `#107 GET /jobs/{job_id}`;
   * 一旦 `params.irreversible_since_ms` 出现就**进了不可逆阶段**,取消按钮要灰掉
   * (点了也是 `409 NOT_CANCELLABLE`,backend-api-2 §6)。
   */
  purge: (id: string) => request<{ job_id: string }>(`/accounts/${id}/purge`, { method: 'POST', body: { confirm: id } }),

  /**
   * #16 登出。🔴 **按通道分界**(backend-api-2 §3,页面要分别提示,不许一律「登出失败」):
   * - 微信 = 经 WinAgent 真登出 → `202 {account_id, via}`;
   * - QQ   = `409 NOT_APPLICABLE` `reason=channel_no_logout`(登录态在 `qq_data` 卷里,通道无此概念);
   * - 企点 = `503 NOT_READY` `reason=logout_backend_missing`(有这个概念,但本期没有执行体)。
   */
  logout: (id: string) =>
    request<{ account_id: string; via?: string }>(`/accounts/${id}/logout`, { method: 'POST' }),

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

  /**
   * #22 账号级设置。
   * 🔴 总控裁决④:**`Account` 对象不带 `settings` 子对象**,账号级设置只走本端点写
   * (键集见 02 #22;`auto_recover`/`quota_mb` 等落在 Account 顶层,详情页从那里读)。
   */
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

  /**
   * #33 截图。🔴 **二进制,不是 JSON**:`Content-Type: image/png|image/jpeg`,
   * `media_id`/`sha256` 在响应头 `X-QT-Media-Id` / `X-QT-Sha256` 里(非 JSON 响应不带 `trace_id` 字段,
   * 指令 trace 在 `X-QT-Trace-Id`,backend-api-2 §6)。要存档/复盘就读这两个头,再走 #55 `GET /media/{sha256}`。
   * QQ 通道 `409 NOT_APPLICABLE`;企点截图执行层本期未接 ⇒ `409 UNSUPPORTED`(**不回占位图**)。
   */
  screenshot: (id: string, q?: { region?: string; format?: 'png' | 'jpeg' }) =>
    requestBinary(`/accounts/${id}/screenshot`, { query: q as Record<string, unknown> }),

  /**
   * #35 无 WS 时的 REST 注入兜底(C-08)。
   * 有 WS 时走控制帧、没有才用它(两条路都会写 `stream_input` 审计);
   * 画面流执行体未装配时 `503 NOT_READY` `reason=stream_backend_missing`。
   */
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

  /**
   * #28 单账号指令。
   * 🔴 走 `requestCommand`(不是 `request`):`200 + ok:false + 业务结果码` 是**正常业务结果**,
   * 要把完整 `CommandResult` 交给页面;同步等待超 `http_sync_max_wait_ms` / `async:true` 时
   * 回 `202 {ok:true, trace_id, accepted:true, pending?}`(R6-52/R6-53),用 `accepted` 判别。
   */
  run: (accountId: string, body: {
    op: string
    args: Record<string, unknown>
    idempotency_key?: string
    confirm?: boolean
    timeout_ms?: number
    async?: boolean
  }) => requestCommand<CommandOutcome>(`/accounts/${accountId}/commands`, {
    method: 'POST', body, idempotencyKey: body.idempotency_key,
  }),

  /** #29 直发(等价 #28 且 `confirm` 恒 true) */
  send: (accountId: string, body: {
    session: string
    text?: string
    image_ref?: string
    file_ref?: string
    idempotency_key: string
    timeout_ms?: number
  }) => requestCommand<CommandOutcome>(`/accounts/${accountId}/send`, {
    method: 'POST', body, idempotencyKey: body.idempotency_key,
  }),

  /** #31 按 trace_id 回查一条指令与结果 */
  get: (accountId: string, traceId: string) =>
    request<{ command: Record<string, unknown>; result: CommandResult }>(
      `/accounts/${accountId}/commands/${traceId}`,
    ),

  /** #36 广播:必须显式勾选账号,没有「全部」 */
  broadcast: (body: {
    account_ids: string[]
    op: string
    args: Record<string, unknown>
    idempotency_key: string
    confirm?: boolean
  }) => requestCommand<{ broadcast_id: string; results: Record<string, CommandResult> }>(
    '/broadcast/commands', { method: 'POST', body, idempotencyKey: body.idempotency_key },
  ),

  /**
   * #37 广播汇总回查。🔴 `results`/`counts` **只含当前令牌有权的账号**(受限令牌看到的 `total`
   * 会比发起时小,这是有意的,backend-api-2 §6)—— 界面别把它当「有账号丢了」。
   */
  broadcastGet: (broadcastId: string) =>
    request<{
      broadcast_id: string
      op: string
      actor?: string
      submitted_at?: string
      counts: { total: number; ok: number; failed: number }
      results: Record<string, CommandResult>
    }>(`/broadcast/${broadcastId}`),

  /** #24 机型档案库(字段名统一 `profile_key`,带 `release/weight`) */
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

  /** #27b 单会话详情(02 只给了 PATCH 半,后端补的只读兄弟端点,待文档方登记) */
  session: (accountId: string, sessionId: string) =>
    request<SessionRow>(`/accounts/${accountId}/sessions/${sessionId}`),

  /**
   * #27 单会话设置。🔴 键集**只有这四个**,多一个键后端就 `400 bad_field`;
   * `retention_days > 30` 同样 400(E-18,与 #22 同判据)。
   */
  patchSession: (accountId: string, sessionId: string, body: {
    muted?: boolean
    capture_text?: boolean | null
    retention_days?: number | null
    media_policy?: string | null
  }) => request<SessionRow>(`/accounts/${accountId}/sessions/${sessionId}`, { method: 'PATCH', body }),

  list: (q: MessageQuery) => requestList<Message>('/messages', { query: q as Record<string, unknown> }),

  /** #55 取媒体一律按 sha256,`ref` 只用于显示 */
  media: (sha256: string) => requestBinary(`/media/${sha256}`),

  /**
   * #50 按消息 + 媒体**下标**取(路径参数是 `{idx}` = 媒体在 `media_json` 里的下标,不是 media_id;
   * 手上只有 media_id 就走 #55)。二进制 + `X-QT-Media-Id`/`X-QT-Sha256` 头。
   * 🔴 `pending:true`(HTTP 202)= 懒下载还没完 ⇒ 轮询本端点或等 `message` 事件的 `media[].state`,
   * **不是错误**;`413` = 超限未下载,`410` = 已过保留期。
   */
  mediaOfMessage: (messageId: string, idx: number) =>
    requestBinary(`/messages/${messageId}/media/${idx}`),

  /**
   * #53 语音转文字。🔴 回的 `trace_id` 是**作业 trace,不是指令 trace**
   * (`voice_to_text` 本期不走总线,backend-api-2 §7-8)——
   * 拿它去 `#31 GET /accounts/{id}/commands/{trace_id}` 查**是查不到的**,
   * 结果要等 `message` 事件回填的 `asr_text`/`asr_state`。
   * 执行体未装配时 `503 NOT_READY` `reason=asr_backend_missing`。
   */
  asr: (messageId: string) =>
    request<{ trace_id?: string }>(`/messages/${messageId}/asr`, { method: 'POST' }),

  /**
   * #54 消息清除(danger,admin 级)。🔴 **须 `confirm:true`**,缺则 400 且一行都不删;
   * `mode:'text_only'` 只清正文、`'all'` 删行并减媒体 refcount。回 `202 {job_id}`,进度走 #107。
   */
  purge: (body: { account_id?: string; before?: string; mode: 'all' | 'text_only' }) =>
    request<{ job_id: string }>('/messages/purge', { method: 'POST', body: { ...body, confirm: true } }),

  /**
   * #51 异步导出 → `202 {job_id}`。
   * 🔴 入参逐字按 02 #51:`{fmt:'jsonl|csv|eml', with_media:'none|zip', filter:{…#48 的过滤}}`
   * (原先写的 `format`/`include_media` 两个键在 docs 里不存在)。
   */
  export: (body: { filter: MessageQuery; fmt: 'jsonl' | 'csv' | 'eml'; with_media: 'none' | 'zip' }) =>
    request<{ job_id: string }>('/messages/export', { method: 'POST', body }),
}

/* ───────────────── 导出产物(#52) ───────────────── */

export const exportsApi = {
  /**
   * #52 导出作业状态。`download_url` 是**相对路径**(`/api/v1/exports/<job_id>/file`),
   * `status !== 'succeeded'` 时它是 `null`;`expires_at` 后端当前恒 `null`(交接 §7-6,待补写点)。
   * 只许取本人作业:非 admin 取别人的 `403 job_not_owned`。
   */
  status: (jobId: string) =>
    request<{
      job_id: string
      kind: string
      status: string
      rows: number | null
      bytes: number | null
      download_url: string | null
      expires_at: string | null
    }>(`/exports/${jobId}`),

  /** #52 取产物本体(二进制;后端对 `file_path` 做了 realpath 越界防护,越界 `403 path_escape`) */
  file: (jobId: string) => requestBinary(`/exports/${jobId}/file`),
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
  /** #43 `{args, idempotency_key?}` → `202 {run_id}`;幂等键进 **body**(E-02) */
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
  /**
   * #70 新增向导预检。
   * 🔴 总控裁决③:`can_add` 是 **bool**(「这个通道现在还能不能再加一个」);
   * #69 `ResourcePool.can_add` 才是**数量**,同名不同义,不要互相赋值。
   */
  precheck: (channel: Channel) =>
    request<{ can_add: boolean; reason?: string; alternatives?: unknown[] }>('/resources/precheck', {
      method: 'POST', body: { channel },
    }),
}

/* ───────────────── 系统 / 环境(一律经 Agent,C-32) ───────────────── */

export const systemApi = {
  /**
   * #73。总控裁决①:`agent` 的形状**以后端现实现为准** = `{version}`(不是裸字符串);
   * `kernel/wsl/docker` 后端当前可能恒 `null` —— 页面按「未知」渲染,不写死。
   */
  version: () => request<SystemVersion>('/system/version'),

  /**
   * #72。🔴 **两种形态(免鉴权摘要、带令牌全量)都不带 `trace_id`,这是规格口径**
   * (总控 2026-09-21 确认;02 §3.4 通用段 R6-62 (b) 的例外①)——
   * 前端**不要**据此报错或告警,也不要把它当成「后端漏注入」。
   * 需要 trace 时用客户端回落的本地 ULID(`http.ts` 的 `recordTrace`)。
   */
  health: () => request<SystemHealth>('/system/health'),
  env: () => request<SystemEnv>('/system/env'),

  /**
   * #86 合规告知文案与版本(免鉴权 loopback)。
   * 真后端顺带回了 `ack_ms/acked_at/acked_version` —— 「这一版勾过没有」就从这里读,
   * 不必再去猜(#88 的 group 枚举里没有 compliance 组)。
   */
  notice: () => request<{
    notice_version: string
    text: string
    ack_ms?: number | null
    acked_at?: string | null
    acked_version?: string | null
  }>('/system/notice'),
  /** #87 勾选合规告知 → 写 `settings compliance.ack_ms / compliance.notice_version` */
  noticeAck: (noticeVersion: string) =>
    request<{ ok: boolean }>('/system/notice/ack', { method: 'POST', body: { notice_version: noticeVersion } }),

  /** #76 `?kind=result`(缺省):探测结论 */
  probes: (kind?: 'result' | 'observed') =>
    requestList<ProbeRow>('/system/probes', { query: kind ? { kind } : undefined }),

  /**
   * #76 `?kind=observed`:实测采样候选。
   * 🔴 R6-58 (dc) 出参**两键** `{data, targets}` —— `targets` 是当前正式探测目标,
   * 行里带 `id` 与 `in_config`;丢掉 `targets` 就做不出 01 §2.7.9「默认只勾新增项」。
   */
  observedProbes: () =>
    requestEnvelope<ObservedProbeRow[]>('/system/probes', { query: { kind: 'observed' } })
      .then((env) => ({
        items: (env.data as ObservedProbeRow[]) ?? [],
        targets: (env.targets as string[]) ?? [],
      })),
  probe: (targets?: string[]) =>
    request<{ run_id: string; results: ProbeRow[] }>('/system/probe', {
      method: 'POST', body: { targets, trigger: 'manual' },
    }),
  /** C-1 实测采样 */
  probeSample: (durationS = 30) =>
    request<{ sampled_at: string; rows: SampleRow[] }>('/system/probe', {
      method: 'POST', body: { mode: 'sample', duration_s: durationS },
    }),
  /**
   * #76b 把采样行采纳为正式探测目标(经 Agent 转 WinAgent,控制台不直调 /wa/v1/probes)。
   * 🔴 R6-58 (z)/(db):`observed_ids` 是**采纳后的全集、不是增量** —— 不在集合里的已采纳行会被取消采纳,
   * `[]` 合法 = 清空全部采纳;传 `targets` 一律 `400 use_observed_ids`。
   */
  adoptProbeTargets: (observedIds: number[]) =>
    request<AdoptProbeResult>('/settings/probe', {
      method: 'PUT', body: { observed_ids: observedIds },
    }),

  /** #78 起一轮自检 → `202 {run_id}` */
  selftestRun: () => request<{ run_id: string }>('/system/selftest', { method: 'POST' }),
  /**
   * #79b `GET /system/selftest[?run_id=]` —— 不带 `run_id` 取**最近一轮**。
   * 🔴 从没跑过时后端回 `{ok:true, data:null, run_id:null}`(不是 404):
   * 页面要显示「暂无」,不能弹红。
   */
  selftestResult: (runId?: string) =>
    requestEnvelope<SelftestRun | SelftestRow[] | null>('/system/selftest', {
      query: runId ? { run_id: runId } : undefined,
    }).then((env) => ({
      // #79 的 data 是**一轮的对象**(`{redroid_boot_ms, napcat_ok, winagent_ok, probes}`);
      // 01 §4 的自检表要行 ⇒ 在 selftestRows 里派生,没跑过的项一律 warn + 原因,不假装 ok
      items: selftestRows(env.data as SelftestRun | SelftestRow[] | null),
      run: (Array.isArray(env.data) ? null : (env.data as SelftestRun | null)) ?? null,
      runId: (env.run_id as string | null) ?? null,
    })),
  diagnostics: (withScreenshots: boolean) =>
    request<{ job_id: string }>('/system/diagnostics', { method: 'POST', body: { with_screenshots: withScreenshots } }),
  /**
   * #85 写 docker 代理。真后端在有账号在跑时回 `{pending:true, reason:'accounts_running', running:[…]}`
   * (不立刻应用),两种出参都要接住。
   */
  dockerProxy: (enable: boolean) =>
    request<{
      applied?: boolean
      proxy?: string | null
      pending?: boolean
      reason?: string | null
      running?: string[]
    }>('/system/docker-proxy', { method: 'POST', body: { enable } }),
  /** C-03:Agent 自 drain 后调 WinAgent;基线 §11-6 绝不自动 */
  wslRestart: () =>
    request<{ ok: boolean }>('/system/wsl-restart', { method: 'POST', body: { mode: 'shutdown', confirm: true } }),
  publicEndpoint: (refresh = false) =>
    request<PublicEndpoint>('/system/public-endpoint', { query: refresh ? { refresh: true } : undefined }),

  /**
   * #82 排空(升级前用)。🔴 **之后整个控制台的写操作都会 `503` `reason=draining`** —— 这是预期,
   * 提示语要写「正在为升级排空」而不是「后端故障」;**后端没有逆操作端点**(交接 §7-3),
   * 恢复受理只能重启 Agent。界面必须在按下前把这句话说清楚。
   */
  drain: (timeoutS = 30) =>
    request<{
      drained: boolean
      inflight: number
      inflight_before: number
      waited_s: number
      stopped_accounts: string[]
    }>('/system/drain', { method: 'POST', body: { timeout_s: timeoutS } }),

  /** #83 优雅停机:须 `confirm:true`,缺则 400。回 `202` 之后控制台会断开 */
  shutdown: () =>
    request<{ accepted?: boolean; stopping?: boolean }>('/system/shutdown', {
      method: 'POST', body: { confirm: true },
    }),
}

/* ───────────────── 邮件 ───────────────── */

export const mailApi = {
  /** #56。两形都接住(后端当前把它做成了列表端点),见 `normalizeMailStatus` 的说明 */
  status: () => requestEnvelope<unknown>('/mail/status').then((env) => normalizeMailStatus(
    env.data !== undefined ? env.data : { enabled: env.enabled, routes: env.routes, route: env.route },
  )),
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
  /**
   * `GET /mail/hmac-keys`:短名表(R6-58 (ac) 指名的唯一来源;`senders[].shortname` 不随
   * `GET /settings/mail` 下发)。**只回短名与发件人,不回密钥**。
   */
  hmacKeys: () => requestList<{ sender: string; short_name: string; created_at?: string }>('/mail/hmac-keys'),

  /**
   * #67 入参只有 sender + short_name,**无 route**(短名全局唯一)。
   * `secret` 同 #91 是**一次性明文**,走信封取法(N-1 同型)。
   */
  createHmacKey: async (sender: string, shortName: string) => {
    const env = await requestEnvelope<{ secret?: string }>('/mail/hmac-keys', {
      method: 'POST', body: { sender, short_name: shortName },
    })
    return { secret: pickOnceSecret(env, 'secret'), traceId: env.trace_id ?? '' }
  },
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

  /**
   * #89 整组替换。🔴 走 `requestEnvelope` —— 这个端点在 `data`(组值)**之外**还有四个业务键
   * `restart_required` / `config_written` / `warnings` / `secret_refs`(真后端实测),
   * 用 `request()` 的「有 data 就返回 data」会把它们**静默丢掉**(与 N-1 同型)。
   * 丢了 `restart_required`,界面就说不出「改完要重启 Agent 才生效」这句最关键的话。
   */
  put: async <T = Record<string, unknown>>(group: string, body: Record<string, unknown>) => {
    const env = await requestEnvelope<T>(`/settings/${group}`, { method: 'PUT', body })
    return {
      data: (env.data as T | undefined) ?? null,
      restartRequired: env.restart_required === true,
      configWritten: env.config_written === true,
      warnings: Array.isArray(env.warnings) ? (env.warnings as string[]) : [],
      secretRefs: (env.secret_refs ?? null) as Record<string, string> | null,
    }
  },

  apiClients: () => requestList<ApiClientRow>('/settings/api-clients'),

  /**
   * #91 新建 API 客户端。
   * 🔴 **一次性明文令牌**(N-1):走 `requestEnvelope` 拿完整信封,再用 `pickOnceSecret()` 挑键。
   * 成功响应是 **R6-55 顶层平铺**(`{ok, app_id, …行字段, token, trace_id}`,**无 `data`**);
   * `request()` 的「有 `data` 就返回 `data`」会把顶层的 `token`/`app_id` 静默丢掉,
   * 而令牌**只下发这一次**,丢了只能删了重建。`token` 为 `null` 说明后端这一版没下发,
   * 页面必须明说「本次没拿到明文,请吊销后重建」,不许显示一个空框假装成功。
   */
  createApiClient: async (body: Record<string, unknown>) => {
    const env = await requestEnvelope<ApiClientRow>('/settings/api-clients', { method: 'POST', body })
    // 🔴 最终形状 = 顶层平铺(总控 2026-09-21 裁决):`data` 是空的,行就在信封顶层,
    // 摘掉信封键与明文键即是。读 `env.data` 的那一路仅作防御,保留。
    const { ok: _ok, code: _c, error: _e, trace_id: _t, next_cursor: _n, token: _tk, secret: _s, data: _d, ...flatRow }
      = env as Record<string, unknown>
    const row = (env.data as ApiClientRow | undefined)
      ?? (typeof flatRow.app_id === 'string' ? (flatRow as unknown as ApiClientRow) : null)
    return {
      row,
      // app_id 同样两形兼容(收口后只留 data 那一路)
      appId: row?.app_id ?? (typeof env.app_id === 'string' ? env.app_id : null),
      token: pickOnceSecret(env, 'token') ?? pickOnceSecret(env, 'secret'),
      traceId: env.trace_id ?? '',
    }
  },

  /** #92 轮换:同样是一次性明文,取法同 #91;`grace_minutes` 是旧凭据宽限期 */
  rotateApiClient: async (appId: string) => {
    const env = await requestEnvelope<ApiClientRow>(`/settings/api-clients/${appId}/rotate`, { method: 'POST' })
    const data = (env.data ?? {}) as Record<string, unknown>
    const grace = data.grace_minutes ?? env.grace_minutes
    return {
      token: pickOnceSecret(env, 'token') ?? pickOnceSecret(env, 'secret'),
      graceMinutes: typeof grace === 'number' ? grace : null,
      traceId: env.trace_id ?? '',
    }
  },

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

  /**
   * #102 的落点逐字 = **`PUT /settings/api {public_domain}`**
   * (`PUT /settings/public-endpoint` 在 docs 里不存在,后端也没有)。
   * 🔴 #89 是**整组替换**:先把当前 `api` 组读回来再并上 `public_domain`,否则会把整组打回默认。
   */
  putPublicDomain: async (publicDomain: string) => {
    const cur = await request<Record<string, unknown>>('/settings/api')
    const env = await requestEnvelope<Record<string, unknown>>('/settings/api', {
      method: 'PUT', body: { ...cur, public_domain: publicDomain },
    })
    return { data: env.data ?? null, restartRequired: env.restart_required === true }
  },
  webhooks: () => requestList<{ id: string; url: string; enabled: boolean }>('/settings/webhooks'),
  addWebhook: (url: string) => request<{ id: string }>('/settings/webhooks', { method: 'POST', body: { url } }),
  removeWebhook: (id: string) => request<{ ok: boolean }>(`/settings/webhooks/${id}`, { method: 'DELETE' }),

  /* 🔴 `/settings/compliance` 在 02 #88 的 group 枚举里**不存在** —— 合规告知(01-P3)走
     #86 `GET /system/notice` + #87 `POST /system/notice/ack`,见 `systemApi.notice/noticeAck`。 */
}

/* ───────────────── 审计 ───────────────── */

export const auditApi = {
  /**
   * #95。🔴 服务端**只认** `kind|actor|account_id|action|since|until|limit|cursor|fmt`
   * (02 #95 参数列)。未知参数会被静默忽略 —— 把 `op`/`code`/`trace_id` 当查询串发出去,
   * 筛选器会「假装生效」。这三项改在客户端本地过滤,见 `stores/audit.ts`。
   */
  list: (q: {
    kind?: 'command' | 'api' | 'system' | 'stream_input'
    account_id?: string
    action?: string
    actor?: string
    since?: string
    until?: string
    limit?: number
    cursor?: string
  }) => requestList<AuditRow>('/audit', { query: q as Record<string, unknown> }),
}

/* WinAgent 能力不在这里:渲染进程被 webRequest 阻断,一律经 qt.wa.invoke → 主进程(§2.5 白名单)。 */
