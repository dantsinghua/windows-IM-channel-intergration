/**
 * `audit` store:指令 / API / 系统事件三个 Tab(C-40 kind、C-42 参数名)。
 *
 * 🔴 02 #95 的参数列只有 `kind|actor|account_id|action|since|until|limit|cursor|fmt`。
 * 01 §4 还留了 `qt-log-filter-{op|code}` 两个控件与 trace 抽屉,服务端没有对应参数
 * (未知参数被 FastAPI 静默忽略 ⇒ 筛选器会「假装生效」,S-02)。
 * 因此:**`op`/`code`/`trace_id` 一律在客户端本地过滤**,并在界面上标明「本地筛选」。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { auditApi } from '@/api/client'
import { auditDetail, type AuditRow } from '@/api/types'

export type AuditKind = 'command' | 'api' | 'system'

/** trace 抽屉在本地筛的窗口大小(服务端不认 `trace_id` 参数) */
const TRACE_SCAN_LIMIT = 200

export const useAuditStore = defineStore('audit', () => {
  const kind = ref<AuditKind>('command')
  /** `account`→`account_id`、`actor`/`since`/`until` 走服务端;`op`/`code` 本地筛 */
  const filter = ref<Record<string, string | undefined>>({})
  const rawRows = ref<AuditRow[]>([])
  const nextCursor = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const traceRows = ref<AuditRow[]>([])

  const rows = computed(() => {
    const op = (filter.value.op ?? '').trim()
    const code = (filter.value.code ?? '').trim()
    if (!op && !code) return rawRows.value
    return rawRows.value.filter((r) => {
      const d = auditDetail(r)
      const opText = `${r.action ?? ''} ${d.op ?? ''}`
      if (op && !opText.includes(op)) return false
      if (code && !String(r.result_code ?? '').includes(code)) return false
      return true
    })
  })

  /** 本地筛掉的行数 —— 界面据此提示「本地筛选」而不是让人以为服务端筛过了 */
  const localFilteredOut = computed(() => rawRows.value.length - rows.value.length)

  async function load(more = false): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const r = await auditApi.list({
        kind: kind.value,
        account_id: filter.value.account_id,
        actor: filter.value.actor,
        since: filter.value.since,
        until: filter.value.until,
        limit: 50,
        cursor: more ? nextCursor.value ?? undefined : undefined,
      })
      rawRows.value = more ? [...rawRows.value, ...r.items] : r.items
      nextCursor.value = r.nextCursor
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** trace 抽屉:服务端无 `trace_id` 过滤 ⇒ 拉最近一窗再本地筛(01 §4 `qt-log-trace-drawer`) */
  async function loadTrace(traceId: string): Promise<void> {
    const r = await auditApi.list({ limit: TRACE_SCAN_LIMIT })
    traceRows.value = r.items.filter((x) => x.trace_id === traceId)
  }

  return {
    kind, filter, rows, rawRows, nextCursor, loading, error, traceRows, localFilteredOut,
    load, loadTrace,
  }
})
