import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useSessionStore } from '@/stores/session'
import { systemApi } from '@/api/client'

beforeEach(() => { setActivePinia(createPinia()) })
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('preserves unknown session state and removes the hard-coded distribution diagnosis', async () => {
  vi.spyOn(systemApi, 'health').mockRejectedValue(new Error('offline'))
  vi.stubGlobal('qt', { wa: { invoke: vi.fn().mockRejectedValue(new Error('401')) }, localService: { status: vi.fn().mockResolvedValue({ service: 'online', session: 'unknown', api: 'offline', code: 'OK' }) } })
  const s = useSessionStore(); await s.pingAgent()
  expect(s.userAgentOnline).toBeNull(); expect(s.sessionStateText).toBe('未知')
  expect(s.GATE_LOGIN_HINT).not.toContain('请先登录 Windows')
  expect(s.gateTitle).not.toContain('qtrade')
})
it('coalesces multiple clicks and keeps UAC cancellation visible', async () => {
  let resolve!: (v: unknown) => void
  const start = vi.fn().mockImplementation(() => new Promise(r => { resolve = r }))
  vi.stubGlobal('qt', { localService: { start, status: vi.fn() } })
  const s = useSessionStore(); const a = s.startLocalService(); const b = s.startLocalService()
  expect(start).toHaveBeenCalledTimes(1); expect(s.localServiceBusy).toBe(true)
  resolve({ ok: false, code: 'UAC_CANCELLED', status: { service: 'offline', session: 'offline', api: 'unknown', code: 'UAC_CANCELLED' } })
  await Promise.all([a, b]); expect(s.localServiceMessage).toContain('已取消'); expect(s.localServiceBusy).toBe(false)
})
it('does not invent a startup ability in an old preload or ordinary browser', async () => {
  vi.stubGlobal('qt', undefined)
  const s = useSessionStore(); await s.refreshLocalStatus(); await s.startLocalService()
  expect(s.canStartLocalService).toBe(false); expect(s.localStatus).toBeNull()
})
