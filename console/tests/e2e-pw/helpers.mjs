/**
 * Playwright 用例公用件。
 *
 * - `collectErrors(page)`:收集未捕获异常(pageerror)与 console.error,用例末尾断言为空。
 * - `installQtStub(page, opts)`:在页面脚本执行前注入一个最小的 `window.qt`(模拟 Electron preload)。
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
 * @param {import('@playwright/test').Page} page
 * @param {{ setupDone?: boolean }} [opts]
 */
export async function installQtStub(page, opts = {}) {
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
