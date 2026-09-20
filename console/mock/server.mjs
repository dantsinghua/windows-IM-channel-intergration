/**
 * 开发用 mock 后端 —— 按 02 §3.4 的信封回假数据 + WS `/api/v1/events` 事件流。
 * 只为 `npm run dev:web` 能在浏览器里独立跑起来,**不是** Agent 的实现。
 *
 * 监听 127.0.0.1:17600(与 [endpoint] agent 一致),vite dev server 把 /api/v1 代理过来。
 */
import { createServer } from 'node:http'
import { WebSocketServer } from 'ws'
import { CAPABILITIES, makeAccounts, makeMessages, makeMetrics, makeResources, now } from './data.mjs'

const PORT = Number(process.env.MOCK_PORT ?? 17600)

const state = {
  accounts: makeAccounts(),
  resources: makeResources(),
  metrics: makeMetrics(),
  ...makeMessages(60),
  jobs: new Map(),
  seq: 0,
  settings: {
    api: { lan_enabled: false, bind: '127.0.0.1', ip_allow: '', https: false },
    retention: { files_days: 7, text_days: 30, raw_enabled: false, audit_days: 30 },
    resources: { qidian_mb: 2560, qq_mb: 614, wechat_mb: 1536, base_mb: 2048, mem_warn_mb: 6144, mem_critical_mb: 3072 },
    asr: { endpoint: 'http://10.0.0.8:9000/asr', key: '', concurrency: 2, min_confidence: 0.6 },
    ocr: { engine: 'offline', model_dir: '/opt/qtrade/ocr', min_conf: 0.8, lang: 'zh' },
    mail: {
      enabled: true, require_signature: true, archive: true,
      scopes: {
        default: {
          override: true, proto: 'imap', host: 'imap.163.com', port: 993, ssl: true,
          user: 'ops@corp', pass: '', poll: 60,
          'smtp-host': 'smtp.163.com', 'smtp-port': 465, 'smtp-from': 'ops@corp', 'smtp-pass': '',
          recipients: 'team@corp', 'template-id': 'tpl-ibquote',
          allow_ops: ['read_messages', 'list_sessions', 'get_state', 'screenshot', 'send_text', 'send_image'],
          senders: [{ addr: 'ops@corp', shortname: 'ops', keyed: true }],
        },
        qidian: { override: false }, qq: { override: false }, wechat: { override: false },
      },
    },
    compliance: { ack_ms: null, notice_version: 'v1' },
  },
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

function emit(event, payload, extra = {}) {
  const frame = JSON.stringify({ event, ts: now(), seq: nextSeq(), payload, ...extra })
  for (const ws of sockets) { try { ws.send(frame) } catch { /* 客户端已走 */ } }
}

function ok(res, data, extra = {}) {
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'X-QT-Api-Version': '1.3',
    'X-QT-Agent-Version': '1.0.3-mock',
  })
  res.end(JSON.stringify({ ok: true, data, trace_id: `01MOCK${Date.now()}`, ...extra }))
}

function flat(res, obj) {
  res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'X-QT-Api-Version': '1.3' })
  res.end(JSON.stringify({ ok: true, ...obj, trace_id: `01MOCK${Date.now()}` }))
}

function fail(res, status, code, message, extra = {}) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
  res.end(JSON.stringify({
    ok: false, code,
    error: { message, retryable: status >= 500, needs_human: false, ...extra },
    trace_id: `01MOCK${Date.now()}`,
  }))
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

  // ── 账号
  if (p === '/accounts' && m === 'GET') return ok(res, state.accounts, { next_cursor: null })
  if (p === '/accounts' && m === 'POST') {
    const seq = state.accounts.filter((a) => a.channel === body.channel).length + 1
    const prefix = { qidian: 'qd', qq: 'qq', wechat: 'wx' }[body.channel]
    const a = {
      id: `${prefix}${String(seq).padStart(2, '0')}`, channel: body.channel, host: body.channel === 'wechat' ? 'windows' : 'wsl',
      label: body.label, state: 'provisioning', state_code: '', state_reason: '', error_since_ms: null,
      enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, identity: {},
      login: { mode: body.login?.mode ?? 'password', remember: !!body.login?.remember },
      capabilities: [], quota_mb: state.resources.quota_mb[body.channel], created_at: now(), updated_at: now(),
      last_seen_at: null, settings: {},
    }
    state.accounts.push(a)
    setTimeout(() => {
      a.state = 'login_required'
      a.state_code = body.channel === 'qq' ? 'WAIT_QRCODE' : 'WAIT_SMS'
      pushAccountState(a)
    }, 1200)
    return ok(res, a)
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
    return flat(res, { state: next })
  }
  if ((mm = /^\/accounts\/([^/]+)\/prompt$/.exec(p))) {
    const a = acct(mm[1])
    if (!a || a.state !== 'login_required') return ok(res, { kind: null })
    return ok(res, { kind: a.state_code, text: '请完成验证', countdown_s: 90 })
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
    return flat(res, { state: 'logging_in', login_session_id: 'ls_01MOCK' })
  }
  if ((mm = /^\/accounts\/([^/]+)\/capabilities$/.exec(p))) {
    const a = acct(mm[1])
    const matrix = {}
    for (const c of CAPABILITIES) matrix[c.op] = c.channels[a?.channel ?? 'qidian']
    return ok(res, { capabilities: a?.capabilities ?? [], matrix })
  }
  if ((mm = /^\/accounts\/([^/]+)\/purge$/.exec(p)) && m === 'POST') {
    return flat(res, { job_id: newJob('account_purge', () => ({ purged: true })) })
  }
  if ((mm = /^\/accounts\/([^/]+)\/export-identity$/.exec(p)) && m === 'POST') {
    return flat(res, { job_id: newJob('identity_export', () => ({ download_url: '/api/v1/exports/mock/file' })) })
  }
  if (/^\/accounts\/[^/]+\/(credential|settings|runtime\/.+|webui\/.+|stream\/input)$/.test(p)) {
    const a = acct(p.split('/')[2])
    if (a && p.endsWith('/settings') && m === 'PATCH') Object.assign(a.settings ?? {}, body)
    if (p.endsWith('/webui/open')) return flat(res, { url: 'http://127.0.0.1:16301/', until: new Date(Date.now() + 600_000).toISOString() })
    return flat(res, { ok: true, adb_state: 'device' })
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
    return ok(res, { results })
  }
  if (p === '/device-profiles/templates') {
    return ok(res, [
      { profile_key: 'xiaomi-mi11', brand: 'Xiaomi', model: 'MI 11' },
      { profile_key: 'huawei-p40', brand: 'HUAWEI', model: 'P40' },
    ])
  }

  // ── 能力 / 指令
  if (p === '/capabilities') return ok(res, CAPABILITIES, { capabilities_version: 'mock-1' })
  if ((mm = /^\/accounts\/([^/]+)\/commands$/.exec(p)) && m === 'POST') {
    const text = String(body.args?.text ?? '')
    // R6-48:含控制字符在入口被拒
    if (/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/.test(text)) {
      return fail(res, 400, 'INVALID_ARGS', '正文含不可见控制字符', {
        reason: 'text_has_control_chars', details: [{ pointer: '/text', message: 'control chars' }],
      })
    }
    const result = {
      ok: true, code: body.op?.startsWith('send') ? 'DELIVERED' : 'OK',
      data: { message_id: `msg_${Date.now()}`, confirmed_by: 'ingest_merge', confirm_ms: 742, needs_review: false },
      cost_ms: 865, trace_id: `01MOCK${Date.now()}`,
      source: 'qidian_db', state_before: 'READY', state_after: 'READY',
    }
    emit('command_done', { trace_id: result.trace_id, code: result.code, cost_ms: result.cost_ms })
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
    return res.end(JSON.stringify(result))
  }
  if (p === '/broadcast/commands' && m === 'POST') {
    const results = {}
    for (const id of body.account_ids ?? []) {
      results[id] = { ok: true, code: 'DELIVERED', cost_ms: 700, trace_id: `01MOCK${id}` }
    }
    return ok(res, { broadcast_id: 'bc_mock', results })
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
  if (p === '/messages/export' && m === 'POST') {
    return flat(res, { job_id: newJob('messages_export', () => ({ download_url: '/api/v1/exports/mock/file' })) })
  }

  // ── 作业
  if ((mm = /^\/jobs\/([^/]+)$/.exec(p))) {
    const j = state.jobs.get(mm[1])
    if (!j) return fail(res, 404, 'TARGET_NOT_FOUND', '作业不存在')
    return flat(res, j)
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
    const runId = `run_${Date.now().toString(36)}`
    setTimeout(() => emit('workflow', { run_id: runId, status: 'started' }), 300)
    return flat(res, { run_id: runId })
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
  if (p === '/resources/precheck') return ok(res, { can_add: state.resources.can_add[body.channel] ?? 0 })
  if (p === '/resources/calibrate') return ok(res, { suggestions: { qidian: 2400, qq: 420, wechat: 1500 } })
  if (p === '/system/cleanup/run') return flat(res, { job_id: newJob('system_cleanup', () => ({ freed_mb: 512 })) })
  if (p === '/system/metrics') return ok(res, state.metrics)
  if (p === '/system/version') {
    return ok(res, {
      agent: '1.0.3-mock', winagent: { version: '1.0.2-mock', online: true, user_agent: true },
      kernel: '6.6.123-binder', kernel_state: 'OURS', wsl: '2.4.10', wsl_state: 'WSL2_STORE',
      docker: '27.3.1', distro: 'qtrade', api_version: '1.3', capabilities_version: 'mock-1',
      schema_version: 12, wa_schema_version: 4, migration: { state: 'idle' },
    })
  }
  if (p === '/system/health') {
    return ok(res, {
      ok: true, agent: { version: '1.0.3-mock', api_version: '1.3', uptime_s: 3600, db_mb: 120, wal_mb: 8 },
      dockerd: true, winagent: { online: true, version: '1.0.2-mock', user_agent: true },
      disk_free_mb: 43008, checks: { H01: 'ok', H03: 'ok', H12: 'firing' },
      alerts: [],
    })
  }
  if (p === '/system/env') {
    return ok(res, {
      net_state: 'VPN_ACTIVE_WITH_PROXY', proxy: 'winhttp proxy.corp:8080', vpn_adapter: 'Corp VPN',
      wsl_subnet: '172.23.16.0/20', host_ip: '172.23.16.1', mtu: 1400, clock_drift_s: 0.3,
      pending_restart: false, kernel_state: 'OURS', wsl_state: 'WSL2_STORE',
      wslconfig: { memory: '8GB' },
      docker_cidr: '10.213.0.0/16', docker_conflict: { state: 'ok' },
    })
  }
  if (p === '/system/notice') {
    return ok(res, {
      notice_version: 'v1',
      text: [
        '1. 本软件通过自动化方式操作企点/QQ/微信客户端。',
        '2. 模拟设备身份与批量自动化可能违反服务协议,使用方自行评估风险。',
        '3. 每个账号须为使用方自有且有权自动化的企业账号。',
        '4. 消息正文只存本机,按保留期清理;日志不记正文。',
      ].join('\n'),
    })
  }
  if (p === '/system/probes') {
    return ok(res, [
      { target: 'apk_url', result: 'OK' },
      { target: 'mail_imap', result: 'OK' },
      { target: 'mail_pop3', result: 'SKIPPED' },
      { target: 'qidian_msf', result: 'PROXY_REQUIRED' },
      { target: 'winagent_from_wsl', result: 'OK' },
    ], { next_cursor: null })
  }
  if (p === '/system/probe' && m === 'POST') {
    if (body.mode === 'sample') {
      return ok(res, {
        sampled_at: now(),
        rows: [
          { account_id: 'qd01', channel: 'qidian', remote_ip: '14.215.177.39', port: 8080, proto: 'TCP', samples: 12 },
          { account_id: 'qq01', channel: 'qq', remote_host: 'msfwifi.3g.qq.com', remote_ip: '58.250.137.36', port: 8080, proto: 'TCP', samples: 7 },
        ],
      })
    }
    return ok(res, { run_id: 'probe_mock', results: [{ target: 'apk_url', result: 'OK' }] })
  }
  if (p === '/system/selftest' && m === 'POST') return flat(res, { run_id: 'st_mock' })
  if (p === '/system/selftest') {
    return ok(res, [
      { item: 'agent', label: 'Agent 健康', level: 'ok' },
      { item: 'winagent', label: 'WinAgent 健康', level: 'ok' },
      { item: 'kernel', label: '内核 binder', level: 'ok' },
      { item: 'dockerd', label: 'dockerd', level: 'ok' },
      { item: 'wslconfig', label: '.wslconfig memory=8GB(建议 11GB)', level: 'warn', message: '建议调到 11GB' },
    ], { next_cursor: null })
  }
  if (p === '/system/diagnostics' && m === 'POST') {
    return flat(res, { job_id: newJob('diagnostics', () => ({ download_url: '/api/v1/exports/mock/file' })) })
  }
  if (p === '/system/public-endpoint') {
    return ok(res, {
      public_ip: '113.88.12.34', checked_at: now(), configured_host: 'qt.example.com',
      dns_resolved_ip: '113.88.12.34', matches: true, last_changed_at: now(),
      history: [{ at: now(), from_ip: '113.88.12.10', to_ip: '113.88.12.34' }],
    })
  }
  if (p === '/system/docker-proxy' || p === '/system/wsl-restart') return flat(res, { ok: true, applied: true })

  // ── 邮件
  if (p === '/mail/status') {
    return ok(res, {
      enabled: true,
      routes: [{
        route_id: 'r_default', channel: null, account_id: null, scope: 'default',
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
  if (p === '/mail/cleanup/run') return flat(res, { job_id: newJob('mail_cleanup', () => ({ freed_mb: 20 })) })
  if (p === '/mail/test') return ok(res, { ok: true, imap_ok: false, pop3_ok: true })
  if (p === '/mail/hmac-keys' && m === 'POST') return ok(res, { secret: `hmac_${Math.random().toString(36).slice(2)}` })
  if (/^\/mail\/hmac-keys\/[^/]+$/.test(p) && m === 'DELETE') return flat(res, { ok: true })
  if (/^\/mail\/templates\/[^/]+\/preview$/.test(p)) {
    return ok(res, { subject: '[QTrade] 微信 某某群 2026-09-20 10:03', body_text: '渠道: 微信\n会话: 某某群\n正文: 3M 报价 1.62', warnings: [] })
  }

  // ── 设置
  if ((mm = /^\/settings\/([a-z-]+)$/.exec(p))) {
    const g = mm[1]
    if (m === 'GET') return ok(res, state.settings[g] ?? {})
    if (m === 'PUT') { state.settings[g] = { ...(state.settings[g] ?? {}), ...body }; return ok(res, state.settings[g]) }
  }
  if (p === '/settings/api-clients') {
    if (m === 'GET') {
      return ok(res, [
        { app_id: 'console', name: '控制台', prefix6: 'qt_abc', level: 'admin', ip_allow: ['127.0.0.1'], created_at: now(), last_used_at: now() },
        { app_id: 'ibquote', name: 'ibquote fetcher', prefix6: 'qt_xyz', level: 'read', ip_allow: ['10.0.0.0/8'], created_at: now() },
      ], { next_cursor: null })
    }
    return ok(res, { app_id: `app_${Date.now().toString(36)}`, token: `qt_${Math.random().toString(36).slice(2)}` })
  }
  if (/^\/settings\/api-clients\/[^/]+\/rotate$/.test(p)) return ok(res, { token: `qt_${Math.random().toString(36).slice(2)}` })
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
    if (m === 'GET') return ok(res, [], { next_cursor: null })
    return ok(res, { id: `wh_${Date.now().toString(36)}` })
  }
  if (/^\/settings\/webhooks\/[^/]+$/.test(p)) return flat(res, { ok: true })
  if (p === '/settings/public-endpoint' || p === '/settings/probe') return flat(res, { ok: true, adopted: [], targets: [] })

  // ── 审计
  if (p === '/audit') {
    const kind = url.searchParams.get('kind') ?? 'command'
    const rows = Array.from({ length: 12 }, (_, i) => ({
      id: `au${i}`, ts: new Date(Date.now() - i * 60000).toISOString(), kind,
      account_id: 'qd01', op: 'send_text', action: 'command.run', actor: 'token:console',
      transport: 'local', ip: '127.0.0.1', code: 'DELIVERED', cost_ms: 820,
      http_status: 200, method: 'POST', path: '/api/v1/accounts/qd01/commands', sig_ok: true,
      args_digest: 'ab12cd34', trace_id: `01TRACE${i}`, source: 'agent',
    }))
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
wss.on('connection', (ws) => {
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
