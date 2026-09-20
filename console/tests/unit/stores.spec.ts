/** stores 的事件归约:account_state / message 三字段 / alert 去重 / 槽位空串判据 / job 终态 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useEventsStore, alertKey, alertRoute } from '@/stores/events'
import { useAccountsStore } from '@/stores/accounts'
import { useMessagesStore } from '@/stores/messages'
import { useResourcesStore } from '@/stores/resources'
import { useJobsStore } from '@/stores/jobs'
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
