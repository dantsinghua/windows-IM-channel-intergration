/**
 * 托盘(01 §2.4):常驻;左键切换窗口;右键菜单;角标 = 未读 firing 告警数(按 (code,subject) 去重)。
 * 关窗口 = 最小化到托盘,**不退出**;退出必须走托盘「退出」。
 */
import { app, Menu, nativeImage, Tray, type BrowserWindow } from 'electron'

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

  constructor(
    private readonly getWindow: () => BrowserWindow | null,
    private readonly onQuit: () => void,
    private readonly onToggleNotify: (paused: boolean) => void,
  ) {}

  install(): void {
    if (this.tray) return
    this.tray = new Tray(nativeImage.createEmpty())
    this.tray.setToolTip('QTrade 控制台')
    this.tray.on('click', () => this.toggleWindow())
    this.render()
  }

  private toggleWindow(): void {
    const win = this.getWindow()
    if (!win) return
    if (win.isVisible() && !win.isMinimized()) win.hide()
    else this.show()
  }

  show(): void {
    const win = this.getWindow()
    if (!win) return
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
