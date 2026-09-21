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
 * - 02 #89「整组替换(缺省键回默认)」+ 07 `[retention]` 键全集(`files_days` · `messages_days` · `media_days` · `raw_days` ·
 *   `mail_archive_days` · `commands_days` · `audit_days` · `idempotency_days` · `mail_inbox_rows_days` · `export_jobs_days` ·
 *   `cleanup_at` · `cleanup_batch` · `disk_warn/high/critical_mb` · `disk_low_watermark_mb`)⇒ 整组 PUT 的键只能出自这一组。
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import { patch, raw, seen, seenSince, waitFor } from './harness'
import SetPage from '@/pages/set/SetPage.vue'
import { useSettingsStore } from '@/stores/settings'

/** 07 `[retention]` 键全集(见文件头) */
const RETENTION_KEYS_07 = [
  'files_days', 'messages_days', 'media_days', 'raw_days', 'mail_archive_days', 'commands_days', 'audit_days',
  'idempotency_days', 'mail_inbox_rows_days', 'export_jobs_days', 'cleanup_at', 'cleanup_batch',
  'disk_warn_mb', 'disk_high_mb', 'disk_critical_mb', 'disk_low_watermark_mb',
]

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

  it('整组 PUT 的键只出自 07 [retention](不提交 docs 里不存在的键)', async () => {
    /* 该卡片的四个控件(01 §4 `qt-set-retention-{files|text|raw|audit}`)写回 `store.groups.retention` 的哪几个键,
       决定了 PUT 出去的是什么。后端 #89 不拒未知键(实测照单全收 + 整组替换把其余键回默认) ⇒
       前端提交 docs 里不存在的键 = 用户改了个「看起来生效」的值,实际什么都没变。 */
    const st = useSettingsStore()
    st.groups.retention = { ...(st.groups.retention as Record<string, unknown>) }
    // 模拟用户动了四个控件(不认识的键就是控件写回的键 —— 由 v-model 决定,不由本用例决定)
    const host = wrapper.get('[data-testid="qt-set-retention-text"]')
    const input = host.element.tagName === 'INPUT' ? host : host.get('input')      // antd 把 attrs 落在 <input> 上
    await input.setValue('20')
    await input.trigger('change')
    await input.trigger('blur')
    const sw = wrapper.find('[data-testid="qt-set-retention-raw"]')
    if (sw.exists()) await sw.trigger('click')
    await flushPromises()
    const mark = await clickRetentionSave()
    const put = seenSince(mark, '/settings/retention').find((s) => s.method === 'PUT')
    const keys = Object.keys(JSON.parse(put!.body!))
    const allowed = new Set([...RETENTION_KEYS_07, ...serverKeys])
    const alien = keys.filter((k) => !allowed.has(k))
    expect(alien, `P-SET 保留期卡片提交了 07 [retention] 里不存在的键:${alien.join('、')}`).toEqual([])
  })
})
