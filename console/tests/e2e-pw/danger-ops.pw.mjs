/**
 * R6-81 保留动作的真实点击门：未确认或取消不得发送请求/调用桌面桥。
 * 全部连接独占 mock，绝不连接真实 WSL、账号或邮件服务。
 */
import { test, expect } from '@playwright/test'
import { installQtStub } from './helpers.mjs'

const WRITE = ['POST', 'PUT', 'DELETE', 'PATCH']
async function clickAndWatch(page, locator) {
  const writes = []
  const onReq = r => { if (WRITE.includes(r.method()) && r.url().includes('/api/v1')) writes.push(r.method() + ' ' + r.url().split('/api/v1')[1]) }
  page.on('request', onReq)
  await locator.click()
  await expect(page.locator('.ant-modal-wrap:visible, .ant-popconfirm:visible')).toHaveCount(1)
  page.off('request', onReq)
  return writes
}
async function cancelDialog(page) {
  await page.getByRole('button', { name: /取\s*消/ }).last().click()
  await expect(page.locator('.ant-modal-wrap:visible, .ant-popconfirm:visible')).toHaveCount(0)
}
async function writesToBridge(page) {
  return page.evaluate(() => (window.__pwQtCalls ?? []).filter(c => c.name === 'wa.invoke' && /^(wsl\.kernel\.|firewall)/.test(c.args?.[0] ?? '')))
}

test('P-ENV 保留的重启/回滚均确认影响范围，取消后没有副作用', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/env')
  await expect(page.getByRole('heading', { name: '让每个账号，稳定在线' })).toBeVisible()
  for (const id of ['qt-env-wsl-restart', 'qt-env-kernel-rollback']) {
    const button = page.getByTestId(id)
    await expect(button).toBeEnabled()
    const before = await writesToBridge(page)
    expect(await clickAndWatch(page, button)).toEqual([])
    await expect(page.locator('.ant-modal-wrap:visible')).toContainText('所有 WSL')
    await cancelDialog(page)
    expect(await writesToBridge(page)).toEqual(before)
  }
})

test('P-ACCT-DETAIL 移除须确认数据保留，退役的彻底删除不再呈现', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/acct/qd03')
  await expect(page.getByText('账号信息', { exact: true })).toBeVisible()
  const writes = await clickAndWatch(page, page.getByTestId('qt-acct-detail-delete'))
  expect(writes).toEqual([])
  await expect(page.locator('.ant-popconfirm:visible')).toContainText('数据与登录态保留')
  await cancelDialog(page)
  await expect(page.getByRole('button', { name: '彻底删除数据' })).toHaveCount(0)
})

test('P-MAIL 待确认与发件标签分别保留执行/丢弃确认', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/mail')
  await expect(page.getByText('邮箱连接', { exact: true })).toBeVisible()
  await page.getByRole('tab', { name: /^待确认/ }).click()
  const approve = page.getByTestId('qt-mail-danger-row-0-approve')
  await expect(approve).toBeEnabled()
  expect(await clickAndWatch(page, approve)).toEqual([])
  await cancelDialog(page)
  await page.getByRole('tab', { name: '发件', exact: true }).click()
  const discard = page.locator('[data-testid^="qt-mail-outbox-row-"][data-testid$="-discard"]').first()
  await expect(discard).toBeVisible()
  expect(await clickAndWatch(page, discard)).toEqual([])
  await cancelDialog(page)
})

test('R6-81 清理移至资源页：先确认，再进入 60 秒防重', async ({ page }) => {
  await installQtStub(page)
  const posts = []
  page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/system/cleanup/run')) posts.push(r.url()) })
  await page.goto('/#/res?section=cleanup')
  const button = page.getByTestId('qt-res-action-cleanup')
  expect(await clickAndWatch(page, button)).toEqual([])
  expect(posts).toEqual([])
  await page.locator('.ant-popconfirm:visible').getByRole('button', { name: /确\s*定|确\s*认/ }).click()
  await expect.poll(() => posts.length).toBe(1)
  await expect(button).toBeDisabled()
  await button.click({ force: true })
  expect(posts).toHaveLength(1)
})

test('P-ENV 修复网络访问在确认/取消之前不调用 firewall 桥', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/env')
  const button = page.getByTestId('qt-env-firewall-fix')
  await expect(button).toBeEnabled()
  const before = await writesToBridge(page)
  expect(await clickAndWatch(page, button)).toEqual([])
  expect(await writesToBridge(page)).toEqual(before)
  await cancelDialog(page)
  expect(await writesToBridge(page)).toEqual(before)
})
