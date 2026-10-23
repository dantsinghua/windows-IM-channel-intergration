/**
 * C-42 游标翻页的真点击用例:账号 / 会话 / 收件 / 发件 四条**独立**翻页线。
 *
 * 手法:先从后端取一份真实的行,再由用例**自己切页**回给前端(第 1 页带 `next_cursor`,
 * 第 2 页不带),这样「加载更多」必然出现,且「该追加哪几行」是确定的 —— 不依赖 mock 的行数。
 * 只改这一次会话里的 HTTP 响应,**不改 `console/mock`**。
 *
 * 判据:点「加载更多」后 ①第一页的行原样留在前面(没被顶掉/重排)②第二页的行追加在后面
 * ③没有重复行 ④第二页请求必须带上第一页回的 `cursor`(G-16:游标只透传不构造)。
 */
import { test, expect } from '@playwright/test'
import { collectErrors, installQtStub } from './helpers.mjs'

const BASE = '/api/v1'

/**
 * 接管一个列表端点,把 `rows` 切成每页 `size` 行按游标下发。
 * 返回 `{ cursors }`:每次请求带来的 cursor(第一次应为 null)。
 */
async function paginate(page, pathname, rows, size) {
  const cursors = []
  await page.route((u) => u.pathname === `${BASE}${pathname}`, async (route) => {
    const u = new URL(route.request().url())
    const cursor = u.searchParams.get('cursor')
    cursors.push(cursor)
    const start = cursor ? Number(cursor.replace('pw-cur-', '')) : 0
    const slice = rows.slice(start, start + size)
    const end = start + size
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        data: slice,
        next_cursor: end < rows.length ? `pw-cur-${end}` : null,
        trace_id: 'pw',
      }),
    })
  })
  return cursors
}

async function fetchRows(request, path) {
  const res = await request.get(`${BASE}${path}`)
  expect(res.ok(), `取不到 ${path} 的样本数据`).toBeTruthy()
  const body = await res.json()
  const rows = Array.isArray(body.data) ? body.data : body.data?.items
  expect(Array.isArray(rows) && rows.length >= 2, `${path} 样本行不足 2 条,切不出两页`).toBeTruthy()
  return rows
}

function assertAppendOnly(before, after, label) {
  expect(after.length, `${label}:点「加载更多」后行数没增加`).toBeGreaterThan(before.length)
  expect(after.slice(0, before.length), `${label}:第一页的行被顶掉或重排了`).toEqual(before)
  expect(new Set(after).size, `${label}:出现重复行`).toBe(after.length)
}

test('P-ACCT 账号列表:加载更多只追加、不重不漏,第二页带游标', async ({ page, request }) => {
  const errors = collectErrors(page)
  const rows = await fetchRows(request, '/accounts?include_stopped=true')
  const size = Math.floor(rows.length / 2)
  await installQtStub(page)
  const cursors = await paginate(page, '/accounts', rows, size)
  await page.goto('/#/acct')

  // 账号页按通道分成三张表,列位置并不统一 ⇒ 用行上的 testid 取 id(01 §4 `qt-acct-row-{id}-detail`)
  const ids = () => page.locator('[data-testid^="qt-acct-row-"][data-testid$="-detail"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute('data-testid').slice('qt-acct-row-'.length, -'-detail'.length)))
  await expect.poll(async () => (await ids()).length).toBe(size)
  const before = await ids()
  const more = page.getByRole('button', { name: '加载更多' })
  await expect(more).toBeVisible()
  await more.click()
  await expect.poll(async () => (await ids()).length).toBe(size * 2)
  const after = await ids()
  // 账号页按通道分三张表展示 ⇒ DOM 顺序不等于下发顺序,这里按集合判「不重不漏」,
  // 顺序只在**同一通道表内部**要求与后端下发序一致(store 不自排,01 §2.5)。
  expect(new Set(after).size, '账号:出现重复行').toBe(after.length)
  for (const id of before) expect(after, `账号:第一页的 ${id} 翻页后丢了`).toContain(id)
  for (const id of rows.slice(size, size * 2).map((a) => a.id)) {
    expect(after, `账号:第二页的 ${id} 没显示出来`).toContain(id)
  }
  const backendOrder = rows.slice(0, size * 2).map((a) => a.id)
  const byChannel = new Map()
  for (const a of rows.slice(0, size * 2)) {
    if (!byChannel.has(a.channel)) byChannel.set(a.channel, [])
    byChannel.get(a.channel).push(a.id)
  }
  for (const [ch, expectIds] of byChannel) {
    const got = after.filter((id) => expectIds.includes(id))
    expect(got, `账号:${ch} 表内顺序被前端重排了(后端序 ${backendOrder.join(',')})`).toEqual(expectIds)
  }
  expect(cursors.at(-1), '第二页必须带第一页回的 cursor').toBe(`pw-cur-${size}`)
  expect(errors).toEqual([])
})

test('P-MSG 会话列表:加载更多只追加,且会话游标不串到消息线上', async ({ page, request }) => {
  const errors = collectErrors(page)
  const rows = await fetchRows(request, '/sessions')
  await installQtStub(page)
  const size = Math.floor(rows.length / 2)
  const sessCursors = await paginate(page, '/sessions', rows, size)
  const msgCursors = []
  await page.route((u) => u.pathname === `${BASE}/messages`, async (route) => {
    msgCursors.push(new URL(route.request().url()).searchParams.get('cursor'))
    await route.continue()
  })
  await page.goto('/#/msg')

  const more = page.getByRole('button', { name: '更多会话', exact: true })
  await expect(more).toBeVisible()
  await more.click()
  await expect.poll(() => sessCursors.length).toBeGreaterThan(1)
  expect(sessCursors[0], '首拉不该带游标').toBeNull()
  expect(sessCursors.at(-1)).toBe(`pw-cur-${size}`)
  expect(msgCursors.includes(`pw-cur-${size}`), '会话的游标被拿去翻消息了(两条线必须独立)').toBeFalsy()
  expect(errors).toEqual([])
})

test('P-MAIL 收件 / 发件:两条翻页线各走各的', async ({ page, request }) => {
  const errors = collectErrors(page)
  const inboxRows = await fetchRows(request, '/mail/inbox')
  const outboxRows = await fetchRows(request, '/mail/outbox')
  await installQtStub(page)
  const inSize = Math.floor(inboxRows.length / 2)
  const outSize = Math.floor(outboxRows.length / 2)
  const inboxCursors = await paginate(page, '/mail/inbox', inboxRows, inSize)
  const outboxCursors = await paginate(page, '/mail/outbox', outboxRows, outSize)
  await page.goto('/#/mail')

  const buttons = page.getByRole('button', { name: '加载更多' })
  await expect(buttons).toHaveCount(1)
  await buttons.click()
  await expect.poll(() => inboxCursors.length).toBeGreaterThan(1)
  expect(outboxCursors.filter(Boolean)).toEqual([])
  await page.getByRole('tab', { name: '发件', exact: true }).click()
  await expect(buttons).toHaveCount(1)
  await buttons.click()
  await expect.poll(() => inboxCursors.length).toBeGreaterThan(1)
  await expect.poll(() => outboxCursors.length).toBeGreaterThan(1)
  expect(inboxCursors.at(-1)).toBe(`pw-cur-${inSize}`)
  expect(outboxCursors.at(-1)).toBe(`pw-cur-${outSize}`)
  expect(errors).toEqual([])
})

test('P-LOG 审计:有下一页时「加载更多」可点,且不重复', async ({ page, request }) => {
  const errors = collectErrors(page)
  const rows = await fetchRows(request, '/audit?kind=command')
  await installQtStub(page)
  await paginate(page, '/audit', rows, 2)
  await page.goto('/#/log')

  const bodyRows = page.locator('table tbody tr')
  await expect.poll(async () => bodyRows.count()).toBe(2)
  const before = await bodyRows.allInnerTexts()
  const more = page.getByTestId('qt-log-load-more')
  await expect(more).toBeVisible()
  await more.click()
  await expect.poll(async () => bodyRows.count()).toBe(4)
  assertAppendOnly(before, await bodyRows.allInnerTexts(), '审计')
  expect(errors).toEqual([])
})
