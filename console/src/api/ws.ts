/**
 * 事件流客户端(01 §2.8 / §5.1;协议 02 §3.4.7)。
 *
 * - `ws://127.0.0.1:17600/api/v1/events`,**首帧由客户端发订阅**
 *   `{"subscribe":{events,accounts,channels,since_seq}}`
 * - 服务端每帧带 `seq`:store 按 `seq` 去重与排序,**不再按 `ts` 丢弃乱序旧事件**
 * - 重连指数退避 1s→2s→4s→…→30s 封顶、±20% 抖动;带 `since_seq` 触发 `events_outbox` 重放(G-08)
 * - **只有**首帧回 `{"replay":"truncated","from_seq":N}` 才需要全量拉 `/accounts` `/resources` `/mail/status`
 * - 心跳:客户端每 20s ping,40s 无任何帧判断线
 * - 关闭码 4401 = 令牌无效;4400 = 首帧订阅非法
 * - 🔴 **鉴权失败的时序**(02 §3.4.7 R6-62 (a),后端已按此实现):服务端**先 `accept()` 再
 *   `close(4401, reason)`**,所以客户端**看得到 4401**(R6-52 原「accept 前关闭」已作废;
 *   那种写法会退化成拒绝握手、线上只剩 `1006`)。4401 的处置 = 顶部挂横幅 + 用户点按钮重取令牌,
 *   **不自动重连**(令牌没换,重连只会再吃一次 4401)。
 * - **握手连败防御**(保留):网络/代理/端口等原因也会「从未 open 过就关闭」,
 *   连续 `maxHandshakeFailures` 次即停手并转「需要重新取令牌」,免得无声空转刷日志。
 */

import type { EventKind, QtEvent } from './types'

export type WsStatus = 'connecting' | 'open' | 'closed'

export interface SubscribeFilter {
  events: EventKind[]
  accounts: string[]
  channels?: string[]
}

export interface EventsClientHooks {
  onEvent: (ev: QtEvent) => void
  onStatus: (status: WsStatus) => void
  /** 首帧 replay:truncated —— 需要全量重拉后再接事件 */
  onReplayTruncated: (fromSeq: number) => void
  /**
   * 令牌失效(4401,R6-62 (a) 之后能稳定拿到)。
   * 🔴 收到即**停止重连**并交回上层:令牌不换,再连也是 4401。
   */
  onAuthFailed?: () => void
  /**
   * 连续 N 次握手就断(从未 open) —— 停止重连,交由上层走「重新取令牌」。
   * `code` 是最后一次的关闭码(传输层原因时通常是 `1006`)。
   */
  onHandshakeGivenUp?: (info: { failures: number; code: number }) => void
}

export interface EventsClientOptions {
  url?: string
  /** 便于单测注入假 WebSocket */
  factory?: (url: string) => WebSocketLike
  /** 便于单测注入假定时器 */
  now?: () => number
  setTimeoutFn?: (fn: () => void, ms: number) => unknown
  clearTimeoutFn?: (h: unknown) => void
  /** 退避抖动系数生成器(默认随机 ±20%),单测传常量 */
  jitter?: () => number
  heartbeatMs?: number
  deadAfterMs?: number
  /** 连续多少次「从未 open 就关闭」后停手(默认 5) */
  maxHandshakeFailures?: number
}

export interface WebSocketLike {
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((ev?: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code: number; reason?: string }) => void) | null
  onerror: ((ev?: unknown) => void) | null
}

export const ALL_EVENTS: EventKind[] = [
  'message', 'account_state', 'command_done', 'workflow', 'alert', 'resource', 'mail', 'net', 'job',
]

/** 退避序列:1s→2s→4s→…→30s 封顶 */
export function backoffMs(attempt: number, jitter = 1): number {
  const base = Math.min(30000, 1000 * 2 ** Math.max(0, attempt - 1))
  return Math.round(base * jitter)
}

/** ±20% 抖动 */
export function randomJitter(): number {
  return 0.8 + Math.random() * 0.4
}

export class EventsClient {
  private ws: WebSocketLike | null = null
  private readonly opts: Required<Omit<EventsClientOptions, 'factory'>> & { factory: (url: string) => WebSocketLike }
  private readonly hooks: EventsClientHooks
  private filter: SubscribeFilter = { events: ALL_EVENTS, accounts: ['*'] }
  private attempt = 0
  private reconnectHandle: unknown = null
  private heartbeatHandle: unknown = null
  private lastFrameAt = 0
  private stopped = false
  /** 本次连接是否曾 open 过 —— 区分「握手就被拒」与「连上后掉线」 */
  private everOpened = false
  /** 连续握手失败次数(open 成功即归零) */
  handshakeFailures = 0
  /** 已因握手连败停手,等上层重新取令牌后 `retry()` */
  gaveUp = false

  /** 已收到的最大 seq —— 重连 since_seq 用(G-08) */
  lastSeq = 0
  status: WsStatus = 'closed'

  constructor(hooks: EventsClientHooks, options: EventsClientOptions = {}) {
    this.hooks = hooks
    this.opts = {
      url: options.url ?? defaultEventsUrl(),
      factory: options.factory ?? ((u: string) => new WebSocket(u) as unknown as WebSocketLike),
      now: options.now ?? (() => Date.now()),
      setTimeoutFn: options.setTimeoutFn ?? ((fn, ms) => setTimeout(fn, ms)),
      clearTimeoutFn: options.clearTimeoutFn ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>)),
      jitter: options.jitter ?? randomJitter,
      heartbeatMs: options.heartbeatMs ?? 20000,
      deadAfterMs: options.deadAfterMs ?? 40000,
      maxHandshakeFailures: options.maxHandshakeFailures ?? 5,
    }
  }

  connect(): void {
    this.stopped = false
    this.open()
  }

  /** 上层重新取到令牌后调用:清零计数并重连 */
  retry(): void {
    this.handshakeFailures = 0
    this.gaveUp = false
    this.connect()
  }

  close(): void {
    this.stopped = true
    this.clearTimers()
    this.ws?.close()
    this.ws = null
    this.setStatus('closed')
  }

  /** 收窄/放宽订阅(P-MSG 不在前台时收窄 message,01 §2.8) */
  resubscribe(filter: Partial<SubscribeFilter>): void {
    this.filter = { ...this.filter, ...filter }
    if (this.status === 'open') this.sendSubscribe()
  }

  private setStatus(s: WsStatus): void {
    if (this.status !== s) {
      this.status = s
      this.hooks.onStatus(s)
    }
  }

  private clearTimers(): void {
    if (this.reconnectHandle !== null) this.opts.clearTimeoutFn(this.reconnectHandle)
    if (this.heartbeatHandle !== null) this.opts.clearTimeoutFn(this.heartbeatHandle)
    this.reconnectHandle = null
    this.heartbeatHandle = null
  }

  private open(): void {
    this.clearTimers()
    this.setStatus('connecting')
    const ws = this.opts.factory(this.opts.url)
    this.ws = ws
    this.everOpened = false
    ws.onopen = () => {
      this.attempt = 0
      this.everOpened = true
      this.handshakeFailures = 0
      this.lastFrameAt = this.opts.now()
      this.setStatus('open')
      this.sendSubscribe()
      this.scheduleHeartbeat()
    }
    ws.onmessage = (ev) => this.handleFrame(ev.data)
    ws.onerror = () => { /* onclose 会接着来,这里不重复调度 */ }
    ws.onclose = (ev) => {
      const code = ev?.code ?? 0
      this.clearTimers()
      this.setStatus('closed')
      // 🔴 4401 = 令牌无效(R6-62 (a) 起稳定可见):停手,等上层重取令牌后 `retry()`。
      // 再退避重连只是拿同一把坏令牌再试一次,白刷日志也白刷审计。
      if (code === 4401) {
        this.gaveUp = true
        this.stopped = true
        this.hooks.onAuthFailed?.()
        return
      }
      if (!this.everOpened) {
        this.handshakeFailures += 1
        if (this.handshakeFailures >= this.opts.maxHandshakeFailures) {
          // 握手一次都没成过 —— 再退避重连也只是刷日志;停手并交回上层
          this.gaveUp = true
          this.stopped = true
          this.hooks.onHandshakeGivenUp?.({ failures: this.handshakeFailures, code })
          return
        }
      }
      this.scheduleReconnect()
    }
  }

  private sendSubscribe(): void {
    const frame = {
      subscribe: {
        events: this.filter.events,
        accounts: this.filter.accounts,
        ...(this.filter.channels ? { channels: this.filter.channels } : {}),
        since_seq: this.lastSeq,
      },
    }
    this.ws?.send(JSON.stringify(frame))
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    this.attempt += 1
    const delay = backoffMs(this.attempt, this.opts.jitter())
    this.reconnectHandle = this.opts.setTimeoutFn(() => this.open(), delay)
  }

  private scheduleHeartbeat(): void {
    this.heartbeatHandle = this.opts.setTimeoutFn(() => {
      if (this.stopped) return
      const idle = this.opts.now() - this.lastFrameAt
      if (idle >= this.opts.deadAfterMs) {
        // 40s 无任何帧 → 判断线并重连
        this.ws?.close()
        return
      }
      try {
        this.ws?.send(JSON.stringify({ ping: 1 }))
      } catch {
        this.ws?.close()
        return
      }
      this.scheduleHeartbeat()
    }, this.opts.heartbeatMs)
  }

  private handleFrame(raw: unknown): void {
    this.lastFrameAt = this.opts.now()
    let msg: Record<string, unknown>
    try {
      msg = JSON.parse(String(raw)) as Record<string, unknown>
    } catch {
      return
    }

    // 首帧可能是重放截断告知
    if (msg.replay === 'truncated') {
      const from = Number(msg.from_seq ?? 0)
      this.lastSeq = Number.isFinite(from) ? from : 0
      this.hooks.onReplayTruncated(this.lastSeq)
      return
    }
    // 心跳帧:只刷新存活,不入环形缓冲
    if (msg.event === 'ping' || msg.ping !== undefined || msg.pong !== undefined) return

    if (typeof msg.event !== 'string') return
    const ev = msg as unknown as QtEvent
    const seq = Number(ev.seq)
    if (Number.isFinite(seq)) {
      // 按 seq 去重:比已收到的最大 seq 小或相等的一律丢
      if (seq <= this.lastSeq) return
      this.lastSeq = seq
    }
    this.hooks.onEvent(ev)
  }
}

export function defaultEventsUrl(): string {
  // Electron 形态:preload 给出的 Agent 源优先(主进程只对它注入令牌;页面来源是 Vite/file://,不是 Agent)
  const origin = typeof window !== 'undefined' ? window.qt?.endpoint?.agent : undefined
  if (typeof origin === 'string' && /^https?:\/\/[^/]+$/.test(origin)) {
    return origin.replace(/^http/, 'ws') + '/api/v1/events'
  }
  if (typeof location !== 'undefined' && location.protocol.startsWith('http')) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}/api/v1/events`
  }
  return 'ws://127.0.0.1:17600/api/v1/events'
}
