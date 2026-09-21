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
    // P-SET 的 `/settings/compliance` 404 是已知缺陷,由本文件末尾那条专门用例盯着
    const errors = collectErrors(page, { allow404: ['/settings/compliance'] })
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
  const errors = collectErrors(page, { allow404: ['/settings/compliance'] })
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

test.fail('P-SET 不该再调已废弃的 /settings/compliance(真后端 404,合规块恒为空)', async ({ page }) => {
  await installQtStub(page)
  const notFound = []
  page.on('response', (r) => { if (r.status() === 404) notFound.push(r.url().split('/api/v1')[1] ?? r.url()) })
  await page.goto('/#/set')
  await expect(page.getByTestId('qt-set-compliance-version')).toBeVisible()
  await page.waitForTimeout(1500)
  expect(notFound, 'SetPage.vue:270 仍在 loadGroup("compliance")').toEqual([])
  // 合规块应显示真实的告知版本/确认时间(数据源 = #86 GET /system/notice)
  await expect(page.getByTestId('qt-set-compliance-version')).not.toContainText('—')
})

test('空数据不崩:四个列表端点都回空数组时,各页渲染空态且不报错', async ({ page }) => {
  const errors = collectErrors(page, { allow404: ['/settings/compliance'] })
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
