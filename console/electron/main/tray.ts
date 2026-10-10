/**
 * 托盘(01 §2.4):常驻;左键切换窗口;右键菜单;角标 = 未读 firing 告警数(按 (code,subject) 去重)。
 * 关窗口 = 最小化到托盘,**不退出**;退出必须走托盘「退出」。
 */
import { app, Menu, nativeImage, Tray, type BrowserWindow } from 'electron'

import { BRAND_ICON_PNG_DATA_URL } from './brand-icon'

/** 托盘 / 窗口共用的品牌图标(南京银行花形标);托盘按 16px 显示 */
export function brandIcon(size?: number): Electron.NativeImage {
  const img = nativeImage.createFromDataURL(BRAND_ICON_PNG_DATA_URL)
  return size ? img.resize({ width: size, height: size, quality: 'best' }) : img
}

export interface TraySummary {
  /** 每通道在线数 */
  online: Record<string, number>
  unreadAlerts: number
  notifyPaused: boolean
}

const CHANNEL_LABEL: Record<string, string> = { qidian: '企点', qq: 'QQ', wechat: '微信' }

export class TrayController {
  private tray: Tray | null = null
  private summary: TraySummary = { online: {}, unreadAlerts: 0, notifyPaused: false }

  /**
   * @param getWindow     取当前主窗口;**可能返回 null 或已销毁的窗口**
   * @param onQuit        托盘「退出」
   * @param onToggleNotify 暂停/恢复通知
   * @param createWindow  窗口不存在时重建(2026-10-10 真机:`minimize_to_tray_on_close=false` 关窗后窗口已销毁,
   *                      点托盘对死对象调 `isVisible()` ⇒ 主进程 `Object has been destroyed` 弹窗,再也打不开)
   */
  constructor(
    private readonly getWindow: () => BrowserWindow | null,
    private readonly onQuit: () => void,
    private readonly onToggleNotify: (paused: boolean) => void,
    private readonly createWindow: () => void = () => {},
  ) {}

  install(): void {
    if (this.tray) return
    this.tray = new Tray(brandIcon(16))           // 此前是 nativeImage.createEmpty():托盘里一个空白方块
    this.tray.setToolTip('QTrade 控制台')
    this.tray.on('click', () => this.toggleWindow())
    this.render()
  }

  /** 活着的主窗口;null 表示要重建 */
  private liveWindow(): BrowserWindow | null {
    const win = this.getWindow()
    return win && !win.isDestroyed() ? win : null
  }

  private toggleWindow(): void {
    const win = this.liveWindow()
    if (!win) {
      this.createWindow()
      return
    }
    if (win.isVisible() && !win.isMinimized()) win.hide()
    else this.show()
  }

  show(): void {
    const win = this.liveWindow()
    if (!win) {
      this.createWindow()
      return
    }
    if (win.isMinimized()) win.restore()
    win.show()
    win.focus()
  }

  update(summary: Partial<TraySummary>): void {
    this.summary = { ...this.summary, ...summary }
    this.render()
  }

  private render(): void {
    if (!this.tray) return
    const lines = Object.entries(this.summary.online)
      .map(([ch, n]) => `${CHANNEL_LABEL[ch] ?? ch} ${n} 在线`)
      .join(' · ')
    const menu = Menu.buildFromTemplate([
      { label: '打开控制台', click: () => this.show() },
      { label: lines || '暂无在线账号', enabled: false },
      { type: 'separator' },
      {
        label: this.summary.notifyPaused ? '恢复通知' : '暂停通知',
        click: () => {
          this.summary.notifyPaused = !this.summary.notifyPaused
          this.onToggleNotify(this.summary.notifyPaused)
          this.render()
        },
      },
      { type: 'separator' },
      { label: '退出', click: () => this.onQuit() },
    ])
    this.tray.setContextMenu(menu)
    const badge = this.summary.unreadAlerts > 0 ? `(${this.summary.unreadAlerts}) ` : ''
    this.tray.setToolTip(`${badge}QTrade 控制台${lines ? ` — ${lines}` : ''}`)
    if (process.platform === 'win32') {
      app.setBadgeCount?.(this.summary.unreadAlerts)
    }
  }

  destroy(): void {
    this.tray?.destroy()
    this.tray = null
  }
}
