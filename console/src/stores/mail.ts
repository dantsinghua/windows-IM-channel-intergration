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

export const useMailStore = defineStore('mail', () => {
  const status = ref<MailStatus | null>(null)
  const pending = ref<PendingConfirm[]>([])
  const inbox = ref<MailInboxRow[]>([])
  const inboxFilter = ref<Record<string, unknown>>({ limit: 50 })
  const inboxDetail = ref<MailInboxDetail | null>(null)
  const outbox = ref<MailOutboxRow[]>([])
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
        mailApi.outbox(),
        mailApi.cleanupLog(),
      ])
      status.value = st
      pending.value = pc.items
      inbox.value = ib.items
      outbox.value = ob.items
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

  async function reloadInbox(): Promise<void> {
    inbox.value = (await mailApi.inbox(inboxFilter.value)).items
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
    status, pending, inbox, inboxFilter, inboxDetail, outbox, cleanupLog, loading, error, cleanupLockedUntil,
    reloadAll, reloadPending, reloadInbox, startPolling, stopPolling, bindEvents, cleanupLocked, lockCleanup,
  }
})
