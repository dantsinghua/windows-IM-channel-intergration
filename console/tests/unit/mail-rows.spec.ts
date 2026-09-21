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
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import MailPage from '@/pages/mail/MailPage.vue'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'
import type { MailInboxRow, MailOutboxRow } from '@/api/types'

vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"ok":true,"data":[]}')))
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

async function renderMail() {
  const w = shallowMount(MailPage, {
    global: {
      // 两张表都包在 `PageState` 的默认插槽里;shallowMount 会整个 stub 掉 ⇒ 插槽不渲染
      stubs: { teleport: true, PageState: { template: '<div><slot /></div>' } },
    },
  })
  useSettingsStore().mailEnabled = true
  const mail = useMailStore()
  mail.inbox = INBOX
  mail.outbox = OUTBOX
  await flushPromises()
  return w
}

describe('P-MAIL 收件列表:时间 / 发件人 / route 三列都不许空(联调 P-1)', () => {
  it('时间列显示 `received_at`,不是空白也不是毫秒整数', async () => {
    const row = (await renderMail()).find('[data-testid="qt-mail-inbox-row-0"]')
    expect(row.exists(), '收件行没渲染出来').toBe(true)
    const cells = row.findAll('td')
    expect(cells[0].text(), '时间列空了 —— 键名又对不上了(P-1 的原样)').toBe('2026-09-21T08:30:47+08:00')
    expect(cells[0].text(), '不该透出 *_ms 库列').not.toMatch(/^\d{10,}$/)
  })

  it('发件人列显示 `from_addr`', async () => {
    const cells = (await renderMail()).find('[data-testid="qt-mail-inbox-row-0"]').findAll('td')
    expect(cells[2].text()).toBe('ops@corp')
  })

  it('route 列渲染**中文显示名**,不甩后端的 scope 名', async () => {
    const cell = (await renderMail()).find('[data-testid="qt-mail-inbox-row-0-route"]')
    expect(cell.text(), 'route 原样甩了英文 scope 名').toBe('企点')
  })
})

describe('P-MAIL 发件队列:收件人 / 下次 / 失败原因 / 关联(01 §2.7.8 逐字)', () => {
  it('收件人列显示 `to`(后端 `to_addrs` 已在出参视图里改名)', async () => {
    const cells = (await renderMail()).find('[data-testid="qt-mail-outbox-row-0"]').findAll('td')
    expect(cells[1].text(), '收件人列空了').toBe('ops@corp')
  })

  it('终态行没有下次重投 ⇒ 显示「—」而不是 1970', async () => {
    const cells = (await renderMail()).find('[data-testid="qt-mail-outbox-row-0"]').findAll('td')
    expect(cells[5].text()).toBe('—')
    expect(cells[5].text()).not.toContain('1970')
  })

  it('DEAD 行要说得出为什么死(`last_error`),回执要对得回哪封来信(`ref`)', async () => {
    const cells = (await renderMail()).find('[data-testid="qt-mail-outbox-row-0"]').findAll('td')
    expect(cells[6].text()).toBe('smtp 550 mailbox unavailable')
    expect(cells[6].classes(), '失败原因该标红').toContain('qt-danger')
    expect(cells[7].text()).toBe('mi_0004')
  })
})
