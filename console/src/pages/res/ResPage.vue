<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { res as T } from '@/testids'
import { useResourcesStore } from '@/stores/resources'
import { useAccountsStore } from '@/stores/accounts'
import { resourcesApi } from '@/api/client'
import JobProgress from '@/components/JobProgress.vue'
import PageState from '@/components/PageState.vue'
import { CHANNEL_TEXT } from '@/i18n/zh-CN/codes'
import type { Job } from '@/api/types'

const router = useRouter()
const route = useRoute()
const cleanupSection = ref<HTMLElement | null>(null)
let cleanupLinkHandled = false
async function revealCleanup(): Promise<void> {
  await nextTick()
  if (route.query.section !== 'cleanup' || !cleanupSection.value || cleanupLinkHandled) return
  cleanupLinkHandled = true
  cleanupSection.value.scrollIntoView({ block: 'center', behavior: 'auto' })
  cleanupSection.value.focus({ preventScroll: true })
}
const store = useResourcesStore()
const accounts = useAccountsStore()
const cleanupJob = ref<string | null>(null)
const cleanupResult = ref('')
const cleanupSucceeded = ref(false)
const busy = ref(false)
const consoleRssMb = ref<number | null>(null)
const cleanupLockedUntil = ref(0)
const now = ref(Date.now())
let tick: ReturnType<typeof setInterval> | null = null
const m = computed(() => store.metrics)
const disk = computed(() => m.value?.disk_watermark)
const diskError = computed(() => m.value?.hardware?.disk_source_error)
const mem = computed(() => m.value?.mem_watermark)
const memoryPercent = computed(() => {
  const total = m.value?.hardware?.mem?.total_mb
  const used = m.value?.hardware?.mem?.used_mb
  return total && used != null ? Math.min(100, Math.max(0, used / total * 100)) : null
})
const cpuPercent = computed(() => m.value?.hardware?.cpu?.load_pct ?? null)
function mb(value?: number | null): string { return value == null ? '—' : (value / 1024).toFixed(2) + ' GB' }
function pct(value?: number | null): string { return value == null ? '—' : value.toFixed(1) + '%' }
function diskRoles(roles?: string[]): string { return (roles ?? []).map((role) => role === 'app' ? '应用' : role === 'data' ? '数据' : role).join(' / ') }
function levelText(level?: string): string { return ({ normal: '正常', ok: '正常', warn: '预警', high: '较高', critical: '严重' } as Record<string,string>)[level ?? ''] ?? '未知' }
function diskErrorText(error: string): string {
  if (error.includes('distro')) return '无法定位当前 WSL 发行版的 Windows 存储位置'
  if (error.includes('timeout')) return 'Windows 分区读取超时'
  if (error.includes('path')) return '无法确定部署目录所在分区'
  if (error.includes('capacity')) return '分区容量信息不完整'
  return '部署分区信息暂不可用'
}
function usedPercent(total: number | null, free: number | null): number {
  return total && free != null ? Math.min(100, Math.max(0, (total - free) / total * 100)) : 0
}
async function refresh(): Promise<void> {
  await store.loadMetrics()
  try { const value = await window.qt?.app.rssKb(); consoleRssMb.value = value == null ? null : value / 1024 }
  catch { consoleRssMb.value = null }
}
async function runCleanup(): Promise<void> {
  if (busy.value || cleanupJob.value || Date.now() < cleanupLockedUntil.value) return
  busy.value = true
  try {
    const result = await resourcesApi.cleanupRun()
    cleanupJob.value = result.job_id
    cleanupLockedUntil.value = Date.now() + 60000
    cleanupResult.value = ''
  } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
  finally { busy.value = false }
}
function onCleanupDone(job: Job): void {
  cleanupSucceeded.value = job.state === 'succeeded'
  cleanupResult.value = job.state === 'succeeded' ? '已清出 ' + (job.result?.freed_mb ?? 0) + ' MB' : job.state === 'cancelled' ? '清理已取消' : '清理失败，请在日志中查看原因'
  cleanupJob.value = null
  void refresh()
}
watch(() => route.query.section, () => { cleanupLinkHandled = false; void revealCleanup() })
watch(cleanupSection, () => { void revealCleanup() })
onMounted(() => { void revealCleanup(); void refresh(); if (!accounts.items.length) void accounts.loadFirst(); tick = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => { if (tick) clearInterval(tick) })
</script>

<template>
  <div class="qt-page qt-stack">
    <header class="qt-page-heading"><div><div class="qt-eyebrow">RESOURCE MONITOR</div><h1>看清资源，安心运行</h1><p>查看 WSL 运行环境、账号占用与部署分区的最新快照。</p></div><div class="qt-row"><span class="qt-small qt-muted">上次 {{ store.lastAt?.slice(11,19) ?? '—' }}</span><a-button :data-testid="T.refresh" :loading="store.loading" @click="refresh">刷新</a-button></div></header>
    <PageState :loading="store.loading && !m" :error="store.metricsError" @retry="refresh">
      <div class="resource-overview">
        <section class="resource-stat qt-glass"><div class="qt-eyebrow">WSL VIRTUAL MACHINE</div><span>WSL 虚拟机内存</span><strong :data-testid="T.memVmmem">{{ mb(m?.hardware?.mem?.vmmem_mb) }}</strong><p>{{ m?.hardware?.mem?.vmmem_mb == null ? 'Windows 侧尚未提供此项采样' : 'Windows 侧采集的虚拟机占用' }}</p></section>
        <section class="resource-stat qt-glass"><div class="qt-eyebrow">WSL MEMORY</div><span>WSL 已用内存</span><strong :data-testid="T.memHost('used')">{{ mb(m?.hardware?.mem?.used_mb) }}</strong><div class="usage-track"><span :style="{ width: (memoryPercent ?? 0) + '%' }" /></div><p>{{ memoryPercent == null ? '等待内存快照' : '已用 ' + memoryPercent.toFixed(1) + '%' }} · 总量 <span :data-testid="T.memHost('total')">{{ mb(m?.hardware?.mem?.total_mb) }}</span></p></section>
        <section class="resource-stat qt-glass"><div class="qt-eyebrow">PROCESSOR</div><span>WSL CPU 负载</span><strong :data-testid="T.cpuHost('load')">{{ pct(cpuPercent) }}</strong><div class="usage-track golden"><span :style="{ width: Math.min(100, Math.max(0, cpuPercent ?? 0)) + '%' }" /></div><p :data-testid="T.cpuHost('cores')">{{ m?.hardware?.cpu?.logical_cores == null ? '等待处理器快照' : m.hardware.cpu.logical_cores + ' 个逻辑核心' }}</p></section>
      </div>
      <div class="resource-columns">
        <section class="qt-card resource-panel" :data-testid="T.col('ours')">
          <header class="section-heading"><div><div class="qt-eyebrow">ACCOUNT FOOTPRINT</div><h2>账号资源占用</h2></div><span class="soft-tag">{{ m ? m.ours.accounts.length : '—' }} 个账号</span></header>
          <div class="table-scroll"><table class="tbl"><thead><tr><th>账号</th><th>内存</th><th>CPU</th><th /></tr></thead><tbody>
            <tr v-for="row in m?.ours.accounts ?? []" :key="row.id"><td><button class="account-link" @click="router.push('/acct/' + row.id)">{{ accounts.byId[row.id]?.label || row.id }}<small>{{ accounts.byId[row.id] ? CHANNEL_TEXT[accounts.byId[row.id].channel] : '账号' }} · {{ row.id }}</small></button></td><td :data-testid="T.memAcct(row.id, 'current')">{{ mb(row.current_mb ?? row.rss_mb) }}</td><td :data-testid="T.cpuAcct(row.id)">{{ pct(row.cpu_pct) }}</td><td><a-button type="text" @click="router.push('/acct/' + row.id)">↗</a-button></td></tr>
            <tr v-if="!m?.ours.accounts.length"><td colspan="4"><a-empty description="暂无账号资源快照" /></td></tr>
          </tbody></table></div>
          <div class="process-grid"><div><span>Agent</span><b :data-testid="T.memProc('agent')">{{ mb(m?.ours.procs.agent_mb) }}</b></div><div><span>WinAgent</span><b :data-testid="T.memProc('winagent')">{{ mb(m?.ours.procs.winagent_mb) }}</b></div><div><span>控制台</span><b :data-testid="T.memProc('console')">{{ mb(consoleRssMb ?? m?.ours.procs.console_mb) }}</b></div><div><span>微信 PC</span><b :data-testid="T.memWechat">{{ mb(m?.ours.wechat?.wechat_pc_mb) }}</b></div></div>
        </section>
        <section class="qt-card resource-panel" :data-testid="T.col('host')">
          <header class="section-heading"><div><div class="qt-eyebrow">SYSTEM CAPACITY</div><h2>可用资源</h2></div><span class="soft-tag" :data-testid="T.memWatermark">{{ levelText(mem?.level) }}</span></header>
          <div class="available-memory"><span>WSL 可用内存</span><strong :data-testid="T.memHost('avail')">{{ mb(m?.hardware?.mem?.avail_mb) }}</strong></div>
          <p class="qt-small qt-muted">预警 {{ mb(mem?.warn_mb) }} · 严重 {{ mb(mem?.critical_mb) }}</p>
          <p class="capacity-note" :data-testid="T.lruNote">{{ mem?.level === 'critical' ? '当前内存紧张，请减少运行中的账号。系统已限制新增与自动恢复。' : mem?.level === 'warn' ? '可用内存接近预警线，建议关注账号占用。' : mem ? '可在账号工作台按需启停账号，管理资源占用。' : '尚未取得内存状态，等待服务返回采集结果。' }}</p>
          <a-button block :data-testid="T.actionOpenEnv" @click="router.push('/env')">查看系统环境 ↗</a-button>
        </section>
      </div>
      <section class="qt-card resource-panel disk-panel">
        <header class="section-heading"><div><div class="qt-eyebrow">DEPLOYMENT STORAGE</div><h2>部署分区</h2></div><span class="soft-tag" :class="'level-' + (disk?.level ?? 'unknown')" :data-testid="T.diskWatermark">{{ levelText(disk?.level) }}</span></header>
        <p v-if="diskError" class="qt-small qt-warn">{{ diskErrorText(diskError) }}</p>
        <div class="disk-grid"><article v-for="volume in m?.hardware?.disks ?? []" :key="volume.mount" class="volume-card">
          <div class="volume-heading"><strong>{{ volume.mount }}</strong><span>{{ diskRoles(volume.roles) || '部署卷' }}</span></div>
          <div class="usage-track golden"><span :style="{ width: usedPercent(volume.total_mb, volume.free_mb) + '%' }" /></div>
          <div class="volume-values"><span>剩余 <b :data-testid="T.diskPart(volume.mount, 'free')">{{ mb(volume.free_mb) }}</b></span><span>共 <span :data-testid="T.diskPart(volume.mount, 'total')">{{ mb(volume.total_mb) }}</span></span></div>
          <small>{{ volume.source === 'windows_volume' ? 'Windows 实际承载卷' : volume.source ? '本机文件系统' : '来源未提供' }}</small>
        </article></div>
        <a-empty v-if="!m?.hardware?.disks?.length && !diskError" description="尚未取得部署分区信息" />
        <p v-if="disk?.runtime_level && disk.runtime_level !== disk.level" class="qt-small qt-muted">当前保护状态：{{ levelText(disk.runtime_level) }}</p>
        <div id="cleanup" ref="cleanupSection" class="cleanup-bar" tabindex="-1" aria-label="日常清理"><div><h3>日常清理</h3><p :data-testid="T.diskRetention">仅清理已过保留期的本地数据与临时文件。上次清理 {{ disk?.last_cleanup_at ?? '—' }}<template v-if="disk?.last_cleanup_freed_mb != null">，释放 {{ disk.last_cleanup_freed_mb }} MB</template></p></div><a-popconfirm title="清理已过保留期的本地数据和临时文件？不会删除服务器邮件。" @confirm="runCleanup"><a-button type="primary" :disabled="busy || !!cleanupJob || now < cleanupLockedUntil" :data-testid="T.actionCleanup">清理本地过期数据</a-button></a-popconfirm></div>
        <JobProgress :job-id="cleanupJob" :testid="T.actionCleanupProgress" @done="onCleanupDone" />
        <p v-if="cleanupResult" :data-testid="T.actionCleanupResult" :class="cleanupSucceeded ? 'qt-ok' : 'qt-warn'">{{ cleanupResult }}</p>
      </section>
      <p class="snapshot-note">数据为最近一次采集快照，未采集的指标显示为「—」。</p>
    </PageState>
  </div>
</template>

<style scoped>
.cleanup-bar { scroll-margin-block: 24px; }
.cleanup-bar:focus { outline: 2px solid #b69acb; outline-offset: 8px; border-radius: 12px; }
.resource-overview { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 20px; margin-bottom: 24px; }
.resource-stat { display: flex; flex-direction: column; padding: 27px; border: 1px solid rgba(117,71,168,.12); border-radius: 24px; }
.resource-stat > span { font-size: 13px; color: var(--qt-text-secondary); margin: 10px 0; }
.resource-stat strong { font-size: 36px; line-height: 1.2; letter-spacing: -1.5px; font-weight: 600; color: var(--qt-text); }
.resource-stat p { margin: auto 0 0; padding-top: 14px; font-size: 11px; color: var(--qt-text-secondary); }
.usage-track { height: 5px; background: rgba(117,71,168,.08); border-radius: 6px; margin-top: 22px; overflow: hidden; }
.usage-track > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg,#b899d9,#7547a8); }
.usage-track.golden > span { background: linear-gradient(90deg,#f9d88c,#f2ad38); }
.resource-columns { display: grid; grid-template-columns: minmax(0,1.65fr) minmax(260px,1fr); gap: 22px; margin-bottom: 22px; }
.resource-panel { padding: 26px; min-width: 0; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
.section-heading h2 { font-size: 20px; margin: 5px 0 0; }
.soft-tag { background: rgba(117,71,168,.07); color: var(--qt-primary); padding: 6px 12px; font-size: 11px; border-radius: 14px; }
.table-scroll { overflow: auto; }
.tbl { width: 100%; border-collapse: collapse; white-space: nowrap; }
.tbl th, .tbl td { text-align: left; padding: 14px 10px; border-bottom: 1px solid var(--qt-border); font-size: 13px; }
.tbl th { font-size: 11px; color: var(--qt-text-secondary); font-weight: 400; background: rgba(117,71,168,.035); }
.account-link { border: 0; background: none; font: inherit; color: var(--qt-text); cursor: pointer; text-align: left; padding: 0; }
.account-link:hover { color: var(--qt-primary); }
.account-link small { display: block; color: var(--qt-text-secondary); font-size: 10px; margin-top: 5px; }
.process-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); gap: 12px; padding-top: 24px; }
.process-grid > div { display: flex; flex-direction: column; gap: 8px; font-size: 11px; color: var(--qt-text-secondary); }
.process-grid b { font-size: 14px; color: var(--qt-text); font-weight: 500; }
.available-memory { display: flex; flex-direction: column; gap: 15px; margin: 30px 0 18px; }
.available-memory > span { color: var(--qt-text-secondary); font-size: 13px; }
.available-memory strong { font-size: 36px; font-weight: 500; letter-spacing: -1px; }
.capacity-note { margin: 22px 0; padding: 16px; border-radius: 15px; background: rgba(117,71,168,.04); font-size: 12px; line-height: 1.8; color: var(--qt-text-secondary); }
.disk-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 20px; }
.volume-card { border: 1px solid var(--qt-border); border-radius: 18px; padding: 22px; }
.volume-heading, .volume-values { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.volume-heading strong { font-size: 22px; font-weight: 500; }
.volume-heading > span, .volume-values, .volume-card small { font-size: 11px; color: var(--qt-text-secondary); }
.volume-values { margin: 16px 0 12px; }
.volume-values b { color: var(--qt-text); font-size: 14px; font-weight: 500; }
.cleanup-bar { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding-top: 25px; margin-top: 24px; border-top: 1px solid var(--qt-border); }
.cleanup-bar h3 { font-size: 15px; margin: 0 0 8px; }
.cleanup-bar p { font-size: 11px; color: var(--qt-text-secondary); line-height: 1.8; margin: 0; }
.level-warn { color: var(--qt-sev-warn); background: #fff5df; }
.level-high, .level-critical { color: var(--qt-sev-crit); background: #fff1f0; }
.snapshot-note { font-size: 11px; color: var(--qt-text-secondary); padding: 18px 4px; }
@media(max-width: 1000px) { .resource-columns { grid-template-columns: 1fr; } }
@media(max-width: 720px) { .resource-overview, .disk-grid { grid-template-columns: 1fr; } .resource-stat strong { font-size: 30px; } .resource-panel { padding: 18px; } .cleanup-bar { align-items: flex-start; flex-direction: column; } .process-grid { grid-template-columns: repeat(2,minmax(0,1fr)); } }
</style>
