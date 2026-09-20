/**
 * P-SETUP 步 1 告知区「看完全文才可勾」的判定(console-fix-4):
 * 不溢出 ⇒ 挂载即解锁;溢出 ⇒ 未滚动时锁、滚到底解锁;由溢出变为不溢出(resize)⇒ 解锁。
 * jsdom 不做布局,scrollHeight/clientHeight/scrollTop 由测试按场景注入;ResizeObserver 用假实现手动触发。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import SetupPage from '@/pages/setup/SetupPage.vue'
import { useSetupStore } from '@/stores/setup'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }))

const NOTICE = { notice_version: 'v1', text: '须知正文', ack_ms: null, acked_version: null }

/** 场景尺寸:所有 .notice 元素共用,测试中途可改 */
const box = { scrollHeight: 200, clientHeight: 300, scrollTop: 0 }

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  observed: Element[] = []
  disconnected = false
  constructor(private cb: ResizeObserverCallback) { FakeResizeObserver.instances.push(this) }
  observe(el: Element): void { this.observed.push(el) }
  unobserve(): void { /* 未用 */ }
  disconnect(): void { this.disconnected = true }
  fire(): void { this.cb([], this as unknown as ResizeObserver) }
}

const saved: Record<string, PropertyDescriptor | undefined> = {}
const PROPS = ['scrollHeight', 'clientHeight', 'scrollTop'] as const

beforeEach(() => {
  setActivePinia(createPinia())
  FakeResizeObserver.instances = []
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) =>
    new Response(JSON.stringify({ ok: true, data: String(url).includes('/system/notice') ? NOTICE : {} }))))
  for (const k of PROPS) {
    saved[k] = Object.getOwnPropertyDescriptor(HTMLElement.prototype, k) ?? Object.getOwnPropertyDescriptor(Element.prototype, k)
    Object.defineProperty(HTMLElement.prototype, k, {
      configurable: true,
      get(this: HTMLElement) { return this.classList.contains('notice') ? box[k] : 0 },
      set(this: HTMLElement, v: number) { if (this.classList.contains('notice') && k === 'scrollTop') box.scrollTop = v },
    })
  }
})

afterEach(() => {
  vi.unstubAllGlobals()
  for (const k of PROPS) {
    const d = saved[k]
    if (d) Object.defineProperty(HTMLElement.prototype, k, d)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[k]
  }
})

/** 页面用全局注册的 antd 组件;单测不装 antd,按名字桩掉以免 resolve 警告 */
const ANTD_STUBS = Object.fromEntries(
  ['a-steps', 'a-step', 'a-alert', 'a-button', 'a-checkbox'].map((n) => [n, true]),
)

async function mountPage() {
  const w = shallowMount(SetupPage, { global: { stubs: ANTD_STUBS } })
  await flushPromises()
  return w
}

const HINT = '请滚动到底部后再勾选'

describe('P-SETUP 告知区解锁判定', () => {
  it('内容不溢出 ⇒ 挂载(文案到货)即解锁,不显示滚动提示', async () => {
    Object.assign(box, { scrollHeight: 200, clientHeight: 300, scrollTop: 0 })
    const w = await mountPage()
    const store = useSetupStore()
    expect(store.noticeText).toBe('须知正文')
    expect(store.scrolledToBottom).toBe(true)
    expect(w.text()).not.toContain(HINT)
  })

  it('文案未到货(占位文本)时不因不溢出而提前解锁', async () => {
    Object.assign(box, { scrollHeight: 50, clientHeight: 300, scrollTop: 0 })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('down')))
    const w = await mountPage()
    const store = useSetupStore()
    expect(store.noticeText).toBe('')
    FakeResizeObserver.instances.forEach((o) => o.fire())
    expect(store.scrolledToBottom).toBe(false)
    expect(w.text()).toContain(HINT)
  })

  it('内容溢出 ⇒ 未滚动时锁;滚到底解锁;之后不回锁', async () => {
    Object.assign(box, { scrollHeight: 900, clientHeight: 300, scrollTop: 0 })
    const w = await mountPage()
    const store = useSetupStore()
    expect(store.scrolledToBottom).toBe(false)
    expect(w.text()).toContain(HINT)

    box.scrollTop = 300
    await w.find('.notice').trigger('scroll')
    expect(store.scrolledToBottom).toBe(false)

    box.scrollTop = 600
    await w.find('.notice').trigger('scroll')
    expect(store.scrolledToBottom).toBe(true)
    expect(w.text()).not.toContain(HINT)

    box.scrollTop = 0
    await w.find('.notice').trigger('scroll')
    FakeResizeObserver.instances.forEach((o) => o.fire())
    expect(store.scrolledToBottom).toBe(true)
  })

  it('由溢出变为不溢出(窗口/容器 resize)⇒ ResizeObserver 回调解锁', async () => {
    Object.assign(box, { scrollHeight: 900, clientHeight: 300, scrollTop: 0 })
    const w = await mountPage()
    const store = useSetupStore()
    expect(store.scrolledToBottom).toBe(false)
    const ro = FakeResizeObserver.instances.at(-1)
    expect(ro?.observed.length).toBeGreaterThan(0)

    Object.assign(box, { scrollHeight: 280, clientHeight: 300 })
    ro?.fire()
    await w.vm.$nextTick()
    expect(store.scrolledToBottom).toBe(true)
    expect(w.text()).not.toContain(HINT)
  })

  it('卸载时断开 ResizeObserver', async () => {
    const w = await mountPage()
    const ro = FakeResizeObserver.instances.at(-1)
    expect(ro?.disconnected).toBe(false)
    w.unmount()
    expect(ro?.disconnected).toBe(true)
  })
})
