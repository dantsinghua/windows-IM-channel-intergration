import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { shallowMount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OssboxConfig from '@/pages/mail/OssboxConfig.vue'
import { ossboxApi } from '@/api/client'
import { normalizeInstitutionReporting } from '@/api/institution-reporting'
import type { OssboxSettingsResponse } from '@/api/types'
const pages: VueWrapper[] = []
const buttons = { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' }
function response(): OssboxSettingsResponse {
  return {
    mode: 'its_relay', state: 'running', applied: true,
    data: { sts_url: '', allow_http: false, endpoint: '', bucket: '', workbench_id: '', auth_key_configured: false, sm4_key_configured: false, saved: false },
    relay: { base_url: 'https://synthetic.invalid', workbench_name: 'existing', workbench_id: 'wb', configured: true, key_configured: true, sm4_key_configured: true, saved: true },
    restart_required: false, inquiry_configured: true, catalog_configured: true,
    institution_runtime: { contract: 'its-relay-path-v1', active: true, account_workers: 1, last_directory_at: 0, errors: { directory: 'FILE_GATEWAY_HTTP_ERROR' }, history: { '10001': { scanned: 12, published: 0, media_bytes: 0, error: null } }, native_presence: { '10001': { state: 'online', code: 'NATIVE_MAIN_VISIBLE', confirmed_at: Date.now() / 1000 } } },
  }
}
beforeEach(() => { setActivePinia(createPinia()); vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No network'))); vi.spyOn(ossboxApi, 'get').mockResolvedValue(response()) })
afterEach(() => { pages.splice(0).forEach(p => p.unmount()); vi.restoreAllMocks(); vi.unstubAllGlobals() })
async function mount() { const p = shallowMount(OssboxConfig, { global: { stubs: { 'a-button': buttons } } }); pages.push(p); await flushPromises(); return p }
it('shows loaded separately from unconfirmed upload and native send', async () => {
  const p = await mount()
  expect(p.get('[data-testid="ossbox-save-status"]').text()).toContain('已由采集服务装载')
  const text = p.get('[data-testid="institution-reporting-status"]').text()
  expect(text).toContain('尚无成功记录'); expect(text).toContain('FILE_GATEWAY_HTTP_ERROR'); expect(text).toContain('读取 12，提交 0')
  expect(text).not.toContain('消息发送成功')
})
it('refreshing status never overwrites draft fields or saves configuration', async () => {
  const p = await mount(); const put = vi.spyOn(ossboxApi, 'put')
  await p.get('[data-testid="ossbox-workbench-name"]').setValue('unsaved-draft')
  await p.get('[data-testid="institution-refresh-status"]').trigger('click'); await flushPromises()
  expect((p.get('[data-testid="ossbox-workbench-name"]').element as HTMLInputElement).value).toBe('unsaved-draft')
  expect(put).not.toHaveBeenCalled()
})
it('does not call an old native confirmation online', async () => {
  const r = response(); r.institution_runtime!.native_presence['10001']!.confirmed_at -= 61
  vi.mocked(ossboxApi.get).mockResolvedValue(r)
  const p = await mount(); expect(p.get('[data-testid="institution-reporting-status"]').text()).toContain('原生状态尚未确认')
})
it('normalizer rejects unrelated contracts and untrusted diagnostic strings', () => {
  expect(normalizeInstitutionReporting({ contract: 'old', active: true })).toBeUndefined()
  const r = normalizeInstitutionReporting({ contract: 'its-relay-path-v1', active: true, errors: { 'component': 'secret=must-not-render' }, history: { '10001': { scanned: Infinity, published: -2, media_bytes: NaN } } })!
  expect(r.errors.component).toBe('DETAIL_UNAVAILABLE'); expect(r.history['10001']!.scanned).toBe(0)
  expect(JSON.stringify(r)).not.toContain('must-not-render')
})