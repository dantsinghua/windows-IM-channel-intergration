<script setup lang="ts">
/**
 * `P-SCREEN` 画面(01 §2.7.4):企点 WebCodecs 内嵌流 + 事件回注;微信窗口截图预览(只看不点);QQ 无画面。
 * `login_required` 下画面注入照常可用(R-06),但发消息类不在画面里。
 * 降档:硬解 → 软解(thumb)→ 静态预览(每 2 s #33 截图 + #35 REST 注入);自动降、不自动升。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { screen as T, SCREEN_PERF_ITEMS, SCREEN_TOOLS } from '@/testids'
import { useAccountsStore } from '@/stores/accounts'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'
import { accountsApi } from '@/api/client'
import { ApiFailure } from '@/api/http'
import { ScreenStream, type DecodePath, type StreamClosed, type StreamProfile } from '@/codec/stream'
import StateDot from '@/components/StateDot.vue'
import { STATE_CODES } from '@/i18n/zh-CN/codes'
import { normInContain, PointerRelay, restGesture } from './pointer'

const route = useRoute()
const router = useRouter()
const accounts = useAccountsStore()
const ui = useUiStore()
const session = useSessionStore()

const focusId = ref<string>(String(route.params.id ?? ''))
const canvas = ref<HTMLCanvasElement | null>(null)
const stats = ref({ fps: 0, latencyMs: 0, decoder: 'hardware' as DecodePath, codec: '', connected: false })
const degradeMsg = ref('')
/** #34 关闭码分诊结果(4401/4400/4409/4410/4503 分别提示,不一律「连接失败」) */
const streamClosed = ref<StreamClosed | null>(null)
const shotUrl = ref('')
const shotOpen = ref(false)
const wechatSrc = ref('')
const wechatNotReady = ref(false)
const staticSrc = ref('')
/**
 * R6-72:画面注入是 W 级。R 令牌注入 ⇒ #34 以 4403 关 WS、#35 回 403。
 * 渲染进程拿不到令牌级别(session store 只有 authState 三值),只能兜底:
 * 收到 4403 / 403 就进只读 —— 不再发 touch/key/scroll/text、不自动重连;
 * 只读跨账号保持(令牌是同一张),直到令牌变化(authState 离开 ok 再回到 ok = 重新取过令牌)。
 */
const readOnly = ref(false)
let stream: ScreenStream | null = null
let previewTimer: ReturnType<typeof setInterval> | null = null
let visibilityOff: (() => void) | undefined
/** 窗口当前是否被最小化/隐藏(§2.7.4:隐藏时全部 pause,轮询也停) */
let hidden = false

const streamable = computed(() => accounts.items.filter((a) => a.channel !== 'qq'))
const qqAccounts = computed(() => accounts.items.filter((a) => a.channel === 'qq'))
const focus = computed(() => (focusId.value ? accounts.byId[focusId.value] ?? null : null))
const isWechat = computed(() => focus.value?.channel === 'wechat')
const loginPhase = computed(() => focus.value?.state === 'login_required')
const notRunning = computed(() =>
  !!focus.value && ['stopped', 'disabled', 'created'].includes(focus.value.state))
const starting = computed(() =>
  !!focus.value && ['starting', 'provisioning', 'logging_in'].includes(focus.value.state))
const isStatic = computed(() => stats.value.decoder === 'static')

const PROFILE_OF: Record<string, StreamProfile> = { focus30: 'focus', focus15: 'focus15', thumb10: 'thumb10' }

/** 画布指针 → #34 控制帧(首帧前不发、move ≤ 60 Hz、长按期间不发任何东西) */
const relay = new PointerRelay((f) => { if (!readOnly.value) stream?.send(f) })

function draw(frame: VideoFrame): void {
  const c = canvas.value
  if (!c) { frame.close(); return }
  const ctx = c.getContext('2d')
  if (!ctx) { frame.close(); return }
  if (c.width !== frame.displayWidth || c.height !== frame.displayHeight) {
    c.width = frame.displayWidth
    c.height = frame.displayHeight
    // 切档改了分辨率:以真正解出来的帧尺寸为准
    relay.setMedia(frame.displayWidth, frame.displayHeight)
  }
  ctx.drawImage(frame, 0, 0)
  frame.close()
}

function stopAll(): void {
  relay.releaseAll()
  relay.setMedia(0, 0)
  stream?.stop()
  stream = null
  if (previewTimer) clearInterval(previewTimer)
  previewTimer = null
}

async function startFocus(): Promise<void> {
  stopAll()
  degradeMsg.value = ''
  closeIme()
  const a = focus.value
  if (!a) return
  if (a.channel === 'wechat') {
    // 微信:2s 一帧窗口截图,不接收任何输入
    previewTimer = setInterval(() => void pollWechat(), 2000)
    void pollWechat()
    return
  }
  if (!['running', 'degraded', 'login_required', 'starting'].includes(a.state)) return
  streamClosed.value = null
  const s = new ScreenStream(a.id, PROFILE_OF[ui.perfProfile] ?? 'focus', {
    onHeader: (h) => relay.setMedia(h.width, h.height),
    onStats: (st) => { stats.value = { ...stats.value, ...st } },
    onFrame: draw,
    onFatal: (why) => {
      degradeMsg.value = why
    },
    onClosed: (info) => {
      streamClosed.value = info
      // 4403:令牌只能看。停手、不自动重连,等用户点「重新连接(只看)」
      if (info.reason === 'forbidden_inject') { enterReadOnly(); stopAll(); return }
      if (info.reason === 'stream_backend_missing') { stopAll(); return }
      // 4401 由 App.vue 的事件流横幅统一引导「重取令牌」;4409/4400 重试无意义,都停手。
      if (!info.retryable) stopAll()
    },
    // 第三档:流已关,改走 #33 截图轮询,点击改走 #35 REST
    onStatic: () => {
      relay.releaseAll()
      startStaticPreview()
    },
  })
  stream = s
  if (hidden) s.pause()
  await s.start()
}

/** 进只读:先置位再松开指针(松开时的 up/cancel 也不许再发) */
function enterReadOnly(): void {
  readOnly.value = true
  relay.releaseAll()
  staticDown = null
  closeIme()
}

/** #35 REST 注入被拒(R 令牌 ⇒ 403):进只读,之后不再请求 */
function onRestInjectError(e: unknown): void {
  if (e instanceof ApiFailure && e.status === 403) enterReadOnly()
}

function startStaticPreview(): void {
  if (previewTimer) clearInterval(previewTimer)
  previewTimer = setInterval(() => void pollStatic(), 2000)
  void pollStatic()
}

async function pollStatic(): Promise<void> {
  if (!focus.value || hidden) return
  try {
    const shot = await accountsApi.screenshot(focus.value.id)
    if (!shot.blob) return
    if (staticSrc.value) URL.revokeObjectURL(staticSrc.value)
    staticSrc.value = URL.createObjectURL(shot.blob)
  } catch { /* 下一轮再试 */ }
}

async function pollWechat(): Promise<void> {
  if (!focus.value || hidden) return
  try {
    const v = (await window.qt?.wa.invoke('wechat.ui-visible', {})) as { visible?: boolean } | undefined
    if (v && v.visible === false) { wechatNotReady.value = true; return }
    const shot = await accountsApi.screenshot(focus.value.id)
    if (!shot.blob) { wechatNotReady.value = true; return }
    wechatNotReady.value = false
    if (wechatSrc.value) URL.revokeObjectURL(wechatSrc.value)
    wechatSrc.value = URL.createObjectURL(shot.blob)
  } catch {
    wechatNotReady.value = true
  }
}

/** 窗口可见性:隐藏 → pause,恢复 → resume(§2.7.4 规格策略) */
function applyVisibility(visible: boolean): void {
  hidden = !visible
  if (visible) {
    stream?.resume()
    if (isWechat.value) void pollWechat()
    else if (isStatic.value) void pollStatic()
  } else {
    relay.releaseAll()
    stream?.pause()
  }
}

function watchVisibility(): (() => void) | undefined {
  if (window.qt?.window?.onVisibility) return window.qt.window.onVisibility(applyVisibility)
  // 纯浏览器调试(没有 preload):退回页面可见性
  if (typeof document === 'undefined') return undefined
  const onChange = (): void => applyVisibility(document.visibilityState !== 'hidden')
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}

const zoom = ref(100)

/** 流模式:canvas 指针事件原样透传(down / move / up / cancel),前端不合成 tap / longpress */
function onCanvasPointer(e: PointerEvent, kind: 'down' | 'move' | 'up' | 'cancel'): void {
  if (isWechat.value || !stream || readOnly.value) return
  const el = e.currentTarget as HTMLElement | null
  if (!el) return
  const box = el.getBoundingClientRect()
  if (kind === 'down') {
    if (relay.down(e, box)) {
      e.preventDefault()
      el.focus?.()
      try { el.setPointerCapture(e.pointerId) } catch { /* 指针已失效 */ }
    }
  } else if (kind === 'move') {
    relay.move(e, box)
  } else if (kind === 'up') {
    relay.up(e, box)
    try { el.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
  } else {
    relay.cancel(e.pointerId)
  }
}

function onWheel(e: WheelEvent): void {
  if (isWechat.value || !stream || readOnly.value) return
  const el = e.currentTarget as HTMLElement | null
  if (el) relay.wheel(e, e.deltaX, e.deltaY, el.getBoundingClientRect())
}

/**
 * 静态预览模式:#35 只有 tap/swipe,没有 down/move/up,
 * 所以按下记起点、松手时把整段手势换成一条 REST(长按 = 原地 swipe)。
 */
let staticDown: { x: number; y: number; t: number; last: { x: number; y: number }; pointerId: number } | null = null

function staticPoint(e: PointerEvent): { x: number; y: number } | null {
  const el = e.currentTarget as HTMLImageElement | null
  if (!el || !el.naturalWidth || !el.naturalHeight) return null
  return normInContain(e.clientX, e.clientY, el.getBoundingClientRect(), el.naturalWidth, el.naturalHeight)
}

function onStaticPointer(e: PointerEvent, kind: 'down' | 'move' | 'up' | 'cancel'): void {
  if (isWechat.value || !focus.value || readOnly.value) return
  const el = e.currentTarget as HTMLElement | null
  if (kind === 'down') {
    const p = staticPoint(e)
    if (!p) return
    e.preventDefault()
    el?.focus?.()
    staticDown = { ...p, t: Date.now(), last: p, pointerId: e.pointerId }
    try { el?.setPointerCapture(e.pointerId) } catch { /* 指针已失效 */ }
    return
  }
  if (!staticDown || staticDown.pointerId !== e.pointerId) return
  if (kind === 'move') {
    const p = staticPoint(e)
    if (p) staticDown.last = p
    return
  }
  const start = staticDown
  staticDown = null
  if (kind === 'cancel') return
  const end = staticPoint(e) ?? start.last
  void accountsApi.streamInput(focus.value.id, restGesture(start, end, Date.now() - start.t)).catch(onRestInjectError)
}

/** 控制键 → `key`(按下 + 抬起各一帧);静态预览走 #35 */
function sendKey(keycode: string): void {
  if (readOnly.value) return
  if (isStatic.value) {
    if (focus.value) void accountsApi.streamInput(focus.value.id, { type: 'key', keycode }).catch(onRestInjectError)
    return
  }
  if (!stream) return
  stream.send({ type: 'key', keycode, action: 'down' })
  stream.send({ type: 'key', keycode, action: 'up' })
}

function sendText(text: string): void {
  if (!text || readOnly.value) return
  if (isStatic.value) {
    if (focus.value) void accountsApi.streamInput(focus.value.id, { type: 'text', text }).catch(onRestInjectError)
    return
  }
  stream?.send({ type: 'text', text })
}

const KEYCODE_OF: Record<string, string> = {
  Enter: 'ENTER', Backspace: 'DEL', Escape: 'ESCAPE', Tab: 'TAB', Delete: 'FORWARD_DEL',
  ArrowUp: 'DPAD_UP', ArrowDown: 'DPAD_DOWN', ArrowLeft: 'DPAD_LEFT', ArrowRight: 'DPAD_RIGHT',
  Home: 'MOVE_HOME', End: 'MOVE_END', PageUp: 'PAGE_UP', PageDown: 'PAGE_DOWN',
}

/**
 * 输入框攒字:可打印字符不逐个当 `text` 发(一次按键一条注入,又慢又会被输入法拆碎),
 * 而是攒进输入框(中文输入法也在这里上屏),回车一次发整段 `text`。
 * 登录态下(可能是密码/短信码)输入框按密码框显示,不回显(§2.7.4 / §6)。
 */
const imeOpen = ref(false)
const imeText = ref('')
const imeInput = ref<HTMLInputElement | null>(null)

function openIme(initial = ''): void {
  if (readOnly.value) return
  imeOpen.value = true
  imeText.value += initial
  void nextTick(() => imeInput.value?.focus())
}

function closeIme(): void {
  imeOpen.value = false
  imeText.value = ''
}

function submitIme(): void {
  const text = imeText.value
  closeIme()
  sendText(text)
  canvas.value?.focus()
}

function onImeKey(e: KeyboardEvent): void {
  // 输入法组字中(拼音候选)的回车 / Esc 归输入法,不算提交
  if (e.isComposing || e.keyCode === 229) return
  if (e.key === 'Enter') { e.preventDefault(); submitIme() }
  else if (e.key === 'Escape') { e.preventDefault(); closeIme(); canvas.value?.focus() }
}

function onKey(e: KeyboardEvent): void {
  if (isWechat.value || readOnly.value || (!stream && !isStatic.value) || e.isComposing) return
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault()
    openIme(e.key)
    return
  }
  const keycode = KEYCODE_OF[e.key]
  if (!keycode) return
  e.preventDefault()
  sendKey(keycode)
}

function tool(t: string): void {
  if (t === 'back') sendKey('BACK')
  else if (t === 'home') sendKey('HOME')
  else if (t === 'shot') void takeShot()
  else if (t === 'keyboard') openIme()
}

/** #33:二进制 + `X-QT-Media-Id`/`X-QT-Sha256` 头 —— 存档时把这两个值一起显示,便于对账 */
const shotMeta = ref<{ mediaId: string | null; sha256: string | null }>({ mediaId: null, sha256: null })

async function takeShot(): Promise<void> {
  if (!focus.value) return
  const shot = await accountsApi.screenshot(focus.value.id)
  if (!shot.blob) return
  if (shotUrl.value) URL.revokeObjectURL(shotUrl.value)
  shotUrl.value = URL.createObjectURL(shot.blob)
  shotMeta.value = { mediaId: shot.mediaId, sha256: shot.sha256 }
  shotOpen.value = true
}

async function saveShot(): Promise<void> {
  if (!shotUrl.value) return
  const bytes = new Uint8Array(await (await fetch(shotUrl.value)).arrayBuffer())
  await window.qt?.files.saveAs(`${focus.value?.id}-${Date.now()}.png`, 'image/png', bytes)
}

function setPerf(p: string): void {
  if (p === 'retry-hw') {
    // 静态预览档流已关:整条重开(start 会重新探测);其余档在原流上升回硬解
    if (isStatic.value || !stream) {
      stats.value = { ...stats.value, decoder: 'hardware' }
      void startFocus()
    } else stream.retryHardware()
    return
  }
  ui.perfProfile = p as typeof ui.perfProfile
  stream?.setProfile(PROFILE_OF[p] ?? 'focus')
}

function selectThumb(id: string): void {
  focusId.value = id
  ui.focusedAccountId = id
  void router.replace(`/screen/${id}`)
}

watch(focusId, () => void startFocus())
// 令牌变化(离开 ok 再回到 ok = 重新取过令牌)才解除只读;换账号不解除
watch(() => session.authState, (v, old) => { if (v === 'ok' && old !== 'ok') readOnly.value = false })
watch(() => route.params.id, (v) => { if (v) focusId.value = String(v) })

onMounted(async () => {
  // 🔴 先注册可见性监听,再走任何可能提前 return 的分支(B5:以前首次自动选账号后 return,监听没注册)
  visibilityOff = watchVisibility()
  if (!accounts.items.length) await accounts.load()
  // 这里再调一次 startFocus 会和上面的 watch 各开一条 focus 流。
  // 后到的那条被服务端拒绝(同时只允许 1 个),页面就停在空白画布上。
  if (!focusId.value && streamable.value.length) {
    focusId.value = streamable.value[0].id
    return
  }
  await startFocus()
})

onUnmounted(() => {
  stopAll()
  visibilityOff?.()
  visibilityOff = undefined
  if (shotUrl.value) URL.revokeObjectURL(shotUrl.value)
  if (wechatSrc.value) URL.revokeObjectURL(wechatSrc.value)
  if (staticSrc.value) URL.revokeObjectURL(staticSrc.value)
})
</script>

<template>
  <div class="screen">
    <aside class="thumbs">
      <div class="qt-section-title">账号</div>
      <div
        v-for="a in streamable"
        :key="a.id"
        class="thumb"
        :class="{ active: a.id === focusId, guide: a.state === 'login_required' }"
        :data-testid="T.thumb(a.id)"
        @click="selectThumb(a.id)"
      >
        <StateDot :state="a.state" :reason="a.state_reason" />
        <span class="qt-grow">{{ a.id }}</span>
        <span class="qt-small qt-muted">{{ a.channel === 'wechat' ? '截图 2s' : '540p@5fps' }}</span>
        <a-button
          v-if="['stopped', 'created', 'disabled'].includes(a.state)"
          size="small"
          :data-testid="T.thumbStart(a.id)"
          @click.stop="accountsApi.start(a.id)"
        >启动</a-button>
      </div>
      <div
        v-for="a in qqAccounts"
        :key="a.id"
        class="thumb qq"
        :data-testid="T.qqHint(a.id)"
        @click="router.push({ path: '/msg', query: { account_id: a.id } })"
      >
        {{ a.id }} —— 无画面,去消息页
      </div>
    </aside>

    <main class="focus">
      <template v-if="focus">
        <header class="qt-row">
          <StateDot :state="focus.state" :reason="focus.state_reason" />
          <strong class="qt-grow">{{ focus.id }} {{ focus.label }}</strong>
          <span v-if="focus.state_code" class="qt-warn qt-small">
            {{ STATE_CODES[focus.state_code]?.zh ?? focus.state_code }}
          </span>
        </header>

        <!-- R-06:登录态可在画面里输验证码 / 拖滑块 -->
        <div v-if="loginPhase && !isWechat" class="banner" :data-testid="T.loginHint">
          登录中:可在画面里输验证码/拖滑块。发消息类请去指令台(登录态置灰)。
          ⚠️ 密码与短信码不回显、不落任何前端日志。
        </div>

        <a-skeleton v-if="starting" active />
        <a-empty v-else-if="notRunning" description="未运行">
          <a-button type="primary" @click="accountsApi.start(focus.id)">启动</a-button>
        </a-empty>

        <!-- 微信:只看不点 -->
        <template v-else-if="isWechat">
          <p v-if="wechatNotReady" :data-testid="T.wechatNotready" class="qt-warn">微信窗口不可见</p>
          <img
            v-else-if="wechatSrc"
            class="wxpreview"
            :data-testid="T.wechatPreview"
            :src="wechatSrc"
            title="微信画面仅预览;操作请在微信窗口进行"
            alt="微信窗口预览"
          />
          <p v-if="focus.state_code === 'SCREEN_LOCKED'" class="qt-danger">系统已锁屏,发送不可用</p>
        </template>

        <!-- 企点:WebCodecs canvas + 事件回注;静态预览为第三档降级 -->
        <template v-else>
          <!--
            🔴 #34 关闭码分诊(backend-api-2 §6):4503 / 4409 / 4401 / 4400 各说各的,
            不一律「连接失败」;不可重试的一律停手,不无限重连。
            ⚠️ 01 §4 还没有这块提示的元素 id(关闭码表 01 §5.1 也缺 4409/4410/4503),已列给文档方。
          -->
          <!-- R6-72:R 令牌只读。4403 后停流,点「重新连接(只看)」才重连,重连后仍只读 -->
          <div v-if="readOnly" class="banner" :data-testid="T.readonlyBanner">
            当前令牌只能观看,不能操作画面
            <a-button
              v-if="streamClosed?.reason === 'forbidden_inject'"
              size="small"
              :data-testid="T.readonlyReconnect"
              @click="startFocus"
            >重新连接(只看)</a-button>
          </div>
          <div v-else-if="streamClosed && !streamClosed.retryable" class="banner crit">
            <strong>{{ streamClosed.text }}</strong>
            <span class="qt-small qt-muted">(关闭码 {{ streamClosed.code }} · {{ streamClosed.reason }})</span>
            <p v-if="streamClosed.reason === 'stream_backend_missing'" class="qt-small qt-muted">
              画面流执行体本期未装配,已停止重连。截图与事件回注同样不可用,请改用消息页操作。
            </p>
            <p v-else-if="streamClosed.reason === 'unauthorized'" class="qt-small qt-muted">
              请用顶部横幅的「重新取令牌」恢复,再回到本页。
            </p>
          </div>
          <div v-else-if="streamClosed && streamClosed.reason === 'focus_taken'" class="banner">
            {{ streamClosed.text }}
            <a-button size="small" @click="startFocus">再试一次</a-button>
          </div>

          <!-- 第三档:静态预览(#33 每 2 s 一帧),点击/拖动松手时经 #35 REST 注入 -->
          <template v-if="isStatic">
            <img
              v-if="staticSrc"
              class="wxpreview static-preview"
            :class="{ readonly: readOnly }"
              :src="staticSrc"
              alt="静态预览"
              tabindex="0"
              draggable="false"
              :style="{ width: zoom + '%' }"
              @pointerdown="(e) => onStaticPointer(e, 'down')"
              @pointermove="(e) => onStaticPointer(e, 'move')"
              @pointerup="(e) => onStaticPointer(e, 'up')"
              @pointercancel="(e) => onStaticPointer(e, 'cancel')"
              @keydown="onKey"
            />
            <p v-else class="qt-small qt-muted">静态预览:正在取第一张截图…</p>
          </template>
          <canvas
            v-else
            ref="canvas"
            class="canvas"
            :class="{ readonly: readOnly }"
            tabindex="0"
            :data-testid="T.canvas"
            :style="{ width: zoom + '%' }"
            @pointerdown="(e) => onCanvasPointer(e, 'down')"
            @pointermove="(e) => onCanvasPointer(e, 'move')"
            @pointerup="(e) => onCanvasPointer(e, 'up')"
            @pointercancel="(e) => onCanvasPointer(e, 'cancel')"
            @lostpointercapture="(e) => onCanvasPointer(e, 'cancel')"
            @keydown="onKey"
            @wheel.prevent="onWheel"
          />
          <!-- 键盘输入:可打印字符攒在这里,回车整段发 text;控制键仍在画面上直接发 key -->
          <div v-if="imeOpen" class="qt-row ime">
            <input
              ref="imeInput"
              v-model="imeText"
              class="ime-input"
              :type="loginPhase ? 'password' : 'text'"
              autocomplete="off"
              spellcheck="false"
              placeholder="输入文字,回车发送到画面;Esc 取消"
              @keydown="onImeKey"
            />
            <a-button size="small" type="primary" @click="submitIme">发送</a-button>
            <a-button size="small" @click="closeIme">取消</a-button>
          </div>
          <div class="qt-row tools">
            <a-button
              v-for="t in SCREEN_TOOLS"
              :key="t"
              size="small"
              :disabled="readOnly && t !== 'shot'"
              :data-testid="T.tool(t)"
              @click="tool(t)"
            >
              {{ ({ back: '返回', home: '主页', shot: '截图', keyboard: '键盘输入' } as Record<string, string>)[t] }}
            </a-button>
            <a-dropdown>
              <a-button size="small" :data-testid="T.perfMenu">性能 ▾</a-button>
              <template #overlay>
                <a-menu>
                  <a-menu-item v-for="p in SCREEN_PERF_ITEMS" :key="p" :data-testid="T.perf(p)" @click="setPerf(p)">
                    {{ ({ focus30: '720p@30fps', focus15: '720p@15fps', thumb10: '540p@10fps', 'retry-hw': '重试硬解' } as Record<string, string>)[p] }}
                  </a-menu-item>
                </a-menu>
              </template>
            </a-dropdown>
            <span v-if="degradeMsg" class="tag" :data-testid="T.degradeTag">{{ degradeMsg }}</span>
          </div>
          <div class="qt-row status qt-small qt-muted">
            <span :data-testid="T.statusDecoder">
              解码 {{ stats.decoder === 'hardware' ? '硬解' : stats.decoder === 'software' ? '软解' : '静态预览' }}
              {{ stats.codec }}
            </span>
            <span :data-testid="T.statusFps">{{ stats.fps }} fps</span>
            <span :data-testid="T.statusLatency">延迟 {{ stats.latencyMs }} ms</span>
            <span :data-testid="T.statusStreamDot" class="dot"
                  :style="{ background: stats.connected ? 'var(--qt-state-running)' : 'var(--qt-state-stopped)' }" />
            <label class="zoom">缩放
              <input type="range" min="40" max="160" v-model.number="zoom" />
              {{ zoom }}%
            </label>
          </div>
        </template>
      </template>
      <a-empty v-else description="没有可显示画面的账号" />
    </main>

    <a-modal v-model:open="shotOpen" title="截图" :footer="null" :data-testid="T.shotPreview">
      <img v-if="shotUrl" :src="shotUrl" class="shot" alt="截图" />
      <!-- 二进制响应的 media_id / sha256 只在响应头里(非 JSON 响应不带 trace_id 字段) -->
      <p v-if="shotMeta.sha256" class="qt-small qt-muted qt-mono">
        media_id {{ shotMeta.mediaId ?? '—' }} · sha256 {{ shotMeta.sha256.slice(0, 16) }}…
      </p>
      <a-button type="primary" :data-testid="T.shotSave" @click="saveShot">保存</a-button>
    </a-modal>
  </div>
</template>

<style scoped>
.screen { display: flex; height: 100%; }
.thumbs { width: 260px; flex: 0 0 auto; padding: var(--qt-space-3); border-right: 1px solid var(--qt-border); overflow: auto; }
.thumb { display: flex; align-items: center; gap: var(--qt-space-2); padding: 6px; cursor: pointer; border-radius: var(--qt-radius-sm); }
.thumb:hover { background: var(--qt-bg-elevated); }
.thumb.active { background: var(--qt-bg-elevated); border: 1px solid var(--qt-primary); }
.thumb.guide { box-shadow: 0 0 0 2px var(--qt-state-login_required) inset; }
.thumb.qq { color: var(--qt-text-disabled); }
.focus { flex: 1 1 auto; padding: var(--qt-space-3); overflow: auto; }
.banner { background: #FFFBE6; color: var(--qt-sev-warn); padding: 6px var(--qt-space-3); margin: var(--qt-space-2) 0; }
.banner.crit { background: #FFF1F0; color: var(--qt-sev-crit); }
.canvas, .wxpreview {
  width: 100%; max-width: 720px; max-height: 70vh; object-fit: contain;
  background: #000; display: block; outline: none; cursor: pointer;
}
/* 触屏/笔:不让浏览器把拖动当成页面滚动或缩放吞掉 */
.canvas, .static-preview { touch-action: none; user-select: none; }
.canvas.readonly, .static-preview.readonly { cursor: default; }
.ime { margin-top: var(--qt-space-2); gap: var(--qt-space-2); }
.ime-input { flex: 1 1 auto; max-width: 480px; padding: 2px 8px; border: 1px solid var(--qt-border); border-radius: var(--qt-radius-sm); }
.zoom { display: inline-flex; align-items: center; gap: 6px; }
.tools { margin-top: var(--qt-space-2); flex-wrap: wrap; }
.status { margin-top: var(--qt-space-2); gap: var(--qt-space-3); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.tag { color: var(--qt-sev-warn); border: 1px solid var(--qt-sev-warn); border-radius: 8px; padding: 0 6px; font-size: var(--qt-font-xs); }
.shot { max-width: 100%; margin-bottom: var(--qt-space-3); }
</style>
