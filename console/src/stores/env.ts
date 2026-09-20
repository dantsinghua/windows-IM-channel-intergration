/** `env` store:版本、健康、环境快照、探测、net_state、公网端点(一律经 Agent,C-32) */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { systemApi } from '@/api/client'
import type {
  ProbeRow, PublicEndpoint, QtEvent, SampleRow, SelftestRow, SystemEnv, SystemHealth, SystemVersion,
} from '@/api/types'
import { useEventsStore } from './events'

export const useEnvStore = defineStore('env', () => {
  const version = ref<SystemVersion | null>(null)
  const health = ref<SystemHealth | null>(null)
  const snapshot = ref<SystemEnv | null>(null)
  const probes = ref<ProbeRow[]>([])
  const samples = ref<SampleRow[]>([])
  const sampledAt = ref<string | null>(null)
  const selftest = ref<SelftestRow[]>([])
  const selftestAt = ref<string | null>(null)
  const publicEndpoint = ref<PublicEndpoint | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** Agent 不可达时的一句话状态(§5.4;经主进程 qt.wa.invoke 读 /wa/v1/health) */
  const agentDownReason = ref<string | null>(null)

  const netState = computed(() => snapshot.value?.net_state ?? 'DIRECT')
  const dockerConflict = computed(() => snapshot.value?.docker_conflict ?? { state: 'ok' as const })
  const winagentOnline = computed(() => {
    const w = health.value?.winagent
    return typeof w === 'object' ? w.online : !!w
  })
  const userAgentOnline = computed(() => {
    const w = health.value?.winagent
    return typeof w === 'object' ? w.user_agent : false
  })
  /** 自检有红项则 P-SETUP 不能继续(P-ENV 只标红不阻断) */
  const selftestHasError = computed(() => selftest.value.some((r) => r.level === 'error'))

  async function loadAll(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [v, h, e, p] = await Promise.all([
        systemApi.version(), systemApi.health(), systemApi.env(), systemApi.probes(),
      ])
      version.value = v
      health.value = h
      snapshot.value = e
      probes.value = p.items
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  async function loadPublicEndpoint(): Promise<void> {
    publicEndpoint.value = await systemApi.publicEndpoint()
  }

  async function runSelftest(): Promise<void> {
    await systemApi.selftestRun()
    selftest.value = (await systemApi.selftestResult()).items
    selftestAt.value = new Date().toISOString()
  }

  async function runProbe(): Promise<void> {
    const r = await systemApi.probe()
    probes.value = r.results
  }

  async function runSample(durationS = 30): Promise<void> {
    const r = await systemApi.probeSample(durationS)
    samples.value = r.rows
    sampledAt.value = r.sampled_at
  }

  function bindEvents(): void {
    const events = useEventsStore()
    events.on('net', (ev: QtEvent) => {
      const p = ev.payload as { net_state?: string; code?: string }
      if (p.net_state && snapshot.value) snapshot.value = { ...snapshot.value, net_state: p.net_state }
      if (p.code === 'NET_PUBLIC_ENDPOINT_CHANGED') void loadPublicEndpoint()
      if (p.code === 'DOCKER_POOL_ALL_CONFLICT' || p.net_state) void systemApi.env().then((e) => { snapshot.value = e })
    })
  }

  return {
    version, health, snapshot, probes, samples, sampledAt, selftest, selftestAt, publicEndpoint,
    loading, error, agentDownReason,
    netState, dockerConflict, winagentOnline, userAgentOnline, selftestHasError,
    loadAll, loadPublicEndpoint, runSelftest, runProbe, runSample, bindEvents,
  }
})
