<script setup lang="ts">
/**
 * `P-ENV` 环境(01 §2.7.9)。数据**一律经 Agent**(C-32);
 * 只有内核/发行版/防火墙/微信模块这几类动作经主进程白名单代调 WinAgent。
 */
import { computed, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { env as T, ENV_SNAPSHOT_ITEMS, ENV_VERSION_ITEMS } from '@/testids'
import { useEnvStore } from '@/stores/env'
import { useSessionStore } from '@/stores/session'
import { useAccountsStore } from '@/stores/accounts'
import { systemApi } from '@/api/client'
import ProbeTable from '@/components/ProbeTable.vue'
import JobProgress from '@/components/JobProgress.vue'
import PageState from '@/components/PageState.vue'
import { ALERT_CODES, KERNEL_STATE_TEXT, NET_STATE_TEXT, WSL_STATE_TEXT } from '@/i18n/zh-CN/codes'
import type { Job } from '@/api/types'

const store = useEnvStore()
const session = useSessionStore()
const accounts = useAccountsStore()

const sampling = ref(false)
const sampleLeft = ref(0)
const samplePick = ref<Set<string>>(new Set())
const sampleApplyOpen = ref(false)
const wxStatus = ref<Record<string, any> | null>(null)
const wxDisk = ref<{ partition?: string; free_mb?: number } | null>(null)
const diagJob = ref<string | null>(null)
const withShots = ref(false)
const busy = ref(false)

const v = computed(() => store.version)
const snap = computed(() => store.snapshot)
const hostsBlock = computed(() => (wxStatus.value?.hosts_block ?? {}) as Record<string, any>)
const anyOnline = computed(() => accounts.items.some((a) => a.state === 'running' || a.state === 'degraded'))

function versionText(k: string): string {
  switch (k) {
    case 'console': return session.appVersion?.console ?? '—'
    case 'agent': return v.value?.agent ?? '—'
    case 'winagent': return v.value?.winagent?.version ?? '—'
    case 'winagent-user': return v.value?.winagent?.user_agent ? '在线' : '不在线'
    case 'kernel': return `${v.value?.kernel ?? '—'}(${KERNEL_STATE_TEXT[v.value?.kernel_state ?? ''] ?? '—'})`
    case 'wsl': return `${v.value?.wsl ?? '—'}(${WSL_STATE_TEXT[v.value?.wsl_state ?? ''] ?? '—'})`
    case 'docker': return v.value?.docker ?? '—'
    default: return v.value?.distro ?? '—'
  }
}

function snapText(k: string): string {
  const s = snap.value
  if (!s) return '—'
  switch (k) {
    case 'netstate': return NET_STATE_TEXT[s.net_state] ?? s.net_state
    case 'proxy': return s.proxy ?? '无'
    case 'vpn': return s.vpn_adapter ?? '无'
    case 'subnet': return s.wsl_subnet ?? '—'
    case 'hostip': return s.host_ip ?? '—'
    case 'mtu': return String(s.mtu ?? '—')
    case 'clock': return `${s.clock_drift_s ?? 0} s`
    case 'wslconfig': return JSON.stringify(s.wslconfig ?? {})
    default: return s.pending_restart ? '是' : '否'
  }
}

async function loadWx(): Promise<void> {
  try {
    wxStatus.value = (await window.qt?.wa.invoke('wechat.status', {})) as Record<string, unknown>
  } catch { wxStatus.value = null }
  const al = (store.health?.alerts ?? []).find((a) => a.code === 'WECHAT_DISK_LOW')
  wxDisk.value = al ? (al.evidence as { partition?: string; free_mb?: number }) : null
}

async function runProbe(): Promise<void> {
  busy.value = true
  try { await store.runProbe() } finally { busy.value = false }
}

async function rerunTarget(target: string): Promise<void> {
  busy.value = true
  try {
    const r = await systemApi.probe([target])
    const map = new Map(store.probes.map((x) => [x.target, x]))
    for (const row of r.results) map.set(row.target, row)
    store.probes = [...map.values()]
  } finally {
    busy.value = false
  }
}

async function runSample(): Promise<void> {
  sampling.value = true
  sampleLeft.value = 30
  const t = setInterval(() => { sampleLeft.value = Math.max(0, sampleLeft.value - 1) }, 1000)
  try {
    await store.runSample(30)
    samplePick.value = new Set(store.samples.map((_, i) => String(i)))
  } finally {
    clearInterval(t)
    sampling.value = false
  }
}

async function applySample(): Promise<void> {
  await systemApi.adoptProbeTargets([...samplePick.value])
  sampleApplyOpen.value = false
  message.success('已写入探测目标,正在跑一轮探测')
  await store.runProbe()
}

async function waAction(op: string, args: Record<string, unknown>, okText: string): Promise<void> {
  try {
    await window.qt?.wa.invoke(op, args)
    message.success(okText)
    await store.loadAll()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

function confirmKernelReapply(): void {
  Modal.confirm({
    title: '重新应用内核',
    content: '将写入我方内核文件,写入后需要重启 WSL 才生效;本操作不会自动 shutdown。',
    okType: 'danger',
    onOk: () => waAction('wsl.kernel.apply', { confirm_shutdown: true }, '已写入,需重启 WSL 生效'),
  })
}

function confirmWslRestart(): void {
  const affected = accounts.items.filter((a) => ['running', 'degraded', 'login_required'].includes(a.state)).map((a) => a.id)
  Modal.confirm({
    title: '重启 WSL(经 Agent,需确认)',
    okType: 'danger',
    content: `将中断这些账号:${affected.join('、') || '无'};你在 WSL 里的其它发行版也会一并停止。`,
    onOk: async () => {
      await systemApi.wslRestart()
      message.success('已请求重启 WSL')
    },
  })
}

function openLogsDir(): void {
  void window.qt?.app.openLogsDir()
}

async function exportDiag(): Promise<void> {
  const r = await systemApi.diagnostics(withShots.value)
  diagJob.value = r.job_id
}

async function onDiagDone(job: Job): Promise<void> {
  diagJob.value = null
  if (job.state !== 'succeeded') { message.error('诊断包生成失败'); return }
  await window.qt?.files.saveAs(`qtrade-diag-${Date.now()}.zip`, 'application/zip', String(job.result?.download_url ?? ''))
}

onMounted(async () => {
  await store.loadAll()
  await store.loadPublicEndpoint().catch(() => undefined)
  await loadWx()
  if (!accounts.items.length) void accounts.load()
})
</script>

<template>
  <div class="qt-page qt-stack">
    <PageState :loading="store.loading && !v" :error="store.error" @retry="store.loadAll()">
      <!-- 版本 -->
      <section class="qt-card box">
        <div class="qt-section-title">版本</div>
        <div class="qt-row wrap">
          <span v-for="k in ENV_VERSION_ITEMS" :key="k" class="kvi" :data-testid="T.version(k)">
            {{ ({ console: '控制台', agent: 'Agent', winagent: 'WinAgent', 'winagent-user': '会话代理',
                  kernel: '内核', wsl: 'WSL', docker: 'docker', distro: '发行版' } as Record<string, string>)[k] }}
            <b>{{ versionText(k) }}</b>
          </span>
        </div>
      </section>

      <!-- 一键自检 -->
      <section class="qt-card box">
        <div class="qt-row">
          <div class="qt-section-title qt-grow">一键自检</div>
          <span class="qt-small qt-muted">上次 {{ store.selftestAt ?? '—' }}</span>
          <a-button :data-testid="T.selfcheckRun" @click="store.runSelftest()">运行</a-button>
        </div>
        <table class="tbl" :data-testid="T.selfcheckTable">
          <tbody>
            <tr v-for="r in store.selftest" :key="r.item" :data-testid="T.selfcheckRow(r.item)">
              <td :class="r.level === 'error' ? 'qt-danger' : r.level === 'warn' ? 'qt-warn' : 'qt-ok'">
                {{ r.level === 'error' ? '✗' : r.level === 'warn' ? '⚠' : '✔' }}
              </td>
              <td>{{ r.label }}</td>
              <td class="qt-small">{{ r.message }}</td>
            </tr>
            <tr v-if="!store.selftest.length"><td colspan="3" class="qt-muted">还没有自检结果</td></tr>
          </tbody>
        </table>
        <div class="qt-row wrap actions">
          <a-button :data-testid="T.kernelReapply" @click="confirmKernelReapply">重新应用内核</a-button>
          <a-popconfirm title="回滚到上一版内核?" @confirm="waAction('wsl.kernel.rollback', {}, '已回滚内核')">
            <a-button :data-testid="T.kernelRollback">回滚内核</a-button>
          </a-popconfirm>
          <a-popconfirm title="重导入发行版并从最近备份恢复?" @confirm="waAction('wsl.distro.repair', {}, '已请求修复发行版')">
            <a-button :data-testid="T.distroRepair">修复发行版</a-button>
          </a-popconfirm>
          <a-button
            :disabled="!session.winagentOnline"
            :data-testid="T.firewallFix"
            @click="waAction('firewall.ensure', {}, '防火墙规则已修复')"
          >修复防火墙规则</a-button>
          <a-popconfirm
            title="容器拉镜像将经公司代理,是否启用?"
            @confirm="systemApi.dockerProxy(true).then(() => message.success('已为 docker 启用系统代理'))"
          >
            <a-button
              :disabled="!['SYSTEM_PROXY', 'VPN_ACTIVE_WITH_PROXY'].includes(store.netState)"
              :data-testid="T.dockerProxy"
            >为 docker 启用系统代理</a-button>
          </a-popconfirm>
          <a-button danger :data-testid="T.wslRestart" @click="confirmWslRestart">重启 WSL…</a-button>
          <span :data-testid="T.wslRestartModal" class="hidden" />
        </div>
      </section>

      <!-- 环境快照 -->
      <section class="qt-card box">
        <div class="qt-section-title">环境快照</div>
        <div class="qt-row wrap">
          <span v-for="k in ENV_SNAPSHOT_ITEMS" :key="k" class="kvi" :data-testid="T.snapshot(k)">
            {{ ({ netstate: '网络形态', proxy: '代理', vpn: 'VPN', subnet: 'WSL 子网', hostip: '主机 IP',
                  mtu: 'MTU', clock: '时钟漂移', wslconfig: '.wslconfig', 'pending-restart': '待重启' } as Record<string, string>)[k] }}
            <b>{{ snapText(k) }}</b>
          </span>
        </div>
        <div class="qt-row">
          <span :data-testid="T.dockerCidr">docker 网段 <b>{{ snap?.docker_cidr ?? '—' }}</b></span>
          <span v-if="store.dockerConflict.state === 'ok'" class="qt-ok qt-small">无冲突</span>
        </div>
        <!-- N-21:常驻黄字,不是一闪而过的 toast -->
        <div v-if="store.dockerConflict.state === 'conflict'" class="warnbar" :data-testid="T.dockerConflict">
          {{ (ALERT_CODES.DOCKER_POOL_ALL_CONFLICT.zh ?? '').replace('{docker_cidr}', snap?.docker_cidr ?? '') }}
          <span v-if="store.dockerConflict.source === 'runtime_vpn'">
            本次是连上 VPN 后才重叠,重启 WSL 大概率避开;反复重叠请 IT 评估。
          </span>
        </div>
      </section>

      <!-- 连通性探测 + 实测采样 -->
      <section class="qt-card box">
        <div class="qt-row">
          <div class="qt-section-title qt-grow">连通性探测</div>
          <a-button :loading="busy" :data-testid="T.probeRun" @click="runProbe">运行</a-button>
          <a-tooltip :title="anyOnline ? '' : '至少一个账号在线才有真实连接可采'">
            <a-button
              :loading="sampling"
              :disabled="!anyOnline"
              :data-testid="T.probeSample"
              @click="runSample"
            >实测采样{{ sampling ? `(${sampleLeft}s)` : '' }}</a-button>
          </a-tooltip>
        </div>
        <ProbeTable :rows="store.probes" @rerun="rerunTarget" />

        <div class="qt-section-title mt">实测采样(上次 {{ store.sampledAt?.slice(11, 16) ?? '—' }})</div>
        <table class="tbl" :data-testid="T.sampleTable">
          <thead><tr><th>账号</th><th>通道</th><th>实际连接</th><th>端口</th><th>协议</th><th>样本</th><th>写入</th></tr></thead>
          <tbody>
            <tr v-for="(s, i) in store.samples" :key="`${s.account_id}-${i}`" :data-testid="T.sampleRow(s.account_id, i)">
              <td>{{ s.account_id }}</td>
              <td>{{ s.channel }}</td>
              <td class="qt-mono qt-small">{{ s.remote_host ? `${s.remote_host}→` : '' }}{{ s.remote_ip }}</td>
              <td>{{ s.port }}</td>
              <td>{{ s.proto }}</td>
              <td>{{ s.samples }}</td>
              <td>
                <a-checkbox
                  :data-testid="T.sampleRowPick(s.account_id, i)"
                  :checked="samplePick.has(String(i))"
                  @change="(e: any) => { const n = new Set(samplePick); e.target.checked ? n.add(String(i)) : n.delete(String(i)); samplePick = n }"
                />
              </td>
            </tr>
            <tr v-if="!store.samples.length"><td colspan="7" class="qt-muted">还没有采样结果</td></tr>
          </tbody>
        </table>
        <a-button
          :disabled="!samplePick.size"
          :data-testid="T.sampleApply"
          @click="sampleApplyOpen = true"
        >写入探测目标(需确认)</a-button>
      </section>

      <!-- 公网端点 -->
      <section class="qt-card box">
        <div class="qt-section-title">公网端点</div>
        <div class="qt-row wrap">
          <span class="kvi" :data-testid="T.pubep('ip')">出口 IP <b>{{ store.publicEndpoint?.public_ip ?? '—' }}</b></span>
          <span class="kvi" :data-testid="T.pubep('host')">配置域名 <b>{{ store.publicEndpoint?.configured_host ?? '—' }}</b></span>
          <span
            class="kvi"
            :data-testid="T.pubep('dns')"
            :class="{ 'qt-danger': store.publicEndpoint && !store.publicEndpoint.matches }"
          >
            解析 <b>{{ store.publicEndpoint?.matches ? '一致' : '不一致' }}</b>
            <span v-if="store.publicEndpoint && !store.publicEndpoint.matches">
              —— 域名解析 ≠ 当前出口,使用方侧 DDNS/反代需更新
            </span>
          </span>
          <span class="kvi" :data-testid="T.pubep('changed')">最近变更 <b>{{ store.publicEndpoint?.last_changed_at ?? '—' }}</b></span>
        </div>
        <a-collapse>
          <a-collapse-panel key="h" :header="`历史 ${store.publicEndpoint?.history?.length ?? 0}`" :data-testid="T.pubepHistory">
            <div
              v-for="(h, i) in store.publicEndpoint?.history ?? []"
              :key="i"
              class="qt-small"
              :data-testid="T.pubepHistoryRow(i)"
            >{{ h.at }} {{ h.from_ip }} → {{ h.to_ip }}</div>
          </a-collapse-panel>
        </a-collapse>
        <p class="qt-small qt-muted">本机 API 不绑 IP;公网入站请在使用方侧用域名 + DDNS/反代,IP 变了只需改解析。</p>
      </section>

      <!-- 微信模块 -->
      <section v-if="wxStatus" class="qt-card box">
        <div class="qt-section-title">微信模块</div>
        <div :data-testid="T.wxblockDomains">
          屏蔽的下载域名:{{ (hostsBlock.domains ?? ['dldir1.qq.com', 'dldir1v6.qq.com']).join('、') }}
        </div>
        <div :data-testid="T.wxblockState" :class="{ 'qt-warn': hostsBlock.last_result === 'blocked_by_policy' }">
          生效状态:{{ hostsBlock.enabled ? '已启用' : '未启用' }}
          ({{ hostsBlock.last_result ?? '—' }}{{ hostsBlock.applied_at ? ` · ${hostsBlock.applied_at}` : '' }})
          <template v-if="hostsBlock.last_result === 'blocked_by_policy'">
            —— hosts 受保护(只读/EDR/组策略),已降级为告警;微信仍会收到更新提示,由版本守卫兜底
          </template>
        </div>
        <a-popconfirm
          :title="hostsBlock.enabled ? '关闭微信更新屏蔽?' : '开启微信更新屏蔽(将写本机 hosts)?'"
          @confirm="waAction('wechat.update-block', { enable: !hostsBlock.enabled }, '已切换更新屏蔽').then(loadWx)"
        >
          <a-switch :data-testid="T.wxblockToggle" :checked="!!hostsBlock.enabled" />
        </a-popconfirm>
        <!-- R4-9:独立 warn,只针对微信通道,不进产品级水位 -->
        <div v-if="wxDisk" class="warnbar" :data-testid="T.wechatDiskWarn">
          {{ (ALERT_CODES.WECHAT_DISK_LOW.zh ?? '').replace('{partition}', wxDisk.partition ?? '') }}
          (剩余 {{ wxDisk.free_mb ?? '—' }} MB)
        </div>
      </section>

      <!-- 日志 -->
      <section class="qt-card box">
        <div class="qt-section-title">日志</div>
        <div class="qt-row">
          <a-button :data-testid="T.diagExport" @click="exportDiag">导出诊断包</a-button>
          <a-checkbox v-model:checked="withShots" :data-testid="T.diagWithShots">包含截图(默认不含)</a-checkbox>
          <a-button :data-testid="T.openLogs" @click="openLogsDir">打开控制台日志目录</a-button>
        </div>
        <JobProgress :job-id="diagJob" @done="onDiagDone" />
      </section>
    </PageState>

    <a-modal
      v-model:open="sampleApplyOpen"
      title="写入探测目标"
      :data-testid="T.sampleApplyModal"
      @ok="applySample"
    >
      <p>将新增/替换以下探测目标(命名 {channel}_{host|ip}:{port},同名即替换):</p>
      <ul>
        <li v-for="i in [...samplePick]" :key="i" class="qt-mono qt-small">
          {{ store.samples[Number(i)]?.channel }}_{{ store.samples[Number(i)]?.remote_ip }}:{{ store.samples[Number(i)]?.port }}
        </li>
      </ul>
    </a-modal>
  </div>
</template>

<style scoped>
.box { padding: var(--qt-space-4); }
.wrap { flex-wrap: wrap; gap: var(--qt-space-3); }
.kvi { font-size: var(--qt-font-sm); }
.tbl { width: 100%; border-collapse: collapse; margin-top: var(--qt-space-2); }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.warnbar { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); margin-top: var(--qt-space-2); }
.actions { margin-top: var(--qt-space-3); }
.mt { margin-top: var(--qt-space-4); }
.hidden { display: none; }
</style>
