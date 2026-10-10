/** 2026-10-10 账号详情头部重排 + 名称就地编辑(安琳要求):画面直接跟在头部之后、状态引导进右侧栏、失焦即保存。全假后端。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { message } from 'ant-design-vue'
import { accountsApi } from '@/api/client'
import type { Account } from '@/api/types'
import AcctDetailPage from '@/pages/acct/AcctDetailPage.vue'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { acctDetail as T } from '@/testids'

const routing = vi.hoisted(() => ({
  route: { path: '/acct/qd90', query: {} as Record<string, string>, params: { id: 'qd90' } as Record<string, string> },
  push: vi.fn(), replace: vi.fn(),
}))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => routing.route,
  useRouter: () => ({ push: routing.push, replace: routing.replace }),
}))

const STUBS = Object.fromEntries(['a-button', 'a-popconfirm', 'a-modal', 'a-input', 'a-checkbox', 'a-skeleton', 'a-result',
  'AccountScreen', 'PromptCard', 'StateDot'].map((name) => [name, true]))
let wrapper: VueWrapper | undefined

function account(state: Account['state'] = 'running', code = ''): Account {
  return { id: 'qd90', channel: 'qidian', host: 'wsl', label: '真实链路-企点', state, state_code: code, state_reason: '',
    error_since_ms: null, enabled: true, auto_recover: true, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 2560,
    self_uid: '3007378246', self_nick: '安琳' }
}
function mount(): VueWrapper {
  wrapper = shallowMount(AcctDetailPage, { global: { stubs: STUBS, renderStubDefaultSlot: true }, attachTo: document.body })
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  routing.route = reactive({ path: '/acct/qd90', query: {}, params: { id: 'qd90' } })
  for (const key of ['success', 'info', 'warning', 'error'] as const) vi.spyOn(message, key).mockReturnValue((() => undefined) as never)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network request in isolated UI test')))
  vi.spyOn(accountsApi, 'get').mockImplementation(async () => account())
  vi.spyOn(accountsApi, 'patch').mockImplementation(async (_id, body) => ({ ...account(), ...body }))
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  vi.spyOn(useAccountsStore(), 'refreshPrompt').mockResolvedValue()
  vi.spyOn(useResourcesStore(), 'loadMetrics').mockResolvedValue()
  useAccountsStore().upsert(account())
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('头部重排', () => {
  it('操作按钮进入头部,画面区紧跟头部,状态引导卡落在右侧栏', async () => {
    useAccountsStore().upsert(account('login_required', 'WAIT_PASSWORD'))
    const page = mount()
    await flushPromises()
    const header = page.get('header.account-heading')
    expect(header.find(`[data-testid="${T.op('stop')}"]`).exists()).toBe(true)
    expect(header.find(`[data-testid="${T.op('restart')}"]`).exists()).toBe(true)
    expect(header.text()).toContain('查询历史消息')
    expect(header.text()).not.toContain('改名')                      // 独立改名按钮已退役
    // 头部之后第一块就是工作区(画面),状态卡不再横在中间
    const blocks = page.findAll('.account-detail > *').map((n) => n.classes().join(' '))
    const headerIdx = blocks.findIndex((c) => c.includes('account-heading'))
    const layoutIdx = blocks.findIndex((c) => c.includes('account-layout'))
    expect(layoutIdx).toBe(headerIdx + 1)
    const sidebar = page.get('aside.account-sidebar')
    expect(sidebar.find(`[data-testid="${T.stateCard}"]`).exists()).toBe(true)
    expect(sidebar.find(`[data-testid="${T.stateCardAction('password')}"]`).exists()).toBe(true)
    expect(page.find(`.account-layout > section [data-testid="${T.stateCard}"]`).exists()).toBe(false)
  })
})

describe('名称就地编辑', () => {
  it('点编辑 icon 变输入框,失焦即 PATCH 并恢复展示', async () => {
    const page = mount()
    await flushPromises()
    expect(page.find(`[data-testid="${T.labelSave}"]`).exists()).toBe(false)
    await page.get(`[data-testid="${T.labelEdit}"]`).trigger('click')
    await flushPromises()
    const input = page.get<HTMLInputElement>(`[data-testid="${T.labelSave}"]`)
    expect(input.element.value).toBe('真实链路-企点')
    await input.setValue('  固收-企点  ')
    await input.trigger('blur')
    await flushPromises()
    expect(accountsApi.patch).toHaveBeenCalledWith('qd90', { label: '固收-企点' })
    expect(page.find(`[data-testid="${T.labelSave}"]`).exists()).toBe(false)
    expect(page.find('.label-display').exists()).toBe(true)
    expect(page.get('.label-display').text()).toContain('固收-企点')
  })

  it('回车等同失焦保存;未改动或清空时不发请求;Esc 放弃', async () => {
    const page = mount()
    await flushPromises()
    await page.get('h1.label-display').trigger('click')
    await flushPromises()
    let input = page.get<HTMLInputElement>(`[data-testid="${T.labelSave}"]`)
    await input.trigger('blur')                                          // 未改动
    await flushPromises()
    expect(accountsApi.patch).not.toHaveBeenCalled()
    await page.get(`[data-testid="${T.labelEdit}"]`).trigger('click')
    await flushPromises()
    input = page.get<HTMLInputElement>(`[data-testid="${T.labelSave}"]`)
    await input.setValue('   ')
    await input.trigger('blur')                                          // 清空 ⇒ 不发、恢复原名
    await flushPromises()
    expect(accountsApi.patch).not.toHaveBeenCalled()
    expect(page.text()).toContain('真实链路-企点')
    await page.get(`[data-testid="${T.labelEdit}"]`).trigger('click')
    await flushPromises()
    input = page.get<HTMLInputElement>(`[data-testid="${T.labelSave}"]`)
    await input.setValue('放弃这个名字')
    await input.trigger('keydown', { key: 'Escape' })
    await flushPromises()
    expect(accountsApi.patch).not.toHaveBeenCalled()
    expect(page.find(`[data-testid="${T.labelSave}"]`).exists()).toBe(false)
    expect(page.text()).toContain('真实链路-企点')
  })
})
