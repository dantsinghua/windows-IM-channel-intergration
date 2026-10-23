/**
 * 主进程(01 §2.2):只做「必须是 Node/OS 才能做的事」——
 * 单实例锁、托盘、自启、窗口与崩溃恢复、令牌与网络层注入、文件对话框、命名管道。
 * 不解析业务数据、不缓存消息、不存令牌到磁盘。
 */
import { app, BrowserWindow, dialog, ipcMain, Notification, shell } from 'electron'
import { dirname, join } from 'node:path'
import { patchConfig, readConfig } from './config'
import { DEFAULT_AGENT_ORIGIN, DEFAULT_WINAGENT_ORIGIN, installNetGuard, normalizeLocalOrigin, openExternalAllowed } from './netguard'
import { TokenHolder } from './token'
import { TrayController, brandIcon } from './tray'
import { openLogsDir, pickFile, saveAs } from './files'
import { loadWindowState, recordCrash, saveWindowState, clearCrashes } from './window-state'
import { resolvePath, WA_WHITELIST } from './wa-whitelist'
import { createLocalMetricsReader } from './local-metrics'
import { createPipeClient } from './winagent-pipe'
import { createWinAgentRecovery, registerRecoveryIpc, runRecoveryPowerShell } from './winagent-recovery'

const DEV_URL = app.isPackaged ? undefined : process.env.VITE_DEV_SERVER_URL
const tokens = new TokenHolder(createPipeClient({ isPackaged: app.isPackaged }), Date.now, setTimeout, clearTimeout, {
  refreshIntervalMs: 30_000,
  onRefreshed: syncAgentOrigin,
})
let mainWindow: BrowserWindow | null = null
let tray: TrayController | null = null
let quitting = false
let notifyPaused = false
let pendingRoute: string | null = null
/** 正式 Agent 源只由认证管道发现;开发形态可配置回环地址。所有网络入口共用当前源。 */
let agentOrigin = DEFAULT_AGENT_ORIGIN
let winagentOrigin = DEFAULT_WINAGENT_ORIGIN
const localService = createWinAgentRecovery({
  platform: process.platform,
  run: runRecoveryPowerShell,
  refresh: () => tokens.refresh(),
  delay: () => new Promise(resolve => setTimeout(resolve, 1000)),
  probeWinAgent: async () => {
    if (!tokens.winagentToken) return null
    try {
      const response = await fetch(`${winagentOrigin}/wa/v1/health`, { headers: { Authorization: `Bearer ${tokens.winagentToken}` }, signal: AbortSignal.timeout(5000) })
      if (!response.ok) return null
      const body = await response.json()
      const health = body.data ?? body
      return typeof health.user_agent === 'boolean' ? health.user_agent : null
    } catch { return null }
  },
  probeAgent: async () => {
    try {
      const response = await fetch(`${agentOrigin}/api/v1/system/health`, { signal: AbortSignal.timeout(5000) })
      return response.ok ? true : null
    } catch { return false }
  },
})

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

function syncAgentOrigin(): void {
  if (!app.isPackaged || !tokens.agentBaseUrl || tokens.agentBaseUrl === agentOrigin) return
  agentOrigin = tokens.agentBaseUrl
  if (mainWindow) {
    installNetGuard(tokens, undefined, { agentOrigin, winagentOrigin })
    // 同次重载更新 preload、HTTP、事件与画面;旧页面不会携新令牌继续连旧地址。
    mainWindow.webContents.reload()
  }
}

async function bootstrap(): Promise<void> {
  await app.whenReady()

  const cfg = readConfig()
  if (!app.isPackaged) {
    agentOrigin = normalizeLocalOrigin(cfg.endpoint?.agent, DEFAULT_AGENT_ORIGIN)
    winagentOrigin = normalizeLocalOrigin(cfg.endpoint?.winagent, DEFAULT_WINAGENT_ORIGIN)
  }

  await tokens.refresh()
  installNetGuard(tokens, undefined, { agentOrigin, winagentOrigin, devUrl: DEV_URL })
  registerIpc()

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
    () => createWindow(true),
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
    title: '机器人采集终端',
    icon: brandIcon(),                             // 任务栏 / 窗口左上角与托盘同一枚南银标
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
  // 窗口真被销毁(minimize_to_tray_on_close=false / 退出)后不再留死引用;托盘与 IPC 都以 null 判「要重建」
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
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
      dialog.showErrorBox('机器人采集终端已多次崩溃', '已停在环境页(画面流关闭),建议在环境页导出诊断包。')
    }
    createWindow(true)
    if (!win.isDestroyed()) win.destroy()          // 渲染进程已没了的旧壳不留着
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
  registerRecoveryIpc(ipcMain, localService, (candidate) => {
    const event = candidate as Electron.IpcMainInvokeEvent
    return event.sender === mainWindow?.webContents && event.senderFrame === event.sender.mainFrame
  })
  // 固定只读 IPC:不接受来源或令牌参数,同步返回同份已验证源供每次 preload 加载。
  ipcMain.on('qt:endpoint.agent', (event) => { event.returnValue = agentOrigin })
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

  ipcMain.handle('qt:auth.state', () => tokens.ensureFresh())   // 到期先续签再回答,不让渲染进程先看到 no_token
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
    const res = await fetch(`${winagentOrigin}${path}`, {
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
