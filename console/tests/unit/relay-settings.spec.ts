/** V2.2 公网认证：合成密码与假 HTTP，独立观察公开 DTO、表单和生命周期。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OssboxConfig from '@/pages/mail/OssboxConfig.vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure, configureHttp } from '@/api/http'
import type { OssboxAuthenticationResult, OssboxSettingsInput, OssboxSettingsResponse } from '@/api/types'
import { useSessionStore } from '@/stores/session'

const KEY = 'synthetic-ui-relay-key'
const SM4 = '0123456789abcdef'
const BASE = 'https://relay.example.invalid/its-relay'
const FIELDS = { key: KEY, sm4_key: SM4 }
const GOOD = { authenticated: true } as OssboxAuthenticationResult
const wrappers: VueWrapper[] = []

function settings(configured = false, restart = false): OssboxSettingsResponse {
  return {
    mode: 'its_relay', relay: { base_url: BASE, config_file: 'config.yaml', configured,
      key_configured: configured, sm4_key_configured: configured },
    restart_required: restart, inquiry_configured: false, catalog_configured: false,
  } as unknown as OssboxSettingsResponse
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((yes) => { resolve = yes })
  return { resolve, promise }
}

function find(page: VueWrapper, suffix: string) { return page.get(`[data-testid="ossbox-${suffix}"]`) }
function input(page: VueWrapper, suffix: string) { return find(page, suffix).element as HTMLInputElement }

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
  await find(page, 'relay-key').setValue(KEY)
  await find(page, 'relay-sm4-key').setValue(SM4)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Unexpected real network')))
  vi.spyOn(ossboxApi, 'get').mockResolvedValue(settings())
  vi.spyOn(ossboxApi, 'put').mockResolvedValue(settings(true, true))
  vi.spyOn(ossboxApi, 'test').mockResolvedValue(GOOD)
  vi.spyOn(Storage.prototype, 'setItem')
})

afterEach(() => {
  for (const page of wrappers.splice(0)) page.unmount()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('its-relay 公网认证字段', () => {
  it('只显示两个空密码字段与部署URL，没有旧STS/OSS表单或文件导入', async () => {
    const page = await mount()
    for (const suffix of ['relay-key', 'relay-sm4-key']) {
      expect(input(page, suffix).type).toBe('password')
      expect(input(page, suffix).value).toBe('')
    }
    const url = find(page, 'relay-url')
    if (url.element instanceof HTMLInputElement) {
      expect(url.element.value).toBe(BASE)
      expect(url.element.readOnly || url.element.disabled).toBe(true)
    } else expect(url.text()).toContain(BASE)
    for (const old of ['sts-url', 'auth-key', 'sm4-key', 'endpoint', 'bucket', 'workbench', 'allow-http']) {
      expect(page.find(`[data-testid="ossbox-${old}"]`).exists()).toBe(false)
    }
    expect(page.find('input[type="file"]').exists()).toBe(false)
    expect(page.text()).not.toMatch(/STS|目标桶|终端编号/)
  })

  it('保存只提交两个字段，成功清空密码，不写浏览器存储或自动测试', async () => {
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith(FIELDS, expect.any(AbortSignal))
    expect(input(page, 'relay-key').value).toBe('')
    expect(input(page, 'relay-sm4-key').value).toBe('')
    expect(page.emitted('saved')).toHaveLength(1)
    expect(page.text()).toContain('重启')
    expect(Storage.prototype.setItem).not.toHaveBeenCalled()
    expect(ossboxApi.test).not.toHaveBeenCalled()
    expect(page.text()).not.toContain(KEY)
    expect(page.text()).not.toContain(SM4)
  })

  it('已配置时留空测试只发空对象，不发送占位符或旧字段', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue(settings(true))
    const page = await mount()
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledWith({}, expect.any(AbortSignal))
    expect(find(page, 'auth-result').text()).toContain('通过')
    expect(page.text()).not.toContain('STS')
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it('已配置时仅改一项保存不发送空密钥', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue(settings(true))
    const page = await mount()
    await find(page, 'relay-key').setValue(KEY)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledWith({ key: KEY }, expect.any(AbortSignal))
  })

  it('草稿测试只提交两钥，不保存，也不显示输入凭据', async () => {
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(ossboxApi.test).toHaveBeenCalledWith(FIELDS, expect.any(AbortSignal))
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(find(page, 'auth-result').text()).toContain('通过')
    expect(page.text()).not.toContain(KEY)
    expect(page.text()).not.toContain(SM4)
    expect(Storage.prototype.setItem).not.toHaveBeenCalled()
  })

  it('未配置且缺少一项时不发保存或测试请求', async () => {
    const page = await mount()
    await find(page, 'relay-key').setValue(KEY)
    await page.get('form').trigger('submit')
    await find(page, 'test').trigger('click')
    expect(ossboxApi.put).not.toHaveBeenCalled()
    expect(ossboxApi.test).not.toHaveBeenCalled()
  })

  it.each([['中'.repeat(16), false], ['中'.repeat(5) + 'a', true]])(
    'SM4按UTF8字节验证 %s', async (sm4, valid) => {
      const page = await mount()
      await fill(page)
      await find(page, 'relay-sm4-key').setValue(sm4)
      await find(page, 'test').trigger('click')
      await flushPromises()
      expect(ossboxApi.test).toHaveBeenCalledTimes(valid ? 1 : 0)
    })

  it.each([new Error(`upstream ${KEY} ${SM4}`), new ApiFailure('NOT_READY', 503, 'test-trace', {
    message: `private ${KEY} ${SM4}`, reason: 'file_auth_rejected', retryable: true, needs_human: false,
  })])('连接失败无假成功，忽略上游错误中的凭据', async (error) => {
    vi.mocked(ossboxApi.test).mockRejectedValue(error)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
    expect(find(page, 'note').text()).toMatch(/失败|拒绝/)
    expect(page.text()).not.toContain(KEY)
    expect(page.text()).not.toContain(SM4)
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(false)
  })

  it('authenticated不是true时不显示认证通过', async () => {
    vi.mocked(ossboxApi.test).mockResolvedValue({ authenticated: false } as OssboxAuthenticationResult)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('编辑草稿后丢弃旧测试晚到成功', async () => {
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    await find(page, 'relay-key').setValue('changed-after-submit')
    pending.resolve(GOOD)
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('门禁触发即清空两钥并取消请求，晚到成功不展示', async () => {
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await find(page, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    useSessionStore().authState = 'no_token'
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(input(page, 'relay-key').value).toBe('')
    expect(input(page, 'relay-sm4-key').value).toBe('')
    pending.resolve(GOOD)
    await flushPromises()
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })

  it('卸载取消保存请求，晚到响应不得发saved', async () => {
    const pending = deferred<OssboxSettingsResponse>()
    vi.mocked(ossboxApi.put).mockReturnValue(pending.promise)
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    const signal = vi.mocked(ossboxApi.put).mock.calls[0][1]!
    page.unmount()
    expect(signal.aborted).toBe(true)
    pending.resolve(settings(true, true))
    await flushPromises()
    expect(page.emitted('saved')).toBeUndefined()
  })

  it('读取返回403时锁定写入和连接测试', async () => {
    vi.mocked(ossboxApi.get).mockRejectedValue(new ApiFailure('FORBIDDEN', 403, 'test-trace', {
      message: 'forbidden', retryable: false, needs_human: false,
    }))
    const page = await mount()
    expect((page.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect((find(page, 'test').element as HTMLButtonElement).disabled).toBe(true)
    expect(page.text()).toContain('管理员')
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it('保存失败不发saved，也不把密码标为已配置', async () => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new ApiFailure('INVALID_ARGS', 400, 'test-trace', {
      message: `private ${KEY}`, retryable: false, needs_human: false,
    }))
    const page = await mount()
    await fill(page)
    await page.get('form').trigger('submit')
    await flushPromises()
    expect(page.emitted('saved')).toBeUndefined()
    expect(page.text()).not.toContain('配置已保存')
    expect(page.text()).not.toContain(KEY)
  })

  it('已配置但applied=false时明确显示采集服务尚未应用', async () => {
    vi.mocked(ossboxApi.get).mockResolvedValue({ ...settings(true), applied: false })
    const page = await mount()
    expect(page.text()).toContain('配置尚未由采集服务应用。')
    expect(page.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
  })
})

describe('its-relay 实际HTTP外观', () => {
  it.each([true, false])('GET保留服务端applied=%s，不将已配置等同于已应用', async (applied) => {
    vi.restoreAllMocks()
    configureHttp({})
    const wire = { ok: true, group: 'ossbox', ...settings(true), applied }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify(wire), { status: 200 })))
    expect((await ossboxApi.get()).applied).toBe(applied)
  })

  it('GET保留mode/relay元数据，PUT两钥只进入JSON请求体', async () => {
    vi.restoreAllMocks()
    configureHttp({})
    const wire = { ok: true, group: 'ossbox', ...settings(true, true) }
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(wire), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    const response = await ossboxApi.get()
    expect(response).toEqual(expect.objectContaining(settings(true, true)))
    expect(JSON.stringify(response)).not.toContain(KEY)
    expect(JSON.stringify(response)).not.toContain(SM4)
    await ossboxApi.put(FIELDS as unknown as OssboxSettingsInput)
    const [url, init] = fetch.mock.calls[1] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/settings/ossbox')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual(FIELDS)
    expect(JSON.stringify(init.headers)).not.toContain(KEY)
    expect(url).not.toContain(KEY)
  })
})
