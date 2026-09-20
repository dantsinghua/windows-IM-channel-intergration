/**
 * `data-testid` 覆盖度对账 —— 01 §4 是元素 ID 的**唯一出处**(C-45)。
 * 本测试从 01 §4 全表反解每个 testid,断言源码里都出现过。
 *
 * 归一化规则:
 *  - 表格首列才算元素 id(其它列出现的是引用/作废说明,不算)
 *  - ~~删除线~~ 的行整条跳过
 *  - `{a|b|c}` 是枚举,展开成多个 id;也接受源码写成模板 `${x}`
 *  - `{id}` / `*` 是占位符,源码里必须是模板串 `${…}`
 *  - 以 `-` 开头的续写项按「与前一个 id 的尾部重叠」解析
 */
import { describe, expect, it } from 'vitest'
import { collectSource, readDoc, section, splitRow, stripStrikethrough } from './doc-utils'

const PLACEHOLDER_RE = '\\$\\{[^}]+\\}'

interface Spec { raw: string; regexes: RegExp[] }

/** `{a|b|c}` → 展开;`{x}` / `*` → 占位符 */
function toRegexes(id: string): RegExp[] {
  // 先展开枚举
  const enumMatch = /\{([^{}]*\|[^{}]*)\}/.exec(id)
  if (enumMatch) {
    const options = enumMatch[1].split('|').map((s) => s.trim())
    return options.flatMap((o) =>
      toRegexes(id.slice(0, enumMatch.index) + o + id.slice(enumMatch.index + enumMatch[0].length)),
    ).concat(toRegexes(id.replace(enumMatch[0], '\u0000PH\u0000')))
  }
  // 剩下的 {xxx} 与 * 都是占位符
  let pattern = ''
  for (const part of id.split(/(\{[^}]*\}|\*|\u0000PH\u0000)/)) {
    if (!part) continue
    if (part === '*' || part === '\u0000PH\u0000' || (part.startsWith('{') && part.endsWith('}'))) {
      pattern += PLACEHOLDER_RE
    } else {
      pattern += part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    }
  }
  return [new RegExp(pattern)]
}

/** 按 `-` 切段,但**不切** `{...}` 里的 `-`(`{send-interval|log-body}` 是一段) */
function segments(id: string): string[] {
  const out: string[] = []
  let cur = ''
  let depth = 0
  for (const ch of id.replace(/^-/, '')) {
    if (ch === '{') depth += 1
    if (ch === '}') depth -= 1
    if (ch === '-' && depth === 0) { out.push(cur); cur = ''; continue }
    cur += ch
  }
  out.push(cur)
  return out.filter((s) => s !== '')
}

/**
 * 续写项 `-suffix` 相对前一个完整 id 的候选解析:
 * 去掉前 id 的最后 k 个段(k=0,1,2),再与 suffix 的开头做最长重叠。
 */
function resolveContinuation(prev: string, suffix: string): string[] {
  const p = segments(prev)
  const s = segments(suffix)
  const out = new Set<string>()
  for (let k = 0; k <= 2 && k < p.length; k++) {
    const base = p.slice(0, p.length - k)
    let best = 0
    for (let n = Math.min(base.length, s.length); n > 0; n--) {
      if (base.slice(base.length - n).join('-') === s.slice(0, n).join('-')) { best = n; break }
    }
    out.add([...base, ...s.slice(best)].join('-'))
  }
  return [...out]
}

function parseSpecs(): Spec[] {
  const doc = readDoc('01-控制台前端设计.md')
  const sec = section(doc, '**全局外框(shell)**', /^## 5\. /)
  const specs: Spec[] = []
  let prev = ''
  for (const rawLine of sec.split('\n')) {
    if (!rawLine.trim().startsWith('|')) continue
    const cells = splitRow(rawLine)
    const first = stripStrikethrough(cells[1] ?? '')
    if (!first.trim()) continue
    const tokens = [...first.matchAll(/`([^`]+)`/g)].map((m) => m[1].trim())
    for (const t of tokens) {
      if (/^qt-/.test(t)) {
        prev = t
        specs.push({ raw: t, regexes: toRegexes(t) })
      } else if (/^-[a-z0-9{}]/i.test(t) && prev) {
        const cands = resolveContinuation(prev, t)
        specs.push({ raw: `${prev} / ${t}`, regexes: cands.flatMap(toRegexes) })
      }
    }
  }
  return specs
}

describe('01 §4 元素全表覆盖', () => {
  const specs = parseSpecs()
  const source = collectSource(['src', 'electron'])

  it('从 01 §4 解析出了足量的 testid', () => {
    expect(specs.length).toBeGreaterThan(200)
  })

  it('每个 testid 在源码里都出现', () => {
    const missing = specs.filter((s) => !s.regexes.some((r) => r.test(source))).map((s) => s.raw)
    expect(missing, `源码里找不到这些 01 §4 元素:\n${missing.join('\n')}`).toEqual([])
  })

  it('作废元素不得出现在源码里(C-45 / R-11 / R-12)', () => {
    for (const dead of [
      'qt-acct-add-btn', 'qt-acct-qrcode', 'qt-acct-narrator-countdown', 'qt-acct-switch-btn',
      'qt-acct-login-password', 'qt-acct-retry-now', 'qt-acct-delete-purge-input',
      'qt-acct-detail-retry-now',
    ]) {
      expect(source.includes(`'${dead}'`), `${dead} 已作废`).toBe(false)
      expect(source.includes(`"${dead}"`), `${dead} 已作废`).toBe(false)
    }
  })

  it('P-SET 的全局 auto_stop 开关已删除(R5-8 老分叉②)', () => {
    // 只留固定说明 qt-set-autostop-note,不得有 qt-set-autostop 开关
    expect(/['"`]qt-set-autostop['"`]/.test(source)).toBe(false)
    expect(source).toContain('qt-set-autostop-note')
  })
})
