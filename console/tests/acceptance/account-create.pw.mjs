import { fileURLToPath, URL } from 'node:url'
import process from 'node:process';
import { createRequire } from 'node:module'
import path from 'node:path'
import { randomUUID } from 'node:crypto'

const require = createRequire(path.join(process.env.QT_PW_NODE_MODULES || fileURLToPath(new URL('../../node_modules', import.meta.url)), '__acceptance__.cjs'))
const { test, expect } = require('@playwright/test')
const SECRET = `AcceptOnly-${randomUUID()}`
const ACCOUNT = 'acceptance@example.invalid'
let sequence = 0
const observations = new Map()

function endpoint() {
  const value = process.env.QT_ACCEPT_UI_URL
  if (!value) throw new Error('QT_ACCEPT_UI_URL must identify the exclusive all-fake acceptance service')
  const url = new URL(value)
  if (!['127.0.0.1', 'localhost'].includes(url.hostname) || ['5273', '17600', '17610'].includes(url.port)) {
    throw new Error('Acceptance requires an exclusive loopback service; user service ports are forbidden')
  }
  return url.origin
}

const tid = (page, name) => page.getByTestId(`qt-acct-new-${name}`)
const kind = request => {
  const p = new URL(request.url()).pathname
  if (request.method() === 'POST' && /\/api\/v1\/accounts\/?$/.test(p)) return 'create'
  if (request.method() === 'POST' && /\/api\/v1\/accounts\/[^/]+\/start$/.test(p)) return 'start'
  if (request.method() === 'POST' && /\/api\/v1\/accounts\/[^/]+\/login$/.test(p)) return 'login'
  if (request.method() === 'DELETE' && /\/api\/v1\/accounts\/[^/]+$/.test(p)) return 'delete'
  return null
}
const requestId = request => new URL(request.url()).pathname.split('/')[4]
const count = (h, name) => h.calls.filter(call => call.kind === name).length

// This is a fixture control contract, never a product endpoint.
async function control(page, op, body) {
  const root = process.env.QT_ACCEPT_CONTROL_URL || `${endpoint()}/__accept__`
  const response = await page.request.post(`${root}/${op}`, { data: body })
  expect(response.ok(), `exclusive fake-device control ${op}`).toBeTruthy()
  return response.json()
}

async function observe(page, info) {
  const h = { calls: [], reads: [], responses: [], events: [], mocks: [], leaked: false, id: null, phase: null }
  observations.set(info.testId, h)
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname
    if (request.method() === 'GET' && /\/api\/v1\/accounts\/[^/]+(?:\/prompt)?$/.test(pathname)) h.reads.push(pathname)
    const k = kind(request)
    if (!k) return
    const body = request.postDataJSON() || {}
    const login = k === 'create' ? body.login || {} : body
    h.calls.push({
      kind: k, id: k === 'create' ? null : requestId(request),
      hasSecret: Object.hasOwn(login, 'secret'), correctSecret: login.secret === SECRET,
      remember: login.remember,
    })
  })
  page.on('response', async response => {
    const k = kind(response.request())
    if (k) h.responses.push({ kind: k, status: response.status() })
    if (k !== 'create' || response.status() !== 201) return
    const payload = await response.json().catch(() => null)
    h.id = payload?.data?.id || payload?.id || null
  })
  page.on('console', message => { if (message.text().includes(SECRET)) h.leaked = true })
  page.on('pageerror', error => { if (error.message.includes(SECRET)) h.leaked = true })
  const recordFrame = message => {
    try {
      const event = JSON.parse(String(message))
      if (event.event === 'account_state') {
        const data = event.payload || event.data || {}
        const id = event.account_id || data.account_id || data.id
        h.events.push({ id, state: data.state, code: data.state_code })
        if (id === h.id) h.phase = data.state
      }
    } catch { /* Binary streams are outside this acceptance scope. */ }
  }
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== endpoint()) {
      await route.abort('blockedbyclient')
      return
    }
    await route.fallback()
  })
  await page.routeWebSocket('**', socket => {
    const url = new URL(socket.url())
    if (url.host !== new URL(endpoint()).host) {
      socket.close({ code: 1008, reason: 'Exclusive acceptance service only' })
      return
    }
    const upstream = socket.connectToServer()
    upstream.onMessage(message => {
      recordFrame(message)
      socket.send(message)
    })
  })
  info.annotations.push({ type: 'contract', description: 'R6-78; isolated real API; fake device backends only' })
  return h
}

async function openForm(page, channel, remember = false) {
  // Public fixture contract: this suite starts after installation and first-run setup.
  const noticeResponse = await page.request.get(`${endpoint()}/api/v1/system/notice`)
  expect(noticeResponse.status()).toBe(200)
  const notice = await noticeResponse.json()
  expect(notice.notice_version).toBeTruthy()
  const acknowledgment = await page.request.post(`${endpoint()}/api/v1/system/notice/ack`, { data: { notice_version: notice.notice_version } })
  expect(acknowledgment.status()).toBe(200)
  await page.addInitScript(() => { localStorage.setItem('qt.setup.done', 'true') })
  await page.goto(`${endpoint()}/#/acct/new`)
  await tid(page, `channel-${channel}`).click()
  await expect(tid(page, 'res-card')).toBeVisible()
  await expect(tid(page, 'next')).toBeEnabled()
  await tid(page, 'next').click()
  sequence += 1
  await tid(page, 'label').fill(`BUG-ACCEPT-${Date.now().toString(36).slice(-6)}${sequence}`.slice(0, 20))
  if (channel === 'qidian') {
    await tid(page, 'next').click()
    await tid(page, 'profile-random').click()
    await tid(page, 'next').click()
    await tid(page, 'account').fill(ACCOUNT)
    await tid(page, 'secret').fill(SECRET)
    await tid(page, 'remember').setChecked(remember)
  }
  await expect(tid(page, 'create')).toBeVisible()
}

async function assertNoCompletion(page) {
  await expect(tid(page, 'done-summary')).not.toBeVisible()
}

async function assertManualPasswordEntry(page) {
  const prompt = page.getByRole('button', { name: '输入密码登录', exact: true })
  await expect(prompt).toBeVisible()
  await prompt.click()
  const dialog = page.getByRole('dialog', { name: '输入密码登录', exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('textbox', { name: '密码(不回显、不入库)', exact: true })).toBeVisible()
  await expect(tid(page, 'password-modal')).toBeVisible()
}

async function assertNoForwardBypass(page) {
  const next = tid(page, 'next')
  if (await next.isVisible() && await next.isEnabled()) await next.click()
  await assertNoCompletion(page)
}

async function assertClearedForm(page) {
  const empty = await page.evaluate(() => {
    const input = document.querySelector('[data-testid="qt-acct-new-secret"]')
    return !input || input.value === ''
  })
  expect(empty, 'password form is cleared or removed immediately after submit').toBe(true)
}

async function assertNoStoredSecret(page, h) {
  const surfaces = await page.evaluate(async value => {
    let idbLeak = false
    const databases = indexedDB.databases ? await indexedDB.databases() : []
    for (const entry of databases) {
      if (!entry.name) continue
      const db = await new Promise((resolve, reject) => {
        const req = indexedDB.open(entry.name)
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => reject(req.error)
      })
      for (const name of db.objectStoreNames) {
        const records = await new Promise((resolve, reject) => {
          const req = db.transaction(name).objectStore(name).getAll()
          req.onsuccess = () => resolve(req.result)
          req.onerror = () => reject(req.error)
        })
        if (JSON.stringify(records).includes(value)) idbLeak = true
      }
      db.close()
    }
    return {
      local: JSON.stringify(localStorage).includes(value),
      session: JSON.stringify(sessionStorage).includes(value),
      cookie: document.cookie.includes(value), url: location.href.includes(value), indexedDB: idbLeak,
    }
  }, SECRET)
  expect(surfaces).toEqual({ local: false, session: false, cookie: false, url: false, indexedDB: false })
  expect(h.leaked, 'console/pageerror contains no password').toBe(false)
}

async function submit(page, h) {
  await tid(page, 'create').click()
  await assertClearedForm(page)
  await expect.poll(() => h.id, { message: '#2 returned the created account id' }).toBeTruthy()
  await expect.poll(() => count(h, 'start')).toBe(1)
  expect(count(h, 'create')).toBe(1)
  expect(h.calls.find(call => call.kind === 'start').id).toBe(h.id)
  await assertNoCompletion(page)
}

async function state(page, h, value, code = '') {
  h.mocks.push(`hostile account state injected through fixture: ${value}/${code}`)
  await control(page, `accounts/${h.id}/state`, {
    state: value, state_code: code,
    ...(code ? { prompt: { kind: code, text: 'Acceptance fake-device prompt' } } : {}),
  })
}

async function holdDevice(page, name) {
  return control(page, 'scenario', { name, hold_start: true, hold_login: true })
}

async function releaseDevice(page, h, phase) {
  await control(page, `accounts/${h.id}/release`, { phase })
}

async function deviceEvidence(page, h) {
  const root = process.env.QT_ACCEPT_CONTROL_URL || `${endpoint()}/__accept__`
  const response = await page.request.get(`${root}/accounts/${h.id}/evidence`)
  expect(response.ok()).toBeTruthy()
  const data = await response.json()
  h.deviceEvidence = data
  return data
}

async function evidence(info, h) {
  await info.attach('black-box-evidence', {
    body: JSON.stringify({ calls: h.calls, reads: h.reads, responses: h.responses, events: h.events, mocks: h.mocks, deviceEvidence: h.deviceEvidence, promptEvidence: h.promptEvidence, consoleSecretLeak: h.leaked }, null, 2),
    contentType: 'application/json',
  })
}

test.afterEach(async ({ page }, info) => {
  const h = observations.get(info.testId)
  if (h && info.status !== 'passed') {
    if (h.id) {
      try {
        await deviceEvidence(page, h)
        const response = await page.request.get(`${endpoint()}/api/v1/accounts/${h.id}/prompt`)
        const payload = await response.json()
        const prompt = payload.prompt || payload.data?.prompt || payload.data || payload
        h.promptEvidence = { status: response.status(), kind: prompt.kind, hasQrcode: Boolean(prompt.qrcode_png_b64), hasExpiresAt: Boolean(prompt.expires_at) }
      } catch { h.evidenceReadUnavailable = true }
    }
    await evidence(info, h)
  }
  observations.delete(info.testId)
})

async function errorResponse(route, status, code, reason) {
  await route.fulfill({ status, json: {
    ok: false, code, data: null, error: { message: 'Acceptance injected fault', reason, retryable: false, needs_human: true },
    trace_id: 'acceptance-fault',
  } })
}

for (const channel of ['qidian', 'qq']) {
  test(`AC-01 ${channel}: Next cannot bypass creation or fabricate progress`, async ({ page }, info) => {
    const h = await observe(page, info)
    await holdDevice(page, 'gate')
    await openForm(page, channel)
    await assertNoForwardBypass(page)
    expect(count(h, 'create')).toBe(0)
    expect(count(h, 'start')).toBe(0)
    await expect(tid(page, 'create')).toBeVisible()
    await expect(tid(page, 'progress')).not.toBeVisible()
    await evidence(info, h)
  })

  test(`AC-02 ${channel}: #2 then same-id #9; running alone completes`, async ({ page }, info) => {
    const h = await observe(page, info)
    await holdDevice(page, 'normal')
    await openForm(page, channel)
    await submit(page, h)
    await expect(tid(page, 'progress')).toBeVisible()
    await expect(tid(page, 'progress')).not.toContainText(/40\s*%|APK.*\d+\s*%/)
    await assertNoForwardBypass(page)
    await releaseDevice(page, h, 'start')
    if (channel === 'qidian') await expect.poll(() => count(h, 'login')).toBe(1)
    else {
      await expect(tid(page, 'qr-img')).toBeVisible({ timeout: 30_000 })
      expect(count(h, 'login')).toBe(0)
    }
    await assertNoCompletion(page)
    await releaseDevice(page, h, 'login')
    await expect(tid(page, 'done-summary')).toBeVisible()
    const backend = await deviceEvidence(page, h)
    expect(backend).toMatchObject({ state: 'running', runtime_create_count: 1, runtime_start_count: 1, state_injection_count: 0, credential_present: false })
    if (channel === 'qidian') expect(backend).toMatchObject({ device_login_count: 1, device_login_completed_count: 1, vault_write_count: 0 })
    else expect(backend.qq_login_info_count).toBeGreaterThan(0)
    expect(count(h, 'create')).toBe(1)
    expect(count(h, 'start')).toBe(1)
    await assertNoStoredSecret(page, h)
    await evidence(info, h)
  })
}

test('AC-03 start failure retries #9 with the existing id', async ({ page }, info) => {
  const h = await observe(page, info)
  await holdDevice(page, 'start-failure')
  let first = true
  await page.route('**/api/v1/accounts/*/start', async route => {
    if (first) { first = false; return errorResponse(route, 503, 'INTERNAL', 'acceptance_start_failure') }
    await route.fallback()
  })
  h.mocks.push('first #9 returns HTTP 503; second #9 reaches real API')
  await openForm(page, 'qidian')
  await submit(page, h)
  await expect(tid(page, 'retry')).toBeVisible()
  await tid(page, 'retry').click()
  await expect.poll(() => count(h, 'start')).toBe(2)
  expect(count(h, 'create')).toBe(1)
  expect(h.calls.filter(call => call.kind === 'start').map(call => call.id)).toEqual([h.id, h.id])
  await releaseDevice(page, h, 'start')
  await page.waitForTimeout(2200)
  expect(count(h, 'login'), 'a failed start clears the original one-time password').toBe(0)
  await assertManualPasswordEntry(page)
  await assertNoCompletion(page)
  await evidence(info, h)
})

test('AC-04 unremembered password is submitted once only after WAIT_PASSWORD', async ({ page }, info) => {
  const h = await observe(page, info)
  await holdDevice(page, 'password')
  await openForm(page, 'qidian', false)
  await submit(page, h)
  expect(h.calls.find(call => call.kind === 'create').hasSecret).toBe(false)
  expect(count(h, 'login')).toBe(0)
  await assertNoStoredSecret(page, h)
  await releaseDevice(page, h, 'start')
  await expect.poll(() => count(h, 'login')).toBe(1)
  expect(h.calls.find(call => call.kind === 'login')).toMatchObject({ id: h.id, correctSecret: true, remember: false })
  await page.waitForTimeout(2200)
  expect(count(h, 'login')).toBe(1)
  await assertNoStoredSecret(page, h)
  await assertNoCompletion(page)
  expect(await deviceEvidence(page, h)).toMatchObject({ credential_present: false, vault_write_count: 0, device_login_count: 1, state_injection_count: 0 })
  await evidence(info, h)
})

test('AC-05 remembered password stays on #2/Vault/#9 and never duplicates #12', async ({ page }, info) => {
  const h = await observe(page, info)
  await holdDevice(page, 'remembered')
  await openForm(page, 'qidian', true)
  await submit(page, h)
  expect(h.calls.find(call => call.kind === 'create')).toMatchObject({ hasSecret: true, correctSecret: true, remember: true })
  await releaseDevice(page, h, 'start')
  await page.waitForTimeout(2200)
  expect(count(h, 'login')).toBe(0)
  await releaseDevice(page, h, 'login')
  await expect(tid(page, 'done-summary')).toBeVisible()
  expect(await deviceEvidence(page, h)).toMatchObject({ state: 'running', credential_present: true, vault_write_count: 1, vault_read_count: 1, device_login_count: 1, device_login_completed_count: 1, state_injection_count: 0 })
  await assertNoStoredSecret(page, h)
  await evidence(info, h)
})

for (const exit of ['cancel', 'unmount']) {
  test(`AC-06 ${exit}: a delayed #2 response never starts a new login chain`, async ({ page }, info) => {
    const h = await observe(page, info)
    await holdDevice(page, 'late-create')
    let release
    let fetched = false
    const gate = new Promise(resolve => { release = resolve })
    await page.route('**/api/v1/accounts', async route => {
      if (route.request().method() !== 'POST') return route.fallback()
      const response = await route.fetch()
      fetched = true
      await gate
      await route.fulfill({ response }).catch(() => {})
    })
    h.mocks.push('#2 real API response delayed until after cancel/unmount')
    await openForm(page, 'qidian')
    await tid(page, 'create').click()
    await expect.poll(() => fetched).toBe(true)
    try {
      if (exit === 'cancel') {
        await tid(page, 'cancel').click()
        // Cancellation may wait for the created id before soft-delete and navigation.
        release()
      } else {
        await page.evaluate(() => { location.hash = '#/acct' })
      }
      await expect(page).not.toHaveURL(/\/acct\/new/)
      await expect(page.getByTestId('qt-acct-add-qidian')).toBeVisible()
      await expect(tid(page, 'steps')).not.toBeVisible()
      release()
      await page.waitForTimeout(2500)
      expect(count(h, 'start')).toBe(0)
      expect(count(h, 'login')).toBe(0)
      if (exit === 'cancel') expect(count(h, 'delete')).toBe(1)
      await assertNoStoredSecret(page, h)
      await evidence(info, h)
    } finally { release() }
  })

  test(`AC-06 ${exit}: a delayed #9 response never triggers password login`, async ({ page }, info) => {
    const h = await observe(page, info)
    await holdDevice(page, 'late-start')
    let release
    const gate = new Promise(resolve => { release = resolve })
    await page.route('**/api/v1/accounts/*/start', async route => {
      const response = await route.fetch()
      await gate
      await route.fulfill({ response }).catch(() => {})
    })
    h.mocks.push('#9 real API response delayed until after cancel/unmount')
    await openForm(page, 'qidian')
    await tid(page, 'create').click()
    await expect.poll(() => h.id).toBeTruthy()
    await expect.poll(() => count(h, 'start')).toBe(1)
    try {
      if (exit === 'cancel') {
        page.on('dialog', dialog => dialog.accept())
        await tid(page, 'cancel').click()
        const confirmationDialog = page.getByRole('dialog')
        if (await confirmationDialog.isVisible()) {
          await confirmationDialog.getByRole('button', { name: /^(确定|确认|删除并返回)$/ }).click()
        }
      } else {
        await page.evaluate(() => { location.hash = '#/acct' })
      }
      await expect(page.getByTestId('qt-acct-add-qidian')).toBeVisible()
      await expect(tid(page, 'steps')).not.toBeVisible()
      release()
      if (exit === 'unmount') await releaseDevice(page, h, 'start')
      await page.waitForTimeout(2500)
      expect(count(h, 'login')).toBe(0)
      if (exit === 'cancel') expect(count(h, 'delete')).toBe(1)
      await assertNoStoredSecret(page, h)
      await evidence(info, h)
    } finally { release() }
  })
}

for (const fault of ['busy', 'other-409', 'http-error', 'network']) {
  test(`AC-07/08 ${fault}: automatic retries obey the bounded exception`, async ({ page }, info) => {
    const h = await observe(page, info)
    await holdDevice(page, 'login-fault')
    await page.route('**/api/v1/accounts/*/login', async route => {
      if (fault === 'network') return route.abort('failed')
      return errorResponse(route, fault === 'http-error' ? 503 : 409,
        fault === 'http-error' ? 'INTERNAL' : 'NOT_APPLICABLE', fault === 'busy' ? 'busy' : 'not_ready')
    })
    h.mocks.push(`#12 ${fault} fault; real #2/#9 and real Agent state events`)
    await openForm(page, 'qidian')
    await submit(page, h)
    await releaseDevice(page, h, 'start')
    await expect.poll(() => count(h, 'login')).toBeGreaterThan(0)
    await page.waitForTimeout(6500)
    const attempts = count(h, 'login')
    if (fault === 'busy') expect(attempts).toBeLessThanOrEqual(3)
    else expect(attempts).toBe(1)
    await state(page, h, 'login_required', 'WAIT_PASSWORD')
    await page.waitForTimeout(2200)
    expect(count(h, 'login')).toBe(attempts)
    await assertManualPasswordEntry(page)
    await assertNoCompletion(page)
    await assertNoStoredSecret(page, h)
    await evidence(info, h)
  })
}

test('AC-09 one-time password expires after five minutes of waiting', async ({ page }, info) => {
  const h = await observe(page, info)
  await page.clock.install()
  await holdDevice(page, 'expiry')
  await openForm(page, 'qidian')
  await submit(page, h)
  await page.clock.fastForward(301_000)
  h.mocks.push('Playwright browser clock advances 301 seconds; no Agent clock change')
  await releaseDevice(page, h, 'start')
  await page.clock.runFor(2500)
  expect(count(h, 'login')).toBe(0)
  await assertManualPasswordEntry(page)
  await assertNoCompletion(page)
  await assertNoStoredSecret(page, h)
  await evidence(info, h)
})

test('AC-11 accepted #12 consumes the password and does not auto-login again', async ({ page }, info) => {
  const h = await observe(page, info)
  await holdDevice(page, 'consume-on-accept')
  await openForm(page, 'qidian')
  await submit(page, h)
  await releaseDevice(page, h, 'start')
  await expect.poll(() => count(h, 'login')).toBe(1)
  await expect.poll(() => h.responses.some(response => response.kind === 'login' && [200, 202].includes(response.status))).toBe(true)
  await state(page, h, 'logging_in')
  await state(page, h, 'login_required', 'WAIT_PASSWORD')
  await page.waitForTimeout(2200)
  expect(count(h, 'login')).toBe(1)
  await assertManualPasswordEntry(page)
  await assertNoCompletion(page)
  await evidence(info, h)
})

test('AC-12 reload restores the same account and prompt without reusing its password', async ({ page }, info) => {
  const h = await observe(page, info)
  await holdDevice(page, 'reload')
  await openForm(page, 'qidian')
  await submit(page, h)
  await state(page, h, 'login_required', 'WAIT_SMS')
  await expect(tid(page, 'prompt-card')).toBeVisible()
  const readsBefore = h.reads.length
  await page.reload()
  await expect(tid(page, 'progress')).toBeVisible()
  await expect(tid(page, 'prompt-card')).toBeVisible()
  const restoredReads = h.reads.slice(readsBefore)
  expect(restoredReads).toContain(`/api/v1/accounts/${h.id}`)
  expect(restoredReads).toContain(`/api/v1/accounts/${h.id}/prompt`)
  expect(count(h, 'create')).toBe(1)
  expect(count(h, 'start')).toBe(1)
  await state(page, h, 'login_required', 'WAIT_PASSWORD')
  await page.waitForTimeout(2200)
  expect(count(h, 'login')).toBe(0)
  await assertManualPasswordEntry(page)
  await assertNoStoredSecret(page, h)
  await evidence(info, h)
})
