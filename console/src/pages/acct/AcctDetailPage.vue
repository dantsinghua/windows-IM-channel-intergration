<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { acctDetail as T } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useEventsStore } from '@/stores/events'
import { useSessionStore } from '@/stores/session'
import { accountsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import StateDot from '@/components/StateDot.vue'
import PromptCard from '@/components/PromptCard.vue'
import { ACCOUNT_STATES, ALERT_CODES, CHANNEL_TEXT, LOGIN_PHASE_TITLE, QIDIAN_READ_DEGRADED_CODES, STATE_CODES, stateCardSubtitle, stateCardTitle } from '@/i18n/zh-CN/codes'

const AccountScreen = defineAsyncComponent(() => import('@/pages/screen/ScreenPage.vue'))
const route = useRoute()
const router = useRouter()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const events = useEventsStore()
const session = useSessionStore()
const id = computed(() => String(route.params.id ?? ''))
const a = computed(() => accounts.byId[id.value] ?? null)
const prompt = computed(() => accounts.prompts[id.value] ?? null)
const ops = computed(() => accounts.ops(a.value))
const stateCode = computed(() => a.value?.state_code ?? '')
const codeMeta = computed(() => STATE_CODES[stateCode.value])
const stateActions = computed(() => (codeMeta.value?.actions ?? []).map((action) =>
  a.value?.channel === 'qq' && action === 'goto-screen' ? 'refresh-qr' : action))
const showStateCard = computed(() => !!a.value && ['login_required', 'degraded', 'error'].includes(a.value.state) && stateCode.value !== 'RATE_LIMITED')
const readDegraded = computed(() => events.firing.find((alert) => (QIDIAN_READ_DEGRADED_CODES as readonly string[]).includes(alert.code) && alert.subject === 'account:' + id.value) ?? null)
const resSnapshot = computed(() => resources.metrics?.ours.accounts.find((item) => item.id === id.value) ?? resources.pool?.accounts?.find((item) => item.id === id.value) ?? null)
const memoryMb = computed(() => resSnapshot.value?.current_mb ?? (resSnapshot.value && 'rss_mb' in resSnapshot.value ? resSnapshot.value.rss_mb : null))
const loadingAccount = ref(false)
const accountError = ref('')
const screenOpen = ref(false)
const editLabel = ref(false)
const editingLabel = ref('')
const pwModal = ref(false)
const pwSecret = ref('')
const pwRemember = ref(false)
const passwordPurpose = ref<'login' | 'credential'>('login')
const busy = ref(false)
const webuiUntil = ref<number | null>(null)
const now = ref(Date.now())
const webuiLeft = computed(() => webuiUntil.value ? Math.max(0, Math.round((webuiUntil.value - now.value) / 1000)) : 0)
let tick: ReturnType<typeof setInterval> | null = null

function errorText(error: unknown): string {
  return error instanceof ApiFailure ? error.message + '（trace ' + error.traceShort + '）' : error instanceof Error ? error.message : String(error)
}
async function reload(): Promise<void> {
  const targetId = id.value
  if (!targetId) return
  loadingAccount.value = true
  accountError.value = ''
  try {
    if (!accounts.byId[targetId]) accounts.upsert(await accountsApi.get(targetId))
    if (id.value !== targetId) return
    editingLabel.value = a.value?.label ?? ''
    if (a.value && ['login_required', 'degraded', 'error'].includes(a.value.state)) await accounts.refreshPrompt(targetId).catch(() => undefined)
    if (!resources.metrics) await resources.loadMetrics().catch(() => undefined)
  } catch (error) { if (id.value === targetId) accountError.value = errorText(error) }
  finally { if (id.value === targetId) loadingAccount.value = false }
}
async function act(fn: () => Promise<unknown>, okText: string): Promise<boolean> {
  if (busy.value) return false
  busy.value = true
  try { await fn(); message.success(okText); await accounts.load(); if (!accounts.byId[id.value]) await reload(); return true }
  catch (error) { message.error(errorText(error)); return false }
  finally { busy.value = false }
}
async function saveLabel(): Promise<void> {
  if (await act(() => accountsApi.patch(id.value, { label: editingLabel.value }), '账号名称已更新')) editLabel.value = false
}
function closePassword(): void { pwSecret.value = ''; pwModal.value = false; passwordPurpose.value = 'login' }
async function submitPassword(): Promise<void> {
  if (!pwSecret.value || busy.value) return
  const pendingSecret = pwSecret.value
  pwSecret.value = ''
  const updated = passwordPurpose.value === 'credential'
    ? await act(() => accountsApi.putCredential(id.value, { secret: pendingSecret, remember: true }), '已更新保存的登录密码')
    : await act(() => accountsApi.login(id.value, { secret: pendingSecret, remember: pwRemember.value }), '已提交登录')
  if (updated) closePassword()
}
async function runStateAction(action: string): Promise<void> {
  switch (action) {
    case 'login': await act(() => accountsApi.login(id.value, {}), '已重新发起登录'); break
    case 'password': passwordPurpose.value = 'login'; pwModal.value = true; break
    case 'goto-screen': screenOpen.value = true; break
    case 'refresh-qr':
      if (busy.value) return
      busy.value = true
      try { await accounts.refreshPrompt(id.value) }
      catch (error) { message.error(errorText(error)) }
      finally { busy.value = false }
      break
    case 'key-retry':
      if (!window.qt?.wa) message.info('请在桌面控制台重新取钥')
      else await act(() => window.qt!.wa.invoke('wechat.key.retry', {}), '已请求重新取钥')
      break
    case 'reinstall': void router.push({ path: '/acct/new', query: { ch: 'wechat', step: 'reinstall' } }); break
    case 'unlock': message.info('请解锁 Windows 桌面后重试'); break
    case 'cred-update': passwordPurpose.value = 'credential'; pwModal.value = true; pwRemember.value = true; break
    case 'restart': await act(() => accountsApi.restart(id.value), '已请求重启'); break
    case 'open-env': void router.push('/env'); break
    case 'open-logs': void router.push({ path: '/log', query: { account_id: id.value } }); break
    case 'narrator-redo': message.info('请按 Win + Ctrl + Enter 开启讲述人，再完成微信登录'); break
    default: break
  }
}
async function openWebui(): Promise<void> {
  if (!window.qt?.app.openExternal) { message.info('请在桌面控制台打开 NapCat 工作台'); return }
  if (busy.value) return
  busy.value = true
  try {
    const result = await accountsApi.webuiOpen(id.value, 10)
    webuiUntil.value = Date.parse(result.until)
    const opened = await window.qt.app.openExternal(result.url)
    if (opened === false) message.warning('临时入口已开启，但地址不在本机安全白名单内，无法打开')
    else message.success('已打开 NapCat 工作台，入口将在 10 分钟后关闭')
  } catch (error) { message.error(errorText(error)) }
  finally { busy.value = false }
}
async function closeWebui(): Promise<void> {
  if (await act(() => accountsApi.webuiClose(id.value), '临时工作台入口已关闭')) webuiUntil.value = null
}
async function doSoftDelete(): Promise<void> {
  if (!a.value) return
  if (await act(() => accountsApi.softDelete(id.value, a.value!.label), '账号已从列表移除，数据与登录态保留')) void router.push('/acct')
}
function openMessages(): void { void router.push({ path: '/msg', query: { account_id: id.value } }) }
function startAccount(): void {
  if (a.value?.channel === 'wechat') void router.push({ path: '/acct/new', query: { ch: 'wechat', wxnn: id.value } })
  else void act(() => accountsApi.start(id.value), '已请求启动')
}
function memoryText(value?: number | null): string { return value == null ? '—' : (value / 1024).toFixed(2) + ' GB' }
watch(id, () => { screenOpen.value = false; closePassword(); webuiUntil.value = null; void reload() })
onMounted(() => { void reload(); tick = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => { if (tick) clearInterval(tick); pwSecret.value = '' })
</script>

<template>
  <div class="qt-page account-detail" :data-testid="T.drawer">
    <a-skeleton v-if="loadingAccount && !a" active />
    <a-result v-else-if="!a" status="404" title="暂时无法打开此账号" :sub-title="accountError || '账号可能已移除，请返回列表确认。'"><template #extra><a-button @click="reload">重试</a-button><a-button @click="router.push('/acct')">返回账号列表</a-button></template></a-result>
    <template v-else>
      <header class="account-heading qt-glass">
        <div class="account-avatar">{{ CHANNEL_TEXT[a.channel].slice(0, 1) }}</div>
        <div class="account-title"><div class="qt-eyebrow">{{ CHANNEL_TEXT[a.channel] }} / 账号工作台</div>
          <template v-if="!editLabel"><h1>{{ a.label || a.self_nick || a.id }}</h1><span class="qt-small qt-muted">{{ a.self_nick || '尚未取得昵称' }} · {{ a.wxid || a.self_uid || a.id }}</span></template>
          <div v-else class="qt-row"><a-input v-model:value="editingLabel" :maxlength="20" /><a-button type="primary" :loading="busy" :data-testid="T.labelSave" @click="saveLabel">保存</a-button><a-button @click="editLabel = false">取消</a-button></div>
        </div>
        <a-button v-if="!editLabel" size="small" :data-testid="T.labelEdit" @click="editLabel = true">改名</a-button>
        <span class="account-state-text"><StateDot :state="a.state" :reason="a.state_reason" />{{ ACCOUNT_STATES[a.state]?.zh || '状态未知' }}</span>
      </header>

      <div class="qt-toolbar account-toolbar">
        <a-button type="primary" @click="openMessages">查询历史消息</a-button>
        <a-button v-if="ops.start" :loading="busy" :disabled="a.channel === 'wechat' && resources.hasPending" :data-testid="T.op('start')" @click="startAccount">{{ a.channel === 'wechat' ? '登录此微信' : '启动账号' }}</a-button>
        <a-popconfirm v-if="ops.stop" title="停止当前账号？消息采集与正在进行的任务会受影响。" @confirm="act(() => accountsApi.stop(a!.id), '已请求停止')"><a-button :disabled="busy" :data-testid="T.op('stop')">停止账号</a-button></a-popconfirm>
        <a-popconfirm v-if="ops.restart" title="重启当前账号？连接和正在进行的任务会暂时中断。" @confirm="act(() => accountsApi.restart(a!.id), '已请求重启')"><a-button :disabled="busy" :data-testid="T.op('restart')">重启</a-button></a-popconfirm>
        <a-button class="back-link" @click="router.push('/acct')">全部账号 ↗</a-button>
      </div>

      <div v-if="readDegraded" class="warnbar" :data-testid="T.readDegraded">{{ ALERT_CODES[readDegraded.code]?.zh ?? readDegraded.message }}<small>系统每 5 分钟自动重试；也可按需重启当前账号。</small></div>
      <section v-if="showStateCard" class="qt-card state-guide" :data-testid="T.stateCard">
        <div class="qt-row"><strong>{{ stateCardTitle(stateCode) }}</strong><span v-if="codeMeta?.group === 'wait'" class="workspace-label" :data-testid="T.stateCardLoginPhase">{{ LOGIN_PHASE_TITLE }}</span></div>
        <p v-if="stateCardSubtitle(stateCode)" class="qt-muted">{{ stateCardSubtitle(stateCode) }}</p><p>{{ prompt?.text || codeMeta?.zh || a.state_reason }}</p>
        <PromptCard :prompt="prompt" :state-code="stateCode" />
        <div class="qt-row wrap"><a-button v-for="action in stateActions" :key="action" type="primary" :disabled="busy" :data-testid="T.stateCardAction(action)" @click="runStateAction(action)">{{ ({ login: '重新登录', password: '输入密码登录', 'goto-screen': '查看账号画面', 'refresh-qr': '刷新二维码', 'key-retry': '重新取钥', reinstall: '修复微信', unlock: '解锁 Windows', 'cred-update': '更新登录密码', restart: '重试启动', 'open-env': '检查环境', 'open-logs': '查看日志', 'narrator-redo': '重新准备登录' } as Record<string,string>)[action] ?? action }}</a-button></div>
      </section>

      <div class="account-layout">
        <section class="qt-glass account-workspace">
          <header class="workspace-heading"><div><div class="qt-eyebrow">{{ a.channel === 'qq' ? 'NAPCAT / ONEBOT' : a.channel === 'wechat' ? 'WECHAT / WINDOWS' : 'QIDIAN / ANDROID' }}</div><h2>{{ a.channel === 'qq' ? 'QQ 工作台' : a.channel === 'wechat' ? '微信账号画面' : '企点操作画面' }}</h2></div><span class="workspace-label">{{ a.channel === 'qq' ? '本机受控入口' : a.channel === 'wechat' ? '只读预览' : '实时画面' }}</span></header>
          <template v-if="a.channel === 'qq'">
            <div class="channel-preview"><div class="preview-orbit">QQ</div><h3>{{ a.self_nick || a.label }}</h3><p>通过 NapCat 管理当前账号，收发记录可在消息中心查询。</p><a-popconfirm title="临时开放本机 NapCat 工作台 10 分钟并在浏览器打开？" @confirm="openWebui"><a-button type="primary" :disabled="session.draining || busy" :data-testid="T.webuiOpen">打开 NapCat 工作台</a-button></a-popconfirm><div v-if="webuiLeft" class="webui-timer"><span :data-testid="T.webuiCountdown">临时入口剩余 {{ webuiLeft }} 秒</span><a-button size="small" :data-testid="T.webuiClose" @click="closeWebui">提前关闭</a-button></div></div>
          </template>
          <AccountScreen v-else-if="screenOpen" :key="a.id" embedded :account-id="a.id" />
          <div v-else class="channel-preview"><div class="preview-orbit">{{ a.channel === 'wechat' ? '微' : '企' }}</div><h3>{{ a.channel === 'wechat' ? '查看当前微信窗口' : '打开专属账号画面' }}</h3><p>{{ a.channel === 'wechat' ? '窗口每 2 秒更新一次。操作请在 Windows 中的微信窗口完成。' : '按需连接实时画面，完成登录、验证码与日常操作。' }}</p><a-button type="primary" :data-testid="T.op('screen')" @click="screenOpen = true">{{ a.channel === 'wechat' ? '查看微信画面' : '连接企点画面' }}</a-button></div>
        </section>
        <aside class="account-sidebar">
          <section class="qt-card facts-card"><div class="qt-eyebrow">ACCOUNT PROFILE</div><h2>账号信息</h2><div class="kv"><span>通道</span><b>{{ CHANNEL_TEXT[a.channel] }}</b></div><div class="kv"><span>标识</span><b>{{ a.wxid || a.self_uid || a.id }}</b></div><div class="kv"><span>最近活动</span><b>{{ a.last_seen_at?.slice(5,19).replace('T',' ') || '—' }}</b></div><p v-if="a.state_reason" class="qt-small qt-muted">{{ a.state_reason }}</p></section>
          <section class="qt-card facts-card"><div class="qt-eyebrow">RESOURCE USAGE</div><h2>当前占用</h2><div class="resource-value" :data-testid="T.resField('current')">{{ memoryText(memoryMb) }}</div><div class="kv"><span>CPU</span><b :data-testid="T.resField('cpu')">{{ resSnapshot?.cpu_pct == null ? '—' : resSnapshot.cpu_pct + '%' }}</b></div><a-button block @click="router.push('/res')">查看资源监控 ↗</a-button></section>
          <div class="remove-account"><a-popconfirm title="从列表移除该账号？账号将停用，数据与登录态保留。" @confirm="doSoftDelete"><a-button type="text" :disabled="!ops.del || busy" :data-testid="T.delete">移除账号</a-button></a-popconfirm></div>
        </aside>
      </div>
    </template>

    <a-modal v-model:open="pwModal" :title="passwordPurpose === 'credential' ? '更新保存的登录密码' : '输入密码登录'" :data-testid="T.loginPasswordModal" :footer="null" @cancel="closePassword">
      <a-input v-model:value="pwSecret" type="password" autocomplete="new-password" placeholder="输入登录密码" />
      <a-checkbox v-if="passwordPurpose === 'login'" v-model:checked="pwRemember" :disabled="!session.winagentOnline" class="mt">这次保存到保险库</a-checkbox>
      <div class="qt-row mt"><a-button @click="closePassword">取消</a-button><a-button type="primary" :loading="busy" :disabled="!pwSecret || (passwordPurpose === 'credential' && !session.winagentOnline)" :data-testid="T.loginPasswordSubmit" @click="submitPassword">{{ passwordPurpose === 'credential' ? '保存' : '登录' }}</a-button></div>
    </a-modal>
  </div>
</template>

<style scoped>
.account-heading { display: flex; align-items: center; margin-bottom: 22px; padding: 28px; border: 1px solid rgba(117,71,168,.12); border-radius: 26px; gap: 18px; }
.account-state-text { display: inline-flex; align-items: center; gap: 8px; color: var(--qt-text-secondary); font-size: 13px; }
.account-title { flex: 1; min-width: 0; }
.account-title h1 { margin: 4px 0; font-size: 28px; letter-spacing: -.8px; }
.account-avatar { display: grid; place-items: center; flex-shrink: 0; width: 64px; height: 64px; border: 1px solid rgba(255,255,255,.8); border-radius: 22px; color: var(--qt-primary); background: linear-gradient(135deg,#eee5fa,#fff6de); font-size: 26px; }
.account-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 24px; }
.back-link { margin-left: auto; }
.account-layout { display: grid; grid-template-columns: minmax(0,1fr) 285px; gap: 22px; }
.account-workspace { border: 1px solid rgba(117,71,168,.12); border-radius: 26px; padding: 26px; min-width: 0; }
.workspace-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.workspace-heading h2, .facts-card h2 { margin: 5px 0 0; font-size: 20px; }
.workspace-label { background: rgba(117,71,168,.08); color: var(--qt-primary); font-size: 11px; padding: 7px 12px; border-radius: 20px; white-space: nowrap; }
.channel-preview { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 390px; padding: 28px 18px; text-align: center; background: radial-gradient(ellipse at 38% 60%,rgba(117,71,168,.11),transparent 52%),radial-gradient(ellipse at 66% 40%,rgba(242,173,56,.14),transparent 46%); }
.preview-orbit { display: grid; place-items: center; width: 88px; height: 88px; border-radius: 28px; background: rgba(255,255,255,.75); border: 1px solid #fff; color: var(--qt-primary); box-shadow: 0 16px 45px rgba(117,71,168,.12); font-size: 30px; }
.channel-preview h3 { margin: 24px 0 8px; font-size: 20px; }
.channel-preview p { color: var(--qt-text-secondary); max-width: 420px; line-height: 1.8; font-size: 13px; margin-bottom: 24px; }
.webui-timer { display: flex; align-items: center; gap: 10px; margin-top: 18px; color: var(--qt-text-secondary); font-size: 11px; }
.account-sidebar { display: flex; flex-direction: column; gap: 20px; }
.facts-card { padding: 24px; }
.facts-card h2 { margin-bottom: 18px; font-size: 17px; }
.kv { display: flex; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--qt-border); font-size: 12px; }
.kv span { color: var(--qt-text-secondary); flex-shrink: 0; }
.kv b { text-align: right; overflow-wrap: anywhere; font-weight: 500; }
.resource-value { font-size: 31px; margin: 12px 0; letter-spacing: -1px; }
.facts-card .ant-btn { margin-top: 18px; }
.remove-account { text-align: center; }
.state-guide { padding: 24px; margin-bottom: 22px; }
.wrap { flex-wrap: wrap; gap: 8px; }
.warnbar { background: #fff5df; color: var(--qt-sev-warn); padding: 18px 22px; border-radius: 16px; margin-bottom: 20px; }
.warnbar small { display: block; margin-top: 6px; }
.mt { margin-top: 18px; }
@media(max-width: 1000px) { .account-layout { grid-template-columns: 1fr; } .account-sidebar { display: grid; grid-template-columns: 1fr 1fr; } }
@media(max-width: 760px) { .account-heading { padding: 20px; flex-wrap: wrap; } .account-workspace { padding: 18px; } .account-avatar { width: 48px; height: 48px; } .account-sidebar { display: flex; } .account-title h1 { font-size: 23px; } .back-link { margin-left: 0; } }
</style>
