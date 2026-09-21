<script setup lang="ts">
/**
 * 全局外框(01 §2.7 统一外框 + §4 shell 段)。
 * 左侧 200px 导航(可折叠 56px)+ 顶栏(WS 连接灯、资源摘要芯片、告警铃、主题切换)+ 内容区。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { shell as T } from '@/testids'
import { useSessionStore } from '@/stores/session'
import { useEventsStore, alertRoute } from '@/stores/events'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useMessagesStore } from '@/stores/messages'
import { useCommandsStore } from '@/stores/commands'
import { useWorkflowsStore } from '@/stores/workflows'
import { useMailStore } from '@/stores/mail'
import { useEnvStore } from '@/stores/env'
import { useJobsStore } from '@/stores/jobs'
import { useUiStore } from '@/stores/ui'
import { useSetupStore } from '@/stores/setup'
import { installGuards } from '@/router'
import AlertCard from '@/components/AlertCard.vue'
import { CHANNELS, STATE_CODES } from '@/i18n/zh-CN/codes'
import type { AccountStatePayload, QtEvent } from '@/api/types'

const NAV_ITEMS = [
  { seg: 'dash', path: '/dash', label: '首页' },
  { seg: 'res', path: '/res', label: '资源监控' },
  { seg: 'acct', path: '/acct', label: '账号' },
  { seg: 'screen', path: '/screen', label: '画面' },
  { seg: 'cmd', path: '/cmd', label: '指令台' },
  { seg: 'flow', path: '/flow', label: '工作流' },
  { seg: 'msg', path: '/msg', label: '消息' },
  { seg: 'mail', path: '/mail', label: '邮件摆渡' },
  { seg: 'env', path: '/env', label: '环境' },
  { seg: 'set', path: '/set', label: '设置' },
  { seg: 'log', path: '/log', label: '日志 / 审计' },
] as const

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const events = useEventsStore()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const messages = useMessagesStore()
const commands = useCommandsStore()
const workflows = useWorkflowsStore()
const mail = useMailStore()
const env = useEnvStore()
const jobs = useJobsStore()
const ui = useUiStore()
const setup = useSetupStore()

const staleSeconds = ref(0)
let staleTimer: ReturnType<typeof setInterval> | null = null
let resourceTimer: ReturnType<typeof setInterval> | null = null
let unsubRoute: (() => void) | undefined

const isSetupRoute = computed(() => route.path === '/setup')
const wsColor = computed(() => (events.connected ? 'var(--qt-state-running)' : 'var(--qt-state-stopped)'))
const resChipText = computed(() => {
  const p = resources.pool
  if (!p) return '资源 —'
  return `WSL ${(p.pools.wsl.used_mb / 1024).toFixed(1)}/${(p.pools.wsl.total_mb / 1024).toFixed(1)}G`
})
const onlineByChannel = computed(() =>
  Object.fromEntries(CHANNELS.map((ch) => [ch, accounts.summary[ch].online])) as Record<string, number>)

/** 门禁「重试」与事件流「重新取令牌」是一件事:先让主进程重取,再把 WS 拉起来 */
async function retryRealtime(): Promise<void> {
  await session.retryToken()
  events.retry()
}

function go(path: string): void {
  void router.push(path)
}

function openAlert(a: { code: string; subject: string }): void {
  ui.alertDrawerOpen = false
  go(alertRoute(a))
}

/** hint_actions 落点(04 §4;`fix_firewall` 直接调 WinAgent 白名单 op) */
async function runHintAction(act: string): Promise<void> {
  if (act === 'open_env' || act === 'wsl_restart_when_convenient') go('/env')
  else if (act === 'open_mail') go('/mail')
  else if (act === 'open_account') go('/acct')
  else if (act === 'wechat_switch') go('/acct')
  else if (act === 'wechat_reinstall_bundled') go('/acct/new?ch=wechat&step=reinstall')
  else if (act === 'retry_key') go('/acct')
  else if (act === 'fix_firewall') await window.qt?.wa.invoke('firewall.ensure', {})
}

/** 全量拉:首启、以及 WS 重放截断时(§5.1) */
async function fullReload(): Promise<void> {
  await Promise.all([accounts.load(), resources.load(), mail.reloadAll().catch(() => undefined)])
  events.markSynced()
}

onMounted(async () => {
  session.install()
  await ui.loadFromConfig()
  ui.applyTheme()
  await setup.loadConfig()
  installGuards(() => setup.done, () => setup.reackRequired)
  if (!setup.done && route.path !== '/setup') void router.replace('/setup')
  // 告知页改版 ⇒ 已完成向导的机器重启后也要重新勾一次(05 §6.1 末句 / §8b.6 U1)。
  // 异步核对、不挡首屏;#86 拉不到时不判(见 store.refreshAck)。
  void setup.refreshAck().then(() => {
    if (setup.reackRequired && route.path !== '/setup') void router.replace('/setup')
  }).catch(() => undefined)

  accounts.bindEvents()
  resources.bindEvents()
  messages.bindEvents()
  commands.bindEvents()
  workflows.bindEvents()
  mail.bindEvents()
  env.bindEvents()
  jobs.bindEvents()
  events.onReplayTruncated(() => void fullReload())
  events.start()

  await fullReload()
  void env.loadAll()
  void commands.loadCatalog().catch(() => undefined)

  // P-DASH 每 30s 拉一次 /resources(事件即时刷之外的兜底)
  resourceTimer = setInterval(() => void resources.load(), 30000)
  staleTimer = setInterval(() => {
    staleSeconds.value = events.disconnectedAt ? Math.round((Date.now() - events.disconnectedAt) / 1000) : 0
  }, 1000)

  unsubRoute = window.qt?.router.onRoute((r) => go(r))

  // 系统通知:→login_required 且非向导中、→error、crit 告警(01 §2.8 / §7 notify 开关)
  events.on('account_state', (ev: QtEvent) => {
    const p = ev.payload as AccountStatePayload
    const id = ev.account_id ?? ''
    if (!ui.notifyEnabled || route.path.startsWith('/acct/new')) return
    if (p.state === 'login_required') {
      const zh = STATE_CODES[p.state_code]?.zh ?? '需要人工登录'
      void window.qt?.notify(`${id} 需要人工登录`, zh, `/screen/${id}`)
    } else if (p.state === 'error') {
      void window.qt?.notify(`${id} 进入故障`, p.state_reason || '账号异常', `/acct/${id}`)
    }
  })
  events.on('alert', (ev: QtEvent) => {
    const p = ev.payload as { severity?: string; title?: string; message?: string; code?: string; subject?: string }
    if (!ui.notifyEnabled || p.severity !== 'crit') return
    void window.qt?.notify(p.title ?? p.code ?? '严重告警', p.message ?? '', alertRoute({ code: p.code ?? '', subject: p.subject ?? '' }))
  })
})

watch(
  () => [onlineByChannel.value, events.unreadCount] as const,
  () => {
    void window.qt?.tray.update({ online: onlineByChannel.value, unreadAlerts: events.unreadCount })
  },
  { deep: true },
)

watch(() => ui.theme, () => ui.applyTheme())

onUnmounted(() => {
  events.stop()
  session.uninstall()
  mail.stopPolling()
  if (resourceTimer) clearInterval(resourceTimer)
  if (staleTimer) clearInterval(staleTimer)
  unsubRoute?.()
})
</script>

<template>
  <a-config-provider>
    <a-app>
      <div class="shell">
        <!-- 首次向导占满全屏,不套外框 -->
        <router-view v-if="isSetupRoute" />

        <template v-else>
          <aside class="nav" :style="{ width: ui.navCollapsed ? 'var(--qt-nav-w-collapsed)' : 'var(--qt-nav-w)' }">
            <div class="brand">{{ ui.navCollapsed ? 'QT' : 'QTrade 控制台' }}</div>
            <a
              v-for="it in NAV_ITEMS"
              :key="it.seg"
              class="nav-item"
              :class="{ active: route.path.startsWith(it.path) }"
              :data-testid="T.nav(it.seg)"
              @click="go(it.path)"
            >{{ ui.navCollapsed ? it.label.slice(0, 1) : it.label }}</a>
            <a class="nav-item collapse" :data-testid="T.navCollapse" @click="ui.toggleNav()">
              {{ ui.navCollapsed ? '»' : '« 折叠' }}
            </a>
          </aside>

          <div class="main">
            <header class="header">
              <span class="ws-dot" :data-testid="T.wsDot" :style="{ background: wsColor }"
                    :title="events.connected ? '实时连接正常' : '实时连接已断开'" />
              <a class="chip" :data-testid="T.resChip" @click="go('/dash')">{{ resChipText }}</a>
              <span class="qt-grow" />
              <a-badge :count="events.unreadCount" :offset="[-4, 4]">
                <a-button size="small" :data-testid="T.alertBell" @click="ui.alertDrawerOpen = true">告警</a-button>
              </a-badge>
              <a-switch
                class="theme"
                :data-testid="T.themeToggle"
                :checked="ui.theme === 'dark'"
                checked-children="深"
                un-checked-children="浅"
                :disabled="true"
                title="深色模式 M5 交付"
                @change="(v: any) => { ui.theme = v ? 'dark' : 'light'; ui.persist({ ui: { theme: ui.theme } }) }"
              />
            </header>

            <!-- WS 重连同步中 -->
            <div v-if="events.syncing" class="banner sync" :data-testid="T.syncBanner">
              同步中——正在全量拉取账号、资源与邮件状态
            </div>
            <!--
              事件流握手连败 / 4401:停止无限重连后,这里是唯一的出口提示。
              HTTP 侧可能还好着,所以只挂横幅、不遮整页。
            -->
            <div v-if="events.authLost" class="banner crit">
              实时事件流连不上,需要重新取令牌
              <span v-if="events.authLost.reason === 'handshake'">
                (连续 {{ events.authLost.failures }} 次握手就断,最后关闭码 {{ events.authLost.code }};
                已停止重连,避免无声空转)
              </span>
              <span v-else>
                (服务端回 4401:令牌无效 —— 已停止重连,重取令牌前再连也是同一个结果)
              </span>
              <a-button size="small" @click="retryRealtime">重新取令牌并重连</a-button>
            </div>
            <!--
              #82 drain:Agent 正在为升级排空,**指令类**写操作一律 503 draining
              (实测拦截面 = 指令/群发/账号动作;设置类与只读不受影响)。
              🔴 这是**预期状态不是故障**,后端没有逆操作端点 —— 恢复受理只能重启 Agent。
            -->
            <div v-if="session.draining" class="banner crit">
              Agent 正在为升级排空(#82 drain):已停止受理<strong>新指令</strong> ——
              发指令、群发、账号启停/登出都会被拒(结果码 NOT_READY,原因 draining)。
              只读与设置类页面照常;恢复受理需要重启 Agent(后端没有「取消排空」的端点)。
            </div>
            <!-- E-18/E-19:crit 水位告警常驻红横幅 -->
            <div
              v-if="events.watermarkAlert"
              class="banner crit"
              :data-testid="T.watermarkBanner"
              @click="go('/res')"
            >
              {{ events.watermarkAlert.title || events.watermarkAlert.code }} —— 点此查看资源监控
            </div>

            <main class="content">
              <router-view />
            </main>
          </div>
        </template>

        <!-- 门禁覆盖层:保留原路由,恢复后原地继续(§2.5 ②) -->
        <div v-if="session.gateVisible && !isSetupRoute" class="gate" :data-testid="T.gate">
          <div class="gate-box qt-card">
            <h2>{{ session.gateTitle }}</h2>
            <!-- 426:把「要什么版本、现在是什么版本、该怎么办」一次说清(02 §3.8) -->
            <p v-if="session.gateDetail" class="qt-warn">{{ session.gateDetail }}</p>
            <p v-if="session.agentDownReason" class="qt-muted">{{ session.agentDownReason }}</p>
            <!-- A-7:两处门禁共用的固定一句 -->
            <p class="qt-warn" :data-testid="T.gateLoginHint">{{ session.GATE_LOGIN_HINT }}</p>
            <div class="qt-row">
              <a-button type="primary" :data-testid="T.gateRetry" @click="retryRealtime">重试</a-button>
              <a-button v-if="!session.agentReachable" @click="session.startWsl()">让 WinAgent 拉起 Agent</a-button>
              <a-button @click="go('/env')">去环境页</a-button>
            </div>
            <p class="qt-small qt-muted">
              本页**不提供**手工粘贴令牌的入口——令牌一旦经剪贴板与渲染进程就失去了 DPAPI 保护。
            </p>
          </div>
        </div>

        <a-drawer v-model:open="ui.alertDrawerOpen" title="告警" width="480">
          <a-empty v-if="!events.firing.length" description="暂无未恢复告警" />
          <AlertCard
            v-for="(a, i) in events.firing"
            :key="a.code + a.subject"
            :alert="a"
            :testid="T.alertItem(i)"
            @open="openAlert(a)"
            @action="runHintAction"
          />
        </a-drawer>
      </div>
    </a-app>
  </a-config-provider>
</template>

<style scoped>
.shell { display: flex; height: 100%; position: relative; }
.nav {
  flex: 0 0 auto; background: var(--qt-bg-elevated); border-right: 1px solid var(--qt-border);
  display: flex; flex-direction: column; overflow: hidden; transition: width .15s;
}
.brand { padding: 0 var(--qt-space-4); height: var(--qt-header-h); display: flex; align-items: center; font-weight: 600; }
.nav-item {
  padding: 8px var(--qt-space-4); cursor: pointer; color: var(--qt-text); white-space: nowrap;
  border-left: 3px solid transparent;
}
.nav-item:hover { background: var(--qt-bg); }
.nav-item.active { border-left-color: var(--qt-primary); color: var(--qt-primary); background: var(--qt-bg); }
.collapse { margin-top: auto; color: var(--qt-text-secondary); }
.main { flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; }
.header {
  height: var(--qt-header-h); flex: 0 0 auto; display: flex; align-items: center; gap: var(--qt-space-3);
  padding: 0 var(--qt-space-4); background: var(--qt-bg-elevated); border-bottom: 1px solid var(--qt-border);
}
.ws-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.chip {
  border: 1px solid var(--qt-border); border-radius: 10px; padding: 1px 10px;
  font-size: var(--qt-font-xs); cursor: pointer; color: var(--qt-text-secondary);
}
.theme { margin-left: var(--qt-space-2); }
.banner { padding: 6px var(--qt-space-4); font-size: var(--qt-font-sm); }
.sync { background: #E6F4FF; color: var(--qt-state-starting); }
.crit { background: #FFF1F0; color: var(--qt-state-error); cursor: pointer; }
.content { flex: 1 1 auto; overflow: auto; }
.gate {
  position: absolute; inset: 0; background: rgba(0, 0, 0, .45);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.gate-box { padding: var(--qt-space-6); width: 520px; }
</style>
