/**
 * P-SETUP 首启向导(console-fix-5-wizard;R6-84 四步):
 * ① 「向导已完成」的持久化落点(`console.toml [setup] done`,无 `window.qt` 时本机镜像兜底);
 * ② 步骤存 store(组件重挂不回第 1 步,01 §2.7.1 步 3「完成后回到本步」);
 * ③ 路由守卫的首登窄例外(D-D);告知改版重勾随「阅读须知」步退役(R6-84,2026-10-10);
 * ④ 自检行聚合:`SKIPPED` 灰、不计红黄,说明列走中文(C-18 / 01 §2.7.9 / M4-7)。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import SetupPage from '@/pages/setup/SetupPage.vue'
import { SETUP_LAST_STEP, useSetupStore } from '@/stores/setup'
import { useSessionStore } from '@/stores/session'
import { setupRedirect } from '@/router'
import { selftestRows, type SelftestRun } from '@/api/types'

// 页面只用 `useRouter`;`@/router` 自己要真的 `createRouter`,所以按需局部 mock
const routerSpy = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }))
vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRouter: () => routerSpy,
}))

const NOTICE_ACKED = { notice_version: 'v2', text: '须知正文', ack_ms: 1, acked_version: 'v2' }
const NOTICE_STALE = { notice_version: 'v3', text: '须知正文', ack_ms: 1, acked_version: 'v2' }

function stubFetch(notice: Record<string, unknown>): void {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) =>
    new Response(JSON.stringify({ ok: true, data: String(url).includes('/system/notice') ? notice : {} }))))
}

beforeEach(() => {
  routerSpy.push.mockReset()
  routerSpy.replace.mockReset()
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  stubFetch(NOTICE_ACKED)
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

/* ────────── ① 「向导已完成」的持久化 ────────── */

describe('向导完成后不再从向导进', () => {
  it('有 window.qt ⇒ 以 console.toml [setup] done 为准,并同步本机镜像', async () => {
    vi.stubGlobal('qt', { config: { read: async () => ({ setup: { done: true } }), patch: vi.fn() } })
    const store = useSetupStore()
    await store.loadConfig()
    expect(store.done).toBe(true)
    expect(localStorage.getItem('qt.setup.done')).toBe('true')
  })

  it('没有 window.qt(dev:web / 纯浏览器)⇒ 完成后刷新仍算已完成', async () => {
    const store = useSetupStore()
    await store.finish(true, true)
    expect(store.done).toBe(true)

    // 刷新 = 新 store 重新读配置
    setActivePinia(createPinia())
    const fresh = useSetupStore()
    await fresh.loadConfig()
    expect(fresh.done).toBe(true)
  })

  it('从没跑过向导 ⇒ done=false(该进向导)', async () => {
    const store = useSetupStore()
    await store.loadConfig()
    expect(store.done).toBe(false)
  })

  it('config.read 抛错不打断启动序列,退回本机镜像', async () => {
    localStorage.setItem('qt.setup.done', 'true')
    vi.stubGlobal('qt', { config: { read: async () => { throw new Error('IPC down') }, patch: vi.fn() } })
    const store = useSetupStore()
    await expect(store.loadConfig()).resolves.toBeUndefined()
    expect(store.done).toBe(true)
  })

  it('开机自启设置失败,不影响「向导已完成」落盘', async () => {
    const patch = vi.fn().mockResolvedValue({})
    vi.stubGlobal('qt', {
      config: { read: async () => ({}), patch },
      app: { setAutoLaunch: vi.fn().mockRejectedValue(new Error('无权限')) },
    })
    const store = useSetupStore()
    await store.finish(true, false)
    expect(patch).toHaveBeenCalledWith(expect.objectContaining({ setup: { done: true } }))
    expect(store.done).toBe(true)
    expect(localStorage.getItem('qt.setup.done')).toBe('true')
  })

  it('「重新运行向导」把 done 置回 false,并回到第 1 步', async () => {
    const store = useSetupStore()
    await store.finish(true, true)
    store.step = SETUP_LAST_STEP

    await store.rerun()
    expect(store.done).toBe(false)
    expect(localStorage.getItem('qt.setup.done')).toBe('false')
    expect(store.step).toBe(0)
  })
})

describe('R6-84:向导没有「阅读须知」步,告知改版不再打断已完成向导的机器', () => {
  it('store 不再暴露重勾态;告知版本落后也不会把 step 拉回/把 done 置假', async () => {
    stubFetch(NOTICE_STALE)
    const store = useSetupStore()
    store.done = true
    store.step = SETUP_LAST_STEP
    await store.loadNotice()
    expect(store.acked).toBe(false)                        // 事实照记(偏好页「查看使用告知」会用)
    expect(store.done).toBe(true)
    expect(store.step).toBe(SETUP_LAST_STEP)
    expect('reackRequired' in store).toBe(false)
    expect(setupRedirect({ path: '/dash', name: 'P-DASH', query: {} }, { done: true })).toBeNull()
  })

  it('刷新时存下的旧五步序号(4)不再合法,回到第 1 步', () => {
    sessionStorage.setItem('qt.setup.step', '4')
    setActivePinia(createPinia())
    expect(useSetupStore().step).toBe(0)
  })
})

/* ────────── ② / ③ 步骤持久与守卫例外 ────────── */

const ANTD_STUBS = Object.fromEntries(
  ['a-steps', 'a-step', 'a-alert', 'a-button', 'a-checkbox'].map((n) => [n, true]),
)

describe('向导步骤存 store(D-B)', () => {
  it('组件重挂后回到原来的步,不退回第 1 步', async () => {
    const store = useSetupStore()
    store.step = 2                                          // R6-84:添加账号是第 3 步(下标 2)
    const first = shallowMount(SetupPage, { global: { stubs: ANTD_STUBS } })
    await flushPromises()
    expect(first.find('[data-testid="qt-setup-login-qidian-add"]').exists()).toBe(true)

    first.unmount()
    const again = shallowMount(SetupPage, { global: { stubs: ANTD_STUBS } })
    await flushPromises()
    expect(store.step).toBe(2)
    expect(again.find('[data-testid="qt-setup-login-qidian-add"]').exists()).toBe(true)
    again.unmount()
  })

  it('整页刷新后步骤还在,不退回第 1 步', () => {
    const store = useSetupStore()
    store.step = 2
    expect(sessionStorage.getItem('qt.setup.step')).toBe('2')

    setActivePinia(createPinia())
    const fresh = useSetupStore()
    expect(fresh.step).toBe(2)
  })

  it('第 1 步就是连接服务:服务连上即可下一步,不再要求勾告知', async () => {
    const store = useSetupStore()
    const session = useSessionStore()
    session.authState = 'ok'
    session.agentReachable = true
    const w = shallowMount(SetupPage, { global: { stubs: ANTD_STUBS } })
    await flushPromises()
    expect(w.find('[data-testid="qt-setup-conn-agent"]').exists()).toBe(true)
    expect(w.find('[data-testid="qt-setup-notice-ack"]').exists()).toBe(false)
    // `a-button` 桩把布尔 prop 原样序列化成 "true"/"false" 字符串
    expect(w.find('[data-testid="qt-setup-next"]').attributes('disabled')).toBe('false')
    await w.find('[data-testid="qt-setup-next"]').trigger('click')
    expect(store.step).toBe(1)
    w.unmount()
  })

  it('第 1 步服务没连上 ⇒ 下一步禁用', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('agent down')))   // 挂载时的 pingAgent 也要失败
    const session = useSessionStore()
    session.authState = 'ok'
    session.agentReachable = false
    const w = shallowMount(SetupPage, { global: { stubs: ANTD_STUBS } })
    await flushPromises()
    expect(session.agentReachable).toBe(false)
    expect(w.find('[data-testid="qt-setup-next"]').attributes('disabled')).toBe('true')
    w.unmount()
  })
})

describe('路由守卫:未完成向导时的窄例外(D-D)', () => {
  const NOT_DONE = { done: false }

  it('向导首登步「现在添加」放行进 P-ACCT-NEW', () => {
    expect(setupRedirect(
      { path: '/acct/new', name: 'P-ACCT-NEW', query: { ch: 'qidian', from: 'setup' } },
      NOT_DONE,
    )).toBeNull()
  })

  it('例外只认 P-ACCT-NEW + from=setup:少一个条件都打回向导', () => {
    expect(setupRedirect({ path: '/acct/new', name: 'P-ACCT-NEW', query: {} }, NOT_DONE)).toEqual({ path: '/setup' })
    expect(setupRedirect({ path: '/dash', name: 'P-DASH', query: { from: 'setup' } }, NOT_DONE)).toEqual({ path: '/setup' })
    expect(setupRedirect({ path: '/set', name: 'P-SET', query: { from: 'setup' } }, NOT_DONE)).toEqual({ path: '/setup' })
  })

  it('建号页离开(返回列表 / 取消)被打回向导 ⇒ 配合 store 里的 step 即「回到本步」', () => {
    expect(setupRedirect({ path: '/acct', name: 'P-ACCT', query: {} }, NOT_DONE)).toEqual({ path: '/setup' })
  })

  it('向导已完成 ⇒ 全部放行,不再被按回 /setup', () => {
    const done = { done: true }
    expect(setupRedirect({ path: '/dash', name: 'P-DASH', query: {} }, done)).toBeNull()
    expect(setupRedirect({ path: '/setup', name: 'P-SETUP', query: {} }, done)).toBeNull()
  })
})

/* ────────── ④ 自检行聚合:SKIPPED 灰 + 中文(D-C) ────────── */

function runWith(probes: Array<{ target: string; status: string; detail?: string }>): SelftestRun {
  return { napcat_ok: true, winagent_ok: true, redroid_boot_ms: 1200, probes }
}

function probeRow(rows: ReturnType<typeof selftestRows>) {
  return rows.find((r) => r.item === 'probes')
}

describe('自检行:SKIPPED 不计红黄(C-18 / M4-7)', () => {
  it('全部 SKIPPED ⇒ 灰「未探测」,不是黄色警告', () => {
    const row = probeRow(selftestRows(runWith([
      { target: 'mail_pop3', status: 'SKIPPED', detail: 'not_configured' },
      { target: 'mail_imap', status: 'SKIPPED', detail: 'agent_probe_disabled' },
    ])))
    expect(row?.level).toBe('skip')
  })

  it('OK 与 SKIPPED 混排 ⇒ 按通过算(没测的不算问题)', () => {
    const row = probeRow(selftestRows(runWith([
      { target: 'apk_url', status: 'OK' },
      { target: 'mail_pop3', status: 'SKIPPED', detail: 'not_configured' },
    ])))
    expect(row?.level).toBe('ok')
  })

  it('真红项按 01 §2.7.9 的芯片色判(TCP_TIMEOUT / PROXY_REQUIRED / BLOCKED_BY_POLICY)', () => {
    for (const status of ['TCP_TIMEOUT', 'PROXY_REQUIRED', 'BLOCKED_BY_POLICY', 'DNS_FAIL', 'TCP_REFUSED', 'TLS_FAIL']) {
      const row = probeRow(selftestRows(runWith([{ target: 'qidian_msf', status }])))
      expect(`${status}:${row?.level}`).toBe(`${status}:error`)
    }
  })

  it('HTTP_4XX / HTTP_5XX 是黄;未知结论不伪装成正常', () => {
    expect(probeRow(selftestRows(runWith([{ target: 'apk_url', status: 'HTTP_4XX' }])))?.level).toBe('warn')
    expect(probeRow(selftestRows(runWith([{ target: 'apk_url', status: 'HTTP_5XX' }])))?.level).toBe('warn')
    expect(probeRow(selftestRows(runWith([{ target: 'apk_url', status: 'WAT' }])))?.level).toBe('warn')
  })

  it('说明列不甩裸枚举:结论与已知 detail 都是中文', () => {
    const row = probeRow(selftestRows(runWith([
      { target: 'apk_url', status: 'OK' },
      { target: 'mail_pop3', status: 'SKIPPED', detail: 'not_configured' },
      { target: 'winagent_from_wsl', status: 'SKIPPED', detail: 'agent_unreachable' },
    ])))
    expect(row?.message).toBe('apk_url:正常、mail_pop3:未探测(目标未配置)、winagent_from_wsl:未探测(Agent 不可达)')
    expect(row?.message).not.toMatch(/SKIPPED|not_configured|agent_unreachable/)
  })

  it('未知 detail(诊断摘要)按原文显示,不丢信息', () => {
    const row = probeRow(selftestRows(runWith([
      { target: 'mail_smtp', status: 'TLS_FAIL', detail: 'mitm_ca=ACME Corp CA' },
    ])))
    expect(row?.message).toBe('mail_smtp:TLS 失败(mitm_ca=ACME Corp CA)')
  })
})
