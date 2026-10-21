import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { shallowMount, flushPromises } from '@vue/test-utils'
import EnvPage from '@/pages/env/EnvPage.vue'
import { useEnvStore } from '@/stores/env'
import { useSettingsStore } from '@/stores/settings'
import { useAccountsStore } from '@/stores/accounts'
import { useSessionStore } from '@/stores/session'
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }), useRouter: () => ({ push: vi.fn() }) }))
vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), error: vi.fn(), info: vi.fn() }, Modal: { confirm: vi.fn() } }))
let wrapper: ReturnType<typeof shallowMount> | undefined
beforeEach(() => {
  setActivePinia(createPinia())
  vi.spyOn(useEnvStore(), 'loadAll').mockResolvedValue(undefined)
  vi.spyOn(useEnvStore(), 'loadSelftest').mockResolvedValue(undefined)
  vi.spyOn(useSettingsStore(), 'loadAll').mockResolvedValue(undefined)
  vi.spyOn(useAccountsStore(), 'loadFirst').mockResolvedValue(undefined)
})
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('offers a service start action independent of a stopped collector API', async () => {
  const start = vi.fn().mockResolvedValue({ ok: false, code: 'UAC_CANCELLED', status: { service: 'offline', session: 'unknown', api: 'offline', code: 'UAC_CANCELLED' } })
  vi.stubGlobal('qt', { localService: { start, status: vi.fn() } })
  const session = useSessionStore(); session.localStatus = { service: 'offline', session: 'unknown', api: 'offline', code: 'OK' }
  wrapper = shallowMount(EnvPage, { global: { renderStubDefaultSlot: true } }); await flushPromises()
  const button = wrapper.get('[data-testid="qt-env-start-winagent"]')
  expect(button.text()).toContain('启动 WinAgent')
  expect(wrapper.text()).toContain('用户会话代理 · 未知')
  expect(wrapper.text()).toContain('采集 API · 离线')
  await button.trigger('click'); await flushPromises()
  expect(start).toHaveBeenCalledWith()
  expect(wrapper.text()).toContain('已取消管理员确认')
})
