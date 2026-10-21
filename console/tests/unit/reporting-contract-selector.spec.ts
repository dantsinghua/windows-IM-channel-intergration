import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OssboxConfig from '@/pages/mail/OssboxConfig.vue'
import { ossboxApi } from '@/api/client'
import { configureHttp } from '@/api/http'
import type { OssboxSettingsResponse } from '@/api/types'

const base = 'https://synthetic-relay.invalid/its-relay'
const legacy = { mode: 'its_relay', legacy_available: true, relay: { base_url: base, config_file: 'config.yaml', configured: true, key_configured: true, sm4_key_configured: true }, restart_required: false, inquiry_configured: false, catalog_configured: false } as unknown as OssboxSettingsResponse
const modern = { ...legacy, state: 'pending_alignment', applied: false, relay: { base_url: base, workbench_name: 'Synthetic Workbench', workbench_id: 'synthetic-wb', configured: true, key_configured: true, sm4_key_configured: true } } as OssboxSettingsResponse
const pages: VueWrapper[] = []
beforeEach(() => { setActivePinia(createPinia()); configureHttp({}); vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No network allowed'))) })
afterEach(() => { for (const page of pages.splice(0)) page.unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('explicit independent configuration editors', () => {
  it('opens editable workstation fields from legacy mode and clears unsaved secrets without changing runtime', async () => {
    vi.spyOn(ossboxApi, 'get').mockResolvedValue(legacy)
    vi.spyOn(ossboxApi, 'getForContract').mockImplementation(async contract => contract === 'legacy' ? legacy : modern)
    const put = vi.spyOn(ossboxApi, 'putForContract').mockResolvedValue(modern)
    const test = vi.spyOn(ossboxApi, 'testForContract')
    const page = shallowMount(OssboxConfig, { global: { stubs: { 'a-button': { props: ['disabled', 'loading', 'htmlType'], template: '<button :type="htmlType || \'button\'" :disabled="disabled || loading"><slot /></button>' } } } })
    pages.push(page); await flushPromises()
    await page.get('[data-testid="ossbox-relay-key"]').setValue('unsaved-synthetic-key')
    await page.get('[data-testid="reporting-edit-collector"]').trigger('click'); await flushPromises()
    const url = page.get('[data-testid="ossbox-relay-url"]').element as HTMLInputElement
    expect(url.readOnly).toBe(false)
    expect((page.get('[data-testid="ossbox-relay-key"]').element as HTMLInputElement).value).toBe('')
    expect(put).not.toHaveBeenCalled(); expect(test).not.toHaveBeenCalled()
    await page.get('[data-testid="ossbox-workbench-name"]').setValue('Changed Workbench')
    await page.get('form').trigger('submit'); await flushPromises()
    expect(put).toHaveBeenCalledWith('collector', { base_url: base, workbench_name: 'Changed Workbench' }, expect.any(AbortSignal))
    await page.get('[data-testid="reporting-edit-legacy"]').trigger('click'); await flushPromises()
    expect(ossboxApi.getForContract).toHaveBeenLastCalledWith('legacy', expect.any(AbortSignal))
    expect((page.get('[data-testid="ossbox-relay-url"]').element as HTMLInputElement).readOnly).toBe(true)
    expect(page.find('[data-testid="ossbox-workbench-name"]').exists()).toBe(false)
  })

  it('pins the requested editor in the URL without putting credentials in it', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, group: 'ossbox', ...modern })))
    vi.stubGlobal('fetch', fetch)
    await ossboxApi.putForContract('collector', { base_url: base, workbench_name: 'Desk', key: 'synthetic-key' })
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/settings/ossbox?contract=collector')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ base_url: base, workbench_name: 'Desk', key: 'synthetic-key' })
    expect(fetch.mock.calls[0][0]).not.toContain('synthetic-key')
  })

  it('rejects an old server that silently ignores the collector selector', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true, group: 'ossbox', ...legacy }))))
    await expect(ossboxApi.getForContract('collector')).rejects.toThrow('服务端尚不支持独立工作台配置')
  })
})
