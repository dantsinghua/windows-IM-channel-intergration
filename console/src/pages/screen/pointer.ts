/**
 * 画面点击落点。
 * canvas 用 `object-fit: contain` 画在比视频更宽的盒子里,左右是黑边。
 * 按整块盒子归一化时,黑边上的点击会被钳到设备最左/最右,
 * 底部那一截正好是系统返回键,连点几次就把应用退回桌面。
 */

export interface NormBox {
  left: number
  top: number
  width: number
  height: number
}

/** `object-fit: contain` 且居中时,视频真正画出来的矩形。媒体尺寸未知则用整个盒子。 */
export function containRect(box: NormBox, mediaW: number, mediaH: number): NormBox {
  if (box.width <= 0 || box.height <= 0) return box
  if (mediaW <= 0 || mediaH <= 0) return box
  const scale = Math.min(box.width / mediaW, box.height / mediaH)
  const width = mediaW * scale
  const height = mediaH * scale
  return {
    left: box.left + (box.width - width) / 2,
    top: box.top + (box.height - height) / 2,
    width,
    height,
  }
}

/**
 * 指针相对视频画面的 0~1 坐标。
 * 落在黑边上返回 `null`(这次点击不送出,避免打到系统返回键)。
 */
export function normInContain(
  clientX: number,
  clientY: number,
  box: NormBox,
  mediaW: number,
  mediaH: number,
): { x: number; y: number } | null {
  const r = containRect(box, mediaW, mediaH)
  if (r.width <= 0 || r.height <= 0) return null
  if (clientX < r.left || clientY < r.top || clientX > r.left + r.width || clientY > r.top + r.height) {
    return null
  }
  return {
    x: Math.min(1, Math.max(0, (clientX - r.left) / r.width)),
    y: Math.min(1, Math.max(0, (clientY - r.top) / r.height)),
  }
}

/* ───────────── 指针 → #34 控制帧(C-08;02 #34 `touch{action,x,y,pointer}` / `scroll{x,y,dx,dy}`) ───────────── */

/** move / scroll 合并后的最短发送间隔(≤ 60 Hz) */
export const MOVE_MIN_INTERVAL_MS = 1000 / 60

export interface PointerSample {
  pointerId: number
  clientX: number
  clientY: number
}

export type TouchFrame = { type: 'touch'; action: 'down' | 'move' | 'up'; x: number; y: number; pointer: number }
export type ScrollFrame = { type: 'scroll'; x: number; y: number; dx: number; dy: number }

interface Pt { x: number; y: number }
interface Active { last: Pt; pending: Pt | null; lastSentAt: number; timer: ReturnType<typeof setTimeout> | null }

/**
 * 画布指针事件 → 控制帧,原样透传给 redroid,**前端不合成 tap / longpress**:
 * - `down` 立即发;`move` 只在按住时发,并合并到 ≤ 60 Hz(尾随补发最后位置,不丢终点);
 * - `up` / `cancel` 发 `up`(先补发还没发出的 move);松手落在黑边上时用最后一个画面内的点收尾;
 * - 长按 = down 与 up 之间什么都不发(位置没变的 move 不发);
 * - `pointerId` 原样透传为 `pointer`,多指各自独立;
 * - 首帧前(还不知道视频宽高)一律不发 —— 不能拿 canvas 默认 300×150 去算 contain 区。
 */
export class PointerRelay {
  private media: { w: number; h: number } | null = null
  private readonly active = new Map<number, Active>()
  private wheelAcc: { x: number; y: number; dx: number; dy: number } | null = null
  private wheelSentAt = -Infinity
  private wheelTimer: ReturnType<typeof setTimeout> | null = null

  constructor(
    private readonly send: (f: TouchFrame | ScrollFrame) => void,
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** 首帧 `{width,height}` 到了 / 解码出的帧尺寸变了时调用;传 0 表示「还不知道」 */
  setMedia(w: number, h: number): void {
    this.media = w > 0 && h > 0 ? { w, h } : null
  }

  get hasMedia(): boolean {
    return this.media !== null
  }

  get activeCount(): number {
    return this.active.size
  }

  private norm(s: { clientX: number; clientY: number }, box: NormBox): Pt | null {
    if (!this.media) return null
    return normInContain(s.clientX, s.clientY, box, this.media.w, this.media.h)
  }

  /** 返回 true 表示接管了这根指针(调用方据此 `setPointerCapture`) */
  down(s: PointerSample, box: NormBox): boolean {
    const p = this.norm(s, box)
    if (!p) return false
    // 同一根指针没收到 up 又按下(丢了事件):先把上一次收尾,别让设备侧一直按着
    if (this.active.has(s.pointerId)) this.finish(s.pointerId, null)
    this.active.set(s.pointerId, { last: p, pending: null, lastSentAt: this.now(), timer: null })
    this.send({ type: 'touch', action: 'down', x: p.x, y: p.y, pointer: s.pointerId })
    return true
  }

  move(s: PointerSample, box: NormBox): void {
    const st = this.active.get(s.pointerId)
    if (!st) return
    const p = this.norm(s, box)
    if (!p) return // 拖进黑边:不发,免得被钳到屏幕边缘触发系统手势
    const ref = st.pending ?? st.last
    if (ref.x === p.x && ref.y === p.y) return
    st.pending = p
    const wait = st.lastSentAt + MOVE_MIN_INTERVAL_MS - this.now()
    if (wait <= 0) this.flushMove(s.pointerId)
    else if (!st.timer) {
      st.timer = setTimeout(() => {
        st.timer = null
        this.flushMove(s.pointerId)
      }, wait)
    }
  }

  up(s: PointerSample, box: NormBox): void {
    if (!this.active.has(s.pointerId)) return
    this.finish(s.pointerId, this.norm(s, box))
  }

  /** `pointercancel` / `lostpointercapture`:用最后一个已知点收尾 */
  cancel(pointerId: number): void {
    if (!this.active.has(pointerId)) return
    this.finish(pointerId, null)
  }

  /** 切账号 / 断流前:把所有按着的指针都抬起来 */
  releaseAll(): void {
    for (const id of [...this.active.keys()]) this.finish(id, null)
    if (this.wheelTimer) clearTimeout(this.wheelTimer)
    this.wheelTimer = null
    this.wheelAcc = null
  }

  private flushMove(id: number): void {
    const st = this.active.get(id)
    if (!st || !st.pending) return
    const p = st.pending
    st.pending = null
    st.last = p
    st.lastSentAt = this.now()
    this.send({ type: 'touch', action: 'move', x: p.x, y: p.y, pointer: id })
  }

  private finish(id: number, at: Pt | null): void {
    const st = this.active.get(id)
    if (!st) return
    if (st.timer) clearTimeout(st.timer)
    st.timer = null
    this.flushMove(id)
    this.active.delete(id)
    const p = at ?? st.last
    this.send({ type: 'touch', action: 'up', x: p.x, y: p.y, pointer: id })
  }

  /** 滚轮 → `scroll`;位置取指针所在点,增量在 ≤ 60 Hz 窗口内累加 */
  wheel(s: { clientX: number; clientY: number }, dx: number, dy: number, box: NormBox): void {
    const p = this.norm(s, box)
    if (!p) return
    const acc = this.wheelAcc ?? { x: p.x, y: p.y, dx: 0, dy: 0 }
    acc.x = p.x
    acc.y = p.y
    acc.dx += dx
    acc.dy += dy
    this.wheelAcc = acc
    const wait = this.wheelSentAt + MOVE_MIN_INTERVAL_MS - this.now()
    if (wait <= 0) this.flushWheel()
    else if (!this.wheelTimer) {
      this.wheelTimer = setTimeout(() => {
        this.wheelTimer = null
        this.flushWheel()
      }, wait)
    }
  }

  private flushWheel(): void {
    const a = this.wheelAcc
    this.wheelAcc = null
    if (!a || (a.dx === 0 && a.dy === 0)) return
    this.wheelSentAt = this.now()
    this.send({ type: 'scroll', x: a.x, y: a.y, dx: a.dx, dy: a.dy })
  }
}

/* ───────────── 静态预览第三档:#35 REST 注入兜底 ───────────── */

/** 位移不超过画面的 2% 视为原地 */
export const TAP_SLOP = 0.02
/** 原地按住超过这个时长按长按处理(REST 只有 tap/swipe,长按 = 原地 swipe) */
export const LONG_PRESS_MS = 500

/**
 * 02 #35 `POST /accounts/{id}/stream/input` 只认 `tap|swipe|key|text`,没有 down/move/up,
 * 所以静态预览这一档只能在松手时把整段手势换算成一条 REST 请求(WS 流那一档不走这里)。
 */
export function restGesture(a: Pt, b: Pt, durationMs: number): Record<string, unknown> {
  const moved = Math.hypot(b.x - a.x, b.y - a.y) > TAP_SLOP
  if (!moved && durationMs < LONG_PRESS_MS) return { type: 'tap', x: a.x, y: a.y }
  const end = moved ? b : a
  return {
    type: 'swipe',
    x: a.x,
    y: a.y,
    x2: end.x,
    y2: end.y,
    duration_ms: Math.min(5000, Math.max(50, Math.round(durationMs))),
  }
}
