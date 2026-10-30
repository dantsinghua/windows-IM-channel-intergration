import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { messagesApi } from '@/api/client'
import type { Message, QtEvent } from '@/api/types'
import { useEventsStore } from '@/stores/events'
import { useMessagesStore } from '@/stores/messages'

// docs/01 §2.7.7、§5.1：正常回放保留历史页；消息事实来自 message/#49，
// 旧请求不能倒退新状态。本轮并发预算来自现场回归要求，不绑定产品常量或队列实现。
const MAX_ALLOWED_IN_FLIGHT = 8
let pinia: Pinia

function message(id: string, extra: Partial<Message> = {}): Message {
  return {
    id, account_id: 'wx-synthetic', channel: 'wechat',
    session: { id: 'wx-synthetic:group', name: 'Synthetic group', kind: 'group' },
    dir: 'out', type: 'text', state: 'SENDING', text: `Original ${id}`, text_len: 12,
    media: [], sender: { id: 'self', name: 'Synthetic' }, self: true,
    ts: '2026-10-29T10:00:00Z', received_at: '2026-10-29T10:00:00Z',
    source: 'chatlog', revoked: false, trace_id: null, ...extra,
  }
}

function updated(id: string, text = `Read back ${id}`): Message {
  return message(id, { state: 'DELIVERED', text, ext_msg_id: `native:${id}`, confirmed_by: 'chatlog' })
}

function event(row: Message, seq: number): QtEvent<Message> {
  return { event: 'message', payload: row, seq, account_id: row.account_id, ts: row.ts }
}

function deferredDetails() {
  const pending: { id: string; resolve: (row: Message) => void; reject: (error: Error) => void }[] = []
  let active = 0
  let peak = 0
  const get = vi.spyOn(messagesApi, 'get').mockImplementation(id => {
    active += 1
    peak = Math.max(peak, active)
    return new Promise<Message>((resolve, reject) => {
      pending.push({ id, resolve, reject })
    }).finally(() => { active -= 1 })
  })

  function takeBatch() {
    return pending.splice(0)
  }

  async function drain(maxRounds: number, response: (id: string) => Message = updated) {
    for (let round = 0; round < maxRounds; round += 1) {
      await flushPromises()
      const batch = takeBatch()
      if (!batch.length) return
      for (const request of batch) request.resolve(response(request.id))
    }
    await flushPromises()
    expect(pending, '有限轮次内必须排空回读，不能不断重排同一消息').toHaveLength(0)
  }

  return { get, pending, takeBatch, drain, get active() { return active }, get peak() { return peak } }
}

async function reconnect(rows: Message[]) {
  const store = useMessagesStore()
  store.items = rows
  store.selectedId = rows[0].id
  store.nextCursor = 'older-page'
  store.filter = { account_id: 'wx-synthetic', limit: 50 }
  store.selectedSessionId = 'wx-synthetic:group'
  store.bindEvents()
  useEventsStore().status = 'open'
  await flushPromises()
  return store
}

function expectBounded(io: ReturnType<typeof deferredDetails>) {
  expect(io.active, '必须实际启动回读，不能通过禁用回读规避并发').toBeGreaterThan(0)
  expect(io.peak, '未完成的详情 GET 必须保持小的固定并发预算').toBeLessThanOrEqual(MAX_ALLOWED_IN_FLIGHT)
}

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Real HTTP forbidden')))
})

afterEach(() => {
  disposePinia(pinia)
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('消息详情回读的并发与状态权威', () => {
  it('重连 257 条历史消息时有界回读，逐批释放后每条更新且保留历史页', async () => {
    const count = 257
    const rows = Array.from({ length: count }, (_, i) => message(`m${i}`))
    const io = deferredDetails()
    const store = await reconnect(rows)
    expectBounded(io)
    expect(io.get.mock.calls.length).toBeLessThan(count)

    await io.drain(count + 1)

    expect(io.peak).toBeLessThanOrEqual(MAX_ALLOWED_IN_FLIGHT)
    expect(io.active).toBe(0)
    expect(io.get.mock.calls.map(([id]) => id).sort()).toEqual(rows.map(row => row.id).sort())
    expect(store.items).toEqual(rows.map(row => updated(row.id)))
    expect(store.selected?.id).toBe('m0')
    expect(store.nextCursor).toBe('older-page')
    expect(store.filter).toEqual({ account_id: 'wx-synthetic', limit: 50 })
    expect(store.selectedSessionId).toBe('wx-synthetic:group')
    expect(store.newCount).toBe(0)
  })

  it('首批全部拒绝后仍释放容量，剩余原消息全部回读且失败行不丢失', async () => {
    const rows = Array.from({ length: 41 }, (_, i) => message(`m${i}`))
    const io = deferredDetails()
    const store = await reconnect(rows)
    expectBounded(io)
    const failed = io.takeBatch()
    const failedIds = new Set(failed.map(request => request.id))
    for (const request of failed) request.reject(new Error('Synthetic detail failure'))
    await flushPromises()
    expectBounded(io)
    expect(io.get.mock.calls.length).toBeGreaterThan(failed.length)

    await io.drain(rows.length + 1)

    expect(io.active).toBe(0)
    expect(io.peak).toBeLessThanOrEqual(MAX_ALLOWED_IN_FLIGHT)
    expect(io.get.mock.calls.map(([id]) => id).sort()).toEqual(rows.map(row => row.id).sort())
    expect(store.items).toEqual(rows.map(row => failedIds.has(row.id) ? row : updated(row.id)))
  })

  it('排队期间切换查询，旧上下文回读不能覆盖新查询中的同 ID 消息', async () => {
    const rows = Array.from({ length: 25 }, (_, i) => message(`m${i}`))
    const io = deferredDetails()
    const store = await reconnect(rows)
    expectBounded(io)
    const started = new Set(io.get.mock.calls.map(([id]) => id))
    const queued = rows.find(row => !started.has(row.id))!
    expect(queued).toBeDefined()
    const fresh = updated(queued.id, 'Current query result')
    vi.spyOn(messagesApi, 'list').mockResolvedValue({ items: [fresh], nextCursor: 'current-page' })
    store.filter = { account_id: 'wx-synthetic', q: 'Current', limit: 50 }
    await store.search()

    await io.drain(rows.length + 1, id => message(id, { text: 'Stale old query' }))

    expect(store.items).toEqual([fresh])
    expect(store.nextCursor).toBe('current-page')
    expect(store.newCount).toBe(0)
  })

  it('排队期间收到更新的 message 事件，旧回读不得覆盖或丢失该事件', async () => {
    const rows = Array.from({ length: 25 }, (_, i) => message(`m${i}`))
    const io = deferredDetails()
    const store = await reconnect(rows)
    expectBounded(io)
    const started = new Set(io.get.mock.calls.map(([id]) => id))
    const queued = rows.find(row => !started.has(row.id))!
    expect(queued).toBeDefined()
    const fresh = updated(queued.id, 'New event while queued')
    useEventsStore().injectForTest(event(fresh, 200))
    expect(store.items.find(row => row.id === queued.id)).toEqual(fresh)

    await io.drain(rows.length + 1, id => message(id, { text: 'Stale detail' }))

    expect(store.items.find(row => row.id === queued.id)).toEqual(fresh)
    expect(store.items.map(row => row.id)).toEqual(rows.map(row => row.id))
    expect(store.nextCursor).toBe('older-page')
  })

  it('在途详情收到更晚的同 ID message 事件后，旧响应不能倒退状态', async () => {
    const io = deferredDetails()
    const store = await reconnect([message('same')])
    expect(io.get).toHaveBeenCalledWith('same')
    const fresh = updated('same', 'New event in flight')
    useEventsStore().injectForTest(event(fresh, 201))
    await io.drain(3, id => message(id, { text: 'Old in-flight response' }))
    expect(store.items).toEqual([fresh])
    expect(store.selected).toEqual(fresh)
  })

  it('同 ID 两次回读发生响应乱序，旧请求不能覆盖较新的回读结果', async () => {
    const io = deferredDetails()
    const store = useMessagesStore()
    const row = message('same')
    store.items = [row]
    const oldRequest = store.refreshMessage(row)
    await flushPromises()
    const old = io.takeBatch()
    expect(old).toHaveLength(1)
    const newerRequest = store.refreshMessage(row)
    await flushPromises()
    const newer = io.takeBatch()
    const fresh = updated('same', 'Newest detail response')
    // 允许同 ID 串行化或并发：只约束最终状态，不要求内部调度策略。
    if (newer.length) {
      for (const request of newer) request.resolve(fresh)
      await flushPromises()
      old[0].resolve(message('same', { text: 'Old detail response' }))
    } else {
      old[0].resolve(message('same', { text: 'Old detail response' }))
      await flushPromises()
    }
    await io.drain(4, () => fresh)
    await Promise.all([oldRequest, newerRequest])
    expect(store.items).toEqual([fresh])
    expect(io.active).toBe(0)
  })
})

it('真实详情传输在 15 秒取消四个挂起请求，释放容量让新查询完成', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const held: { id: string; signal: AbortSignal | null | undefined; reject: (error: Error) => void }[] = []
  const aborted: string[] = []
  const started: string[] = []
  const requests: Promise<void>[] = []
  let active = 0
  let peak = 0
  const fresh = updated('new-query', 'Current query result')
  // 保留真实 messagesApi.get -> request -> fetch 链，只模拟传输层支持 abort 的挂起响应。
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://synthetic.invalid')
    const id = decodeURIComponent(url.pathname.slice('/api/v1/messages/'.length))
    expect(url.pathname).toBe(`/api/v1/messages/${encodeURIComponent(id)}`)
    expect(init?.method).toBe('GET')
    started.push(id)
    active += 1
    peak = Math.max(peak, active)
    if (id === fresh.id) {
      active -= 1
      return Promise.resolve(new Response(JSON.stringify({ ok: true, data: fresh }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
    }
    return new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal
      held.push({ id, signal, reject })
      const onAbort = () => { aborted.push(id); reject(signal?.reason) }
      if (signal?.aborted) onAbort()
      else signal?.addEventListener('abort', onAbort, { once: true })
    }).finally(() => { active -= 1 })
  }))

  try {
    const store = useMessagesStore()
    const rows = Array.from({ length: 4 }, (_, i) => message(`hung-${i}`))
    store.items = rows
    requests.push(...rows.map(row => store.refreshMessage(row)))
    await flushPromises()
    expect(held).toHaveLength(4)

    store.filter = { q: 'Current', limit: 50 }
    store.items = [message(fresh.id)]
    requests.push(store.refreshMessage(store.items[0]))
    await flushPromises()
    expect(started).not.toContain(fresh.id)
    await vi.advanceTimersByTimeAsync(14_999)
    expect(aborted).toHaveLength(0)
    expect(started).not.toContain(fresh.id)

    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(aborted.sort(), '必须取消真实底层请求，不能只 race 超时后假释放许可').toEqual(rows.map(row => row.id).sort())
    expect(held.every(request => request.signal?.aborted && request.signal.reason.name === 'TimeoutError')).toBe(true)
    expect(started).toContain(fresh.id)
    await Promise.all(requests)
    expect(store.items).toEqual([fresh])
    expect(store.error).toBeNull()
    expect(peak).toBeLessThanOrEqual(MAX_ALLOWED_IN_FLIGHT)
    expect(active).toBe(0)
  } finally {
    // RED 时也结算合成挂起请求，避免把遗留任务带入后续用例。
    for (const request of held) request.reject(new Error('Synthetic cleanup'))
    await Promise.allSettled(requests)
    vi.useRealTimers()
  }
})
