import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import Config from '@/pages/mail/OssboxConfig.vue'
import { ossboxApi } from '@/api/client'
import type { OssboxSettingsResponse } from '@/api/types'
const old = { mode: 'its_relay', legacy_available: true, relay: { config_file: 'config.yaml', base_url: 'https://old.invalid', configured: true, key_configured: true, sm4_key_configured: true } } as unknown as OssboxSettingsResponse
const fresh = { mode: 'its_relay', legacy_available: true, relay: { base_url: 'https://new.invalid', workbench_name: 'New', workbench_id: 'synthetic-id', configured: true, key_configured: true, sm4_key_configured: true } } as unknown as OssboxSettingsResponse
const pages: VueWrapper[] = []
beforeEach(() => {
  setActivePinia(createPinia())
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No external network')))
  vi.spyOn(ossboxApi, 'get').mockResolvedValue(old)
  vi.spyOn(ossboxApi, 'getForContract')
  vi.spyOn(ossboxApi, 'put').mockResolvedValue(old)
  vi.spyOn(ossboxApi, 'putForContract').mockImplementation(async target => target === 'legacy' ? old : fresh)
})
afterEach(() => { for (const page of pages.splice(0)) page.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })
async function mount() {
  const page = shallowMount(Config, { global: { stubs: { 'a-button': { props: ['disabled', 'loading', 'htmlType'], template: '<button :type="htmlType || \'button\'" :disabled="disabled || loading"><slot /></button>' } } } })
  pages.push(page); await flushPromises(); return page
}

describe('configuration read and write target must commit together', () => {
  it.each(['collector', 'legacy'] as const)('failed switch to %s preserves the displayed editor and write destination', async target => {
    const page = await mount()
    if (target === 'legacy') {
      vi.mocked(ossboxApi.getForContract).mockResolvedValueOnce(fresh)
      await page.get('[data-testid="reporting-edit-collector"]').trigger('click'); await flushPromises()
    }
    vi.mocked(ossboxApi.getForContract).mockRejectedValueOnce(new Error('synthetic-private-error'))
    await page.get(`[data-testid="reporting-edit-${target}"]`).trigger('click'); await flushPromises()
    expect(page.text()).not.toContain('synthetic-private-error')
    await page.get('[data-testid="ossbox-relay-key"]').setValue('synthetic-rotation')
    await page.get('form').trigger('submit'); await flushPromises()
    expect(ossboxApi.putForContract).not.toHaveBeenCalledWith(target, expect.anything(), expect.anything())
    if (target === 'collector') {
      expect(ossboxApi.put).toHaveBeenCalledWith({ key: 'synthetic-rotation' }, expect.any(AbortSignal))
      expect((page.get('[data-testid="ossbox-relay-url"]').element as HTMLInputElement).readOnly).toBe(true)
    } else {
      expect(ossboxApi.putForContract).toHaveBeenCalledWith('collector', { base_url: 'https://new.invalid', workbench_name: 'New', key: 'synthetic-rotation' }, expect.any(AbortSignal))
      expect((page.get('[data-testid="ossbox-relay-url"]').element as HTMLInputElement).readOnly).toBe(false)
    }
  })

  it('a timed-out read and its late response cannot change the displayed editor or subsequent write target', async () => {
    vi.useFakeTimers()
    const page = await mount()
    let resolve!: (value: OssboxSettingsResponse) => void
    vi.mocked(ossboxApi.getForContract).mockImplementationOnce(async (_target, signal) => new Promise<OssboxSettingsResponse>(yes => { resolve = yes; signal?.addEventListener('abort', () => {}, { once: true }) }))
    await page.get('[data-testid="reporting-edit-collector"]').trigger('click')
    await vi.advanceTimersByTimeAsync(20_001); await flushPromises()
    await page.get('[data-testid="ossbox-relay-key"]').setValue('synthetic-after-timeout')
    await page.get('form').trigger('submit'); await flushPromises()
    expect(ossboxApi.putForContract).not.toHaveBeenCalled()
    expect(ossboxApi.put).toHaveBeenCalledWith({ key: 'synthetic-after-timeout' }, expect.any(AbortSignal))
    resolve(fresh); await flushPromises()
    expect(page.find('[data-testid="ossbox-workbench-name"]').exists()).toBe(false)
    expect((page.get('[data-testid="ossbox-relay-url"]').element as HTMLInputElement).readOnly).toBe(true)
  })
})
