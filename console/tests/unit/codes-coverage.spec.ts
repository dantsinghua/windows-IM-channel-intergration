/**
 * `codes.ts` 覆盖度对账 —— 机器码枚举的唯一来源在 02/00/06,中文对照在 01。
 * 本测试从文档反解码表,断言 `codes.ts` 逐条登记。
 */
import { describe, expect, it } from 'vitest'
import { readDoc, section, splitRow } from './doc-utils'
import {
  ALERT_CODES, MAIL_BANNER_CODES, MAIL_INBOX_STATUS, PROBE_RESULTS, RESULT_CODES,
  STATE_CODES, WECHAT_MATCH, SOURCE_TEXT, HINT_ACTIONS, DANGER_OPS,
} from '@/i18n/zh-CN/codes'

/** 02 §3.7 告警码枚举表 —— 机器码单一来源 */
function alertCodesFromDoc(): { code: string; severity: string }[] {
  const doc = readDoc('02-后端与本地数据库设计.md')
  const sec = section(doc, '### 3.7 告警码枚举表', /^### 3\.8 /)
  const out: { code: string; severity: string }[] = []
  for (const line of sec.split('\n')) {
    if (!line.startsWith('|')) continue
    const cells = splitRow(line).map((c) => c.trim())
    // cells[0] 是行首空串;cells[1]=code、cells[2]=severity
    const m = /^`?([A-Z][A-Z0-9_]{3,})`?$/.exec(cells[1] ?? '')
    if (!m) continue
    if (cells[1] === 'code') continue
    out.push({ code: m[1], severity: (cells[2] ?? '').replace(/`/g, '') })
  }
  return out
}

describe('告警码(02 §3.7)', () => {
  const rows = alertCodesFromDoc()

  it('文档里确实解析到了一整张表', () => {
    expect(rows.length).toBeGreaterThan(50)
  })

  it('每个告警码都在 codes.ts 的 ALERT_CODES 里登记', () => {
    const missing = rows.filter((r) => !(r.code in ALERT_CODES)).map((r) => r.code)
    expect(missing, `codes.ts 缺登记:${missing.join(', ')}`).toEqual([])
  })

  it('codes.ts 不得自造文档里没有的告警码', () => {
    const known = new Set(rows.map((r) => r.code))
    const extra = Object.keys(ALERT_CODES).filter((c) => !known.has(c))
    expect(extra, `codes.ts 多出:${extra.join(', ')}`).toEqual([])
  })

  it('默认 severity 与文档一致', () => {
    const bad: string[] = []
    for (const r of rows) {
      const meta = ALERT_CODES[r.code]
      if (!meta) continue
      // 文档里写成 "warn / crit" 之类的取第一个
      const docSev = r.severity.split(/[\s/|]+/)[0]
      if (['info', 'warn', 'crit'].includes(docSev) && meta.severity !== docSev) {
        bad.push(`${r.code}: 文档 ${docSev} ≠ codes.ts ${meta.severity}`)
      }
    }
    expect(bad, bad.join('; ')).toEqual([])
  })

  it('MAIL_* 是十四码,且都配了中文(01 §2.10 逐码维护)', () => {
    const mail = Object.keys(ALERT_CODES).filter((c) => c.startsWith('MAIL_'))
    expect(mail.length).toBe(14)
    for (const c of mail) expect(ALERT_CODES[c].zh, `${c} 缺中文`).toBeTruthy()
  })

  it('常驻码(N-25 判据)必须有固定文案', () => {
    for (const c of ['WECHAT_DISK_LOW', 'DOCKER_POOL_ALL_CONFLICT', 'QIDIAN_NOT_ROOT', 'QIDIAN_DB_UNAVAILABLE']) {
      expect(ALERT_CODES[c]?.zh, `${c} 需常驻文案`).toBeTruthy()
    }
  })

  it('R6-50:只进铃的两个企点码不配文案、也不上横幅', () => {
    expect(ALERT_CODES.QIDIAN_TABLE_DECODE_STUCK.zh).toBeUndefined()
    expect(ALERT_CODES.QIDIAN_MSG_GAP.zh).toBeUndefined()
  })

  it('P-MAIL 横幅六码不含 MAIL_PROTOCOL_FALLBACK(R6-35)', () => {
    expect(MAIL_BANNER_CODES).toHaveLength(6)
    expect(MAIL_BANNER_CODES).not.toContain('MAIL_PROTOCOL_FALLBACK')
  })

  it('hint_actions 取值与 02 §3.7 末行一致', () => {
    const doc = readDoc('02-后端与本地数据库设计.md')
    const line = doc.split('\n').find((l) => l.includes('`hint_actions` 取值(04 §4'))
    expect(line).toBeTruthy()
    const acts = [...(line ?? '').matchAll(/`([a-z_]+)`/g)].map((x) => x[1])
      .filter((x) => x !== 'hint_actions')
    for (const a of acts) expect(HINT_ACTIONS[a], `hint_action ${a} 缺中文`).toBeTruthy()
  })
})

describe('结果码(00 §8.3)', () => {
  it('每个结果码都在 RESULT_CODES 里', () => {
    const doc = readDoc('00-共享基线与口径.md')
    const sec = section(doc, '### 8.3 结果码', /^### 8\.4 /)
    const codes = new Set<string>()
    for (const line of sec.split('\n')) {
      if (!line.startsWith('|')) continue
      const first = splitRow(line)[1] ?? ''
      for (const m of first.matchAll(/`([A-Z][A-Z_]{2,})`/g)) codes.add(m[1])
    }
    expect(codes.size).toBeGreaterThan(15)
    const missing = [...codes].filter((c) => !(c in RESULT_CODES))
    expect(missing, `RESULT_CODES 缺:${missing.join(', ')}`).toEqual([])
  })
})

describe('state_code / 邮件状态 / 探测结论 / 微信匹配 / 来源', () => {
  it('01 §2.10 的 state_code 表逐条在 STATE_CODES 里', () => {
    const doc = readDoc('01-控制台前端设计.md')
    const sec = section(doc, '| 组 | `state_code` |', /^⚠️ \*\*`WAIT_KEY_IMG`/)
    const codes: string[] = []
    for (const line of sec.split('\n')) {
      if (!line.startsWith('|')) continue
      const cells = splitRow(line)
      const m = /`([A-Z][A-Z_]+)`/.exec(cells[2] ?? '')
      if (m) codes.push(m[1])
    }
    expect(codes.length).toBeGreaterThan(20)
    const missing = codes.filter((c) => !(c in STATE_CODES))
    expect(missing, `STATE_CODES 缺:${missing.join(', ')}`).toEqual([])
  })

  it('06 §2.3.5 的 mail_inbox.status 全集在 MAIL_INBOX_STATUS 里', () => {
    const doc = readDoc('06-邮件摆渡与消息存取.md')
    const sec = section(doc, '#### 2.3.5 ', /^#### 2\.3\.6 /)
    // 只取「状态机那两行散文」,并剥掉括号里的举例(那里混着 INVALID_ARGS 等别的码)
    const head = sec.split('\n')
      .filter((l) => l.includes('`RECEIVED`') || l.startsWith('失败终态'))
      .map((l) => {
        // 剥掉括号里的举例:\uFF08\uFF09 与 () 各剥一遍
        let t = l
        for (let i = 0; i < 5; i++) {
          t = t.replace(/\uFF08[^\uFF08\uFF09]*\uFF09/g, '').replace(/\([^()]*\)/g, '')
        }
        return t
      })
      .join('\n')
    const codes = new Set([...head.matchAll(/`([A-Z][A-Z_]{3,})`/g)].map((m) => m[1]))
    expect(codes.size).toBeGreaterThan(15)
    const missing = [...codes].filter((c) => !(c in MAIL_INBOX_STATUS))
    expect(missing, `MAIL_INBOX_STATUS 缺:${missing.join(', ')}`).toEqual([])
  })

  it('00 §8.5 的 probe_result 全集在 PROBE_RESULTS 里', () => {
    const doc = readDoc('00-共享基线与口径.md')
    const line = doc.split('\n').find((l) => l.includes('探测结论 `probe_result`'))
    expect(line).toBeTruthy()
    const codes = (line ?? '').split('`probe_result`:')[1]
      .split('(')[0]
      .split('|')
      .map((s) => s.replace(/[`\s]/g, ''))
      .filter(Boolean)
    expect(codes.length).toBe(10)
    for (const c of codes) expect(PROBE_RESULTS[c], `probe_result ${c} 缺中文`).toBeTruthy()
  })

  it('00 §8.6 的 wechat_match 五值齐全', () => {
    for (const c of ['NOT_INSTALLED', 'SUPPORTED', 'UNSUPPORTED_NEWER', 'UNSUPPORTED_OLDER', 'MULTIPLE_INSTALLS']) {
      expect(WECHAT_MATCH[c]).toBeTruthy()
    }
  })

  it('A-1:screenshot 恒显示「截图+OCR」,不出现「模型/转录」字样', () => {
    expect(SOURCE_TEXT.screenshot).toBe('截图+OCR')
    for (const v of Object.values(SOURCE_TEXT)) {
      expect(v).not.toMatch(/模型|转录/)
    }
  })

  it('danger 十项与 02 §3.10 权威集合一致', () => {
    const doc = readDoc('02-后端与本地数据库设计.md')
    const line = doc.split('\n').find((l) => l.includes('**danger 十项的权威集合'))
    expect(line).toBeTruthy()
    const inBraces = /\{([^}]+)\}/.exec(line ?? '')?.[1] ?? ''
    const ops = inBraces.split(',').map((s) => s.trim()).filter(Boolean)
    expect(ops.length).toBe(10)
    expect([...DANGER_OPS].sort()).toEqual(ops.sort())
  })
})
