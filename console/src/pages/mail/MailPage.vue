<script setup lang="ts">
/** P-MAIL: read, review and track mail without transport configuration. */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { mail as T } from '@/testids'
import { useMailStore } from '@/stores/mail'
import { useSettingsStore } from '@/stores/settings'
import { useEventsStore } from '@/stores/events'
import { mailApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import PageState from '@/components/PageState.vue'
import QtIcon from '@/components/QtIcon.vue'
import {
  ALERT_CODES, ARGS_DIGEST_MISMATCH_TEXT, MAIL_BANNER_CODES, MAIL_INBOX_STATUS,
  capabilityText, mailInboxStatusText, mailRouteText,
} from '@/i18n/zh-CN/codes'

const router = useRouter()
const store = useMailStore()
const settings = useSettingsStore()
const events = useEventsStore()
const tab = ref<'inbox' | 'outbox' | 'pending'>('inbox')
const dateFiltersOpen = ref(false)
const now = ref(Date.now())
const detailOpen = ref(false)
const busy = ref(false)
const tamperModal = ref({ open: false, traceId: '' })
let tick: ReturnType<typeof setInterval> | null = null
const enabled = computed(() => store.status?.enabled ?? settings.mailEnabled)
const banner = computed(() => events.firing.find(a => (MAIL_BANNER_CODES as readonly string[]).includes(a.code)) ?? null)
const routeOptions = computed(() => [...new Set((store.status?.routes ?? []).map(r => r.scope))]
  .map(value => ({ value, label: mailRouteText(value) })))
const statusOptions = Object.entries(MAIL_INBOX_STATUS).map(([value, label]) => ({ value, label }))
const outboxStatuses: Record<string, string> = {
  QUEUED: '等待发送', SENDING: '正在发送', RETRY: '等待重试',
  SENT: '已发送', DEAD: '发送失败', DISCARDED: '已丢弃',
}
const outboxOptions = Object.entries(outboxStatuses).map(([value, label]) => ({ value, label }))

function timeText(value?: string | null): string {
  if (!value) return '暂无记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function ttlOf(expiresAt: string, remaining: number): number {
  const byExpire = Math.round((Date.parse(expiresAt) - now.value) / 1000)
  return Math.max(0, Number.isFinite(byExpire) ? byExpire : remaining)
}
function setDate(key: 'since' | 'until', event: Event): void {
  const value = (event.target as HTMLInputElement).value
  store.inboxFilter[key] = value ? new Date(value).toISOString() : undefined
}
function dateValue(key: 'since' | 'until'): string {
  const raw = store.inboxFilter[key]
  if (typeof raw !== 'string' || !raw) return ''
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return ''
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
async function runAction(action: () => Promise<unknown>): Promise<void> {
  if (busy.value) return
  busy.value = true
  try { await action() } catch (e) { message.error(e instanceof Error ? e.message : String(e)) }
  finally { busy.value = false }
}
async function approve(id: string): Promise<void> {
  try {
    await mailApi.approve(id)
    message.success('已确认执行')
    await store.reloadAll()
  } catch (e) {
    if (!(e instanceof ApiFailure)) { message.error(String(e)); return }
    if (e.code === 'CONFIRM_EXPIRED') {
      message.info('该确认已过期,已自动作废')
      await store.reloadPending()
      await store.reloadInbox()
      return
    }
    if (e.reason === 'args_digest_mismatch') {
      tamperModal.value = { open: true, traceId: e.traceId }
      await store.reloadPending()
      await store.reloadInbox()
      return
    }
    message.error(e.message + '(trace ' + e.traceShort + ')')
  }
}
async function reject(id: string): Promise<void> {
  try {
    await mailApi.reject(id)
    message.success('已驳回')
    await store.reloadAll()
  } catch (e) {
    if (e instanceof ApiFailure && e.code === 'CONFIRM_EXPIRED') {
      message.info('该确认已过期,已自动作废')
      await store.reloadPending()
      await store.reloadInbox()
      return
    }
    message.error(e instanceof Error ? e.message : String(e))
  }
}
async function openDetail(id: string): Promise<void> {
  await runAction(async () => {
    store.inboxDetail = await mailApi.inboxDetail(id)
    detailOpen.value = true
  })
}
async function resend(id: string): Promise<void> {
  await runAction(async () => { await mailApi.resend(id); await store.reloadAll() })
}
async function discard(id: string): Promise<void> {
  await runAction(async () => { await mailApi.discard(id); await store.reloadAll() })
}
function openBanner(): void {
  if (banner.value?.subject.startsWith('endpoint:')) void router.push('/env')
  else void router.push({ path: '/log', query: { tab: 'alerts' } })
}
onMounted(() => {
  void store.reloadAll()
  store.startPolling()
  tick = setInterval(() => {
    now.value = Date.now()
    if (store.pending.some(p => ttlOf(p.expires_at, p.remaining_ttl_s) === 0)) {
      void store.reloadPending().catch(() => undefined)
      void store.reloadInbox().catch(() => undefined)
    }
  }, 1000)
})
onUnmounted(() => { store.stopPolling(); if (tick) clearInterval(tick) })
</script>

<template>
  <div class="qt-page mail-page">
    <header class="qt-page-heading">
      <div><span class="qt-eyebrow">MAIL CENTER</span><h1>邮件</h1><p>查收邮件、跟踪发送，把需要你确认的操作集中在一起。</p></div>
      <a-button :data-testid="T.refresh" :loading="store.loading" @click="store.reloadAll()"><template #icon><QtIcon name="refresh" :size="16" /></template>刷新</a-button>
    </header>
    <PageState :loading="store.loading && !store.status" :error="store.error" @retry="store.reloadAll()">
      <section v-if="!enabled" class="mail-empty qt-glass">
        <span class="empty-icon"><QtIcon name="mail" :size="32" /></span>
        <h2 :data-testid="T.disabledHint">邮件服务尚未启用</h2>
        <p>启用并连接邮箱后，收件记录与待确认操作会显示在这里。</p>
        <a-button @click="router.push('/dash')">回到账号首页</a-button>
      </section>
      <template v-else>
        <button v-if="banner" class="mail-banner" :data-testid="T.errorBanner" @click="openBanner">
          <QtIcon name="bell" :size="18" /><span>{{ banner.title || ALERT_CODES[banner.code]?.zh || banner.code }} · {{ banner.message }}</span><QtIcon name="chevron" :size="16" />
        </button>
        <div v-if="!settings.requireSignature" class="mail-banner is-warning" :data-testid="T.testmodeBanner"><QtIcon name="shield" :size="18" />测试模式：当前不校验签名，仅允许内网白名单发件人。</div>

        <section class="mail-health qt-glass" :data-testid="T.block('health')">
          <div class="health-heading"><span class="health-icon"><QtIcon name="mail" /></span><div><strong>邮箱连接</strong><span>来自当前邮件服务的状态</span></div></div>
          <div class="route-list" :data-testid="T.healthCard">
            <div v-for="r in store.status?.routes ?? []" :key="r.route_id" class="route-status" :data-testid="T.healthRoute(r.scope)">
              <div><b>{{ mailRouteText(r.scope) }}</b><span class="status-chip" :class="{ attention: r.inbound.consecutive_failures > 0 || r.outbound.consecutive_failures > 0 }">{{ r.inbound.consecutive_failures > 0 || r.outbound.consecutive_failures > 0 ? '连接需关注' : r.inbound.last_success_at ? '收信正常' : '等待首次收信' }}</span></div>
              <p>最近收信 {{ timeText(r.inbound.last_success_at) }} · 待发送 {{ r.outbound.queued }} 封<span v-if="r.outbound.dead"> · 失败 {{ r.outbound.dead }} 封</span></p>
              <p v-if="r.inbound.protocol_configured === 'imap' && r.inbound.protocol_active === 'pop3'" class="protocol-note" :data-testid="T.healthProtoFallback">IMAP 不可用，已回落 POP3：{{ r.inbound.fallback?.reason ?? '原因暂不可用' }}</p>
              <p v-if="r.inbound.protocol_active === 'pop3'" class="protocol-note" :data-testid="T.healthJunkHint">垃圾邮件文件夹不可见，被拦截的指令邮件可能漏收。</p>
            </div>
            <p v-if="!(store.status?.routes ?? []).length" class="qt-muted">尚未连接邮箱，暂无收发状态。</p>
          </div>
        </section>

        <section class="mail-workspace qt-surface">
          <div class="mail-tabs" role="tablist" aria-label="邮件分类">
            <button id="mail-tab-inbox" role="tab" :aria-selected="tab === 'inbox'" aria-controls="mail-panel-inbox" :class="{ active: tab === 'inbox' }" @click="tab = 'inbox'">收件</button>
            <button id="mail-tab-outbox" role="tab" :aria-selected="tab === 'outbox'" aria-controls="mail-panel-outbox" :class="{ active: tab === 'outbox' }" @click="tab = 'outbox'">发件</button>
            <button id="mail-tab-pending" role="tab" :aria-selected="tab === 'pending'" aria-controls="mail-panel-pending" :class="{ active: tab === 'pending' }" @click="tab = 'pending'">待确认<span v-if="store.pending.length" class="tab-count">{{ store.pending.length }}</span></button>
          </div>

          <div v-if="tab === 'inbox'" id="mail-panel-inbox" role="tabpanel" aria-labelledby="mail-tab-inbox" :data-testid="T.block('inbox')">
            <form class="qt-toolbar inbox-filters" @submit.prevent="runAction(() => store.reloadInbox())">
              <a-input :value="store.inboxFilter.q as string" :data-testid="T.inboxFilter('q')" class="search-field" placeholder="搜索发件人或邮件主题" allow-clear aria-label="搜索邮件" @change="(e: any) => store.inboxFilter.q = e.target.value"><template #prefix><QtIcon name="search" :size="17" /></template></a-input>
              <a-select :value="store.inboxFilter.route as string" :data-testid="T.inboxFilter('route')" :options="routeOptions" placeholder="全部邮箱" allow-clear aria-label="按邮箱筛选" @change="(value: string) => store.inboxFilter.route = value" />
              <a-select :value="store.inboxFilter.status as string" :data-testid="T.inboxFilter('status')" :options="statusOptions" placeholder="全部状态" allow-clear aria-label="按邮件状态筛选" @change="(value: string) => store.inboxFilter.status = value" />
              <a-button :aria-expanded="dateFiltersOpen" @click="dateFiltersOpen = !dateFiltersOpen">时间范围</a-button><a-button type="primary" html-type="submit" :loading="busy">查询</a-button>
              <div v-if="dateFiltersOpen" class="date-filters"><label>开始时间<input type="datetime-local" :value="dateValue('since')" :data-testid="T.inboxFilter('since')" @change="setDate('since', $event)" /></label><span>至</span><label>结束时间<input type="datetime-local" :value="dateValue('until')" :data-testid="T.inboxFilter('until')" @change="setDate('until', $event)" /></label></div>
            </form>
            <div class="table-scroll">
              <table class="mail-table"><thead><tr><th>邮件</th><th>所属邮箱</th><th>收到时间</th><th>状态</th><th>操作</th></tr></thead><tbody>
                <tr v-for="(r, i) in store.inbox" :key="r.id" :data-testid="T.inboxRow(i)">
                  <td><button class="subject-link" :data-testid="T.inboxRowDetail(i)" @click="openDetail(r.id)">{{ r.subject || '无主题' }}</button><div class="row-secondary">{{ r.from_addr }}</div></td>
                  <td :data-testid="T.inboxRowRoute(i)">{{ mailRouteText(r.route) }}</td><td class="time-cell">{{ timeText(r.received_at) }}</td>
                  <td><span class="status-chip" :class="{ failure: mailInboxStatusText(r.status, r.reason).red }">{{ mailInboxStatusText(r.status, r.reason).zh }}</span><p v-if="r.reason" class="row-secondary reason">{{ r.reason }}</p></td>
                  <td><a-button v-if="r.trace_id" type="text" size="small" :data-testid="T.inboxRowTrace(i)" @click="router.push({ path: '/log', query: { trace_id: r.trace_id } })">查看记录</a-button><span v-else class="qt-muted">—</span></td>
                </tr>
              </tbody></table>
            </div>
            <div v-if="!store.inbox.length" class="list-empty"><QtIcon name="mail" :size="32" /><h3>还没有符合条件的邮件</h3><p>试试其他关键词，或调整邮箱与时间范围。</p></div>
            <footer class="list-footer"><span>已加载 {{ store.inbox.length }} 封邮件</span><a-button v-if="store.inboxCursor" :loading="busy" @click="runAction(() => store.reloadInbox(true))">加载更多</a-button></footer>
          </div>

          <div v-else-if="tab === 'outbox'" id="mail-panel-outbox" role="tabpanel" aria-labelledby="mail-tab-outbox" :data-testid="T.block('outbox')">
            <div class="outbox-heading"><div><h2>发送记录</h2><p>查看投递状态，处理未能发送的邮件。</p></div><a-select :value="store.outboxFilter.status as string" :options="outboxOptions" placeholder="全部状态" allow-clear aria-label="按发送状态筛选" @change="(value: string) => { store.outboxFilter.status = value; runAction(() => store.reloadOutbox()) }" /></div>
            <div class="table-scroll"><table class="mail-table"><thead><tr><th>邮件与收件人</th><th>时间</th><th>发送状态</th><th>操作</th></tr></thead><tbody>
              <tr v-for="(o, i) in store.outbox" :key="o.id" :data-testid="T.outboxRow(i)">
                <td><strong>{{ o.subject || '无主题' }}</strong><div class="row-secondary">{{ o.to }}</div></td><td class="time-cell">{{ timeText(o.sent_at || o.created_at) }}</td>
                <td><span class="status-chip" :class="{ failure: o.status === 'DEAD' }">{{ outboxStatuses[o.status] ?? o.status }}</span><p v-if="o.last_error" class="row-secondary reason">{{ o.last_error }}</p><p v-if="o.next_attempt_at" class="row-secondary">下次尝试 {{ timeText(o.next_attempt_at) }}</p></td>
                <td><div class="row-actions"><a-popover v-if="o.ref" title="关联来源" trigger="click"><template #content><p class="qt-small">来源编号：{{ o.ref }}</p><a-button v-if="o.kind === 'receipt' && /^\d+$/.test(o.ref)" size="small" @click="openDetail(o.ref)">查看原邮件</a-button></template><a-button size="small" type="text">关联来源</a-button></a-popover><a-popconfirm v-if="['DEAD', 'RETRY'].includes(o.status)" :title="'重新发送「' + (o.subject || '无主题') + '」给 ' + o.to + '？'" ok-text="确认发送" cancel-text="取消" @confirm="resend(o.id)"><a-button size="small" :disabled="busy" :data-testid="T.outboxRowResend(i)">重试发送</a-button></a-popconfirm><a-popconfirm v-if="['QUEUED', 'RETRY', 'DEAD'].includes(o.status)" title="丢弃这封邮件？丢弃后将不再发送。" ok-text="确认丢弃" cancel-text="取消" @confirm="discard(o.id)"><a-button size="small" type="text" danger :disabled="busy" :data-testid="T.outboxRowDiscard(i)">丢弃</a-button></a-popconfirm></div></td>
              </tr>
            </tbody></table></div>
            <div v-if="!store.outbox.length" class="list-empty"><QtIcon name="mail" :size="32" /><h3>暂无发送记录</h3><p>符合筛选条件的邮件会显示在这里。</p></div>
            <footer class="list-footer"><span>已加载 {{ store.outbox.length }} 封邮件</span><a-button v-if="store.outboxCursor" :loading="busy" @click="runAction(() => store.reloadOutbox(true))">加载更多</a-button></footer>
          </div>

          <div v-else id="mail-panel-pending" role="tabpanel" aria-labelledby="mail-tab-pending" :data-testid="T.block('danger')">
            <div class="pending-note" :data-testid="T.dangerNote"><QtIcon name="shield" :size="20" /><span>危险操作只能在本机控制台确认，不能回邮件确认。请核对发件人、目标账号与操作内容。</span></div>
            <div v-if="store.pending.length" class="pending-list" :data-testid="T.dangerList">
              <article v-for="(p, i) in store.pending" :key="p.id" class="pending-card qt-glass" :data-testid="T.dangerRow(i)">
                <div class="pending-top"><span class="pending-icon"><QtIcon name="shield" /></span><div class="qt-grow"><h3>{{ capabilityText(p.op) }}</h3><p>目标账号 {{ p.account_id ?? '未指定' }} · {{ p.from_addr }}</p></div><span class="ttl" :data-testid="T.dangerRowTtl(i)">剩余 {{ ttlOf(p.expires_at, p.remaining_ttl_s) }} 秒</span></div>
                <div class="request-summary"><span>操作摘要</span><code>{{ p.args_digest }}</code></div>
                <footer><span class="qt-muted qt-small">收到于 {{ timeText(p.created_at) }}</span><div class="row-actions"><a-button :data-testid="T.dangerRowReject(i)" :disabled="busy" @click="runAction(() => reject(p.id))">拒绝</a-button><a-popconfirm title="该操作由邮件远程发起,确认后立即执行" ok-type="danger" ok-text="确认执行" cancel-text="取消" @confirm="runAction(() => approve(p.id))"><a-button danger :data-testid="T.dangerRowApprove(i)" :disabled="busy || ttlOf(p.expires_at, p.remaining_ttl_s) === 0">确认执行</a-button></a-popconfirm></div></footer>
              </article>
            </div>
            <div v-else class="list-empty"><QtIcon name="check" :size="36" /><h3>无待确认的危险指令</h3><p>有新的待确认操作时，会在这里提醒你。</p></div>
          </div>
        </section>
      </template>
    </PageState>

    <a-drawer v-model:open="detailOpen" title="邮件详情" :width="640" :data-testid="T.inboxDetailDrawer">
      <template v-if="store.inboxDetail">
        <div class="detail-heading"><span class="qt-eyebrow">收到的邮件</span><h2>{{ store.inboxDetail.subject || '无主题' }}</h2><p>{{ store.inboxDetail.from_addr }} · {{ timeText(store.inboxDetail.received_at) }}</p><span class="status-chip" :class="{ failure: mailInboxStatusText(store.inboxDetail.status, store.inboxDetail.reason).red }">{{ mailInboxStatusText(store.inboxDetail.status, store.inboxDetail.reason).zh }}</span></div>
        <section class="detail-section"><h3>邮件正文</h3><p class="mail-body">{{ store.inboxDetail.body_text || '此邮件没有可显示的正文。' }}</p></section>
        <section v-if="store.inboxDetail.parsed" class="detail-section"><h3>相关操作</h3><p :data-testid="T.inboxDetail('op')">{{ capabilityText(store.inboxDetail.parsed.op) }}</p><p :data-testid="T.inboxDetail('target')">目标 {{ store.inboxDetail.parsed.target ?? '未指定' }}</p><p :data-testid="T.inboxDetail('sig')">签名{{ store.inboxDetail.parsed.sig_ok === true ? '已验证' : store.inboxDetail.parsed.sig_ok === false ? '未通过验证' : '状态未知' }}</p></section>
        <section class="detail-section"><h3>处理结果</h3><p :data-testid="T.inboxDetailResult">{{ store.inboxDetail.result?.code ?? mailInboxStatusText(store.inboxDetail.status, store.inboxDetail.reason).zh }}</p><p v-if="store.inboxDetail.reason" class="reason">{{ store.inboxDetail.reason }}</p><a-button v-if="store.inboxDetail.trace_id" :data-testid="T.inboxDetailTrace" @click="router.push({ path: '/log', query: { trace_id: store.inboxDetail.trace_id } })">查看相关日志</a-button></section>
      </template>
    </a-drawer>
    <a-modal v-model:open="tamperModal.open" title="已拒绝执行" :footer="null"><a-result status="error" :title="ARGS_DIGEST_MISMATCH_TEXT"><template #subTitle><span class="qt-mono qt-small">trace {{ tamperModal.traceId.slice(0, 8) }} · 需人工</span></template><template #extra><a-button @click="router.push('/log'); tamperModal.open = false">查看审计</a-button></template></a-result></a-modal>
  </div>
</template>

<style scoped>
.mail-page { position: relative; }
.mail-banner { display: flex; align-items: center; gap: 12px; width: 100%; margin-bottom: 16px; padding: 16px 20px; border: 1px solid #ecddc1; border-radius: 16px; background: #fff7e8; color: #805213; text-align: left; }
button.mail-banner { cursor: pointer; }.mail-banner span { flex: 1; }.is-warning { background: #fff1e9; color: #924b2e; }
.mail-health { display: flex; gap: 28px; padding: 24px 28px; margin-bottom: 24px; background: radial-gradient(ellipse at 0 100%, #f4e6ff99, transparent 60%), rgba(255,255,255,.75); }
.health-heading { display: flex; align-items: center; gap: 14px; flex: 0 0 180px; }.health-heading strong { display: block; font-size: 16px; }.health-heading span:not(.health-icon) { display: block; margin-top: 6px; font-size: 12px; color: var(--qt-text-secondary); }
.health-icon, .empty-icon, .pending-icon { display: grid; place-items: center; color: var(--qt-primary); background: #eee3fc; border: 1px solid #fff; width: 46px; height: 46px; border-radius: 16px; flex-shrink: 0; }.route-list { display: flex; flex: 1; gap: 18px 32px; flex-wrap: wrap; }.route-status { flex: 1; min-width: 230px; }.route-status > div { display: flex; gap: 12px; align-items: center; }.route-status p { margin: 8px 0 0; color: var(--qt-text-secondary); font-size: 12px; }.route-status .protocol-note { color: #976111; }
.mail-workspace { padding: 24px; }.mail-tabs { display: flex; gap: 6px; width: fit-content; padding: 5px; background: #f2eef6; border-radius: 28px; margin-bottom: 24px; }.mail-tabs button { display: flex; align-items: center; gap: 8px; border: 0; border-radius: 22px; background: transparent; padding: 10px 28px; color: var(--qt-text-secondary); cursor: pointer; }.mail-tabs button.active { background: var(--qt-primary); color: white; box-shadow: 0 4px 12px #7547a82b; }.tab-count { display: inline-grid; place-items: center; min-width: 22px; height: 22px; border-radius: 50%; font-size: 12px; background: #f3b849; color: #4d3516; }
.inbox-filters { margin-bottom: 20px; }.search-field { flex: 1; min-width: 240px; }.inbox-filters :deep(.ant-select) { width: 145px; }.date-filters { display: flex; align-items: flex-end; gap: 12px; width: 100%; flex-wrap: wrap; }.date-filters label { display: flex; flex-direction: column; gap: 6px; color: var(--qt-text-secondary); font-size: 12px; }.date-filters input { padding: 8px 12px; border: 1px solid var(--qt-border); border-radius: 9px; background: white; color: var(--qt-text); }.date-filters > span { padding-bottom: 8px; }
.table-scroll { overflow-x: auto; }.mail-table { width: 100%; border-collapse: collapse; min-width: 720px; }.mail-table thead { background: #faf9fc; color: var(--qt-text-secondary); }.mail-table th { padding: 14px 16px; font-size: 12px; font-weight: 500; text-align: left; }.mail-table td { padding: 20px 16px; border-bottom: 1px dashed #e9e4ef; vertical-align: middle; }.mail-table tbody tr:hover { background: #fdfbff; }.mail-table td:first-child { width: 37%; overflow-wrap: anywhere; }.subject-link { border: none; padding: 0; background: none; text-align: left; font-weight: 600; color: var(--qt-text); cursor: pointer; }.subject-link:hover { color: var(--qt-primary); }.row-secondary { margin-top: 6px; font-size: 12px; color: var(--qt-text-secondary); line-height: 1.6; }.time-cell { font-size: 12px; white-space: nowrap; color: var(--qt-text-secondary); }.status-chip { display: inline-block; padding: 4px 10px; border-radius: 20px; background: #f0eaf7; color: #70459a; font-size: 12px; white-space: nowrap; }.status-chip.attention { background: #fff0d6; color: #8a5a16; }.status-chip.failure { background: #fff0ee; color: #ae352d; }.reason { max-width: 340px; overflow-wrap: anywhere; }.list-footer { display: flex; justify-content: space-between; align-items: center; padding-top: 20px; color: var(--qt-text-secondary); font-size: 12px; }.row-actions { display: flex; gap: 8px; align-items: center; }
.outbox-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 22px; }.outbox-heading h2 { margin: 0; font-size: 18px; }.outbox-heading p { margin: 8px 0 0; color: var(--qt-text-secondary); }.outbox-heading :deep(.ant-select) { width: 160px; }
.pending-note { display: flex; gap: 12px; align-items: center; color: #825d23; background: #fff7e8; padding: 18px 20px; border-radius: 16px; margin-bottom: 20px; }.pending-note svg { flex-shrink: 0; }.pending-list { display: grid; gap: 16px; }.pending-card { padding: 22px; background: linear-gradient(120deg, #f5effba6, #fff8e599 140%); }.pending-top { display: flex; gap: 14px; align-items: center; }.pending-top h3 { margin: 0; font-size: 17px; }.pending-top p { margin: 7px 0 0; font-size: 12px; color: var(--qt-text-secondary); overflow-wrap: anywhere; }.ttl { font-size: 12px; color: #8b6224; white-space: nowrap; }.request-summary { display: flex; gap: 18px; padding: 16px 0; margin-top: 8px; font-size: 12px; }.request-summary span { color: var(--qt-text-secondary); flex-shrink: 0; }.request-summary code { overflow-wrap: anywhere; }.pending-card footer { display: flex; justify-content: space-between; align-items: center; gap: 16px; border-top: 1px solid #ece3f3; padding-top: 16px; }
.mail-empty, .list-empty { display: flex; flex-direction: column; align-items: center; text-align: center; padding: 68px 24px; color: var(--qt-text-secondary); }.mail-empty h2, .list-empty h3 { color: var(--qt-text); margin: 20px 0 8px; font-weight: 500; }.mail-empty p, .list-empty p { margin: 0 0 24px; }.mail-empty .empty-icon { width: 72px; height: 72px; border-radius: 24px; background: linear-gradient(145deg, #e6d6f8, #fff4d5); }.list-empty svg { color: #9e82bd; }
.detail-heading h2 { margin: 12px 0; font-size: 22px; overflow-wrap: anywhere; }.detail-heading p { color: var(--qt-text-secondary); font-size: 12px; }.detail-section { margin-top: 28px; padding-top: 22px; border-top: 1px solid var(--qt-border); }.detail-section h3 { font-size: 14px; }.mail-body { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.9; background: #faf8fd; padding: 20px; border-radius: 16px; }
@media (max-width: 1000px) { .mail-health { flex-direction: column; gap: 20px; }.health-heading { flex-basis: auto; } }
@media (max-width: 640px) { .mail-workspace, .mail-health { padding: 18px; }.mail-tabs { width: 100%; }.mail-tabs button { padding: 10px 16px; flex: 1; justify-content: center; }.inbox-filters { padding: 12px; }.search-field { min-width: 100%; }.pending-top { flex-wrap: wrap; }.pending-card footer { align-items: flex-start; flex-direction: column; }.outbox-heading { align-items: flex-start; flex-direction: column; }.route-status { min-width: 100%; } }
</style>
