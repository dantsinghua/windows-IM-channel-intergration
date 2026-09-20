/**
 * 事件流客户端:首帧订阅、seq 去重、since_seq 续传、replay 截断、退避重连、心跳判死、
 * **4401 即停手**(R6-62 (a):服务端先 accept 再 close,客户端看得到 4401)、
 * **握手连败即停手**(传输层原因只看得到 1006 时的兜底防御)。
 */
import { describe, expect, it } from 'vitest'
import { ALL_EVENTS, EventsClient, backoffMs, type WebSocketLike } from '@/api/ws'
import type { QtEvent } from '@/api/types'

class FakeWs implements WebSocketLike {
  static instances: FakeWs[] = []
  sent: string[] = []
  closed: { code?: number } | null = null
  onopen: ((ev?: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number; reason?: string }) => void) | null = null
  onerror: ((ev?: unknown) => void) | null = null

  constructor(public url: string) { FakeWs.instances.push(this) }
  send(data: string): void { this.sent.push(data) }
  close(code = 1000): void {
    this.closed = { code }
    this.onclose?.({ code })
  }
  open(): void { this.onopen?.() }
  recv(obj: unknown): void { this.onmessage?.({ data: JSON.stringify(obj) }) }
  lastSubscribe(): Record<string, unknown> {
    return JSON.parse(this.sent[0]).subscribe
  }
}

interface Harness {
  client: EventsClient
  events: QtEvent[]
  statuses: string[]
  truncated: number[]
  timers: Map<number, { fn: () => void; ms: number }>
  runTimers: (times?: number) => void
  now: { value: number }
}

function harness(): Harness {
  FakeWs.instances = []
  const events: QtEvent[] = []
  const statuses: string[] = []
  const truncated: number[] = []
  const timers = new Map<number, { fn: () => void; ms: number }>()
  let timerId = 0
  const now = { value: 0 }
  const client = new EventsClient(
    {
      onEvent: (e) => events.push(e),
      onStatus: (s) => statuses.push(s),
      onReplayTruncated: (n) => truncated.push(n),
    },
    {
      factory: (u) => new FakeWs(u),
      now: () => now.value,
      setTimeoutFn: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id },
      clearTimeoutFn: (h) => { timers.delete(h as number) },
      jitter: () => 1,
      heartbeatMs: 20000,
      deadAfterMs: 40000,
    },
  )
  const runTimers = (times = 1): void => {
    for (let i = 0; i < times; i++) {
      const next = [...timers.keys()].sort((a, b) => a - b)[0]
      if (next === undefined) return
      const t = timers.get(next)!
      timers.delete(next)
      t.fn()
    }
  }
  return { client, events, statuses, truncated, timers, runTimers, now }
}

describe('退避序列(01 §5.1)', () => {
  it('1s→2s→4s→…→30s 封顶', () => {
    expect(backoffMs(1, 1)).toBe(1000)
    expect(backoffMs(2, 1)).toBe(2000)
    expect(backoffMs(3, 1)).toBe(4000)
    expect(backoffMs(4, 1)).toBe(8000)
    expect(backoffMs(5, 1)).toBe(16000)
    expect(backoffMs(6, 1)).toBe(30000)
    expect(backoffMs(20, 1)).toBe(30000)
  })
  it('±20% 抖动作用在基数上', () => {
    expect(backoffMs(3, 0.8)).toBe(3200)
    expect(backoffMs(3, 1.2)).toBe(4800)
  })
})

describe('首帧订阅', () => {
  it('客户端先发 subscribe,带全部事件类型与 since_seq', () => {
    const h = harness()
    h.client.connect()
    FakeWs.instances[0].open()
    const sub = FakeWs.instances[0].lastSubscribe()
    expect(sub.events).toEqual(ALL_EVENTS)
    expect(sub.accounts).toEqual(['*'])
    expect(sub.since_seq).toBe(0)
  })

  it('可随时重发订阅帧收窄 message(P-MSG 不在前台)', () => {
    const h = harness()
    h.client.connect()
    FakeWs.instances[0].open()
    h.client.resubscribe({ accounts: ['qd01'] })
    const second = JSON.parse(FakeWs.instances[0].sent[1]).subscribe
    expect(second.accounts).toEqual(['qd01'])
  })
})

describe('seq 去重与续传(G-08)', () => {
  it('按 seq 去重,不按 ts 丢弃', () => {
    const h = harness()
    h.client.connect()
    const ws = FakeWs.instances[0]
    ws.open()
    ws.recv({ event: 'alert', seq: 5, ts: '2026-01-01T00:00:05+08:00', payload: {} })
    // 乱序到达的「更早 ts」但 seq 更大 —— 必须收下
    ws.recv({ event: 'alert', seq: 6, ts: '2026-01-01T00:00:01+08:00', payload: {} })
    // 重复 seq 丢弃
    ws.recv({ event: 'alert', seq: 6, ts: '2026-01-01T00:00:07+08:00', payload: {} })
    // 更小 seq 丢弃
    ws.recv({ event: 'alert', seq: 3, ts: '2026-01-01T00:00:09+08:00', payload: {} })
    expect(h.events.map((e) => e.seq)).toEqual([5, 6])
    expect(h.client.lastSeq).toBe(6)
  })

  it('重连时 since_seq = 已收到的最大 seq', () => {
    const h = harness()
    h.client.connect()
    const ws1 = FakeWs.instances[0]
    ws1.open()
    ws1.recv({ event: 'message', seq: 42, ts: 'x', payload: {} })
    ws1.close(1006)
    h.runTimers() // 触发重连
    const ws2 = FakeWs.instances[1]
    ws2.open()
    expect(ws2.lastSubscribe().since_seq).toBe(42)
  })

  it('首帧 replay:truncated 才要求全量拉', () => {
    const h = harness()
    h.client.connect()
    const ws = FakeWs.instances[0]
    ws.open()
    ws.recv({ replay: 'truncated', from_seq: 900 })
    expect(h.truncated).toEqual([900])
    expect(h.client.lastSeq).toBe(900)
    // 正常重放不触发
    ws.recv({ event: 'message', seq: 901, ts: 'x', payload: {} })
    expect(h.truncated).toHaveLength(1)
  })
})

describe('心跳与断线(01 §5.1)', () => {
  it('20s 发 ping;40s 无任何帧判断线并重连', () => {
    const h = harness()
    h.client.connect()
    const ws = FakeWs.instances[0]
    ws.open()
    // 第一次心跳:20s,没到 40s,发 ping
    h.now.value = 20000
    h.runTimers()
    expect(JSON.parse(ws.sent[1])).toEqual({ ping: 1 })
    // 第二次心跳:距最后一帧 40s → 判断线
    h.now.value = 60000
    h.runTimers()
    expect(ws.closed).toBeTruthy()
  })

  it('收到任何帧都刷新存活', () => {
    const h = harness()
    h.client.connect()
    const ws = FakeWs.instances[0]
    ws.open()
    h.now.value = 30000
    ws.recv({ event: 'ping' })     // 心跳帧不入环形缓冲
    h.now.value = 45000
    h.runTimers()
    expect(ws.closed).toBeNull()
    expect(h.events).toHaveLength(0)
  })

  it('4401 关闭码触发令牌失效回调', () => {
    let authFailed = 0
    const client = new EventsClient(
      { onEvent: () => undefined, onStatus: () => undefined, onReplayTruncated: () => undefined, onAuthFailed: () => { authFailed += 1 } },
      { factory: (u) => new FakeWs(u), setTimeoutFn: () => 0, clearTimeoutFn: () => undefined, jitter: () => 1 },
    )
    FakeWs.instances = []
    client.connect()
    FakeWs.instances[0].close(4401)
    expect(authFailed).toBe(1)
  })
})

describe('连接状态', () => {
  it('connecting → open → closed', () => {
    const h = harness()
    h.client.connect()
    FakeWs.instances[0].open()
    FakeWs.instances[0].close(1006)
    expect(h.statuses).toEqual(['connecting', 'open', 'closed'])
  })

  it('close() 之后不再重连', () => {
    const h = harness()
    h.client.connect()
    FakeWs.instances[0].open()
    h.client.close()
    const before = FakeWs.instances.length
    h.runTimers(3)
    expect(FakeWs.instances.length).toBe(before)
  })
})

describe('握手连败防御(E-05 前端侧)', () => {
  /**
   * ⚠️ 这一组防的不是 4401(那条按 R6-62 (a) 已经看得到,见下一个 describe),
   * 而是**传输层**原因(代理/端口/防火墙)下只看得到 1006 的情形 —— 不能就这么无限重连下去。
   */
  function handshakeHarness(max = 3) {
    FakeWs.instances = []
    const timers = new Map<number, { fn: () => void; ms: number }>()
    let timerId = 0
    const givenUp: { failures: number; code: number }[] = []
    const authFailed: number[] = []
    const client = new EventsClient(
      {
        onEvent: () => undefined,
        onStatus: () => undefined,
        onReplayTruncated: () => undefined,
        onAuthFailed: () => authFailed.push(1),
        onHandshakeGivenUp: (i) => givenUp.push(i),
      },
      {
        factory: (u) => new FakeWs(u),
        now: () => 0,
        setTimeoutFn: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id },
        clearTimeoutFn: (h) => { timers.delete(h as number) },
        jitter: () => 1,
        maxHandshakeFailures: max,
      },
    )
    const runTimers = (times = 1): void => {
      for (let i = 0; i < times; i++) {
        const next = [...timers.keys()].sort((a, b) => a - b)[0]
        if (next === undefined) return
        const t = timers.get(next)!
        timers.delete(next)
        t.fn()
      }
    }
    return { client, givenUp, authFailed, runTimers }
  }

  it('连续 N 次「从未 open 就 1006」⇒ 停止重连并回调 onHandshakeGivenUp', () => {
    const h = handshakeHarness(3)
    h.client.connect()
    FakeWs.instances[0].close(1006)   // 第 1 次
    h.runTimers(1)
    FakeWs.instances[1].close(1006)   // 第 2 次
    h.runTimers(1)
    FakeWs.instances[2].close(1006)   // 第 3 次 ⇒ 停手
    expect(h.givenUp).toEqual([{ failures: 3, code: 1006 }])
    const before = FakeWs.instances.length
    h.runTimers(5)
    expect(FakeWs.instances.length).toBe(before)
  })

  it('open 过一次就把计数清零(正常掉线照常无限重连)', () => {
    const h = handshakeHarness(3)
    h.client.connect()
    FakeWs.instances[0].close(1006)
    h.runTimers(1)
    FakeWs.instances[1].open()
    FakeWs.instances[1].close(1006)
    h.runTimers(1)
    expect(h.givenUp).toEqual([])
    expect(FakeWs.instances.length).toBe(3)
  })

  it('retry() 清零后可以重新连(门禁「重新取令牌」用)', () => {
    const h = handshakeHarness(2)
    h.client.connect()
    FakeWs.instances[0].close(1006)
    h.runTimers(1)
    FakeWs.instances[1].close(1006)
    expect(h.givenUp).toHaveLength(1)
    h.client.retry()
    expect(FakeWs.instances).toHaveLength(3)
  })

  it('4401 仍然照常回调 onAuthFailed', () => {
    const h = handshakeHarness(5)
    h.client.connect()
    FakeWs.instances[0].close(4401)
    expect(h.authFailed).toHaveLength(1)
  })
})

/* ───────────────── R6-62 (a):4401 先 accept 再 close,客户端看得到 ───────────────── */

describe('4401 的处置(02 §3.4.7 R6-62 (a))', () => {
  function authHarness() {
    FakeWs.instances = []
    const timers = new Map<number, { fn: () => void; ms: number }>()
    let timerId = 0
    const authFailed: number[] = []
    const client = new EventsClient(
      {
        onEvent: () => undefined,
        onStatus: () => undefined,
        onReplayTruncated: () => undefined,
        onAuthFailed: () => authFailed.push(1),
      },
      {
        factory: (u) => new FakeWs(u),
        setTimeoutFn: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id },
        clearTimeoutFn: (h) => { timers.delete(h as number) },
        jitter: () => 1,
      },
    )
    const runTimers = (times = 3): void => {
      for (let i = 0; i < times; i++) {
        const next = [...timers.keys()].sort((a, b) => a - b)[0]
        if (next === undefined) return
        const t = timers.get(next)!
        timers.delete(next)
        t.fn()
      }
    }
    return { client, authFailed, runTimers }
  }

  it('🔴 收到 4401 即 onAuthFailed 且**停止重连** —— 令牌没换,再连也是同一个结果', () => {
    const h = authHarness()
    h.client.connect()
    const ws = FakeWs.instances[0]
    // R6-62 (a):服务端先 accept 再 close(4401)⇒ 客户端可能已经 open 过
    ws.open()
    ws.onclose?.({ code: 4401, reason: '缺少或无效的令牌' })

    expect(h.authFailed).toHaveLength(1)
    expect(h.client.gaveUp, '4401 之后不该再排重连').toBe(true)
    h.runTimers(5)
    expect(FakeWs.instances).toHaveLength(1)
  })

  it('连都没连上就 4401(未 open)同样停手,不消耗握手连败额度', () => {
    const h = authHarness()
    h.client.connect()
    FakeWs.instances[0].onclose?.({ code: 4401 })
    expect(h.authFailed).toHaveLength(1)
    expect(h.client.handshakeFailures, '4401 是鉴权问题,不该算进传输层连败计数').toBe(0)
    h.runTimers(5)
    expect(FakeWs.instances).toHaveLength(1)
  })

  it('用户点「重新取令牌」后 retry() 才重连', () => {
    const h = authHarness()
    h.client.connect()
    FakeWs.instances[0].onclose?.({ code: 4401 })
    expect(FakeWs.instances).toHaveLength(1)
    h.client.retry()
    expect(FakeWs.instances).toHaveLength(2)
    expect(h.client.gaveUp).toBe(false)
  })
})
