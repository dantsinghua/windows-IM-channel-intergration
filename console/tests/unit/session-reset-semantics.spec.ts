import { afterEach, expect, it, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { ApiFailure } from '@/api/http'
import Card from '@/components/AccountSessionResetStatus.vue'
const get = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', () => ({ accountsApi: { sessionResetStatus: get } }))
let w: ReturnType<typeof shallowMount> | undefined
afterEach(() => { w?.unmount(); get.mockReset() })
async function show(value: unknown) {
  get.mockResolvedValueOnce(value)
  w = shallowMount(Card, { props: { accountId: 'qd01' }, global: { renderStubDefaultSlot: true } })
  await flushPromises(); return w
}
it('states the xunjia reset purpose without claiming login or history failure', async () => {
  const page = await show({ available: true, account_id: 'qd01', account_epoch: 0, reset: null })
  expect(page.text()).toContain('询价会话重置')
  expect(page.text()).toContain('当前没有会话重置任务')
  expect(page.find('[role="alert"]').exists()).toBe(false)
})
it('not connected is an actionable state not a transient read failure', async () => {
  const page = await show({ available: false, account_id: 'qd01', connection_state: 'not_connected', reset: null })
  expect(page.text()).toContain('询价文件通道尚未接入')
  expect(page.find('[role="alert"]').exists()).toBe(false)
})
it('does not hide genuine backend failures', async () => {
  get.mockRejectedValueOnce(new Error('synthetic database read failed'))
  w = shallowMount(Card, { props: { accountId: 'qd01' } }); await flushPromises()
  expect(w.get('[role="alert"]').text()).toContain('采集 API 或本地账本')
  expect(w.text()).not.toContain('当前没有会话重置任务')
})
it('rejects a mismatched account response', async () => {
  const page = await show({ available: true, account_id: 'qd02', account_epoch: 0, reset: null })
  expect(page.find('[role="alert"]').exists()).toBe(true)
})
it('rejects the retired API response instead of converting it to success', async () => {
  get.mockRejectedValueOnce(new ApiFailure('NOT_READY', 503, 'synthetic', { message: 'not configured', reason: 'session_reset_not_configured', retryable: false, needs_human: false }))
  w = shallowMount(Card, { props: { accountId: 'qd01' } }); await flushPromises()
  expect(w.get('[role="alert"]').text()).toContain('采集 API 或本地账本')
  expect(w.text()).not.toContain('当前没有会话重置任务')
})
