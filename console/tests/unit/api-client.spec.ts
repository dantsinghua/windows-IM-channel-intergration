/**
 * HTTP 客户端:信封、错误码映射、401 重取重放、429 退避、507 DISK_FULL、trace 头、
 * **body 幂等键**(E-02)、**指令业务结果不当异常**(E-03/E-04)、版本协商(E-01)。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_MIN_VERSION, ApiFailure, configureHttp, lastTraceId, newIdempotencyKey, pickOnceSecret,
  recentTraces, request, requestBinary, requestCommand, requestEnvelope, requestList, statusToCode,
} from '@/api/http'
import { ulid } from '@/api/ulid'
import type { CommandResult } from '@/api/types'

type FetchArgs = [RequestInfo | URL, RequestInit?]

function jsonRes(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', 'X-QT-Api-Version': '1.0' },
    ...init,
  })
}

let calls: FetchArgs[] = []

function mockFetch(handler: (n: number, args: FetchArgs) => Response | Promise<Response>): void {
  calls = []
  vi.stubGlobal('fetch', (...args: FetchArgs) => {
    calls.push(args)
    return Promise.resolve(handler(calls.length, args))
  })
}

beforeEach(() => {
  configureHttp({
    onUnauthorized: undefined, onTokenLost: undefined, onApiVersion: undefined,
    onUpgradeRequired: undefined, onDraining: undefined,
  })
})
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('ulid', () => {
  it('26 字符、Crockford base32、时间有序', () => {
    const a = ulid(1000)
    const b = ulid(2000)
    expect(a).toHaveLength(26)
    expect(a).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
    expect(a < b).toBe(true)
  })
})

describe('HTTP 状态 → 结果码(00 §10)', () => {
  it('507 单独识别为 DISK_FULL,不当 500/INTERNAL', () => {
    expect(statusToCode(507)).toBe('DISK_FULL')
    expect(statusToCode(500)).toBe('INTERNAL')
  })
  it('其余按基线映射', () => {
    expect(statusToCode(400)).toBe('INVALID_ARGS')
    expect(statusToCode(401)).toBe('UNAUTHORIZED')
    expect(statusToCode(403)).toBe('FORBIDDEN')
    expect(statusToCode(404)).toBe('TARGET_NOT_FOUND')
    expect(statusToCode(409)).toBe('RESOURCE_EXHAUSTED')
    expect(statusToCode(429)).toBe('RATE_LIMITED')
    expect(statusToCode(503)).toBe('NOT_READY')
  })
})

describe('请求头(E-01)', () => {
  it('每请求带 X-Trace-Id(ULID)与 X-QT-Api-Min', async () => {
    mockFetch(() => jsonRes({ ok: true, data: { hi: 1 } }))
    await request('/ping')
    const headers = calls[0][1]?.headers as Record<string, string>
    expect(headers['X-Trace-Id']).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
    expect(headers['X-QT-Api-Min']).toBe(API_MIN_VERSION)
    expect(headers.Authorization).toBeUndefined() // 令牌由主进程注入,渲染进程从不带
  })

  it('X-QT-Api-Min 取 docs/07 的 api_version="1.0",不得写死成更高的次版本', () => {
    // 凭空声明 1.3 ⇒ 真 Agent(1.0)每个请求都 426,控制台全功能不可用(E-01)
    expect(API_MIN_VERSION).toBe('1.0')
  })

  it('426 触发 onUpgradeRequired,带上「要什么 / 服务端是什么」', async () => {
    const onUpgradeRequired = vi.fn()
    configureHttp({ onUpgradeRequired })
    mockFetch(() => jsonRes(
      { ok: false, code: 'UPGRADE_REQUIRED', error: { message: '需要 API 1.3,当前 1.0', retryable: false, needs_human: true } },
      { status: 426 },
    ))
    await expect(request('/system/version')).rejects.toBeInstanceOf(ApiFailure)
    expect(onUpgradeRequired).toHaveBeenCalledWith(expect.objectContaining({
      needMin: API_MIN_VERSION, serverVersion: '1.0',
    }))
  })
})

describe('幂等键落 body(E-02)', () => {
  it('写类把 idempotency_key 并进 body,而不是发 X-Idempotency-Key 头', async () => {
    mockFetch(() => jsonRes({ ok: true, data: {} }))
    const key = newIdempotencyKey()
    await request('/accounts', { method: 'POST', body: { channel: 'qidian' }, idempotencyKey: key })
    const headers = calls[0][1]?.headers as Record<string, string>
    // 全 docs 不存在这个头;后端只认 body 里的键
    expect(headers['X-Idempotency-Key']).toBeUndefined()
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ channel: 'qidian', idempotency_key: key })
  })

  it('body 里已写了 idempotency_key 就不覆盖(#28/#36 自带那把键)', async () => {
    mockFetch(() => jsonRes({ ok: true, data: {} }))
    await request('/accounts/qd01/commands', {
      method: 'POST', body: { op: 'send_text', idempotency_key: 'mine' }, idempotencyKey: 'other',
    })
    expect(JSON.parse(String(calls[0][1]?.body)).idempotency_key).toBe('mine')
  })

  it('没 body 时也能只带一个 idempotency_key 发出去', async () => {
    mockFetch(() => jsonRes({ ok: true, data: {} }))
    await request('/x', { method: 'POST', idempotencyKey: 'k1' })
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({ idempotency_key: 'k1' })
  })
})

describe('指令结果:业务结果 ≠ 传输错误(E-03 / E-04)', () => {
  const unconfirmed = {
    ok: false,
    code: 'SEND_CALLED_BUT_UNCONFIRMED',
    data: { message_id: 'msg_1', ext_msg_id: 'qd:1', confirmed_by: null },
    cost_ms: 15009,
    trace_id: '01TRACE0001',
    source: 'qidian_db',
    state_before: 'READY',
    state_after: 'READY',
    error: { message: '已发出但未读回确认', retryable: true, needs_human: false },
  }

  it('200 + ok:false 的业务结果码**不抛异常**,完整 CommandResult 交给页面', async () => {
    mockFetch(() => jsonRes(unconfirmed))
    const r = await requestCommand<CommandResult>('/accounts/qd01/commands', { method: 'POST', body: { op: 'send_text' } })
    expect(r.code).toBe('SEND_CALLED_BUT_UNCONFIRMED')
    expect(r.cost_ms).toBe(15009)
    expect(r.source).toBe('qidian_db')
    expect(r.state_after).toBe('READY')
    expect(r.data?.message_id).toBe('msg_1')
  })

  it('成功时也不把 CommandResult 拆成它的 data(E-04)', async () => {
    mockFetch(() => jsonRes({
      ok: true, code: 'DELIVERED', cost_ms: 2091, trace_id: '01T', source: 'qidian_db',
      data: { message_id: 'm1', confirmed_by: 'ingest_merge' },
    }))
    const r = await requestCommand<CommandResult>('/accounts/qd01/commands', { method: 'POST', body: {} })
    expect(r.code).toBe('DELIVERED')
    expect(r.cost_ms).toBe(2091)
    expect(r.data?.confirmed_by).toBe('ingest_merge')
  })

  it('202 受理体原样回(同步等待超时 / async)', async () => {
    mockFetch(() => jsonRes({ ok: true, trace_id: '01T', accepted: true, pending: true }, { status: 202 }))
    const r = await requestCommand<{ accepted: boolean; pending: boolean }>('/accounts/qd01/commands', { method: 'POST', body: {} })
    expect(r.accepted).toBe(true)
    expect(r.pending).toBe(true)
  })

  it('HTTP 层错误仍然抛,且 409 IDEMPOTENT_REPLAY 的完整 CommandResult 挂在 envelope 上', async () => {
    mockFetch(() => jsonRes(
      { ...unconfirmed, code: 'DELIVERED', ok: true, error: undefined },
      { status: 409 },
    ))
    const err = await requestCommand('/accounts/qd01/commands', { method: 'POST', body: {} }).catch((e) => e as ApiFailure)
    expect(err).toBeInstanceOf(ApiFailure)
    expect((err as ApiFailure).status).toBe(409)
    expect(((err as ApiFailure).envelope as unknown as CommandResult).code).toBe('DELIVERED')
  })
})

describe('trace 留存(总控裁决⑤)', () => {
  it('成功响应的 trace_id 被记下来,供「复制 trace」与本地日志用', async () => {
    mockFetch(() => jsonRes({ ok: true, data: {}, trace_id: '01SERVERTRACE' }))
    await request('/accounts')
    expect(lastTraceId()).toBe('01SERVERTRACE')
    expect(recentTraces()[0]).toMatchObject({ path: '/accounts', status: 200 })
  })

  it('服务端没回 trace_id 时回落本地 ULID(不丢这一跳)', async () => {
    mockFetch(() => jsonRes({ ok: true, data: {} }))
    await request('/accounts')
    expect(lastTraceId()).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
  })
})

describe('信封解包', () => {
  it('有 data 就取 data', async () => {
    mockFetch(() => jsonRes({ ok: true, data: { a: 1 }, trace_id: 't1' }))
    await expect(request<{ a: number }>('/x')).resolves.toEqual({ a: 1 })
  })

  it('顶层平铺的端点(#69/#9/#7)原样回', async () => {
    mockFetch(() => jsonRes({ ok: true, deleted: true, data_kept: true, trace_id: 't1' }))
    await expect(request('/accounts/qd01')).resolves.toEqual({ deleted: true, data_kept: true })
  })

  it('列表端点回 items + next_cursor', async () => {
    mockFetch(() => jsonRes({ ok: true, data: [1, 2], next_cursor: 'c1' }))
    await expect(requestList<number>('/messages')).resolves.toEqual({ items: [1, 2], nextCursor: 'c1' })
  })
})

describe('错误信封归一', () => {
  it('FastAPI 默认的 {"detail":"Not Found"} 也归一成人话,不只剩「请求失败」', async () => {
    mockFetch(() => new Response(JSON.stringify({ detail: 'Not Found' }), {
      status: 404, headers: { 'Content-Type': 'application/json' },
    }))
    const err = (await request('/system/env').catch((e) => e)) as ApiFailure
    expect(err.code).toBe('TARGET_NOT_FOUND')
    expect(err.message).toBe('Not Found')
  })
})

describe('错误处理', () => {
  it('507 抛 DISK_FULL,且带 trace 前 8 位', async () => {
    mockFetch(() => jsonRes(
      { ok: false, code: 'DISK_FULL', error: { message: '剩余 120 MB', retryable: false, needs_human: true }, trace_id: '01ABCDEFGH' },
      { status: 507 },
    ))
    const err = await request('/messages').catch((e) => e as ApiFailure)
    expect(err).toBeInstanceOf(ApiFailure)
    expect((err as ApiFailure).code).toBe('DISK_FULL')
    expect((err as ApiFailure).status).toBe(507)
    expect((err as ApiFailure).traceShort).toBe('01ABCDEF')
  })

  it('INVALID_ARGS 带 reason 透出(R6-48)', async () => {
    mockFetch(() => jsonRes(
      { ok: false, code: 'INVALID_ARGS', error: { message: 'bad', reason: 'text_has_control_chars', retryable: false, needs_human: false } },
      { status: 400 },
    ))
    const err = (await request('/x', { method: 'POST' }).catch((e) => e)) as ApiFailure
    expect(err.reason).toBe('text_has_control_chars')
  })

  it('409 CONFIRM_EXPIRED 原样透出码,交页面判', async () => {
    mockFetch(() => jsonRes(
      { ok: false, code: 'CONFIRM_EXPIRED', error: { message: '已过期', retryable: false, needs_human: false } },
      { status: 409 },
    ))
    const err = (await request('/mail/pending-confirms/x/approve', { method: 'POST' }).catch((e) => e)) as ApiFailure
    expect(err.code).toBe('CONFIRM_EXPIRED')
  })
})

describe('401 / 429(§5.2)', () => {
  it('401 → 让主进程重取令牌一次 → 自动重放原请求', async () => {
    const onUnauthorized = vi.fn().mockResolvedValue(true)
    configureHttp({ onUnauthorized })
    mockFetch((n) => (n === 1
      ? jsonRes({ ok: false, code: 'UNAUTHORIZED', error: { message: 'no', retryable: false, needs_human: false } }, { status: 401 })
      : jsonRes({ ok: true, data: { ok: 1 } })))
    await expect(request('/accounts')).resolves.toEqual({ ok: 1 })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(calls).toHaveLength(2)
  })

  it('仍 401 → 触发门禁,不再重试', async () => {
    const onTokenLost = vi.fn()
    configureHttp({ onUnauthorized: vi.fn().mockResolvedValue(false), onTokenLost })
    mockFetch(() => jsonRes(
      { ok: false, code: 'UNAUTHORIZED', error: { message: 'no', retryable: false, needs_human: false } },
      { status: 401 },
    ))
    await expect(request('/accounts')).rejects.toBeInstanceOf(ApiFailure)
    expect(onTokenLost).toHaveBeenCalled()
    expect(calls).toHaveLength(1)
  })

  it('429 读 Retry-After 退避后重试一次', async () => {
    vi.useFakeTimers()
    mockFetch((n) => (n === 1
      ? new Response('{}', { status: 429, headers: { 'Retry-After': '1' } })
      : jsonRes({ ok: true, data: { done: true } })))
    const p = request('/messages')
    await vi.advanceTimersByTimeAsync(1100)
    await expect(p).resolves.toEqual({ done: true })
    expect(calls).toHaveLength(2)
  })

  it('429 再次失败按 RATE_LIMITED 抛出', async () => {
    vi.useFakeTimers()
    mockFetch(() => new Response('{}', { status: 429 }))
    const p = request('/messages').catch((e) => e as ApiFailure)
    await vi.advanceTimersByTimeAsync(2100)
    const err = (await p) as ApiFailure
    expect(err.code).toBe('RATE_LIMITED')
    expect(calls).toHaveLength(2)
  })
})

describe('版本协商', () => {
  it('响应头 X-QT-Api-Version 回调出去', async () => {
    const onApiVersion = vi.fn()
    configureHttp({ onApiVersion })
    mockFetch(() => jsonRes({ ok: true, data: {} }))
    await request('/system/version')
    expect(onApiVersion).toHaveBeenCalledWith('1.0')
  })
})

describe('查询串', () => {
  it('跳过空值、数组展开', async () => {
    mockFetch(() => jsonRes({ ok: true, data: [] }))
    await requestList('/messages', { query: { account_id: 'qd01', dir: undefined, q: '', tags: ['a', 'b'] } })
    const url = String(calls[0][0])
    expect(url).toContain('account_id=qd01')
    expect(url).not.toContain('dir=')
    expect(url).not.toContain('q=')
    expect(url).toContain('tags=a&tags=b')
  })
})

/* ───────────────── N-1:一次性明文凭据(#91/#92/#67) ───────────────── */

describe('N-1 一次性明文凭据:两种形状都要接住', () => {
  it('🔴 令牌在**顶层**(后端收口前的现状)也拿得到 —— request() 的「有 data 就返回 data」会丢掉它', async () => {
    const body = {
      ok: true,
      data: { app_id: 'app-1', name: 'x', level: 'read', ip_allow: [], created_at: '2026-09-21T00:00:00+08:00' },
      app_id: 'app-1',
      token: '01M30ONETIMETOKEN',
      trace_id: '01M30TRACE',
    }
    mockFetch(() => jsonRes(body, { status: 201 }))
    const env = await requestEnvelope<Record<string, unknown>>('/settings/api-clients', { method: 'POST', body: {} })
    expect(pickOnceSecret(env, 'token')).toBe('01M30ONETIMETOKEN')

    // 对照组:request() 只会返回 data,明文就此丢失(这正是 N-1 的前端次责)
    mockFetch(() => jsonRes(body, { status: 201 }))
    const onlyData = await request<Record<string, unknown>>('/settings/api-clients', { method: 'POST', body: {} })
    expect(onlyData.token, 'request() 对混合形状静默丢键 —— 所以一次性凭据不能用它').toBeUndefined()
  })

  it('令牌收口进 `data` 之后同样拿得到(后端收口后这一路是唯一形状)', async () => {
    mockFetch(() => jsonRes({
      ok: true,
      data: { app_id: 'app-2', name: 'y', level: 'read', ip_allow: [], created_at: '', token: '01M30INDATA' },
      trace_id: '01M30TRACE2',
    }, { status: 201 }))
    const env = await requestEnvelope<Record<string, unknown>>('/settings/api-clients', { method: 'POST', body: {} })
    expect(pickOnceSecret(env, 'token')).toBe('01M30INDATA')
  })

  it('两处都没有明文时回 null —— 页面据此明说「本次没拿到」,不显示空框', async () => {
    mockFetch(() => jsonRes({ ok: true, data: { app_id: 'app-3' }, trace_id: 't' }, { status: 201 }))
    const env = await requestEnvelope<Record<string, unknown>>('/settings/api-clients', { method: 'POST', body: {} })
    expect(pickOnceSecret(env, 'token')).toBeNull()
    expect(pickOnceSecret(env, 'secret')).toBeNull()
  })
})

/* ───────────────── 二进制端点:响应头里的 media_id / sha256 ───────────────── */

describe('requestBinary(#33 截图 / #50 媒体 / #55)', () => {
  it('把 X-QT-Media-Id / X-QT-Sha256 / X-QT-Trace-Id 一起交给调用方', async () => {
    mockFetch(() => new Response(new Blob([new Uint8Array([1, 2, 3])]), {
      status: 200,
      headers: {
        'Content-Type': 'image/png',
        'X-QT-Media-Id': '17',
        'X-QT-Sha256': 'a'.repeat(64),
        'X-QT-Trace-Id': '01M30SHOT',
      },
    }))
    const r = await requestBinary('/accounts/qd01/screenshot')
    expect(r.pending).toBe(false)
    expect(r.blob).not.toBeNull()
    expect(r.mediaId).toBe('17')
    expect(r.sha256).toBe('a'.repeat(64))
    expect(r.traceId).toBe('01M30SHOT')
  })

  it('#50 懒下载未完成:202 JSON ⇒ pending,不是错误', async () => {
    mockFetch(() => jsonRes({ state: 'pending' }, { status: 202 }))
    const r = await requestBinary('/messages/m1/media/0')
    expect(r.pending).toBe(true)
    expect(r.blob).toBeNull()
  })

  it('错误分支仍走 00 §10 信封:code / reason 交给页面', async () => {
    mockFetch(() => jsonRes({
      ok: false, code: 'UNSUPPORTED',
      error: { message: '截图失败:UNSUPPORTED', reason: 'screenshot_failed', retryable: false, needs_human: false },
      trace_id: '01M30ERR',
    }, { status: 409 }))
    await expect(requestBinary('/accounts/qd01/screenshot')).rejects.toMatchObject({
      code: 'UNSUPPORTED', status: 409, traceId: '01M30ERR',
    })
  })
})

/* ───────────────── #82 drain:写操作 503 draining ───────────────── */

describe('#82 drain 的全局提示', () => {
  it('写操作撞上 503 draining ⇒ onDraining(true);写操作又成功 ⇒ onDraining(false)', async () => {
    const seen: boolean[] = []
    configureHttp({ onDraining: (on) => seen.push(on) })

    mockFetch(() => jsonRes({
      ok: false, code: 'NOT_READY',
      error: { message: 'Agent 正在排空', reason: 'draining', retryable: true, needs_human: false },
    }, { status: 503 }))
    await expect(request('/system/cleanup/run', { method: 'POST' })).rejects.toBeInstanceOf(ApiFailure)
    expect(seen).toEqual([true])

    mockFetch(() => jsonRes({ ok: true, data: { job_id: 'j1' } }))
    await request('/system/cleanup/run', { method: 'POST' })
    expect(seen).toEqual([true, false])

    configureHttp({ onDraining: undefined })
  })

  it('🔴 别的写端点成功**不解除** —— 排空期间设置类端点照样通,解除条件只认被拒过的那个 path', async () => {
    const seen: boolean[] = []
    configureHttp({ onDraining: (on) => seen.push(on) })

    mockFetch(() => jsonRes({
      ok: false, code: 'NOT_READY',
      error: { message: '排空中', reason: 'draining', retryable: true, needs_human: false },
    }, { status: 503 }))
    await expect(request('/accounts/qd01/start', { method: 'POST' })).rejects.toBeInstanceOf(ApiFailure)
    expect(seen).toEqual([true])

    // 设置类写端点在真后端排空期间仍会 201 —— 不能因此把横幅抹掉
    mockFetch(() => jsonRes({ ok: true, app_id: 'a1', token: 't' }, { status: 201 }))
    await request('/settings/api-clients', { method: 'POST', body: {} })
    expect(seen, '随手改一条设置就解除横幅 = 谎报「已恢复受理」').toEqual([true])

    // 被拒过的那个 path 又通了才解除
    mockFetch(() => jsonRes({ ok: true, data: { state: 'starting' } }))
    await request('/accounts/qd01/start', { method: 'POST' })
    expect(seen).toEqual([true, false])

    configureHttp({ onDraining: undefined })
  })

  it('只读请求不参与 draining 判定(排空期间只读照常)', async () => {
    const seen: boolean[] = []
    configureHttp({ onDraining: (on) => seen.push(on) })
    mockFetch(() => jsonRes({ ok: true, data: [] }, { status: 200 }))
    await requestList('/accounts')
    expect(seen).toEqual([])
    configureHttp({ onDraining: undefined })
  })
})
