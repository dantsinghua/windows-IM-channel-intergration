/**
 * 极简 TOML 读写 —— 只覆盖 `console.toml`(01 §7)的形态:
 * `[section]` + `key = value`(字符串/布尔/整数),不支持数组表、内联表、多行串。
 * 刻意不引第三方库:控制台配置就这十几个键,依赖越少主进程越稳。
 */

export type TomlValue = string | number | boolean
export type TomlDoc = Record<string, Record<string, TomlValue>>

export function parseToml(text: string): TomlDoc {
  const doc: TomlDoc = {}
  let section = ''
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const sec = /^\[([^\]]+)\]$/.exec(line)
    if (sec) {
      section = sec[1].trim()
      doc[section] ??= {}
      continue
    }
    const kv = /^([A-Za-z0-9_.-]+)\s*=\s*(.+)$/.exec(line)
    if (!kv) continue
    const key = kv[1]
    let raw = kv[2].trim()
    const hash = findCommentStart(raw)
    if (hash >= 0) raw = raw.slice(0, hash).trim()
    doc[section] ??= {}
    doc[section][key] = parseValue(raw)
  }
  return doc
}

function findCommentStart(s: string): number {
  let inStr = false
  for (let i = 0; i < s.length; i++) {
    const c = s[i]
    if (c === '"') inStr = !inStr
    else if (c === '#' && !inStr) return i
  }
  return -1
}

function parseValue(raw: string): TomlValue {
  if (raw === 'true') return true
  if (raw === 'false') return false
  if (/^-?\d+$/.test(raw)) return Number(raw)
  if (/^-?\d+\.\d+$/.test(raw)) return Number(raw)
  if (raw.startsWith('"') && raw.endsWith('"')) return raw.slice(1, -1).replace(/\\"/g, '"')
  if (raw.startsWith("'") && raw.endsWith("'")) return raw.slice(1, -1)
  return raw
}

export function stringifyToml(doc: TomlDoc): string {
  const out: string[] = []
  for (const [section, kv] of Object.entries(doc)) {
    out.push(`[${section}]`)
    for (const [k, v] of Object.entries(kv)) out.push(`${k} = ${formatValue(v)}`)
    out.push('')
  }
  return out.join('\n')
}

function formatValue(v: TomlValue): string {
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'number') return String(v)
  return `"${String(v).replace(/"/g, '\\"')}"`
}
