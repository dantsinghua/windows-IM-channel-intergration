/** `commands` store:能力目录(schema)、最近指令与结果 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { commandsApi } from '@/api/client'
import type { CapabilityDef, CommandResult, QtEvent } from '@/api/types'
import type { Channel } from '@/i18n/zh-CN/codes'
import { useEventsStore } from './events'

export interface HistoryEntry {
  n: number
  accountId: string
  op: string
  args: Record<string, unknown>
  idempotencyKey?: string
  confirm?: boolean
  timeoutMs?: number
  result?: CommandResult
  errorCode?: string
  errorMessage?: string
  errorReason?: string | null
  traceId?: string
  at: string
}

const HISTORY_MAX = 50

export const useCommandsStore = defineStore('commands', () => {
  const catalog = ref<CapabilityDef[]>([])
  const catalogVersion = ref('')
  const history = ref<HistoryEntry[]>([])
  const running = ref(false)
  const lastResult = ref<CommandResult | null>(null)
  const lastError = ref<{ code: string; message: string; reason: string | null; traceId: string } | null>(null)
  const broadcastResults = ref<Record<string, CommandResult>>({})

  const byOp = computed(() => Object.fromEntries(catalog.value.map((c) => [c.op, c])) as Record<string, CapabilityDef>)

  async function loadCatalog(): Promise<void> {
    const r = await commandsApi.capabilities()
    catalog.value = r.items
    catalogVersion.value = r.version
  }

  /** 某账号上该 op 的标注:supported / UNSUPPORTED / NOT_APPLICABLE */
  function tagFor(op: string, channel: Channel, accountCaps: string[]): 'supported' | 'UNSUPPORTED' | 'NOT_APPLICABLE' {
    if (accountCaps.includes(op)) return 'supported'
    const def = byOp.value[op]
    if (!def) return 'UNSUPPORTED'
    const c = def.channels?.[channel]
    return c === 'not_applicable' ? 'NOT_APPLICABLE' : 'UNSUPPORTED'
  }

  function pushHistory(e: Omit<HistoryEntry, 'n' | 'at'>): void {
    history.value.unshift({ ...e, n: history.value.length + 1, at: new Date().toISOString() })
    if (history.value.length > HISTORY_MAX) history.value.length = HISTORY_MAX
  }

  function applyCommandDone(ev: QtEvent<{ trace_id: string; code: string; cost_ms: number }>): void {
    const p = ev.payload
    const hit = history.value.find((h) => h.traceId === p.trace_id)
    if (hit && hit.result) {
      hit.result = { ...hit.result, code: p.code, cost_ms: p.cost_ms }
      if (lastResult.value?.trace_id === p.trace_id) lastResult.value = { ...hit.result }
    }
  }

  function bindEvents(): void {
    useEventsStore().on('command_done', (ev) =>
      applyCommandDone(ev as QtEvent<{ trace_id: string; code: string; cost_ms: number }>))
  }

  return {
    catalog, catalogVersion, history, running, lastResult, lastError, broadcastResults,
    byOp, loadCatalog, tagFor, pushHistory, applyCommandDone, bindEvents,
  }
})
