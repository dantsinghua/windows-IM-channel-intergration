/**
 * 业务线 + WS 事件流联调(真 Agent,全假后端)。
 * 每一步都由控制台客户端源码发起、真后端响应。E-01/E-02 两处硬伤按「模拟已修」打开开关,否则一步都走不下去。
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { patch, WS_BASE, waitFor } from './harness'
import { accountsApi, commandsApi, messagesApi } from '@/api/client'
import { requestEnvelope } from '@/api/http'
import { EventsClient, ALL_EVENTS, backoffMs } from '@/api/ws'
import type { QtEvent } from '@/api/types'

const ACC = 'qd01'
const TOKEN = 'e2e-admin-token'
const WS_URL = `${WS_BASE}/api/v1/events?token=${TOKEN}`

/** 把账号拉回 running(create→start→login→running,05 §2.1.1 的登录阶段) */
async function ensureRunning(): Promise<void> {
  const a = await accountsApi.get(ACC)
  if (a.state === 'running') return
  if (!a.enabled) await accountsApi.enable(ACC)
  await accountsApi.start(ACC)
  await waitFor(async () => {
    const x = await accountsApi.get(ACC)
    if (x.state === 'login_required') {
      await accountsApi.login(ACC, { secret: 'pwd123', remember: true })
      return false
    }
    return x.state === 'running'
  }, 40000, 500)
}

function collector() {
  const events: QtEvent[] = []
  const statuses: string[] = []
  const client = new EventsClient(
    { onEvent: (ev) => events.push(ev), onStatus: (s) => statuses.push(s), onReplayTruncated: () => {} },
    { url: WS_URL, jitter: () => 0.01, heartbeatMs: 3000, deadAfterMs: 120000 },
  )
  return { events, statuses, client }
}

describe('完整业务线:企点账号 → 登录阶段 → running → send_text → DELIVERED → 消息可查', () => {
  beforeAll(async () => {
    patch.stripApiMin = true
    patch.idemIntoBody = true
    await ensureRunning()
  }, 90000)

  it('账号 running 且身份回填;时间键是 ISO+08:00(00 §6)', async () => {
    const acc = await accountsApi.get(ACC)
    expect(acc.state).toBe('running')
    expect(acc.self_uid).toBe('3007373675')
    expect(acc.created_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$/)
    expect(acc.error_since_ms).toBeNull()               // R6-4:非 error 态恒 null
  })

  it('#20 该账号当前可用能力里有 send_text', async () => {
    const c = await accountsApi.capabilities(ACC)
    expect(c.capabilities).toContain('send_text')
    expect(c.matrix.send_text).toBeTruthy()
  })

  it('send_text 指令 → 假 RPA 落库 → 读库合并确认 DELIVERED(用信封层断言);E-04:客户端 request() 会把 CommandResult 拆成它的 data', async () => {
    const sessions = await messagesApi.sessions({ account_id: ACC })
    const s = sessions.items[0] as unknown as Record<string, string>
    const native = s.native_id ?? s.id
    const env = await requestEnvelope<Record<string, unknown>>(`/accounts/${ACC}/commands`, {
      method: 'POST',
      body: { op: 'send_text', args: { session: native, text: '报价 1Y 1.70' }, idempotency_key: `e2e-send-${Date.now()}` },
    }) as unknown as Record<string, any>
    expect(env.ok).toBe(true)
    expect(env.code).toBe('DELIVERED')                       // 后端按 #28 回完整 CommandResult
    expect(env.data.confirmed_by).toBe('ingest_merge')
    expect(String(env.data.ext_msg_id)).toMatch(/^qd:/)
    expect(typeof env.cost_ms).toBe('number')

    // 同一端点经 commandsApi.run:request() 见到顶层有 `data` 就只回 data ⇒ code/cost_ms/trace_id 全丢
    const viaClient = await commandsApi.run(ACC, {
      op: 'send_text', args: { session: native, text: '报价 1Y 1.71' }, idempotency_key: `e2e-send2-${Date.now()}`,
    }) as unknown as Record<string, unknown>
    expect(viaClient.code).toBeUndefined()
    expect(viaClient.cost_ms).toBeUndefined()
    expect(viaClient.confirmed_by).toBe('ingest_merge')       // 拿到的其实是 CommandResult.data
  }, 60000)

  it('消息列表能查到该条出向消息(DELIVERED / dir=out / ISO 时间)', async () => {
    const r = await messagesApi.list({ account_id: ACC, dir: 'out', limit: 20 })
    const m = r.items.find((x) => x.text === '报价 1Y 1.70')
    expect(m).toBeTruthy()
    expect(m!.state).toBe('DELIVERED')
    expect(m!.dir).toBe('out')
    expect(m!.ts).toMatch(/\+08:00$/)
    expect(m!.ext_msg_id).toBeTruthy()
    expect(m!.session.id).toBeTruthy()
  })

  it('幂等:同一 idempotency_key 重复提交 ⇒ 409 IDEMPOTENT_REPLAY,通道里只有一条', async () => {
    const key = `e2e-idem-${Date.now()}`
    const text = `幂等测试文本-${key}`
    const body = { op: 'send_text', args: { session: '415011447', text }, idempotency_key: key }
    const first = await requestEnvelope<Record<string, unknown>>(`/accounts/${ACC}/commands`, { method: 'POST', body }) as unknown as Record<string, unknown>
    expect(first.code).toBe('DELIVERED')
    let status = 0
    let code = ''
    try {
      await commandsApi.run(ACC, body)
    } catch (e) {
      status = (e as { status: number }).status
      code = (e as { code: string }).code
    }
    expect(status).toBe(409)
    expect(code).toBe('IDEMPOTENT_REPLAY')
    const r = await messagesApi.list({ account_id: ACC, dir: 'out', limit: 50 })
    expect(r.items.filter((m) => m.text === text).length).toBe(1)
  }, 60000)
})

describe('WS /api/v1/events 事件流', () => {
  beforeAll(async () => {
    patch.stripApiMin = true
    patch.idemIntoBody = true
    await ensureRunning()
  }, 90000)
  afterAll(async () => { await ensureRunning() }, 90000)

  it('连接 → 首帧订阅 → 收到真 message 事件(发送一条即触发)', async () => {
    const c = collector()
    c.client.resubscribe({ events: ALL_EVENTS, accounts: ['*'] })
    c.client.connect()
    await waitFor(async () => c.client.status === 'open', 15000, 200)
    await commandsApi.run(ACC, { op: 'send_text', args: { session: '415011447', text: 'WS 用例文本' }, idempotency_key: `e2e-ws-${Date.now()}` })
    const ev = await waitFor(async () => c.events.find((e) => e.event === 'message'), 30000, 300)
    expect(ev.seq).toBeGreaterThan(0)
    expect(typeof ev.ts).toBe('string')
    expect(ev.account_id).toBe(ACC)
    c.client.close()
  }, 90000)

  it('account_state 事件在状态变更时到达', async () => {
    const c = collector()
    c.client.connect()
    await waitFor(async () => c.client.status === 'open', 15000, 200)
    await accountsApi.disable(ACC)
    const ev = await waitFor(async () => c.events.find((e) => e.event === 'account_state'), 30000, 300)
    expect((ev.payload as Record<string, unknown>).state).toBeTruthy()
    c.client.close()
    await accountsApi.enable(ACC)
  }, 90000)

  it('断线重连带 since_seq 续传:断线期间的事件补齐,且不重不乱序', async () => {
    const c = collector()
    c.client.connect()
    await waitFor(async () => c.client.status === 'open', 15000, 200)
    await accountsApi.disable(ACC)
    await waitFor(async () => c.events.some((e) => e.event === 'account_state'), 30000, 300)
    const seqBefore = c.client.lastSeq
    expect(seqBefore).toBeGreaterThan(0)

    // 掐掉底层 socket(不是 client.close(),客户端应自行重连)
    const statusesLen = c.statuses.length
    ;(c.client as unknown as { ws: { close: () => void } }).ws.close()
    await waitFor(async () => c.statuses.slice(statusesLen).includes('closed'), 15000, 50)

    // 断线期间再产生两次状态变更
    await accountsApi.enable(ACC)
    await accountsApi.disable(ACC)

    await waitFor(async () => c.client.status === 'open', 25000, 200)
    const backfilled = await waitFor(async () => {
      const news = c.events.filter((e) => e.seq > seqBefore)
      return news.length >= 2 ? news : null
    }, 30000, 300)
    const seqs = c.events.map((e) => e.seq)
    expect(new Set(seqs).size).toBe(seqs.length)
    expect(seqs.every((s, i) => i === 0 || s > seqs[i - 1])).toBe(true)
    expect(backfilled.length).toBeGreaterThanOrEqual(2)
    c.client.close()
  }, 120000)

  it('退避序列 1s→2s→4s…30s 封顶', () => {
    expect(backoffMs(1, 1)).toBe(1000)
    expect(backoffMs(2, 1)).toBe(2000)
    expect(backoffMs(9, 1)).toBe(30000)
  })
})

describe('WS 握手鉴权与首帧订阅', () => {
  // E-05:后端在 accept() **之前** close(4401) ⇒ 握手被 HTTP 层直接拒,客户端只看得到 1006,
  //       ws.ts 里 `code === 4401` 的门禁分支永远走不到,只会无限重连。下面断言的是**实测行为**。
  it('不带令牌的 WS ⇒ 实测关闭码 1006(非 §3.4.7 要求的 4401),onAuthFailed 不触发', async () => {
    let authFailed = false
    const codes: number[] = []
    const client = new EventsClient(
      { onEvent: () => {}, onStatus: () => {}, onReplayTruncated: () => {}, onAuthFailed: () => { authFailed = true } },
      {
        url: `${WS_BASE}/api/v1/events`,
        jitter: () => 0.01,
        factory: (u: string) => {
          const ws = new WebSocket(u) as unknown as Record<string, any>
          const origSetter = ws
          return new Proxy(ws, {
            set(t, k, v) {
              if (k === 'onclose') {
                t[k] = (ev: { code: number }) => { codes.push(ev.code); (v as (e: unknown) => void)(ev) }
                return true
              }
              t[k as string] = v
              return true
            },
            get(t, k) {
              const val = (t as Record<string, any>)[k as string]
              return typeof val === 'function' ? val.bind(origSetter) : val
            },
          }) as never
        },
      },
    )
    client.connect()
    await waitFor(async () => codes.length > 0, 15000, 100)
    client.close()
    expect(codes[0]).toBe(1006)
    expect(authFailed).toBe(false)
  }, 40000)
})

describe('WS 首帧订阅非法', () => {
  it('带合法令牌但首帧不是 {subscribe:{…}} ⇒ 服务端 4400 关闭', async () => {
    const codes: number[] = []
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(WS_URL)
      const timer = setTimeout(() => reject(new Error('15s 内未收到关闭帧')), 15000)
      ws.onopen = () => ws.send(JSON.stringify({ hello: 'world' }))
      ws.onclose = (ev: CloseEvent) => { codes.push(ev.code); clearTimeout(timer); resolve() }
      ws.onerror = () => { /* onclose 会接着来 */ }
    })
    expect(codes[0]).toBe(4400)
  }, 40000)
})
