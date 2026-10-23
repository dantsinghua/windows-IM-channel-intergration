/**
 * R6-81 只退役设置页专业卡片；既有 #88/#89 后端契约仍保留。
 * 从旧 set-page-real 搬移接口与 store 断言，不再要求已退役的 DOM。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { raw, seen, seenSince } from './harness'
import { settingsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import { useSettingsStore } from '@/stores/settings'

const RETENTION_KEYS_07 = [
  'files_days', 'messages_days', 'media_days', 'raw_days', 'mail_archive_days', 'commands_days', 'audit_days',
  'idempotency_days', 'mail_inbox_rows_days', 'export_jobs_days', 'cleanup_at', 'cleanup_batch',
  'disk_warn_mb', 'disk_high_mb', 'disk_critical_mb', 'disk_low_watermark_mb',
  'health_raw_h', 'health_1m_d', 'health_1h_d',
]

beforeEach(() => { setActivePinia(createPinia()) })

describe('保留的设置 API 和 store 保存契约', () => {
  it('retention 保存仅发一次 PUT，结果标记待重启，store 保存本次提交值', async () => {
    const body = { ...await settingsApi.get('retention'), messages_days: 20, raw_days: 5 }
    const mark = seen.length
    const result = await settingsApi.put('retention', body)
    const store = useSettingsStore()
    store.applySaved('retention', body, result.restartRequired)
    expect(seenSince(mark, '/settings/retention').map(s => s.method)).toEqual(['PUT'])
    expect(result.restartRequired).toBe(true)
    expect(store.isPendingRestart('retention')).toBe(true)
    expect(store.groups.retention).toEqual(body)
    expect(store.groups.retention.messages_days).toBe(20)
    expect(store.groups.retention.raw_days).toBe(5)
    const put = seenSince(mark, '/settings/retention')[0]
    expect(put.status).toBe(200)
    expect(JSON.parse(put.body!)).toEqual(body)
    expect(Object.keys(body).filter(k => !RETENTION_KEYS_07.includes(k))).toEqual([])
    expect(body).not.toHaveProperty('text_days')
    expect(body).not.toHaveProperty('raw_enabled')
  })

  it('retention 出参没有 07 未登记键', async () => {
    const body = (await raw('/api/v1/settings/retention')).body.data
    expect(Object.keys(body).filter(k => !RETENTION_KEYS_07.includes(k))).toEqual([])
  })

  it('资源池只下发 pools/quota_mb，修改 qq 配额后可按实际值回读', async () => {
    const previous = await settingsApi.get('resources')
    const body = { pools: previous.pools, quota_mb: { ...(previous.quota_mb as Record<string, number>), qq: 777 } }
    const mark = seen.length
    const result = await settingsApi.put('resources', body)
    const put = seenSince(mark, '/settings/resources').find(s => s.method === 'PUT')!
    const submitted = JSON.parse(put.body!)
    expect(Object.keys(submitted).sort()).toEqual(['pools', 'quota_mb'])
    expect(submitted.quota_mb.qq).toBe(777)
    for (const key of ['qidian_mb', 'qq_mb', 'wechat_mb', 'base_mb', 'mem_warn_mb', 'mem_critical_mb']) {
      expect(submitted).not.toHaveProperty(key)
    }
    expect(put.status).toBe(200)
    expect(result.restartRequired).toBe(false)
    expect(((await raw('/api/v1/settings/resources')).body.data).quota_mb.qq).toBe(777)
  })

  it('后端拒绝未知 retention 键并把 text_days 错误指针保留给调用方', async () => {
    let failure: ApiFailure | undefined
    try {
      await settingsApi.put('retention', { ...await settingsApi.get('retention'), text_days: 20 })
    } catch (error) { failure = error as ApiFailure }
    expect(failure).toBeInstanceOf(ApiFailure)
    expect(failure!.status).toBe(400)
    expect(JSON.stringify(failure!.envelope)).toContain('text_days')
  })
})
