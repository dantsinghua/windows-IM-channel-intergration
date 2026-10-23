import { afterEach, expect, it, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import Card from '@/components/AccountSessionResetStatus.vue'
const get = vi.hoisted(() => vi.fn())
vi.mock('@/api/client', () => ({ accountsApi: { sessionResetStatus: get } }))
let w: ReturnType<typeof shallowMount> | undefined
afterEach(() => { w?.unmount(); get.mockReset() })
async function show(channel: unknown) {
  get.mockResolvedValue({ available: true, account_id: 'qd01', reset: null, channel_status: channel })
  w = shallowMount(Card, { props: { accountId: 'qd01' }, global: { renderStubDefaultSlot: true } })
  await flushPromises(); return w
}
it('shows native online and relay HTTP 502 without pretending a reset is running', async () => {
  const page = await show({ native_state: 'online', heartbeat_fresh: false, intake_ready: true, errors: { directory: 'FILE_GATEWAY_HTTP_ERROR (HTTP 502)' } })
  expect(page.get('h3').text()).toBe('询价文件通道')
  expect(page.text()).toContain('企点原生界面：在线')
  expect(page.text()).toContain('HTTP 502')
  expect(page.text()).toContain('当前没有会话重置任务')
  expect(page.find('[data-testid="session-reset-phase"]').exists()).toBe(false)
})
it('shows a stale heartbeat as unverified transport, not a logout', async () => {
  const page = await show({ native_state: 'online', heartbeat_fresh: false, intake_ready: true, errors: {} })
  expect(page.text()).toContain('心跳已过期')
  expect(page.text()).not.toContain('企点已离线')
})
it('recovers relay status without changing the reset state', async () => {
  const page = await show({ native_state: 'online', heartbeat_fresh: true, intake_ready: true, errors: {} })
  expect(page.text()).toContain('目录心跳正常')
  expect(page.text()).not.toContain('HTTP 502')
})
