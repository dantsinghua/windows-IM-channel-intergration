/**
 * P-ACCT-NEW 生命周期回归（01 §2.7.3.1 / §2.7.3.2）。
 * 只通过表单、按钮和 account_state 事件推进；不写组件内部 step。
 * 所有 API 均为桩，不连接真实 Agent、Docker、ADB 或工作账号。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AcctNewPage from '@/pages/acct/AcctNewPage.vue'
import { accountsApi, commandsApi, resourcesApi } from '@/api/client'
import type { Account, AccountStatePayload, ResourcePool } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { acctNew as T } from '@/testids'

type Channel = 'qidian' | 'qq'
const channels: Channel[] = ['qidian', 'qq']
const routeState = vi.hoisted(() => ({ query: {} as Record<string, string> }))
const routerSpy = vi.hoisted(() => ({ push: vi.fn() }))
const notices = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }))
vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRoute: () => routeState,
  useRouter: () => routerSpy,
}))
vi.mock('ant-design-vue', () => ({ message: notices }))

// 原生 button 保留真实 disabled 行为，避免通用 true 桩让禁用按钮仍能触发点击。
const STUBS = {
  'a-button': {
    props: ['disabled', 'loading'],
    template: '<button :disabled="disabled || loading"><slot /></button>',
  },
  'a-input': {
    props: ['value'],
    emits: ['update:value'],
    template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
  },
  'a-progress': {
    props: ['percent'],
    template: '<div role="progressbar" :aria-valuenow="percent" />',
  },
  'a-steps': true,
  'a-step': true,
  'a-radio-group': true,
  'a-radio': true,
  'a-select': true,
  'a-checkbox': true,
  'a-modal': true,
  'a-alert': true,
}

function account(channel: Channel, state: Account['state'] = 'created'): Account {
  return {
    id: channel === 'qidian' ? 'qd-test' : 'qq-test', channel, host: 'wsl',
    label: '回归测试', state, state_code: '', state_reason: '', error_since_ms: null,
    enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 512,
  }
}

function resourcePool(): ResourcePool {
  return {
    pools: {
      wsl: { total_mb: 8192, reserved_mb: 0, used_mb: 0, free_mb: 8192 },
      windows: {
        total_mb: 8192, reserved_mb: 0, wechat_mb: 0,
        wechat_slots: { used: 0, max: 1, holder: '', pending: '', pending_expires_at: null, pending_login_session_id: '' },
      },
    },
    realtime: { wsl_anon_mb: 0, win_available_mb: 8192 },
    quota_mb: { qidian: 2048, qq: 512, wechat: 512 },
    can_add: { qidian: 4, qq: 16, wechat: 1 },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

let wrapper: VueWrapper | null = null
const find = (id: string) => wrapper!.find(`[data-testid="${id}"]`)

async function clickNextIfAvailable(): Promise<void> {
  const next = find(T.next)
  if (next.exists()) await next.trigger('click')
  await flushPromises()
}

async function reachCreateForm(channel: Channel): Promise<void> {
  routeState.query = { ch: channel }
  wrapper = shallowMount(AcctNewPage, { global: { stubs: STUBS } })
  await flushPromises()
  await clickNextIfAvailable()
  await find(T.label).setValue('回归测试')
  if (channel === 'qidian') {
    await clickNextIfAvailable()
    await clickNextIfAvailable()
    await find(T.account).setValue('9000000009')
    await find(T.secret).setValue('test-only-placeholder')
  }
  expect(find(T.create).exists()).toBe(true)
}

async function submitCreate(channel: Channel): Promise<void> {
  await reachCreateForm(channel)
  await find(T.create).trigger('click')
  await flushPromises()
  expect(accountsApi.create).toHaveBeenCalledTimes(1)
}

async function stateEvent(channel: Channel, state: Account['state'], patch: Partial<AccountStatePayload> = {}): Promise<void> {
  const payload: AccountStatePayload = {
    state, state_code: '', state_reason: '', error_since_ms: null, enabled: true,
    runtime: {}, capabilities: [], ...patch,
  }
  useAccountsStore().applyAccountState({
    event: 'account_state', seq: 1, ts: '2026-10-08T00:00:00Z',
    account_id: account(channel).id, channel, payload,
  })
  await flushPromises()
}

beforeEach(() => {
  // 每个挂载用例都是新浏览器上下文；刷新恢复的跨挂载场景另有独立验收。
  window.sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.spyOn(resourcesApi, 'get').mockResolvedValue(resourcePool())
  vi.spyOn(commandsApi, 'deviceProfiles').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(accountsApi, 'create').mockImplementation(async (body) => account(body.channel as Channel))
  vi.spyOn(accountsApi, 'start').mockResolvedValue({ state: 'starting' })
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network call in isolated unit test')))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('新增向导的普通导航不能代替创建', () => {
  it('企点账密页点普通下一步仍停留表单，不跳过创建进入进度页', async () => {
    await reachCreateForm('qidian')
    await clickNextIfAvailable()
    expect(accountsApi.create).not.toHaveBeenCalled()
    expect(find(T.account).exists()).toBe(true)
    expect(find(T.create).exists()).toBe(true)
    expect(find(T.progress).exists()).toBe(false)
  })

  it('QQ 起名页点普通下一步仍可提交创建，不进入没有账号的扫码页', async () => {
    await reachCreateForm('qq')
    await clickNextIfAvailable()
    expect(accountsApi.create).not.toHaveBeenCalled()
    expect(find(T.label).exists()).toBe(true)
    expect(find(T.create).exists()).toBe(true)
    expect(find(T.qrRefresh).exists()).toBe(false)
  })

  it.each(channels)('%s 未提交创建时连续点下一步不能出现完成摘要', async (channel) => {
    await reachCreateForm(channel)
    for (let i = 0; i < 3; i++) await clickNextIfAvailable()
    expect(accountsApi.create).not.toHaveBeenCalled()
    expect(useAccountsStore().items).toHaveLength(0)
    expect(find(T.doneSummary).exists()).toBe(false)
  })

  it('企点没有创建账号时不得展示固定 40% 的创建进度', async () => {
    await reachCreateForm('qidian')
    await clickNextIfAvailable()
    expect(useAccountsStore().items).toHaveLength(0)
    const progress = find(T.progress)
    expect(progress.exists() ? Number(progress.attributes('aria-valuenow') ?? 0) : 0).toBe(0)
    expect(wrapper!.text()).not.toContain('当前状态:创建中')
  })

  it('企点创建响应未返回时不能用下一步显示已有账号的确定进度', async () => {
    const pending = deferred<Account>()
    vi.mocked(accountsApi.create).mockReturnValueOnce(pending.promise)
    try {
      await submitCreate('qidian')
      await clickNextIfAvailable()
      expect(useAccountsStore().items).toHaveLength(0)
      expect(accountsApi.start).not.toHaveBeenCalled()
      const progress = find(T.progress)
      expect(progress.exists() ? Number(progress.attributes('aria-valuenow') ?? 0) : 0).toBe(0)
      expect(find(T.doneSummary).exists()).toBe(false)
    } finally {
      pending.resolve(account('qidian'))
      await flushPromises()
    }
  })
})

describe.each(channels)('%s 完成页由账号真实状态驱动', (channel) => {
  it.each(['created', 'starting', 'login_required', 'error'] as const)(
    '账号处于 %s 时点下一步不能进入完成页', async (state) => {
      await submitCreate(channel)
      await stateEvent(channel, state)
      await clickNextIfAvailable()
      expect(find(T.doneSummary).exists()).toBe(false)
    },
  )

  it('收到当前账号 running 事件才自动显示该账号的完成摘要', async () => {
    await submitCreate(channel)
    expect(find(T.doneSummary).exists()).toBe(false)
    await stateEvent(channel, 'running', { self_nick: '测试昵称' })
    expect(find(T.doneSummary).text()).toContain(account(channel).id)
    expect(find(T.doneSummary).text()).toContain('测试昵称')
  })

  it('创建请求失败可见且连续导航不能伪装完成', async () => {
    vi.mocked(accountsApi.create).mockRejectedValueOnce(new Error('测试创建失败'))
    await submitCreate(channel)
    expect(notices.error).toHaveBeenCalledWith(expect.stringContaining('测试创建失败'))
    expect(accountsApi.start).not.toHaveBeenCalled()
    for (let i = 0; i < 3; i++) await clickNextIfAvailable()
    expect(find(T.doneSummary).exists()).toBe(false)
  })
})

describe.each(channels)('%s 创建后启动失败不能产生假完成', (channel) => {
  it('等待创建成功拿到 id 后才启动该账号', async () => {
    const pending = deferred<Account>()
    vi.mocked(accountsApi.create).mockReturnValueOnce(pending.promise)
    await submitCreate(channel)
    expect(accountsApi.start).not.toHaveBeenCalled()
    pending.resolve(account(channel))
    await flushPromises()
    expect(accountsApi.start).toHaveBeenCalledTimes(1)
    expect(accountsApi.start).toHaveBeenCalledWith(account(channel).id)
    expect(find(T.doneSummary).exists()).toBe(false)
  })

  it('启动请求失败可见并保留新账号，普通导航不能完成', async () => {
    vi.mocked(accountsApi.start).mockRejectedValueOnce(new Error('测试启动失败'))
    await submitCreate(channel)
    const visibleErrors = `${wrapper!.text()} ${notices.error.mock.calls.flat().join(' ')}`
    expect(visibleErrors).toContain('测试启动失败')
    expect(useAccountsStore().byId[account(channel).id]).toBeDefined()
    await clickNextIfAvailable()
    expect(find(T.doneSummary).exists()).toBe(false)
  })

  it('启动失败后重试同一账号，不重复创建；running 后才完成', async () => {
    vi.mocked(accountsApi.start).mockRejectedValueOnce(new Error('测试启动失败'))
    await submitCreate(channel)
    expect(find(T.retry).exists()).toBe(true)
    await find(T.retry).trigger('click')
    await flushPromises()
    expect(accountsApi.create).toHaveBeenCalledTimes(1)
    expect(vi.mocked(accountsApi.start).mock.calls).toEqual([[account(channel).id], [account(channel).id]])
    expect(find(T.doneSummary).exists()).toBe(false)
    await stateEvent(channel, 'running')
    expect(find(T.doneSummary).exists()).toBe(true)
  })
})
