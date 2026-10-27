import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSessionStore } from '@/stores/session'
import { systemApi } from '@/api/client'
import type { RecoveryResult, RecoveryStatus } from '../../electron/main/winagent-recovery'

const offline: RecoveryStatus = { service: 'online', session: 'online', api: 'offline', code: 'OK' }
const online: RecoveryStatus = { service: 'online', session: 'online', api: 'online', code: 'OK' }

function shell(result: RecoveryResult) {
  const status = vi.fn().mockResolvedValue({ ...offline })
  const start = vi.fn().mockResolvedValue(result)
  vi.stubGlobal('qt', {
    localService: { status, start },
    auth: { state: vi.fn().mockResolvedValue('ok'), refresh: vi.fn().mockResolvedValue(true) },
    wa: { invoke: vi.fn().mockResolvedValue({ user_agent: true }) },
  })
  return { status, start }
}

function healthy() {
  return { ok: true, agent: true, dockerd: true, winagent: { online: true, version: 'synthetic', user_agent: true } }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Real HTTP is forbidden in this regression') }))
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('refreshes the successful-start observation after the collector API recovers without another start', async () => {
  const health = vi.spyOn(systemApi, 'health').mockRejectedValue(new Error('synthetic offline'))
  const api = shell({ ok: true, code: 'STARTED', status: { ...offline } })
  const session = useSessionStore()
  await session.startLocalService()
  expect(session.apiStateText).toBe('离线')
  expect(session.agentReachable).toBe(false)

  health.mockResolvedValue(healthy())
  api.status.mockResolvedValue({ ...online })
  await session.pingAgent()

  expect(session.agentReachable).toBe(true)
  expect(session.apiStateText).toBe('在线')
  expect(session.localServiceMessage).not.toContain('采集 API 离线')
  expect(api.start).toHaveBeenCalledTimes(1)
  expect(fetch).not.toHaveBeenCalled()
})

it('does not keep an online observation after a later API failure', async () => {
  const health = vi.spyOn(systemApi, 'health').mockResolvedValue(healthy())
  const api = shell({ ok: true, code: 'STARTED', status: { ...online } })
  api.status.mockResolvedValue({ ...online })
  const session = useSessionStore()
  await session.startLocalService()
  expect(session.apiStateText).toBe('在线')

  health.mockRejectedValue(new Error('synthetic offline'))
  api.status.mockResolvedValue({ ...offline })
  await session.pingAgent()

  expect(session.agentReachable).toBe(false)
  expect(session.apiStateText).toBe('离线')
  expect(session.localServiceMessage).not.toContain('采集 API 在线')
  expect(api.start).toHaveBeenCalledTimes(1)
})

it.each(['UAC_CANCELLED', 'START_FAILED'])(
  'keeps the actual %s result when later health becomes online', async (code) => {
    vi.spyOn(systemApi, 'health').mockResolvedValue(healthy())
    const api = shell({ ok: false, code, status: { ...offline, code } })
    const session = useSessionStore()
    await session.startLocalService()
    const failureMessage = session.localServiceMessage
    expect(failureMessage).toMatch(code === 'UAC_CANCELLED' ? /已取消/ : /启动未完成/)

    api.status.mockResolvedValue({ ...online })
    await session.pingAgent()

    expect(session.agentReachable).toBe(true)
    expect(session.apiStateText).toBe('在线')
    expect(session.localServiceMessage).toBe(failureMessage)
    expect(api.start).toHaveBeenCalledTimes(1)
  },
)

it('keeps a rejected start operation distinct from later healthy observation', async () => {
  vi.spyOn(systemApi, 'health').mockResolvedValue(healthy())
  const api = shell({ ok: true, code: 'STARTED', status: { ...online } })
  api.start.mockRejectedValue(new Error('synthetic startup failure'))
  const session = useSessionStore()
  await session.startLocalService()
  const failureMessage = session.localServiceMessage
  expect(failureMessage).toContain('启动操作失败')

  api.status.mockResolvedValue({ ...online })
  await session.pingAgent()

  expect(session.agentReachable).toBe(true)
  expect(session.localServiceMessage).toBe(failureMessage)
  expect(api.start).toHaveBeenCalledTimes(1)
})

it('does not erase an authentication-refresh failure just because the API later answers', async () => {
  vi.spyOn(systemApi, 'health').mockResolvedValue(healthy())
  const api = shell({ ok: true, code: 'STARTED', status: { ...online } })
  api.status.mockResolvedValue({ ...online })
  vi.stubGlobal('qt', {
    localService: api,
    auth: { state: vi.fn().mockResolvedValue('ok'), refresh: vi.fn().mockRejectedValue(new Error('synthetic auth failure')) },
    wa: { invoke: vi.fn() },
  })
  const session = useSessionStore()
  await session.startLocalService()
  const failureMessage = session.localServiceMessage
  expect(failureMessage).toContain('启动操作失败')

  await session.pingAgent()

  expect(session.agentReachable).toBe(true)
  expect(session.localServiceMessage).toBe(failureMessage)
  expect(api.start).toHaveBeenCalledTimes(1)
})
