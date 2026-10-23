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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { message } from 'ant-design-vue'
import SetPage from '@/pages/set/SetPage.vue'
import { settingsApi, systemApi } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import {
  API_KEYS, ASR_KEYS, ASR_SECRET_KEY, OCR_KEYS, POOL_KEYS, RETENTION_KEYS, pickKeys,
} from '../../src/api/settingsKeys'

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

/**
 * R6-81 / 安琳 2026-10-08 最新指令：复杂保留期、配额和保险库控件退役。
 * 上面的接口白名单守护原样保留；这里用日常偏好行为替代已移除控件的存在性断言。
 */
describe('P-SET 仅保留本地偏好与只读告知', () => {
  const wrappers: VueWrapper[] = []
  const configPatch = vi.fn()
  const autoLaunch = vi.fn()

  beforeEach(() => {
    setActivePinia(createPinia())
    configPatch.mockReset().mockResolvedValue(undefined)
    autoLaunch.mockReset().mockResolvedValue(false)
    vi.stubGlobal('qt', { config: { patch: configPatch }, app: { setAutoLaunch: autoLaunch } })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected settings network call')))
    vi.spyOn(systemApi, 'notice').mockResolvedValue({ notice_version: 'test-v1', text: '测试使用告知', acked_version: 'test-v1' })
    vi.spyOn(systemApi, 'noticeAck').mockResolvedValue({ ok: true })
    vi.spyOn(settingsApi, 'put').mockRejectedValue(new Error('Preferences must not replace Agent configuration'))
    vi.spyOn(message, 'success').mockReturnValue((() => undefined) as never)
    vi.spyOn(message, 'error').mockReturnValue((() => undefined) as never)
  })

  afterEach(() => {
    for (const wrapper of wrappers.splice(0)) wrapper.unmount()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  function renderPreferences() {
    const wrapper = shallowMount(SetPage, {
      global: { stubs: { 'a-switch': true, 'a-button': true, 'a-modal': true }, renderStubDefaultSlot: true },
    })
    wrappers.push(wrapper)
    return wrapper
  }

  it('只展示三项日常偏好，不再提供保留期、配额或保险库表单', () => {
    const page = renderPreferences()
    expect(page.findAll('a-switch-stub').map((item) => item.attributes('aria-label'))).toEqual([
      '桌面提醒', '关闭窗口后继续运行', '登录后自动启动',
    ])
    expect(page.findAll('input,textarea,a-input-stub,a-input-number-stub')).toHaveLength(0)
    expect(page.text()).not.toMatch(/保留期|保险库|资源配额/)
  })

  it.each([
    ['桌面提醒', { notify: { enabled: false } }],
    ['关闭窗口后继续运行', { app: { minimize_to_tray_on_close: false } }],
  ])('修改%s只补丁写本地键，不替换 Agent 设置组', async (label, expected) => {
    const page = renderPreferences()
    ;(page.getComponent(`[aria-label="${label}"]`) as VueWrapper).vm.$emit('change', false)
    await flushPromises()
    expect(configPatch).toHaveBeenCalledTimes(1)
    expect(configPatch).toHaveBeenCalledWith(expected)
    expect(settingsApi.put).not.toHaveBeenCalled()
  })

  it('自动启动以主进程实际应用结果回填，不能把请求值冒充结果', async () => {
    const page = renderPreferences()
    ;(page.getComponent('[aria-label="登录后自动启动"]') as VueWrapper).vm.$emit('change', true)
    await flushPromises()
    expect(autoLaunch).toHaveBeenCalledWith(true)
    expect(configPatch).toHaveBeenCalledWith({ app: { auto_launch: false } })
    expect(useUiStore().autoLaunch).toBe(false)
    expect(settingsApi.put).not.toHaveBeenCalled()
  })

  it('告知仍读取 #86，查看不得自动确认告知或写配置', async () => {
    const page = renderPreferences()
    await flushPromises()
    expect(systemApi.notice).toHaveBeenCalledTimes(1)
    expect(page.text()).toContain('测试使用告知')
    expect(systemApi.noticeAck).not.toHaveBeenCalled()
    expect(settingsApi.put).not.toHaveBeenCalled()
    expect(configPatch).not.toHaveBeenCalled()
  })

  it('保存失败保留原偏好并反馈失败，不假报保存成功', async () => {
    configPatch.mockRejectedValue(new Error('fixture local save failed'))
    const page = renderPreferences()
    ;(page.getComponent('[aria-label="桌面提醒"]') as VueWrapper).vm.$emit('change', false)
    await flushPromises()
    expect(useUiStore().notifyEnabled).toBe(true)
    expect(message.error).toHaveBeenCalledWith('fixture local save failed')
    expect(message.success).not.toHaveBeenCalled()
    expect(settingsApi.put).not.toHaveBeenCalled()
  })
})
