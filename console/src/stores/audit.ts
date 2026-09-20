/** `audit` store:指令 / API / 系统事件三个 Tab(C-40 kind、C-42 参数名) */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { auditApi } from '@/api/client'
import type { AuditRow } from '@/api/types'

export type AuditKind = 'command' | 'api' | 'system'

export const useAuditStore = defineStore('audit', () => {
  const kind = ref<AuditKind>('command')
  const filter = ref<Record<string, string | undefined>>({})
  const rows = ref<AuditRow[]>([])
  const nextCursor = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const traceRows = ref<AuditRow[]>([])

  async function load(more = false): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const r = await auditApi.list({
        kind: kind.value,
        ...filter.value,
        limit: 50,
        cursor: more ? nextCursor.value ?? undefined : undefined,
      })
      rows.value = more ? [...rows.value, ...r.items] : r.items
      nextCursor.value = r.nextCursor
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function loadTrace(traceId: string): Promise<void> {
    traceRows.value = (await auditApi.list({ trace_id: traceId, limit: 200 })).items
  }

  return { kind, filter, rows, nextCursor, loading, error, traceRows, load, loadTrace }
})
