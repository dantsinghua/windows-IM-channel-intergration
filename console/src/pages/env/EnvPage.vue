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
/** 勾选态按 `probe_targets_observed.id` 记(R6-58 (cu):不得用行下标) */
const samplePick = ref<Set<number>>(new Set())
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
    // 裁决①:`agent` 是 `{version}` 对象(按裸字符串渲染会显示 [object Object])
    case 'agent': return v.value?.agent?.version ?? '—'
    case 'winagent': return v.value?.winagent?.version ?? '—'
    case 'winagent-user': return v.value?.winagent?.user_agent ? '在线' : '不在线'
    case 'kernel': return `${v.value?.kernel ?? '—'}(${KERNEL_STATE_TEXT[v.value?.kernel_state ?? ''] ?? '—'})`
    case 'wsl': return `${v.value?.wsl ?? '—'}(${WSL_STATE_TEXT[v.value?.wsl_state ?? ''] ?? '—'})`
    case 'docker': return v.value?.docker ?? '—'
    default: return v.value?.distro ?? '—'
  }
}

/**
 * 九项环境快照。🔴 数据源按 #74 的两半:
 * Windows 侧(net_state/代理/VPN/子网/宿主 IP)在 `windows`,WSL 侧(MTU/时钟/内核)在 `wsl`。
 * `windows` 为 null(WinAgent 不可达)时显示「未知」+ 原因,不显示成「无」。
 */
function snapText(k: string): string {
  const s = snap.value
  if (!s) return '—'
  const w = s.windows
  const unknown = w ? '—' : `未知(${store.windowsError ?? 'WinAgent 不可达'})`
  switch (k) {
    case 'netstate': return w?.net_state ? NET_STATE_TEXT[w.net_state] ?? w.net_state : unknown
    case 'proxy': return w ? w.proxy ?? '无' : unknown
    case 'vpn': return w ? w.vpn_adapter ?? '无' : unknown
    case 'subnet': return w ? w.wsl_subnet ?? '—' : unknown
    case 'hostip': return w ? w.host_ip ?? '—' : unknown
    case 'mtu': return String(s.wsl?.mtu ?? '—')
    case 'clock': {
      const ms = s.wsl?.clock?.drift_ms
      return ms == null ? '未探测' : `${(ms / 1000).toFixed(1)} s`
    }
    case 'wslconfig': return s.wslconfig ? JSON.stringify(s.wslconfig) : '未读到(会话代理不在线)'
    default: return s.reboot_required ? '是' : '否'
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
    // 01 §2.7.9:提交的是整张表的当前勾选态(已采纳项默认勾上 + 新候选也勾上),不是本次新增的那几个
    samplePick.value = new Set(store.observed.map((r) => r.id))
  } finally {
    clearInterval(t)
    sampling.value = false
  }
}

/**
 * #76b:`observed_ids` 是**采纳后的全集**(取消勾选一个已采纳项 = 把它从数组里拿掉;
 * 全不勾提交 `[]` 合法 = 清空全部正式目标)。
 */
async function applySample(): Promise<void> {
  await store.adoptObserved([...samplePick.value])
  sampleApplyOpen.value = false
  message.success('已写入探测目标,正在跑一轮探测')
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

/** 当前会被中断的账号(重启 WSL / 排空 / 停机的共同影响面) */
function affectedAccounts(): string[] {
  return accounts.items
    .filter((a) => ['running', 'degraded', 'login_required', 'starting', 'logging_in'].includes(a.state))
    .map((a) => a.id)
}

/**
 * 🔴 基线 §11.6 [NOSHUTDOWN]:重启 WSL **必须**二次确认,并**列出影响面**;
 * 客户端恒带 `confirm:true`(02 #84),不带 confirm 的请求后端会直接拒绝。
 */
function confirmWslRestart(): void {
  const affected = affectedAccounts()
  Modal.confirm({
    title: '重启 WSL(经 Agent,需确认)',
    okType: 'danger',
    okText: '我已知悉影响面,重启',
    content: `将中断这些账号:${affected.join('、') || '无'};`
      + '你在 WSL 里的其它发行版也会一并停止,未保存的工作会丢失。'
      + '本机不直接执行 wsl 命令,请求经 Agent 转 WinAgent 执行。',
    onOk: async () => {
      await systemApi.wslRestart()
      message.success('已请求重启 WSL')
    },
  })
}

/**
 * #82 排空(升级前用)。🔴 **没有逆操作端点**(backend-api-2 §7-3)——
 * 排空之后所有写操作都会 503,恢复受理只能重启 Agent。这句话必须在按下之前说清楚。
 */
function confirmDrain(): void {
  const affected = affectedAccounts()
  Modal.confirm({
    title: '排空 Agent(#82,升级前用)',
    okType: 'danger',
    okText: '我知道要重启才能恢复,排空',
    content: `将停止受理新指令并停止这些账号的容器:${affected.join('、') || '无'};`
      + '在途指令最多等 30 秒。⚠️ 没有「取消排空」的端点 —— 排空后要恢复受理,只能重启 Agent。',
    onOk: async () => {
      const r = await systemApi.drain()
      message.success(
        `已排空:在途 ${r.inflight_before} → ${r.inflight},等待 ${r.waited_s}s,`
        + `停止账号 ${r.stopped_accounts.join('、') || '无'}`,
      )
      session.draining = true
    },
  })
}

/** #83 优雅停机:须 `confirm:true`;回 202 之后控制台会断开 */
function confirmShutdown(): void {
  const affected = affectedAccounts()
  Modal.confirm({
    title: '停止 Agent(#83)',
    okType: 'danger',
    okText: '我已知悉控制台会断开,停机',
    content: `将停止 Agent 与这些账号:${affected.join('、') || '无'};`
      + '停机后控制台会失去连接,需要在 WinAgent 里重新拉起 WSL 发行版才能继续。',
    onOk: async () => {
      await systemApi.shutdown()
      message.success('已请求停机,控制台即将断开')
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
  // 后端未就绪的块各自吞掉,页面照常显示已取到的部分(不白屏、不无限转圈)
  await store.loadPublicEndpoint().catch(() => undefined)
  await store.loadSelftest().catch(() => undefined)
  // 页面直开就显示上一次的采样候选(#76 observed 承载上次结果)
  await store.loadObserved().catch(() => undefined)
  await loadWx()
  if (!accounts.items.length) void accounts.load()
})
</script>

<template>
  <div class="qt-page qt-stack">
    <PageState :loading="store.loading && !v" :error="store.error" @retry="store.loadAll()">
      <a-alert
        v-if="store.agentDownReason"
        type="warning"
        show-icon
        :message="store.agentDownReason"
        class="mb"
      >
        <template #action><a-button size="small" @click="store.loadAll()">重试</a-button></template>
      </a-alert>
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
              <!--
                C-18 / 01 §2.7.9 探测结论表:`skip` = 灰「未探测」,**既不是 ✔ 也不是 ⚠**
                (01 M4-7「SKIPPED 不计红项」)。把「没测」显示成 ✔ 或 ⚠ 都是界面说假话。
                中文说明由 `selftestRows()` 内部的 `probeLineZh()` 生成,这里只管色与图标,与 `P-SETUP` 步 3 同款。
              -->
              <td :class="r.level === 'error' ? 'qt-danger' : r.level === 'warn' ? 'qt-warn' : r.level === 'skip' ? 'qt-muted' : 'qt-ok'">
                {{ r.level === 'error' ? '✗' : r.level === 'warn' ? '⚠' : r.level === 'skip' ? '—' : '✔' }}
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
          <!--
            🔴 01 §2.7.9 :696「**运行期动作按钮**(C-39/C-45;**全部二次确认** + 审计)」——
            本按钮直接调 WinAgent 改 Windows 防火墙规则,与同组其余动作一样必须先确认(D-F)。
          -->
          <a-popconfirm
            title="按端口精确放行 Windows 防火墙规则(不放 Any),并写审计。确认执行?"
            ok-type="danger"
            :disabled="!session.winagentOnline"
            @confirm="waAction('firewall.ensure', {}, '防火墙规则已修复')"
          >
            <a-button
              :disabled="!session.winagentOnline"
              :data-testid="T.firewallFix"
            >修复防火墙规则</a-button>
          </a-popconfirm>
          <a-popconfirm
            title="容器拉镜像将经公司代理,是否启用?"
            @confirm="systemApi.dockerProxy(true).then(() => message.success('已为 docker 启用系统代理'))"
          >
            <a-button
              :disabled="!['SYSTEM_PROXY', 'VPN_ACTIVE_WITH_PROXY'].includes(store.netState)"
              :data-testid="T.dockerProxy"
            >为 docker 启用系统代理</a-button>
          </a-popconfirm>
          <a-button
            danger
            :disabled="session.draining"
            :data-testid="T.wslRestart"
            @click="confirmWslRestart"
          >重启 WSL…</a-button>
          <span :data-testid="T.wslRestartModal" class="hidden" />
          <!--
            #82 / #83:排空与停机。两者都要二次确认并列影响面;
            ⚠️ 01 §4 还没有这两个按钮的元素 id,已列给文档方。
          -->
          <a-button :disabled="session.draining" @click="confirmDrain">排空(升级前)…</a-button>
          <a-button danger @click="confirmShutdown">停止 Agent…</a-button>
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
          <span :data-testid="T.dockerCidr">docker 网段 <b>{{ store.dockerCidr ?? '—' }}</b></span>
          <span v-if="store.dockerConflict.state === 'ok'" class="qt-ok qt-small">无冲突</span>
        </div>
        <!-- N-21:常驻黄字,不是一闪而过的 toast -->
        <div v-if="store.dockerConflict.state === 'conflict'" class="warnbar" :data-testid="T.dockerConflict">
          {{ (ALERT_CODES.DOCKER_POOL_ALL_CONFLICT.zh ?? '').replace('{docker_cidr}', store.dockerCidr ?? '') }}
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

        <div class="qt-section-title mt">实测采样(上次 {{ store.observedAt?.slice(11, 16) ?? '—' }})</div>
        <table class="tbl" :data-testid="T.sampleTable">
          <thead>
            <tr><th>账号</th><th>通道</th><th>实际连接</th><th>端口</th><th>协议</th><th>样本</th><th>已在配置</th><th>写入</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="s in store.observed"
              :key="s.id"
              :data-testid="T.sampleRow(s.account_id ?? 'unknown', s.id)"
            >
              <td>{{ s.account_id ?? '—' }}</td>
              <td>{{ s.channel ?? '—' }}</td>
              <td class="qt-mono qt-small">{{ s.remote_host ? `${s.remote_host}→` : '' }}{{ s.remote_ip }}</td>
              <td>{{ s.port }}</td>
              <td>{{ s.proto ?? '—' }}</td>
              <td>{{ s.samples ?? '—' }}</td>
              <td>{{ s.in_config ? '是' : '新增' }}</td>
              <td>
                <a-checkbox
                  :data-testid="T.sampleRowPick(s.account_id ?? 'unknown', s.id)"
                  :checked="samplePick.has(s.id)"
                  @change="(e: any) => { const n = new Set(samplePick); e.target.checked ? n.add(s.id) : n.delete(s.id); samplePick = n }"
                />
              </td>
            </tr>
            <tr v-if="!store.observed.length"><td colspan="8" class="qt-muted">还没有采样结果</td></tr>
          </tbody>
        </table>
        <div class="qt-row">
          <a-button
            :data-testid="T.sampleApply"
            @click="sampleApplyOpen = true"
          >写入探测目标(需确认)</a-button>
          <span class="qt-small qt-muted">
            提交的是整张表的当前勾选态(全集);取消勾一个已采纳项 = 取消采纳,全不勾 = 清空全部正式目标。
          </span>
        </div>
        <div v-if="store.observedTargets.length" class="qt-small qt-muted">
          当前正式目标:<span v-for="t in store.observedTargets" :key="t" class="qt-mono">{{ t }} </span>
        </div>
      </section>

      <!-- 公网端点 -->
      <section class="qt-card box">
        <div class="qt-section-title">公网端点</div>
        <div class="qt-row wrap">
          <!-- 🔴 键集按 02 #102:`{public_ip, public_ip_v6?, configured_domain?, checked_at, changed_at,
               probe:{url, unreachable_rounds}}`;`matches / dns_resolved_ip / history` 在 docs 里不存在。 -->
          <span class="kvi" :data-testid="T.pubep('ip')">
            出口 IP <b>{{ store.publicEndpoint?.public_ip ?? '—' }}</b>
            <span v-if="store.publicEndpoint?.public_ip_v6" class="qt-small qt-muted">
              / v6 {{ store.publicEndpoint.public_ip_v6 }}
            </span>
          </span>
          <span class="kvi" :data-testid="T.pubep('host')">
            配置域名 <b>{{ store.publicEndpoint?.configured_domain ?? '—' }}</b>
          </span>
          <span
            class="kvi"
            :data-testid="T.pubep('dns')"
            :class="{ 'qt-danger': (store.publicEndpoint?.probe?.unreachable_rounds ?? 0) > 0 }"
          >
            出口探测
            <b v-if="(store.publicEndpoint?.probe?.unreachable_rounds ?? 0) > 0">
              连续不可达 {{ store.publicEndpoint?.probe?.unreachable_rounds }} 轮
            </b>
            <b v-else>正常</b>
            <span class="qt-small qt-muted">
              ({{ store.publicEndpoint?.probe?.url ?? '未配置探测源' }};域名解析是否与出口一致请在使用方侧核对 ——
              Agent 不下发解析比对结果)
            </span>
          </span>
          <span class="kvi" :data-testid="T.pubep('changed')">
            最近变更 <b>{{ store.publicEndpoint?.changed_at ?? '—' }}</b>
            <span class="qt-small qt-muted">· 上次探测 {{ store.publicEndpoint?.checked_at ?? '—' }}</span>
          </span>
        </div>
        <a-collapse>
          <!-- 历史来自 `NET_PUBLIC_ENDPOINT_CHANGED` 事件(01 §2.7.9),不是 #102 的出参 -->
          <a-collapse-panel key="h" :header="`历史 ${store.endpointHistory.length}`" :data-testid="T.pubepHistory">
            <div
              v-for="(h, i) in store.endpointHistory"
              :key="i"
              class="qt-small"
              :data-testid="T.pubepHistoryRow(i)"
            >{{ h.at }} {{ h.from_ip }} → {{ h.to_ip }}</div>
            <div v-if="!store.endpointHistory.length" class="qt-small qt-muted">
              本次会话内未收到出口变更事件。
            </div>
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
      <p>提交后的正式探测目标 = 下面这些(全集;命名 {channel}_{host|ip}:{port},同名即替换):</p>
      <ul>
        <li v-for="s in store.observed.filter((x) => samplePick.has(x.id))" :key="s.id" class="qt-mono qt-small">
          {{ s.channel ?? 'any' }}_{{ s.remote_host || s.remote_ip }}:{{ s.port }}
        </li>
        <li v-if="!samplePick.size" class="qt-small qt-danger">
          一个都没勾 —— 提交后会**清空**全部正式探测目标。
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
.mb { margin-bottom: var(--qt-space-3); }
.hidden { display: none; }
</style>
