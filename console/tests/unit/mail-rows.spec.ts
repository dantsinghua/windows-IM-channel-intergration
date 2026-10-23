/**
 * `P-MAIL` 收发件列表**真的把值显示出来**(独立联调 P-1 的前端半)。
 *
 * 缺陷现场:`#58`/`#61` 原先把库行原样透出(`received_ms`/`to_addrs`/`route_id`),
 * 而前端读的是 `received_at`/`to`/`route` ⇒ **时间列与收件人列全空**。
 * 后端第四批出了出参视图(backend-api-4 §1 P-1)之后,键名两边对上了 —— 这条测试就是把
 * 「对上了」钉住:哪天谁再把键名改回去,这里先红,而不是等用户看到一排「—」。
 *
 * 另外两条判据:
 *  - `route` 后端给的是 **scope 名**(`default`/`qidian`…),界面必须渲染成中文显示名,不许甩英文枚举;
 *  - 发件行的 `last_error`/`ref`(01 §2.7.8 逐字)要摆出来 —— DEAD 行只剩状态就看不出为什么死。
 *
 * ⚠️ 跑起来会刷一片「Failed to resolve component: a-*」—— ant-design-vue 没在测试里 `app.use()`,
 * 而 SFC 是预编译的,`compilerOptions.isCustomElement` 对它无效。这些 warn 与本文件的判据无关
 * (断的是原生 `<table>` 里的单元格),**不要**为了消 warn 去把真实组件装进来。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import MailPage from '@/pages/mail/MailPage.vue'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'
import type { MailInboxRow, MailOutboxRow } from '@/api/types'
import { mailApi } from '@/api/client'

const wrappers: VueWrapper[] = []

vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"ok":true,"data":[]}')))
  vi.spyOn(useMailStore(), 'reloadAll').mockResolvedValue()
  vi.spyOn(useMailStore(), 'startPolling').mockImplementation(() => undefined)
  vi.spyOn(useMailStore(), 'stopPolling').mockImplementation(() => undefined)
})

afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const INBOX: MailInboxRow[] = [{
  id: 'mi_0001',
  received_at: '2026-09-21T08:30:47+08:00',
  route: 'qidian',
  from_addr: 'ops@corp',
  subject: '[QTrade] 停账号',
  status: 'CONFIRM_REQUIRED',
  trace_id: '01TRACEMOCK',
  archived: false,
}]

const OUTBOX: MailOutboxRow[] = [{
  id: 'mo_3',
  kind: 'receipt',
  to: 'ops@corp',
  subject: 'Re: 查会话',
  status: 'DEAD',
  attempts: 5,
  next_attempt_at: null,
  created_at: '2026-09-21T07:10:00+08:00',
  route: 'default',
  last_error: 'smtp 550 mailbox unavailable',
  ref: 'mi_0004',
}]

async function renderMail(tab: 'inbox' | 'outbox' = 'inbox') {
  const w = shallowMount(MailPage, {
    global: {
      renderStubDefaultSlot: true,
      // 两张表都包在 `PageState` 的默认插槽里;shallowMount 会整个 stub 掉 ⇒ 插槽不渲染
      stubs: {
        teleport: true, 'a-button': true, 'a-popconfirm': true,
        'a-popover': { data: () => ({ open: false }), template: '<div><div @click="open = true"><slot /></div><div v-if="open"><slot name="content" /></div></div>' },
        PageState: { template: '<div><slot /></div>' },
      },
    },
  })
  wrappers.push(w)
  useSettingsStore().mailEnabled = true
  const mail = useMailStore()
  mail.inbox = INBOX
  mail.outbox = OUTBOX
  await flushPromises()
  if (tab === 'outbox') await w.get('#mail-tab-outbox').trigger('click')
  return w
}

describe('P-MAIL 收件列表:时间 / 发件人 / route 三列都不许空(联调 P-1)', () => {
  it('时间列显示 `received_at`,不是空白也不是毫秒整数', async () => {
    const row = (await renderMail()).find('[data-testid="qt-mail-inbox-row-0"]')
    expect(row.exists(), '收件行没渲染出来').toBe(true)
    const cell = row.get('.time-cell')
    expect(cell.text(), '时间列空了 —— 键名又对不上了(P-1 的原样)')
      .toBe(new Date(INBOX[0].received_at).toLocaleString('zh-CN', { hour12: false }))
    expect(cell.text(), '不该透出 *_ms 库列').not.toMatch(/^\d{10,}$/)
  })

  it('发件人列显示 `from_addr`', async () => {
    const row = (await renderMail()).get('[data-testid="qt-mail-inbox-row-0"]')
    expect(row.get('.row-secondary').text()).toBe('ops@corp')
  })

  it('route 列渲染**中文显示名**,不甩后端的 scope 名', async () => {
    const cell = (await renderMail()).find('[data-testid="qt-mail-inbox-row-0-route"]')
    expect(cell.text(), 'route 原样甩了英文 scope 名').toBe('企点')
  })
})

describe('P-MAIL 发件队列:收件人 / 下次 / 失败原因 / 关联(01 §2.7.8 逐字)', () => {
  it('收件人列显示 `to`(后端 `to_addrs` 已在出参视图里改名)', async () => {
    const row = (await renderMail('outbox')).get('[data-testid="qt-mail-outbox-row-0"]')
    expect(row.get('.row-secondary').text(), '收件人列空了').toBe('ops@corp')
  })

  it('终态行没有下次重投 ⇒ 不虚构重试时间或显示 1970', async () => {
    const row = (await renderMail('outbox')).get('[data-testid="qt-mail-outbox-row-0"]')
    expect(row.text()).not.toContain('下次尝试')
    expect(row.text()).not.toContain('1970')
  })

  it('DEAD 行保留失败原因，并以失败状态明确标识', async () => {
    const row = (await renderMail('outbox')).get('[data-testid="qt-mail-outbox-row-0"]')
    expect(row.get('.reason').text()).toBe('smtp 550 mailbox unavailable')
    expect(row.get('.status-chip.failure').text()).toBe('发送失败')
  })

  it('关联来源仍展示 ref，非数字来源不错误当成原邮件 ID', async () => {
    const detail = vi.spyOn(mailApi, 'inboxDetail').mockResolvedValue(INBOX[0])
    const page = await renderMail('outbox')
    const row = page.get('[data-testid="qt-mail-outbox-row-0"]')
    const source = row.findAll('button,a-button-stub,a').find((button) => /关联来源/.test(button.text()))
    expect(source, '发件记录缺少关联来源入口').toBeDefined()
    await source!.trigger('click')
    await flushPromises()
    expect(page.text()).toContain(OUTBOX[0].ref)
    expect(page.findAll('button,a-button-stub,a').some((button) => /查看原邮件/.test(button.text()))).toBe(false)
    expect(detail).not.toHaveBeenCalled()
  })
})
