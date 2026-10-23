/**
 * mock ↔ 规格 形状对账闸(照 `tests/e2e/shape_audit.py` 的思路搬进控制台自己的单测)。
 *
 * 一次端到端联调(`.omc/handoffs/e2e-console-agent.md`)暴露的根因是:
 * **`console/mock` 与真 Agent 的形状差得比前端代码还远**,于是前端被养出了一堆只在 mock 下
 * 成立的假设(审计列名、邮件设置形状、公网端点键集、版本头、幂等键载体……)。
 *
 * 本测试真的把 `mock/server.mjs` 起起来,逐端点断言:
 *  1. 信封形态与 02 §3.4 / R6-55 一致(`{ok,data}` / 列表 `{ok,data:[],next_cursor}` / 顶层平铺);
 *  2. 每个响应对象的键集 **⊆ `src/api/types.ts` 里对应 interface 的键集**(类型是唯一出处);
 *  3. 规格定死的键**必须在**,docs 里不存在的键**必须不在**;
 *  4. 版本协商(426)、body 幂等键(400/409)、#28 的业务失败码(200 + ok:false)这三条行为在 mock 里也成立
 *     —— 它们正是真后端会卡住控制台的地方,mock 不复现就等于没有测试价值。
 *
 * 改 mock 或改类型,这条测试会先红:这是刻意的闸门。
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import {
  API_KEYS, ASR_KEYS, OCR_KEYS, POOL_KEYS, RETENTION_KEYS,
} from '../../src/api/settingsKeys'

const PORT = 17698
const BASE = `http://127.0.0.1:${PORT}/api/v1`
const REPO_CONSOLE = resolve(__dirname, '../..')

let child: ChildProcess | null = null

/* ───────────────── 从 types.ts 反解 interface 的顶层键集 ───────────────── */

const TYPES_SRC = readFileSync(resolve(REPO_CONSOLE, 'src/api/types.ts'), 'utf8')

interface IfaceInfo {
  keys: Set<string>
  /** 有 `[k: string]: …` 索引签名 ⇒ 允许任意键,不做 ⊆ 检查 */
  open: boolean
}

function interfaceInfo(name: string): IfaceInfo {
  const head = new RegExp(`export interface ${name}(?:\\s+extends\\s+\\w+)?\\s*\\{`).exec(TYPES_SRC)
  if (!head) throw new Error(`types.ts 里没有 interface ${name}`)
  // head[0] 以 `{` 结尾 —— 从它**之后**开始收集,最外层花括号不进 body
  let i = head.index + head[0].length
  let depth = 1
  const body: string[] = []
  for (; i < TYPES_SRC.length; i++) {
    const ch = TYPES_SRC[i]
    if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) break
    }
    body.push(ch)
  }
  const text = body.join('')
  const keys = new Set<string>()
  let open = false
  let d = 0
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()
    if (d === 0) {
      if (/^\[\s*\w+\s*:\s*string\s*\]\s*:/.test(line)) open = true
      const m = /^(\w+)\s*\??\s*:/.exec(line)
      if (m) keys.add(m[1])
    }
    d += (rawLine.match(/\{/g) ?? []).length - (rawLine.match(/\}/g) ?? []).length
    if (d < 0) d = 0
  }
  // `extends` 的父接口键也算(MailInboxDetail extends MailInboxRow)
  const ext = new RegExp(`export interface ${name}\\s+extends\\s+(\\w+)`).exec(TYPES_SRC)
  if (ext) {
    const parent = interfaceInfo(ext[1])
    for (const k of parent.keys) keys.add(k)
    open = open || parent.open
  }
  return { keys, open }
}

/* ───────────────── 起 / 停 mock ───────────────── */

async function waitReady(timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    try {
      const r = await fetch(`${BASE}/system/version`, { headers: { 'X-QT-Api-Min': '1.0' } })
      if (r.ok) return
    } catch {
      // 还没起来
    }
    if (Date.now() > deadline) throw new Error('mock 没能在超时内就绪')
    await new Promise((r) => setTimeout(r, 150))
  }
}

beforeAll(async () => {
  child = spawn(process.execPath, ['mock/server.mjs'], {
    cwd: REPO_CONSOLE,
    env: { ...process.env, MOCK_PORT: String(PORT) },
    stdio: 'ignore',
  })
  await waitReady()
}, 30000)

afterAll(() => {
  child?.kill('SIGTERM')
  child = null
})

type Env = Record<string, unknown>

async function get(path: string): Promise<{ status: number; body: Env }> {
  const res = await fetch(`${BASE}${path}`, { headers: { 'X-QT-Api-Min': '1.0' } })
  return { status: res.status, body: (await res.json()) as Env }
}

async function post(path: string, body?: unknown): Promise<{ status: number; body: Env }> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'X-QT-Api-Min': '1.0', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return { status: res.status, body: (await res.json()) as Env }
}

async function put(path: string, body?: unknown): Promise<{ status: number; body: Env }> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'PUT',
    headers: { 'X-QT-Api-Min': '1.0', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return { status: res.status, body: (await res.json()) as Env }
}

/** 信封自身的键(00 §10);检查业务键集时要先把它们摘掉 */
const ENVELOPE_KEYS = new Set(['ok', 'code', 'data', 'error', 'trace_id', 'next_cursor'])

function assertSubset(obj: Record<string, unknown>, iface: string, label: string): void {
  const info = interfaceInfo(iface)
  if (info.open) return
  const extra = Object.keys(obj).filter((k) => !info.keys.has(k))
  expect(extra, `${label}:这些键不在 ${iface} 里(mock 与类型/规格不同形)`).toEqual([])
}

function assertHas(obj: Record<string, unknown>, keys: string[], label: string): void {
  const missing = keys.filter((k) => !(k in obj))
  expect(missing, `${label}:规格定死的键缺了`).toEqual([])
}

function assertHasNot(obj: Record<string, unknown>, keys: string[], label: string): void {
  const present = keys.filter((k) => k in obj)
  expect(present, `${label}:这些键在 docs 里不存在,mock 不该造`).toEqual([])
}

/* ───────────────── 1. 信封 ───────────────── */

describe('信封(00 §10 / R6-55)', () => {
  // ⚠️ 这条必须在任何 `POST /system/selftest` 之前跑(它验的是「一轮都没跑过」的状态)
  it('#79b 自检:从没跑过回 {ok:true, data:null},不是 404', async () => {
    const { status, body } = await get('/system/selftest')
    expect(status).toBe(200)
    expect(body.ok).toBe(true)
    expect(body.data).toBeNull()
  })

  it('每个响应都带 ok', async () => {
    for (const p of ['/accounts', '/sessions', '/messages', '/audit', '/resources', '/system/health']) {
      const { body } = await get(p)
      expect(body.ok, `${p} 缺 ok`).toBe(true)
    }
  })

  it('列表端点统一 {data:[…], next_cursor}(C-42)', async () => {
    for (const p of ['/accounts', '/sessions', '/messages', '/audit', '/capabilities', '/device-profiles/templates']) {
      const { body } = await get(p)
      expect(Array.isArray(body.data), `${p} 的 data 不是数组`).toBe(true)
      expect('next_cursor' in body, `${p} 缺 next_cursor`).toBe(true)
    }
  })

  it('字面键集的端点顶层平铺、不再包 data(#69/#72/#73/#77/#102)', async () => {
    for (const p of ['/resources', '/system/health', '/system/version', '/system/metrics', '/system/public-endpoint']) {
      const { body } = await get(p)
      expect(body.data, `${p} 不该包 data(R6-55:顶层平铺)`).toBeUndefined()
    }
  })

  it('错误信封:code 在顶层、error 四键齐全', async () => {
    const { status, body } = await get('/jobs/does-not-exist')
    expect(status).toBe(404)
    expect(body.ok).toBe(false)
    expect(body.code).toBe('TARGET_NOT_FOUND')
    const err = body.error as Record<string, unknown>
    assertHas(err, ['message', 'retryable', 'needs_human'], '错误信封 error')
  })
})

/* ───────────────── 2. 键集 ⊆ TypeScript 类型 ───────────────── */

describe('响应键集 ⊆ src/api/types.ts', () => {
  it('#1 Account:不带 settings 子对象(裁决④)', async () => {
    const { body } = await get('/accounts')
    const rows = body.data as Record<string, unknown>[]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) {
      assertSubset(r, 'Account', '#1 Account')
      assertHasNot(r, ['settings'], '#1 Account')
    }
  })

  it('#26 Session:最后消息时间是 last_msg_at(裁决②),不是 last_ts', async () => {
    const { body } = await get('/sessions')
    for (const r of body.data as Record<string, unknown>[]) {
      assertSubset(r, 'SessionRow', '#26 Session')
      assertHas(r, ['last_msg_at'], '#26 Session')
      assertHasNot(r, ['last_ts'], '#26 Session')
    }
  })

  it('#48 Message:不下发事件专属三字段,也不下发 needs_review', async () => {
    const { body } = await get('/messages?limit=5')
    for (const r of body.data as Record<string, unknown>[]) {
      assertSubset(r, 'Message', '#48 Message')
      assertHasNot(r, ['lag_s', 'late', 'origin', 'needs_review'], '#48 Message')
    }
  })

  it('#95 审计:十列定死,cost_ms/ip/http_status 在 detail_json 里(R6-58 (ag))', async () => {
    const { body } = await get('/audit?kind=command')
    const rows = body.data as Record<string, unknown>[]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) {
      assertSubset(r, 'AuditRow', '#95 审计')
      assertHas(r, [
        'id', 'ts_ms', 'kind', 'transport', 'actor', 'action', 'account_id', 'trace_id',
        'result_code', 'detail_json',
      ], '#95 审计')
      // 这些是前端此前按平铺列取的名字 —— 一律不存在
      assertHasNot(r, ['ts', 'op', 'code', 'method', 'path', 'sig_ok', 'args_digest', 'source'], '#95 审计')
      expect(typeof r.ts_ms, '#95 ts_ms 必须是 epoch 毫秒').toBe('number')
      expect(() => JSON.parse(String(r.detail_json)), '#95 detail_json 必须是可解析 JSON').not.toThrow()
    }
  })

  it('#102 公网端点:键集逐字按 02,不得有 matches/history/configured_host', async () => {
    const { body } = await get('/system/public-endpoint')
    const biz = Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k)))
    assertSubset(biz, 'PublicEndpoint', '#102 公网端点')
    assertHas(biz, ['public_ip', 'checked_at', 'changed_at', 'probe', 'configured_domain'], '#102 公网端点')
    assertHasNot(biz, ['matches', 'history', 'configured_host', 'dns_resolved_ip', 'last_changed_at'], '#102 公网端点')
  })

  it('#73 版本:agent 是 {version} 对象(裁决①)', async () => {
    const { body } = await get('/system/version')
    const biz = Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k)))
    assertSubset(biz, 'SystemVersion', '#73 版本')
    expect(typeof biz.agent, '#73 agent 必须是对象,不是裸字符串').toBe('object')
    expect((biz.agent as Record<string, unknown>).version).toBeTruthy()
  })

  it('#72 健康:checks 另带 per-account 子键(R6-58 (y))', async () => {
    const { body } = await get('/system/health')
    const biz = Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k)))
    assertSubset(biz, 'SystemHealth', '#72 健康')
    const checks = biz.checks as Record<string, unknown>
    expect(checks.accounts, '#72 checks.accounts 缺席 ⇒ 账号健康行无处取数').toBeTruthy()
  })

  it('#72 健康:**两种形态都不注入 `trace_id`**(02 §3.4 例外① = R6-62 Ⅵ W1,按 path 整端点)', async () => {
    const { body } = await get('/system/health')
    expect(body.checks, '先确认拿到的是全量体').toBeTruthy()
    expect('trace_id' in body, 'mock 给 health 注了 trace_id,与现行口径反着(M-13)').toBe(false)
    // 对照组:同一个 mock 的其它端点照常注入 —— 证明不是 mock 整体不发 trace
    expect('trace_id' in (await get('/system/version')).body, '对照组 /system/version 该有 trace_id').toBe(true)
  })

  it('#77 监控:两组并排 + ours.procs_detail(R6-58 (aa));扁平两键不嵌套(R6-30)', async () => {
    const { body } = await get('/system/metrics?snapshot=1')
    const biz = Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k)))
    assertSubset(biz, 'MetricsSnapshot', '#77 监控')
    const ours = biz.ours as Record<string, unknown>
    assertHas(ours, ['procs', 'procs_detail', 'accounts'], '#77 ours')
    const dw = biz.disk_watermark as Record<string, unknown>
    assertHas(dw, ['last_cleanup_at', 'last_cleanup_freed_mb'], '#77 disk_watermark')
    assertHasNot(dw, ['last_cleanup'], '#77 disk_watermark(R6-30:扁平两键,不嵌套)')
  })

  it('#69 资源池:pending 无值时是空串、pending_expires_at 是 ISO 或 null(R6-58 (j))', async () => {
    const { body } = await get('/resources')
    const biz = Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k)))
    assertSubset(biz, 'ResourcePool', '#69 资源池')
    const slots = ((biz.pools as Record<string, Record<string, Record<string, unknown>>>).windows).wechat_slots
    assertHas(slots, ['used', 'max', 'holder', 'pending', 'pending_expires_at', 'pending_login_session_id'], '#69 槽位')
    expect(typeof slots.pending, '#69 pending 必须是字符串(无 pending 时空串,不是 null)').toBe('string')
    const exp = slots.pending_expires_at
    expect(exp === null || typeof exp === 'string', '#69 pending_expires_at 必须是 ISO 字符串或 null').toBe(true)
  })

  it('#76 observed:两键 {data, targets},行带 id 与 in_config(R6-58 (dc))', async () => {
    const { body } = await get('/system/probes?kind=observed')
    assertHas(body, ['data', 'targets'], '#76 observed')
    for (const r of body.data as Record<string, unknown>[]) {
      assertSubset(r, 'ObservedProbeRow', '#76 observed 行')
      assertHas(r, ['id', 'in_config'], '#76 observed 行')
      expect(typeof r.id, '#76 行 id 必须是整数(不得用行下标)').toBe('number')
    }
  })

  it('#88 settings/mail:scopes 每块逐字四键 {override,route_id,enabled,inbound,outbound}(R6-58 (ac))', async () => {
    const { body } = await get('/settings/mail')
    const data = body.data as Record<string, unknown>
    assertHas(data, ['enabled', 'require_signature', 'template_version', 'scopes'], '#88 mail 组')
    assertHasNot(data, ['archive'], '#88 mail 组')
    const scopes = data.scopes as Record<string, Record<string, unknown>>
    expect(Object.keys(scopes).sort()).toEqual(['default', 'qidian', 'qq', 'wechat'])
    for (const [name, cfg] of Object.entries(scopes)) {
      expect(Object.keys(cfg).sort(), `#88 scopes.${name}`).toEqual(
        ['enabled', 'inbound', 'outbound', 'override', 'route_id'],
      )
      // 密码类只写不读:读回来只有 *_ref
      const inbound = cfg.inbound as Record<string, unknown>
      assertHasNot(inbound, ['secret', 'pass', 'password'], `#88 scopes.${name}.inbound`)
    }
  })

  it('#24/#21/#68b:行键集 ⊆ 类型', async () => {
    const profiles = await get('/device-profiles/templates')
    for (const r of profiles.body.data as Record<string, unknown>[]) {
      assertSubset(r, 'DeviceProfileTemplate', '#24 机型档案')
    }
    const caps = await get('/capabilities')
    for (const r of caps.body.data as Record<string, unknown>[]) {
      assertSubset(r, 'CapabilityDef', '#21 能力目录')
    }
    const pend = await get('/mail/pending-confirms')
    for (const r of pend.body.data as Record<string, unknown>[]) {
      assertSubset(r, 'PendingConfirm', '#68b 待确认')
      assertHasNot(r, ['confirm_via', 'confirm_nonce'], '#68b 待确认(v1 无此两列)')
    }
  })

  it('#107 作业:时间键是 ISO *_at,不是 *_ms(02 #107 / 00 §6)', async () => {
    const started = await post('/system/cleanup/run')
    expect(started.status).toBe(202)
    const jobId = String(started.body.job_id)
    const { body } = await get(`/jobs/${jobId}`)
    const job = body.data as Record<string, unknown>
    assertSubset(job, 'Job', '#107 作业')
    assertHas(job, ['job_id', 'kind', 'state', 'progress', 'created_at', 'updated_at'], '#107 作业')
    assertHasNot(job, ['created_ms', 'updated_ms', 'expires_ms'], '#107 作业')
    expect(String(job.created_at)).toMatch(/^\d{4}-\d{2}-\d{2}T/)
  })

  it('#75/#76 探测行:结论列是 status、诊断是 detail(键名以后端为准)', async () => {
    const { body } = await get('/system/probes')
    for (const r of body.data as Record<string, unknown>[]) {
      assertSubset(r, 'ProbeRow', '#76 探测行')
      assertHas(r, ['side', 'target', 'status'], '#76 探测行')
    }
  })

  it('#79 自检:data 是一轮的对象(不是行数组),行由 selftestRows() 派生', async () => {
    await post('/system/selftest')
    const { body } = await get('/system/selftest')
    const run = body.data as Record<string, unknown>
    assertSubset(run, 'SelftestRun', '#79 自检一轮')
    assertHas(run, ['redroid_boot_ms', 'napcat_ok', 'winagent_ok', 'probes'], '#79 自检一轮')
    expect(Array.isArray(run.probes)).toBe(true)
  })

  it('#74 环境快照:Windows 侧与 WSL 侧分成两半(windows 为 null 时带 windows_error)', async () => {
    const { body } = await get('/system/env')
    const biz = Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k)))
    assertSubset(biz, 'SystemEnv', '#74 环境快照')
    assertHas(biz, ['windows', 'wsl', 'wslconfig', 'reboot_required'], '#74 环境快照')
    // 这几个是前端此前自造的平铺键 —— 真后端没有
    assertHasNot(biz, ['net_state', 'proxy', 'vpn_adapter', 'docker_cidr', 'docker_conflict', 'pending_restart'], '#74 环境快照')
    const wsl = biz.wsl as Record<string, unknown>
    assertSubset(wsl, 'SystemEnvWsl', '#74 wsl 侧')
  })

  it('#86 合规告知:顺带回「勾过没有」(ack_ms/acked_version),控制台不本地存判据', async () => {
    const { body } = await get('/system/notice')
    assertHas(body, ['notice_version', 'text', 'ack_ms', 'acked_version'], '#86 合规告知')
  })

  it('#90 API 客户端:键集 ⊆ ApiClientRow 且不回 secret', async () => {
    const { body } = await get('/settings/api-clients')
    for (const r of body.data as Record<string, unknown>[]) {
      assertSubset(r, 'ApiClientRow', '#90 API 客户端')
      assertHasNot(r, ['token', 'secret'], '#90 API 客户端(一次性返回,列表不回)')
    }
  })

})

/* ───────────────── 3. 三条会卡住控制台的行为 ───────────────── */

describe('mock 必须复现真后端会卡住控制台的三处(E-01 / E-02 / E-03)', () => {
  it('E-01:声明比服务端更高的 X-QT-Api-Min ⇒ 426 UPGRADE_REQUIRED', async () => {
    const res = await fetch(`${BASE}/accounts`, { headers: { 'X-QT-Api-Min': '1.3' } })
    expect(res.status).toBe(426)
    const body = (await res.json()) as Env
    expect(body.code).toBe('UPGRADE_REQUIRED')
  })

  it('E-02:建号不带 body 的 idempotency_key ⇒ 400 idempotency_key_required', async () => {
    const { status, body } = await post('/accounts', {
      channel: 'qidian', label: '测试', login: { mode: 'password' },
    })
    expect(status).toBe(400)
    expect(body.code).toBe('INVALID_ARGS')
    expect((body.error as Record<string, unknown>).reason).toBe('idempotency_key_required')
  })

  it('E-02:带 body 幂等键 ⇒ 201 Account;同键重放 ⇒ 409 IDEMPOTENT_REPLAY + 同一份 data', async () => {
    const key = `e2e-${Date.now()}`
    const first = await post('/accounts', {
      channel: 'qidian', label: '测试', login: { mode: 'password' }, idempotency_key: key,
    })
    expect(first.status).toBe(201)
    const created = first.body.data as Record<string, unknown>
    expect(created.id).toBeTruthy()

    const again = await post('/accounts', {
      channel: 'qidian', label: '测试', login: { mode: 'password' }, idempotency_key: key,
    })
    expect(again.status).toBe(409)
    expect(again.body.code).toBe('IDEMPOTENT_REPLAY')
    expect((again.body.data as Record<string, unknown>).id).toBe(created.id)
  })

  it('E-03:#28 的业务失败码是 200 + ok:false + 完整 CommandResult', async () => {
    const { status, body } = await post('/accounts/qd01/commands', {
      op: 'send_text',
      args: { session: 'qd01:415011447', text: '#unconfirmed 报价' },
      idempotency_key: `cmd-${Date.now()}`,
    })
    expect(status, '业务失败**不是**传输错误,HTTP 必须是 200').toBe(200)
    expect(body.ok).toBe(false)
    expect(body.code).toBe('SEND_CALLED_BUT_UNCONFIRMED')
    // CommandResult 顶层自带业务 data —— 不能被拆、也不能只剩 error.message
    assertHas(body, ['code', 'cost_ms', 'trace_id', 'source', 'state_before', 'state_after', 'data'], '#28 CommandResult')
    assertSubset(
      Object.fromEntries(Object.entries(body).filter(([k]) => k !== 'next_cursor')),
      'CommandResult',
      '#28 CommandResult',
    )
  })

  it('#76b:传 targets 一律 400 use_observed_ids;observed_ids 是全集(空数组合法)', async () => {
    const bad = await fetch(`${BASE}/settings/probe`, {
      method: 'PUT',
      headers: { 'X-QT-Api-Min': '1.0', 'Content-Type': 'application/json' },
      body: JSON.stringify({ targets: ['a:1'] }),
    })
    expect(bad.status).toBe(400)
    expect(((await bad.json()) as Env).error).toMatchObject({ reason: 'use_observed_ids' })

    const okRes = await fetch(`${BASE}/settings/probe`, {
      method: 'PUT',
      headers: { 'X-QT-Api-Min': '1.0', 'Content-Type': 'application/json' },
      body: JSON.stringify({ observed_ids: [] }),
    })
    expect(okRes.status, '空数组 = 清空全部采纳,是合法入参').toBe(200)
    const body = (await okRes.json()) as Env
    assertHas(body, ['adopted', 'targets'], '#76b 出参')
    expect(body.adopted).toEqual([])
    expect(body.targets).toEqual([])
  })

  it('#88 没有 compliance 这个组(合规告知走 #86/#87)', async () => {
    const { status } = await get('/settings/compliance')
    expect(status).toBe(404)
    const ack = await post('/system/notice/ack', { notice_version: 'v1' })
    expect(ack.status).toBe(200)
    expect(ack.body.ok).toBe(true)
  })
})

/* ───────────────── 5. N-6:mock 与**规格**不符的 12 处(e2e-recheck §3.2 B 表 M-1~M-12) ───────────────── */

async function req(
  method: string, path: string, body?: unknown,
): Promise<{ status: number; body: Env }> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { 'X-QT-Api-Min': '1.0', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return { status: res.status, body: (await res.json()) as Env }
}

describe('N-6:mock 对齐规格(M-1 ~ M-12)', () => {
  it('M-3 🔴 #84 wsl-restart 不带 confirm 必须**拒**(基线 §11.6 [NOSHUTDOWN] / 02 #84)', async () => {
    const { status, body } = await post('/system/wsl-restart', { mode: 'shutdown' })
    expect(status, 'mock 放行会让页面开发以为不需要二次确认').toBe(400)
    expect(body.ok).toBe(false)
    expect(body.code).toBe('INVALID_ARGS')
    expect(body.error).toMatchObject({ reason: 'confirm_required' })
  })

  it('M-3 带 confirm:true 才放行(控制台客户端恒带)', async () => {
    const { status, body } = await post('/system/wsl-restart', { mode: 'shutdown', confirm: true })
    expect(status).toBe(200)
    expect(body.ok).toBe(true)
  })

  it('M-2 #51:fmt=eml 与 with_media=zip 明着 400,不静默降级(R6-62 (g))', async () => {
    const eml = await post('/messages/export', { fmt: 'eml', with_media: 'none', filter: {} })
    expect(eml.status).toBe(400)
    expect(eml.body.error).toMatchObject({ reason: 'unsupported_fmt' })

    const zip = await post('/messages/export', { fmt: 'jsonl', with_media: 'zip', filter: {} })
    expect(zip.status).toBe(400)
    expect(zip.body.error).toMatchObject({ reason: 'unsupported_with_media' })

    const good = await post('/messages/export', { fmt: 'jsonl', with_media: 'none', filter: {} })
    expect(good.status).toBe(202)
  })

  it('M-1 #28 的 409 IDEMPOTENT_REPLAY:响应体是**顶层平铺**的完整 CommandResult', async () => {
    const key = `replay-${Date.now()}`
    const first = await post('/accounts/qd01/commands', {
      op: 'send_text', args: { session: 'qd01:415011447', text: '首发' }, idempotency_key: key,
    })
    expect(first.status).toBe(200)

    const again = await post('/accounts/qd01/commands', {
      op: 'send_text', args: { session: 'qd01:415011447', text: '首发' }, idempotency_key: key,
    })
    expect(again.status).toBe(409)
    expect(again.body.code).toBe('IDEMPOTENT_REPLAY')
    // 顶层平铺 = CommandResult 自己就是信封:`data` 是**业务 data**(message_id 之类),
    // 不是「再包一层完整 CommandResult」。M-1 要挡的正是后者。
    const replayData = (again.body.data ?? {}) as Env
    expect(
      Object.keys(replayData).filter((k) => ['ok', 'code', 'cost_ms', 'source', 'state_before'].includes(k)),
      '把完整 CommandResult 包进 data 了 —— 规格与真后端都是顶层平铺',
    ).toEqual([])
    assertHas(again.body, ['cost_ms', 'trace_id', 'source', 'state_before', 'state_after'], '#28 重放体')
    expect(again.body.trace_id, '重放要回首次那条 trace').toBe(first.body.trace_id)
  })

  it('M-4 #75 mode=sample:出参顶层平铺 {sampled_at,duration_s,rows,skipped},不包 data', async () => {
    const { status, body } = await post('/system/probe', { mode: 'sample', duration_s: 5 })
    expect(status).toBe(200)
    expect(body.data, 'R6-58 (de) 是字面键集 ⇒ 顶层平铺').toBeUndefined()
    assertHas(body, ['sampled_at', 'duration_s', 'rows', 'skipped'], '#75 sample')
    expect(body).not.toHaveProperty('run_id')
  })

  it('M-5 #19 /accounts/{id}/state 存在,且是六键 + trace_id 的**闭集**(R6-62 (b))', async () => {
    const { status, body } = await get('/accounts/qd01/state')
    expect(status, 'mock 原先 404').toBe(200)
    const six = ['state', 'state_code', 'state_reason', 'error_since_ms', 'enabled', 'last_seen_at']
    assertHas(body, six, '#19')
    expect(new Set(Object.keys(body))).toEqual(new Set([...six, 'ok', 'trace_id']))
  })

  it('M-6 #88 的 group 枚举逐字含 runtime / pool / events / log 四组', async () => {
    for (const g of ['runtime', 'pool', 'events', 'log']) {
      const { status, body } = await get(`/settings/${g}`)
      expect(status, `/settings/${g} 应在 #88 的枚举里`).toBe(200)
      expect(body.group).toBe(g)
      expect(typeof body.data).toBe('object')
    }
  })

  it('M-7 #79 GET /system/selftest/{run_id}:跑过能查到,乱填的 run_id 404', async () => {
    const run = await post('/system/selftest')
    expect(run.status).toBe(202)
    const runId = String(run.body.run_id)

    const got = await get(`/system/selftest/${runId}`)
    expect(got.status, 'mock 原先只有不带 run_id 的 #79b').toBe(200)
    expect((got.body.data as Env).run_id).toBe(runId)

    const missing = await get('/system/selftest/st_nope')
    expect(missing.status, '#79 用不存在的 run_id 是 404,与 #79b「没跑过 ⇒ data:null」是两回事').toBe(404)
  })

  it('M-8 #87 notice/ack:版本对不上必须拒,否则合规判据失真', async () => {
    const bad = await post('/system/notice/ack', { notice_version: 'v0-old' })
    expect(bad.status).toBe(400)
    expect(bad.body.error).toMatchObject({ reason: 'notice_version_mismatch' })

    const cur = await get('/system/notice')
    const good = await post('/system/notice/ack', { notice_version: cur.body.notice_version })
    expect(good.status).toBe(200)
  })

  it('M-9 #73 /system/version 不造 docs 里不存在的五个键', async () => {
    const { body } = await get('/system/version')
    assertHasNot(
      body,
      ['kernel_state', 'wsl_state', 'distro', 'migration', 'wa_schema_version'],
      '#73(第一轮 S-09,裁决① = 以后端现实现为准)',
    )
    assertSubset(
      Object.fromEntries(Object.entries(body).filter(([k]) => !ENVELOPE_KEYS.has(k))),
      'SystemVersion',
      '#73',
    )
  })

  it('M-10 #3 Account.identity 不造 brand / model / serialno(那是 #24 档案库的键)', async () => {
    const { body } = await get('/accounts')
    for (const r of body.data as Env[]) {
      const identity = (r.identity ?? {}) as Env
      assertHasNot(identity, ['brand', 'model', 'serialno'], `#3 ${String(r.id)} 的 identity`)
    }
  })

  it('M-11 #22 PATCH /accounts/{id}/settings 的出参是 Account,不带 adb_state', async () => {
    const { status, body } = await req('PATCH', '/accounts/qd01/settings', { auto_recover: true })
    expect(status).toBe(200)
    const row = body.data as Env
    expect(row, '02 #22 的出参是 Account').toBeTruthy()
    expect(row.id).toBe('qd01')
    assertSubset(row, 'Account', '#22 出参')
    assertHasNot(row, ['adb_state'], '#22 出参(adb_state 只属于 #100)')
  })

  it('M-12 #91 建 API 客户端:201 + 顶层平铺 + 一次性明文 token;列表端点永不回 token', async () => {
    const { status, body } = await post('/settings/api-clients', { name: 'shape-test', level: 'read' })
    expect(status, '建资源按 00 §10 / #2 的惯例是 201').toBe(201)
    expect(body.ok).toBe(true)
    // R6-55 二选一:ApiClient 不在 00 §7 的对象清单里 ⇒ 顶层平铺
    expect(body.data, '既包 data 又在顶层放 token 是 B-1 那种两头占').toBeUndefined()
    assertHas(body, ['app_id', 'token'], '#91')
    expect(typeof body.token).toBe('string')
    expect(String(body.token).length).toBeGreaterThan(0)

    const list = await get('/settings/api-clients')
    for (const r of list.body.data as Env[]) {
      assertHasNot(r, ['token', 'secret', 'secret_hash'], '#90 列表行')
      assertSubset(r, 'ApiClientRow', '#90 列表行')
    }
  })

  it('#92 轮换同样一次性下发明文 + 旧凭据宽限期', async () => {
    const { status, body } = await post('/settings/api-clients/ibquote/rotate')
    expect(status).toBe(200)
    assertHas(body, ['token', 'grace_minutes'], '#92')
  })
})

/* ───────────────── 6. 后端第二批端点在 mock 里的最小实现 ───────────────── */

describe('后端第二批新端点(诚实回 503/409,不伪造)', () => {
  it('#16 登出按通道分界:微信 202 / QQ 409 channel_no_logout / 企点 503 logout_backend_missing', async () => {
    const wx = await post('/accounts/wx01/logout')
    expect(wx.status).toBe(202)
    expect(wx.body.via).toBe('winagent')

    const qq = await post('/accounts/qq01/logout')
    expect(qq.status).toBe(409)
    expect(qq.body.error).toMatchObject({ reason: 'channel_no_logout' })

    const qd = await post('/accounts/qd01/logout')
    expect(qd.status).toBe(503)
    expect(qd.body.error).toMatchObject({ reason: 'logout_backend_missing' })
  })

  it('#8 彻底删除:confirm 要逐字等于 id,且须先软删', async () => {
    const mismatch = await post('/accounts/qd02/purge', { confirm: 'nope' })
    expect(mismatch.status).toBe(400)
    expect(mismatch.body.error).toMatchObject({ reason: 'confirm_mismatch' })

    const notSoftDeleted = await post('/accounts/qd02/purge', { confirm: 'qd02' })
    expect(notSoftDeleted.status).toBe(409)
    expect(notSoftDeleted.body.error).toMatchObject({ reason: 'not_soft_deleted' })
  })

  it('#54 消息清除:无 confirm 一行都不删(400 confirm_required)', async () => {
    const no = await post('/messages/purge', { account_id: 'qd01', mode: 'all' })
    expect(no.status).toBe(400)
    expect(no.body.error).toMatchObject({ reason: 'confirm_required' })

    const yes = await post('/messages/purge', { account_id: 'qd01', mode: 'text_only', confirm: true })
    expect(yes.status).toBe(202)
    expect(typeof yes.body.job_id).toBe('string')
  })

  it('#53 ASR 没有执行体 ⇒ 如实 503 asr_backend_missing(不伪造转写结果)', async () => {
    const { status, body } = await post('/messages/m1/asr')
    expect(status).toBe(503)
    expect(body.error).toMatchObject({ reason: 'asr_backend_missing' })
  })

  it('#83 停机须 confirm:true', async () => {
    const no = await post('/system/shutdown', {})
    expect(no.status).toBe(400)
    expect(no.body.error).toMatchObject({ reason: 'confirm_required' })
  })

  // ⚠️ 这条必须**放在最后**:drain 之后 mock 的写操作一律 503(与真后端一样没有逆操作)
  it('#82 drain 之后:写操作一律 503 draining,只读照常', async () => {
    const drain = await post('/system/drain', { timeout_s: 1 })
    expect(drain.status).toBe(200)
    assertHas(drain.body, ['drained', 'inflight', 'inflight_before', 'waited_s', 'stopped_accounts'], '#82')

    const write = await post('/accounts/qd01/commands', {
      op: 'send_text', args: { session: 'qd01:415011447', text: '排空后' }, idempotency_key: `d-${Date.now()}`,
    })
    expect(write.status).toBe(503)
    expect(write.body.error).toMatchObject({ reason: 'draining' })

    const read = await get('/accounts')
    expect(read.status, '只读在排空期间照常可用').toBe(200)
  })
})

/* ───────────────── 7. request( 调用点审计:data 之外还有业务键的端点 ───────────────── */

describe('信封里 data 之外的业务键不能被静默丢掉(N-1 同型)', () => {
  it('#89 PUT /settings/{group}:restart_required / config_written 在 data 之外的顶层', async () => {
    const cur = await get('/settings/log')
    const { status, body } = await req('PUT', '/settings/log', cur.body.data as Env)
    expect(status).toBe(200)
    expect(body.data, '组值在 data 里').toBeTruthy()
    assertHas(body, ['group', 'restart_required', 'config_written'], '#89')
    expect(body.restart_required, 'v1 除 resources 外一律需要重启').toBe(true)
  })

  it('#21 /capabilities:capabilities_version 在 data 之外的顶层', async () => {
    const { body } = await get('/capabilities')
    assertHas(body, ['capabilities_version'], '#21')
  })

  it('#76 observed:targets 在 data 之外的顶层', async () => {
    const { body } = await get('/system/probes?kind=observed')
    assertHas(body, ['targets'], '#76 observed')
  })

  it('#79b 自检:run_id 在 data 之外的顶层', async () => {
    const { body } = await get('/system/selftest')
    assertHas(body, ['run_id'], '#79b')
  })
})

/* ───────────────── 8. C-42 统一分页(N-2 的 mock 侧闸门) ─────────────────
 *
 * 后端第三批把 `limit/cursor → next_cursor` 铺到了 `#1 /accounts`、`#26 /sessions`、
 * `#58 /mail/inbox`、`#61 /mail/outbox`(backend-api-3 §1②)。mock 若还把 `limit` 静默忽略、
 * `next_cursor` 恒 null,前端就会被养出「翻不翻页都一样」的假设 —— 那正是 N-2 的成因。
 */

/** C-42 铺开的四个列表端点,以及各自的排序列(降序) */
const PAGED = [
  { path: '/accounts', tsKey: 'created_at' },
  { path: '/sessions', tsKey: 'last_msg_at' },
  { path: '/mail/inbox', tsKey: 'received_at' },
  { path: '/mail/outbox', tsKey: 'created_at' },
] as const

/** 按游标一路翻到底,回所有行的 id(顺序保留);`from` = 起始游标(不给即从第一页起) */
async function drainPages(path: string, limit: number, from: string | null = null): Promise<string[]> {
  const ids: string[] = []
  let cursor: string | null = from
  for (let guard = 0; guard < 50; guard++) {
    const q = `${path}?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
    const { status, body } = await get(q)
    expect(status, `${q} 应 200`).toBe(200)
    for (const r of body.data as Record<string, unknown>[]) ids.push(String(r.id))
    cursor = (body.next_cursor as string | null) ?? null
    if (!cursor) return ids
  }
  throw new Error(`${path} 翻页没有终点(next_cursor 一直非空?)`)
}

describe('C-42 统一分页(#1 / #26 / #58 / #61)', () => {
  it('limit 真生效,且 next_cursor 仅在本页满 limit 时非空', async () => {
    for (const { path } of PAGED) {
      const { body } = await get(`${path}?limit=2`)
      const rows = body.data as unknown[]
      expect(rows.length, `${path} 的 limit=2 被忽略了`).toBeLessThanOrEqual(2)
      // 这四个端点的 mock 数据都 >2 行 ⇒ 第一页必满、必给游标
      expect(rows.length, `${path} mock 数据不足 3 行,测不出翻页`).toBe(2)
      expect(typeof body.next_cursor, `${path} 本页满 limit 却没给 next_cursor`).toBe('string')
    }
  })

  it('翻到底:不重不漏,且与不分页的全量一致', async () => {
    for (const { path } of PAGED) {
      const all = ((await get(`${path}?limit=500`)).body.data as Record<string, unknown>[]).map((r) => String(r.id))
      const paged = await drainPages(path, 2)
      expect(new Set(paged).size, `${path} 翻页有重复行`).toBe(paged.length)
      expect([...paged].sort(), `${path} 翻页与全量对不上`).toEqual([...all].sort())
      // 末页不满 limit ⇒ next_cursor 必须是 null(否则客户端会一直空转)
      expect(paged.length).toBe(all.length)
    }
  })

  it('排序列严格降序,且游标是「定位」不是 OFFSET(第二页首行 < 第一页末行)', async () => {
    for (const { path, tsKey } of PAGED) {
      const p1 = await get(`${path}?limit=2`)
      const rows1 = p1.body.data as Record<string, unknown>[]
      expect(Date.parse(String(rows1[0][tsKey])), `${path} 的 ${tsKey} 不是降序`)
        .toBeGreaterThanOrEqual(Date.parse(String(rows1[1][tsKey])))
      const p2 = await get(`${path}?limit=2&cursor=${encodeURIComponent(String(p1.body.next_cursor))}`)
      const rows2 = p2.body.data as Record<string, unknown>[]
      expect(Date.parse(String(rows2[0][tsKey])), `${path} 第二页没有接着第一页末行往下走`)
        .toBeLessThanOrEqual(Date.parse(String(rows1[1][tsKey])))
    }
  })

  it('非法 limit 必须报错(422 + INVALID_ARGS + pointer=/limit),不能被静默忽略', async () => {
    for (const { path } of PAGED) {
      for (const bad of ['0', '-1', '99999', 'abc']) {
        const { status, body } = await get(`${path}?limit=${bad}`)
        // 真后端用 FastAPI Query(100, ge=1, le=500) ⇒ 422;信封仍是 INVALID_ARGS(backend-api-3 §1②)
        expect(status, `${path}?limit=${bad} 应当报错`).toBe(422)
        expect(body.code).toBe('INVALID_ARGS')
        const details = (body.error as Record<string, unknown>).details as { pointer?: string }[]
        expect(details?.[0]?.pointer).toBe('/limit')
      }
    }
  })

  it('乱码 cursor → 400 bad_cursor(而不是把整表当一页吐回来)', async () => {
    for (const { path } of PAGED) {
      const { status, body } = await get(`${path}?cursor=not-a-real-cursor`)
      expect(status, `${path} 对伪造游标应当 400`).toBe(400)
      expect(body.code).toBe('INVALID_ARGS')
      expect(body.error).toMatchObject({ reason: 'bad_cursor' })
    }
  })

  it('游标稳定:翻页中途插入新行,老行不重复也不丢(G-16 双键定位)', async () => {
    const p1 = await get('/accounts?limit=2')
    const first = (p1.body.data as Record<string, unknown>[]).map((r) => String(r.id))
    // 中途建一个新号(created_at = 现在 ⇒ 按降序它属于**第一页之前**,不该挤进后面的页)
    const made = await post('/accounts', {
      channel: 'qq', label: '分页守卫', login: { mode: 'qrcode' }, idempotency_key: `page-${Date.now()}`,
    })
    expect(made.status).toBe(201)
    const rest = await drainPages('/accounts', 2, String(p1.body.next_cursor))
    expect(rest.filter((id) => first.includes(id)), '老行在后续页里重复了').toEqual([])
    expect(new Set(rest).size).toBe(rest.length)
  })

  it('#1 /accounts 默认按创建时间降序(前端不再自排)', async () => {
    const rows = (await get('/accounts')).body.data as Record<string, unknown>[]
    const ts = rows.map((r) => Date.parse(String(r.created_at)))
    expect(ts, '/accounts 默认顺序不是 created_at 降序').toEqual([...ts].sort((a, b) => b - a))
  })
})

/* ───────────────── 9. #67b GET /mail/hmac-keys 的新出参 ───────────────── */

describe('#67b 短名表(R6-58 (ac) 指名的唯一来源)', () => {
  it('出参恰六键,且一个字节的密钥都不出现', async () => {
    const { status, body } = await get('/mail/hmac-keys')
    expect(status).toBe(200)
    const rows = body.data as Record<string, unknown>[]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) {
      assertHas(r, ['short_name', 'sender', 'route_id', 'enabled', 'secret_ref', 'created_at'], '#67b')
      // 🔴 明文只在 #67 建密钥那一次;这里出现任何密钥字段都是泄漏
      assertHasNot(r, ['secret', 'key', 'hmac_key', 'secret_value'], '#67b')
      expect(r.enabled, 'enabled 恒 true(吊销 = 删条目)').toBe(true)
    }
    // 整个响应体里不该出现 #67 发过的明文前缀
    expect(JSON.stringify(body)).not.toContain('hmac_')
  })

  it('M-15 短名表**不回 `next_cursor`**(全集小列表;真后端也只有 limit、没有游标)', async () => {
    const { body } = await get('/mail/hmac-keys')
    expect('next_cursor' in body, 'mock 带了 next_cursor、真后端没有 ⇒ 两边对不上(P-6)').toBe(false)
  })

  it('#67 建密钥后短名表里能看到它,且只有 secret_ref 没有明文', async () => {
    const short = `gw${Date.now().toString(36)}`.slice(0, 16)
    const made = await post('/mail/hmac-keys', { sender: 'guard@corp', short_name: short })
    expect(made.status).toBe(200)
    // 🔴 R6-55 单一形状:一次性明文**顶层平铺**,不包进 `data`(M-14:mock 原先包在 data.secret 里)
    expect(typeof made.body.secret, '#67 那一次必须在顶层回明文').toBe('string')
    expect(made.body.data, '#67 不该再包 data(R6-55 顶层平铺)').toBeUndefined()

    const rows = (await get('/mail/hmac-keys')).body.data as Record<string, unknown>[]
    const hit = rows.find((r) => r.short_name === short)
    expect(hit, '新建的短名没出现在表里').toBeTruthy()
    expect(hit!.secret_ref).toBe(`vault://mail/hmac/cmd/${short}`)
    expect(hit!.route_id, '#67 入参没有 route ⇒ 全局短名').toBeNull()
  })
})

/* ───────────────── 10. #89 整组替换的两条硬语义(P-2 / P-3 的闸门) ───────────────── */

describe('#89 PUT /settings/{group}:未知键拒收 + 缺省键回默认', () => {
  it('未知键 ⇒ 400 INVALID_ARGS,且 details[].pointer 指到那个键(不是照单全收 200)', async () => {
    /* P-2 的现场:P-SET 保留期卡片曾提交 `text_days`/`raw_enabled` —— 07 `[retention]` 没有这两个键。
       旧行为是 200 + 回显,于是「改了等于没改」,同组其余键还被整组替换洗回默认。 */
    const { status, body } = await put('/settings/retention', { files_days: 7, text_days: 20, raw_enabled: true })
    expect(status, '未知键必须在任何落库动作之前被拒').toBe(400)
    expect(body.code).toBe('INVALID_ARGS')
    const err = body.error as Record<string, unknown>
    const pointers = ((err.details ?? []) as { pointer: string }[]).map((d) => d.pointer)
    expect(pointers.sort(), '哪个键不认必须说清楚,否则前端只能显示一句「未知配置键」')
      .toEqual(['/raw_enabled', '/text_days'])
    // 被拒的请求一个字节都不该落库
    const after = (await get('/settings/retention')).body.data as Record<string, unknown>
    expect(after.text_days, '400 之后仍把野键写进去了').toBeUndefined()
    expect(after.messages_days, '400 不该动到同组其它键').toBe(30)
  })

  it('已知键全通过;没给的键回默认(整组替换语义)', async () => {
    const { status, body } = await put('/settings/retention', { messages_days: 14 })
    expect(status).toBe(200)
    const data = body.data as Record<string, unknown>
    expect(data.messages_days).toBe(14)
    expect(data.audit_days, '没给的键应回默认值 30,而不是消失或保留上一次').toBe(30)
    await put('/settings/retention', { messages_days: 30 })          // 还原,免得影响别的用例
  })

  it('密码类只写不读:`*_secret` 进去、`*_ref` 出来,明文不回显(#88)', async () => {
    const { status, body } = await put('/settings/asr', { endpoint: 'http://x/asr', api_key_secret: 'plain-秘密-42' })
    expect(status).toBe(200)
    expect(JSON.stringify(body), '响应里出现了明文密钥').not.toContain('plain-秘密-42')
    const data = body.data as Record<string, unknown>
    expect(data.api_key_secret, '明文不该被存下来').toBeUndefined()
    expect(String(data.api_key_ref ?? ''), '应改存保险库引用').toContain('vault://')
  })
})

/* ───────────────── 11. 前端下发的键 ↔ 各组**真实出参**键集(P-2 的正闸门) ───────────────── */

describe('#88 GET /settings/{group} 的出参键集 == 前端白名单(src/api/settingsKeys.ts)', () => {
  /** 读回的键集 = 该组的已知键集(真后端就是各配置 dataclass 的字段名,mock 按 07 写同一份) */
  async function groupKeys(group: string): Promise<string[]> {
    const { status, body } = await get(`/settings/${group}`)
    expect(status, `GET /settings/${group} 没回 200`).toBe(200)
    return Object.keys(body.data as Record<string, unknown>).sort()
  }

  const sorted = (v: readonly string[]): string[] => [...v].sort()

  it('retention:一个键不多、一个键不少(`text_days`/`raw_enabled` 这类野键会在这里现形)', async () => {
    expect(sorted(RETENTION_KEYS)).toEqual(await groupKeys('retention'))
  })

  it('api:白名单 == 出参键集(含 public_domain,02 #102)', async () => {
    expect(sorted(API_KEYS)).toEqual(await groupKeys('api'))
  })

  it('pool:白名单 == 出参键集(内存水位阈值在这一组)', async () => {
    expect(sorted(POOL_KEYS)).toEqual(await groupKeys('pool'))
  })

  it('ocr:白名单 = 出参键集去掉只读的 `engine`', async () => {
    const keys = await groupKeys('ocr')
    expect(keys, 'engine 要回出来供只读展示').toContain('engine')
    expect(sorted(OCR_KEYS)).toEqual(keys.filter((k) => k !== 'engine'))
  })

  it('asr:白名单 = 出参键集去掉只读的 `api_key_ref`(密钥只写不读)', async () => {
    const keys = await groupKeys('asr')
    expect(keys, '密钥应以 api_key_ref 的形式回出来').toContain('api_key_ref')
    expect(sorted(ASR_KEYS)).toEqual(keys.filter((k) => k !== 'api_key_ref'))
  })

  it('resources:两键形状 `{pools, quota_mb}`(#88 逐字),不是一堆 *_mb 顶层键', async () => {
    expect(await groupKeys('resources')).toEqual(['pools', 'quota_mb'])
  })

  it('mail:四键形状(R6-58 (ac) 逐字)', async () => {
    expect(await groupKeys('mail')).toEqual(['enabled', 'require_signature', 'scopes', 'template_version'])
  })
})

/* ───────── 12. #58/#59/#61 的出参视图(backend-api-4 §1 P-1 定稿的三张表) ───────── */

describe('邮件收发件行:ISO 时间、列表不带正文、route 是 scope 名', () => {
  const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$/
  /** route 的合法取值:`default` / 三通道 / `<account_id>`;**中文显示名是前端的事** */
  const SCOPES = ['default', 'qidian', 'qq', 'wechat']

  it('#58 收件行:无 body_text、无 *_ms、id 是字符串、时间是 ISO', async () => {
    const rows = (await get('/mail/inbox')).body.data as Record<string, unknown>[]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) {
      assertSubset(r, 'MailInboxRow', '#58 收件行')
      // 02 #58 逐字:列表**不含 body_text**(详情才给)—— 列表页把邮件正文全量下发是数据面与隐私面都不该的
      assertHasNot(r, ['body_text', 'body_html', 'attach_json', 'dedup_key'], '#58 收件行')
      expect(typeof r.id, 'id 要是字符串(库里是 int)').toBe('string')
      expect(String(r.received_at), `received_at 不是 ISO:${r.received_at}`).toMatch(ISO)
      // R6-62 (f):`*_ms` 的例外只有 Account 的两个键,邮件行一个都不许有
      for (const k of Object.keys(r)) expect(k.endsWith('_ms'), `收件行透出了库列 ${k}`).toBe(false)
      expect(SCOPES, `route 应是 scope 名而不是中文显示名,实得 ${r.route}`).toContain(String(r.route))
    }
  })

  it('#59 详情 = 收件行 + body_text', async () => {
    const d = (await get('/mail/inbox/mi_0001')).body.data as Record<string, unknown>
    assertSubset(d, 'MailInboxDetail', '#59 详情')
    expect(typeof d.body_text, '详情必须给正文').toBe('string')
    expect(String(d.received_at)).toMatch(ISO)
    expect(SCOPES).toContain(String(d.route))
  })

  it('#61 发件行:13 键形状,时间 ISO,没有下次重投时 next_attempt_at 为 null(不回 1970)', async () => {
    const rows = (await get('/mail/outbox')).body.data as Record<string, unknown>[]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) {
      assertSubset(r, 'MailOutboxRow', '#61 发件行')
      assertHas(r, ['id', 'kind', 'to', 'subject', 'status', 'attempts', 'created_at', 'route'], '#61 发件行')
      assertHasNot(r, ['body_text', 'body_html', 'smtp_response', 'dedup_key', 'to_addrs'], '#61 发件行')
      expect(String(r.created_at), 'created_at 是 C-42 的排序列,必须是 ISO').toMatch(ISO)
      for (const k of Object.keys(r)) expect(k.endsWith('_ms'), `发件行透出了库列 ${k}`).toBe(false)
      if (r.next_attempt_at !== null) expect(String(r.next_attempt_at)).toMatch(ISO)
    }
    const dead = rows.find((r) => r.status === 'DEAD')
    expect(dead?.next_attempt_at, '终态行不该还有下次重投时间').toBeNull()
  })

  it('前端不依赖 `receipt_status`(后端无此列,定义待裁决)', async () => {
    const rows = (await get('/mail/inbox')).body.data as Record<string, unknown>[]
    for (const r of rows) {
      expect('receipt_status' in r, 'mock 造了一个后端根本没有的键,页面会被养出错误假设').toBe(false)
    }
  })
})
