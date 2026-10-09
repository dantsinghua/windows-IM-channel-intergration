/** 首页监控回归：真实 App/store/DOM，所有外部 API 与事件连接均隔离。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { reactive } from 'vue'
import App from '@/App.vue'
import DashPage from '@/pages/dash/DashPage.vue'
import ResPage from '@/pages/res/ResPage.vue'
import { messagesApi, resourcesApi } from '@/api/client'
import type { MetricsSnapshot, ResourcePool, SystemHealth, SystemVersion } from '@/api/types'
import type { LocalMetricsSnapshot } from '@/types/local-metrics'
import { useAccountsStore } from '@/stores/accounts'
import { useCommandsStore } from '@/stores/commands'
import { useEnvStore } from '@/stores/env'
import { useEventsStore } from '@/stores/events'
import { useMailStore } from '@/stores/mail'
import { useResourcesStore } from '@/stores/resources'
import { useSessionStore } from '@/stores/session'
import { useSetupStore } from '@/stores/setup'
import { useUiStore } from '@/stores/ui'
import { dash as T } from '@/testids'

const routing = vi.hoisted(() => ({ route: { path: '/dash', query: {} as Record<string, string> } }))
vi.mock('vue-router', () => ({
  useRoute: () => routing.route,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/router', () => ({ installGuards: vi.fn() }))

const STUBS = Object.fromEntries([
  'a-config-provider', 'a-app', 'a-badge', 'a-button', 'a-switch', 'a-drawer',
  'a-empty', 'a-progress', 'router-view',
].map((name) => [name, true]))
const wrappers: VueWrapper[] = []
let replayTruncated: (() => void) | undefined
const localMetrics = vi.fn<() => Promise<LocalMetricsSnapshot>>()

function localSnapshot(): LocalMetricsSnapshot {
  return {
    memory: { available: true, source: 'electron_host', sampledAt: '2026-10-08T12:00:00Z', totalBytes: 32 * 1024 ** 3, usedBytes: 6 * 1024 ** 3, freeBytes: 26 * 1024 ** 3 },
    storage: { available: true, bytes: 2 * 1024 ** 3, sampledAt: '2026-10-08T12:00:00Z', scope: 'client_directory', measurement: 'file_sizes', excludedLinks: 0 },
  }
}

function pool(): ResourcePool {
  return {
    pools: {
      wsl: { total_mb: 11264, used_mb: 2048, reserved_mb: 1024, free_mb: 8192 },
      windows: {
        total_mb: 16384, reserved_mb: 4096, wechat_mb: 1536,
        wechat_slots: { used: 0, max: 1, holder: '', pending: '', pending_expires_at: null, pending_login_session_id: '' },
      },
    },
    realtime: { wsl_anon_mb: null, win_available_mb: null },
    quota_mb: { qidian: 2048, qq: 512, wechat: 1536 },
    can_add: { qidian: 4, qq: 16, wechat: 1 },
  }
}

function metrics(): MetricsSnapshot {
  return {
    hardware: {
      // 与 11 GB 资源池故意不同，锁住整机读数与预算不可混用。
      mem: { total_mb: 28672, used_mb: 8192, avail_mb: 20480, vmmem_mb: 4096 },
      cpu: { logical_cores: 8, load_pct: 12.5 },
      disks: [{ mount: '/data', total_mb: 204800, free_mb: 51200 }],
    },
    ours: { procs: { agent_mb: null, winagent_mb: null, console_mb: null }, accounts: [] },
    budget_vs_actual: [],
    disk_watermark: { level: 'normal', free_mb: 51200, actions: [], last_cleanup_at: null, last_cleanup_freed_mb: null },
    mem_watermark: { level: 'normal', avail_mb: 20480, lru_suggest: [] },
  }
}

function mountDash(): VueWrapper {
  // R6-81 将状态图抽为独立组件；仍验证真实 DOM，不以 stub 隐去状态判据。
  const wrapper = shallowMount(DashPage, { global: { stubs: { ...STUBS, SystemTopology: false }, renderStubDefaultSlot: true } })
  wrappers.push(wrapper)
  return wrapper
}

function mountApp(): void {
  wrappers.push(shallowMount(App, { global: { stubs: STUBS, renderStubDefaultSlot: true } }))
}

function card(wrapper: VueWrapper, id: string) {
  return wrapper.get(`[data-testid="${id}"]`)
}

function statusDot(wrapper: VueWrapper, name: string) {
  return card(wrapper, T.sys(name)).get('.health')
}

function setSystem(dockerd: boolean | null, kernelState: string | null, dockerVersion: string | null = null): void {
  const env = useEnvStore()
  env.version = {
    agent: { version: 'test' }, api_version: '1.0', capabilities_version: 'test', schema_version: 1,
    winagent: { online: true, version: 'test' }, kernel: null, kernel_state: kernelState, docker: dockerVersion,
  } satisfies SystemVersion
  // 真实鉴权健康接口在尚未探测时允许 null；旧 TS 类型对此描述过窄。
  env.health = { ok: true, agent: true, winagent: true, dockerd } as unknown as SystemHealth
}

beforeEach(() => {
  setActivePinia(createPinia())
  routing.route = reactive({ path: '/dash', query: {} })
  localMetrics.mockReset().mockResolvedValue(localSnapshot())
  vi.stubGlobal('qt', { app: { localMetrics, rssKb: vi.fn().mockResolvedValue(0) }, router: { onRoute: vi.fn(() => vi.fn()) }, tray: { update: vi.fn() }, notify: vi.fn() })
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  replayTruncated = undefined
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network call in isolated dashboard test')))
  vi.spyOn(resourcesApi, 'get').mockResolvedValue(pool())
  vi.spyOn(resourcesApi, 'metrics').mockResolvedValue(metrics())
  vi.spyOn(messagesApi, 'list').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  vi.spyOn(useMailStore(), 'reloadAll').mockResolvedValue()
  vi.spyOn(useMailStore(), 'stopPolling').mockImplementation(() => undefined)
  vi.spyOn(useCommandsStore(), 'loadCatalog').mockResolvedValue()
  vi.spyOn(useEnvStore(), 'loadAll').mockResolvedValue()
  vi.spyOn(useSessionStore(), 'install').mockImplementation(() => undefined)
  vi.spyOn(useSessionStore(), 'uninstall').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'start').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'stop').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'onReplayTruncated').mockImplementation((fn) => { replayTruncated = fn })
  vi.spyOn(useSetupStore(), 'loadConfig').mockResolvedValue()
  vi.spyOn(useSetupStore(), 'refreshAck').mockResolvedValue()
  vi.spyOn(useUiStore(), 'loadFromConfig').mockResolvedValue()
  vi.spyOn(useUiStore(), 'applyTheme').mockImplementation(() => undefined)
})

afterEach(() => {
  for (const wrapper of wrappers.splice(0).reverse()) wrapper.unmount()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('首页监控快照加载链', () => {
  it('冷进首页分别读取后端快照与本机总内存、客户端目录大小，不能混用预算或部署卷', async () => {
    mountApp()
    const page = mountDash()
    await flushPromises()
    expect(resourcesApi.metrics).toHaveBeenCalledTimes(1)
    expect(localMetrics).toHaveBeenCalledOnce()
    expect(card(page, T.resMem).get('strong').text()).toMatch(/32\.0+\s*GB/)
    expect(card(page, T.resMem).text()).toContain('已用 6.00')
    expect(card(page, T.resDisk).get('strong').text()).toMatch(/2\.0+\s*GB/)
    expect(card(page, T.resDisk).text()).toContain('客户端目录占用')
    expect(card(page, T.resDisk).text()).not.toMatch(/50\.0|200\.0|\/data/)
    // 首页不再显示资源池预算卡；预算仍加载，但摘要必须使用实测值。
    expect(resourcesApi.get).toHaveBeenCalledTimes(1)
    expect(page.findAll(`[data-testid="${T.resMem}"]`)).toHaveLength(1)
    expect(page.findAll(`[data-testid="${T.resDisk}"]`)).toHaveLength(1)
    expect(page.find('.detail-grid').exists()).toBe(false)
  })

  it('30 秒兜底刷新同时更新监控快照，不只刷新资源池', async () => {
    mountApp()
    const page = mountDash()
    await flushPromises()
    vi.mocked(resourcesApi.metrics).mockClear()
    localMetrics.mockClear()
    const next = metrics()
    next.hardware!.cpu!.load_pct = 18.5
    const localNext = localSnapshot()
    if (localNext.memory.available) localNext.memory.totalBytes = 64 * 1024 ** 3
    localMetrics.mockResolvedValue(localNext)
    vi.mocked(resourcesApi.metrics).mockResolvedValue(next)
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(resourcesApi.metrics).toHaveBeenCalledTimes(1)
    expect(localMetrics).toHaveBeenCalledTimes(1)
    expect(card(page, T.resMem).get('strong').text()).toMatch(/64\.0+\s*GB/)
    expect(page.get('[aria-label="运行摘要"]').text()).toContain('18.5')
  })

  it('WS 重放被截断后的全量恢复也重新获取监控快照', async () => {
    mountApp()
    await flushPromises()
    vi.mocked(resourcesApi.metrics).mockClear()
    expect(replayTruncated).toBeTypeOf('function')
    replayTruncated!()
    await flushPromises()
    expect(resourcesApi.metrics).toHaveBeenCalledTimes(1)
  })
})

describe('首页缺数和采样失败不能伪装正常', () => {
  it('没有本机桥时显示未知，不显示 normal 或虚构 0', async () => {
    vi.stubGlobal('qt', undefined)
    const page = mountDash()
    await flushPromises()
    for (const id of [T.resMem, T.resDisk]) {
      expect(card(page, id).get('strong').text()).toContain('—')
      expect(card(page, id).text()).not.toContain('normal')
      expect(card(page, id).text()).not.toContain('0.0 GB')
    }
  })

  it('本机采样失败时不借用后端 WSL 或部署分区数字填空', async () => {
    const snapshot = metrics()
    useResourcesStore().metrics = snapshot
    const native = localSnapshot()
    native.memory = { available: false, source: 'electron_host', sampledAt: '2026-10-08T12:00:00Z', totalBytes: null, usedBytes: null, freeBytes: null, error: 'read_failed' }
    native.storage = { ...native.storage, available: false, bytes: null, error: 'timeout' }
    localMetrics.mockResolvedValue(native)
    const page = mountDash()
    await flushPromises()
    for (const id of [T.resMem, T.resDisk]) {
      expect(card(page, id).get('strong').text()).toContain('—')
      expect(card(page, id).text()).not.toContain('normal')
    }
  })

  it('监控更新失败保留最后数值并明确提示旧数据，资源池成功不能抹去此提示', async () => {
    const store = useResourcesStore()
    await store.loadMetrics()
    vi.mocked(resourcesApi.metrics).mockRejectedValue(new Error('metrics unavailable'))
    await store.loadMetrics()
    await store.load()
    const page = mountDash()
    expect(page.get('[aria-label="运行摘要"]').text()).toContain('12.5')
    expect(page.text()).toMatch(/更新失败|旧数据|暂不可用/)
  })

  it('新 WS 监控快照覆盖旧数据并清除更新失败提示', async () => {
    const store = useResourcesStore()
    await store.loadMetrics()
    vi.mocked(resourcesApi.metrics).mockRejectedValue(new Error('metrics unavailable'))
    await store.loadMetrics()
    const page = mountDash()
    expect(page.text()).toMatch(/更新失败|旧数据|暂不可用/)
    const next = metrics()
    next.hardware!.cpu!.load_pct = 20.5
    store.applyResource({ event: 'resource', seq: 10, ts: '2026-10-08T08:00:00Z', payload: { metrics_snapshot: next } })
    await flushPromises()
    expect(page.get('[aria-label="运行摘要"]').text()).toContain('20.5')
    expect(page.text()).not.toMatch(/更新失败|旧数据|暂不可用/)
  })

  it('本机桥刷新失败后清除旧目录数值，显示不可用而非旧值冒充新采样', async () => {
    const page = mountDash()
    await flushPromises()
    expect(card(page, T.resDisk).get('strong').text()).toMatch(/2\.0+\s*GB/)
    localMetrics.mockRejectedValue(new Error('fixture bridge unavailable'))
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(card(page, T.resDisk).get('strong').text()).toContain('—')
    expect(card(page, T.resMem).get('strong').text()).toContain('—')
    expect(card(page, T.resDisk).text()).toMatch(/不可用|未连接|未采集|客户端/)
  })

  it('离开首页后停止本机目录轮询，不继续触发后台扫描', async () => {
    const page = mountDash()
    await flushPromises()
    expect(localMetrics).toHaveBeenCalledOnce()
    page.unmount()
    wrappers.splice(wrappers.indexOf(page), 1)
    await vi.advanceTimersByTimeAsync(90000)
    expect(localMetrics).toHaveBeenCalledOnce()
  })
})

describe('清理入口只导航并定位', () => {
  it('首页清理是独立链接，不嵌套在指标链接内，也不发清理请求', async () => {
    const cleanup = vi.spyOn(resourcesApi, 'cleanupRun').mockRejectedValue(new Error('No cleanup allowed'))
    const page = mountDash()
    await flushPromises()
    const link = page.get(`[data-testid="${T.cleanup}"]`)
    expect(link.element.tagName).toBe('A')
    expect(link.attributes('href')).toBe('#/res?section=cleanup')
    expect(link.element.parentElement?.closest('a')).toBeNull()
    await link.trigger('click')
    expect(cleanup).not.toHaveBeenCalled()
  })

  it('资源页清理深链滚动并聚焦目标，但不会自动启动清理', async () => {
    routing.route.path = '/res'
    routing.route.query = { section: 'cleanup' }
    const cleanup = vi.spyOn(resourcesApi, 'cleanupRun').mockRejectedValue(new Error('No cleanup allowed'))
    const originalScroll = HTMLElement.prototype.scrollIntoView
    const scroll = vi.fn()
    HTMLElement.prototype.scrollIntoView = scroll
    try {
      const page = shallowMount(ResPage, {
        attachTo: document.body,
        global: { renderStubDefaultSlot: true, stubs: { ...STUBS, PageState: false, 'a-skeleton': true, 'a-popconfirm': true } },
      })
      wrappers.push(page)
      await flushPromises()
      const target = page.get('#cleanup')
      expect(target.attributes('tabindex')).toBe('-1')
      expect(scroll).toHaveBeenCalledOnce()
      expect(document.activeElement).toBe(target.element)
      expect(cleanup).not.toHaveBeenCalled()
    } finally {
      if (originalScroll) HTMLElement.prototype.scrollIntoView = originalScroll
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
    }
  })
})

describe('首页系统状态与版本信息分开判断', () => {
  it.each([null, 'UNKNOWN'])('内核状态 %s 是未知，不能因未自检或缺版本判红', (state) => {
    setSystem(true, state)
    const page = mountDash()
    expect(statusDot(page, 'kernel').attributes('aria-label')).toMatch(/未知|未独立采样/)
    expect(statusDot(page, 'kernel').classes()).not.toContain('bad')
    expect(statusDot(page, 'kernel').classes()).not.toContain('ok')
  })

  it('已明确 OURS 内核状态为正常', () => {
    setSystem(true, 'OURS')
    const page = mountDash()
    expect(statusDot(page, 'kernel').classes()).toContain('ok')
  })

  it('明确 DEFAULT 内核状态保持异常', () => {
    setSystem(true, 'DEFAULT')
    expect(statusDot(mountDash(), 'kernel').classes()).toContain('bad')
  })

  it('Docker 缺版本但 dockerd=true 时仍正常', () => {
    setSystem(true, null)
    expect(statusDot(mountDash(), 'docker').classes()).toContain('ok')
  })

  it('Docker 有版本但 dockerd=false 时必须异常', () => {
    setSystem(false, null, '27.5.1')
    expect(statusDot(mountDash(), 'docker').classes()).toContain('bad')
  })

  it('Docker 尚未探测，即使有版本也只能显示未知', () => {
    setSystem(null, null, '27.5.1')
    const page = mountDash()
    expect(statusDot(page, 'docker').attributes('aria-label')).toMatch(/未知|未独立采样/)
    expect(statusDot(page, 'docker').classes()).not.toContain('bad')
    expect(statusDot(page, 'docker').classes()).not.toContain('ok')
  })
})

describe('资源监控页错误反馈', () => {
  it('监控请求失败显示错误，资源池成功不能抹去，重试成功后恢复数值', async () => {
    vi.mocked(resourcesApi.metrics).mockRejectedValue(new Error('metrics unavailable'))
    const page = shallowMount(ResPage, {
      global: {
        renderStubDefaultSlot: true,
        stubs: {
          ...STUBS,
          PageState: false,
          'a-skeleton': true,
          'a-result': {
            props: ['title'],
            template: '<section role="alert">{{ title }}<slot name="extra" /></section>',
          },
        },
      },
    })
    wrappers.push(page)
    await flushPromises()
    expect(page.get('[role="alert"]').text()).toContain('metrics unavailable')

    await useResourcesStore().load()
    await flushPromises()
    expect(page.get('[role="alert"]').text()).toContain('metrics unavailable')

    vi.mocked(resourcesApi.metrics).mockResolvedValue(metrics())
    await page.get('[role="alert"] a-button-stub').trigger('click')
    await flushPromises()
    expect(page.find('[role="alert"]').exists()).toBe(false)
    expect(page.get('[data-testid="qt-res-mem-host-total"]').text()).toMatch(/^28\.0+ GB$/)
  })
})
