<script setup lang="ts">
/**
 * `P-RES` 资源监控(E-19,独立页;01 §2.7.2a)。
 * 左「整机硬件」右「本程序占用」两栏并排;**本期(M1~M5)= 快照 + 水位色 + 最近一次清理结果**,
 * 24h 时间序列曲线延后到 M6(R6-17/R-13/§11.22 [SCOPE])。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { res as T, RES_DIRS } from '@/testids'
import { useResourcesStore } from '@/stores/resources'
import { useAccountsStore } from '@/stores/accounts'
import { accountsApi, resourcesApi } from '@/api/client'
import JobProgress from '@/components/JobProgress.vue'
import PageState from '@/components/PageState.vue'
import { CHANNELS, CHANNEL_TEXT } from '@/i18n/zh-CN/codes'
import type { Job } from '@/api/types'

const router = useRouter()
const store = useResourcesStore()
const accounts = useAccountsStore()

const cleanupJob = ref<string | null>(null)
const cleanupResult = ref<string>('')
const calibrateJob = ref<string | null>(null)
const calibrateResult = ref<string>('')
const busy = ref(false)
const consoleRssMb = ref<number | null>(null)
/** 60s 防重(与 P-MAIL 立即清理同款) */
const cleanupLockedUntil = ref(0)

const m = computed(() => store.metrics)
const disk = computed(() => m.value?.disk_watermark)
const mem = computed(() => m.value?.mem_watermark)

/** 差值超阈值标注「配额偏松/偏紧」;阈值以 04 为准,前端用 30% 作显示门槛 */
const DRIFT_THRESHOLD = 30

function mb(v?: number | null): string { return v == null ? '—' : `${(v / 1024).toFixed(1)} GB` }
function pct(v?: number | null): string { return v == null ? '—' : `${v}%` }

function budgetOf(ch: string) {
  const rows = (m.value?.budget_vs_actual ?? []).filter((b) => accounts.byId[b.id]?.channel === ch)
  const quota = rows.reduce((s, r) => s + r.quota_mb, 0)
  const rss = rows.reduce((s, r) => s + (r.rss_mb ?? 0), 0)
  const drift = quota ? Math.round(((rss - quota) / quota) * 100) : 0
  return { quota, rss, drift, loose: Math.abs(drift) > DRIFT_THRESHOLD }
}

async function refresh(): Promise<void> {
  await store.loadMetrics()
  consoleRssMb.value = Math.round(((await window.qt?.app.rssKb()) ?? 0) / 1024) || null
}

async function runCleanup(): Promise<void> {
  if (Date.now() < cleanupLockedUntil.value) return
  busy.value = true
  try {
    const r = await resourcesApi.cleanupRun()
    cleanupJob.value = r.job_id
    cleanupLockedUntil.value = Date.now() + 60000
  } finally {
    busy.value = false
  }
}

function onCleanupDone(job: Job): void {
  // 🔴 R6-34:清出量字段是 result.freed_mb,不是 cleaned_mb
  const freed = job.result?.freed_mb
  cleanupResult.value = job.state === 'succeeded' ? `已清出 ${freed ?? 0} MB` : `清理${job.state === 'cancelled' ? '已取消' : '失败'}`
  cleanupJob.value = null
  void refresh()
}

async function runCalibrate(): Promise<void> {
  busy.value = true
  try {
    const r = await resourcesApi.calibrate(true)
    if (r.job_id) calibrateJob.value = r.job_id
    else {
      calibrateResult.value = Object.entries(r.suggestions ?? {})
        .map(([k, v]) => `${CHANNEL_TEXT[k as 'qidian'] ?? k} → ${v} MB`).join(' · ')
      await refresh()
    }
  } finally {
    busy.value = false
  }
}

function onCalibrateDone(job: Job): void {
  calibrateResult.value = job.state === 'succeeded' ? '预算已写回' : '校准失败'
  calibrateJob.value = null
  void refresh()
}

async function stopAccount(id: string): Promise<void> {
  await accountsApi.disable(id)
  await accounts.load()
  await refresh()
}

onMounted(() => { void refresh() })
</script>

<template>
  <div class="qt-page qt-stack">
    <div class="qt-row">
      <h2 class="qt-grow">资源监控</h2>
      <span class="qt-small qt-muted">上次 {{ store.lastAt?.slice(11, 19) ?? '—' }}</span>
      <a-button :data-testid="T.refresh" :loading="store.loading" @click="refresh">刷新</a-button>
    </div>

    <PageState :loading="store.loading && !m" :error="store.error" @retry="refresh">
      <div class="cols">
        <!-- 左:整机硬件 -->
        <section class="qt-card box" :data-testid="T.col('host')">
          <div class="qt-section-title">整机硬件</div>
          <!-- 02 #77 的 `hardware` 组后端本期还没下发:显示「—」并说明,不白屏也不假装有数 -->
          <p v-if="m && !m.hardware" class="qt-small qt-muted">
            整机硬件快照暂不可用(Agent 未下发 <code>hardware</code> 组);下方各项显示「—」。
          </p>

          <h4>内存</h4>
          <div class="kv"><span>物理总量</span><b :data-testid="T.memHost('total')">{{ mb(m?.hardware?.mem?.total_mb) }}</b></div>
          <div class="kv"><span>已用</span><b :data-testid="T.memHost('used')">{{ mb(m?.hardware?.mem?.used_mb) }}</b></div>
          <div class="kv"><span>可用</span><b :data-testid="T.memHost('avail')">{{ mb(m?.hardware?.mem?.avail_mb) }}</b></div>
          <div class="kv"><span>vmmem(WSL VM)</span><b :data-testid="T.memVmmem">{{ mb(m?.hardware?.mem?.vmmem_mb) }}</b></div>

          <h4>CPU</h4>
          <div class="kv"><span>逻辑核</span><b :data-testid="T.cpuHost('cores')">{{ m?.hardware?.cpu?.logical_cores ?? '—' }}</b></div>
          <div class="kv"><span>总负载</span><b :data-testid="T.cpuHost('load')">{{ pct(m?.hardware?.cpu?.load_pct) }}</b></div>

          <h4>磁盘</h4>
          <div v-for="d in m?.hardware?.disks ?? []" :key="d.mount" class="kv">
            <span>{{ d.mount }}</span>
            <b>
              <span :data-testid="T.diskPart(d.mount, 'total')">{{ mb(d.total_mb) }}</span> /
              剩 <span :data-testid="T.diskPart(d.mount, 'free')">{{ mb(d.free_mb) }}</span>
            </b>
          </div>
          <div class="watermark" :data-testid="T.diskWatermark" :class="`lv-${disk?.level ?? 'normal'}`">
            三级水位:normal — warn — high — critical(当前 <b>{{ disk?.level ?? 'normal' }}</b>)
          </div>
          <div :data-testid="T.diskDegradeList" class="degrade">
            <div class="qt-small qt-muted">已触发降级:</div>
            <template v-if="disk?.actions?.length">
              <span v-for="a in disk.actions" :key="a" class="tag" :data-testid="T.diskDegrade(a)">{{ a }}</span>
            </template>
            <span v-else class="qt-small qt-muted">暂无动作</span>
          </div>
        </section>

        <!-- 右:本程序占用 -->
        <section class="qt-card box" :data-testid="T.col('ours')">
          <div class="qt-section-title">本程序占用</div>

          <h4>内存(配额 vs 实占)</h4>
          <div class="kv"><span>Agent</span><b :data-testid="T.memProc('agent')">{{ mb(m?.ours.procs.agent_mb) }}</b></div>
          <div class="kv"><span>WinAgent</span><b :data-testid="T.memProc('winagent')">{{ mb(m?.ours.procs.winagent_mb) }}</b></div>
          <div class="kv">
            <span>控制台</span>
            <b :data-testid="T.memProc('console')">{{ mb(consoleRssMb ?? m?.ours.procs.console_mb) }}</b>
          </div>
          <div v-for="a in m?.ours.accounts ?? []" :key="a.id" class="kv">
            <span>{{ a.id }} 容器</span>
            <b>
              anon <span :data-testid="T.memAcct(a.id, 'anon')">{{ mb(a.anon_mb ?? a.rss_mb) }}</span> /
              current <span :data-testid="T.memAcct(a.id, 'current')">{{ mb(a.current_mb ?? a.rss_mb) }}</span>
              <span v-if="a.cpu_pct != null" class="qt-small qt-muted">· CPU {{ pct(a.cpu_pct) }}</span>
            </b>
          </div>
          <!-- R6-58 (aa):每进程明细(采样缺失时后端给 null,照原样显示「—」不编造) -->
          <div v-for="d in m?.ours.procs_detail ?? []" :key="d.name" class="kv">
            <span>{{ d.name }}</span>
            <b>{{ mb(d.rss_mb) }} · CPU {{ pct(d.cpu_pct) }}</b>
          </div>
          <div class="kv"><span>微信 PC</span><b :data-testid="T.memWechat">{{ mb(m?.ours.wechat?.wechat_pc_mb) }}</b></div>
          <div class="kv"><span>chatlog</span><b :data-testid="T.memChatlog">{{ mb(m?.ours.wechat?.chatlog_mb) }}</b></div>

          <div v-for="ch in CHANNELS" :key="ch" class="budget" :data-testid="T.memBudget(ch)">
            <div class="qt-row">
              <span class="qt-grow">{{ CHANNEL_TEXT[ch] }} 预算 {{ mb(budgetOf(ch).quota) }} / 实占 {{ mb(budgetOf(ch).rss) }}</span>
              <span v-if="budgetOf(ch).loose" class="qt-warn qt-small">
                配额{{ budgetOf(ch).drift < 0 ? '偏松' : '偏紧' }}({{ budgetOf(ch).drift }}%)
              </span>
              <a-button
                v-if="budgetOf(ch).loose"
                size="small"
                :data-testid="T.memBudgetCalibrate(ch)"
                @click="runCalibrate"
              >校准</a-button>
            </div>
            <a-progress
              :percent="budgetOf(ch).quota ? Math.min(100, Math.round((budgetOf(ch).rss / budgetOf(ch).quota) * 100)) : 0"
              size="small" :show-info="false"
            />
          </div>

          <h4>CPU</h4>
          <div v-for="a in m?.ours.accounts ?? []" :key="`cpu-${a.id}`" class="kv">
            <span>{{ a.id }}</span><b :data-testid="T.cpuAcct(a.id)">{{ pct(a.cpu_pct) }}</b>
          </div>
          <div class="kv"><span>Agent 进程</span><b :data-testid="T.cpuProc('agent')">—</b></div>

          <h4>我方目录占用</h4>
          <div v-for="d in RES_DIRS" :key="d" class="kv">
            <span>{{ d }}</span>
            <b :data-testid="T.diskDir(d)">{{ mb((m?.ours.storage as Record<string, number> | undefined)?.[`${d}_mb`]) }}</b>
          </div>
          <div class="qt-small" :data-testid="T.diskRetention">
            保留期 {{ disk?.retention_shrunk_to ?? 30 }} 天{{ disk?.retention_shrunk_to ? '(已被水位压缩)' : '(未被压缩)' }} ·
            上次清理 {{ disk?.last_cleanup_at ?? '—' }} 清出 {{ disk?.last_cleanup_freed_mb ?? 0 }} MB
          </div>
          <div class="qt-small qt-warn" :data-testid="T.diskVhdxDiff">
            ext4.vhdx 镜像比 WSL 内实际用量多 {{ disk?.vhdx_grown_mb ?? 0 }} MB ——
            删数据不会自动缩小镜像;需停 WSL 才能压缩(需你确认,去环境页走重启 WSL 流程)
          </div>
        </section>
      </div>

      <!-- 时间序列曲线:M6 交付,M1~M5 只给当前值 + 水位色 -->
      <section class="qt-card box">
        <div class="qt-section-title">时间序列曲线</div>
        <div class="placeholder">
          <span :data-testid="T.chart('mem')">时间序列曲线 M6 交付</span>
          <span :data-testid="T.chart('cpu')" class="hidden-m6" />
          <span :data-testid="T.chart('disk')" class="hidden-m6" />
          <span :data-testid="T.chartAlertMark(0)" class="hidden-m6" />
        </div>
      </section>

      <!-- 内存水位与 LRU 建议 -->
      <section class="qt-card box">
        <div class="qt-section-title">内存水位与建议</div>
        <div :data-testid="T.memWatermark" :class="`lv-${mem?.level ?? 'normal'}`">
          内存水位 <b>{{ mem?.level ?? 'normal' }}</b>(warn {{ mb(mem?.warn_mb) }} / crit {{ mb(mem?.critical_mb) }});
          低于水位时:告警 → 暂停新增与自动恢复 → 建议停用
        </div>
        <p class="qt-small qt-muted" :data-testid="T.lruNote">
          系统不会自动停用你的账号——除非你在某账号详情页为它开启 auto_stop_on_pressure
        </p>
        <table class="tbl" :data-testid="T.lruList">
          <thead><tr><th>账号</th><th>最后收发</th><th>占用</th><th>操作</th></tr></thead>
          <tbody>
            <tr
              v-for="r in mem?.lru_suggest ?? []"
              :key="r.id"
              :data-testid="T.lruRow(r.id)"
              :class="{ autostop: r.auto_stop_on_pressure }"
            >
              <td>
                {{ r.id }}
                <span v-if="r.auto_stop_on_pressure" class="qt-danger qt-small">
                  已允许自动停用:内存 critical 时可能被按 LRU 自动停
                </span>
              </td>
              <td>{{ r.last_seen_at ?? '—' }}</td>
              <td>{{ mb(r.rss_mb) }}</td>
              <td>
                <a-popconfirm title="停用该账号?停用后不占资源、不自动恢复" @confirm="stopAccount(r.id)">
                  <a-button size="small" :data-testid="T.lruRowStop(r.id)">停用</a-button>
                </a-popconfirm>
              </td>
            </tr>
            <tr v-if="!(mem?.lru_suggest ?? []).length">
              <td colspan="4" class="qt-muted">内存充裕,暂无建议</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 动作 -->
      <section class="qt-card box">
        <div class="qt-section-title">动作</div>
        <div class="qt-row">
          <a-popconfirm
            title="立即执行本地全量清理?只清本机已过保留期的数据与临时文件,绝不删服务器邮件"
            @confirm="runCleanup"
          >
            <a-button
              type="primary"
              :data-testid="T.actionCleanup"
              :disabled="busy || !!cleanupJob || Date.now() < cleanupLockedUntil"
            >立即清理</a-button>
          </a-popconfirm>
          <a-popconfirm title="按实占重算三通道预算并写回?" @confirm="runCalibrate">
            <a-button :data-testid="T.actionCalibrate" :disabled="busy || !!calibrateJob">资源自校准</a-button>
          </a-popconfirm>
          <a-button :data-testid="T.actionOpenEnv" @click="router.push('/env')">去环境页</a-button>
        </div>
        <JobProgress
          :job-id="cleanupJob"
          :testid="T.actionCleanupProgress"
          @done="onCleanupDone"
        />
        <div v-if="cleanupResult" :data-testid="T.actionCleanupResult" class="qt-ok">{{ cleanupResult }}</div>
        <JobProgress
          :job-id="calibrateJob"
          :testid="T.actionCalibrateProgress"
          @done="onCalibrateDone"
        />
        <div v-if="calibrateResult" :data-testid="T.actionCalibrateResult" class="qt-ok">{{ calibrateResult }}</div>
      </section>
    </PageState>
  </div>
</template>

<style scoped>
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: var(--qt-space-4); }
.box { padding: var(--qt-space-4); }
h4 { margin: var(--qt-space-3) 0 var(--qt-space-1); font-size: var(--qt-font-sm); color: var(--qt-text-secondary); }
.kv { display: flex; justify-content: space-between; padding: 2px 0; border-bottom: 1px dashed var(--qt-border); }
.watermark { margin-top: var(--qt-space-2); padding: 4px 8px; border-radius: var(--qt-radius-sm); }
.lv-warn { background: #FFFBE6; color: var(--qt-sev-warn); }
.lv-high, .lv-critical { background: #FFF1F0; color: var(--qt-sev-crit); }
.degrade { margin-top: var(--qt-space-2); }
.tag { display: inline-block; margin-right: 6px; padding: 0 6px; border: 1px solid var(--qt-sev-warn); border-radius: 8px; font-size: var(--qt-font-xs); }
.budget { margin-top: var(--qt-space-2); }
.placeholder { color: var(--qt-text-secondary); padding: var(--qt-space-6); text-align: center; border: 1px dashed var(--qt-border); }
.hidden-m6 { display: none; }
.tbl { width: 100%; border-collapse: collapse; margin-top: var(--qt-space-2); }
.tbl th, .tbl td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--qt-border); }
.autostop td { color: var(--qt-state-error); }
</style>
