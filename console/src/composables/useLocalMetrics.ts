import { onMounted, onUnmounted, ref } from 'vue'
import type { LocalMetricsSnapshot } from '@/types/local-metrics'

/** 只读客户端采样。普通浏览器没有本机桥，保持未知，不借用 Agent 分区数据。 */
export function useLocalMetrics() {
  const snapshot = ref<LocalMetricsSnapshot | null>(null)
  const loading = ref(false)
  const unavailable = ref(false)
  let timer: ReturnType<typeof setInterval> | undefined
  let active = true

  async function refresh(): Promise<void> {
    if (loading.value) return
    const read = window.qt?.app?.localMetrics
    if (!read) { unavailable.value = true; snapshot.value = null; return }
    loading.value = true
    try {
      const result = await read()
      if (active) { snapshot.value = result; unavailable.value = false }
    } catch {
      if (active) { snapshot.value = null; unavailable.value = true }
    } finally {
      loading.value = false
    }
  }

  onMounted(() => {
    void refresh()
    timer = setInterval(() => { void refresh() }, 30000)
  })
  onUnmounted(() => { active = false; clearInterval(timer) })

  return { snapshot, loading, unavailable, refresh }
}
