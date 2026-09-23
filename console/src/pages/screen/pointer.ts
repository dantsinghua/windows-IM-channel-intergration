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
