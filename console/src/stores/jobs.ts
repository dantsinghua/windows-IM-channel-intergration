/**
 * `jobs` store:异步作业统一契约(R-补,§11.21 [JOB])。
 * 凡 `202 {job_id}` 的操作都走这里:轮询 `GET /jobs/{job_id}` + 监听 `job` 事件推终态。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { jobsApi } from '@/api/client'
import type { Job, QtEvent } from '@/api/types'
import { useEventsStore } from './events'

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'expired'])

export const useJobsStore = defineStore('jobs', () => {
  const byId = ref<Record<string, Job>>({})
  const timers = new Map<string, ReturnType<typeof setInterval>>()

  function isTerminal(j: Job | undefined): boolean {
    return !!j && TERMINAL.has(j.state)
  }

  function put(j: Job): void {
    byId.value[j.job_id] = { ...byId.value[j.job_id], ...j }
    if (isTerminal(byId.value[j.job_id])) stopPolling(j.job_id)
  }

  function stopPolling(jobId: string): void {
    const t = timers.get(jobId)
    if (t) {
      clearInterval(t)
      timers.delete(jobId)
    }
  }

  /** 开始跟踪一个 job:事件优先,轮询兜底(1.5s) */
  function track(jobId: string, intervalMs = 1500): void {
    if (timers.has(jobId)) return
    byId.value[jobId] = byId.value[jobId] ?? { job_id: jobId, kind: '', state: 'queued', progress: 0 }
    void poll(jobId)
    timers.set(jobId, setInterval(() => void poll(jobId), intervalMs))
  }

  async function poll(jobId: string): Promise<void> {
    try {
      put(await jobsApi.get(jobId))
    } catch {
      // 轮询失败不打断界面;事件仍可能推终态
    }
  }

  async function cancel(jobId: string): Promise<void> {
    await jobsApi.cancel(jobId)
    await poll(jobId)
  }

  function bindEvents(): void {
    useEventsStore().on('job', (ev: QtEvent) => put(ev.payload as Job))
  }

  function clear(jobId: string): void {
    stopPolling(jobId)
    delete byId.value[jobId]
  }

  return { byId, track, cancel, put, clear, isTerminal, bindEvents }
})
