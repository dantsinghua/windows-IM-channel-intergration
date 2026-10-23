/** `env` store:版本、健康、环境快照、探测、net_state、公网端点(一律经 Agent,C-32) */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { systemApi } from '@/api/client'
import { dockerCidrOf } from '@/api/types'
import type {
  ObservedProbeRow, ProbeRow, PublicEndpoint, QtEvent, SelftestRow, SelftestRun, SystemEnv, SystemHealth,
  SystemVersion,
} from '@/api/types'
import { useEventsStore } from './events'

/** 公网出口变更历史的一行(01 §2.7.9:数据源 = `NET_PUBLIC_ENDPOINT_CHANGED` 事件) */
export interface EndpointChange {
  at: string
  from_ip: string
  to_ip: string
}

const ENDPOINT_HISTORY_MAX = 20

export const useEnvStore = defineStore('env', () => {
  const version = ref<SystemVersion | null>(null)
  const health = ref<SystemHealth | null>(null)
  const snapshot = ref<SystemEnv | null>(null)
  const probes = ref<ProbeRow[]>([])
  /**
   * 实测采样候选(#76 `?kind=observed`)。
   * 🔴 行带 `id`(= `probe_targets_observed.id`)与 `in_config`;
   * 勾选态一律按 `id` 记,**不得用行下标**(R6-58 (cu):下标会随排序/新采样漂移)。
   */
  const observed = ref<ObservedProbeRow[]>([])
  /** 当前正式探测目标(#76 observed 的第二个键 / #76b 出参) */
  const observedTargets = ref<string[]>([])
  const selftest = ref<SelftestRow[]>([])
  /** #79 的原始一轮对象(页面要展开细节时用) */
  const selftestRun = ref<SelftestRun | null>(null)
  const selftestRunId = ref<string | null>(null)
  const selftestAt = ref<string | null>(null)
  const publicEndpoint = ref<PublicEndpoint | null>(null)
  /** 公网出口变更历史:#102 出参里没有 `history`,按 01 §2.7.9 由 net 事件累积 */
  const endpointHistory = ref<EndpointChange[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  /** Agent 不可达时的一句话状态(§5.4;经主进程 qt.wa.invoke 读 /wa/v1/health) */
  const agentDownReason = ref<string | null>(null)

  /**
   * #74 把 Windows 侧与 WSL 侧分成两半:`net_state` / 代理 / VPN / docker 冲突都在
   * `windows`(= `GET /wa/v1/net`)里;WinAgent 不可达时 `windows` 为 null。
   */
  const netState = computed(() => snapshot.value?.windows?.net_state ?? 'DIRECT')
  /** Windows 侧取不到时的原因(`winagent_offline` / `winagent_error` / …),页面据此显示「未知 + 原因」 */
  const windowsError = computed(() => (snapshot.value && !snapshot.value.windows ? snapshot.value.windows_error ?? 'winagent_error' : null))
  const dockerConflict = computed(() => snapshot.value?.windows?.docker_conflict ?? { state: 'ok' as const })
  const dockerCidr = computed(() => dockerCidrOf(snapshot.value))
  const winagentOnline = computed(() => {
    const w = health.value?.winagent
    return typeof w === 'object' ? w.online : !!w
  })
  const userAgentOnline = computed(() => {
    const w = health.value?.winagent
    return typeof w === 'object' ? w.user_agent === true : false
  })
  /** 自检有红项则 P-SETUP 不能继续(P-ENV 只标红不阻断) */
  const selftestHasError = computed(() => selftest.value.some((r) => r.level === 'error'))
  /** 采样表「上次」时刻 = 各候选行里最新的 `last_seen_at` */
  const observedAt = computed(() => {
    const times = observed.value.map((r) => r.last_seen_at ?? '').filter(Boolean).sort()
    return times.length ? times[times.length - 1] : null
  })

  /**
   * 首屏四拉。
   * 🔴 逐个端点各自兜底:后端有 13 个端点还没实现(#74 `/system/env` 就在其中),
   * 一个 404 不能把整页拖成错误态 —— 页面对缺的那块显示「不可用 + 重试」。
   */
  async function loadAll(): Promise<void> {
    loading.value = true
    error.value = null
    const results = await Promise.allSettled([
      systemApi.version(), systemApi.health(), systemApi.env(), systemApi.probes(),
    ])
    const [v, h, e, p] = results
    if (v.status === 'fulfilled') version.value = v.value
    if (h.status === 'fulfilled') health.value = h.value
    if (e.status === 'fulfilled') snapshot.value = e.value
    if (p.status === 'fulfilled') probes.value = p.value.items
    const failed = results.filter((r) => r.status === 'rejected') as PromiseRejectedResult[]
    // 全挂 ⇒ 整页错误态;部分挂 ⇒ 只在顶部提示,已取到的块照常显示
    if (failed.length === results.length) {
      error.value = failed[0]?.reason instanceof Error ? failed[0].reason.message : String(failed[0]?.reason)
    } else if (failed.length) {
      error.value = null
      agentDownReason.value = `部分环境信息暂不可用(${failed.length}/${results.length} 个端点未就绪)`
    }
    loading.value = false
  }

  async function loadPublicEndpoint(): Promise<void> {
    publicEndpoint.value = await systemApi.publicEndpoint()
  }

  async function runSelftest(): Promise<void> {
    // 开跑前清掉上一轮的错误,免得这轮成功了页面还挂着旧的「还没写完」
    error.value = null
    const started = await systemApi.selftestRun()
    const runId = started.run_id
    const deadline = Date.now() + 45_000
    do {
      await loadSelftest(runId)
      if (selftestRun.value?.finished_at) return
      await new Promise((resolve) => setTimeout(resolve, 400))
    } while (Date.now() < deadline)
    error.value = '自检结果还没写完,请再点一次'
  }

  /** #79b:不带 `run_id` 取最近一轮;带上则等这一轮落库。从没跑过时 `data:null` ⇒ 空表(不是错误) */
  async function loadSelftest(runId?: string): Promise<void> {
    const r = await systemApi.selftestResult(runId)
    selftest.value = r.items
    selftestRun.value = r.run
    selftestRunId.value = r.runId
    // 「上次 HH:MM」用服务端的 finished_at,取不到才回落本地时刻
    selftestAt.value = r.run?.finished_at ?? (r.items.length ? new Date().toISOString() : null)
  }

  async function runProbe(): Promise<void> {
    const r = await systemApi.probe()
    probes.value = r.results
  }

  /** #76 `?kind=observed`:取带 `id`/`in_config` 的候选行与当前正式目标 */
  async function loadObserved(): Promise<void> {
    const r = await systemApi.observedProbes()
    observed.value = r.items
    observedTargets.value = r.targets
  }

  /** C-1 实测采样(01 §2.7.9):采完立刻按 #76 observed 重取,拿到稳定 `id` */
  async function runSample(durationS = 30): Promise<void> {
    await systemApi.probeSample(durationS)
    await loadObserved()
  }

  /**
   * #76b 写入探测目标。
   * 🔴 `observedIds` 是**采纳后的全集**:整张表的当前勾选态,不是本次新增的那几个;
   * `[]` 合法 = 清空全部采纳。写完按出参 `targets` 刷新,并跑一轮探测。
   */
  async function adoptObserved(observedIds: number[]): Promise<void> {
    const r = await systemApi.adoptProbeTargets(observedIds)
    observedTargets.value = r.targets
    await loadObserved()
    await runProbe()
  }

  function bindEvents(): void {
    const events = useEventsStore()
    events.on('net', (ev: QtEvent) => {
      const p = ev.payload as {
        net_state?: string
        code?: string
        evidence?: { from_ip?: string; to_ip?: string }
      }
      if (p.net_state && snapshot.value) {
        // net 事件只带 net_state:就地覆盖 windows 侧那一项,别把整份快照打回 null
        snapshot.value = {
          ...snapshot.value,
          windows: { ...(snapshot.value.windows ?? {}), net_state: p.net_state },
        }
      }
      if (p.code === 'NET_PUBLIC_ENDPOINT_CHANGED') {
        endpointHistory.value.unshift({
          at: ev.ts,
          from_ip: p.evidence?.from_ip ?? '—',
          to_ip: p.evidence?.to_ip ?? '—',
        })
        if (endpointHistory.value.length > ENDPOINT_HISTORY_MAX) {
          endpointHistory.value.length = ENDPOINT_HISTORY_MAX
        }
        void loadPublicEndpoint()
      }
      if (p.code === 'DOCKER_POOL_ALL_CONFLICT' || p.net_state) {
        void systemApi.env().then((e) => { snapshot.value = e }).catch(() => undefined)
      }
    })
  }

  return {
    version, health, snapshot, probes, observed, observedTargets, observedAt,
    selftest, selftestRun, selftestRunId, selftestAt, publicEndpoint, endpointHistory,
    loading, error, agentDownReason,
    netState, windowsError, dockerConflict, dockerCidr, winagentOnline, userAgentOnline, selftestHasError,
    loadAll, loadPublicEndpoint, runSelftest, loadSelftest, runProbe, loadObserved, runSample,
    adoptObserved, bindEvents,
  }
})
