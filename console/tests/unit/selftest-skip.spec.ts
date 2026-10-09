/**
 * 自检表的 `skip`(SKIPPED = 灰「未探测」)在 **P-ENV** 也要渲染成灰,不能当成绿 ✔。
 *
 * 缺陷现场(D-C 的连带):`SelftestRow.level` 由向导线新增了 `skip` 档之后,
 * `EnvPage.vue` 的三元表达式只判 `error`/`warn`,其余一律走 `qt-ok` + `✔`
 * ⇒ 「没探测」被显示成「探测通过」—— 界面说假话,比显示成黄色更糟。
 *
 * 判据出处:01 §2.7.9 探测结论表(`SKIPPED` → 灰,文案「未探测(目标未配置或 Agent 不可达)」,C-18)、
 * 01 M4-7「SKIPPED 不计红项,`P-SETUP` 步 3 不因它阻断」。
 *
 * ⚠️ 跑起来会刷一片「Failed to resolve component: a-*」—— ant-design-vue 没在测试里 `app.use()`,
 * 而 SFC 是预编译的,`compilerOptions.isCustomElement` 对它无效。这些 warn 与本文件的判据无关
 * (断的是原生 `<table>` 里的单元格),**不要**为了消 warn 去把真实组件装进来。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import EnvPage from '@/pages/env/EnvPage.vue'
import { useEnvStore } from '@/stores/env'
import type { SelftestRow } from '@/api/types'

vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/env', query: {} }) }))

beforeEach(() => {
  setActivePinia(createPinia())
  // 页面 onMounted 会拉一串端点;一律回空信封,本测试只关心自检表那几行怎么渲染
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"ok":true,"data":{}}')))
})

const ROWS: SelftestRow[] = [
  { item: 'napcat', label: 'NapCat 可用', level: 'ok' },
  { item: 'winagent', label: 'WinAgent 健康', level: 'warn', message: '未执行' },
  { item: 'redroid_boot', label: '临时容器启动', level: 'error', message: '检查未通过' },
  { item: 'probes', label: '连通性探测(2 项)', level: 'skip', message: 'apk_url:正常、mail_pop3:未探测(目标未配置)' },
]

async function renderEnv() {
  const w = shallowMount(EnvPage, {
    global: {
      stubs: {
        teleport: true,
        // 自检表包在 `PageState` 的默认插槽里;shallowMount 会把它整个 stub 掉 ⇒ 插槽不渲染。
        // 只把这一个换成「透传插槽」的桩,其余子组件照常 stub。
        PageState: { template: '<div><slot /></div>' },
      },
    },
  })
  useEnvStore().selftest = ROWS
  await flushPromises()
  return w
}

function cellOf(w: Awaited<ReturnType<typeof renderEnv>>, item: string) {
  const row = w.find(`[data-testid="qt-env-selfcheck-row-${item}"]`)
  expect(row.exists(), `P-ENV 自检表没渲染出 ${item} 这一行`).toBe(true)
  // R6-81 左列仅为状态圆点；可读结论迁至末列，继续核四档语义和颜色。
  return row.findAll('td').at(-1)!
}

describe('P-ENV 一键自检:四档各渲染各的色与结论', () => {
  it('`skip` = 灰「未探测」,不得显示成正常或提醒', async () => {
    const cell = cellOf(await renderEnv(), 'probes')
    expect(cell.text(), '把「没探测」显示成了正常或提醒').toBe('未探测')
    expect(cell.classes(), 'skip 行应走 qt-muted(灰)').toContain('qt-muted')
    expect(cell.classes()).not.toContain('qt-ok')
    expect(cell.classes()).not.toContain('qt-warn')
    expect(cell.classes()).not.toContain('qt-danger')
  })

  it('`ok`/`warn`/`error` 三档不受影响', async () => {
    const w = await renderEnv()
    expect(cellOf(w, 'napcat').text()).toBe('正常')
    expect(cellOf(w, 'napcat').classes()).toContain('qt-ok')
    expect(cellOf(w, 'winagent').text()).toBe('提醒')
    expect(cellOf(w, 'winagent').classes()).toContain('qt-warn')
    expect(cellOf(w, 'redroid_boot').text()).toBe('异常')
    expect(cellOf(w, 'redroid_boot').classes()).toContain('qt-danger')
  })

  it('说明列照常显示中文文案(由 selftestRows 内部的 probeLineZh 生成,不甩英文枚举)', async () => {
    const row = (await renderEnv()).find('[data-testid="qt-env-selfcheck-row-probes"]')
    expect(row.text()).toContain('未探测(目标未配置)')
    expect(row.text(), '不该把后端枚举 SKIPPED 原样甩到界面上').not.toContain('SKIPPED')
  })
})
