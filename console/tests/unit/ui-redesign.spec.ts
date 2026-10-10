/** 2026-10-08 账号中心改版的开发者行为验证。仅假后端，不启动真实服务。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { reactive, type Component } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { Modal, message } from 'ant-design-vue'
import { accountsApi, auditApi, messagesApi, systemApi } from '@/api/client'
import type { Account, AuditRow, Message, MetricsSnapshot } from '@/api/types'
import { router } from '@/router'
import App from '@/App.vue'
import DashPage from '@/pages/dash/DashPage.vue'
import MsgPage from '@/pages/msg/MsgPage.vue'
import LogPage from '@/pages/log/LogPage.vue'
import EnvPage from '@/pages/env/EnvPage.vue'
import AcctDetailPage from '@/pages/acct/AcctDetailPage.vue'
import { useAccountsStore } from '@/stores/accounts'
import { useAuditStore } from '@/stores/audit'
import { useMessagesStore } from '@/stores/messages'
import { useEnvStore } from '@/stores/env'
import { useSessionStore } from '@/stores/session'
import { useEventsStore } from '@/stores/events'
import { useResourcesStore } from '@/stores/resources'
import { useMailStore } from '@/stores/mail'
import { useCommandsStore } from '@/stores/commands'
import { useSetupStore } from '@/stores/setup'
import { useUiStore } from '@/stores/ui'
import { env as ENV, msg as MSG, log as LOG } from '@/testids'

const routing = vi.hoisted(() => ({
  route: { path: '/dash', query: {} as Record<string, string>, params: {} as Record<string, string> },
  push: vi.fn(), replace: vi.fn(),
}))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => routing.route,
  useRouter: () => ({ push: routing.push, replace: routing.replace }),
}))
vi.mock('@/router', async (original) => ({
  ...await original<typeof import('@/router')>(), installGuards: vi.fn(),
}))

const STUBS = Object.fromEntries([
  'a-config-provider', 'a-app', 'a-badge', 'a-button', 'a-switch', 'a-drawer',
  'a-empty', 'a-progress', 'a-alert', 'a-input', 'a-select', 'a-checkbox',
  'a-radio', 'a-radio-group', 'a-dropdown', 'a-menu', 'a-menu-item',
  'a-tabs', 'a-tab-pane', 'a-modal', 'a-collapse', 'a-collapse-panel',
  'a-popconfirm', 'a-tooltip', 'a-input-number', 'a-input-password',
  'a-table', 'a-tag', 'a-form', 'a-form-item', 'a-skeleton', 'a-result', 'router-view',
  'PageState', 'AccountScreen', 'JobProgress', 'ProbeTable',
].map((name) => [name, true]))
const wrappers: VueWrapper[] = []
const waInvoke = vi.fn()

function account(id: string, channel: Account['channel'] = 'qidian'): Account {
  return {
    id, channel, host: channel === 'wechat' ? 'windows' : 'wsl', label: `测试-${id}`,
    state: 'running', state_code: '', state_reason: '', error_since_ms: null,
    enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 512,
    self_uid: `identity-${id}`, self_nick: `昵称-${id}`,
  }
}

function msg(id: string, overrides: Partial<Message> = {}): Message {
  return {
    id, account_id: 'qa01', channel: 'qidian', session: { id: 'session-a', name: '测试会话', kind: 'private' },
    dir: 'in', type: 'text', state: 'DELIVERED', text: '查询关键字', text_len: 5,
    media: [], sender: { id: 'sender-a', name: '测试发送者' }, self: false,
    ts: '2026-10-08T09:00:00Z', received_at: '2026-10-08T09:00:00Z', source: 'test', revoked: false,
    ...overrides,
  }
}

function audit(id: number): AuditRow {
  return { id, ts_ms: 1791446400000, kind: 'system', account_id: 'qa01', action: 'state_changed', result_code: 'OK' }
}

function mountPage(component: Component): VueWrapper {
  const wrapper = shallowMount(component, { global: { stubs: STUBS, renderStubDefaultSlot: true } })
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  routing.route = reactive({ path: '/dash', query: {}, params: {} })
  routing.push.mockClear()
  routing.replace.mockClear()
  waInvoke.mockReset().mockResolvedValue({})
  vi.stubGlobal('qt', { wa: { invoke: waInvoke }, router: { onRoute: vi.fn(() => vi.fn()) }, tray: { update: vi.fn() }, notify: vi.fn() })
  for (const key of ['success', 'info', 'warning', 'error'] as const) vi.spyOn(message, key).mockReturnValue((() => undefined) as never)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network request in isolated UI test')))
  vi.spyOn(messagesApi, 'list').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(messagesApi, 'sessions').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(auditApi, 'list').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(accountsApi, 'get').mockImplementation(async (id) => account(id))
  vi.spyOn(accountsApi, 'capabilities').mockResolvedValue({ capabilities: [], matrix: {} })
  vi.spyOn(systemApi, 'health').mockResolvedValue({ ok: true } as Awaited<ReturnType<typeof systemApi.health>>)
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  for (const key of ['loadAll', 'loadPublicEndpoint', 'loadSelftest', 'loadObserved'] as const) {
    vi.spyOn(useEnvStore(), key).mockResolvedValue()
  }
  vi.spyOn(useResourcesStore(), 'load').mockResolvedValue()
  vi.spyOn(useResourcesStore(), 'loadMetrics').mockResolvedValue()
  vi.spyOn(useMailStore(), 'reloadAll').mockResolvedValue()
  vi.spyOn(useMailStore(), 'stopPolling').mockImplementation(() => undefined)
  vi.spyOn(useCommandsStore(), 'loadCatalog').mockResolvedValue()
  vi.spyOn(useSessionStore(), 'install').mockImplementation(() => undefined)
  vi.spyOn(useSessionStore(), 'uninstall').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'start').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'stop').mockImplementation(() => undefined)
  vi.spyOn(useSetupStore(), 'loadConfig').mockResolvedValue()
  vi.spyOn(useUiStore(), 'loadFromConfig').mockResolvedValue()
  vi.spyOn(useUiStore(), 'applyTheme').mockImplementation(() => undefined)
})

afterEach(() => {
  for (const wrapper of wrappers.splice(0).reverse()) wrapper.unmount()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('简洁导航与已移除的调试入口', () => {
  it.each(['/cmd', '/flow'])('%s 旧深链只重定向，不再提供调试页组件', (path) => {
    const route = router.getRoutes().find((row) => row.path === path)
    expect(route?.redirect).toBeTruthy()
    expect(route?.components?.default).toBeFalsy()
  })

  it('主导航只有首页、消息、日志、资源、环境，调试入口不再藏在更多工具', async () => {
    const page = mountPage(App)
    await flushPromises()
    const nav = page.findAll('a[data-testid^="qt-shell-nav-"]')
    const primary = nav.filter((item) => ['dash', 'msg', 'log', 'res', 'env'].some((seg) => item.attributes('href') === `#/${seg}`))
    expect(primary.map((item) => item.attributes('href')).sort()).toEqual(['#/dash', '#/env', '#/log', '#/msg', '#/res'])
    expect(page.find('a[href="#/cmd"]').exists()).toBe(false)
    expect(page.find('a[href="#/flow"]').exists()).toBe(false)
    expect(page.text()).not.toContain('更多工具')
  })
})

describe('首页以账号为主体且不虚构监控数字', () => {
  it('五项摘要按资源、资源、资源、消息、日志顺序提供入口', async () => {
    const page = mountPage(DashPage)
    await flushPromises()
    const metrics = page.get('[aria-label="运行摘要"]').findAll('.metric')
    expect(metrics).toHaveLength(5)
    expect(metrics.map((metric) => (metric.element.tagName === 'A' ? metric : metric.get('a')).attributes('href')?.split('?')[0])).toEqual(['#/res', '#/res', '#/res', '#/msg', '#/log'])
  })

  it('未知宿主总内存、客户端目录和 CPU 不冒充实测零', async () => {
    const page = mountPage(DashPage)
    await flushPromises()
    const links = page.get('[aria-label="运行摘要"]').findAll('.metric').slice(0, 3)
    for (const link of links) {
      expect(link.get('strong').text()).toMatch(/—|未知|未采集/)
      expect(link.get('strong').text()).not.toMatch(/0(?:\.0+)?\s*(?:GB|%)/)
    }
  })

  it('实测零磁盘与 CPU 保留 0，总内存使用独立宿主容量', async () => {
    vi.stubGlobal('qt', { app: { localMetrics: vi.fn().mockResolvedValue({
      memory: { available: true, totalBytes: 8 * 1024 ** 3, usedBytes: 0, freeBytes: 8 * 1024 ** 3, source: 'electron_host', sampledAt: '2026-10-08T12:00:00Z' },
      storage: { available: true, bytes: 0, sampledAt: '2026-10-08T12:00:00Z', scope: 'client_directory', measurement: 'file_sizes', excludedLinks: 0 },
    }) } })
    useResourcesStore().metrics = {
      hardware: { mem: { used_mb: 0, total_mb: 8192, avail_mb: 8192, vmmem_mb: 0 }, cpu: { load_pct: 0, logical_cores: 4 }, disks: [] },
      ours: { procs: { agent_mb: null, winagent_mb: null, console_mb: null }, accounts: [] }, budget_vs_actual: [],
      mem_watermark: { level: 'normal', avail_mb: 8192, lru_suggest: [] },
      disk_watermark: { level: 'unknown', free_mb: null, actions: [], last_cleanup_at: null, last_cleanup_freed_mb: null },
    } satisfies MetricsSnapshot
    const page = mountPage(DashPage)
    await flushPromises()
    const links = page.get('[aria-label="运行摘要"]').findAll('.metric')
    expect(links[0].get('strong').text()).toMatch(/8(?:\.0+)?\s*GB/)
    expect(links[1].get('strong').text()).toMatch(/0(?:\.0+)?\s*(?:B|GB)/)
    expect(links[2].get('strong').text()).toMatch(/0(?:\.0+)?\s*%/)
  })

  it('接口返回一页消息不能被显示为累计收发总数', async () => {
    vi.mocked(messagesApi.list).mockResolvedValue({ items: [msg('fixture-one')], nextCursor: 'more-records' })
    const page = mountPage(DashPage)
    await flushPromises()
    const summary = page.get('[aria-label="运行摘要"]').findAll('a').find((link) => link.attributes('href') === '#/msg')!
    expect(summary.get('strong').text()).not.toMatch(/^1\s*条$/)
    expect(summary.text()).toMatch(/尚未提供|未知|已加载|当前/)
  })

  it.each(['qidian', 'wechat', 'qq'] as const)('%s 账号卡展示身份并按内部账号 id 进入详情', async (channel) => {
    const fixture = account(`fixture-${channel}`, channel)
    useAccountsStore().items = [fixture]
    const page = mountPage(DashPage)
    await flushPromises()
    const link = page.get(`a[href="#/acct/${fixture.id}"]`)
    expect(link.text()).toContain(fixture.self_nick)
    expect(link.text()).toContain(fixture.self_uid)
    expect(link.text()).not.toContain('已用 0.00 GB')
  })
})

describe('消息查询与账号上下文', () => {
  it('账号详情进入消息页时，首个查询与会话列表就限定 route account_id', async () => {
    routing.route.path = '/msg'
    routing.route.query = { account_id: 'qa02' }
    useAccountsStore().items = [account('qa01'), account('qa02')]
    mountPage(MsgPage)
    await flushPromises()
    expect(messagesApi.list).toHaveBeenCalledWith(expect.objectContaining({ account_id: 'qa02' }))
    expect(messagesApi.sessions).toHaveBeenCalledWith(expect.objectContaining({ account_id: 'qa02' }))
  })

  it('查询、加载更多、重新查询保留筛选条件并正确使用或清除游标', async () => {
    const store = useMessagesStore()
    store.filter = { account_id: 'qa01', dir: 'in', type: 'text', q: '关键字', since: '2026-10-01T00:00:00Z', until: '2026-10-09T00:00:00Z', limit: 50 }
    vi.mocked(messagesApi.list).mockResolvedValueOnce({ items: [msg('m1')], nextCursor: 'opaque-page-2' })
    const page = mountPage(MsgPage)
    await flushPromises()
    const more = page.findAll('a-button-stub,button').find((button) => /加载更多/.test(button.text()))
    expect(more, '消息页应提供消息列表的续页入口').toBeDefined()
    vi.mocked(messagesApi.list).mockResolvedValueOnce({ items: [msg('m2')], nextCursor: null })
    await more!.trigger('click')
    await flushPromises()
    expect(messagesApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ ...store.filter, cursor: 'opaque-page-2' }))
    expect(store.items.map((row) => row.id)).toEqual(['m1', 'm2'])
    vi.mocked(messagesApi.list).mockResolvedValueOnce({ items: [msg('m3')], nextCursor: null })
    await page.get(`[data-testid="${MSG.search}"]`).trigger('click')
    await flushPromises()
    expect(vi.mocked(messagesApi.list).mock.calls.at(-1)?.[0].cursor).toBeUndefined()
    expect(store.items.map((row) => row.id)).toEqual(['m3'])
  })

  it.each([
    { filter: { q: '关键字' }, incoming: { text: '完全无关的内容' } },
    { filter: { since: '2026-10-08T00:00:00Z' }, incoming: { ts: '2026-10-01T09:00:00Z' } },
    { filter: { until: '2026-10-08T10:00:00Z' }, incoming: { ts: '2026-10-09T09:00:00Z' } },
  ])('实时消息不得污染当前关键字或时间查询 $filter', ({ filter, incoming }) => {
    const store = useMessagesStore()
    store.filter = filter
    store.items = [msg('matched')]
    store.applyMessageEvent({ event: 'message', seq: 1, ts: '2026-10-08T09:00:00Z', payload: msg('unmatched', incoming) })
    expect(store.items.map((row) => row.id)).toEqual(['matched'])
  })

  it('读取失败保留错误信息，不能呈现为查询无结果', async () => {
    vi.mocked(messagesApi.list).mockRejectedValue(new Error('fixture query failure'))
    const store = useMessagesStore()
    await store.search()
    expect(store.error).toContain('fixture query failure')
    expect(store.loading).toBe(false)
  })
})

describe('日志查询与账号上下文', () => {
  it('首页告警入口打开告警列表，并明示只覆盖本次连接收到的记录', async () => {
    routing.route.path = '/log'
    routing.route.query = { tab: 'alerts' }
    const page = mountPage(LogPage)
    await flushPromises()
    expect(page.get('[role="tab"][aria-selected="true"]').text()).toContain('告警')
    expect(page.text()).toContain('本次连接')
    expect(page.text()).toContain('不是完整的近 7 天历史')
  })

  it('日志深链中的账号和 system 类别用于首个后台查询', async () => {
    routing.route.path = '/log'
    routing.route.query = { account_id: 'qa02', kind: 'system' }
    mountPage(LogPage)
    await flushPromises()
    expect(auditApi.list).toHaveBeenCalledWith(expect.objectContaining({ account_id: 'qa02', kind: 'system' }))
  })

  it('日志续页保留账号和时间范围，重新查询重置游标及旧行', async () => {
    const store = useAuditStore()
    store.kind = 'system'
    store.filter = { account_id: 'qa01', since: '2026-10-01T00:00:00Z', until: '2026-10-09T00:00:00Z' }
    vi.mocked(auditApi.list).mockResolvedValueOnce({ items: [audit(1)], nextCursor: 'audit-page-2' })
    const page = mountPage(LogPage)
    await flushPromises()
    vi.mocked(auditApi.list).mockResolvedValueOnce({ items: [audit(2)], nextCursor: null })
    await page.get(`[data-testid="${LOG.loadMore}"]`).trigger('click')
    await flushPromises()
    expect(auditApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ ...store.filter, cursor: 'audit-page-2' }))
    expect(store.rows.map((row) => row.id)).toEqual([1, 2])
    vi.mocked(auditApi.list).mockResolvedValueOnce({ items: [audit(3)], nextCursor: null })
    await page.get(`[data-testid="${LOG.search}"]`).trigger('click')
    await flushPromises()
    expect(vi.mocked(auditApi.list).mock.calls.at(-1)?.[0]?.cursor).toBeUndefined()
    expect(store.rows.map((row) => row.id)).toEqual([3])
  })
})

describe('时间查询不受浏览器与服务端时区差异影响', () => {
  it.each(['msg', 'log'] as const)('%s 本地时间输入转换为带时区的查询，回显和清空保持一致', async (kind) => {
    routing.route.path = `/${kind}`
    const isMessage = kind === 'msg'
    const store = isMessage ? useMessagesStore() : useAuditStore()
    const ids = isMessage ? MSG : LOG
    const expectedSince = new Date(2026, 9, 8, 15, 0).toISOString()
    const expectedUntil = new Date(2026, 9, 8, 16, 30).toISOString()
    const exportMessages = vi.spyOn(messagesApi, 'export').mockResolvedValue({ job_id: 'fixture-export' })
    store.filter = { since: expectedSince, until: expectedUntil }
    const page = mountPage(isMessage ? MsgPage : LogPage)
    await flushPromises()
    await page.get('button.time-toggle').trigger('click')
    const since = page.getComponent(`[data-testid="${ids.filter('since')}"]`) as VueWrapper
    const until = page.getComponent(`[data-testid="${ids.filter('until')}"]`) as VueWrapper
    expect(since.attributes('value')).toBe('2026-10-08T15:00')
    expect(until.attributes('value')).toBe('2026-10-08T16:30')
    since.vm.$emit('update:value', '2026-10-08T15:00')
    until.vm.$emit('update:value', '2026-10-08T16:30')
    await page.get(`[data-testid="${ids.search}"]`).trigger('click')
    await flushPromises()
    const queryApi = isMessage ? messagesApi.list : auditApi.list
    expect(queryApi).toHaveBeenLastCalledWith(expect.objectContaining({ since: expectedSince, until: expectedUntil }))
    expect(store.filter.since).toMatch(/Z$/)
    expect(store.filter.until).toMatch(/Z$/)
    if (process.env.TZ === 'Asia/Shanghai') {
      expect(store.filter.since).toBe('2026-10-08T07:00:00.000Z')
      expect(store.filter.until).toBe('2026-10-08T08:30:00.000Z')
    }
    if (isMessage) {
      await page.get(`[data-testid="${MSG.export}"]`).trigger('click')
      await flushPromises()
      expect(exportMessages).toHaveBeenCalledWith(expect.objectContaining({ filter: expect.objectContaining({ since: expectedSince, until: expectedUntil }) }))
    }
    since.vm.$emit('update:value', '')
    until.vm.$emit('update:value', '')
    await page.get(`[data-testid="${ids.search}"]`).trigger('click')
    await flushPromises()
    expect(store.filter.since).toBeUndefined()
    expect(store.filter.until).toBeUndefined()
    expect(vi.mocked(queryApi).mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ since: undefined, until: undefined }))
  })
})

describe('跨页账号详情', () => {
  it('账号不在当前列表页时仍按路由 id 单独加载，并展示真实身份', async () => {
    routing.route.path = '/acct/qa99'
    routing.route.params = { id: 'qa99' }
    useAccountsStore().items = [account('qa01')]
    const page = mountPage(AcctDetailPage)
    await flushPromises()
    expect(accountsApi.get).toHaveBeenCalledWith('qa99')
    expect(useAccountsStore().byId.qa99).toBeTruthy()
    expect(page.text()).toContain('昵称-qa99')
    expect(page.text()).not.toContain('账号不存在')
  })
})

describe('环境破坏性动作必须经过确认', () => {
  it('取消修复网络访问，不修改防火墙规则', async () => {
    const confirmation = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const page = mountPage(EnvPage)
    await flushPromises()
    waInvoke.mockClear()
    await page.get(`[data-testid="${ENV.firewallFix}"]`).trigger('click')
    expect(confirmation).toHaveBeenCalledOnce()
    const config = confirmation.mock.calls[0][0]
    expect(String(config.content)).toContain('防火墙')
    if (typeof config.onCancel === 'function') await config.onCancel()
    expect(waInvoke).not.toHaveBeenCalled()
  })

  it.each([
    [ENV.kernelRollback, 'wsl.kernel.rollback'],
    [ENV.wslRestart, 'wsl.restart'],
  ])('%s 打开确认并取消，不得发出破坏请求', async (testid, operation) => {
    const confirmation = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const restart = vi.spyOn(systemApi, 'wslRestart').mockResolvedValue({ ok: true })
    useAccountsStore().items = [account('qa01')]
    const page = mountPage(EnvPage)
    await flushPromises()
    waInvoke.mockClear()
    await page.get(`[data-testid="${testid}"]`).trigger('click')
    expect(confirmation).toHaveBeenCalledOnce()
    const config = confirmation.mock.calls[0][0]
    expect(String(config.content)).toContain('qa01')
    expect(String(config.content)).toMatch(/所有 WSL|其它发行版/)
    expect(waInvoke).not.toHaveBeenCalledWith(operation, expect.anything())
    expect(restart).not.toHaveBeenCalled()
    if (typeof config.onCancel === 'function') await config.onCancel()
    await flushPromises()
    expect(waInvoke).not.toHaveBeenCalled()
    expect(restart).not.toHaveBeenCalled()
  })

  it('确认内核回滚仅向假后端提交明确 shutdown 确认', async () => {
    const confirmation = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const page = mountPage(EnvPage)
    await flushPromises()
    waInvoke.mockClear()
    await page.get(`[data-testid="${ENV.kernelRollback}"]`).trigger('click')
    expect(waInvoke).not.toHaveBeenCalled()
    const onOk = confirmation.mock.calls[0][0].onOk
    expect(onOk).toBeTypeOf('function')
    await onOk!()
    expect(waInvoke).toHaveBeenCalledTimes(1)
    expect(waInvoke).toHaveBeenCalledWith('wsl.kernel.rollback', { confirm_shutdown: true })
  })
})
