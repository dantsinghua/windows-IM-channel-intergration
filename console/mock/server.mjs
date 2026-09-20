/**
 * 开发用 mock 后端 —— 按 02 §3.4 的信封回假数据 + WS `/api/v1/events` 事件流。
 * 只为 `npm run dev:web` 能在浏览器里独立跑起来,**不是** Agent 的实现。
 *
 * 🔴 **口径:mock 向真 Agent 与 02 §3.4 看齐,不是反过来。**
 * 一次端到端联调(见 `.omc/handoffs/e2e-console-agent.md`)暴露出:mock 因为不校验版本头、
 * 不校验幂等键、信封与字段名自成一套,把前端养出了一堆只在 mock 下成立的假设。
 * 因此本文件现在:
 *  - 校验 `X-QT-Api-Min`(> `API_VERSION` 即 `426 UPGRADE_REQUIRED`);
 *  - 写端点校验 **body 里的 `idempotency_key`**,重放回 `409 IDEMPOTENT_REPLAY`;
 *  - `#28` 回顶层平铺的 `CommandResult`,并能造出 `ok:false` 的业务结果码;
 *  - 审计、邮件设置、公网端点、探测 observed 等一律按 02 的键集。
 * 形状由 `console/tests/unit/mock-shape.spec.ts` 守着(改这里会先让那条测试变红)。
 *
 * 监听 127.0.0.1:17600(与 [endpoint] agent 一致),vite dev server 把 /api/v1 代理过来。
 */
import { createServer } from 'node:http'
import { WebSocketServer } from 'ws'
import { CAPABILITIES, makeAccounts, makeMessages, makeMetrics, makeResources, now } from './data.mjs'

const PORT = Number(process.env.MOCK_PORT ?? 17600)

/** 与 `docs/07` `[api] api_version="1.0"` 同值 —— 前端的 `X-QT-Api-Min` 必须 ≤ 它 */
const API_VERSION = '1.0'

/** `X-QT-Api-Min: a.b` 是否被本 mock 满足(§3.8:主版本须相同、次版本不得更高) */
function apiMinSatisfied(want) {
  if (!want) return true
  const [wMajor, wMinor] = String(want).split('.').map((x) => Number(x))
  const [hMajor, hMinor] = API_VERSION.split('.').map((x) => Number(x))
  if (!Number.isFinite(wMajor) || !Number.isFinite(wMinor)) return true
  return wMajor === hMajor && wMinor <= hMinor
}

const state = {
  accounts: makeAccounts(),
  resources: makeResources(),
  metrics: makeMetrics(),
  ...makeMessages(60),
  jobs: new Map(),
  seq: 0,
  // 🔴 各组的键名 = 02 §3.9 配置总表 / `docs/07`(真后端回的就是 AgentConfig 的字段名)
  settings: {
    api: {
      bind: '127.0.0.1', port: 17600, ws_impl: 'websockets', rate_default_per_min: 120,
      http_sync_max_wait_ms: 25000, unauth_health_sources: ['127.0.0.1/32', '::1/128', 'wsl_gateway'],
      public_ip_probe_urls: ['https://api.ipify.org'], public_ip_check_interval_s: 600,
      public_domain: '', api_version: '1.0',
    },
    retention: {
      messages_days: 30, media_days: 7, files_days: 7, raw_days: 7, mail_archive_days: 7,
      mail_inbox_rows_days: 30, commands_days: 30, audit_days: 30, idempotency_days: 7,
      export_jobs_days: 7, health_raw_h: 48, health_1m_d: 7, health_1h_d: 30,
      disk_warn_mb: 5120, disk_high_mb: 2048, disk_critical_mb: 1024,
      cleanup_at: '03:00', cleanup_batch: 500,
    },
    resources: {
      pools: {
        wsl: { total_mb: 11264, reserved_mb: 2048 },
        windows: { total_mb: 16384, reserved_mb: 4096, wechat_mb: 1536 },
      },
      quota_mb: { qidian: 2560, qq: 614, wechat: 1536 },
    },
    // 🔴 M-6:02 #88 的 group 枚举逐字含 runtime / pool / events / log 四组(mock 原先 404)
    runtime: { adb_connect_timeout_s: 10, ui_action_timeout_ms: 8000, screenshot_format: 'png' },
    pool: { max_parallel_commands: 4, queue_max: 64, idle_stop_minutes: 0 },
    events: { outbox_max_rows: 5000, replay_window_rows: 2000, ws_send_timeout_ms: 5000 },
    log: { level: 'INFO', rotate_mb: 64, keep_files: 7, body_logging: false },
    asr: { endpoint: 'http://10.0.0.8:9000/asr', concurrency: 2, min_confidence: 0.6 },
    ocr: { engine: 'offline', model_dir: '/opt/qtrade/ocr', min_conf: 0.8, lang: 'zh' },
    // 🔴 R6-58 (ac):`mail` 组逐字四键 + scopes 每块 {override, route_id, enabled, inbound, outbound}
    mail: {
      enabled: true,
      require_signature: true,
      template_version: 'v1',
      scopes: {
        default: {
          override: true,
          route_id: 'r_default',
          enabled: true,
          inbound: {
            protocol: 'imap', host: 'imap.163.com', port: 993, ssl: true, user: 'ops@corp',
            // 密码类只写不读:读回来只有 *_ref
            secret_ref: 'vault://mail/route/r_default/imap',
            folders: ['INBOX'], processed_folder: 'QTrade/processed', poll_interval_s: 60,
            idle: false, keep_raw: true,
            allowed_senders: ['ops@corp'], require_signature: true,
            allow_ops: ['read_messages', 'list_sessions', 'get_state', 'screenshot', 'send_text', 'send_image'],
            scope_subject_prefix: ['[QTrade]'],
          },
          outbound: {
            enabled: true, host: 'smtp.163.com', port: 465, ssl: true, user: 'ops@corp',
            secret_ref: 'vault://mail/route/r_default/smtp',
            from: 'ops@corp', recipients: ['team@corp'], cc: [],
            send_rate_per_min: 6, compat_title: true, receipt_to_sender: true,
            template_id: 'tpl-ibquote',
          },
        },
        qidian: { override: false, route_id: null, enabled: true, inbound: {}, outbound: {} },
        qq: { override: false, route_id: null, enabled: true, inbound: {}, outbound: {} },
        wechat: { override: false, route_id: null, enabled: true, inbound: {}, outbound: {} },
      },
    },
  },
  /** #67/#68 的短名表(`GET /mail/hmac-keys`);短名**不随 mail 组下发**(R6-58 (ac)) */
  hmacKeys: [{ sender: 'ops@corp', short_name: 'ops', created_at: now() }],
  /** 建号/工作流的幂等键 → 结果(R6-54:重放回 409 IDEMPOTENT_REPLAY + 同一份 data) */
  idempotency: new Map(),
  /** #76 `?kind=observed` 的候选行(行 id = probe_targets_observed.id) */
  observed: [
    {
      id: 1, account_id: 'qd01', channel: 'qidian', remote_host: null, remote_ip: '14.215.177.39',
      port: 8080, proto: 'TCP', samples: 12, first_seen_at: now(), last_seen_at: now(),
      adopted_at: null, in_config: false,
    },
    {
      id: 2, account_id: 'qq01', channel: 'qq', remote_host: 'msfwifi.3g.qq.com', remote_ip: '58.250.137.36',
      port: 8080, proto: 'TCP', samples: 7, first_seen_at: now(), last_seen_at: now(),
      adopted_at: now(), in_config: true,
    },
  ],
  probeTargets: ['msfwifi.3g.qq.com:8080'],
  webhooks: [],
  selftestRunId: null,
  /** #82 drain 置位后,写操作一律 503 draining(没有 undrain 端点,重启 mock 才恢复) */
  draining: false,
  /** #87 写的 compliance(读侧 02 #88 没有 compliance 组,控制台本地判,见 stores/setup.ts) */
  compliance: { ack_ms: null, notice_version: 'v1' },
  /** 当前告知版本(#86 下发、#87 校验的唯一出处;版本一变就得重新勾选) */
  noticeVersion: 'v1',
  pendingConfirms: [
    {
      id: 'mi_0001', op: 'account_stop', from_addr: 'ops@corp', account_id: 'qd01',
      args_digest: 'a1b2c3d4e5f60718',
      created_at: now(),
      expires_at: new Date(Date.now() + 900_000).toISOString(),
      remaining_ttl_s: 900,
    },
  ],
}

const sockets = new Set()

function nextSeq() { return ++state.seq }

/** #79/#79b 的一轮自检对象(两个端点同一份,免得两处各写一份走形) */
function selftestRun(runId) {
  return {
    run_id: runId,
    redroid_boot_ms: 8200,
    napcat_ok: true,
    winagent_ok: true,
    winagent_version: '1.0.2-mock',
    probes: [
      { side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null },
      { side: 'wsl', target: 'mail_pop3', status: 'SKIPPED', level_reached: 'none', detail: '目标未配置' },
    ],
    started_at: now(),
    finished_at: now(),
    skipped: [],
  }
}

function emit(event, payload, extra = {}) {
  const frame = JSON.stringify({ event, ts: now(), seq: nextSeq(), payload, ...extra })
  for (const ws of sockets) { try { ws.send(frame) } catch { /* 客户端已走 */ } }
}

function ok(res, data, extra = {}, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'X-QT-Api-Version': API_VERSION,
    'X-QT-Agent-Version': '1.0.3-mock',
  })
  res.end(JSON.stringify({ ok: true, data, trace_id: `01MOCK${Date.now()}`, ...extra }))
}

/** 顶层平铺 + ok 的端点(R6-55:#7/#9/#10/#19/#69/#72/#28) */
function flat(res, obj, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'X-QT-Api-Version': API_VERSION,
  })
  res.end(JSON.stringify({ ok: true, ...obj, trace_id: `01MOCK${Date.now()}` }))
}

/** 错误信封(00 §10):`code` 在**顶层**,`error` 四键齐全 */
function fail(res, status, code, message, extra = {}) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'X-QT-Api-Version': API_VERSION,
  })
  res.end(JSON.stringify({
    ok: false, code,
    error: { message, retryable: status >= 500, needs_human: false, ...extra },
    trace_id: `01MOCK${Date.now()}`,
  }))
}

/**
 * `409 IDEMPOTENT_REPLAY`:错误信封 + `data` 回首次的结果(02 #2 R6-54)。
 * 用在**响应列是 §7 对象名**的端点(#2 建号 ⇒ `data` 是同一个 Account)。
 */
function replay(res, code, message, data) {
  res.writeHead(409, {
    'Content-Type': 'application/json; charset=utf-8',
    'X-QT-Api-Version': API_VERSION,
  })
  res.end(JSON.stringify({
    ok: false, code, data,
    error: { message, retryable: false, needs_human: false },
    trace_id: `01MOCK${Date.now()}`,
  }))
}

/**
 * 🔴 M-1:`#28` 的 `409 IDEMPOTENT_REPLAY` **响应体仍是完整 `CommandResult`,顶层平铺**
 * (02 #28 R6-52 逐字 +「R6-55:#28 的 CommandResult 本身就是信封」)——
 * 包进 `data` 会让前端 `ApiFailure.envelope` 里取不到首次结果。
 */
function replayCommand(res, code, message, result) {
  res.writeHead(409, {
    'Content-Type': 'application/json; charset=utf-8',
    'X-QT-Api-Version': API_VERSION,
  })
  res.end(JSON.stringify({
    ...result,
    ok: false, code,
    error: { message, retryable: false, needs_human: false },
  }))
}

/**
 * 写端点的幂等键校验(02 §3.4 #2/#28/#43;E-02)。
 * 返回 `null` 表示放行;否则已经把响应写完了。
 */
function requireIdempotencyKey(res, body, path) {
  const key = body?.idempotency_key
  if (typeof key === 'string' && key && key.length <= 128) return null
  fail(res, 400, 'INVALID_ARGS', `POST ${path} 必带 idempotency_key(≤128 字符)`, {
    reason: 'idempotency_key_required',
    details: [{ pointer: '/idempotency_key', message: '必填' }],
  })
  return true
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = ''
    req.on('data', (c) => { raw += c })
    req.on('end', () => {
      try { resolve(raw ? JSON.parse(raw) : {}) } catch { resolve({}) }
    })
  })
}

function acct(id) { return state.accounts.find((a) => a.id === id) }

function pushAccountState(a) {
  emit('account_state', {
    state: a.state, state_code: a.state_code, state_reason: a.state_reason,
    error_since_ms: a.error_since_ms, enabled: a.enabled, runtime: a.runtime,
    capabilities: a.capabilities, self_nick: a.self_nick,
    prompt: a.state === 'login_required'
      ? { kind: a.state_code, text: '请在画面里输入短信验证码', countdown_s: 120 }
      : undefined,
    login_session_id: a.state === 'login_required' ? 'ls_01MOCK' : null,
  }, { account_id: a.id, channel: a.channel })
}

function newJob(kind, resultFactory) {
  const jobId = `job_${Date.now().toString(36)}`
  const job = { job_id: jobId, kind, state: 'running', progress: 10, created_at: now(), updated_at: now() }
  state.jobs.set(jobId, job)
  let p = 10
  const t = setInterval(() => {
    p += 30
    job.progress = Math.min(100, p)
    job.updated_at = now()
    if (p >= 100) {
      job.state = 'succeeded'
      job.result = resultFactory()
      clearInterval(t)
    }
    emit('job', { ...job })
  }, 700)
  return jobId
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${PORT}`)
  const p = url.pathname.replace(/^\/api\/v1/, '')
  const m = req.method ?? 'GET'
  const body = m === 'GET' || m === 'DELETE' ? {} : await readBody(req)

  res.setHeader('Access-Control-Allow-Origin', '*')
  res.setHeader('Access-Control-Allow-Headers', '*')
  res.setHeader('Access-Control-Allow-Methods', '*')
  if (m === 'OPTIONS') { res.writeHead(204); res.end(); return }

  // ── 版本协商(02 §3.8):控制台声明的最低次版本高于本端 ⇒ 426,像真 Agent 一样把请求挡住
  const wantMin = req.headers['x-qt-api-min']
  if (!apiMinSatisfied(wantMin)) {
    return fail(res, 426, 'UPGRADE_REQUIRED', `需要 API ${wantMin},当前 ${API_VERSION}`, {
      needs_human: true,
    })
  }

  /*
   * 🔴 #82 之后:停止受理**新指令**(02 #82 逐字「停接新指令」)。
   * 实测真后端的拦截面 = 总线指令类(`/commands`、`/broadcast/commands`、账号动作 start/stop/
   * restart/enable/disable/logout);**设置类写端点、建号、`/messages/purge` 不被拦**。
   * mock 照这个范围来 —— 拦宽了会让页面开发以为「drain 后什么都不能写」,那同样是误导。
   */
  const COMMAND_LIKE = /^\/(accounts\/[^/]+\/(commands|send|start|stop|restart|enable|disable|logout)|broadcast\/commands|workflows\/[^/]+\/run)$/
  if (state.draining && m !== 'GET' && COMMAND_LIKE.test(p)) {
    return fail(res, 503, 'NOT_READY', 'Agent 正在排空(#82 drain),已停止受理新指令', {
      reason: 'draining', retryable: true, needs_human: false,
    })
  }

  // ── 账号
  if (p === '/accounts' && m === 'GET') return ok(res, state.accounts, { next_cursor: null })
  if (p === '/accounts' && m === 'POST') {
    // E-02:幂等键在 **body**,不带就 400(与真 Agent 同判据)
    if (requireIdempotencyKey(res, body, '/accounts')) return
    const idemKey = `accounts:${body.idempotency_key}`
    const replayed = state.idempotency.get(idemKey)
    // R6-54:重放 `409 IDEMPOTENT_REPLAY`,**`data` 是同一个 Account**(不重复建号、不消耗 seq)
    if (replayed) return replay(res, 'IDEMPOTENT_REPLAY', '同一幂等键已建过号', replayed)
    const seq = state.accounts.filter((a) => a.channel === body.channel).length + 1
    const prefix = { qidian: 'qd', qq: 'qq', wechat: 'wx' }[body.channel]
    const a = {
      id: `${prefix}${String(seq).padStart(2, '0')}`, channel: body.channel, host: body.channel === 'wechat' ? 'windows' : 'wsl',
      label: body.label, state: 'provisioning', state_code: '', state_reason: '', error_since_ms: null,
      enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, identity: {},
      login: { mode: body.login?.mode ?? 'password', remember: !!body.login?.remember },
      capabilities: [], quota_mb: state.resources.quota_mb[body.channel], created_at: now(), updated_at: now(),
      last_seen_at: null,
    }
    state.accounts.push(a)
    state.idempotency.set(idemKey, a)
    setTimeout(() => {
      a.state = 'login_required'
      a.state_code = body.channel === 'qq' ? 'WAIT_QRCODE' : 'WAIT_SMS'
      pushAccountState(a)
    }, 1200)
    // #2 成功是 201 Account
    return ok(res, a, {}, 201)
  }
  let mm
  if ((mm = /^\/accounts\/([^/]+)$/.exec(p))) {
    const a = acct(mm[1])
    if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    if (m === 'GET') return ok(res, a)
    if (m === 'PATCH') { Object.assign(a, body); pushAccountState(a); return ok(res, a) }
    if (m === 'DELETE') { a.deleted_ms = Date.now(); a.state = 'stopped'; return flat(res, { deleted: true, data_kept: true }) }
  }
  if ((mm = /^\/accounts\/([^/]+)\/(start|stop|restart|enable|disable)$/.exec(p)) && m === 'POST') {
    const a = acct(mm[1]); if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    const next = { start: 'starting', stop: 'stopping', restart: 'starting', enable: 'stopped', disable: 'disabled' }[mm[2]]
    a.state = next
    pushAccountState(a)
    setTimeout(() => {
      a.state = { starting: 'running', stopping: 'stopped', stopped: 'stopped', disabled: 'disabled' }[next] ?? next
      pushAccountState(a)
    }, 1500)
    return flat(res, { state: next }, 202)
  }
  if ((mm = /^\/accounts\/([^/]+)\/prompt$/.exec(p))) {
    const a = acct(mm[1])
    if (!a || a.state !== 'login_required') return flat(res, { kind: null, login_session_id: null })
    // R6-56:响应顶层另带 login_session_id(prompt 对象本身不变)
    return flat(res, { kind: a.state_code, text: '请完成验证', countdown_s: 90, login_session_id: 'ls_01MOCK' })
  }
  if ((mm = /^\/accounts\/([^/]+)\/login\/cancel$/.exec(p)) && m === 'POST') {
    const slot = state.resources.pools.windows.wechat_slots
    const same = body.login_session_id && body.login_session_id === slot.pending_login_session_id
    if (!same) return flat(res, { cancelled: false, stale: true, current_login_session_id: slot.pending_login_session_id })
    slot.pending = ''; slot.pending_expires_at = null; slot.pending_login_session_id = ''
    emit('resource', state.resources)
    return flat(res, { cancelled: true, stale: false })
  }
  if ((mm = /^\/accounts\/([^/]+)\/login$/.exec(p)) && m === 'POST') {
    const a = acct(mm[1]); if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    a.state = 'logging_in'; pushAccountState(a)
    setTimeout(() => { a.state = 'running'; a.state_code = ''; pushAccountState(a) }, 1500)
    return flat(res, { state: 'logging_in', login_session_id: 'ls_01MOCK' }, 202)
  }
  if ((mm = /^\/accounts\/([^/]+)\/capabilities$/.exec(p))) {
    const a = acct(mm[1])
    const matrix = {}
    for (const c of CAPABILITIES) matrix[c.op] = c.channels[a?.channel ?? 'qidian']
    return flat(res, { capabilities: a?.capabilities ?? [], matrix })
  }
  if ((mm = /^\/accounts\/([^/]+)\/purge$/.exec(p)) && m === 'POST') {
    // #8:`confirm` 要逐字等于账号 id;须先 #7 软删
    const a = acct(mm[1])
    if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    if (String(body.confirm ?? '') !== mm[1]) {
      return fail(res, 400, 'INVALID_ARGS', 'confirm 须逐字等于账号 id', {
        reason: 'confirm_mismatch', details: [{ pointer: '/confirm', message: mm[1] }],
      })
    }
    if (!a.deleted_ms) {
      return fail(res, 409, 'NOT_APPLICABLE', '请先软删该账号(#7)再彻底删除', { reason: 'not_soft_deleted' })
    }
    return flat(res, { job_id: newJob('account_purge', () => ({
      account_id: mm[1], deleted: { messages: 0, sessions: 0, cursors: 0, media_deref: 0 },
      dir_removed: true, vault_removed: true, freed_mb: 12.5,
    })) }, 202)
  }
  // #16 登出:🔴 通道分界(QQ 无此概念 409 / 企点本期无执行体 503 / 微信经 WinAgent 202)
  if ((mm = /^\/accounts\/([^/]+)\/logout$/.exec(p)) && m === 'POST') {
    const a = acct(mm[1])
    if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    if (a.channel === 'qq') {
      return fail(res, 409, 'NOT_APPLICABLE', 'QQ 通道没有「登出」概念(登录态在 qq_data 卷里)', {
        reason: 'channel_no_logout',
      })
    }
    if (a.channel === 'qidian') {
      return fail(res, 503, 'NOT_READY', 'qidian 通道的登出执行体本期未装配', {
        reason: 'logout_backend_missing', retryable: false, needs_human: true,
      })
    }
    return flat(res, { account_id: mm[1], via: 'winagent' }, 202)
  }
  if ((mm = /^\/accounts\/([^/]+)\/export-identity$/.exec(p)) && m === 'POST') {
    return flat(res, { job_id: newJob('identity_export', () => ({ download_url: '/api/v1/exports/mock/file' })) }, 202)
  }
  if (/^\/accounts\/[^/]+\/(credential|settings|runtime\/.+|webui\/.+|stream\/input)$/.test(p)) {
    const a = acct(p.split('/')[2])
    // 🔴 M-11:02 #22 的出参是 **Account**(不是 `{ok, adb_state}`);
    // 裁决④ Account 不带 `settings` 子对象 ⇒ 账号级设置并到顶层。
    if (p.endsWith('/settings') && m === 'PATCH') {
      if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
      Object.assign(a, body)
      a.updated_at = now()
      return ok(res, a)
    }
    if (p.endsWith('/webui/open')) return flat(res, { url: 'http://127.0.0.1:16301/', until: new Date(Date.now() + 600_000).toISOString() })
    // `adb_state` 只属于 #100 runtime/reconnect-adb,别端点不要顺手带
    if (p.endsWith('/runtime/reconnect-adb')) return flat(res, { ok: true, adb_state: 'device' })
    return flat(res, { ok: true })
  }

  // 🔴 M-5:#19 单账号运行态(mock 原先 404)。02 #19 是**平铺**六键 + 通用 `trace_id`。
  if ((mm = /^\/accounts\/([^/]+)\/state$/.exec(p)) && m === 'GET') {
    const a = acct(mm[1])
    if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    return flat(res, {
      state: a.state,
      state_code: a.state_code ?? '',
      state_reason: a.state_reason ?? '',
      error_since_ms: a.error_since_ms ?? null,
      enabled: a.enabled,
      last_seen_at: a.last_seen_at ?? null,
    })
  }
  if (p === '/accounts/switch' && m === 'POST') {
    const slot = state.resources.pools.windows.wechat_slots
    slot.pending = 'wx03'
    slot.pending_expires_at = new Date(Date.now() + 600_000).toISOString()
    slot.pending_login_session_id = 'ls_01MOCKSWITCH'
    emit('resource', state.resources)
    return flat(res, { target: 'wx03' })
  }
  if ((mm = /^\/accounts\/([^/]+)\/switch$/.exec(p)) && m === 'POST') {
    const slot = state.resources.pools.windows.wechat_slots
    slot.pending = mm[1]
    slot.pending_expires_at = new Date(Date.now() + 600_000).toISOString()
    slot.pending_login_session_id = 'ls_01MOCKSWITCH'
    emit('resource', state.resources)
    return flat(res, { holder_before: slot.holder, target: mm[1] })
  }
  if (p === '/accounts/batch' && m === 'POST') {
    const results = {}
    for (const id of body.ids ?? []) results[id] = { ok: true, code: 'OK' }
    return flat(res, { results })
  }
  if (p === '/device-profiles/templates') {
    // #24:字段名统一 `profile_key`,另带 `release/weight`
    return ok(res, [
      { profile_key: 'xiaomi-mi11', brand: 'Xiaomi', model: 'MI 11', release: '13', weight: 5 },
      { profile_key: 'huawei-p40', brand: 'HUAWEI', model: 'P40', release: '12', weight: 3 },
    ], { next_cursor: null })
  }

  // ── 能力 / 指令
  if (p === '/capabilities') return ok(res, CAPABILITIES, { capabilities_version: 'mock-1', next_cursor: null })
  if ((mm = /^\/accounts\/([^/]+)\/commands$/.exec(p)) && m === 'POST') {
    const a = acct(mm[1])
    if (!a) return fail(res, 404, 'TARGET_NOT_FOUND', '账号不存在')
    const text = String(body.args?.text ?? '')
    // R6-48:含控制字符在入口被拒
    if (/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(text)) {
      return fail(res, 400, 'INVALID_ARGS', '正文含不可见控制字符', {
        reason: 'text_has_control_chars', details: [{ pointer: '/text', message: 'control chars' }],
      })
    }
    const isWrite = (CAPABILITIES.find((c) => c.op === body.op)?.kind ?? 'write') !== 'read'
    if (isWrite && requireIdempotencyKey(res, body, `/accounts/${mm[1]}/commands`)) return
    const idemKey = `cmd:${mm[1]}:${body.idempotency_key}`
    const before = state.idempotency.get(idemKey)
    // B-06 / M-1:重放的响应体仍是**完整 CommandResult 且顶层平铺**,`trace_id` 是首次那条
    if (before) return replayCommand(res, 'IDEMPOTENT_REPLAY', '同一幂等键已执行过', before)

    /*
     * 🔴 R6-52:**HTTP 状态说「有没有被受理执行」,结果码说「执行成了没有」**。
     * 业务失败(SEND_FAILED / UNCONFIRMED / GATE_BLOCKED …)一律 `200 + ok:false + CommandResult`,
     * 不是 4xx/5xx —— mock 必须能造出这一支,否则前端永远发现不了自己把它当异常吞了。
     * 用正文里的关键词触发,方便开发时自测。
     */
    let code = body.op?.startsWith('send') ? 'DELIVERED' : 'OK'
    if (text.includes('#fail')) code = 'SEND_FAILED'
    else if (text.includes('#unconfirmed')) code = 'SEND_CALLED_BUT_UNCONFIRMED'
    else if (text.includes('#gate')) code = 'GATE_BLOCKED'
    const okResult = code === 'DELIVERED' || code === 'OK'
    const result = {
      ok: okResult,
      code,
      data: {
        message_id: `msg_${Date.now()}`,
        ext_msg_id: okResult ? `qd:${Date.now()}` : null,
        confirmed_by: code === 'SEND_CALLED_BUT_UNCONFIRMED' ? null : 'ingest_merge',
        confirm_ms: 742,
      },
      cost_ms: code === 'SEND_CALLED_BUT_UNCONFIRMED' ? 15009 : 865,
      trace_id: `01MOCK${Date.now()}`,
      source: 'qidian_db',
      state_before: 'READY',
      state_after: 'READY',
      ...(okResult ? {} : {
        error: {
          message: {
            SEND_FAILED: '发送失败:目标会话不可达',
            SEND_CALLED_BUT_UNCONFIRMED: '已调用发送但未读回确认',
            GATE_BLOCKED: '被发送闸拦下(超过每分钟上限)',
          }[code],
          retryable: code !== 'GATE_BLOCKED',
          needs_human: code === 'GATE_BLOCKED',
        },
      }),
    }
    if (isWrite) state.idempotency.set(idemKey, result)
    emit('command_done', { trace_id: result.trace_id, code: result.code, cost_ms: result.cost_ms })
    // #28 的响应体**本身就是信封**:顶层平铺,不再包一层 data
    res.writeHead(200, {
      'Content-Type': 'application/json; charset=utf-8',
      'X-QT-Api-Version': API_VERSION,
    })
    return res.end(JSON.stringify(result))
  }
  if (p === '/broadcast/commands' && m === 'POST') {
    if (requireIdempotencyKey(res, body, '/broadcast/commands')) return
    const results = {}
    for (const id of body.account_ids ?? []) {
      results[id] = {
        ok: true, code: 'DELIVERED', cost_ms: 700, trace_id: `01MOCK${id}`,
        source: 'qidian_db', state_before: 'READY', state_after: 'READY',
        data: { message_id: `msg_${id}_${Date.now()}` },
      }
    }
    // #36:`200 {broadcast_id, results}` 顶层平铺
    return flat(res, { broadcast_id: 'bc_mock', results })
  }

  // ── 会话 / 消息
  if (p === '/sessions') return ok(res, state.sessions, { next_cursor: null })
  if (p === '/messages') {
    const q = url.searchParams
    let rows = state.messages
    if (q.get('account_id')) rows = rows.filter((x) => x.account_id === q.get('account_id'))
    if (q.get('session_id')) rows = rows.filter((x) => x.session.id === q.get('session_id'))
    if (q.get('dir')) rows = rows.filter((x) => x.dir === q.get('dir'))
    if (q.get('type')) rows = rows.filter((x) => x.type === q.get('type'))
    if (q.get('needs_review') === 'true') rows = rows.filter((x) => x.needs_review)
    if (q.get('q')) rows = rows.filter((x) => (x.text ?? '').includes(q.get('q')))
    // GET /messages 不带事件专属三字段
    return ok(res, rows.slice(0, Number(q.get('limit') ?? 50)), { next_cursor: null })
  }
  // #54 消息清除:danger,须 confirm:true
  if (p === '/messages/purge' && m === 'POST') {
    if (body.confirm !== true) {
      return fail(res, 400, 'INVALID_ARGS', 'messages/purge 是不可逆清理,须带 confirm:true', {
        reason: 'confirm_required', details: [{ pointer: '/confirm', message: '必须为 true' }],
      })
    }
    if (!['all', 'text_only'].includes(String(body.mode))) {
      return fail(res, 400, 'INVALID_ARGS', "mode 只能是 all|text_only", {
        reason: 'bad_field', details: [{ pointer: '/mode', message: 'all|text_only' }],
      })
    }
    return flat(res, { job_id: newJob('messages_purge', () => ({ purged: true })) }, 202)
  }
  // #53 语音转文字:本期无执行体 ⇒ 如实 503(不伪造转写结果)
  if ((mm = /^\/messages\/([^/]+)\/asr$/.exec(p)) && m === 'POST') {
    return fail(res, 503, 'NOT_READY', 'ASR 执行体本期未装配', {
      reason: 'asr_backend_missing', retryable: false, needs_human: true,
    })
  }
  if (p === '/messages/export' && m === 'POST') {
    // #51 入参逐字 `{fmt:'jsonl|csv|eml', with_media:'none|zip', filter}`
    if (!['jsonl', 'csv', 'eml'].includes(String(body.fmt))) {
      return fail(res, 400, 'INVALID_ARGS', 'fmt 只能是 jsonl|csv|eml', {
        reason: 'bad_field', details: [{ pointer: '/fmt', message: 'jsonl|csv|eml' }],
      })
    }
    // 🔴 M-2:02 #51 R6-62 (g) —— `eml` 与 `with_media:'zip'` 本期**明着 400**,
    // 「不静默降级成别的格式」。mock 放行会让页面开发以为这条路通。
    if (String(body.fmt) === 'eml') {
      return fail(res, 400, 'INVALID_ARGS', 'fmt=eml 本期未实现', {
        reason: 'unsupported_fmt', details: [{ pointer: '/fmt', message: 'eml 本期不支持' }],
      })
    }
    if (String(body.with_media ?? 'none') === 'zip') {
      return fail(res, 400, 'INVALID_ARGS', 'with_media=zip 本期未实现', {
        reason: 'unsupported_with_media', details: [{ pointer: '/with_media', message: 'zip 本期不支持' }],
      })
    }
    return flat(res, { job_id: newJob('messages_export', () => ({ download_url: '/api/v1/exports/mock/file' })) }, 202)
  }

  // ── 作业
  if ((mm = /^\/jobs\/([^/]+)$/.exec(p))) {
    const j = state.jobs.get(mm[1])
    if (!j) return fail(res, 404, 'TARGET_NOT_FOUND', '作业不存在')
    // #107 的时间键是 ISO `*_at`(00 §6「API/事件时间一律 ISO 8601 带时区偏移」)
    return ok(res, j)
  }
  if ((mm = /^\/jobs\/([^/]+)\/cancel$/.exec(p)) && m === 'POST') {
    const j = state.jobs.get(mm[1]); if (j) { j.state = 'cancelled'; emit('job', { ...j }) }
    return flat(res, { ok: true })
  }

  // ── 工作流
  if (p === '/workflows') {
    return ok(res, [{
      id: 'wf_daily', name: '每日报价采集', version: 3, steps: 4, enabled: true,
      schedule_cron: '0 9 * * *', last_run_at: now(),
      inputs: [{ key: 'session', label: '会话', required: true }],
    }], { next_cursor: null })
  }
  if ((mm = /^\/workflows\/([^/]+)$/.exec(p))) {
    return ok(res, { id: mm[1], name: '每日报价采集', version: 3, steps: 4, enabled: true, yaml: 'name: 每日报价采集\nsteps:\n  - op: list_sessions\n  - op: read_messages\n' })
  }
  if ((mm = /^\/workflows\/([^/]+)\/run$/.exec(p)) && m === 'POST') {
    // #43 `{args, idempotency_key?}` → `202 {run_id}`;幂等键在 body(E-02)
    if (requireIdempotencyKey(res, body, `/workflows/${mm[1]}/run`)) return
    const idemKey = `wf:${mm[1]}:${body.idempotency_key}`
    const before = state.idempotency.get(idemKey)
    if (before) return replay(res, 'IDEMPOTENT_REPLAY', '同一幂等键已触发过该工作流', before)
    const runId = `run_${Date.now().toString(36)}`
    state.idempotency.set(idemKey, { run_id: runId })
    setTimeout(() => emit('workflow', { run_id: runId, status: 'started' }), 300)
    return flat(res, { run_id: runId }, 202)
  }
  if ((mm = /^\/workflows\/runs\/([^/]+)$/.exec(p))) {
    return ok(res, {
      run_id: mm[1], workflow: '每日报价采集', status: 'running', account_ids: ['qd01'], started_at: now(),
      steps: [
        { step_id: 's1', name: '列会话', op: 'list_sessions', status: 'finished', code: 'OK', cost_ms: 320, state_before: 'READY', state_after: 'READY' },
        { step_id: 's2', name: '读消息', op: 'read_messages', status: 'running' },
      ],
    })
  }
  if (/^\/workflows\/runs\/[^/]+\/cancel$/.test(p)) return flat(res, { ok: true })

  // ── 资源 / 系统
  if (p === '/resources') return flat(res, state.resources)
  // 🔴 裁决③:#70 的 `can_add` 是 **bool**(#69 的同名键才是数量)
  if (p === '/resources/precheck') {
    const n = state.resources.can_add[body.channel] ?? 0
    return flat(res, {
      can_add: n > 0,
      reason: n > 0 ? null : 'pool_exhausted',
      alternatives: n > 0 ? [] : [{ action: 'stop_account', id: 'qd03' }],
    })
  }
  // #71/#25:R6-58 (x) 一律 `202 {job_id}`,结果经 #107 取
  if (p === '/resources/calibrate') {
    return flat(res, { job_id: newJob('resources_calibrate', () => ({ suggestions: { qidian: 2400, qq: 420, wechat: 1500 } })) }, 202)
  }
  if (p === '/system/cleanup/run') return flat(res, { job_id: newJob('system_cleanup', () => ({ freed_mb: 512 })) }, 202)
  // #77 的响应列是字面键集(hardware/ours/…)⇒ 顶层平铺(R6-55),与真后端一致
  if (p === '/system/metrics') return flat(res, state.metrics)
  if (p === '/system/version') {
    // 🔴 裁决①:`agent` 是 `{version}` 对象(真后端现实现),不是裸字符串
    return flat(res, {
      agent: { version: '1.0.3-mock', api_version: API_VERSION },
      winagent: { version: '1.0.2-mock', online: true, user_agent: true },
      // 🔴 M-9:`kernel_state / wsl_state / distro / migration / wa_schema_version` 在 docs 里不存在
      // (第一轮 S-09,总控裁决① = 以后端现实现为准),一律不下发。
      kernel: '6.6.123-binder', wsl: '2.4.10',
      docker: '27.3.1', images: {}, api_version: API_VERSION,
      capabilities_version: 'mock-1', schema_version: 12,
    })
  }
  if (p === '/system/health') {
    // #72 顶层平铺;R6-58 (y):`checks` 另带 per-account 子键(01 §2.7.3.4 的账号健康行取这里)
    return flat(res, {
      agent: { version: '1.0.3-mock', api_version: API_VERSION, uptime_s: 3600, db_mb: 120, wal_mb: 8 },
      dockerd: true,
      winagent: { online: true, version: '1.0.2-mock', user_agent: true },
      accounts: { running: 3, n: state.accounts.length },
      disk_free_mb: 43008,
      mem: { avail_mb: 6246, level: 'normal' },
      checks: {
        H01: 'ok', H03: 'ok', H12: 'firing',
        accounts: {
          qd01: { H04: 'ok', H05: 'ok', H06: 'ok', H07: 'unknown', H08: 'ok' },
          qq01: { H04: 'ok', H05: 'unknown', H06: 'ok', H07: 'unknown', H08: 'ok' },
        },
      },
      scheduler: { retention_cleanup: { runs: 3, skipped: 0, errors: 0 } },
      alerts: [],
    })
  }
  if (p === '/system/env') {
    // 🔴 #74:Windows 侧(= /wa/v1/net)与 WSL 侧分成两半;WinAgent 不可达时 windows 为 null
    return flat(res, {
      windows: {
        net_state: 'VPN_ACTIVE_WITH_PROXY',
        proxy: 'winhttp proxy.corp:8080',
        vpn_adapter: 'Corp VPN',
        wsl_subnet: '172.23.16.0/20',
        host_ip: '172.23.16.1',
        docker_conflict: { state: 'ok' },
      },
      windows_error: null,
      wsl: {
        iface: 'eth0',
        mtu: 1400,
        resolv_conf: { source: 'custom', nameservers: ['223.5.5.5'] },
        docker: { default_address_pools: [{ base: '10.213.0.0/16', size: 24 }] },
        ksm: { run: 0 },
        zram: { disksize_mb: 0 },
        kernel_release: '6.6.123-binder',
        clock: { drift_ms: 300, last_probe_at: now() },
        adb_server: { running: true, reason: null },
      },
      wslconfig: { memory: '8GB' },
      reboot_required: false,
      winagent: { online: true, version: '1.0.2-mock', user_agent: true },
    })
  }
  if (p === '/system/notice') {
    return flat(res, {
      // 真后端顺带回「勾过没有」(ack_ms / acked_at / acked_version),控制台据此判,不本地存
      ack_ms: state.compliance.ack_ms,
      acked_at: state.compliance.ack_ms ? new Date(state.compliance.ack_ms).toISOString() : null,
      acked_version: state.compliance.ack_ms ? state.compliance.notice_version : null,
      notice_version: state.noticeVersion,
      text: [
        '1. 本软件通过自动化方式操作企点/QQ/微信客户端。',
        '2. 模拟设备身份与批量自动化可能违反服务协议,使用方自行评估风险。',
        '3. 每个账号须为使用方自有且有权自动化的企业账号。',
        '4. 消息正文只存本机,按保留期清理;日志不记正文。',
      ].join('\n'),
    })
  }
  // #87 勾选合规告知(写 settings compliance.*)
  if (p === '/system/notice/ack' && m === 'POST') {
    // 🔴 M-8:02 #87「版本升级后需重新勾选」—— 版本对不上必须拒,否则合规判据失真
    const want = String(state.noticeVersion ?? 'v1')
    if (String(body.notice_version ?? '') !== want) {
      return fail(res, 400, 'INVALID_ARGS', `告知版本不符(当前 ${want}),请重新读取后再勾选`, {
        reason: 'notice_version_mismatch',
        details: [{ pointer: '/notice_version', message: want }],
      })
    }
    state.compliance = { ack_ms: Date.now(), notice_version: want }
    return flat(res, { ok: true })
  }
  if (p === '/system/probes') {
    const kind = url.searchParams.get('kind') ?? 'result'
    // 🔴 R6-58 (dc):`?kind=observed` 出参**两键** `{data, targets}`,行里带 `id` 与 `in_config`
    if (kind === 'observed') {
      return ok(res, state.observed, { targets: state.probeTargets, kind })
    }
    // 🔴 行键名按真后端(= 04 `probe_results` 列):结论是 `status`、诊断是 `detail`
    return ok(res, [
      { side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null, at: now() },
      { side: 'wsl', target: 'mail_imap', status: 'OK', level_reached: 'tls', detail: null, at: now() },
      { side: 'wsl', target: 'mail_pop3', status: 'SKIPPED', level_reached: 'none', detail: '目标未配置', at: now() },
      { side: 'wsl', target: 'qidian_msf', status: 'PROXY_REQUIRED', level_reached: 'tcp', detail: null, at: now() },
      { side: 'windows', target: 'winagent_from_wsl', status: 'OK', level_reached: 'http', detail: null, at: now() },
    ], { next_cursor: null, kind })
  }
  if (p === '/system/probe' && m === 'POST') {
    if (body.mode === 'sample') {
      // 🔴 M-4:02 #75 R6-58 (de) 逐字 `{sampled_at, duration_s, rows, skipped}` ⇒ **顶层平铺**
      return flat(res, {
        sampled_at: now(),
        duration_s: Number(body.duration_s ?? 30),
        skipped: [],
        rows: [
          { account_id: 'qd01', channel: 'qidian', remote_ip: '14.215.177.39', port: 8080, proto: 'TCP', samples: 12 },
          { account_id: 'qq01', channel: 'qq', remote_host: 'msfwifi.3g.qq.com', remote_ip: '58.250.137.36', port: 8080, proto: 'TCP', samples: 7 },
        ],
      })
    }
    return flat(res, {
      run_id: `probe_${Date.now().toString(36)}`,
      results: [{ side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null }],
    })
  }
  // #78 → 202 {run_id}
  if (p === '/system/selftest' && m === 'POST') {
    state.selftestRunId = `st_${Date.now().toString(36)}`
    return flat(res, { run_id: state.selftestRunId }, 202)
  }
  // 🔴 M-7:#79 `GET /system/selftest/{run_id}`(mock 原先只有不带 run_id 的 #79b ⇒ 404)
  if ((mm = /^\/system\/selftest\/([^/]+)$/.exec(p)) && m === 'GET') {
    if (mm[1] !== state.selftestRunId) return fail(res, 404, 'TARGET_NOT_FOUND', `没有这一轮自检:${mm[1]}`)
    return ok(res, selftestRun(mm[1]), { run_id: mm[1] })
  }
  // #79b 不带 run_id = 最近一轮;从没跑过回 `{ok:true, data:null, run_id:null}`(不是 404)
  if (p === '/system/selftest') {
    if (!state.selftestRunId && !url.searchParams.get('run_id')) {
      return ok(res, null, { run_id: null })
    }
    const runId = state.selftestRunId ?? url.searchParams.get('run_id')
    // 🔴 #79 的 data 是**一轮的对象**(不是行数组);行由控制台 `selftestRows()` 派生
    return ok(res, selftestRun(runId), { run_id: runId })
  }
  if (p === '/system/diagnostics' && m === 'POST') {
    return flat(res, { job_id: newJob('diagnostics', () => ({ download_url: '/api/v1/exports/mock/file' })) }, 202)
  }
  if (p === '/system/public-endpoint') {
    // 🔴 #102 逐字键集:没有 matches / dns_resolved_ip / history / configured_host
    return flat(res, {
      public_ip: '113.88.12.34',
      public_ip_v6: null,
      configured_domain: String(state.settings.api.public_domain || '') || null,
      checked_at: now(),
      changed_at: now(),
      probe: { url: state.settings.api.public_ip_probe_urls?.[0] ?? null, unreachable_rounds: 0 },
    })
  }
  // #85:有账号在跑时不立刻应用(回 pending + 原因 + 在跑的账号)
  if (p === '/system/docker-proxy') {
    const running = state.accounts.filter((a) => a.state === 'running').map((a) => a.id)
    if (running.length) return flat(res, { pending: true, reason: 'accounts_running', running })
    return flat(res, { applied: true, proxy: body.enable ? 'http://127.0.0.1:7890' : null })
  }
  // #82 排空:之后写操作一律 503 draining(没有逆操作端点,只能重启 Agent)
  if (p === '/system/drain' && m === 'POST') {
    const running = state.accounts.filter((a) => a.state === 'running').map((a) => a.id)
    state.draining = true
    return flat(res, {
      drained: true, inflight: 0, inflight_before: 0, waited_s: 0,
      stopped_accounts: running,
    })
  }
  // #83 优雅停机:须 confirm:true
  if (p === '/system/shutdown' && m === 'POST') {
    if (body.confirm !== true) {
      return fail(res, 400, 'INVALID_ARGS', '优雅停机须带 confirm:true(停机后控制台会断开)', {
        reason: 'confirm_required', details: [{ pointer: '/confirm', message: '必须为 true' }],
      })
    }
    return flat(res, { accepted: true, stopping: true }, 202)
  }
  if (p === '/system/wsl-restart') {
    // 🔴 M-3(最危险的一条):基线 §11.6 [NOSHUTDOWN] + 02 #84 —— **必须** `confirm:true`。
    // 真后端不带 confirm 时 `400 confirm_required` 且一条请求都不发往 WinAgent;
    // mock 放行会让页面开发以为不需要二次确认。
    if (body.confirm !== true) {
      return fail(res, 400, 'INVALID_ARGS', '重启 WSL 是破坏性操作,须带 confirm:true', {
        reason: 'confirm_required', needs_human: true,
        details: [{ pointer: '/confirm', message: '必须为 true' }],
      })
    }
    return flat(res, { ok: true })
  }

  // ── 邮件
  if (p === '/mail/status') {
    return ok(res, {
      enabled: true,
      routes: [{
        route_id: 'r_default', channel: null, account_id: null, scope: 'default',
        // #56 逐字:另带 `route:{id,channel,account_id,outbound_template_id,inbound_template_id}`
        route: {
          id: 'r_default', channel: null, account_id: null,
          outbound_template_id: 'tpl-ibquote', inbound_template_id: null,
        },
        inbound: {
          protocol_configured: 'imap', protocol_active: 'pop3',
          fallback: { since_at: now(), reason: 'IMAP 登录失败' },
          folders: [{ name: 'INBOX', uidvalidity: 12, last_uid: 3401 }],
          last_success_at: now(), idle_supported: false, consecutive_failures: 0,
          quota: { used_mb: 150, limit_mb: 1024, source: 'estimate' },
        },
        outbound: { queued: 2, retrying: 0, dead: 0, last_sent_at: now(), consecutive_failures: 0, rate_per_min: 6 },
        cleanup: { last_run_at: now(), last_status: 'ok', archived_mb: 42, next_run_at: now() },
      }],
    })
  }
  if (p === '/mail/pending-confirms') return ok(res, state.pendingConfirms, { next_cursor: null })
  if ((mm = /^\/mail\/pending-confirms\/([^/]+)\/(approve|reject)$/.exec(p)) && m === 'POST') {
    const i = state.pendingConfirms.findIndex((x) => x.id === mm[1])
    if (i < 0) return fail(res, 409, 'CONFIRM_EXPIRED', '该确认已过期或已被处理')
    state.pendingConfirms.splice(i, 1)
    return flat(res, { ok: true })
  }
  if (p === '/mail/inbox') {
    return ok(res, [
      { id: 'mi_0001', received_at: now(), route: '全局', from_addr: 'ops@corp', subject: '[QTrade] 停账号', status: 'CONFIRM_REQUIRED', trace_id: '01TRACEMOCK' },
      { id: 'mi_0002', received_at: now(), route: '企点', from_addr: 'x@corp', subject: '格式不对', status: 'PARSE_FAILED', reason: 'bad template' },
      { id: 'mi_0003', received_at: now(), route: '全局', from_addr: 'evil@x.com', subject: '越权', status: 'OP_DENIED', reason: 'NOT_ALLOWED:vault_write' },
    ], { next_cursor: null })
  }
  if ((mm = /^\/mail\/inbox\/([^/]+)$/.exec(p))) {
    return ok(res, {
      id: mm[1], received_at: now(), route: '全局', from_addr: 'ops@corp', subject: '[QTrade] 停账号',
      status: 'CONFIRM_REQUIRED', template_alias: 'qtrade-cmd',
      parsed: { op: 'account_stop', args: { 账号: 'qd01' }, args_digest: 'a1b2c3d4e5f60718', target: 'qd01', req_id: '20260920-ops-0007', nonce: 'n1', sig_ok: true },
      trace_id: '01TRACEMOCK', result: { code: 'OK', cost_ms: 120 },
    })
  }
  if (/^\/mail\/inbox\/[^/]+\/reparse$/.test(p)) return flat(res, { ok: true })
  if (p === '/mail/outbox') {
    return ok(res, [
      { id: 'mo_1', kind: 'digest', to: 'team@corp', subject: '[QTrade] 报价汇总', status: 'QUEUED', attempts: 0, next_attempt_at: now() },
      { id: 'mo_2', kind: 'receipt', to: 'ops@corp', subject: 'Re: 停账号', status: 'RECEIPT_SENT', attempts: 1 },
    ], { next_cursor: null })
  }
  if (/^\/mail\/outbox\/[^/]+\/(resend|discard)$/.test(p)) return flat(res, { ok: true })
  if (p === '/mail/cleanup/log') {
    return ok(res, [{ id: 'c1', at: now(), deleted: 12, archived: 30, status: 'ok' }], { next_cursor: null })
  }
  if (p === '/mail/cleanup/run') return flat(res, { job_id: newJob('mail_cleanup', () => ({ freed_mb: 20 })) }, 202)
  if (p === '/mail/test') return flat(res, { imap_ok: false, pop3_ok: true })
  // 短名表的唯一来源(R6-58 (ac)):不回密钥
  if (p === '/mail/hmac-keys' && m === 'GET') return ok(res, state.hmacKeys, { next_cursor: null })
  if (p === '/mail/hmac-keys' && m === 'POST') {
    if (!/^[A-Za-z0-9._-]{1,32}$/.test(String(body.short_name ?? ''))) {
      return fail(res, 400, 'INVALID_ARGS', '短名只能是字母/数字/点/下划线/连字符,长度 1~32', {
        reason: 'short_name_invalid',
      })
    }
    if (state.hmacKeys.some((k) => k.short_name === body.short_name)) {
      return fail(res, 400, 'INVALID_ARGS', '短名已被占用(全局唯一)', { reason: 'short_name_taken' })
    }
    state.hmacKeys.push({ sender: body.sender, short_name: body.short_name, created_at: now() })
    return ok(res, { secret: `hmac_${Math.random().toString(36).slice(2)}` })
  }
  if ((mm = /^\/mail\/hmac-keys\/([^/]+)$/.exec(p)) && m === 'DELETE') {
    const i = state.hmacKeys.findIndex((k) => k.short_name === mm[1])
    if (i < 0) return fail(res, 404, 'TARGET_NOT_FOUND', '该短名不存在')
    state.hmacKeys.splice(i, 1)
    return flat(res, { ok: true })
  }
  // #104 路径带 {id}(后端实现成 `/mail/templates/preview` 的写法是后端该改)
  if (/^\/mail\/templates\/[^/]+\/preview$/.test(p) && m === 'POST') {
    return flat(res, {
      subject: '[QTrade] 微信 某某群 2026-09-20 10:03',
      body_text: '渠道: 微信\n会话: 某某群\n正文: 3M 报价 1.62',
      fields_used: ['channel', 'session_name', 'text'],
      warnings: [],
    })
  }

  // ── 设置
  /*
   * #76b `PUT /settings/probe`:入参**只收** `observed_ids`(整数数组,**采纳后的全集**,`[]` 合法);
   * 传 `targets` 一律 400 `use_observed_ids`。出参逐字四键。
   */
  if (p === '/settings/probe' && m === 'PUT') {
    if (body.targets !== undefined) {
      return fail(res, 400, 'INVALID_ARGS', '请改用 observed_ids(值 = 行的 probe_targets_observed.id)', {
        reason: 'use_observed_ids',
      })
    }
    const ids = body.observed_ids
    if (!Array.isArray(ids) || ids.some((x) => !Number.isInteger(x))) {
      return fail(res, 400, 'INVALID_ARGS', 'observed_ids 必须是整数数组', { reason: 'bad_field' })
    }
    if (new Set(ids).size !== ids.length) {
      return fail(res, 400, 'INVALID_ARGS', 'observed_ids 有重复 id', { reason: 'duplicate_ids' })
    }
    const unknown = ids.filter((id) => !state.observed.some((r) => r.id === id))
    if (unknown.length) return fail(res, 404, 'TARGET_NOT_FOUND', `不存在的采样行 id:${unknown.join(',')}`)
    const picked = new Set(ids)
    for (const row of state.observed) {
      // 全集语义:不在集合里的已采纳行取消采纳;已采纳的再次入选不刷新时刻(幂等)
      if (picked.has(row.id)) {
        if (!row.adopted_at) row.adopted_at = now()
        row.in_config = true
      } else {
        row.adopted_at = null
        row.in_config = false
      }
    }
    const adoptedRows = state.observed.filter((r) => picked.has(r.id))
    state.probeTargets = adoptedRows.map((r) => `${r.remote_host || r.remote_ip}:${r.port}`)
    const byChannel = { qidian_hosts: [], qq_hosts: [], wechat_hosts: [] }
    for (const r of adoptedRows) {
      const k = `${r.channel}_hosts`
      if (byChannel[k]) byChannel[k].push(`${r.remote_host || r.remote_ip}:${r.port}`)
    }
    return flat(res, {
      adopted: adoptedRows.map((r) => r.id),
      adopted_rows: adoptedRows,
      targets: state.probeTargets,
      hosts_by_channel: byChannel,
    })
  }

  // 🔴 #88 的 `group` 枚举逐字(不是任意 `[a-z-]+` —— 那会把 `/settings/api-clients` 也吞掉)
  if ((mm = /^\/settings\/(api|winagent|runtime|pool|bus|adapters|asr|ocr|messages|media|retention|events|mail|log|resources|compliance)$/.exec(p))) {
    const g = mm[1]
    // `compliance` 不在 #88 的 group 枚举里 ⇒ 404(与真 Agent 一致;合规告知走 #86/#87)
    if (!Object.prototype.hasOwnProperty.call(state.settings, g)) {
      return fail(res, 404, 'TARGET_NOT_FOUND', `没有这个设置组:${g}(枚举见 02 #88)`)
    }
    if (m === 'GET') return ok(res, state.settings[g], { group: g })
    if (m === 'PUT') {
      // #89 是**整组替换**(缺省键回默认);v1 除 resources 外一律 restart_required
      state.settings[g] = { ...body }
      return ok(res, state.settings[g], { group: g, restart_required: g !== 'resources', config_written: true })
    }
  }
  if (p === '/settings/api-clients') {
    if (m === 'GET') {
      return ok(res, [
        {
          app_id: 'console', name: '控制台', auth_kind: 'bearer', level: 'admin', ip_allow: [],
          allow_ops: ['*'], allow_accounts: ['*'], rate_per_min: 120, api_version_min: 1,
          enabled: true, secret_ref: null, builtin: true,
          created_at: now(), updated_at: now(), last_used_at: now(), revoked_at: null,
        },
        {
          app_id: 'ibquote', name: 'ibquote fetcher', auth_kind: 'bearer', level: 'read',
          ip_allow: ['10.0.0.0/8'], allow_ops: ['read_messages'], allow_accounts: ['*'],
          rate_per_min: 60, api_version_min: 1, enabled: true, secret_ref: null, builtin: false,
          created_at: now(), updated_at: now(), last_used_at: null, revoked_at: null,
        },
      ], { next_cursor: null })
    }
    /*
     * 🔴 M-12 + N-1:建资源按 00 §10 / #2 的惯例是 **201**(mock 原先 200)。
     * 形状按 R6-55 二选一 —— `ApiClient` 不在 00 §7 的对象清单里 ⇒ **顶层平铺 + `ok`**
     * (真后端当前两种都占,那是 B-1,后端正在收口)。
     * 明文 `token` **只在这一次下发**,列表端点永远不回。
     */
    const appId = `app_${Date.now().toString(36)}`
    return flat(res, {
      app_id: appId,
      name: String(body.name ?? appId),
      auth_kind: String(body.auth_kind ?? 'bearer'),
      level: String(body.level ?? 'read'),
      ip_allow: body.ip_allow ?? [],
      allow_ops: body.allow_ops ?? [],
      allow_accounts: body.allow_accounts ?? ['*'],
      rate_per_min: 60, api_version_min: 1, enabled: true, secret_ref: null, builtin: false,
      prefix6: 'qt_abc',
      created_at: now(), updated_at: now(), last_used_at: null, revoked_at: null,
      token: `qt_${Math.random().toString(36).slice(2)}`,
    }, 201)
  }
  // #92 轮换:同样是一次性明文 + 旧凭据宽限期
  if (/^\/settings\/api-clients\/[^/]+\/rotate$/.test(p)) return flat(res, { token: `qt_${Math.random().toString(36).slice(2)}`, grace_minutes: 10 })
  if (/^\/settings\/api-clients\/[^/]+$/.test(p) && m === 'DELETE') return flat(res, { ok: true })
  if (p === '/settings/mail/routes') {
    if (m === 'GET') return ok(res, [], { next_cursor: null })
    return flat(res, { ok: true })
  }
  if (p === '/settings/mail/templates') {
    if (m === 'GET') {
      return ok(res, [{
        template_id: 'tpl-ibquote', name: 'ibquote 兼容', kind: 'outbound', compat_profile: 'ibquote-163-v1',
        subject_pattern: '[QTrade] {channel} {session_name} {ts}',
        body_fields: [], referenced_by: ['default'], version: 1, updated_at: now(),
      }], { next_cursor: null })
    }
    return ok(res, { template_id: `tpl_${Date.now().toString(36)}`, name: body.name, kind: 'outbound', compat_profile: body.compat_profile, subject_pattern: body.subject_pattern, body_fields: body.body_fields ?? [], referenced_by: [], version: 1, updated_at: now() })
  }
  if (/^\/settings\/mail\/templates\/[^/]+$/.test(p)) {
    if (m === 'DELETE') return flat(res, { ok: true })
    return ok(res, { ...body, updated_at: now() })
  }
  if (p === '/settings/webhooks') {
    if (m === 'GET') return ok(res, state.webhooks ?? [], { next_cursor: null })
    state.webhooks = [...(state.webhooks ?? []), { id: `wh_${Date.now().toString(36)}`, url: body.url, enabled: true }]
    return flat(res, { id: state.webhooks[state.webhooks.length - 1].id })
  }
  if (/^\/settings\/webhooks\/[^/]+$/.test(p)) return flat(res, { ok: true })

  // ── 审计
  if (p === '/audit') {
    /*
     * 🔴 R6-58 (ag):列集定死十列 —— `id, ts_ms, kind, transport, actor, action, account_id,
     * trace_id, result_code, detail_json`。`cost_ms / ip / http_status / method / path / sig_ok`
     * **在 `detail_json` 里**,不是独立列(此前 mock 把它们平铺,养出了前端按 `ts/op/code` 取值的错)。
     */
    const kind = url.searchParams.get('kind') ?? 'command'
    const rows = Array.from({ length: 12 }, (_, i) => {
      const isApi = kind === 'api'
      const detail = isApi
        ? { http_status: 200, cost_ms: 2, ip: '127.0.0.1', method: 'GET', path: '/api/v1/accounts' }
        : { cost_ms: 820, ip: '127.0.0.1', op: 'send_text', args_digest: 'ab12cd34', source: 'agent' }
      return {
        id: 300 - i,
        ts_ms: Date.now() - i * 60000,
        kind,
        transport: 'local',
        actor: 'token:console',
        action: isApi ? 'GET /api/v1/accounts' : 'send_text',
        account_id: kind === 'command' ? 'qd01' : null,
        trace_id: `01TRACE${i}`,
        result_code: isApi ? '200' : 'DELIVERED',
        detail_json: JSON.stringify(detail),
      }
    })
    return ok(res, rows, { next_cursor: null })
  }

  // ── 截图 / 媒体(回一张 1x1 PNG)
  if (/\/screenshot$/.test(p) || /^\/media\//.test(p)) {
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
      'base64',
    )
    res.writeHead(200, { 'Content-Type': 'image/png' })
    return res.end(png)
  }

  return fail(res, 404, 'TARGET_NOT_FOUND', `mock 未实现:${m} ${p}`)
})

const wss = new WebSocketServer({ server, path: '/api/v1/events' })
wss.on('connection', (ws, req) => {
  // 先 accept 再 close(4401):这正是后端要改成的行为(E-05);mock 提前照这个语义来
  const q = new URL(req?.url ?? '/', `http://127.0.0.1:${PORT}`).searchParams
  if (q.get('token') === 'bad') {
    ws.close(4401, 'invalid token')
    return
  }
  sockets.add(ws)
  ws.on('close', () => sockets.delete(ws))
  ws.on('message', (raw) => {
    let msg
    try { msg = JSON.parse(String(raw)) } catch { return }
    if (msg.ping) { ws.send(JSON.stringify({ event: 'ping', ts: now(), seq: 0, payload: {} })); return }
    if (!msg.subscribe) { ws.close(4400, 'bad subscribe'); return }
    // 订阅成功后先推一帧资源,方便界面立刻有数
    ws.send(JSON.stringify({ event: 'resource', ts: now(), seq: nextSeq(), payload: { ...state.resources, metrics_snapshot: state.metrics } }))
  })
})

// 周期性推事件:资源 + 一条带 late/origin 的消息(演示 R6-49 的三字段)
let msgN = 0
setInterval(() => emit('resource', { ...state.resources, metrics_snapshot: state.metrics }), 5000)
setInterval(() => {
  msgN += 1
  const base = state.messages[0]
  emit('message', {
    message: {
      ...base,
      id: `msg_live${msgN}`,
      text: `实时消息 #${msgN}`,
      ts: new Date(Date.now() - (msgN % 3 === 0 ? 5400_000 : 4000)).toISOString(),
      received_at: now(),
      // 事件专属、不落库的三字段
      lag_s: msgN % 3 === 0 ? 5400 : 4,
      late: msgN % 3 === 0,
      origin: msgN % 4 === 0 ? 'external' : 'rpa',
    },
  }, { account_id: base.account_id, channel: base.channel })
}, 8000)
setInterval(() => {
  emit('alert', {
    code: 'H12_DISK_LOW', severity: 'warn', state: 'firing', subject: 'host',
    title: '磁盘剩余偏低', message: 'D: 剩余 3.1 GB,低于 warn 水位 5 GB',
    hint_actions: ['open_env', 'run_cleanup'], first_seen_at: now(), last_seen_at: now(), count: 1,
    evidence: { free_mb: 3174 },
  })
}, 30000)

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock] Agent 假后端已启动:http://127.0.0.1:${PORT}/api/v1  (WS /api/v1/events)`)
})
