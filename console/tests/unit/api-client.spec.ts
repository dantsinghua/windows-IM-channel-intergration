/** HTTP 客户端:信封、错误码映射、401 重取重放、429 退避、507 DISK_FULL、trace 头、幂等键 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiFailure, configureHttp, request, requestList, statusToCode, newIdempotencyKey } from '@/api/http'
import { ulid } from '@/api/ulid'

type FetchArgs = [RequestInfo | URL, RequestInit?]

function jsonRes(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', 'X-QT-Api-Version': '1.3' },
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
  configureHttp({ onUnauthorized: undefined, onTokenLost: undefined, onApiVersion: undefined })
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

describe('请求头', () => {
  it('每请求带 X-Trace-Id(ULID)与 X-QT-Api-Min', async () => {
    mockFetch(() => jsonRes({ ok: true, data: { hi: 1 } }))
    await request('/ping')
    const headers = calls[0][1]?.headers as Record<string, string>
    expect(headers['X-Trace-Id']).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/)
    expect(headers['X-QT-Api-Min']).toBe('1.3')
    expect(headers.Authorization).toBeUndefined() // 令牌由主进程注入,渲染进程从不带
  })

  it('写类可带幂等键', async () => {
    mockFetch(() => jsonRes({ ok: true, data: {} }))
    const key = newIdempotencyKey()
    await request('/accounts/qd01/commands', { method: 'POST', body: { op: 'send_text' }, idempotencyKey: key })
    const headers = calls[0][1]?.headers as Record<string, string>
    expect(headers['X-Idempotency-Key']).toBe(key)
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
    expect(onApiVersion).toHaveBeenCalledWith('1.3')
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
