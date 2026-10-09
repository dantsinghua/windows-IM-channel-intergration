<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { message } from 'ant-design-vue'
import { log as T } from '@/testids'
import { useAuditStore, type AuditKind } from '@/stores/audit'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore, alertRoute } from '@/stores/events'
import { auditDetail, auditTsText, type AuditRow } from '@/api/types'
import PageState from '@/components/PageState.vue'
import QtIcon from '@/components/QtIcon.vue'
import { localDateTimeToIso, isoToLocalDateTime } from '@/utils/datetime'
const route = useRoute()
const store = useAuditStore()
const accounts = useAccountsStore()
const events = useEventsStore()
const activeTab = ref<AuditKind|'alerts'>(route.query.tab === 'alerts' ? 'alerts' : 'system')
const keyword = ref('')
const severity = ref<string>()
const traceOpen = ref(false)
const traceId = ref('')
const datesOpen = ref(false)
const tabs = [{key:'system',label:'系统日志'},{key:'command',label:'操作记录'},{key:'alerts',label:'告警'}] as const
const alerts = computed(()=>[...events.alerts.values()].filter(a=>{
  if(severity.value&&a.severity!==severity.value)return false
  if(store.filter.account_id&&a.subject!==`account:${store.filter.account_id}`)return false
  return `${a.title} ${a.message} ${a.code} ${a.subject}`.toLowerCase().includes(keyword.value.toLowerCase())
}))
const rows = computed(()=>store.rows.filter(r=>`${r.action} ${r.result_code} ${r.account_id} ${r.actor}`.toLowerCase().includes(keyword.value.toLowerCase())))
function accountName(id?: string|null): string { return id ? accounts.byId[id]?.label || id : '系统' }
function tsText(r: AuditRow): string { return auditTsText(r) }
function resultText(r: AuditRow): string { const code=r.result_code;return code==='OK'?'成功':code==='PENDING'?'处理中':code||'已记录' }
async function openTrace(tid: string): Promise<void> {traceId.value=tid;await store.loadTrace(tid);traceOpen.value=true}
async function search(): Promise<void> { if(activeTab.value!=='alerts')await store.load() }
async function exportCsv(): Promise<void> {
  if(!window.qt){message.info('请在桌面控制台导出日志');return}
  const cols=['id','ts_ms','kind','transport','actor','action','account_id','trace_id','result_code','detail_json'] as const
  const csvCell=(value:unknown)=>`"${String(typeof value==='object'&&value!==null?JSON.stringify(value):value??'').replace(/"/g,'""')}"`
  const lines=[cols.join(','),...rows.value.map(r=>cols.map(k=>csvCell(r[k as keyof AuditRow])).join(','))]
  try { await window.qt.files.saveAs(`audit-${store.kind}-${Date.now()}.csv`,'text/csv',lines.join('\n')) }
  catch (e) { message.error(e instanceof Error ? e.message : '日志导出未完成，请重试') }
}
function applyRoute(): void { if(typeof route.query.account_id==='string')store.filter.account_id=route.query.account_id;if(route.query.kind==='system'||route.query.kind==='command')activeTab.value=route.query.kind;if(route.query.tab==='alerts')activeTab.value='alerts' }
watch(activeTab,async tab=>{if(tab!=='alerts'){store.kind=tab;await store.load()}})
watch(()=>route.query.account_id,async()=>{applyRoute();await search()})
watch(()=>route.query.tab,()=>{if(route.query.tab==='alerts')activeTab.value='alerts'})
onMounted(async()=>{applyRoute();if(!accounts.items.length)void accounts.load();store.kind=activeTab.value==='alerts'?'system':activeTab.value;await store.load();if(typeof route.query.trace_id==='string')await openTrace(route.query.trace_id)})
</script>
<template>
  <div class="qt-page logs-page"><header class="qt-page-heading"><div><span class="qt-eyebrow">ACTIVITY & HEALTH</span><h1>日志与告警</h1><p>了解发生了什么，快速定位需要处理的问题。</p></div><a-button v-if="activeTab!=='alerts'" :data-testid="T.export" @click="exportCsv">导出当前列表</a-button></header>
    <div class="logs-overview"><div class="overview-item qt-glass"><span class="overview-icon"><QtIcon name="bell"/></span><div><span>需要关注</span><strong>{{ events.firing.length }}<small>条活动告警</small></strong></div></div><div class="overview-item qt-glass"><span class="overview-icon gold"><QtIcon name="shield"/></span><div><span>事件记录</span><strong>{{ store.rawRows.length }}<small>条已加载日志</small></strong></div></div><div class="overview-description"><QtIcon name="clock" :size="18"/><span>日志可按账号与时间查询<br/><small>告警仅显示本次连接已收到的记录</small></span></div></div>
    <section class="qt-surface log-surface"><div class="log-tabs" role="tablist" aria-label="记录类型"><button v-for="tab in tabs" :key="tab.key" role="tab" :aria-selected="activeTab===tab.key" :class="{active:activeTab===tab.key}" :data-testid="T.tab(tab.key)" @click="activeTab=tab.key">{{ tab.label }}<span v-if="tab.key==='alerts'&&events.firing.length">{{ events.firing.length }}</span></button></div>
      <div class="log-toolbar"><a-input v-model:value="keyword" placeholder="在已加载记录中搜索" allow-clear @press-enter="search"><template #prefix><QtIcon name="search" :size="16"/></template></a-input><a-select v-model:value="store.filter.account_id" :data-testid="T.filter('account')" allow-clear placeholder="全部账号" :options="accounts.items.map(a=>({value:a.id,label:a.self_nick||a.label||a.id}))"/><a-select v-if="activeTab==='alerts'" v-model:value="severity" allow-clear placeholder="全部级别" :options="[{value:'info',label:'提醒'},{value:'warn',label:'警告'},{value:'crit',label:'严重'}]"/><button v-if="activeTab!=='alerts'" class="time-toggle" :aria-expanded="datesOpen" @click="datesOpen=!datesOpen"><QtIcon name="clock" :size="16"/>时间</button><a-button type="primary" :data-testid="T.search" :loading="store.loading" @click="search">查询</a-button></div>
      <div v-if="datesOpen&&activeTab!=='alerts'" class="date-row"><a-input :value="isoToLocalDateTime(store.filter.since)" @update:value="store.filter.since = localDateTimeToIso($event)" :data-testid="T.filter('since')" type="datetime-local" aria-label="开始时间"/><span>至</span><a-input :value="isoToLocalDateTime(store.filter.until)" @update:value="store.filter.until = localDateTimeToIso($event)" :data-testid="T.filter('until')" type="datetime-local" aria-label="结束时间"/></div>
      <template v-if="activeTab==='alerts'"><div class="alert-head"><span>告警事件</span><span>{{ alerts.length }} 条</span></div><div v-for="a in alerts" :key="a.code+a.subject" class="alert-row"><span class="severity-icon" :class="a.severity"><QtIcon name="bell" :size="18"/></span><div class="alert-body"><div><strong>{{ a.title||a.code }}</strong><span class="severity-label">{{ a.severity==='crit'?'严重':a.severity==='warn'?'警告':'提醒' }}</span></div><p>{{ a.message }}</p><small>{{ a.subject }} · {{ a.last_seen_at ? new Date(a.last_seen_at).toLocaleString('zh-CN') : '时间未知' }}</small></div><span class="alert-status" :class="{resolved:a.state==='resolved'}">{{ a.state==='resolved'?'已恢复':'待处理' }}</span><a :href="`#${alertRoute(a)}`">查看</a></div><a-empty v-if="!alerts.length" description="暂无匹配的告警"/><p class="scope-note">此列表不是完整的近 7 天历史；连接断开前或保留期之外的告警可能不在此处。</p></template>
      <PageState v-else :loading="store.loading&&!store.rows.length" :error="store.error" :empty="!store.loading&&!rows.length" empty-text="没有找到日志，试试其他时间或账号" @retry="search"><div class="table-scroll"><table class="tbl" :data-testid="T.table"><thead><tr><th>发生时间</th><th>账号 / 对象</th><th>操作</th><th>结果</th><th>来源</th><th/></tr></thead><tbody><tr v-for="(r,i) in rows" :key="r.id" :data-testid="T.row(i)"><td class="time-cell">{{ tsText(r) }}</td><td><span class="object-label">{{ accountName(r.account_id) }}</span></td><td>{{ r.action||auditDetail(r).op||'系统事件' }}</td><td><span class="result-pill" :class="{success:r.result_code==='OK',failure:r.result_code&&r.result_code!=='OK'&&r.result_code!=='PENDING'}">{{ resultText(r) }}</span></td><td class="source-cell">{{ r.transport||'本机' }}</td><td><button v-if="r.trace_id" class="detail-button" :data-testid="T.rowTrace(i)" @click="openTrace(r.trace_id)">详情</button></td></tr></tbody></table></div><div class="list-footer"><span>已加载 {{ store.rawRows.length }} 条，显示 {{ rows.length }} 条</span><a-button v-if="store.nextCursor" :data-testid="T.loadMore" :loading="store.loading" @click="store.load(true)">加载更多</a-button><span v-else>已加载至末尾</span></div></PageState>
    </section>
    <a-drawer v-model:open="traceOpen" title="事件详情" :width="520" :data-testid="T.traceDrawer"><p class="trace-ref">追踪编号 {{ traceId.slice(0,8) }}</p><div v-for="r in store.traceRows" :key="r.id" class="trace-row"><span class="trace-point"/><time>{{ tsText(r) }}</time><h3>{{ r.action||'系统事件' }}</h3><p>{{ resultText(r) }} · {{ accountName(r.account_id) }}</p></div><a-empty v-if="!store.traceRows.length" description="当前查询范围内未找到详情"/><p class="scope-note">审计仅展示操作记录，不包含消息正文。详情从最近 200 条日志中匹配。</p></a-drawer>
  </div>
</template>
<style scoped>
.logs-overview{display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:20px;margin:26px 0}.overview-item{padding:22px;display:flex;align-items:center;gap:18px}.overview-icon{height:46px;width:46px;border-radius:15px;background:#eae0f4;color:#9265b5;display:grid;place-items:center}.overview-icon.gold{background:#faeccd;color:#b58b30}.overview-item div>span{font-size:12px;color:#9a8da4}.overview-item strong{display:block;font-size:28px;font-weight:600;margin-top:6px}.overview-item small{font-size:12px;font-weight:400;color:#7e6c8b;margin-left:10px}.overview-description{display:flex;align-items:center;gap:14px;color:#91839e;padding:20px;font-size:13px;line-height:1.9}.overview-description small{font-size:12px;color:#a599af}.log-surface{padding:14px 26px 24px}.log-tabs{display:flex;gap:6px;padding:8px 0 20px;border-bottom:1px solid #eee8f4}.log-tabs button{border:0;padding:9px 20px;background:transparent;color:#97899f;border-radius:20px;cursor:pointer;font-size:14px}.log-tabs button.active{background:#7547a8;color:white;box-shadow:0 5px 12px #7547a81a}.log-tabs button span{font-size:12px;margin-left:7px}.log-toolbar{display:flex;gap:12px;margin:22px 0;align-items:center}.log-toolbar>.ant-input-affix-wrapper{max-width:340px}.log-toolbar .ant-select{width:170px;flex-shrink:0}.time-toggle{border:0;background:transparent;color:#937fa4;display:flex;gap:5px;align-items:center;cursor:pointer;font-size:13px}.date-row{display:flex;align-items:center;gap:12px;max-width:560px;margin-bottom:18px}.table-scroll{overflow:auto}.tbl{min-width:630px;width:100%;border-collapse:collapse}.tbl th{font-size:12px}.tbl td{font-size:13px}.time-cell,.source-cell{color:#998ca4;font-size:12px}.object-label{font-weight:500;color:#665471}.result-pill{font-size:12px;padding:4px 9px;background:#f1ebf8;color:#8b67a9;border-radius:12px;white-space:nowrap}.result-pill.success{background:#edf6f1;color:#599573}.result-pill.failure{background:#fff0eb;color:#ae6852}.detail-button{border:0;background:transparent;color:#9476af;cursor:pointer;font-size:12px}.list-footer{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#81708d;padding-top:24px}.alert-head{display:flex;justify-content:space-between;padding:12px 0;color:#9c8ca7;font-size:12px}.alert-row{display:flex;align-items:center;gap:16px;padding:22px 0;border-top:1px solid #f0eaf6}.severity-icon{width:40px;height:40px;display:grid;place-items:center;border-radius:13px;background:#f3edf9;color:#9368b0;flex-shrink:0}.severity-icon.warn{background:#fdf2d9;color:#ad832f}.severity-icon.crit{background:#fbece9;color:#ba6b60}.alert-body{flex:1;min-width:0}.alert-body strong{font-size:14px;font-weight:600}.severity-label{font-size:12px;color:#a38cac;margin-left:10px}.alert-body p{font-size:13px;color:#8e7d9a;margin:6px 0;overflow-wrap:anywhere}.alert-body small{font-size:12px;color:#887194}.alert-status{font-size:12px;color:#b28b40;white-space:nowrap}.alert-status.resolved{color:#699c7b}.alert-row>a{font-size:12px}.scope-note{font-size:12px;line-height:1.8;color:#a995b3;margin-top:24px}.trace-ref{font-size:12px;color:#a394ae}.trace-row{position:relative;border-left:1px solid #e6dced;padding:12px 0 18px 24px;margin-left:8px}.trace-point{position:absolute;left:-4px;top:19px;width:7px;height:7px;border-radius:50%;background:#a27cbf}.trace-row time{font-size:12px;color:#ab9bb7}.trace-row h3{font-size:14px;margin:10px 0}.trace-row p{font-size:12px;color:#9b88a7}@media(max-width:950px){.logs-overview{grid-template-columns:1fr 1fr}.overview-description{display:none}.log-toolbar{flex-wrap:wrap}.log-toolbar .ant-select{width:145px}}@media(max-width:600px){.logs-overview{gap:12px}.overview-item{padding:15px;gap:10px}.overview-icon{display:none}.log-surface{padding:14px}.log-tabs button{padding:9px 13px}.alert-row{gap:10px}.alert-status{display:none}.date-row{flex-wrap:wrap}}
</style>
