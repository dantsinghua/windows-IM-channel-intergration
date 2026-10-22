/** 工作台配置独立验收：只用合成字段与 API 假边界，不访问业务队列。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OssboxConfig from '@/pages/mail/OssboxConfig.vue'
import MailPage from '@/pages/mail/MailPage.vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure, configureHttp } from '@/api/http'
import type { OssboxAuthenticationResult, OssboxSettingsResponse } from '@/api/types'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'
import { useSessionStore } from '@/stores/session'

const NAME = '上海采集工作台'
const BASE = 'https://collector.example.invalid'
const DRAFT = 'https://draft.example.invalid'
const KEY = 'synthetic-independent-collector-key'
const SM4 = '0123456789abcdef'
const pages: VueWrapper[] = []
const GOOD: OssboxAuthenticationResult = { authenticated: true, pending: 0 }

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { resolve, promise }
}
function inputValue(wrapper: VueWrapper, suffix: string) {
  return (field(wrapper, suffix).element as HTMLInputElement).value
}


vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

function response(configured = true): OssboxSettingsResponse {
  return {
    mode: 'its_relay', state: 'pending_alignment', applied: false,
    relay: { workbench_name: configured ? NAME : '', workbench_id: 'stable-synthetic-workbench',
      base_url: BASE, configured, key_configured: configured, sm4_key_configured: configured,
      saved: configured,
    },
    restart_required: false, inquiry_configured: false, catalog_configured: false,
  } as unknown as OssboxSettingsResponse
}

const buttons = {
  props: ['disabled', 'loading', 'htmlType'],
  template: '<button :disabled="disabled || loading" :type="htmlType || \'button\'"><slot /></button>',
}

async function page() {
  const wrapper = shallowMount(OssboxConfig, { global: { stubs: { 'a-button': buttons } } })
  pages.push(wrapper)
  await flushPromises()
  return wrapper
}

function field(wrapper: VueWrapper, suffix: string) {
  return wrapper.get(`[data-testid="ossbox-${suffix}"]`)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No real network in collector acceptance')))
  vi.spyOn(ossboxApi, 'get').mockResolvedValue(response())
  vi.spyOn(ossboxApi, 'put').mockResolvedValue(response())
  vi.spyOn(ossboxApi, 'test').mockResolvedValue({ authenticated: true, pending: 0 })
})

afterEach(() => {
  pages.splice(0).forEach(wrapper => wrapper.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  configureHttp({})
  vi.useRealTimers()
})

describe('采集工作台配置', () => {
  it('只提供当前工作台与TLS字段，不提供旧配置切换或STS/OSS输入', async () => {
    const wrapper = await page()
    for (const suffix of ['relay-key', 'relay-sm4-key']) {
      expect((field(wrapper, suffix).element as HTMLInputElement).type).toBe('password')
      expect(inputValue(wrapper, suffix)).toBe('')
    }
    for (const testid of ['reporting-edit-legacy', 'reporting-edit-collector', 'ossbox-sts-url',
      'ossbox-endpoint', 'ossbox-bucket', 'ossbox-allow-http', 'ossbox-auth-key', 'ossbox-workbench']) {
      expect(wrapper.find(`[data-testid="${testid}"]`).exists()).toBe(false)
    }
    for (const testid of ['relay-client-cert', 'relay-client-key', 'relay-ca-file']) {
      expect(wrapper.get(`[data-testid="${testid}"]`).element).toBeInstanceOf(HTMLInputElement)
    }
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
  })

  it('证书路径回读并提交完整当前配置，清空旧路径是显式清空', async () => {
    const current = response()
    Object.assign(current.relay, { client_cert_file: '/synthetic/client.crt', client_key_file: '/synthetic/client.key', ca_file: '/synthetic/ca.crt' })
    vi.mocked(ossboxApi.get).mockResolvedValue(current)
    const wrapper = await page()
    expect((wrapper.get('[data-testid="relay-client-cert"]').element as HTMLInputElement).value).toBe('/synthetic/client.crt')
    await wrapper.get('[data-testid="relay-client-cert"]').setValue('/synthetic/new.crt')
    await wrapper.get('[data-testid="relay-client-key"]').setValue('/synthetic/new.key')
    await wrapper.get('[data-testid="relay-ca-file"]').setValue('')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith({ workbench_name: NAME, base_url: BASE,
      client_cert_file: '/synthetic/new.crt', client_key_file: '/synthetic/new.key', ca_file: '' }, expect.any(AbortSignal))
    expect(ossboxApi.test).not.toHaveBeenCalled()
  })

  it('认证结果必须明确为true，失败正文与秘密不进入页面', async () => {
    vi.mocked(ossboxApi.test).mockResolvedValue({ authenticated: false })
    const wrapper = await page()
    await field(wrapper, 'test').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('读取403锁定所有配置操作', async () => {
    vi.mocked(ossboxApi.get).mockRejectedValue(new ApiFailure('FORBIDDEN', 403, 'synthetic-trace', {
      message: KEY, retryable: false, needs_human: false,
    }))
    const wrapper = await page()
    expect((wrapper.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect((field(wrapper, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(true)
    expect(wrapper.text()).toContain('管理员')
    expect(wrapper.text()).not.toContain(KEY)
  })

  it('实际客户端只向稳定路径JSON体发送当前配置，不自动选旧合同或存浏览器秘密', async () => {
    vi.mocked(ossboxApi.get).mockRestore()
    vi.mocked(ossboxApi.put).mockRestore()
    const store = vi.spyOn(Storage.prototype, 'setItem')
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ ok: true, group: 'ossbox', ...response() })))
    vi.stubGlobal('fetch', fetch)
    const current = await ossboxApi.get()
    expect(current.relay.workbench_name).toBe(NAME)
    expect(current.relay.workbench_id).toBe('stable-synthetic-workbench')
    await ossboxApi.put({ workbench_name: NAME, base_url: DRAFT, key: KEY, sm4_key: SM4 })
    const [url, init] = fetch.mock.calls[1] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/settings/ossbox')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({ workbench_name: NAME, base_url: DRAFT, key: KEY, sm4_key: SM4 })
    expect(JSON.stringify(init.headers)).not.toContain(KEY)
    expect(store).not.toHaveBeenCalled()
    expect('getForContract' in ossboxApi || 'putForContract' in ossboxApi || 'testForContract' in ossboxApi).toBe(false)
  })

  it.each([
    { mode: 'sts', data: { sts_url: 'https://obsolete.invalid', bucket: 'obsolete' } },
    { mode: 'its_relay', relay: { base_url: BASE, config_file: 'config.yaml', configured: true } },
  ])('实际客户端拒绝旧格式响应且不进行格式回退', async old => {
    vi.mocked(ossboxApi.get).mockRestore()
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ ok: true, group: 'ossbox', ...old })))
    vi.stubGlobal('fetch', fetch)
    await expect(ossboxApi.get()).rejects.toThrow('Invalid collector settings response')
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('回读工作台名称和统一公网URL，两个字段可编辑', async () => {
    const wrapper = await page()
    const name = field(wrapper, 'workbench-name').element as HTMLInputElement
    const url = field(wrapper, 'relay-url').element as HTMLInputElement
    expect(name).toBeInstanceOf(HTMLInputElement)
    expect(name.value).toBe(NAME)
    expect(url).toBeInstanceOf(HTMLInputElement)
    expect(url.value).toBe(BASE)
    expect(name.readOnly || name.disabled || url.readOnly || url.disabled).toBe(false)
  })

  it.each(['https://collector.example.invalid/its-relay', 'http://collector.example.invalid'])('旧当前草稿地址可修复但不能继续认证或保存：%s', async oldUrl => {
    const old = response()
    Object.assign(old.relay, { base_url: oldUrl, configured: false })
    vi.mocked(ossboxApi.get).mockResolvedValue(old)
    const wrapper = await page()
    expect(inputValue(wrapper, 'relay-url')).toBe(oldUrl)
    expect((field(wrapper, 'relay-url').element as HTMLInputElement).disabled).toBe(false)
    await field(wrapper, 'test').trigger('click')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(ossboxApi.test).not.toHaveBeenCalled()
    expect(ossboxApi.put).not.toHaveBeenCalled()
    await field(wrapper, 'relay-url').setValue(DRAFT)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith({ workbench_name: NAME, base_url: DRAFT, key: KEY, sm4_key: SM4 }, expect.any(AbortSignal))
    expect(field(wrapper, 'workbench-id').text()).toContain('stable-synthetic-workbench')
  })

  it('首次保存要求非空工作台名称，即使两钥均已填写', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(field(wrapper, 'note').text()).toMatch(/工作台.*名称|名称/)
  })

  it('保存名称和草稿URL，空密码保留原值', async () => {
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue('深圳采集工作台')
    await field(wrapper, 'relay-url').setValue(DRAFT)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith({ workbench_name: '深圳采集工作台', base_url: DRAFT }, expect.any(AbortSignal))
    expect(wrapper.emitted('saved')).toHaveLength(1)
    expect(ossboxApi.test).not.toHaveBeenCalled()
  })

  it('新建配置提交名称URL和两项凭据，不要求导入文件', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-url').setValue(DRAFT)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith({ workbench_name: NAME, base_url: DRAFT, key: KEY, sm4_key: SM4 }, expect.any(AbortSignal))
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect((field(wrapper, 'relay-key').element as HTMLInputElement).value).toBe('')
    expect((field(wrapper, 'relay-sm4-key').element as HTMLInputElement).value).toBe('')
  })

  it.each([['short', false], ['中'.repeat(16), false], ['中'.repeat(5) + 'a', true]])('已补齐名称URL时仍按UTF8字节验证SM4：%s', async (sm4, valid) => {
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-url').setValue(DRAFT)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(sm4)
    await field(wrapper, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledTimes(valid ? 1 : 0)
    if (valid) expect(ossboxApi.test).toHaveBeenCalledWith(expect.objectContaining({ sm4_key: sm4 }), expect.any(AbortSignal))
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it('连接测试携带草稿URL但不保存，不冒称发现工作台或可以发送', async () => {
    const wrapper = await page()
    await field(wrapper, 'relay-url').setValue(DRAFT)
    await field(wrapper, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledWith(expect.objectContaining({ base_url: DRAFT }), expect.any(AbortSignal))
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(wrapper.text()).toMatch(/待.*对齐|尚未.*对齐|未.*发布|尚未.*发布/)
    expect(wrapper.text()).not.toMatch(/已发现|可以发送|可发送|已发布/)
  })

  it('测试期间修改URL后，旧URL晚到成功不得展示', async () => {
    let resolve!: (value: OssboxAuthenticationResult) => void
    vi.mocked(ossboxApi.test).mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = await page()
    await field(wrapper, 'test').trigger('click')
    await field(wrapper, 'relay-url').setValue(DRAFT)
    resolve({ authenticated: true, pending: 0 })
    await flushPromises()
    expect(wrapper.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it('管理会话门禁取消公网草稿测试并清空密码，不接受晚到结果', async () => {
    let resolve!: (value: OssboxAuthenticationResult) => void
    vi.mocked(ossboxApi.test).mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = await page()
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await field(wrapper, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    useSessionStore().authState = 'no_token'
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect((field(wrapper, 'relay-key').element as HTMLInputElement).value).toBe('')
    expect((field(wrapper, 'relay-sm4-key').element as HTMLInputElement).value).toBe('')
    resolve({ authenticated: true, pending: 0 })
    await flushPromises()
    expect(wrapper.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('卸载取消工作台配置保存，晚到结果不触发后续业务入口', async () => {
    let resolve!: (value: OssboxSettingsResponse) => void
    vi.mocked(ossboxApi.put).mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = await page()
    await field(wrapper, 'relay-url').setValue(DRAFT)
    await wrapper.get('form').trigger('submit')
    const signal = vi.mocked(ossboxApi.put).mock.calls[0][1]!
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    resolve(response())
    await flushPromises()
    expect(wrapper.emitted('saved')).toBeUndefined()
  })

  it.each(['', 'ftp://collector.example.invalid', 'https://user:pass@example.invalid/its-relay', 'https://collector.example.invalid/?token=private',
    'http://127.0.0.1', 'http://collector.example.invalid', 'https://collector.example.invalid/its-relay',
    'https://collector.example.invalid/api/files', 'https://collector.example.invalid/other'])('无效URL不发送请求：%s', async (url) => {
    const wrapper = await page()
    await field(wrapper, 'relay-url').setValue(url)
    await wrapper.get('form').trigger('submit')
    await field(wrapper, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(ossboxApi.test).not.toHaveBeenCalled()
  })

  it('默认文案使用公网上报且不展示信箱术语', async () => {
    const wrapper = await page()
    expect(wrapper.text()).toContain('公网上报')
    expect(wrapper.text()).not.toMatch(/信箱/)
  })

  it('已有保存而Vault暂不可用时保留保存状态并给出安全诊断', async () => {
    const offline = response()
    Object.assign(offline.relay!, { saved: true, configured: false, key_configured: false,
      sm4_key_configured: false, credential_error: 'vault_unavailable' })
    vi.mocked(ossboxApi.get).mockResolvedValue(offline)
    const wrapper = await page()
    const status = field(wrapper, 'save-status').text()
    expect(status).toMatch(/已有保存|已保存/)
    expect(status).not.toContain('尚未保存配置')
    expect(wrapper.text()).toMatch(/凭据.*不可用|凭据.*无法读取/)
    expect(wrapper.text()).not.toMatch(/vault_unavailable|已启用|已应用|可发送|可以发送|已发现/)
    expect(wrapper.text()).not.toContain(KEY)
    expect(wrapper.text()).not.toContain(SM4)
  })

  it('没有本地保存配置时不冒称已有保存', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    expect(field(wrapper, 'save-status').text()).toContain('尚未保存配置')
    expect(field(wrapper, 'save-status').text()).not.toContain('已有保存')
  })

  it.each(['vault_unavailable', 'collector_credentials_missing', 'private-provider-reason'])('真实客户端保留saved状态且只保留安全诊断码：%s', async (reason) => {
    vi.mocked(ossboxApi.get).mockRestore()
    configureHttp({})
    const wire = response()
    Object.assign(wire.relay!, { saved: true, configured: false, credential_error: reason })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, group: 'ossbox', ...wire }), { status: 200 })))
    const result = await ossboxApi.get()
    expect(result.relay?.saved).toBe(true)
    expect(result.relay?.configured).toBe(false)
    expect(result.state).toBe('pending_alignment')
    expect(result.relay?.credential_error).toBe(reason === 'private-provider-reason' ? undefined : reason)
  })

  it('公网故障文案也不展示信箱术语或上游密钥', async () => {
    vi.mocked(ossboxApi.test).mockRejectedValue(new ApiFailure('NOT_READY', 503, 'synthetic-trace', {
      message: KEY, reason: 'file_gateway_unavailable', retryable: true, needs_human: false,
    }))
    const wrapper = await page()
    await field(wrapper, 'test').trigger('click')
    await flushPromises()
    expect(field(wrapper, 'note').text()).toMatch(/失败|不可用/)
    expect(wrapper.text()).not.toMatch(/信箱/)
    expect(wrapper.text()).not.toContain(KEY)
  })

  it('网络联通页保留公网上报和邮件中转两条可选入口', async () => {
    vi.spyOn(useSettingsStore(), 'loadAll').mockResolvedValue()
    vi.spyOn(useMailStore(), 'reloadAll').mockResolvedValue()
    vi.spyOn(useMailStore(), 'startPolling').mockImplementation(() => undefined)
    vi.spyOn(useMailStore(), 'stopPolling').mockImplementation(() => undefined)
    const wrapper = shallowMount(MailPage, { global: { stubs: { 'a-button': buttons, QtIcon: true, PageState: true } } })
    pages.push(wrapper)
    await flushPromises()
    await wrapper.get('input[type="radio"][value="oss"]').setValue()
    expect(wrapper.findComponent(OssboxConfig).exists()).toBe(true)
    await wrapper.get('input[type="radio"][value="imap"]').setValue()
    expect(wrapper.text()).toContain('邮件中转')
    expect(wrapper.find('input[placeholder="imap.example.com"]').exists()).toBe(true)
    expect(wrapper.text()).not.toMatch(/信箱/)
  })

  it('超时已取消的请求即使随后返回成功也不得显示绿灯', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await field(wrapper, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    await vi.advanceTimersByTimeAsync(45_000)
    expect(signal.aborted).toBe(true)
    expect(field(wrapper, 'note').text()).toContain('超时')
    expect((field(wrapper, 'test').element as HTMLButtonElement).disabled).toBe(false)
    pending.resolve(GOOD)
    await flushPromises()
    expect(wrapper.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('保存超时提示结果待核实并锁定覆盖，晚到成功不能冒充确认', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const pending = deferred<OssboxSettingsResponse>()
    vi.mocked(ossboxApi.put).mockReturnValue(pending.promise)
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    const signal = vi.mocked(ossboxApi.put).mock.calls[0][1]!
    await vi.advanceTimersByTimeAsync(45_000)
    expect(signal.aborted).toBe(true)
    expect(field(wrapper, 'note').text()).toMatch(/超时.*(读取|核实)/)
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(true)
    pending.resolve(response(true))
    await flushPromises()
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(field(wrapper, 'save-status').text()).toContain('尚未保存配置')
    expect(field(wrapper, 'note').text()).toContain('超时')
  })

  it('400 明确校验拒绝保留输入供修正，也不显示已配置', async () => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new ApiFailure('INVALID_ARGS', 400, 'fake-trace', {
      message: 'invalid configuration', retryable: false, needs_human: false,
    }))
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(inputValue(wrapper, 'relay-key')).toBe(KEY)
    expect(inputValue(wrapper, 'relay-sm4-key')).toBe(SM4)
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(field(wrapper, 'note').text()).toMatch(/格式|检查/)
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(false)
    expect(wrapper.emitted('saved')).toBeUndefined()
  })

  it.each([
    ['断线', new TypeError('Failed to fetch')],
    ['坏 JSON', new SyntaxError('Unexpected end of JSON input')],
    ['无效响应', new Error('Invalid collector settings response')],
    ['HTTP 500', new ApiFailure('INTERNAL', 500, 'fake-trace', { message: 'server error', retryable: true, needs_human: false })],
    ['HTTP 503', new ApiFailure('NOT_READY', 503, 'fake-trace', { message: 'temporarily unavailable', retryable: true, needs_human: false })],
  ])('保存遇到%s时结果待核实，重新读取前禁止再次覆盖', async (_name, error) => {
    vi.mocked(ossboxApi.put).mockRejectedValue(error)
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(field(wrapper, 'note').text()).toMatch(/结果待核实.*重新读取/)
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(true)
    expect((field(wrapper, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect(wrapper.emitted('saved')).toBeUndefined()
    expect(ossboxApi.put).toHaveBeenCalledOnce()
  })

  it('后端已提交但响应断线，重新读取核实已保存后才解锁且不重发 PUT', async () => {
    vi.mocked(ossboxApi.put).mockImplementation(async () => {
      // 假服务器已提交；浏览器只收到断线，下一次 GET 才提供持久化事实。
      vi.mocked(ossboxApi.get).mockResolvedValue(response(true))
      throw new TypeError('Failed to fetch after commit')
    })
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(true)
    const reload = wrapper.findAll('button').find(button => button.text() === '重新读取')
    expect(reload).toBeDefined()
    await reload!.trigger('click')
    await flushPromises()
    expect(ossboxApi.get).toHaveBeenCalledTimes(2)
    expect(ossboxApi.put).toHaveBeenCalledOnce()
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(false)
    expect(field(wrapper, 'save-status').text()).toMatch(/已有保存|已保存/)
    expect(inputValue(wrapper, 'relay-key')).toBe('')
    expect(inputValue(wrapper, 'relay-sm4-key')).toBe('')
    expect(field(wrapper, 'save-status').text()).toMatch(/已有保存|已保存/)
  })

  it.each([401, 403])('保存收到%d权限拒绝后锁定操作，不能继续保存或测试', async (status) => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new ApiFailure('FORBIDDEN', status, 'fake-trace', {
      message: 'permission denied', retryable: false, needs_human: false,
    }))
    vi.mocked(ossboxApi.get).mockResolvedValue(response(false))
    const wrapper = await page()
    await field(wrapper, 'workbench-name').setValue(NAME)
    await field(wrapper, 'relay-key').setValue(KEY)
    await field(wrapper, 'relay-sm4-key').setValue(SM4)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect((field(wrapper, 'save').element as HTMLButtonElement).disabled).toBe(true)
    expect((field(wrapper, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect(wrapper.emitted('saved')).toBeUndefined()
  })


})
