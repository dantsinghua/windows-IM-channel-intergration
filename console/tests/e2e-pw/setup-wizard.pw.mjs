/**
 * `P-SETUP` 首次启动向导的真点击用例(01 §2.7.1)。
 *
 * 🔴 浏览器形态(`dev:web`)下没有 `window.qt` ⇒ `setup.done` 恒 false ⇒ 每次加载都进向导,
 * 正好是安琳实测时的现场,这里不装 qt 桩。
 *
 * 2026-10-10 R6-84(安琳):原第 1 步「阅读须知」整步删除,向导为四步「连接 → 自检 → 首登(可跳过)→ 完成」;
 * 守卫不再看告知确认,告知改版重勾(原 05 §6.1 末句)随之退役。原 D-A / #87 勾选 / 告知改版 / 告知拉不到四类用例删除,
 * 使用告知只保留偏好页「查看使用告知」一条只读用例。
 */
import { test, expect } from '@playwright/test'
import { collectErrors, currentStep, installQtStub, walkWizardToDash } from './helpers.mjs'

/** 接管 #86,仅供偏好页「查看使用告知」用例;向导本身不再请求它 */
async function freshNotice(page, text, { version = 'pw-v1', acked = true } = {}) {
  const acks = []
  await page.route('**/api/v1/system/notice', async (route) => {
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, ack_ms: acked ? 1789900000000 : null, acked_at: acked ? '2026-09-21T00:00:00.000Z' : null,
        acked_version: acked ? version : null, notice_version: version, text, trace_id: 'pw' }),
    })
  })
  await page.route('**/api/v1/system/notice/ack', async (route) => {
    acks.push(route.request().postDataJSON())
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, trace_id: 'pw' }) })
  })
  return acks
}

/**
 * 🔴 用例隔离:本文件每条用例都要自带干净初态,不吃 mock 进程里别的用例留下的全局状态。
 * - 自检 #79:mock 的「最近一轮」是进程内全局的(别的用例 POST 过就变),这里默认把 GET 接成一轮全绿,
 *   需要特定结果的用例(D-C / 红项阻断)自己再 `route`,后注册的优先生效;
 * - 本机存储:Playwright 每条用例是新 browser context,localStorage/sessionStorage 天然为空。
 */
test.beforeEach(async ({ page, context }) => {
  expect(context.pages().length, '每条用例应从一个全新的 context 开始').toBe(1)
  await page.route('**/api/v1/system/selftest*', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true, trace_id: 'pw',
        data: {
          run_id: 'pw-green', redroid_boot_ms: 8000, napcat_ok: true, winagent_ok: true,
          probes: [{ side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null }],
          started_at: null, finished_at: null, skipped: [],
        },
      }),
    })
  })
})

const SHORT_NOTICE = '1. 本软件通过自动化方式操作企点/QQ/微信客户端。\n2. 每个账号须为使用方自有。'

test('R6-84:向导第 1 步就是「连接服务」,没有告知勾选,也不请求 #86/#87', async ({ page }) => {
  const errors = collectErrors(page)
  const noticeHits = []
  page.on('request', (r) => { if (r.url().includes('/api/v1/system/notice')) noticeHits.push(r.url()) })
  await page.goto('/')
  await expect(page).toHaveURL(/#\/setup/)
  await expect(page.getByTestId('qt-setup-conn-agent')).toBeVisible()
  await expect(page.getByTestId('qt-setup-notice-ack')).toHaveCount(0)
  await expect(page.getByTestId('qt-setup-notice-text')).toHaveCount(0)
  await expect(page.getByTestId('qt-setup-steps').locator('.ant-steps-item')).toHaveCount(4)
  await expect(page.getByTestId('qt-setup-steps')).not.toContainText('阅读须知')
  expect(await currentStep(page)).toBe(0)
  await page.waitForTimeout(500)
  expect(noticeHits, '向导不再碰告知接口').toEqual([])
  expect(errors).toEqual([])
})

test('四步能一路走通:连接 → 自检 → 首登(跳过)→ 完成 → P-DASH', async ({ page }) => {
  const errors = collectErrors(page)
  await page.goto('/')
  await expect(page.getByTestId('qt-setup-conn-agent')).toContainText('已连接')
  await page.getByTestId('qt-setup-next').click()
  await expect(page.getByTestId('qt-setup-selfcheck-table')).toBeVisible()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-login-skip').click()
  await expect(page.getByTestId('qt-setup-finish')).toBeVisible()
  await page.getByTestId('qt-setup-finish').click()
  await expect(page).toHaveURL(/#\/dash/)
  expect(errors).toEqual([])
})

test('自检有红项时「下一步」必须禁用(01:352)', async ({ page }) => {
  await page.route('**/api/v1/system/selftest*', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true, trace_id: 'pw',
        data: {
          run_id: 'pw1', redroid_boot_ms: 8200, napcat_ok: true, winagent_ok: false,
          probes: [{ side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null }],
          started_at: null, finished_at: null, skipped: [],
        },
      }),
    })
  })
  await page.goto('/')
  await page.getByTestId('qt-setup-next').click()
  // 向导进第 2 步时并不会自动拉最近一轮自检(表里是「还没有自检结果」),得先点「运行自检」
  await page.getByTestId('qt-setup-selfcheck-run').click()
  await expect(page.getByTestId('qt-setup-selfcheck-table')).toContainText('未通过')
  await expect(page.getByTestId('qt-setup-next'), '红项必须拦住').toBeDisabled()
})

test('D-B:重跑向导时离开到 P-ACCT-NEW 再返回,仍回到首登那一步(01:353)', async ({ page }) => {
  // 用 qt 桩把 `setup.done` 置 true(= P-SET 重新运行向导之外、已完成机器上的向导),这样进出向导不受守卫限制。
  // 向导的 step 唯一真值在 `stores/setup.ts` 的 `step`,重挂后必须还在原步。
  await installQtStub(page, { ack: false })
  await page.goto('/#/setup')
  for (let i = 0; i < 2; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(2)
  await page.getByTestId('qt-setup-login-qidian-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page), '01:353「完成后回到本步」').toBe(2)
})

test('D-D:首启态(向导未完成)下首登「现在添加」能进 P-ACCT-NEW(01:353)', async ({ page }) => {
  // 不装 qt 桩 = 浏览器形态 `setup.done` 恒 false = 真正的首启态,正是守卫最严的那一路。
  await page.goto('/')
  for (let i = 0; i < 2; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(2)
  await page.getByTestId('qt-setup-login-qidian-add').click()
  await expect(page, '01:353 要求进 P-ACCT-NEW 对应分支').toHaveURL(/#\/acct\/new/)
  // 守卫的窄例外三条判据里有两条写在 URL 上,必须真的带出来(`router/index.ts` setupRedirect)
  await expect(page).toHaveURL(/from=setup/)
  await expect(page).toHaveURL(/ch=qidian/)
  // 真渲染了建号页,而且是**企点那一支**:`?ch=qidian` 让它跳过通道选择直接进第 1 步「资源预检」
  await expect(page.getByTestId('qt-acct-new-steps'), '不能只是改了地址栏而没真渲染建号页').toBeVisible()
  await expect(page.getByTestId('qt-acct-new-res-card')).toBeVisible()
  await expect(page.getByText('资源预检').first()).toBeVisible()
})

/**
 * 造一轮自检结果:`POST` 交给 mock,只接管拉结果的 `GET`(与「红项阻断」那条同手法)。
 * 这样「这一轮里有哪些 probe」是用例说了算,判据确定,不随 mock 数据漂移。
 */
async function stubSelftest(page, probes) {
  await page.route('**/api/v1/system/selftest*', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true, trace_id: 'pw',
        data: {
          run_id: 'pw-sk', redroid_boot_ms: 8200, napcat_ok: true, winagent_ok: true,
          probes, started_at: null, finished_at: null, skipped: [],
        },
      }),
    })
  })
}

async function gotoSelfcheckStep(page) {
  await page.goto('/')
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-selfcheck-run').click()
}

test('D-C:整轮只有 SKIPPED 时显示灰档「未探测」—— 不显黄、不显 ✔、不甩英文枚举、不阻断', async ({ page }) => {
  // detail 取真后端会给的两个已登记枚举(04:1001 `not_configured` / 04:740 `agent_probe_disabled`),
  // 它们同样必须走中文对照表,不能原文甩到界面上。
  await stubSelftest(page, [
    { side: 'wsl', target: 'apk_url', status: 'SKIPPED', level_reached: null, detail: 'not_configured' },
    { side: 'win', target: 'mail_pop3', status: 'SKIPPED', level_reached: null, detail: 'agent_probe_disabled' },
  ])
  await gotoSelfcheckStep(page)
  const row = page.getByTestId('qt-setup-selfcheck-table').locator('tr', { hasText: '连通性探测' })
  await expect(row).toBeVisible()
  // 01:694 探测结论对照表:`SKIPPED` → 灰色;C-18(01:119)文案「未探测(目标未配置或 Agent 不可达)」
  await expect(row.locator('.qt-warn'), '只有 SKIPPED 的一轮不该显示 ⚠').toHaveCount(0)
  await expect(row.locator('.qt-danger')).toHaveCount(0)
  await expect(row.locator('.qt-ok'), '「没探测」显示成 ✔ = 界面说假话(P-ENV 曾犯)').toHaveCount(0)
  await expect(row.locator('td.qt-muted'), '灰档的结果列必须明确表示未探测').toHaveText('未探测')
  await expect(row).toContainText('未探测')
  // 01 §2.9 约定 6:界面文案走中文对照,不得把后端枚举原样甩出来
  await expect(row).not.toContainText('SKIPPED')
  await expect(row).not.toContainText('not_configured')
  await expect(row).not.toContainText('agent_probe_disabled')
  // 01:1513 M4-7:SKIPPED 不计红项,P-SETUP 自检步不因它阻断
  await expect(page.getByTestId('qt-setup-next')).toBeEnabled()
  await expect(page.locator('.verdict'), '总体结论要如实点出「未探测」而不是说成通过').toContainText('未探测')
})

test('D-C 护栏:OK 与 SKIPPED 混排时按 OK 算(不因为「有一项没测」就把整行报成黄)', async ({ page }) => {
  await stubSelftest(page, [
    { side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null },
    { side: 'win', target: 'mail_pop3', status: 'SKIPPED', level_reached: null, detail: 'not_configured' },
  ])
  await gotoSelfcheckStep(page)
  const row = page.getByTestId('qt-setup-selfcheck-table').locator('tr', { hasText: '连通性探测' })
  await expect(row.locator('.qt-warn')).toHaveCount(0)
  await expect(row.locator('td.qt-ok')).toHaveText('已通过')
  await expect(row, '没测的那项仍要如实说明,不能被 OK 吞掉').toContainText('未探测')
  await expect(page.getByTestId('qt-setup-next')).toBeEnabled()
})

// 每个失败枚举使用独立 browser context,避免上一枚举留下的向导步骤污染下一例。
for (const status of ['TCP_TIMEOUT', 'PROXY_REQUIRED', 'BLOCKED_BY_POLICY']) {
  test(`真红项 ${status} 必须判红并阻断(00 §8.5)`, async ({ page }) => {
    await stubSelftest(page, [{ side: 'wsl', target: 'apk_url', status, level_reached: null, detail: null }])
    await gotoSelfcheckStep(page)
    const row = page.getByTestId('qt-setup-selfcheck-table').locator('tr', { hasText: '连通性探测' })
    await expect(row.locator('td.qt-danger'), `${status} 必须判红`).toHaveText('未通过')
    await expect(page.getByTestId('qt-setup-next'), `${status} 必须阻断自检步(01:352)`).toBeDisabled()
  })
}

// 窄屏/宽屏各走一遍
for (const viewport of [{ width: 900, height: 600 }, { width: 1600, height: 1000 }]) {
  test(`视口 ${viewport.width}×${viewport.height} 下向导能一路走到完成`, async ({ page }) => {
    const errors = collectErrors(page)
    await page.setViewportSize(viewport)
    await page.goto('/')
    for (let i = 0; i < 2; i++) await page.getByTestId('qt-setup-next').click()
    await page.getByTestId('qt-setup-login-skip').click()
    await page.getByTestId('qt-setup-finish').click()
    await expect(page).toHaveURL(/#\/dash/)
    expect(errors).toEqual([])
  })
}

test('🔴 跑过一次引导后:刷新与「重启程序」都直达主页,不再从引导进(01:331)', async ({ page, context }) => {
  // 浏览器形态(无 `window.qt`)= 安琳看的 dev:web 现场;「向导已完成」落在本机存储,
  // 同一个 browser context 的新 page 共享它 —— 等同于关掉程序再开。
  const errors = collectErrors(page)
  await page.goto('/')
  await expect(page, '没跑过引导 ⇒ 必须先进向导(01:331 前半句)').toHaveURL(/#\/setup/)
  expect(await page.evaluate(() => localStorage.getItem('qt.setup.done')), '起点必须是干净的本机存储').not.toBe('true')
  await walkWizardToDash(page)
  // 「完成」要真落到本机存储上才算数(不是只跳了个页面);等它落下再刷新,免得刷新抢在写入之前
  await expect.poll(() => page.evaluate(() => localStorage.getItem('qt.setup.done'))).toBe('true')

  // ① 刷新(F5)
  await page.reload()
  await expect(page, '刷新后又被带回引导 = 安琳报的那个问题').toHaveURL(/#\/dash/)

  // ② 刷新后直接开别的页也不该被守卫打回
  await page.goto('/#/acct')
  await expect(page).toHaveURL(/#\/acct$/)

  // ③ 同一 context 新开一个 page = 重启程序后的新窗口
  const fresh = await context.newPage()
  await fresh.goto('/')
  await expect(fresh, '01:331「完成后不再出现」').toHaveURL(/#\/dash/)
  await fresh.goto('/#/set')
  await expect(fresh).toHaveURL(/#\/set$/)
  await fresh.close()
  expect(errors).toEqual([])
})

test('R6-81 偏好页只读查看告知,已完成向导不会被重置', async ({ page }) => {
  await installQtStub(page, { ack: false })
  await freshNotice(page, SHORT_NOTICE, { acked: true })
  await page.goto('/#/set')
  await expect(page.getByTestId('qt-set-rerun-setup')).toHaveCount(0)
  await page.getByRole('button', { name: '查看使用告知' }).click()
  await expect(page.locator('.notice-text')).toHaveText(SHORT_NOTICE)
  expect(await page.evaluate(() => window.__pwQtCalls.filter(c => c.name === 'config.patch'))).toEqual([])
  await page.locator('.ant-modal-close').click()
  await page.reload()
  await expect(page).toHaveURL(/#\/set/)
  await page.goto('/#/dash')
  await expect(page).toHaveURL(/#\/dash/)
})

test('R6-84:已完成向导的机器即使这一版告知没勾过,也不会被带回向导', async ({ page }) => {
  await installQtStub(page, { ack: false })
  await freshNotice(page, SHORT_NOTICE, { version: 'pw-v2', acked: false })
  await page.goto('/#/dash')
  await page.waitForTimeout(800)
  await expect(page, '告知改版重勾已随「阅读须知」步退役').toHaveURL(/#\/dash/)
  await page.goto('/#/acct')
  await expect(page).toHaveURL(/#\/acct$/)
})

test('🔴 首登「现在添加」→ 建号页 →(返回 / 去账号列表)⇒ 都回到第 3 步(01:353)', async ({ page }) => {
  await page.goto('/')
  for (let i = 0; i < 2; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(2)

  // (a) 浏览器后退
  await page.getByTestId('qt-setup-login-qq-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page), '01:353「完成后回到本步」').toBe(2)

  // (b) 建号页上的「返回列表 / 取消」跳 `/acct` —— 向导未完成,守卫照旧打回 `/setup`,
  //     而步骤存在 store 里 ⇒ 回去仍是第 3 步(不能变成「退回第 1 步重走」)
  await page.getByTestId('qt-setup-login-wechat-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goto('/#/acct')
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page)).toBe(2)
})

test('守卫的窄例外不能被借道:未完成向导时 ?from=setup 只对 P-ACCT-NEW 有效', async ({ page }) => {
  // `router/index.ts` setupRedirect:例外要「未完成 + 目标恰是 P-ACCT-NEW + ?from=setup」三条同时成立。
  for (const url of ['/#/dash?from=setup', '/#/set?from=setup', '/#/log?from=setup', '/#/acct/new', '/#/acct/new?ch=qq']) {
    await page.goto(url)
    await expect(page, `${url} 不该被放行`).toHaveURL(/#\/setup/)
  }
  // 三条全中才放行(对照组)
  await page.goto('/#/acct/new?ch=qq&from=setup')
  await expect(page).toHaveURL(/#\/acct\/new/)
})
