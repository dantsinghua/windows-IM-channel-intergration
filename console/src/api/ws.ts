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
  /** 令牌失效(4401) */
  onAuthFailed?: () => void
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
    }
  }

  connect(): void {
    this.stopped = false
    this.open()
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
    ws.onopen = () => {
      this.attempt = 0
      this.lastFrameAt = this.opts.now()
      this.setStatus('open')
      this.sendSubscribe()
      this.scheduleHeartbeat()
    }
    ws.onmessage = (ev) => this.handleFrame(ev.data)
    ws.onerror = () => { /* onclose 会接着来,这里不重复调度 */ }
    ws.onclose = (ev) => {
      if (ev?.code === 4401) this.hooks.onAuthFailed?.()
      this.clearTimers()
      this.setStatus('closed')
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
  if (typeof location !== 'undefined' && location.protocol.startsWith('http')) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${location.host}/api/v1/events`
  }
  return 'ws://127.0.0.1:17600/api/v1/events'
}
