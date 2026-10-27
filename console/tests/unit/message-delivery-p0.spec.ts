import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { useMessagesStore } from '@/stores/messages'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore } from '@/stores/events'
import { commandsApi, messagesApi } from '@/api/client'
import type { Message, QtEvent } from '@/api/types'
import MsgPage from '@/pages/msg/MsgPage.vue'

vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
let wrapper: VueWrapper | undefined
function msg(state: Message['state'] = 'SENDING', extra: Partial<Message> = {}): Message {
  return { id: 'm1', account_id: 'wx-test', channel: 'wechat', session: { id: 'wx-test:p', name: 'Synthetic', kind: 'group' },
    dir: 'out', type: 'text', state, text: 'Synthetic text', text_len: 14, media: [], sender: { id: 'self', name: 'Synthetic' },
    self: true, ts: '2026-10-26T10:00:00Z', received_at: '2026-10-26T10:00:00Z', source: 'chatlog',
    revoked: false, trace_id: 'trace1', ...extra }
}
function event(message: Message, seq: number): QtEvent<Message> {
  return { event: 'message', payload: message, seq, account_id: message.account_id, ts: message.ts }
}
beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Real HTTP forbidden')))
})
afterEach(() => { wrapper?.unmount(); wrapper = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each(['FAILED', 'UNCONFIRMED', 'DELIVERED'] as const)('same ID updates list and open detail to %s without pagination loss', (state) => {
  const store = useMessagesStore()
  store.items = [msg()]; store.selectedId = 'm1'; store.nextCursor = 'page2'
  store.filter = { account_id: 'wx-test', dir: 'out', limit: 50 }; store.selectedSessionId = 'wx-test:p'
  store.applyMessageEvent(event(msg(state, state === 'DELIVERED' ? { ext_msg_id: 'p:12', confirmed_by: 'chatlog' } : {}), 20))
  expect(store.items).toHaveLength(1)
  expect(store.selected?.state).toBe(state)
  expect(store.selected?.dir).toBe('out')
  expect(store.nextCursor).toBe('page2')
  expect(store.filter).toEqual({ account_id: 'wx-test', dir: 'out', limit: 50 })
  expect(store.newCount).toBe(0)
})

it('older event cannot regress a newer terminal state', () => {
  const store = useMessagesStore()
  store.applyMessageEvent(event(msg('DELIVERED', { ext_msg_id: 'p:12', confirmed_by: 'chatlog' }), 22))
  store.applyMessageEvent(event(msg('SENDING'), 21))
  expect(store.items[0].state).toBe('DELIVERED')
  expect(store.items[0].ext_msg_id).toBe('p:12')
})

it('an already listed matching search result still receives same-ID state updates', () => {
  const store = useMessagesStore()
  store.items = [msg()]; store.selectedId = 'm1'
  store.filter = { q: 'Synthetic', account_id: 'wx-test', limit: 50 }
  store.applyMessageEvent(event(msg('FAILED'), 21))
  expect(store.selected?.state).toBe('FAILED')
  expect(store.filter.q).toBe('Synthetic')
})

it('native poll row followed by trace-bound row has one visible native message', () => {
  const store = useMessagesStore()
  store.items = [msg()]; store.selectedId = 'm1'
  store.applyMessageEvent(event(msg('DELIVERED', { id: 'native-only', trace_id: null, ext_msg_id: 'p:12' }), 21))
  store.applyMessageEvent(event(msg('DELIVERED', { ext_msg_id: 'p:12', confirmed_by: 'chatlog' }), 22))
  expect(store.items.map(m => m.id)).toEqual(['m1'])
  expect(store.selected?.state).toBe('DELIVERED')
})

it('same native ID on different account never deduplicates another account message', () => {
  const store = useMessagesStore()
  store.applyMessageEvent(event(msg('DELIVERED', { id: 'a', account_id: 'wx-a', ext_msg_id: 'p:12', trace_id: null }), 21))
  store.applyMessageEvent(event(msg('DELIVERED', { id: 'b', account_id: 'wx-b', ext_msg_id: 'p:12', trace_id: 'trace-b', confirmed_by: 'chatlog' }), 22))
  expect(store.items).toHaveLength(2)
  expect(new Set(store.items.map(m => m.account_id))).toEqual(new Set(['wx-a', 'wx-b']))
})

it.each([20, 23])('native row arriving after canonical row cannot recreate a duplicate (seq %s)', (seq) => {
  const store = useMessagesStore()
  store.applyMessageEvent(event(msg('DELIVERED', { ext_msg_id: 'p:12', confirmed_by: 'chatlog' }), 22))
  store.selectedId = 'm1'
  store.applyMessageEvent(event(msg('DELIVERED', { id: 'native-only', trace_id: null, ext_msg_id: 'p:12' }), seq))
  expect(store.items.map(m => m.id)).toEqual(['m1'])
  expect(store.selected?.confirmed_by).toBe('chatlog')
  expect(store.selected?.trace_id).toBe('trace1')
})

it('late REST response cannot overwrite a newer event', async () => {
  const store = useMessagesStore()
  store.items = [msg()]
  let complete!: (v: { items: Message[]; nextCursor: null }) => void
  vi.spyOn(messagesApi, 'list').mockImplementation(() => new Promise(resolve => { complete = resolve }))
  const pending = store.search()
  store.applyMessageEvent(event(msg('FAILED'), 30))
  complete({ items: [msg()], nextCursor: null })
  await pending
  expect(store.items[0].state).toBe('FAILED')
})

it('command_done refreshes matching message without resetting filter, selection or page cursor', async () => {
  const store = useMessagesStore()
  store.items = [msg(), msg('DELIVERED', { id: 'older' })]
  store.selectedId = 'm1'; store.nextCursor = 'page3'; store.filter = { account_id: 'wx-test', q: 'Synthetic', limit: 50 }
  vi.spyOn(messagesApi, 'get').mockImplementation(async id => msg('FAILED', { id }))
  vi.spyOn(messagesApi, 'list').mockResolvedValue({ items: [msg('FAILED')], nextCursor: 'page1' })
  store.bindEvents()
  useEventsStore().injectForTest({ event: 'command_done', seq: 40, account_id: 'wx-test', ts: msg().ts,
    payload: { trace_id: 'trace1', ok: false, code: 'TARGET_NOT_FOUND', cost_ms: 1 } })
  await flushPromises()
  expect(store.selected?.state).toBe('FAILED')
  expect(store.items.map(m => m.id)).toEqual(['m1', 'older'])
  expect(store.nextCursor).toBe('page3')
  expect(store.filter.q).toBe('Synthetic')
})

it.each([
  ['SENDING', '发送中'], ['FAILED', '失败'], ['UNCONFIRMED', '待确认'], ['DELIVERED', '原生'],
] as const)('renders %s independently of outgoing direction in list and detail', async (state, label) => {
  const store = useMessagesStore()
  store.items = [msg(state, state === 'DELIVERED' ? { ext_msg_id: 'p:12', confirmed_by: 'chatlog' } : {})]
  vi.spyOn(store, 'search').mockResolvedValue()
  vi.spyOn(store, 'loadSessions').mockResolvedValue()
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  wrapper = shallowMount(MsgPage, { global: { renderStubDefaultSlot: true,
    stubs: { 'a-drawer': { template: '<section class="test-drawer"><slot /></section>' },
      'a-button': true, 'a-input': true, 'a-select': true } } })
  await flushPromises()
  const listRow = wrapper.get('.mrow')
  expect(listRow.get('.message-direction').text()).toBe('发送')
  expect(listRow.text()).toContain(label)
  await listRow.trigger('click')
  expect(wrapper.get('.test-drawer').text()).toContain(label)
  if (state !== 'DELIVERED') expect(wrapper.get('.test-drawer').text()).not.toContain('采集来源chatlog')
})

it.each([{ ext_msg_id: 'p:12' }, { confirmed_by: 'chatlog' }, {}])(
  'DELIVERED without both native ID and confirmation does not claim native confirmation: %j', async (proof) => {
    const store = useMessagesStore()
    store.items = [msg('DELIVERED', proof)]
    vi.spyOn(store, 'search').mockResolvedValue()
    vi.spyOn(store, 'loadSessions').mockResolvedValue()
    vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
    wrapper = shallowMount(MsgPage, { global: { renderStubDefaultSlot: true,
      stubs: { 'a-drawer': { template: '<section class="test-drawer"><slot /></section>' },
        'a-button': true, 'a-input': true, 'a-select': true } } })
    await flushPromises()
    expect(wrapper.get('.mrow').text()).toContain('待确认')
    await wrapper.get('.mrow').trigger('click')
    expect(wrapper.get('.test-drawer').text()).toContain('待确认')
    expect(wrapper.get('.test-drawer').text()).not.toContain('采集来源chatlog')
  },
)

it('failed message detail shows the original command refusal reason', async () => {
  const store = useMessagesStore()
  store.items = [msg('FAILED')]
  vi.spyOn(store, 'search').mockResolvedValue()
  vi.spyOn(store, 'loadSessions').mockResolvedValue()
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  vi.spyOn(commandsApi, 'get').mockResolvedValue({ command: { trace_id: 'trace1' }, result: {
    ok: false, code: 'TARGET_NOT_FOUND', trace_id: 'trace1', data: {}, cost_ms: 1,
    error: { message: 'Synthetic target has no trusted name', retryable: false, needs_human: false },
  } })
  wrapper = shallowMount(MsgPage, { global: { renderStubDefaultSlot: true,
    stubs: { 'a-drawer': { template: '<section class="test-drawer"><slot /></section>' },
      'a-button': true, 'a-input': true, 'a-select': true } } })
  await flushPromises()
  await wrapper.get('.mrow').trigger('click')
  await flushPromises()
  expect(wrapper.get('.test-drawer').text()).toContain('Synthetic target has no trusted name')
})

it('a manually sent native chatlog fact is collected evidence rather than an application send placeholder', async () => {
  const store = useMessagesStore()
  store.items = [msg('DELIVERED', { id: 'native-manual', ext_msg_id: 'p:99', trace_id: null, confirmed_by: null })]
  vi.spyOn(store, 'search').mockResolvedValue()
  vi.spyOn(store, 'loadSessions').mockResolvedValue()
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  const command = vi.spyOn(commandsApi, 'get')
  wrapper = shallowMount(MsgPage, { global: { renderStubDefaultSlot: true,
    stubs: { 'a-drawer': { template: '<section class="test-drawer"><slot /></section>' },
      'a-button': true, 'a-input': true, 'a-select': true } } })
  await flushPromises()
  expect(wrapper.get('.mrow').text()).not.toContain('待确认')
  expect(wrapper.get('.mrow').text()).toContain('原生')
  await wrapper.get('.mrow').trigger('click')
  expect(wrapper.get('.test-drawer').text()).not.toContain('待确认')
  expect(wrapper.get('.test-drawer').text()).not.toContain('发送占位')
  expect(command).not.toHaveBeenCalled()
})
