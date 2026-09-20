<script setup lang="ts">
/**
 * `P-ACCT-NEW` 新增账号向导(01 §2.7.3.1~§2.7.3.3)。
 * 企点:资源预检→起名→机型档案→账号密码→创建与登录→完成
 * QQ  :资源预检→起名→扫码→完成
 * 微信:检查模块与槽位→版本匹配→(重装引导)→发起→讲述人仪式→首次登录→取钥三段→完成
 *
 * 🔴 密码只在 ④→⑤ 之间存在于表单,提交即清;不进 store、不进路由 query、不进日志。
 * 🔴 取钥是实测三段固定顺序(R-05):起 hook → 先开图片取 img_key → 再退出重登取 data_key。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { acctNew as T } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useResourcesStore } from '@/stores/resources'
import { useSessionStore } from '@/stores/session'
import { accountsApi, commandsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import PromptCard from '@/components/PromptCard.vue'
import { CHANNELS, CHANNEL_TEXT, STATE_CODES, WECHAT_ACTION, WECHAT_MATCH, type Channel } from '@/i18n/zh-CN/codes'
import type { Account, DeviceProfileTemplate } from '@/api/types'

const route = useRoute()
const router = useRouter()
const accounts = useAccountsStore()
const resources = useResourcesStore()
const session = useSessionStore()

const channel = ref<Channel | null>((route.query.ch as Channel) ?? null)
const step = ref(0)
const busy = ref(false)
const createdId = ref<string | null>(null)
const rejectInfo = ref<{ message: string; alternatives: unknown[] } | null>(null)
const stalled = ref(false)
let stallTimer: ReturnType<typeof setTimeout> | null = null

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

const wxMatch = computed(() => (wxStatus.value?.match as string) ?? '')
const wxAction = computed(() => (wxStatus.value?.action as string) ?? '')
const wxInstalls = computed(() => (wxStatus.value?.installed as { path: string; version: string }[]) ?? [])
const holderBusy = computed(() => (resources.slots?.holder ?? '') !== '')

function go(p: string): void { void router.push(p) }

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

/* ── 企点 / QQ 创建 ── */

async function create(): Promise<void> {
  if (!channel.value) return
  busy.value = true
  rejectInfo.value = null
  try {
    const a = await accountsApi.create({
      channel: channel.value,
      label: label.value.trim(),
      profile_key: channel.value === 'qidian' && profileMode.value === 'pick' ? profileKey.value : undefined,
      login: channel.value === 'qq'
        ? { mode: 'qrcode' }
        : { mode: 'password', account: acctName.value, secret: secret.value, remember: remember.value },
    })
    // 密码提交即清:不进 store、不进路由 query、不进日志
    secret.value = ''
    createdId.value = a.id
    accounts.upsert(a)
    step.value = channel.value === 'qq' ? 2 : 4
    armStallTimer()
  } catch (e) {
    if (e instanceof ApiFailure && e.code === 'RESOURCE_EXHAUSTED') {
      rejectInfo.value = { message: e.detail.message, alternatives: e.detail.alternatives ?? [] }
      step.value = 0
    } else {
      message.error(e instanceof Error ? e.message : String(e))
    }
  } finally {
    busy.value = false
  }
}

function armStallTimer(): void {
  if (stallTimer) clearTimeout(stallTimer)
  stalled.value = false
  // 超过 10 分钟无状态变化 → 提示「进度停滞,可去环境页看日志」
  stallTimer = setTimeout(() => { stalled.value = true }, 10 * 60 * 1000)
}

watch(() => account.value?.state, (s) => {
  if (!s) return
  armStallTimer()
  if (s === 'running') {
    if (channel.value === 'qidian') step.value = 5
    else if (channel.value === 'qq') step.value = 3
    else step.value = 6
  }
  if (channel.value === 'wechat') {
    const c = stateCode.value
    if (c === 'WAIT_NARRATOR') step.value = 3
    else if (c === 'WAIT_QRCODE') step.value = 4
    else if (c === 'WAIT_KEY_IMG' || c === 'WAIT_KEY_RELOGIN' || c === 'KEY_FAIL') step.value = 5
  }
})

async function submitPassword(): Promise<void> {
  if (!createdId.value) return
  await accountsApi.login(createdId.value, { secret: modalSecret.value, remember: false })
  modalSecret.value = ''
  passwordModal.value = false
}

async function refreshQr(): Promise<void> {
  if (createdId.value) await accounts.refreshPrompt(createdId.value)
}

function openWebui(): void {
  const port = 16300 + ((account.value?.runtime?.ws_port ?? 16100) - 16100)
  void window.qt?.app.openExternal(`http://127.0.0.1:${port}/`)
}

async function retryStart(): Promise<void> {
  if (createdId.value) await accountsApi.restart(createdId.value)
}

async function deleteAndBack(): Promise<void> {
  if (createdId.value && account.value) await accountsApi.softDelete(createdId.value, account.value.label)
  go('/acct')
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
    const blob = await accountsApi.screenshot(createdId.value)
    if (wxPreviewSrc.value) URL.revokeObjectURL(wxPreviewSrc.value)
    wxPreviewSrc.value = URL.createObjectURL(blob)
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

onMounted(() => { if (channel.value) void preload() })
onUnmounted(() => {
  if (stallTimer) clearTimeout(stallTimer)
  if (wxPreviewTimer) clearInterval(wxPreviewTimer)
  if (wxPreviewSrc.value) URL.revokeObjectURL(wxPreviewSrc.value)
})
</script>

<template>
  <div class="qt-page qt-stack">
    <!-- 入口:选分支 -->
    <section v-if="!channel" class="qt-card box">
      <div class="qt-section-title">新增账号</div>
      <div class="chcards">
        <div
          v-for="ch in CHANNELS"
          :key="ch"
          class="qt-card chcard"
          :data-testid="T.channel(ch)"
          @click="pickChannel(ch)"
        >
          <div class="qt-section-title">{{ CHANNEL_TEXT[ch] }}</div>
          <p class="qt-small qt-muted">
            {{ ch === 'qidian' ? '账密登录(短信/滑块在画面里过)' : ch === 'qq' ? '扫码登录(qq_data 免扫)' : '微信 PC 扫码 + 三段取钥' }}
          </p>
        </div>
      </div>
    </section>

    <template v-else>
      <a-steps :current="step" size="small" :data-testid="T.steps">
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
            <a-button :data-testid="T.cancel" @click="go('/acct')">取消</a-button>
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
          {{ session.winagentOnline ? '不保存则每次启动需手输。' : 'WinAgent 未运行,本次密码不会保存。' }}
        </p>
        <a-button type="primary" :loading="busy" :data-testid="T.create" @click="create">创建并登录</a-button>
      </section>

      <!-- QQ ③ 扫码 -->
      <section v-if="channel === 'qq' && step === 1" class="qt-card box">
        <a-button type="primary" :loading="busy" :data-testid="T.create" @click="create">创建并取二维码</a-button>
      </section>

      <section v-if="channel === 'qq' && step === 2" class="qt-card box">
        <div class="qt-section-title">扫码登录</div>
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
      </section>

      <!-- 企点 ⑤ 创建与登录 -->
      <section v-if="channel === 'qidian' && step === 4" class="qt-card box">
        <div class="qt-section-title">创建与登录</div>
        <a-progress
          :data-testid="T.progress"
          :percent="account?.state === 'running' ? 100 : account?.state === 'login_required' ? 70 : 40"
          :status="account?.state === 'error' ? 'exception' : 'active'"
        />
        <p class="qt-muted">当前状态:{{ account?.state ?? '创建中' }} {{ account?.state_reason }}</p>
        <PromptCard :prompt="prompt" :state-code="stateCode" :testid="T.promptCard">
          <div class="qt-row">
            <a-button
              v-if="['WAIT_SMS', 'WAIT_CAPTCHA', 'WAIT_DEVICE_CONFIRM'].includes(stateCode)"
              type="primary"
              :data-testid="T.gotoScreen"
              @click="go(`/screen/${createdId}`)"
            >去画面</a-button>
            <a-button
              v-if="stateCode === 'WAIT_PASSWORD'"
              type="primary"
              @click="passwordModal = true"
            >输入密码登录</a-button>
          </div>
        </PromptCard>
        <div v-if="account?.state === 'error'" class="qt-row">
          <a-button :data-testid="T.retry" @click="retryStart">重试(重新启动)</a-button>
          <a-button danger :data-testid="T.deleteBack" @click="deleteAndBack">删除并返回</a-button>
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
        <a-button
          v-if="!wxStatus?.module_enabled"
          :data-testid="T.wxGotoSettings"
          @click="go('/set')"
        >去设置启用</a-button>
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
        v-if="(channel === 'qidian' && step === 5) || (channel === 'qq' && step === 3) || (channel === 'wechat' && step === 6)"
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
          <a-button type="primary" :data-testid="T.doneGocmd" @click="go('/cmd')">去指令台试一条</a-button>
          <a-button :data-testid="T.doneGolist" @click="go('/acct')">返回列表</a-button>
        </div>
      </section>

      <footer class="qt-row">
        <a-button :data-testid="T.cancel" @click="go('/acct')">取消</a-button>
        <span class="qt-grow" />
        <a-button v-if="step > 0" :data-testid="T.prev" @click="step = Math.max(0, step - 1)">上一步</a-button>
        <a-button
          v-if="step < steps.length - 1"
          type="primary"
          :data-testid="T.next"
          :disabled="(step === 0 && channel !== 'wechat' && canAddHere === 0)
            || (step === 1 && channel !== 'wechat' && labelInvalid)
            || (channel === 'wechat' && step === 0 && (!wxStatus?.module_enabled || resources.hasPending))"
          @click="step += 1"
        >下一步</a-button>
      </footer>
    </template>

    <a-modal
      v-model:open="passwordModal"
      title="输入密码登录"
      :data-testid="T.passwordModal"
      @ok="submitPassword"
    >
      <a-input v-model:value="modalSecret" type="password" autocomplete="new-password" placeholder="密码(不回显、不入库)" />
      <template #footer>
        <a-button @click="passwordModal = false">取消</a-button>
        <a-button type="primary" :data-testid="T.passwordSubmit" @click="submitPassword">登录</a-button>
      </template>
    </a-modal>
  </div>
</template>

<style scoped>
.box { padding: var(--qt-space-4); }
.chcards { display: flex; gap: var(--qt-space-3); }
.chcard { flex: 1; padding: var(--qt-space-4); cursor: pointer; }
.chcard:hover { border-color: var(--qt-primary); }
.reject { background: #FFF1F0; padding: var(--qt-space-3); border-radius: var(--qt-radius-sm); margin-top: var(--qt-space-3); }
.pick { width: 320px; display: block; margin-top: var(--qt-space-2); }
.pwrow { margin: var(--qt-space-2) 0; }
.inner { padding: var(--qt-space-3); margin-bottom: var(--qt-space-3); }
.preview { max-width: 420px; border: 1px solid var(--qt-border); }
.keysteps li { padding: 4px 0; color: var(--qt-text-secondary); }
.keysteps li.active { color: var(--qt-primary); font-weight: 600; }
.fail { margin-top: var(--qt-space-3); }
</style>
