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
