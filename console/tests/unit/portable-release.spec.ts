import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createPipeClient } from '../../electron/main/winagent-pipe'
import MailPage from '@/pages/mail/MailPage.vue'
import { useMailStore } from '@/stores/mail'
import { ossboxApi } from '@/api/client'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))
let wrapper: VueWrapper | undefined

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.spyOn(useMailStore(), 'reloadAll').mockResolvedValue()
  vi.spyOn(useMailStore(), 'startPolling').mockImplementation(() => undefined)
  vi.spyOn(useMailStore(), 'stopPolling').mockImplementation(() => undefined)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"ok":true,"data":{}}')))
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('正式发行环境隔离', () => {
  it('即使宿主设置开发令牌，正式包仍选真实命名管道', () => {
    vi.stubEnv('QT_DEV_TOKEN', 'UNTRUSTED_HOST_DEV_TOKEN')
    expect(createPipeClient({ isPackaged: true }).describe()).toMatch(/^named-pipe /)
  })
  it('未明确启用开发形态时不能使用环境变量令牌', () => {
    vi.stubEnv('QT_DEV_TOKEN', 'UNTRUSTED_HOST_DEV_TOKEN')
    expect(createPipeClient().describe()).toMatch(/^named-pipe /)
  })
  it('开发形态没有显式令牌时不得合成 dev-token', () => {
    vi.stubEnv('QT_DEV_TOKEN', '')
    expect(createPipeClient({ isPackaged: false }).describe()).toMatch(/^named-pipe /)
  })
  it('显式开发形态和显式令牌可以使用隔离 mock', async () => {
    vi.stubEnv('QT_DEV_TOKEN', 'EXPLICIT_TEST_ONLY_TOKEN')
    const client = createPipeClient({ isPackaged: false })
    expect((await client.fetchTokens()).agent_token).toBe('EXPLICIT_TEST_ONLY_TOKEN')
  })
})

describe('新机器没有个人工作台或实验占位配置', () => {
  it('选择公网上报后，三个现场配置均为空且无开发占位值', async () => {
    vi.spyOn(ossboxApi, 'get').mockResolvedValue({
      mode: 'its_relay', state: 'pending_alignment', applied: false,
      relay: { base_url: '', workbench_name: '', workbench_id: 'synthetic-new-workbench',
        configured: false, key_configured: false, sm4_key_configured: false, saved: false },
      restart_required: false,
    })
    wrapper = shallowMount(MailPage, { global: { renderStubDefaultSlot: true, stubs: { OssboxConfig: false } } })
    await flushPromises()
    await wrapper.get('input[type="radio"][value="oss"]').setValue()
    await flushPromises()
    const inputs = wrapper.findAll('[data-testid="ossbox-workbench-name"], [data-testid="ossbox-relay-url"], [data-testid="ossbox-relay-key"]')
    expect(inputs).toHaveLength(3)
    expect(inputs.map(input => (input.element as HTMLInputElement).value)).toEqual(['', '', ''])
    expect((wrapper.get('[data-testid="ossbox-relay-sm4-key"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.html()).not.toMatch(/anlin-dev|qtrade-nyoss-lab/)
  })
})
