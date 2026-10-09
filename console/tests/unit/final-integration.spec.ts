/** Final account/setup regressions. All bridges and APIs are local fakes. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, reactive, type Component } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import AcctNewPage from '@/pages/acct/AcctNewPage.vue'
import AcctDetailPage from '@/pages/acct/AcctDetailPage.vue'
import SetupPage from '@/pages/setup/SetupPage.vue'
import { accountsApi, commandsApi, systemApi } from '@/api/client'
import type { Account, Prompt } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'
import { useSetupStore } from '@/stores/setup'
import { useEnvStore } from '@/stores/env'
import { setupRedirect } from '@/router'
import { acctNew as NEW, acctDetail as DETAIL, setup as SETUP } from '@/testids'

const routing = vi.hoisted(() => ({
  route: { path: '/acct/new', query: {} as Record<string, string>, params: {} as Record<string, string> },
  push: vi.fn(), replace: vi.fn(),
}))
const notices = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), warning: vi.fn(), error: vi.fn() }))
vi.mock('vue-router', async (original) => ({
  ...await original<typeof import('vue-router')>(),
  useRoute: () => routing.route,
  useRouter: () => ({ push: routing.push, replace: routing.replace }),
}))
vi.mock('ant-design-vue', () => ({ message: notices }))

const ScreenStub = defineComponent({
  props: { accountId: String, embedded: Boolean },
  template: '<div data-screen-stub :data-account-id="accountId" :data-embedded="String(embedded)" />',
})
const STUBS = {
  ...Object.fromEntries(['a-steps', 'a-step', 'a-radio', 'a-radio-group', 'a-select', 'a-progress',
    'a-skeleton', 'a-result', 'a-modal', 'a-tooltip', 'StateDot', 'QtIcon'].map((name) => [name, true])),
  'a-button': { props: ['disabled', 'loading'], template: '<button :disabled="disabled || loading"><slot /></button>' },
  'a-input': { props: ['value'], emits: ['update:value'], template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />' },
  'a-checkbox': { props: ['checked', 'disabled'], emits: ['change'], template: '<input type="checkbox" :checked="checked" :disabled="disabled" @change="$emit(\'change\', $event)" />' },
  'a-alert': { props: ['message', 'description'], template: '<div role="alert">{{ message }} {{ description }}<slot /><slot name="action" /></div>' },
  'a-card': { template: '<section><slot /></section>' },
  'a-popconfirm': {
    emits: ['confirm', 'cancel'], data: () => ({ open: false }),
    template: '<div><span @click="open = true"><slot /></span><div v-if="open" role="dialog"><button data-confirm @click="$emit(\'confirm\'); open = false">Confirm</button><button data-cancel @click="$emit(\'cancel\'); open = false">Cancel</button></div></div>',
  },
  PromptCard: false, AccountScreen: ScreenStub, ScreenPage: ScreenStub,
}
const wrappers: VueWrapper[] = []
const waInvoke = vi.fn()
const eventErrors: unknown[] = []
let moduleEnabled = false

function account(channel: Account['channel'], code = 'WAIT_QRCODE'): Account {
  return { id: channel + '-test', channel, host: channel === 'wechat' ? 'windows' : 'wsl', label: 'Test account',
    state: 'login_required', state_code: code, state_reason: '', error_since_ms: null, enabled: true,
    auto_recover: true, deleted_ms: null, runtime: {}, capabilities: [], quota_mb: 512 }
}
function mountPage(page: Component): VueWrapper {
  const wrapper = shallowMount(page, { global: { stubs: STUBS, renderStubDefaultSlot: true,
    config: { errorHandler: (error) => eventErrors.push(error) } } })
  wrappers.push(wrapper)
  return wrapper
}
function byId(wrapper: VueWrapper, id: string) { return wrapper.get(`[data-testid="${id}"]`) }
async function mountWechat(): Promise<VueWrapper> {
  routing.route.query = { ch: 'wechat' }
  const wrapper = mountPage(AcctNewPage)
  await flushPromises()
  return wrapper
}
async function confirmEnable(wrapper: VueWrapper): Promise<void> {
  await byId(wrapper, 'qt-acct-new-wx-enable-module').trigger('click')
  await wrapper.get('[data-confirm]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear()
  setActivePinia(createPinia())
  routing.route = reactive({ path: '/acct/new', query: {}, params: {} })
  routing.push.mockReset(); routing.replace.mockReset(); eventErrors.length = 0
  moduleEnabled = false
  waInvoke.mockReset().mockImplementation(async (method: string) => method === 'wechat.status'
    ? { module_enabled: moduleEnabled, user_agent: true, match: 'EXACT' } : {})
  vi.stubGlobal('qt', { wa: { invoke: waInvoke } })
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network request')))
  vi.spyOn(useResourcesStore(), 'load').mockResolvedValue()
  vi.spyOn(useResourcesStore(), 'loadMetrics').mockResolvedValue()
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  vi.spyOn(commandsApi, 'deviceProfiles').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(accountsApi, 'create').mockResolvedValue(account('qidian'))
  vi.spyOn(accountsApi, 'start').mockResolvedValue({ state: 'starting' })
  vi.spyOn(accountsApi, 'get').mockImplementation(async (id) => account(id.startsWith('qq') ? 'qq' : 'qidian', 'WAIT_SMS'))
  vi.spyOn(accountsApi, 'prompt').mockResolvedValue({ kind: 'qrcode', text: 'Scan test QR', qrcode_png_b64: 'TESTQR' } as Prompt)
  vi.spyOn(accountsApi, 'webuiOpen').mockRejectedValue(new Error('QQ QR must not open WebUI'))
  vi.spyOn(accountsApi, 'screenshot').mockRejectedValue(new Error('QQ QR must not request screenshot'))
  vi.spyOn(useSettingsStore(), 'saveWechatModule').mockImplementation(async () => { moduleEnabled = true })
  vi.spyOn(useSessionStore(), 'pingAgent').mockResolvedValue()
  vi.spyOn(useEnvStore(), 'loadAll').mockResolvedValue()
  vi.spyOn(useEnvStore(), 'loadSelftest').mockResolvedValue()
  vi.spyOn(useSetupStore(), 'loadNotice').mockResolvedValue()
})
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  vi.restoreAllMocks(); vi.unstubAllGlobals()
})

describe('WeChat module enablement at first account creation', () => {
  it('cancelled confirmation performs no write and cannot continue', async () => {
    const wrapper = await mountWechat()
    await byId(wrapper, 'qt-acct-new-wx-enable-module').trigger('click')
    await wrapper.get('[data-cancel]').trigger('click')
    expect(useSettingsStore().saveWechatModule).not.toHaveBeenCalled()
    expect(byId(wrapper, NEW.next).attributes('disabled')).toBeDefined()
  })
  it('confirmed enablement rereads status before enabling the next step', async () => {
    const wrapper = await mountWechat()
    expect(byId(wrapper, NEW.next).attributes('disabled')).toBeDefined()
    await confirmEnable(wrapper)
    expect(useSettingsStore().saveWechatModule).toHaveBeenCalledTimes(1)
    expect(useSettingsStore().saveWechatModule).toHaveBeenCalledWith({ enabled: true })
    expect(waInvoke.mock.calls.filter(([method]) => method === 'wechat.status')).toHaveLength(2)
    expect(byId(wrapper, NEW.next).attributes('disabled')).toBeUndefined()
  })
  it('failed enablement stays visible and allows a fresh explicit retry', async () => {
    vi.mocked(useSettingsStore().saveWechatModule).mockRejectedValueOnce(new Error('enable failed'))
    const wrapper = await mountWechat()
    await confirmEnable(wrapper)
    expect(wrapper.get('[role="alert"]').text()).toContain('enable failed')
    expect(byId(wrapper, NEW.next).attributes('disabled')).toBeDefined()
    await confirmEnable(wrapper)
    expect(useSettingsStore().saveWechatModule).toHaveBeenCalledTimes(2)
    expect(byId(wrapper, NEW.next).attributes('disabled')).toBeUndefined()
  })
  it('offline WinAgent cannot write module settings', async () => {
    useSessionStore().winagentOnline = false
    const wrapper = await mountWechat()
    const enable = byId(wrapper, 'qt-acct-new-wx-enable-module')
    await enable.trigger('click')
    if (wrapper.find('[data-confirm]').exists()) await wrapper.get('[data-confirm]').trigger('click')
    await flushPromises()
    expect(useSettingsStore().saveWechatModule).not.toHaveBeenCalled()
    expect(byId(wrapper, NEW.next).attributes('disabled')).toBeDefined()
  })
})

describe('First setup Qidian manual verification stays inside the account flow', () => {
  it.each(['WAIT_SMS', 'WAIT_CAPTCHA', 'WAIT_DEVICE_CONFIRM'])('%s opens embedded screen without losing the created account', async (code) => {
    routing.route.query = { ch: 'qidian', from: 'setup' }
    useSetupStore().done = false
    sessionStorage.setItem('qtrade.acct-new.pending', JSON.stringify({ createdId: 'qidian-test', channel: 'qidian' }))
    vi.mocked(accountsApi.get).mockResolvedValue(account('qidian', code))
    const wrapper = mountPage(AcctNewPage)
    await flushPromises()
    await byId(wrapper, NEW.gotoScreen).trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-screen-stub]').attributes('data-account-id')).toBe('qidian-test')
    expect(wrapper.get('[data-screen-stub]').attributes('data-embedded')).toBe('true')
    expect(routing.push).not.toHaveBeenCalled()
    expect(accountsApi.create).not.toHaveBeenCalled()
    expect(accountsApi.start).not.toHaveBeenCalled()
    expect(JSON.parse(sessionStorage.getItem('qtrade.acct-new.pending')!)).toEqual({ createdId: 'qidian-test', channel: 'qidian' })
    expect(setupRedirect({ path: '/screen/qidian-test', query: {} }, { done: false, needsReack: false })).toEqual({ path: '/setup' })
  })
})

describe('QQ account detail refreshes its QR instead of requesting scrcpy', () => {
  it('refresh action obtains and renders a fresh prompt without screen or WebUI', async () => {
    const a = account('qq')
    routing.route.params = { id: a.id }
    useAccountsStore().upsert(a)
    const wrapper = mountPage(AcctDetailPage)
    await flushPromises()
    vi.mocked(accountsApi.prompt).mockClear().mockResolvedValue({ kind: 'qrcode', qrcode_png_b64: 'FRESHQR', text: 'Fresh QR' } as Prompt)
    await byId(wrapper, DETAIL.stateCardAction('refresh-qr')).trigger('click')
    await flushPromises()
    expect(accountsApi.prompt).toHaveBeenCalledTimes(1)
    expect(accountsApi.prompt).toHaveBeenCalledWith(a.id, undefined)
    expect(wrapper.get('img[alt="登录二维码"]').attributes('src')).toBe('data:image/png;base64,FRESHQR')
    expect(wrapper.find('[data-screen-stub]').exists()).toBe(false)
    expect(accountsApi.webuiOpen).not.toHaveBeenCalled()
    expect(accountsApi.screenshot).not.toHaveBeenCalled()
  })
})

describe('Notice acknowledgment reports failures and stays retryable', () => {
  it('failed acknowledgment leaves the box unchecked and next disabled, then retry succeeds', async () => {
    const store = useSetupStore()
    store.step = 0; store.noticeText = 'Test notice'; store.noticeVersion = 'test-v1'; store.scrolledToBottom = true
    const ack = vi.spyOn(systemApi, 'noticeAck').mockRejectedValueOnce(new Error('notice save failed')).mockResolvedValue({ ok: true })
    vi.spyOn(systemApi, 'notice').mockImplementation(async () => ({
      notice_version: 'test-v1', text: 'Test notice', acked_version: ack.mock.calls.length > 1 ? 'test-v1' : null,
    }))
    const wrapper = mountPage(SetupPage)
    await flushPromises()
    await byId(wrapper, SETUP.noticeAck).setValue(true)
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('notice save failed')
    expect(store.acked).toBe(false)
    expect(byId(wrapper, SETUP.next).attributes('disabled')).toBeDefined()
    expect(eventErrors).toHaveLength(0)
    await byId(wrapper, SETUP.noticeAck).setValue(false)
    await byId(wrapper, SETUP.noticeAck).setValue(true)
    await flushPromises()
    expect(ack).toHaveBeenCalledTimes(2)
    expect(store.acked).toBe(true)
    expect(byId(wrapper, SETUP.next).attributes('disabled')).toBeUndefined()
  })
})
