/**
 * 企点首次创建的密码生命周期：#2 建档 → #9 启动 → WAIT_PASSWORD → #12。
 * 只操作表单/按钮和账号事件；API 全部为桩，不访问真实服务或账号。
 * TEST_SECRET 是虚构测试值。只观察请求、DOM 和公开状态，不断言私有变量。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import AcctNewPage from '@/pages/acct/AcctNewPage.vue'
import { accountsApi, commandsApi, resourcesApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import type { Account, AccountStatePayload, ResourcePool } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { acctNew as T } from '@/testids'

const TEST_SECRET = 'fictional-secret-for-unit-tests-only'
const ACCOUNT_ID = 'qd-secret-test'
const routeState = vi.hoisted(() => ({ query: { ch: 'qidian' } }))
const routerSpy = vi.hoisted(() => ({ push: vi.fn() }))
const notices = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }))
vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRoute: () => routeState,
  useRouter: () => routerSpy,
}))
vi.mock('ant-design-vue', () => ({ message: notices }))

// 与既有生命周期测试一致：原生元素保留 disabled、input 和 checked 行为。
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
  'a-checkbox': {
    props: ['checked', 'disabled'],
    emits: ['update:checked'],
    template: '<input type="checkbox" :checked="checked" :disabled="disabled" @change="$emit(\'update:checked\', $event.target.checked)" />',
  },
  'a-progress': true,
  'a-steps': true,
  'a-step': true,
  'a-radio-group': true,
  'a-radio': true,
  'a-select': true,
  'a-modal': true,
  'a-alert': true,
}

function account(): Account {
  return {
    id: ACCOUNT_ID, channel: 'qidian', host: 'wsl', label: '密码生命周期测试',
    state: 'created', state_code: '', state_reason: '', error_since_ms: null,
    enabled: true, auto_recover: true, deleted_ms: null,
    runtime: {}, capabilities: [], quota_mb: 2048,
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

function deferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>((done) => { resolve = done })
  return { promise, resolve }
}

function apiError(status: number, reason: string): ApiFailure {
  return new ApiFailure('NOT_APPLICABLE', status, 'test-trace-secret', {
    message: `测试登录失败 ${reason}`, reason, retryable: reason === 'busy', needs_human: false,
  })
}

let wrapper: VueWrapper | null = null
let pinia: Pinia
let seq = 0
const find = (id: string) => wrapper!.find(`[data-testid="${id}"]`)

async function openForm(remember = false): Promise<void> {
  wrapper = shallowMount(AcctNewPage, { global: { stubs: STUBS } })
  await flushPromises()
  await find(T.next).trigger('click')
  await find(T.label).setValue('密码生命周期测试')
  await find(T.next).trigger('click')
  await find(T.next).trigger('click')
  await find(T.account).setValue('9000000088')
  await find(T.secret).setValue(TEST_SECRET)
  await find(T.remember).setValue(remember)
  expect((find(T.remember).element as HTMLInputElement).checked).toBe(remember)
}

async function createAccount(remember = false): Promise<void> {
  await openForm(remember)
  await find(T.create).trigger('click')
  await flushPromises()
  expect(accountsApi.create).toHaveBeenCalledTimes(1)
  expect(useAccountsStore().byId[ACCOUNT_ID]).toBeDefined()
}

async function stateEvent(state: Account['state'], stateCode = '', id = ACCOUNT_ID): Promise<void> {
  const payload: AccountStatePayload = {
    state, state_code: stateCode, state_reason: '', error_since_ms: null,
    enabled: true, runtime: {}, capabilities: [],
  }
  useAccountsStore().applyAccountState({
    event: 'account_state', seq: ++seq, ts: '2026-10-08T00:00:00Z',
    account_id: id, channel: 'qidian', payload,
  })
  await flushPromises()
}

async function advance(ms = 60_000): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms)
  await flushPromises()
}

function expectSecretNotRetained(): void {
  expect(JSON.stringify(pinia.state.value)).not.toContain(TEST_SECRET)
  expect(JSON.stringify(routeState)).not.toContain(TEST_SECRET)
  expect(JSON.stringify(routerSpy.push.mock.calls)).not.toContain(TEST_SECRET)
  expect(JSON.stringify(window.localStorage)).not.toContain(TEST_SECRET)
  expect(JSON.stringify(window.sessionStorage)).not.toContain(TEST_SECRET)
  expect(JSON.stringify(vi.mocked(window.Storage.prototype.setItem).mock.calls)).not.toContain(TEST_SECRET)
}

function visibleErrors(): string {
  return `${wrapper?.text() ?? ''} ${notices.error.mock.calls.flat().join(' ')}`
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  pinia = createPinia()
  setActivePinia(pinia)
  seq = 0
  window.localStorage.clear()
  window.sessionStorage.clear()
  vi.spyOn(window.Storage.prototype, 'setItem')
  vi.spyOn(resourcesApi, 'get').mockResolvedValue(resourcePool())
  vi.spyOn(commandsApi, 'deviceProfiles').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(accountsApi, 'create').mockResolvedValue(account())
  vi.spyOn(accountsApi, 'start').mockResolvedValue({ state: 'starting' })
  vi.spyOn(accountsApi, 'restart').mockResolvedValue({ state: 'starting' })
  vi.spyOn(accountsApi, 'softDelete').mockResolvedValue({ deleted: true, data_kept: true })
  vi.spyOn(accountsApi, 'login').mockResolvedValue({ state: 'login_required', state_code: 'WAIT_SMS', login_session_id: 'ls_test' })
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network call in isolated unit test')))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('新增企点账号的不保存密码生命周期', () => {
  it('不保存时建档请求省略密码，提交即清空表单，密码不进入 Pinia、storage 或路由', async () => {
    const pending = deferred<Account>()
    vi.mocked(accountsApi.create).mockReturnValueOnce(pending.promise)
    await openForm()
    try {
      await find(T.create).trigger('click')
      await flushPromises()
      expect(accountsApi.create).toHaveBeenCalledTimes(1)
      expect.soft(vi.mocked(accountsApi.create).mock.calls[0]![0].login).toEqual({
        mode: 'password', account: '9000000088', remember: false,
      })
      const input = find(T.secret)
      expect.soft(input.exists() ? (input.element as HTMLInputElement).value : '').toBe('')
      expectSecretNotRetained()
    } finally {
      pending.resolve(account())
      await flushPromises()
    }
  })

  it('首次稳定 WAIT_PASSWORD 只提交一次密码，重复事件和后续掉线不会自动重登', async () => {
    const pending = deferred<Awaited<ReturnType<typeof accountsApi.login>>>()
    vi.mocked(accountsApi.login).mockReturnValueOnce(pending.promise)
    await createAccount()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    try {
      expect(vi.mocked(accountsApi.start).mock.calls).toEqual([[ACCOUNT_ID]])
      expect(vi.mocked(accountsApi.login).mock.calls).toEqual([[ACCOUNT_ID, { secret: TEST_SECRET, remember: false }]])
      expectSecretNotRetained()
      await stateEvent('login_required', 'WAIT_PASSWORD')
      await advance()
      expect(accountsApi.login).toHaveBeenCalledTimes(1)
    } finally {
      pending.resolve({ state: 'login_required', state_code: 'WAIT_SMS', login_session_id: 'ls_test' })
      await flushPromises()
    }
    await stateEvent('running')
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).toHaveBeenCalledTimes(1)
  })

  it('保存密码沿用建档与启动的保险库路径，收到 WAIT_PASSWORD 也不额外调用登录', async () => {
    await createAccount(true)
    expect(vi.mocked(accountsApi.create).mock.calls[0]![0].login).toEqual({
      mode: 'password', account: '9000000088', secret: TEST_SECRET, remember: true,
    })
    expect(vi.mocked(accountsApi.start).mock.calls).toEqual([[ACCOUNT_ID]])
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    expectSecretNotRetained()
  })

  it('仅当前账号同时为 login_required 和 WAIT_PASSWORD 时才交接密码，也响应同 state 的 code 变化', async () => {
    await createAccount()
    await stateEvent('login_required', 'WAIT_PASSWORD', 'qd-unrelated-test')
    await stateEvent('starting', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    await stateEvent('login_required', 'WAIT_SMS')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(vi.mocked(accountsApi.login).mock.calls).toEqual([[ACCOUNT_ID, { secret: TEST_SECRET, remember: false }]])
  })

  it('启动请求尚未完成时不抢发登录；启动完成且仍等待密码时才交接', async () => {
    const pending = deferred<{ state: string }>()
    vi.mocked(accountsApi.start).mockReturnValueOnce(pending.promise)
    await createAccount()
    try {
      await stateEvent('login_required', 'WAIT_PASSWORD')
      await advance()
      expect(accountsApi.login).not.toHaveBeenCalled()
    } finally {
      pending.resolve({ state: 'starting' })
      await flushPromises()
    }
    await advance()
    expect(vi.mocked(accountsApi.login).mock.calls).toEqual([[ACCOUNT_ID, { secret: TEST_SECRET, remember: false }]])
  })

  it('登录明确返回 409 busy 可有限重试，成功后不再提交', async () => {
    vi.mocked(accountsApi.login).mockRejectedValueOnce(apiError(409, 'busy'))
    await createAccount()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(vi.mocked(accountsApi.login).mock.calls).toEqual([
      [ACCOUNT_ID, { secret: TEST_SECRET, remember: false }],
      [ACCOUNT_ID, { secret: TEST_SECRET, remember: false }],
    ])
    await stateEvent('starting')
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).toHaveBeenCalledTimes(2)
  })

  it('持续 409 busy 会达到上限并清除密码，后续事件和时间推进不能无限重试', async () => {
    vi.mocked(accountsApi.login).mockRejectedValue(apiError(409, 'busy'))
    await createAccount()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    const attempts = vi.mocked(accountsApi.login).mock.calls.length
    expect(attempts).toBeGreaterThan(1)
    expect(attempts).toBeLessThanOrEqual(3)
    expect(visibleErrors()).toContain('busy')
    await stateEvent('starting')
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance(10 * 60_000)
    expect(accountsApi.login).toHaveBeenCalledTimes(attempts)
  })

  it('409 的其他 reason 不重试；后续 WAIT_PASSWORD 不能复用已清密码', async () => {
    vi.mocked(accountsApi.login).mockRejectedValue(apiError(409, 'invalid_state'))
    await createAccount()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).toHaveBeenCalledTimes(1)
    expect(visibleErrors()).toContain('invalid_state')
    await stateEvent('starting')
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).toHaveBeenCalledTimes(1)
  })

  it('登录传输超时后不自动重试或复用已清密码', async () => {
    vi.mocked(accountsApi.login).mockRejectedValue(new Error('测试请求超时'))
    await createAccount()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).toHaveBeenCalledTimes(1)
    expect(visibleErrors()).toContain('测试请求超时')
    await stateEvent('starting')
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).toHaveBeenCalledTimes(1)
  })

  it('取消向导后，即使启动请求完成或再收到等待密码事件也不提交密码', async () => {
    const pending = deferred<{ state: string }>()
    vi.mocked(accountsApi.start).mockReturnValueOnce(pending.promise)
    await createAccount()
    await find(T.cancel).trigger('click')
    await flushPromises()
    expect(routerSpy.push).toHaveBeenCalledWith('/acct')
    pending.resolve({ state: 'starting' })
    await flushPromises()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    expectSecretNotRetained()
  })

  it('卸载后迟到的启动响应和账号事件不会提交密码', async () => {
    const pending = deferred<{ state: string }>()
    vi.mocked(accountsApi.start).mockReturnValueOnce(pending.promise)
    await createAccount()
    wrapper!.unmount()
    wrapper = null
    pending.resolve({ state: 'starting' })
    await flushPromises()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    expectSecretNotRetained()
  })

  it('取消软删失败必须可见并保留账号，密码仍清除且不会继续登录', async () => {
    vi.mocked(accountsApi.softDelete).mockRejectedValueOnce(new Error('测试取消失败'))
    await createAccount()
    await find(T.cancel).trigger('click')
    await flushPromises()
    expect(accountsApi.softDelete).toHaveBeenCalledTimes(1)
    expect(visibleErrors()).toContain('测试取消失败')
    expect(routerSpy.push).not.toHaveBeenCalled()
    expect(useAccountsStore().byId[ACCOUNT_ID]).toBeDefined()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    expectSecretNotRetained()
  })

  it('首次流程临时密码超过五分钟即失效，迟到的 WAIT_PASSWORD 不会自动登录', async () => {
    await createAccount()
    await stateEvent('starting')
    await advance(5 * 60_000 + 1)
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
    expectSecretNotRetained()
  })

  it('启动失败后重试同一账号且不重建，也不复用首次流程已清的密码', async () => {
    vi.mocked(accountsApi.start).mockRejectedValueOnce(new Error('测试启动失败'))
    await createAccount()
    expect(visibleErrors()).toContain('测试启动失败')
    expect(find(T.retry).exists()).toBe(true)
    await find(T.retry).trigger('click')
    await flushPromises()
    expect(accountsApi.create).toHaveBeenCalledTimes(1)
    expect(vi.mocked(accountsApi.start).mock.calls).toEqual([[ACCOUNT_ID], [ACCOUNT_ID]])
    expect(accountsApi.restart).not.toHaveBeenCalled()
    await stateEvent('login_required', 'WAIT_PASSWORD')
    await advance()
    expect(accountsApi.login).not.toHaveBeenCalled()
  })
})
