// @vitest-environment jsdom
/**
 * R6-81: 偏好页保留桌面提醒、托盘、自启和 #86 告知。
 * 退役卡片原有 #88/#89 键集及保存语义转入 settings-contract-real.spec.ts。
 * 此文件真挂载当前页面；桌面桥为局部假件，告知仍取隔离真 Agent。
 */
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import { patch, seen, seenSince, waitFor, raw } from './harness'
import SetPage from '@/pages/set/SetPage.vue'
import { useUiStore } from '@/stores/ui'
import { useSetupStore } from '@/stores/setup'

let wrapper: VueWrapper
const persisted: Record<string, Record<string, unknown>>[] = []
const setAutoLaunch = vi.fn(async () => false)
const originalBridge = window.qt
let initialMark = 0

beforeAll(async () => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (q: string) => ({ matches: false, media: q, onchange: null, addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false }),
  })
  patch.token = 'e2e-admin-token'
  patch.noAuth = false
  patch.forceApiMin = null
  window.qt = {
    app: { setAutoLaunch },
    config: { patch: async (value: Record<string, Record<string, unknown>>) => { persisted.push(value); return value } },
  } as unknown as NonNullable<Window['qt']>
  setActivePinia(createPinia())
  initialMark = seen.length
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })
  wrapper = mount(SetPage, { global: { plugins: [router, Antd] }, attachTo: document.body })
  await waitFor(async () => { await flushPromises(); return useSetupStore().noticeText ? true : null }, 20000, 100)
}, 60000)

afterAll(() => { wrapper?.unmount(); window.qt = originalBridge })

describe('R6-81 保留的偏好和告知页面', () => {
  it('通过 #86 加载告知并显示服务器正文，不访问退役的配置卡片', async () => {
    const notice = (await raw('/api/v1/system/notice')).body
    expect(seenSince(initialMark, '/system/notice').map(s => s.method)).toContain('GET')
    expect(seenSince(initialMark, '/settings/')).toEqual([])
    expect(wrapper.text()).toContain('偏好设置')
    expect(wrapper.find('[data-testid="qt-set-retention-save"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="qt-set-res-save"]').exists()).toBe(false)
    const button = wrapper.findAll('button').find(b => b.text().includes('查看使用告知'))!
    expect(button).toBeTruthy()
    await button.trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain(notice.text)
  })

  it('桌面提醒只保存 notify.enabled，并同步已保存值', async () => {
    await wrapper.get('[aria-label="桌面提醒"]').trigger('click')
    await flushPromises()
    expect(persisted.at(-1)).toEqual({ notify: { enabled: false } })
    expect(useUiStore().notifyEnabled).toBe(false)
  })

  it('关闭后继续运行只保存 app.minimize_to_tray_on_close', async () => {
    await wrapper.get('[aria-label="关闭窗口后继续运行"]').trigger('click')
    await flushPromises()
    expect(persisted.at(-1)).toEqual({ app: { minimize_to_tray_on_close: false } })
    expect(useUiStore().trayOnClose).toBe(false)
  })

  it('自动启动按桌面桥实际返回值保存，不伪造已应用成功', async () => {
    await wrapper.get('[aria-label="登录后自动启动"]').trigger('click')
    await flushPromises()
    expect(setAutoLaunch).toHaveBeenCalledWith(false)
    expect(persisted.at(-1)).toEqual({ app: { auto_launch: false } })
    expect(useUiStore().autoLaunch).toBe(false)
  })
})
