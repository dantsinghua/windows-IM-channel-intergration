/**
 * 主进程(01 §2.2):只做「必须是 Node/OS 才能做的事」——
 * 单实例锁、托盘、自启、窗口与崩溃恢复、令牌与网络层注入、文件对话框、命名管道。
 * 不解析业务数据、不缓存消息、不存令牌到磁盘。
 */
import { app, BrowserWindow, dialog, ipcMain, Notification, shell } from 'electron'
import { dirname, join } from 'node:path'
import { patchConfig, readConfig } from './config'
import { installNetGuard, openExternalAllowed } from './netguard'
import { TokenHolder } from './token'
import { TrayController } from './tray'
import { openLogsDir, pickFile, saveAs } from './files'
import { loadWindowState, recordCrash, saveWindowState, clearCrashes } from './window-state'
import { resolvePath, WA_WHITELIST } from './wa-whitelist'
import { createLocalMetricsReader } from './local-metrics'

const DEV_URL = process.env.VITE_DEV_SERVER_URL
const tokens = new TokenHolder()
let mainWindow: BrowserWindow | null = null
let tray: TrayController | null = null
let quitting = false
let notifyPaused = false
let pendingRoute: string | null = null

/* ── 单实例锁:第二次启动只把已有窗口 show()+focus(),并把 --route= 转给它 ── */
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', (_e, argv) => {
    const route = parseRoute(argv)
    if (route) mainWindow?.webContents.send('qt:route', route)
    tray?.show()
  })
  void bootstrap()
}

function parseRoute(argv: string[]): string | null {
  const hit = argv.find((a) => a.startsWith('--route='))
  return hit ? hit.slice('--route='.length) : null
}

async function bootstrap(): Promise<void> {
  await app.whenReady()

  await tokens.refresh()
  installNetGuard(tokens)
  registerIpc()

  const cfg = readConfig()
  const startHidden = process.argv.includes('--hidden') && cfg.app?.start_hidden !== false
  pendingRoute = parseRoute(process.argv)

  tray = new TrayController(
    () => mainWindow,
    () => {
      quitting = true
      app.quit()
    },
    (paused) => {
      notifyPaused = paused
    },
  )
  tray.install()

  createWindow(!startHidden)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow(true)
  })
  app.on('before-quit', () => {
    quitting = true
  })
  // 关窗口不退出(基线 §2:控制台崩了/关了都不影响 Agent 与 WinAgent)
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin' && quitting) app.quit()
  })
}

function createWindow(show: boolean): void {
  const st = loadWindowState()
  const win = new BrowserWindow({
    x: st.x,
    y: st.y,
    width: st.width,
    height: st.height,
    minWidth: 1100,
    minHeight: 720,
    show,
    title: 'QTrade 控制台',
    autoHideMenuBar: true,
    backgroundColor: '#F5F6F8',
    webPreferences: {
      preload: join(__dirname, '..', 'preload', 'index.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
    },
  })
  mainWindow = win
  if (st.maximized) win.maximize()

  // 拒绝一切导航与新窗口;外链只放行本机 NapCat WebUI
  win.webContents.setWindowOpenHandler(({ url }) => {
    void openExternalAllowed(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (e, url) => {
    if (!DEV_URL || !url.startsWith(DEV_URL)) e.preventDefault()
  })

  win.on('close', (e) => {
    const cfg = readConfig()
    if (!quitting && cfg.app?.minimize_to_tray_on_close !== false) {
      e.preventDefault()
      win.hide()
      return
    }
    persistWindow(win)
  })
  win.on('resize', () => persistWindow(win))
  win.on('move', () => persistWindow(win))
  win.on('show', () => {
    if (pendingRoute) {
      win.webContents.send('qt:route', pendingRoute)
      pendingRoute = null
    }
  })
  win.webContents.on('page-title-updated', (e) => e.preventDefault())

  // 崩溃恢复(01 §2.4):连续 3 次/5 分钟 → 停安全页 P-ENV(不开画面流)
  win.webContents.on('render-process-gone', () => {
    const cfg = readConfig()
    const threshold = Number(cfg.app?.crash_safe_threshold ?? 3)
    const { safeMode, route } = recordCrash(pendingRoute ?? '/dash', threshold)
    pendingRoute = route
    if (safeMode) {
      dialog.showErrorBox('控制台已多次崩溃', '已停在环境页(画面流关闭),建议在环境页导出诊断包。')
    }
    createWindow(true)
  })

  loadRenderer(win)
  win.webContents.once('did-finish-load', () => clearCrashes())
}

function persistWindow(win: BrowserWindow): void {
  if (win.isDestroyed()) return
  const b = win.getBounds()
  saveWindowState({ x: b.x, y: b.y, width: b.width, height: b.height, maximized: win.isMaximized() })
}

function loadRenderer(win: BrowserWindow): void {
  if (DEV_URL) void win.loadURL(DEV_URL)
  else void win.loadFile(join(__dirname, '..', '..', 'renderer', 'index.html'))
}

function registerIpc(): void {
  // Fixed local scope: packaged executable folder, or the development application folder.
  const readLocalMetrics = createLocalMetricsReader(app.isPackaged ? dirname(process.execPath) : app.getAppPath())
  ipcMain.handle('qt:app.localMetrics', () => readLocalMetrics())
  ipcMain.handle('qt:app.version', () => ({ console: app.getVersion(), electron: process.versions.electron }))
  ipcMain.handle('qt:app.minimizeToTray', () => {
    mainWindow?.hide()
  })
  ipcMain.handle('qt:app.quit', () => {
    quitting = true
    app.quit()
  })
  ipcMain.handle('qt:app.relaunch', () => {
    app.relaunch()
    quitting = true
    app.quit()
  })
  ipcMain.handle('qt:app.setAutoLaunch', (_e, on: boolean) => {
    app.setLoginItemSettings({ openAtLogin: !!on, args: ['--hidden'] })
    patchConfig({ app: { auto_launch: !!on } })
    return true
  })
  ipcMain.handle('qt:app.getAutoLaunch', () => app.getLoginItemSettings().openAtLogin)
  ipcMain.handle('qt:app.openLogsDir', () => openLogsDir())
  ipcMain.handle('qt:app.openExternal', (_e, url: string) => openExternalAllowed(url))
  ipcMain.handle('qt:app.rss', async () => {
    const info = await process.getProcessMemoryInfo()
    return Math.round((info.private ?? 0) / 1024)
  })

  ipcMain.handle('qt:files.saveAs', (_e, name: string, _mime: string, data: Uint8Array | string) =>
    saveAs(mainWindow, name, data))
  ipcMain.handle('qt:files.pickFile', (_e, accept?: string[]) => pickFile(mainWindow, accept))

  ipcMain.handle('qt:auth.state', () => tokens.authState)
  ipcMain.handle('qt:auth.refresh', () => tokens.refresh())

  ipcMain.handle('qt:config.read', () => readConfig())
  ipcMain.handle('qt:config.patch', (_e, patch: Record<string, Record<string, unknown>>) => patchConfig(patch))

  ipcMain.handle('qt:notify', (_e, title: string, body: string, route?: string) => {
    const cfg = readConfig()
    if (notifyPaused || cfg.notify?.enabled === false) return false
    const n = new Notification({ title, body })
    if (route) n.on('click', () => {
      tray?.show()
      mainWindow?.webContents.send('qt:route', route)
    })
    n.show()
    return true
  })

  ipcMain.handle('qt:tray.update', (_e, summary: Record<string, unknown>) => {
    tray?.update(summary as never)
  })

  // WinAgent 能力唯一入口:主进程持令牌代调 17610(渲染进程直连已被阻断)
  ipcMain.handle('qt:wa.invoke', async (_e, op: string, args: Record<string, unknown> = {}) => {
    const def = WA_WHITELIST[op]
    if (!def) throw new Error(`op 不在白名单:${op}`)
    if (def.nativeConfirm) {
      const r = await dialog.showMessageBox(mainWindow ?? undefined!, {
        type: 'warning',
        buttons: ['取消', '确认'],
        defaultId: 0,
        cancelId: 0,
        title: def.nativeConfirm.title,
        message: def.nativeConfirm.message,
      })
      if (r.response !== 1) throw new Error('用户取消')
    }
    const { path, body } = resolvePath(def, args)
    const token = tokens.winagentToken
    const base = String(readConfig().endpoint?.winagent ?? 'http://127.0.0.1:17610')
    const res = await fetch(`${base}${path}`, {
      method: def.method,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    const text = await res.text()
    const parsed = text ? JSON.parse(text) : {}
    if (!res.ok) throw new Error(parsed?.error?.message ?? `WinAgent ${res.status}`)
    return parsed
  })
}

export { shell }
