/**
 * 预加载(01 §2.2):`contextBridge` 只暴露白名单函数。
 * **不暴露 `ipcRenderer` 本体、不暴露任何 Node API**;令牌永不出现在这里。
 */
import { contextBridge, ipcRenderer } from 'electron'

const qt = {
  app: {
    version: () => ipcRenderer.invoke('qt:app.version'),
    minimizeToTray: () => ipcRenderer.invoke('qt:app.minimizeToTray'),
    quit: () => ipcRenderer.invoke('qt:app.quit'),
    relaunch: () => ipcRenderer.invoke('qt:app.relaunch'),
    setAutoLaunch: (on: boolean) => ipcRenderer.invoke('qt:app.setAutoLaunch', on),
    getAutoLaunch: () => ipcRenderer.invoke('qt:app.getAutoLaunch'),
    openLogsDir: () => ipcRenderer.invoke('qt:app.openLogsDir'),
    /** 只允许 http://127.0.0.1:163NN/(NapCat WebUI) */
    openExternal: (url: string) => ipcRenderer.invoke('qt:app.openExternal', url),
    /** 控制台自身 RSS(P-RES 右栏,由主进程 process.getProcessMemoryInfo 上报) */
    rssKb: () => ipcRenderer.invoke('qt:app.rss'),
    /** 客户端目录文件大小与宿主物理内存；不接受路径，不返回路径。 */
    localMetrics: () => ipcRenderer.invoke('qt:app.localMetrics'),
  },
  files: {
    saveAs: (suggestName: string, mime: string, data: Uint8Array | string) =>
      ipcRenderer.invoke('qt:files.saveAs', suggestName, mime, data),
    /** 返回 {name, bytes};**不返回路径** */
    pickFile: (accept?: string[]) => ipcRenderer.invoke('qt:files.pickFile', accept),
  },
  auth: {
    /** 'ok' | 'winagent_offline' | 'no_token' */
    state: () => ipcRenderer.invoke('qt:auth.state'),
    refresh: () => ipcRenderer.invoke('qt:auth.refresh'),
  },
  config: {
    read: () => ipcRenderer.invoke('qt:config.read'),
    patch: (patch: Record<string, Record<string, unknown>>) => ipcRenderer.invoke('qt:config.patch', patch),
  },
  window: {
    /** 最小化/恢复 → 画面流降级(§2.7.4) */
    onVisibility: (cb: (visible: boolean) => void) => {
      const h = (_e: unknown, v: boolean) => cb(v)
      ipcRenderer.on('qt:visibility', h)
      return () => ipcRenderer.off('qt:visibility', h)
    },
  },
  /** 系统通知,点击跳路由 */
  notify: (title: string, body: string, route?: string) => ipcRenderer.invoke('qt:notify', title, body, route),
  tray: {
    update: (summary: Record<string, unknown>) => ipcRenderer.invoke('qt:tray.update', summary),
  },
  /** WinAgent 能力唯一入口;op 限 §2.5 白名单,主进程持令牌代调 */
  wa: {
    invoke: (op: string, args?: Record<string, unknown>) => ipcRenderer.invoke('qt:wa.invoke', op, args ?? {}),
  },
  router: {
    onRoute: (cb: (route: string) => void) => {
      const h = (_e: unknown, r: string) => cb(r)
      ipcRenderer.on('qt:route', h)
      return () => ipcRenderer.off('qt:route', h)
    },
  },
}

contextBridge.exposeInMainWorld('qt', qt)
