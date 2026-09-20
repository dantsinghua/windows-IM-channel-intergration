/** `window.qt` —— preload 经 contextBridge 暴露的白名单(01 §2.2) */

export interface QtBridge {
  app: {
    version(): Promise<{ console: string; electron: string }>
    minimizeToTray(): Promise<void>
    quit(): Promise<void>
    relaunch(): Promise<void>
    setAutoLaunch(on: boolean): Promise<boolean>
    getAutoLaunch(): Promise<boolean>
    openLogsDir(): Promise<void>
    openExternal(url: string): Promise<boolean>
    rssKb(): Promise<number>
  }
  files: {
    saveAs(suggestName: string, mime: string, data: Uint8Array | string): Promise<{ saved: boolean; path?: string }>
    pickFile(accept?: string[]): Promise<{ name: string; bytes: Uint8Array } | null>
  }
  auth: {
    state(): Promise<'ok' | 'winagent_offline' | 'no_token'>
    refresh(): Promise<boolean>
  }
  config: {
    read(): Promise<Record<string, Record<string, unknown>>>
    patch(patch: Record<string, Record<string, unknown>>): Promise<Record<string, Record<string, unknown>>>
  }
  window: { onVisibility(cb: (visible: boolean) => void): () => void }
  notify(title: string, body: string, route?: string): Promise<boolean>
  tray: { update(summary: Record<string, unknown>): Promise<void> }
  wa: { invoke(op: string, args?: Record<string, unknown>): Promise<unknown> }
  router: { onRoute(cb: (route: string) => void): () => void }
}

declare global {
  interface Window {
    qt?: QtBridge
  }
}

export {}
