/**
 * 企点画面流(01 §2.7.4;协议 C-08 / 02 #34)。
 *
 * `ws://127.0.0.1:17600/api/v1/accounts/{id}/stream?profile=…`,子协议 `qtrade-scrcpy-v1`。
 * - **首帧由服务端发** JSON `{codec,width,height,profile,fps,seq0}`(客户端不发首包,档位只在 query 里给)
 * - 后续二进制帧 = 8 字节 PTS(ms,big-endian)+ Annex-B H.264 NAL(SPS/PPS 随每个 IDR 重发)
 * - 控制帧 JSON `{type:'touch'|'key'|'scroll'|'text'|'pause'|'resume'|'profile'|'pong', …}` 走同一 WS
 *
 * 解码:`VideoDecoder` → `VideoFrame` → `canvas.drawImage`;`optimizeForLatency:true`;
 * 背压:`decodeQueueSize` 超过 `MAX_DECODE_QUEUE` 就丢非关键帧,一直丢到下一个关键帧(画面要「实时」不要「完整」)。
 * 降级三档:硬解 → 软解(强制 thumb)→ 静态预览(#33 截图轮询 + #35 REST 注入);**自动降、不自动升**。
 */

export type StreamProfile = 'thumb' | 'focus' | 'focus15' | 'thumb10'
export type DecodePath = 'hardware' | 'software' | 'static'

export interface StreamHeader {
  codec: string
  width: number
  height: number
  profile?: StreamProfile
  fps?: number
  seq0?: number
}

export interface StreamStats {
  fps: number
  latencyMs: number
  decoder: DecodePath
  codec: string
  connected: boolean
}

export interface ControlFrame {
  type: 'touch' | 'key' | 'scroll' | 'text' | 'pause' | 'resume' | 'profile' | 'pong'
  [k: string]: unknown
}

export const SUBPROTOCOL = 'qtrade-scrcpy-v1'
/** 解码器队列超过这个数就开始丢非关键帧 */
export const MAX_DECODE_QUEUE = 6
/** 同一档连续解码出错这么多次才降档(01 §2.7.4 降级 1) */
export const MAX_DECODE_ERRORS = 3
const PROBE_CODEC = 'avc1.42E01E'

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

export interface AnnexNal {
  /** NAL 头的低 5 位:7=SPS,8=PPS,5=IDR */
  type: number
  /** 不含起始码,含 NAL 头 */
  bytes: Uint8Array
}

/**
 * 拆 Annex-B。4 字节起始码 `00 00 00 01` 与 3 字节 `00 00 01` 都认。
 * `00 00 03` 是防竞争字节(emulation prevention),不是起始码,原样留在 NAL 里。
 * NAL 末尾的 `00`(下一个 4 字节起始码前多出的 trailing_zero)剥掉 —— 合法 NAL 不以 `00` 结尾。
 */
export function splitAnnexB(data: Uint8Array): AnnexNal[] {
  const marks: { sc: number; payload: number }[] = []
  for (let i = 0; i + 2 < data.length; i++) {
    if (data[i] !== 0 || data[i + 1] !== 0) continue
    if (data[i + 2] === 1) {
      marks.push({ sc: i, payload: i + 3 })
      i += 2
    } else if (data[i + 2] === 0 && i + 3 < data.length && data[i + 3] === 1) {
      marks.push({ sc: i, payload: i + 4 })
      i += 3
    }
  }
  const out: AnnexNal[] = []
  for (let n = 0; n < marks.length; n++) {
    let end = n + 1 < marks.length ? marks[n + 1].sc : data.length
    while (end > marks[n].payload && data[end - 1] === 0) end--
    const bytes = data.subarray(marks[n].payload, end)
    if (!bytes.length) continue
    out.push({ type: bytes[0] & 0x1f, bytes })
  }
  return out
}

/** Annex-B 关键帧判定:找 NAL type 5(IDR)或 7(SPS) */
export function isKeyFrame(nal: Uint8Array): boolean {
  return splitAnnexB(nal).some((n) => n.type === 5 || n.type === 7)
}

/**
 * WebCodecs 解 `avc1` 要的是 avcC 描述 + 4 字节长度前缀的 NAL,不是裸 Annex-B。
 * 把 Annex-B 直接塞进 `EncodedVideoChunk` 时,解码器报错、画布保持空白。
 * SPS/PPS 字节原样放进 avcC(含防竞争字节,avcC 存的就是 NAL 原文)。
 */
export function buildAvcC(sps: Uint8Array, pps: Uint8Array): { codec: string; description: Uint8Array } {
  const hex = (b: number) => b.toString(16).toUpperCase().padStart(2, '0')
  const codec = `avc1.${hex(sps[1] ?? 0)}${hex(sps[2] ?? 0)}${hex(sps[3] ?? 0)}`
  const description = new Uint8Array(11 + sps.length + pps.length)
  let o = 0
  description[o++] = 1
  description[o++] = sps[1] ?? 0x42
  description[o++] = sps[2] ?? 0xe0
  description[o++] = sps[3] ?? 0x1f
  description[o++] = 0xff
  description[o++] = 0xe1
  description[o++] = (sps.length >> 8) & 0xff
  description[o++] = sps.length & 0xff
  description.set(sps, o)
  o += sps.length
  description[o++] = 1
  description[o++] = (pps.length >> 8) & 0xff
  description[o++] = pps.length & 0xff
  description.set(pps, o)
  return { codec, description }
}

/** 一条访问单元里的 NAL 串成 AVCC(每条前面 4 字节大端长度)。 */
export function lengthPrefixed(nals: Uint8Array[]): Uint8Array {
  let size = 0
  for (const n of nals) size += 4 + n.length
  const out = new Uint8Array(size)
  let o = 0
  for (const n of nals) {
    out[o++] = (n.length >>> 24) & 0xff
    out[o++] = (n.length >>> 16) & 0xff
    out[o++] = (n.length >>> 8) & 0xff
    out[o++] = n.length & 0xff
    out.set(n, o)
    o += n.length
  }
  return out
}

function sameBytes(a: Uint8Array | null, b: Uint8Array): boolean {
  if (!a || a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false
  return true
}

/**
 * 🔴 #34 的关闭码分诊(backend-api-2 §6:「前四个要分别提示,不要一律『连接失败』」)。
 * 02 §3.4.7 目前只定义了 4401/4400,其余三个是后端为 #34 补的(待文档方在 §3.4.7 补登,
 * 控制台的关闭码表 01 §5.1 也要跟着加)。
 */
export const STREAM_CLOSE_CODES: Record<number, { reason: string; text: string; retryable: boolean }> = {
  4401: { reason: 'unauthorized', text: '令牌无效或已过期,需要重新取令牌', retryable: false },
  4400: { reason: 'bad_request', text: '画面流参数或控制帧格式不对', retryable: false },
  4409: { reason: 'channel_no_stream', text: '该通道不提供画面流(微信请改用截图预览)', retryable: false },
  4410: { reason: 'focus_taken', text: '该账号已有 focus 连接(同时只允许 1 个)', retryable: true },
  4503: { reason: 'stream_backend_missing', text: '画面流后端未就绪(本期未装配执行体)', retryable: false },
}

export interface StreamClosed {
  code: number
  reason: string
  text: string
  /** 本次连接曾经 open 过没有 —— 区分「握手就被拒」与「看着看着断了」 */
  everOpened: boolean
  /** 重试有没有意义(4503/4409/4401/4400 都没有) */
  retryable: boolean
}

export function classifyClose(code: number, everOpened: boolean): StreamClosed {
  const known = STREAM_CLOSE_CODES[code]
  if (known) return { code, reason: known.reason, text: known.text, everOpened, retryable: known.retryable }
  if (code === 1000 || code === 1001) return { code, reason: 'normal', text: '画面流已关闭', everOpened, retryable: true }
  return { code, reason: 'transport', text: `画面流连接中断(关闭码 ${code || '未知'})`, everOpened, retryable: true }
}

export interface StreamHooks {
  onHeader(h: StreamHeader): void
  onStats(s: Partial<StreamStats>): void
  onFrame(frame: VideoFrame): void
  onFatal(reason: string): void
  /** 连接关闭:按 `STREAM_CLOSE_CODES` 分诊后交给页面(不重连、不降级的判断在页面) */
  onClosed?(info: StreamClosed): void
  /** 降到第三档:流已关,页面改走 #33 截图轮询 + #35 REST 注入 */
  onStatic?(reason: string): void
}

/** 兼容旧调用:本机 WebCodecs 能不能解这路 H.264(不区分软硬) */
export async function hardwareSupported(codec = PROBE_CODEC): Promise<boolean> {
  return (await probeDecodePath(codec)) !== 'static'
}

/**
 * 启动探测(01 §2.7.4 降级 1):
 * 没有 `VideoDecoder` ⇒ 软解也没有 ⇒ 直接静态预览;
 * `prefer-hardware` 支持 ⇒ 硬解;只有 `prefer-software` 支持 ⇒ 软解;都不支持 ⇒ 静态预览。
 */
export async function probeDecodePath(codec = PROBE_CODEC): Promise<DecodePath> {
  const VD = (globalThis as { VideoDecoder?: typeof VideoDecoder }).VideoDecoder
  if (!VD || typeof VD.isConfigSupported !== 'function') return 'static'
  const ok = async (hardwareAcceleration: HardwareAcceleration): Promise<boolean> => {
    try {
      return !!(await VD.isConfigSupported({ codec, optimizeForLatency: true, hardwareAcceleration })).supported
    } catch {
      return false
    }
  }
  if (await ok('prefer-hardware')) return 'hardware'
  if (await ok('prefer-software')) return 'software'
  return 'static'
}

const RANK: Record<DecodePath, number> = { hardware: 0, software: 1, static: 2 }

export class ScreenStream {
  private ws: WebSocket | null = null
  private decoder: VideoDecoder | null = null
  private frames = 0
  private fpsTimer: ReturnType<typeof setInterval> | null = null
  private decodeErrors = 0
  private header: StreamHeader | null = null
  private sps: Uint8Array | null = null
  private pps: Uint8Array | null = null
  /** 当前解码器是按哪组 SPS/PPS 配的 —— 变了(切档改分辨率)就要重新 configure */
  private configuredSps: Uint8Array | null = null
  private configuredPps: Uint8Array | null = null
  private configuring = false
  /** 配置过一次以后,宽高以 SPS 为准,不再带首帧里的宽高 */
  private configuredOnce = false
  /** 背压触发后置位:丢掉所有非关键帧直到下一个关键帧 */
  private dropUntilKey = false
  private tsUs = 0
  private paused = false
  /** 收到不可重试的关闭码(4401/4400/4409/4503)后置位:服务端的 `restart` 一律不再理会 */
  private fatalClosed = false

  path: DecodePath = 'hardware'
  /** 背压丢掉的帧数(给状态条/测试看) */
  dropped = 0
  /** 最近一次关闭的分诊结果(页面据此提示,而不是一律「连接失败」) */
  lastClose: StreamClosed | null = null

  constructor(
    private readonly accountId: string,
    private profile: StreamProfile,
    private readonly hooks: StreamHooks,
  ) {}

  get currentProfile(): StreamProfile {
    return this.profile
  }

  async start(): Promise<void> {
    this.fatalClosed = false
    this.decodeErrors = 0
    this.dropUntilKey = false
    const probed = await probeDecodePath()
    // 不自动升:已经降过的档不因为重新 start 就回去(升档只走 retryHardware)
    if (RANK[probed] > RANK[this.path]) this.path = probed
    if (this.path === 'software') this.profile = 'thumb'
    this.hooks.onStats({ decoder: this.path })
    if (this.path === 'static') {
      this.enterStatic('本机没有可用的 WebCodecs 解码器')
      return
    }
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
    let everOpened = false
    ws.onopen = () => {
      everOpened = true
      this.hooks.onStats({ connected: true })
      // 窗口在开流前就已隐藏:连上立刻补一个 pause
      if (this.paused) ws.send(JSON.stringify({ type: 'pause' }))
    }
    ws.onclose = (ev) => {
      this.hooks.onStats({ connected: false })
      const info = classifyClose(ev?.code ?? 0, everOpened)
      this.lastClose = info
      // 🔴 4503/4409/4401/4400 重试没有意义 —— 停手,由页面显示对应提示,不无限重连
      if (!info.retryable) this.fatalClosed = true
      this.hooks.onClosed?.(info)
    }
    ws.onerror = () => this.hooks.onStats({ connected: false })
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        this.handleJson(ev.data)
        return
      }
      this.handleBinary(ev.data as ArrayBuffer)
    }
  }

  /** 主动关连接:先摘掉回调,免得自己关的连接又被当成「断线」上报 */
  private closeSocket(): void {
    const ws = this.ws
    this.ws = null
    if (!ws) return
    ws.onopen = null
    ws.onclose = null
    ws.onerror = null
    ws.onmessage = null
    try { ws.close() } catch { /* 已关闭 */ }
  }

  private closeDecoder(): void {
    try { this.decoder?.close() } catch { /* 已关闭 */ }
    this.decoder = null
    this.configuredSps = null
    this.configuredPps = null
  }

  private handleJson(text: string): void {
    let obj: Record<string, unknown>
    try { obj = JSON.parse(text) } catch { return }
    if (obj.type === 'restart') {
      if (this.fatalClosed || this.path === 'static') return
      this.closeDecoder()
      this.closeSocket()
      this.openSocket()
      return
    }
    if (obj.type === 'ping') {
      this.send({ type: 'pong' })
      return
    }
    if (typeof obj.codec === 'string') {
      this.header = obj as unknown as StreamHeader
      this.hooks.onHeader(this.header)
      this.hooks.onStats({ codec: this.header.codec })
    }
  }

  /**
   * 等到带 SPS/PPS 的关键帧再 `configure`。
   * `description` 是 avcC;之后每帧用长度前缀,不用 Annex-B 起始码。
   */
  private async configureDecoder(accessUnit: Uint8Array): Promise<void> {
    const VD = (globalThis as { VideoDecoder?: typeof VideoDecoder }).VideoDecoder
    const sps = this.sps
    const pps = this.pps
    if (!VD) {
      this.configuring = false
      this.enterStatic('本机不支持 WebCodecs')
      return
    }
    if (!sps || !pps) {
      this.configuring = false
      return
    }
    const { codec, description } = buildAvcC(sps, pps)
    const hardwareAcceleration: HardwareAcceleration = this.path === 'software' ? 'prefer-software' : 'prefer-hardware'
    const config: VideoDecoderConfig = { codec, description, optimizeForLatency: true, hardwareAcceleration }
    // 首帧给的宽高只在第一次配置时用;SPS 变过(切档改分辨率)以后以 SPS 为准,不再带旧宽高
    if (this.header && !this.configuredOnce && this.header.width > 0 && this.header.height > 0) {
      config.codedWidth = this.header.width
      config.codedHeight = this.header.height
    }
    try {
      if (typeof VD.isConfigSupported === 'function') {
        const supported = await VD.isConfigSupported(config)
        if (!supported.supported) {
          this.configuring = false
          const before = this.path
          this.degrade('WebCodecs 解不了这路 H.264')
          // 硬解 → 软解:手上这个就是关键帧,直接按软解再配一次,不用等下一个 IDR
          if (before === 'hardware' && this.path === 'software') {
            this.configuring = true
            await this.configureDecoder(accessUnit)
          }
          return
        }
      }
      this.closeDecoder()
      const decoder: VideoDecoder = new VD({
        output: (frame) => {
          this.decodeErrors = 0
          this.frames += 1
          this.hooks.onFrame(frame)
        },
        error: () => {
          // 出错后解码器已关闭:丢掉它,等下一个关键帧重配;连续出错才降档
          if (this.decoder === decoder) this.closeDecoder()
          this.onDecodeError('解码器连续出错')
        },
      })
      this.decoder = decoder
      decoder.configure(config)
      this.configuredSps = sps
      this.configuredPps = pps
      this.configuredOnce = true
      this.hooks.onStats({ codec, decoder: this.path })
      this.decodeAccessUnit(accessUnit, true)
    } catch (e) {
      this.closeDecoder()
      this.onDecodeError(e instanceof Error ? e.message : '解码器初始化失败')
    } finally {
      this.configuring = false
    }
  }

  private onDecodeError(reason: string): void {
    this.decodeErrors += 1
    if (this.decodeErrors >= MAX_DECODE_ERRORS) this.degrade(reason)
  }

  private handleBinary(buf: ArrayBuffer): void {
    if (this.path === 'static') return
    const { ptsMs, nal } = parseFrame(buf)
    const parts = splitAnnexB(nal)
    const vcl: Uint8Array[] = []
    let key = false
    for (const part of parts) {
      if (part.type === 7) this.sps = part.bytes
      else if (part.type === 8) this.pps = part.bytes
      else if (part.type === 5) { key = true; vcl.push(part.bytes) }
      else if (part.type === 1) vcl.push(part.bytes)
    }
    if (!vcl.length) return
    const access = lengthPrefixed(key && this.sps && this.pps ? [this.sps, this.pps, ...vcl] : vcl)
    this.hooks.onStats({ latencyMs: Math.max(0, Date.now() - ptsMs) })
    if (this.configuring) return
    const paramsChanged = key && !!this.sps && !!this.pps && this.decoder?.state === 'configured'
      && (!sameBytes(this.configuredSps, this.sps) || !sameBytes(this.configuredPps, this.pps))
    if (!this.decoder || this.decoder.state !== 'configured' || paramsChanged) {
      if (key && this.sps && this.pps) {
        this.configuring = true
        this.dropUntilKey = false
        void this.configureDecoder(access)
      }
      return
    }
    if (this.dropUntilKey) {
      if (!key) { this.dropped += 1; return }
      this.dropUntilKey = false
    } else if (!key && this.decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
      // 解码跟不上:这一帧起丢非关键帧,直到下一个关键帧(delta 帧丢一个后面的都没法解)
      this.dropUntilKey = true
      this.dropped += 1
      return
    }
    this.decodeAccessUnit(access, key)
  }

  private decodeAccessUnit(data: Uint8Array, key: boolean): void {
    if (!this.decoder || this.decoder.state !== 'configured') return
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta',
        timestamp: this.tsUs,
        duration: 100_000,
        data,
      }))
      this.tsUs += 100_000
    } catch {
      this.onDecodeError('解码失败')
    }
  }

  /** 三档之间自动降、**不自动升**(升级需用户点「重试硬解」) */
  private degrade(reason: string): void {
    this.decodeErrors = 0
    this.closeDecoder()
    if (this.path === 'hardware') {
      this.path = 'software'
      this.hooks.onStats({ decoder: 'software' })
      this.hooks.onFatal(`已降为软解(540p@5fps):${reason}`)
      // 软解强制 thumb;在线切档也让 Agent 重起编码器、尽快给出新的 IDR
      if (this.profile !== 'thumb') this.setProfile('thumb')
    } else if (this.path === 'software') {
      this.enterStatic(reason)
    }
  }

  private enterStatic(reason: string): void {
    this.path = 'static'
    this.closeDecoder()
    this.stop()
    this.hooks.onStats({ decoder: 'static', connected: false })
    this.hooks.onFatal(`已降为静态预览(每 2 秒截图):${reason}`)
    this.hooks.onStatic?.(reason)
  }

  /** 发出去了返回 true(WS 没连上时返回 false) */
  send(frame: ControlFrame): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify(frame))
    return true
  }

  setProfile(p: StreamProfile): void {
    // 软解档只能 thumb(01 §2.7.4 降级 1)
    if (this.path === 'software' && p !== 'thumb') return
    this.profile = p
    this.send({ type: 'profile', profile: p })
  }

  /** 窗口最小化/隐藏(§2.7.4):没连上时记住,连上后补发 */
  pause(): void {
    this.paused = true
    this.send({ type: 'pause' })
  }

  resume(): void {
    this.paused = false
    this.send({ type: 'resume' })
  }

  get isPaused(): boolean {
    return this.paused
  }

  /** 用户点「重试硬解」才升档 */
  retryHardware(): void {
    this.stop()
    this.path = 'hardware'
    this.hooks.onStats({ decoder: 'hardware' })
    void this.start()
  }

  stop(): void {
    if (this.fpsTimer) clearInterval(this.fpsTimer)
    this.fpsTimer = null
    this.closeDecoder()
    this.closeSocket()
  }
}
