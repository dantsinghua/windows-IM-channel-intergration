/**
 * Playwright 用例公用件。
 *
 * - `collectErrors(page)`:收集未捕获异常(pageerror)与 console.error,用例末尾断言为空。
 * - `ackNotice(page)`:对后端补一次 #87 合规告知确认(版本号从 #86 现取,不写死)。
 * - `installQtStub(page, opts)`:在页面脚本执行前注入一个最小的 `window.qt`(模拟 Electron preload),
 *   并默认顺带 `ackNotice` —— **桩的职责是造出一个「可用初态」**,见下。
 *   🔴 只在「绕开首启向导去走查其它页」时用;复现安琳现场(浏览器 dev:web,**没有** window.qt)的用例不装。
 *   `console.toml` 用 sessionStorage 模拟,刷新后仍在(与真 Electron 的落盘语义一致)。
 * - `walkWizardToDash(page)`:不装桩时,浏览器形态下每次加载都会进向导(setup.done 恒 false),
 *   这里把它一路点完进 P-DASH。
 */
import { expect } from '@playwright/test'

/**
 * 收集未捕获异常与 console.error。
 *
 * `allow404`:已知缺陷造成的 404(比如 P-SET 仍在调废弃的 `/settings/compliance`),
 * 在专门的用例里单独盯着,别让它把每一条冒烟用例都染红。浏览器给的 console.error
 * 文本里没有 URL,所以这里顺带盯 response,只有**全部** 404 都在白名单里时才滤掉那几行。
 */
export function collectErrors(page, { allow404 = [] } = {}) {
  const errors = []
  const seen404 = []
  page.on('response', (r) => {
    if (r.status() !== 404) return
    seen404.push(r.url().split('/api/v1')[1] ?? r.url())
  })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    errors.push(`console.error: ${msg.text()}`)
  })
  // 挂成不可枚举,免得 `expect(errors).toEqual([])` 因为多了这个属性而误红
  Object.defineProperty(errors, 'filtered', { enumerable: false, value: () => {
    const unexpected404 = seen404.filter((u) => !allow404.some((a) => u.includes(a)))
    if (!allow404.length || unexpected404.length) return [...errors]
    return errors.filter((e) => !/status of 404/.test(e))
  } })
  return errors
}

/**
 * 补一次合规告知确认(#87 `POST /system/notice/ack`),版本号**从 #86 现取、不写死**。
 *
 * 🔴 为什么桩要做这件事:向导线按 05 §6.1(「告知页文本改版则要求重新勾选」)新增了 `reackRequired` ——
 * 「已完成向导 + 这一版告知没勾过」也会被守卫按回 `/setup`(`router/index.ts` 的 `setupRedirect`)。
 * 而 mock 的初态是 `compliance.ack_ms = null`(= 从没确认过),与它其余数据(已有账号/消息/邮件 = 已在用)
 * 并不自洽。**既然桩负责造「向导已完成」这个初态,就得把同一个初态里必然为真的「已确认当前版告知」一并造出来**,
 * 否则走查其它页的用例测的是「半个初态」。这里不改 mock 初始值,只在用例侧补这一次真实的 #87 调用。
 *
 * 用 `page.request`(APIRequestContext)直发,**不经页面**,因此也不会被用例自己的 `page.route` 拦截。
 * @param {import('@playwright/test').Page} page
 * @returns {Promise<string>} 确认掉的 notice_version
 */
export async function ackNotice(page) {
  const got = await page.request.get('/api/v1/system/notice')
  if (!got.ok()) throw new Error(`取 #86 /system/notice 失败:HTTP ${got.status()}`)
  const body = await got.json()
  const version = body?.notice_version ?? body?.data?.notice_version
  if (!version) throw new Error(`#86 没回 notice_version:${JSON.stringify(body)}`)
  const acked = await page.request.post('/api/v1/system/notice/ack', { data: { notice_version: version } })
  if (!acked.ok()) throw new Error(`#87 ack 失败:HTTP ${acked.status()} ${await acked.text()}`)
  return version
}

/**
 * @param {import('@playwright/test').Page} page
 * @param {{ setupDone?: boolean, ack?: boolean }} [opts]
 *   `ack` 默认 true(造完整初态);自己用 `page.route` 接管 #86/#87 的用例传 `false`。
 */
export async function installQtStub(page, opts = {}) {
  if (opts.ack !== false) await ackNotice(page)
  await page.addInitScript((o) => {
    const KEY = '__pw_console_toml'
    const read = () => {
      try { return JSON.parse(sessionStorage.getItem(KEY) || 'null') } catch { return null }
    }
    if (!read()) {
      sessionStorage.setItem(KEY, JSON.stringify({ setup: { done: o.setupDone !== false }, app: {}, ui: {} }))
    }
    const calls = []
    window.__pwQtCalls = calls
    const rec = (name, args) => { calls.push({ name, args }); }
    window.qt = {
      app: {
        version: async () => ({ console: '0.0.0-pw', electron: '0.0.0' }),
        minimizeToTray: async () => rec('app.minimizeToTray'),
        quit: async () => rec('app.quit'),
        relaunch: async () => rec('app.relaunch'),
        setAutoLaunch: async (on) => { rec('app.setAutoLaunch', on); return on },
        getAutoLaunch: async () => true,
        openLogsDir: async () => rec('app.openLogsDir'),
        openExternal: async (u) => { rec('app.openExternal', u); return true },
        rssKb: async () => 123456,
      },
      files: {
        saveAs: async (name) => { rec('files.saveAs', name); return { saved: true, path: `C:/tmp/${name}` } },
        pickFile: async () => null,
      },
      auth: { state: async () => 'ok', refresh: async () => true },
      config: {
        read: async () => read(),
        patch: async (patch) => {
          rec('config.patch', patch)
          const cur = read() || {}
          for (const [k, v] of Object.entries(patch)) cur[k] = { ...(cur[k] || {}), ...v }
          sessionStorage.setItem(KEY, JSON.stringify(cur))
          return cur
        },
      },
      window: { onVisibility: () => () => undefined },
      notify: async (t, b, r) => { rec('notify', [t, b, r]); return true },
      tray: { update: async () => undefined },
      wa: { invoke: async (op, args) => { rec('wa.invoke', [op, args]); return { ok: true } } },
      router: { onRoute: () => () => undefined },
    }
  }, opts)
}

/** 浏览器形态(无 window.qt)下把向导一路点完;返回时停在 P-DASH */
export async function walkWizardToDash(page) {
  await expect(page).toHaveURL(/#\/setup/)
  // a-checkbox 把 data-testid 透传到了 <input type=checkbox> 本身
  const box = page.getByTestId('qt-setup-notice-ack')
  await expect(page.getByTestId('qt-setup-notice-text')).not.toHaveText(/正在读取/)
  // 需要滚动时先滚到底
  await page.locator('.notice').evaluate((el) => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')) })
  await expect(box).toBeEnabled()
  if (!(await box.isChecked())) await box.click()
  await expect(box).toBeChecked()
  await page.getByTestId('qt-setup-next').click()
  await expect(page.getByTestId('qt-setup-conn-agent')).toBeVisible()
  await page.getByTestId('qt-setup-next').click()
  await expect(page.getByTestId('qt-setup-selfcheck-run')).toBeVisible()
  await page.getByTestId('qt-setup-next').click()
  await page.getByTestId('qt-setup-login-skip').click()
  await page.getByTestId('qt-setup-finish').click()
  await expect(page).toHaveURL(/#\/dash/)
}

/** 当前停在向导第几步(0 起) —— 以 a-steps 的「进行中」项为准 */
export async function currentStep(page) {
  return page.getByTestId('qt-setup-steps').locator('.ant-steps-item').evaluateAll(
    (items) => items.findIndex((n) => n.classList.contains('ant-steps-item-process')),
  )
}
