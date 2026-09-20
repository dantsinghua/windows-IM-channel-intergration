/**
 * `messages` store:会话列表、检索条件、分页结果、媒体缓存、导出任务。
 * 🔴 事件专属三字段 `lag_s`/`late`/`origin` 只挂在**经事件前插**的行上,
 * 不做本地持久化;`GET /messages` 重拉后消失是预期行为(R6-49,00 §7.4)。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { messagesApi, type MessageQuery } from '@/api/client'
import { normalizeSession } from '@/api/types'
import type { Message, QtEvent, SessionRow } from '@/api/types'
import { useEventsStore } from './events'

export interface EventFlags {
  lag_s?: number
  late?: boolean
  origin?: 'rpa' | 'external'
}

export const useMessagesStore = defineStore('messages', () => {
  const sessions = ref<SessionRow[]>([])
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

  async function loadSessions(): Promise<void> {
    const { items: rows } = await messagesApi.sessions({ account_id: filter.value.account_id })
    // 裁决②:统一按 `last_msg_at`;`last_ts` 的一次性兼容在 normalizeSession 里(后端改完删)
    sessions.value = rows.map(normalizeSession)
  }

  async function search(reset = true): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const q: MessageQuery = { ...filter.value }
      if (selectedSessionId.value) q.session_id = selectedSessionId.value
      if (!reset && nextCursor.value) q.cursor = nextCursor.value
      const r = await messagesApi.list(q)
      items.value = reset ? r.items : [...items.value, ...r.items]
      nextCursor.value = r.nextCursor
      if (reset) {
        newCount.value = 0
        // 重拉即丢事件标志:字段不落库(00 §7.4)
        eventFlags.value = {}
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** 命中当前筛选才前插 */
  function matchesFilter(m: Message): boolean {
    const f = filter.value
    if (f.account_id && m.account_id !== f.account_id) return false
    if (selectedSessionId.value && m.session.id !== selectedSessionId.value) return false
    if (f.dir && m.dir !== f.dir) return false
    if (f.type && m.type !== f.type) return false
    if (f.needs_review && !m.needs_review) return false
    return true
  }

  function applyMessageEvent(ev: QtEvent<{ message?: Message } & Message>): void {
    const raw = (ev.payload as { message?: Message }).message ?? (ev.payload as unknown as Message)
    if (!raw?.id) return
    if (!matchesFilter(raw)) return
    const flags: EventFlags = {}
    if (raw.lag_s !== undefined) flags.lag_s = raw.lag_s
    if (raw.late !== undefined) flags.late = raw.late
    if (raw.origin !== undefined) flags.origin = raw.origin
    eventFlags.value[raw.id] = flags
    if (!items.value.some((m) => m.id === raw.id)) {
      items.value.unshift(raw)
      newCount.value += 1
    }
  }

  function bindEvents(): void {
    useEventsStore().on('message', (ev) => applyMessageEvent(ev as QtEvent<Message>))
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
    sessions, sessionKeyword, selectedSessionId, filter, items, nextCursor, loading, error,
    newCount, selectedId, eventFlags, mediaUrls, exportJobId,
    selected, qTooShort, filteredSessions,
    loadSessions, search, applyMessageEvent, bindEvents, flagsOf, lateText, isExternal, matchesFilter,
  }
})
