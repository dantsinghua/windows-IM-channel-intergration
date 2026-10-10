<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Modal, message } from 'ant-design-vue'
import { acct as T } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useEventsStore } from '@/stores/events'
import { useSessionStore } from '@/stores/session'
import { accountsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import StateDot from '@/components/StateDot.vue'
import PageState from '@/components/PageState.vue'
import { ACCOUNT_STATES, CHANNEL_TEXT, STATE_CODES, type Channel } from '@/i18n/zh-CN/codes'
import type { Account } from '@/api/types'

const router = useRouter()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const events = useEventsStore()
const session = useSessionStore()
const query = ref('')
const now = ref(Date.now())
const channelOrder: Channel[] = ['qidian', 'wechat', 'qq']
const slots = computed(() => resources.slots)
const holder = computed(() => slots.value?.holder ? accounts.byId[slots.value.holder] ?? null : null)
const holderFaulted = computed(() => holder.value?.state === 'error' && holder.value.error_since_ms != null)
const holderErrorSeconds = computed(() => accounts.errorSeconds(holder.value, now.value) ?? 0)
const canForceRelease = computed(() => holderFaulted.value && holderErrorSeconds.value >= 300)
const pendingLeft = computed(() => slots.value?.pending_expires_at ? Math.max(0, Math.round((Date.parse(slots.value.pending_expires_at) - now.value) / 1000)) : 0)
let tick: ReturnType<typeof setInterval> | null = null
function rows(channel: Channel): Account[] {
  const keyword = query.value.trim().toLocaleLowerCase()
  return accounts.byChannel[channel].filter((a) => a.deleted_ms == null && (!keyword || [a.id, a.label, a.self_nick, a.wxid, a.self_uid].some((value) => String(value ?? '').toLocaleLowerCase().includes(keyword))))
}
function accountMemory(id: string): string {
  const row = resources.metrics?.ours.accounts.find((item) => item.id === id)
  const value = row?.current_mb ?? row?.rss_mb
  return value == null ? '—' : (value / 1024).toFixed(2) + ' GB'
}
function add(channel: Channel): void { void router.push({ path: '/acct/new', query: { ch: channel } }) }
function canRemove(account: Account): boolean {
  return !['provisioning', 'starting', 'logging_in', 'stopping'].includes(account.state)
}
async function removeAccount(account: Account): Promise<void> {
  try {
    await accountsApi.softDelete(account.id, account.label)
  } catch (error) {
    const alreadyGone = error instanceof ApiFailure && (error.status === 404 || error.code === 'TARGET_NOT_FOUND')
    if (!alreadyGone) {
      message.error(error instanceof Error ? error.message : String(error))
      return
    }
  }
  accounts.forget(account.id)
  message.success('已从列表移除，聊天记录与登录态保留')
}
async function cancelPending(): Promise<void> {
  const target = slots.value?.pending
  const loginSessionId = resources.pendingLoginSessionId
  if (!target || !loginSessionId) return
  Modal.confirm({
    title: '取消本次微信绑定', content: '取消后将释放本次占用的微信槽位，历史账号档案保留。',
    okType: 'danger', okText: '确认取消', cancelText: '继续绑定',
    onOk: async () => {
      try { const result = await accountsApi.loginCancel(target, loginSessionId); message.info(result.stale ? '该次登录尝试已结束' : '绑定已取消，槽位已释放') }
      catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
      finally { await resources.load() }
    },
  })
}
function forceRelease(): void {
  if (!canForceRelease.value) return
  Modal.confirm({
    title: '释放故障微信并切换',
    content: '当前账号将登出，其消息采集会停止；微信槽位会供新账号登录。',
    okType: 'danger', okText: '确认接管', cancelText: '取消',
    okButtonProps: { 'data-testid': T.wechatHolderForceReleaseConfirm } as never,
    onOk: async () => {
      try { await accountsApi.switchNew(true); message.success('已接管槽位'); await Promise.all([accounts.load(), resources.load()]) }
      catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
    },
  })
}
onMounted(() => { void accounts.loadFirst(); void resources.load(); if (!resources.metrics) void resources.loadMetrics(); tick = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => { if (tick) clearInterval(tick) })
</script>

<template>
  <div class="qt-page qt-stack">
    <header class="qt-page-heading"><div><div class="qt-eyebrow">CONNECTED ACCOUNTS</div><h1>每个账号，都有自己的工作台</h1><p>选择账号，查看画面、连接状态与历史消息。</p></div><a-button type="primary" @click="router.push('/acct/new')">＋ 添加账号</a-button></header>
    <div class="qt-toolbar account-search"><a-input v-model:value="query" allow-clear placeholder="搜索账号、昵称或标识" /><span class="qt-small qt-muted">{{ accounts.items.filter((a) => a.deleted_ms == null).length }} 个已加载账号</span></div>
    <div v-if="!events.connected" class="banner" :data-testid="T.staleBanner">实时状态已断开，显示的是 {{ events.disconnectedAt ? Math.round((now - events.disconnectedAt) / 1000) : 0 }} 秒前的数据。</div>
    <PageState :loading="accounts.loading && !accounts.items.length" :error="accounts.error" @retry="accounts.load()">
      <section v-for="channel in channelOrder" :key="channel" class="channel-group" :data-testid="T.group(channel)">
        <header class="group-heading"><h2>{{ CHANNEL_TEXT[channel] }} <small>{{ accounts.summary[channel].online }} 在线 · {{ accounts.byChannel[channel].filter((a) => a.deleted_ms == null).length }} 个账号</small></h2><a-button :data-testid="T.add(channel)" :disabled="channel === 'wechat' && (session.wechatDisabled || resources.hasPending)" @click="add(channel)">＋ 添加{{ CHANNEL_TEXT[channel] }}</a-button></header>
        <div v-if="channel === 'wechat' && resources.hasPending" class="banner pending-banner"><span :data-testid="T.wechatSlot">微信账号正在绑定</span><span :data-testid="T.wechatPendingCountdown">{{ pendingLeft > 0 ? '剩余 ' + pendingLeft + ' 秒' : '即将释放…' }}</span><a-button v-if="resources.canCancelPending" size="small" :data-testid="T.wechatPendingCancel" @click="cancelPending">取消绑定</a-button></div>
        <div v-if="channel === 'wechat' && holderFaulted" class="banner" :data-testid="T.wechatHolderFault">当前微信账号故障：{{ STATE_CODES[holder?.state_code ?? '']?.zh ?? holder?.state_reason ?? '未知' }}，已持续 {{ accounts.errorMinutes(holder, now) ?? 0 }} 分钟。<a-tooltip :title="canForceRelease ? '' : '故障持续满 5 分钟后可接管'"><a-button size="small" :disabled="!canForceRelease" :data-testid="T.wechatHolderForceRelease" @click="forceRelease">释放并切换</a-button></a-tooltip></div>
        <div class="account-grid">
          <article v-for="account in rows(channel)" :key="account.id" class="account-card qt-glass" :data-testid="T.row(account.id)">
            <div class="card-state" :data-testid="T.rowState(account.id)"><StateDot :state="account.state" :reason="account.state_reason" />{{ ACCOUNT_STATES[account.state]?.zh || '状态未知' }}</div>
            <button class="account-identity" :data-testid="T.rowDetail(account.id)" @click="router.push('/acct/' + account.id)"><span class="account-avatar">{{ (account.self_nick || account.label || CHANNEL_TEXT[channel]).slice(0, 1) }}</span><span>{{ account.label || account.id }}<small>{{ account.self_nick || CHANNEL_TEXT[channel] + '账号' }}</small></span><span class="identity-arrow">↗</span></button>
            <div class="account-id">{{ account.wxid || account.self_uid || account.id }}</div>
            <div class="meta-row"><span>当前内存</span><b>{{ accountMemory(account.id) }}</b></div><div class="meta-row"><span>最近活动</span><span>{{ account.last_seen_at?.slice(5,16).replace('T',' ') ?? '—' }}</span></div>
            <div class="card-actions"><a-button type="primary" @click="router.push('/acct/' + account.id)">进入工作台</a-button><a-button @click="router.push({ path: '/msg', query: { account_id: account.id } })">历史消息</a-button><a-popconfirm :title="'从列表移除「' + (account.label || account.id) + '」？会先停止该账号，聊天记录与登录态保留。'" ok-text="移除" cancel-text="取消" ok-type="danger" @confirm="removeAccount(account)"><a-button danger :disabled="!canRemove(account)" :data-testid="T.rowDelete(account.id)">移除</a-button></a-popconfirm></div>
          </article>
          <div v-if="!rows(channel).length" class="empty-card"><a-empty :description="query ? '没有匹配的账号' : '还没有' + CHANNEL_TEXT[channel] + '账号'"><a-button v-if="!query" :data-testid="T.emptyAdd(channel)" :disabled="channel === 'wechat' && (session.wechatDisabled || resources.hasPending)" @click="add(channel)">添加{{ CHANNEL_TEXT[channel] }}</a-button></a-empty></div>
        </div>
      </section>
      <div v-if="accounts.nextCursor" class="load-more"><a-button :loading="accounts.loading" @click="accounts.load(true)">加载更多账号</a-button></div>
    </PageState>
  </div>
</template>

<style scoped>
.account-search { display: flex; align-items: center; gap: 20px; }
.account-search :deep(.ant-input-affix-wrapper) { max-width: 400px; }
.channel-group { padding: 10px 0 22px; }
.group-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 12px 0 20px; }
.group-heading h2 { margin: 0; font-size: 20px; }
.group-heading small { font-size: 11px; color: var(--qt-text-secondary); font-weight: 400; margin-left: 14px; }
.account-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 18px; }
.account-card { position: relative; padding: 24px; border: 1px solid rgba(117,71,168,.12); border-radius: 24px; box-shadow: 0 8px 26px rgba(53,39,77,.035); }
.card-state { display: flex; align-items: center; gap: 8px; font-size: 12px; margin-bottom: 24px; color: var(--qt-text-secondary); }
.account-identity { appearance: none; width: 100%; display: flex; align-items: center; gap: 13px; border: 0; background: none; padding: 0; color: var(--qt-text); font: inherit; font-size: 18px; text-align: left; cursor: pointer; }
.account-identity > span:nth-child(2) { min-width: 0; overflow-wrap: anywhere; }
.account-identity small { display: block; font-size: 12px; font-weight: 400; color: var(--qt-text-secondary); margin-top: 5px; }
.account-avatar { display: grid; place-items: center; flex: 0 0 48px; width: 48px; height: 48px; border-radius: 16px; background: linear-gradient(145deg,#eee3fc,#fff2d8); color: var(--qt-primary); font-size: 23px; }
.identity-arrow { margin-left: auto; color: var(--qt-primary); }
.account-id { margin: 18px 0; font-size: 11px; color: var(--qt-text-secondary); overflow-wrap: anywhere; }
.meta-row { display: flex; justify-content: space-between; gap: 8px; padding: 10px 0; border-top: 1px solid var(--qt-border); color: var(--qt-text-secondary); font-size: 12px; }
.meta-row b { color: var(--qt-text); font-weight: 500; }
.card-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 20px; }
.empty-card { grid-column: 1/-1; border: 1px dashed var(--qt-border); border-radius: 24px; padding: 20px; }
.banner { background: #fff5df; color: var(--qt-sev-warn); padding: 14px 18px; border-radius: 14px; margin-bottom: 16px; font-size: 12px; }
.banner .ant-btn { margin-left: 14px; }
.pending-banner { display: flex; align-items: center; gap: 16px; }
.load-more { text-align: center; padding: 18px; }
@media(max-width: 1250px) { .account-grid { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media(max-width: 760px) { .account-grid { grid-template-columns: 1fr; } .account-search { flex-wrap: wrap; } .group-heading small { display: block; margin: 6px 0 0; } }
</style>
