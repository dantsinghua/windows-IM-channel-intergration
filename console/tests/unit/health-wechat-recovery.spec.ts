/** 已批准的告警回放与微信失败闭环；只操作假 API 和 Vue DOM。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import App from '@/App.vue'
import AcctNewPage from '@/pages/acct/AcctNewPage.vue'
import { accountsApi, resourcesApi, commandsApi } from '@/api/client'
import { EventsClient, type WebSocketLike } from '@/api/ws'
import type { Account, QtEvent, ResourcePool } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore } from '@/stores/events'
import { useResourcesStore } from '@/stores/resources'
import { useMailStore } from '@/stores/mail'
import { useEnvStore } from '@/stores/env'
import { useSessionStore } from '@/stores/session'
import { useSetupStore } from '@/stores/setup'
import { useUiStore } from '@/stores/ui'
import { acctNew as T } from '@/testids'

const route = vi.hoisted(() => ({ path: '/dash', query: {} as Record<string, string> }))
const notices = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }))
vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
vi.mock('@/router', () => ({ installGuards: vi.fn() }))
vi.mock('ant-design-vue', () => ({ message: notices }))

const stubs = {
  ...Object.fromEntries(['a-config-provider', 'a-app', 'a-badge', 'a-switch', 'a-drawer', 'a-empty', 'a-progress',
    'router-view', 'a-steps', 'a-step', 'a-radio-group', 'a-radio', 'a-select', 'a-checkbox', 'a-modal', 'a-alert', 'a-input', 'a-popconfirm'].map(n => [n, true])),
  'a-button': { props: ['disabled', 'loading'], template: '<button :disabled="disabled || loading"><slot /></button>' },
}
let wrapper: VueWrapper | undefined
const invoke = vi.fn()
const notify = vi.fn()
const pool: ResourcePool = {
  pools: { wsl: { total_mb: 8192, reserved_mb: 0, used_mb: 0, free_mb: 8192 },
    windows: { total_mb: 8192, reserved_mb: 0, wechat_mb: 0,
      wechat_slots: { used: 0, max: 1, holder: '', pending: '', pending_expires_at: null, pending_login_session_id: '' } } },
  realtime: { wsl_anon_mb: 0, win_available_mb: 8192 }, quota_mb: { qidian: 2048, qq: 512, wechat: 512 },
  can_add: { qidian: 4, qq: 16, wechat: 1 },
}
function account(state: Account['state'] = 'starting', reason = ''): Account {
  return { id: 'wx01', channel: 'wechat', host: 'windows', label: '测试微信', state, state_code: '', state_reason: reason,
    error_since_ms: null, enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 512 }
}
beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  window.sessionStorage.clear()
  route.path = '/dash'; route.query = {}
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No network allowed')))
  vi.stubGlobal('qt', { wa: { invoke }, notify, router: { onRoute: vi.fn(() => vi.fn()) }, tray: { update: vi.fn() } })
  invoke.mockImplementation(async (method: string) => method === 'wechat.version-match'
    ? { match: 'SUPPORTED', current_version: '4.1.12.26', bundled_version: '4.1.12.26' }
    : { enabled: true, wechat: { installed: true, version: '4.1.12.26', running: true, logged_in: true },
      login_session: { phase: 'idle' } })
  vi.spyOn(resourcesApi, 'get').mockResolvedValue(pool)
  vi.spyOn(commandsApi, 'deviceProfiles').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(useAccountsStore(), 'load').mockImplementation(async () => { useAccountsStore().upsert(account()) })
  vi.spyOn(accountsApi, 'switchNew').mockResolvedValue({ target: 'wx01', login_session_id: 'round-1' } as never)
  vi.spyOn(accountsApi, 'start').mockResolvedValue({ state: 'starting' })
  vi.spyOn(accountsApi, 'create').mockResolvedValue(account())
  for (const [object, key] of [[useResourcesStore(), 'loadMetrics'], [useMailStore(), 'reloadAll'], [useEnvStore(), 'loadAll'],
    [useSetupStore(), 'loadConfig'], [useUiStore(), 'loadFromConfig']] as const) vi.spyOn(object, key as never).mockResolvedValue(undefined as never)
  vi.spyOn(useSessionStore(), 'install').mockImplementation(() => undefined)
  vi.spyOn(useSessionStore(), 'uninstall').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'start').mockImplementation(() => undefined)
  vi.spyOn(useEventsStore(), 'stop').mockImplementation(() => undefined)
  vi.spyOn(useUiStore(), 'applyTheme').mockImplementation(() => undefined)
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('告警通知只面向实时故障', () => {
  it.each([
    [true, 'firing', 0], [false, 'resolved', 0], [undefined, 'firing', 0], [false, 'firing', 1],
  ])('replayed=%s state=%s notification=%s', async (replayed, state, count) => {
    useUiStore().notifyEnabled = true
    wrapper = shallowMount(App, { global: { stubs, renderStubDefaultSlot: true } })
    await flushPromises()
    useEventsStore().injectForTest({ event: 'alert', seq: 1, ts: '2026-10-13T22:00:00Z', replayed,
      payload: { code: 'H02_WINAGENT_API_DOWN', severity: 'crit', state, subject: 'host', count: 1 } } as QtEvent)
    await flushPromises()
    expect(notify).toHaveBeenCalledTimes(count)
  })
})

describe('微信向导真实失败终态', () => {
  async function start(): Promise<void> {
    route.path = '/acct/new'; route.query = { ch: 'wechat' }
    wrapper = shallowMount(AcctNewPage, { global: { stubs } })
    await flushPromises()
    for (let i = 0; i < 2; i++) { await wrapper.get(`[data-testid="${T.next}"]`).trigger('click'); await flushPromises() }
    await wrapper.get(`[data-testid="${T.wxSwitchConfirm}"]`).trigger('click')
    await flushPromises()
  }
  it('缓存 logged_in 不能把未启动的会话显示成正在起 hook', async () => {
    await start()
    expect(wrapper!.find(`[data-testid="${T.wxKeyProgress}"]`).exists()).toBe(false)
    expect(wrapper!.find(`[data-testid="${T.doneSummary}"]`).exists()).toBe(false)
  })
  it.each(['stopped', 'error'] as const)('%s 显示后台失败原因且重试原账号', async (state) => {
    await start()
    useAccountsStore().upsert(account(state, 'chatlog.exe missing in deployment'))
    await flushPromises()
    expect(wrapper!.text()).toContain('chatlog.exe missing in deployment')
    expect(wrapper!.find(`[data-testid="${T.wxKeyProgress}"]`).exists()).toBe(false)
    await wrapper!.get(`[data-testid="${T.wxKeyRetry}"]`).trigger('click')
    await flushPromises()
    expect(accountsApi.start).toHaveBeenCalledWith('wx01')
    expect(accountsApi.switchNew).toHaveBeenCalledTimes(1)
    expect(accountsApi.create).not.toHaveBeenCalled()
    expect(invoke.mock.calls.every(([method]) => method !== 'wechat.key.retry')).toBe(true)
  })
})

class FakeWs implements WebSocketLike {
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  send(): void {}
  close(): void {}
  recv(value: unknown): void { this.onmessage?.({ data: JSON.stringify(value) }) }
}
it('截断后的第一条事件必须被接收，重复 seq 仍丢弃', () => {
  const socket = new FakeWs()
  const events: QtEvent[] = []
  const client = new EventsClient({ onEvent: event => events.push(event), onStatus: () => undefined, onReplayTruncated: () => undefined },
    { factory: () => socket, setTimeoutFn: () => 1, clearTimeoutFn: () => undefined })
  client.connect(); socket.onopen?.()
  socket.recv({ replay: 'truncated', from_seq: 900 })
  socket.recv({ event: 'alert', seq: 900, ts: 'x', payload: { code: 'H02_WINAGENT_API_DOWN' }, replayed: true })
  socket.recv({ event: 'alert', seq: 900, ts: 'x', payload: {}, replayed: true })
  socket.recv({ event: 'alert', seq: 901, ts: 'x', payload: {}, replayed: false })
  expect(events.map(event => event.seq)).toEqual([900, 901])
  client.close()
})
