/**
 * 危险/不可逆动作的二次确认(01 §2.7.9:「运行期动作按钮 …… **全部二次确认** + 审计」,
 * docs/01-控制台前端设计.md:696-700;账号软删/真删 01:465/538;邮件 approve/discard 01:639/642)。
 *
 * 判据:点下去**不能立刻发写请求**,必须先弹确认;确认框要能取消/Esc 关掉。
 */
import { test, expect } from '@playwright/test'
import { installQtStub } from './helpers.mjs'

const WRITE = ['POST', 'PUT', 'DELETE', 'PATCH']
/** antd 两字中文按钮会被渲染成「确 认」,名字用宽松正则匹配 */
const loose = (s) => new RegExp(s.split('').join('\\s*'))

/** 点按钮 → 返回 {立刻发出的写请求, 弹窗数} */
async function clickAndWatch(page, locator) {
  const writes = []
  const onReq = (r) => { if (WRITE.includes(r.method()) && r.url().includes('/api/v1')) writes.push(`${r.method()} ${r.url().split('/api/v1')[1]}`) }
  page.on('request', onReq)
  await locator.scrollIntoViewIfNeeded()
  await locator.click()
  await page.waitForTimeout(600)
  page.off('request', onReq)
  const dialogs = await page.locator('.ant-modal-wrap:visible, .ant-popconfirm:visible, .ant-popover:visible').count()
  return { writes, dialogs }
}

async function closeDialogs(page) {
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
  const left = page.locator('.ant-modal-wrap:visible, .ant-popconfirm:visible')
  if (await left.count()) {
    await page.getByRole('button', { name: loose('取消') }).first().click().catch(() => undefined)
    await page.waitForTimeout(300)
  }
}

test('P-ENV 的内核/发行版/WSL 动作:先弹确认,不直接下手', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/env')
  await expect(page.getByText('一键自检').first()).toBeVisible()
  for (const name of ['重新应用内核', '回滚内核', '修复发行版', '重启 WSL']) {
    const btn = page.getByRole('button', { name: loose(name) }).first()
    if (!(await btn.count()) || !(await btn.isEnabled())) continue
    const { writes, dialogs } = await clickAndWatch(page, btn)
    expect(writes, `「${name}」没确认就发了写请求`).toEqual([])
    expect(dialogs, `「${name}」缺二次确认`).toBeGreaterThan(0)
    await closeDialogs(page)
  }
})

test('P-ACCT-DETAIL 软删有确认;彻底删除要手输账号 ID(01:465)', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/acct/qd03')
  await expect(page.getByText('概览').first()).toBeVisible()

  const del = page.getByRole('button', { name: loose('停用') }).first()
  const r1 = await clickAndWatch(page, del)
  expect(r1.writes, '软删没确认就发了 DELETE').toEqual([])
  expect(r1.dialogs).toBeGreaterThan(0)
  await closeDialogs(page)

  const purge = page.getByRole('button', { name: loose('彻底删除数据') }).first()
  const r2 = await clickAndWatch(page, purge)
  expect(r2.writes, '真删没确认就发了 purge').toEqual([])
  const dialog = page.locator('.ant-modal-wrap:visible')
  await expect(dialog).toContainText('不可恢复')
  await expect(dialog, '必须要求手输账号 ID').toContainText('qd03')
  const confirmBtn = page.getByRole('button', { name: loose('确认彻底删除') })
  await expect(confirmBtn, 'ID 没输之前不该能点').toBeDisabled()
  await closeDialogs(page)
})

test('P-MAIL 邮件来的危险指令:确认执行要二次确认;丢弃要确认', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/mail')
  await expect(page.getByText('水位与健康').first()).toBeVisible()

  const approve = page.getByRole('button', { name: loose('确认执行') }).first()
  if (await approve.count()) {
    const r = await clickAndWatch(page, approve)
    expect(r.writes, 'approve 没确认就执行了邮件来的危险指令').toEqual([])
    expect(r.dialogs).toBeGreaterThan(0)
    await closeDialogs(page)
  }

  const discard = page.getByRole('button', { name: loose('丢弃') }).first()
  if (await discard.count()) {
    const r = await clickAndWatch(page, discard)
    expect(r.writes, '丢弃没确认就发了 discard').toEqual([])
    expect(r.dialogs).toBeGreaterThan(0)
    await closeDialogs(page)
  }
})

test('P-MAIL 立即清理:点一次后按钮进入 60s 防重(01:643)', async ({ page }) => {
  await installQtStub(page)
  const posts = []
  page.on('request', (r) => { if (r.method() === 'POST' && r.url().includes('/mail/cleanup/run')) posts.push(1) })
  await page.goto('/#/mail')
  const btn = page.getByRole('button', { name: loose('立即清理') }).first()
  await btn.scrollIntoViewIfNeeded()
  await btn.click()
  await expect(btn, '点完必须立刻置灰,否则会连点连发').toBeDisabled()
  await btn.click({ force: true }).catch(() => undefined)
  await page.waitForTimeout(600)
  expect(posts.length, '连点发出了多次清理').toBe(1)
})

test.fail('P-ENV「修复防火墙规则」缺二次确认(01:696「全部二次确认」)', async ({ page }) => {
  await installQtStub(page)
  await page.goto('/#/env')
  await expect(page.getByText('一键自检').first()).toBeVisible()
  const btn = page.getByRole('button', { name: loose('修复防火墙规则') }).first()
  const { dialogs } = await clickAndWatch(page, btn)
  expect(dialogs, '点下去直接调 WinAgent 改防火墙,没有任何确认').toBeGreaterThan(0)
})
