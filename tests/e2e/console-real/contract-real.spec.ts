/**
 * 控制台 API 客户端 ↔ 真 Agent(全假后端)契约联调。
 * **只测不修**:用的是 `console/src/api/` 的真源码,断言写的是「实测到的真实行为」,不迁就任何一侧。
 */
import { describe, it, expect, beforeAll } from 'vitest'
import { patch } from './harness'
import { ApiFailure, requestEnvelope, API_MIN_VERSION } from '@/api/http'
import { accountsApi, systemApi, settingsApi, commandsApi, messagesApi, auditApi, resourcesApi, jobsApi } from '@/api/client'

const ACC = 'qd01'

async function fails(fn: () => Promise<unknown>): Promise<ApiFailure> {
  try {
    await fn()
  } catch (e) {
    if (e instanceof ApiFailure) return e
    throw e
  }
  throw new Error('期望抛 ApiFailure,实际成功返回')
}

describe('E-01 版本协商头', () => {
  it('控制台声明的 API_MIN_VERSION 是 1.3', () => {
    expect(API_MIN_VERSION).toBe('1.3')
  })

  it('带 X-QT-Api-Min:1.3 打真后端 ⇒ 426 UPGRADE_REQUIRED(控制台全功能不可用)', async () => {
    patch.stripApiMin = false
    const e = await fails(() => systemApi.version())
    expect(e.status).toBe(426)
    expect(e.code).toBe('UPGRADE_REQUIRED')
    expect(e.detail.message).toContain('1.0')
  })

  it('摘掉该头后同一调用 200,后端 api_version=1.0', async () => {
    patch.stripApiMin = true
    const v = await systemApi.version() as Record<string, unknown>
    expect(v.api_version).toBe('1.0')
  })
})

describe('鉴权与错误信封解析', () => {
  beforeAll(() => { patch.stripApiMin = true; patch.token = 'e2e-admin-token' })

  it('无令牌 ⇒ 401 UNAUTHORIZED,error 四键齐全', async () => {
    patch.token = ''
    const e = await fails(() => accountsApi.list())
    patch.token = 'e2e-admin-token'
    expect(e.status).toBe(401)
    expect(e.code).toBe('UNAUTHORIZED')
    expect(typeof e.detail.message).toBe('string')
    expect(typeof e.detail.retryable).toBe('boolean')
    expect(typeof e.detail.needs_human).toBe('boolean')
    expect(e.reason).toBe('missing_token')
  })

  it('read 级令牌调 admin 端点 ⇒ 403 FORBIDDEN(reason=level_insufficient)', async () => {
    patch.token = 'e2e-read-token'
    const e = await fails(() => settingsApi.get('mail'))
    patch.token = 'e2e-admin-token'
    expect(e.status).toBe(403)
    expect(e.code).toBe('FORBIDDEN')
    expect(e.reason).toBe('level_insufficient')
  })

  it('令牌 allow_accounts 收窄 ⇒ 403 account_not_allowed', async () => {
    patch.token = 'e2e-limited-token'
    const e = await fails(() => accountsApi.get(ACC))
    patch.token = 'e2e-admin-token'
    expect(e.status).toBe(403)
    expect(e.reason).toBe('account_not_allowed')
  })

  it('不存在的账号 ⇒ 404 TARGET_NOT_FOUND', async () => {
    const e = await fails(() => accountsApi.get('zz99'))
    expect(e.status).toBe(404)
    expect(e.code).toBe('TARGET_NOT_FOUND')
  })

  it('trace_id:成功响应后端不回 trace_id,客户端回落到本地 ULID(26 位)', async () => {
    const env = await requestEnvelope('/system/version')
    expect(typeof env.trace_id).toBe('string')
    expect((env.trace_id as string).length).toBe(26)
  })
})

describe('E-02 幂等键载体', () => {
  beforeAll(() => { patch.stripApiMin = true })

  it('accountsApi.create 只把 key 放头 ⇒ 400 idempotency_key_required', async () => {
    patch.idemIntoBody = false
    const e = await fails(() => accountsApi.create({
      channel: 'qidian', label: 'e2e-临时', login: { mode: 'password', account: 'u', secret: 'p', remember: true },
    }))
    expect(e.status).toBe(400)
    expect(e.code).toBe('INVALID_ARGS')
    expect(e.reason).toBe('idempotency_key_required')
    expect(e.detail.details?.[0]?.pointer).toBe('/idempotency_key')
  })

  it('把同一个 key 并进 body ⇒ 201 建号成功(证明差异只在载体)', async () => {
    patch.idemIntoBody = true
    const acc = await accountsApi.create({
      channel: 'qq', label: 'e2e-QQ', login: { mode: 'qrcode', remember: false },
    })
    expect(acc.id).toMatch(/^qq\d+$/)
    expect(acc.channel).toBe('qq')
    expect(acc.state).toBe('created')
  })
})

describe('列表/单对象信封与字段口径', () => {
  beforeAll(() => { patch.stripApiMin = true; patch.idemIntoBody = true })

  it('#69 /resources 顶层平铺(无 data 包裹),客户端 request() 能吃下', async () => {
    const r = await resourcesApi.get()
    expect(r.pools?.wsl?.total_mb).toBeGreaterThan(0)
    expect(r.pools?.windows?.wechat_slots).toBeTruthy()
    // 前端 WechatSlots 期望 pending_expires_at 是 ISO 字符串或 null
    const pe = r.pools.windows.wechat_slots.pending_expires_at
    expect(pe === null || /^\d{4}-\d{2}-\d{2}T/.test(String(pe))).toBe(true)
  })

  it('#72 /system/health:后端给 checks.accounts(per-account 五项)', async () => {
    const h = await systemApi.health() as Record<string, any>
    expect(h.checks).toBeTruthy()
    expect(h.checks.accounts?.[ACC]).toBeTruthy()
    expect(Object.keys(h.checks.accounts[ACC]).sort()).toEqual(['H04', 'H05', 'H06', 'H07', 'H08'])
  })

  it('#77 /system/metrics:后端给 ours.procs_detail / ours.procs', async () => {
    const m = await resourcesApi.metrics() as Record<string, any>
    expect(m.ours).toBeTruthy()
    expect(Array.isArray(m.ours.procs_detail)).toBe(true)
    expect(m.ours.procs).toBeTruthy()
  })

  it('#76b observed:后端两键 {data, targets},但客户端 requestList 只取 data、丢掉 targets', async () => {
    const viaClient = await systemApi.probes('observed')
    expect(Array.isArray(viaClient.items)).toBe(true)
    expect((viaClient as Record<string, unknown>).targets).toBeUndefined()
    const env = await requestEnvelope('/system/probes', { query: { kind: 'observed' } })
    expect(Array.isArray(env.targets)).toBe(true)          // 后端确实给了,前端接不住
  })

  it('#79b selftest 没跑过 ⇒ {ok:true, data:null},客户端归一成空数组', async () => {
    const env = await requestEnvelope('/system/selftest')
    expect(env.ok).toBe(true)
    expect(env.data).toBeNull()
    const r = await systemApi.selftestResult()
    expect(r.items).toEqual([])
  })

  it('#95 /audit 行的键集 = 库列(ts_ms/result_code/detail_json),与前端 AuditRow(ts/code/op/path) 不相交', async () => {
    const r = await auditApi.list({ limit: 3 })
    expect(r.items.length).toBeGreaterThan(0)
    const row = r.items[0] as unknown as Record<string, unknown>
    expect(Object.keys(row)).toEqual(expect.arrayContaining(['ts_ms', 'result_code', 'detail_json', 'action']))
    expect(row.ts).toBeUndefined()
    expect(row.code).toBeUndefined()
    expect(row.op).toBeUndefined()
  })

  it('#88 settings/mail 的 scopes 是 {override,route_id,enabled,inbound,outbound},不是 mock 的平铺 host/port', async () => {
    const s = await settingsApi.get<Record<string, any>>('mail')
    expect(Object.keys(s.scopes).sort()).toEqual(['default', 'qidian', 'qq', 'wechat'])
    expect(Object.keys(s.scopes.default).sort()).toEqual(['enabled', 'inbound', 'outbound', 'override', 'route_id'])
    expect(s.scopes.default.host).toBeUndefined()
  })

  it('#26 /sessions 行给 last_msg_at,前端 SessionRow 期望 last_ts', async () => {
    const r = await messagesApi.sessions()
    expect(r.items.length).toBeGreaterThan(0)
    const row = r.items[0] as unknown as Record<string, unknown>
    expect(row.last_msg_at).toBeTruthy()
    expect(row.last_ts).toBeUndefined()
  })

  it('#102 public-endpoint 给 configured_domain/changed_at/probe,前端 PublicEndpoint 期望 configured_host/matches/history', async () => {
    const p = await systemApi.publicEndpoint() as unknown as Record<string, unknown>
    expect('configured_domain' in p).toBe(true)
    expect('probe' in p).toBe(true)
    expect(p.matches).toBeUndefined()
    expect(p.history).toBeUndefined()
    expect(p.configured_host).toBeUndefined()
  })

  it('#21 /capabilities:客户端要的 capabilities_version 在顶层,能取到', async () => {
    const c = await commandsApi.capabilities()
    expect(c.items.length).toBeGreaterThan(0)
    expect(c.version).toBeTruthy()
  })
})

describe('控制台调用的端点在真后端缺失(404)', () => {
  beforeAll(() => { patch.stripApiMin = true })
  const missing: [string, () => Promise<unknown>][] = [
    ['#24 GET /device-profiles/templates', () => commandsApi.deviceProfiles()],
    ['#74 GET /system/env', () => systemApi.env()],
    ['#86 GET /system/notice', () => systemApi.notice()],
    ['#90 GET /settings/api-clients', () => settingsApi.apiClients()],
    ['GET /settings/compliance(docs 无此 group)', () => settingsApi.compliance()],
  ]
  for (const [name, fn] of missing) {
    it(`${name} ⇒ 404`, async () => {
      const e = await fails(fn)
      expect(e.status).toBe(404)
    })
  }
})

describe('E-03 #28 的 CommandResult:业务结果码被客户端当成请求失败抛掉', () => {
  beforeAll(() => { patch.stripApiMin = true; patch.idemIntoBody = true })

  it('账号非 running 时 send_text ⇒ HTTP 200 + {ok:false, code:业务码},客户端却抛 ApiFailure 并丢掉 CommandResult', async () => {
    await accountsApi.disable('qd01')
    const key = `e2e-e03-${Date.now()}`
    // 先用裸 fetch 取真实 HTTP 状态(证明后端按 R6-52 回 200)
    const raw = await fetch('/api/v1/accounts/qd01/commands', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'send_text', args: { session: '415011447', text: 'E-03 取证' }, idempotency_key: key }),
    })
    const body = await raw.json() as Record<string, any>
    expect(raw.status).toBe(200)
    expect(body.ok).toBe(false)
    expect(['SEND_FAILED', 'SEND_CALLED_BUT_UNCONFIRMED', 'LOGIN_REQUIRED', 'NOT_APPLICABLE', 'GATE_BLOCKED']).toContain(body.code)
    expect(body.cost_ms).toBeGreaterThanOrEqual(0)

    // 同一形状经控制台客户端 ⇒ 抛异常,页面拿不到 code/data/cost_ms/state_after
    const e = await fails(() => commandsApi.run('qd01', {
      op: 'send_text', args: { session: '415011447', text: 'E-03 取证2' }, idempotency_key: `${key}-2`,
    }))
    expect(e.status).toBe(200)                       // HTTP 成功,却走了失败分支
    expect((e as unknown as { detail: Record<string, unknown> }).detail.data).toBeUndefined()
    await accountsApi.enable('qd01')
  }, 120000)
})

describe('202 job 契约(#25/#71/#109 → #107)', () => {
  beforeAll(() => { patch.stripApiMin = true; patch.idemIntoBody = true })

  it('#109 cleanup/run ⇒ 202 {ok, job_id} 顶层平铺,客户端取得 job_id', async () => {
    const r = await resourcesApi.cleanupRun()
    expect(r.job_id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
  })

  it('#25 calibrate ⇒ 202 {job_id};#107 GET /jobs/{id} 的时间键是 *_ms 毫秒,不是 docs #107 要求的 *_at ISO', async () => {
    const c = await resourcesApi.calibrate(false)
    expect(c.job_id).toBeTruthy()
    const job = await jobsApi.get(c.job_id!) as unknown as Record<string, unknown>
    expect(job.job_id).toBe(c.job_id)
    expect(['queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired']).toContain(job.state)
    expect(typeof job.progress).toBe('number')
    expect(typeof job.created_ms).toBe('number')     // 后端实际
    expect(job.created_at).toBeUndefined()           // docs #107 / 前端 Job 期望的键缺席
    expect(job.updated_at).toBeUndefined()
    expect(job.expires_at).toBeUndefined()
  })

  it('#71 diagnostics 与 #51 messages/export 在真后端不存在(404 / 405)', async () => {
    const e1 = await fails(() => systemApi.diagnostics(false))
    expect(e1.status).toBe(404)
    const e2 = await fails(() => messagesApi.export({ filter: {}, format: 'csv', include_media: false }))
    expect(e2.status).toBe(405)                       // 被 /messages/{message_id} 的 GET 路由吃掉
  })

  it('未知路由/405 的响应体不是 00 §10 信封,是 FastAPI 默认 {detail}', async () => {
    const raw = await fetch('/api/v1/no-such-endpoint')
    expect(raw.status).toBe(404)
    const body = await raw.json() as Record<string, unknown>
    expect(body.detail).toBe('Not Found')
    expect(body.ok).toBeUndefined()
    expect(body.code).toBeUndefined()
    expect(body.error).toBeUndefined()
  })
})
