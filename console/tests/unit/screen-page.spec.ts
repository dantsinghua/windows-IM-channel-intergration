/**
 * P-SCREEN 画面页(01 §2.7.4):
 * B5 —— 首次进页自动选账号那条路径也要注册可见性监听;隐藏 ⇒ pause,恢复 ⇒ resume;
 * B4 —— 没有 WebCodecs ⇒ 降到静态预览,真的去轮询 #33 截图;
 * 画布事件:首帧前不发;按住期间什么都不发(长按);可打印字符攒进输入框、回车整段发 text。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ScreenPage from '@/pages/screen/ScreenPage.vue'
import { useAccountsStore } from '@/stores/accounts'
import type { Account } from '@/api/types'

const routeState = vi.hoisted(() => ({ params: {} as Record<string, string> }))
const routerSpy = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }))
vi.mock('vue-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('vue-router')>()),
  useRoute: () => routeState,
  useRouter: () => routerSpy,
}))

class FakeWS {
  static OPEN = 1
  static instances: FakeWS[] = []
  readyState = 0
  binaryType = ''
  sent: Record<string, unknown>[] = []
  onopen: ((ev: unknown) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  constructor(public url: string) { FakeWS.instances.push(this) }
  send(d: string): void { this.sent.push(JSON.parse(d)) }
  close(): void { this.readyState = 3 }
  open(): void { this.readyState = 1; this.onopen?.({}) }
  json(o: unknown): void { this.onmessage?.({ data: JSON.stringify(o) }) }
}

class FakeVD {
  static async isConfigSupported() { return { supported: true } }
  state = 'unconfigured'
  decodeQueueSize = 0
  configure(): void { this.state = 'configured' }
  decode(): void {}
  close(): void { this.state = 'closed' }
}

function acct(id: string, channel: Account['channel'], state: Account['state'] = 'running'): Account {
  return {
    id, channel, host: 'wsl', label: id, state, state_code: '', state_reason: '', error_since_ms: null,
    enabled: true, auto_recover: true, deleted_ms: null, runtime: {} as Account['runtime'], capabilities: [], quota_mb: 0,
  } as Account
}

let wrapper: VueWrapper | null = null

async function mountPage(accounts: Account[] = [acct('qd01', 'qidian')]): Promise<VueWrapper> {
  useAccountsStore().items = accounts
  wrapper = shallowMount(ScreenPage)
  for (let i = 0; i < 6; i++) await flushPromises()
  return wrapper
}

const ws = () => FakeWS.instances.at(-1)!

beforeEach(() => {
  setActivePinia(createPinia())
  routeState.params = {}
  FakeWS.instances = []
  vi.stubGlobal('WebSocket', FakeWS)
  vi.stubGlobal('VideoDecoder', FakeVD)
  vi.stubGlobal('fetch', vi.fn().mockImplementation(async () =>
    new Response(new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: 'image/png' }),
      { status: 200, headers: { 'Content-Type': 'image/png' } })))
  URL.createObjectURL = vi.fn(() => 'blob:shot')
  URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.unstubAllGlobals()
})

/* ────────── B5 ────────── */

describe('窗口最小化/隐藏 ⇒ pause,恢复 ⇒ resume(B5)', () => {
  function stubQt() {
    let cb: ((visible: boolean) => void) | null = null
    const off = vi.fn()
    const onVisibility = vi.fn((f: (visible: boolean) => void) => { cb = f; return off })
    vi.stubGlobal('qt', { window: { onVisibility }, wa: { invoke: vi.fn() } })
    return { onVisibility, off, fire: (v: boolean) => cb?.(v) }
  }

  it('首次进页(路由没带 id,自动选第一个账号)也注册了监听', async () => {
    const qt = stubQt()
    await mountPage()
    expect(qt.onVisibility).toHaveBeenCalledTimes(1)
    expect(FakeWS.instances).toHaveLength(1) // 只开一条 focus 流
    ws().open()
    qt.fire(false)
    expect(ws().sent).toContainEqual({ type: 'pause' })
    qt.fire(true)
    expect(ws().sent.at(-1)).toEqual({ type: 'resume' })
    wrapper!.unmount()
    wrapper = null
    expect(qt.off).toHaveBeenCalledTimes(1)
  })

  it('路由带了 id 的路径同样注册', async () => {
    routeState.params = { id: 'qd01' }
    const qt = stubQt()
    await mountPage()
    expect(qt.onVisibility).toHaveBeenCalledTimes(1)
    ws().open()
    qt.fire(false)
    expect(ws().sent).toContainEqual({ type: 'pause' })
  })

  it('隐藏时切账号:新流连上就补发 pause', async () => {
    const qt = stubQt()
    await mountPage([acct('qd01', 'qidian'), acct('qd02', 'qidian')])
    qt.fire(false)
    await wrapper!.find('[data-testid="qt-screen-thumb-qd02"]').trigger('click')
    for (let i = 0; i < 6; i++) await flushPromises()
    expect(ws().url).toContain('/accounts/qd02/')
    ws().open()
    expect(ws().sent).toEqual([{ type: 'pause' }])
  })

  it('没有 window.qt(纯浏览器调试)⇒ 退回 document.visibilitychange', async () => {
    let state: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
    await mountPage()
    ws().open()
    state = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))
    expect(ws().sent).toContainEqual({ type: 'pause' })
    state = 'visible'
    document.dispatchEvent(new Event('visibilitychange'))
    expect(ws().sent.at(-1)).toEqual({ type: 'resume' })
  })
})

/* ────────── B4 ────────── */

describe('降到静态预览(B4)', () => {
  it('没有 WebCodecs ⇒ 不开流,改为轮询 #33 截图,状态条显示「静态预览」', async () => {
    vi.stubGlobal('VideoDecoder', undefined)
    const w = await mountPage()
    expect(FakeWS.instances).toHaveLength(0)
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]))
    expect(calls.some((u) => u.includes('/accounts/qd01/screenshot'))).toBe(true)
    expect(w.find('[data-testid="qt-screen-status-decoder"]').text()).toContain('静态预览')
    expect(w.find('[data-testid="qt-screen-degrade-tag"]').text()).toContain('静态预览')
    expect(w.find('img.static-preview').exists()).toBe(true)
  })
})

/* ────────── 画布事件与键盘 ────────── */

describe('画布事件透传', () => {
  const box = { left: 0, top: 0, width: 720, height: 1280, right: 720, bottom: 1280, x: 0, y: 0, toJSON() {} }

  async function canvasReady(header = true) {
    const w = await mountPage()
    ws().open()
    if (header) ws().json({ codec: 'h264', width: 720, height: 1280, profile: 'focus', fps: 30, seq0: 0 })
    const c = w.find('canvas')
    ;(c.element as HTMLCanvasElement).getBoundingClientRect = () => box as DOMRect
    return { w, c }
  }

  it('首帧前按下不发任何帧', async () => {
    const { c } = await canvasReady(false)
    await c.trigger('pointerdown', { pointerId: 1, clientX: 360, clientY: 640 })
    expect(ws().sent.filter((f) => f.type === 'touch')).toEqual([])
  })

  it('长按:down 之后按住 1.5 秒什么都不发,松手才发 up', async () => {
    const { c } = await canvasReady()
    await c.trigger('pointerdown', { pointerId: 1, clientX: 360, clientY: 640 })
    await new Promise((r) => setTimeout(r, 30))
    await c.trigger('pointermove', { pointerId: 1, clientX: 360, clientY: 640 })
    const touches = () => ws().sent.filter((f) => f.type === 'touch')
    expect(touches()).toEqual([{ type: 'touch', action: 'down', x: 0.5, y: 0.5, pointer: 1 }])
    await c.trigger('pointerup', { pointerId: 1, clientX: 360, clientY: 640 })
    expect(touches().map((f) => f.action)).toEqual(['down', 'up'])
  })

  it('可打印字符不逐个发 text:攒进输入框,回车整段发;控制键直接发 key', async () => {
    const { w, c } = await canvasReady()
    await c.trigger('keydown', { key: 'a' })
    expect(ws().sent.filter((f) => f.type === 'text')).toEqual([])
    const input = w.find('input.ime-input')
    expect(input.exists()).toBe(true)
    await input.setValue('a你好')
    await input.trigger('keydown', { key: 'Enter' })
    expect(ws().sent.filter((f) => f.type === 'text')).toEqual([{ type: 'text', text: 'a你好' }])
    expect(w.find('input.ime-input').exists()).toBe(false)

    await c.trigger('keydown', { key: 'Backspace' })
    expect(ws().sent.slice(-2)).toEqual([
      { type: 'key', keycode: 'DEL', action: 'down' },
      { type: 'key', keycode: 'DEL', action: 'up' },
    ])
  })

  it('输入法组字中的回车不提交', async () => {
    const { w, c } = await canvasReady()
    await c.trigger('keydown', { key: 'n' })
    const input = w.find('input.ime-input')
    await input.trigger('keydown', { key: 'Enter', isComposing: true })
    expect(ws().sent.filter((f) => f.type === 'text')).toEqual([])
    expect(w.find('input.ime-input').exists()).toBe(true)
  })

  it('登录态输入框按密码框显示(不回显)', async () => {
    const w = await mountPage([acct('qd01', 'qidian', 'login_required')])
    ws().open()
    await w.find('canvas').trigger('keydown', { key: '1' })
    expect(w.find('input.ime-input').attributes('type')).toBe('password')
  })

  it('工具条没有「旋转」', async () => {
    const w = await mountPage()
    expect(w.find('[data-testid="qt-screen-tool-back"]').exists()).toBe(true)
    expect(w.find('[data-testid="qt-screen-tool-rotate"]').exists()).toBe(false)
  })
})
