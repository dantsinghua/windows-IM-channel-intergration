/** `accounts` store:Account[] 按通道分组、选中账号、微信档案(01 §2.5) */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { accountsApi } from '@/api/client'
import type { Account, AccountStatePayload, Prompt, QtEvent } from '@/api/types'
import { CHANNELS, type Channel, STATE_OPS, type AccountState } from '@/i18n/zh-CN/codes'
import { useEventsStore } from './events'

/** C-42 通用段字面的默认页大小(`limit=50`);后端实现缺省是 100,客户端显式传以免两边各说各话 */
const PAGE_LIMIT = 50

export const useAccountsStore = defineStore('accounts', () => {
  const items = ref<Account[]>([])
  /** C-42 游标:非空 = 还有下一页(后端仅在本页满 `limit` 时才给);opaque,只透传不构造(G-16) */
  const nextCursor = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const selectedId = ref<string | null>(null)
  /** 事件推来的等人提示,按 account_id 存(二维码只在内存) */
  const prompts = ref<Record<string, Prompt>>({})
  /** 当前登录尝试 id,按 account_id 存(N-3) */
  const loginSessions = ref<Record<string, string | null>>({})

  const byId = computed(() => Object.fromEntries(items.value.map((a) => [a.id, a])) as Record<string, Account>)
  const selected = computed(() => (selectedId.value ? byId.value[selectedId.value] ?? null : null))

  const byChannel = computed(() => {
    const out: Record<Channel, Account[]> = { qidian: [], qq: [], wechat: [] }
    for (const a of items.value) out[a.channel]?.push(a)
    return out
  })

  /** 微信历史档案 = channel=wechat 且 stopped/disabled 的行(C-01) */
  const wechatProfiles = computed(() =>
    byChannel.value.wechat.filter((a) => a.state === 'stopped' || a.state === 'disabled'),
  )

  const summary = computed(() =>
    Object.fromEntries(
      CHANNELS.map((ch) => {
        const rows = byChannel.value[ch]
        return [ch, {
          total: rows.length,
          online: rows.filter((a) => a.state === 'running' || a.state === 'degraded').length,
          loginRequired: rows.filter((a) => a.state === 'login_required').length,
          stopped: rows.filter((a) => a.state === 'stopped' || a.state === 'created').length,
        }]
      }),
    ) as Record<Channel, { total: number; online: number; loginRequired: number; stopped: number }>,
  )

  function ops(a: Account | null | undefined) {
    return STATE_OPS[(a?.state ?? 'stopped') as AccountState]
  }

  /**
   * 拉账号列表。`more=true` = 用游标续下一页并**追加**,否则从头拉并重置游标(C-42)。
   *
   * 🔴 后端已按 **`created_ms` 降序**下发(backend-api-3 §7-8:C-42 的游标要求排序列 = 游标里的 `ts_ms`),
   * 所以这里**不再自己排一遍** —— 页面的 `byChannel` 只是展示层分桶,桶内保持后端顺序。
   */
  async function load(more = false): Promise<void> {
    loading.value = true
    error.value = null
    try {
      // 微信档案要带 include_stopped(C-40);C-42:`limit` 真生效,翻页靠 `cursor` 透传
      const { items: rows, nextCursor: nc } = await accountsApi.list({
        include_stopped: true,
        limit: PAGE_LIMIT,
        cursor: more ? nextCursor.value ?? undefined : undefined,
      })
      items.value = more ? [...items.value, ...rows] : rows
      nextCursor.value = nc
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function upsert(a: Account): void {
    const i = items.value.findIndex((x) => x.id === a.id)
    if (i >= 0) items.value[i] = { ...items.value[i], ...a }
    else items.value.push(a)
  }

  /** `account_state` 事件归约:按真实 account_id 更新/合并 */
  function applyAccountState(ev: QtEvent<AccountStatePayload>): void {
    const id = ev.account_id
    if (!id) return
    const p = ev.payload
    const i = items.value.findIndex((x) => x.id === id)
    const patch: Partial<Account> = {
      state: p.state,
      state_code: p.state_code ?? '',
      state_reason: p.state_reason ?? '',
      error_since_ms: p.error_since_ms ?? null,
      enabled: p.enabled,
      runtime: p.runtime ?? {},
      capabilities: p.capabilities ?? [],
      self_nick: p.self_nick,
    }
    if (i >= 0) items.value[i] = { ...items.value[i], ...patch }
    else items.value.push({ id, channel: (ev.channel ?? 'qidian') as Channel, label: id, host: 'wsl',
      quota_mb: 0, deleted_ms: null, auto_recover: true, ...patch } as Account)

    if (p.prompt) prompts.value[id] = p.prompt
    else if (p.state !== 'login_required') delete prompts.value[id]
    loginSessions.value[id] = p.login_session_id ?? null
  }

  function bindEvents(): void {
    const events = useEventsStore()
    events.on('account_state', (ev) => applyAccountState(ev as QtEvent<AccountStatePayload>))
  }

  async function refreshPrompt(id: string): Promise<void> {
    const p = await accountsApi.prompt(id, loginSessions.value[id] ?? undefined)
    if (p.kind) prompts.value[id] = p
    else delete prompts.value[id]
  }

  /** `已故障 N 分钟` —— 唯一判据是信封字段 account.error_since_ms(R6-4) */
  function errorMinutes(a: Account | null | undefined, now = Date.now()): number | null {
    if (!a || a.state !== 'error' || a.error_since_ms == null) return null
    return Math.floor((now - a.error_since_ms) / 60000)
  }

  function errorSeconds(a: Account | null | undefined, now = Date.now()): number | null {
    if (!a || a.state !== 'error' || a.error_since_ms == null) return null
    return Math.floor((now - a.error_since_ms) / 1000)
  }

  return {
    items, nextCursor, loading, error, selectedId, prompts, loginSessions,
    byId, selected, byChannel, wechatProfiles, summary,
    ops, load, upsert, applyAccountState, bindEvents, refreshPrompt, errorMinutes, errorSeconds,
  }
})
