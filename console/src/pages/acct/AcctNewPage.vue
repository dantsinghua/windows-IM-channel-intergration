<script setup lang="ts">
/**
 * `P-ACCT-NEW` 新增账号向导(01 §2.7.3.1~§2.7.3.3)。
 * 企点:资源预检→起名→机型档案→账号密码→创建与登录→完成
 * QQ  :资源预检→起名→扫码→完成
 * 微信:检查模块与槽位→版本匹配→(重装引导)→发起→讲述人仪式→首次登录→取钥三段→完成
 *
 * 🔴 密码提交即清表单;未保存密码仅在本次首次流程的局部变量中有界暂留(R6-78)。
 * 🔴 取钥是实测三段固定顺序(R-05):起 hook → 先开图片取 img_key → 再退出重登取 data_key。
 */
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { acctNew as T } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'
import { accountsApi, commandsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import PromptCard from '@/components/PromptCard.vue'
import { ACCOUNT_STATES, CHANNELS, CHANNEL_TEXT, STATE_CODES, WECHAT_ACTION, WECHAT_MATCH, type Channel } from '@/i18n/zh-CN/codes'
import type { Account, DeviceProfileTemplate } from '@/api/types'

const route = useRoute()
const router = useRouter()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const session = useSessionStore()
const settings = useSettingsStore()
const AccountScreen = defineAsyncComponent(() => import('@/pages/screen/ScreenPage.vue'))
const screenOpen = ref(false)
const wxEnableError = ref('')

const channel = ref<Channel | null>((route.query.ch as Channel) ?? null)
const step = ref(0)
const busy = ref(false)
const createdId = ref<string | null>(null)
const rejectInfo = ref<{ message: string; alternatives: unknown[] } | null>(null)
const stalled = ref(false)
let stallTimer: ReturnType<typeof setTimeout> | null = null
const startError = ref<string | null>(null)
const passwordError = ref<string | null>(null)
const passwordBusy = ref(false)
const restoreError = ref<string | null>(null)
const cancelError = ref<string | null>(null)
const cancelling = ref(false)
const FLOW_STORAGE_KEY = 'qtrade.acct-new.pending'
let flowActive = true
let pageMounted = true
let createPending = false
let cancelRequested = false
let startAccepted = false
// 仅本页首次创建使用,不进入响应式状态、路由或持久存储。
let firstSecret = ''
let secretDeadline = 0
let secretVersion = 0
let passwordAttempts = 0
let secretTimer: ReturnType<typeof setTimeout> | null = null
let passwordRetryTimer: ReturnType<typeof setTimeout> | null = null

/* 企点表单 */
const label = ref('')
const profileMode = ref<'random' | 'pick'>('random')
const profileKey = ref<string | undefined>()
const profiles = ref<DeviceProfileTemplate[]>([])
const acctName = ref('')
const secret = ref('')
const showSecret = ref(false)
const remember = ref(true)
const passwordModal = ref(false)
const modalSecret = ref('')

/* 微信流程 */
const wxStatus = ref<Record<string, any> | null>(null)
const wxPickIndex = ref(0)
const wxReinstallCk = ref({ backup: false, autoupdate: false, confirm: false })
const wxPreviewSrc = ref('')
let wxPreviewTimer: ReturnType<typeof setInterval> | null = null

const STEP_NAMES: Record<Channel, string[]> = {
  qidian: ['资源预检', '起名', '机型档案', '账号密码', '创建与登录', '完成'],
  qq: ['资源预检', '起名', '扫码', '完成'],
  wechat: ['检查模块与槽位', '版本匹配', '发起', '讲述人仪式', '首次登录', '取钥', '完成'],
}

const steps = computed(() => (channel.value ? STEP_NAMES[channel.value] : []))
const account = computed<Account | null>(() => (createdId.value ? accounts.byId[createdId.value] ?? null : null))
const prompt = computed(() => (createdId.value ? accounts.prompts[createdId.value] ?? null : null))
const stateCode = computed(() => account.value?.state_code ?? '')
const loginSessionId = computed(() => (createdId.value ? accounts.loginSessions[createdId.value] ?? null : null))
const canAddHere = computed(() => (channel.value ? resources.canAdd[channel.value] ?? 0 : 0))
const labelDup = computed(() => accounts.items.some((a) => a.label === label.value.trim() && a.id !== createdId.value))
const labelInvalid = computed(() => !label.value.trim() || label.value.trim().length > 20 || labelDup.value)
const formLastStep = computed(() => channel.value === 'qidian' ? 3 : channel.value === 'qq' ? 1 : 2)
const canNavigateForm = computed(() => !busy.value && !restoreError.value && !createdId.value && Number.isInteger(step.value) && step.value <= formLastStep.value)
const nextDisabled = computed(() => busy.value
  || (step.value === 0 && channel.value !== 'wechat' && canAddHere.value === 0)
  || (step.value === 1 && channel.value !== 'wechat' && labelInvalid.value)
  || (channel.value === 'wechat' && step.value === 0 && (!wxStatus.value?.module_enabled || resources.hasPending)))

const wxMatch = computed(() => (wxStatus.value?.match as string) ?? '')
const wxAction = computed(() => (wxStatus.value?.action as string) ?? '')
const wxInstalls = computed(() => (wxStatus.value?.installed as { path: string; version: string }[]) ?? [])
const holderBusy = computed(() => (resources.slots?.holder ?? '') !== '')

function clearPendingFlow(): void {
  try { window.sessionStorage.removeItem(FLOW_STORAGE_KEY) } catch { /* 存储被禁用时仍可继续当前流程 */ }
}

function savePendingFlow(id: string, ch: Channel): void {
  if (ch === 'wechat') return
  try {
    // 仅持久化已建档身份,不复制表单、账号名、密码或登录提示。
    window.sessionStorage.setItem(FLOW_STORAGE_KEY, JSON.stringify({ createdId: id, channel: ch }))
  } catch {
    message.warning('无法保存向导进度,刷新后请到账号列表继续。')
  }
}

async function restorePendingFlow(): Promise<void> {
  let saved: { createdId: string; channel: 'qidian' | 'qq' }
  try {
    const raw = window.sessionStorage.getItem(FLOW_STORAGE_KEY)
    if (!raw) return
    const value = JSON.parse(raw)
    if (!value || Object.keys(value).length !== 2 || typeof value.createdId !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(value.createdId)
      || !['qidian', 'qq'].includes(value.channel)) {
      clearPendingFlow()
      return
    }
    saved = { createdId: value.createdId, channel: value.channel }
  } catch {
    clearPendingFlow()
    return
  }
  if (!flowActive || (channel.value && channel.value !== saved.channel)) return
  busy.value = true
  restoreError.value = null
  channel.value = saved.channel
  clearFirstSecret()
  startAccepted = false
  const before = accounts.byId[saved.createdId]
  try {
    const a = await accountsApi.get(saved.createdId)
    if (!flowActive) return
    if (a.id !== saved.createdId || a.channel !== saved.channel || a.deleted_ms != null) {
      clearPendingFlow()
      return
    }
    const latest = accounts.byId[a.id]
    accounts.upsert(latest && latest !== before ? { ...a, ...latest, label: a.label } : a)
    createdId.value = a.id
    label.value = a.label
    step.value = saved.channel === 'qq' ? 2 : 4
    armStallTimer()
    // 恢复只读账号与提示;不得自动调用 #9 或 #12。
    const previousPrompt = accounts.prompts[a.id]
    try {
      const p = await accountsApi.prompt(a.id, accounts.loginSessions[a.id] ?? undefined)
      if (!flowActive || accounts.prompts[a.id] !== previousPrompt) return
      if (p.kind) accounts.prompts[a.id] = p
      else delete accounts.prompts[a.id]
    } catch (e) {
      if (flowActive) message.error(e instanceof Error ? e.message : String(e))
    }
  } catch (e) {
    if (!flowActive) return
    if (e instanceof ApiFailure && e.status === 404) clearPendingFlow()
    else restoreError.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (flowActive) busy.value = false
  }
}

function clearFirstSecret(): void {
  firstSecret = ''
  secretDeadline = 0
  secretVersion++
  passwordBusy.value = false
  if (secretTimer) clearTimeout(secretTimer)
  if (passwordRetryTimer) clearTimeout(passwordRetryTimer)
  secretTimer = null
  passwordRetryTimer = null
}

function leaveFlow(): void {
  flowActive = false
  clearFirstSecret()
  secret.value = ''
  modalSecret.value = ''
  passwordModal.value = false
  if (stallTimer) clearTimeout(stallTimer)
}

function go(p: string): void {
  if (p !== `/screen/${createdId.value}`) clearPendingFlow()
  leaveFlow()
  void router.push(p)
}

async function finishCancellation(): Promise<void> {
  const a = account.value
  try {
    if (createdId.value) {
      if (!a || a.id !== createdId.value || a.channel !== channel.value || a.channel === 'wechat') {
        throw new Error('无法确认本次创建的账号,已保留进度,请刷新后重试取消。')
      }
      // 已完成的在线账号不属于本次取消清理范围。
      if (a.state !== 'running' && a.deleted_ms == null) await accountsApi.softDelete(a.id, a.label)
    }
    clearPendingFlow()
    if (pageMounted) go('/acct')
  } catch (e) {
    cancelError.value = e instanceof Error ? e.message : String(e)
    message.error(cancelError.value)
  } finally {
    cancelling.value = false
    busy.value = false
  }
}

async function cancelWizard(): Promise<void> {
  if (cancelling.value || (!flowActive && !cancelError.value)) return
  if (channel.value === 'wechat') { go('/acct'); return }
  cancelRequested = true
  cancelling.value = true
  cancelError.value = null
  // 先阻断迟到响应的启动/密码续跑;软删失败时保留身份供重试或刷新恢复。
  leaveFlow()
  if (!createdId.value && createPending) return
  await finishCancellation()
}

function nextFormStep(): void {
  if (canNavigateForm.value && step.value < formLastStep.value && !nextDisabled.value) step.value++
}

function pickChannel(ch: Channel): void {
  channel.value = ch
  step.value = 0
  void preload()
}

async function preload(): Promise<void> {
  await resources.load()
  if (channel.value === 'qidian') {
    try { profiles.value = (await commandsApi.deviceProfiles()).items } catch { profiles.value = [] }
  }
  if (channel.value === 'wechat') await loadWxStatus()
}

async function loadWxStatus(): Promise<void> {
  try {
    wxStatus.value = (await window.qt?.wa.invoke('wechat.status', {})) as Record<string, unknown>
  } catch {
    wxStatus.value = null
  }
}

async function enableWechatModule(): Promise<void> {
  if (busy.value || wxStatus.value?.module_enabled) return
  wxEnableError.value = ''
  if (!window.qt?.wa || !session.winagentOnline) {
    wxEnableError.value = '请在已连接 WinAgent 的桌面控制台启用微信模块。'
    return
  }
  busy.value = true
  try {
    await settings.saveWechatModule({ enabled: true })
    await loadWxStatus()
    if (!wxStatus.value?.module_enabled) throw new Error('尚未确认微信模块已启用，请检查服务连接后重试。')
    message.success('微信模块已启用')
  } catch (e) {
    wxEnableError.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

/* ── 企点 / QQ 创建 ── */

async function create(): Promise<void> {
  if (!flowActive || busy.value || restoreError.value || createdId.value || !channel.value || channel.value === 'wechat' || labelInvalid.value) return
  busy.value = true
  rejectInfo.value = null
  startError.value = null
  passwordError.value = null
  startAccepted = false
  clearFirstSecret()
  passwordAttempts = 0
  const savePassword = remember.value
  const body: Parameters<typeof accountsApi.create>[0] = {
    channel: channel.value,
    label: label.value.trim(),
    profile_key: channel.value === 'qidian' && profileMode.value === 'pick' ? profileKey.value : undefined,
    login: channel.value === 'qq'
      ? { mode: 'qrcode' }
      : { mode: 'password', account: acctName.value, remember: savePassword,
        ...(savePassword ? { secret: secret.value } : {}) },
  }
  if (channel.value === 'qidian' && !savePassword && secret.value) {
    firstSecret = secret.value
    secretDeadline = Date.now() + 5 * 60 * 1000
    secretTimer = setTimeout(() => {
      clearFirstSecret()
      passwordError.value = '本次密码等待已超时,请重新输入密码登录。'
    }, 5 * 60 * 1000)
  }
  // 提交时就清表单,不等网络响应。
  secret.value = ''
  createPending = true
  try {
    const a = await accountsApi.create(body)
    createPending = false
    if (!flowActive && !cancelRequested) return
    createdId.value = a.id
    savePendingFlow(a.id, a.channel)
    // 创建响应到达前可能已收到该账号的状态事件,不能用旧的 created 覆盖它。
    accounts.upsert({ ...a, ...accounts.byId[a.id], label: a.label })
    step.value = channel.value === 'qq' ? 2 : 4
    if (cancelRequested) {
      await finishCancellation()
      return
    }
    armStallTimer()
    await startCreatedAccount()
  } catch (e) {
    clearFirstSecret()
    if (cancelRequested && !createdId.value) {
      clearPendingFlow()
      if (pageMounted) go('/acct')
      return
    }
    if (!flowActive) return
    if (e instanceof ApiFailure && e.code === 'RESOURCE_EXHAUSTED') {
      rejectInfo.value = { message: e.detail.message, alternatives: e.detail.alternatives ?? [] }
      step.value = 0
    } else {
      message.error(e instanceof Error ? e.message : String(e))
    }
  } finally {
    createPending = false
    if (cancelRequested && !createdId.value) cancelling.value = false
    if (flowActive) busy.value = false
  }
}

async function startCreatedAccount(): Promise<void> {
  if (!flowActive || !createdId.value) return
  startAccepted = false
  startError.value = null
  try {
    await accountsApi.start(createdId.value)
    if (!flowActive) return
    startAccepted = true
    void submitFirstSecret()
  } catch (e) {
    clearFirstSecret()
    if (!flowActive) return
    startError.value = e instanceof Error ? e.message : String(e)
    message.error(startError.value)
  }
}

async function submitFirstSecret(): Promise<void> {
  if (!flowActive || !startAccepted || !createdId.value || !firstSecret || passwordBusy.value || passwordRetryTimer
    || account.value?.state !== 'login_required' || stateCode.value !== 'WAIT_PASSWORD') return
  if (Date.now() >= secretDeadline) { clearFirstSecret(); return }
  const version = secretVersion
  passwordBusy.value = true
  passwordAttempts++
  try {
    await accountsApi.login(createdId.value, { secret: firstSecret, remember: false })
    if (!flowActive || version !== secretVersion) return
    // #12 受理即清除,后续掉线不能复用。
    clearFirstSecret()
    passwordError.value = null
  } catch (e) {
    if (!flowActive || version !== secretVersion) return
    if (e instanceof ApiFailure && e.status === 409 && e.code === 'NOT_APPLICABLE' && e.reason === 'busy'
      && passwordAttempts < 3 && Date.now() + 300 < secretDeadline) {
      passwordRetryTimer = setTimeout(() => {
        passwordRetryTimer = null
        void submitFirstSecret()
      }, 300)
    } else {
      clearFirstSecret()
      passwordError.value = e instanceof Error ? e.message : String(e)
      message.error(passwordError.value)
    }
  } finally {
    if (flowActive && version === secretVersion) passwordBusy.value = false
  }
}

function armStallTimer(): void {
  if (stallTimer) clearTimeout(stallTimer)
  stalled.value = false
  // 超过 10 分钟无状态变化 → 提示「进度停滞,可去环境页看日志」
  stallTimer = setTimeout(() => { stalled.value = true }, 10 * 60 * 1000)
}

watch(() => [account.value?.state, stateCode.value] as const, ([s, c]) => {
  if (!flowActive || !s) return
  armStallTimer()
  if (['running', 'degraded', 'error', 'stopping', 'stopped', 'disabled'].includes(s)
    || ['fail', 'offline'].includes(STATE_CODES[c]?.group ?? '')) clearFirstSecret()
  if (s === 'running') {
    clearPendingFlow()
    startError.value = null
    passwordError.value = null
    if (channel.value === 'qidian') step.value = 5
    else if (channel.value === 'qq') step.value = 3
    else step.value = 6
  } else if (channel.value === 'qidian' || channel.value === 'qq') {
    step.value = channel.value === 'qq' ? 2 : 4
    void submitFirstSecret()
  }
  if (channel.value === 'wechat') {
    if (c === 'WAIT_NARRATOR') step.value = 3
    else if (c === 'WAIT_QRCODE') step.value = 4
    else if (c === 'WAIT_KEY_IMG' || c === 'WAIT_KEY_RELOGIN' || c === 'KEY_FAIL') step.value = 5
  }
})

function openPasswordModal(): void {
  clearFirstSecret()
  passwordModal.value = true
}

function closePasswordModal(): void {
  clearFirstSecret()
  modalSecret.value = ''
  passwordModal.value = false
}

async function submitPassword(): Promise<void> {
  if (!flowActive || !createdId.value || busy.value || passwordBusy.value || !modalSecret.value) return
  clearFirstSecret()
  const version = secretVersion
  passwordBusy.value = true
  const pending = accountsApi.login(createdId.value, { secret: modalSecret.value, remember: false })
  modalSecret.value = ''
  try {
    await pending
    if (!flowActive || version !== secretVersion) return
    passwordError.value = null
    passwordModal.value = false
  } catch (e) {
    if (!flowActive || version !== secretVersion) return
    passwordError.value = e instanceof Error ? e.message : String(e)
    message.error(passwordError.value)
  } finally {
    if (flowActive && version === secretVersion) passwordBusy.value = false
  }
}

async function refreshQr(): Promise<void> {
  if (createdId.value) await accounts.refreshPrompt(createdId.value)
}

function openWebui(): void {
  const port = 16300 + ((account.value?.runtime?.ws_port ?? 16100) - 16100)
  void window.qt?.app.openExternal(`http://127.0.0.1:${port}/`)
}

async function retryStart(): Promise<void> {
  if (!flowActive || !createdId.value || busy.value) return
  clearFirstSecret()
  busy.value = true
  try { await startCreatedAccount() }
  finally { if (flowActive) busy.value = false }
}

async function deleteAndBack(): Promise<void> {
  await cancelWizard()
}

/* ── 微信流程 ── */

async function reinstall(): Promise<void> {
  busy.value = true
  try {
    await window.qt?.wa.invoke('wechat.reinstall', {
      install_path: wxInstalls.value[wxPickIndex.value]?.path,
      confirmed: true,
    })
    await loadWxStatus()
    step.value = 1
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function startWechatFlow(): Promise<void> {
  busy.value = true
  try {
    const target = route.query.wxnn as string | undefined
    const r = target ? await accountsApi.switchTo(target) : await accountsApi.switchNew()
    createdId.value = target ?? r.target
    await accounts.load()
    step.value = 3
    armStallTimer()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    busy.value = false
  }
}

async function keyRetry(): Promise<void> {
  await window.qt?.wa.invoke('wechat.key.retry', {})
  message.info('已请求重新取钥:将重走 起 hook → 打开图片 → 退出重登')
}

/** 向导内「取消」也走 Agent 级取消(R4-4),不打 WinAgent 17610 */
async function cancelWechatLogin(): Promise<void> {
  if (!createdId.value || !loginSessionId.value) return
  try {
    const r = await accountsApi.loginCancel(createdId.value, loginSessionId.value)
    if (r.stale) message.info('该次登录尝试已结束')
    else message.success('已取消本次登录')
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    await resources.load()
    go('/acct')
  }
}

async function pollWechatPreview(): Promise<void> {
  if (!createdId.value) return
  try {
    const v = (await window.qt?.wa.invoke('wechat.ui-visible', {})) as { visible?: boolean } | undefined
    if (v && v.visible === false) { wxPreviewSrc.value = ''; return }
    // #33 是二进制 + 响应头(媒体 id / sha256 在头里);这里只做预览,拿 blob 即可
    const shot = await accountsApi.screenshot(createdId.value)
    if (!shot.blob) { wxPreviewSrc.value = ''; return }
    if (wxPreviewSrc.value) URL.revokeObjectURL(wxPreviewSrc.value)
    wxPreviewSrc.value = URL.createObjectURL(shot.blob)
  } catch {
    wxPreviewSrc.value = ''
  }
}

watch(step, (s) => {
  if (channel.value === 'wechat' && s === 4) {
    if (!wxPreviewTimer) wxPreviewTimer = setInterval(() => void pollWechatPreview(), 2000)
  } else if (wxPreviewTimer) {
    clearInterval(wxPreviewTimer)
    wxPreviewTimer = null
  }
})

watch(createdId, () => { screenOpen.value = false })

onMounted(async () => {
  await restorePendingFlow()
  if (flowActive && channel.value) void preload()
})
onUnmounted(() => {
  pageMounted = false
  leaveFlow()
  if (wxPreviewTimer) clearInterval(wxPreviewTimer)
  if (wxPreviewSrc.value) URL.revokeObjectURL(wxPreviewSrc.value)
})
</script>

<template>
  <div class="qt-page qt-stack account-wizard">
    <header class="qt-page-heading"><div><div class="qt-eyebrow">CONNECT AN ACCOUNT</div><h1>{{ channel ? `连接${CHANNEL_TEXT[channel]}账号` : '把新的账号，带入工作台' }}</h1><p>按步骤完成准备与登录，进度会随真实账号状态更新。</p></div><span class="wizard-security">凭据受控 · 账号独立</span></header>
    <section v-if="cancelError" class="qt-card box">
      <p class="qt-danger">取消失败:{{ cancelError }}</p>
      <p>账号 {{ createdId }} 的进度已保留,可重试取消或刷新恢复。</p>
    </section>
    <section v-if="restoreError" class="qt-card box">
      <p class="qt-danger">无法恢复创建进度:{{ restoreError }}</p>
      <a-button :loading="busy" :data-testid="T.retry" @click="restorePendingFlow">重新读取账号进度</a-button>
    </section>
    <!-- 入口:选分支 -->
    <section v-if="!channel" class="qt-card box">
      <div class="qt-section-title">选择要连接的通道</div>
      <div class="chcards">
        <div
          v-for="ch in CHANNELS"
          :key="ch"
          class="qt-glass chcard"
          role="button"
          tabindex="0"
          :data-testid="T.channel(ch)"
          @click="pickChannel(ch)"
          @keydown.enter="pickChannel(ch)"
          @keydown.space.prevent="pickChannel(ch)"
        >
          <span class="channel-icon">{{ ch === 'qidian' ? '企' : ch === 'wechat' ? '微' : 'Q' }}</span>
          <div class="qt-section-title">{{ CHANNEL_TEXT[ch] }}</div>
          <p class="qt-small qt-muted">
            {{ ch === 'qidian' ? '账密登录(短信/滑块在画面里过)' : ch === 'qq' ? '扫码登录(qq_data 免扫)' : '微信 PC 扫码 + 三段取钥' }}
          </p>
          <span class="channel-enter">开始连接 ↗</span>
        </div>
      </div>
    </section>

    <template v-else>
      <a-steps class="wizard-steps qt-glass" :current="step" size="small" :data-testid="T.steps">
        <a-step v-for="s in steps" :key="s" :title="s" />
      </a-steps>

      <!-- ① 资源预检(企点/QQ) -->
      <section v-if="channel !== 'wechat' && step === 0" class="qt-card box" :data-testid="T.resCard">
        <div class="qt-section-title">资源预检</div>
        <p>
          WSL 池剩余 {{ ((resources.pool?.pools.wsl.free_mb ?? 0) / 1024).toFixed(1) }} GB ·
          单账号预算 {{ ((resources.pool?.quota_mb?.[channel] ?? 0) / 1024).toFixed(1) }} GB ·
          还能开 <b>{{ canAddHere }}</b> 个
        </p>
        <div v-if="canAddHere === 0 || rejectInfo" class="reject" :data-testid="T.resReject">
          <p class="qt-danger">{{ rejectInfo?.message ?? '当前资源不足,无法新增' }}</p>
          <ul v-if="rejectInfo?.alternatives?.length">
            <li v-for="(alt, i) in rejectInfo.alternatives" :key="i">{{ String(alt) }}</li>
          </ul>
          <div class="qt-row">
            <a-button :data-testid="T.resAltQq" @click="pickChannel('qq')">改为新增 QQ</a-button>
            <a-button :data-testid="T.resAltGoacct" @click="go('/acct')">去账号页停用</a-button>
            <a-button :loading="cancelling" :data-testid="T.cancel" @click="cancelWizard">取消</a-button>
          </div>
        </div>
      </section>

      <!-- ② 起名 -->
      <section v-if="channel !== 'wechat' && step === 1" class="qt-card box">
        <div class="qt-section-title">起名</div>
        <a-input
          v-model:value="label"
          :data-testid="T.label"
          placeholder="例:张三-固收(≤ 20 字,需唯一)"
          :status="labelInvalid && label ? 'error' : undefined"
          :maxlength="20"
        />
        <p v-if="labelDup" class="qt-danger qt-small">该标签已被占用</p>
      </section>

      <!-- ③ 机型档案(企点) -->
      <section v-if="channel === 'qidian' && step === 2" class="qt-card box">
        <div class="qt-section-title">机型档案</div>
        <a-radio-group v-model:value="profileMode">
          <a-radio value="random" :data-testid="T.profileRandom">随机分配(推荐)</a-radio>
          <a-radio value="pick">从档案库选</a-radio>
        </a-radio-group>
        <a-select
          v-if="profileMode === 'pick'"
          v-model:value="profileKey"
          class="pick"
          :data-testid="T.profilePick"
          :options="profiles.map((p) => ({ value: p.profile_key, label: `${p.brand} ${p.model}` }))"
          placeholder="选择机型"
        />
        <p class="qt-small qt-warn">一经生成永不改变。</p>
        <p v-if="!profiles.length && profileMode === 'pick'" class="qt-small qt-muted">档案库拉取失败,只保留「随机」。</p>
      </section>

      <!-- ④ 账号密码(企点) -->
      <section v-if="channel === 'qidian' && step === 3" class="qt-card box">
        <div class="qt-section-title">账号密码</div>
        <a-input v-model:value="acctName" :data-testid="T.account" placeholder="企点账号" autocomplete="off" />
        <div class="qt-row pwrow">
          <a-input
            v-model:value="secret"
            class="qt-grow"
            :data-testid="T.secret"
            :type="showSecret ? 'text' : 'password'"
            placeholder="密码"
            autocomplete="new-password"
          />
          <a-button :data-testid="T.secretEye" @click="showSecret = !showSecret">{{ showSecret ? '隐藏' : '显示' }}</a-button>
        </div>
        <a-checkbox
          v-model:checked="remember"
          :data-testid="T.remember"
          :disabled="!session.winagentOnline"
        >保存到保险库(WinAgent,DPAPI)</a-checkbox>
        <p class="qt-small qt-muted">
          {{ session.winagentOnline ? '不保存则每次启动需手输。' : 'WinAgent 未运行,保险库暂不可用。' }}
        </p>
        <a-button type="primary" :loading="busy" :disabled="labelInvalid" :data-testid="T.create" @click="create">创建并登录</a-button>
      </section>

      <!-- QQ ③ 扫码 -->
      <section v-if="channel === 'qq' && step === 1" class="qt-card box">
        <a-button type="primary" :loading="busy" :disabled="labelInvalid" :data-testid="T.create" @click="create">创建并取二维码</a-button>
      </section>

      <section v-if="channel === 'qq' && step === 2 && account" class="qt-card box">
        <div class="qt-section-title">扫码登录</div>
        <p :data-testid="T.progress" role="status">当前阶段:{{ ACCOUNT_STATES[account.state].zh }} ({{ account.state }})</p>
        <p v-if="account.state_reason" class="qt-muted">{{ account.state_reason }}</p>
        <PromptCard
          :prompt="prompt"
          :state-code="stateCode"
          :testid="T.promptCard"
          :qr-testid="T.qrImg"
          :expire-testid="T.qrExpire"
        />
        <div class="qt-row">
          <a-button :data-testid="T.qrRefresh" @click="refreshQr">刷新二维码</a-button>
          <a-button :data-testid="T.qrOpenWebui" @click="openWebui">在浏览器打开 NapCat WebUI</a-button>
        </div>
        <p v-if="startError" class="qt-danger">启动失败:{{ startError }}</p>
        <div v-if="startError || ['created', 'stopped', 'error'].includes(account.state)" class="qt-row">
          <a-button :loading="busy" :data-testid="T.retry" @click="retryStart">重试启动</a-button>
          <a-button danger :disabled="busy" :data-testid="T.deleteBack" @click="deleteAndBack">删除并返回</a-button>
        </div>
        <p v-if="stalled" class="qt-warn" :data-testid="T.stalled">进度停滞,可去环境页看日志</p>
      </section>

      <!-- 企点 ⑤ 创建与登录 -->
      <section v-if="channel === 'qidian' && step === 4 && account" class="qt-card box">
        <div class="qt-section-title">创建与登录</div>
        <p :data-testid="T.progress" role="status">当前阶段:{{ ACCOUNT_STATES[account.state].zh }} ({{ account.state }})</p>
        <p v-if="account.state_reason" class="qt-muted">{{ account.state_reason }}</p>
        <PromptCard :prompt="prompt" :state-code="stateCode" :testid="T.promptCard">
          <div class="qt-row">
            <a-button
              v-if="['WAIT_SMS', 'WAIT_CAPTCHA', 'WAIT_DEVICE_CONFIRM'].includes(stateCode)"
              type="primary"
              :data-testid="T.gotoScreen"
              @click="screenOpen = true"
            >去画面</a-button>
          </div>
        </PromptCard>
        <div v-if="screenOpen && createdId" class="qt-card inner">
          <a-button @click="screenOpen = false">收起画面</a-button>
          <AccountScreen :key="createdId" embedded :account-id="createdId" />
        </div>
        <a-button
          v-if="account.state === 'login_required' && stateCode === 'WAIT_PASSWORD'"
          type="primary"
          :disabled="busy || passwordBusy"
          @click="openPasswordModal"
        >输入密码登录</a-button>
        <p v-if="passwordError" class="qt-danger">{{ passwordError }}</p>
        <p v-if="startError" class="qt-danger">启动失败:{{ startError }}</p>
        <div v-if="startError || ['created', 'stopped', 'error'].includes(account.state)" class="qt-row">
          <a-button :loading="busy" :data-testid="T.retry" @click="retryStart">重试启动</a-button>
          <a-button danger :disabled="busy" :data-testid="T.deleteBack" @click="deleteAndBack">删除并返回</a-button>
        </div>
        <p v-if="stalled" class="qt-warn" :data-testid="T.stalled">进度停滞,可去环境页看日志</p>
      </section>

      <!-- 微信 ① 检查模块与槽位 -->
      <section v-if="channel === 'wechat' && step === 0" class="qt-card box">
        <div class="qt-section-title">检查模块与槽位</div>
        <div :data-testid="T.wxModuleStatus" class="qt-col">
          <div>微信模块:{{ wxStatus?.module_enabled ? '已启用' : '未启用' }}</div>
          <div>用户会话代理:{{ wxStatus?.user_agent ? '在线' : '不在线' }}</div>
          <div>槽位:holder {{ resources.slots?.holder || '—' }} · pending {{ resources.slots?.pending || '—' }}</div>
        </div>
        <a-popconfirm v-if="!wxStatus?.module_enabled" title="启用本机微信模块，以便继续添加微信账号？" @confirm="enableWechatModule">
          <a-button :data-testid="T.wxEnableModule" :loading="busy" :disabled="busy || !session.winagentOnline">启用微信模块</a-button>
        </a-popconfirm>
        <p v-if="wxEnableError" role="alert" class="qt-danger">{{ wxEnableError }}</p>
        <p v-if="!wxStatus?.user_agent" class="qt-warn" :data-testid="T.wxUserAgentHint">请先登录 Windows 桌面</p>
        <p v-if="resources.hasPending" class="qt-danger">另一次切换正在进行,不能继续。</p>
        <p v-else-if="holderBusy" class="qt-warn" :data-testid="T.wxHolderHint">
          当前 {{ resources.slots?.holder }} 在线,新增会先登出它;历史档案保留。
        </p>
      </section>

      <!-- 微信 ② 版本匹配 -->
      <section v-if="channel === 'wechat' && step === 1" class="qt-card box">
        <div class="qt-section-title">版本匹配</div>
        <div class="qt-card inner" :data-testid="T.wxMatch">
          <div>检测到的版本:{{ (wxStatus?.installed as any[])?.[0]?.version ?? '—' }}</div>
          <div>结论:<b>{{ WECHAT_MATCH[wxMatch] ?? wxMatch ?? '—' }}</b></div>
          <div>处理:{{ WECHAT_ACTION[wxAction] ?? wxAction ?? '—' }}</div>
          <div class="qt-small qt-muted">随包版本 {{ wxStatus?.bundled_version ?? '—' }}</div>
        </div>
        <div v-if="wxMatch === 'MULTIPLE_INSTALLS'" class="qt-col">
          <a-radio-group v-model:value="wxPickIndex">
            <a-radio
              v-for="(ins, i) in wxInstalls"
              :key="ins.path"
              :value="i"
              :data-testid="T.wxInstallPick(i)"
            >{{ ins.path }}({{ ins.version }})</a-radio>
          </a-radio-group>
        </div>
        <a-button
          v-if="['UNSUPPORTED_NEWER', 'UNSUPPORTED_OLDER', 'NOT_INSTALLED'].includes(wxMatch)"
          type="primary"
          :data-testid="T.wxReinstallEnter"
          @click="step = 1.5"
        >用随包版本重装</a-button>
      </section>

      <!-- 微信 ②b 重装引导 -->
      <section v-if="channel === 'wechat' && step === 1.5" class="qt-card box">
        <div class="qt-section-title">重装引导</div>
        <div class="qt-col">
          <a-checkbox v-model:checked="wxReinstallCk.backup" :data-testid="T.wxReinstallCk('backup')">我已备份聊天数据</a-checkbox>
          <a-checkbox v-model:checked="wxReinstallCk.autoupdate" :data-testid="T.wxReinstallCk('autoupdate')">我已关闭微信自动更新</a-checkbox>
          <a-checkbox v-model:checked="wxReinstallCk.confirm" :data-testid="T.wxReinstallCk('confirm')">我确认重装</a-checkbox>
        </div>
        <a-button
          type="primary"
          danger
          :loading="busy"
          :disabled="!(wxReinstallCk.backup && wxReinstallCk.autoupdate && wxReinstallCk.confirm)"
          :data-testid="T.wxReinstallRun"
          @click="reinstall"
        >开始重装</a-button>
      </section>

      <!-- 微信 ③ 发起 -->
      <section v-if="channel === 'wechat' && step === 2" class="qt-card box">
        <div class="qt-section-title">发起</div>
        <p v-if="holderBusy">
          将登出当前 {{ resources.slots?.holder }},其消息采集会停止;队列里已受理的写指令跑完(≤60s)后登出;运行中的工作流会挂起。
        </p>
        <a-button type="primary" danger :loading="busy" :data-testid="T.wxSwitchConfirm" @click="startWechatFlow">
          确认发起
        </a-button>
      </section>

      <!-- 微信 ④ 讲述人仪式 -->
      <section v-if="channel === 'wechat' && step === 3" class="qt-card box">
        <div class="qt-section-title">讲述人仪式</div>
        <p>pyweixin 需要讲述人先于微信登录开启并持续 ≥5 分钟;可静音,不要关闭讲述人。</p>
        <PromptCard :prompt="prompt" :state-code="stateCode" :testid="T.promptCard" :timer-testid="T.wxNarratorTimer" />
        <div class="qt-row">
          <a-button :data-testid="T.wxNarratorOpen">开启讲述人(Win + Ctrl + Enter)</a-button>
          <a-button v-if="stateCode === 'WAIT_UI_TREE'" :data-testid="T.wxNarratorRedo">再做一次仪式</a-button>
          <a-button danger :data-testid="T.wxCancel" @click="cancelWechatLogin">取消</a-button>
        </div>
        <p class="qt-small qt-muted" :data-testid="T.wxSessionHint">
          正在登录 {{ createdId }}(本次尝试 {{ loginSessionId ?? '—' }})
        </p>
      </section>

      <!-- 微信 ⑤ 首次登录 -->
      <section v-if="channel === 'wechat' && step === 4" class="qt-card box">
        <div class="qt-section-title">首次登录(扫码 / 快捷登录均可)</div>
        <p>请在微信 PC 窗口登录。微信没有取码接口,这里显示的是窗口截图预览。</p>
        <img v-if="wxPreviewSrc" :data-testid="T.wxQrPreview" class="preview" :src="wxPreviewSrc" alt="微信窗口预览" />
        <p v-else class="qt-warn" :data-testid="T.wxQrPreview">微信窗口不可见</p>
        <div class="qt-row">
          <a-button :data-testid="T.wxRelaunch" @click="startWechatFlow">重新发起</a-button>
          <a-button danger :data-testid="T.wxCancel" @click="cancelWechatLogin">取消</a-button>
        </div>
      </section>

      <!-- 微信 ⑥ 取钥三段 -->
      <section v-if="channel === 'wechat' && step === 5" class="qt-card box">
        <div class="qt-section-title">取钥(三段固定顺序)</div>
        <ol class="keysteps" :data-testid="T.wxKeyProgress">
          <li :class="{ active: !['WAIT_KEY_IMG', 'WAIT_KEY_RELOGIN'].includes(stateCode) }">
            ⑥a 起 hook —— 正在挂接微信(勿手动关闭微信)
          </li>
          <li :class="{ active: stateCode === 'WAIT_KEY_IMG' }">
            ⑥b 请在微信里<b>随便打开一张图片</b>(任意聊天里的图都行)取 img_key
          </li>
          <li :class="{ active: stateCode === 'WAIT_KEY_RELOGIN' }">
            ⑥c 请<b>退出微信后重新登录</b>(扫码或快捷登录均可)—— 这一步不能省,不重登就取不到钥
          </li>
        </ol>
        <PromptCard
          v-if="stateCode === 'WAIT_KEY_IMG'"
          :prompt="prompt"
          :state-code="stateCode"
          :testid="T.promptCard"
          :timer-testid="T.wxKeyImgTimer"
        />
        <PromptCard
          v-else-if="stateCode === 'WAIT_KEY_RELOGIN'"
          :prompt="prompt"
          :state-code="stateCode"
          :testid="T.promptCard"
          :timer-testid="T.wxKeyReloginTimer"
        />
        <div v-if="stateCode === 'KEY_FAIL'" class="fail">
          <p class="qt-danger" :data-testid="T.wxKeyResult">
            {{ STATE_CODES.KEY_FAIL.zh }} —— 消息读取与发送均不可用
          </p>
          <p class="qt-warn" :data-testid="T.wxKeyFailHint">
            排查顺序:① 是否<b>重新登录</b>了微信(最常见,没重登必失败)→ ② 再考虑换随包 DLL 版本重装
          </p>
          <div class="qt-row">
            <a-button type="primary" :data-testid="T.wxKeyRetry" @click="keyRetry">重新取钥</a-button>
            <a-button :data-testid="T.wxKeyReinstall" @click="step = 1.5">用随包版本重装</a-button>
            <a-button @click="go('/env')">去环境页导日志</a-button>
          </div>
        </div>
      </section>

      <!-- 完成 -->
      <section
        v-if="account?.state === 'running' && ((channel === 'qidian' && step === 5) || (channel === 'qq' && step === 3) || (channel === 'wechat' && step === 6))"
        class="qt-card box"
      >
        <div class="qt-section-title">完成</div>
        <div :data-testid="T.doneSummary" class="qt-col">
          <div>账号 {{ createdId }} · {{ account?.label }}</div>
          <div>昵称 {{ account?.self_nick ?? '—' }}</div>
          <div v-if="channel === 'qidian'">端口 adb{{ account?.runtime?.adb_port }} 流{{ account?.runtime?.stream_port }}</div>
          <div v-if="channel === 'qq'" class="qt-small qt-muted">设备指纹已存入 qq_data 卷,下次启动免扫码;请勿删除数据卷。</div>
          <div v-if="channel === 'wechat'" class="qt-small qt-muted">
            微信 PC 勿休眠、勿锁屏(锁屏会进 degraded(SCREEN_LOCKED),发送不可用)。
          </div>
          <div v-if="channel === 'wechat' && account?.wxid" class="qt-small">wxid {{ account.wxid }}</div>
        </div>
        <p v-if="account?.wxid && route.query.wxnn && account.id !== route.query.wxnn" class="qt-warn" :data-testid="T.wxMismatchHint">
          登录的是 {{ account.id }},不是你选的 {{ route.query.wxnn }}
        </p>
        <div class="qt-row">
          <a-button type="primary" @click="go(`/acct/${createdId}`)">进入账号工作台</a-button>
          <a-button :data-testid="T.doneGolist" @click="go('/acct')">返回列表</a-button>
        </div>
      </section>

      <footer class="qt-row wizard-footer">
        <a-button :loading="cancelling" :data-testid="T.cancel" @click="cancelWizard">取消</a-button>
        <span class="qt-grow" />
        <a-button v-if="canNavigateForm && step > 0" :data-testid="T.prev" @click="step = Math.max(0, step - 1)">上一步</a-button>
        <a-button
          v-if="canNavigateForm && step < formLastStep"
          type="primary"
          :data-testid="T.next"
          :disabled="nextDisabled"
          @click="nextFormStep"
        >下一步</a-button>
      </footer>
    </template>

    <a-modal
      v-model:open="passwordModal"
      title="输入密码登录"
      @ok="submitPassword"
      @cancel="closePasswordModal"
    >
      <div :data-testid="T.passwordModal">
        <a-input v-model:value="modalSecret" type="password" autocomplete="new-password" placeholder="密码(不回显、不入库)" />
      </div>
      <template #footer>
        <a-button @click="closePasswordModal">取消</a-button>
        <a-button type="primary" :loading="passwordBusy" :disabled="busy || !modalSecret" :data-testid="T.passwordSubmit" @click="submitPassword">登录</a-button>
      </template>
    </a-modal>
  </div>
</template>

<style scoped>
.account-wizard { max-width: 1160px; margin: 0 auto; }
.wizard-security { padding: 9px 15px; border: 1px solid rgba(117,71,168,.15); color: var(--qt-primary); background: rgba(255,255,255,.65); border-radius: 22px; font-size: 12px; }
.box { padding: 32px; }
.box > .qt-section-title { margin-bottom: 22px; }
.chcards { display: flex; gap: 20px; margin-top: 18px; }
.chcard { flex: 1; padding: 28px; cursor: pointer; border: 1px solid rgba(117,71,168,.13); border-radius: 24px; transition: border-color .18s, transform .18s; }
.chcard:hover { border-color: var(--qt-primary); transform: translateY(-3px); }
.chcard:focus-visible { outline: 3px solid rgba(117,71,168,.35); outline-offset: 3px; }
.channel-icon { display: grid; place-items: center; width: 54px; height: 54px; background: linear-gradient(135deg,#eadef8,#fff0d3); color: var(--qt-primary); font-size: 26px; border: 1px solid #fff; border-radius: 18px; margin-bottom: 26px; }
.chcard .qt-section-title { font-size: 21px; }
.chcard p { line-height: 1.9; min-height: 50px; }
.channel-enter { display: inline-block; margin-top: 18px; color: var(--qt-primary); font-size: 13px; }
.wizard-steps { padding: 24px; border: 1px solid rgba(117,71,168,.1); border-radius: 20px; }
.wizard-footer { padding: 20px 24px; background: rgba(255,255,255,.78); border: 1px solid var(--qt-border); border-radius: 20px; }
.account-wizard :deep(.ant-input), .account-wizard :deep(.ant-input-affix-wrapper) { max-width: 680px; }
.account-wizard :deep(.ant-checkbox-wrapper) { margin-top: 12px; }
.reject { background: #FFF1F0; padding: var(--qt-space-3); border-radius: var(--qt-radius-sm); margin-top: var(--qt-space-3); }
.pick { width: 320px; display: block; margin-top: var(--qt-space-2); }
.pwrow { margin: var(--qt-space-2) 0; }
.inner { padding: var(--qt-space-3); margin-bottom: var(--qt-space-3); }
.preview { max-width: 420px; border: 1px solid var(--qt-border); border-radius: 16px; box-shadow: 0 14px 34px rgba(45,25,65,.12); }
.keysteps li { padding: 4px 0; color: var(--qt-text-secondary); }
.keysteps li.active { color: var(--qt-primary); font-weight: 600; }
.fail { margin-top: var(--qt-space-3); }
@media(max-width: 760px) { .chcards { flex-direction: column; } .box { padding: 22px; } .wizard-steps { padding: 18px; } }
@media(prefers-reduced-motion: reduce) { .chcard { transition: none; } .chcard:hover { transform: none; } }
</style>
