/**
 * `events` store:WS 连接状态、最近 500 条事件环形缓冲、`last_seq`、告警集合。
 * 告警按 `(code, subject)` 去重(C-14);`state=firing` 为未读。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { AlertPayload, EventKind, QtEvent } from '@/api/types'
import { ALL_EVENTS, EventsClient, type WsStatus } from '@/api/ws'

const RING_SIZE = 500

export const alertKey = (a: Pick<AlertPayload, 'code' | 'subject'>) => `${a.code}\u0000${a.subject}`

/** 告警时间 → 毫秒;ISO 字符串、毫秒数字、缺失都能比较(缺失记 0 排最后) */
export function seenMs(v: unknown): number {
  if (typeof v === 'number') return v
  if (typeof v === 'string' && v) {
    const t = Date.parse(v)
    return Number.isNaN(t) ? 0 : t
  }
  return 0
}

/** 告警 → 应该跳哪一页(01 §2.8:由 subject 与 code 前缀推出;`source` 不是字段) */
export function alertRoute(a: Pick<AlertPayload, 'code' | 'subject'>): string {
  const { code, subject } = a
  if (code === 'WECHAT_DISK_LOW') return '/env'
  if (code === 'H12_DISK_LOW' || code === 'MEM_PRESSURE' || code === 'H23_VHDX_GROWTH' || code === 'DB_WRITE_FAILED') return '/res'
  if (code.startsWith('MAIL_')) {
    if (subject.startsWith('route:')) return '/set'
    if (subject.startsWith('endpoint:')) return '/env'
    return '/mail'
  }
  if (code.startsWith('POOL_')) return '/dash'
  if (code.startsWith('WSLCONFIG_')) return '/set'
  if (subject.startsWith('account:')) return `/acct/${subject.slice('account:'.length)}`
  if (subject.startsWith('net:')) return '/env'
  if (code.startsWith('H')) return '/env'
  return '/env'
}

export const useEventsStore = defineStore('events', () => {
  const status = ref<WsStatus>('closed')
  const lastSeq = ref(0)
  const ring = ref<QtEvent[]>([])
  const alerts = ref<Map<string, AlertPayload>>(new Map())
  /** 重放截断后置 true,全量拉完置回 —— 顶部「同步中」横幅 */
  const syncing = ref(false)
  /** 断线时刻,用于 qt-acct-stale-banner 的「N 秒前」 */
  const disconnectedAt = ref<number | null>(null)
  /**
   * 事件流「需要重新取令牌」:
   * ① 服务端回 **4401**(令牌无效);② 连续 N 次握手就断(从未 open)。
   *
   * 🔴 ① 的时序按 02 §3.4.7 **R6-62 (a)**:服务端**先 `accept()` 再 `close(4401)`**,
   * 后端已经这么实现了 —— 所以 4401 是**看得到**的,不会再退化成 1006
   * (R6-52 原「accept 前关闭」已作废;旧注释说的「后端会改」这件事已经改完)。
   * 处置 = 停止重连 + 顶部挂横幅 + 用户点按钮重取令牌(`retry()`),**界面不自动重连**:
   * 令牌没换,重连只会再吃一次 4401。
   * ② 仍保留:网络/代理/端口等传输层原因下关闭码通常是 1006,那种情况才靠连败计数停手。
   */
  const authLost = ref<{ reason: '4401' | 'handshake'; failures?: number; code?: number } | null>(null)

  let client: EventsClient | null = null
  const handlers = new Map<EventKind, ((ev: QtEvent) => void)[]>()
  let truncatedHandler: (() => void) | null = null

  const firing = computed(() =>
    [...alerts.value.values()]
      .filter((a) => a.state === 'firing')
      // `last_seen_at` 规格是 ISO 字符串;防御:旧版 Agent 曾发毫秒整数 / 缺字段,一律转成可比较的时间值,不让排序抛错
      .sort((a, b) => sevRank(b.severity) - sevRank(a.severity) || seenMs(b.last_seen_at) - seenMs(a.last_seen_at)),
  )
  const unreadCount = computed(() => firing.value.length)
  const connected = computed(() => status.value === 'open')
  /** E-18/E-19:crit 且 subject∈{host,wsl} 的磁盘/内存水位告警 → 顶部常驻红横幅 */
  const watermarkAlert = computed(() =>
    firing.value.find(
      (a) =>
        a.severity === 'crit' &&
        (a.subject === 'host' || a.subject === 'wsl') &&
        (a.code === 'H12_DISK_LOW' || a.code === 'MEM_PRESSURE' || a.code === 'DB_WRITE_FAILED'),
    ) ?? null,
  )

  function sevRank(s: string): number {
    return s === 'crit' ? 3 : s === 'warn' ? 2 : 1
  }

  function on(kind: EventKind, fn: (ev: QtEvent) => void): void {
    const list = handlers.get(kind) ?? []
    list.push(fn)
    handlers.set(kind, list)
  }

  function onReplayTruncated(fn: () => void): void {
    truncatedHandler = fn
  }

  function pushAlert(p: AlertPayload): void {
    const key = alertKey(p)
    if (p.state === 'resolved') {
      const prev = alerts.value.get(key)
      if (prev) alerts.value.set(key, { ...prev, ...p, state: 'resolved' })
      return
    }
    const prev = alerts.value.get(key)
    alerts.value.set(key, prev ? { ...prev, ...p, count: p.count ?? prev.count + 1 } : p)
    alerts.value = new Map(alerts.value)
  }

  function dismissResolved(): void {
    for (const [k, v] of alerts.value) if (v.state === 'resolved') alerts.value.delete(k)
    alerts.value = new Map(alerts.value)
  }

  function ingest(ev: QtEvent): void {
    if (typeof ev.seq === 'number' && ev.seq > lastSeq.value) lastSeq.value = ev.seq
    // 环形缓冲:二维码等敏感 payload 不进(§6-10)
    ring.value.push(sanitize(ev))
    if (ring.value.length > RING_SIZE) ring.value.splice(0, ring.value.length - RING_SIZE)

    const payload = ev.payload as Partial<AlertPayload> | undefined
    if ((ev.event === 'alert' || ev.event === 'mail' || ev.event === 'net' || ev.event === 'resource') && payload?.code) {
      pushAlert(payload as AlertPayload)
    }
    for (const fn of handlers.get(ev.event) ?? []) fn(ev)
  }

  /** 二维码 base64 绝不进环形缓冲/日志(01 §6-10) */
  function sanitize(ev: QtEvent): QtEvent {
    const p = ev.payload as Record<string, unknown> | undefined
    const prompt = p?.prompt as Record<string, unknown> | undefined
    if (prompt && 'qrcode_png_b64' in prompt) {
      const { qrcode_png_b64: _drop, ...restPrompt } = prompt
      return { ...ev, payload: { ...p, prompt: { ...restPrompt, qrcode_png_b64: '<omitted>' } } }
    }
    return ev
  }

  function start(): void {
    if (client) return
    client = new EventsClient({
      onEvent: ingest,
      onAuthFailed: () => {
        authLost.value = { reason: '4401' }
      },
      onHandshakeGivenUp: (info) => {
        authLost.value = { reason: 'handshake', failures: info.failures, code: info.code }
      },
      onStatus: (s) => {
        status.value = s
        if (s === 'open') {
          disconnectedAt.value = null
        } else if (disconnectedAt.value === null) {
          disconnectedAt.value = Date.now()
        }
      },
      onReplayTruncated: () => {
        syncing.value = true
        truncatedHandler?.()
      },
    })
    client.connect()
  }

  function stop(): void {
    client?.close()
    client = null
  }

  /** 门禁「重试」按钮:上层重新取到令牌后重连事件流 */
  function retry(): void {
    authLost.value = null
    if (client) client.retry()
    else start()
  }

  /** P-MSG 不在前台时把 message 收窄为当前筛选的账号(01 §2.8) */
  function narrowMessages(accounts: string[]): void {
    client?.resubscribe({ events: ALL_EVENTS, accounts: accounts.length ? accounts : ['*'] })
  }

  function markSynced(): void {
    syncing.value = false
  }

  /** 单测/mock 注入用 */
  function injectForTest(ev: QtEvent): void {
    ingest(ev)
  }

  return {
    status, lastSeq, ring, alerts, syncing, disconnectedAt, authLost,
    firing, unreadCount, connected, watermarkAlert,
    on, onReplayTruncated, start, stop, retry, narrowMessages, markSynced, injectForTest,
    dismissResolved, pushAlert,
  }
})
