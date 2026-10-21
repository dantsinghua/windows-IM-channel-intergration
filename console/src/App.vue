<script setup lang="ts">
/**
 * 全局外框(01 §2.7 统一外框 + §4 shell 段)。
 * 左侧 200px 导航(可折叠 56px)+ 顶栏(WS 连接灯、资源摘要芯片、告警铃、主题切换)+ 内容区。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { antTheme } from '@/theme/brands'
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
import QtIcon from '@/components/QtIcon.vue'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import { CHANNELS, STATE_CODES } from '@/i18n/zh-CN/codes'
import type { AccountStatePayload, QtEvent } from '@/api/types'

const NAV_ITEMS = [
  { seg: 'dash', path: '/dash', label: '首页' },
  { seg: 'res', path: '/res', label: '资源监控' },
  { seg: 'acct', path: '/acct', label: '账号' },
  { seg: 'screen', path: '/screen', label: '画面' },
  { seg: 'msg', path: '/msg', label: '消息' },
  { seg: 'mail', path: '/mail', label: '网络联通' },
  { seg: 'env', path: '/env', label: '环境' },
  { seg: 'set', path: '/set', label: '设置' },
  { seg: 'log', path: '/log', label: '日志 / 审计' },
] as const

const route = useRoute()
const primaryNav = ['dash', 'msg', 'log', 'res', 'env']
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
const themeConfig = computed(() => antTheme(ui.brand))
const setup = useSetupStore()

const staleSeconds = ref(0)
let staleTimer: ReturnType<typeof setInterval> | null = null
let resourceTimer: ReturnType<typeof setInterval> | null = null
let unsubRoute: (() => void) | undefined

const isSetupRoute = computed(() => route.path === '/setup')
/** 环境页是门禁的恢复入口,不被门禁覆盖(页内自有「拉起 Agent / 重启 WSL」等动作与状态提示) */
const isEnvRoute = computed(() => route.path === '/env')
const wsColor = computed(() => (events.connected ? 'var(--qt-state-running)' : 'var(--qt-state-stopped)'))
const resChipText = computed(() => {
  const p = resources.pool
  if (!p) return '资源 —'
  return `WSL ${(p.pools.wsl.used_mb / 1024).toFixed(1)}/${(p.pools.wsl.total_mb / 1024).toFixed(1)}G`
})
const onlineByChannel = computed(() =>
  Object.fromEntries(CHANNELS.map((ch) => [ch, accounts.summary[ch].online])) as Record<string, number>)

const realtimeRetrying = ref(false)

/** 认证失败和原门禁重试仍由主进程重取令牌。 */
async function retryRealtime(): Promise<void> {
  if (realtimeRetrying.value) return
  realtimeRetrying.value = true
  try {
    await session.retryToken()
    events.retry()
  } finally {
    realtimeRetrying.value = false
  }
}

/** 连接失败使用现有认证重连；没有认证失败证据时不要求重签令牌。 */
async function retryEventStream(): Promise<void> {
  if (events.authLost?.reason !== 'handshake' || session.authState !== 'ok') {
    await retryRealtime()
    return
  }
  if (realtimeRetrying.value) return
  realtimeRetrying.value = true
  try {
    events.retry()
    await session.pingAgent()
  } finally {
    realtimeRetrying.value = false
  }
}

// 原15秒健康探测确认服务恢复时，仅恢复因连接失败而停下的事件流。
watch(() => session.agentReachable && session.authState === 'ok', (ready, wasReady) => {
  if (ready && wasReady === false && !realtimeRetrying.value && !events.connected
      && events.authLost?.reason === 'handshake') events.retry()
})

/**
 * 门禁「去环境页」:环境页本身就是恢复入口(拉起 Agent / 重启 WSL / 看日志),到了那里门禁不能再盖着——
 * 否则按钮只换了路由、覆盖层纹丝不动(2026-10-10 安琳实测)。顺手重试一次令牌,多数情况直接恢复。
 */
async function goEnvFromGate(): Promise<void> {
  await router.push('/env')
  await retryRealtime()
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
  else if (act === 'calibrate') go('/res')                   // POOL_CALIBRATION_DRIFT:资源页「重新校准」(02 #78)
  else if (act === 'fix_firewall') await window.qt?.wa.invoke('firewall.ensure', {})
}

/** 全量拉:首启、以及 WS 重放截断时(§5.1) */
async function fullReload(): Promise<void> {
  await Promise.all([accounts.load(), resources.load(), resources.loadMetrics(), mail.reloadAll().catch(() => undefined)])
  events.markSynced()
}

onMounted(async () => {
  session.install()
  await ui.loadFromConfig()
  ui.applyTheme()
  await setup.loadConfig()
  installGuards(() => setup.done)
  if (!setup.done && route.path !== '/setup') void router.replace('/setup')
  // R6-84(2026-10-10):告知改版重勾随「阅读须知」步退役;告知只在偏好设置里按需查看,启动不再核对 ack。

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

  // 资源池与监控快照来自不同端点；首屏和事件流之外均需兜底刷新。
  resourceTimer = setInterval(() => {
    void resources.load()
    void resources.loadMetrics()
  }, 30000)
  staleTimer = setInterval(() => {
    staleSeconds.value = events.disconnectedAt ? Math.round((Date.now() - events.disconnectedAt) / 1000) : 0
  }, 1000)

  unsubRoute = window.qt?.router.onRoute((r) => go(r))

  // 系统通知:→login_required 且非向导中、→error、crit 告警(01 §2.8 / §7 notify 开关)
  events.on('account_state', (ev: QtEvent) => {
    const p = ev.payload as AccountStatePayload
    const id = ev.account_id ?? ''
    if (!ui.notifyEnabled || ev.replayed !== false || route.path.startsWith('/acct/new')) return
    if (p.state === 'login_required') {
      const zh = STATE_CODES[p.state_code]?.zh ?? '需要人工登录'
      void window.qt?.notify(`${id} 需要人工登录`, zh, `/screen/${id}`)
    } else if (p.state === 'error') {
      void window.qt?.notify(`${id} 进入故障`, p.state_reason || '账号异常', `/acct/${id}`)
    }
  })
  events.on('alert', (ev: QtEvent) => {
    const p = ev.payload as { severity?: string; state?: string; title?: string; message?: string; code?: string; subject?: string }
    if (!ui.notifyEnabled || ev.replayed !== false || p.state !== 'firing' || p.severity !== 'crit') return
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
  <a-config-provider :theme="themeConfig" :locale="zhCN">
    <a-app>
      <div class="shell" :class="{ 'nav-collapsed': ui.navCollapsed, 'setup-shell': isSetupRoute }">
        <!-- 首次向导占满全屏,不套外框 -->
        <router-view v-if="isSetupRoute" />

        <template v-else>
          <aside class="nav" :style="{ width: ui.navCollapsed ? 'var(--qt-nav-w-collapsed)' : 'var(--qt-nav-w)' }">
            <a class="brand" href="#/dash" aria-label="机器人采集终端首页"><span class="brand-mark">Q<span /></span><span v-if="!ui.navCollapsed" class="brand-name">机器人采集终端</span></a>
            <nav class="nav-menu" aria-label="机器人采集终端导航">
            <div v-if="!ui.navCollapsed" class="nav-caption">机器人采集终端</div>
            <a
              v-for="it in primaryNav.map(seg => NAV_ITEMS.find(x => x.seg === seg)!)"
              :key="it.seg"
              class="nav-item"
              :class="{ active: route.path.startsWith(it.path) }"
              :data-testid="T.nav(it.seg)"
              :href="`#${it.path}`"
              :aria-current="route.path.startsWith(it.path) ? 'page' : undefined"
              :title="it.label"
            ><QtIcon :name="it.seg" /><span v-if="!ui.navCollapsed">{{ it.seg === 'log' ? '日志与告警' : it.label }}</span></a>
            <div class="nav-divider" />
            <a class="nav-item" :class="{ active: route.path.startsWith('/acct') }" href="#/acct" :data-testid="T.nav('acct')" title="账号管理"><QtIcon name="acct" /><span v-if="!ui.navCollapsed">账号管理</span></a>
            <a class="nav-item" :class="{ active: route.path.startsWith('/mail') }" href="#/mail" :data-testid="T.nav('mail')" title="网络联通"><QtIcon name="link" /><span v-if="!ui.navCollapsed">网络联通</span></a>
            </nav>
            <div class="nav-footer">
              <a class="nav-item" href="#/set" :data-testid="T.nav('set')" title="偏好设置"><QtIcon name="set" /><span v-if="!ui.navCollapsed">偏好设置</span></a>
              <div v-if="!ui.navCollapsed" class="workspace-card"><QtIcon name="shield" /><div>机器人采集终端<small>账号 · 消息 · 环境</small></div></div>
              <button class="nav-item collapse" :data-testid="T.navCollapse" title="折叠导航" @click="ui.toggleNav()"><QtIcon name="menu" /><span v-if="!ui.navCollapsed">收起导航</span></button>
            </div>
          </aside>

          <div class="main">
            <header class="header">
              <span class="header-path">机器人采集终端 <span>/</span> <b>{{ NAV_ITEMS.find(x => route.path.startsWith(x.path))?.label ?? '账号详情' }}</b></span>
              <span class="qt-grow" />
              <span class="connection-label">{{ events.connected ? '实时连接' : '连接已断开' }}</span>
              <span class="ws-dot" :data-testid="T.wsDot" :style="{ background: wsColor }"
                    :title="events.connected ? '实时连接正常' : '实时连接已断开'" />
              <a class="chip" :data-testid="T.resChip" href="#/res">{{ resChipText }}</a>
              <a-badge :count="events.unreadCount" :offset="[-4, 4]">
                <button class="icon-button" aria-label="查看当前告警" :data-testid="T.alertBell" @click="ui.alertDrawerOpen = true"><QtIcon name="bell" /></button>
              </a-badge>
              <a-switch v-show="false"
                class="theme"
                :data-testid="T.themeToggle"
                :checked="ui.theme === 'dark'"
                checked-children="深"
                un-checked-children="浅"
                :disabled="true"
                title="深色模式 M5 交付"
                @change="(v: any) => { ui.theme = v ? 'dark' : 'light'; ui.persist({ ui: { theme: ui.theme } }) }"
              />
              <div class="brand-switch" role="group" aria-label="机构主题">
                <button type="button" :class="{ on: ui.brand === 'ceb' }" @click="ui.setBrand('ceb')">光大</button>
                <button type="button" :class="{ on: ui.brand === 'ny' }" @click="ui.setBrand('ny')">南银</button>
              </div>
              <div class="avatar" :title="ui.brand === 'ny' ? '南银理财' : '光大理财'">{{ ui.brand === 'ny' ? '南银' : '光大' }}</div>
            </header>

            <!-- WS 重连同步中 -->
            <div v-if="events.syncing" class="banner sync" :data-testid="T.syncBanner">
              同步中——正在全量拉取账号、资源与邮件状态
            </div>
            <!--
              事件流握手连败与4401分别提示；连接失败可在确认服务恢复后重连一次。
              HTTP 侧可能还好着,所以只挂横幅、不遮整页。
            -->
            <div v-if="events.authLost && !events.connected" class="banner crit">
              <template v-if="events.authLost.reason === 'handshake'">
                <span>{{ session.agentReachable ? '实时事件流连接失败' : '采集服务暂时不可达，实时事件流无法连接' }}
                  （连续 {{ events.authLost.failures }} 次握手失败，关闭码 {{ events.authLost.code }}）。已暂停重试。</span>
                <span v-if="!session.agentReachable">采集服务恢复后会自动重连，也可手动重试。</span>
              </template>
              <span v-else>实时事件流认证失败（关闭码 4401），需要重新获取访问令牌。</span>
              <a-button size="small" :loading="realtimeRetrying" @click="retryEventStream">{{ events.authLost.reason === 'handshake' ? '重试连接' : '重新取令牌并重连' }}</a-button>
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
        <div v-if="session.gateVisible && !isSetupRoute && !isEnvRoute" class="gate" :data-testid="T.gate">
          <div class="gate-box qt-card">
            <h2>{{ session.gateTitle }}</h2>
            <!-- 426:把「要什么版本、现在是什么版本、该怎么办」一次说清(02 §3.8) -->
            <p v-if="session.gateDetail" class="qt-warn">{{ session.gateDetail }}</p>
            <p v-if="session.agentDownReason" class="qt-muted">{{ session.agentDownReason }}</p>
            <p data-testid="qt-local-service-status">WinAgent 服务：{{ session.serviceStateText }} · 会话代理：{{ session.sessionStateText }} · 采集 API：{{ session.apiStateText }}</p>
            <p class="qt-warn" :data-testid="T.gateLoginHint">{{ session.GATE_LOGIN_HINT }}</p>
            <p v-if="session.localServiceMessage" role="status">{{ session.localServiceMessage }}</p>
            <div class="qt-row">
              <a-button type="primary" :data-testid="T.gateRetry" @click="retryRealtime">重试</a-button>
              <a-button v-if="session.canStartLocalService" data-testid="qt-start-winagent" :loading="session.localServiceBusy" :disabled="session.localServiceBusy" @click="session.startLocalService()">启动 WinAgent 服务及会话代理</a-button>
              <a-button v-if="!session.agentReachable" :disabled="!session.winagentOnline || session.userAgentOnline !== true" @click="session.startWsl()">启动受管 WSL 环境</a-button>
              <a-button @click="goEnvFromGate">去环境页</a-button>
            </div>
            <p v-if="!session.canStartLocalService" class="qt-muted">当前窗口未提供本机服务启动能力，请使用更新后的 Electron 桌面端。</p>
            <p class="qt-small qt-muted">启动 WSL 环境与启动 WinAgent 服务是独立操作；采集 API 状态以实际连接结果为准。</p>
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
.shell { display: flex; height: 100vh; height: 100dvh; min-height: 0; overflow: hidden; position: relative; background: var(--qt-shell-bg); }
.shell.setup-shell { display: block; overflow-y: auto; }
.nav {
  flex: 0 0 auto; background: var(--qt-nav-bg); border-right: 1px solid var(--qt-nav-border);
  display: flex; flex-direction: column; min-height: 0; overflow: hidden; transition: width .15s; padding: 0 16px; backdrop-filter: blur(28px);
}
.brand { height: 112px; flex: 0 0 auto; display: flex; gap: 12px; align-items: center; padding: 0 6px; color: #30213f; }
.brand-mark { width: 37px; height: 40px; font-size: 40px; font-weight: 800; line-height: 1; color: var(--qt-primary); position: relative; letter-spacing: -4px; }
.brand-mark span { position: absolute; right: -2px; bottom: 0; width: 12px; height: 7px; border-radius: 3px; transform: rotate(45deg); background: var(--qt-accent); }
.brand-name { font-size: 23px; font-weight: 680; letter-spacing: -.7px; line-height: 1.1; }
.brand-name small { display: block; font-size:12px; font-weight: 400; letter-spacing: 1.5px; color: var(--qt-text-secondary); margin-top: 8px; }
.nav-menu { flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; }
.nav-caption { padding: 12px 16px; font-size: 12px; color: #9a919f; }
.nav-item {
  padding: 13px 16px; margin: 3px 0; display: flex; align-items: center; gap: 13px; cursor: pointer; color: var(--qt-nav-text); white-space: nowrap;
  border: 0; border-radius: 13px; background: transparent; text-align: left; font-size: 14px; min-height: 45px; flex-shrink: 0;
}
.nav-item:hover { background: var(--qt-nav-hover); color: var(--qt-primary); }
.nav-item.active { color: white; background: var(--qt-nav-active); box-shadow: var(--qt-nav-active-shadow); }
.nav-divider { height: 1px; background: var(--qt-border); margin: 24px 12px 18px; flex-shrink: 0; }
.tools-list .nav-item { font-size: 13px; min-height: 40px; padding-top: 9px; padding-bottom: 9px; }
.tools-toggle { width: 100%; }
.nav-footer { flex: 0 0 auto; padding-top: 16px; padding-bottom: max(12px, env(safe-area-inset-bottom)); }
.workspace-card { display: flex; align-items: center; gap: 10px; padding: 15px 10px; background: var(--qt-workspace-bg); border: 1px solid #ffffff; border-radius: 16px; font-size: 12px; color: var(--qt-text-secondary); margin-bottom: 12px; }
.workspace-card small { display: block; font-size:12px; margin-top: 5px; color: #8a7d94; }
.nav-collapsed .nav { padding: 0 8px; }
.nav-collapsed .brand { justify-content: center; }
.nav-collapsed .nav-item { justify-content: center; padding-left: 10px; padding-right: 10px; }
.collapse { width: 100%; color: var(--qt-text-secondary); }
.main { flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }
.header {
  height: var(--qt-header-h); flex: 0 0 auto; display: flex; align-items: center; gap: var(--qt-space-3);
  padding: 0 36px; border-bottom: 1px solid #eae5ee88;
}
.header-path { color: var(--qt-text-secondary); font-size: 13px; }.header-path span { margin: 0 15px; color: #bab1c4; }.header-path b { color: var(--qt-text); font-weight: 500; }
.connection-label { font-size: 12px; color: #84798e; }
.icon-button { display: grid; place-items: center; width: 40px; height: 40px; border: 1px solid #e8e3ed; border-radius: 50%; background: #ffffff80; cursor: pointer; color: #776586; }
.brand-switch { display: flex; padding: 3px; border-radius: 999px; background: var(--qt-primary-soft); border: 1px solid var(--qt-border); }
.brand-switch button { border: 0; background: transparent; border-radius: 999px; padding: 4px 10px; cursor: pointer; color: var(--qt-text-secondary); font-size: 12px; }
.brand-switch button.on { background: var(--qt-primary); color: white; }
.avatar { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 50%; background: var(--qt-avatar-bg); color: var(--qt-primary); font-size: 12px; font-weight: 650; margin-left: 8px; }
.ws-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.chip {
  border: 1px solid var(--qt-border); border-radius: 10px; padding: 1px 10px;
  font-size: var(--qt-font-xs); cursor: pointer; color: var(--qt-text-secondary);
}
.theme { margin-left: var(--qt-space-2); }
.banner { padding: 6px var(--qt-space-4); font-size: var(--qt-font-sm); }
.sync { background: #E6F4FF; color: var(--qt-state-starting); }
.crit { background: #FFF1F0; color: var(--qt-state-error); cursor: pointer; }
.content { flex: 1 1 auto; min-height: 0; overflow: auto; overscroll-behavior: contain; }
.gate {
  position: absolute; inset: 0; background: rgba(0, 0, 0, .45);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
}
.gate-box { padding: var(--qt-space-6); width: 520px; }
@media (max-width: 1100px) { .nav { width: 72px !important; padding: 0 8px; } .brand-name,.nav-caption,.nav-item span,.workspace-card,.tools-toggle span { display: none; } .brand,.nav-item { justify-content: center; } .header { padding: 0 22px; height: 72px; } }
@media (max-width: 640px) { .nav { width: 58px !important; padding: 0 5px; } .brand { height: 80px; } .brand-mark { font-size: 32px; width: 30px; } .nav-item { padding: 12px 8px; } .header { height: 62px; gap: 8px; padding: 0 16px; }.header-path,.chip,.connection-label { display: none; } .nav-divider { margin: 16px 7px; } .gate-box { width: calc(100% - 24px); } }
@media (max-height: 640px) { .brand { height: 76px; } .nav-footer { padding-top: 8px; } .workspace-card { padding: 10px; margin-bottom: 4px; } }
</style>
