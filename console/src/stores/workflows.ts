/** `workflows` store:定义、运行、步骤(M5 只做直线流程,R-13) */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { workflowsApi } from '@/api/client'
import type { QtEvent, WorkflowDef, WorkflowRun } from '@/api/types'
import { useEventsStore } from './events'

export const useWorkflowsStore = defineStore('workflows', () => {
  const items = ref<WorkflowDef[]>([])
  const currentRun = ref<WorkflowRun | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      items.value = (await workflowsApi.list()).items
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  async function loadRun(runId: string): Promise<void> {
    currentRun.value = await workflowsApi.getRun(runId)
  }

  /** `workflow` 事件推进步骤;`command_done` 不再承担此职责(C-16) */
  function applyWorkflow(ev: QtEvent<{ run_id: string; step_id?: string; status: string; code?: string; cost_ms?: number }>): void {
    const p = ev.payload
    if (!currentRun.value || currentRun.value.run_id !== p.run_id) return
    if (!p.step_id) {
      currentRun.value = { ...currentRun.value, status: p.status }
      return
    }
    const steps = currentRun.value.steps.map((s) =>
      s.step_id === p.step_id ? { ...s, status: p.status, code: p.code ?? s.code, cost_ms: p.cost_ms ?? s.cost_ms } : s,
    )
    currentRun.value = { ...currentRun.value, steps }
  }

  function bindEvents(): void {
    useEventsStore().on('workflow', (ev) =>
      applyWorkflow(ev as QtEvent<{ run_id: string; status: string; step_id?: string }>))
  }

  return { items, currentRun, loading, error, load, loadRun, applyWorkflow, bindEvents }
})
