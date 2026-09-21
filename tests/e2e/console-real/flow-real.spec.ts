/**
 * 业务线 + WS 事件流联调(真 Agent,全假后端)—— **第二轮:按规格断言**。
 *
 * 每一步都由**控制台客户端源码**发起、真后端响应。第一轮需要 `patch.stripApiMin` / `patch.idemIntoBody`
 * 两个开关「模拟已修」才走得下去;两处都已修好,本轮**一个开关都不开**。
 *
 * 规格出处:`02` = docs/02,`00` = docs/00,`05` = docs/05。
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { patch, WS_BASE, waitFor, raw } from './harness'
import { accountsApi, commandsApi, messagesApi, settingsApi, systemApi } from '@/api/client'
import { requestEnvelope, ApiFailure } from '@/api/http'
import { EventsClient, ALL_EVENTS, backoffMs } from '@/api/ws'
import { classifyClose } from '@/codec/stream'
import type { QtEvent } from '@/api/types'

const ACC = 'qd01'
const TOKEN = 'e2e-admin-token'
const WS_URL = `${WS_BASE}/api/v1/events?token=${TOKEN}`
/** 假主库里预置的对端(见 tests/e2e/serve_fake_agent.py 的 `PEER`)—— 会话的 native_id */
const PEER = '415011447'

/** 把账号拉回 running(create→start→login→running,05 §2.1.1 的登录阶段) */
async function ensureRunning(): Promise<void> {
  let a
  try {
    a = await accountsApi.get(ACC)
  } catch (e) {
    if (!(e instanceof ApiFailure) || e.status !== 404) throw e
    // 02 §3.4 #2:建号幂等键在 body,由客户端 http 层落进去
    await accountsApi.create({
      channel: 'qidian', label: 'e2e-企点', login: { mode: 'password', account: 'u', secret: 'p', remember: true },
    })
    a = await accountsApi.get(ACC)
  }
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

/* ══════════════ 业务线①:企点发消息全链路 ══════════════ */

describe('业务线①企点:建号 → start → 登录 → running → send_text → DELIVERED → 消息可查 → 幂等重放', () => {
  beforeAll(async () => {
    patch.forceApiMin = null
    patch.noAuth = false
    patch.token = TOKEN
    await ensureRunning()
  }, 120000)

  it('步1-4 账号 running 且身份回填;时间键 ISO+08:00(00 §6),error_since_ms 是已登记的 _ms 例外', async () => {
    /* 00 §6「API/事件时间一律 ISO 8601 带时区偏移」;00 §7.1 R6-4:`error_since_ms` 仅 state=error 时非空;
       R6-62 (f):`error_since_ms`/`deleted_ms` 沿用毫秒整数,是已登记例外,不该被改成 ISO。 */
    const acc = await accountsApi.get(ACC)
    expect(acc.state).toBe('running')
    expect(acc.self_uid).toBe('3007373675')
    expect(acc.created_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$/)
    expect(acc.last_seen_at).toMatch(/[+-]\d{2}:\d{2}$/)
    expect(acc.error_since_ms).toBeNull()
  })

  it('步5 #20 该账号当前可用能力里有 send_text', async () => {
    /* 02 #20:账号当前实际可用能力(与 #21 全局目录按运行态取交集)。 */
    const c = await accountsApi.capabilities(ACC)
    expect(c.capabilities).toContain('send_text')
    expect(c.matrix.send_text).toBeTruthy()
  })

  it('步6 send_text:commandsApi.run() 返回**完整 CommandResult**(不是它的 data);读回确认 DELIVERED', async () => {
    /* 02 #28 R6-52:同步 `200 CommandResult`;00 §7.3 CommandResult 的键
       `{ok, code, data, cost_ms, trace_id, source, state_before, state_after, error}`。
       第一轮 E-04:客户端 `request()` 见顶层有 `data` 就只回 data ⇒ `code`/`cost_ms`/`state_*` 全丢。 */
    const text = `报价 1Y 1.70 #${Date.now()}`
    const out = await commandsApi.run(ACC, {
      op: 'send_text', args: { session: PEER, text }, idempotency_key: `e2e-send-${Date.now()}`,
    }) as unknown as Record<string, any>

    expect(typeof out.ok).toBe('boolean')
    expect(typeof out.code, 'CommandResult 被拆成了它的 data(E-04)').toBe('string')
    expect(typeof out.cost_ms).toBe('number')
    expect(typeof out.trace_id).toBe('string')
    expect('source' in out).toBe(true)
    expect('state_before' in out).toBe(true)
    expect('state_after' in out).toBe(true)
    // 假 RPA 落库 → 企点读库 ingest 合并确认(06 §2.9.5 的确认正线)
    expect(out.ok, `发送未确认:code=${out.code} cost_ms=${out.cost_ms}`).toBe(true)
    expect(out.code).toBe('DELIVERED')
    expect(out.data.confirmed_by).toBe('ingest_merge')
    expect(String(out.data.ext_msg_id)).toMatch(/^qd:/)
    ;(globalThis as Record<string, any>).__e2eSentText = text
  }, 90000)

  it('步7 消息列表能查到该条出向消息(DELIVERED / dir=out / ISO 时间)', async () => {
    /* 00 §7.4 Message:`state` 出向确认态、`dir`、`ts` ISO、`ext_msg_id`、`session.id`。 */
    const text = (globalThis as Record<string, any>).__e2eSentText as string
    const m = await waitFor(async () => {
      const r = await messagesApi.list({ account_id: ACC, dir: 'out', limit: 30 })
      return r.items.find((x) => x.text === text) ?? null
    }, 30000, 500)
    expect(m.state).toBe('DELIVERED')
    expect(m.dir).toBe('out')
    expect(m.ts).toMatch(/[+-]\d{2}:\d{2}$/)
    expect(m.ext_msg_id).toBeTruthy()
    expect(m.session.id).toBeTruthy()
  }, 60000)

  it('步8 幂等:同一 idempotency_key 重放 ⇒ 409 IDEMPOTENT_REPLAY,响应体仍是完整 CommandResult(B-06),通道里只有一条', async () => {
    /* 02 #28 R6-52 逐字:「`409 IDEMPOTENT_REPLAY`(**响应体仍是完整 `CommandResult`**,`data` 与首次逐字相同,B-06)」。
       客户端 `ApiFailure.envelope` 要保留原始信封,页面才能直接显示首次结果。 */
    const key = `e2e-idem-${Date.now()}`
    const text = `幂等测试文本-${key}`
    const body = { op: 'send_text', args: { session: PEER, text }, idempotency_key: key }
    const first = await commandsApi.run(ACC, body) as unknown as Record<string, any>
    expect(first.code).toBe('DELIVERED')

    let err: ApiFailure | null = null
    try {
      await commandsApi.run(ACC, body)
    } catch (e) {
      err = e as ApiFailure
    }
    expect(err, '重放必须被拒').toBeTruthy()
    expect(err!.status).toBe(409)
    expect(err!.code).toBe('IDEMPOTENT_REPLAY')
    const env = err!.envelope as unknown as Record<string, any> | null
    expect(env, '409 的原始信封被丢掉了,页面显示不了首次结果').toBeTruthy()
    expect(env!.data?.message_id ?? env!.data?.ext_msg_id, '409 的 data 应与首次逐字相同(B-06)').toBeTruthy()

    const r = await messagesApi.list({ account_id: ACC, dir: 'out', limit: 50 })
    expect(r.items.filter((m) => m.text === text).length).toBe(1)
  }, 90000)
})

/* ══════════════ WS 事件流 ══════════════ */

describe('WS /api/v1/events 事件流(02 §3.4.7)', () => {
  beforeAll(async () => {
    patch.forceApiMin = null
    patch.noAuth = false
    patch.token = TOKEN
    await ensureRunning()
  }, 120000)
  afterAll(async () => { await ensureRunning() }, 120000)

  it('步9 连接 → 首帧订阅 → 收到真 message 事件;每帧 ts 是 ISO、seq 是整数', async () => {
    /* 02 §3.4.7 R6-53:「每帧顶层 `ts` 为 ISO 8601(00 §6),`seq` 整数」。 */
    const c = collector()
    c.client.resubscribe({ events: ALL_EVENTS, accounts: ['*'] })
    c.client.connect()
    await waitFor(async () => c.client.status === 'open', 15000, 200)
    await commandsApi.run(ACC, { op: 'send_text', args: { session: PEER, text: `WS 用例文本 ${Date.now()}` }, idempotency_key: `e2e-ws-${Date.now()}` })
    const ev = await waitFor(async () => c.events.find((e) => e.event === 'message'), 30000, 300)
    expect(Number.isInteger(ev.seq)).toBe(true)
    expect(ev.seq).toBeGreaterThan(0)
    expect(String(ev.ts)).toMatch(/[+-]\d{2}:\d{2}$/)
    expect(ev.account_id).toBe(ACC)
    c.client.close()
  }, 120000)

  it('account_state 事件在状态变更时到达,payload 带 state/error_since_ms(00 §7.5)', async () => {
    /* 00 §7.5:`account_state` payload = `{state, state_code, state_reason, error_since_ms, enabled, runtime, capabilities, self_nick, prompt?, login_session_id?}`。 */
    const c = collector()
    c.client.connect()
    await waitFor(async () => c.client.status === 'open', 15000, 200)
    await accountsApi.disable(ACC)
    const ev = await waitFor(async () => c.events.find((e) => e.event === 'account_state'), 30000, 300)
    const p = ev.payload as Record<string, unknown>
    expect(p.state).toBeTruthy()
    expect('error_since_ms' in p).toBe(true)
    c.client.close()
    await accountsApi.enable(ACC)
  }, 120000)

  it('步10 断线重连带 since_seq 续传:断线期间的事件补齐,不重不乱序(G-08)', async () => {
    /* 02 §3.4.7:「**重连一律带 `since_seq`**,不要全量拉列表(G-08)」;`seq` 单调递增(00 §7.5)。 */
    const c = collector()
    c.client.connect()
    await waitFor(async () => c.client.status === 'open', 15000, 200)
    await accountsApi.disable(ACC)
    await waitFor(async () => c.events.some((e) => e.event === 'account_state'), 30000, 300)
    const seqBefore = c.client.lastSeq
    expect(seqBefore).toBeGreaterThan(0)

    const statusesLen = c.statuses.length
    ;(c.client as unknown as { ws: { close: () => void } }).ws.close()
    await waitFor(async () => c.statuses.slice(statusesLen).includes('closed'), 15000, 50)

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
    await accountsApi.enable(ACC)
  }, 150000)

  it('退避序列 1s→2s→4s…30s 封顶', () => {
    /* 01 §2.5 / ws.ts 的重连退避(客户端行为,不依赖后端)。 */
    expect(backoffMs(1, 1)).toBe(1000)
    expect(backoffMs(2, 1)).toBe(2000)
    expect(backoffMs(9, 1)).toBe(30000)
  })
})

/* ══════════════ E-05 WS 握手鉴权(R6-62 (a) 新口径) ══════════════ */

describe('E-05 WS 握手鉴权:先 accept 再 close(4401)(02 §3.4.7 R6-62 (a))', () => {
  /* 02 §3.4.7 R6-62 (a) 逐字:「无令牌/令牌无效 → 服务端**先 `accept()` 把握手做完,再 `close(code=4401, reason)`**」;
     并明写「R6-52 原写的『`accept` 前直接关闭 + 关闭码 `4401`』自相矛盾、已作废」——在 accept 前 close 会退化成
     拒绝握手,客户端只看得到 1006,控制台按关闭码分诊那条路恒不触发。第一轮断言的 `1006` 就是那个缺陷现状。 */

  async function closeCodeOf(url: string, firstFrame?: string): Promise<number> {
    return await new Promise<number>((resolve, reject) => {
      const ws = new WebSocket(url)
      const timer = setTimeout(() => reject(new Error('15s 内未收到关闭帧')), 15000)
      ws.onopen = () => { if (firstFrame !== undefined) ws.send(firstFrame) }
      ws.onclose = (ev: CloseEvent) => { clearTimeout(timer); resolve(ev.code) }
      ws.onerror = () => { /* onclose 会接着来 */ }
    })
  }

  it('不带令牌 ⇒ 关闭码 4401', async () => {
    expect(await closeCodeOf(`${WS_BASE}/api/v1/events`)).toBe(4401)
  }, 40000)

  it('令牌无效 ⇒ 关闭码 4401', async () => {
    expect(await closeCodeOf(`${WS_BASE}/api/v1/events?token=totally-bogus`)).toBe(4401)
  }, 40000)

  it('客户端 onAuthFailed 在 4401 时触发一次(01 §5.1 按关闭码分诊)', async () => {
    /* 01 §5.1:控制台按关闭码分诊;`ws.ts` 里 `code === 4401` ⇒ `onAuthFailed`(「控制台令牌失效,请重新取令牌」)。
       第一轮:后端只给 1006,这条分支永远走不到,只会按退避无限重连。 */
    let authFailed = 0
    const client = new EventsClient(
      { onEvent: () => {}, onStatus: () => {}, onReplayTruncated: () => {}, onAuthFailed: () => { authFailed += 1 } },
      { url: `${WS_BASE}/api/v1/events`, jitter: () => 0.01 },
    )
    client.connect()
    await waitFor(async () => authFailed > 0, 15000, 100)
    client.close()
    expect(authFailed).toBeGreaterThanOrEqual(1)
  }, 40000)

  it('4401 之后**不自动重连**;上层重取令牌后 retry() 才再连(01 §5.1 ①)', async () => {
    /* 01 §5.1 逐字:「`code=4401` ⇒ 置**令牌失效**标记,顶部挂横幅并给出【重新取令牌并重连】,由**用户点按钮**触发…
       HTTP 侧 401 是自动重取并重放一次,**WS 侧当前不自动重取**」。⇒ 收到 4401 后客户端必须停手
       (`gaveUp`),否则拿同一把坏令牌无限重连,只刷日志与审计。 */
    let authFailed = 0
    const client = new EventsClient(
      { onEvent: () => {}, onStatus: () => {}, onReplayTruncated: () => {}, onAuthFailed: () => { authFailed += 1 } },
      { url: `${WS_BASE}/api/v1/events?token=totally-bogus`, jitter: () => 0.01 },
    )
    client.connect()
    await waitFor(async () => authFailed > 0, 15000, 100)
    // 停手:等 4 s(退避首挡 1 s)也不该再触发第二次 4401
    await new Promise((r) => setTimeout(r, 4000))
    expect(authFailed, '4401 之后仍在自动重连(每次都再吃一个 4401)').toBe(1)
    expect((client as unknown as { gaveUp: boolean }).gaveUp).toBe(true)
    expect(client.status).toBe('closed')
    // 用户点「重新取令牌并重连」= retry():这才允许再连一次
    client.retry()
    await waitFor(async () => authFailed >= 2, 15000, 100)
    expect(authFailed).toBe(2)
    client.close()
  }, 60000)

  it('合法令牌 + 首帧不是 {subscribe:{…}} ⇒ 关闭码 4400', async () => {
    /* 02 §3.4.7 R6-53:「合法首帧 = 含 `subscribe` 对象的 JSON(缺 `subscribe` 键同 `4400`)」;R6-62 (a):同样先 accept 再 close。 */
    expect(await closeCodeOf(WS_URL, JSON.stringify({ hello: 'world' }))).toBe(4400)
  }, 40000)
})

/* ══════════════ #34 画面流关闭码分诊(客户端侧判据,01 §5.1 第四条) ══════════════ */

describe('#34 画面流三个关闭码:4409/4503 停手,只有 4410 值得再试(01 §5.1 R6-62 Ⅷ)', () => {
  /* 01 §5.1 逐字:「**`4409`**(该通道不提供画面流)⇒ 提示改用截图预览(#33)并**停手不重连**;
     **`4410`**(该账号已有 `focus*` 连接)⇒ 提示被占用并**保留重试入口**(三个码里**只有它值得再试**);
     **`4503`**(画面流执行体本期未装配)⇒ 显示静态提示,**不重连、也不退化成轮询截图**」。
     这三个码归**客户端**分诊(`codec/stream.ts` 的 `classifyClose`),与后端无关,故不打后端。 */
  it('4409 / 4503 retryable=false,4410 retryable=true;三档各有自己的人话', () => {
    const a = classifyClose(4409, false)
    const b = classifyClose(4410, false)
    const c = classifyClose(4503, false)
    expect(a.retryable, '4409 不该保留重试入口').toBe(false)
    expect(c.retryable, '4503 不该保留重试入口(更不能退化成轮询截图)').toBe(false)
    expect(b.retryable, '4410 是三个里唯一值得再试的').toBe(true)
    for (const x of [a, b, c]) expect(x.text.length, '每个码都要给一条人话').toBeGreaterThan(0)
    expect(new Set([a.text, b.text, c.text]).size, '三档文案不能是同一句').toBe(3)
  })

  it('事件流的 4401/4400 在画面流码表里同样不可重试(不会被当成「断线了再连」)', () => {
    expect(classifyClose(4401, false).retryable).toBe(false)
    expect(classifyClose(4400, false).retryable).toBe(false)
  })
})

/* ══════════════ 业务线②:管理线(API 客户端令牌 / 自检 / wsl-restart) ══════════════ */

describe('业务线②管理线:api-client 令牌全生命周期(02 #90~#93)', () => {
  beforeAll(() => { patch.forceApiMin = null; patch.noAuth = false; patch.token = TOKEN })

  let appId = ''
  let issued = ''
  /** 步3b 轮换出来的新令牌(步4 吊销后它们也必须失效) */
  let rotated: string[] = []

  it('步1a #91 新建调用方 ⇒ 201,后端一次性下发明文令牌(02 #91)', async () => {
    /* 02 #91 逐字:「`{name, auth_kind, level, ip_allow, allow_ops, allow_accounts, rate_per_min, api_version_min?}`
       → **一次性**返回 `token` 或 `secret`」。这里用信封层取证「后端到底回了什么」。 */
    const env = await requestEnvelope('/settings/api-clients', {
      method: 'POST',
      body: { name: 'e2e-复测只读机器人', auth_kind: 'bearer', level: 'read', allow_accounts: ['*'] },
    }) as unknown as Record<string, any>
    /* 第三轮收紧(总控口径 ②):R6-55「字面键集 ⇒ 顶层平铺 + ok」的**单一形状** ——
       上一轮这里写的是 `env.app_id ?? env.data?.app_id`(两形都收),现在只认顶层、且不许再有 `data`。 */
    expect(env.data, '#91 成功响应不该再包 data(R6-55 单一形状:顶层平铺)').toBeUndefined()
    appId = env.app_id
    issued = env.token
    expect(appId).toBeTruthy()
    expect(env.level).toBe('read')                     // 行字段同在顶层
    expect(typeof issued, '后端没有下发一次性明文令牌').toBe('string')
    expect(issued.length).toBeGreaterThan(16)
  })

  it('步1b 经控制台客户端 settingsApi.createApiClient() 也要拿得到那把一次性令牌', async () => {
    /* P-SET 令牌页就是这么调的,而 `token` **只在这一次下发**(02 #91):客户端这一跳丢了它,
       用户就永远拿不到刚建的令牌,只能删了重建。
       第三轮**按客户端新返回形状翻面**(console-fix-2 §9 第 1 行):`createApiClient()` 现在回
       `{row, appId, token, traceId}`(不再把信封原样透给页面)。判据一条没松:
       ① 明文令牌拿得到且能用;② `appId` 与 #90 列表行对得上;③ 列表行不含明文(02 #90「不含 secret」)。 */
    const r = await settingsApi.createApiClient({
      name: 'e2e-复测只读机器人-经客户端', auth_kind: 'bearer', level: 'read', allow_accounts: ['*'],
    })
    expect(r.appId, '客户端拿不到 app_id').toBeTruthy()
    expect(typeof r.token, '客户端把一次性明文令牌丢了(P-SET 令牌页显示不出来)').toBe('string')
    expect(r.token!.length).toBeGreaterThan(16)
    expect(r.row?.app_id, '行对象与 appId 不一致').toBe(r.appId)
    expect(typeof r.traceId).toBe('string')
    // 令牌真能用(不是随便一个字符串)
    expect((await raw('/api/v1/accounts', { token: r.token })).status).toBe(200)
    // 与 #90 列表行对得上,且列表里读不回明文
    const list = await settingsApi.apiClients()
    const row = (list.items as unknown as Record<string, any>[]).find((x) => x.app_id === r.appId)
    expect(row, '经客户端新建的调用方不在 #90 列表里').toBeTruthy()
    expect(row!.token).toBeUndefined()
    expect(row!.secret).toBeUndefined()
    expect(JSON.stringify(list.items).includes(r.token!), '#90 列表回显了明文令牌').toBe(false)
    await settingsApi.revokeApiClient(r.appId!)
  })

  it('步2 列表里读不回明文(02 #90「不含 secret」)', async () => {
    const list = await settingsApi.apiClients()
    const row = (list.items as unknown as Record<string, any>[]).find((x) => x.app_id === appId)
    expect(row, '新建的调用方不在列表里').toBeTruthy()
    expect(row!.token).toBeUndefined()
    expect(row!.secret).toBeUndefined()
    expect(row!.level).toBe('read')
    expect(row!.enabled).toBe(true)
  })

  it('步3 用新令牌调只读端点 ⇒ 200;调 admin 端点 ⇒ 403 level_insufficient', async () => {
    /* 02 §3.4 通用段:级别 R/W/A,高包含低;`read` 级不得调 A 级端点(#88 `mail` 组)。 */
    expect(issued, '步1a 没拿到令牌,本条无从验起').toBeTruthy()
    const ok = await raw('/api/v1/accounts', { token: issued })
    expect(ok.status).toBe(200)
    const no = await raw('/api/v1/settings/mail', { token: issued })
    expect(no.status).toBe(403)
    expect(no.body.error.reason).toBe('level_insufficient')
  })

  it('步3b #92 轮换:单一顶层形状 + 客户端拿到新令牌;新令牌立刻可用,旧令牌在宽限期内仍可用(02 #92)', async () => {
    /* ⚠️ 本条必须排在步3 之后:轮换两次后**首把**令牌就不在宽限期内了(宽限只护「上一把」),
       排在前面会把步3 的前提破坏掉(那不是缺陷,是 #92 的语义)。 */
    /* 02 #92 逐字:「轮换,旧凭据宽限 `grace_minutes`(默认 10)」;形状同 #91(R6-55 字面键集 ⇒ 顶层平铺,总控口径 ②)。 */
    expect(issued, '步1a 没拿到令牌,本条无从验起').toBeTruthy()
    const env = await requestEnvelope(`/settings/api-clients/${appId}/rotate`, { method: 'POST' }) as unknown as Record<string, any>
    expect(env.data, '#92 成功响应不该包 data').toBeUndefined()
    expect(typeof env.token).toBe('string')
    expect(env.token).not.toBe(issued)
    expect(env.grace_minutes).toBe(10)
    expect((await raw('/api/v1/accounts', { token: env.token })).status, '新令牌不可用').toBe(200)
    expect((await raw('/api/v1/accounts', { token: issued })).status, '宽限期内旧令牌就失效了').toBe(200)
    // 经客户端再轮换一次:同样要拿得到一次性明文
    const viaClient = await settingsApi.rotateApiClient(appId)
    expect(typeof viaClient.token, 'rotateApiClient() 把一次性明文丢了').toBe('string')
    expect(viaClient.graceMinutes).toBe(10)
    rotated = [env.token, viaClient.token!]
  })

  it('步4 #93 吊销 ⇒ 该令牌立刻 401(02 #93)', async () => {
    /* 02 #93 逐字:「吊销」。吊销后旧令牌必须立刻失效,否则「泄露了就换一个」这条运维手段不成立。 */
    expect(issued, '步1a 没拿到令牌,本条无从验起').toBeTruthy()
    await settingsApi.revokeApiClient(appId)
    const after = await raw('/api/v1/accounts', { token: issued })
    expect(after.status).toBe(401)
    expect(after.body.ok).toBe(false)
    expect(after.body.code).toBe('UNAUTHORIZED')
    // 第三轮:轮换出来的新令牌同属这个调用方,吊销后同样立刻失效(否则「吊销」只吊了一半)
    for (const t of rotated) expect((await raw('/api/v1/accounts', { token: t })).status, '吊销后轮换出的令牌仍可用').toBe(401)
  })

  it('步5 内置 console 不可吊销(吊了控制台自己就连不上)', async () => {
    /* 该行为 docs 未逐字登记(见交接「规格缺口」);这里断言的是**不可破坏性**:
       要么拒绝,要么至少不能让当前这条连接立刻失效。 */
    const r = await raw('/api/v1/settings/api-clients/console', { method: 'DELETE' })
    expect([400, 409, 403]).toContain(r.status)
    expect(r.body.ok).toBe(false)
    const still = await raw('/api/v1/accounts')
    expect(still.status).toBe(200)
  })
})

describe('业务线②管理线:#78 自检 202 → 轮询到终态', () => {
  beforeAll(() => { patch.forceApiMin = null; patch.noAuth = false; patch.token = TOKEN })

  it('POST /system/selftest ⇒ 202 {run_id};轮询 #79 拿到该轮结果(02 #78/#79)', async () => {
    /* 02 #78 R6-62 (g):出参 `run_id`(不走 jobs 表);02 #79:`GET /system/selftest/{run_id}` 回
       `{redroid_boot_ms, napcat_ok, winagent_ok, probes}`。无执行体的项如实 null + `skipped`。 */
    const started = await systemApi.selftestRun()
    expect(started.run_id).toBeTruthy()
    const run = await waitFor(async () => {
      const r = await systemApi.selftestResult(started.run_id)
      return r.run ?? null
    }, 30000, 500)
    const d = run as unknown as Record<string, any>
    for (const k of ['redroid_boot_ms', 'napcat_ok', 'winagent_ok', 'probes']) expect(k in d).toBe(true)
    // 01 §4 的自检表要行;没有执行体的项一律 warn + 原因,不假装 ok
    const rows = (await systemApi.selftestResult(started.run_id)).items
    expect(rows.length).toBeGreaterThan(0)
    /* 行 level 的取值 = `SelftestRow.level`(console/src/api/types.ts:763)四档 ok / warn / error / skip。
       `skip` 是第五批按 C-18(01:119)/ 01:694「SKIPPED → 灰『未探测』」/ 01:1513 M4-7 新增的灰档;
       原断言里的 'fail' 并不是 level 的取值(那是探测结论表 tone 的名字),从来匹配不到真实行,顺带改正。
       不是放松:新增的 skip 档另加文案约束(不甩裸枚举,01 §2.9 约定 6)。 */
    for (const row of rows) expect(['ok', 'warn', 'error', 'skip']).toContain(row.level)
    for (const row of rows.filter((x) => x.level === 'skip')) {
      expect(row.message ?? '', 'skip 档说明列甩了英文枚举').not.toMatch(/SKIPPED/)
      expect(row.message ?? '', 'skip 档要写「未探测」').toContain('未探测')
    }
    if (d.redroid_boot_ms === null) {
      expect(rows.find((x) => x.item === 'redroid_boot')?.level, '没跑成的项不得标 ok').not.toBe('ok')
    }
  }, 60000)

  it('#79 用不存在的 run_id ⇒ 404 §10 信封(而 #79b 不带 run_id 的「没跑过」是 data:null,两者不同)', async () => {
    /* 02 #79b R6-58 (ab):「从没跑过时回 `{ok:true, data:null}`,**不是 `404`**」—— 那是**不带 run_id** 的语义;
       指名一个不存在的 run_id 是「目标不存在」,按 00 §10 = 404 TARGET_NOT_FOUND。 */
    const r = await raw('/api/v1/system/selftest/nope-run-id')
    expect(r.status).toBe(404)
    expect(r.body.ok).toBe(false)
  })
})

describe('业务线②管理线:#84 wsl-restart 的 [NOSHUTDOWN] 闸门', () => {
  beforeAll(() => { patch.forceApiMin = null; patch.noAuth = false; patch.token = TOKEN })

  it('不带 confirm ⇒ 400 confirm_required(基线 §11.6 [NOSHUTDOWN])', async () => {
    /* 02 #84 逐字:「`shutdown` 必须带 `confirm:true`(用户已在控制台确认,基线 §11.6 [NOSHUTDOWN])」。
       🔴「一条请求都没发到 WinAgent」由 `tests/e2e/test_admin_line.py` 用 FakeWinAgent 的 `calls` 断言 —— TS 侧读不到进程内假件。 */
    const r = await raw('/api/v1/system/wsl-restart', { method: 'POST', body: JSON.stringify({ mode: 'shutdown' }) })
    expect(r.status).toBe(400)
    expect(r.body.ok).toBe(false)
    expect(r.body.error.reason).toBe('confirm_required')
  })

  it('write 级令牌调它 ⇒ 403(02 #84 是 A 级)', async () => {
    /* 02 #84 级别列 = **A**。整机级破坏动作不能让 write 级调用方发起。 */
    const r = await raw('/api/v1/system/wsl-restart', {
      method: 'POST', body: JSON.stringify({ mode: 'shutdown', confirm: true }), token: 'e2e-write-token',
    })
    expect(r.status).toBe(403)
    expect(r.body.error.reason).toBe('level_insufficient')
  })
})
