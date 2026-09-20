/** `resources` store:ResourcePool + 监控快照(§7.6 / 02 #77) */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { resourcesApi } from '@/api/client'
import type { MetricsSnapshot, QtEvent, ResourcePool } from '@/api/types'
import type { Channel } from '@/i18n/zh-CN/codes'
import { useEventsStore } from './events'

export const useResourcesStore = defineStore('resources', () => {
  const pool = ref<ResourcePool | null>(null)
  const metrics = ref<MetricsSnapshot | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const lastAt = ref<string | null>(null)

  const slots = computed(() => pool.value?.pools.windows.wechat_slots ?? null)
  /** 🔴 判据是**非空串**,不是 `!= null`(R-04/§11.18 [SLOT]) */
  const hasPending = computed(() => (slots.value?.pending ?? '') !== '')
  const pendingLoginSessionId = computed(() => slots.value?.pending_login_session_id ?? '')
  const canCancelPending = computed(() => hasPending.value && pendingLoginSessionId.value !== '')
  const canAdd = computed(() => pool.value?.can_add ?? ({ qidian: 0, qq: 0, wechat: 0 } as Record<Channel, number>))

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      pool.value = await resourcesApi.get()
      lastAt.value = new Date().toISOString()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function loadMetrics(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      metrics.value = await resourcesApi.metrics()
      lastAt.value = new Date().toISOString()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** `resource` 事件有两副面孔:code 非空 = 告警(events store 处理);code 为空 = 数据事件 */
  function applyResource(ev: QtEvent<Record<string, unknown>>): void {
    const p = ev.payload
    if (p && typeof p === 'object' && 'code' in p && p.code) return
    if (p?.pools) pool.value = p as unknown as ResourcePool
    if (p?.metrics_snapshot) metrics.value = p.metrics_snapshot as MetricsSnapshot
    lastAt.value = new Date().toISOString()
  }

  function bindEvents(): void {
    useEventsStore().on('resource', (ev) => applyResource(ev as QtEvent<Record<string, unknown>>))
  }

  return {
    pool, metrics, loading, error, lastAt,
    slots, hasPending, pendingLoginSessionId, canCancelPending, canAdd,
    load, loadMetrics, applyResource, bindEvents,
  }
})
