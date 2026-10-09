import process from 'node:process';
import { Buffer } from 'node:buffer';
import { createRequire } from 'node:module'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import fs from 'node:fs/promises'

const require = createRequire(path.join(process.env.QT_PW_NODE_MODULES || '/home/anlin/work/qtrade-build/manual-20261008-a0stq5rk/console/node_modules', '__volume_acceptance__.cjs'))
const { test, expect } = require('@playwright/test')
const run = promisify(execFile)
const origin = process.env.QT_MONITOR_UI_URL || 'http://127.0.0.1:5273'
const evidence = process.env.QT_VOLUME_EVIDENCE || path.resolve(import.meta.dirname, '../../../.codex/test-results/20261008-monitor-fix/acceptance/windows-volume')
const targetFile = process.env.QT_VOLUME_TARGETS || path.join(evidence, 'probe-targets.json')
const metricsPath = '/api/v1/system/metrics'
const observations = new Map()
const diskCard = page => page.getByTestId('qt-dash-res-disk')
const apiPath = request => new URL(request.url()).pathname
const gb = mb => (mb / 1024).toFixed(1)
const driveOf = mount => String(mount).match(/^([a-z]):(?:[\\/])?$/i)?.[1]?.toUpperCase()
const normalizedPath = value => String(value).replace(/\\+/g, '\\').toLowerCase()

async function windowsProbe() {
  const targets = JSON.parse(await fs.readFile(targetFile, 'utf8'))
  const localScript = path.join(import.meta.dirname, 'windows-volume-probe.ps1')
  const script = await fs.readFile(localScript)
  expect([...script].every(byte => byte < 128), 'Windows probe must remain ASCII-only').toBe(true)
  const converted = await run('wslpath', ['-w', localScript], { timeout: 10_000 })
  const request = Buffer.from(JSON.stringify(targets)).toString('base64')
  const result = await run('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe', [
    '-NoProfile', '-NonInteractive', '-File', converted.stdout.trim(), '-RequestBase64', request,
  ], { timeout: 30_000, maxBuffer: 1024 * 1024 })
  const rows = JSON.parse(result.stdout)
  expect(rows.map(row => row.role).sort()).toEqual(['application', 'data'])
  const data = rows.find(row => row.role === 'data')
  expect(data.distro).toBeTruthy()
  expect(data.registry_base_path).toBeTruthy()
  expect(data.registry_vhd_filename).toBeTruthy()
  expect(data.backing_path.toLowerCase()).toContain(data.registry_vhd_filename.toLowerCase())
  for (const row of rows) {
    expect(row.total_bytes).toBeGreaterThan(0)
    expect(row.free_bytes).toBeGreaterThanOrEqual(0)
    expect(row.free_bytes).toBeLessThanOrEqual(row.total_bytes)
  }
  return rows
}

async function openReadOnly(page, info, mutate) {
  const h = { kind: mutate ? '浏览器GET故障注入' : '真实Windows独立探针与API同快照', writes: [], probes: [], snapshots: [] }
  observations.set(info.testId, h)
  await page.addInitScript(() => localStorage.setItem('qt.setup.done', 'true'))
  await page.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== origin) return route.abort('blockedbyclient')
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
      h.writes.push({ method: request.method(), path: url.pathname })
      return route.abort('blockedbyclient')
    }
    if (url.pathname === metricsPath && mutate) {
      const response = await route.fetch()
      expect(response.status()).toBe(200)
      const payload = await response.json()
      const changed = await mutate(payload)
      if (changed) return route.fulfill({ response, json: payload })
      return route.fulfill({ response })
    }
    return route.continue()
  })
  return h
}

async function assertWindowsSnapshot(page, payload, before, after) {
  const disks = payload.hardware.disks
  expect(payload.hardware.disk_source_error ?? null).toBeNull()
  const expectedDrives = [...new Set(before.map(row => row.drive_letter.toUpperCase()))].sort()
  expect(disks.map(row => driveOf(row.mount)).sort()).toEqual(expectedDrives)
  expect(new Set(disks.map(row => row.mount)).size).toBe(disks.length)
  expect(payload.disk_watermark.scope).toBe('deployment_volumes')
  expect(Math.abs(payload.disk_watermark.free_mb - Math.min(...disks.map(row => row.free_mb)))).toBeLessThanOrEqual(1)
  for (const disk of disks) {
    const drive = driveOf(disk.mount)
    const first = before.filter(row => row.drive_letter.toUpperCase() === drive)
    const second = after.filter(row => row.drive_letter.toUpperCase() === drive)
    expect(disk.source).toBe('windows_volume')
    expect([...disk.roles].sort()).toEqual(first.map(row => row.role === 'application' ? 'app' : 'data').sort())
    for (const sample of first) expect(normalizedPath(disk.backing_path)).toContain(normalizedPath(sample.backing_path))
    expect(Math.abs(disk.total_mb - first[0].total_bytes / 1024 ** 2)).toBeLessThanOrEqual(1)
    const free = [...first, ...second].map(row => row.free_bytes / 1024 ** 2)
    expect(disk.free_mb).toBeGreaterThanOrEqual(Math.min(...free) - 64)
    expect(disk.free_mb).toBeLessThanOrEqual(Math.max(...free) + 64)
    await expect(diskCard(page)).toContainText(disk.mount)
    await expect(diskCard(page)).toContainText(`${gb(disk.free_mb)} GB`)
  }
  await expect(diskCard(page)).toContainText('Windows')
  await expect(diskCard(page)).toContainText('应用')
  await expect(diskCard(page)).toContainText('数据')
  await expect(diskCard(page)).not.toContainText('/home/')
}

test.afterEach(async ({ page }, info) => {
  const h = observations.get(info.testId)
  if (!h) return
  h.disk_text = await diskCard(page).textContent().catch(() => '')
  expect(h.writes, '验收不得调用写API').toEqual([])
  await fs.mkdir(evidence, { recursive: true })
  await fs.writeFile(path.join(evidence, `${info.title}.json`), `${JSON.stringify(h, null, 2)}\n`)
})

test('real_windows_backing_volumes_match_api_and_dom_then_refresh', async ({ page }, info) => {
  const h = await openReadOnly(page, info)
  const before = await windowsProbe()
  h.probes.push(before)
  const firstResponse = page.waitForResponse(response => apiPath(response) === metricsPath)
  await page.goto(`${origin}/#/dash`)
  const response = await firstResponse
  expect(response.status()).toBe(200)
  const firstAt = Date.now()
  const first = await response.json()
  const after = await windowsProbe()
  h.probes.push(after)
  h.snapshots.push({ hardware: first.hardware, disk_watermark: first.disk_watermark })
  await assertWindowsSnapshot(page, first, before, after)
  await diskCard(page).screenshot({ path: path.join(evidence, 'real-windows-disk-card.png') })

  const nextResponse = await page.waitForResponse(response => apiPath(response) === metricsPath, { timeout: 36_000 })
  h.refresh_interval_ms = Date.now() - firstAt
  expect(h.refresh_interval_ms).toBeGreaterThanOrEqual(25_000)
  expect(h.refresh_interval_ms).toBeLessThanOrEqual(36_000)
  const next = await nextResponse.json()
  const last = await windowsProbe()
  h.probes.push(last)
  h.snapshots.push({ hardware: next.hardware, disk_watermark: next.disk_watermark })
  await assertWindowsSnapshot(page, next, after, last)
})

test('same_volume_roles_render_one_capacity', async ({ page }, info) => {
  let expected
  await openReadOnly(page, info, data => {
    expected = { ...data.hardware.disks[0], roles: ['app', 'data'] }
    data.hardware.disks = [expected]
    data.hardware.disk_source_error = null
    return true
  })
  const response = page.waitForResponse(response => apiPath(response) === metricsPath)
  await page.goto(`${origin}/#/dash`)
  await response
  await expect(diskCard(page)).toContainText(`${gb(expected.free_mb)} GB`)
  const text = await diskCard(page).textContent()
  expect(text.split(expected.mount).length - 1).toBe(1)
})

for (const failure of ['windows_host_unavailable', 'wsl_vhdx_mapping_failed', 'application_path_unavailable']) {
  test(`unknown_on_${failure}`, async ({ page }, info) => {
    await openReadOnly(page, info, data => {
      data.hardware.disks = []
      data.hardware.disk_source_error = failure
      data.disk_watermark = { ...data.disk_watermark, level: 'unknown', free_mb: null, scope: 'deployment_volumes' }
      return true
    })
    const response = page.waitForResponse(response => apiPath(response) === metricsPath)
    await page.goto(`${origin}/#/dash`)
    await response
    await expect(diskCard(page)).toContainText('未知')
    await expect(diskCard(page)).not.toContainText(/\[(normal|ok)\]|\d+(?:\.\d+)?\s*GB/)
  })
}

test('host_source_recovers_to_current_windows_values', async ({ page }, info) => {
  let calls = 0
  const h = await openReadOnly(page, info, data => {
    calls += 1
    if (calls > 1) return false
    data.hardware.disks = []
    data.hardware.disk_source_error = 'windows_host_unavailable'
    data.disk_watermark = { ...data.disk_watermark, level: 'unknown', free_mb: null }
    return true
  })
  const first = page.waitForResponse(response => apiPath(response) === metricsPath)
  await page.goto(`${origin}/#/dash`)
  await first
  await expect(diskCard(page)).toContainText('未知')
  const before = await windowsProbe()
  const next = await page.waitForResponse(response => apiPath(response) === metricsPath, { timeout: 36_000 })
  const payload = await next.json()
  const after = await windowsProbe()
  h.probes.push(before, after)
  h.snapshots.push({ hardware: payload.hardware, disk_watermark: payload.disk_watermark })
  await assertWindowsSnapshot(page, payload, before, after)
})

test('zero_free_is_a_measurement_not_unknown', async ({ page }, info) => {
  await openReadOnly(page, info, data => {
    data.hardware.disks = [{ ...data.hardware.disks[0], free_mb: 0, roles: ['app', 'data'] }]
    data.hardware.disk_source_error = null
    data.disk_watermark = { ...data.disk_watermark, level: 'critical', free_mb: 0 }
    return true
  })
  const response = page.waitForResponse(response => apiPath(response) === metricsPath)
  await page.goto(`${origin}/#/dash`)
  await response
  await expect(diskCard(page)).toContainText('0.0 GB')
  await expect(diskCard(page)).not.toContainText('未知')
})
