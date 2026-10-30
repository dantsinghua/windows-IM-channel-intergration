/**
 * P-MSG 引用块(R6-116):01 §2.7.7「引用块」条 + §4 `qt-msg-row-{id}-reply` / `-reply-jump`;
 * 字段形状 00 §7.4 `reply`。测试数据全部为合成内容。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { useMessagesStore } from '@/stores/messages'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore } from '@/stores/events'
import { commandsApi, messagesApi } from '@/api/client'
import { msg as T } from '@/testids'
import type { Message, MessageReply } from '@/api/types'
import MsgPage from '@/pages/msg/MsgPage.vue'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { CONSOLE_ROOT, readDoc, section, splitRow, stripStrikethrough } from './doc-utils'

vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))

let wrapper: VueWrapper | undefined
let scrollSpy: ReturnType<typeof vi.fn>
let fetchSpy: ReturnType<typeof vi.fn>
const hadScrollIntoView = Object.prototype.hasOwnProperty.call(Element.prototype, 'scrollIntoView')
const originalScrollIntoView = Element.prototype.scrollIntoView

function row(id: string, extra: Partial<Message> = {}): Message {
  return {
    id, account_id: 'qd-test', channel: 'qidian', session: { id: 'qd-test:p1', name: '合成会话', kind: 'private' },
    dir: 'in', type: 'text', state: 'DELIVERED', text: `合成正文-${id}`, text_len: 8, media: [],
    sender: { id: '900001', name: '合成发送者' }, self: false,
    ts: '2026-10-28T10:00:00+08:00', received_at: '2026-10-28T10:00:05+08:00', source: 'qidian_db',
    revoked: false, reply: null, ...extra,
  }
}

function okReply(over: Partial<Omit<MessageReply, 'sender'>> & { sender?: Partial<MessageReply['sender']> } = {}): MessageReply {
  const { sender, ...rest } = over
  return {
    state: 'ok', text: '合成摘要', sender: { id: '900002', name: '合成甲', ...sender },
    ts: '2026-10-28T09:58:00+08:00', source_msg_seq: '1001', source_msg_uid: '72057594000000001',
    source_ext_msg_id: 'qd:1001', resolved_message_id: null, ...rest,
  }
}

const UNPARSED: MessageReply = {
  state: 'unparsed', text: null, sender: { id: null, name: null }, ts: null,
  source_msg_seq: null, source_msg_uid: null, source_ext_msg_id: null, resolved_message_id: null,
}

const sel = (id: string): string => `[data-testid="${id}"]`
const clone = <V>(v: V): V => JSON.parse(JSON.stringify(v)) as V

async function mountPage(rows: Message[]): Promise<VueWrapper> {
  vi.spyOn(messagesApi, 'list').mockResolvedValue({ items: rows, nextCursor: null })
  vi.spyOn(messagesApi, 'sessions').mockResolvedValue({ items: [], nextCursor: null })
  vi.spyOn(messagesApi, 'get').mockRejectedValue(new Error('unexpected GET /messages/{id}'))
  vi.spyOn(commandsApi, 'get').mockRejectedValue(new Error('unexpected GET command'))
  vi.spyOn(useAccountsStore(), 'load').mockResolvedValue()
  wrapper = shallowMount(MsgPage, { global: { renderStubDefaultSlot: true,
    stubs: { 'a-drawer': { props: ['open'], template: '<section class="test-drawer" :data-open="String(!!open)"><slot /></section>' },
      'a-button': true, 'a-input': true, 'a-select': true } } })
  await flushPromises()
  return wrapper
}

const rowEl = (w: VueWrapper, id: string) => w.get(sel(T.row(id)))
const replyEl = (w: VueWrapper, id: string) => w.get(sel(T.rowReply(id)))

/** 网络面的全部出口:fetch 与消息/命令相关 API */
function networkCalls(): number {
  return fetchSpy.mock.calls.length
    + vi.mocked(messagesApi.list).mock.calls.length
    + vi.mocked(messagesApi.get).mock.calls.length
    + vi.mocked(messagesApi.sessions).mock.calls.length
    + vi.mocked(commandsApi.get).mock.calls.length
}
function resetNetworkCounters(): void {
  fetchSpy.mockClear()
  for (const fn of [messagesApi.list, messagesApi.get, messagesApi.sessions, commandsApi.get]) vi.mocked(fn).mockClear()
}

beforeEach(() => {
  setActivePinia(createPinia())
  fetchSpy = vi.fn().mockRejectedValue(new Error('Real HTTP forbidden'))
  vi.stubGlobal('fetch', fetchSpy)
  scrollSpy = vi.fn()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: scrollSpy, configurable: true, writable: true })
})
afterEach(() => {
  wrapper?.unmount(); wrapper = undefined
  vi.restoreAllMocks(); vi.unstubAllGlobals()
  if (hadScrollIntoView) Object.defineProperty(Element.prototype, 'scrollIntoView', { value: originalScrollIntoView, configurable: true, writable: true })
  else delete (Element.prototype as unknown as Record<string, unknown>).scrollIntoView
})

describe('reply=null / 键缺省:不渲染引用块', () => {
  it('reply=null 与 reply 键缺省都不出现 -reply 与 -reply-jump', async () => {
    const noKey = row('qd-nokey')
    delete (noKey as Partial<Message>).reply
    const w = await mountPage([row('qd-null', { reply: null }), noKey])
    for (const id of ['qd-null', 'qd-nokey']) {
      expect(w.find(sel(T.row(id))).exists()).toBe(true)
      expect(w.find(sel(T.rowReply(id))).exists(), `${id} 不该有引用块`).toBe(false)
      expect(w.find(sel(T.rowReplyJump(id))).exists()).toBe(false)
    }
    expect(w.text()).not.toContain('摘要可能不完整')
    expect(w.text()).not.toContain('引用内容无法解析')
  })

  it('WS 事件前插的行 payload 不带 reply 键也不渲染引用块', async () => {
    const w = await mountPage([])
    const store = useMessagesStore()
    store.bindEvents()
    const ev = row('qd-ev-nokey')
    delete (ev as Partial<Message>).reply
    useEventsStore().injectForTest({ event: 'message', seq: 5, account_id: ev.account_id, ts: ev.ts, payload: ev })
    await flushPromises()
    expect(w.find(sel(T.row('qd-ev-nokey'))).exists()).toBe(true)
    expect(w.find(sel(T.rowReply('qd-ev-nokey'))).exists()).toBe(false)
  })
})

describe('state="ok":文案与回退(01 §2.7.7 ①)', () => {
  it.each([
    ['name 与 text 都有', okReply(), '回复 合成甲：合成摘要'],
    ['name=null 回落 sender.id', okReply({ sender: { name: null } }), '回复 900002：合成摘要'],
    ['name 为空串同样回落 sender.id(|| 语义)', okReply({ sender: { name: '' } }), '回复 900002：合成摘要'],
    ['name 与 id 都为 null 显示「未知」', okReply({ sender: { id: null, name: null } }), '回复 未知：合成摘要'],
    ['text=null 显示「（无摘要）」', okReply({ text: null }), '回复 合成甲：（无摘要）'],
    ['text 为空串同样显示「（无摘要）」', okReply({ text: '' }), '回复 合成甲：（无摘要）'],
    ['三者全无', okReply({ text: null, sender: { id: null, name: null } }), '回复 未知：（无摘要）'],
  ])('%s', async (_label, reply, expected) => {
    const w = await mountPage([row('qd-r1', { reply })])
    const block = replyEl(w, 'qd-r1')
    expect(block.text()).toContain(expected)
    expect(block.text()).toContain('摘要可能不完整')
    expect(block.text()).not.toContain('null')
    expect(block.text()).not.toContain('undefined')
    expect(block.text()).not.toContain('引用内容无法解析')
  })

  it('引用块在正文之前,且引用内容不拼进正文展示', async () => {
    const w = await mountPage([row('qd-r1', { text: '合成正文主体', reply: okReply({ text: '被引用的合成摘要' }) })])
    const r = rowEl(w, 'qd-r1').element
    const block = replyEl(w, 'qd-r1').element
    const body = Array.from(r.querySelectorAll('*')).find(el => el.textContent?.trim() === '合成正文主体')
    expect(body, '正文元素应只含正文 text 本身').toBeTruthy()
    expect(block.contains(body!)).toBe(false)
    expect(block.compareDocumentPosition(body!) & Node.DOCUMENT_POSITION_FOLLOWING, '引用块须在正文之前').toBeTruthy()
    const outside = (r.textContent ?? '').replace(block.textContent ?? '', '')
    expect(outside, '引用摘要只在引用块里出现').not.toContain('被引用的合成摘要')
  })

  it('多行摘要:悬停可见全文(引用块内有承载全文的 title)', async () => {
    const full = '合成第一行\n合成第二行\n合成第三行\n合成第四行'
    const w = await mountPage([row('qd-r1', { reply: okReply({ text: full }) })])
    const block = replyEl(w, 'qd-r1').element
    const titled = [block, ...Array.from(block.querySelectorAll('[title]'))].map(el => el.getAttribute('title') ?? '')
    expect(titled.some(t => t.includes(full)), `title 未含全文:${JSON.stringify(titled)}`).toBe(true)
  })
})

describe('state="unparsed"(01 §2.7.7 ②)', () => {
  it('显示「引用内容无法解析」,无 -reply-jump,不按 ok 文案拼「回复 …」', async () => {
    const w = await mountPage([row('qd-u1', { reply: UNPARSED })])
    const block = replyEl(w, 'qd-u1')
    expect(block.text()).toContain('引用内容无法解析')
    expect(block.text()).not.toContain('回复 ')
    expect(w.find(sel(T.rowReplyJump('qd-u1'))).exists()).toBe(false)
  })
})

describe('跳转:resolved_message_id 与当前列表(01 §2.7.7 / §4 -reply-jump)', () => {
  it('resolved_message_id=null:无 -reply-jump', async () => {
    const w = await mountPage([row('qd-a', { reply: okReply({ resolved_message_id: null }) }), row('qd-b')])
    expect(w.find(sel(T.rowReply('qd-a'))).exists()).toBe(true)
    expect(w.find(sel(T.rowReplyJump('qd-a'))).exists()).toBe(false)
  })

  it('resolved 指向不在当前列表的消息:无 -reply-jump;点引用块不滚动、不为找原消息发任何请求', async () => {
    const w = await mountPage([row('qd-a', { reply: okReply({ resolved_message_id: 'qd-absent' }) }), row('qd-b')])
    expect(w.find(sel(T.rowReplyJump('qd-a'))).exists()).toBe(false)
    resetNetworkCounters()
    await replyEl(w, 'qd-a').trigger('click')
    await flushPromises()
    expect(scrollSpy).not.toHaveBeenCalled()
    expect(networkCalls(), '不可跳转时也不得额外请求').toBe(0)
    expect(vi.mocked(messagesApi.get)).not.toHaveBeenCalled()
  })

  it('目标在当前列表:出现 -reply-jump;点击滚动并高亮目标行,不发请求、不打开详情', async () => {
    const w = await mountPage([
      row('qd-a', { reply: okReply({ resolved_message_id: 'qd-target' }) }),
      row('qd-mid'),
      row('qd-target', { text: '合成原消息全文' }),
    ])
    const store = useMessagesStore()
    const jump = w.get(sel(T.rowReplyJump('qd-a')))
    expect(replyEl(w, 'qd-a').element.contains(jump.element), '-reply-jump 属于引用块').toBe(true)
    const before = Object.fromEntries(['qd-a', 'qd-mid', 'qd-target'].map(id => [id, rowEl(w, id).classes().slice()]))
    resetNetworkCounters()

    await jump.trigger('click')
    await flushPromises()

    expect(scrollSpy).toHaveBeenCalled()
    const scrolled = scrollSpy.mock.contexts as Element[]
    expect(scrolled.every(el => el === rowEl(w, 'qd-target').element), '只滚动到目标行').toBe(true)
    const added = rowEl(w, 'qd-target').classes().filter(c => !before['qd-target'].includes(c))
    expect(added.length, '目标行应被高亮(新增样式类)').toBeGreaterThan(0)
    for (const id of ['qd-a', 'qd-mid']) {
      expect(rowEl(w, id).classes().filter(c => added.includes(c)), `${id} 不该被高亮`).toEqual([])
    }
    expect(networkCalls(), '跳转不得发任何网络请求').toBe(0)
    expect(store.selectedId, '点跳转不得选中/打开详情').toBeNull()
    expect(w.get('.test-drawer').attributes('data-open')).toBe('false')
  })

  it('目标稍后经 WS 事件进入当前列表后,引用块变为可跳转', async () => {
    const w = await mountPage([row('qd-a', { reply: okReply({ resolved_message_id: 'qd-late-target' }) })])
    expect(w.find(sel(T.rowReplyJump('qd-a'))).exists()).toBe(false)
    useMessagesStore().bindEvents()
    const target = row('qd-late-target')
    useEventsStore().injectForTest({ event: 'message', seq: 7, account_id: target.account_id, ts: target.ts, payload: target })
    await flushPromises()
    expect(w.find(sel(T.rowReplyJump('qd-a'))).exists()).toBe(true)
  })
})

describe('WS 事件前插与 GET /messages 拉回同形、落库(刷新不丢)', () => {
  it('同形 reply:事件行与拉回行渲染一致;重拉后事件行的引用块仍在', async () => {
    const reply = okReply({ text: '合成共同摘要', resolved_message_id: 'qd-target' })
    const target = row('qd-target')
    const fromGet = row('qd-get', { reply })
    const w = await mountPage([fromGet, target])
    const store = useMessagesStore()
    store.bindEvents()

    const fromEvent = row('qd-evt', { reply: clone(reply) })
    useEventsStore().injectForTest({ event: 'message', seq: 9, account_id: fromEvent.account_id, ts: fromEvent.ts, payload: fromEvent })
    await flushPromises()
    expect(store.items.map(m => m.id)).toContain('qd-evt')

    const snapshot = (id: string) => {
      const block = replyEl(w, id)
      return {
        text: block.text(),
        titles: [block.element, ...Array.from(block.element.querySelectorAll('[title]'))].map(el => el.getAttribute('title')),
        jump: w.find(sel(T.rowReplyJump(id))).exists(),
      }
    }
    const getView = snapshot('qd-get')
    expect(getView.jump).toBe(true)
    expect(snapshot('qd-evt')).toEqual(getView)

    // 重拉:后端把事件行落库后同形返回
    vi.mocked(messagesApi.list).mockResolvedValue({ items: [row('qd-evt', { reply: clone(reply) }), fromGet, target], nextCursor: null })
    await store.search(true)
    await flushPromises()
    expect(snapshot('qd-evt')).toEqual(getView)
    expect(snapshot('qd-get')).toEqual(getView)
  })

  it('unparsed 经事件与经 GET 渲染一致', async () => {
    const w = await mountPage([row('qd-get-u', { reply: UNPARSED })])
    useMessagesStore().bindEvents()
    const ev = row('qd-evt-u', { reply: { ...UNPARSED, sender: { id: null, name: null } } })
    useEventsStore().injectForTest({ event: 'message', seq: 11, account_id: ev.account_id, ts: ev.ts, payload: ev })
    await flushPromises()
    expect(replyEl(w, 'qd-evt-u').text()).toBe(replyEl(w, 'qd-get-u').text())
    expect(w.find(sel(T.rowReplyJump('qd-evt-u'))).exists()).toBe(false)
  })
})

describe('testid 取值与 01 §4 一致', () => {
  const docIds = (() => {
    const sec = section(readDoc('01-控制台前端设计.md'), '**全局外框(shell)**', /^## 5\. /)
    const out: string[] = []
    for (const line of sec.split('\n')) {
      if (!line.trim().startsWith('|')) continue
      const first = stripStrikethrough(splitRow(line)[1] ?? '')
      for (const m of first.matchAll(/`([^`]+)`/g)) out.push(m[1].trim())
    }
    return out
  })()

  it.each([
    ['qt-msg-row-{id}-reply', T.rowReply],
    ['qt-msg-row-{id}-reply-jump', T.rowReplyJump],
  ] as const)('%s 在 §4 元素表首列,且工厂产出逐字一致', (template, factory) => {
    expect(docIds).toContain(template)
    for (const id of ['msg_x1', 'qd-test-7']) expect(factory(id)).toBe(template.replace('{id}', id))
  })
})

describe('mock 消息的 reply 形状(00 §7.4)', () => {
  const REPLY_KEYS = ['resolved_message_id', 'sender', 'source_ext_msg_id', 'source_msg_seq', 'source_msg_uid', 'state', 'text', 'ts']

  it('键恒在;对象九值键齐全;非 state 值为字符串或 null;unparsed 其余全 null;QQ/微信恒 null', async () => {
    const mockPath = pathToFileURL(join(CONSOLE_ROOT, 'mock', 'data.mjs')).href
    const { makeMessages } = await import(/* @vite-ignore */ mockPath)
    const { messages } = makeMessages(40) as { messages: Array<Record<string, unknown>> }
    const ids = new Set(messages.map(m => String(m.id)))
    let nonNull = 0
    for (const m of messages) {
      expect('reply' in m, `${m.id} 缺 reply 键`).toBe(true)
      const r = m.reply as Record<string, unknown> | null
      if (m.channel !== 'qidian') { expect(r, `${m.id} 非企点应为 null`).toBeNull(); continue }
      if (r === null) continue
      nonNull += 1
      expect(Object.keys(r).sort()).toEqual(REPLY_KEYS)
      expect(['ok', 'unparsed']).toContain(r.state)
      const sender = r.sender as Record<string, unknown>
      expect(sender && typeof sender === 'object', 'sender 恒为对象').toBe(true)
      expect(Object.keys(sender).sort()).toEqual(['id', 'name'])
      const values = [r.text, r.ts, r.source_msg_seq, r.source_msg_uid, r.source_ext_msg_id, r.resolved_message_id, sender.id, sender.name]
      for (const v of values) expect(v === null || typeof v === 'string', `${m.id} 值须为字符串或 null:${String(v)}`).toBe(true)
      if (r.state === 'unparsed') expect(values.every(v => v === null), `${m.id} unparsed 其余须全 null`).toBe(true)
      if (typeof r.source_ext_msg_id === 'string') expect(r.source_ext_msg_id).toMatch(/^qd:/)
      if (typeof r.resolved_message_id === 'string') expect(ids.has(r.resolved_message_id), 'resolved 应指向 messages.id').toBe(true)
      if (typeof r.text === 'string' && typeof m.text === 'string') expect(m.text).not.toContain(r.text)
    }
    expect(nonNull, 'mock 至少应有一条企点引用样例,否则本用例空转').toBeGreaterThan(0)
  })
})
