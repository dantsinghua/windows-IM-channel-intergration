/** stores 的事件归约:account_state / message 三字段 / alert 去重 / 槽位空串判据 / job 终态 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useEventsStore, alertKey, alertRoute } from '@/stores/events'
import { useAccountsStore } from '@/stores/accounts'
import { useMessagesStore } from '@/stores/messages'
import { useResourcesStore } from '@/stores/resources'
import { useJobsStore } from '@/stores/jobs'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'
import type { AccountStatePayload, AlertPayload, Message, QtEvent, ResourcePool } from '@/api/types'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"ok":true,"data":[]}')))
})

function ev<T>(event: string, payload: T, seq = 1, accountId?: string): QtEvent<T> {
  return { event: event as QtEvent['event'], ts: new Date().toISOString(), seq, payload, account_id: accountId }
}

describe('events store', () => {
  it('告警按 (code,subject) 去重合并,firing 计未读', () => {
    const s = useEventsStore()
    const base: AlertPayload = {
      code: 'H12_DISK_LOW', severity: 'warn', state: 'firing', subject: 'host',
      title: '磁盘低', message: 'x', hint_actions: [], first_seen_at: 'a', last_seen_at: 'a', count: 1,
    }
    s.pushAlert(base)
    s.pushAlert({ ...base, count: 2, last_seen_at: 'b' })
    s.pushAlert({ ...base, subject: 'wsl' })
    expect(s.alerts.size).toBe(2)
    expect(s.unreadCount).toBe(2)
    expect(s.alerts.get(alertKey(base))?.count).toBe(2)
  })

  it('告警时间是数字/缺失时排序不抛错,按时间新者在前(2026-10-10 真机:Agent 曾发毫秒整数,告警铃整体崩)', () => {
    const s = useEventsStore()
    const mk = (subject: string, last: unknown): AlertPayload => ({
      code: 'POOL_CALIBRATION_DRIFT', severity: 'info', state: 'firing', subject,
      title: 't', message: 'm', hint_actions: [], first_seen_at: 'x', last_seen_at: last as string, count: 1,
    })
    s.pushAlert(mk('a', Date.parse('2026-10-10T21:46:00+08:00')))
    s.pushAlert(mk('b', '2026-10-10T22:00:00+08:00'))
    s.pushAlert(mk('c', undefined))
    expect(() => s.firing.map((x) => x.subject)).not.toThrow()
    expect(s.firing.map((x) => x.subject)).toEqual(['b', 'a', 'c'])
  })

  it('resolved 从未读移出', () => {
    const s = useEventsStore()
    const a: AlertPayload = {
      code: 'MAIL_AUTH_FAILED', severity: 'crit', state: 'firing', subject: 'mailbox:a@b',
      title: 't', message: 'm', hint_actions: [], first_seen_at: 'x', last_seen_at: 'x', count: 1,
    }
    s.pushAlert(a)
    expect(s.unreadCount).toBe(1)
    s.pushAlert({ ...a, state: 'resolved' })
    expect(s.unreadCount).toBe(0)
  })

  it('crit 的磁盘/内存水位告警驱动顶栏常驻横幅', () => {
    const s = useEventsStore()
    s.pushAlert({
      code: 'MEM_PRESSURE', severity: 'crit', state: 'firing', subject: 'host',
      title: '内存吃紧', message: '', hint_actions: [], first_seen_at: 'x', last_seen_at: 'x', count: 1,
    })
    expect(s.watermarkAlert?.code).toBe('MEM_PRESSURE')
  })

  it('warn 级不上常驻横幅', () => {
    const s = useEventsStore()
    s.pushAlert({
      code: 'H12_DISK_LOW', severity: 'warn', state: 'firing', subject: 'host',
      title: '', message: '', hint_actions: [], first_seen_at: 'x', last_seen_at: 'x', count: 1,
    })
    expect(s.watermarkAlert).toBeNull()
  })

  it('二维码 base64 不进环形缓冲(§6-10)', () => {
    const s = useEventsStore()
    s.injectForTest(ev('account_state', { prompt: { kind: 'WAIT_QRCODE', qrcode_png_b64: 'AAAA' } }, 1, 'qq01'))
    const buffered = JSON.stringify(s.ring)
    expect(buffered).not.toContain('AAAA')
    expect(buffered).toContain('<omitted>')
  })

  it('告警跳转页按 subject / code 前缀推出', () => {
    expect(alertRoute({ code: 'WECHAT_DISK_LOW', subject: 'host' })).toBe('/env')
    expect(alertRoute({ code: 'H12_DISK_LOW', subject: 'host' })).toBe('/res')
    expect(alertRoute({ code: 'MAIL_ROUTE_UNRESOLVED', subject: 'route:qidian/qd01' })).toBe('/set')
    expect(alertRoute({ code: 'MAIL_ENDPOINT_CHANGED', subject: 'endpoint:a→b' })).toBe('/env')
    expect(alertRoute({ code: 'MAIL_AUTH_FAILED', subject: 'mailbox:a@b' })).toBe('/mail')
    expect(alertRoute({ code: 'POOL_CALIBRATION_DRIFT', subject: 'pool' })).toBe('/dash')
    expect(alertRoute({ code: 'H06_ADB_OFFLINE', subject: 'account:qd01' })).toBe('/acct/qd01')
  })
})

describe('accounts store', () => {
  const payload = (over: Partial<AccountStatePayload> = {}): AccountStatePayload => ({
    state: 'running', state_code: '', state_reason: '', error_since_ms: null,
    enabled: true, runtime: {}, capabilities: ['get_state'], ...over,
  })

  it('account_state 事件按 account_id 归约', () => {
    const s = useAccountsStore()
    s.applyAccountState(ev('account_state', payload({ state: 'login_required', state_code: 'WAIT_SMS' }), 1, 'qd02'))
    expect(s.byId.qd02.state).toBe('login_required')
    expect(s.byId.qd02.state_code).toBe('WAIT_SMS')
  })

  it('prompt 随事件进 store,离开 login_required 即清', () => {
    const s = useAccountsStore()
    s.applyAccountState(ev('account_state', payload({
      state: 'login_required', state_code: 'WAIT_QRCODE',
      prompt: { kind: 'WAIT_QRCODE', text: '扫码' }, login_session_id: 'ls_1',
    }), 1, 'qq01'))
    expect(s.prompts.qq01?.kind).toBe('WAIT_QRCODE')
    expect(s.loginSessions.qq01).toBe('ls_1')
    s.applyAccountState(ev('account_state', payload({ state: 'running', login_session_id: null }), 2, 'qq01'))
    expect(s.prompts.qq01).toBeUndefined()
    expect(s.loginSessions.qq01).toBeNull()
  })

  it('已故障 N 分钟只认信封字段 error_since_ms(R6-4)', () => {
    const s = useAccountsStore()
    const t = 1_700_000_000_000
    s.applyAccountState(ev('account_state', payload({ state: 'error', error_since_ms: t - 360_000 }), 1, 'wx01'))
    expect(s.errorMinutes(s.byId.wx01, t)).toBe(6)
    expect(s.errorSeconds(s.byId.wx01, t)).toBe(360)
    // 离开 error 即 null → 不可算
    s.applyAccountState(ev('account_state', payload({ state: 'running', error_since_ms: null }), 2, 'wx01'))
    expect(s.errorMinutes(s.byId.wx01, t)).toBeNull()
  })

  it('§2.6.1 可用操作:login_required 放开注入与只读、禁 IM 写类(R-06)', () => {
    const s = useAccountsStore()
    s.applyAccountState(ev('account_state', payload({ state: 'login_required', state_code: 'WAIT_SMS' }), 1, 'qd02'))
    const ops = s.ops(s.byId.qd02)
    expect(ops.inject).toBe(true)
    expect(ops.read).toBe(true)
    expect(ops.write).toBe(false)
    expect(ops.screen).toBe(true)
  })

  it('软删后的停止事件不能把账号加回列表', () => {
    const s = useAccountsStore()
    s.applyAccountState(ev('account_state', payload({ state: 'stopped' }), 1, 'wx01'))
    expect(s.byId.wx01.state).toBe('stopped')
    s.forget('wx01')
    expect(s.byId.wx01).toBeUndefined()
    s.applyAccountState(ev('account_state', payload({ state: 'stopped' }), 2, 'wx01'))
    s.applyAccountState(ev('account_state', payload({ state: 'stopped', deleted_at: '2026-10-10T23:49:00+08:00' }), 3, 'wx02'))
    expect(s.byId.wx01).toBeUndefined()
    expect(s.byId.wx02).toBeUndefined()
  })

  it('微信档案 = channel=wechat 且 stopped/disabled 的行(C-01)', () => {
    const s = useAccountsStore()
    s.upsert({ id: 'wx01', channel: 'wechat', state: 'running' } as never)
    s.upsert({ id: 'wx02', channel: 'wechat', state: 'stopped' } as never)
    s.upsert({ id: 'wx03', channel: 'wechat', state: 'disabled' } as never)
    expect(s.wechatProfiles.map((a) => a.id)).toEqual(['wx02', 'wx03'])
  })
})

describe('messages store —— 事件专属三字段(R6-49,00 §7.4)', () => {
  const msg = (over: Partial<Message> = {}): Message => ({
    id: 'm1', account_id: 'wx01', channel: 'wechat',
    session: { id: 'wx01:g1', name: '群', kind: 'group' },
    dir: 'in', type: 'text', state: 'DELIVERED', text: 'hi', text_len: 2, media: [],
    sender: { id: 'u1', name: '张三' }, self: false,
    ts: 'a', received_at: 'b', source: 'chatlog', revoked: false, ...over,
  })

  it('late=true 行标「迟到 {lag_s} s」;≥3600 s 显示小时', () => {
    const s = useMessagesStore()
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm1', late: true, lag_s: 5 }) } as never))
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm2', late: true, lag_s: 5400 }) } as never))
    expect(s.lateText('m1')).toBe('迟到 5 s')
    expect(s.lateText('m2')).toBe('迟到 2 小时')
  })

  it('late=false 不显示任何东西(正常读库延迟不标)', () => {
    const s = useMessagesStore()
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm3', late: false, lag_s: 8 }) } as never))
    expect(s.lateText('m3')).toBe('')
  })

  it('origin=external 才标「外部来源」,rpa 与入向行不标', () => {
    const s = useMessagesStore()
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm4', dir: 'out', origin: 'external' }) } as never))
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm5', dir: 'out', origin: 'rpa' }) } as never))
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm6' }) } as never))
    expect(s.isExternal('m4')).toBe(true)
    expect(s.isExternal('m5')).toBe(false)
    expect(s.isExternal('m6')).toBe(false)
  })

  it('重拉(GET /messages)后标签消失 —— 字段不落库,属预期', async () => {
    const s = useMessagesStore()
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm7', late: true, lag_s: 99 }) } as never))
    expect(s.lateText('m7')).toBe('迟到 99 s')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true, data: [msg({ id: 'm7' })], next_cursor: null })),
    ))
    await s.search(true)
    expect(s.lateText('m7')).toBe('')
    expect(s.isExternal('m7')).toBe(false)
  })

  it('不命中当前筛选的消息不前插', () => {
    const s = useMessagesStore()
    s.filter.account_id = 'qd01'
    s.applyMessageEvent(ev('message', { message: msg({ id: 'm8' }) } as never))
    expect(s.items).toHaveLength(0)
    expect(s.newCount).toBe(0)
  })

  it('关键字 <3 字给「至少 3 个字」提示', () => {
    const s = useMessagesStore()
    s.filter.q = '报'
    expect(s.qTooShort).toBe(true)
    s.filter.q = '报价单'
    expect(s.qTooShort).toBe(false)
  })
})

describe('resources store —— 微信槽位空串判据(R-04/§11.18)', () => {
  const pool = (over: Partial<ResourcePool['pools']['windows']['wechat_slots']> = {}): ResourcePool => ({
    pools: {
      wsl: { total_mb: 1, reserved_mb: 0, used_mb: 0, free_mb: 1 },
      windows: {
        total_mb: 1, reserved_mb: 0, wechat_mb: 0,
        wechat_slots: {
          used: 0, max: 1, holder: '', pending: '', pending_expires_at: null, pending_login_session_id: '', ...over,
        },
      },
    },
    realtime: { wsl_anon_mb: 0, win_available_mb: 0 },
    quota_mb: { qidian: 0, qq: 0, wechat: 0 },
    can_add: { qidian: 0, qq: 0, wechat: 0 },
  })

  it('无 pending 时两个字符串字段是空串,不是 null', () => {
    const s = useResourcesStore()
    s.applyResource(ev('resource', pool() as unknown as Record<string, unknown>))
    expect(s.slots?.pending).toBe('')
    expect(s.slots?.pending_login_session_id).toBe('')
    expect(s.slots?.pending_expires_at).toBeNull()
    expect(s.hasPending).toBe(false)
    expect(s.canCancelPending).toBe(false)
  })

  it('有 pending 且有 login_session_id 才能取消绑定(恒带 id,R6-6)', () => {
    const s = useResourcesStore()
    s.applyResource(ev('resource', pool({
      pending: 'wx03', pending_login_session_id: 'ls_01ABC',
      pending_expires_at: new Date().toISOString(),
    }) as unknown as Record<string, unknown>))
    expect(s.hasPending).toBe(true)
    expect(s.pendingLoginSessionId).toBe('ls_01ABC')
    expect(s.canCancelPending).toBe(true)
  })

  it('pending 非空但 login_session_id 空 → 不渲染取消按钮', () => {
    const s = useResourcesStore()
    s.applyResource(ev('resource', pool({ pending: 'wx03' }) as unknown as Record<string, unknown>))
    expect(s.hasPending).toBe(true)
    expect(s.canCancelPending).toBe(false)
  })

  it('code 非空的 resource 事件是告警,不当数据事件写 pool', () => {
    const s = useResourcesStore()
    s.applyResource(ev('resource', { code: 'MEM_PRESSURE', severity: 'warn' } as Record<string, unknown>))
    expect(s.pool).toBeNull()
  })
})

describe('jobs store(§11.21 [JOB])', () => {
  it('job 事件推终态即停轮询', () => {
    const s = useJobsStore()
    s.put({ job_id: 'j1', kind: 'system_cleanup', state: 'running', progress: 40 })
    expect(s.isTerminal(s.byId.j1)).toBe(false)
    s.put({ job_id: 'j1', kind: 'system_cleanup', state: 'succeeded', progress: 100, result: { freed_mb: 512 } })
    expect(s.isTerminal(s.byId.j1)).toBe(true)
    // R6-34:清出量字段是 freed_mb
    expect(s.byId.j1.result?.freed_mb).toBe(512)
  })
})

/* ───────────────── C-42 分页:调用方**存不存游标、用不用游标** ─────────────────
 *
 * N-2 的根因不在 `requestList()`(它一直都回 `{items, nextCursor}`),而在**调用方把 nextCursor 丢了**。
 * 下面几条就钉这一跳:store 必须存住游标、`more=true` 时把它回传、并**追加**而不是覆盖。
 */

/** 造一个按调用次序回不同页的 fetch;并记下每次请求的 URL 供断言 */
function pagedFetch(pages: { data: unknown[]; next_cursor: string | null }[]) {
  const urls: string[] = []
  let i = 0
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input))
    const p = pages[Math.min(i, pages.length - 1)]
    i += 1
    return new Response(JSON.stringify({ ok: true, ...p }))
  })
  vi.stubGlobal('fetch', fn)
  return urls
}

describe('C-42 分页:store 存游标并续页(N-2)', () => {
  it('accounts:第一页存住 next_cursor,load(true) 带上它并追加', async () => {
    const urls = pagedFetch([
      { data: [{ id: 'a1', channel: 'qidian' }], next_cursor: 'cur-1' },
      { data: [{ id: 'a2', channel: 'qidian' }], next_cursor: null },
    ])
    const s = useAccountsStore()
    await s.load()
    expect(s.nextCursor, '第一页的 next_cursor 被丢掉了(N-2 的原样)').toBe('cur-1')
    expect(urls[0]).toContain('limit=')
    expect(urls[0]).not.toContain('cursor=')

    await s.load(true)
    expect(urls[1], 'load(true) 没有把游标回传').toContain('cursor=cur-1')
    expect(s.items.map((a) => a.id), '续页应当追加,不是覆盖').toEqual(['a1', 'a2'])
    expect(s.nextCursor, '翻到底应当清空游标').toBeNull()
  })

  it('accounts:load() 不带 more 时回到第一页(不带游标、覆盖旧行)', async () => {
    const urls = pagedFetch([
      { data: [{ id: 'a1', channel: 'qidian' }], next_cursor: 'cur-1' },
      { data: [{ id: 'a9', channel: 'qidian' }], next_cursor: 'cur-9' },
    ])
    const s = useAccountsStore()
    await s.load()
    await s.load()
    expect(urls[1]).not.toContain('cursor=')
    expect(s.items.map((a) => a.id)).toEqual(['a9'])
  })

  it('D-H accounts:loadFirst() 与首启的 load() 合流成一次往返;load() 本身照常无条件发', async () => {
    const urls = pagedFetch([
      { data: [{ id: 'a1', channel: 'qidian' }], next_cursor: null },
      { data: [{ id: 'a1', channel: 'qidian' }], next_cursor: null },
    ])
    const s = useAccountsStore()
    // App.vue 的 fullReload() 与页面 onMounted 的首拉几乎同时发生 ⇒ 后者搭前者的车
    const both = Promise.all([s.load(), s.loadFirst()])
    await both
    expect(urls.length, 'loadFirst 又开了一次往返(D-H 没修住)').toBe(1)
    // 紧接着再 loadFirst(仍在新鲜期内)⇒ 还是不发
    await s.loadFirst()
    expect(urls.length, '新鲜期内的页面首拉不该再发').toBe(1)
    // 🔴 但 load() 的语义不变:点「刷新」/ 批量操作后回拉一律照发
    await s.load()
    expect(urls.length, 'load() 被误加了跳过条件').toBe(2)
  })

  it('messages:会话列表有自己的一条翻页线(与消息列表的游标互不干扰)', async () => {
    const urls = pagedFetch([
      { data: [{ id: 's1', name: '群一', last_msg_at: 'b' }], next_cursor: 'sc-1' },
      { data: [{ id: 's2', name: '群二', last_msg_at: 'a' }], next_cursor: null },
    ])
    const s = useMessagesStore()
    await s.loadSessions()
    expect(s.sessionsCursor).toBe('sc-1')
    expect(s.nextCursor, '消息列表的游标不该被会话列表动到').toBeNull()

    await s.loadSessions(true)
    expect(urls[1]).toContain('cursor=sc-1')
    expect(s.sessions.map((x) => x.id)).toEqual(['s1', 's2'])
  })

  it('mail:收件与发件各存各的游标,续页追加', async () => {
    const urls = pagedFetch([
      { data: [{ id: 'mi1' }], next_cursor: 'ic-1' },
      { data: [{ id: 'mi2' }], next_cursor: null },
    ])
    const s = useMailStore()
    await s.reloadInbox()
    expect(s.inboxCursor).toBe('ic-1')
    await s.reloadInbox(true)
    expect(urls[1]).toContain('cursor=ic-1')
    expect(s.inbox.map((r) => r.id)).toEqual(['mi1', 'mi2'])
    expect(s.outboxCursor, '收件翻页不该动到发件的游标').toBeNull()
  })

  it('mail:发件同款', async () => {
    const urls = pagedFetch([
      { data: [{ id: 'mo1' }], next_cursor: 'oc-1' },
      { data: [{ id: 'mo2' }], next_cursor: null },
    ])
    const s = useMailStore()
    await s.reloadOutbox()
    expect(s.outboxCursor).toBe('oc-1')
    await s.reloadOutbox(true)
    expect(urls[1]).toContain('cursor=oc-1')
    expect(s.outbox.map((r) => r.id)).toEqual(['mo1', 'mo2'])
  })
})

/* ───────────────── #88/#89:保存后用「本次提交的值」回填 ───────────────── */

describe('settings store:保存后的回填(backend-api-3 §3)', () => {
  it('restart_required 时用本次提交的值回填,并记下「重启后生效」', () => {
    const s = useSettingsStore()
    s.groups.api = { rate_default_per_min: 120, port: 17600 }
    s.applySaved('api', { rate_default_per_min: 999 }, true)
    // 🔴 关键:不是回头 GET(那会读回「当前生效值」= 旧值,让人以为没保存上)
    expect(s.groups.api.rate_default_per_min).toBe(999)
    expect(s.groups.api.port, '未提交的键保持原样').toBe(17600)
    expect(s.isPendingRestart('api')).toBe(true)
  })

  it('restart_required=false 的组不挂「重启后生效」标记', () => {
    const s = useSettingsStore()
    s.applySaved('resources', { pools: {} }, false)
    expect(s.isPendingRestart('resources')).toBe(false)
  })
})
