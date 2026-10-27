/**
 * `messages` store:会话列表、检索条件、分页结果、媒体缓存、导出任务。
 * 🔴 事件专属三字段 `lag_s`/`late`/`origin` 只挂在**经事件前插**的行上,
 * 不做本地持久化;`GET /messages` 重拉后消失是预期行为(R6-49,00 §7.4)。
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { commandsApi, messagesApi, type MessageQuery } from '@/api/client'
import { normalizeSession } from '@/api/types'
import type { CommandResult, Message, QtEvent, SessionRow } from '@/api/types'
import { useEventsStore } from './events'

export interface EventFlags {
  lag_s?: number
  late?: boolean
  origin?: 'rpa' | 'external'
}

/** C-42 通用段字面的默认页大小(`limit=50`) */
const PAGE_LIMIT = 50

export const useMessagesStore = defineStore('messages', () => {
  const sessions = ref<SessionRow[]>([])
  /** 会话列表的 C-42 游标(与消息列表的 `nextCursor` 是两条独立的翻页线) */
  const sessionsCursor = ref<string | null>(null)
  const sessionKeyword = ref('')
  const selectedSessionId = ref<string | null>(null)
  const filter = ref<MessageQuery>({ limit: 50 })
  const items = ref<Message[]>([])
  const nextCursor = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const newCount = ref(0)
  const selectedId = ref<string | null>(null)
  /** 本次会话内的事件标志,按 message.id 挂;刷新即丢 */
  const eventFlags = ref<Record<string, EventFlags>>({})
  const mediaUrls = ref<Record<string, string>>({})
  const exportJobId = ref<string | null>(null)
  const messageResults = ref<Record<string, CommandResult>>({})
  const messageSeq = new Map<string, number>()
  const messageRevision = new Map<string, number>()
  const refreshRequests = new Map<string, number>()
  const resultRequests = new Map<string, number>()
  const completedTraces = new Map<string, number>()
  const canonicalMessages = new Map<string, string>()
  let revision = 0
  let searchRequest = 0
  let eventsBound = false

  function queryKey(): string {
    return JSON.stringify([filter.value, selectedSessionId.value])
  }

  function traceKey(m: Pick<Message, 'account_id' | 'trace_id'>): string {
    return `${m.account_id}\u0000${m.trace_id ?? ''}`
  }

  function nativeKey(m: Message): string | null {
    return m.channel === 'wechat' && m.ext_msg_id ? `${m.account_id}\u0000${m.ext_msg_id}` : null
  }

  function isMergedNative(m: Message): boolean {
    const key = nativeKey(m)
    return Boolean(key && !m.trace_id && canonicalMessages.has(key) && canonicalMessages.get(key) !== m.id)
  }

  function deduplicateNative(rows: Message[]): Message[] {
    for (const m of rows) {
      const key = nativeKey(m)
      if (key && m.trace_id && m.state === 'DELIVERED' && m.confirmed_by) canonicalMessages.set(key, m.id)
    }
    return rows.filter((m) => {
      if (!isMergedNative(m)) return true
      if (selectedId.value === m.id) selectedId.value = canonicalMessages.get(nativeKey(m)!)!
      delete eventFlags.value[m.id]
      return false
    })
  }

  async function loadResult(m: Message): Promise<void> {
    if (!m.trace_id || m.dir !== 'out') return
    const request = (resultRequests.get(m.id) ?? 0) + 1
    resultRequests.set(m.id, request)
    try {
      const r = await commandsApi.get(m.account_id, m.trace_id)
      if (resultRequests.get(m.id) === request && r.result) messageResults.value[m.id] = r.result
    } catch {
      // A missing command result is not evidence of delivery or failure.
    }
  }

  /** #49 is authoritative; command codes never supply a message state. */
  async function refreshMessage(m: Message, seq = useEventsStore().lastSeq): Promise<void> {
    const context = queryKey()
    const request = (refreshRequests.get(m.id) ?? 0) + 1
    refreshRequests.set(m.id, request)
    const version = ++revision
    messageRevision.set(m.id, version)
    messageSeq.set(m.id, Math.max(messageSeq.get(m.id) ?? 0, seq, useEventsStore().lastSeq))
    try {
      const fresh = await messagesApi.get(m.id)
      if (queryKey() !== context || refreshRequests.get(m.id) !== request || messageRevision.get(m.id) !== version) return
      if (fresh.id !== m.id || fresh.account_id !== m.account_id || fresh.session.id !== m.session.id) return
      if (isMergedNative(fresh)) return
      const index = items.value.findIndex((row) => row.id === m.id)
      if (!matchesFilter(fresh, index >= 0)) return
      if (index >= 0) items.value[index] = fresh
      else {
        items.value.unshift(fresh)
        newCount.value += 1
      }
      items.value = deduplicateNative(items.value)
      messageRevision.set(m.id, ++revision)
      void loadResult(fresh)
    } catch (e) {
      if (queryKey() === context && refreshRequests.get(m.id) === request) {
        error.value = e instanceof Error ? e.message : String(e)
      }
    }
  }

  const selected = computed(() => items.value.find((m) => m.id === selectedId.value) ?? null)
  /** UI 提示「关键字至少 3 个字」(02-P8 FTS trigram) */
  const qTooShort = computed(() => {
    const q = (filter.value.q ?? '').trim()
    return q.length > 0 && q.length < 3
  })

  const filteredSessions = computed(() => {
    const kw = sessionKeyword.value.trim()
    if (!kw) return sessions.value
    return sessions.value.filter((s) => s.name.includes(kw) || s.id.includes(kw))
  })

  /**
   * 拉会话列表。`more=true` = 用游标续下一页并**追加**(C-42;后端已把 `limit/cursor` 铺到 `#26`,
   * 排序 `last_msg_at` 降序),否则从头拉并重置游标。
   */
  async function loadSessions(more = false): Promise<void> {
    const r = await messagesApi.sessions({
      account_id: filter.value.account_id,
      limit: PAGE_LIMIT,
      cursor: more ? sessionsCursor.value ?? undefined : undefined,
    })
    // 裁决②:统一按 `last_msg_at`;`last_ts` 的一次性兼容在 normalizeSession 里(后端改完删)
    const rows = r.items.map(normalizeSession)
    sessions.value = more ? [...sessions.value, ...rows] : rows
    sessionsCursor.value = r.nextCursor
  }

  async function search(reset = true): Promise<void> {
    const request = ++searchRequest
    const context = queryKey()
    const version = revision
    const seq = useEventsStore().lastSeq
    loading.value = true
    error.value = null
    try {
      const q: MessageQuery = { ...filter.value }
      if (selectedSessionId.value) q.session_id = selectedSessionId.value
      if (!reset && nextCursor.value) q.cursor = nextCursor.value
      const r = await messagesApi.list(q)
      if (request !== searchRequest || queryKey() !== context) return
      const current = new Map(items.value.map((m) => [m.id, m]))
      const rows = r.items.map((m) => {
        if ((messageRevision.get(m.id) ?? 0) > version && current.has(m.id)) return current.get(m.id)!
        messageSeq.set(m.id, Math.max(messageSeq.get(m.id) ?? 0, seq))
        messageRevision.set(m.id, ++revision)
        return m
      })
      const ids = new Set(rows.map((m) => m.id))
      const keep = reset
        ? items.value.filter((m) => !ids.has(m.id) && (messageRevision.get(m.id) ?? 0) > version && matchesFilter(m))
        : items.value.filter((m) => !ids.has(m.id))
      if (reset) items.value = [...keep, ...rows]
      else {
        const updates = new Map(rows.map((m) => [m.id, m]))
        items.value = [...items.value.map((m) => updates.get(m.id) ?? m), ...rows.filter((m) => !current.has(m.id))]
      }
      items.value = deduplicateNative(items.value)
      nextCursor.value = r.nextCursor
      if (reset) {
        newCount.value = 0
        // 重拉即丢事件标志:字段不落库(00 §7.4)
        eventFlags.value = {}
      }
      for (const m of rows) {
        if (m.state === 'FAILED' || m.state === 'UNCONFIRMED') void loadResult(m)
        const doneSeq = completedTraces.get(traceKey(m))
        if (doneSeq !== undefined && m.state === 'SENDING') void refreshMessage(m, doneSeq)
      }
    } catch (e) {
      if (request === searchRequest && queryKey() === context) error.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (request === searchRequest) loading.value = false
    }
  }

  /** 命中当前筛选才前插 */
  function matchesFilter(m: Message, existing = false): boolean {
    const f = filter.value
    if (f.account_id && m.account_id !== f.account_id) return false
    if (selectedSessionId.value && m.session.id !== selectedSessionId.value) return false
    if (f.dir && m.dir !== f.dir) return false
    if (f.type && m.type !== f.type) return false
    if (f.needs_review && !m.needs_review) return false
    // 全文匹配交给后端；查询中不前插未经同一 FTS 规则验证的事件。
    if (f.q?.trim() && !existing) return false
    const at = Date.parse(m.ts)
    if (f.since && (!Number.isFinite(at) || at < Date.parse(f.since))) return false
    if (f.until && (!Number.isFinite(at) || at > Date.parse(f.until))) return false
    return true
  }

  function applyMessageEvent(ev: QtEvent<{ message?: Message } & Message>): void {
    const incoming = (ev.payload as { message?: Message }).message ?? (ev.payload as unknown as Message)
    if (!incoming?.id || isMergedNative(incoming)) return
    const index = items.value.findIndex((m) => m.id === incoming.id)
    const raw = index >= 0 ? { ...items.value[index], ...incoming } : incoming
    if (!matchesFilter(raw, index >= 0)) return
    const seq = Number.isSafeInteger(ev.seq) && ev.seq > 0 ? ev.seq : undefined
    if (seq !== undefined && seq <= (messageSeq.get(raw.id) ?? 0)) return
    const doneSeq = completedTraces.get(traceKey(raw))
    if (seq === undefined || (doneSeq !== undefined && (seq <= doneSeq || raw.state === 'SENDING'))
        || (raw.dir === 'out' && raw.state === 'DELIVERED' && raw.trace_id && !raw.confirmed_by)) {
      void refreshMessage(raw, Math.max(seq ?? 0, doneSeq ?? 0, useEventsStore().lastSeq))
      return
    }
    messageSeq.set(raw.id, seq)
    messageRevision.set(raw.id, ++revision)
    const flags: EventFlags = {}
    if (raw.lag_s !== undefined) flags.lag_s = raw.lag_s
    if (raw.late !== undefined) flags.late = raw.late
    if (raw.origin !== undefined) flags.origin = raw.origin
    eventFlags.value[raw.id] = flags
    if (index >= 0) items.value[index] = raw
    else {
      items.value.unshift(raw)
      newCount.value += 1
    }
    items.value = deduplicateNative(items.value)
    if (raw.state === 'FAILED' || raw.state === 'UNCONFIRMED') void loadResult(raw)
  }

  function applyCommandDone(ev: QtEvent<{ trace_id?: string }>): void {
    const traceId = ev.payload?.trace_id ?? ev.trace_id
    if (!traceId || !ev.account_id) return
    const key = traceKey({ account_id: ev.account_id, trace_id: traceId })
    const seq = Number.isSafeInteger(ev.seq) && ev.seq > 0 ? ev.seq : useEventsStore().lastSeq
    if (seq <= (completedTraces.get(key) ?? -1)) return
    completedTraces.set(key, seq)
    for (const m of items.value) {
      if (traceKey(m) === key) void refreshMessage(m, seq)
    }
  }

  function bindEvents(): void {
    if (eventsBound) return
    eventsBound = true
    const events = useEventsStore()
    events.on('message', (ev) => applyMessageEvent(ev as QtEvent<Message>))
    events.on('command_done', (ev) => applyCommandDone(ev as QtEvent<{ trace_id?: string }>))
    watch(() => events.connected, (connected, wasConnected) => {
      if (connected && !wasConnected) {
        for (const m of items.value) void refreshMessage(m)
      }
    })
  }

  function flagsOf(id: string): EventFlags {
    return eventFlags.value[id] ?? {}
  }

  /** 「迟到 {lag_s} s」;≥3600 s 显示小时(R6-49) */
  function lateText(id: string): string {
    const f = flagsOf(id)
    if (!f.late || f.lag_s === undefined) return ''
    if (f.lag_s >= 3600) return `迟到 ${Math.round(f.lag_s / 3600)} 小时`
    return `迟到 ${f.lag_s} s`
  }

  function isExternal(id: string): boolean {
    return flagsOf(id).origin === 'external'
  }

  return {
    sessions, sessionsCursor, sessionKeyword, selectedSessionId, filter, items, nextCursor, loading, error,
    newCount, selectedId, eventFlags, mediaUrls, exportJobId, messageResults,
    selected, qTooShort, filteredSessions,
    loadSessions, search, applyMessageEvent, applyCommandDone, refreshMessage, loadResult, bindEvents, flagsOf, lateText, isExternal, matchesFilter,
  }
})
