/**
 * `mail` store:五块(待确认危险指令 / 水位健康 / 收件 / 发件 / 清理)。
 * 🔴 `mail` 事件是**告警**不是数据变更(C-15):收到即重拉五块,另每 30s 定时重拉。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { mailApi } from '@/api/client'
import type {
  MailCleanupRow, MailInboxDetail, MailInboxRow, MailOutboxRow, MailStatus, PendingConfirm,
} from '@/api/types'
import { useEventsStore } from './events'

/** C-42 通用段字面的默认页大小(`limit=50`) */
const PAGE_LIMIT = 50

export const useMailStore = defineStore('mail', () => {
  const status = ref<MailStatus | null>(null)
  const pending = ref<PendingConfirm[]>([])
  const inbox = ref<MailInboxRow[]>([])
  const inboxFilter = ref<Record<string, unknown>>({ limit: PAGE_LIMIT })
  /** 收件的 C-42 游标;非空 = 还有下一页 */
  const inboxCursor = ref<string | null>(null)
  const inboxDetail = ref<MailInboxDetail | null>(null)
  const outbox = ref<MailOutboxRow[]>([])
  const outboxFilter = ref<Record<string, unknown>>({ limit: PAGE_LIMIT })
  /** 发件的 C-42 游标 */
  const outboxCursor = ref<string | null>(null)
  const cleanupLog = ref<MailCleanupRow[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** 立即清理 60s 防重 */
  const cleanupLockedUntil = ref(0)

  let timer: ReturnType<typeof setInterval> | null = null

  async function reloadAll(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [st, pc, ib, ob, cl] = await Promise.all([
        mailApi.status(),
        mailApi.pendingConfirms(),
        mailApi.inbox(inboxFilter.value),
        mailApi.outbox(outboxFilter.value),
        mailApi.cleanupLog(),
      ])
      status.value = st
      pending.value = pc.items
      // 整页重拉 = 回到第一页,两条翻页线的游标一并重置(C-42)
      inbox.value = ib.items
      inboxCursor.value = ib.nextCursor
      outbox.value = ob.items
      outboxCursor.value = ob.nextCursor
      cleanupLog.value = cl.items
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function reloadPending(): Promise<void> {
    pending.value = (await mailApi.pendingConfirms()).items
  }

  /** `more=true` = 用游标续下一页并**追加**;否则按当前筛选从头拉并重置游标(C-42) */
  async function reloadInbox(more = false): Promise<void> {
    const r = await mailApi.inbox({
      ...inboxFilter.value,
      cursor: more ? inboxCursor.value ?? undefined : undefined,
    })
    inbox.value = more ? [...inbox.value, ...r.items] : r.items
    inboxCursor.value = r.nextCursor
  }

  /** 发件同款(#61 的 `limit/cursor` 与 `since/until` 都已铺开) */
  async function reloadOutbox(more = false): Promise<void> {
    const r = await mailApi.outbox({
      ...outboxFilter.value,
      cursor: more ? outboxCursor.value ?? undefined : undefined,
    })
    outbox.value = more ? [...outbox.value, ...r.items] : r.items
    outboxCursor.value = r.nextCursor
  }

  function startPolling(): void {
    if (timer) return
    // 页面可见时每 30s 重拉;不可见时停
    timer = setInterval(() => {
      if (typeof document === 'undefined' || document.visibilityState === 'visible') void reloadAll()
    }, 30000)
  }

  function stopPolling(): void {
    if (timer) clearInterval(timer)
    timer = null
  }

  function bindEvents(): void {
    useEventsStore().on('mail', () => void reloadAll())
  }

  function cleanupLocked(now = Date.now()): boolean {
    return now < cleanupLockedUntil.value
  }

  function lockCleanup(now = Date.now()): void {
    cleanupLockedUntil.value = now + 60000
  }

  return {
    status, pending, inbox, inboxFilter, inboxCursor, inboxDetail,
    outbox, outboxFilter, outboxCursor, cleanupLog, loading, error, cleanupLockedUntil,
    reloadAll, reloadPending, reloadInbox, reloadOutbox,
    startPolling, stopPolling, bindEvents, cleanupLocked, lockCleanup,
  }
})
