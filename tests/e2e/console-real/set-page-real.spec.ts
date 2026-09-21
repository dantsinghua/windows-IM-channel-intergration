// @vitest-environment jsdom
/**
 * 第三轮(独立端到端复测)新增:**真挂载 `P-SET` 页面(SetPage.vue)** ↔ 真 Agent(全假后端)。
 *
 * 为什么要挂页面:「保存后不再发 GET」是 `SetPage.saveGroup()` 的行为(store 的 `applySaved` 本身不发请求),
 * 只在 store 层断言等于替实现方「复述了一遍调用序列」。这里真点「保存」按钮,用 harness 的 `seen`
 * 看页面**实际发出**的请求序列。
 *
 * 规格出处:
 * - 02 #89 R6-58 (ad):v1「可热加载键」= 空集,除 `resources` 外一律 `restart_required:true`;
 *   GET 读当前生效值 ⇒ 总控口径:保存后用**本次提交值**回填并标「已保存,重启 Agent 后生效」,**不回头 GET**。
 * - 02 #89「整组替换(缺省键回默认)」+ 07 `[retention]` 键全集(07:63-64:`files_days` · `messages_days` · `media_days` ·
 *   `raw_days` · `mail_archive_days` · `commands_days` · `audit_days` · `idempotency_days` · `mail_inbox_rows_days` ·
 *   `export_jobs_days` · `cleanup_at` · `cleanup_batch` · `disk_warn/high/critical_mb` · `disk_low_watermark_mb` ·
 *   `health_raw_h/health_1m_d/health_1h_d`)⇒ 整组 PUT 的键只能出自这一组。
 * - 第四轮(console-retest)补:保留期两个控件的落点(`text`→`messages_days`、`raw`→`raw_days`,
 *   console-fix-5-pages §1 ①)、资源池卡片的线上形状 `{pools, quota_mb}`(#88 `_group_view('resources')` 逐字,§1 ⑥)、
 *   #89 400 时界面摊开 `details[].pointer`(§0 `saveErrorText`)。
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import { patch, raw, seen, seenSince, waitFor } from './harness'
import SetPage from '@/pages/set/SetPage.vue'
import { useSettingsStore } from '@/stores/settings'

/** 07 `[retention]` 键全集(07:63-64,见文件头)。上一轮漏了 07:64 的 `health_*` 三键,靠「后端出参键」兜底才没红 */
const RETENTION_KEYS_07 = [
  'files_days', 'messages_days', 'media_days', 'raw_days', 'mail_archive_days', 'commands_days', 'audit_days',
  'idempotency_days', 'mail_inbox_rows_days', 'export_jobs_days', 'cleanup_at', 'cleanup_batch',
  'disk_warn_mb', 'disk_high_mb', 'disk_critical_mb', 'disk_low_watermark_mb',
  'health_raw_h', 'health_1m_d', 'health_1h_d',
]

/** 在 antd 控件上填一个数:testid 可能落在 `<input>` 本身,也可能落在外层容器 */
async function typeInto(testid: string, v: string): Promise<void> {
  const host = wrapper.get(`[data-testid="${testid}"]`)
  const input = host.element.tagName === 'INPUT' ? host : host.get('input')
  await input.setValue(v)
  await input.trigger('change')
  await input.trigger('blur')
  await flushPromises()
}

async function clickSave(testid: string, group: string): Promise<number> {
  const mark = seen.length
  await wrapper.get(`[data-testid="${testid}"]`).trigger('click')
  await waitFor(async () => (seenSince(mark, `/settings/${group}`).some((s) => s.method === 'PUT') ? true : null), 15000, 100)
  for (let i = 0; i < 10; i++) { await flushPromises(); await new Promise((r) => setTimeout(r, 50)) }
  return mark
}

let wrapper: VueWrapper
let serverKeys: string[] = []

beforeAll(async () => {
  // jsdom 没有 matchMedia(antd 栅格的响应式监听要它)—— 测试环境补丁,与被测逻辑无关
  if (!window.matchMedia) {
    Object.defineProperty(window, 'matchMedia', {
      value: (q: string) => ({ matches: false, media: q, onchange: null, addListener() {}, removeListener() {},
        addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false }),
    })
  }
  patch.token = 'e2e-admin-token'
  patch.noAuth = false
  patch.forceApiMin = null
  // 后端这一组现在有哪些键(补进允许集:后端多给的派生键不算前端的错)
  serverKeys = Object.keys((await raw('/api/v1/settings/retention')).body.data ?? {})
  setActivePinia(createPinia())
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  wrapper = mount(SetPage, { global: { plugins: [router, Antd] }, attachTo: document.body })
  // 等 onMounted 的 loadAll 把 retention 组拉回来
  await waitFor(async () => {
    await flushPromises()
    return useSettingsStore().groups.retention ? true : null
  }, 20000, 200)
}, 60000)

afterAll(() => { wrapper?.unmount() })

async function clickRetentionSave(): Promise<number> {
  const mark = seen.length
  await wrapper.get('[data-testid="qt-set-retention-save"]').trigger('click')
  // 等 PUT 回来 + 页面把后续动作做完(有 GET 的话也会在这段时间里发出去)
  await waitFor(async () => (seenSince(mark, '/settings/retention').some((s) => s.method === 'PUT') ? true : null), 15000, 100)
  for (let i = 0; i < 10; i++) { await flushPromises(); await new Promise((r) => setTimeout(r, 50)) }
  return mark
}

describe('P-SET 保留期卡片:点「保存」之后(真挂载 SetPage.vue)', () => {
  it('只发一次 PUT /settings/retention,之后不回头 GET /settings/retention', async () => {
    const mark = await clickRetentionSave()
    const calls = seenSince(mark, '/settings/retention').map((s) => s.method)
    expect(calls, '保存后又 GET 了一次(会把表单洗回旧的生效值)').toEqual(['PUT'])
  })

  it('卡片上挂出「已保存,重启 Agent 后生效」(restart_required:true)', async () => {
    const st = useSettingsStore()
    expect(st.isPendingRestart('retention')).toBe(true)
    expect(wrapper.text()).toContain('已保存,重启 Agent 后生效')
  })

  it('store 里这一组 = 本次提交的请求体(不是回头读的旧值)', async () => {
    const st = useSettingsStore()
    const put = [...seen].reverse().find((s) => s.method === 'PUT' && s.url.includes('/settings/retention'))
    expect(put?.body).toBeTruthy()
    expect(st.groups.retention).toEqual(JSON.parse(put!.body!))
  })

  it('整组 PUT 的键只出自 07 [retention],且两个控件落在 messages_days / raw_days 上', async () => {
    /* 01 §4 `qt-set-retention-{text|raw}`:「库内消息正文与其余库表保留天数」= `messages_days`,
       「原始载荷保留天数」= `raw_days`(console-fix-5-pages §1 ①;上一轮实得是 07 里不存在的 `text_days`/`raw_enabled`)。
       🔴 本轮撤掉「后端出参键也算允许」的兜底:判据只认 07 登记的键全集,后端多给的键另条断言对账。 */
    await typeInto('qt-set-retention-text', '20')
    await typeInto('qt-set-retention-raw', '5')
    const mark = await clickSave('qt-set-retention-save', 'retention')
    const put = seenSince(mark, '/settings/retention').find((s) => s.method === 'PUT')!
    const body = JSON.parse(put.body!)
    const alien = Object.keys(body).filter((k) => !RETENTION_KEYS_07.includes(k))
    expect(alien, `P-SET 保留期卡片提交了 07 [retention] 里不存在的键:${alien.join('、')}`).toEqual([])
    expect(body.messages_days, '「库内消息正文」控件没落在 messages_days 上').toBe(20)
    expect(body.raw_days, '「原始载荷」控件没落在 raw_days 上').toBe(5)
    expect(body).not.toHaveProperty('text_days')
    expect(body).not.toHaveProperty('raw_enabled')
    expect(put.status, '#89 未知键 ⇒ 400;这里必须真被后端收下').toBe(200)
    /* 🔴 不回读 GET:retention 组 `restart_required:true`,R6-58 (ad) GET 读的是**当前生效值**,
       重启 Agent 前本就还是旧值(本文件头与第 1 条用例的口径)。收下与否以 PUT 200 为准,
       界面上显示的应是**本次提交值** + 「重启后生效」。 */
    const st = useSettingsStore()
    expect((st.groups.retention as Record<string, unknown>).messages_days).toBe(20)
    expect((st.groups.retention as Record<string, unknown>).raw_days).toBe(5)
    expect(st.isPendingRestart('retention')).toBe(true)
  })

  it('对账:后端 #88 retention 出参键 ⊆ 07 [retention](后端多给 = 后端/规格问题,单独报)', async () => {
    const extra = serverKeys.filter((k) => !RETENTION_KEYS_07.includes(k))
    expect(extra, `后端 retention 组出参里有 07 未登记的键:${extra.join('、')}`).toEqual([])
  })
})

describe('P-SET 资源池卡片:线上形状 {pools, quota_mb}(#88 resources 组)', () => {
  it('保存下发的顶层键恰为 {pools, quota_mb},改的配额落在 quota_mb.qq,且后端收下', async () => {
    /* console-fix-5-pages §1 ⑥:此前卡片下发六个顶层 `*_mb` 键,#88 里一个都不存在。
       内存水位 `mem_warn_mb`/`mem_critical_mb` 属 07 `[pool]`,**不得**混进这个 body。 */
    await typeInto('qt-set-res-qq', '777')
    const mark = await clickSave('qt-set-res-save', 'resources')
    const put = seenSince(mark, '/settings/resources').find((s) => s.method === 'PUT')!
    const body = JSON.parse(put.body!)
    expect(Object.keys(body).sort()).toEqual(['pools', 'quota_mb'])
    expect(body.quota_mb.qq).toBe(777)
    for (const k of ['qidian_mb', 'qq_mb', 'wechat_mb', 'base_mb', 'mem_warn_mb', 'mem_critical_mb']) {
      expect(body, `资源池 body 里不该再有旧的顶层键 ${k}`).not.toHaveProperty(k)
    }
    expect(put.status).toBe(200)
    const back = (await raw('/api/v1/settings/resources')).body.data
    expect(back.quota_mb.qq, '保存后回读,配额真的改了').toBe(777)
  })
})

describe('P-SET 保存遇 #89 400:界面把 details[].pointer 指到的键显示出来', () => {
  it('真后端回 400 unknown_key 时,错误提示里点名 text_days', async () => {
    // 前端已按白名单过滤、界面发不出野键 ⇒ 在出口注入一个,让**真后端**真的回 400(见 harness `patch.mutateBody`)
    patch.mutateBody = (url, method, body) => {
      if (method !== 'PUT' || !url.includes('/settings/retention') || !body) return undefined
      return JSON.stringify({ ...JSON.parse(body), text_days: 20 })
    }
    try {
      const mark = await clickSave('qt-set-retention-save', 'retention')
      const put = seenSince(mark, '/settings/retention').find((s) => s.method === 'PUT')!
      expect(put.status, '造错条件没造出来(后端没回 400),本条不成立').toBe(400)
      await waitFor(async () => (document.body.textContent?.includes('text_days') ? true : null), 5000, 100)
      const txt = document.body.textContent ?? ''
      expect(txt, '400 的 details[].pointer 没摊开给用户看').toMatch(/涉及[::]\s*text_days/)
    } finally {
      patch.mutateBody = null
    }
  })
})
