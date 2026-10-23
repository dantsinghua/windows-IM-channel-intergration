/**
 * `console.toml`(01 §7)—— `%ProgramData%\QTrade\config\console.toml`,机器级偏好。
 * 控制台没有业务配置(基线 §5),这里只有偏好与本地行为。
 */
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { parseToml, stringifyToml, type TomlDoc } from './toml'

export const DEFAULT_CONFIG: TomlDoc = {
  setup: { done: false },
  ui: { theme: 'light', nav_collapsed: false, locale: 'zh-CN' },
  app: { auto_launch: true, start_hidden: true, minimize_to_tray_on_close: true, crash_safe_threshold: 3 },
  notify: { enabled: true, login_required: true, alert_crit: true, account_error: true },
  endpoint: { agent: 'http://127.0.0.1:17600', winagent: 'http://127.0.0.1:17610' },
  screen: {
    default_focus_profile: 'focus30',
    thumb_enabled: true,
    prefer_software_decode: false,
    wechat_preview_interval_ms: 2000,
    static_preview_interval_ms: 2000,
  },
  log: { level: 'info', retention_days: 30 },
  export: { include_screenshots: false, include_media: false },
}

export function configPath(): string {
  const programData = process.env.PROGRAMDATA || process.env.QT_STATE_DIR || join(process.env.HOME ?? '.', '.qtrade')
  return join(programData, 'QTrade', 'config', 'console.toml')
}

let cache: TomlDoc | null = null

export function readConfig(): TomlDoc {
  if (cache) return cache
  const p = configPath()
  let doc: TomlDoc = {}
  if (existsSync(p)) {
    try {
      doc = parseToml(readFileSync(p, 'utf8'))
    } catch {
      doc = {}
    }
  }
  cache = mergeDefaults(doc)
  return cache
}

function mergeDefaults(doc: TomlDoc): TomlDoc {
  const out: TomlDoc = {}
  for (const [sec, kv] of Object.entries(DEFAULT_CONFIG)) out[sec] = { ...kv, ...(doc[sec] ?? {}) }
  for (const [sec, kv] of Object.entries(doc)) if (!out[sec]) out[sec] = { ...kv }
  return out
}

/** 原子写:临时文件 + rename */
export function writeConfig(doc: TomlDoc): void {
  const p = configPath()
  mkdirSync(dirname(p), { recursive: true })
  const tmp = `${p}.tmp`
  writeFileSync(tmp, stringifyToml(doc), 'utf8')
  renameSync(tmp, p)
  cache = doc
}

export function patchConfig(patch: Record<string, Record<string, unknown>>): TomlDoc {
  const doc = { ...readConfig() }
  for (const [sec, kv] of Object.entries(patch)) {
    doc[sec] = { ...(doc[sec] ?? {}), ...(kv as Record<string, string | number | boolean>) }
  }
  writeConfig(doc)
  return doc
}
