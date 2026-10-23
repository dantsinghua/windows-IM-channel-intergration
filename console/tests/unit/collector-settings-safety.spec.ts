/** Current collector safety contracts migrated from retired settings editors. Synthetic only. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OssboxConfig from '@/pages/mail/OssboxConfig.vue'
import { ossboxApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import type { OssboxAuthenticationResult, OssboxSettingsResponse } from '@/api/types'

const KEY = 'synthetic-current-safety-key'
const SM4 = '0123456789abcdef'
const pages: VueWrapper[] = []
function response(configured = false): OssboxSettingsResponse {
  return { mode: 'its_relay', state: 'pending_alignment', applied: false,
    relay: { base_url: 'https://collector.example.invalid', workbench_name: 'Synthetic collector',
      workbench_id: 'synthetic-stable-id', configured, saved: configured,
      key_configured: configured, sm4_key_configured: configured },
    restart_required: false }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}
function field(p: VueWrapper, name: string) { return p.get(`[data-testid="ossbox-${name}"]`) }
function value(p: VueWrapper, name: string) { return (field(p, name).element as HTMLInputElement).value }
function disabled(p: VueWrapper, name: string) { return (field(p, name).element as HTMLButtonElement).disabled }
async function mount() {
  const p = shallowMount(OssboxConfig, { global: { stubs: {
    'a-button': { props: ['disabled', 'loading', 'htmlType'], template: '<button :type="htmlType || \'button\'" :disabled="disabled || loading"><slot /></button>' },
  } } })
  pages.push(p)
  await flushPromises()
  return p
}
async function fill(p: VueWrapper) {
  await field(p, 'relay-key').setValue(KEY)
  await field(p, 'relay-sm4-key').setValue(SM4)
}
beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Real network forbidden')))
  vi.spyOn(ossboxApi, 'get').mockResolvedValue(response())
  vi.spyOn(ossboxApi, 'put').mockResolvedValue(response(true))
  vi.spyOn(ossboxApi, 'test').mockResolvedValue({ authenticated: true, pending: 0 })
})
afterEach(() => { pages.splice(0).forEach(p => p.unmount()); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

describe('current collector request safety', () => {
  it('aborts timed out authentication and rejects its late success', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const pending = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.test).mockReturnValue(pending.promise)
    const p = await mount(); await fill(p); await field(p, 'test').trigger('click')
    const signal = vi.mocked(ossboxApi.test).mock.calls[0][1]!
    await vi.advanceTimersByTimeAsync(45_000)
    expect(signal.aborted).toBe(true)
    expect(field(p, 'note').text()).toContain('超时')
    expect(disabled(p, 'test')).toBe(false)
    pending.resolve({ authenticated: true, pending: 0 }); await flushPromises()
    expect(p.find('[data-testid="ossbox-auth-result"]').exists()).toBe(false)
    expect(ossboxApi.put).not.toHaveBeenCalled()
  })

  it('locks uncertain timed out save and ignores its late success', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const pending = deferred<OssboxSettingsResponse>()
    vi.mocked(ossboxApi.put).mockReturnValue(pending.promise)
    const p = await mount(); await fill(p); await p.get('form').trigger('submit')
    const signal = vi.mocked(ossboxApi.put).mock.calls[0][1]!
    await vi.advanceTimersByTimeAsync(45_000)
    expect(signal.aborted).toBe(true)
    expect(field(p, 'note').text()).toMatch(/超时.*核实/)
    expect(disabled(p, 'save')).toBe(true)
    expect(disabled(p, 'test')).toBe(true)
    pending.resolve(response(true)); await flushPromises()
    expect(p.emitted('saved')).toBeUndefined()
    expect(field(p, 'save-status').text()).toContain('尚未保存配置')
    expect(field(p, 'note').text()).toContain('超时')
  })

  it.each([
    ['disconnect', new TypeError(`Failed to fetch ${KEY}`)],
    ['bad JSON', new SyntaxError(`Unexpected JSON ${SM4}`)],
    ['invalid response', new Error(`Invalid OSS settings response ${KEY}`)],
    ['HTTP 500', new ApiFailure('INTERNAL', 500, 'synthetic-trace', { message: KEY, retryable: true, needs_human: false })],
    ['HTTP 503', new ApiFailure('NOT_READY', 503, 'synthetic-trace', { message: SM4, retryable: true, needs_human: false })],
  ])('requires readback before overwriting an uncertain %s save', async (_label, error) => {
    vi.mocked(ossboxApi.put).mockRejectedValue(error)
    const p = await mount(); await fill(p); await p.get('form').trigger('submit'); await flushPromises()
    expect(field(p, 'note').text()).toMatch(/结果待核实.*重新读取/)
    expect(disabled(p, 'save')).toBe(true); expect(disabled(p, 'test')).toBe(true)
    expect((p.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    await p.get('form').trigger('submit'); await field(p, 'test').trigger('click'); await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledOnce(); expect(ossboxApi.test).not.toHaveBeenCalled()
    expect(p.emitted('saved')).toBeUndefined()
    expect(p.text()).not.toContain(KEY); expect(p.text()).not.toContain(SM4)
  })

  it('confirms committed save by GET before unlocking and never resends PUT', async () => {
    vi.mocked(ossboxApi.put).mockImplementation(async () => {
      vi.mocked(ossboxApi.get).mockResolvedValue(response(true))
      throw new TypeError('synthetic disconnect after commit')
    })
    const p = await mount(); await fill(p); await p.get('form').trigger('submit'); await flushPromises()
    expect(disabled(p, 'save')).toBe(true)
    const reload = p.findAll('button').find(b => b.text() === '重新读取')!
    expect(reload.exists()).toBe(true); await reload.trigger('click'); await flushPromises()
    expect(ossboxApi.get).toHaveBeenCalledTimes(2); expect(ossboxApi.put).toHaveBeenCalledOnce()
    expect(disabled(p, 'save')).toBe(false)
    expect(field(p, 'save-status').text()).toMatch(/已有保存|已保存/)
    expect(value(p, 'relay-key')).toBe(''); expect(value(p, 'relay-sm4-key')).toBe('')
    expect(p.emitted('saved')).toBeUndefined()
  })

  it('failed readback keeps uncertain save locked without retrying writes', async () => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new TypeError('synthetic disconnect'))
    const p = await mount(); await fill(p); await p.get('form').trigger('submit'); await flushPromises()
    vi.mocked(ossboxApi.get).mockRejectedValue(new TypeError('synthetic read unavailable'))
    await p.findAll('button').find(b => b.text() === '重新读取')!.trigger('click'); await flushPromises()
    expect(disabled(p, 'save')).toBe(true); expect(disabled(p, 'test')).toBe(true)
    expect(ossboxApi.put).toHaveBeenCalledOnce(); expect(p.emitted('saved')).toBeUndefined()
  })

  it('GET 403 keeps the form locked and does not attempt writes or tests', async () => {
    vi.mocked(ossboxApi.get).mockRejectedValue(new ApiFailure('FORBIDDEN', 403, 'synthetic-trace', { message: KEY, retryable: false, needs_human: false }))
    const p = await mount()
    expect((p.get('fieldset').element as HTMLFieldSetElement).disabled).toBe(true)
    expect(disabled(p, 'save')).toBe(true); expect(disabled(p, 'test')).toBe(true)
    expect(p.text()).toContain('仅管理员'); expect(p.text()).not.toContain(KEY)
    expect(ossboxApi.put).not.toHaveBeenCalled(); expect(ossboxApi.test).not.toHaveBeenCalled()
  })

  it.each([401, 403])('save HTTP %s locks later writes and tests', async status => {
    vi.mocked(ossboxApi.put).mockRejectedValue(new ApiFailure('FORBIDDEN', status, 'synthetic-trace', { message: KEY, retryable: false, needs_human: false }))
    const p = await mount(); await fill(p); await p.get('form').trigger('submit'); await flushPromises()
    expect(disabled(p, 'save')).toBe(true); expect(disabled(p, 'test')).toBe(true)
    await p.get('form').trigger('submit'); await field(p, 'test').trigger('click'); await flushPromises()
    expect(ossboxApi.put).toHaveBeenCalledOnce(); expect(ossboxApi.test).not.toHaveBeenCalled()
    expect(p.emitted('saved')).toBeUndefined(); expect(p.text()).not.toContain(KEY)
  })

  it.each(['save', 'test'] as const)('unmount aborts %s and discards late response', async operation => {
    const saving = deferred<OssboxSettingsResponse>(); const testing = deferred<OssboxAuthenticationResult>()
    vi.mocked(ossboxApi.put).mockReturnValue(saving.promise); vi.mocked(ossboxApi.test).mockReturnValue(testing.promise)
    const p = await mount(); await fill(p)
    if (operation === 'save') await p.get('form').trigger('submit')
    else await field(p, 'test').trigger('click')
    const signal = operation === 'save' ? vi.mocked(ossboxApi.put).mock.calls[0][1]! : vi.mocked(ossboxApi.test).mock.calls[0][1]!
    p.unmount(); expect(signal.aborted).toBe(true)
    saving.resolve(response(true)); testing.resolve({ authenticated: true, pending: 0 }); await flushPromises()
    expect(p.emitted('saved')).toBeUndefined()
  })
})
