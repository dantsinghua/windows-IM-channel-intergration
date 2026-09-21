/**
 * `P-SETUP` 首次启动向导的真点击用例(01 §2.7.1,docs/01-控制台前端设计.md:329-356)。
 *
 * 🔴 浏览器形态(`dev:web`)下没有 `window.qt` ⇒ `setup.done` 恒 false ⇒ 每次加载都进向导,
 * 正好是安琳实测时的现场,这里不装 qt 桩。
 *
 * 已知缺陷用 `test.fail()` 标住:**跑起来必须是「失败即通过」**;哪天实现方修好了,
 * 这条会因为「本该失败却过了」而变红,提醒把标记摘掉 —— 不会出现「修好了没人知道」。
 */
import { test, expect } from '@playwright/test'
import { collectErrors, currentStep, installQtStub } from './helpers.mjs'

/**
 * 让告知文案永远从「没勾过」开始:mock 的勾选状态是进程内全局的,会被别的用例污染。
 * 这里把 #86 `GET /system/notice` 与 #87 `POST /system/notice/ack` 一起接管,
 * 在浏览器侧维护一份「这一版勾没勾过」,语义与真后端一致(acked_version === notice_version 才算勾过)。
 * 返回 `acks` 数组,用例可断言真发了 #87、且带对了 notice_version。
 */
async function freshNotice(page, text) {
  const acks = []
  const version = 'pw-v1'
  let acked = false
  await page.route('**/api/v1/system/notice', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        ack_ms: acked ? 1789900000000 : null,
        acked_at: acked ? '2026-09-21T00:00:00.000Z' : null,
        acked_version: acked ? version : null,
        notice_version: version,
        text,
        trace_id: 'pw',
      }),
    })
  })
  await page.route('**/api/v1/system/notice/ack', async (route) => {
    acks.push(route.request().postDataJSON())
    acked = true
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, trace_id: 'pw' }) })
  })
  return acks
}

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

test.fail('D-B:从首登进 P-ACCT-NEW 再返回,应回到首登那一步 —— 现在退回第 1 步', async ({ page }) => {
  // 用 qt 桩把 `setup.done` 置 true,才绕得开路由守卫、真的能离开向导再回来(见 D-D:首启态下压根出不去)。
  // 安琳现场是 vite HMR 触发的重挂,机制相同:向导的 step 只活在组件内存里(SetupPage.vue:24),
  // 组件一重挂就回第 1 步 —— store 里那个 `setup.step`(stores/setup.ts)根本没人用。
  await installQtStub(page)
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/#/setup')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  for (let i = 0; i < 3; i++) await page.getByTestId('qt-setup-next').click()
  expect(await currentStep(page)).toBe(3)
  await page.getByTestId('qt-setup-login-qidian-add').click()
  await expect(page).toHaveURL(/#\/acct\/new/)
  await page.goBack()
  await expect(page).toHaveURL(/#\/setup/)
  expect(await currentStep(page), '01:353「完成后回到本步」').toBe(3)
})

test.fail('D-D:首登「现在添加」应进 P-ACCT-NEW —— 现在被路由守卫打回、点了没反应', async ({ page }) => {
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  for (let i = 0; i < 3; i++) await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-login-qidian-add').click()
  await expect(page, '01:353 要求进 P-ACCT-NEW 对应分支').toHaveURL(/#\/acct\/new/)
})

test.fail('D-C:自检里的 SKIPPED 不该渲染成黄色警告,也不该直接甩英文枚举', async ({ page }) => {
  await freshNotice(page, SHORT_NOTICE)
  await page.goto('/')
  await page.getByTestId('qt-setup-notice-ack').click()
  await expect(page.getByTestId('qt-setup-notice-ack')).toBeChecked()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-selfcheck-run').click()
  const row = page.getByTestId('qt-setup-selfcheck-table').locator('tr', { hasText: '连通性探测' })
  await expect(row).toBeVisible()
  // 01:694 SKIPPED = 灰;01:1513 SKIPPED 不计红项、P-SETUP 步 3 不因它阻断;文案「未探测(目标未配置或 Agent 不可达)」
  await expect(row.locator('.qt-warn'), '只有 SKIPPED 的一轮不该显示 ⚠').toHaveCount(0)
  await expect(row).toContainText('未探测')
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
