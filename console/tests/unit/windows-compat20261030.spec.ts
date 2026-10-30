// @vitest-environment node
// Real config and main IPC, memory filesystem and Electron/OS boundaries only.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { join } from 'node:path'

const h = vi.hoisted(() => ({
  files: new Map<string, string>(), ipc: new Map<string, (...args: any[]) => any>(),
  writes: [] as string[], login: true, loginFailure: false, writeFailure: false, renameFailure: false,
  windows: [] as any[],
  launchItems: [] as Array<{ name: string; path: string; args: string[]; enabled: boolean; scope: string }>,
  loginCalls: [] as Array<Record<string, unknown>>,
  hasLaunchItems: false,
}))
vi.mock('node:fs', () => ({
  existsSync: (p: string) => h.files.has(p),
  readFileSync: (p: string) => { if (!h.files.has(p)) throw new Error('ENOENT'); return h.files.get(p) },
  mkdirSync: vi.fn(),
  writeFileSync: (p: string, value: string) => { if (h.writeFailure) throw new Error('EACCES write'); h.writes.push(p); h.files.set(p, value) },
  renameSync: (from: string, to: string) => {
    if (h.renameFailure) throw new Error('EACCES rename')
    h.writes.push(to); h.files.set(to, h.files.get(from)!); h.files.delete(from)
  },
}))
vi.mock('electron', () => ({
  app: { isPackaged: true, requestSingleInstanceLock: () => true, on: vi.fn(), whenReady: async () => undefined,
    quit: vi.fn(), getAppPath: () => '/fixture/app', getVersion: () => 'fixture',
    getLoginItemSettings: () => ({ openAtLogin: h.login, args: ['--hidden'], ...(h.hasLaunchItems ? { launchItems: h.launchItems } : {}) }),
    setLoginItemSettings: (settings: { openAtLogin: boolean }) => {
      h.loginCalls.push({ ...settings })
      if (h.loginFailure) throw new Error('LOGIN_DENIED')
      h.login = settings.openAtLogin
    } },
  BrowserWindow: class {
    webContents = { on: vi.fn(), once: vi.fn(), send: vi.fn(), setWindowOpenHandler: vi.fn(), reload: vi.fn() }
    loadURL = vi.fn(); loadFile = vi.fn(); on = vi.fn(); maximize = vi.fn()
    constructor() { h.windows.push(this) }
    static getAllWindows() { return h.windows }
  },
  ipcMain: { on: (k: string, fn: any) => h.ipc.set(k, fn), handle: (k: string, fn: any) => h.ipc.set(k, fn) },
  dialog: {}, Notification: {}, shell: {},
}))
vi.mock('../../electron/main/winagent-pipe', () => ({ createPipeClient: () => ({
  describe: () => 'fixture', fetchTokens: async () => ({ agent_token: 'fixture', winagent_token: 'fixture',
    agent_base_url: 'http://127.0.0.1:17600', expires_at: Date.now() + 3600000 }),
}) }))
vi.mock('../../electron/main/netguard', async () => ({
  ...(await import('../../electron/main/origins')), installNetGuard: vi.fn(), openExternalAllowed: vi.fn(),
}))
vi.mock('../../electron/main/tray', () => ({ TrayController: class { install() {} }, brandIcon: () => undefined }))
vi.mock('../../electron/main/files', () => ({ openLogsDir: vi.fn(), pickFile: vi.fn(), saveAs: vi.fn() }))
vi.mock('../../electron/main/window-state', () => ({
  loadWindowState: () => ({ width: 1280, height: 800 }), recordCrash: vi.fn(), saveWindowState: vi.fn(), clearCrashes: vi.fn(),
}))
vi.mock('../../electron/main/local-metrics', () => ({ createLocalMetricsReader: () => vi.fn() }))

const root = '/fixture/ProgramData'
const legacy = join(root, 'QTrade', 'config', 'console.toml')
const current = join(root, 'QTrade', 'config', 'console', 'console.toml')
beforeEach(() => {
  vi.resetModules(); vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  vi.stubEnv('PROGRAMDATA', root)
  vi.stubGlobal('__dirname', '/fixture/dist/electron/main')
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('NETWORK_FORBIDDEN') }))
  h.files.clear(); h.ipc.clear(); h.windows.length = 0; h.writes.length = 0
  h.launchItems.length = 0; h.loginCalls.length = 0
  h.hasLaunchItems = false
  h.login = true; h.loginFailure = false; h.writeFailure = false; h.renameFailure = false
})
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllEnvs(); vi.unstubAllGlobals() })

it('RUN-01 writes only the dedicated config/console directory', async () => {
  const config = await import('../../electron/main/config')
  expect(config.configPath()).toBe(current)
  config.patchConfig({ ui: { theme: 'dark' } })
  expect(h.files.has(current)).toBe(true)
  expect(h.files.has(legacy)).toBe(false)
})

it('RUN-01 reads legacy preferences but never overwrites the legacy file', async () => {
  const old = '[ui]\ntheme = "dark"\n[app]\nauto_launch = false\n'
  h.files.set(legacy, old)
  const config = await import('../../electron/main/config')
  expect(config.readConfig().ui.theme).toBe('dark')
  config.patchConfig({ ui: { nav_collapsed: true } })
  expect(h.files.get(legacy)).toBe(old)
  expect(h.files.has(current)).toBe(true)
  expect(config.readConfig().app.auto_launch).toBe(false)
})

it('RUN-01 current preferences win over stale legacy data', async () => {
  h.files.set(legacy, '[ui]\ntheme = "dark"\n')
  h.files.set(current, '[ui]\ntheme = "light"\n')
  const config = await import('../../electron/main/config')
  expect(config.readConfig().ui.theme).toBe('light')
})

it.each(['write', 'rename'])('RUN-01 persistence %s failure rolls back OS autostart and cache', async (failure) => {
  await import('../../electron/main/index')
  await new Promise<void>(resolve => setImmediate(resolve))
  const config = await import('../../electron/main/config')
  const previous = config.readConfig().app.auto_launch
  if (failure === 'write') h.writeFailure = true
  else h.renameFailure = true
  const handler = h.ipc.get('qt:app.setAutoLaunch')!
  expect(handler).toBeTypeOf('function')
  await expect(Promise.resolve().then(() => handler(undefined, false))).rejects.toThrow('EACCES')
  expect(h.login).toBe(true)
  expect(config.readConfig().app.auto_launch).toBe(previous)
})

it('RUN-01 OS autostart rejection leaves persisted preferences untouched', async () => {
  await import('../../electron/main/index')
  await new Promise<void>(resolve => setImmediate(resolve))
  h.loginFailure = true
  const before = [...h.files.entries()]
  await expect(Promise.resolve().then(() => h.ipc.get('qt:app.setAutoLaunch')!(undefined, false))).rejects.toThrow('LOGIN_DENIED')
  expect(h.files).toEqual(new Map(before))
  expect(h.login).toBe(true)
})

it.each([true, false])('RUN-01 failure restores exact user entry args and enabled=%s without touching machine scope', async (enabled) => {
  h.hasLaunchItems = true
  const owned = { name: 'QTrade Custom User', path: process.execPath, args: ['--profile', 'saved profile', '--minimized'], enabled, scope: 'user' }
  h.launchItems.push(
    { name: 'Machine Entry', path: process.execPath, args: ['--machine'], enabled: true, scope: 'machine' },
    { name: 'Other App', path: '/other/app.exe', args: [], enabled: true, scope: 'user' },
    owned,
  )
  await import('../../electron/main/index')
  await new Promise<void>(resolve => setImmediate(resolve))
  h.renameFailure = true
  await expect(Promise.resolve().then(() => h.ipc.get('qt:app.setAutoLaunch')!(undefined, false))).rejects.toThrow('EACCES')
  expect(h.loginCalls).toHaveLength(2)
  expect(h.loginCalls[0]).toMatchObject({ name: owned.name, openAtLogin: false })
  expect(h.loginCalls[1]).toEqual({ openAtLogin: true, name: owned.name, path: owned.path, args: owned.args, enabled })
  expect(h.loginCalls.some(call => call.name === 'Machine Entry' || call.name === 'Other App')).toBe(false)
})

it('RUN-01 ambiguous user entries reject before autostart or filesystem changes', async () => {
  h.hasLaunchItems = true
  h.launchItems.push(...['QTrade One', 'QTrade Two'].map(name => ({ name, path: process.execPath, args: [], enabled: true, scope: 'user' })))
  await import('../../electron/main/index')
  await new Promise<void>(resolve => setImmediate(resolve))
  const files = [...h.files.entries()]
  await expect(Promise.resolve().then(() => h.ipc.get('qt:app.setAutoLaunch')!(undefined, false))).rejects.toThrow('AUTO_LAUNCH_IDENTITY_AMBIGUOUS')
  expect(h.loginCalls).toEqual([])
  expect([...h.files.entries()]).toEqual(files)
})

it('RUN-01 machine-only startup does not become a duplicate user entry after persistence failure', async () => {
  h.hasLaunchItems = true
  h.login = true
  h.launchItems.push({ name: 'Machine Entry', path: process.execPath, args: ['--machine'], enabled: true, scope: 'machine' })
  await import('../../electron/main/index')
  await new Promise<void>(resolve => setImmediate(resolve))
  h.writeFailure = true
  await expect(Promise.resolve().then(() => h.ipc.get('qt:app.setAutoLaunch')!(undefined, true))).rejects.toThrow('EACCES')
  expect(h.loginCalls).toHaveLength(2)
  expect(h.loginCalls[1]).toMatchObject({ openAtLogin: false })
  expect(h.loginCalls.some(call => call.name === 'Machine Entry')).toBe(false)
})
