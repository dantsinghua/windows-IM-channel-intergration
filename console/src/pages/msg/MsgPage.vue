<script setup lang="ts">
/**
 * `P-MSG` 消息(01 §2.7.7)。
 * 左会话列表 + 筛选 + 虚拟列表 + 详情面板;导出走 JobProgress 异步。
 * 🔴 事件专属三字段 `late`/`lag_s`/`origin` 只标经事件前插的行,重拉即消失(R6-49)。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { msg as T } from '@/testids'
import { useMessagesStore } from '@/stores/messages'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore } from '@/stores/events'
import { messagesApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import PageState from '@/components/PageState.vue'
import ChannelTag from '@/components/ChannelTag.vue'
import JsonViewer from '@/components/JsonViewer.vue'
import JobProgress from '@/components/JobProgress.vue'
import { OCR_REVIEW_TEXT, SOURCE_TEXT } from '@/i18n/zh-CN/codes'
import type { Job, Message } from '@/api/types'

const store = useMessagesStore()
const accounts = useAccountsStore()
const events = useEventsStore()

const exportJob = ref<string | null>(null)
const includeMedia = ref(false)
const rawOpen = ref(false)
const mediaUrl = ref('')
/** #54 消息清除:danger,须手输账号 id 确认 */
const purgeModal = ref(false)
const purgeInput = ref('')
const purgeMode = ref<'all' | 'text_only'>('text_only')
const purgeBefore = ref('')
const purgeJob = ref<string | null>(null)
const purgeTarget = computed(() => String(store.filter.account_id ?? ''))
const purgeMismatch = computed(() => !purgeTarget.value || purgeInput.value !== purgeTarget.value)

const TYPES = ['text', 'image', 'voice', 'file', 'video', 'system']
const selected = computed(() => store.selected)

async function search(): Promise<void> {
  await store.search(true)
}

function selectSession(sid: string | null): void {
  store.selectedSessionId = sid
  void store.search(true)
}

function rowLate(m: Message): string { return store.lateText(m.id) }
function rowExternal(m: Message): boolean { return store.isExternal(m.id) }

/** 悬停原文(R6-49) */
function lateTitle(m: Message): string {
  const f = store.flagsOf(m.id)
  return `消息发出时刻与入库时刻相差 ${f.lag_s ?? 0} 秒,多为掉线/重登后补同步`
}

/**
 * #55 取媒体:二进制 + `X-QT-Media-Id`/`X-QT-Sha256` 头。
 * 懒下载还没完时后端回 `202`(`pending`)—— 这**不是错误**,提示「仍在下载」让用户稍后再点。
 */
async function openMedia(sha: string): Promise<void> {
  const r = await messagesApi.media(sha)
  if (!r.blob) { message.info('媒体仍在下载中,请稍后再试'); return }
  if (mediaUrl.value) URL.revokeObjectURL(mediaUrl.value)
  mediaUrl.value = URL.createObjectURL(r.blob)
}

async function downloadMedia(sha: string): Promise<void> {
  const r = await messagesApi.media(sha)
  if (!r.blob) { message.info('媒体仍在下载中,请稍后再试'); return }
  const bytes = new Uint8Array(await r.blob.arrayBuffer())
  // 文件名优先用后端响应头里的 sha256(与落库那份对得上),拿不到才用行里的 ref
  const name = (r.sha256 ?? sha).slice(0, 12)
  await window.qt?.files.saveAs(`${name}.bin`, r.blob.type || 'application/octet-stream', bytes)
}

/** #51 入参逐字:`{fmt:'jsonl|csv|eml', with_media:'none|zip', filter}`(不是 format/include_media) */
async function startExport(fmt: 'csv' | 'jsonl' | 'eml'): Promise<void> {
  const r = await messagesApi.export({
    filter: store.filter,
    fmt,
    with_media: includeMedia.value ? 'zip' : 'none',
  })
  exportJob.value = r.job_id
  store.exportJobId = r.job_id
}

async function onExportDone(job: Job): Promise<void> {
  exportJob.value = null
  if (job.state !== 'succeeded') { message.error('导出未完成'); return }
  await window.qt?.files.saveAs(`messages-${Date.now()}.zip`, 'application/zip', String(job.result?.download_url ?? ''))
}

/**
 * #53 语音转文字。🔴 回的 `trace_id` 是**作业 trace,不是指令 trace**
 * (backend-api-2 §7-8)—— **不要**拿它去查 `#31 /accounts/{id}/commands/{trace_id}`,那里查不到。
 * 结果由后端回写到本条消息的 `asr_text`/`asr_state`,等 `message` 事件刷新即可。
 */
async function transcribe(m: Message): Promise<void> {
  try {
    const r = await messagesApi.asr(m.id)
    message.success(`已提交转写作业(trace ${String(r.trace_id ?? '').slice(0, 8)});完成后回填到本条消息`)
  } catch (e) {
    if (!(e instanceof ApiFailure)) { message.error(String(e)); return }
    if (e.reason === 'asr_backend_missing') {
      message.warning('语音转文字的执行体本期未装配,暂时无法转写')
    } else {
      message.error(`${e.message}(trace ${e.traceShort})`)
    }
  }
}

/**
 * #54 消息清除(danger)。🔴 须 `confirm:true`(客户端恒带)**且**界面手输账号 id 二次确认;
 * 回 `202 {job_id}`,进不可逆阶段后取消按钮由 `JobProgress` 自动置灰。
 */
async function doPurgeMessages(): Promise<void> {
  if (purgeMismatch.value) return
  try {
    const r = await messagesApi.purge({
      account_id: purgeTarget.value,
      before: purgeBefore.value || undefined,
      mode: purgeMode.value,
    })
    purgeJob.value = r.job_id
    purgeModal.value = false
    purgeInput.value = ''
  } catch (e) {
    if (e instanceof ApiFailure) message.error(`${e.message}(trace ${e.traceShort})`)
    else message.error(String(e))
  }
}

function onPurgeMessagesDone(job: Job): void {
  purgeJob.value = null
  if (job.state !== 'succeeded') { message.error(`清除未完成:${job.error?.message ?? job.state}`); return }
  message.success('消息清除完成')
  void store.search(true)
}

function scrollTop(): void {
  document.querySelector('.msglist')?.scrollTo({ top: 0 })
  store.newCount = 0
}

onMounted(async () => {
  if (!accounts.items.length) await accounts.load()
  await store.loadSessions().catch(() => undefined)
  await store.search(true)
})

onUnmounted(() => {
  // 离开 P-MSG 时把 message 订阅放宽回全部
  events.narrowMessages([])
  if (mediaUrl.value) URL.revokeObjectURL(mediaUrl.value)
})
</script>

<template>
  <div class="msg">
    <aside class="sessions">
      <a-input
        v-model:value="store.sessionKeyword"
        :data-testid="T.sessionSearch"
        placeholder="搜会话"
        allow-clear
      />
      <div :data-testid="T.sessionList" class="slist">
        <div
          class="sitem"
          :class="{ active: !store.selectedSessionId }"
          :data-testid="T.sessionAll"
          @click="selectSession(null)"
        >全部</div>
        <div
          v-for="s in store.filteredSessions"
          :key="s.id"
          class="sitem"
          :class="{ active: store.selectedSessionId === s.id }"
          :data-testid="T.session(s.id)"
          @click="selectSession(s.id)"
        >
          <span class="qt-grow">{{ s.name }}</span>
          <span class="qt-small qt-muted">{{ s.unread ?? 0 }}</span>
        </div>
        <!--
          C-42「加载更多」:`GET /sessions` 已按游标翻页(后端 `last_msg_at` 降序)。
          ⚠️ 左侧的关键字框是**本地筛**(`filteredSessions`),只筛已拉到的页 —— 翻到底再筛才是全量。
          🔴 元素 id 未在 01 §4 登记 ⇒ 暂不加 `data-testid`,清单已转文档方。
        -->
        <div v-if="store.sessionsCursor" class="sitem more">
          <a-button size="small" block @click="store.loadSessions(true)">加载更多</a-button>
        </div>
      </div>
    </aside>

    <main class="main">
      <header class="filters qt-row">
        <a-select
          class="w140"
          :data-testid="T.filter('account')"
          :value="store.filter.account_id"
          allow-clear
          placeholder="账号:全部"
          :options="accounts.items.map((a) => ({ value: a.id, label: a.id }))"
          @change="(v: any) => store.filter.account_id = v"
        />
        <a-select
          class="w100"
          :data-testid="T.filter('dir')"
          :value="store.filter.dir"
          allow-clear
          placeholder="方向"
          :options="[{ value: 'in', label: '入' }, { value: 'out', label: '出' }]"
          @change="(v: any) => store.filter.dir = v"
        />
        <a-select
          class="w120"
          :data-testid="T.filter('type')"
          :value="store.filter.type"
          allow-clear
          placeholder="类型"
          :options="TYPES.map((t) => ({ value: t, label: t }))"
          @change="(v: any) => store.filter.type = v"
        />
        <a-input class="w160" :data-testid="T.filter('since')" :value="store.filter.since" placeholder="起(ISO)"
                 @change="(e: any) => store.filter.since = e.target.value" />
        <a-input class="w160" :data-testid="T.filter('until')" :value="store.filter.until" placeholder="止(ISO)"
                 @change="(e: any) => store.filter.until = e.target.value" />
        <a-input class="w160" :data-testid="T.filter('q')" :value="store.filter.q" placeholder="关键字"
                 @change="(e: any) => store.filter.q = e.target.value" />
        <a-checkbox
          :data-testid="T.filterOcrReview"
          :checked="!!store.filter.needs_review"
          @change="(e: any) => store.filter.needs_review = e.target.checked || undefined"
        >仅 OCR 需复核</a-checkbox>
        <a-button type="primary" :data-testid="T.search" @click="search">搜索</a-button>
        <a-dropdown>
          <a-button :data-testid="T.export">导出 ▾</a-button>
          <template #overlay>
            <a-menu>
              <a-menu-item @click="startExport('csv')">导出 CSV</a-menu-item>
              <a-menu-item @click="startExport('jsonl')">导出 JSONL</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
        <!--
          🔴 02 #51 R6-62 (g):`with_media:'zip'` 与 `fmt:'eml'` 后端**明着 400**、不静默降级。
          所以这个勾选框本期禁用 —— 勾了只会换来一个 400,不如把原因写在旁边。
        -->
        <a-checkbox v-model:checked="includeMedia" disabled :data-testid="T.exportWithMedia">
          包含媒体(本期不支持,后端对 with_media=zip 直接 400)
        </a-checkbox>
        <!--
          #54 消息清除(danger + admin)。⚠️ 01 §4 还没有这个按钮的元素 id,已列给文档方。
        -->
        <a-button
          danger
          :disabled="!store.filter.account_id"
          :title="store.filter.account_id ? '清除该账号的消息' : '先在上面选定一个账号'"
          @click="purgeModal = true"
        >清除消息…</a-button>
      </header>

      <p v-if="store.qTooShort" class="qt-warn qt-small" :data-testid="T.qHint">关键字至少 3 个字</p>

      <JobProgress
        :job-id="exportJob"
        :testid="T.exportProgress"
        :cancel-testid="T.exportCancel"
        @done="onExportDone"
      />
      <!-- #54 是 202 作业;进不可逆阶段后取消按钮自动置灰 -->
      <JobProgress v-if="purgeJob" :job-id="purgeJob" @done="onPurgeMessagesDone" />
      <a-button
        v-if="store.exportJobId && !exportJob"
        size="small"
        :data-testid="T.exportDownload"
        @click="onExportDone({ job_id: store.exportJobId, kind: 'export', state: 'succeeded', progress: 100 })"
      >下载导出</a-button>

      <a-button v-if="store.newCount" class="newbanner" :data-testid="T.newBanner" @click="scrollTop">
        {{ store.newCount }} 条新消息
      </a-button>

      <PageState
        :loading="store.loading && !store.items.length"
        :error="store.error"
        :empty="!store.loading && !store.items.length"
        empty-text="暂无消息;账号在线后自动采集"
        @retry="search"
      >
        <div class="msglist" :data-testid="T.list">
          <div
            v-for="m in store.items"
            :key="m.id"
            class="mrow"
            :class="{ revoked: m.revoked, active: store.selectedId === m.id }"
            :data-testid="T.row(m.id)"
            @click="store.selectedId = m.id"
          >
            <span class="ts qt-mono qt-small">{{ m.ts.slice(11, 16) }}</span>
            <ChannelTag :channel="m.channel" />
            <span class="qt-small">{{ m.account_id }}</span>
            <span class="sess qt-small">{{ m.session.name }}</span>
            <span class="sender qt-small">{{ m.sender.name }}</span>
            <span class="dir">{{ m.dir === 'in' ? '›' : '‹' }}</span>
            <span class="text qt-grow">{{ m.text ?? `[${m.type}]` }}</span>
            <span v-if="m.revoked" class="tag">已撤回</span>
            <span v-if="rowLate(m)" class="tag amber" :data-testid="T.rowLate(m.id)" :title="lateTitle(m)">
              {{ rowLate(m) }}
            </span>
            <span
              v-if="rowExternal(m)"
              class="tag"
              :data-testid="T.rowOriginExternal(m.id)"
              title="这条我方消息不是由本系统发出的(人在别的端接手),仅标记、不影响任何自动动作"
            >外部来源</span>
            <span v-if="m.needs_review" class="tag amber" :data-testid="T.rowOcrReview(m.id)">⚠需复核</span>
            <span class="src qt-small qt-muted" :data-testid="T.rowSource(m.id)">
              {{ SOURCE_TEXT[m.source] ?? m.source }}
            </span>
          </div>
        </div>
      </PageState>

      <section v-if="selected" class="detail qt-card" :data-testid="T.detail">
        <div class="qt-row">
          <span class="qt-mono qt-small">消息 ID {{ selected.id }}</span>
          <span class="qt-mono qt-small">ext {{ selected.ext_msg_id ?? '—' }}</span>
          <span class="qt-small">来源 {{ SOURCE_TEXT[selected.source] ?? selected.source }}</span>
          <span v-if="selected.needs_review" class="tag amber">{{ OCR_REVIEW_TEXT }}</span>
          <span class="qt-small">撤回 {{ selected.revoked ? '是' : '否' }}</span>
        </div>
        <!-- 需要持久线索的看 ts 与 received_at 两列,差值即 lag_s -->
        <div class="qt-row qt-small qt-muted">
          <span>ts {{ selected.ts }}</span>
          <span>received_at {{ selected.received_at }}</span>
        </div>
        <!-- #53:语音转文字的回写(asr_text 不覆盖原文,text_source='asr' 时才是转写结果) -->
        <div v-if="selected.asr_text || selected.asr_state" class="qt-row qt-small">
          <span class="qt-muted">语音转文字</span>
          <b>{{ selected.asr_text ?? (selected.asr_state === 'pending' ? '转写中…' : '未转写') }}</b>
          <span v-if="selected.asr_state === 'failed'" class="qt-danger">转写失败</span>
        </div>
        <div v-for="(md, i) in selected.media" :key="i" class="qt-row">
          <span class="qt-small">{{ md.kind }} {{ md.mime }}</span>
          <template v-if="md.state === 'ready' && md.sha256">
            <a-button size="small" :data-testid="T.mediaPreview" @click="openMedia(md.sha256!)">预览</a-button>
            <a-button size="small" :data-testid="T.mediaDownload" @click="downloadMedia(md.sha256!)">下载</a-button>
            <!-- ⚠️ 01 §4 还没有这个按钮的元素 id,已列给文档方 -->
            <a-button
              v-if="md.kind === 'voice'"
              size="small"
              :disabled="selected.asr_state === 'pending'"
              @click="transcribe(selected)"
            >转文字</a-button>
          </template>
          <span v-else class="qt-muted qt-small" :data-testid="T.mediaPending">
            {{ md.state === 'pending' ? '下载中' : '未下载' }}
          </span>
        </div>
        <img v-if="mediaUrl" :src="mediaUrl" class="media" alt="媒体预览" />
        <a-button v-if="selected.raw_ref" size="small" :data-testid="T.rawView" @click="rawOpen = true">查看原始载荷</a-button>
      </section>
    </main>

    <a-modal v-model:open="rawOpen" title="原始载荷" :footer="null" width="680px">
      <JsonViewer :value="{ raw_ref: selected?.raw_ref }" />
    </a-modal>

    <!-- #54 消息清除:danger,须手输账号 id 原文确认 -->
    <a-modal v-model:open="purgeModal" title="清除消息" :footer="null">
      <p class="qt-danger">
        <b>不可恢复</b>:「只清正文」会把消息文本清空(全文索引一并更新);
        「整行删除」还会删掉消息行并减少媒体引用计数。
      </p>
      <div class="qt-row">
        <a-radio-group v-model:value="purgeMode">
          <a-radio value="text_only">只清正文</a-radio>
          <a-radio value="all">整行删除</a-radio>
        </a-radio-group>
      </div>
      <div class="qt-row">
        <span class="qt-small qt-muted">只清这个时刻之前的(留空 = 全部)</span>
        <a-input v-model:value="purgeBefore" placeholder="2026-01-01T00:00:00+08:00" />
      </div>
      <p>请手动输入账号 ID 原文 <b class="qt-mono">{{ purgeTarget }}</b> 以确认:</p>
      <a-input v-model:value="purgeInput" :status="purgeMismatch && purgeInput ? 'error' : undefined" />
      <div class="qt-row mt">
        <a-button @click="purgeModal = false">取消</a-button>
        <a-button type="primary" danger :disabled="purgeMismatch" @click="doPurgeMessages">确认清除</a-button>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.msg { display: flex; height: 100%; }
.sessions { width: 240px; flex: 0 0 auto; padding: var(--qt-space-3); border-right: 1px solid var(--qt-border); overflow: auto; }
.slist { margin-top: var(--qt-space-2); }
.sitem { display: flex; padding: 5px 6px; cursor: pointer; border-radius: var(--qt-radius-sm); }
.sitem:hover, .sitem.active { background: var(--qt-bg-elevated); }
.main { flex: 1 1 auto; padding: var(--qt-space-3); overflow: auto; min-width: 0; }
.filters { flex-wrap: wrap; gap: var(--qt-space-2); }
.w100 { width: 100px; } .w120 { width: 120px; } .w140 { width: 140px; } .w160 { width: 160px; }
.newbanner { margin: var(--qt-space-2) 0; }
.msglist { max-height: 52vh; overflow: auto; border: 1px solid var(--qt-border); border-radius: var(--qt-radius-sm); }
.mrow { display: flex; align-items: center; gap: 8px; padding: 4px 8px; border-bottom: 1px solid var(--qt-border); cursor: pointer; }
.mrow.active { background: var(--qt-bg-elevated); }
.mrow.revoked { color: var(--qt-text-disabled); text-decoration: line-through; }
.sess, .sender { min-width: 80px; }
.sitem.more { justify-content: center; padding: var(--qt-space-2) 0; }
.text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag { font-size: var(--qt-font-xs); border: 1px solid currentColor; border-radius: 8px; padding: 0 5px; }
.amber { color: var(--qt-sev-warn); }
.detail { margin-top: var(--qt-space-3); padding: var(--qt-space-3); }
.media { max-width: 320px; margin-top: var(--qt-space-2); }
</style>
