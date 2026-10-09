<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { message } from 'ant-design-vue'
import { msg as T } from '@/testids'
import { useMessagesStore } from '@/stores/messages'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore } from '@/stores/events'
import { messagesApi } from '@/api/client'
import PageState from '@/components/PageState.vue'
import ChannelTag from '@/components/ChannelTag.vue'
import JobProgress from '@/components/JobProgress.vue'
import QtIcon from '@/components/QtIcon.vue'
import { localDateTimeToIso, isoToLocalDateTime } from '@/utils/datetime'
import { SOURCE_TEXT } from '@/i18n/zh-CN/codes'
import type { Job, Message } from '@/api/types'
const store = useMessagesStore()
const accounts = useAccountsStore()
const events = useEventsStore()
const route = useRoute()
const detailOpen = ref(false)
const datesOpen = ref(false)
const exportJob = ref<string | null>(null)
const mediaUrl = ref('')
const mediaKind = ref('')
const selected = computed(() => store.selected)
const types = [{value:'text',label:'文字'},{value:'image',label:'图片'},{value:'voice',label:'语音'},{value:'file',label:'文件'},{value:'video',label:'视频'},{value:'system',label:'系统通知'}]
const selectedAccount = computed(() => accounts.items.find(a => a.id === store.filter.account_id))
function accountName(id: string): string { const a=accounts.byId[id]; return a?.self_nick || a?.label || id }
function time(ts: string): string { return new Date(ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) }
async function search(): Promise<void> { if(store.qTooShort) return; await store.search(true) }
function openDetail(m: Message): void { store.selectedId = m.id; detailOpen.value = true; clearMedia() }
function clearMedia(): void { if(mediaUrl.value) URL.revokeObjectURL(mediaUrl.value); mediaUrl.value = '' }
async function selectSession(id: string | null): Promise<void> { store.selectedSessionId = id; await search() }
async function changeAccount(id?: string): Promise<void> { store.filter.account_id=id; store.selectedSessionId=null; await Promise.all([store.loadSessions(),search()]) }
async function startExport(): Promise<void> {
  try { const r=await messagesApi.export({fmt:'csv',with_media:'none',filter:{...store.filter,...(store.selectedSessionId ? {session_id:store.selectedSessionId} : {})}}); exportJob.value=r.job_id }
  catch(e) { message.error(e instanceof Error ? e.message : String(e)) }
}
async function onExportDone(job: Job): Promise<void> {
  exportJob.value=null
  if(job.state!=='succeeded'){message.error('导出未完成，请重试');return}
  if(!window.qt){message.info('导出已完成，请在桌面控制台保存文件');return}
  try { await window.qt.files.saveAs(`messages-${Date.now()}.zip`,'application/zip',String(job.result?.download_url ?? '')) }
  catch (e) { message.error(e instanceof Error ? e.message : '文件保存未完成，请重新导出') }
}
async function openMedia(sha: string,kind: string): Promise<void> {
  try { const r=await messagesApi.media(sha); if(!r.blob){message.info('媒体正在下载，请稍后重试');return} clearMedia(); mediaUrl.value=URL.createObjectURL(r.blob);mediaKind.value=kind }
  catch(e){message.error(e instanceof Error?e.message:String(e))}
}
async function downloadMedia(sha: string): Promise<void> {
  try{const r=await messagesApi.media(sha);if(!r.blob){message.info('媒体正在下载');return}if(window.qt){await window.qt.files.saveAs(`${(r.sha256??sha).slice(0,12)}.bin`,r.blob.type,new Uint8Array(await r.blob.arrayBuffer()))}else{message.info('请在桌面控制台保存附件')}}
  catch(e){message.error(e instanceof Error?e.message:String(e))}
}
function applyRoute(): void { if(typeof route.query.account_id==='string'){store.filter.account_id=route.query.account_id;store.selectedSessionId=null} }
watch(()=>route.query.account_id,async()=>{applyRoute();await Promise.all([store.loadSessions(),search()])})
onMounted(async()=>{applyRoute();if(!accounts.items.length)await accounts.load();await store.loadSessions().catch(()=>undefined);await store.search(true)})
onUnmounted(()=>{events.narrowMessages([]);clearMedia()})
</script>
<template>
  <div class="qt-page messages-page">
    <header class="qt-page-heading"><div><span class="qt-eyebrow">MESSAGE ARCHIVE</span><h1>历史消息</h1><p>{{ selectedAccount ? `${selectedAccount.self_nick || selectedAccount.label || selectedAccount.id} 的消息记录` : '按账号或关键词，找到每一段对话。' }}</p></div><a-button :data-testid="T.export" @click="startExport">导出查询结果</a-button></header>
    <div class="message-workspace">
      <aside class="sessions qt-glass"><div class="session-title">会话 <span>{{ store.sessions.length }}</span></div><a-input v-model:value="store.sessionKeyword" :data-testid="T.sessionSearch" placeholder="搜索已加载会话" allow-clear><template #prefix><QtIcon name="search" :size="15" /></template></a-input><div class="slist" :data-testid="T.sessionList"><button class="sitem all" :class="{active:!store.selectedSessionId}" :data-testid="T.sessionAll" @click="selectSession(null)"><QtIcon name="msg" :size="18" /><span>全部会话</span></button><button v-for="s in store.filteredSessions" :key="s.id" class="sitem" :class="{active:store.selectedSessionId===s.id}" :data-testid="T.session(s.id)" @click="selectSession(s.id)"><span class="session-avatar">{{ s.name.slice(0,1) }}</span><span class="session-info"><strong>{{ s.name }}</strong><small>{{ accountName(s.account_id) }}</small></span><span v-if="s.unread" class="unread">{{ s.unread }}</span></button></div><a-button v-if="store.sessionsCursor" block size="small" @click="store.loadSessions(true)">更多会话</a-button><span class="session-note">搜索范围为已加载的会话</span></aside>
      <main class="message-main qt-surface">
        <div class="search-row"><a-input v-model:value="store.filter.q" :data-testid="T.filter('q')" placeholder="搜索消息内容，至少 3 个字" allow-clear @press-enter="search"><template #prefix><QtIcon name="search" :size="17" /></template></a-input><a-button type="primary" :data-testid="T.search" :loading="store.loading" @click="search">查询</a-button></div>
        <div class="filter-row"><a-select :value="store.filter.account_id" :data-testid="T.filter('account')" allow-clear placeholder="全部账号" :options="accounts.items.map(a=>({value:a.id,label:a.self_nick||a.label||a.id}))" @change="(v:any)=>changeAccount(v)"/><a-select v-model:value="store.filter.dir" :data-testid="T.filter('dir')" allow-clear placeholder="全部方向" :options="[{value:'in',label:'收到的消息'},{value:'out',label:'发出的消息'}]"/><a-select v-model:value="store.filter.type" :data-testid="T.filter('type')" allow-clear placeholder="全部类型" :options="types"/><button class="time-toggle" :aria-expanded="datesOpen" @click="datesOpen=!datesOpen"><QtIcon name="clock" :size="15" />时间范围</button></div>
        <div v-if="datesOpen" class="date-row"><a-input :value="isoToLocalDateTime(store.filter.since)" @update:value="store.filter.since = localDateTimeToIso($event)" :data-testid="T.filter('since')" type="datetime-local" aria-label="开始时间"/><span>至</span><a-input :value="isoToLocalDateTime(store.filter.until)" @update:value="store.filter.until = localDateTimeToIso($event)" :data-testid="T.filter('until')" type="datetime-local" aria-label="结束时间"/></div>
        <p v-if="store.qTooShort" class="qt-warn" :data-testid="T.qHint">请输入至少 3 个字后查询</p><JobProgress :job-id="exportJob" :testid="T.exportProgress" :cancel-testid="T.exportCancel" @done="onExportDone"/>
        <div class="list-heading"><span>消息记录</span><span>已加载 {{ store.items.length }} 条{{ store.nextCursor ? ' · 还有更多' : '' }}</span></div>
        <button v-if="store.newCount" class="new-messages" :data-testid="T.newBanner" @click="store.search(true)">{{ store.newCount }} 条新消息 · 刷新列表</button>
        <PageState :loading="store.loading&&!store.items.length" :error="store.error" :empty="!store.loading&&!store.items.length" empty-text="没有找到消息，试试其他关键词或时间范围" @retry="search"><div class="msglist" :data-testid="T.list"><button v-for="m in store.items" :key="m.id" class="mrow" :class="{revoked:m.revoked,active:store.selectedId===m.id}" :data-testid="T.row(m.id)" @click="openDetail(m)"><span class="message-avatar" :class="m.channel">{{ (m.sender.name||'消').slice(0,1) }}</span><div class="message-body"><div class="message-title"><strong>{{ m.sender.name || '未知发送人' }}</strong><ChannelTag :channel="m.channel"/><span class="message-direction">{{ m.dir==='in' ? '接收' : '发送' }}</span><time>{{ time(m.ts) }}</time></div><p>{{ m.revoked ? '此消息已撤回' : m.text || `[${types.find(t=>t.value===m.type)?.label||m.type}]` }}</p><div class="message-meta"><span>{{ m.session.name }}</span><span>·</span><span>{{ accountName(m.account_id) }}</span><span v-if="store.lateText(m.id)" :data-testid="T.rowLate(m.id)" class="meta-warning">{{ store.lateText(m.id) }}</span><span v-if="store.isExternal(m.id)" :data-testid="T.rowOriginExternal(m.id)">外部来源</span><span v-if="m.needs_review" :data-testid="T.rowOcrReview(m.id)" class="meta-warning">需复核</span></div></div><QtIcon name="chevron" :size="15"/></button></div><div class="list-footer"><span>按消息时间排序</span><a-button v-if="store.nextCursor" :loading="store.loading" @click="store.search(false)">加载更多消息</a-button><span v-else>已显示全部查询结果</span></div></PageState>
      </main>
    </div>
    <a-drawer v-model:open="detailOpen" title="消息详情" :width="520" :data-testid="T.detail"><template v-if="selected"><div class="detail-identity"><span class="message-avatar" :class="selected.channel">{{ (selected.sender.name||'消').slice(0,1) }}</span><div><h3>{{ selected.sender.name }}</h3><span>{{ time(selected.ts) }} · {{ selected.session.name }}</span></div></div><div class="detail-text">{{ selected.revoked?'此消息已撤回':selected.text||'媒体消息' }}</div><div class="detail-meta"><span>所属账号</span><a :href="`#/acct/${selected.account_id}`">{{ accountName(selected.account_id) }}</a><span>消息方向</span><b>{{ selected.dir==='in'?'接收':'发送' }}</b><span>采集来源</span><b>{{ SOURCE_TEXT[selected.source]??selected.source }}</b><span>入库时间</span><b>{{ time(selected.received_at) }}</b></div><div v-for="(md,i) in selected.media" :key="i" class="media-item"><span>{{ types.find(t=>t.value===md.kind)?.label||md.kind }}</span><template v-if="md.state==='ready'&&md.sha256"><a-button size="small" :data-testid="T.mediaPreview" @click="openMedia(md.sha256!,md.kind)">预览</a-button><a-button size="small" :data-testid="T.mediaDownload" @click="downloadMedia(md.sha256!)">保存</a-button></template><span v-else :data-testid="T.mediaPending">正在准备媒体</span></div><img v-if="mediaUrl&&mediaKind==='image'" :src="mediaUrl" class="media-preview" alt="消息图片"/><audio v-else-if="mediaUrl&&mediaKind==='voice'" :src="mediaUrl" controls/><video v-else-if="mediaUrl&&mediaKind==='video'" :src="mediaUrl" controls class="media-preview"/><p v-else-if="mediaUrl">此附件请保存后查看</p></template></a-drawer>
  </div>
</template>
<style scoped>
.message-workspace{display:grid;grid-template-columns:235px minmax(0,1fr);gap:24px;align-items:start}.sessions{padding:20px 14px;min-height:620px}.session-title{display:flex;justify-content:space-between;font-size:15px;font-weight:600;margin:4px 4px 20px}.session-title span{color:#a292b2;font-size:12px;font-weight:400}.slist{margin:18px 0}.sitem{width:100%;display:flex;align-items:center;gap:10px;padding:13px 10px;border:0;border-radius:12px;background:transparent;text-align:left;color:#7e708c;cursor:pointer;margin:4px 0}.sitem:hover{background:#ffffffa0}.sitem.active{background:#fff;box-shadow:0 3px 14px #64438509;color:#7547a8}.sitem.all{gap:12px;margin-bottom:15px}.session-avatar,.message-avatar{display:grid;place-items:center;flex-shrink:0;background:#ede5f6;border:1px solid #fff;color:#9370b2;border-radius:12px;width:36px;height:36px}.session-info{min-width:0;flex:1}.session-info strong{display:block;font-weight:500;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.session-info small{display:block;font-size:12px;color:#a195ad;margin-top:5px}.unread{font-size:12px;background:#7547a8;color:#fff;border-radius:10px;padding:2px 5px}.session-note{font-size:12px;color:#a294b1}.message-main{padding:24px 26px;min-width:0;min-height:660px}.search-row{display:flex;gap:10px}.filter-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}.filter-row .ant-select{min-width:130px;flex:1;max-width:190px}.time-toggle{display:flex;align-items:center;gap:6px;border:0;background:transparent;color:#8d7b9f;font-size:12px;cursor:pointer;padding:8px}.date-row{display:flex;align-items:center;gap:10px;margin-top:12px}.list-heading{display:flex;justify-content:space-between;font-size:12px;color:#a496ae;margin-top:26px;padding-bottom:14px;border-bottom:1px solid #eee8f4}.list-heading span:first-child{font-size:14px;color:#64566f;font-weight:600}.msglist{max-height:640px;overflow:auto}.mrow{display:flex;align-items:flex-start;gap:14px;width:100%;border:0;border-bottom:1px solid #f0edf4;background:white;padding:20px 0;cursor:pointer;text-align:left;color:var(--qt-text)}.mrow:hover,.mrow.active{background:#fcfaff}.message-avatar{width:40px;height:40px;border-radius:13px;font-size:14px;background:linear-gradient(145deg,#e8def5,#f6f0fc)}.message-avatar.wechat{background:linear-gradient(145deg,#f6e5ba,#fff5dd);color:#aa8433}.message-avatar.qq{background:linear-gradient(145deg,#ded8f2,#f0ecfb);color:#7d6da5}.message-body{flex:1;min-width:0}.message-title{display:flex;gap:10px;align-items:center;font-size:13px;flex-wrap:wrap}.message-title strong{font-weight:600}.message-title:deep(.tag){font-size:12px}.message-title time{margin-left:auto;color:#80718c;font-size:12px}.message-direction{font-size:12px;color:#80718c}.message-body p{margin:9px 0 8px;font-size:14px;line-height:1.7;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#5d5267}.message-meta{display:flex;gap:7px;flex-wrap:wrap;font-size:12px;color:#7e708a}.meta-warning{color:#9c6d17}.mrow>svg{margin-top:5px;color:#baaaca}.revoked .message-body p{color:#83708e}.list-footer{display:flex;justify-content:space-between;align-items:center;padding-top:20px;color:#81718d;font-size:12px}.new-messages{width:100%;border:0;padding:9px;background:#f1eaf8;color:#7547a8;cursor:pointer}.detail-identity{display:flex;align-items:center;gap:14px;margin:12px 0 24px}.detail-identity h3{margin:0 0 6px}.detail-identity span{font-size:12px;color:#a093ab}.detail-text{padding:22px;background:#f8f5fb;border-radius:16px;font-size:16px;line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere}.detail-meta{display:grid;grid-template-columns:95px 1fr;gap:18px;font-size:13px;padding:28px 0;color:#97889f}.detail-meta b{font-weight:400;color:#5c4e69}.media-item{display:flex;gap:10px;align-items:center;margin:15px 0}.media-preview{max-width:100%;border-radius:12px}@media(max-width:1000px){.message-workspace{grid-template-columns:180px minmax(0,1fr);gap:16px}.message-main{padding:20px}.message-title time{width:100%;margin-left:0}}@media(max-width:740px){.message-workspace{grid-template-columns:1fr}.sessions{min-height:0}.slist{display:flex;overflow:auto;max-height:160px;margin:10px 0}.sitem{min-width:160px}.session-note{display:none}.message-main{padding:16px}.date-row{flex-wrap:wrap}.message-title{gap:6px}}
</style>
