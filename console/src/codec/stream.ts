/**
 * 企点画面流(01 §2.7.4;协议 C-08 / 02 #34)。
 *
 * `ws://127.0.0.1:17600/api/v1/accounts/{id}/stream?profile=…`,子协议 `qtrade-scrcpy-v1`。
 * - **首帧由服务端发** JSON `{codec,width,height}`(客户端不发首包,档位只在 query 里给)
 * - 后续二进制帧 = 8 字节 PTS(ms,big-endian)+ Annex-B H.264 NAL
 * - 控制帧 JSON `{type:'touch'|'key'|'scroll'|'text'|'pause'|'resume'|'profile', …}` 走同一 WS
 *
 * 解码:`VideoDecoder` → `VideoFrame` → `canvas.drawImage`;`optimizeForLatency:true`;
 * 队列 > 3 帧丢到最新关键帧(画面要「实时」不要「完整」)。
 * 降级三档:硬解 → 软解(强制 thumb)→ 静态预览;**自动降、不自动升**。
 */

export type StreamProfile = 'thumb' | 'focus' | 'focus15' | 'thumb10'
export type DecodePath = 'hardware' | 'software' | 'static'

export interface StreamHeader {
  codec: string
  width: number
  height: number
  profile?: StreamProfile
  fps?: number
}

export interface StreamStats {
  fps: number
  latencyMs: number
  decoder: DecodePath
  codec: string
  connected: boolean
}

export interface ControlFrame {
  type: 'touch' | 'key' | 'scroll' | 'text' | 'pause' | 'resume' | 'profile'
  [k: string]: unknown
}

export const SUBPROTOCOL = 'qtrade-scrcpy-v1'
const MAX_QUEUE = 3

export function streamUrl(accountId: string, profile: StreamProfile): string {
  const base =
    typeof location !== 'undefined' && location.protocol.startsWith('http')
      ? `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}`
      : 'ws://127.0.0.1:17600'
  return `${base}/api/v1/accounts/${accountId}/stream?profile=${profile}`
}

/** 拆一帧二进制:前 8 字节 PTS(ms, BE uint64)+ Annex-B NAL */
export function parseFrame(buf: ArrayBuffer): { ptsMs: number; nal: Uint8Array } {
  const view = new DataView(buf)
  const hi = view.getUint32(0)
  const lo = view.getUint32(4)
  const ptsMs = hi * 2 ** 32 + lo
  return { ptsMs, nal: new Uint8Array(buf, 8) }
}

/** Annex-B 关键帧判定:找 NAL type 5(IDR)或 7(SPS) */
export function isKeyFrame(nal: Uint8Array): boolean {
  for (let i = 0; i + 4 < nal.length; i++) {
    if (nal[i] === 0 && nal[i + 1] === 0 && nal[i + 2] === 1) {
      const t = nal[i + 3] & 0x1f
      if (t === 5 || t === 7) return true
    }
  }
  return false
}

export interface StreamHooks {
  onHeader(h: StreamHeader): void
  onStats(s: Partial<StreamStats>): void
  onFrame(frame: VideoFrame): void
  onFatal(reason: string): void
}

export async function hardwareSupported(codec = 'avc1.42E01E'): Promise<boolean> {
  const VD = (globalThis as { VideoDecoder?: typeof VideoDecoder }).VideoDecoder
  if (!VD || typeof VD.isConfigSupported !== 'function') return false
  try {
    const r = await VD.isConfigSupported({ codec, optimizeForLatency: true })
    return !!r.supported
  } catch {
    return false
  }
}

export class ScreenStream {
  private ws: WebSocket | null = null
  private decoder: VideoDecoder | null = null
  private queue = 0
  private frames = 0
  private fpsTimer: ReturnType<typeof setInterval> | null = null
  private decodeErrors = 0
  private header: StreamHeader | null = null

  path: DecodePath = 'hardware'

  constructor(
    private readonly accountId: string,
    private profile: StreamProfile,
    private readonly hooks: StreamHooks,
  ) {}

  async start(): Promise<void> {
    const hw = await hardwareSupported()
    if (!hw) this.path = 'software'
    this.hooks.onStats({ decoder: this.path })
    this.openSocket()
    this.fpsTimer = setInterval(() => {
      this.hooks.onStats({ fps: this.frames })
      this.frames = 0
    }, 1000)
  }

  private openSocket(): void {
    const ws = new WebSocket(streamUrl(this.accountId, this.profile), SUBPROTOCOL)
    ws.binaryType = 'arraybuffer'
    this.ws = ws
    ws.onopen = () => this.hooks.onStats({ connected: true })
    ws.onclose = () => this.hooks.onStats({ connected: false })
    ws.onerror = () => this.hooks.onStats({ connected: false })
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        this.handleJson(ev.data)
        return
      }
      this.handleBinary(ev.data as ArrayBuffer)
    }
  }

  private handleJson(text: string): void {
    let obj: Record<string, unknown>
    try { obj = JSON.parse(text) } catch { return }
    if (obj.type === 'restart') {
      this.stop()
      this.openSocket()
      return
    }
    if (obj.type === 'ping') {
      this.ws?.send(JSON.stringify({ type: 'pong' }))
      return
    }
    if (typeof obj.codec === 'string') {
      this.header = obj as unknown as StreamHeader
      this.hooks.onHeader(this.header)
      this.hooks.onStats({ codec: this.header.codec })
      void this.configureDecoder(this.header)
    }
  }

  private async configureDecoder(h: StreamHeader): Promise<void> {
    const VD = (globalThis as { VideoDecoder?: typeof VideoDecoder }).VideoDecoder
    if (!VD) { this.degrade('本机不支持 WebCodecs'); return }
    try {
      this.decoder = new VD({
        output: (frame) => {
          this.queue = Math.max(0, this.queue - 1)
          this.frames += 1
          this.hooks.onFrame(frame)
        },
        error: () => {
          this.decodeErrors += 1
          // 连续 3 次解码 error → 降档
          if (this.decodeErrors >= 3) this.degrade('解码器连续出错')
        },
      })
      this.decoder.configure({
        codec: h.codec.startsWith('avc') ? h.codec : 'avc1.42E01E',
        codedWidth: h.width,
        codedHeight: h.height,
        optimizeForLatency: true,
      })
    } catch (e) {
      this.degrade(e instanceof Error ? e.message : '解码器初始化失败')
    }
  }

  private handleBinary(buf: ArrayBuffer): void {
    if (!this.decoder || this.decoder.state !== 'configured') return
    const { ptsMs, nal } = parseFrame(buf)
    const key = isKeyFrame(nal)
    // 队列 > 3 帧丢到最新关键帧:画面要「实时」不要「完整」
    if (this.queue > MAX_QUEUE && !key) return
    this.hooks.onStats({ latencyMs: Math.max(0, Date.now() - ptsMs) })
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta',
        timestamp: ptsMs * 1000,
        data: nal,
      }))
      this.queue += 1
    } catch {
      this.decodeErrors += 1
      if (this.decodeErrors >= 3) this.degrade('解码失败')
    }
  }

  /** 三档之间自动降、**不自动升**(升级需用户点「重试硬解」) */
  private degrade(reason: string): void {
    if (this.path === 'hardware') {
      this.path = 'software'
      this.profile = 'thumb'
      this.hooks.onStats({ decoder: 'software' })
      this.hooks.onFatal(`已降为软解(540p@5fps):${reason}`)
    } else if (this.path === 'software') {
      this.path = 'static'
      this.hooks.onStats({ decoder: 'static' })
      this.hooks.onFatal(`已降为静态预览:${reason}`)
    }
  }

  send(frame: ControlFrame): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(frame))
  }

  setProfile(p: StreamProfile): void {
    this.profile = p
    this.send({ type: 'profile', profile: p })
  }

  pause(): void { this.send({ type: 'pause' }) }
  resume(): void { this.send({ type: 'resume' }) }

  /** 用户点「重试硬解」才升档 */
  retryHardware(): void {
    this.decodeErrors = 0
    this.path = 'hardware'
    this.hooks.onStats({ decoder: 'hardware' })
    this.stop()
    void this.start()
  }

  stop(): void {
    if (this.fpsTimer) clearInterval(this.fpsTimer)
    this.fpsTimer = null
    try { this.decoder?.close() } catch { /* 已关闭 */ }
    this.decoder = null
    try { this.ws?.close() } catch { /* 已关闭 */ }
    this.ws = null
  }
}
