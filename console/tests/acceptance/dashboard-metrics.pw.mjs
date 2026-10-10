import { fileURLToPath, URL } from 'node:url'
import process from 'node:process';
import { createRequire } from 'node:module'
import path from 'node:path'
import fs from 'node:fs/promises'

const require = createRequire(path.join(process.env.QT_PW_NODE_MODULES || fileURLToPath(new URL('../../node_modules', import.meta.url)), '__monitor_acceptance__.cjs'))
const { test, expect } = require('@playwright/test')
const ORIGIN = process.env.QT_MONITOR_UI_URL || 'http://127.0.0.1:5273'
const evidence = process.env.QT_MONITOR_EVIDENCE || path.resolve(import.meta.dirname, '../../../.codex/test-results/20261008-monitor-fix/acceptance')
const observations = new Map()
const metricsPath = '/api/v1/system/metrics'
const healthPath = '/api/v1/system/health'
const versionPath = '/api/v1/system/version'
const card = (page, name) => page.getByTestId(`qt-dash-${name}`)
const pathname = request => new URL(request.url()).pathname
const gb = mb => (mb / 1024).toFixed(1)

function safeSnapshot(payload) {
  return { hardware: payload.hardware, mem_watermark: payload.mem_watermark, disk_watermark: payload.disk_watermark }
}

async function start(page, info, mutate) {
  const h = { kind: mutate ? '浏览器 GET 故障注入，其他请求走现有 API' : '现有 API 实际响应', blockedWrites: [], snapshots: [], dom: [] }
  observations.set(info.testId, h)
  await page.addInitScript(() => localStorage.setItem('qt.setup.done', 'true'))
  await page.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== ORIGIN) return route.abort('blockedbyclient')
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
      h.blockedWrites.push({ method: request.method(), path: url.pathname })
      return route.abort('blockedbyclient')
    }
    if (mutate && await mutate(route, h)) return
    return route.continue()
  })
  return h
}

async function snapshotDom(page) {
  return page.locator('[data-testid="qt-dash-res-mem"],[data-testid="qt-dash-res-disk"],[data-testid="qt-dash-sys-docker"],[data-testid="qt-dash-sys-kernel"]').evaluateAll(elements => elements.map(element => ({
    id: element.dataset.testid, text: element.textContent,
    dotColor: element.querySelector('.dot') ? getComputedStyle(element.querySelector('.dot')).backgroundColor : null,
  })))
}

async function colorOf(page, name) {
  return page.evaluate(name => {
    const element = document.createElement('span')
    element.style.backgroundColor = `var(--qt-state-${name})`
    document.body.append(element)
    const result = getComputedStyle(element).backgroundColor
    element.remove()
    return result
  }, name)
}

async function expectColor(page, name, color) {
  const expected = await colorOf(page, color)
  await expect.poll(() => card(page, name).locator('.dot').evaluate(el => getComputedStyle(el).backgroundColor)).toBe(expected)
}

async function checkMetrics(page, snapshot) {
  const mem = snapshot.hardware.mem
  await expect(card(page, 'res-mem')).toContainText(`已用 ${gb(mem.used_mb)} / ${gb(mem.total_mb)} GB`)
  await expect(card(page, 'res-mem')).toContainText(`可用 ${gb(mem.avail_mb)} GB`)
  for (const disk of snapshot.hardware.disks) {
    await expect(card(page, 'res-disk')).toContainText(`${disk.mount} 剩 ${gb(disk.free_mb)} GB`)
  }
}

async function rewrite(route, changes) {
  const response = await route.fetch()
  expect(response.status()).toBe(200)
  const payload = await response.json()
  changes(payload)
  await route.fulfill({ response, json: payload })
  return true
}

test.afterEach(async ({ page }, info) => {
  const h = observations.get(info.testId)
  if (!h) return
  h.dom = await snapshotDom(page).catch(() => [])
  expect(h.blockedWrites, '首页只读验收不得触发任何写 API').toEqual([])
  await fs.mkdir(evidence, { recursive: true })
  const name = info.title.replace(/[^a-zA-Z0-9_-]/g, '_')
  await fs.writeFile(path.join(evidence, `${name}.json`), `${JSON.stringify(h, null, 2)}\n`)
})

test('real_api_cold_start_and_30_second_refresh', async ({ page }, info) => {
  const h = await start(page, info)
  const firstResponse = page.waitForResponse(response => pathname(response) === metricsPath)
  await page.goto(`${ORIGIN}/#/dash`)
  const response = await firstResponse
  expect(response.status()).toBe(200)
  const firstAt = Date.now()
  const first = await response.json()
  h.snapshots.push(safeSnapshot(first))
  await checkMetrics(page, first)
  await fs.mkdir(evidence, { recursive: true })
  await card(page, 'res-mem').screenshot({ path: path.join(evidence, 'real-memory-card.png') })
  await card(page, 'res-disk').screenshot({ path: path.join(evidence, 'real-disk-card.png') })

  const refreshed = await page.waitForResponse(response => pathname(response) === metricsPath, { timeout: 36_000 })
  const delta = Date.now() - firstAt
  expect(delta).toBeGreaterThanOrEqual(25_000)
  expect(delta).toBeLessThanOrEqual(36_000)
  h.refresh_interval_ms = delta
  const next = await refreshed.json()
  h.snapshots.push(safeSnapshot(next))
  await checkMetrics(page, next)
})

test('first_http_failure_is_unknown_then_recovers_on_poll', async ({ page }, info) => {
  let count = 0
  const h = await start(page, info, async route => {
    if (pathname(route.request()) !== metricsPath) return false
    count += 1
    if (count > 1) return false
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ ok: false, code: 'NOT_READY' }) })
    return true
  })
  const failed = page.waitForResponse(response => pathname(response) === metricsPath && response.status() === 503)
  await page.goto(`${ORIGIN}/#/dash`)
  await failed
  await expect(card(page, 'res-mem')).not.toContainText(/\[(normal|ok)\]/)
  await expect(card(page, 'res-disk')).not.toContainText(/\[(normal|ok)\]/)
  await expect(card(page, 'res-mem')).toContainText(/未知|未获取|不可用|失败/)
  h.failed_dom = await snapshotDom(page)
  const recovered = await page.waitForResponse(response => pathname(response) === metricsPath && response.status() === 200, { timeout: 36_000 })
  const data = await recovered.json()
  h.snapshots.push(safeSnapshot(data))
  await checkMetrics(page, data)
})

test('missing_measurements_do_not_invent_normal_or_zero', async ({ page }, info) => {
  await start(page, info, async route => {
    if (pathname(route.request()) !== metricsPath) return false
    return rewrite(route, data => {
      data.hardware = { mem: { total_mb: null, used_mb: null, avail_mb: null }, cpu: {}, disks: [] }
      data.mem_watermark = {}
      data.disk_watermark = {}
    })
  })
  const response = page.waitForResponse(response => pathname(response) === metricsPath)
  await page.goto(`${ORIGIN}/#/dash`)
  await response
  for (const name of ['res-mem', 'res-disk']) {
    await expect(card(page, name)).toContainText(/未知|未获取|不可用|暂无|—/)
    await expect(card(page, name)).not.toContainText(/\[(normal|ok)\]|0\.0 GB|NaN|undefined/)
  }
})

for (const scenario of [
  { name: 'online_without_version', dockerd: true, version: null, text: '在线', color: 'running' },
  { name: 'offline_with_version', dockerd: false, version: '26.1.4-acceptance', text: /离线|异常/, color: 'error' },
  { name: 'unknown_with_version', dockerd: null, version: '26.1.4-acceptance', text: '未知', color: 'stopped' },
]) {
  test(`docker_${scenario.name}`, async ({ page }, info) => {
    await start(page, info, async route => {
      const p = pathname(route.request())
      if (p === healthPath) return rewrite(route, data => { data.dockerd = scenario.dockerd })
      if (p === versionPath) return rewrite(route, data => { data.docker = scenario.version })
      return false
    })
    await page.goto(`${ORIGIN}/#/dash`)
    await expect(card(page, 'sys-docker')).toContainText(scenario.text)
    if (scenario.dockerd !== true) await expect(card(page, 'sys-docker')).not.toContainText('在线')
    await expect(card(page, 'sys-docker')).toContainText(scenario.version || '版本未知')
    await expectColor(page, 'sys-docker', scenario.color)
  })
}

test('unknown_kernel_does_not_show_error_red', async ({ page }, info) => {
  await start(page, info, async route => {
    const p = pathname(route.request())
    if (p === versionPath) return rewrite(route, data => { data.kernel = null })
    if (p === '/api/v1/system/env') return rewrite(route, data => {
      delete data.kernel_state
      if (data.wsl) delete data.wsl.kernel_state
    })
    return false
  })
  await page.goto(`${ORIGIN}/#/dash`)
  await expect(card(page, 'sys-kernel')).toContainText('未知')
  await expectColor(page, 'sys-kernel', 'stopped')
})
