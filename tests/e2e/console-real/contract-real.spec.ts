/**
 * 控制台 API 客户端 ↔ 真 Agent(全假后端)契约联调 —— **第二轮:按规格断言**。
 *
 * 🔴 与第一轮的根本区别:第一轮断言的是「实测到的现状(含缺陷)」,本轮断言的是
 * **规格要求的正确行为**。每条用例的 docstring 都写明预期值的规格出处;
 * 预期**不出自** `.omc/handoffs/*.md` 里任何一方的说法,交接只当线索。
 *
 * 规格出处缩写:`02` = docs/02-后端与本地数据库设计.md,`00` = docs/00-共享基线与口径.md,`07` = docs/07-配置项总表.md。
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { patch, raw, lastSeen, waitFor, ensureAccountRunning } from './harness'
import { ApiFailure, requestEnvelope, request, API_MIN_VERSION, recentTraces } from '@/api/http'
import {
  accountsApi, systemApi, settingsApi, commandsApi, messagesApi, auditApi, resourcesApi, jobsApi,
} from '@/api/client'
import { auditDetail, normalizeSession, normalizeJob, selftestRows } from '@/api/types'

const ACC = 'qd01'

/* 全局前置:本文件多条用例(#72 checks.accounts、/sessions、#28)要求有一个 running 的企点账号。
   建号/登录一律经控制台客户端发起,不绕过被测代码。 */
beforeAll(async () => {
  patch.token = 'e2e-admin-token'
  patch.noAuth = false
  patch.forceApiMin = null
  await ensureAccountRunning(ACC)
}, 120000)

async function fails(fn: () => Promise<unknown>): Promise<ApiFailure> {
  try {
    await fn()
  } catch (e) {
    if (e instanceof ApiFailure) return e
    throw e
  }
  throw new Error('期望抛 ApiFailure,实际成功返回')
}

/** 00 §10:错误信封 `{ok:false, code, error:{message,reason?,retryable,needs_human}, trace_id}` */
function expectErrorEnvelope(body: Record<string, any>): void {
  expect(body.ok).toBe(false)
  expect(typeof body.code).toBe('string')
  expect(body.error).toBeTruthy()
  expect(typeof body.error.message).toBe('string')
  expect(typeof body.error.retryable).toBe('boolean')
  expect(typeof body.error.needs_human).toBe('boolean')
  expect(typeof body.trace_id).toBe('string')
}

/* ══════════════ E-01 版本协商 ══════════════ */

describe('E-01 版本协商头(02 §3.8)', () => {
  it('控制台声明的 API_MIN_VERSION = 1.0', () => {
    /* 02 §3.8 R6-62 (c):「本期现行 API 版本 = `1.0`(§3.8/§3.9 与 07 §2 `[api] api_version="1.0"` 三处同值),
       **控制台发的 `X-QT-Api-Min` 就写 `1.0`**」。照一个高于现行值的数写死 ⇒ 每条请求被 426 挡住。 */
    expect(API_MIN_VERSION).toBe('1.0')
  })

  it('客户端按本来的样子发请求 ⇒ 200,且后端 api_version = 1.0(不再需要摘头)', async () => {
    /* 02 §3.8 R6-62 (c) 三处同值。本轮**不摘任何头**:客户端发什么就是什么。 */
    patch.forceApiMin = null
    const v = await systemApi.version() as unknown as Record<string, unknown>
    expect(v.api_version).toBe('1.0')
    const sent = lastSeen('/system/version')
    expect(sent?.headers['X-QT-Api-Min']).toBe('1.0')
    expect(sent?.status).toBe(200)
  })

  it('人为声明 X-QT-Api-Min:1.3 ⇒ 426 UPGRADE_REQUIRED(协商机制本身仍有效)', async () => {
    /* 02 §3.8:「请求可带 `X-QT-Api-Min` 声明所需最低次版本,服务端不满足回 `426 {code:'UPGRADE_REQUIRED'}`」。
       客户端已不会发 1.3,这里**人为构造**,验证机制没有被顺手拆掉。 */
    patch.forceApiMin = '1.3'
    const e = await fails(() => systemApi.version())
    patch.forceApiMin = null
    expect(e.status).toBe(426)
    expect(e.code).toBe('UPGRADE_REQUIRED')
    expect(e.detail.message).toContain('1.0')
  })
})

/* ══════════════ 鉴权 / 信封 / trace_id ══════════════ */

describe('鉴权与错误信封(00 §10)', () => {
  beforeAll(() => { patch.token = 'e2e-admin-token'; patch.noAuth = false; patch.forceApiMin = null })

  it('无令牌 ⇒ 401 UNAUTHORIZED,error 四键齐全', async () => {
    /* 00 §10 错误信封 + HTTP 状态映射。 */
    patch.noAuth = true
    const e = await fails(() => accountsApi.list())
    patch.noAuth = false
    expect(e.status).toBe(401)
    expect(e.code).toBe('UNAUTHORIZED')
    expect(typeof e.detail.message).toBe('string')
    expect(typeof e.detail.retryable).toBe('boolean')
    expect(typeof e.detail.needs_human).toBe('boolean')
    expect(e.reason).toBe('missing_token')
  })

  it('read 级令牌调 admin 端点 ⇒ 403 FORBIDDEN(level_insufficient)', async () => {
    /* 02 §3.4 通用段:级别 R/W/A(`api_clients.level`,高包含低);#88 `mail` 组是 A。 */
    patch.token = 'e2e-read-token'
    const e = await fails(() => settingsApi.get('mail'))
    patch.token = 'e2e-admin-token'
    expect(e.status).toBe(403)
    expect(e.code).toBe('FORBIDDEN')
    expect(e.reason).toBe('level_insufficient')
  })

  it('令牌 allow_accounts 收窄 ⇒ 403 account_not_allowed', async () => {
    /* 02 §3.4.7 / §3.4 通用:按 `api_clients.allow_accounts_json` 收窄。 */
    patch.token = 'e2e-limited-token'
    const e = await fails(() => accountsApi.get(ACC))
    patch.token = 'e2e-admin-token'
    expect(e.status).toBe(403)
    expect(e.reason).toBe('account_not_allowed')
  })

  it('不存在的账号 ⇒ 404 TARGET_NOT_FOUND', async () => {
    /* 00 §10:404 `TARGET_NOT_FOUND`。 */
    const e = await fails(() => accountsApi.get('zz99'))
    expect(e.status).toBe(404)
    expect(e.code).toBe('TARGET_NOT_FOUND')
  })

  it('🔴 R6-62 (b):成功响应也带 trace_id(不再由客户端回落本地 ULID)', async () => {
    /* 02 §3.4 通用段 R6-62 (b):「成功响应一律带 `trace_id`…取值 = 请求头 `X-Trace-Id`(调用方给了就采纳)、
       否则服务端本地生成」。第一轮断言的是「后端不回、客户端回落」—— 那是缺陷现状。 */
    const env = await requestEnvelope('/system/version')
    expect(typeof env.trace_id).toBe('string')
    expect((env.trace_id as string).length).toBeGreaterThan(0)
    const body = (await raw('/api/v1/system/version')).body
    expect(typeof body.trace_id).toBe('string')       // 服务端原样回的,不是客户端补的
  })

  it('🔴 R6-62 (b):服务端采纳调用方给的 X-Trace-Id,且同值写进 audit_log(拿界面上那串能查到审计行)', async () => {
    /* 02 §3.4 通用段 R6-62 (b) 逐字:「取值 = 请求头 `X-Trace-Id`(调用方给了就采纳)…**同值同时写进 `audit_log`**
       (拿着界面上那串就能直接查到审计行)」。#95 的查询参数里没有 trace_id(02 #95 参数列),故拉一窗本地找。 */
    const mine = '01M30E2ERECHECKTRACE000001'
    const r = await raw(`/api/v1/accounts/${ACC}/settings`, {
      method: 'PATCH', body: JSON.stringify({ capture_text: true }), headers: { 'X-Trace-Id': mine },
    })
    expect(r.status).toBe(200)
    expect(r.body.trace_id).toBe(mine)
    const rows = await auditApi.list({ limit: 50 })
    const hit = rows.items.find((x) => (x as unknown as Record<string, unknown>).trace_id === mine)
    expect(hit, `审计行里找不到 trace_id=${mine}`).toBeTruthy()
  })

  it('🔴 R6-62 (b) 例外①:#72 /system/health 的免鉴权摘要不注入 trace_id', async () => {
    /* 02 §3.4 通用段 R6-62 (b) 三条例外之①:「`#72 GET /system/health` 的免鉴权摘要键集逐字定死、**不注入** `trace_id`」;
       键集出自 02 #72:`{ok, agent, dockerd, winagent, user_agent}`(布尔级摘要,C-33)。 */
    const r = await raw('/api/v1/system/health', { token: null })
    expect(r.status).toBe(200)
    expect(Object.keys(r.body).sort()).toEqual(['agent', 'dockerd', 'ok', 'user_agent', 'winagent'])
    expect(r.body.trace_id).toBeUndefined()
  })

  it('🔴 R6-62 Ⅵ W1:#72 **带令牌**的全量响应同样不注入 trace_id(例外按 path 整端点)', async () => {
    /* 第三轮按现行口径**翻面**(docs-fix-7 W5 点名本条:上一轮断言的是 W1 **改前**的原句,「现在是红的但不是实现缺陷」)。
       02 §3.4 通用段例外①现行逐字:「**`#72 GET /system/health` 这个路径**(免鉴权摘要与带令牌全量**两种形态都不注入**
       `trace_id`;免鉴权摘要的键集另由本表 #72 行逐字定死)…排除是**按 path 整端点**做的、与带不带令牌无关」
       (00 §15g R6-62 Ⅵ W1;总控 2026-09-21 裁决维持)。
       判据不放松:这里仍先确认拿到的**确实是带令牌的全量体**(`checks` 在),再断言没有 `trace_id`。 */
    const r = await raw('/api/v1/system/health')
    expect(r.status).toBe(200)
    expect(r.body.checks, '这应当是带令牌的全量体').toBeTruthy()
    expect(r.body.agent?.version, '全量体的 agent 是对象').toBeTruthy()
    expect('trace_id' in r.body, '#72 这个路径两种形态都不注入 trace_id(02 §3.4 例外①)').toBe(false)
  })

  it('🔴 R6-62 Ⅷ Q7:#72 这个路径两种形态都**不记审计**(02 §2.2.1 唯一例外)', async () => {
    /* 02 §2.2.1 现行逐字:「把每次调用记 `audit_log`(`kind='api'`、`action='<METHOD> <path>'`…)**唯一例外 = `/system/health`
       这个路径**(免鉴权摘要与带令牌全量**两种形态都不记**)」(R6-62 Ⅷ Q7)。
       对照组:同一窗口里打一次 `/system/version`,它必须留下审计行 —— 证明「查不到」不是审计整体没写。 */
    await raw('/api/v1/system/health', { token: null })
    await raw('/api/v1/system/health')
    const ctl = await raw('/api/v1/system/version')
    expect(ctl.status).toBe(200)
    const rows = await auditApi.list({ kind: 'api', limit: 200 })
    const actions = rows.items.map((x) => String((x as unknown as Record<string, unknown>).action))
    expect(actions.some((a) => a.endsWith('/system/version')), '对照组 /system/version 没进审计(审计整体失效?)').toBe(true)
    expect(actions.filter((a) => a.includes('/system/health')), '/system/health 被记了审计').toEqual([])
  })

  it('C-42 分页:列表端点统一回 next_cursor(02 §3.4 通用段)', async () => {
    /* 02 §3.4 通用段逐字:「**分页/时间参数全端点统一(C-42)**:`?since=<ISO>&until=<ISO>&limit=50&cursor=<opaque>`,
       响应 `{ok:true, data:[…], next_cursor}`」。`/messages` 与 `/audit` 已经有了;
       会变长的几个列表(#1 accounts / #26 sessions / #57 inbox / #58 outbox)还没有 ⇒ 控制台翻不了页。
       另:`GET /accounts?limit=1` 现在**不截断**,`limit` 被静默忽略 —— 参数假装生效比不支持更危险。 */
    const paged = await raw('/api/v1/messages?limit=1')
    expect('next_cursor' in paged.body, '对照组:/messages 是有分页的').toBe(true)

    const miss: string[] = []
    for (const path of ['/api/v1/accounts', '/api/v1/sessions', '/api/v1/mail/inbox', '/api/v1/mail/outbox']) {
      const r = await raw(`${path}?limit=1`)
      /* 第三轮收紧:上一轮这里是 `if (r.status !== 200) continue` —— 端点 503/404 时会被静默跳过、用例照绿。
         四个端点现在都已装配,非 200 直接记为不符合。 */
      if (r.status !== 200) { miss.push(`${path}(HTTP ${r.status})`); continue }
      if (!('next_cursor' in r.body)) miss.push(`${path}(无 next_cursor)`)
      if (Array.isArray(r.body.data) && r.body.data.length > 1) miss.push(`${path}(limit=1 却回了 ${r.body.data.length} 行)`)
    }
    expect(miss, `这些列表端点不符合 C-42 统一分页:${miss.join('、')}`).toEqual([])
  })

  it('客户端把成功响应的 trace_id 记进 recentTraces(供「复制 trace」与日志联查)', async () => {
    /* 01 §2.5「错误提示末尾显示 trace_id 前 8 位」+ R6-62 (b) 的联查用途。这是客户端行为,不是后端契约。 */
    await systemApi.version()
    const traces = recentTraces()
    expect(traces.length).toBeGreaterThan(0)
    expect(typeof traces[traces.length - 1].traceId).toBe('string')
  })
})

/* ══════════════ E-02 幂等键载体 ══════════════ */

describe('E-02 幂等键载体 = body 的 idempotency_key(02 §3.4 #2 入参列)', () => {
  beforeAll(() => { patch.token = 'e2e-admin-token'; patch.noAuth = false; patch.forceApiMin = null })

  it('accountsApi.create 直接 201:幂等键在 body,且请求头里没有 X-Idempotency-Key', async () => {
    /* 02 §3.4 #2 入参列写的是 body 内 `idempotency_key`(幂等列 `key`);全 docs 不存在 `X-Idempotency-Key` 这个头。
       第一轮断言的是「只放头 ⇒ 400」的缺陷现状。 */
    const acc = await accountsApi.create({
      channel: 'qq', label: 'e2e-QQ-复测', login: { mode: 'qrcode', remember: false },
    })
    expect(acc.id).toMatch(/^qq\d+$/)
    expect(acc.channel).toBe('qq')
    expect(acc.state).toBe('created')
    const sent = lastSeen('/accounts')
    expect(sent?.method).toBe('POST')
    expect(sent?.headers['X-Idempotency-Key']).toBeUndefined()
    expect(JSON.parse(sent!.body!).idempotency_key).toBeTruthy()
  })

  it('不带 idempotency_key 的写请求仍被拒(机制没被拆掉):400 INVALID_ARGS idempotency_key_required', async () => {
    /* 02 §3.4 #2 幂等列 `key` = 必带 `idempotency_key`;#28 R6-52 明写 `reason=idempotency_key_required`。 */
    const r = await raw('/api/v1/accounts', {
      method: 'POST',
      body: JSON.stringify({ channel: 'qidian', label: 'e2e-无key', login: { mode: 'password', account: 'u', secret: 'p' } }),
    })
    expect(r.status).toBe(400)
    expect(r.body.code).toBe('INVALID_ARGS')
    expect(r.body.error.reason).toBe('idempotency_key_required')
    expect(r.body.error.details?.[0]?.pointer).toBe('/idempotency_key')
  })
})

/* ══════════════ 形状组 ══════════════ */

describe('形状:00 §7 对象与 02 §3.4 逐字键集', () => {
  beforeAll(() => { patch.token = 'e2e-admin-token'; patch.noAuth = false; patch.forceApiMin = null })

  it('#69 /resources 顶层平铺(R6-55),pending_expires_at 是 ISO 或 null(R6-58 (j))', async () => {
    /* 02 §3.4 R6-55:响应列给字面键集的端点顶层平铺 + `ok`;00 §7.6 + 02 #69 R6-58 (j):
       `pending_expires_at` 出参是 ISO 8601 字符串,库列 `slot_pending_expires_ms` 不得原样透出;
       `pending` / `pending_login_session_id` 无 pending 时是 **空串**不是 null。 */
    const r = await resourcesApi.get()
    expect(r.pools?.wsl?.total_mb).toBeGreaterThan(0)
    const slots = r.pools.windows.wechat_slots as unknown as Record<string, unknown>
    const pe = slots.pending_expires_at
    expect(pe === null || /^\d{4}-\d{2}-\d{2}T/.test(String(pe))).toBe(true)
    expect(slots.slot_pending_expires_ms).toBeUndefined()       // 库列名不得出参
    if (!slots.pending) expect(slots.pending).toBe('')
    if (!slots.pending_login_session_id) expect(slots.pending_login_session_id).toBe('')
  })

  it('#72 /system/health 带令牌:checks 全局 + checks.accounts 五项(R6-58 (y))', async () => {
    /* 02 #72 R6-58 (y):全局 `checks` 键集一个字不动,另补
       `checks.accounts: {"<account_id>": {"H04","H05","H06","H07","H08"}}`。 */
    const h = await systemApi.health() as unknown as Record<string, any>
    expect(h.checks).toBeTruthy()
    expect(h.checks.accounts?.[ACC]).toBeTruthy()
    expect(Object.keys(h.checks.accounts[ACC]).sort()).toEqual(['H04', 'H05', 'H06', 'H07', 'H08'])
  })

  it('#77 /system/metrics:hardware 与 ours 两组并排,逐字键集(02 #77)', async () => {
    /* 02 #77 逐字:`hardware`(整机)`{mem:{total_mb,used_mb,avail_mb,vmmem_mb}, cpu:{logical_cores,load_pct},
       disks:[{mount,total_mb,free_mb}]}` 与 `ours`(本程序)`{procs, accounts:[{id,anon_mb,current_mb,cpu_pct,quota_mb}],
       wechat:{chatlog_mb,wechat_pc_mb}, storage:{db_mb,media_mb,mail_mb,accounts_mb,backup_mb,vhdx_mb}}`;
       另 `disk_watermark` 含 `vhdx_grown_mb`。第一轮时后端**没有 hardware 组**(S-07)。 */
    const m = await resourcesApi.metrics() as unknown as Record<string, any>
    expect(m.hardware, '02 #77 定死的 hardware 组缺席').toBeTruthy()
    expect(Object.keys(m.hardware.mem).sort()).toEqual(['avail_mb', 'total_mb', 'used_mb', 'vmmem_mb'])
    expect(Object.keys(m.hardware.cpu).sort()).toEqual(['load_pct', 'logical_cores'])
    expect(Array.isArray(m.hardware.disks)).toBe(true)
    expect(m.ours).toBeTruthy()
    expect(m.ours.procs).toBeTruthy()
    expect(m.ours.wechat, '02 #77 的 ours.wechat 缺席').toBeTruthy()
    expect(Object.keys(m.ours.wechat).sort()).toEqual(['chatlog_mb', 'wechat_pc_mb'])
    expect(m.ours.storage, '02 #77 的 ours.storage 缺席').toBeTruthy()
    expect(Object.keys(m.ours.storage).sort())
      .toEqual(['accounts_mb', 'backup_mb', 'db_mb', 'mail_mb', 'media_mb', 'vhdx_mb'])
    expect(Array.isArray(m.ours.accounts)).toBe(true)
    expect('vhdx_grown_mb' in m.disk_watermark).toBe(true)
  })

  it('#76 kind=observed:后端两键 {data, targets},客户端 observedProbes() 两样都接住(R6-58 (dc))', async () => {
    /* 02 #76 R6-58 (dc):`?kind=observed` 出参**定死两键** `{data:[行], targets:["host:port"…]}`,
       行原样带 `probe_targets_observed.id` 与派生的 `in_config`。第一轮:客户端 requestList 只取 data、丢 targets。 */
    const viaClient = await systemApi.observedProbes()
    expect(Array.isArray(viaClient.items)).toBe(true)
    expect(Array.isArray(viaClient.targets), '客户端丢掉了 targets').toBe(true)
    const env = await requestEnvelope('/system/probes', { query: { kind: 'observed' } })
    expect(Array.isArray(env.data)).toBe(true)
    expect(Array.isArray(env.targets)).toBe(true)
    for (const row of (env.data as Record<string, unknown>[])) {
      expect(typeof row.id).toBe('number')            // 稳定 id,控制台不得用行下标
      expect(typeof row.in_config).toBe('boolean')
    }
  })

  it('#79b 从没跑过时回 {ok:true, data:null}(不是 404);客户端归一成空行表', async () => {
    /* 02 #79b R6-58 (ab) 逐字:「**从没跑过时回 `{ok:true, data:null}`,不是 `404`** —— 页面要能显示「暂无」」。
       ⚠️ 本条依赖「本进程尚未跑过自检」。flow 用例里的管理线会 POST 一轮,故此处两态都按规格断言:
       null 态 = 上面那句;对象态 = 02 #79 的一轮对象 `{redroid_boot_ms, napcat_ok, winagent_ok, probes}`。 */
    const env = await requestEnvelope('/system/selftest')
    expect(env.ok).toBe(true)
    if (env.data === null) {
      const r = await systemApi.selftestResult()
      expect(r.items).toEqual([])
      expect(r.run).toBeNull()
    } else {
      const d = env.data as Record<string, unknown>
      for (const k of ['redroid_boot_ms', 'napcat_ok', 'winagent_ok', 'probes']) expect(k in d).toBe(true)
      expect(selftestRows(d as never).length).toBeGreaterThan(0)
    }
  })

  it('#95 /audit 行 = R6-58 (ag) 的十列;cost_ms/ip/http_status 在 detail_json 里', async () => {
    /* 02 #95 R6-58 (ag) 逐字十列:`id,ts_ms,kind,transport,actor,action,account_id,trace_id,result_code,detail_json`。
       第一轮:前端 AuditRow 是另一套键(ts/code/op/path),与后端不相交,P-LOG 全列空。 */
    const r = await auditApi.list({ limit: 3 })
    expect(r.items.length).toBeGreaterThan(0)
    const row = r.items[0] as unknown as Record<string, unknown>
    for (const k of ['id', 'ts_ms', 'kind', 'transport', 'actor', 'action', 'account_id', 'trace_id', 'result_code', 'detail_json']) {
      expect(k in row, `审计行缺 R6-58 (ag) 定死的列:${k}`).toBe(true)
    }
    const d = auditDetail(r.items[0])
    expect(d, 'auditDetail() 解不出 detail_json').toBeTruthy()
    const apiRow = r.items.find((x) => (x as unknown as Record<string, unknown>).kind === 'api')
    if (apiRow) {
      const det = auditDetail(apiRow)
      expect(typeof det.http_status === 'number' || det.http_status === undefined).toBe(true)
    }
  })

  it('#95 查询参数只认白名单;客户端不再把 op/code/trace_id 发出去(02 #95 参数列)', async () => {
    /* 02 #95 参数列逐字:`?kind|actor|account_id|action|since|until|limit|cursor` + `?fmt=csv`。
       未知参数被 FastAPI 静默忽略 ⇒ 筛选器会「假装生效」,故客户端只能本地筛。 */
    await auditApi.list({ kind: 'api', limit: 5 })
    const sent = lastSeen('/audit')
    const q = new URL(sent!.url).searchParams
    for (const bad of ['op', 'code', 'trace_id']) expect(q.has(bad), `客户端仍在发服务端不认的参数 ${bad}`).toBe(false)
    expect(q.get('kind')).toBe('api')
  })

  it('#88 GET /settings/mail:R6-58 (ac) 定死的 scopes 四键', async () => {
    /* 02 #88 R6-58 (ac) 逐字:`{enabled, require_signature, template_version,
       scopes:{default|qidian|qq|wechat: {override, route_id, enabled, inbound, outbound}}}`。 */
    const s = await settingsApi.get<Record<string, any>>('mail')
    expect(Object.keys(s.scopes).sort()).toEqual(['default', 'qidian', 'qq', 'wechat'])
    expect(Object.keys(s.scopes.default).sort()).toEqual(['enabled', 'inbound', 'outbound', 'override', 'route_id'])
    expect(s.scopes.default.host, 'mock 那套平铺 host/port 不是规格形状').toBeUndefined()
  })

  it('#88 group 枚举里没有 compliance:合规告知走 #86/#87,该 group 打过去是 §10 信封的错误', async () => {
    /* 02 #88 group 枚举逐字 = `api|winagent|runtime|pool|bus|adapters|asr|ocr|messages|media|retention|events|mail|log|resources`
       —— **没有 compliance**;01-P3 的合规告知走 #86 `GET /system/notice` + #87 ack。
       客户端已删 `settingsApi.compliance`(见 client.ts 尾部注释),这里验后端也不认这个组。 */
    expect((settingsApi as unknown as Record<string, unknown>).compliance).toBeUndefined()
    expect((settingsApi as unknown as Record<string, unknown>).putCompliance).toBeUndefined()
    const r = await raw('/api/v1/settings/compliance')
    expect([400, 404]).toContain(r.status)
    expectErrorEnvelope(r.body)
  })

  it('#26 /sessions:last_msg_at 是 ISO(R6-62 (f)),库列 last_msg_ms 不出参', async () => {
    /* 02 #26 R6-62 (f) 逐字:「`Session` 出参的「最后消息时刻」键 = `last_msg_at`,ISO 8601;库列仍是 `sessions.last_msg_ms`」。 */
    /* 企点读库 poll 的游标从「账号启动时刻」起(不回灌历史),所以新起的进程里必须**先产生一条消息**
       才会有会话映射 —— 这是前置条件,不是对断言的放松。 */
    await commandsApi.run(ACC, {
      op: 'send_text', args: { session: '415011447', text: `建会话用 ${Date.now()}` },
      idempotency_key: `e2e-sess-${Date.now()}`,
    })
    const items = await waitFor(async () => {
      const r = await messagesApi.sessions()
      return r.items.length > 0 ? r.items : null
    }, 20000, 500)
    const row = items[0] as unknown as Record<string, unknown>
    expect(typeof row.last_msg_at).toBe('string')
    expect(String(row.last_msg_at)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/)
    expect(row.last_msg_ms, '库列名不得原样透出').toBeUndefined()
    expect(normalizeSession(items[0]).last_msg_at).toBe(row.last_msg_at)
  })

  it('#70 /resources/precheck:can_add 是 bool(R6-62 (f)),与 00 §7.6 的整数同名不同物', async () => {
    /* 02 #70 R6-62 (f):「本端点的 `can_add` 是 `bool`」;00 §7.6 `ResourcePool.can_add` 是按通道剩余**个数**。 */
    const p = await resourcesApi.precheck('qidian')
    expect(typeof p.can_add).toBe('boolean')
    const pool = await resourcesApi.get() as unknown as Record<string, any>
    expect(typeof pool.can_add.qidian).toBe('number')
  })

  it('Account 出参不带 settings 子对象(R6-62 (f));error_since_ms/deleted_ms 是已登记的 _ms 例外', async () => {
    /* 02 #3 R6-62 (f):「Account 出参里没有 `settings` 子对象」,账号级设置走 #22。
       00 §7.1 R6-62 (f):`error_since_ms` / `deleted_ms` **沿用毫秒整数、不改 ISO**,属已登记例外。 */
    const a = await accountsApi.get(ACC) as unknown as Record<string, unknown>
    expect(a.settings, 'R6-62 (f):Account 不该带 settings').toBeUndefined()
    expect('error_since_ms' in a).toBe(true)
    expect(a.error_since_ms === null || typeof a.error_since_ms === 'number').toBe(true)
    expect('deleted_ms' in a).toBe(true)
    expect(typeof a.created_at).toBe('string')
    expect(String(a.created_at)).toMatch(/[+-]\d{2}:\d{2}$/)      // 00 §6:新增时间键一律 ISO
  })

  it('#102 /system/public-endpoint 逐字键集(02 #102)', async () => {
    /* 02 #102 逐字:`{public_ip, public_ip_v6?, configured_domain?, checked_at, changed_at, probe:{url, unreachable_rounds}}`。
       前端自造的 `configured_host/dns_resolved_ip/matches/last_changed_at/history` 在 docs 里不存在。 */
    const p = await systemApi.publicEndpoint() as unknown as Record<string, unknown>
    for (const k of ['public_ip', 'configured_domain', 'checked_at', 'changed_at', 'probe']) expect(k in p).toBe(true)
    expect(Object.keys(p.probe as object).sort()).toEqual(['unreachable_rounds', 'url'])
    for (const bad of ['matches', 'history', 'configured_host', 'dns_resolved_ip', 'last_changed_at']) {
      expect(p[bad], `docs 里不存在的键 ${bad} 不该出现`).toBeUndefined()
    }
  })

  it('#21 /capabilities:capabilities_version 在信封顶层 + 响应头 X-QT-Capabilities-Version', async () => {
    /* 02 #21 逐字:响应 `{ok, capabilities_version, data:[…]}` 并带响应头 `X-QT-Capabilities-Version`。 */
    const c = await commandsApi.capabilities()
    expect(c.items.length).toBeGreaterThan(0)
    expect(c.version).toBeTruthy()
    const r = await raw('/api/v1/capabilities')
    expect(r.headers.get('X-QT-Capabilities-Version')).toBeTruthy()
  })

  it('#48 /messages 行不下发 needs_review(00 §7.4 消息列里没有它)', async () => {
    /* 00 §7.4 Message 与 02 #48 出参列都不含 `needs_review`。01 §4 有这个筛选控件但无数据源 —— 规格缺口,见交接。 */
    const r = await messagesApi.list({ account_id: ACC, limit: 5 })
    if (r.items.length > 0) {
      expect((r.items[0] as unknown as Record<string, unknown>).needs_review).toBeUndefined()
    }
  })
})

/* ══════════════ 后端新补的 12 个端点(第一轮的「缺失表」翻面) ══════════════ */

describe('后端新补端点:按 02 §3.4 的编号逐条验形状', () => {
  beforeAll(() => { patch.token = 'e2e-admin-token'; patch.noAuth = false; patch.forceApiMin = null })

  it('#24 GET /device-profiles/templates:逐字 [{profile_key,brand,model,release,weight}]', async () => {
    /* 02 #24 逐字:「内置档案库模板列表 `[{profile_key, brand, model, release, weight}]`(C-40;字段名统一 `profile_key`)」。 */
    const r = await commandsApi.deviceProfiles()
    expect(r.items.length).toBeGreaterThan(0)
    expect(Object.keys(r.items[0] as object).sort()).toEqual(['brand', 'model', 'profile_key', 'release', 'weight'])
  })

  it('#74 GET /system/env:WinAgent 不可达时 200 + windows:null + windows_error,不编造(R6-62 (g))', async () => {
    /* 02 #74 R6-62 (g) 逐字:「WinAgent 不可达时**不回 5xx**,而是 `200` + `windows: null` + `windows_error:"<原因>"`
       (WSL 侧照常采到的那半原样给);`wslconfig` 同理不可达即 `null`…🔴 **一律不编造**」。 */
    const env = await systemApi.env() as unknown as Record<string, any>
    expect('windows' in env).toBe(true)
    expect('wsl' in env).toBe(true)
    if (env.windows === null) expect(typeof env.windows_error).toBe('string')   // 缺采集方必须如实报缺
    expect(env.wsl).toBeTruthy()
    expect('mtu' in env.wsl).toBe(true)
    expect('kernel_release' in env.wsl).toBe(true)
    expect('wslconfig' in env).toBe(true)
  })

  it('#75 POST /system/probe:mode=sample 出参无 run_id(R6-58 (de));mode=full 才有 run_id', async () => {
    /* 02 #75 R6-58 (de) 逐字:「`mode:'sample'` 的出参没有 `run_id`(逐字 `{sampled_at, duration_s, rows, skipped}`),
       只有 `full`/`probe` 才有 `run_id` —— 两个 mode 共用一个路径但出参形状不同,调用方别按同一个形状解」。 */
    const s = await raw('/api/v1/system/probe', { method: 'POST', body: JSON.stringify({ mode: 'sample', duration_s: 1 }) })
    expect(s.status).toBe(200)
    expect(s.body.run_id, 'sample 模式不该有 run_id').toBeUndefined()
    for (const k of ['sampled_at', 'duration_s', 'rows', 'skipped']) expect(k in s.body).toBe(true)

    const f = await raw('/api/v1/system/probe', { method: 'POST', body: JSON.stringify({ mode: 'full', targets: ['example.com:443'] }) })
    expect(f.status).toBe(200)
    expect(typeof f.body.run_id).toBe('string')
    expect(Array.isArray(f.body.results)).toBe(true)
    // R6-62 (g):未装配探测器时该侧逐目标记 SKIPPED,**缺省不出网**
    for (const row of f.body.results) expect(['OK', 'FAIL', 'SKIPPED', 'DEGRADED']).toContain(row.status)
  })

  it('#78 POST /system/selftest ⇒ 202 {run_id}(不是 job_id);无执行体项如实 null + skipped(R6-62 (g))', async () => {
    /* 02 #78 R6-62 (g) 逐字:「出参是 `run_id` 不是 `job_id` —— 本端点**不走 `jobs` 表**…
       `redroid_boot_ms` / `napcat_ok` 如实回 `null`,并在 `data.skipped` 里逐项写 `{step, reason:"no_executor"}`,不拿假值充数」。 */
    const r = await raw('/api/v1/system/selftest', { method: 'POST' })
    expect(r.status).toBe(202)
    expect(typeof r.body.run_id).toBe('string')
    expect(r.body.job_id, '#78 不走 jobs 表,不该回 job_id').toBeUndefined()
    const got = await waitFor(async () => {
      const e = await requestEnvelope('/system/selftest', { query: { run_id: r.body.run_id } })
      return e.data ? e : null
    }, 20000, 400)
    const d = got.data as Record<string, any>
    expect('redroid_boot_ms' in d).toBe(true)
    expect('napcat_ok' in d).toBe(true)
    expect('winagent_ok' in d).toBe(true)
    expect(Array.isArray(d.probes)).toBe(true)
    if (d.redroid_boot_ms === null) {
      const sk = (d.skipped ?? []) as Record<string, string>[]
      expect(sk.some((x) => x.step === 'redroid_boot' && !!x.reason), '如实 null 时必须在 skipped 里写明原因').toBe(true)
    }
  })

  it('#80 POST /system/diagnostics ⇒ 202 {job_id};with_screenshots:true 明着 400(R6-62 (g))', async () => {
    /* 02 #80 逐字 `202 {job_id}`;R6-62 (g):「`with_screenshots:true` **没有执行体 ⇒ 明着 `400`**
       (`reason=screenshots_not_implemented`),不静默按 `false` 出包;排除清单…原样回在 `result.excluded` 里」。 */
    const ok = await systemApi.diagnostics(false)
    expect(ok.job_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
    const e = await fails(() => systemApi.diagnostics(true))
    expect(e.status).toBe(400)
    expect(e.reason).toBe('screenshots_not_implemented')
    const job = await waitFor(async () => {
      const j = await jobsApi.get(ok.job_id) as unknown as Record<string, any>
      return ['succeeded', 'failed'].includes(j.state) ? j : null
    }, 30000, 500)
    expect(job.state).toBe('succeeded')
    expect(Array.isArray(job.result.excluded)).toBe(true)
  })

  it('#84 POST /system/wsl-restart:mode=shutdown 不带 confirm ⇒ 400(基线 §11.6 [NOSHUTDOWN])', async () => {
    /* 02 #84 逐字:「`{mode:'terminate|shutdown'}`,`shutdown` 必须带 `confirm:true`(用户已在控制台确认,基线 §11.6 [NOSHUTDOWN])」。
       「一条请求都没发到 WinAgent」的断言在 python 侧(`tests/e2e/test_admin_line.py`),TS 侧读不到 FakeWinAgent 的 calls。 */
    const r = await raw('/api/v1/system/wsl-restart', { method: 'POST', body: JSON.stringify({ mode: 'shutdown' }) })
    expect(r.status).toBe(400)
    expectErrorEnvelope(r.body)
    expect(r.body.error.reason).toBe('confirm_required')
  })

  it('#85 POST /system/docker-proxy:无执行体时诚实 503,不假装写过 proxy.conf(R6-62 (g))', async () => {
    /* 02 #85 R6-62 (g) 逐字:「未装配时诚实回 `503 NOT_READY`(`reason=docker_proxy_applier_missing`),
       **不假装写过 `proxy.conf`**;有 running 账号且未带 `confirm` 时先回 `202 {pending:true}`」。 */
    const r = await raw('/api/v1/system/docker-proxy', { method: 'POST', body: JSON.stringify({ enable: true }) })
    expect([202, 503]).toContain(r.status)
    if (r.status === 503) {
      expectErrorEnvelope(r.body)
      expect(r.body.error.reason).toBe('docker_proxy_applier_missing')
    } else {
      expect(r.body.pending).toBe(true)
    }
  })

  it('#86 GET /system/notice 对 loopback 免鉴权 + #87 ack 写回(02 #86/#87)', async () => {
    /* 02 #86 逐字:「合规告知文案与版本 `{notice_version, text}`(01-P3)」,R(**免鉴权 loopback**);
       02 #87:`{notice_version}` → 写 `settings compliance.ack_ms / compliance.notice_version`。 */
    const anon = await raw('/api/v1/system/notice', { token: null })
    expect(anon.status).toBe(200)
    expect(typeof anon.body.notice_version).toBe('string')
    expect(typeof anon.body.text).toBe('string')
    const n = await systemApi.notice()
    await systemApi.noticeAck(n.notice_version)
    const after = await systemApi.notice() as unknown as Record<string, unknown>
    expect(after.acked_version).toBe(n.notice_version)
  })

  it('#87 版本对不上 ⇒ 400,不静默记成已勾选', async () => {
    /* 02 #87:「版本升级后需重新勾选」—— 用一个不存在的版本号 ack,必须被拒,否则合规判据失真。 */
    const r = await raw('/api/v1/system/notice/ack', { method: 'POST', body: JSON.stringify({ notice_version: 'not-a-version' }) })
    expect(r.status).toBe(400)
    expectErrorEnvelope(r.body)
  })

  it('#90 GET /settings/api-clients:列表不含 secret(02 #90)', async () => {
    /* 02 #90 逐字:「列表(不含 secret)」。 */
    const r = await settingsApi.apiClients()
    expect(r.items.length).toBeGreaterThan(0)
    for (const row of r.items as unknown as Record<string, unknown>[]) {
      expect(row.secret).toBeUndefined()
      expect(row.token).toBeUndefined()
      expect(row.secret_hash, '连 secret_hash 都不该出表').toBeUndefined()
    }
  })

  it('#36 POST /broadcast/commands:不接受 "*",须显式账号清单(02 #36)', async () => {
    /* 02 #36 逐字:「`{account_ids:[…], op, args, idempotency_key, confirm}`;**不接受 `*`**」。 */
    const r = await raw('/api/v1/broadcast/commands', {
      method: 'POST',
      body: JSON.stringify({ account_ids: ['*'], op: 'list_sessions', args: {}, idempotency_key: `e2e-bc-star-${Date.now()}` }),
    })
    expect(r.status).toBe(400)
    expectErrorEnvelope(r.body)
  })

  it('#51 POST /messages/export:jsonl ⇒ 202 {job_id};eml / with_media:zip 明着 400(R6-62 (g))', async () => {
    /* 02 #51 R6-62 (g) 逐字:「`fmt:'jsonl'` 与 `'csv'` 已实现;**`fmt:'eml'` 与 `with_media:'zip'` 本期没有执行体 ⇒ 明着 `400`**
       (`reason=fmt_not_implemented` / `with_media_not_implemented`),**不静默降级成别的格式**」。 */
    const ok = await messagesApi.export({ filter: {}, fmt: 'jsonl', with_media: 'none' })
    expect(ok.job_id).toBeTruthy()
    const e1 = await fails(() => messagesApi.export({ filter: {}, fmt: 'eml', with_media: 'none' }))
    expect(e1.status).toBe(400)
    expect(e1.reason).toBe('fmt_not_implemented')
    const e2 = await fails(() => messagesApi.export({ filter: {}, fmt: 'jsonl', with_media: 'zip' }))
    expect(e2.status).toBe(400)
    expect(e2.reason).toBe('with_media_not_implemented')
  })

  it('#104 POST /mail/templates/{id}/preview:路径带 {id}(02 #104),老路径不存在', async () => {
    /* 02 #104 逐字路径 = `POST /mail/templates/{id}/preview`。第一轮后端实现成了不带 `{id}` 的版本。 */
    const old = await raw('/api/v1/mail/templates/preview', { method: 'POST', body: JSON.stringify({}) })
    expect([404, 405]).toContain(old.status)
    expectErrorEnvelope(old.body)
    const r = await raw('/api/v1/mail/templates/default/preview', { method: 'POST', body: JSON.stringify({ ctx: { text: 'x' } }) })
    expect(r.status).not.toBe(404)      // 路径存在(参数对不对是另一回事)
  })
})

/* ══════════════ E-03 / E-04 指令结果 ══════════════ */

describe('E-03/E-04 #28 CommandResult:业务结果 ≠ 传输错误(02 #28 R6-52)', () => {
  beforeAll(() => { patch.token = 'e2e-admin-token'; patch.noAuth = false; patch.forceApiMin = null })

  it('业务失败码 ⇒ HTTP 200 + ok:false,客户端**返回**完整 CommandResult 而不是抛异常', async () => {
    /* 02 #28 R6-52 逐字:「**指令的业务结果**(`DELIVERED`/`SEND_FAILED`/…/`GATE_BLOCKED`/…)一律 **`200` + `CommandResult`
       (`ok` 可为 false)** —— HTTP 状态说的是「这次调用有没有被受理执行」,结果码说的是「执行成没成」」。
       00 §7.3 CommandResult 的键:`{ok, code, data, cost_ms, trace_id, source, state_before, state_after, error}`。
       第一轮:客户端 `if (!res.ok || env.ok === false) throw` 把业务码当传输失败抛掉(E-03),
       且 `request()` 的「有 data 就返回 data」把 CommandResult 拆成它的 data(E-04)。 */
    /* 造一个**业务**失败:把账号停掉再发。假 RPA 对任何 native_id 都会发成功,
       所以只能从账号态入手(→ LOGIN_REQUIRED / NOT_APPLICABLE / SEND_FAILED,哪个码都行,形状要求一样)。 */
    await accountsApi.disable(ACC)
    let out: Record<string, any>
    try {
      out = await commandsApi.run(ACC, {
        op: 'send_text',
        args: { session: '415011447', text: 'E-03 复测' },
        idempotency_key: `e2e-e03-${Date.now()}`,
      }) as unknown as Record<string, any>
    } finally {
      await accountsApi.enable(ACC)
    }
    expect(out!, '业务失败不该抛异常').toBeTruthy()
    expect(out.ok).toBe(false)
    expect(typeof out.code).toBe('string')
    expect(typeof out.cost_ms).toBe('number')
    expect(typeof out.trace_id).toBe('string')
    expect(out.error).toBeTruthy()
    expect(typeof out.error.message).toBe('string')
    expect(typeof out.error.retryable).toBe('boolean')
    expect(typeof out.error.needs_human).toBe('boolean')
    expect('state_before' in out).toBe(true)
    expect('state_after' in out).toBe(true)
    const sent = lastSeen('/commands')
    expect(sent?.status, '业务失败的 HTTP 状态必须是 200').toBe(200)
    await ensureAccountRunning(ACC)          // 还原,不影响后续用例
  }, 120000)
})

/* ══════════════ 202 job 契约 + 横切信封 ══════════════ */

describe('202 job 契约(§11.21 [JOB])与横切错误信封', () => {
  beforeAll(() => { patch.token = 'e2e-admin-token'; patch.noAuth = false; patch.forceApiMin = null })

  it('#109 cleanup/run ⇒ 202 {ok, job_id} 顶层平铺', async () => {
    /* 02 #109 R6-24 逐字:「→ **`202 {job_id}`**(`jobs.kind='system_cleanup'`,经 #107 查)」。 */
    const r = await resourcesApi.cleanupRun()
    expect(r.job_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
  })

  it('#25 calibrate ⇒ 202 {job_id};#107 的时间键是 ISO 的 *_at(R6-62 (f))', async () => {
    /* 02 #107 R6-62 (f) 逐字:「出参三个时间键逐字 `created_at`/`updated_at`/`expires_at`,值是 ISO 8601 带 `+08:00`;
       **库列仍是 `jobs.created_ms`/`updated_ms`/`expires_ms`**…照库列名写断言会「无此键」」。
       第一轮:后端出参是 `*_ms`(S-05)。 */
    const c = await resourcesApi.calibrate(false)
    expect(c.job_id).toBeTruthy()
    const job = await jobsApi.get(c.job_id!) as unknown as Record<string, unknown>
    expect(job.job_id).toBe(c.job_id)
    expect(['queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired']).toContain(job.state)
    expect(typeof job.progress).toBe('number')
    expect(typeof job.created_at, '#107 的 created_at 缺席').toBe('string')
    expect(String(job.created_at)).toMatch(/[+-]\d{2}:\d{2}$/)
    expect(typeof job.updated_at).toBe('string')
    expect('expires_at' in job).toBe(true)
    expect(job.created_ms, '库列名不得原样透出').toBeUndefined()
    expect(job.updated_ms).toBeUndefined()
    expect(normalizeJob(job as never).created_at).toBe(job.created_at)
  })

  it('未知路由 ⇒ 404 走 00 §10 信封(不再是 FastAPI 的 {detail})', async () => {
    /* 00 §10:错误响应统一 `{ok:false, code, error:{…}, trace_id}`。第一轮漏出 FastAPI 默认 `{"detail":"Not Found"}`,
       控制台只能退化成「请求失败(HTTP 404)」的兜底文案、trace_id 也对不上日志。 */
    const r = await raw('/api/v1/no-such-endpoint')
    expect(r.status).toBe(404)
    expectErrorEnvelope(r.body)
    expect(r.body.detail, '不该再漏出 FastAPI 的 detail').toBeUndefined()
  })

  it('方法不对 ⇒ 405 走 §10 信封,且保留 Allow 头', async () => {
    /* 00 §10 同上;HTTP 语义要求 405 带 `Allow`。 */
    const r = await raw('/api/v1/system/version', { method: 'DELETE' })
    expect(r.status).toBe(405)
    expectErrorEnvelope(r.body)
    expect(r.headers.get('Allow')).toBeTruthy()
  })

  it('入参校验失败 ⇒ 422/400 走 §10 信封,details 带 pointer', async () => {
    /* 00 §10 + 02 §3.4「400 `INVALID_ARGS`」;逐条错误给 `details[].pointer`(照 #28/#2 的 details 形状)。 */
    const r = await raw('/api/v1/accounts', { method: 'POST', body: JSON.stringify({ channel: 123 }) })
    expect([400, 422]).toContain(r.status)
    expectErrorEnvelope(r.body)
    expect(r.body.code).toBe('INVALID_ARGS')
    expect(Array.isArray(r.body.error.details)).toBe(true)
    expect(r.body.error.details[0].pointer).toBeTruthy()
  })

  it('客户端把任何非信封响应体都归一成 ApiFailure(不让页面看到裸 detail)', async () => {
    /* 01 §2.5:失败一律走 `PageState` 错误态 + 结果码 + trace 前 8 位。这是客户端侧的兜底行为。 */
    const e = await fails(() => request('/no-such-endpoint'))
    expect(e).toBeInstanceOf(ApiFailure)
    expect(typeof e.detail.message).toBe('string')
    expect(e.detail.message.length).toBeGreaterThan(0)
  })
})
