/** 登录尝试隔离与完成身份：独立回归，所有 HTTP/桌面能力均为本地假件。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AcctNewPage from '@/pages/acct/AcctNewPage.vue'
import { accountsApi } from '@/api/client'
import type { Account, AccountStatePayload, Prompt, QtEvent } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { acctNew } from '@/testids'

const route = vi.hoisted(() => ({ path: '/acct/new', query: { ch: 'qq' }, params: {} }))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => route,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } }))

const ID = 'qq08'
let wrapper: VueWrapper | undefined
function account(state: Account['state'] = 'login_required'): Account {
  return { id: ID, channel: 'qq', host: 'wsl', label: 'Fixture QQ', state,
    state_code: state === 'login_required' ? 'WAIT_QRCODE' : '', state_reason: '', error_since_ms: null,
    enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 512,
    self_nick: 'Current nickname' }
}
function prompt(value: string): Prompt { return { kind: 'qrcode', qrcode_png_b64: value, text: value } as Prompt }
function frame(seq: number, session: string | null, state: Account['state'] = 'login_required',
  extra: Record<string, unknown> = {}): QtEvent<AccountStatePayload> {
  return { event: 'account_state', account_id: ID, channel: 'qq', seq, ts: '2026-10-08T20:00:00+08:00',
    payload: { state, state_code: state === 'login_required' ? 'WAIT_QRCODE' : '', state_reason: '',
      error_since_ms: null, enabled: true, runtime: {}, capabilities: [], login_session_id: session,
      ...extra } as AccountStatePayload }
}

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear()
  setActivePinia(createPinia())
  useAccountsStore().upsert(account())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network')))
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('共享账号 store 的登录尝试边界', () => {
  it.each(['login_required', 'running'] as const)('ls2 之后迟到 ls1 的 %s 整帧不覆盖新状态与身份', (state) => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls1', 'login_required', { prompt: prompt('OLD') }))
    store.applyAccountState(frame(2, 'ls2', 'login_required', { prompt: prompt('NEW'), self_nick: 'Current nickname' }))
    store.applyAccountState(frame(3, 'ls1', state, { prompt: prompt('OLD'), self_uid: '99999999', self_nick: 'Old nickname' }))
    expect(store.loginSessions[ID]).toBe('ls2')
    expect(store.prompts[ID].qrcode_png_b64).toBe('NEW')
    expect(store.byId[ID].state).toBe('login_required')
    expect(store.byId[ID].self_nick).toBe('Current nickname')
    expect(store.byId[ID].self_uid).not.toBe('99999999')
  })

  it('同一个有效尝试的新提示正常更新', () => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls2', 'login_required', { prompt: prompt('FIRST') }))
    store.applyAccountState(frame(2, 'ls2', 'login_required', { prompt: prompt('UPDATED') }))
    expect(store.loginSessions[ID]).toBe('ls2')
    expect(store.prompts[ID].qrcode_png_b64).toBe('UPDATED')
  })

  it('未退休的 ls3 正常接替 ls2，不把新登录一概当旧帧拒绝', () => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls2', 'login_required', { prompt: prompt('SECOND') }))
    store.applyAccountState(frame(2, 'ls3', 'login_required', { prompt: prompt('THIRD') }))
    expect(store.loginSessions[ID]).toBe('ls3')
    expect(store.prompts[ID].qrcode_png_b64).toBe('THIRD')
  })

  it.each(['running', 'stopped'] as const)('%s 终态使旧 ls1 失效，后续旧帧不能恢复登录提示', (state) => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls1', 'login_required', { prompt: prompt('OLD') }))
    store.applyAccountState(frame(2, null, state))
    store.applyAccountState(frame(3, 'ls1', 'login_required', { prompt: prompt('LATE') }))
    expect(store.byId[ID].state).toBe(state)
    expect(store.loginSessions[ID]).toBeNull()
    expect(store.prompts[ID]).toBeUndefined()
  })

  it('首次出现即为 running 的 session 也会退休，但仍允许不同的新尝试', () => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'finished-first', 'running', { self_uid: '91000001' }))
    store.applyAccountState(frame(2, 'finished-first', 'login_required', { prompt: prompt('RETIRED') }))
    expect(store.byId[ID].state).toBe('running')
    expect(store.loginSessions[ID]).toBeNull()
    expect(store.prompts[ID]).toBeUndefined()
    store.applyAccountState(frame(3, 'fresh-session', 'login_required', { prompt: prompt('FRESH') }))
    expect(store.loginSessions[ID]).toBe('fresh-session')
    expect(store.prompts[ID].qrcode_png_b64).toBe('FRESH')
  })

  it('ls1 的 #15 响应迟于 ls2 返回时不覆盖新 PNG', async () => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls1', 'login_required', { prompt: prompt('OLD') }))
    let resolve!: (value: Prompt) => void
    const get = vi.spyOn(accountsApi, 'prompt').mockImplementation(() => new Promise<Prompt>(done => { resolve = done }))
    const request = store.refreshPrompt(ID)
    expect(get).toHaveBeenCalledWith(ID, 'ls1')
    store.applyAccountState(frame(2, 'ls2', 'login_required', { prompt: prompt('NEW') }))
    resolve(prompt('LATE_HTTP'))
    await request
    expect(store.loginSessions[ID]).toBe('ls2')
    expect(store.prompts[ID].qrcode_png_b64).toBe('NEW')
  })

  it('同账号两次 #15 后发先回时，旧请求不得盖掉最新响应', async () => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls1'))
    let first!: (value: Prompt) => void
    let second!: (value: Prompt) => void
    vi.spyOn(accountsApi, 'prompt')
      .mockImplementationOnce(() => new Promise<Prompt>(done => { first = done }))
      .mockImplementationOnce(() => new Promise<Prompt>(done => { second = done }))
    const older = store.refreshPrompt(ID)
    const newer = store.refreshPrompt(ID)
    second(prompt('LATEST_HTTP'))
    await newer
    first(prompt('OLDER_HTTP'))
    await older
    expect(store.prompts[ID].qrcode_png_b64).toBe('LATEST_HTTP')
  })

  it('running 事件携带的真实 QQ 身份和昵称进入账号视图', () => {
    const store = useAccountsStore()
    store.applyAccountState(frame(1, 'ls1'))
    store.applyAccountState(frame(2, null, 'running', { self_uid: '91000001', self_nick: 'Actual QQ nickname' }))
    expect(store.byId[ID].self_uid).toBe('91000001')
    expect(store.byId[ID].self_nick).toBe('Actual QQ nickname')
  })
})

it('QQ 完成摘要同时展示真实 QQ 号与昵称，内部账号 ID 不能替代 QQ 号', async () => {
  const actual = { ...account('running'), self_uid: '91000001', self_nick: 'Actual QQ nickname' }
  useAccountsStore().items = []
  sessionStorage.setItem('qtrade.acct-new.pending', JSON.stringify({ createdId: ID, channel: 'qq' }))
  vi.spyOn(accountsApi, 'get').mockResolvedValue(actual)
  vi.spyOn(accountsApi, 'prompt').mockResolvedValue({} as Prompt)
  vi.spyOn(useResourcesStore(), 'load').mockResolvedValue()
  wrapper = shallowMount(AcctNewPage, { global: { renderStubDefaultSlot: true, stubs: {
    ...Object.fromEntries(['a-steps', 'a-step', 'a-radio', 'a-radio-group', 'a-select', 'a-progress',
      'a-skeleton', 'a-result', 'a-modal', 'a-tooltip', 'a-input', 'a-checkbox', 'a-popconfirm',
      'StateDot', 'QtIcon', 'PromptCard', 'AccountScreen'].map(name => [name, true])),
    'a-button': { template: '<button><slot /></button>' },
  } } })
  await flushPromises()
  const summary = wrapper.get(`[data-testid="${acctNew.doneSummary}"]`)
  expect(summary.text()).toContain('91000001')
  expect(summary.text()).toContain('Actual QQ nickname')
})
