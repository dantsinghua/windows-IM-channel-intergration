/**
 * P-SET「控件 ↔ 配置键」的静态对账闸。
 *
 * 缺陷现场(独立联调 P-2 / X-4):保留期卡片把「库内消息正文保留天数」写成 `text_days`、
 * 「原始载荷」写成 `raw_enabled` —— `docs/07-配置项总表.md` `[retention]` 里**没有这两个键**。
 * 旧后端照单全收 200,于是用户改了等于没改,而 `#89` 的「整组替换(缺省键回默认)」
 * 又把同组其余键静默洗回默认;新后端改判 `400 INVALID_ARGS` 之后,一个笔误就让整组存不进去。
 *
 * 这里断的是**前端自己这一侧**:白名单里有没有野键、页面控件是不是真绑在白名单的键上。
 * 「白名单 ↔ 真实出参键集」那一半在 `mock-shape.spec.ts`(那里有跑着的 mock,按 GET 的真出参对账)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  API_KEYS, ASR_KEYS, ASR_SECRET_KEY, OCR_KEYS, POOL_KEYS, RETENTION_KEYS, pickKeys,
} from '../../src/api/settingsKeys'

const SET_PAGE_SRC = readFileSync(resolve(__dirname, '../../src/pages/set/SetPage.vue'), 'utf8')

describe('各组白名单本身不含野键', () => {
  it('retention:是 `messages_days`/`raw_days`,不是 `text_days`/`raw_enabled`', () => {
    expect(RETENTION_KEYS).toContain('messages_days')
    expect(RETENTION_KEYS).toContain('raw_days')
    expect(RETENTION_KEYS).not.toContain('text_days')
    expect(RETENTION_KEYS).not.toContain('raw_enabled')
  })

  it('api:`lan_enabled`/`ip_allow`/`https` 在 07 `[api]` 里没有出处,不得下发', () => {
    for (const wild of ['lan_enabled', 'ip_allow', 'https']) {
      expect(API_KEYS, `${wild} 不是配置键`).not.toContain(wild)
    }
    expect(API_KEYS, 'public_domain 不在 agent.toml 里,但**在**本组可提交键里(02 #102)').toContain('public_domain')
    expect(API_KEYS, '「允许局域网访问」的落点就是 bind').toContain('bind')
  })

  it('pool:内存水位阈值属于这一组(不是 `resources`)', () => {
    expect(POOL_KEYS).toContain('mem_warn_mb')
    expect(POOL_KEYS).toContain('mem_critical_mb')
  })

  it('ocr:`engine` 只读不下发(A-1:引擎不可选、不可填端点)', () => {
    expect(OCR_KEYS).not.toContain('engine')
    expect(sorted(OCR_KEYS)).toEqual(['lang', 'min_conf', 'model_dir'])
  })

  it('asr:密钥不走普通键;写入键以 secret/password/token 结尾才会被收进 Vault(#88)', () => {
    expect(ASR_KEYS, 'api_key_ref 是只读引用,提交它等于把引用当配置值写回').not.toContain('api_key_ref')
    expect(ASR_KEYS).not.toContain('key')
    expect(ASR_SECRET_KEY).toMatch(/(secret|password|token)$/)
  })

  it('pickKeys 只留白名单里的键,并丢掉 undefined', () => {
    expect(pickKeys({ messages_days: 14, text_days: 20, raw_days: undefined }, RETENTION_KEYS))
      .toEqual({ messages_days: 14 })
  })
})

function sorted(v: Iterable<string>): string[] {
  return [...v].sort()
}

describe('P-SET 的控件**真的**绑在白名单的键上(白名单对了、控件绑错一样是 P-2)', () => {
  const retentionBlock = (() => {
    const i = SET_PAGE_SRC.indexOf('<!-- 保留期 -->')
    expect(i, 'SetPage.vue 里找不到保留期卡片').toBeGreaterThan(0)
    return SET_PAGE_SRC.slice(i, SET_PAGE_SRC.indexOf('<!-- 保险库(WinAgent) -->', i))
  })()

  it('保留期四个控件读写的都是 07 `[retention]` 登记的键', () => {
    const used = new Set([...retentionBlock.matchAll(/retention\.([a-z_0-9]+)/g)].map((m) => m[1]))
    expect(used.size, '没扫到任何 retention.<key> 绑定').toBeGreaterThan(0)
    for (const k of used) {
      expect(RETENTION_KEYS as readonly string[], `控件绑了 07 没有的键 retention.${k}`).toContain(k)
    }
    // 01 §4 :1337 登记的四个控件(files|text|raw|audit)各自的落点
    for (const k of ['files_days', 'messages_days', 'raw_days', 'audit_days']) {
      expect(used, `保留期卡片少了 ${k} 的绑定`).toContain(k)
    }
  })

  it('E-18 的 30 天上限判在 `messages_days` 上(判错键 = 上限形同虚设)', () => {
    expect(/const days = Number\(retention\.value\.messages_days \?\? 30\)/.test(SET_PAGE_SRC),
      'saveRetention 的 30 天上限没有判 messages_days').toBe(true)
  })

  it('资源池卡片按 #88 的 `{pools, quota_mb}` 两键取数,不再有 `qidian_mb` 这类顶层野键', () => {
    for (const wild of ['resGroup.qidian_mb', 'resGroup.qq_mb', 'resGroup.wechat_mb', 'resGroup.base_mb']) {
      expect(SET_PAGE_SRC, `资源池仍在读 ${wild}(#88 的 resources 组没有这个键)`).not.toContain(wild)
    }
    expect(SET_PAGE_SRC).toContain("setQuota('qidian'")
    expect(SET_PAGE_SRC, 'WSL 基础占用 = pools.wsl.reserved_mb').toContain("setPoolReserved('wsl'")
  })

  it('合规块走 #86 `GET /system/notice`,不再调已废弃的 `/settings/compliance`(D-E)', () => {
    expect(SET_PAGE_SRC, "SetPage 仍在 loadGroup('compliance') ⇒ 每次打开都白打一条 404")
      .not.toContain("loadGroup('compliance')")
    expect(SET_PAGE_SRC).toContain('systemApi.notice()')
  })
})
