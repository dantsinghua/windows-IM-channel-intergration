/** scrcpy PTS 是视频时间基准，不能与浏览器 Unix 毫秒相减冒充绝对延迟。 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ScreenStream, type StreamHooks } from '@/codec/stream'

class LocalSocket {
  static instance: LocalSocket
  onmessage: ((event: { data: ArrayBuffer }) => void) | null = null
  constructor() { LocalSocket.instance = this }
  close() {}
}

let stream: ScreenStream | undefined

beforeEach(() => {
  vi.stubGlobal('WebSocket', LocalSocket)
  vi.stubGlobal('VideoDecoder', {
    isConfigSupported: async () => ({ supported: true }),
  })
  vi.spyOn(Date, 'now').mockReturnValue(1_791_446_000_000)
})

afterEach(() => {
  stream?.stop()
  stream = undefined
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

it.each([0, 123456, 1_791_445_999_900])('PTS %s 未附时钟映射时不报告绝对延迟', async (pts) => {
  const hooks = {
    onHeader: vi.fn(), onStats: vi.fn(), onFrame: vi.fn(),
    onFatal: vi.fn(), onClosed: vi.fn(), onStatic: vi.fn(),
  } satisfies StreamHooks
  stream = new ScreenStream('qd82', 'focus', hooks)
  await stream.start()
  // 从真实公开 WebSocket 消息入口输入一帧；不用私有方法或真实设备。
  const frame = new ArrayBuffer(14)
  new DataView(frame).setBigUint64(0, BigInt(pts))
  new Uint8Array(frame, 8).set([0, 0, 0, 1, 0x41, 0x80])
  LocalSocket.instance.onmessage?.({ data: frame })
  const timing = hooks.onStats.mock.calls.map(([stats]) => stats).filter((stats) => 'latencyMs' in stats)
  expect(timing.length).toBeGreaterThan(0)
  expect(timing.at(-1)?.latencyMs).toBeNull()
})
