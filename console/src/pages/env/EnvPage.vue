<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { env as T } from '@/testids'
import { useEnvStore } from '@/stores/env'
import { useSessionStore } from '@/stores/session'
import { useAccountsStore } from '@/stores/accounts'
import { systemApi } from '@/api/client'
import ProbeTable from '@/components/ProbeTable.vue'
import PageState from '@/components/PageState.vue'
import { KERNEL_STATE_TEXT, NET_STATE_TEXT, WSL_STATE_TEXT } from '@/i18n/zh-CN/codes'

const store = useEnvStore()
const session = useSessionStore()
const accounts = useAccountsStore()
const route = useRoute()
const busy = ref(false)
const v = computed(() => store.version)
const bridgeAvailable = computed(() => !!window.qt?.wa)
const kernelText = computed(() => v.value?.kernel ?? '等待环境信息')
const wslText = computed(() => v.value?.wsl ?? '未知版本')
const networkText = computed(() => {
  const state = store.snapshot?.windows?.net_state
  return state ? NET_STATE_TEXT[state] ?? state : '网络状态未知'
})
const winagentText = computed(() => {
  if (!store.health) return '状态未知'
  const state = store.health.winagent
  return (typeof state === 'object' ? state.online : state) ? '已连接' : '未连接'
})
const userAgentText = computed(() => {
  if (!store.health) return '状态未知'
  const state = store.health.winagent
  const online = typeof state === 'object' ? state.user_agent : store.health.user_agent
  return online == null ? '状态未知' : online ? '已连接' : '未连接'
})
const dockerText = computed(() => store.health?.dockerd == null ? '状态未知' : store.health.dockerd ? '运行中' : '未运行')
const sectionTargets: Record<string, string> = {
  ws: 'network', network: 'network',
  agent: 'wsl', sqlite: 'wsl', kernel: 'wsl', wsl: 'wsl', docker: 'wsl', redroid: 'wsl', napcat: 'wsl',
  winagent: 'winagent', 'winagent-user': 'winagent',
}
async function focusSection(): Promise<void> {
  const value = route.query.section
  const section = Array.isArray(value) ? value[0] : value
  const target = section ? sectionTargets[section] : undefined
  if (!target) return
  await nextTick()
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  document.getElementById(target)?.scrollIntoView?.({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' })
}
watch(() => route.query.section, () => { void focusSection() }, { flush: 'post' })

function affectedAccounts(): string[] {
  return accounts.items
    .filter((a) => ['running', 'degraded', 'login_required', 'starting', 'logging_in'].includes(a.state))
    .map((a) => a.label || a.id)
}
function restartImpact(): string {
  return '受影响账号：' + (affectedAccounts().join('、') || '无') + '。所有 WSL 发行版与容器将停止，未保存的工作会丢失；随后重新拉起 QTrade 发行版。'
}
async function runAction(fn: () => Promise<unknown>, success: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    await fn()
    message.success(success)
    await store.loadAll()
  } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
  finally { busy.value = false }
}
function confirmWslRestart(): void {
  Modal.confirm({
    title: '重启 WSL',
    content: '将先停止账号任务，再重启 WSL。' + restartImpact(),
    okType: 'danger', okText: '确认重启', cancelText: '取消',
    onOk: () => runAction(() => systemApi.wslRestart(), '已请求重启 WSL，连接恢复后可刷新查看'),
  })
}
function confirmKernelRollback(): void {
  if (!window.qt?.wa) { message.info('请在桌面控制台执行内核回滚'); return }
  Modal.confirm({
    title: '回滚内核并重启 WSL',
    content: '将恢复上一版内核。' + restartImpact(),
    okType: 'danger', okText: '确认回滚并重启', cancelText: '取消',
    onOk: () => runAction(() => window.qt!.wa.invoke('wsl.kernel.rollback', { confirm_shutdown: true }), '内核回滚请求已完成，请检查恢复后的环境'),
  })
}
function confirmFirewall(): void {
  if (!window.qt?.wa) { message.info('请在桌面控制台修复网络访问'); return }
  Modal.confirm({
    title: '修复本机网络访问',
    content: '将按 QTrade 使用的端口更新 Windows 防火墙放行规则，并记录操作日志。不会放开任意端口。',
    okText: '确认修复', cancelText: '取消',
    onOk: () => runAction(() => window.qt!.wa.invoke('firewall.ensure', {}), '网络访问规则已修复'),
  })
}
async function runProbe(): Promise<void> {
  await runAction(() => store.runProbe(), '网络检查完成')
}
async function runSelftest(): Promise<void> {
  await runAction(() => store.runSelftest(), '系统检查完成')
}
async function rerunTarget(target: string): Promise<void> {
  await runAction(async () => {
    const result = await systemApi.probe([target])
    const map = new Map(store.probes.map((row) => [row.target, row]))
    for (const row of result.results) map.set(row.target, row)
    store.probes = [...map.values()]
  }, '检查完成')
}
onMounted(async () => {
  await store.loadAll()
  await focusSection()
  await store.loadSelftest().catch(() => undefined)
  if (!accounts.items.length) void accounts.loadFirst()
})
</script>

<template>
  <div class="qt-page qt-stack environment-page">
    <header class="qt-page-heading">
      <div><div class="qt-eyebrow">SYSTEM ENVIRONMENT</div><h1>让每个账号，稳定在线</h1><p>检查环境与连接，在需要时完成恢复。</p></div>
      <a-button :loading="store.loading" @click="store.loadAll()">刷新环境</a-button>
    </header>
    <PageState :loading="store.loading && !v" :error="store.error" @retry="store.loadAll()">
      <a-alert v-if="store.agentDownReason" type="warning" show-icon :message="store.agentDownReason" class="environment-notice" />
      <div class="environment-grid">
        <section id="wsl" class="environment-card qt-glass">
          <span class="environment-symbol">⌘</span><div class="qt-eyebrow">WSL / KERNEL</div><h2>运行内核</h2>
          <p :data-testid="T.version('kernel')">{{ kernelText }}</p>
          <div class="environment-state" :data-testid="T.version('wsl')">WSL {{ wslText }} · {{ WSL_STATE_TEXT[v?.wsl_state ?? ''] ?? '状态未知' }}</div>
          <div class="qt-row wrap">
            <a-button type="primary" :disabled="session.draining || busy" :data-testid="T.wslRestart" @click="confirmWslRestart">重启 WSL</a-button>
            <a-button :disabled="!bridgeAvailable || busy" :data-testid="T.kernelRollback" @click="confirmKernelRollback">回滚内核</a-button>
          </div>
          <p class="qt-small qt-muted">{{ KERNEL_STATE_TEXT[v?.kernel_state ?? ''] ?? '内核归属未知' }} · 操作前会确认影响范围</p>
          <span :data-testid="T.wslRestartModal" class="hidden" />
        </section>
        <section id="winagent" class="environment-card qt-glass">
          <span class="environment-symbol">◇</span><div class="qt-eyebrow">WINDOWS / WINAGENT</div><h2>Windows 连接服务</h2>
          <p :data-testid="T.version('winagent')">WinAgent · {{ winagentText }}</p>
          <div class="environment-state" :data-testid="T.version('winagent-user')">用户会话代理 · {{ userAgentText }}</div>
          <a-button :loading="busy" :data-testid="T.selfcheckRun" @click="runSelftest">检查系统环境</a-button>
          <p class="qt-small qt-muted">会话代理需要 Windows 用户保持登录。</p>
        </section>
        <section id="network" class="environment-card qt-glass">
          <span class="environment-symbol">◎</span><div class="qt-eyebrow">NETWORK / CONNECTIVITY</div><h2>网络连接</h2>
          <p :data-testid="T.snapshot('netstate')">{{ networkText }}</p>
          <div class="environment-state" :data-testid="T.version('docker')">Docker · {{ dockerText }}</div>
          <div class="qt-row wrap">
            <a-button :loading="busy" :data-testid="T.probeRun" @click="runProbe">检查连通性</a-button>
            <a-button :disabled="!bridgeAvailable || !session.winagentOnline || busy" :data-testid="T.firewallFix" @click="confirmFirewall">修复网络访问</a-button>
          </div>
          <p class="qt-small qt-muted">检查结果会在下方逐项展示。</p>
        </section>
      </div>
      <div v-if="store.snapshot?.windows?.docker_conflict?.state === 'conflict'" class="environment-notice warning-note" :data-testid="T.dockerConflict">
        当前网络与 Docker 网段 {{ store.dockerCidr ?? '未知' }} 存在重叠，部分内网地址可能无法访问。请联系管理员检查网络配置。
      </div>
      <div class="checks-grid">
        <section class="qt-card check-card">
          <header class="section-heading"><div><div class="qt-eyebrow">ENVIRONMENT HEALTH</div><h2>系统检查</h2></div><small>{{ store.selftestAt ? '上次 ' + store.selftestAt.slice(11, 19) : '尚未运行' }}</small></header>
          <div v-if="store.selftest.length" class="check-summary"><b>{{ store.selftest.filter((row) => row.level === 'error').length }}</b> 项异常 <span>{{ store.selftest.filter((row) => row.level === 'warn').length }} 项提醒</span><span>{{ store.selftest.filter((row) => row.level === 'skip').length }} 项未探测</span></div>
          <table class="tbl" :data-testid="T.selfcheckTable"><tbody>
            <tr v-for="row in store.selftest" :key="row.item" :data-testid="T.selfcheckRow(row.item)">
              <td><span class="result-dot" :class="row.level" /></td><td>{{ row.label }}<small>{{ row.message }}</small></td>
              <td :class="row.level === 'error' ? 'qt-danger' : row.level === 'warn' ? 'qt-warn' : row.level === 'skip' ? 'qt-muted' : 'qt-ok'">{{ row.level === 'error' ? '异常' : row.level === 'warn' ? '提醒' : row.level === 'skip' ? '未探测' : '正常' }}</td>
            </tr>
            <tr v-if="!store.selftest.length"><td colspan="3"><a-empty description="运行系统检查，了解各项服务状态" /></td></tr>
          </tbody></table>
        </section>
        <section class="qt-card check-card">
          <header class="section-heading"><div><div class="qt-eyebrow">NETWORK HEALTH</div><h2>网络检查</h2></div><span class="section-tag">{{ store.probes.length }} 项结果</span></header>
          <ProbeTable v-if="store.probes.length" :rows="store.probes" @rerun="rerunTarget" />
          <a-empty v-else description="运行连通性检查，查看网络是否可达" />
        </section>
      </div>
      <div class="environment-footer"><span :data-testid="T.version('console')">控制台 {{ session.appVersion?.console ?? '—' }}</span><span :data-testid="T.version('agent')">Agent {{ v?.agent?.version ?? '—' }}</span><span>操作记录可在日志页面查询</span></div>
    </PageState>
  </div>
</template>

<style scoped>
.environment-grid { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 20px; }
.environment-card { position: relative; display: flex; flex-direction: column; align-items: flex-start; min-height: 320px; padding: 28px; border: 1px solid rgba(117,71,168,.13); border-radius: 26px; overflow: hidden; scroll-margin-top: 24px; }
.environment-card::after { content: ''; position: absolute; pointer-events: none; width: 170px; height: 170px; background: radial-gradient(circle,rgba(242,173,56,.14),transparent 68%); right: -45px; top: -55px; }
.environment-symbol { width: 44px; height: 44px; display: grid; place-items: center; border-radius: 14px; background: rgba(117,71,168,.08); color: var(--qt-primary); font-size: 26px; margin-bottom: 22px; }
.environment-card h2 { font-size: 20px; margin: 5px 0 12px; }
.environment-card > p { color: var(--qt-text-secondary); overflow-wrap: anywhere; font-size: 13px; }
.environment-card .qt-small { font-size: 11px; margin-top: 16px; margin-bottom: 0; }
.environment-state { flex: 1; margin: 2px 0 24px; color: var(--qt-text-secondary); font-size: 12px; }
.wrap { flex-wrap: wrap; gap: 8px; }
.environment-notice { margin-bottom: 20px; }
.warning-note { margin-top: 20px; background: #fff5df; padding: 18px 22px; border-radius: 16px; color: #8b5b10; }
.checks-grid { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 22px; margin-top: 26px; }
.check-card { padding: 26px; min-width: 0; overflow: auto; }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
.section-heading h2 { font-size: 20px; margin: 5px 0 0; }
.section-heading small { color: var(--qt-text-secondary); font-size: 11px; }
.section-tag { background: rgba(117,71,168,.07); color: var(--qt-primary); padding: 6px 10px; font-size: 11px; border-radius: 14px; }
.check-summary { display: flex; align-items: center; gap: 10px; padding: 15px 18px; margin-bottom: 18px; background: rgba(117,71,168,.04); border-radius: 14px; font-size: 12px; color: var(--qt-text-secondary); flex-wrap: wrap; }
.check-summary b { font-size: 24px; color: var(--qt-text); }
.check-summary span { margin-left: 12px; }
.tbl { width: 100%; border-collapse: collapse; }
.tbl td { padding: 15px 8px; border-bottom: 1px solid var(--qt-border); font-size: 13px; }
.tbl td:last-child { white-space: nowrap; text-align: right; font-size: 11px; }
.tbl small { display: block; max-width: 340px; color: var(--qt-text-secondary); font-size: 11px; line-height: 1.6; margin-top: 5px; overflow-wrap: anywhere; }
.result-dot { display: block; width: 7px; height: 7px; border-radius: 50%; background: var(--qt-state-running); }
.result-dot.skip { background: var(--qt-text-disabled); }
.result-dot.warn { background: var(--qt-sev-warn); }
.result-dot.error { background: var(--qt-state-error); }
.environment-footer { display: flex; flex-wrap: wrap; gap: 20px; color: var(--qt-text-secondary); font-size: 11px; padding: 24px 6px; }
.hidden { display: none; }
@media(max-width: 1100px) { .environment-grid, .checks-grid { grid-template-columns: 1fr; } .environment-card { min-height: 0; } }
</style>
