/**
 * 全路由冒烟:以 `console/src/router/index.ts` 为准逐个路由真打开。
 * 判据:①能打开(URL 不被守卫弹走)②没有未捕获异常 / console.error
 * ③页面主标题类元素渲染出来了(不是白屏)。
 *
 * 用 `installQtStub` 把 `setup.done` 置 true —— 浏览器形态下没有 `window.qt`,
 * `setup.done` 恒 false,任何路由都会被守卫重定向到 `/setup`(App.vue:108 / router/index.ts:32)。
 */
import { test, expect } from '@playwright/test'
import { collectErrors, installQtStub } from './helpers.mjs'

/** [路由, 该页必须出现的一段文字] */
const ROUTES = [
  ['/dash', '资源池'],
  ['/res', '资源监控'],
  ['/acct', '新增企点'],
  ['/acct/new', '新增账号'],
  ['/acct/qd01', '概览'],
  ['/screen/qd01', '性能'],
  ['/cmd', '能力'],
  ['/flow', '工作流'],
  ['/msg', '方向'],
  ['/mail', '水位与健康'],
  ['/env', '一键自检'],
  ['/set', '保留期'],
  ['/set/mail-templates', '出站模板'],
  ['/log', '指令审计'],
]

for (const [route, mustSee] of ROUTES) {
  test(`打开 ${route}:不白屏、无未捕获异常`, async ({ page }) => {
    // D-E 修好后这里不再留 404 白名单:任何 404 都算错(白名单会掩盖同类回归)
    const errors = collectErrors(page)
    await installQtStub(page)
    await page.goto(`/#${route}`)
    await expect(page.getByText(mustSee).first(), `${route} 没渲染出「${mustSee}」`).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`#${route.replace(/\//g, '\\/')}$`))
    await page.waitForTimeout(1200)
    if (route === '/screen/qd01') {
      // mock 没有画面推流 WS(`/accounts/{id}/stream`),握手 400 是 mock 的缺口,不是页面缺陷;
      // 但页面必须自己降级、不抛异常。
      expect(errors.filtered().filter((e) => !e.includes('/stream'))).toEqual([])
      return
    }
    expect(errors.filtered()).toEqual([])
  })
}

test('左侧导航 11 个入口都能点开对应页面', async ({ page }) => {
  const errors = collectErrors(page)
  await installQtStub(page)
  await page.goto('/#/dash')
  for (const seg of ['dash', 'res', 'acct', 'screen', 'cmd', 'flow', 'msg', 'mail', 'env', 'set', 'log']) {
    await page.getByTestId(`qt-shell-nav-${seg}`).click()
    await expect(page, `导航「${seg}」点不过去`).toHaveURL(new RegExp(`#\\/${seg}`))
  }
  expect(errors.filtered()).toEqual([])
})

test('顶栏告警抽屉能开能关', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/dash')
  await page.getByTestId('qt-shell-alert-bell').click()
  const drawer = page.locator('.ant-drawer-content:visible')
  await expect(drawer).toBeVisible()
  await page.locator('.ant-drawer-close').click()
  await expect(drawer).toBeHidden()
})

test('P-SET 合规块走 #86 GET /system/notice,不再调已废弃的 /settings/compliance', async ({ page }) => {
  // `/settings/compliance` 不在 #88 的 group 枚举里、真后端 404 ⇒ 合规块此前恒为「—」。
  // 现数据源 = #86(`SetPage.vue:377` systemApi.notice())。这里接管 #86 回一组特征值,
  // 断言界面显示的就是它回的东西 —— 光断「不是 —」不足以证明数据源换对了。
  await installQtStub(page, { ack: false })
  const notFound = []
  const paths = []
  page.on('response', (r) => {
    const u = r.url()
    if (!u.includes('/api/v1')) return
    paths.push(u.split('/api/v1')[1].split('?')[0])
    if (r.status() === 404) notFound.push(u.split('/api/v1')[1])
  })
  await page.route('**/api/v1/system/notice', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({
      ok: true, notice_version: 'pw-compliance-9', text: '合规告知',
      ack_ms: 1789900000000, acked_at: '2026-09-20T11:22:33+08:00', acked_version: 'pw-compliance-9',
      trace_id: 'pw',
    }),
  }))
  await page.goto('/#/set')
  await expect(page.getByTestId('qt-set-compliance-version')).toBeVisible()
  await page.waitForTimeout(1500)
  expect(notFound, '不该再有 404').toEqual([])
  expect(paths.filter((x) => x.includes('/settings/compliance')), 'SetPage 不该再 loadGroup("compliance")').toEqual([])
  expect(paths.filter((x) => x === '/system/notice').length, '合规块必须真去拉 #86').toBeGreaterThan(0)
  await expect(page.getByTestId('qt-set-compliance-version')).toContainText('pw-compliance-9')
  // 确认时间优先 ISO 的 `acked_at`(00 §6)
  await expect(page.getByTestId('qt-set-compliance-ack')).toContainText('2026-09-20')
})

test('空数据不崩:四个列表端点都回空数组时,各页渲染空态且不报错', async ({ page }) => {
  const errors = collectErrors(page)
  await installQtStub(page)
  for (const p of ['/accounts', '/sessions', '/messages', '/mail/inbox', '/mail/outbox', '/mail/pending-confirms', '/audit', '/workflows']) {
    await page.route((u) => u.pathname === `/api/v1${p}`, (route) => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, data: [], next_cursor: null, trace_id: 'pw' }),
    }))
  }
  for (const route of ['/acct', '/msg', '/mail', '/log', '/flow', '/dash']) {
    await page.goto(`/#${route}`)
    await page.waitForTimeout(1200)
    await expect(page.locator('.content, .setup').first(), `${route} 空数据下白屏了`).toBeVisible()
  }
  expect(errors.filtered()).toEqual([])
})
