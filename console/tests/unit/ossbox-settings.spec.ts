/** STS 设置只使用合成值与假 API，观察表单和请求，不访问真实认证服务。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OssboxConfig from '@/pages/mail/OssboxConfig.vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure, configureHttp } from '@/api/http'
import type { OssboxAuthenticationResult, OssboxSettingsResponse } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const AUTH = 'ui-test-business-secret'
const SM4 = 'fEdCbA9876543210'
const STS_URL = 'https://sts.example.invalid/v1/api/sts'
const PUBLIC = { sts_url: STS_URL, allow_http: false, endpoint: '', bucket: '', workbench_id: '' }
const GOOD: OssboxAuthenticationResult = { authenticated: true, expiration: '2030-01-01T00:00:00Z' }
const wrappers: VueWrapper[] = []

function settings(configured = false): OssboxSettingsResponse {
  return {
    data: { ...PUBLIC, sts_url: configured ? STS_URL : '', auth_key_configured: configured,
      sm4_key_configured: configured, saved: configured },
    restart_required: configured, inquiry_configured: false, catalog_configured: false,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { resolve, reject, promise }
}

function find(page: VueWrapper, suffix: string) { return page.get(`[data-testid="ossbox-${suffix}"]`) }
function value(page: VueWrapper, suffix: string) { return (find(page, suffix).element as HTMLInputElement).value }

async function mount() {
  const page = shallowMount(OssboxConfig, { global: { stubs: {
    'a-button': { props: ['disabled', 'loading', 'htmlType'],
      template: '<button :type="htmlType || \'button\'" :disabled="disabled || loading"><slot /></button>' },
  } } })
  wrappers.push(page)
  await flushPromises()
  return page
}

async function fill(page: VueWrapper) {
  await find(page, 'sts-url').setValue(STS_URL)
  await find(page, 'auth-key').setValue(AUTH)
  await find(page, 'sm4-key').setValue(SM4)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected network access')))
  vi.spyOn(ossboxApi, 'get').mockResolvedValue(settings())
  vi.spyOn(ossboxApi, 'put').mockResolvedValue(settings(true))
  vi.spyOn(ossboxApi, 'test').mockResolvedValue(GOOD)
  vi.spyOn(Storage.prototype, 'setItem')
})

afterEach(() => {
  for (const page of wrappers.splice(0)) page.unmount()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('公网上报 STS 配置表单', () => {
  it('初装所有地址和凭据为空，两个凭据输入均为密码框', async () => {
    const page = await mount()
    for (const field of ['sts-url', 'auth-key', 'sm4-key', 'endpoint', 'bucket', 'workbench']) expect(value(page, field)).toBe('')
    for (const field of ['auth-key', 'sm4-key']) expect(find(page, field).attributes('type')).toBe('password')
    expect(page.text()).not.toContain('synthetic-business-key')
    expect(page.text()).not.toContain('0123456789abcdef')
    expect(page.text()).toContain('认证通过不代表业务已启用')
  })

  it('保存按契约发送输入，成功后清空密码并提示重启和已配置', async () => {
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledOnce()
    expect(ossboxApi.put).toHaveBeenCalledWith({ ...PUBLIC, auth_key: AUTH, sm4_key: SM4 }, expect.any(AbortSignal))
    expect(value(page, 'auth-key')).toBe('')
    expect(value(page, 'sm4-key')).toBe('')
    expect(find(page, 'auth-status').text()).toContain('已配置')
    expect(find(page, 'sm4-status').text()).toContain('已配置')
    expect(page.text()).toContain('重启 Agent 后生效')
    expect(Storage.prototype.setItem).not.toHaveBeenCalled()
    expect(ossboxApi.test).not.toHaveBeenCalled()
  })

  it('已配置密码留空只发送公开字段，不发送空密码或占位符', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue(settings(true))
    const page = await mount()
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith(PUBLIC, expect.any(AbortSignal))
  })

  it('首次保存缺少密码不发请求', async () => {
    const page = await mount()
    await find(page, 'sts-url').setValue(STS_URL)
    await page.get('form').trigger('submit')
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(page.text()).toContain('请填写业务口令')
  })

  it('认证测试只调用测试接口，并显示业务能力仍未启用', async () => {
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledWith({ ...PUBLIC, auth_key: AUTH, sm4_key: SM4 }, expect.any(AbortSignal))
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(find(page, 'auth-result').text()).toContain('认证通过')
    expect(find(page, 'business-status').text()).toContain('尚未启用')
    expect(Storage.prototype.setItem).not.toHaveBeenCalled()
  })

  it('修改任一输入立即清除上一份认证成功结论', async () => {
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(true)
    await find(page, 'auth-key').setValue('new-secret')
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('请求发出后编辑草稿，旧请求成功不得把新草稿标为通过', async () => {
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await find(page, 'auth-key').setValue('changed-after-submit')
    pending.resolve(GOOD)
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it.each([new Error(`failed ${AUTH}`), new ApiFailure('NOT_READY', 503, 'fake-trace', {
    message: `upstream ${SM4}`, retryable: true, needs_human: false,
  })])('失败后不绿且不显示上游异常里的凭据', async (error) => {
    vi.mocked(ossboxApi.test).mockRejectedValue(error)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
    expect(find(page, 'note').text()).toContain('失败')
    expect(page.text()).not.toContain(AUTH)
    expect(page.text()).not.toContain(SM4)
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(false)
  })

  it('服务端未明确 authenticated=true 时不显示认证通过', async () => {
    vi.mocked(ossboxApi.test).mockResolvedValue({ ...GOOD, authenticated: false })
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
    expect(find(page, 'note').text()).toContain('失败')
  })

  it('口令已输入但尚未保存时明确标示草稿状态', async () => {
    const page = await mount()
    await fill(page)
    expect(find(page, 'auth-status').text()).toContain('已输入')
    expect(find(page, 'auth-status').text()).toContain('尚未保存')
    expect(find(page, 'sm4-status').text()).toContain('已输入')
    expect(find(page, 'sm4-status').text()).toContain('尚未保存')
    expect(find(page, 'save-status').text()).toContain('尚未保存配置')
  })

  it('STS 桶名不匹配时显示诊断建议，不回显服务端敏感信息', async () => {
    vi.mocked(ossboxApi.test).mockRejectedValue(new ApiFailure('NOT_READY', 503, 'fake-trace', {
      message: 'sensitive ' + AUTH + SM4, reason: 'sts_http_401',
      retryable: true, needs_human: false,
    }))
    const page = await mount()
    await fill(page)
    await find(page, 'bucket').setValue('synthetic-wrong-bucket')
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledWith(
      expect.objectContaining({ bucket: 'synthetic-wrong-bucket' }), expect.any(AbortSignal))
    expect(find(page, 'note').text()).toContain('STS 返回 401')
    expect(find(page, 'note').text()).toContain('业务口令')
    expect(find(page, 'note').text()).toContain('SM4')
    expect(page.text()).not.toContain(AUTH)
    expect(page.text()).not.toContain(SM4)
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it.each([
    ['sts_http_401', 'STS 返回 401'],
    ['sts_unavailable', '无法连接 STS'],
    ['sts_test_timeout', '请求超时'],
    ['sts_response_invalid', '凭据字段'],
  ])('STS 错误原因 %s 对应精确提示', async (reason, expected) => {
    vi.mocked(ossboxApi.test).mockRejectedValue(new ApiFailure('NOT_READY', 503, 'fake-trace', {
      message: 'sensitive ' + AUTH, reason, retryable: true, needs_human: false,
    }))
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(find(page, 'note').text()).toContain(expected)
    expect(page.text()).not.toContain(AUTH)
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('失去管理员读取权限时锁定写入与测试', async () => {
    vi.mocked(ossboxApi.get).mockRejectedValue(new ApiFailure('FORBIDDEN', 403, 'fake-trace', {
      message: 'forbidden', retryable: false, needs_human: false,
    }))
    const page = await mount()
    expect((page.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect(page.text()).toContain('仅管理员')
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it('会话失效取消在途请求、擦除密码且丢弃晚到成功', async () => {
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    useSessionStore().authState = 'no_token'
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(value(page, 'auth-key')).toBe('')
    expect(value(page, 'sm4-key')).toBe('')
    pending.resolve(GOOD)
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('超时已取消的请求即使随后返回成功也不得显示绿灯', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    await vi.advanceTimersByTimeAsync(45_000)
    expect(signal.aborted).toBe(true)
    expect(find(page, 'note').text()).toContain('超时')
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(false)
    pending.resolve(GOOD)
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('保存超时提示结果待核实并锁定覆盖，晚到成功不能冒充确认', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const pending = deferred<OssboxSettingsResponse>()
    vi.mocked(ossboxApi.put).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    const signal = vi.mocked(ossboxApi.put).mock.calls[0][1]!
    await vi.advanceTimersByTimeAsync(45_000)
    expect(signal.aborted).toBe(true)
    expect(find(page, 'note').text()).toMatch(/超时.*(读取|核实)/)
    expect((find(page, 'save').element as HTMLButtonElement).disabled).toBe(true)
    pending.resolve(settings(true))
    await flushPromises()
    expect(page.emitted('saved')).toBeUndefined()
    expect(find(page, 'save-status').text()).toContain('尚未保存配置')
    expect(find(page, 'note').text()).toContain('超时')
  })

  it('400 明确校验拒绝保留输入供修正，也不显示已配置', async () => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new ApiFailure('INVALID_ARGS', 400, 'fake-trace', {
      message: 'invalid configuration', retryable: false, needs_human: false,
    }))
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(value(page, 'auth-key')).toBe(AUTH)
    expect(value(page, 'sm4-key')).toBe(SM4)
    expect(find(page, 'auth-status').text()).toContain('尚未保存')
    expect(find(page, 'note').text()).toMatch(/格式|检查/)
    expect((find(page, 'save').element as HTMLButtonElement).disabled).toBe(false)
    expect(page.emitted('saved')).toBeUndefined()
  })

  it.each([
    ['断线', new TypeError('Failed to fetch')],
    ['坏 JSON', new SyntaxError('Unexpected end of JSON input')],
    ['无效响应', new Error('Invalid OSS settings response')],
    ['HTTP 500', new ApiFailure('INTERNAL', 500, 'fake-trace', { message: 'server error', retryable: true, needs_human: false })],
    ['HTTP 503', new ApiFailure('NOT_READY', 503, 'fake-trace', { message: 'temporarily unavailable', retryable: true, needs_human: false })],
  ])('保存遇到%s时结果待核实，重新读取前禁止再次覆盖', async (_name, error) => {
    vi.mocked(ossboxApi.put).mockRejectedValue(error)
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(find(page, 'note').text()).toMatch(/结果待核实.*重新读取/)
    expect((find(page, 'save').element as HTMLButtonElement).disabled).toBe(true)
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect((page.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect(page.emitted('saved')).toBeUndefined()
    expect(ossboxApi.put).toHaveBeenCalledOnce()
  })

  it('后端已提交但响应断线，重新读取核实已保存后才解锁且不重发 PUT', async () => {
    vi.mocked(ossboxApi.put).mockImplementation(async () => {
      // 假服务器已提交；浏览器只收到断线，下一次 GET 才提供持久化事实。
      vi.mocked(ossboxApi.get).mockResolvedValue(settings(true))
      throw new TypeError('Failed to fetch after commit')
    })
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect((find(page, 'save').element as HTMLButtonElement).disabled).toBe(true)
    const reload = page.findAll('button').find(button => button.text() === '重新读取')
    expect(reload).toBeDefined()
    await reload!.trigger('click')
    await flushPromises()
    expect(ossboxApi.get).toHaveBeenCalledTimes(2)
    expect(ossboxApi.put).toHaveBeenCalledOnce()
    expect((find(page, 'save').element as HTMLButtonElement).disabled).toBe(false)
    expect(find(page, 'save-status').text()).toContain('已有保存配置')
    expect(value(page, 'auth-key')).toBe('')
    expect(value(page, 'sm4-key')).toBe('')
    expect(find(page, 'auth-status').text()).toContain('已配置')
  })

  it.each([401, 403])('保存收到%d权限拒绝后锁定操作，不能继续保存或测试', async (status) => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new ApiFailure('FORBIDDEN', status, 'fake-trace', {
      message: 'permission denied', retryable: false, needs_human: false,
    }))
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect((find(page, 'save').element as HTMLButtonElement).disabled).toBe(true)
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect((page.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect(page.emitted('saved')).toBeUndefined()
  })

  it('离开页面会取消在途请求', async () => {
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    page.unmount()
    expect(signal.aborted).toBe(true)
    pending.resolve(GOOD)
    await flushPromises()
    expect(page.emitted('saved')).toBeUndefined()
  })

  it.each([['中'.repeat(16), false], ['中'.repeat(5) + 'a', true]])('SM4 按 UTF-8 字节长度校验 %s', async (key, valid) => {
    const page = await mount()
    await fill(page)
    await find(page, 'sm4-key').setValue(key)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledTimes(valid ? 1 : 0)
  })

  it('非回环 HTTP 必须显式允许才能提交', async () => {
    const page = await mount()
    await fill(page)
    await find(page, 'sts-url').setValue('http://mq.example.invalid/v1/api/sts')
    await find(page, 'test').trigger('click')
    expect(ossboxApi.test).not.toHaveBeenCalled()
    await find(page, 'allow-http').setValue(true)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledOnce()
  })
})

describe('公网上报 STS API 的实际 HTTP 契约', () => {
  it('GET 保留重启/业务配置标志，PUT 密钥只在请求体且不进入存储', async () => {
    vi.restoreAllMocks()
    configureHttp({})
    const wire = { ok: true, group: 'ossbox', ...settings(true) }
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(wire), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    const response = await ossboxApi.get()
    expect(response).toEqual(settings(true))
    fetch.mockResolvedValue(new Response(JSON.stringify(wire), { status: 200 }))
    await ossboxApi.put({ ...PUBLIC, auth_key: AUTH, sm4_key: SM4 })
    const [url, init] = fetch.mock.calls[1]
    expect(url).toBe('/api/v1/settings/ossbox')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(init.body)).toEqual({ ...PUBLIC, auth_key: AUTH, sm4_key: SM4 })
    expect(JSON.stringify(init.headers)).not.toContain(AUTH)
  })
})
