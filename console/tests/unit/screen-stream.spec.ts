/**
 * 企点画面流(01 §2.7.4 / 02 #34):
 * ① Annex-B 拆分、avcC、长度前缀(3/4 字节起始码、防竞争字节、尾随零);
 * ② 降档链:硬解 → 软解(强制 thumb)→ 静态预览,自动降、不自动升;
 * ③ 解码背压:decodeQueueSize > 6 丢非关键帧直到下一个关键帧;
 * ④ SPS/PPS 变化(切档改分辨率)重新 configure;
 * ⑤ 开流前就隐藏:连上后补发 pause。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildAvcC, lengthPrefixed, MAX_DECODE_QUEUE, ScreenStream, splitAnnexB, type StreamHooks,
} from '@/codec/stream'

/* ────────── ① 纯函数 ────────── */

/** SPS 里带防竞争字节 `00 00 03`(payload 里本来是 `00 00 01` / `00 00 00`) */
const SPS_EP = new Uint8Array([0x67, 0x64, 0x00, 0x1f, 0xac, 0x00, 0x00, 0x03, 0x01, 0x00, 0x00, 0x03, 0x00, 0x80])
const PPS = new Uint8Array([0x68, 0xee, 0x3c, 0x80])

describe('splitAnnexB', () => {
  it('4 字节起始码', () => {
    const data = new Uint8Array([0, 0, 0, 1, 0x67, 0xaa, 0, 0, 0, 1, 0x68, 0xbb, 0, 0, 0, 1, 0x65, 0x88, 0x84])
    const nals = splitAnnexB(data)
    expect(nals.map((n) => n.type)).toEqual([7, 8, 5])
    expect([...nals[2].bytes]).toEqual([0x65, 0x88, 0x84])
  })

  it('3 字节起始码,以及 3/4 字节混用', () => {
    const data = new Uint8Array([0, 0, 1, 0x67, 0xaa, 0, 0, 0, 1, 0x68, 0xbb, 0, 0, 1, 0x41, 0x9a])
    const nals = splitAnnexB(data)
    expect(nals.map((n) => n.type)).toEqual([7, 8, 1])
    expect([...nals[0].bytes]).toEqual([0x67, 0xaa])
    expect([...nals[1].bytes]).toEqual([0x68, 0xbb])
    expect([...nals[2].bytes]).toEqual([0x41, 0x9a])
  })

  it('SPS 里的防竞争字节 00 00 03 不当起始码,原样保留', () => {
    const data = new Uint8Array([0, 0, 0, 1, ...SPS_EP, 0, 0, 0, 1, ...PPS])
    const nals = splitAnnexB(data)
    expect(nals).toHaveLength(2)
    expect([...nals[0].bytes]).toEqual([...SPS_EP])
    expect([...nals[1].bytes]).toEqual([...PPS])
  })

  it('下一个起始码前多出来的 trailing zero 剥掉', () => {
    const data = new Uint8Array([0, 0, 1, 0x67, 0xaa, 0, 0, 0, 0, 1, 0x68, 0xbb])
    const nals = splitAnnexB(data)
    expect([...nals[0].bytes]).toEqual([0x67, 0xaa])
    expect([...nals[1].bytes]).toEqual([0x68, 0xbb])
  })

  it('没有起始码 ⇒ 空', () => {
    expect(splitAnnexB(new Uint8Array([0x65, 1, 2, 3]))).toEqual([])
  })
})

describe('buildAvcC', () => {
  it('codec 串取 SPS 的 profile/constraint/level;avcC 原样带上 SPS(含防竞争字节)与 PPS', () => {
    const { codec, description } = buildAvcC(SPS_EP, PPS)
    expect(codec).toBe('avc1.64001F')
    expect([...description.subarray(0, 6)]).toEqual([1, 0x64, 0x00, 0x1f, 0xff, 0xe1])
    expect((description[6] << 8) | description[7]).toBe(SPS_EP.length)
    expect([...description.subarray(8, 8 + SPS_EP.length)]).toEqual([...SPS_EP])
    const o = 8 + SPS_EP.length
    expect(description[o]).toBe(1)
    expect((description[o + 1] << 8) | description[o + 2]).toBe(PPS.length)
    expect([...description.subarray(o + 3)]).toEqual([...PPS])
    expect(description.length).toBe(11 + SPS_EP.length + PPS.length)
  })
})

describe('lengthPrefixed', () => {
  it('每条 NAL 前 4 字节大端长度', () => {
    const out = lengthPrefixed([new Uint8Array([0x65, 1, 2]), new Uint8Array(300).fill(7)])
    expect([...out.subarray(0, 7)]).toEqual([0, 0, 0, 3, 0x65, 1, 2])
    expect([...out.subarray(7, 11)]).toEqual([0, 0, 0x01, 0x2c])
    expect(out.length).toBe(4 + 3 + 4 + 300)
  })

  it('空列表 ⇒ 空', () => {
    expect(lengthPrefixed([]).length).toBe(0)
  })
})

/* ────────── 假 WebSocket / VideoDecoder ────────── */

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
  constructor(public url: string, public protocol?: string) { FakeWS.instances.push(this) }
  send(d: string): void { this.sent.push(JSON.parse(d)) }
  close(): void { this.readyState = 3 }
  open(): void { this.readyState = 1; this.onopen?.({}) }
  json(o: unknown): void { this.onmessage?.({ data: JSON.stringify(o) }) }
  bin(b: ArrayBuffer): void { this.onmessage?.({ data: b }) }
}

type Accel = 'prefer-hardware' | 'prefer-software'

class FakeVD {
  static support: Record<Accel, boolean> = { 'prefer-hardware': true, 'prefer-software': true }
  static instances: FakeVD[] = []
  static async isConfigSupported(c: { hardwareAcceleration?: Accel }) {
    return { supported: FakeVD.support[c.hardwareAcceleration ?? 'prefer-hardware'] }
  }
  state = 'unconfigured'
  decodeQueueSize = 0
  configs: Record<string, unknown>[] = []
  chunks: { type: string }[] = []
  constructor(public init: { output: (f: unknown) => void; error: (e: unknown) => void }) { FakeVD.instances.push(this) }
  configure(c: Record<string, unknown>): void { this.configs.push(c); this.state = 'configured' }
  decode(ch: { type: string }): void { this.chunks.push(ch) }
  close(): void { this.state = 'closed' }
  fail(): void { this.state = 'closed'; this.init.error(new Error('decode error')) }
  emit(): void { this.init.output({ close() {} }) }
}

class FakeChunk { constructor(o: Record<string, unknown>) { Object.assign(this, o) } }

function frame(nals: number[][]): ArrayBuffer {
  const body = nals.flatMap((n) => [0, 0, 0, 1, ...n])
  const buf = new ArrayBuffer(8 + body.length)
  new DataView(buf).setUint32(4, Date.now() % 2 ** 32)
  new Uint8Array(buf, 8).set(body)
  return buf
}
const SPS_A = [0x67, 0x42, 0xc0, 0x1f, 0xda, 0x01]
const SPS_B = [0x67, 0x42, 0xc0, 0x28, 0xda, 0x02]
const PPS_A = [0x68, 0xce, 0x3c, 0x80]
const idr = (sps = SPS_A) => frame([sps, PPS_A, [0x65, 0x88, 0x84]])
const delta = () => frame([[0x41, 0x9a, 0x02]])

const tick = () => new Promise((r) => setTimeout(r, 0))

function hooks() {
  return {
    onHeader: vi.fn(),
    onStats: vi.fn(),
    onFrame: vi.fn(),
    onFatal: vi.fn(),
    onClosed: vi.fn(),
    onStatic: vi.fn(),
  } satisfies StreamHooks
}

async function openStream(h = hooks(), profile: 'focus' | 'thumb' = 'focus') {
  const s = new ScreenStream('qd01', profile, h)
  await s.start()
  const ws = FakeWS.instances.at(-1)
  if (ws) {
    ws.open()
    ws.json({ codec: 'h264', width: 720, height: 1280, profile, fps: 30, seq0: 0 })
  }
  return { s, ws, h }
}

/** 送一个关键帧并等解码器配好,返回这一台解码器 */
async function feedKey(ws: FakeWS, sps = SPS_A): Promise<FakeVD> {
  ws.bin(idr(sps))
  await tick()
  await tick()
  return FakeVD.instances.at(-1)!
}

beforeEach(() => {
  FakeWS.instances = []
  FakeVD.instances = []
  FakeVD.support = { 'prefer-hardware': true, 'prefer-software': true }
  vi.stubGlobal('WebSocket', FakeWS)
  vi.stubGlobal('VideoDecoder', FakeVD)
  vi.stubGlobal('EncodedVideoChunk', FakeChunk)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/* ────────── ② 降档链 ────────── */

describe('降档:硬解 → 软解 → 静态预览(自动降、不自动升)', () => {
  it('没有 WebCodecs ⇒ 直接静态预览,不开 WS', async () => {
    vi.stubGlobal('VideoDecoder', undefined)
    const h = hooks()
    const s = new ScreenStream('qd01', 'focus', h)
    await s.start()
    expect(s.path).toBe('static')
    expect(h.onStatic).toHaveBeenCalledTimes(1)
    expect(h.onStats).toHaveBeenCalledWith(expect.objectContaining({ decoder: 'static' }))
    expect(FakeWS.instances).toHaveLength(0)
  })

  it('只有软解可用 ⇒ 起步就是软解,连接档位强制 thumb', async () => {
    FakeVD.support['prefer-hardware'] = false
    const { s, ws } = await openStream()
    expect(s.path).toBe('software')
    expect(ws!.url).toContain('profile=thumb')
    const vd = await feedKey(ws!)
    expect(vd.configs[0].hardwareAcceleration).toBe('prefer-software')
    s.stop()
  })

  it('软硬都不支持 ⇒ 静态预览', async () => {
    FakeVD.support = { 'prefer-hardware': false, 'prefer-software': false }
    const { s, h } = await openStream()
    expect(s.path).toBe('static')
    expect(h.onStatic).toHaveBeenCalled()
  })

  it('硬解连续 3 次出错 ⇒ 软解并在线切 thumb;软解再连续 3 次 ⇒ 静态预览、流关闭', async () => {
    const { s, ws, h } = await openStream()
    expect(s.path).toBe('hardware')

    for (let i = 0; i < 3; i++) {
      const vd = await feedKey(ws!)
      expect(vd.configs[0].hardwareAcceleration).toBe('prefer-hardware')
      vd.fail()
    }
    expect(s.path).toBe('software')
    expect(h.onStats).toHaveBeenCalledWith(expect.objectContaining({ decoder: 'software' }))
    expect(h.onFatal).toHaveBeenCalledWith(expect.stringContaining('软解'))
    expect(ws!.sent).toContainEqual({ type: 'profile', profile: 'thumb' })
    expect(h.onStatic).not.toHaveBeenCalled()

    for (let i = 0; i < 3; i++) {
      const vd = await feedKey(ws!)
      expect(vd.configs[0].hardwareAcceleration).toBe('prefer-software')
      vd.fail()
    }
    expect(s.path).toBe('static')
    expect(h.onStatic).toHaveBeenCalledTimes(1)
    expect(h.onStats).toHaveBeenCalledWith(expect.objectContaining({ decoder: 'static' }))
    expect(ws!.readyState).toBe(3)

    // 不自动升:之后再来关键帧也不再建解码器
    const before = FakeVD.instances.length
    ws!.bin(idr())
    await tick()
    expect(FakeVD.instances.length).toBe(before)
  })

  it('出错之间解出过帧 ⇒ 计数清零(「连续」3 次才降)', async () => {
    const { s, ws } = await openStream()
    for (let i = 0; i < 5; i++) {
      const vd = await feedKey(ws!)
      if (i % 2 === 0) vd.emit()
      vd.fail()
    }
    expect(s.path).toBe('hardware')
    s.stop()
  })

  it('配置时 prefer-hardware 不被支持 ⇒ 当场按软解重配同一个关键帧', async () => {
    const { s, ws } = await openStream()
    FakeVD.support['prefer-hardware'] = false
    const vd = await feedKey(ws!)
    expect(s.path).toBe('software')
    expect(vd.configs[0].hardwareAcceleration).toBe('prefer-software')
    expect(vd.chunks).toHaveLength(1)
    s.stop()
  })

  it('用户点「重试硬解」才回到硬解', async () => {
    FakeVD.support['prefer-hardware'] = false
    const { s } = await openStream()
    expect(s.path).toBe('software')
    FakeVD.support['prefer-hardware'] = true
    await s.start() // 自己重开不升档
    expect(s.path).toBe('software')
    s.retryHardware()
    await tick()
    expect(s.path).toBe('hardware')
    s.stop()
  })
})

/* ────────── ③ 背压 ────────── */

describe('解码背压', () => {
  it(`decodeQueueSize > ${MAX_DECODE_QUEUE} ⇒ 丢非关键帧,一直丢到下一个关键帧`, async () => {
    const { s, ws } = await openStream()
    const vd = await feedKey(ws!)
    expect(vd.chunks).toHaveLength(1)

    ws!.bin(delta())
    expect(vd.chunks).toHaveLength(2) // 队列不深,照常解

    vd.decodeQueueSize = MAX_DECODE_QUEUE + 1
    ws!.bin(delta())
    ws!.bin(delta())
    expect(vd.chunks).toHaveLength(2)

    // 队列已经下去了,但中间丢过 delta,参考帧断了 ⇒ 仍然丢到关键帧
    vd.decodeQueueSize = 0
    ws!.bin(delta())
    expect(vd.chunks).toHaveLength(2)
    expect(s.dropped).toBe(3)

    ws!.bin(idr())
    expect(vd.chunks).toHaveLength(3)
    expect(vd.chunks[2].type).toBe('key')

    ws!.bin(delta())
    expect(vd.chunks).toHaveLength(4)
    s.stop()
  })

  it(`队列正好 ${MAX_DECODE_QUEUE} 不丢;关键帧即使队列深也照解`, async () => {
    const { s, ws } = await openStream()
    const vd = await feedKey(ws!)
    vd.decodeQueueSize = MAX_DECODE_QUEUE
    ws!.bin(delta())
    expect(vd.chunks).toHaveLength(2)
    vd.decodeQueueSize = 50
    ws!.bin(idr())
    expect(vd.chunks).toHaveLength(3)
    s.stop()
  })
})

/* ────────── ④ SPS 变化重配 ────────── */

describe('SPS/PPS 变化', () => {
  it('同一组 SPS/PPS 的后续 IDR 不重配', async () => {
    const { s, ws } = await openStream()
    const vd = await feedKey(ws!)
    ws!.bin(idr())
    await tick()
    expect(FakeVD.instances).toHaveLength(1)
    expect(vd.configs).toHaveLength(1)
    expect(vd.chunks).toHaveLength(2)
    s.stop()
  })

  it('切档后 SPS 变了 ⇒ 新解码器按新 avcC 配置,且不再带首帧的旧宽高', async () => {
    const { s, ws } = await openStream()
    const first = await feedKey(ws!)
    expect(first.configs[0]).toMatchObject({ codedWidth: 720, codedHeight: 1280, optimizeForLatency: true })

    const second = await feedKey(ws!, SPS_B)
    expect(FakeVD.instances).toHaveLength(2)
    expect(first.state).toBe('closed')
    expect(second.configs[0].codec).toBe('avc1.42C028')
    expect(second.configs[0]).not.toHaveProperty('codedWidth')
    expect(second.configs[0].optimizeForLatency).toBe(true)
    const desc = second.configs[0].description as Uint8Array
    expect([...desc.subarray(8, 8 + SPS_B.length)]).toEqual(SPS_B)
    expect(second.chunks).toHaveLength(1)
    s.stop()
  })
})

/* ────────── ⑤ pause / pong ────────── */

describe('控制帧', () => {
  it('开流前已隐藏 ⇒ 连上立刻补发 pause;resume 照发', async () => {
    const s = new ScreenStream('qd01', 'focus', hooks())
    s.pause()
    await s.start()
    const ws = FakeWS.instances.at(-1)!
    expect(ws.sent).toEqual([])
    ws.open()
    expect(ws.sent).toEqual([{ type: 'pause' }])
    s.resume()
    expect(ws.sent.at(-1)).toEqual({ type: 'resume' })
    s.stop()
  })

  it('服务端 ping ⇒ 回 pong', async () => {
    const { s, ws } = await openStream()
    ws!.json({ type: 'ping' })
    expect(ws!.sent.at(-1)).toEqual({ type: 'pong' })
    s.stop()
  })

  it('软解档不接受升到 focus', async () => {
    FakeVD.support['prefer-hardware'] = false
    const { s, ws } = await openStream()
    s.setProfile('focus')
    expect(ws!.sent.some((f) => f.type === 'profile' && f.profile === 'focus')).toBe(false)
    expect(s.currentProfile).toBe('thumb')
    s.stop()
  })

  it('主动 stop 不当成断线上报', async () => {
    const { s, h } = await openStream()
    s.stop()
    expect(h.onClosed).not.toHaveBeenCalled()
  })
})
