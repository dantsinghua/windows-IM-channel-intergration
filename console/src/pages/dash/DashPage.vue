<script setup lang="ts">
/**
 * `P-DASH` 首页 / 资源仪表(01 §2.7.2)。
 * 一眼看到两个池的余量、能再开几个、账号在线汇总、最近告警;
 * 资源**只留两格摘要**(内存/磁盘),细节全在 `P-RES`(E-19)。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { dash as T, DASH_SYS_DOTS } from '@/testids'
import { useResourcesStore } from '@/stores/resources'
import { useAccountsStore } from '@/stores/accounts'
import { useEventsStore, alertRoute } from '@/stores/events'
import { useEnvStore } from '@/stores/env'
import { useMailStore } from '@/stores/mail'
import { useSessionStore } from '@/stores/session'
import AlertCard from '@/components/AlertCard.vue'
import { CHANNELS, CHANNEL_TEXT, KERNEL_STATE_TEXT } from '@/i18n/zh-CN/codes'

const router = useRouter()
const resources = useResourcesStore()
const accounts = useAccountsStore()
const events = useEventsStore()
const env = useEnvStore()
const mail = useMailStore()
const session = useSessionStore()

const pool = computed(() => resources.pool)
const slots = computed(() => resources.slots)
const metrics = computed(() => resources.metrics)
const zeroAccounts = computed(() => accounts.items.length === 0)

/** 「档案 N」= channel=wechat 且 stopped 的 Account 行数(C-01) */
const wechatProfileCount = computed(() => accounts.wechatProfiles.length)

const mailSummary = computed(() => {
  const r = mail.status?.routes?.[0]
  if (!r) return null
  return {
    lastRecv: r.inbound.last_success_at ?? '—',
    queued: r.outbound.queued,
    dead: r.outbound.dead,
    quota: r.inbound.quota.limit_mb ? Math.round((r.inbound.quota.used_mb / r.inbound.quota.limit_mb) * 100) : 0,
    idle: r.inbound.idle_supported,
  }
})

function levelColor(level?: string): string {
  if (level === 'warn') return 'var(--qt-sev-warn)'
  if (level === 'high' || level === 'critical') return 'var(--qt-sev-crit)'
  return 'var(--qt-border)'
}

function gb(mb?: number | null): string {
  return mb == null ? '—' : (mb / 1024).toFixed(1)
}

function go(p: string): void { void router.push(p) }

/** 新增入口:can_add==0 也可点,进 P-ACCT-NEW 第一步被拒并显示 Agent 给的替代方案 */
function add(ch: string): void { void router.push({ path: '/acct/new', query: { ch } }) }

function sysDot(what: string): { ok: boolean; text: string } {
  switch (what) {
    case 'agent': return { ok: session.agentReachable, text: 'Agent' }
    case 'winagent': return { ok: session.winagentOnline, text: 'WinAgent' }
    case 'winagent-user': return { ok: session.userAgentOnline, text: '会话代理' }
    case 'ws': return { ok: events.connected, text: 'WS' }
    case 'kernel': return {
      ok: env.version?.kernel_state === 'OURS',
      text: `内核 ${KERNEL_STATE_TEXT[env.version?.kernel_state ?? ''] ?? '—'}`,
    }
    default: return { ok: !!env.version?.docker, text: `docker ${env.version?.docker ?? '—'}` }
  }
}
</script>

<template>
  <div class="qt-page qt-stack">
    <div class="grid2">
      <!-- 资源池两条进度条留在 P-DASH:它们是「还能开几个」的直接依据 -->
      <div class="qt-card box">
        <div class="qt-section-title">资源池</div>
        <div :data-testid="T.poolWsl" class="pool">
          <div class="qt-row"><span class="qt-grow">WSL 池</span>
            <span>{{ gb(pool?.pools.wsl.used_mb) }} / {{ gb(pool?.pools.wsl.total_mb) }} GB</span>
          </div>
          <a-progress
            :percent="pool ? Math.round((pool.pools.wsl.used_mb / Math.max(1, pool.pools.wsl.total_mb)) * 100) : 0"
            size="small" :show-info="false"
          />
          <div class="qt-small qt-muted">预留 {{ gb(pool?.pools.wsl.reserved_mb) }} GB</div>
        </div>
        <div :data-testid="T.poolWindows" class="pool">
          <div class="qt-row"><span class="qt-grow">Windows 池</span>
            <span>微信 {{ gb(pool?.pools.windows.wechat_mb) }} GB</span>
          </div>
          <a-progress
            :percent="pool ? Math.round((pool.pools.windows.wechat_mb / Math.max(1, pool.pools.windows.total_mb)) * 100) : 0"
            size="small" :show-info="false"
          />
          <div class="qt-small qt-muted" :data-testid="T.wechatSlot">
            微信槽 {{ slots?.used ?? 0 }}/{{ slots?.max ?? 1 }} · holder {{ slots?.holder || '—' }} · pending {{ slots?.pending || '—' }}
          </div>
        </div>
      </div>

      <div class="qt-card box">
        <div class="qt-section-title">可新增</div>
        <div v-for="ch in CHANNELS" :key="ch" class="qt-row canadd">
          <span class="qt-grow">{{ CHANNEL_TEXT[ch] }}</span>
          <!-- 直接显示 can_add[通道],控制台不重算(算法在 Agent) -->
          <span :data-testid="T.canadd(ch)" class="qt-mono">还能开 {{ resources.canAdd[ch] ?? 0 }} 个</span>
          <span class="qt-small qt-muted">{{ gb(pool?.quota_mb?.[ch]) }} GB/个</span>
          <a-button size="small" :data-testid="T.add(ch)" @click="add(ch)">新增</a-button>
        </div>
        <a-button
          v-if="!session.wechatDisabled"
          size="small"
          :data-testid="T.wechatSwitch"
          @click="go('/acct')"
        >切换微信({{ wechatProfileCount }} 个档案)</a-button>
      </div>
    </div>

    <div class="grid2">
      <div class="qt-card box">
        <div class="qt-section-title">账号状态</div>
        <a
          v-for="ch in CHANNELS"
          :key="ch"
          class="qt-row summary"
          :data-testid="T.acctSummary(ch)"
          @click="go('/acct')"
        >
          <span class="qt-grow">{{ CHANNEL_TEXT[ch] }}</span>
          <span class="qt-ok">{{ accounts.summary[ch].online }} 在线</span>
          <span class="qt-warn">{{ accounts.summary[ch].loginRequired }} 待登录</span>
          <span class="qt-muted">{{ accounts.summary[ch].stopped }} 停止</span>
        </a>
        <div v-if="zeroAccounts" class="qt-row">
          <a-button
            v-for="ch in CHANNELS"
            :key="ch"
            size="small"
            :data-testid="T.emptyAdd(ch)"
            @click="add(ch)"
          >添加第一个{{ CHANNEL_TEXT[ch] }}账号</a-button>
        </div>
      </div>

      <div class="qt-card box">
        <div class="qt-section-title">最近告警(firing,按 severity)</div>
        <div :data-testid="T.alertList">
          <AlertCard
            v-for="(a, i) in events.firing.slice(0, 6)"
            :key="a.code + a.subject"
            :alert="a"
            :testid="T.alertItem(i)"
            :action-testid="(act) => T.alertItemAction(i, act)"
            @open="go(alertRoute(a))"
            @action="(act) => go(act === 'open_mail' ? '/mail' : act === 'open_account' ? '/acct' : '/env')"
          />
          <a-empty v-if="!events.firing.length" description="暂无未恢复告警" />
        </div>
      </div>
    </div>

    <!-- 邮件健康格(C-45,06 §4) -->
    <div v-if="mailSummary" class="qt-card box mailbar" :data-testid="T.mailHealth" @click="go('/mail')">
      邮件摆渡 —— 最近收到 {{ mailSummary.lastRecv }} | 队列 {{ mailSummary.queued }} |
      死信 {{ mailSummary.dead }} | 容量 {{ mailSummary.quota }}% | IDLE {{ mailSummary.idle ? '✔' : '—' }}
    </div>

    <!-- 资源摘要:只此两格,点击跳 P-RES -->
    <div class="grid2">
      <div
        class="qt-card box resbox"
        :data-testid="T.resMem"
        :style="{ borderColor: levelColor(metrics?.mem_watermark.level) }"
        @click="go('/res')"
      >
        <div class="qt-row">
          <strong class="qt-grow">内存</strong>
          <span class="qt-small">[{{ metrics?.mem_watermark.level ?? 'normal' }}]</span>
        </div>
        <div>
          已用 {{ gb(metrics?.hardware?.mem?.used_mb) }} / {{ gb(metrics?.hardware?.mem?.total_mb) }} GB ·
          可用 {{ gb(metrics?.hardware?.mem?.avail_mb) }} GB
        </div>
      </div>
      <div
        class="qt-card box resbox"
        :data-testid="T.resDisk"
        :style="{ borderColor: levelColor(metrics?.disk_watermark.level) }"
        @click="go('/res')"
      >
        <div class="qt-row">
          <strong class="qt-grow">磁盘</strong>
          <span class="qt-small">[{{ metrics?.disk_watermark.level ?? 'normal' }}]</span>
        </div>
        <div>
          <span v-for="d in metrics?.hardware?.disks ?? []" :key="d.mount" class="diskpart">
            {{ d.mount }} 剩 {{ gb(d.free_mb) }} GB
          </span>
          <span v-if="!metrics">—</span>
        </div>
      </div>
    </div>

    <div class="qt-card box sysbar">
      <a v-for="w in DASH_SYS_DOTS" :key="w" class="sysdot" :data-testid="T.sys(w)" @click="go('/env')">
        <span class="dot" :style="{ background: sysDot(w).ok ? 'var(--qt-state-running)' : 'var(--qt-state-error)' }" />
        {{ sysDot(w).text }}
      </a>
      <span class="qt-grow" />
      <span class="qt-small qt-muted">上次自检 {{ env.selftestAt ?? '—' }}</span>
    </div>
  </div>
</template>

<style scoped>
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--qt-space-4); }
.box { padding: var(--qt-space-4); }
.pool + .pool { margin-top: var(--qt-space-3); }
.canadd { padding: 4px 0; }
.summary { padding: 6px 0; cursor: pointer; border-bottom: 1px solid var(--qt-border); }
.mailbar { cursor: pointer; }
.resbox { cursor: pointer; border-width: 2px; }
.diskpart + .diskpart { margin-left: var(--qt-space-3); }
.sysbar { display: flex; align-items: center; gap: var(--qt-space-4); }
.sysdot { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; color: var(--qt-text); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
</style>
