/** Cold-start recovery: only fake IPC/API boundaries, no Windows processes. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import SetupPage from '@/pages/setup/SetupPage.vue'
import { useSessionStore } from '@/stores/session'
import { useEnvStore } from '@/stores/env'
import { systemApi } from '@/api/client'
import type { SystemVersion } from '@/api/types'

const notices = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }))
vi.mock('ant-design-vue', () => ({ message: notices }))
vi.mock('vue-router', async original => ({
  ...await original<typeof import('vue-router')>(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

const wrappers: VueWrapper[] = []
const invoke = vi.fn(), refresh = vi.fn(), state = vi.fn()
const errors: unknown[] = []
const stubs = {
  'a-button': { props: ['disabled', 'loading'], template: '<button :disabled="disabled || loading"><slot /></button>' },
  'a-steps': true, 'a-step': true, 'a-checkbox': true, QtIcon: true,
}
function mountSetup() {
  const wrapper = shallowMount(SetupPage, { global: { stubs, config: { errorHandler: e => errors.push(e) } } })
  wrappers.push(wrapper)
  return wrapper
}
function startButton(wrapper: VueWrapper) {
  return wrapper.findAll('button').find(button => /启动.*服务/.test(button.text()))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear(); sessionStorage.clear(); errors.length = 0
  setActivePinia(createPinia())
  invoke.mockReset().mockResolvedValue({ user_agent: true, message: 'ready' })
  refresh.mockReset().mockResolvedValue(true)
  state.mockReset().mockResolvedValue('ok')
  vi.stubGlobal('qt', { wa: { invoke }, auth: { refresh, state } })
  vi.spyOn(systemApi, 'health').mockRejectedValue(new Error('guest stopped'))
  const env = useEnvStore()
  vi.spyOn(env, 'loadAll').mockResolvedValue()
  vi.spyOn(env, 'loadSelftest').mockResolvedValue()
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('连接向导的本地服务恢复', () => {
  it('Agent 未启动且本地用户会话在线时展示可操作的启动服务按钮', async () => {
    const wrapper = mountSetup()
    await flushPromises()
    expect(useSessionStore().agentReachable).toBe(false)
    const button = startButton(wrapper)
    expect(button).toBeDefined()
    expect(button!.attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="qt-setup-next"]').attributes('disabled')).toBeDefined()
  })

  it.each(['service', 'user'] as const)('本地 %s 离线时不能尝试启动受管 WSL', async offline => {
    if (offline === 'service') invoke.mockRejectedValue(new Error('WA offline'))
    else invoke.mockResolvedValue({ user_agent: false })
    const wrapper = mountSetup()
    await flushPromises()
    const button = startButton(wrapper)
    expect(!button || button.attributes('disabled') !== undefined).toBe(true)
    expect(invoke.mock.calls.some(([op]) => op === 'wsl.start')).toBe(false)
  })

  it('启动期间禁重入，真实启动完成后才刷新凭据并重新探测 Agent', async () => {
    const order: string[] = []
    let finishStart!: () => void
    invoke.mockImplementation(async (op: string) => {
      if (op === 'wsl.start') {
        order.push('start')
        await new Promise<void>(resolve => { finishStart = resolve })
        order.push('started')
      }
      return { user_agent: true }
    })
    refresh.mockImplementation(async () => { order.push('refresh'); return true })
    state.mockImplementation(async () => { order.push('auth'); return 'ok' })
    const wrapper = mountSetup()
    await flushPromises()
    const button = startButton(wrapper)
    expect(button).toBeDefined()
    await button!.trigger('click')
    expect(button!.attributes('disabled')).toBeDefined()
    await button!.trigger('click')
    expect(invoke.mock.calls.filter(([op]) => op === 'wsl.start')).toHaveLength(1)
    expect(refresh).not.toHaveBeenCalled()
    vi.mocked(systemApi.health).mockImplementation(async () => {
      order.push('health')
      return { winagent: { online: true, user_agent: true } } as Awaited<ReturnType<typeof systemApi.health>>
    })
    finishStart()
    await flushPromises()
    expect(order).toEqual(['start', 'started', 'refresh', 'auth', 'health'])
    expect(useSessionStore().agentReachable).toBe(true)
    expect(wrapper.get('[data-testid="qt-setup-next"]').attributes('disabled')).toBeUndefined()
    expect(errors).toEqual([])
  })

  it('启动被拒绝后显示原因并保留重试入口，不假报已连接', async () => {
    invoke.mockImplementation(async (op: string) => {
      if (op === 'wsl.start') throw new Error('UserAgent not ready')
      return { user_agent: true }
    })
    const wrapper = mountSetup()
    await flushPromises()
    const button = startButton(wrapper)
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
    expect(notices.error).toHaveBeenCalledWith(expect.stringContaining('UserAgent not ready'))
    expect(errors).toEqual([])
    expect(refresh).not.toHaveBeenCalled()
    expect(useSessionStore().agentReachable).toBe(false)
    expect(startButton(wrapper)!.attributes('disabled')).toBeUndefined()
  })

  it('展示健康版本 DTO 中的 agent.version，不向用户打印整个对象', async () => {
    useEnvStore().version = { agent: { version: '0.1.23', git: 'fixture', api: 'v1' } } as unknown as SystemVersion
    const wrapper = mountSetup()
    await flushPromises()
    const text = wrapper.get('[data-testid="qt-setup-conn-agent"]').text()
    expect(text).toContain('Agent · 0.1.23')
    expect(text).not.toContain('"version"')
    expect(text).not.toContain('[object Object]')
  })
})

it('已有 session.startWsl 调用者同样先完成启动，再刷新管道与健康', async () => {
  const order: string[] = []
  invoke.mockImplementation(async () => { order.push('start'); return {} })
  refresh.mockImplementation(async () => { order.push('refresh'); return true })
  state.mockImplementation(async () => { order.push('auth'); return 'ok' })
  vi.mocked(systemApi.health).mockImplementation(async () => {
    order.push('health')
    return { winagent: { online: true, user_agent: true } } as Awaited<ReturnType<typeof systemApi.health>>
  })
  await useSessionStore().startWsl()
  expect(order).toEqual(['start', 'refresh', 'auth', 'health'])
})
