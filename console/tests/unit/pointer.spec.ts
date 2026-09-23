import { describe, expect, it } from 'vitest'
import { normInContain } from '@/pages/screen/pointer'

/** 竖屏 540×960 画在更宽的盒子里:左右黑边,高度铺满。和画面页截图一致。 */
const box = { left: 0, top: 0, width: 720, height: 700 }
const mediaW = 540
const mediaH = 960

describe('画面点击落在视频上,不落在黑边', () => {
  it('点在画面里的按钮,按视频比例换算,不按整块黑边', () => {
    const scale = Math.min(box.width / mediaW, box.height / mediaH)
    const contentW = mediaW * scale
    const contentLeft = (box.width - contentW) / 2
    // 底部「消息」大约在画面宽度的 1/8 处
    const x = contentLeft + contentW * 0.125
    const y = box.height * 0.92
    const p = normInContain(x, y, box, mediaW, mediaH)
    expect(p).not.toBeNull()
    expect(p!.x).toBeCloseTo(0.125, 2)
    expect(p!.y).toBeCloseTo(0.92, 2)
  })

  it('点在左侧黑边上返回空,不会被钳成屏幕最左边的返回键', () => {
    expect(normInContain(20, box.height * 0.95, box, mediaW, mediaH)).toBeNull()
  })

  it('点在右侧黑边上同样不送出', () => {
    expect(normInContain(700, box.height * 0.5, box, mediaW, mediaH)).toBeNull()
  })

  it('还没有画面尺寸时按盒子本身算,避免首帧前全部丢弃', () => {
    const p = normInContain(360, 350, box, 0, 0)
    expect(p).toEqual({ x: 0.5, y: 0.5 })
  })
})
