// @vitest-environment node
/** Real main-module bootstrap with OS boundaries replaced; no Electron process starts. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { join } from 'node:path'

const h = vi.hoisted(() => ({
  fetchTokens: vi.fn(), createPipeClient: vi.fn(), guards: vi.fn(),
  windows: [] as Array<{ options: any; webContents: any; loadURL: any; loadFile: any }>,
  ipc: new Map<string, (...args: any[]) => unknown>(),
}))

vi.mock('electron', () => ({
  app: { isPackaged: true, requestSingleInstanceLock: () => true, on: vi.fn(), whenReady: async () => undefined,
    quit: vi.fn(), getAppPath: () => '/fixture/app', getVersion: () => 'fixture' },
  BrowserWindow: class {
    options: any
    webContents = { on: vi.fn(), once: vi.fn(), send: vi.fn(), setWindowOpenHandler: vi.fn(), reload: vi.fn() }
    loadURL = vi.fn(); loadFile = vi.fn(); on = vi.fn(); maximize = vi.fn()
    constructor(options: any) { this.options = options; h.windows.push(this) }
    static getAllWindows() { return h.windows }
  },
  dialog: {}, Notification: {}, shell: {},
  ipcMain: {
    on: (name: string, fn: (...args: any[]) => unknown) => h.ipc.set(name, fn),
    handle: (name: string, fn: (...args: any[]) => unknown) => h.ipc.set(name, fn),
  },
}))
vi.mock('../../electron/main/winagent-pipe', () => ({
  createPipeClient: (options: unknown) => { h.createPipeClient(options); return { fetchTokens: h.fetchTokens, describe: () => 'fake OS pipe' } },
}))
vi.mock('../../electron/main/config', () => ({
  readConfig: () => ({ endpoint: { agent: 'http://127.0.0.1:19999' }, app: {}, ui: {} }), patchConfig: vi.fn(),
}))
vi.mock('../../electron/main/netguard', async () => ({
  ...(await import('../../electron/main/origins')), installNetGuard: h.guards, openExternalAllowed: vi.fn(),
}))
vi.mock('../../electron/main/tray', () => ({ TrayController: class { install() {} }, brandIcon: () => undefined }))
vi.mock('../../electron/main/files', () => ({ openLogsDir: vi.fn(), pickFile: vi.fn(), saveAs: vi.fn() }))
vi.mock('../../electron/main/window-state', () => ({
  loadWindowState: () => ({ width: 1280, height: 800 }), recordCrash: vi.fn(), saveWindowState: vi.fn(), clearCrashes: vi.fn(),
}))
vi.mock('../../electron/main/local-metrics', () => ({ createLocalMetricsReader: () => vi.fn() }))

beforeEach(() => {
  vi.resetModules()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  vi.stubGlobal('__dirname', '/fixture/dist/electron/main')
  vi.stubEnv('VITE_DEV_SERVER_URL', 'http://127.0.0.1:54321')
  vi.stubEnv('QT_DEV_TOKEN', 'UNTRUSTED_INHERITED_VALUE')
  h.windows.splice(0); h.ipc.clear(); vi.clearAllMocks()
})
afterEach(() => {
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllEnvs(); vi.unstubAllGlobals()
})

it('正式主进程只加载内置页面并使用认证管道来源，源改变只重载一次', async () => {
  const bundle = (ip: string) => ({ agent_token: 'fixture', winagent_token: 'fixture',
    agent_base_url: `http://${ip}:17600`, expires_at: Date.now() + 3600000 })
  h.fetchTokens.mockResolvedValueOnce(bundle('172.23.1.2')).mockResolvedValue(bundle('172.23.1.3'))
  await import('../../electron/main/index')
  await flushPromises()
  expect(h.createPipeClient).toHaveBeenCalledWith({ isPackaged: true })
  expect(h.windows).toHaveLength(1)
  const win = h.windows[0]
  expect(win.loadURL).not.toHaveBeenCalled()
  expect(win.loadFile).toHaveBeenCalledWith(join(process.cwd(), 'renderer', 'index.html'))
  expect(h.guards.mock.calls.at(-1)?.[2]).toMatchObject({ agentOrigin: 'http://172.23.1.2:17600' })
  expect(h.guards.mock.calls.at(-1)?.[2].devUrl).toBeUndefined()
  const event: { returnValue?: string } = {}
  h.ipc.get('qt:endpoint.agent')!(event)
  expect(event.returnValue).toBe('http://172.23.1.2:17600')
  await vi.advanceTimersByTimeAsync(30000)
  expect(win.webContents.reload).toHaveBeenCalledTimes(1)
  h.ipc.get('qt:endpoint.agent')!(event)
  expect(event.returnValue).toBe('http://172.23.1.3:17600')
  expect(h.guards.mock.calls.at(-1)?.[2].agentOrigin).toBe(event.returnValue)
  await vi.advanceTimersByTimeAsync(30000)
  expect(win.webContents.reload).toHaveBeenCalledTimes(1)
})

it('冷启动的本地恢复凭据可调用 wsl.start，guest 发现后才重载受管端点', async () => {
  const localOnly = { agent_token: null, agent_base_url: null, winagent_token: 'LOCAL_ONLY', expires_at: Date.now() + 3600000 }
  h.fetchTokens.mockResolvedValueOnce(localOnly).mockResolvedValue({
    ...localOnly, agent_token: 'AGENT_RECOVERED', agent_base_url: 'http://172.26.1.2:17600',
  })
  const fetch = vi.fn().mockResolvedValue(new Response('{}'))
  vi.stubGlobal('fetch', fetch)
  await import('../../electron/main/index')
  await flushPromises()
  expect(h.windows).toHaveLength(1)
  expect(await h.ipc.get('qt:auth.state')!()).toBe('ok')
  const event: { returnValue?: string } = {}
  h.ipc.get('qt:endpoint.agent')!(event)
  expect(event.returnValue).toBe('http://127.0.0.1:17600')
  await h.ipc.get('qt:wa.invoke')!(undefined, 'wsl.start', {})
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:17610/wa/v1/wsl/start', expect.objectContaining({
    headers: expect.objectContaining({ Authorization: 'Bearer LOCAL_ONLY' }),
  }))
  expect(h.windows[0].webContents.reload).not.toHaveBeenCalled()
  await h.ipc.get('qt:auth.refresh')!()
  h.ipc.get('qt:endpoint.agent')!(event)
  expect(event.returnValue).toBe('http://172.26.1.2:17600')
  expect(h.guards.mock.calls.at(-1)?.[2].agentOrigin).toBe(event.returnValue)
  expect(h.windows[0].webContents.reload).toHaveBeenCalledTimes(1)
})
