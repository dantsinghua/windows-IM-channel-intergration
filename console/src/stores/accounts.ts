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
   * 🔴 本函数的语义是「**无条件**发那一次请求」 —— C-42 的翻页判据、页面上的「刷新」、
   * 批量操作之后的回拉都依赖它,不在这里加任何跳过条件。
   * 需要「进页面时别跟 `App.vue` 的首启全量拉撞车」的,用下面的 `loadFirst()`(D-H)。
   */
  let headInflight: Promise<void> | null = null
  let headLoadedAt = 0

  async function load(more = false): Promise<void> {
    if (more) return loadOnce(true)
    const p = loadOnce(false).finally(() => {
      if (headInflight === p) headInflight = null
      headLoadedAt = Date.now()
    })
    headInflight = p
    return p
  }

  /**
   * **页面首拉专用**(D-H)。
   *
   * 现象:进 `P-ACCT` 时 `GET /accounts` 连发两次 —— `App.vue` 首启的 `fullReload()` 一次、
   * 页面自己 `onMounted` 一次。真机上每进一次页面就多一次 Agent 往返。
   *
   * 判据只有两条,都不碰 `load()` 的语义:
   *  ① 已经有一次首页拉在飞 ⇒ **搭同一班车**(复用它的 promise),不再开一次往返;
   *  ② 距上次首页拉不到 `FIRST_FRESH_MS` ⇒ 跳过(首启那两次就落在这个窗口里)。
   * 窗口取 2 s:只吃掉「刚拉完立刻又挂载一次」这种重复;离开页面再回来(远不止 2 s)照常重拉,
   * 期间的状态变化本来也由 WS 的 `account_state` 事件实时归约,不靠这一次轮询。
   */
  const FIRST_FRESH_MS = 2000

  async function loadFirst(): Promise<void> {
    if (headInflight) return headInflight
    if (headLoadedAt && Date.now() - headLoadedAt < FIRST_FRESH_MS) return
    return load()
  }

  /**
   * 真正发那一次请求。
   * 🔴 后端已按 **`created_ms` 降序**下发(backend-api-3 §7-8:C-42 的游标要求排序列 = 游标里的 `ts_ms`),
   * 所以这里**不再自己排一遍** —— 页面的 `byChannel` 只是展示层分桶,桶内保持后端顺序。
   */
  async function loadOnce(more: boolean): Promise<void> {
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
    ops, load, loadFirst, upsert, applyAccountState, bindEvents, refreshPrompt, errorMinutes, errorSeconds,
  }
})
