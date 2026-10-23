import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { accountsApi } from '@/api/client'
import type { Account, AccountSessionResetStatusData } from '@/api/types'
import AcctDetailPage from '@/pages/acct/AcctDetailPage.vue'
import AccountSessionResetStatus from '@/components/AccountSessionResetStatus.vue'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'

const routing = vi.hoisted(() => ({
  route: { params: { id: 'qd90' }, query: {}, path: '/acct/qd90' },
  push: vi.fn(),
}))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => routing.route,
  useRouter: () => ({ push: routing.push }),
}))

const uiStubs = Object.fromEntries([
  'a-button', 'a-popconfirm', 'a-modal', 'a-input', 'a-checkbox', 'a-skeleton', 'a-result',
  'AccountScreen', 'PromptCard', 'StateDot',
].map((name) => [name, true]))
let wrapper: VueWrapper | undefined

function account(id: string, channel: Account['channel']): Account {
  return { id, channel, host: channel === 'wechat' ? 'windows' : 'wsl', label: `${channel} synthetic`,
    state: 'running', state_code: '', state_reason: '', error_since_ms: null, enabled: true,
    auto_recover: false, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 1024,
    self_uid: channel === 'wechat' ? 'wxid_synthetic' : '3000000001', self_nick: 'synthetic' }
}

function status(accountId: string, resetId = 'fresh-qidian-reset'): AccountSessionResetStatusData {
  return { available: true, account_id: accountId, account_epoch: 2,
    reset: { reset_id: resetId, phase: 'complete', account_epoch: 2,
      snapshot_hash: 'a'.repeat(64), snapshot: { account_epoch: 2, unresolved_history: [] } } }
}

function show(id: string): VueWrapper {
  routing.route.params.id = id
  wrapper = mount(AcctDetailPage, {
    global: { stubs: uiStubs, renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  return wrapper
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  setActivePinia(createPinia())
  routing.route = reactive({ params: { id: 'qd90' }, query: {}, path: '/acct/qd90' })
  for (const [id, channel] of [['qd90', 'qidian'], ['wx90', 'wechat'], ['qq90', 'qq']] as const) {
    useAccountsStore().upsert(account(id, channel))
  }
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  vi.spyOn(useResourcesStore(), 'loadMetrics').mockResolvedValue()
  vi.spyOn(accountsApi, 'sessionResetStatus').mockImplementation(async (id) => status(id))
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected external request')))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('账号详情的询价功能范围', () => {
  it('企点挂载真实询价状态卡并读取和轮询该账号的 reset API', async () => {
    const page = show('qd90')
    await flushPromises()
    expect(page.findComponent(AccountSessionResetStatus).exists()).toBe(true)
    expect(page.get('[data-testid="account-session-reset-status"]').text()).toContain('fresh-qidian-reset')
    expect(accountsApi.sessionResetStatus).toHaveBeenCalledTimes(1)
    expect(accountsApi.sessionResetStatus).toHaveBeenLastCalledWith('qd90', { signal: expect.any(AbortSignal) })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(accountsApi.sessionResetStatus).toHaveBeenCalledTimes(2)
    expect(accountsApi.sessionResetStatus).toHaveBeenLastCalledWith('qd90', { signal: expect.any(AbortSignal) })
  })

  it.each(['wx90', 'qq90'])('%s 不挂载企点询价状态卡，也不启动 reset API 轮询', async (id) => {
    const page = show(id)
    await flushPromises()
    expect(page.findComponent(AccountSessionResetStatus).exists()).toBe(false)
    expect(page.find('[data-testid="account-session-reset-status"]').exists()).toBe(false)
    expect(page.text()).not.toContain('询价文件通道')
    expect(page.text()).not.toContain('企点登录、文件中转和询价会话重置')
    await vi.advanceTimersByTimeAsync(20_000)
    expect(accountsApi.sessionResetStatus).not.toHaveBeenCalled()
  })

  it('企点切换微信时卸载卡片、中止在途轮询，并隔离迟到响应', async () => {
    let resolveLate!: (value: AccountSessionResetStatusData) => void
    const pending = new Promise<AccountSessionResetStatusData>((resolve) => { resolveLate = resolve })
    const read = vi.mocked(accountsApi.sessionResetStatus)
    read.mockResolvedValueOnce(status('qd90', 'initial-qidian-reset')).mockReturnValueOnce(pending)
    const page = show('qd90')
    await flushPromises()
    expect(page.text()).toContain('initial-qidian-reset')
    await vi.advanceTimersByTimeAsync(5_000)
    expect(read).toHaveBeenCalledTimes(2)
    const signal = read.mock.calls[1]![1]!.signal!
    expect(signal.aborted).toBe(false)
    routing.route.params.id = 'wx90'
    await flushPromises()
    expect(page.findComponent(AccountSessionResetStatus).exists()).toBe(false)
    expect(signal.aborted).toBe(true)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(read).toHaveBeenCalledTimes(2)
    resolveLate(status('qd90', 'LATE-QIDIAN-MUST-NOT-LEAK'))
    await flushPromises()
    expect(page.text()).not.toContain('LATE-QIDIAN-MUST-NOT-LEAK')
    expect(page.text()).not.toContain('initial-qidian-reset')
    read.mockResolvedValueOnce(status('qd90', 'new-qidian-reset'))
    routing.route.params.id = 'qd90'
    await flushPromises()
    expect(read).toHaveBeenCalledTimes(3)
    expect(page.get('[data-testid="account-session-reset-status"]').text()).toContain('new-qidian-reset')
    expect(page.text()).not.toContain('LATE-QIDIAN-MUST-NOT-LEAK')
  })
})
