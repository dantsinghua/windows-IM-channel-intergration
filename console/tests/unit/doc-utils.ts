/** 测试用:从 docs/ 的设计文档里反解「真值」,用来对账源码。 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

function findConsoleRoot(): string {
  // vitest 的 root 就是 console/;从 cwd 往上找带 package.json 的那层
  let d = process.cwd()
  for (let i = 0; i < 5; i++) {
    if (existsSync(join(d, 'package.json')) && existsSync(join(d, 'src'))) return d
    d = resolve(d, '..')
  }
  return process.cwd()
}

export const CONSOLE_ROOT = findConsoleRoot()
export const DOCS_DIR = resolve(CONSOLE_ROOT, '..', 'docs')

export function readDoc(name: string): string {
  return readFileSync(join(DOCS_DIR, name), 'utf8')
}

/** 取 `## N.` 或 `### N.x` 开头到下一个同级/更高级标题之间的正文 */
export function section(text: string, startsWith: string, stopWith: RegExp): string {
  const lines = text.split(/\r?\n/)
  const from = lines.findIndex((l) => l.startsWith(startsWith))
  if (from < 0) throw new Error(`文档里找不到章节:${startsWith}`)
  let to = lines.length
  for (let i = from + 1; i < lines.length; i++) {
    if (stopWith.test(lines[i])) { to = i; break }
  }
  return lines.slice(from, to).join('\n')
}

/** 按 `|` 切表格行,但**不切**反引号里的 `|`(`{mem|cpu|disk}` 这种) */
export function splitRow(line: string): string[] {
  const cells: string[] = []
  let cur = ''
  let tick = false
  for (const ch of line) {
    if (ch === '`') { tick = !tick; cur += ch; continue }
    if (ch === '|' && !tick) { cells.push(cur); cur = ''; continue }
    cur += ch
  }
  cells.push(cur)
  return cells
}

/** 去掉 ~~删除线~~ 的整段 */
export function stripStrikethrough(s: string): string {
  return s.replace(/~~[\s\S]*?~~/g, '')
}

/** 递归收集源码文本(用于「这个 testid 在源码里出现过吗」) */
export function collectSource(dirs: string[], exts = ['.ts', '.vue', '.mts']): string {
  const out: string[] = []
  const walk = (d: string): void => {
    for (const name of readdirSync(d)) {
      if (name === 'node_modules' || name === 'dist') continue
      const p = join(d, name)
      const st = statSync(p)
      if (st.isDirectory()) walk(p)
      else if (exts.some((e) => name.endsWith(e))) out.push(readFileSync(p, 'utf8'))
    }
  }
  for (const d of dirs) walk(resolve(CONSOLE_ROOT, d))
  return out.join('\n')
}
