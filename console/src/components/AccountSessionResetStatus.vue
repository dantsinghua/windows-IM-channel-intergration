<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { accountsApi } from '@/api/client'
import type { AccountSessionResetStatusData, SessionResetPhase } from '@/api/types'

const props = defineProps<{ accountId: string }>()
const data = ref<AccountSessionResetStatusData | null>(null)
const loading = ref(false)
const failed = ref(false)
let generation = 0
let disposed = false
let poll: ReturnType<typeof setInterval> | undefined
let active: { controller: AbortController; timeout: ReturnType<typeof setTimeout> } | undefined

const labels: Record<SessionResetPhase, string> = {
  waiting_native: '等待本机任务结束',
  barrier_ready: '采集端已准备，等待工作台应用',
  complete: '双端会话状态已对齐',
  blocked: '会话同步受阻',
}
const channel = computed(() => data.value?.channel_status)
const nativeLabel = computed(() => channel.value?.native_state === 'online' ? '在线' : channel.value?.native_state === 'offline' ? '未登录或已退出' : '尚未确认')
function operationLabel(key: string): string {
  if (key.startsWith('directory')) return '目录心跳上传'
  if (key.startsWith('contacts:')) return '通讯录上传'
  if (key.startsWith('history:')) return '历史消息上传'
  return '原生登录核验'
}
const phase = computed(() => data.value?.reset?.phase)
const statusText = computed(() => phase.value ? labels[phase.value] ?? '状态未识别，请刷新' : '')
const historyCount = computed(() => {
  const items = data.value?.reset?.snapshot?.unresolved_history
  return Array.isArray(items) ? items.length : null
})

function cancelRead(): void {
  if (active) {
    active.controller.abort()
    clearTimeout(active.timeout)
    active = undefined
  }
}

async function refresh(): Promise<void> {
  if (disposed || active || !props.accountId) return
  const accountId = props.accountId
  const revision = ++generation
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 15_000)
  active = { controller, timeout }
  loading.value = true
  try {
    const result = await accountsApi.sessionResetStatus(accountId, { signal: controller.signal })
    if (disposed || revision !== generation || accountId !== props.accountId) return
    if (result.account_id !== accountId) throw new Error('account_mismatch')
    data.value = result
    failed.value = false
  } catch {
    if (!disposed && revision === generation) {
      data.value = null
      failed.value = true
    }
  } finally {
    clearTimeout(timeout)
    if (revision === generation) {
      active = undefined
      loading.value = false
    }
  }
}

watch(() => props.accountId, () => {
  ++generation
  cancelRead()
  data.value = null
  failed.value = false
  void refresh()
}, { immediate: true })
onMounted(() => { poll = setInterval(() => { void refresh() }, 5_000) })
onUnmounted(() => {
  disposed = true
  ++generation
  cancelRead()
  if (poll) clearInterval(poll)
})
</script>

<template>
  <section class="session-reset-status qt-glass" data-testid="account-session-reset-status" aria-live="polite">
    <header><div><h3>询价文件通道</h3><p>企点登录、文件中转和询价会话重置分别显示；进程运行不等于文件通道可用。</p></div>
      <a-button size="small" :loading="loading" @click="refresh">刷新状态</a-button>
    </header>
    <p v-if="failed" role="alert">无法读取本机询价会话重置记录，请检查采集 API 或本地账本；这不表示企点已退出登录。</p>
    <p v-else-if="!data">正在读取询价会话重置记录…</p>
    <p v-else-if="!data.available">{{ data.connection_state === 'not_connected' ? '询价文件通道尚未接入；当前没有可读取的重置记录。请在采集通道配置并连接。' : '当前账号尚未接入询价文件通道，企点登录和历史查询不受本卡影响。' }}</p>
    <template v-else>
      <div v-if="channel" data-testid="institution-channel-health">
        <p data-testid="native-login-state">企点原生界面：{{ nativeLabel }}</p>
        <p v-if="!channel.heartbeat_fresh">目录心跳已过期或尚未发布，请检查文件中转连接；不据此判定企点掉线。</p>
        <p v-else-if="!channel.intake_ready">目录心跳正常，询价收发执行端尚未就绪。</p>
        <p v-else>目录心跳正常，询价收发执行端已连接。</p>
        <p v-for="(detail, operation) in channel.errors" :key="operation" role="alert">文件中转异常：{{ operationLabel(operation) }}，{{ detail }}</p>
      </div>
      <p v-if="!data.reset">当前没有会话重置任务。</p>
      <template v-else>
        <strong data-testid="session-reset-phase">{{ statusText }}</strong>
        <p v-if="phase === 'waiting_native'">本机仍在收敛执行与入站进度，双端尚未完成对齐。</p>
        <p v-else-if="phase === 'barrier_ready'">正在等待工作台应用真实进度并返回确认，尚未完成。</p>
        <p v-else-if="phase === 'blocked'">请在询价工作台查看本次操作的阻塞原因。</p>
        <dl><dt>操作编号</dt><dd>{{ data.reset.reset_id }}</dd>
          <dt>会话版本</dt><dd>{{ data.account_epoch }}</dd>
          <template v-if="data.reset.snapshot_hash"><dt>对齐摘要</dt><dd>{{ data.reset.snapshot_hash }}</dd></template>
          <template v-if="historyCount !== null"><dt>保留的历史未决记录</dt><dd>{{ historyCount }} 项</dd></template>
        </dl>
      </template>
    </template>
  </section>
</template>

<style scoped>
.session-reset-status { padding: 18px 22px; margin: 16px 0; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
h3 { margin: 0 0 6px; }
p { margin: 8px 0; color: var(--qt-text-secondary); }
dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 16px; margin: 12px 0 0; }
dt { color: var(--qt-text-secondary); }
dd { margin: 0; overflow-wrap: anywhere; }
@media (max-width: 600px) { header { flex-wrap: wrap; } dl { grid-template-columns: 1fr; gap: 4px; } }
</style>
