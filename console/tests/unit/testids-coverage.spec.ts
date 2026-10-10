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
import { acctDetail } from '@/testids'
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

function parseSpecs(doc = readDoc('01-控制台前端设计.md')): Spec[] {
  const sec = section(doc, '**全局外框(shell)**', /^## 5\. /)
  const specs: Spec[] = []
  let prev = ''
  for (const rawLine of sec.split('\n')) {
    if (!rawLine.trim().startsWith('|')) continue
    const cells = splitRow(rawLine)
    // 只承认元素类型与要求两列同时明确退役；业务描述中的“退役”不得豁免现行元素。
    if ((cells[2] ?? '').trim() === '历史按钮(退役)' && (cells[4] ?? '').trim() === '不再要求渲染') continue
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

function sourceWithDynamicActions(source: string): string {
  // 单列的具体动作 ID 由既有工厂产生；同时要求页面真实绑定及该动作的处理分支。
  if (!source.includes(':data-testid="T.stateCardAction(action)"') || !source.includes("case 'refresh-qr':")) return source
  return source + '\n' + acctDetail.stateCardAction('refresh-qr')
}

function missingSpecs(specs: Spec[], source: string): string[] {
  const emitted = sourceWithDynamicActions(source)
  return specs.filter((s) => !s.regexes.some((r) => r.test(emitted))).map((s) => s.raw)
}

describe('01 §4 元素全表覆盖', () => {
  const specs = parseSpecs()
  const source = collectSource(['src', 'electron'])

  it('从 01 §4 解析出了足量的 testid', () => {
    expect(specs.length).toBeGreaterThan(200)
  })

  it('每个 testid 在源码里都出现', () => {
    const missing = missingSpecs(specs, source)
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

  it('只有类型和要求两列同时标明退役才免除源码存在要求', () => {
    const doc = '**全局外框(shell)**\n' +
      '| `qt-retired-fixture` | 历史按钮(退役) | R6-81 | 不再要求渲染 |\n' +
      '| `qt-live-fixture` | 按钮 | 附注提到历史退役项 | 始终显示 |\n' +
      '| `qt-partial-fixture` | 历史按钮(退役) | 待裁 | 仍须显示 |\n## 5. 下一节'
    const parsed = parseSpecs(doc)
    expect(parsed.map(s => s.raw)).toEqual(['qt-live-fixture', 'qt-partial-fixture'])
    expect(missingSpecs(parsed, '')).toEqual(['qt-live-fixture', 'qt-partial-fixture'])
    expect(missingSpecs(parsed, "'qt-live-fixture' 'qt-partial-fixture'")).toEqual([])
  })

  it('refresh-qr 只有真实工厂、页面绑定、动作处理三者齐全才算覆盖', () => {
    const id = 'qt-acct-detail-state-card-action-refresh-qr'
    const dynamic = [{ raw: id, regexes: toRegexes(id) }]
    const binding = ':data-testid="T.stateCardAction(action)"'
    const handler = "case 'refresh-qr':"
    expect(acctDetail.stateCardAction('refresh-qr')).toBe(id)
    expect(missingSpecs(dynamic, binding + handler)).toEqual([])
    expect(missingSpecs(dynamic, binding)).toEqual([id])
    expect(missingSpecs(dynamic, handler)).toEqual([id])
    expect(missingSpecs([{ raw: id + '-wrong', regexes: toRegexes(id + '-wrong') }], binding + handler)).toEqual([id + '-wrong'])
  })
})
