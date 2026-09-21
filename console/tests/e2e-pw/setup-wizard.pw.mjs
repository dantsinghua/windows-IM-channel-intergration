/**
 * `P-SETUP` 首次启动向导的真点击用例(01 §2.7.1,docs/01-控制台前端设计.md:329-356)。
 *
 * 🔴 浏览器形态(`dev:web`)下没有 `window.qt` ⇒ `setup.done` 恒 false ⇒ 每次加载都进向导,
 * 正好是安琳实测时的现场,这里不装 qt 桩。
 *
 * 2026-09-21 复测:D-B / D-C / D-D 三条原先的 `test.fail`(已知缺陷)已由第五批修复,
 * 标记全部摘掉并按规格收紧为正向断言;本文件不再有 `test.fail`。
 */
import { test, expect } from '@playwright/test'
import { collectErrors, currentStep, installQtStub, walkWizardToDash } from './helpers.mjs'

/**
 * 一份「这一版告知勾没勾过」的可共享状态。
 * 同一个对象可以挂到**多个 page** 上(见「重开程序」那条用例:新开的 page = 重启后的新窗口,
 * 它看到的后端状态必须与上一个 page 留下的一致,而不是各自一份)。
 */
function noticeState({ version = 'pw-v1', acked = false } = {}) {
  return { version, acked, acks: [] }
}

/**
 * 接管 #86 `GET /system/notice` 与 #87 `POST /system/notice/ack`,在用例侧维护勾选状态。
 * mock 的勾选状态是进程内全局的、会被别的用例污染,所以向导用例一律自带状态,
 * 语义与真后端一致(`acked_version === notice_version` 才算这一版勾过,05 §6.1)。
 * 返回 `state.acks`,用例可断言真发了 #87、且带对了 `notice_version`。
 */
async function freshNotice(page, text, state = noticeState()) {
  await page.route('**/api/v1/system/notice', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        ack_ms: state.acked ? 1789900000000 : null,
        acked_at: state.acked ? '2026-09-21T00:00:00.000Z' : null,
        acked_version: state.acked ? state.version : null,
        notice_version: state.version,
        text,
        trace_id: 'pw',
      }),
    })
  })
  await page.route('**/api/v1/system/notice/ack', async (route) => {
    state.acks.push(route.request().postDataJSON())
    state.acked = true
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, trace_id: 'pw' }) })
  })
  return state.acks
}

/**
 * 🔴 用例隔离(总控复跑发现全量里 :291 偶发超时、单跑通过):本文件每条用例都要自带干净初态,
 * 不吃 mock 进程里别的用例留下的全局状态。
 * - 告知 #86/#87:每条用例用 `freshNotice` 自带状态(见上);
 * - 自检 #79:mock 的「最近一轮」是进程内全局的(别的用例 POST 过就变),这里默认把 GET 接成一轮全绿,
 *   需要特定结果的用例(D-C / 红项阻断)自己再 `route`,后注册的优先生效;
 * - 本机存储:Playwright 每条用例是新 browser context,localStorage/sessionStorage 天然为空,
 *   这里再显式断言一次,免得哪天有人改成共享 context 而没人发现。
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
const LONG_NOTICE = Array.from({ length: 60 }, (_, i) => `${i + 1}. 这是一条很长的合规告知,用来把须知框撑出滚动条。`).join('\n')

test('D-A:须知不足一屏(容器不溢出)时,勾选框必须可用,向导不能卡死', async ({ page }) => {
  const errors = collectErrors(page)
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await expect(page).toHaveURL(/#\/setup/)
  await expect(page.getByTestId('qt-setup-notice-text')).toContainText('本软件通过自动化方式')

  const metrics = await page.locator('.notice').evaluate((el) => ({ sh: el.scrollHeight, ch: el.clientHeight }))
  expect(metrics.sh, '本用例要的就是「不溢出」这一路;撑出滚动条就测不到 D-A 了').toBeLessThanOrEqual(metrics.ch + 1)

  const ack = page.getByTestId('qt-setup-notice-ack')
  await expect(ack, '全文已可见 = 视同读到底(01:350 判据的本意)').toBeEnabled()
  await ack.click()
  await expect(ack).toBeChecked()
  await expect(page.getByTestId('qt-setup-next')).toBeEnabled()
  expect(errors).toEqual([])
})

test('D-A 反面:须知撑出滚动条时,没滚到底不许勾选,滚到底后解禁', async ({ page }) => {
  await freshNotice(page, LONG_NOTICE)
  await page.goto('/')
  await expect(page.getByTestId('qt-setup-notice-text')).toContainText('这是一条很长的合规告知')
  const metrics = await page.locator('.notice').evaluate((el) => ({ sh: el.scrollHeight, ch: el.clientHeight }))
  expect(metrics.sh).toBeGreaterThan(metrics.ch)

  const ack = page.getByTestId('qt-setup-notice-ack')
  await expect(ack, '没滚到底就能勾 = 合规判据形同虚设').toBeDisabled()
  await page.locator('.notice').evaluate((el) => { el.scrollTop = el.scrollHeight })
  await expect(ack).toBeEnabled()
})

test('勾选走的是 #87 /system/notice/ack,且以 Agent 回的 acked_version 为准', async ({ page }) => {
  const acks = await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect.poll(() => acks.length).toBe(1)
  expect(acks[0].notice_version).toBe('pw-v1')
})

test('五步能一路走通:告知 → 连接 → 自检 → 首登(跳过)→ 完成 → P-DASH', async ({ page }) => {
  const errors = collectErrors(page)
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  await page.getByTestId('qt-setup-next').click()
  await expect(page.getByTestId('qt-setup-conn-agent')).toContainText('可达')
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
  await freshNotice(page, SHORT_NOTICE)
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
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-next').click()
  // 向导进第 3 步时并不会自动拉最近一轮自检(表里是「还没有自检结果」),得先点「运行自检」
  await page.getByTestId('qt-setup-selfcheck-run').click()
  await expect(page.getByTestId('qt-setup-selfcheck-table')).toContainText('✗')
  await expect(page.getByTestId('qt-setup-next'), '红项必须拦住').toBeDisabled()
})

// ── 以下是已确认的缺陷,修好之前「失败 = 符合预期」 ────────────────────────────

test('D-B:重跑向导时离开到 P-ACCT-NEW 再返回,仍回到首登那一步(01:353)', async ({ page }) => {
  // 用 qt 桩把 `setup.done` 置 true(= P-SET 重新运行向导之外、已完成机器上的向导),这样进出向导不受守卫限制。
  // 安琳现场是 vite HMR 触发的重挂,机制相同:向导的 step 若只活在组件内存里(旧 SetupPage.vue:24),
  // 组件一重挂就回第 1 步。现在 step 的唯一真值在 `stores/setup.ts` 的 `step`,重挂后必须还在原步。
  // 告知按「这一版已勾过」造:否则 `reackRequired`(05 §6.1)会把向导变成「只重勾一次即进控制台」那一路,
  // 走不到第 4 步 —— 那条路由另一条专门的用例守着。
  await installQtStub(page, { ack: false })
  await freshNotice(page, SHORT_NOTICE, noticeState({ acked: true }))
  await page.goto('/#/setup')
  const ack = page.getByTestId('qt-setup-notice-ack')
  await expect(ack, '桩造的初态就是「已勾过」,以 Agent 的 acked_version 为准(01:350)').toBeChecked()
  for (let i = 0; i < 3; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(3)
  await page.getByTestId('qt-setup-login-qidian-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page), '01:353「完成后回到本步」').toBe(3)
})

test('D-D:首启态(向导未完成)下首登「现在添加」能进 P-ACCT-NEW(01:353)', async ({ page }) => {
  // 不装 qt 桩 = 浏览器形态 `setup.done` 恒 false = 真正的首启态,正是守卫最严的那一路。
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  for (let i = 0; i < 3; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(3)
  await page.getByTestId('qt-setup-login-qidian-add').click()
  await expect(page, '01:353 要求进 P-ACCT-NEW 对应分支').toHaveURL(/#\/acct\/new/)
  // 守卫的窄例外三条判据里有两条写在 URL 上,必须真的带出来(`router/index.ts` setupRedirect)
  await expect(page).toHaveURL(/from=setup/)
  await expect(page).toHaveURL(/ch=qidian/)
  // 真渲染了建号页,而且是**企点那一支**:`?ch=qidian` 让它跳过通道选择直接进第 1 步「资源预检」
  // (`AcctNewPage.vue` 的 resCard `v-if="channel !== 'wechat' && step === 0"`)——
  // 01:353 要的就是「进 P-ACCT-NEW **对应分支**」,光有六步条不足以证明分支对了。
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
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-selfcheck-run').click()
}

test('D-C:整轮只有 SKIPPED 时显示灰档「未探测」—— 不显黄、不显 ✔、不甩英文枚举、不阻断', async ({ page }) => {
  await freshNotice(page, SHORT_NOTICE)
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
  await expect(row.locator('td.qt-muted'), '灰档的结果列是「—」,既不是 ✔ 也不是 ⚠').toHaveText('—')
  await expect(row).toContainText('未探测')
  // 01 §2.9 约定 6:界面文案走中文对照,不得把后端枚举原样甩出来
  await expect(row).not.toContainText('SKIPPED')
  await expect(row).not.toContainText('not_configured')
  await expect(row).not.toContainText('agent_probe_disabled')
  // 01:1513 M4-7:SKIPPED 不计红项,P-SETUP 步 3 不因它阻断
  await expect(page.getByTestId('qt-setup-next')).toBeEnabled()
  await expect(page.locator('.verdict'), '总体结论要如实点出「未探测」而不是说成通过').toContainText('未探测')
})

test('D-C 护栏:OK 与 SKIPPED 混排时按 OK 算(不因为「有一项没测」就把整行报成黄)', async ({ page }) => {
  await freshNotice(page, SHORT_NOTICE)
  await stubSelftest(page, [
    { side: 'wsl', target: 'apk_url', status: 'OK', level_reached: 'tls', detail: null },
    { side: 'win', target: 'mail_pop3', status: 'SKIPPED', level_reached: null, detail: 'not_configured' },
  ])
  await gotoSelfcheckStep(page)
  const row = page.getByTestId('qt-setup-selfcheck-table').locator('tr', { hasText: '连通性探测' })
  await expect(row.locator('.qt-warn')).toHaveCount(0)
  await expect(row.locator('td.qt-ok')).toHaveText('✔')
  await expect(row, '没测的那项仍要如实说明,不能被 OK 吞掉').toContainText('未探测')
  await expect(page.getByTestId('qt-setup-next')).toBeEnabled()
})

test('真红项(TCP_TIMEOUT / PROXY_REQUIRED / BLOCKED_BY_POLICY)必须判红并阻断(00 §8.5)', async ({ page }) => {
  // 老聚合写死的红项枚举是 `FAIL/TCP_FAIL/BLOCKED`(00 §8.5 里根本不存在)⇒ 真红项被判成黄 = 该拦的没拦。
  for (const status of ['TCP_TIMEOUT', 'PROXY_REQUIRED', 'BLOCKED_BY_POLICY']) {
    await freshNotice(page, SHORT_NOTICE)
    await stubSelftest(page, [{ side: 'wsl', target: 'apk_url', status, level_reached: null, detail: null }])
    await gotoSelfcheckStep(page)
    const row = page.getByTestId('qt-setup-selfcheck-table').locator('tr', { hasText: '连通性探测' })
    await expect(row.locator('td.qt-danger'), `${status} 必须判红`).toHaveText('✗')
    await expect(page.getByTestId('qt-setup-next'), `${status} 必须阻断步 3(01:352)`).toBeDisabled()
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  }
})

// 「靠 @scroll / 尺寸变化才解禁」这类坑与视口强相关,窄屏/宽屏各走一遍
for (const viewport of [{ width: 900, height: 600 }, { width: 1600, height: 1000 }]) {
  test(`视口 ${viewport.width}×${viewport.height} 下向导能一路走到完成`, async ({ page }) => {
    const errors = collectErrors(page)
    await page.setViewportSize(viewport)
    await freshNotice(page, SHORT_NOTICE)
    await page.goto('/')
    const ack = page.getByTestId('qt-setup-notice-ack')
    if (!(await ack.isEnabled())) {
      // 该视口下须知溢出了:滚到底再勾(判据仍然成立)
      await page.locator('.notice').evaluate((el) => { el.scrollTop = el.scrollHeight })
    }
    await expect(ack).toBeEnabled()
    await ack.click()
    await expect(ack).toBeChecked()
    for (let i = 0; i < 3; i++) await page.getByTestId('qt-setup-next').click()
    await page.getByTestId('qt-setup-login-skip').click()
    await page.getByTestId('qt-setup-finish').click()
    await expect(page).toHaveURL(/#\/dash/)
    expect(errors).toEqual([])
  })
}

// ── 🔴 安琳亲口提的需求 + 05 §6.1 告知改版这条新行为 ─────────────────────────────

test('🔴 跑过一次引导后:刷新与「重启程序」都直达主页,不再从引导进(01:331)', async ({ page, context }) => {
  // 浏览器形态(无 `window.qt`)= 安琳看的 dev:web 现场;「向导已完成」落在本机存储,
  // 同一个 browser context 的新 page 共享它 —— 等同于关掉程序再开。
  const errors = collectErrors(page)
  const notice = noticeState()
  await freshNotice(page, SHORT_NOTICE, notice)
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
  await freshNotice(fresh, SHORT_NOTICE, notice) // 共用同一份后端告知状态(上一步已勾过)
  await fresh.goto('/')
  await expect(fresh, '01:331「完成后不再出现」').toHaveURL(/#\/dash/)
  await fresh.goto('/#/set')
  await expect(fresh).toHaveURL(/#\/set$/)
  await fresh.close()
  expect(errors).toEqual([])
})

test('🔴 但「重新运行向导」仍能主动进入,且进去后是真的未完成态(01:331 括号句)', async ({ page }) => {
  await freshNotice(page, SHORT_NOTICE, noticeState())
  await page.goto('/')
  await walkWizardToDash(page)

  await page.goto('/#/set')
  const rerun = page.getByTestId('qt-set-rerun-setup')
  await rerun.scrollIntoViewIfNeeded()
  await rerun.click()
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page), '重跑要从第 1 步开始').toBe(0)
  // 重跑不是「跳一下页面」:`[setup] done` 真的被置回 false,刷新与直开别的页都回向导
  await page.reload()
  await expect(page).toHaveURL(/#\/setup/)
  await page.goto('/#/dash')
  await expect(page).toHaveURL(/#\/setup/)
})

test('🔴 首登「现在添加」→ 建号页 →(返回 / 去账号列表)⇒ 都回到第 4 步(01:353)', async ({ page }) => {
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  for (let i = 0; i < 3; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(3)

  // (a) 浏览器后退
  await page.getByTestId('qt-setup-login-qq-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page), '01:353「完成后回到本步」').toBe(3)

  // (b) 建号页上的「返回列表 / 取消」跳 `/acct` —— 向导未完成,守卫照旧打回 `/setup`,
  //     而步骤存在 store 里 ⇒ 回去仍是第 4 步(不能变成「退回第 1 步重走」)
  await page.getByTestId('qt-setup-login-wechat-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goto('/#/acct')
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page)).toBe(3)
})

test('守卫的窄例外不能被借道:未完成向导时 ?from=setup 只对 P-ACCT-NEW 有效', async ({ page }) => {
  // `router/index.ts` setupRedirect:例外要「未完成 + 目标恰是 P-ACCT-NEW + ?from=setup」三条同时成立。
  await freshNotice(page, SHORT_NOTICE)
  for (const url of ['/#/dash?from=setup', '/#/set?from=setup', '/#/log?from=setup', '/#/acct/new', '/#/acct/new?ch=qq']) {
    await page.goto(url)
    await expect(page, `${url} 不该被放行`).toHaveURL(/#\/setup/)
  }
  // 三条全中才放行(对照组)
  await page.goto('/#/acct/new?ch=qq&from=setup')
  await expect(page).toHaveURL(/#\/acct\/new/)
})

test('🔴 告知改版(05 §6.1):已完成向导的机器被带回告知页,只重勾一次即进主页、不重走五步', async ({ page }) => {
  // 桩造「向导已完成」,但这一版告知没勾过 ⇒ `refreshAck()` 置 `reackRequired`。
  await installQtStub(page, { ack: false })
  const notice = noticeState({ version: 'pw-v2', acked: false })
  await freshNotice(page, SHORT_NOTICE, notice)

  await page.goto('/#/dash')
  await expect(page, '05 §8b.6 U1「告知页文本改版则要求重新勾选」').toHaveURL(/#\/setup/)
  expect(await currentStep(page), '只差重勾 ⇒ 停在告知页').toBe(0)
  await expect(page.getByText('合规告知已更新'), '要说清为什么把人带回来').toBeVisible()

  const next = page.getByTestId('qt-setup-next')
  await expect(next, '01:350 未勾不许走').toBeDisabled()
  await expect(next, '按钮文案要让人知道勾完就进控制台,不是重走五步').toContainText(/确\s*认\s*并\s*进\s*入\s*控\s*制\s*台/)

  await page.getByTestId('qt-setup-notice-ack').click()
  await expect.poll(() => notice.acks.length, { message: '必须真发 #87' }).toBe(1)
  expect(notice.acks[0].notice_version, '带的必须是新版本号').toBe('pw-v2')
  await expect(next).toBeEnabled()
  await expect(
    next,
    '勾完之后按钮又变回「下一步」= 这一路退化成重走五步(根因:stores/setup.ts 的 ack() 一成功就把 reackRequired 清掉,SetupPage.vue 的 reackOnly 分支再也进不去)',
  ).toContainText(/确\s*认\s*并\s*进\s*入\s*控\s*制\s*台/)
  await next.click()
  await expect(page, '勾完一次就直达主页').toHaveURL(/#\/dash/)

  // 不重走五步的硬判据:点一次就到主页(若重走会停在第 2 步「连接」);此后再开别的页也不再被拦
  await page.goto('/#/acct')
  await expect(page).toHaveURL(/#\/acct$/)
})

test('告知拉不到时不把人锁死在告知页(判据以 Agent 为准,取不到 ≠ 没勾)', async ({ page }) => {
  await installQtStub(page, { ack: false })
  await page.route('**/api/v1/system/notice', (route) => route.fulfill({
    status: 503, contentType: 'application/json',
    body: JSON.stringify({ ok: false, code: 'UNAVAILABLE', message: 'pw', trace_id: 'pw' }),
  }))
  await page.goto('/#/dash')
  await page.waitForTimeout(1200)
  await expect(page, '#86 拉不到就把已完成向导的人按回告知页 = 永久锁死').toHaveURL(/#\/dash/)
})
