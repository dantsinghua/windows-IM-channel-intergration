/** 窗口位置/尺寸与崩溃记录:`%LOCALAPPDATA%\QTrade\{window,crash}.json`(01 §2.4) */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export interface WindowState {
  x?: number
  y?: number
  width: number
  height: number
  maximized: boolean
}

export interface CrashState {
  route: string
  accountFilter?: string | null
  times: number[]
}

function baseDir(): string {
  const local = process.env.LOCALAPPDATA || join(process.env.HOME ?? '.', '.local', 'share')
  return join(local, 'QTrade')
}

function readJson<T>(p: string, fallback: T): T {
  try {
    if (!existsSync(p)) return fallback
    return JSON.parse(readFileSync(p, 'utf8')) as T
  } catch {
    return fallback
  }
}

function writeJson(p: string, v: unknown): void {
  try {
    mkdirSync(dirname(p), { recursive: true })
    writeFileSync(p, JSON.stringify(v), 'utf8')
  } catch {
    // 偏好缓存写不进去不该让应用起不来
  }
}

export const windowStatePath = () => join(baseDir(), 'window.json')
export const crashStatePath = () => join(baseDir(), 'crash.json')

export function loadWindowState(): WindowState {
  return readJson<WindowState>(windowStatePath(), { width: 1440, height: 900, maximized: false })
}

export function saveWindowState(s: WindowState): void {
  writeJson(windowStatePath(), s)
}

export function loadCrashState(): CrashState {
  return readJson<CrashState>(crashStatePath(), { route: '/dash', times: [] })
}

/**
 * 记一次崩溃并判断是否要停在安全页:
 * 5 分钟内累计达到 `threshold` 次 → 停 `P-ENV`(画面流关闭)并提示导出日志。
 * ⚠️ `crash.json` **只记路由与筛选**,不记任何正文/密码(§6-1)。
 */
export function recordCrash(route: string, threshold: number, now = Date.now()): { safeMode: boolean; route: string } {
  const st = loadCrashState()
  const times = [...st.times, now].filter((t) => now - t <= 5 * 60 * 1000)
  const next: CrashState = { route, times }
  writeJson(crashStatePath(), next)
  const safeMode = times.length >= threshold
  return { safeMode, route: safeMode ? '/env' : route }
}

export function clearCrashes(): void {
  writeJson(crashStatePath(), { route: '/dash', times: [] })
}
