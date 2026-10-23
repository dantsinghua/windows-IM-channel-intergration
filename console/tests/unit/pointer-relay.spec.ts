/**
 * 画布指针 → #34 控制帧序列(02 #34 `touch{action,x,y,pointer}` / `scroll{x,y,dx,dy}`):
 * 点击、滑动、长按、多点都原样透传,前端不合成 tap / longpress;
 * move 只在按住时发且 ≤ 60 Hz;首帧前不发;黑边不发;cancel 也收尾。
 * 另:静态预览档的 #35 REST 手势换算。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  LONG_PRESS_MS, MOVE_MIN_INTERVAL_MS, PointerRelay, restGesture, type ScrollFrame, type TouchFrame,
} from '@/pages/screen/pointer'

/** 盒子 720×1280,视频 720×1280:无黑边,clientX/720 就是 x */
const BOX = { left: 0, top: 0, width: 720, height: 1280 }
const at = (id: number, fx: number, fy: number) => ({ pointerId: id, clientX: fx * 720, clientY: fy * 1280 })

let frames: (TouchFrame | ScrollFrame)[]
let relay: PointerRelay

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(0)
  frames = []
  relay = new PointerRelay((f) => frames.push(f))
  relay.setMedia(720, 1280)
})

afterEach(() => {
  vi.useRealTimers()
})

const actions = () => frames.map((f) => (f.type === 'touch' ? `${f.action}#${f.pointer}` : 'scroll'))

describe('点击 / 长按 / 滑动', () => {
  it('点击 = down + up,不多不少', () => {
    relay.down(at(1, 0.5, 0.5), BOX)
    vi.advanceTimersByTime(80)
    relay.up(at(1, 0.5, 0.5), BOX)
    expect(frames).toEqual([
      { type: 'touch', action: 'down', x: 0.5, y: 0.5, pointer: 1 },
      { type: 'touch', action: 'up', x: 0.5, y: 0.5, pointer: 1 },
    ])
  })

  it('长按 = down …(2 秒什么都不发)… up;原地的 move 也不发', () => {
    relay.down(at(1, 0.3, 0.7), BOX)
    for (let t = 0; t < 2000; t += 16) {
      vi.advanceTimersByTime(16)
      relay.move(at(1, 0.3, 0.7), BOX) // 鼠标没动但浏览器照样给 pointermove
    }
    expect(actions()).toEqual(['down#1'])
    relay.up(at(1, 0.3, 0.7), BOX)
    expect(actions()).toEqual(['down#1', 'up#1'])
    expect(frames.at(-1)).toMatchObject({ x: 0.3, y: 0.7 })
  })

  it('没按下时的 move 不发(鼠标悬停划过画面)', () => {
    relay.move(at(1, 0.2, 0.2), BOX)
    relay.move(at(1, 0.4, 0.4), BOX)
    expect(frames).toEqual([])
  })

  it('滑动:move 合并到 ≤ 60 Hz,终点不丢,up 前补发最后位置', () => {
    relay.down(at(1, 0.5, 0.9), BOX)
    // 1 ms 一个 move,共 100 个
    for (let i = 1; i <= 100; i++) {
      vi.advanceTimersByTime(1)
      relay.move(at(1, 0.5, 0.9 - i * 0.005), BOX)
    }
    const moves = frames.filter((f) => f.type === 'touch' && f.action === 'move')
    expect(moves.length).toBeGreaterThan(3)
    expect(moves.length).toBeLessThanOrEqual(Math.ceil(100 / MOVE_MIN_INTERVAL_MS) + 1)
    relay.up(at(1, 0.5, 0.4), BOX)
    const last2 = frames.slice(-2) as TouchFrame[]
    expect(last2[0].action).toBe('move')
    expect(last2[0].y).toBeCloseTo(0.4, 5)
    expect(last2[1]).toMatchObject({ action: 'up', pointer: 1 })
    expect(last2[1].y).toBeCloseTo(0.4, 5)
  })

  it('节流窗口里停住不动 ⇒ 定时器尾随补发最后位置', () => {
    relay.down(at(1, 0.5, 0.5), BOX)
    vi.advanceTimersByTime(20)
    relay.move(at(1, 0.5, 0.45), BOX) // 立即发
    vi.advanceTimersByTime(2)
    relay.move(at(1, 0.5, 0.40), BOX) // 窗口内,挂起
    expect(frames.filter((f) => f.type === 'touch' && f.action === 'move')).toHaveLength(1)
    vi.advanceTimersByTime(20)
    const moves = frames.filter((f) => f.type === 'touch' && f.action === 'move') as TouchFrame[]
    expect(moves).toHaveLength(2)
    expect(moves[1].y).toBeCloseTo(0.4, 5)
  })
})

describe('多点、取消、黑边、首帧前', () => {
  it('两根手指各自 down/move/up,pointerId 原样透传', () => {
    relay.down(at(3, 0.3, 0.5), BOX)
    relay.down(at(4, 0.7, 0.5), BOX)
    vi.advanceTimersByTime(20)
    relay.move(at(3, 0.2, 0.5), BOX)
    relay.move(at(4, 0.8, 0.5), BOX)
    relay.up(at(3, 0.2, 0.5), BOX)
    relay.up(at(4, 0.8, 0.5), BOX)
    expect(actions()).toEqual(['down#3', 'down#4', 'move#3', 'move#4', 'up#3', 'up#4'])
    expect(relay.activeCount).toBe(0)
  })

  it('pointercancel ⇒ 用最后已知点发 up,设备侧不会一直按着', () => {
    relay.down(at(1, 0.5, 0.5), BOX)
    vi.advanceTimersByTime(20)
    relay.move(at(1, 0.6, 0.6), BOX)
    relay.cancel(1)
    expect(frames.at(-1)).toMatchObject({ type: 'touch', action: 'up', pointer: 1 })
    expect((frames.at(-1) as TouchFrame).x).toBeCloseTo(0.6, 5)
    relay.cancel(1) // 重复 cancel 不再发
    expect(actions()).toEqual(['down#1', 'move#1', 'up#1'])
  })

  it('同一根指针丢了 up 又按下 ⇒ 先补 up 再 down', () => {
    relay.down(at(1, 0.1, 0.1), BOX)
    relay.down(at(1, 0.9, 0.9), BOX)
    expect(actions()).toEqual(['down#1', 'up#1', 'down#1'])
  })

  it('releaseAll 抬起所有按着的指针', () => {
    relay.down(at(1, 0.1, 0.1), BOX)
    relay.down(at(2, 0.2, 0.2), BOX)
    relay.releaseAll()
    expect(actions()).toEqual(['down#1', 'down#2', 'up#1', 'up#2'])
  })

  it('首帧前(不知道视频宽高)一律不发,也不按 canvas 默认 300×150 算', () => {
    const r = new PointerRelay((f) => frames.push(f))
    expect(r.down(at(1, 0.5, 0.5), BOX)).toBe(false)
    r.wheel(at(1, 0.5, 0.5), 0, 100, BOX)
    expect(frames).toEqual([])
    r.setMedia(720, 1280)
    expect(r.down(at(1, 0.5, 0.5), BOX)).toBe(true)
  })

  it('竖屏视频画在宽盒子里:按在黑边上不发;拖进黑边的 move 不发;在黑边松手用最后画面内的点', () => {
    const wide = { left: 0, top: 0, width: 1280, height: 1280 } // 视频 720×1280 ⇒ 左右各 280 黑边
    relay.down({ pointerId: 1, clientX: 100, clientY: 600 }, wide)
    expect(frames).toEqual([])
    relay.down({ pointerId: 1, clientX: 640, clientY: 640 }, wide)
    vi.advanceTimersByTime(20)
    relay.move({ pointerId: 1, clientX: 1200, clientY: 640 }, wide)
    relay.up({ pointerId: 1, clientX: 1200, clientY: 640 }, wide)
    expect(actions()).toEqual(['down#1', 'up#1'])
    expect(frames[1]).toMatchObject({ x: 0.5, y: 0.5 })
  })
})

describe('滚轮 → scroll', () => {
  it('位置取指针所在点;窗口内的增量累加成一帧', () => {
    vi.advanceTimersByTime(100)
    relay.wheel(at(0, 0.25, 0.75), 0, 40, BOX) // 立即发
    relay.wheel(at(0, 0.25, 0.75), 0, 30, BOX)
    relay.wheel(at(0, 0.25, 0.75), 5, 30, BOX)
    expect(frames).toEqual([{ type: 'scroll', x: 0.25, y: 0.75, dx: 0, dy: 40 }])
    vi.advanceTimersByTime(20)
    expect(frames[1]).toEqual({ type: 'scroll', x: 0.25, y: 0.75, dx: 5, dy: 60 })
  })
})

describe('静态预览:#35 REST 手势换算', () => {
  it('原地短按 ⇒ tap', () => {
    expect(restGesture({ x: 0.5, y: 0.5 }, { x: 0.505, y: 0.5 }, 120)).toEqual({ type: 'tap', x: 0.5, y: 0.5 })
  })

  it('原地长按 ⇒ 原地 swipe(带时长)', () => {
    expect(restGesture({ x: 0.5, y: 0.5 }, { x: 0.5, y: 0.5 }, LONG_PRESS_MS + 300)).toEqual({
      type: 'swipe', x: 0.5, y: 0.5, x2: 0.5, y2: 0.5, duration_ms: LONG_PRESS_MS + 300,
    })
  })

  it('拖动 ⇒ swipe 起终点', () => {
    expect(restGesture({ x: 0.5, y: 0.9 }, { x: 0.5, y: 0.3 }, 250)).toMatchObject({
      type: 'swipe', x: 0.5, y: 0.9, x2: 0.5, y2: 0.3, duration_ms: 250,
    })
  })
})
